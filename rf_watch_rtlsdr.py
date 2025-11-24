#!/usr/bin/env python3
"""
rf_watch_rtlsdr.py

RTL-SDR RF "watcher" with spoken alerts.

- Uses rtl_power to scan defined bands
- Detects bins that rise above the median noise floor
- Speaks a short text-to-speech alert when activity is detected
- Optionally tunes rtl_fm and plays audio via aplay for a short listen

Intended to be run MANUALLY while the Ettus B200 rf-watcher.service
is stopped.
"""

import os
import subprocess
import time
import statistics
import signal

# ---------------------- CONFIG ----------------------

# Bands to watch: (label, start_freq_Hz, end_freq_Hz)
BANDS = [
    ("2m Ham",           144_000_000, 148_000_000),
    ("VHF High 160–164", 160_000_000, 164_000_000),  # includes 162 MHz
    ("70cm Ham",         420_000_000, 450_000_000),
    ("FRS/GMRS",         462_000_000, 468_000_000),
    ("ADS-B 1090",     1_089_000_000, 1_091_000_000),
    ("Airband",         118_000_000, 137_000_000),
]

BIN_WIDTH_HZ = 25_000              # 25 kHz resolution in rtl_power
INTEGRATION_SEC = 0.2                # seconds per sweep
THRESHOLD_ABOVE_MEDIAN_DB = 3.5    # how far above noise a bin must be, modify how you see fit based on the area you are in and how active it is. 2.0 is too low and will trigger detections constantly.
GAIN_DB = 35.0                     # RTL-SDR tuner gain in dB (try 30-40)

# Audio behaviour
ENABLE_TTS_ALERTS = True           # speak alerts with espeak
TTS_COOLDOWN_SEC = 5               # minimum time between spoken alerts
ENABLE_AUDIO_LISTEN = False         # also listen to strongest hit with rtl_fm
LISTEN_TIME_SEC = 5               # seconds to listen on strongest hit

# Paths to external tools
RTL_POWER = "rtl_power"
RTL_FM = "rtl_fm"
APLAY = "aplay"
ESPEAK = "espeak"

# ----------------------------------------------------

current_audio_proc = None
stop_requested = False
last_tts_time = 0.0


def handle_sigint(signum, frame):
    """Clean shutdown on Ctrl-C."""
    global stop_requested, current_audio_proc
    print("\n[!] Ctrl-C received, stopping...")
    stop_requested = True
    if current_audio_proc and current_audio_proc.poll() is None:
        current_audio_proc.terminate()
        try:
            current_audio_proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            current_audio_proc.kill()


signal.signal(signal.SIGINT, handle_sigint)


def hz_to_mhz_str(freq_hz: float) -> str:
    return f"{freq_hz / 1e6:.6f}M"


def speak(message: str):
    """
    Fire-and-forget text-to-speech using espeak.
    Runs asynchronously so we don't block scanning.
    """
    global last_tts_time
    now = time.time()
    if not ENABLE_TTS_ALERTS:
        return
    if now - last_tts_time < TTS_COOLDOWN_SEC:
        return

    last_tts_time = now
    print(f"[TTS] {message}")
    try:
        subprocess.Popen(
            [ESPEAK, "-s", "170", "-a", "200", message],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        print("[!] espeak not found. Install with: sudo apt install espeak")


def scan_band(name: str, f_start_hz: int, f_end_hz: int):
    """
    Use rtl_power to scan a single band.

    Returns:
        freqs_hz: list of bin center frequencies (Hz)
        powers_db: list of powers (dB)
    """
    bin_width_khz = int(BIN_WIDTH_HZ / 1000)
    start_mhz = f_start_hz / 1e6
    end_mhz = f_end_hz / 1e6

    freq_spec = f"{start_mhz:.6f}M:{end_mhz:.6f}M:{bin_width_khz}k"

    cmd = [
        RTL_POWER,
        "-f", freq_spec,
        "-i", str(INTEGRATION_SEC),
        "-1",            # single sweep only
        "-c", "0",       # disable compression
        "-g", str(GAIN_DB), # fixed tuner gain
    ]

    print(f"\n[+] Scanning {name}: {freq_spec}")

    try:
        out = subprocess.check_output(cmd, text=True, stderr=subprocess.STDOUT)
    except subprocess.CalledProcessError as e:
        print(f"[!] rtl_power error on band {name}:")
        print(e.output)
        return [], []

    data_parts = []

    # rtl_power output is CSV-like; we only keep real CSV lines
    for line in out.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "," not in line:
            # status lines like "User cancel, exiting..." -> ignore
            continue
        parts = line.split(",")
        if len(parts) >= 8:
            data_parts.append(parts)

    if not data_parts:
        print("[!] No usable CSV data returned from rtl_power")
        return [], []

    # use the last CSV data line
    parts = data_parts[-1]

    try:
        freq_start = float(parts[2])     # Hz
        bin_hz     = float(parts[4])     # Hz
    except ValueError:
        print("[!] Failed to parse header fields from rtl_power line")
        return [], []

    power_values_str = parts[6:]  # power bins in dB
    freqs = []
    powers = []

    for i, p_str in enumerate(power_values_str):
        p_str = p_str.strip()
        if not p_str:
            continue
        try:
            p_db = float(p_str)
        except ValueError:
            continue
        freq = freq_start + i * bin_hz
        freqs.append(freq)
        powers.append(p_db)

    return freqs, powers


def detect_peaks(freqs, powers):
    """
    Return list of (freq_hz, power_db) where power is above noise threshold.
    """
    if not powers:
        return []

    median_noise = statistics.median(powers)
    max_p = max(powers)
    cutoff = median_noise + THRESHOLD_ABOVE_MEDIAN_DB
    print(f"    median={median_noise:.1f} dB, max={max_p:.1f} dB, cutoff={cutoff:.1f} dB")

    hits = []
    for f, p in zip(freqs, powers):
        if p >= cutoff:
            hits.append((f, p))

    return hits


def listen_to_frequency(freq_hz: float, seconds: int = LISTEN_TIME_SEC):
    """
    Pipe rtl_fm audio into aplay for a short listen on the strongest hit.
    Ensures the entire process group (shell + rtl_fm + aplay) is cleaned up.
    """
    global current_audio_proc

    freq_mhz_str = hz_to_mhz_str(freq_hz)
    print(f"[🎧] Listening on {freq_mhz_str} for ~{seconds} s")

    # Kill any previous pipeline (shell + children)
    if current_audio_proc and current_audio_proc.poll() is None:
        try:
            os.killpg(current_audio_proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass

    # Run the pipeline in its own process group so we can kill all children
    cmd = (
        f"{RTL_FM} -f {freq_mhz_str} -M fm -s 12000 -r 48000 -l 0 "
        f"| {APLAY} -r 48000 -f S16_LE -t raw"
    )

    current_audio_proc = subprocess.Popen(
        ["/bin/bash", "-c", cmd],
        preexec_fn=os.setsid,  # new process group
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    t0 = time.time()
    try:
        while time.time() - t0 < seconds:
            if stop_requested:
                break
            if current_audio_proc.poll() is not None:
                # pipeline exited early
                break
            time.sleep(0.2)
    finally:
        if current_audio_proc and current_audio_proc.poll() is None:
            try:
                os.killpg(current_audio_proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

def main():
    print("=== RTL-SDR RF Watcher (with TTS) ===")
    speak("RTL S D R watcher started.")
    print("Bands configured:")
    for name, f1, f2 in BANDS:
        print(f"  - {name}: {f1/1e6:.3f}–{f2/1e6:.3f} MHz")
    print(f"\nBin width: {BIN_WIDTH_HZ/1000:.1f} kHz")
    print(f"Threshold: +{THRESHOLD_ABOVE_MEDIAN_DB} dB over median noise")
    print(f"TTS alerts: {'enabled' if ENABLE_TTS_ALERTS else 'disabled'}")
    print(f"Audio listening: {'enabled' if ENABLE_AUDIO_LISTEN else 'disabled'}")
    print("Press Ctrl-C to quit.\n")

    while not stop_requested:
        for band_name, f_start, f_end in BANDS:
            if stop_requested:
                break

            freqs, powers = scan_band(band_name, f_start, f_end)
            hits = detect_peaks(freqs, powers)

            if not hits:
                print("    No significant activity detected.")
                continue

            # Sort hits by power, strongest first
            hits.sort(key=lambda x: x[1], reverse=True)

            print(f"[!] Activity detected in {band_name}:")
            for f, p in hits[:5]:
                print(f"    ~{f/1e6:.6f} MHz at {p:.1f} dB")

            # Speak a short alert about the strongest hit
            strongest_freq, strongest_power = hits[0]
            mhz = strongest_freq / 1e6
            speak(f"Activity detected. {band_name}. {mhz:.3f} megahertz.")

            # For ADS-B we usually don't want to listen (it's just data bursts)
            # if ENABLE_AUDIO_LISTEN and "ADS-B" not in band_name:
            #   listen_to_frequency(strongest_freq, LISTEN_TIME_SEC)


        time.sleep(0.5)

    print("[*] RTL-SDR RF watcher stopped.")


if __name__ == "__main__":
    main()

