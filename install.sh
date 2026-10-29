#!/bin/bash
# Pironman Pilot installer - Raspberry Pi 5 in a SunFounder Pironman 5 MAX case
set -e
[ "$EUID" -eq 0 ] || exec sudo bash "$0" "$@"

SRC="$(cd "$(dirname "$0")" && pwd)"
DEST=/opt/pironman-pilot
USER_NAME="${SUDO_USER:-$(logname 2>/dev/null || echo pi)}"
USER_HOME="$(getent passwd "$USER_NAME" | cut -d: -f6)"

echo "==> Pironman Pilot installer"
grep -q "Raspberry Pi 5" /proc/device-tree/model 2>/dev/null || echo "   (note: this doesn't look like a Pi 5 - continuing anyway)"

echo "==> Installing helpers (Pillow, lgpio, i2c-tools)"
apt-get install -y python3-pil python3-lgpio i2c-tools fonts-dejavu-core >/dev/null || echo "   apt install had a problem - continuing"

echo "==> Making sure I2C is on (for the OLED)"
raspi-config nonint do_i2c 0 2>/dev/null || true

echo "==> Copying app to $DEST"
mkdir -p "$DEST/web"
cp "$SRC/pilot.py" "$SRC/icon.svg" "$SRC/pilot-open" "$DEST/"
cp "$SRC/web/index.html" "$DEST/web/"
chmod 755 "$DEST/pilot.py" "$DEST/pilot-open"
ln -sf "$DEST/pilot-open" /usr/local/bin/pironman-pilot

echo "==> Picking a free port for Pilot"
systemctl stop pironman-pilot.service 2>/dev/null || true
PORT=$(python3 - <<'PY'
import socket
for p in range(47821, 47900):
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", p)); print(p); break
    except OSError:
        pass
    finally:
        s.close()
PY
)
echo "PILOT_PORT=${PORT}" > /etc/default/pironman-pilot
echo "   using port ${PORT}"

echo "==> Installing background service"
cp "$SRC/pironman-pilot.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable pironman-pilot.service >/dev/null
systemctl restart pironman-pilot.service
sleep 2
if curl -s --max-time 3 "http://127.0.0.1:${PORT}/pilot-id" | grep -q pironman-pilot; then
  echo "   service is running"
else
  echo "!! service didn't answer - check: journalctl -u pironman-pilot -n 30"
fi

echo "==> Adding menu + desktop icon"
install -Dm644 "$SRC/icon.svg" /usr/share/icons/hicolor/scalable/apps/pironman-pilot.svg
install -Dm644 "$SRC/pironman-pilot.desktop" /usr/share/applications/pironman-pilot.desktop
if [ -d "$USER_HOME/Desktop" ]; then
  cp "$SRC/pironman-pilot.desktop" "$USER_HOME/Desktop/"
  chown "$USER_NAME:" "$USER_HOME/Desktop/pironman-pilot.desktop"
  chmod 755 "$USER_HOME/Desktop/pironman-pilot.desktop"
fi
gtk-update-icon-cache -q /usr/share/icons/hicolor 2>/dev/null || true

if ! command -v pironman5 >/dev/null 2>&1 && [ ! -e /usr/local/bin/pironman5 ]; then
  echo
  echo "!! SunFounder's pironman5 software was not found."
  echo "   Stats, the tower fan, custom OLED pages and overclocking work without it,"
  echo "   but the RGB LEDs, side fans and built-in OLED pages need it."
  echo "   Install it from SunFounder's Pironman 5 MAX guide, then reopen Pironman Pilot."
fi

echo
echo "Done!  Open 'Pironman Pilot' from the menu (System Tools) or the desktop icon,"
echo "or type:  pironman-pilot"
