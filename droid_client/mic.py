"""USB microphone capture with health monitor and voice-interrupt hook."""
import json
import re
import subprocess
import threading
import time
from collections import deque

import pyaudio

from .config import FORMAT, MIC_CHANNELS, MIC_GAIN, MIC_HIGHPASS_HZ, SAMPLE_RATE, CHUNK


def _aec_active():
    """True if PulseAudio has module-echo-cancel loaded. When active, the
    PulseAudio default source is the echo-cancelled virtual source, so we
    should capture via the PyAudio 'pulse'/'default' device rather than
    binding directly to the raw hw mic (which would bypass AEC)."""
    try:
        r = subprocess.run(['pactl', 'list', 'short', 'modules'],
                           capture_output=True, text=True, timeout=3)
        return r.returncode == 0 and 'module-echo-cancel' in r.stdout
    except Exception:
        return False


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
        self._enabled = True
        # Recent audio captured while disabled. Replayed into the main buffer
        # when mic flips back on, so the user's first words after the droid
        # finishes speaking aren't lost in the playback_done → mic_on round trip.
        self._lookback = deque(maxlen=2)  # ~1s at 500ms chunks
        self.last_callback_time = time.time()
        self._health_thread = None
        self._health_stop = threading.Event()
        self._ws_send_queue = None  # set externally for voice interrupt
        self._speaker = None        # set externally so we can call speaker.interrupt()

    def _find_capture_device(self):
        """Pick a PyAudio input device. When PulseAudio echo-cancel is
        active, prefer the PyAudio 'pulse'/'default' device so we read
        the AEC virtual source (clean mic). Otherwise fall back to direct
        hw capture from the Logitech USB webcam mic."""
        if _aec_active():
            for i in range(self.pa.get_device_count()):
                try:
                    d = self.pa.get_device_info_by_index(i)
                    if d['maxInputChannels'] > 0 and d.get('name', '').lower() in ('pulse', 'default'):
                        print(f'[Mic] AEC active — using PulseAudio device {i}: {d["name"]}')
                        return i
                except Exception:
                    continue
            print('[Mic] AEC active but no pulse/default PyAudio device — falling back to raw hw')
        try:
            with open('/proc/asound/cards') as f:
                for line in f:
                    m = re.match(r'^\s*(\d+)\s+\[(\w+)', line)
                    if m and any(k in line.lower() for k in
                                 ['logitech', 'brio', '046d', 'c260', 'c270', '0x46d', 'usb device 0x46d']):
                        card_num = int(m.group(1))
                        card_name = m.group(2)
                        print(f'[Mic] Found ALSA card {card_num}: {card_name}')
                        # Search PyAudio devices for this card
                        for i in range(self.pa.get_device_count()):
                            try:
                                d = self.pa.get_device_info_by_index(i)
                                if d['maxInputChannels'] > 0:
                                    name = d.get('name', '')
                                    if f'hw:{card_num}' in name or card_name.lower() in name.lower():
                                        print(f'[Mic] Using device {i}: {name}')
                                        return i
                            except Exception:
                                continue
                        # No hw match — fall back to any non-PulseAudio input device
                        for i in range(self.pa.get_device_count()):
                            try:
                                d = self.pa.get_device_info_by_index(i)
                                if d['maxInputChannels'] > 0 and 'pulse' not in d.get('name', '').lower():
                                    print(f'[Mic] Fallback device {i}: {d["name"]}')
                                    return i
                            except Exception:
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
                except Exception:
                    pass
            kwargs = dict(
                format=FORMAT,
                channels=MIC_CHANNELS,
                rate=SAMPLE_RATE,
                input=True,
                frames_per_buffer=CHUNK,
                stream_callback=self._callback,
            )
            if self._device_index is not None:
                kwargs['input_device_index'] = self._device_index
            self.stream = self.pa.open(**kwargs)
            self.last_callback_time = time.time()
            self._actual_channels = MIC_CHANNELS
            print(f"[Mic] Listening (stereo={MIC_CHANNELS == 2})")
            self._ensure_health_monitor()
        except Exception as e:
            print(f"[Mic] ERROR starting stream: {e}")
            # Retry as mono if stereo failed
            if MIC_CHANNELS == 2:
                try:
                    kwargs['channels'] = 1
                    self.stream = self.pa.open(**kwargs)
                    self.last_callback_time = time.time()
                    self._actual_channels = 1
                    print("[Mic] Listening (mono fallback)")
                except Exception as e2:
                    print(f"[Mic] ERROR mono fallback: {e2}")
            if not self.stream:
                # Force health-monitor rebuild on first check
                self.last_callback_time = time.time() - 20
            self._ensure_health_monitor()

    def _ensure_health_monitor(self):
        if not self._health_thread or not self._health_thread.is_alive():
            self._health_stop.clear()
            self._health_thread = threading.Thread(target=self._health_monitor, daemon=True)
            self._health_thread.start()

    def _health_monitor(self):
        """Check every 10s that callbacks are still firing. Full rebuild if dead."""
        while not self._health_stop.is_set():
            self._health_stop.wait(10)
            if self._health_stop.is_set():
                return
            if not self.enabled:
                continue
            elapsed = time.time() - self.last_callback_time
            if elapsed > 5:
                print(f"[Mic] ⚠️ Stream dead — no callback for {elapsed:.0f}s. Full rebuild...")
                try:
                    if self.stream:
                        try: self.stream.stop_stream()
                        except Exception: pass
                        try: self.stream.close()
                        except Exception: pass
                        self.stream = None
                    self.pa.terminate()
                except Exception:
                    pass
                time.sleep(1)
                self.pa = pyaudio.PyAudio()
                self._device_index = self._find_capture_device()
                self.start()
                if self.stream:
                    print("[Mic] ✅ Stream rebuilt successfully")
                else:
                    print("[Mic] ❌ Rebuild failed — will retry in 10s")

    @property
    def enabled(self):
        return self._enabled

    @enabled.setter
    def enabled(self, value):
        prev = self._enabled
        self._enabled = bool(value)
        # On disabled → enabled, splice the lookback ring into the main buffer.
        # Recovers user audio captured during the playback_done → mic_on window.
        if self._enabled and not prev:
            with self.lock:
                if self._lookback:
                    self.buffer = list(self._lookback) + self.buffer
                    self._lookback.clear()

    def _callback(self, data, frame_count, time_info, status):
        self.last_callback_time = time.time()

        # Downmix stereo to mono if hardware came up as 2-channel
        if getattr(self, '_actual_channels', MIC_CHANNELS) == 2:
            import array
            samples = array.array('h', data)
            left = samples[0::2]
            right = samples[1::2]
            mono = array.array('h', [(l + r) // 2 for l, r in zip(left, right)])
            data = mono.tobytes()

        if MIC_HIGHPASS_HZ > 0 or MIC_GAIN != 1.0:
            import array, math
            samples = array.array('h', data)
            if MIC_HIGHPASS_HZ > 0:
                # Two cascaded RBJ high-pass biquads (24dB/oct), state carried
                # across chunks so there's no click at chunk boundaries.
                if not hasattr(self, '_hp_coef'):
                    w0 = 2.0 * math.pi * MIC_HIGHPASS_HZ / SAMPLE_RATE
                    cw, sw = math.cos(w0), math.sin(w0)
                    alpha = sw / (2.0 * 0.707)
                    a0 = 1.0 + alpha
                    self._hp_coef = ((1.0 + cw) / (2.0 * a0), -(1.0 + cw) / a0,
                                     (1.0 + cw) / (2.0 * a0), -2.0 * cw / a0,
                                     (1.0 - alpha) / a0)
                    self._hp_state = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
                b0, b1, b2, a1, a2 = self._hp_coef
                x1a, x2a, y1a, y2a, x1b, x2b, y1b, y2b = self._hp_state
                for i in range(len(samples)):
                    x = float(samples[i])
                    y = b0 * x + b1 * x1a + b2 * x2a - a1 * y1a - a2 * y2a
                    x2a, x1a, y2a, y1a = x1a, x, y1a, y
                    z = b0 * y + b1 * x1b + b2 * x2b - a1 * y1b - a2 * y2b
                    x2b, x1b, y2b, y1b = x1b, y, y1b, z
                    v = int(z * MIC_GAIN)
                    samples[i] = -32768 if v < -32768 else (32767 if v > 32767 else v)
                self._hp_state = [x1a, x2a, y1a, y2a, x1b, x2b, y1b, y2b]
            else:
                for i in range(len(samples)):
                    v = int(samples[i] * MIC_GAIN)
                    samples[i] = -32768 if v < -32768 else (32767 if v > 32767 else v)
            data = samples.tobytes()

        if not self._enabled:
            # Keep stream alive; stash recent audio so it can be replayed on mic_on.
            self._lookback.append(data)
            return (None, pyaudio.paContinue)

        # Buffer continuously, including during is_speaking. The server compares
        # the transcript to what the droid recently said and drops echo there.
        with self.lock:
            self.buffer.append(data)
        return (None, pyaudio.paContinue)

    def _trigger_interrupt(self):
        """Called from voice-interrupt detector. Kills playback + tells server."""
        try:
            if self._speaker is not None:
                self._speaker.interrupt()
            if self._ws_send_queue is not None:
                self._ws_send_queue.append(json.dumps({'type': 'user_interrupted'}))
        except Exception as e:
            print(f'[Mic] Interrupt trigger error: {e}')

    def get_audio(self):
        with self.lock:
            if not self.buffer:
                return None
            data = b''.join(self.buffer)
            self.buffer.clear()
            return data

    def close(self):
        self._health_stop.set()
        if self.stream:
            try: self.stream.stop_stream()
            except Exception: pass
            try: self.stream.close()
            except Exception: pass
        try: self.pa.terminate()
        except Exception: pass
