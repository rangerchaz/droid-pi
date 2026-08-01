"""Config loading + tunable constants. Read once at import time."""
import json
import os
import sys

import pyaudio

CLIENT_VERSION = '1.0.0'

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'config.json')

if not os.path.exists(CONFIG_PATH):
    print("ERROR: config.json not found. Copy config.example.json to config.json and edit it.")
    sys.exit(1)

with open(CONFIG_PATH) as f:
    _config = json.load(f)


def _g(key, default=None):
    return _config.get(key, default)


# User-facing config
SERVER = 'wss://meckie.ai/ws/device'
TOKEN = _g('token', '')
CAMERA_INDEX = _g('camera_index', 0)
VOLUME = _g('volume', 250)

# Internal constants — not user-configurable
SAMPLE_RATE = 16000
FRAME_INTERVAL = 3.0
AUDIO_CHUNK_MS = 500
JPEG_QUALITY = 60

# Sleep / wake
IDLE_TIMEOUT = _g('idle_timeout', 30)
MOTION_THRESHOLD = _g('motion_threshold', 5)
MOTION_PIXEL_PCT = _g('motion_pixel_pct', 0.5)
RMS_THRESHOLD = _g('rms_threshold', 500)

# Software mic gain applied to captured samples before anything reads them.
# Lav mics with 0dB-max USB interfaces (Saramonic LavMicro-U) capture ~10dB
# below what the VAD/STT chain was tuned for; 3.0 ≈ +9.5dB.
MIC_GAIN = float(_g('mic_gain', 1.0))

# High-pass cutoff in Hz (0 = off). Kills power-supply ground hum (40-140Hz)
# that noisy USB chargers inject into USB mic ADCs; speech formants sit above.
MIC_HIGHPASS_HZ = float(_g('mic_highpass_hz', 0))
WAKE_DEBOUNCE = _g('wake_debounce', 0.5)
SLEEP_ENABLED = _g('sleep_enabled', True)

# Audio
CHANNELS = 1       # output channels (server expects mono)
MIC_CHANNELS = 1   # capture mono
FORMAT = pyaudio.paInt16
CHUNK = int(SAMPLE_RATE * AUDIO_CHUNK_MS / 1000)
