#!/bin/bash
# Removes Pironman Pilot. Its overclock and fan-curve lines are taken out of
# config.txt (a backup is kept), so the Pi goes back to stock after a reboot.
[ "$EUID" -eq 0 ] || exec sudo bash "$0" "$@"
BOOT=/boot/firmware; [ -d $BOOT ] || BOOT=/boot
USER_HOME="$(getent passwd "${SUDO_USER:-pi}" | cut -d: -f6)"

if grep -q '"oled_mode": "pilot"' /etc/pironman-pilot/settings.json 2>/dev/null; then
  echo "==> Giving the OLED back to SunFounder's pages"
  pironman5 -oe true >/dev/null 2>&1 && systemctl restart pironman5.service
fi
systemctl disable --now pironman-pilot.service 2>/dev/null
if grep -q '"side_ctrl": "pilot"' /etc/pironman-pilot/settings.json 2>/dev/null; then
  echo "==> Giving the side fans back to SunFounder (GPIO6)"
  pironman5 -gp 6 >/dev/null 2>&1 && systemctl restart pironman5.service
fi
rm -f /etc/systemd/system/pironman-pilot.service && systemctl daemon-reload

if grep -q "Pironman Pilot" $BOOT/config.txt 2>/dev/null; then
  cp $BOOT/config.txt $BOOT/config.txt.before-pilot-uninstall
  python3 - "$BOOT/config.txt" <<'EOF'
import re, sys
p = sys.argv[1]; t = open(p).read()
for b, e in (("overclock", "overclock"), ("fan curve", "fan curve")):
    t = re.sub(r"\n?# >>> Pironman Pilot %s >>>.*?# <<< Pironman Pilot %s <<<[^\n]*\n?" % (b, e), "\n", t, flags=re.S)
open(p, "w").write(t.rstrip("\n") + "\n")
EOF
  echo "==> Removed Pilot's overclock/fan lines from config.txt (backup: config.txt.before-pilot-uninstall)"
fi
rm -f $BOOT/tryboot.txt /usr/local/bin/pironman-pilot /usr/share/applications/pironman-pilot.desktop \
      /usr/share/icons/hicolor/scalable/apps/pironman-pilot.svg "$USER_HOME/Desktop/pironman-pilot.desktop"
rm -rf /opt/pironman-pilot /etc/default/pironman-pilot
echo "Pironman Pilot removed. Settings + config backups kept in /etc/pironman-pilot (delete it if you like)."
echo "Reboot to return to stock clocks."
