#!/usr/bin/env python3
"""
Droid Pi Client — thin client that streams camera + mic to droid server,
plays audio responses through speaker. All AI runs server-side.
"""

import asyncio
import json
import base64
import sys
import os
import signal
import time
import threading
import wave
import tempfile
import subprocess

try:
    import cv2
except ImportError:
    print("ERROR: opencv not installed. Run: sudo apt install python3-opencv")
    sys.exit(1)

try:
    import pyaudio
except ImportError:
    print("ERROR: pyaudio not installed. Run: sudo apt install python3-pyaudio portaudio19-dev")
    sys.exit(1)

try:
    import websockets
except ImportError:
    print("ERROR: websockets not installed. Run: pip3 install websockets")
    sys.exit(1)


# Load config
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.json')
if not os.path.exists(CONFIG_PATH):
    print("ERROR: config.json not found. Copy config.example.json to config.json and edit it.")
    sys.exit(1)

with open(CONFIG_PATH) as f:
    config = json.load(f)

SERVER = config.get('server', 'wss://droid.turkeycode.ai/ws/device')
TOKEN = config.get('token', '')
CAMERA_INDEX = config.get('camera_index', 0)
SAMPLE_RATE = config.get('sample_rate', 16000)
FRAME_INTERVAL = config.get('frame_interval', 2.0)
AUDIO_CHUNK_MS = config.get('audio_chunk_ms', 500)
JPEG_QUALITY = config.get('jpeg_quality', 60)

# Audio config
CHANNELS = 1
FORMAT = pyaudio.paInt16
CHUNK = int(SAMPLE_RATE * AUDIO_CHUNK_MS / 1000)

# State
running = True
ws_connection = None
is_speaking = False  # True while playing TTS — mute mic to prevent echo


def signal_handler(sig, frame):
    global running
    print("\nShutting down...")
    running = False

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)


class Camera:
    def __init__(self, index=0):
        self.cap = cv2.VideoCapture(index)
        if not self.cap.isOpened():
            raise RuntimeError(f"Cannot open camera {index}")
        # Lower resolution for bandwidth
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
        print(f"[Camera] Opened camera {index}")

    def capture_jpeg(self):
        ret, frame = self.cap.read()
        if not ret:
            return None
        _, jpeg = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
        return jpeg.tobytes()

    def close(self):
        self.cap.release()


class Microphone:
    def __init__(self):
        self.pa = pyaudio.PyAudio()
        self.stream = None
        self.buffer = []
        self.lock = threading.Lock()

    def start(self):
        self.stream = self.pa.open(
            format=FORMAT,
            channels=CHANNELS,
            rate=SAMPLE_RATE,
            input=True,
            frames_per_buffer=CHUNK,
            stream_callback=self._callback
        )
        print("[Mic] Listening")

    def _callback(self, data, frame_count, time_info, status):
        if not is_speaking:  # Mute during TTS playback
            with self.lock:
                self.buffer.append(data)
        return (None, pyaudio.paContinue)

    def get_audio(self):
        with self.lock:
            if not self.buffer:
                return None
            data = b''.join(self.buffer)
            self.buffer.clear()
            return data

    def close(self):
        if self.stream:
            self.stream.stop_stream()
            self.stream.close()
        self.pa.terminate()


class Speaker:
    def __init__(self):
        self.pa = pyaudio.PyAudio()
        self.lock = threading.Lock()

    def play_audio(self, audio_bytes, audio_format='mp3'):
        """Play audio bytes through speaker. Handles mp3 and wav."""
        global is_speaking
        is_speaking = True

        try:
            if audio_format == 'wav':
                self._play_wav(audio_bytes)
            else:
                # Decode mp3 to wav using ffmpeg
                self._play_mp3(audio_bytes)
        except Exception as e:
            print(f"[Speaker] Error: {e}")
        finally:
            is_speaking = False

    def _play_wav(self, data):
        with tempfile.NamedTemporaryFile(suffix='.wav', delete=True) as f:
            f.write(data)
            f.flush()
            wf = wave.open(f.name, 'rb')
            stream = self.pa.open(
                format=self.pa.get_format_from_width(wf.getsampwidth()),
                channels=wf.getnchannels(),
                rate=wf.getframerate(),
                output=True
            )
            chunk = 1024
            out = wf.readframes(chunk)
            while out and running:
                stream.write(out)
                out = wf.readframes(chunk)
            stream.stop_stream()
            stream.close()
            wf.close()

    def _play_mp3(self, data):
        """Decode mp3 with ffmpeg and play raw PCM."""
        proc = subprocess.Popen(
            ['ffmpeg', '-i', 'pipe:0', '-f', 'wav', '-acodec', 'pcm_s16le',
             '-ar', '24000', '-ac', '1', 'pipe:1'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        )
        wav_data, _ = proc.communicate(input=data)
        if wav_data and len(wav_data) > 44:  # Skip WAV header
            stream = self.pa.open(format=pyaudio.paInt16, channels=1, rate=24000, output=True)
            stream.write(wav_data[44:])
            stream.stop_stream()
            stream.close()

    def close(self):
        self.pa.terminate()


async def run():
    global ws_connection

    print(f"[Droid] Connecting to {SERVER}")

    camera = Camera(CAMERA_INDEX)
    mic = Microphone()
    speaker = Speaker()
    mic.start()

    url = SERVER
    if TOKEN:
        url += ('&' if '?' in url else '?') + f'token={TOKEN}'

    reconnect_delay = 1

    while running:
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=10,
                                          max_size=10 * 1024 * 1024) as ws:
                ws_connection = ws
                reconnect_delay = 1
                print("[Droid] Connected!")

                # Send device info
                await ws.send(json.dumps({
                    'type': 'device_info',
                    'platform': 'raspberry_pi',
                    'model': 'Pi 3 Model B',
                    'capabilities': ['camera', 'microphone', 'speaker']
                }))

                # Start concurrent tasks
                await asyncio.gather(
                    send_frames(ws, camera),
                    send_audio(ws, mic),
                    receive_messages(ws, speaker),
                    return_exceptions=True
                )

        except (websockets.ConnectionClosed, ConnectionRefusedError, OSError) as e:
            print(f"[Droid] Disconnected: {e}. Reconnecting in {reconnect_delay}s...")
            await asyncio.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, 30)
        except Exception as e:
            print(f"[Droid] Error: {e}")
            await asyncio.sleep(2)

    camera.close()
    mic.close()
    speaker.close()
    print("[Droid] Shutdown complete")


async def send_frames(ws, camera):
    """Send camera frames at configured interval."""
    while running:
        jpeg = camera.capture_jpeg()
        if jpeg:
            msg = json.dumps({
                'type': 'frame',
                'data': base64.b64encode(jpeg).decode('ascii'),
                'timestamp': time.time()
            })
            await ws.send(msg)
        await asyncio.sleep(FRAME_INTERVAL)


async def send_audio(ws, mic):
    """Send mic audio chunks to server for STT."""
    while running:
        audio = mic.get_audio()
        if audio:
            msg = json.dumps({
                'type': 'audio',
                'data': base64.b64encode(audio).decode('ascii'),
                'sample_rate': SAMPLE_RATE,
                'channels': CHANNELS,
                'format': 'pcm_s16le'
            })
            await ws.send(msg)
        await asyncio.sleep(0.1)


async def receive_messages(ws, speaker):
    """Receive and handle messages from server."""
    async for message in ws:
        try:
            msg = json.loads(message)
        except json.JSONDecodeError:
            continue

        msg_type = msg.get('type', '')

        if msg_type == 'speak':
            # TTS audio to play
            audio_b64 = msg.get('audio', '')
            audio_format = msg.get('format', 'mp3')
            if audio_b64:
                audio_bytes = base64.b64decode(audio_b64)
                text = msg.get('text', '')
                if text:
                    print(f"[Droid] \"{text}\"")
                # Play in thread to not block receive loop
                threading.Thread(
                    target=speaker.play_audio,
                    args=(audio_bytes, audio_format),
                    daemon=True
                ).start()

        elif msg_type == 'text':
            # Text-only response (no TTS)
            print(f"[Droid] {msg.get('text', '')}")

        elif msg_type == 'status':
            print(f"[Status] {msg.get('message', '')}")

        elif msg_type == 'error':
            print(f"[Error] {msg.get('message', '')}")

        elif msg_type == 'vision_processed':
            # Server processed a frame, may include next look timing
            next_ms = msg.get('nextLookMs', 2000)
            # Frame interval is handled server-side via this response

        elif msg_type == 'ping':
            await ws.send(json.dumps({'type': 'pong'}))


if __name__ == '__main__':
    print("=" * 40)
    print("  DROID Pi Client")
    print("  Camera + Mic → Server → Speaker")
    print("=" * 40)
    asyncio.run(run())
