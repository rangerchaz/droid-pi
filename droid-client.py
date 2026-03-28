#!/usr/bin/env python3
"""
Droid Pi Client — thin client that streams camera + mic to droid server,
plays audio responses through speaker. All AI runs server-side.

Sleep/Wake:
  AWAKE + camera on → motion detection via frame differencing
  AWAKE + no motion for idle_timeout → SLEEPING (camera stops, audio stops)
  SLEEPING → noise above threshold for wake_debounce → AWAKE
  Any server 'wake' message or noise → AWAKE
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
import struct
import math

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

# User config
SERVER = 'wss://droid.turkeycode.ai/ws/device'
TOKEN = config.get('token', '')
CAMERA_INDEX = config.get('camera_index', 0)
VOLUME = config.get('volume', 250)

# Internal constants — not user-configurable
SAMPLE_RATE = 16000
FRAME_INTERVAL = 3.0
AUDIO_CHUNK_MS = 500
JPEG_QUALITY = 60

# Sleep/Wake config
IDLE_TIMEOUT = config.get('idle_timeout', 30)         # seconds without motion → sleep
MOTION_THRESHOLD = config.get('motion_threshold', 5)    # pixel diff threshold (0-255)
MOTION_PIXEL_PCT = config.get('motion_pixel_pct', 0.5)  # % pixels changed to count as motion
RMS_THRESHOLD = config.get('rms_threshold', 500)         # RMS level to wake (16-bit PCM scale)
WAKE_DEBOUNCE = config.get('wake_debounce', 0.5)         # seconds of noise to wake
SLEEP_ENABLED = config.get('sleep_enabled', True)

# Audio config
CHANNELS = 1       # Output channels (server expects mono)
MIC_CHANNELS = 1   # Capture mono (simpler, no downmix needed)
FORMAT = pyaudio.paInt16
CHUNK = int(SAMPLE_RATE * AUDIO_CHUNK_MS / 1000)

# State
running = True
is_speaking = False
sleep_state = 'awake'  # 'awake' | 'sleeping'
boot_time = time.time()  # Don't auto-sleep for first 120s after boot
last_motion_time = time.time()
noise_start_time = None
prev_frame_gray = None


def signal_handler(sig, frame):
    global running
    if not running:
        print("\nForce quit.")
        os._exit(0)
    print("\nShutting down...")
    running = False

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)


def compute_rms(pcm_data):
    """Compute RMS energy of 16-bit PCM audio."""
    if len(pcm_data) < 2:
        return 0
    count = len(pcm_data) // 2
    fmt = f'<{count}h'
    try:
        samples = struct.unpack(fmt, pcm_data[:count * 2])
    except struct.error:
        return 0
    if not samples:
        return 0
    sum_sq = sum(s * s for s in samples)
    return math.sqrt(sum_sq / count)


def detect_motion(frame, threshold=MOTION_THRESHOLD, pct=MOTION_PIXEL_PCT):
    """Compare current frame to previous, return True if motion detected."""
    global prev_frame_gray

    # Convert to small grayscale
    small = cv2.resize(frame, (160, 120))
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)

    if prev_frame_gray is None:
        prev_frame_gray = gray
        return False

    diff = cv2.absdiff(gray, prev_frame_gray)
    prev_frame_gray = gray

    changed = (diff > threshold).sum()
    total = 160 * 120
    percent = (changed / total) * 100

    return percent >= pct


def do_sleep(ws_send_queue):
    """Transition to sleep state."""
    global sleep_state, camera
    if sleep_state == 'sleeping':
        return
    sleep_state = 'sleeping'
    camera.disable()
    servo_controller.center()  # Look forward when sleeping
    motion_tracker.prev_gray = None  # Reset so wake doesn't trigger false motion
    print("[Sleep] 💤 Sleeping — no activity for", IDLE_TIMEOUT, "seconds")
    ws_send_queue.append(json.dumps({'type': 'sleep_state', 'state': 'sleeping'}))


def do_wake(reason, ws_send_queue):
    """Transition to awake state."""
    global sleep_state, last_motion_time, noise_start_time, prev_frame_gray, camera
    if sleep_state == 'awake':
        return
    sleep_state = 'awake'
    last_motion_time = time.time()
    noise_start_time = None
    prev_frame_gray = None
    camera.enable()
    print(f"[Sleep] ☀️ Waking — reason: {reason}")
    ws_send_queue.append(json.dumps({'type': 'sleep_state', 'state': 'awake'}))


class Camera:
    def __init__(self, index=0):
        self.index = index
        self.cap = None
        self.enabled = False
        self._open_retries = 0
        self._open()

    def _open(self):
        """Try to open camera. Don't crash if it fails — retry later."""
        try:
            # Auto-detect Brio by trying configured index, then scanning
            for idx in [self.index, 0, 1, 2]:
                cap = cv2.VideoCapture(idx)
                if cap.isOpened():
                    self.cap = cap
                    if idx != self.index:
                        print(f"[Camera] Found camera at index {idx} (configured: {self.index})")
                        self.index = idx
                    break
                cap.release()
            else:
                print(f"[Camera] WARNING: Cannot open any camera — will retry")
                self.cap = None
                self.enabled = False
                return False
            if not self.cap.isOpened():
                print(f"[Camera] WARNING: Cannot open camera {self.index} — will retry")
                self.cap = None
                self.enabled = False
                return False
            self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
            self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
            self.enabled = True
            self._open_retries = 0
            print(f"[Camera] Opened camera {self.index}")
            return True
        except Exception as e:
            print(f"[Camera] ERROR opening camera: {e}")
            self.cap = None
            self.enabled = False
            return False

    def disable(self):
        self.enabled = False
        if self.cap and self.cap.isOpened():
            self.cap.release()
            print("[Camera] Released — light off")

    def enable(self):
        if self.cap and self.cap.isOpened():
            self.enabled = True
            print("[Camera] Already open")
            return
        self._open()

    def capture_frame(self):
        """Return raw frame (for motion detection) and JPEG bytes."""
        if not self.enabled:
            return None, None
        ret, frame = self.cap.read()
        if not ret:
            return None, None
        _, jpeg = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
        return frame, jpeg.tobytes()

    def close(self):
        self.cap.release()


# --- Face tracker (Haar cascade, runs on Pi, no API cost) ---
class FaceTracker:
    def __init__(self):
        # Find haar cascade — try cv2.data, then local copy
        cascade_path = None
        try:
            p = cv2.data.haarcascades + 'haarcascade_frontalface_default.xml'
            if os.path.exists(p):
                cascade_path = p
        except (AttributeError, TypeError):
            pass
        if not cascade_path:
            # Local copy next to script
            p = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'haarcascade_frontalface_default.xml')
            if os.path.exists(p):
                cascade_path = p
        if not cascade_path:
            # Working directory
            p = os.path.join(os.getcwd(), 'haarcascade_frontalface_default.xml')
            if os.path.exists(p):
                cascade_path = p
        if cascade_path:
            print(f'[FaceTracker] Cascade: {cascade_path}')
        else:
            print('[FaceTracker] WARNING: No cascade file found — face tracking disabled')
        self.cascade = cv2.CascadeClassifier(cascade_path or '')
        self.last_face = None  # (x, y, w, h) of last detected face
        self.frames_without_face = 0
        self._enabled = True
        print("[FaceTracker] Initialized")

    def detect(self, frame):
        """Detect largest face in frame. Returns (center_x, center_y, w, h) or None."""
        if not self._enabled:
            return None
        try:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            # Histogram equalization — critical for low-light face detection
            gray = cv2.equalizeHist(gray)
            # Scale down for speed on Pi 3B
            small = cv2.resize(gray, (320, 240))
            scale_x = frame.shape[1] / 320
            scale_y = frame.shape[0] / 240
            faces = self.cascade.detectMultiScale(small, 1.1, 3, minSize=(20, 20))
            if self.frames_without_face % 30 == 0 and self.frames_without_face > 0:
                print(f'[FaceTracker] No face for {self.frames_without_face} frames')
            if len(faces) > 0:
                if self.frames_without_face > 5:
                    print(f'[FaceTracker] Found face! ({len(faces)} detected)')
                # Largest face
                x, y, w, h = max(faces, key=lambda f: f[2] * f[3])
                # Scale back to original resolution
                cx = int((x + w / 2) * scale_x)
                cy = int((y + h / 2) * scale_y)
                self.last_face = (cx, cy, int(w * scale_x), int(h * scale_y))
                self.frames_without_face = 0
                return self.last_face
            else:
                self.frames_without_face += 1
                return None
        except Exception as e:
            return None


class MotionTracker:
    """Track motion centroid via frame differencing — works in any lighting."""
    def __init__(self):
        self.prev_gray = None
        self.last_centroid = None  # (cx, cy) of motion center
        self.frames_without_motion = 0

    def detect(self, frame):
        """Returns (center_x, center_y) of motion, or None."""
        try:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            gray = cv2.GaussianBlur(gray, (21, 21), 0)
            small = cv2.resize(gray, (160, 120))

            if self.prev_gray is None:
                self.prev_gray = small
                return None

            # Frame difference
            diff = cv2.absdiff(self.prev_gray, small)
            self.prev_gray = small

            # Threshold — pixels that changed significantly
            _, thresh = cv2.threshold(diff, 25, 255, cv2.THRESH_BINARY)

            # Find contours of motion regions
            contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

            if not contours:
                self.frames_without_motion += 1
                return None

            # Filter small noise (< 3% of frame area)
            min_area = (160 * 120) * 0.03
            big_contours = [c for c in contours if cv2.contourArea(c) > min_area]

            if not big_contours:
                self.frames_without_motion += 1
                return None

            # Largest motion region
            largest = max(big_contours, key=cv2.contourArea)
            M = cv2.moments(largest)
            if M['m00'] == 0:
                return None

            # Scale back to original frame coords
            scale_x = frame.shape[1] / 160
            scale_y = frame.shape[0] / 120
            cx = int((M['m10'] / M['m00']) * scale_x)
            cy = int((M['m01'] / M['m00']) * scale_y)

            self.last_centroid = (cx, cy)
            self.frames_without_motion = 0
            return (cx, cy)
        except Exception:
            return None


class Microphone:
    def __init__(self):
        # Retry PyAudio init — ALSA devices may not be ready immediately
        for attempt in range(10):
            self.pa = pyaudio.PyAudio()
            self._device_index = self._find_capture_device()
            if self._device_index is not None:
                break
            print(f'[Mic] No capture device found, retrying in 3s... ({attempt+1}/10)')
            self.pa.terminate()
            time.sleep(3)
        self.stream = None
        self.buffer = []
        self.lock = threading.Lock()
        self.enabled = True
        self.last_callback_time = time.time()
        self._health_thread = None

    def _find_capture_device(self):
        """Find the USB webcam mic — try all PyAudio devices with input channels."""
        import re
        # Find ALSA card number from /proc/asound/cards
        try:
            with open('/proc/asound/cards') as f:
                for line in f:
                    m = re.match(r'^\s*(\d+)\s+\[(\w+)', line)
                    if m and any(k in line.lower() for k in ['logitech', 'brio', '046d', 'c260', 'c270', '0x46d', 'usb device 0x46d']):
                        card_num = int(m.group(1))
                        card_name = m.group(2)
                        print(f'[Mic] Found ALSA card {card_num}: {card_name}')
                        # Search all PyAudio devices for this card
                        for i in range(self.pa.get_device_count()):
                            try:
                                d = self.pa.get_device_info_by_index(i)
                                if d['maxInputChannels'] > 0:
                                    name = d.get('name', '')
                                    if f'hw:{card_num}' in name or card_name.lower() in name.lower():
                                        print(f'[Mic] Using device {i}: {name}')
                                        return i
                            except:
                                continue
                        # No hw match — try any non-PulseAudio device with input channels
                        for i in range(self.pa.get_device_count()):
                            try:
                                d = self.pa.get_device_info_by_index(i)
                                if d['maxInputChannels'] > 0 and 'pulse' not in d.get('name', '').lower():
                                    print(f'[Mic] Fallback device {i}: {d["name"]}')
                                    return i
                            except:
                                continue
        except Exception as e:
            print(f'[Mic] Card scan error: {e}')
        print('[Mic] WARNING: No mic found, using default')
        return None

    def start(self):
        try:
            if self.stream:
                try:
                    self.stream.stop_stream()
                    self.stream.close()
                except:
                    pass
            kwargs = dict(
                format=FORMAT,
                channels=MIC_CHANNELS,
                rate=SAMPLE_RATE,
                input=True,
                frames_per_buffer=CHUNK,
                stream_callback=self._callback
            )
            if self._device_index is not None:
                kwargs['input_device_index'] = self._device_index
            self.stream = self.pa.open(**kwargs)
            self.last_callback_time = time.time()
            self._actual_channels = MIC_CHANNELS
            print(f"[Mic] Listening (stereo={MIC_CHANNELS==2})")
            # Start health monitor if not running
            if not self._health_thread or not self._health_thread.is_alive():
                self._health_thread = threading.Thread(target=self._health_monitor, daemon=True)
                self._health_thread.start()
        except Exception as e:
            print(f"[Mic] ERROR starting stream: {e}")
            # Retry with mono if stereo failed
            if MIC_CHANNELS == 2:
                try:
                    kwargs['channels'] = 1
                    self.stream = self.pa.open(**kwargs)
                    self.last_callback_time = time.time()
                    self._actual_channels = 1
                    print("[Mic] Listening (mono fallback)")
                except Exception as e2:
                    print(f"[Mic] ERROR mono fallback: {e2}")
            # Always start health monitor — it will retry if stream dies or never opened
            if not self._health_thread or not self._health_thread.is_alive():
                if not self.stream:
                    self.last_callback_time = time.time() - 20  # Force rebuild on first check
                self._health_thread = threading.Thread(target=self._health_monitor, daemon=True)
                self._health_thread.start()

    def _health_monitor(self):
        """Check every 10s that callbacks are still firing. Full rebuild if dead."""
        while True:
            time.sleep(10)
            if not self.enabled:
                continue
            elapsed = time.time() - self.last_callback_time
            if elapsed > 5:
                print(f"[Mic] ⚠️ Stream dead — no callback for {elapsed:.0f}s. Full rebuild...")
                try:
                    if self.stream:
                        try: self.stream.stop_stream()
                        except: pass
                        try: self.stream.close()
                        except: pass
                        self.stream = None
                    self.pa.terminate()
                except: pass
                time.sleep(1)
                # Full PyAudio rebuild + device re-enumeration
                self.pa = pyaudio.PyAudio()
                self._device_index = self._find_capture_device()
                self.start()
                if self.stream:
                    print("[Mic] ✅ Stream rebuilt successfully")
                else:
                    print("[Mic] ❌ Rebuild failed — will retry in 10s")

    def _callback(self, data, frame_count, time_info, status):
        self.last_callback_time = time.time()
        if not self.enabled:
            return (None, pyaudio.paContinue)  # Keep stream alive, discard data
        if not is_speaking:
            # Downmix stereo to mono — average both channels
            if getattr(self, '_actual_channels', MIC_CHANNELS) == 2:
                import array
                samples = array.array('h', data)
                left = samples[0::2]
                right = samples[1::2]
                mono = array.array('h', [(l + r) // 2 for l, r in zip(left, right)])
                data = mono.tobytes()
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


class MusicPlayer:
    """Plays YouTube audio via yt-dlp + mpv. Wake-word only during playback."""
    def __init__(self):
        self.process = None
        self.playing = False
        self.title = None
        self.volume = 120  # Moderate volume, high-pass filter handles bass distortion

    def play(self, url, title='Unknown', ws_send_queue=None):
        self.stop()
        self.title = title
        self.playing = True
        self._ws_send_queue = ws_send_queue
        try:
            # mpv with yt-dlp backend, audio only — route to same output as TTS
            self._ipc_path = '/tmp/mpv-droid-ipc'
            # Get audio output from speaker singleton (global)
            try:
                spk = globals().get('speaker')
                audio_out = spk.audio_output if spk else Speaker.OUTPUT_EXTERNAL
            except Exception:
                audio_out = Speaker.OUTPUT_EXTERNAL
            if audio_out == Speaker.OUTPUT_EXTERNAL and os.path.exists('/proc/asound/Audio'):
                ao_args = ['--ao=alsa', '--audio-device=alsa/plughw:Audio']
            elif audio_out == Speaker.OUTPUT_INTERNAL and os.path.exists('/proc/asound/UACDemoV10'):
                ao_args = ['--ao=alsa', '--audio-device=alsa/plughw:UACDemoV10']
            elif audio_out == Speaker.OUTPUT_BT:
                ao_args = ['--ao=pulse']
            elif os.path.exists('/proc/asound/Audio'):
                ao_args = ['--ao=alsa', '--audio-device=alsa/plughw:Audio']
            else:
                ao_args = ['--ao=alsa', '--audio-device=alsa/plughw:UACDemoV10']
            # High-pass filter to reduce bass distortion on small speakers
            af_args = ['--af=lavfi=[highpass=f=80]']
            self.process = subprocess.Popen(
                ['mpv', '--no-video', '--really-quiet'] + ao_args + af_args + [
                 '--volume=' + str(self.volume),
                 '--ytdl-format=bestaudio',
                 '--input-ipc-server=' + self._ipc_path,
                 url],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            # Monitor for exit in background — notify server to play next
            def _wait():
                self.process.wait()
                was_playing = self.playing
                self.playing = False
                self.title = None
                print('[Music] Playback finished')
                if was_playing and self._ws_send_queue is not None:
                    self._ws_send_queue.append(json.dumps({'type': 'music_finished'}))
            threading.Thread(target=_wait, daemon=True).start()
        except FileNotFoundError:
            print('[Music] ERROR: mpv not installed. Run: sudo apt install mpv yt-dlp')
            self.playing = False
        except Exception as e:
            print(f'[Music] ERROR: {e}')
            self.playing = False

    def stop(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except:
                self.process.kill()
        self.process = None
        self.playing = False
        self.title = None

    def toggle_pause(self):
        if self.process and self.process.poll() is None:
            try:
                import socket
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.connect(self._ipc_path)
                sock.send(b'{"command": ["cycle", "pause"]}\n')
                sock.close()
            except Exception as e:
                print(f'[Music] Pause toggle failed: {e}')

    def set_volume(self, level):
        self.volume = max(0, min(300, level * 3))  # Scale 0-100 user → 0-300 mpv
        # Send to running mpv via IPC socket
        if self.process and self.process.poll() is None:
            try:
                import socket
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.connect(self._ipc_path)
                cmd = '{"command": ["set_property", "volume", ' + str(self.volume) + ']}\n'
                sock.send(cmd.encode())
                sock.close()
                print(f'[Music] Volume set to {self.volume} (live)')
            except Exception as e:
                print(f'[Music] Volume set to {self.volume} (applies on next play): {e}')
        else:
            print(f'[Music] Volume set to {self.volume} (applies on next play)')

music_player = MusicPlayer()

class Speaker:
    # Audio output targets
    OUTPUT_INTERNAL = 'internal'    # Small USB speaker (UACDemoV10)
    OUTPUT_EXTERNAL = 'external'    # USB DAC dongle (KT USB Audio) → aux → X-GO
    OUTPUT_BT = 'bluetooth'         # Bluetooth A2DP
    # Legacy aliases
    OUTPUT_USB = 'internal'
    OUTPUT_HEADPHONE = 'external'

    def __init__(self):
        self.lock = threading.Lock()
        self.queue = []
        self.queue_lock = threading.Lock()
        self._playing = False
        self.use_pulse = False  # True = route through PulseAudio (for Bluetooth)
        self._pacat_proc = None  # Persistent pacat process to avoid pop
        self._aplay_proc = None  # Persistent aplay process to avoid pop
        # Default: internal USB speaker, switch to external via voice command
        if os.path.exists('/proc/asound/UACDemoV10'):
            self.audio_output = self.OUTPUT_INTERNAL
        elif os.path.exists('/proc/asound/Audio'):
            self.audio_output = self.OUTPUT_EXTERNAL
        else:
            self.audio_output = self.OUTPUT_INTERNAL
        self._silence_thread = None
        self._silence_stop = threading.Event()
        self._last_audio_write = 0  # timestamp of last real audio write
        self._mic_ref = None  # Set after mic is created, for echo flush
        self._ws_send_queue = None  # Set to ws_send_queue for playback_done signal

    def _flush_mic(self):
        """Flush buffered mic data to prevent echo from being processed."""
        try:
            if self._mic_ref and self._mic_ref.stream and self._mic_ref.stream.is_active():
                avail = self._mic_ref.stream.get_read_available()
                if avail > 0:
                    self._mic_ref.stream.read(avail, exception_on_overflow=False)
        except Exception:
            pass

    def enqueue(self, audio_bytes, audio_format='mp3', text='', rate=24000, channels=1):
        """Add audio to playback queue. Starts player thread if not running."""
        with self.queue_lock:
            self.queue.append((audio_bytes, audio_format, text, rate, channels))
            if not self._playing:
                self._playing = True
                threading.Thread(target=self._play_queue, daemon=True).start()

    def interrupt(self):
        """Stop current playback and clear queue."""
        with self.queue_lock:
            self.queue.clear()
        self._interrupted = True
        # Kill any active aplay
        if hasattr(self, '_active_aplay') and self._active_aplay and self._active_aplay.poll() is None:
            self._active_aplay.kill()

    def _play_queue(self):
        """Play all queued audio sequentially, keeping is_speaking=True throughout."""
        global is_speaking
        is_speaking = True
        self._interrupted = False
        try:
            while not self._interrupted:
                with self.queue_lock:
                    if not self.queue:
                        break
                    audio_bytes, audio_format, text, rate, channels = self.queue.pop(0)
                if audio_format == 'pcm':
                    self._play_pcm(audio_bytes, rate, channels)
                else:
                    self._play_one(audio_bytes)
        finally:
            time.sleep(0.3)
            # Flush any buffered mic data that captured our own speech
            self._flush_mic()
            is_speaking = False
            # Notify server that playback is actually done
            if self._ws_send_queue is not None:
                self._ws_send_queue.append(json.dumps({'type': 'playback_done'}))
            with self.queue_lock:
                self._playing = False

    def _start_bt_stream(self):
        """Start persistent pacat with silence feeder to prevent BT pop."""
        if self._pacat_proc and self._pacat_proc.poll() is None:
            return  # Already running
        self._silence_stop.clear()
        self._pacat_proc = subprocess.Popen(
            ['pacat', '--format=s16le', '--rate=24000', '--channels=1', '--playback', '--latency-msec=100'],
            stdin=subprocess.PIPE, stderr=subprocess.DEVNULL
        )
        # Feed silence in background to keep A2DP alive
        def feed_silence():
            # True silence — only feeds when no real audio is flowing
            silence = b'\x00' * 4800  # 100ms of silence at 24kHz 16-bit mono
            while not self._silence_stop.is_set():
                try:
                    now = time.time()
                    # Only feed silence if no real audio in last 0.5s
                    if self._pacat_proc and self._pacat_proc.poll() is None and (now - self._last_audio_write) > 0.5:
                        with self.lock:
                            self._pacat_proc.stdin.write(silence)
                            self._pacat_proc.stdin.flush()
                except (BrokenPipeError, OSError):
                    break
                self._silence_stop.wait(0.1)
        self._silence_thread = threading.Thread(target=feed_silence, daemon=True)
        self._silence_thread.start()
        print('[Speaker] BT stream started with silence feeder')

    def _stop_bt_stream(self):
        """Stop persistent pacat and silence feeder."""
        self._silence_stop.set()
        if self._pacat_proc and self._pacat_proc.poll() is None:
            self._pacat_proc.terminate()
        self._pacat_proc = None
        print('[Speaker] BT stream stopped')

    def _play_pcm(self, pcm_bytes, rate=24000, channels=1):
        """Play raw PCM directly — no ffmpeg decode needed."""
        try:
            if self._interrupted:
                return
            # Apply volume
            vol = VOLUME / 100.0
            if vol != 1.0:
                import array
                samples = array.array('h', pcm_bytes)
                for i in range(len(samples)):
                    samples[i] = max(-32768, min(32767, int(samples[i] * vol)))
                pcm_bytes = samples.tobytes()

            # Add small silence pad
            silence = b'\x00' * (rate * 2 * channels // 30)  # ~33ms
            pcm_bytes = silence + pcm_bytes + silence

            if self.use_pulse:
                if self._pacat_proc is None or self._pacat_proc.poll() is not None:
                    self._start_bt_stream()
                try:
                    with self.lock:
                        self._last_audio_write = time.time()
                        self._pacat_proc.stdin.write(pcm_bytes)
                        self._pacat_proc.stdin.flush()
                        self._last_audio_write = time.time()
                except (BrokenPipeError, OSError):
                    self._start_bt_stream()
                    with self.lock:
                        self._last_audio_write = time.time()
                        self._pacat_proc.stdin.write(pcm_bytes)
                        self._pacat_proc.stdin.flush()
                # Wait for playback duration
                play_secs = len(pcm_bytes) / (rate * 2 * channels)
                time.sleep(play_secs)
            else:
                # Route to configured audio output
                if self.audio_output == self.OUTPUT_EXTERNAL and os.path.exists('/proc/asound/Audio'):
                    aplay_device = 'plughw:Audio'
                elif self.audio_output == self.OUTPUT_INTERNAL and os.path.exists('/proc/asound/UACDemoV10'):
                    aplay_device = 'plughw:UACDemoV10'
                elif os.path.exists('/proc/asound/Audio'):
                    aplay_device = 'plughw:Audio'
                elif os.path.exists('/proc/asound/UACDemoV10'):
                    aplay_device = 'plughw:UACDemoV10'
                else:
                    aplay_device = 'default'
                self._active_aplay = subprocess.Popen(
                    ['aplay', '-D', aplay_device, '-f', 'S16_LE', '-r', str(rate), '-c', str(channels), '-q'],
                    stdin=subprocess.PIPE, stderr=subprocess.PIPE
                )
                _, stderr = self._active_aplay.communicate(input=pcm_bytes, timeout=30)
                if self._active_aplay.returncode != 0:
                    print(f'[Speaker] aplay error: {stderr.decode().strip() if stderr else ""}'[:100])
                self._active_aplay = None
        except Exception as e:
            print(f'[Speaker] PCM play error: {e}')

    def _play_one(self, audio_bytes):
        """Decode and play a single audio chunk."""
        try:
            vol_filter = f'volume={VOLUME / 100.0},afade=t=in:d=0.03,afade=t=out:st=99:d=0.03'
            proc = subprocess.Popen(
                ['ffmpeg', '-i', 'pipe:0', '-af', vol_filter, '-f', 'wav', '-acodec', 'pcm_s16le',
                 '-ar', '24000', '-ac', '1', 'pipe:1'],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
            )
            wav_data, _ = proc.communicate(input=audio_bytes, timeout=30)

            if wav_data and len(wav_data) > 44:
                pcm = wav_data[44:]
                # Silence pads: 30ms before + 30ms after to prevent pop
                silence = b'\x00' * 1440  # 30ms at 24kHz 16-bit mono
                pcm = silence + pcm + silence
                playback_secs = len(pcm) / (24000 * 2)

                if self.use_pulse:
                    # Route through PulseAudio (Bluetooth speaker)
                    if self._pacat_proc is None or self._pacat_proc.poll() is not None:
                        self._start_bt_stream()
                    try:
                        with self.lock:
                            self._last_audio_write = time.time()
                            self._pacat_proc.stdin.write(pcm)
                            self._pacat_proc.stdin.flush()
                            self._last_audio_write = time.time()
                    except (BrokenPipeError, OSError):
                        self._start_bt_stream()
                        with self.lock:
                            self._last_audio_write = time.time()
                            self._pacat_proc.stdin.write(pcm)
                            self._pacat_proc.stdin.flush()
                            self._last_audio_write = time.time()
                else:
                    # Play via aplay — one-shot per chunk (no persistent process)
                    try:
                        ap = subprocess.Popen(
                            ['aplay', '-D', 'default', '-f', 'S16_LE', '-r', '24000', '-c', '1', '-'],
                            stdin=subprocess.PIPE, stderr=subprocess.DEVNULL
                        )
                        ap.stdin.write(pcm)
                        ap.stdin.close()
                        ap.wait(timeout=10)
                    except Exception as e:
                        print(f"[Speaker] Error: {e}")
                        try: ap.kill()
                        except: pass
                # Wait for audio to actually play out before returning
                time.sleep(playback_secs)
        except subprocess.TimeoutExpired:
            print("[Speaker] Timeout — killing audio")
            subprocess.run(['killall', '-q', 'aplay', 'ffmpeg'], stderr=subprocess.DEVNULL)
        except Exception as e:
            print(f"[Speaker] Error: {e}")

    def play_audio(self, audio_bytes, audio_format='mp3'):
        """Legacy non-queued playback."""
        self.enqueue(audio_bytes, audio_format)

    def keep_alive(self):
        """Play silence to prevent audio device from sleeping."""
        try:
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


camera = None  # Global ref for sleep/wake

async def run():
    global last_motion_time, noise_start_time, sleep_state, camera

    print(f"[Droid] Connecting to {SERVER}")
    print(f"[Sleep] {'Enabled' if SLEEP_ENABLED else 'Disabled'} — idle:{IDLE_TIMEOUT}s, rms:{RMS_THRESHOLD}, debounce:{WAKE_DEBOUNCE}s")

    # ── Readiness check — verify all subsystems before connecting ──
    def check_readiness():
        """Returns (ready: bool, issues: list[str])"""
        import subprocess as sp
        issues = []

        # 1. PulseAudio
        try:
            r = sp.run(['pactl', 'info'], capture_output=True, timeout=5)
            if r.returncode != 0:
                issues.append('PulseAudio not responding')
        except Exception as e:
            issues.append(f'PulseAudio check failed: {e}')

        # 2. Network — can we resolve the server?
        import urllib.parse
        host = urllib.parse.urlparse(SERVER.replace('wss://', 'https://').replace('ws://', 'http://')).hostname
        try:
            import socket
            socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM)
        except Exception as e:
            issues.append(f'Cannot resolve {host}: {e}')

        # 3. Server reachable — HTTP health check
        try:
            import urllib.request
            base_url = SERVER.replace('wss://', 'https://').replace('ws://', 'http://').split('/ws')[0]
            req = urllib.request.Request(f'{base_url}/health', method='GET')
            resp = urllib.request.urlopen(req, timeout=5)
            if resp.status != 200:
                issues.append(f'Server health check returned {resp.status}')
        except Exception as e:
            issues.append(f'Server unreachable: {e}')

        # 4. Audio output device exists
        try:
            r = sp.run(['aplay', '-l'], capture_output=True, text=True, timeout=5)
            if 'card' not in r.stdout.lower():
                issues.append('No audio output devices found')
        except Exception as e:
            issues.append(f'Audio device check failed: {e}')

        return (len(issues) == 0, issues)

    max_retries = 10
    for attempt in range(max_retries):
        ready, issues = check_readiness()
        if ready:
            print(f'[Startup] ✅ All systems ready (attempt {attempt + 1})')
            break
        print(f'[Startup] ⏳ Not ready (attempt {attempt + 1}/{max_retries}): {", ".join(issues)}')
        if attempt < max_retries - 1:
            wait = min(5 + attempt * 2, 15)  # 5s, 7s, 9s, ... up to 15s
            time.sleep(wait)
    else:
        print(f'[Startup] ⚠️ Starting anyway after {max_retries} attempts — issues: {", ".join(issues)}')

    # ── Initialize hardware ──
    mic = Microphone()
    speaker = Speaker()
    speaker._mic_ref = mic
    mic.start()
    # Let mic stream establish before opening camera (Pi 3B shared USB controller)
    print('[Startup] Waiting 5s for mic to stabilize before opening camera...')
    time.sleep(5)
    camera = Camera(CAMERA_INDEX)

    from servo import ServoController
    servo_controller = ServoController()
    face_tracker = FaceTracker()
    motion_tracker = MotionTracker()

    # ── Verify mic is actually capturing ──
    mic_check_start = time.time()
    while time.time() - mic_check_start < 10:
        if mic.enabled and hasattr(mic, 'last_callback_time') and mic.last_callback_time > mic_check_start:
            print('[Startup] ✅ Mic stream confirmed active')
            break
        time.sleep(0.5)
    else:
        print('[Startup] ⚠️ Mic stream not confirmed — will rely on health monitor')

    # ── Verify camera opened ──
    if camera.enabled:
        print('[Startup] ✅ Camera open')
    else:
        print('[Startup] ⚠️ Camera failed to open — retry loop will handle it')

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

                # Reset state on connect
                sleep_state = 'awake'
                last_motion_time = time.time()
                noise_start_time = None

                ws_send_queue = []
                speaker._ws_send_queue = ws_send_queue

                await ws.send(json.dumps({
                    'type': 'device_info',
                    'platform': 'raspberry_pi',
                    'model': 'Pi 3 Model B',
                    'capabilities': ['camera', 'microphone', 'speaker', 'sleep_wake']
                }))

                last_frame_time = 0
                last_keepalive = 0

                while running and connected:
                    try:
                        # Send queued messages
                        while ws_send_queue:
                            await ws.send(ws_send_queue.pop(0))

                        # Check for incoming messages
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
                                    # Speaking = activity
                                    last_motion_time = time.time()
                                    speaker.enqueue(audio_bytes, msg.get('format', 'mp3'), text,
                                                    rate=msg.get('rate', 24000), channels=msg.get('channels', 1))

                            elif msg_type == 'music_play':
                                music_url = msg.get('url', '')
                                music_title = msg.get('title', 'Unknown')
                                print(f'[Music] Playing: {music_title}')
                                music_player.play(music_url, music_title, ws_send_queue)

                            elif msg_type == 'music_stop':
                                print('[Music] Stopping')
                                music_player.stop()

                            elif msg_type == 'music_pause':
                                print('[Music] Pause/Resume')
                                music_player.toggle_pause()

                            elif msg_type == 'music_skip':
                                print('[Music] Skip')
                                music_player.stop()  # Just stop current; next play command starts new

                            elif msg_type == 'music_volume':
                                vol = msg.get('level', 50)
                                print(f'[Music] Volume: {vol}')
                                music_player.set_volume(vol)

                            elif msg_type == 'emote':
                                emotes = msg.get('emotes', [])
                                for e in emotes:
                                    print(f"[Emote] {e}")
                                    if servo_controller:
                                        servo_controller.emote(e)

                            elif msg_type == 'done_speaking':
                                if servo_controller:
                                    servo_controller.center()

                            elif msg_type == 'text':
                                print(f"[Droid] {msg.get('text', '')}")

                            elif msg_type == 'status':
                                print(f"[Status] {msg.get('message', '')}")

                            elif msg_type == 'error':
                                print(f"[Error] {msg.get('message', '')}")

                            elif msg_type == 'volume':
                                global VOLUME
                                VOLUME = max(0, min(1000, msg.get('volume', msg.get('level', 80))))
                                print(f"[Volume] Set to {VOLUME}")

                            elif msg_type == 'servo':
                                pan = msg.get('pan', 90)
                                tilt = msg.get('tilt', 90)
                                servo_controller.look_at(pan, tilt)

                            elif msg_type == 'wake':
                                do_wake('server', ws_send_queue)

                            elif msg_type == 'audio_output':
                                target = msg.get('target', 'internal')
                                if target == 'external' or target == 'aux' or target == 'headphone':
                                    speaker.audio_output = Speaker.OUTPUT_EXTERNAL
                                    speaker.use_pulse = False
                                    print('[Speaker] Switched to external speaker (USB DAC → X-GO)')
                                elif target == 'internal' or target == 'usb':
                                    speaker.audio_output = Speaker.OUTPUT_INTERNAL
                                    speaker.use_pulse = False
                                    print('[Speaker] Switched to internal speaker (UACDemoV10)')
                                elif target == 'bluetooth':
                                    speaker.audio_output = Speaker.OUTPUT_BT
                                    speaker.use_pulse = True
                                    print('[Speaker] Switched to Bluetooth')

                            elif msg_type == 'camera_off':
                                camera.disable()
                                print('[Droid] Camera OFF')

                            elif msg_type == 'camera_on':
                                camera.enable()
                                print('[Droid] Camera ON')

                            elif msg_type == 'bluetooth_on':
                                import subprocess as sp
                                import time as _time
                                BT_MAC = '49:ED:E8:CC:23:3D'
                                def bt_connect():
                                    def run(cmd):
                                        r = sp.run(cmd, capture_output=True, text=True, timeout=15)
                                        print(f'[BT] {" ".join(cmd)} → {r.stdout.strip()} {r.stderr.strip()}')
                                        return r
                                    run(['bluetoothctl', 'power', 'on'])
                                    # Try connecting first — works if already paired
                                    r = run(['bluetoothctl', 'connect', BT_MAC])
                                    if 'successful' in r.stdout.lower():
                                        _time.sleep(2)
                                        r = run(['pactl', 'list', 'sinks', 'short'])
                                        for line in r.stdout.split('\n'):
                                            if 'bluez' in line:
                                                sink = line.split('\t')[1] if '\t' in line else line.split()[1]
                                                run(['pactl', 'set-default-sink', sink])
                                                return sink
                                    # If connect failed, do full cycle
                                    run(['bluetoothctl', 'remove', BT_MAC])
                                    _time.sleep(1)
                                    run(['bluetoothctl', '--timeout', '12', 'scan', 'on'])
                                    # Check if found
                                    r = run(['bluetoothctl', 'devices'])
                                    if BT_MAC not in r.stdout:
                                        return None
                                    run(['bluetoothctl', 'pair', BT_MAC])
                                    _time.sleep(1)
                                    run(['bluetoothctl', 'trust', BT_MAC])
                                    _time.sleep(1)
                                    run(['bluetoothctl', 'connect', BT_MAC])
                                    _time.sleep(3)
                                    r = run(['pactl', 'list', 'sinks', 'short'])
                                    for line in r.stdout.split('\n'):
                                        if 'bluez' in line:
                                            sink = line.split('\t')[1] if '\t' in line else line.split()[1]
                                            run(['pactl', 'set-default-sink', sink])
                                            return sink
                                    return None
                                async def do_bt_on():
                                    try:
                                        loop = asyncio.get_event_loop()
                                        sink = await loop.run_in_executor(None, bt_connect)
                                        if sink:
                                            print(f'[Droid] Bluetooth connected: {sink}')
                                            speaker.use_pulse = True
                                            speaker._start_bt_stream()
                                        else:
                                            print('[Droid] Bluetooth failed — staying on USB')
                                    except Exception as e:
                                        print(f'[Droid] Bluetooth error: {e}')
                                asyncio.ensure_future(do_bt_on())

                            elif msg_type == 'bluetooth_off':
                                # Switch back to USB speaker
                                import subprocess
                                try:
                                    # Kill persistent pacat + silence feeder
                                    speaker._stop_bt_stream()
                                    subprocess.run(['bash', '-c', '''
                                        USB_SINK=$(pactl list sinks short | grep -i uac | awk '{print $2}')
                                        if [ -n "$USB_SINK" ]; then
                                            pactl set-default-sink "$USB_SINK"
                                        fi
                                    '''], capture_output=True, text=True, timeout=5)
                                    speaker.use_pulse = False
                                    print('[Droid] Switched to USB speaker')
                                except Exception as e:
                                    print(f'[Droid] Speaker switch error: {e}')

                            elif msg_type == 'mic_off':
                                mic.enabled = False
                                # DON'T stop the ALSA stream — just stop buffering
                                # Stopping/restarting ALSA on Pi 3B kills the stream
                                print('[Droid] Mic OFF (stream stays alive)')

                            elif msg_type == 'mic_on':
                                mic.enabled = True
                                # Stream should still be running — just resume buffering
                                # If stream actually died, health monitor will catch it
                                print('[Droid] Mic ON')

                            elif msg_type == 'wifi_scan':
                                import subprocess as sp, re
                                try:
                                    # Use iwlist for comprehensive scan, nmcli misses some networks
                                    result = sp.run(['sudo', 'iwlist', 'wlan0', 'scan'],
                                                   capture_output=True, text=True, timeout=30)
                                    networks = []
                                    seen = set()
                                    current = {}
                                    for line in result.stdout.split('\n'):
                                        line = line.strip()
                                        if 'ESSID:' in line:
                                            m = re.search(r'ESSID:"(.+)"', line)
                                            if m:
                                                current['ssid'] = m.group(1)
                                        elif 'Quality=' in line:
                                            m = re.search(r'Quality=(\d+)/(\d+)', line)
                                            if m:
                                                current['signal'] = int(100 * int(m.group(1)) / int(m.group(2)))
                                        elif 'Encryption key:' in line:
                                            current['security'] = 'WPA' if 'on' in line else 'Open'
                                        elif line.startswith('Cell ') and current.get('ssid'):
                                            if current['ssid'] not in seen:
                                                seen.add(current['ssid'])
                                                networks.append(current)
                                            current = {}
                                    # Don't forget last cell
                                    if current.get('ssid') and current['ssid'] not in seen:
                                        seen.add(current['ssid'])
                                        networks.append(current)
                                    networks.sort(key=lambda x: x.get('signal', 0), reverse=True)
                                    await ws.send(json.dumps({'type': 'wifi_scan_result', 'networks': networks}))
                                except Exception as e:
                                    await ws.send(json.dumps({'type': 'wifi_scan_result', 'networks': [], 'error': str(e)}))

                            elif msg_type == 'wifi_connect':
                                import subprocess as sp
                                ssid = msg.get('ssid', '')
                                pw = msg.get('password', '')
                                try:
                                    result = sp.run(['nmcli', 'device', 'wifi', 'connect', ssid, 'password', pw, 'ifname', 'wlan0'],
                                                   capture_output=True, text=True, timeout=30)
                                    success = result.returncode == 0
                                    if success:
                                        sp.run(['nmcli', 'connection', 'modify', ssid, 'connection.autoconnect', 'yes', 'connection.autoconnect-retries', '0'],
                                              capture_output=True, timeout=5)
                                    await ws.send(json.dumps({'type': 'wifi_connect_result', 'success': success, 'ssid': ssid,
                                                             'error': '' if success else result.stderr.strip() or result.stdout.strip()}))
                                except Exception as e:
                                    await ws.send(json.dumps({'type': 'wifi_connect_result', 'success': False, 'error': str(e)}))

                            elif msg_type == 'bt_scan':
                                import subprocess as sp
                                def bt_scan_sync():
                                    sp.run(['bluetoothctl', 'power', 'on'], capture_output=True, timeout=5)
                                    sp.run(['bluetoothctl', '--timeout', '8', 'scan', 'on'], capture_output=True, timeout=12)
                                    result = sp.run(['bluetoothctl', 'devices'], capture_output=True, text=True, timeout=5)
                                    bt_devices = []
                                    for line in result.stdout.strip().split('\n'):
                                        parts = line.split(' ', 2)
                                        if len(parts) >= 3:
                                            bt_devices.append({'address': parts[1], 'name': parts[2]})
                                    paired = sp.run(['bluetoothctl', 'devices', 'Paired'], capture_output=True, text=True, timeout=5)
                                    paired_addrs = set()
                                    for line in paired.stdout.strip().split('\n'):
                                        parts = line.split(' ', 2)
                                        if len(parts) >= 2:
                                            paired_addrs.add(parts[1])
                                    for d in bt_devices:
                                        d['paired'] = d['address'] in paired_addrs
                                    return bt_devices
                                async def do_bt_scan():
                                    try:
                                        loop = asyncio.get_event_loop()
                                        devs = await loop.run_in_executor(None, bt_scan_sync)
                                        await ws.send(json.dumps({'type': 'bt_scan_result', 'devices': devs}))
                                    except Exception as e:
                                        await ws.send(json.dumps({'type': 'bt_scan_result', 'devices': [], 'error': str(e)}))
                                asyncio.ensure_future(do_bt_scan())

                            elif msg_type == 'bt_pair':
                                import subprocess as sp
                                addr = msg.get('address', '')
                                def bt_pair_sync(address):
                                    r1 = sp.run(['bluetoothctl', 'pair', address], capture_output=True, text=True, timeout=15)
                                    r2 = sp.run(['bluetoothctl', 'trust', address], capture_output=True, text=True, timeout=5)
                                    r3 = sp.run(['bluetoothctl', 'connect', address], capture_output=True, text=True, timeout=10)
                                    return 'successful' in r3.stdout.lower() or 'successful' in r1.stdout.lower()
                                async def do_bt_pair():
                                    try:
                                        loop = asyncio.get_event_loop()
                                        success = await loop.run_in_executor(None, bt_pair_sync, addr)
                                        await ws.send(json.dumps({'type': 'bt_pair_result', 'success': success, 'address': addr}))
                                    except Exception as e:
                                        await ws.send(json.dumps({'type': 'bt_pair_result', 'success': False, 'error': str(e)}))
                                asyncio.ensure_future(do_bt_pair())

                            elif msg_type == 'ap_config':
                                # Save AP config for wifi-manager
                                ap_ssid = msg.get('ssid', 'Droid-Setup')
                                ap_pw = msg.get('password', 'droid1234')
                                try:
                                    ap_conf = {'ssid': ap_ssid, 'password': ap_pw}
                                    with open('/home/mrcdcox/droid/ap-config.json', 'w') as f:
                                        json.dump(ap_conf, f)
                                    await ws.send(json.dumps({'type': 'ap_config_result', 'success': True}))
                                    print(f'[Droid] AP config saved: {ap_ssid}')
                                except Exception as e:
                                    await ws.send(json.dumps({'type': 'ap_config_result', 'success': False, 'error': str(e)}))

                            elif msg_type == 'ping':
                                await ws.send(json.dumps({'type': 'pong'}))

                        except asyncio.TimeoutError:
                            pass

                        now = time.time()

                        if sleep_state == 'awake':
                            # === AWAKE MODE ===

                            # Fast tracking — face priority, motion fallback, every ~300ms
                            if servo_controller.enabled and servo_controller.kit is not None and camera.enabled and camera.cap is not None and camera.cap.isOpened():
                                if not hasattr(camera, '_last_track_time'):
                                    camera._last_track_time = 0
                                if now - camera._last_track_time >= 1.0:
                                    ret, track_frame = camera.cap.read()
                                    if ret and track_frame is not None:
                                        tracked = False
                                        # Try face first
                                        face = face_tracker.detect(track_frame)
                                        if face:
                                            cx, cy, fw, fh = face
                                            servo_controller.track_face(cx, cy, track_frame.shape[1], track_frame.shape[0])
                                            tracked = True
                                        # Fall back to motion tracking
                                        # Motion tracking disabled for servo — too many false positives
                                        # (monitors, lights, TV). Face tracking only for servo movement.
                                        # Motion detection still used for sleep/wake.

                                        # No face for a while — slowly center
                                        if not tracked and face_tracker.frames_without_face > 30:
                                            servo_controller.center()
                                    camera._last_track_time = now

                            # Send camera frame + check motion (slower interval for server)
                            if now - last_frame_time >= FRAME_INTERVAL:
                                # Retry opening camera if it failed (but not if intentionally disabled)
                                if camera.enabled and (camera.cap is None or not camera.cap.isOpened()):
                                    camera._open_retries += 1
                                    if camera._open_retries % 6 == 1:  # Try every ~30s (6 * 5s frame interval)
                                        camera._open()
                                frame, jpeg = camera.capture_frame()
                                if jpeg:
                                    # Check for motion
                                    if frame is not None and SLEEP_ENABLED:
                                        if detect_motion(frame):
                                            last_motion_time = now

                                    # Tag frame with servo state — server skips diff if camera was moving
                                    servo_moving = (now - servo_controller.last_move_time) < 2.0
                                    await ws.send(json.dumps({
                                        'type': 'frame',
                                        'data': base64.b64encode(jpeg).decode('ascii'),
                                        'timestamp': now,
                                        'servo_moving': servo_moving
                                    }))
                                    last_frame_time = now

                            # Send audio to server for STT
                            audio = mic.get_audio()
                            if audio:
                                # Audio activity also resets idle (if loud enough)
                                rms = compute_rms(audio)
                                if rms > RMS_THRESHOLD:
                                    last_motion_time = now

                                await ws.send(json.dumps({
                                    'type': 'audio',
                                    'data': base64.b64encode(audio).decode('ascii'),
                                    'sample_rate': SAMPLE_RATE,
                                    'channels': CHANNELS,
                                    'format': 'pcm_s16le'
                                }))

                            # Check idle timeout → sleep (skip first 120s after boot)
                            if SLEEP_ENABLED and (now - last_motion_time) > IDLE_TIMEOUT and (now - boot_time) > 120:
                                do_sleep(ws_send_queue)
                                continue  # Skip rest of awake processing

                        else:
                            # === SLEEPING MODE ===
                            # Don't send camera frames or audio to server
                            # Just listen for noise to wake up

                            audio = mic.get_audio()
                            if audio:
                                rms = compute_rms(audio)
                                if rms > RMS_THRESHOLD:
                                    if noise_start_time is None:
                                        noise_start_time = now
                                    elif now - noise_start_time >= WAKE_DEBOUNCE:
                                        do_wake('noise', ws_send_queue)
                                else:
                                    noise_start_time = None

                        # Keep audio device alive (every 10s)
                        if now - last_keepalive > 10:
                            last_keepalive = now
                            if not is_speaking:
                                threading.Thread(target=speaker.keep_alive, daemon=True).start()

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
    servo_controller.close()
    print("[Droid] Shutdown complete")


if __name__ == '__main__':
    print("=" * 40)
    print("  DROID Pi Client")
    print("  Camera + Mic -> Server -> Speaker")
    print("  Sleep/Wake: motion + noise detection")
    print("=" * 40)
    asyncio.run(run())
