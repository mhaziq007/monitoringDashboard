#!/usr/bin/env bash
#
# Installs the HomeLab Dashboard server (FastAPI backend + static frontend)
# as a systemd service. Run this on the collector box, from a checkout of
# this repository:
#
#   sudo ./scripts/install-server.sh
#
# Options:
#   --user <name>   system user to run the service as (default: current sudo user)
#   --port <port>   port to listen on (default: 8000)
#
set -euo pipefail

SERVICE_NAME="homelab-dashboard"
PORT=8000
RUN_USER="${SUDO_USER:-$(id -un)}"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --user) RUN_USER="$2"; shift 2 ;;
        --port) PORT="$2"; shift 2 ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done

if [[ $EUID -ne 0 ]]; then
    echo "This script needs root to install the systemd unit. Re-run with sudo." >&2
    exit 1
fi

# Resolve the repo root from this script's location, so the unit gets real
# absolute paths instead of the /path/to placeholders in the template.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
BACKEND_DIR="$REPO_ROOT/backend"
FRONTEND_DIR="$REPO_ROOT/frontend"
VENV_DIR="$BACKEND_DIR/.venv"

if [[ ! -f "$BACKEND_DIR/main.py" ]]; then
    echo "Could not find backend/main.py under $REPO_ROOT." >&2
    echo "Run this script from inside the cloned repository." >&2
    exit 1
fi

# main.py mounts ../frontend relative to itself; without it the UI 404s.
if [[ ! -d "$FRONTEND_DIR" ]]; then
    echo "Could not find frontend/ next to backend/ under $REPO_ROOT." >&2
    echo "The backend serves the UI from that directory; both must be present." >&2
    exit 1
fi

if ! id -u "$RUN_USER" >/dev/null 2>&1; then
    echo "User '$RUN_USER' does not exist." >&2
    exit 1
fi

echo "==> Installing into $REPO_ROOT (service user: $RUN_USER, port: $PORT)"

if ! command -v python3 >/dev/null 2>&1; then
    echo "python3 not found. Install Python 3.10+ and re-run." >&2
    exit 1
fi

# Rebuild the venv if it cannot run or was built for a different Python than
# the system has now; a distro upgrade otherwise leaves fastapi in the old
# version's site-packages and the service dies with ModuleNotFoundError.
SYS_PY_VER="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
if [[ -d "$VENV_DIR" ]]; then
    BUILT_VER="$(sed -nE 's/^version(_info)? *= *([0-9]+\.[0-9]+).*/\2/p' "$VENV_DIR/pyvenv.cfg" 2>/dev/null | head -n1)"
    if ! "$VENV_DIR/bin/python" -c '' 2>/dev/null || [[ "$BUILT_VER" != "$SYS_PY_VER" ]]; then
        echo "==> Rebuilding virtualenv (built for Python ${BUILT_VER:-unknown}, system has $SYS_PY_VER)"
        rm -rf "$VENV_DIR"
    fi
fi

echo "==> Creating virtualenv at $VENV_DIR"
if ! python3 -m venv "$VENV_DIR"; then
    echo "Could not create a virtualenv. On Debian/Ubuntu, install the venv module:" >&2
    echo "  sudo apt install python3-venv" >&2
    exit 1
fi
"$VENV_DIR/bin/python" -m pip install --quiet --upgrade pip
"$VENV_DIR/bin/python" -m pip install --quiet -r "$BACKEND_DIR/requirements.txt"

if ! "$VENV_DIR/bin/python" -c 'import fastapi, uvicorn' 2>/dev/null; then
    echo "fastapi/uvicorn are not importable from $VENV_DIR; the server cannot run." >&2
    exit 1
fi

# history.db is created here on first start and holds password hashes,
# sessions and the agent key, so the service user must own the directory.
chown -R "$RUN_USER" "$BACKEND_DIR"

echo "==> Writing /etc/systemd/system/$SERVICE_NAME.service"
cat > "/etc/systemd/system/$SERVICE_NAME.service" <<UNIT
[Unit]
Description=HomeLab Dashboard Service
After=network.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$BACKEND_DIR
ExecStart=$VENV_DIR/bin/uvicorn main:app --host 0.0.0.0 --port $PORT --timeout-graceful-shutdown 1
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
# restart, not enable --now: --now leaves a service that is already running
# on its old code and settings.
systemctl enable --quiet "$SERVICE_NAME.service"
systemctl restart "$SERVICE_NAME.service"

echo "==> Waiting for the database to initialise"
for _ in $(seq 1 15); do
    [[ -f "$BACKEND_DIR/history.db" ]] && break
    sleep 1
done

# The agent key is generated into the settings table on first DB init.
AGENT_KEY=""
if [[ -f "$BACKEND_DIR/history.db" ]] && command -v sqlite3 >/dev/null 2>&1; then
    AGENT_KEY="$(sqlite3 "$BACKEND_DIR/history.db" \
        "SELECT value FROM settings WHERE key='agent_auth_key';" 2>/dev/null || true)"
fi

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo
echo "Dashboard running at http://${IP:-<this-host>}:$PORT"
echo "Check status with: systemctl status $SERVICE_NAME"
echo
if [[ -n "$AGENT_KEY" ]]; then
    echo "Agent key: $AGENT_KEY"
    echo
    echo "Install an agent on each node (including this one) with:"
    echo "  sudo ./scripts/install-agent.sh --server http://${IP:-<this-host>}:$PORT --key $AGENT_KEY"
else
    echo "Could not read the agent key automatically (install sqlite3, or open the"
    echo "dashboard, register, and copy the key from the UI)."
fi
