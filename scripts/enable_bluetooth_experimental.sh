#!/usr/bin/env bash
set -euo pipefail

# Detect current ExecStart path
EXEC=$(systemctl cat bluetooth | grep -E '^ExecStart=' | head -n 1 | sed 's/^ExecStart=//')
if [[ -z "${EXEC}" ]]; then
  echo "Cannot detect bluetoothd ExecStart. Aborting."
  exit 1
fi

echo "Detected ExecStart: ${EXEC}"
echo "Writing override to /etc/systemd/system/bluetooth.service.d/override.conf"

sudo mkdir -p /etc/systemd/system/bluetooth.service.d
sudo tee /etc/systemd/system/bluetooth.service.d/override.conf >/dev/null <<EOF
[Service]
ExecStart=
ExecStart=${EXEC} --experimental -E
EOF

sudo systemctl daemon-reload
sudo systemctl restart bluetooth

echo "Verify:"
ps aux | grep bluetoothd | grep -v grep || true
echo "Done."

