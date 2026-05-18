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
            # Route through PulseAudio so we don't fight the speaker.py
            # path (also pulse) for the same ALSA hardware. With PA
            # holding the Jieli sink for TTS playback, mpv's direct
            # `plughw:UACDemoV10` would lose the race and play silently.
            # Going through pulse also lets module-echo-cancel use the
            # music as a reference signal so the mic doesn't pick it up.
            ao_args = ['--ao=pulse']
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

            # Tell the server playback has started so it can sync
            # device.musicPlaying = true. Server uses that flag for
            # wake-word-gated listening + ducking. Without this, a server
            # restart mid-music leaves the server thinking nothing is
            # playing and the droid effectively goes deaf.
            if self._ws_send_queue is not None:
                self._ws_send_queue.append(json.dumps({
                    'type': 'music_started',
                    'title': title or '',
                }))

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
        was_playing = self.playing
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except Exception:
                self.process.kill()
        self.process = None
        self.playing = False
        self.title = None
        # Server tracks musicPlaying for the wake-word gate. Tell it
        # we've stopped if we were actually playing.
        if was_playing and self._ws_send_queue is not None:
            self._ws_send_queue.append(json.dumps({'type': 'music_finished'}))

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
