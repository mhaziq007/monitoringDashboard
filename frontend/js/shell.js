/* Topbar wiring shared by every page: agent key, theme, sign out, link indicator. */

import { agentKey } from './api.js';
import { logout } from './auth.js';
import { mountThemeToggle } from './theme.js';

export function mountShell() {
    mountAgentKey();
    mountThemeToggle();

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
