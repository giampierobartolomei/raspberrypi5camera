#!/usr/bin/env bash
set -euo pipefail

echo "== bluetoothd flags =="
ps aux | grep bluetoothd | grep -v grep || true

echo
echo "== hci0 status =="
hciconfig hci0 || true

echo
echo "== camera formats =="
v4l2-ctl -d /dev/video0 --list-formats-ext || true

echo
echo "== BlueZ managers =="
busctl introspect org.bluez /org/bluez/hci0 org.bluez.GattManager1 | head -n 5 || true
busctl introspect org.bluez /org/bluez/hci0 org.bluez.LEAdvertisingManager1 | head -n 5 || true

