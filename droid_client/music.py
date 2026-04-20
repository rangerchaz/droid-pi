"""YouTube audio playback via yt-dlp + mpv."""
import json
import os
import socket
import subprocess
import threading

# Output target constants (mirrored on Speaker too — kept here as strings to
# avoid the circular speaker → music import).
OUTPUT_INTERNAL = 'internal'
OUTPUT_EXTERNAL = 'external'
OUTPUT_BT = 'bluetooth'


class MusicPlayer:
    """Plays YouTube audio via yt-dlp + mpv. Wake-word only during playback."""

    def __init__(self):
        self.process = None
        self.playing = False
        self.title = None
        self.volume = 120  # moderate; high-pass filter handles bass distortion
        self._ipc_path = '/tmp/mpv-droid-ipc'
        self._ws_send_queue = None
        self._speaker = None  # set externally for output-target lookup

    def play(self, url, title='Unknown', ws_send_queue=None):
        self.stop()
        self.title = title
        self.playing = True
        self._ws_send_queue = ws_send_queue
        try:
            audio_out = getattr(self._speaker, 'audio_output', OUTPUT_EXTERNAL) if self._speaker else OUTPUT_EXTERNAL
            if audio_out == OUTPUT_EXTERNAL and os.path.exists('/proc/asound/Audio'):
                ao_args = ['--ao=alsa', '--audio-device=alsa/plughw:Audio']
            elif audio_out == OUTPUT_INTERNAL and os.path.exists('/proc/asound/UACDemoV10'):
                ao_args = ['--ao=alsa', '--audio-device=alsa/plughw:UACDemoV10']
            elif audio_out == OUTPUT_BT:
                ao_args = ['--ao=pulse']
            elif os.path.exists('/proc/asound/Audio'):
                ao_args = ['--ao=alsa', '--audio-device=alsa/plughw:Audio']
            else:
                ao_args = ['--ao=alsa', '--audio-device=alsa/plughw:UACDemoV10']
            af_args = ['--af=lavfi=[highpass=f=80]']
            self.process = subprocess.Popen(
                ['mpv', '--no-video', '--really-quiet'] + ao_args + af_args + [
                    '--volume=' + str(self.volume),
                    '--ytdl-format=bestaudio',
                    '--input-ipc-server=' + self._ipc_path,
                    url,
                ],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )

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
            except Exception:
                self.process.kill()
        self.process = None
        self.playing = False
        self.title = None

    def toggle_pause(self):
        if self.process and self.process.poll() is None:
            try:
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.connect(self._ipc_path)
                sock.send(b'{"command": ["cycle", "pause"]}\n')
                sock.close()
            except Exception as e:
                print(f'[Music] Pause toggle failed: {e}')

    def set_volume(self, level):
        self.volume = max(0, min(300, level * 3))  # 0-100 user → 0-300 mpv
        if self.process and self.process.poll() is None:
            try:
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
