#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${ROOT}/docs/system_versions.txt"

mkdir -p "${ROOT}/docs"

{
  echo "=============================="
  echo " SYSTEM ENVIRONMENT SNAPSHOT "
  echo "=============================="
  date
  echo

  echo "### OS RELEASE"
  cat /etc/os-release
  echo

  echo "### KERNEL"
  uname -a
  echo

  echo "### HARDWARE"
  cat /proc/cpuinfo | grep -E "Model|Hardware|Revision" || true
  echo

  echo "### PYTHON"
  python3 --version
  command -v python3 || true
  python3 - << 'EOF'
import sys, platform
print("Executable:", sys.executable)
print("Full version:", sys.version)
print("Platform:", platform.platform())
EOF
  echo

  echo "### APT PACKAGES (RELEVANT)"
  dpkg -l | grep -E "bluez|firmware-brcm80211|ffmpeg|v4l-utils|python3-dbus|python3-gi" || true
  echo

  echo "### BLUETOOTHD PROCESS"
  ps aux | grep bluetoothd | grep -v grep || true
  echo

  echo "### HCI STATUS"
  hciconfig hci0 || true
  echo

  echo "### BLUEZ DBUS MANAGERS"
  busctl introspect org.bluez /org/bluez/hci0 org.bluez.GattManager1 | head -n 10 || true
  busctl introspect org.bluez /org/bluez/hci0 org.bluez.LEAdvertisingManager1 | head -n 10 || true
  echo

  echo "### BROADCOM FIRMWARE FILES"
  ls -lh /lib/firmware/brcm | grep -i bcm || true
  echo

  echo "### CAMERA CAPABILITIES (/dev/video0)"
  v4l2-ctl -d /dev/video0 --list-formats-ext || true
  echo

  echo "### FFMPEG VERSION"
  ffmpeg -version | head -n 5 || true
  echo

  echo "### END OF SNAPSHOT"
} > "${OUT}"

echo "Wrote: ${OUT}"

