#!/usr/bin/env bash
# Installs the grill thermometer web server as a systemd user service (sudo only needed for packages).
# Usage:  ./install.sh            -> install and start
#         ./install.sh uninstall  -> stop and remove the service (files and logs are kept)
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT_DIR="$HOME/.config/systemd/user"
UNIT="$UNIT_DIR/grill.service"

if [[ "${1:-}" == "uninstall" ]]; then
    systemctl --user disable --now grill.service 2>/dev/null || true
    rm -f "$UNIT"
    systemctl --user daemon-reload
    echo "Service removed."
    exit 0
fi

# 1) dependencies: BlueZ + Python D-Bus and GLib bindings (Debian/Ubuntu package names)
missing=()
command -v bluetoothctl >/dev/null || missing+=(bluez)
python3 -c 'import dbus' 2>/dev/null || missing+=(python3-dbus)
python3 -c 'import gi; from gi.repository import GLib' 2>/dev/null || missing+=(python3-gi)
if ((${#missing[@]})); then
    echo "Installing missing packages: ${missing[*]}"
    sudo apt-get install -y "${missing[@]}"
fi

# 2) Bluetooth must be powered on
bluetoothctl power on >/dev/null 2>&1 || echo "WARNING: could not power on the Bluetooth adapter."

# 3) systemd user service
mkdir -p "$UNIT_DIR"
cat > "$UNIT" <<EOF
[Unit]
Description=Grill thermometer web server (Grill5.0 BLE)
After=bluetooth.target network-online.target

[Service]
ExecStart=/usr/bin/python3 -u $DIR/grill_web.py
WorkingDirectory=$DIR
# Settings can be changed in $DIR/grill.env (see grill.env.example)
EnvironmentFile=-$DIR/grill.env
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now grill.service
systemctl --user restart grill.service

# 4) keep the service running without an active login (e.g. after a reboot)
if [[ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null)" != "yes" ]]; then
    loginctl enable-linger "$USER" 2>/dev/null || sudo loginctl enable-linger "$USER"
fi

sleep 2
systemctl --user --no-pager status grill.service | head -5
PORT="$(grep -s "^GRILL_PORT=" "$DIR/grill.env" | cut -d= -f2 || true)"
echo
echo "Done. Open: http://localhost:${PORT:-8080}  or  http://$(hostname -I | awk '{print $1}'):${PORT:-8080}"
