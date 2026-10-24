#!/usr/bin/env bash
#
# Brings this node up to date and restarts whatever runs on it: the
# dashboard server, the agent, or both. Run it from the checkout on each
# node after changes are merged:
#
#   sudo ./scripts/update.sh
#
# Options:
#   --branch <name>   switch to this branch first (e.g. to try one before it is merged)
#
# It always restarts, even when there was nothing new to pull: a service
# that was never restarted after an earlier `git pull` keeps running the old
# code, which is what this script is for.
#
set -euo pipefail

BRANCH=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --branch) BRANCH="$2"; shift 2 ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done

if [[ $EUID -ne 0 ]]; then
    echo "This script needs root to restart services. Re-run with sudo." >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Run git as the checkout's owner: as root, git refuses a repository owned
# by another user ("detected dubious ownership").
OWNER="$(stat -c %U "$REPO_ROOT")"
as_owner() { sudo -u "$OWNER" -- "$@"; }
git_() { as_owner git -C "$REPO_ROOT" "$@"; }

if [[ -n "$(git_ status --porcelain --untracked-files=no)" ]]; then
    echo "This checkout has local changes to tracked files; commit or stash them first:" >&2
    git_ status --short --untracked-files=no >&2
    exit 1
fi

BEFORE="$(git_ rev-parse --short HEAD)"
if [[ -n "$BRANCH" ]]; then
    echo "==> Switching to $BRANCH"
    git_ fetch --quiet origin "$BRANCH"
    git_ checkout --quiet "$BRANCH" 2>/dev/null || git_ checkout --quiet -b "$BRANCH" "origin/$BRANCH"
fi
echo "==> Pulling $(git_ rev-parse --abbrev-ref HEAD)"
git_ pull --ff-only --quiet
AFTER="$(git_ rev-parse --short HEAD)"
if [[ "$BEFORE" == "$AFTER" ]]; then
    echo "    Already up to date at $AFTER; restarting anyway."
else
    echo "    $BEFORE -> $AFTER"
    git_ --no-pager log --oneline --no-decorate "$BEFORE..$AFTER" | sed 's/^/      /'
fi

FAILED=0
FOUND=0

restart() {
    local service="$1" dir="$2"
    systemctl cat "$service.service" >/dev/null 2>&1 || return 0
    FOUND=1

    # Dependencies can change between versions. Installed as the venv's
    # owner, so the service user can still read everything in it.
    local venv="$dir/.venv"
    if [[ -x "$venv/bin/python" ]]; then
        sudo -u "$(stat -c %U "$venv")" -- "$venv/bin/python" -m pip install --quiet -r "$dir/requirements.txt" \
            || echo "    Could not update $service's Python packages; restarting with the ones it has." >&2
    fi

    echo "==> Restarting $service"
    systemctl restart "$service.service"
    sleep 3
    if systemctl is-active --quiet "$service.service"; then
        echo "    Running."
    else
        echo "    $service failed to start. Recent log:" >&2
        journalctl -u "$service" -n 20 --no-pager >&2
        FAILED=1
    fi
}

restart homelab-dashboard "$REPO_ROOT/backend"
restart homelab-agent "$REPO_ROOT/agent"

if [[ $FOUND -eq 0 ]]; then
    echo "Neither homelab-dashboard nor homelab-agent is installed on this node." >&2
    echo "Install one with scripts/install-server.sh or scripts/install-agent.sh." >&2
    exit 1
fi
exit $FAILED
