#!/usr/bin/env python3
"""
Droid WiFi Manager — AP fallback + config portal.

On boot:
1. Wait for WiFi connection (30s)
2. If no WiFi → start AP mode (Droid-Setup) + config web server
3. User connects, picks a network, enters password
4. Pi connects to new network, stops AP, restarts droid service

Also runs a periodic check — if WiFi drops for >60s, switch to AP mode.
"""

import subprocess
import time
import json
import os
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

AP_SSID = "Droid-Setup"
AP_PASSWORD = "droid1234"
AP_IP = "192.168.4.1"
CONFIG_PORT = 80
CHECK_INTERVAL = 30  # seconds between WiFi checks
BOOT_WAIT = 30  # seconds to wait for WiFi on boot
RECONNECT_TIMEOUT = 60  # seconds of no WiFi before AP mode

ap_active = False


def is_wifi_connected():
    """Check if wlan0 has an active WiFi connection (not AP)."""
    try:
        result = subprocess.run(
            ['nmcli', '-t', '-f', 'DEVICE,TYPE,STATE', 'device'],
            capture_output=True, text=True, timeout=5
        )
        for line in result.stdout.strip().split('\n'):
            parts = line.split(':')
            if len(parts) >= 3 and parts[0] == 'wlan0' and parts[2] == 'connected':
                # Make sure it's not our own AP
                conn = subprocess.run(
                    ['nmcli', '-t', '-f', 'NAME', 'connection', 'show', '--active'],
                    capture_output=True, text=True, timeout=5
                )
                if AP_SSID not in conn.stdout:
                    return True
        return False
    except Exception:
        return False


def get_current_ssid():
    """Get the SSID we're currently connected to."""
    try:
        result = subprocess.run(
            ['nmcli', '-t', '-f', 'active,ssid', 'device', 'wifi'],
            capture_output=True, text=True, timeout=5
        )
        for line in result.stdout.strip().split('\n'):
            if line.startswith('yes:'):
                return line.split(':', 1)[1]
    except Exception:
        pass
    return None


def scan_networks():
    """Scan for available WiFi networks."""
    try:
        subprocess.run(['nmcli', 'device', 'wifi', 'rescan'], capture_output=True, timeout=10)
        time.sleep(2)
        result = subprocess.run(
            ['nmcli', '-t', '-f', 'SSID,SIGNAL,SECURITY', 'device', 'wifi', 'list'],
            capture_output=True, text=True, timeout=10
        )
        networks = []
        seen = set()
        for line in result.stdout.strip().split('\n'):
            parts = line.split(':')
            if len(parts) >= 3 and parts[0] and parts[0] not in seen and parts[0] != AP_SSID:
                seen.add(parts[0])
                networks.append({
                    'ssid': parts[0],
                    'signal': int(parts[1]) if parts[1].isdigit() else 0,
                    'security': parts[2] if parts[2] else 'Open'
                })
        networks.sort(key=lambda x: x['signal'], reverse=True)
        return networks
    except Exception as e:
        print(f"[WiFi] Scan error: {e}")
        return []


def start_ap():
    """Start AP mode using NetworkManager."""
    global ap_active
    if ap_active:
        return
    print(f"[WiFi] Starting AP: {AP_SSID}")
    try:
        # Create hotspot
        subprocess.run([
            'nmcli', 'device', 'wifi', 'hotspot',
            'ifname', 'wlan0',
            'ssid', AP_SSID,
            'password', AP_PASSWORD
        ], capture_output=True, text=True, timeout=15)
        
        # Set the IP
        subprocess.run([
            'nmcli', 'connection', 'modify', 'Hotspot',
            'ipv4.addresses', f'{AP_IP}/24',
            'ipv4.method', 'shared'
        ], capture_output=True, timeout=5)
        
        ap_active = True
        print(f"[WiFi] AP active: {AP_SSID} / {AP_PASSWORD}")
        print(f"[WiFi] Config portal: http://{AP_IP}")
    except Exception as e:
        print(f"[WiFi] AP start failed: {e}")


def stop_ap():
    """Stop AP mode."""
    global ap_active
    if not ap_active:
        return
    print("[WiFi] Stopping AP")
    try:
        subprocess.run(
            ['nmcli', 'connection', 'down', 'Hotspot'],
            capture_output=True, timeout=5
        )
        subprocess.run(
            ['nmcli', 'connection', 'delete', 'Hotspot'],
            capture_output=True, timeout=5
        )
        ap_active = False
    except Exception:
        pass


def connect_wifi(ssid, password):
    """Connect to a WiFi network."""
    print(f"[WiFi] Connecting to: {ssid}")
    try:
        # Stop AP first
        stop_ap()
        time.sleep(2)
        
        # Try to connect (creates profile if new)
        result = subprocess.run([
            'nmcli', 'device', 'wifi', 'connect', ssid,
            'password', password,
            'ifname', 'wlan0'
        ], capture_output=True, text=True, timeout=30)
        
        if result.returncode == 0:
            # Set infinite retries on the new connection
            subprocess.run([
                'nmcli', 'connection', 'modify', ssid,
                'connection.autoconnect', 'yes',
                'connection.autoconnect-retries', '0'
            ], capture_output=True, timeout=5)
            print(f"[WiFi] Connected to {ssid}")
            
            # Restart droid service
            subprocess.run(['sudo', 'systemctl', 'restart', 'droid'],
                         capture_output=True, timeout=10)
            return True, "Connected!"
        else:
            error = result.stderr.strip() or result.stdout.strip()
            print(f"[WiFi] Connection failed: {error}")
            # Restart AP since we stopped it
            start_ap()
            return False, error
    except Exception as e:
        print(f"[WiFi] Connection error: {e}")
        start_ap()
        return False, str(e)


# ── Config Portal Web Server ──

CONFIG_HTML = """<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Droid WiFi Setup</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { font-family: -apple-system, sans-serif; background: #1a1a2e; color: #e0e0e0; padding: 20px; }
        .container { max-width: 400px; margin: 0 auto; }
        h1 { color: #f5a623; text-align: center; margin-bottom: 8px; font-size: 1.5rem; }
        .subtitle { text-align: center; color: #888; margin-bottom: 24px; font-size: 0.9rem; }
        .network { background: #16213e; border-radius: 8px; padding: 14px; margin-bottom: 8px;
                   cursor: pointer; display: flex; justify-content: space-between; align-items: center;
                   border: 1px solid #0f3460; transition: border-color 0.2s; }
        .network:hover { border-color: #f5a623; }
        .network.selected { border-color: #f5a623; background: #1a2a4e; }
        .network-name { font-weight: 600; }
        .network-info { font-size: 0.8rem; color: #888; }
        .signal { font-size: 0.9rem; }
        .form-group { margin-top: 16px; }
        label { display: block; margin-bottom: 6px; color: #aaa; font-size: 0.85rem; }
        input[type=password], input[type=text] {
            width: 100%; padding: 12px; border-radius: 6px; border: 1px solid #0f3460;
            background: #16213e; color: #e0e0e0; font-size: 1rem; }
        button { width: 100%; padding: 14px; border-radius: 6px; border: none;
                background: #f5a623; color: #1a1a2e; font-size: 1rem; font-weight: 700;
                cursor: pointer; margin-top: 16px; }
        button:hover { background: #e6951a; }
        button:disabled { opacity: 0.5; cursor: not-allowed; }
        .status { text-align: center; padding: 12px; margin-top: 12px; border-radius: 6px; }
        .status.success { background: #1a4a2e; color: #4caf50; }
        .status.error { background: #4a1a1a; color: #e74c3c; }
        .refresh { text-align: center; margin-top: 12px; }
        .refresh a { color: #f5a623; text-decoration: none; font-size: 0.85rem; }
        .loader { display: none; text-align: center; padding: 20px; }
        .loader.active { display: block; }
    </style>
</head>
<body>
    <div class="container">
        <h1>🤖 Droid WiFi Setup</h1>
        <p class="subtitle">Select a network to connect your droid</p>
        
        <div id="networks">Loading networks...</div>
        
        <div id="connect-form" style="display:none;">
            <div class="form-group">
                <label>Selected: <strong id="selected-ssid"></strong></label>
            </div>
            <div class="form-group" id="password-group">
                <label>Password</label>
                <input type="password" id="password" placeholder="Enter WiFi password">
            </div>
            <button id="connect-btn" onclick="connectWifi()">Connect</button>
        </div>
        
        <div id="status"></div>
        <div class="loader" id="loader">Connecting... Please wait.</div>
        <div class="refresh"><a href="/" onclick="location.reload();return false;">↻ Refresh networks</a></div>
    </div>
    
    <script>
        var selectedSSID = '';
        var selectedSecurity = '';
        
        fetch('/api/networks')
            .then(r => r.json())
            .then(networks => {
                var html = '';
                networks.forEach(n => {
                    var bars = n.signal > 75 ? '▂▄▆█' : n.signal > 50 ? '▂▄▆' : n.signal > 25 ? '▂▄' : '▂';
                    html += '<div class="network" onclick="selectNetwork(\\''+n.ssid+'\\', \\''+n.security+'\\', this)">' +
                        '<div><div class="network-name">'+n.ssid+'</div>' +
                        '<div class="network-info">'+n.security+'</div></div>' +
                        '<div class="signal" title="'+n.signal+'%">'+bars+'</div></div>';
                });
                document.getElementById('networks').innerHTML = html || '<p style="text-align:center;color:#888">No networks found. Try refreshing.</p>';
            });
        
        function selectNetwork(ssid, security, el) {
            selectedSSID = ssid;
            selectedSecurity = security;
            document.querySelectorAll('.network').forEach(n => n.classList.remove('selected'));
            el.classList.add('selected');
            document.getElementById('selected-ssid').textContent = ssid;
            document.getElementById('connect-form').style.display = 'block';
            document.getElementById('password-group').style.display = security === 'Open' ? 'none' : 'block';
        }
        
        function connectWifi() {
            var pw = document.getElementById('password').value;
            document.getElementById('connect-btn').disabled = true;
            document.getElementById('loader').classList.add('active');
            document.getElementById('status').innerHTML = '';
            
            fetch('/api/connect', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ssid: selectedSSID, password: pw})
            })
            .then(r => r.json())
            .then(result => {
                document.getElementById('loader').classList.remove('active');
                document.getElementById('connect-btn').disabled = false;
                if (result.success) {
                    document.getElementById('status').innerHTML = '<div class="status success">✅ Connected to ' + selectedSSID + '! Droid is restarting...</div>';
                } else {
                    document.getElementById('status').innerHTML = '<div class="status error">❌ ' + result.error + '</div>';
                }
            })
            .catch(() => {
                document.getElementById('loader').classList.remove('active');
                document.getElementById('status').innerHTML = '<div class="status success">✅ Connecting... If this page stops loading, the droid switched networks successfully!</div>';
            });
        }
    </script>
</body>
</html>"""


class ConfigHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # Suppress default logging

    def do_GET(self):
        path = urlparse(self.path).path
        if path == '/api/networks':
            networks = scan_networks()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(networks).encode())
        else:
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.end_headers()
            self.wfile.write(CONFIG_HTML.encode())

    def do_POST(self):
        if self.path == '/api/connect':
            length = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(length))
            ssid = body.get('ssid', '')
            password = body.get('password', '')
            
            success, msg = connect_wifi(ssid, password)
            
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({
                'success': success,
                'error': '' if success else msg
            }).encode())


def run_config_server():
    """Start the config portal web server."""
    server = HTTPServer(('0.0.0.0', CONFIG_PORT), ConfigHandler)
    print(f"[WiFi] Config portal running on port {CONFIG_PORT}")
    server.serve_forever()


def main():
    print("[WiFi Manager] Starting...")
    
    # Wait for WiFi on boot
    print(f"[WiFi] Waiting {BOOT_WAIT}s for WiFi connection...")
    for i in range(BOOT_WAIT):
        if is_wifi_connected():
            ssid = get_current_ssid()
            print(f"[WiFi] Connected to: {ssid}")
            break
        time.sleep(1)
    else:
        print("[WiFi] No WiFi connection — starting AP mode")
        start_ap()
        # Start config server in foreground
        run_config_server()
        return  # Config server runs until WiFi is configured
    
    # WiFi connected — monitor for drops
    disconnect_time = None
    while True:
        time.sleep(CHECK_INTERVAL)
        if is_wifi_connected():
            if disconnect_time:
                print("[WiFi] Reconnected!")
                disconnect_time = None
                if ap_active:
                    stop_ap()
        else:
            if disconnect_time is None:
                disconnect_time = time.time()
                print("[WiFi] Connection lost, waiting for reconnect...")
            elif time.time() - disconnect_time > RECONNECT_TIMEOUT:
                print(f"[WiFi] No WiFi for {RECONNECT_TIMEOUT}s — starting AP mode")
                start_ap()
                # Start config server
                server_thread = threading.Thread(target=run_config_server, daemon=True)
                server_thread.start()
                disconnect_time = None  # Reset so we don't keep restarting


if __name__ == '__main__':
    main()
