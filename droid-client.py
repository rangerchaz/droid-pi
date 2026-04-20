#!/usr/bin/env python3
"""
Droid Pi Client — thin client that streams camera + mic to droid server,
plays audio responses through speaker. All AI runs server-side.

Sleep/Wake:
  AWAKE + camera on → motion detection via frame differencing
  AWAKE + no motion for idle_timeout → SLEEPING (camera stops, audio stops)
  SLEEPING → noise above threshold for wake_debounce → AWAKE
  Any server 'wake' message or noise → AWAKE

Hardware classes and helpers live in the `droid_client/` package. This
file is the entry point: startup, sleep transitions, and the WS dispatch
loop.
"""

import asyncio
import base64
import json
import os
import signal
import sys
import threading
import time
from collections import deque

try:
    import websockets
except ImportError:
    print("ERROR: websockets not installed. Run: pip3 install websockets --break-system-packages")
    sys.exit(1)

from droid_client import state
from droid_client import config
from droid_client.config import (
    CLIENT_VERSION, SERVER, TOKEN, CAMERA_INDEX,
    SAMPLE_RATE, FRAME_INTERVAL, CHANNELS,
    IDLE_TIMEOUT, RMS_THRESHOLD, WAKE_DEBOUNCE, SLEEP_ENABLED,
)
from droid_client.utils import compute_rms, detect_motion
from droid_client.camera import Camera
from droid_client.face import FaceTracker
from droid_client.motion import MotionTracker
from droid_client.mic import Microphone
from droid_client.music import MusicPlayer
from droid_client.speaker import Speaker


# Module-level ref so do_sleep / do_wake can reach the camera after creation.
camera = None
# Module-level ref so do_sleep can ask the motion tracker to forget its prev
# frame (avoids wake triggering on the delta while the camera was paused).
motion_tracker = None
# Servo (optional — missing on headless installs).
servo_controller = None


def signal_handler(sig, frame):
    if not state.running:
        print("\nForce quit.")
        os._exit(0)
    print("\nShutting down...")
    state.running = False


signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)


def do_sleep(ws_send_queue):
    """Transition to sleep state."""
    if state.sleep_state == 'sleeping':
        return
    state.sleep_state = 'sleeping'
    if camera:
        camera.disable()
    if servo_controller:
        servo_controller.center()
    if motion_tracker:
        motion_tracker.prev_gray = None  # reset so wake doesn't trigger false motion
    print("[Sleep] 💤 Sleeping — no activity for", IDLE_TIMEOUT, "seconds")
    ws_send_queue.append(json.dumps({'type': 'sleep_state', 'state': 'sleeping'}))


def do_wake(reason, ws_send_queue):
    """Transition to awake state."""
    if state.sleep_state == 'awake':
        return
    state.sleep_state = 'awake'
    state.last_motion_time = time.time()
    state.noise_start_time = None
    state.prev_frame_gray = None
    if camera:
        camera.enable()
    print(f"[Sleep] ☀️ Waking — reason: {reason}")
    ws_send_queue.append(json.dumps({'type': 'sleep_state', 'state': 'awake'}))


async def run():
    global camera, motion_tracker, servo_controller

    print(f"[Droid] Connecting to {SERVER}")
    print(f"[Sleep] {'Enabled' if SLEEP_ENABLED else 'Disabled'} — idle:{IDLE_TIMEOUT}s, rms:{RMS_THRESHOLD}, debounce:{WAKE_DEBOUNCE}s")

    # ── Readiness check ──
    def check_readiness():
        """Returns (ready: bool, issues: list[str])"""
        import subprocess as sp
        issues = []

        try:
            r = sp.run(['pactl', 'info'], capture_output=True, timeout=5)
            if r.returncode != 0:
                issues.append('PulseAudio not responding')
        except Exception as e:
            issues.append(f'PulseAudio check failed: {e}')

        import urllib.parse
        host = urllib.parse.urlparse(SERVER.replace('wss://', 'https://').replace('ws://', 'http://')).hostname
        try:
            import socket
            socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM)
        except Exception as e:
            issues.append(f'Cannot resolve {host}: {e}')

        try:
            import urllib.request
            base_url = SERVER.replace('wss://', 'https://').replace('ws://', 'http://').split('/ws')[0]
            req = urllib.request.Request(f'{base_url}/health', method='GET')
            resp = urllib.request.urlopen(req, timeout=5)
            if resp.status != 200:
                issues.append(f'Server health check returned {resp.status}')
        except Exception as e:
            issues.append(f'Server unreachable: {e}')

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
            wait = min(5 + attempt * 2, 15)
            time.sleep(wait)
    else:
        print(f'[Startup] ⚠️ Starting anyway after {max_retries} attempts — issues: {", ".join(issues)}')

    # ── Initialize hardware ──
    mic = Microphone()
    speaker = Speaker()
    music_player = MusicPlayer()
    # Wire cross-component references so the mic can trigger speaker.interrupt()
    # and music player can pick the right audio output target.
    mic._speaker = speaker
    music_player._speaker = speaker
    speaker._mic_ref = mic

    mic.start()
    print('[Startup] Waiting 5s for mic to stabilize before opening camera...')
    time.sleep(5)

    camera = Camera(CAMERA_INDEX)

    try:
        from servo import ServoController
        servo_controller = ServoController()
    except Exception as e:
        print(f'[Droid] Servo not available: {e}')
        servo_controller = None
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

    if camera.enabled:
        print('[Startup] ✅ Camera open')
    else:
        print('[Startup] ⚠️ Camera failed to open — retry loop will handle it')

    url = SERVER
    if TOKEN:
        url += ('&' if '?' in url else '?') + f'token={TOKEN}'

    reconnect_delay = 1

    while state.running:
        try:
            async with websockets.connect(url, ping_interval=30, ping_timeout=15,
                                          max_size=10 * 1024 * 1024) as ws:
                reconnect_delay = 1
                print("[Droid] Connected!")
                connected = True

                # Reset state on connect
                state.sleep_state = 'awake'
                state.last_motion_time = time.time()
                state.noise_start_time = None

                # deque is thread-safe for append + popleft, unlike a plain
                # list. Mic/speaker/music/motion all push messages onto this
                # queue from outside the asyncio loop.
                ws_send_queue = deque()
                speaker._ws_send_queue = ws_send_queue
                mic._ws_send_queue = ws_send_queue

                await ws.send(json.dumps({
                    'type': 'device_info',
                    'platform': 'raspberry_pi',
                    'model': 'Pi 3 Model B',
                    'version': CLIENT_VERSION,
                    'capabilities': ['camera', 'microphone', 'speaker', 'sleep_wake']
                }))

                last_frame_time = 0
                last_keepalive = 0

                while state.running and connected:
                    try:
                        # Send queued messages — popleft() is O(1) + thread-safe.
                        while True:
                            try:
                                await ws.send(ws_send_queue.popleft())
                            except IndexError:
                                break

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
                                    state.last_motion_time = time.time()
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
                                music_player.stop()

                            elif msg_type == 'music_volume':
                                vol = msg.get('level', 50)
                                print(f'[Music] Volume: {vol}')
                                music_player.set_volume(vol)

                            elif msg_type == 'emote':
                                for e in msg.get('emotes', []):
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
                                # Speaker reads config.VOLUME dynamically on each play,
                                # so mutating it here actually changes playback volume.
                                config.VOLUME = max(0, min(1000, msg.get('volume', msg.get('level', 80))))
                                print(f"[Volume] Set to {config.VOLUME}")

                            elif msg_type == 'servo':
                                pan = msg.get('pan', 90)
                                tilt = msg.get('tilt', 90)
                                if servo_controller:
                                    servo_controller.look_at(pan, tilt)

                            elif msg_type == 'wake':
                                do_wake('server', ws_send_queue)

                            elif msg_type == 'audio_output':
                                target = msg.get('target', 'internal')
                                if target in ('external', 'aux', 'headphone'):
                                    speaker.audio_output = Speaker.OUTPUT_EXTERNAL
                                    speaker.use_pulse = False
                                    print('[Speaker] Switched to external speaker')
                                elif target in ('internal', 'usb'):
                                    speaker.audio_output = Speaker.OUTPUT_INTERNAL
                                    speaker.use_pulse = False
                                    print('[Speaker] Switched to internal speaker')
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
                                BT_MAC = '49:ED:E8:CC:23:3D'

                                def bt_connect():
                                    def run_(cmd):
                                        r = sp.run(cmd, capture_output=True, text=True, timeout=15)
                                        print(f'[BT] {" ".join(cmd)} → {r.stdout.strip()} {r.stderr.strip()}')
                                        return r
                                    run_(['bluetoothctl', 'power', 'on'])
                                    r = run_(['bluetoothctl', 'connect', BT_MAC])
                                    if 'successful' in r.stdout.lower():
                                        time.sleep(2)
                                        r = run_(['pactl', 'list', 'sinks', 'short'])
                                        for line in r.stdout.split('\n'):
                                            if 'bluez' in line:
                                                sink = line.split('\t')[1] if '\t' in line else line.split()[1]
                                                run_(['pactl', 'set-default-sink', sink])
                                                return sink
                                    run_(['bluetoothctl', 'remove', BT_MAC])
                                    time.sleep(1)
                                    run_(['bluetoothctl', '--timeout', '12', 'scan', 'on'])
                                    r = run_(['bluetoothctl', 'devices'])
                                    if BT_MAC not in r.stdout:
                                        return None
                                    run_(['bluetoothctl', 'pair', BT_MAC]); time.sleep(1)
                                    run_(['bluetoothctl', 'trust', BT_MAC]); time.sleep(1)
                                    run_(['bluetoothctl', 'connect', BT_MAC]); time.sleep(3)
                                    r = run_(['pactl', 'list', 'sinks', 'short'])
                                    for line in r.stdout.split('\n'):
                                        if 'bluez' in line:
                                            sink = line.split('\t')[1] if '\t' in line else line.split()[1]
                                            run_(['pactl', 'set-default-sink', sink])
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
                                import subprocess
                                try:
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

                            elif msg_type == 'interrupt':
                                try:
                                    speaker.interrupt()
                                    print('[Droid] INTERRUPT — playback killed')
                                except Exception as e:
                                    print(f'[Droid] Interrupt error: {e}')

                            elif msg_type == 'mic_off':
                                mic.enabled = False
                                print('[Droid] Mic OFF (stream stays alive)')

                            elif msg_type == 'mic_on':
                                mic.enabled = True
                                print('[Droid] Mic ON')

                            elif msg_type == 'wifi_scan':
                                import subprocess as sp, re
                                try:
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
                                    cmd = ['nmcli', 'device', 'wifi', 'connect', ssid, 'ifname', 'wlan0']
                                    if pw:
                                        cmd += ['password', pw]
                                    result = sp.run(cmd, capture_output=True, text=True, timeout=30)
                                    success = result.returncode == 0
                                    if success:
                                        sp.run(['nmcli', 'connection', 'modify', ssid,
                                                'connection.autoconnect', 'yes',
                                                'connection.autoconnect-retries', '0'],
                                               capture_output=True, timeout=5)
                                    await ws.send(json.dumps({
                                        'type': 'wifi_connect_result', 'success': success, 'ssid': ssid,
                                        'error': '' if success else result.stderr.strip() or result.stdout.strip(),
                                    }))
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
                                    sp.run(['bluetoothctl', 'trust', address], capture_output=True, text=True, timeout=5)
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
                                # Save AP config for wifi-manager. Path computed from this
                                # file's location so any user can install, not just mrcdcox.
                                ap_ssid = msg.get('ssid', 'Droid-Setup')
                                ap_pw = msg.get('password', 'droid1234')
                                try:
                                    ap_conf = {'ssid': ap_ssid, 'password': ap_pw}
                                    ap_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'ap-config.json')
                                    with open(ap_path, 'w') as f:
                                        json.dump(ap_conf, f)
                                    await ws.send(json.dumps({'type': 'ap_config_result', 'success': True}))
                                    print(f'[Droid] AP config saved: {ap_ssid}')
                                except Exception as e:
                                    await ws.send(json.dumps({'type': 'ap_config_result', 'success': False, 'error': str(e)}))

                            elif msg_type == 'ping':
                                await ws.send(json.dumps({'type': 'pong'}))

                            elif msg_type == 'ota_update':
                                print('[OTA] Update requested by server')
                                try:
                                    import subprocess as sp_ota
                                    work_dir = os.path.dirname(os.path.abspath(__file__))
                                    result = sp_ota.run(['git', 'pull', 'origin', 'main'],
                                                        capture_output=True, text=True, timeout=60, cwd=work_dir)
                                    pull_output = result.stdout.strip() + result.stderr.strip()
                                    print(f'[OTA] git pull: {pull_output}')
                                    if 'Already up to date' in pull_output:
                                        await ws.send(json.dumps({
                                            'type': 'ota_result', 'success': True,
                                            'message': 'Already up to date', 'version': CLIENT_VERSION,
                                        }))
                                    else:
                                        await ws.send(json.dumps({
                                            'type': 'ota_result', 'success': True,
                                            'message': 'Updated. Restarting...',
                                            'output': pull_output[:500],
                                        }))
                                        print('[OTA] Restarting droid service...')
                                        sp_ota.Popen(['sudo', 'systemctl', 'restart', 'droid'],
                                                     stdout=sp_ota.DEVNULL, stderr=sp_ota.DEVNULL)
                                except Exception as ota_err:
                                    print(f'[OTA] Error: {ota_err}')
                                    await ws.send(json.dumps({
                                        'type': 'ota_result', 'success': False, 'message': str(ota_err),
                                    }))

                            elif msg_type == 'ota_version':
                                await ws.send(json.dumps({'type': 'ota_version_result', 'version': CLIENT_VERSION}))

                        except asyncio.TimeoutError:
                            pass

                        now = time.time()

                        if state.sleep_state == 'awake':
                            # === AWAKE MODE ===
                            if (servo_controller and servo_controller.enabled and
                                    servo_controller.kit is not None and camera.enabled and
                                    camera.cap is not None and camera.cap.isOpened()):
                                if not hasattr(camera, '_last_track_time'):
                                    camera._last_track_time = 0
                                if now - camera._last_track_time >= 1.0:
                                    ret, track_frame = camera.cap.read()
                                    if ret and track_frame is not None:
                                        tracked = False
                                        face = face_tracker.detect(track_frame)
                                        if face:
                                            cx, cy, fw, fh = face
                                            servo_controller.track_face(cx, cy, track_frame.shape[1], track_frame.shape[0])
                                            tracked = True
                                        if not tracked and face_tracker.frames_without_face > 30:
                                            servo_controller.center()
                                    camera._last_track_time = now

                            # Send camera frame + check motion
                            if now - last_frame_time >= FRAME_INTERVAL:
                                if camera.enabled and (camera.cap is None or not camera.cap.isOpened()):
                                    camera._open_retries += 1
                                    if camera._open_retries % 6 == 1:
                                        camera._open()
                                frame, jpeg = camera.capture_frame()
                                if jpeg:
                                    if frame is not None and SLEEP_ENABLED:
                                        if detect_motion(frame):
                                            state.last_motion_time = now
                                    servo_moving = servo_controller and (now - servo_controller.last_move_time) < 2.0
                                    await ws.send(json.dumps({
                                        'type': 'frame',
                                        'data': base64.b64encode(jpeg).decode('ascii'),
                                        'timestamp': now,
                                        'servo_moving': servo_moving,
                                    }))
                                    last_frame_time = now

                            # Send audio to server for STT
                            audio = mic.get_audio()
                            if audio:
                                rms = compute_rms(audio)
                                # Skip idle reset while droid is speaking — its own
                                # voice shouldn't count as "someone is here."
                                if rms > RMS_THRESHOLD and not state.is_speaking:
                                    state.last_motion_time = now
                                await ws.send(json.dumps({
                                    'type': 'audio',
                                    'data': base64.b64encode(audio).decode('ascii'),
                                    'sample_rate': SAMPLE_RATE,
                                    'channels': CHANNELS,
                                    'format': 'pcm_s16le',
                                }))

                            if SLEEP_ENABLED and (now - state.last_motion_time) > IDLE_TIMEOUT and (now - state.boot_time) > 120:
                                do_sleep(ws_send_queue)
                                continue

                        else:
                            # === SLEEPING MODE ===
                            audio = mic.get_audio()
                            if audio:
                                rms = compute_rms(audio)
                                if rms > RMS_THRESHOLD:
                                    if state.noise_start_time is None:
                                        state.noise_start_time = now
                                    elif now - state.noise_start_time >= WAKE_DEBOUNCE:
                                        do_wake('noise', ws_send_queue)
                                else:
                                    state.noise_start_time = None

                        # Keep audio device alive (every 10s) — but only while not
                        # actively playing, to avoid stomping on real audio.
                        if now - last_keepalive > 10:
                            last_keepalive = now
                            if not state.is_speaking:
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
    if servo_controller:
        servo_controller.close()
    print("[Droid] Shutdown complete")


if __name__ == '__main__':
    print("=" * 40)
    print("  DROID Pi Client")
    print("  Camera + Mic -> Server -> Speaker")
    print("  Sleep/Wake: motion + noise detection")
    print("=" * 40)
    asyncio.run(run())
