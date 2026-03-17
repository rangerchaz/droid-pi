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
    print("ERROR: websockets not installed. Run: pip3 install websockets --break-system-packages")
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
FRAME_INTERVAL = config.get('frame_interval', 3.0)
AUDIO_CHUNK_MS = config.get('audio_chunk_ms', 500)
JPEG_QUALITY = config.get('jpeg_quality', 60)

# Audio config
CHANNELS = 1
FORMAT = pyaudio.paInt16
CHUNK = int(SAMPLE_RATE * AUDIO_CHUNK_MS / 1000)

# State
running = True
is_speaking = False


def signal_handler(sig, frame):
    global running
    if not running:
        # Second Ctrl+C = force exit
        print("\nForce quit.")
        os._exit(0)
    print("\nShutting down...")
    running = False

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)


class Camera:
    def __init__(self, index=0):
        self.cap = cv2.VideoCapture(index)
        if not self.cap.isOpened():
            raise RuntimeError(f"Cannot open camera {index}")
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
        if not is_speaking:
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
        self.lock = threading.Lock()

    def play_audio(self, audio_bytes, audio_format='mp3'):
        global is_speaking

        with self.lock:
            is_speaking = True
            try:
                # Kill any stuck audio processes first
                subprocess.run(['killall', '-q', 'aplay'], stderr=subprocess.DEVNULL)

                # Decode with ffmpeg
                proc = subprocess.Popen(
                    ['ffmpeg', '-i', 'pipe:0', '-f', 'wav', '-acodec', 'pcm_s16le',
                     '-ar', '24000', '-ac', '1', 'pipe:1'],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
                )
                wav_data, _ = proc.communicate(input=audio_bytes, timeout=30)

                if wav_data and len(wav_data) > 44:
                    play = subprocess.Popen(
                        ['aplay', '-f', 'S16_LE', '-r', '24000', '-c', '1', '-'],
                        stdin=subprocess.PIPE, stderr=subprocess.DEVNULL
                    )
                    play.communicate(input=wav_data[44:], timeout=60)
            except subprocess.TimeoutExpired:
                print("[Speaker] Timeout — killing audio")
                try:
                    proc.kill()
                except:
                    pass
                subprocess.run(['killall', '-q', 'aplay', 'ffmpeg'], stderr=subprocess.DEVNULL)
            except Exception as e:
                print(f"[Speaker] Error: {e}")
            finally:
                is_speaking = False

    def keep_alive(self):
        """Play silence to prevent audio device from sleeping."""
        try:
            # 0.1s of silence at 24kHz 16-bit mono
            silence = b'\x00' * 4800
            proc = subprocess.Popen(
                ['aplay', '-f', 'S16_LE', '-r', '24000', '-c', '1', '-q', '-'],
                stdin=subprocess.PIPE, stderr=subprocess.DEVNULL
            )
            proc.communicate(input=silence, timeout=2)
        except:
            pass

    def close(self):
        pass


async def run():
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
            async with websockets.connect(url, ping_interval=30, ping_timeout=15,
                                          max_size=10 * 1024 * 1024) as ws:
                reconnect_delay = 1
                print("[Droid] Connected!")
                connected = True

                # Send device info
                await ws.send(json.dumps({
                    'type': 'device_info',
                    'platform': 'raspberry_pi',
                    'model': 'Pi 3 Model B',
                    'capabilities': ['camera', 'microphone', 'speaker']
                }))

                last_frame_time = 0
                last_keepalive = 0

                while running and connected:
                    try:
                        # Check for incoming messages (non-blocking, short timeout)
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=0.2)
                            msg = json.loads(raw)
                            msg_type = msg.get('type', '')

                            if msg_type == 'speak':
                                audio_b64 = msg.get('audio', '')
                                if audio_b64:
                                    audio_bytes = base64.b64decode(audio_b64)
                                    text = msg.get('text', '')
                                    if text:
                                        print(f'[Droid] "{text}"')
                                    threading.Thread(
                                        target=speaker.play_audio,
                                        args=(audio_bytes, msg.get('format', 'mp3')),
                                        daemon=True
                                    ).start()

                            elif msg_type == 'text':
                                print(f"[Droid] {msg.get('text', '')}")

                            elif msg_type == 'status':
                                print(f"[Status] {msg.get('message', '')}")

                            elif msg_type == 'error':
                                print(f"[Error] {msg.get('message', '')}")

                            elif msg_type == 'ping':
                                await ws.send(json.dumps({'type': 'pong'}))

                        except asyncio.TimeoutError:
                            pass  # No message, that's fine

                        # Send camera frame if interval elapsed
                        now = time.time()
                        if now - last_frame_time >= FRAME_INTERVAL:
                            jpeg = camera.capture_jpeg()
                            if jpeg:
                                await ws.send(json.dumps({
                                    'type': 'frame',
                                    'data': base64.b64encode(jpeg).decode('ascii'),
                                    'timestamp': now
                                }))
                                last_frame_time = now

                        # Keep audio device alive (every 10s)
                        if now - last_keepalive > 10:
                            last_keepalive = now
                            if not is_speaking:
                                threading.Thread(target=speaker.keep_alive, daemon=True).start()

                        # Send buffered audio
                        audio = mic.get_audio()
                        if audio:
                            await ws.send(json.dumps({
                                'type': 'audio',
                                'data': base64.b64encode(audio).decode('ascii'),
                                'sample_rate': SAMPLE_RATE,
                                'channels': CHANNELS,
                                'format': 'pcm_s16le'
                            }))

                    except websockets.ConnectionClosed:
                        connected = False

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


if __name__ == '__main__':
    print("=" * 40)
    print("  DROID Pi Client")
    print("  Camera + Mic -> Server -> Speaker")
    print("=" * 40)
    asyncio.run(run())
