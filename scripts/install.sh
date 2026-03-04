#!/usr/bin/env bash
set -euo pipefail

echo "[1/7] apt update"
sudo apt update

echo "[2/7] install base packages + BLE + Python deps + OpenCV"
sudo apt install -y \
  git \
  bluez \
  python3 \
  python3-dbus \
  python3-gi \
  python3-gpiozero \
  python3-opencv \
  python3-numpy \
  ffmpeg \
  v4l-utils

echo "[3/7] enable bluetooth service"
sudo systemctl enable --now bluetooth

echo "[4/7] add user to useful groups (for manual testing without sudo)"
# (Service runs as root anyway, but this helps when you run python manually)
sudo usermod -aG video,bluetooth,gpio user || true

echo "[5/7] show versions"
echo "bluez:"; bluetoothctl -v || true
echo "python:"; python3 --version
echo "opencv:"; python3 - << 'EOF'
import cv2
print(cv2.__version__)
EOF
echo "ffmpeg:"; ffmpeg -version | head -n 1
echo "kernel:"; uname -a

echo "[6/7] create recordings dir"
sudo mkdir -p /home/user/camerarec
sudo chmod 755 /home/user/camerarec
sudo chown user:user /home/user/camerarec

echo "[7/7] quick camera sanity check (non-fatal)"
python3 - << 'EOF' || true
import cv2
cap = cv2.VideoCapture("/dev/video0", cv2.CAP_V4L2)
print("Opened:", cap.isOpened())
ret, frame = cap.read()
print("Frame:", ret, frame.shape if ret else None)
cap.release()
EOF

bash scripts/dump_env.sh

echo "Done."
echo "Next: run scripts/enable_bluetooth_experimental.sh and reboot."
echo "Note: group changes take effect after logout/login or reboot."
