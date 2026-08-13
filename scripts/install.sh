#!/usr/bin/env bash
# Printer HQ — install as systemd service + (optional) register CUPS printer.
#
#   sudo ./install.sh              # install + enable the web UI service
#   sudo ./install.sh --setup-cups # also register the CUPS queue via IPP proxy
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE="arch-wifi-printer-webui.service"
RUN_USER="${SUDO_USER:-$USER}"
RUN_GROUP="$(id -gn "$RUN_USER")"
QUEUE="${PWEBUI_CUPS_QUEUE:-Brother-DCP-T720DW}"

echo "[*] Printer HQ installer"
echo "    App dir  : $APP_DIR"
echo "    Run as   : $RUN_USER:$RUN_GROUP"
echo "    CUPS queue: $QUEUE"

# 1) Render systemd unit with the real app path + user
TMP_SVC="$(mktemp)"
python3 - "$APP_DIR/systemd/$SERVICE" "$TMP_SVC" "$APP_DIR" "$RUN_USER" "$RUN_GROUP" "$QUEUE" <<'PYEOF'
import sys
src, dst, appdir, user, group, queue = sys.argv[1:7]
out = []
for line in open(src):
    line = line.rstrip()
    if line.startswith("ExecStart="):
        line = f"ExecStart=/usr/bin/python3 {appdir}/app/server.py"
    elif line.startswith("User="):
        line = f"User={user}"
    elif line.startswith("Group="):
        line = f"Group={group}"
    elif line.startswith("Environment=PWEBUI_CUPS_QUEUE="):
        line = f"Environment=PWEBUI_CUPS_QUEUE={queue}"
    out.append(line)
open(dst, "w").write("\n".join(out) + "\n")
PYEOF

sudo cp "$TMP_SVC" /etc/systemd/system/$SERVICE
rm -f "$TMP_SVC"
echo "[*] Installed /etc/systemd/system/$SERVICE"

# 2) Optional CUPS registration
if [[ "${1:-}" == "--setup-cups" ]]; then
    echo "[*] Registering CUPS queue '$QUEUE' (IPP via IPv6 proxy on 127.0.0.1:3631)..."
    sudo -u "$RUN_USER" lpadmin -p "$QUEUE" -E \
        -v "ipp://127.0.0.1:3631/ipp/print" -m everywhere \
        && echo "    queue added" \
        && lpadmin -p "$QUEUE" -E 2>/dev/null || true
    sudo -u "$RUN_USER" lpoptions -d "$QUEUE" 2>/dev/null || true
    echo "[*] Grant passwordless sudo for enable/disable actions (optional):"
    echo "    sudo visudo -f /etc/sudoers.d/pwebui"
    echo "    $RUN_USER ALL=(ALL) NOPASSWD: /usr/bin/cupsenable, /usr/bin/cupsdisable, /usr/bin/lpoptions"
fi

# 3) Enable + start service
sudo systemctl daemon-reload
sudo systemctl enable --now "$SERVICE" || echo "[-] Start gagal — cek: journalctl -u $SERVICE -e"

echo ""
echo "[+] Done. Dashboard: http://127.0.0.1:8642"
echo "    Printer web admin: http://127.0.0.1:8080"
echo "    Logs: journalctl -u $SERVICE -f"