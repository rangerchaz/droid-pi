# 🤖 Droid Pi Client

A Raspberry Pi thin client that turns a Pi + USB webcam + speaker into an AI companion with vision, voice, memory, and personality. All the heavy lifting (LLM, TTS, STT, face recognition) happens server-side — the Pi just streams audio/video and plays responses.

**[Demo video coming soon]**

![Pi 3B running the droid client](https://img.shields.io/badge/Runs%20on-Raspberry%20Pi%203B+-red?logo=raspberrypi)

## What It Does

- 🎤 **Listens** — Streams mic audio to server for speech-to-text (Groq Whisper)
- 📷 **Sees** — Sends camera frames for vision analysis (Claude) + face recognition
- 🔊 **Speaks** — Plays TTS audio responses through USB or Bluetooth speaker
- 🧠 **Remembers** — Server-side memory system (short/medium/long-term) with relationship graphs
- 👤 **Recognizes faces** — Knows who's talking to it, greets people by name
- 🎙️ **Recognizes voices** — Speaker verification via resemblyzer d-vectors
- 😴 **Sleeps/Wakes** — Goes idle after 60s, wakes on noise detection
- 🔧 **Skills** — Timer, weather, web search, calculator, and 60+ tools via server

## Architecture

```
┌─────────────────┐         ┌──────────────────────────┐
│   Raspberry Pi   │  WSS   │     Droid Server          │
│                  │◄──────►│                            │
│  USB Webcam      │        │  Claude API (LLM)         │
│  USB Mic         │        │  Groq Whisper (STT)       │
│  USB Speaker     │        │  Edge TTS (speech)        │
│  BT Speaker (opt)│        │  Resemblyzer (voice ID)   │
│                  │        │  SQLite (memory)           │
│  ~50MB RAM used  │        │  Face recognition         │
└─────────────────┘         └──────────────────────────┘
```

The Pi is intentionally dumb. It captures audio/video, sends it over WebSocket, and plays back audio. This means:
- Any Pi 3B+ or newer works (no GPU needed)
- All API keys stay on the server
- Multiple Pis can connect to the same server
- Browser clients also work (same server, different frontend)

## Hardware

### Required
| Part | Notes | ~Cost |
|------|-------|-------|
| Raspberry Pi 3B+ or newer | 2.4GHz WiFi only on 3B | $35 |
| USB webcam with mic | Logitech BRIO (recommended) or C270 | $20-70 |
| USB speaker | Any USB audio device | $10 |
| MicroSD card | 16GB+ with Raspberry Pi OS | $8 |
| Power supply | 5V 2.5A+ (3A if using USB speaker) | $10 |

### Optional
| Part | Notes | ~Cost |
|------|-------|-------|
| Bluetooth speaker | Pairs via PulseAudio, voice-command switchable | $15+ |
| PCA9685 servo board | Pan/tilt head tracking (I2C) | $5 |
| Pan/tilt camera mount | SG90 servos + bracket | $10 |
| Powered USB hub | Prevents undervoltage with multiple USB devices | $10 |
| 3D printed body | STL files in the main droid repo | $5 filament |

> **Face tracking** uses Haar cascade detection to find faces in the camera frame and smoothly pans/tilts the head to follow. Only runs when PCA9685 is detected — no CPU overhead on GPIO fallback.

### Tested On
- Raspberry Pi 3 Model B (Debian 13 Trixie, aarch64)
- Logitech BRIO Ultra HD Webcam (video + mic, recommended)
- Logitech C270 webcam (video + mic, budget option)
- HONKYOB USB speaker (80x30x45mm pill)
- X-GO Bluetooth speaker

## Setup

### 1. Install OS
Flash **Raspberry Pi OS Lite (64-bit)** to your SD card. Boot, connect to WiFi, enable SSH.

### 2. Install Dependencies

```bash
sudo apt update && sudo apt install -y \
  python3-pip python3-opencv python3-pyaudio \
  portaudio19-dev ffmpeg bluetooth bluez \
  pulseaudio pulseaudio-module-bluetooth

pip3 install websockets
```

### 3. Configure Audio

Create `~/.asoundrc` for your USB devices. Card names survive USB reordering across reboots (card numbers don't):

```bash
# Find your card names
arecord -l  # capture devices
aplay -l    # playback devices
```

```
# ~/.asoundrc — asymmetric: playback→speaker, capture→webcam mic
pcm.!default {
    type asym
    playback.pcm "plughw:UACDemoV10"   # your USB speaker card name
    capture.pcm "plughw:BRIO"          # your webcam mic card name
}
ctl.!default {
    type hw
    card UACDemoV10
}
```

### 4. Configure Client

```bash
cp config.example.json config.json
```

Edit `config.json`:
```json
{
  "server": "wss://droid.turkeycode.ai/ws/device",
  "token": "your-device-auth-token",
  "camera_index": 0,
  "volume": 250,
  "rms_threshold": 200,
  "idle_timeout": 60
}
```

| Key | Description | Default |
|-----|-------------|---------|
| `server` | WebSocket URL to your droid server | required |
| `token` | Device auth JWT from server | required |
| `camera_index` | OpenCV camera index | `0` |
| `volume` | Playback volume (0-1000, 100 = 1x) | `250` (2.5x) |
| `rms_threshold` | Noise level to wake from sleep | `200` |
| `idle_timeout` | Seconds before sleeping | `60` |

### 5. Run It

```bash
python3 droid-client.py
```

### 6. Run as a Service (recommended)

Create `/etc/systemd/system/droid.service`:

```ini
[Unit]
Description=Droid Client
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=your-user
WorkingDirectory=/home/your-user/droid
ExecStart=/usr/bin/python3 droid-client.py
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1
Environment=XDG_RUNTIME_DIR=/run/user/1000
Environment=DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus
Environment=PULSE_SERVER=unix:/run/user/1000/pulse/native
Environment=HOME=/home/your-user

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable droid
sudo systemctl start droid
sudo journalctl -u droid -f  # watch logs
```

> **Note:** `XDG_RUNTIME_DIR`, `DBUS_SESSION_BUS_ADDRESS`, `PULSE_SERVER`, and `HOME` are all required. Without `HOME`, PyAudio can't load `~/.asoundrc` and won't find ALSA devices. Without `PULSE_SERVER`, PulseAudio connections fail.

## Features

### Sleep/Wake
The droid sleeps after `idle_timeout` seconds of no activity:
- **Awake:** Camera captures frames, mic streams to STT, vision analyzes scenes
- **Sleeping:** Camera off (light off), mic listens for noise only (RMS detection)
- **Wake triggers:** Noise above `rms_threshold`, any speech detected

### Voice Commands
| Command | Action |
|---------|--------|
| `mute` / `go to sleep` / `be quiet` | Mute mic + camera |
| `unmute` / `wake up` / `come back` | Resume everything |
| `camera off` / `stop looking` | Camera only off |
| `camera on` / `start looking` | Camera only on |
| `use bluetooth` / `party mode` | Switch to BT speaker |
| `use regular speaker` / `bluetooth off` | Switch to USB speaker |
| `volume [0-10]` | Set volume (0=silent, 10=max) |
| `turn it up` / `louder` | +1 step |
| `turn it down` / `quieter` | -1 step |
| `learn my voice` | Enroll your voice (3 samples) |

### Smart Vision
- Only sends frames during active conversation (speech within last 2 min)
- Frame diffing: skips API call if scene is >75% similar to last analyzed frame
- Idle room = zero vision API cost

### Mic Health Monitor
- Background thread checks every 10s that PyAudio callbacks are firing
- Auto-restarts the audio stream if ALSA crashes silently
- No more "mic died and nobody noticed"

### WiFi Manager (Optional)
`wifi-manager.py` handles network connectivity:
- Monitors WiFi every 30s
- If disconnected for >60s: starts AP mode (`Droid-Setup` / `droid1234`)
- Web portal at `192.168.4.1` for network configuration
- Install as separate service with `droid-wifi.service`

### Bluetooth Speaker
Pair a BT speaker, then switch with voice commands:

```bash
# Manual pairing
bluetoothctl
> scan on
> pair XX:XX:XX:XX:XX:XX
> trust XX:XX:XX:XX:XX:XX
> connect XX:XX:XX:XX:XX:XX
```

Audio routes through PulseAudio when BT is active. Persistent `pacat` process with silence feeder keeps the A2DP connection alive between speech chunks.

## Files

| File | Description |
|------|-------------|
| `droid-client.py` | Main client — camera, mic, speaker, WebSocket, sleep/wake, face tracking |
| `servo.py` | Pan/tilt servo control (PCA9685 or GPIO PWM fallback) |
| `haarcascade_frontalface_default.xml` | OpenCV face detection model (for face tracking) |
| `wifi-manager.py` | WiFi monitoring + AP fallback with config portal |
| `config.example.json` | Example configuration |
| `droid-wifi.service` | Systemd service for WiFi manager |
| `setup-wifi.sh` | Quick WiFi setup script |

## Server

This is just the Pi client. **You need an account on the droid server for it to work.**

### Getting Started
1. Sign up at **[droid.turkeycode.ai](https://droid.turkeycode.ai)**
2. Create a droid in your dashboard
3. Generate a device token from the dashboard → Hardware section
4. Put the token in your `config.json`
5. Run the client — it connects via WebSocket and you're live

### What the Server Handles
- Claude API calls (LLM + vision)
- Groq Whisper (speech-to-text)
- Edge TTS (text-to-speech)
- Resemblyzer (voice identification)
- Face recognition
- Memory (SQLite — conversations, extractions, relationship graph)
- Skills engine (60+ tools)
- Dashboard (web UI for managing droids, faces, voices, skills, billing)

### Plans
| Plan | Price | Tokens/mo | Vision/day |
|------|-------|-----------|------------|
| Trial | Free (7 days) | 500K | 100 |
| Starter | $9.99/mo | 2M | 500 |
| Pro | $24.99/mo | 8M | 2,000 |
| Ultra | $49.99/mo | 30M | 5,000 |
| BYOK | $4.99/mo | Unlimited | Unlimited |

**BYOK (Bring Your Own Key):** Use your own Claude API key. $4.99/mo covers hosting only.

## Tips

- **Pi 3B only sees 2.4GHz WiFi.** If using iPhone hotspot, enable "Maximize Compatibility."
- **Use ALSA card names, not numbers** in `.asoundrc` — numbers change when USB devices reorder on reboot.
- **Powered USB hub recommended** if running webcam + speaker from Pi USB (prevents undervoltage).
- **PCA9685 uses I2C** — enable with `sudo raspi-config` → Interface Options → I2C.
- **Volume scale:** 0-1000 internally. User-facing 0-10. `volume 5` = 500 = 5x amplification.
- **Brio mic captures mono at 16kHz** — stereo downmix was unreliable, mono is cleaner for STT.
- **Energy threshold** may need tuning per mic. Default 1500 works for Brio; C270 may need 2500.

## License

MIT
