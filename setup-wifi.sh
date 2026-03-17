#!/bin/bash
# Set up known WiFi networks from config.json
# Run once on the Pi: sudo bash setup-wifi.sh

CONFIG="config.json"

if [ ! -f "$CONFIG" ]; then
    echo "No config.json found"
    exit 1
fi

# Parse wifi_networks from config and add each one
python3 -c "
import json
with open('$CONFIG') as f:
    cfg = json.load(f)
networks = cfg.get('wifi_networks', {})
for ssid, password in networks.items():
    print(f'{ssid}|{password}')
" | while IFS='|' read -r ssid password; do
    echo "Adding network: $ssid"
    sudo nmcli dev wifi connect "$ssid" password "$password" 2>/dev/null || \
    sudo bash -c "cat >> /etc/wpa_supplicant/wpa_supplicant.conf << EOF

network={
    ssid=\"$ssid\"
    psk=\"$password\"
    key_mgmt=WPA-PSK
}
EOF"
    echo "  Added $ssid"
done

echo ""
echo "Done. Networks added. The Pi will auto-connect to whichever is available."
echo ""
echo "To enable auto-reconnect, add to crontab:"
echo "  sudo crontab -e"
echo "  */1 * * * * /home/mrcdcox/droid-pi/wifi-reconnect.sh"
