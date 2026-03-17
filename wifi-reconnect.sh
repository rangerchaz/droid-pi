#!/bin/bash
# WiFi auto-reconnect for droid Pi client
# Add to crontab: */1 * * * * /home/mrcdcox/droid-pi/wifi-reconnect.sh
#
# Supports multiple known networks — add them via:
#   sudo nmcli dev wifi connect "YourSSID" password "YourPass"
# or edit /etc/wpa_supplicant/wpa_supplicant.conf

PING_TARGET="8.8.8.8"
WLAN="wlan0"
LOG="/tmp/wifi-reconnect.log"

# Check if we have internet
if ping -c 1 -W 3 "$PING_TARGET" > /dev/null 2>&1; then
    exit 0
fi

echo "$(date): WiFi down — reconnecting..." >> "$LOG"

# Try to reconnect
sudo ifconfig "$WLAN" down
sleep 2
sudo ifconfig "$WLAN" up
sleep 5

# Wait for connection
for i in 1 2 3 4 5; do
    if ping -c 1 -W 3 "$PING_TARGET" > /dev/null 2>&1; then
        echo "$(date): WiFi restored on attempt $i" >> "$LOG"
        exit 0
    fi
    sleep 3
done

# Nuclear option — full network restart
echo "$(date): Hard restart network..." >> "$LOG"
sudo systemctl restart networking 2>/dev/null
sudo systemctl restart dhcpcd 2>/dev/null
sleep 10

if ping -c 1 -W 3 "$PING_TARGET" > /dev/null 2>&1; then
    echo "$(date): WiFi restored after hard restart" >> "$LOG"
else
    echo "$(date): WiFi STILL DOWN — manual intervention needed" >> "$LOG"
fi
