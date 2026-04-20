"""USB microphone capture with health monitor and voice-interrupt hook."""
import json
import re
import threading
import time

import pyaudio

from .config import FORMAT, MIC_CHANNELS, SAMPLE_RATE, CHUNK


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
        self._health_stop = threading.Event()
        self._ws_send_queue = None  # set externally for voice interrupt
        self._speaker = None        # set externally so we can call speaker.interrupt()

    def _find_capture_device(self):
        """Find the USB webcam mic — try all PyAudio devices with input channels."""
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

    def _callback(self, data, frame_count, time_info, status):
        self.last_callback_time = time.time()
        if not self.enabled:
            return (None, pyaudio.paContinue)  # keep stream alive, discard data

        # Downmix stereo to mono if hardware came up as 2-channel
        if getattr(self, '_actual_channels', MIC_CHANNELS) == 2:
            import array
            samples = array.array('h', data)
            left = samples[0::2]
            right = samples[1::2]
            mono = array.array('h', [(l + r) // 2 for l, r in zip(left, right)])
            data = mono.tobytes()

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
