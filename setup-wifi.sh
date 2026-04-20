#!/bin/bash
# Set up known WiFi networks from config.json.
# Run once on the Pi: sudo bash setup-wifi.sh
#
# Uses `nmcli connection add` so networks are saved even when not currently
# in range — works for venue/guest networks you want the Pi to auto-connect
# to later.

set -u

DROID_DIR="$(cd "$(dirname "$0")" && pwd)"
CONFIG="$DROID_DIR/config.json"

if [ ! -f "$CONFIG" ]; then
    echo "No config.json at $CONFIG"
    exit 1
fi

# Emit one SSID|PASSWORD pair per line from config.json. Doing it in Python
# handles quoting/special chars correctly; the bash loop only reads the
# delimited pair into variables.
python3 - "$CONFIG" <<'PY' | while IFS='|' read -r ssid password; do
import json, sys
with open(sys.argv[1]) as f:
    cfg = json.load(f)
for ssid, password in (cfg.get('wifi_networks') or {}).items():
    # '|' is rejected as an SSID/WPA-PSK character by the kernel, so it's
    # safe as a delimiter.
    print(f'{ssid}|{password}')
PY
    [ -z "$ssid" ] && continue
    echo "Adding network: $ssid"

    # `device wifi connect` only works if the AP is in range. Use
    # `connection add` + `modify` so we can register networks for later.
    con_name="droid-$(echo -n "$ssid" | tr -c '[:alnum:]' '-')"
    if nmcli -t -f NAME connection show | grep -qx "$con_name"; then
        echo "  (already configured: $con_name)"
        continue
    fi
    sudo nmcli connection add \
        type wifi \
        ifname wlan0 \
        con-name "$con_name" \
        ssid "$ssid" \
        wifi-sec.key-mgmt wpa-psk \
        wifi-sec.psk "$password" \
        connection.autoconnect yes \
        >/dev/null \
        || {
            echo "  nmcli add failed, appending to wpa_supplicant.conf"
            sudo bash -c "cat >> /etc/wpa_supplicant/wpa_supplicant.conf" <<EOF

network={
    ssid=\"$ssid\"
    psk=\"$password\"
    key_mgmt=WPA-PSK
}
EOF
        }
    echo "  Added $ssid"
done

echo ""
echo "Done. The Pi will auto-connect to whichever saved network is in range."
echo ""
echo "To enable auto-reconnect, add to crontab:"
echo "  sudo crontab -e"
echo "  */1 * * * * $DROID_DIR/wifi-reconnect.sh"
