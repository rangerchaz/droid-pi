# Droid Pi Client

Thin client for Raspberry Pi 3B+. Streams camera + mic to your droid server, plays audio responses through speaker.

## Hardware
- Raspberry Pi 3 Model B (or newer)
- USB webcam with mic
- Speaker via 3.5mm jack

## Setup
```bash
# Install dependencies
sudo apt update
sudo apt install -y python3-pip python3-opencv python3-pyaudio portaudio19-dev ffmpeg

pip3 install websockets

# Configure
cp config.example.json config.json
# Edit config.json with your droid server URL and auth token

# Run
python3 droid-client.py
```

## Config
```json
{
  "server": "wss://droid.turkeycode.ai/ws/device",
  "token": "your-auth-token",
  "camera_index": 0,
  "sample_rate": 16000,
  "frame_interval": 2.0,
  "audio_chunk_ms": 500
}
```
