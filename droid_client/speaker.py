"""Speaker: audio playback queue, persistent aplay/pacat, interrupt support."""
import json
import os
import subprocess
import threading
import time

from . import config, state


class Speaker:
    # Audio output targets
    OUTPUT_INTERNAL = 'internal'    # Small USB speaker (UACDemoV10)
    OUTPUT_EXTERNAL = 'external'    # USB DAC → aux → amp
    OUTPUT_BT = 'bluetooth'         # Bluetooth A2DP
    # Legacy aliases
    OUTPUT_USB = 'internal'
    OUTPUT_HEADPHONE = 'external'

    def __init__(self):
        self.lock = threading.Lock()
        self.queue = []
        self.queue_lock = threading.Lock()
        self._playing = False
        self._interrupted = False
        self.use_pulse = False
        self._pacat_proc = None
        self._aplay_proc = None
        self._active_aplay = None
        if os.path.exists('/proc/asound/UACDemoV10'):
            self.audio_output = self.OUTPUT_INTERNAL
        elif os.path.exists('/proc/asound/Audio'):
            self.audio_output = self.OUTPUT_EXTERNAL
        else:
            self.audio_output = self.OUTPUT_INTERNAL
        self._silence_thread = None
        self._silence_stop = threading.Event()
        self._last_audio_write = 0
        self._mic_ref = None         # set externally — for echo flush (legacy)
        self._ws_send_queue = None   # set externally

    # ---------------------------------------------------------------- queue
    def enqueue(self, audio_bytes, audio_format='mp3', text='', rate=24000, channels=1):
        with self.queue_lock:
            self.queue.append((audio_bytes, audio_format, text, rate, channels))
            if not self._playing:
                self._playing = True
                threading.Thread(target=self._play_queue, daemon=True).start()

    def play_audio(self, audio_bytes, audio_format='mp3'):
        """Legacy non-queued playback."""
        self.enqueue(audio_bytes, audio_format)

    def interrupt(self):
        """Stop current playback and clear queue. Kill happens under self.lock
        so a concurrent _play_one() can't write to a half-killed pipe."""
        with self.queue_lock:
            self.queue.clear()
        self._interrupted = True
        with self.lock:
            ap = self._active_aplay
            if ap and ap.poll() is None:
                try:
                    ap.kill()
                except Exception:
                    pass

    def _play_queue(self):
        """Play all queued audio sequentially, keeping state.is_speaking=True throughout."""
        state.is_speaking = True
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
            state.is_speaking = False
            if self._ws_send_queue is not None:
                self._ws_send_queue.append(json.dumps({'type': 'playback_done'}))
            with self.queue_lock:
                self._playing = False

    # ---------------------------------------------------------------- BT stream
    def _start_bt_stream(self):
        """Start persistent pacat with silence feeder to prevent BT pop."""
        if self._pacat_proc and self._pacat_proc.poll() is None:
            return
        self._silence_stop.clear()
        self._pacat_proc = subprocess.Popen(
            ['pacat', '--format=s16le', '--rate=24000', '--channels=1', '--playback', '--latency-msec=100'],
            stdin=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )

        def feed_silence():
            silence = b'\x00' * 4800  # 100ms at 24kHz 16-bit mono
            while not self._silence_stop.is_set():
                if not self._pacat_proc or self._pacat_proc.poll() is not None:
                    break
                try:
                    now = time.time()
                    if (now - self._last_audio_write) > 0.5:
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
        self._silence_stop.set()
        if self._pacat_proc and self._pacat_proc.poll() is None:
            self._pacat_proc.terminate()
        self._pacat_proc = None
        print('[Speaker] BT stream stopped')

    # ---------------------------------------------------------------- play paths
    def _play_pcm(self, pcm_bytes, rate=24000, channels=1):
        try:
            if self._interrupted:
                return
            vol = config.VOLUME / 100.0
            if vol != 1.0:
                import array
                samples = array.array('h', pcm_bytes)
                for i in range(len(samples)):
                    samples[i] = max(-32768, min(32767, int(samples[i] * vol)))
                pcm_bytes = samples.tobytes()

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
                    if self._pacat_proc and self._pacat_proc.poll() is None:
                        with self.lock:
                            self._last_audio_write = time.time()
                            self._pacat_proc.stdin.write(pcm_bytes)
                            self._pacat_proc.stdin.flush()
                time.sleep(len(pcm_bytes) / (rate * 2 * channels))
            else:
                self._ensure_aplay_stream(rate, channels)
                if self._aplay_proc and self._aplay_proc.poll() is None:
                    try:
                        with self.lock:
                            self._aplay_proc.stdin.write(pcm_bytes)
                            self._aplay_proc.stdin.flush()
                        time.sleep(len(pcm_bytes) / (rate * 2 * channels))
                    except (BrokenPipeError, OSError):
                        print('[Speaker] aplay pipe broken — restarting stream')
                        self._aplay_proc = None
        except Exception as e:
            print(f'[Speaker] PCM play error: {e}')

    def _play_one(self, audio_bytes):
        """Decode and play a single audio chunk (mp3/wav via ffmpeg)."""
        proc = None
        try:
            vol_filter = f'volume={config.VOLUME / 100.0},afade=t=in:d=0.03,afade=t=out:st=99:d=0.03'
            proc = subprocess.Popen(
                ['ffmpeg', '-i', 'pipe:0', '-af', vol_filter, '-f', 'wav', '-acodec', 'pcm_s16le',
                 '-ar', '24000', '-ac', '1', 'pipe:1'],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            )
            wav_data, _ = proc.communicate(input=audio_bytes, timeout=30)

            if wav_data and len(wav_data) > 44:
                pcm = wav_data[44:]
                silence = b'\x00' * 1440  # 30ms at 24kHz 16-bit mono
                pcm = silence + pcm + silence
                playback_secs = len(pcm) / (24000 * 2)

                if self.use_pulse:
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
                        if self._pacat_proc and self._pacat_proc.poll() is None:
                            with self.lock:
                                self._last_audio_write = time.time()
                                self._pacat_proc.stdin.write(pcm)
                                self._pacat_proc.stdin.flush()
                                self._last_audio_write = time.time()
                else:
                    ap = None
                    try:
                        ap = subprocess.Popen(
                            ['aplay', '-D', 'default', '-f', 'S16_LE', '-r', '24000', '-c', '1', '-'],
                            stdin=subprocess.PIPE, stderr=subprocess.DEVNULL,
                        )
                        with self.lock:
                            self._active_aplay = ap
                        ap.stdin.write(pcm)
                        ap.stdin.close()
                        ap.wait(timeout=10)
                    except Exception as e:
                        print(f"[Speaker] Error: {e}")
                    finally:
                        if ap and ap.poll() is None:
                            try: ap.kill()
                            except Exception: pass
                        with self.lock:
                            if self._active_aplay is ap:
                                self._active_aplay = None

                time.sleep(playback_secs)
        except subprocess.TimeoutExpired:
            print("[Speaker] Timeout — killing audio")
            subprocess.run(['killall', '-q', 'aplay', 'ffmpeg'], stderr=subprocess.DEVNULL)
        except Exception as e:
            print(f"[Speaker] Error: {e}")
        finally:
            if proc and proc.poll() is None:
                try: proc.kill()
                except Exception: pass

    # ---------------------------------------------------------------- helpers
    def _flush_mic(self):
        """Legacy — flush buffered mic data. No longer called from _play_queue
        because the server-side transcript echo filter handles echo now, but
        kept in case something external still relies on it."""
        try:
            if self._mic_ref and self._mic_ref.stream and self._mic_ref.stream.is_active():
                avail = self._mic_ref.stream.get_read_available()
                if avail > 0:
                    self._mic_ref.stream.read(avail, exception_on_overflow=False)
        except Exception:
            pass

    def _pulse_available(self):
        try:
            r = subprocess.run(['pactl', 'info'], capture_output=True, timeout=2)
            return r.returncode == 0
        except Exception:
            return False

    def _get_aplay_device(self):
        if self._pulse_available():
            return 'pulse'
        if self.audio_output == self.OUTPUT_EXTERNAL and os.path.exists('/proc/asound/Audio'):
            return 'plughw:Audio'
        elif self.audio_output == self.OUTPUT_INTERNAL and os.path.exists('/proc/asound/UACDemoV10'):
            return 'plughw:UACDemoV10'
        elif os.path.exists('/proc/asound/Audio'):
            return 'plughw:Audio'
        elif os.path.exists('/proc/asound/UACDemoV10'):
            return 'plughw:UACDemoV10'
        return 'default'

    def _ensure_aplay_stream(self, rate=24000, channels=1):
        if self._aplay_proc and self._aplay_proc.poll() is None:
            return
        device = self._get_aplay_device()
        try:
            self._aplay_proc = subprocess.Popen(
                ['aplay', '-D', device, '-f', 'S16_LE', '-r', str(rate), '-c', str(channels), '-q', '--buffer-size=32768'],
                stdin=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            time.sleep(0.3)
            if self._aplay_proc.poll() is not None:
                err = self._aplay_proc.stderr.read().decode(errors='ignore').strip()
                print(f'[Speaker] aplay exited immediately on {device}: {err}'[:150])
                self._aplay_proc = None
                return
            print(f'[Speaker] Persistent aplay started on {device} @ {rate}Hz')
        except Exception as e:
            print(f'[Speaker] Failed to start aplay: {e}')
            self._aplay_proc = None

    def _stop_aplay_stream(self):
        if self._aplay_proc:
            try:
                self._aplay_proc.stdin.close()
                self._aplay_proc.wait(timeout=2)
            except Exception:
                self._aplay_proc.kill()
            self._aplay_proc = None

    def keep_alive(self):
        """Play silence to prevent audio device from sleeping."""
        try:
            silence = b'\x00' * 4800
            proc = subprocess.Popen(
                ['aplay', '-f', 'S16_LE', '-r', '24000', '-c', '1', '-q', '-'],
                stdin=subprocess.PIPE, stderr=subprocess.DEVNULL,
            )
            proc.communicate(input=silence, timeout=2)
        except Exception:
            pass

    def close(self):
        self._stop_aplay_stream()
