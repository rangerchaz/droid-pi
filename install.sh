#!/bin/bash
# Droid Pi Client — Quick Install
# Run: bash install.sh

set -e

USER_NAME=$(whoami)
USER_ID=$(id -u)
DROID_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "🤖 Installing Droid Pi Client"
echo "   User: $USER_NAME"
echo "   Dir:  $DROID_DIR"
echo ""

# Install system deps
echo "📦 Installing dependencies..."
sudo apt update -qq
sudo apt install -y -qq python3-pip python3-opencv python3-pyaudio \
  portaudio19-dev ffmpeg pulseaudio 2>/dev/null

pip3 install -q websockets 2>/dev/null || pip3 install --break-system-packages -q websockets

# Create config if needed
if [ ! -f "$DROID_DIR/config.json" ]; then
  cp "$DROID_DIR/config.example.json" "$DROID_DIR/config.json"
  echo ""
  echo "⚠️  Edit config.json and paste your device token:"
  echo "   nano $DROID_DIR/config.json"
  echo ""
  echo "   Get your token from: https://droid.turkeycode.ai"
  echo "   Dashboard → Hardware → Generate Device Token"
  echo ""
fi

# Enable linger so the user's session (and its PulseAudio) starts at boot
# rather than only on first login. Without this, /run/user/$USER_ID does not
# exist when droid.service starts on boot, and PulseAudio is unreachable
# until you log in interactively. With linger, the user session is created
# during boot and we can order droid after it.
echo "🔒 Enabling user-session linger so PulseAudio is up at boot..."
sudo loginctl enable-linger "$USER_NAME"

# Create systemd service
echo "⚙️  Setting up systemd service..."
cat > /tmp/droid.service << EOF
[Unit]
Description=Droid Client
# Order after the user session so /run/user/$USER_ID/{pulse,bus} exist before
# we start. enable-linger above makes user@$USER_ID.service activate at boot.
After=network-online.target user@$USER_ID.service
Wants=network-online.target user@$USER_ID.service

[Service]
Type=simple
User=$USER_NAME
ExecStart=/usr/bin/python3 $DROID_DIR/droid-client.py
WorkingDirectory=$DROID_DIR
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1
Environment=HOME=/home/$USER_NAME
Environment=XDG_RUNTIME_DIR=/run/user/$USER_ID
Environment=DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$USER_ID/bus
Environment=PULSE_SERVER=unix:/run/user/$USER_ID/pulse/native

[Install]
WantedBy=multi-user.target
EOF

sudo cp /tmp/droid.service /etc/systemd/system/droid.service

# WiFi manager service — uses placeholder __INSTALL_DIR__ in the template
if [ -f "$DROID_DIR/droid-wifi.service" ]; then
    echo "⚙️  Installing droid-wifi.service..."
    sed "s|__INSTALL_DIR__|$DROID_DIR|g" "$DROID_DIR/droid-wifi.service" \
        | sudo tee /etc/systemd/system/droid-wifi.service >/dev/null
    sudo systemctl enable droid-wifi >/dev/null 2>&1 || true
fi

sudo systemctl daemon-reload
sudo systemctl enable droid

echo ""
echo "✅ Droid installed!"
echo ""
echo "   To start now:  sudo systemctl start droid"
echo "   View logs:     sudo journalctl -u droid -f"
echo "   Test first:    python3 $DROID_DIR/droid-client.py"
echo ""

# Check if token is set
TOKEN=$(python3 -c "import json; print(json.load(open('$DROID_DIR/config.json')).get('token',''))" 2>/dev/null || echo "")
if [ "$TOKEN" = "paste-your-device-token-here" ] || [ -z "$TOKEN" ]; then
  echo "⚠️  Don't forget to add your device token to config.json!"
  echo "   Get it from: https://droid.turkeycode.ai → Dashboard → Hardware"
fi
