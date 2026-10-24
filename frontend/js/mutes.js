/* Muting a node's alerts for maintenance. The server holds the mutes
   (backend/mutes.py) and sends them on the stream as "mutes". */

import { api } from './api.js';
import { toast } from './toast.js';
import { esc } from './format.js';

let mutes = {};              // hostname -> { since, until, until_back, back }
let onChange = () => {};

const CHOICES = [['60', '1 hour'], ['240', '4 hours'], ['back', 'Until back online']];

const clock = (seconds) => new Date(seconds * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });

function left(mute) {
    const minutes = Math.max(1, Math.round((mute.until - Date.now() / 1000) / 60));
    if (minutes < 60) return `${minutes} min`;
    const h = Math.floor(minutes / 60);
    const m = minutes % 60;
    return m && h < 10 ? `${h} h ${m} min` : `${h} h`;
}

// Waiting for the node to go down and come back, as opposed to its grace period after.
const waiting = (mute) => mute.until_back && !mute.back;

export const isMuted = (host) => Boolean(mutes[host]) && mutes[host].until * 1000 > Date.now();

/** Text for the card badge, or '' when the node is not muted. */
export function muteBadge(host) {
    if (!isMuted(host)) return '';
    return waiting(mutes[host]) ? 'Until back' : left(mutes[host]);
}

export function muteTitle(host) {
    const mute = mutes[host];
    return waiting(mute) ? 'Alerts muted until this node is back online'
        : `Alerts muted until ${clock(mute.until)}`;
}

function describe(mute) {
    if (waiting(mute)) return 'Alerts muted until this node is back online (24 h at most)';
    if (mute.until_back) return `Back online; alerts stay muted until ${clock(mute.until)} while its services start`;
    return `Alerts muted until ${clock(mute.until)}, ${left(mute)} from now`;
}

/** The mute bar at the top of the node drawer. */
export function renderMuteBar(el, host) {
    const mute = isMuted(host) ? mutes[host] : null;
    const html = mute
        ? `<i class="fa-solid fa-bell-slash"></i>
           <span class="mute-bar__text">${esc(describe(mute))}. Nothing new is sent for this node or the services on it.</span>
           <button type="button" class="btn btn--quiet btn--small" data-unmute>Unmute</button>`
        : `<i class="fa-solid fa-bell"></i>
           <span class="mute-bar__text">Working on this node? Mute its alerts:</span>
           <span class="mute-bar__choices">${CHOICES.map(([value, label]) =>
               `<button type="button" class="btn btn--quiet btn--small" data-mute="${value}">${label}</button>`).join('')}</span>`;
    // The drawer re-renders every few seconds; replacing unchanged buttons
    // would swallow a click that lands mid-refresh.
    if (el.dataset.html !== html) {
        el.innerHTML = html;
        el.dataset.html = html;
    }
    el.dataset.muted = String(Boolean(mute));
    el.dataset.host = host;
}

async function send(host, method, body) {
    const response = await api(`/api/mutes/${encodeURIComponent(host)}`, {
        method,
        headers: { 'Content-Type': 'application/json' },
        body: body ? JSON.stringify(body) : undefined,
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
        // A server not restarted since the update answers from the static files.
        throw new Error(data.detail || (response.status === 405
            ? 'the dashboard server is running older code; run scripts/update.sh on it'
            : `the server answered ${response.status}`));
    }
    applyMutes(data);
}

/** Wire the drawer's mute bar once; it reads the node from its data-host. */
export function wireMuteBar(el) {
    el.addEventListener('click', async (event) => {
        const button = event.target.closest('button');
        if (!button) return;
        const host = el.dataset.host;
        button.disabled = true;
        try {
            if (button.dataset.unmute !== undefined) {
                await send(host, 'DELETE');
                toast(`Alerts for ${host} are back on`, { host, tone: 'ok' });
            } else {
                const choice = button.dataset.mute;
                await send(host, 'PUT', choice === 'back' ? { until_back: true } : { minutes: Number(choice) });
                toast(`Alerts muted: ${muteTitle(host).replace('Alerts muted ', '')}`, { host, tone: 'ok' });
            }
        } catch (err) {
            toast(`Could not change the mute: ${err.message}`, { host, tone: 'crit' });
        } finally {
            button.disabled = false;
        }
    });
}

export function applyMutes(map) {
    mutes = map || {};
    onChange();
}

export function onMutesChange(callback) {
    onChange = callback;
}
