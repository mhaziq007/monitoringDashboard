#!/usr/bin/env bash
#
# Installs the HomeLab Dashboard agent as a systemd service on a monitored
# node. Run this on every machine you want on the dashboard, including the
# server box itself (otherwise it never reports its own metrics):
#
#   sudo ./scripts/install-agent.sh --server http://192.168.1.50:8000 --key <agent-key>
#
# Options:
#   --server <url>    dashboard base URL, or the full /api/report URL
#   --key <key>       agent key from the server (printed by install-server.sh)
#   --interval <sec>  reporting interval (default: 5)
#   --user <name>     user to run the agent as (default: root)
#   --vm <auto|yes|no>  whether this node is a virtual machine; VMs are left
#                     out of the power and cost totals (default: auto-detect)
#
# Config is written to /etc/homelab-agent.env, so agent.py itself is never
# edited and stays in sync with git.
#
set -euo pipefail

SERVICE_NAME="homelab-agent"
ENV_FILE="/etc/homelab-agent.env"
SERVER=""
KEY=""
INTERVAL=5
RUN_USER="root"
IS_VM=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --server)   SERVER="$2";   shift 2 ;;
        --key)      KEY="$2";      shift 2 ;;
        --interval) INTERVAL="$2"; shift 2 ;;
        --user)     RUN_USER="$2"; shift 2 ;;
        --vm)       IS_VM="$2";    shift 2 ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done

if [[ $EUID -ne 0 ]]; then
    echo "This script needs root to install the systemd unit. Re-run with sudo." >&2
    exit 1
fi

# Fall back to whatever a previous install used, so re-running to pick up a
# new agent.py does not require passing the arguments again.
if [[ -f "$ENV_FILE" ]]; then
    # shellcheck disable=SC1090
    source "$ENV_FILE"
    [[ -z "$SERVER" ]] && SERVER="${HOMELAB_SERVER_URL:-}"
    [[ -z "$KEY" ]] && KEY="${HOMELAB_AGENT_KEY:-}"
    [[ -z "$IS_VM" ]] && IS_VM="${HOMELAB_IS_VM:-}"
fi
IS_VM="${IS_VM:-auto}"

case "$IS_VM" in
    auto|yes|no) ;;
    *) echo "--vm takes auto, yes or no (got '$IS_VM')." >&2; exit 1 ;;
esac

if [[ -z "$SERVER" || -z "$KEY" ]]; then
    echo "Both --server and --key are required on first install." >&2
    echo "The key is printed by install-server.sh, or shown in the dashboard UI." >&2
    exit 1
fi

# Accept either a base URL or the full endpoint.
case "$SERVER" in
    */api/report) REPORT_URL="$SERVER" ;;
    */)           REPORT_URL="${SERVER}api/report" ;;
    *)            REPORT_URL="$SERVER/api/report" ;;
esac

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
AGENT_DIR="$REPO_ROOT/agent"
VENV_DIR="$AGENT_DIR/.venv"

if [[ ! -f "$AGENT_DIR/agent.py" ]]; then
    echo "Could not find agent/agent.py under $REPO_ROOT." >&2
    exit 1
fi

if ! id -u "$RUN_USER" >/dev/null 2>&1; then
    echo "User '$RUN_USER' does not exist." >&2
    exit 1
fi

echo "==> Installing agent from $AGENT_DIR (reporting to $REPORT_URL)"

if ! command -v python3 >/dev/null 2>&1; then
    echo "python3 not found. Install Python 3.10+ and re-run." >&2
    exit 1
fi

# Rebuild the venv when it cannot run, or was built for a different Python
# than the system has now. After a distro upgrade (say 3.11 -> 3.12),
# bin/python follows the symlink to the new interpreter while psutil is still
# in lib/python3.11/site-packages, so a working agent suddenly dies with
# "No module named 'psutil'". A venv copied from another OS fails the same way.
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
"$VENV_DIR/bin/python" -m pip install --quiet -r "$AGENT_DIR/requirements.txt"

if ! "$VENV_DIR/bin/python" -c 'import psutil' 2>/dev/null; then
    echo "psutil is not importable from $VENV_DIR; the agent cannot run." >&2
    exit 1
fi

echo "==> Writing $ENV_FILE"
cat > "$ENV_FILE" <<ENV
HOMELAB_SERVER_URL=$REPORT_URL
HOMELAB_AGENT_KEY=$KEY
HOMELAB_INTERVAL=$INTERVAL
HOMELAB_IS_VM=$IS_VM
ENV
# Contains the shared agent key.
chmod 600 "$ENV_FILE"
chown "$RUN_USER" "$ENV_FILE"

echo "==> Writing /etc/systemd/system/$SERVICE_NAME.service"
cat > "/etc/systemd/system/$SERVICE_NAME.service" <<UNIT
[Unit]
Description=HomeLab Dashboard Agent Service
After=network.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$AGENT_DIR
EnvironmentFile=$ENV_FILE
# -u: without a TTY Python buffers stdout, so log lines reached the
# journal minutes late in 8 KB bursts.
ExecStart=$VENV_DIR/bin/python -u agent.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable --now "$SERVICE_NAME.service"

sleep 3
if systemctl is-active --quiet "$SERVICE_NAME.service"; then
    echo
    echo "Agent running. This node should appear on the dashboard within a few seconds."
    echo "Follow its log with: journalctl -u $SERVICE_NAME -f"
else
    echo
    echo "Agent failed to start. Recent log:" >&2
    journalctl -u "$SERVICE_NAME" -n 20 --no-pager >&2
    exit 1
fi
