/* Topbar wiring shared by every page: agent key, theme, sign out, link indicator. */

import { agentKey } from './api.js';
import { logout } from './auth.js';
import { mountThemeToggle } from './theme.js';

export function mountShell() {
    mountAgentKey();
    mountThemeToggle();
    checkServerVersion();
    setInterval(checkServerVersion, 10 * 60 * 1000);

    const logoutBtn = document.getElementById('logout-btn');
    if (logoutBtn) logoutBtn.addEventListener('click', logout);
}

function mountAgentKey() {
    const chip = document.getElementById('agent-key');
    const value = document.getElementById('agent-key-value');
    const key = agentKey.get();
    if (!chip || !value || !key) return;

    chip.hidden = false;
    value.textContent = key;

    chip.addEventListener('click', async () => {
        try {
            await navigator.clipboard.writeText(key);
        } catch {
            return; // Clipboard denied (non-HTTPS origin); leave the chip alone.
        }
        chip.classList.add('is-copied');
        value.textContent = 'Copied';
        setTimeout(() => {
            chip.classList.remove('is-copied');
            value.textContent = key;
        }, 1600);
    });
}

/**
 * After a `git pull` the browser gets the new pages at once, but the server
 * keeps running the old code until it restarts, so new features fail in
 * confusing ways. The server reports both commits; say so when they differ.
 */
async function checkServerVersion() {
    let version;
    try {
        const response = await fetch('/api/version');
        if (!response.ok) return;   // a server older than this check
        version = await response.json();
    } catch {
        return;
    }
    let banner = document.getElementById('stale-banner');
    if (!version.stale) {
        banner?.remove();
        return;
    }
    if (!banner) {
        banner = document.createElement('div');
        banner.id = 'stale-banner';
        banner.className = 'stale-banner';
        document.querySelector('.topbar')?.after(banner);
    }
    banner.innerHTML = `<i class="fa-solid fa-rotate"></i><span>The dashboard server is still running older
        code (${version.running}) than is installed (${version.installed}). On the server, run
        <code>sudo ./scripts/update.sh</code> or <code>sudo systemctl restart homelab-dashboard</code>.</span>`;
}

/** Reflect stream health in the topbar: connecting / live / down. */
export function setLinkState(state) {
    const el = document.getElementById('link-state');
    if (!el) return;
    el.dataset.state = state;
    const label = el.querySelector('.link-state__label');
    if (label) {
        label.textContent = { live: 'Live', down: 'Reconnecting', idle: 'Connecting' }[state] || 'Idle';
    }
}
