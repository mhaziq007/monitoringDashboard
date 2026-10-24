/* Alerts page: what the server has raised, its history, and the ntfy settings. */

import { ensureAuthenticated } from './auth.js';
import { mountShell } from './shell.js';
import { connectStream } from './stream.js';
import { api, getJson } from './api.js';
import { toast } from './toast.js';
import { esc, uptime } from './format.js';

let active = [];
let history = [];
let settings = null;

const ICONS = {
    offline: 'fa-power-off',
    cpu: 'fa-microchip',
    temp: 'fa-temperature-high',
    gputemp: 'fa-temperature-high',
    disk: 'fa-hard-drive',
    service: 'fa-globe',
    digest: 'fa-box-open',
};

const $ = (id) => document.getElementById(id);
const lasted = (seconds) => (seconds < 60 ? 'under a minute' : uptime(seconds));

function when(seconds) {
    const d = new Date(seconds * 1000);
    const time = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false });
    return d.toDateString() === new Date().toDateString()
        ? time
        : `${d.toLocaleDateString([], { month: 'short', day: 'numeric' })} ${time}`;
}

/** One alert, open or closed. A digest is a note, not something that resolves. */
function row(alert) {
    const now = Date.now() / 1000;
    const digest = alert.kind === 'digest';
    const open = !alert.resolved;
    let meta;
    if (digest) {
        meta = `Sent ${when(alert.started)}`;
    } else if (open) {
        meta = `Since ${when(alert.started)} · ${lasted(now - alert.started)}`;
    } else {
        meta = `${when(alert.started)} – ${when(alert.resolved)} · lasted ${lasted(alert.resolved - alert.started)}`;
    }
    const outcome = !open && !digest && alert.resolved_title
        ? `<div class="alert-row__outcome"><i class="fa-solid fa-check"></i>${esc(alert.resolved_title)}</div>` : '';
    return `
        <div class="alert-row" data-severity="${esc(alert.severity)}" data-open="${open && !digest}">
            <span class="alert-row__icon"><i class="fa-solid ${ICONS[alert.kind] || 'fa-bell'}"></i></span>
            <div class="alert-row__body">
                <div class="alert-row__title">${esc(alert.title)}</div>
                <div class="alert-row__message">${esc(alert.message || '')}</div>
                ${outcome}
                <div class="alert-row__meta">${meta}</div>
            </div>
        </div>`;
}

function render() {
    $('active-list').innerHTML = active.length
        ? active.map(row).join('')
        : '<div class="alert-empty"><i class="fa-solid fa-circle-check"></i>All clear</div>';
    $('history-list').innerHTML = history.length
        ? history.map(row).join('')
        : '<div class="alert-empty">Nothing yet. Alerts appear here once the server raises them.</div>';

    const crit = active.some((a) => a.severity === 'crit');
    $('stat-active').textContent = String(active.length);
    $('stat-active-foot').textContent = active.length
        ? active[0].title + (active.length > 1 ? ` and ${active.length - 1} more` : '')
        : 'All clear';
    $('stat-active-card').dataset.tone = active.length ? (crit ? 'crit' : 'warn') : 'ok';

    const dayAgo = Date.now() / 1000 - 86400;
    $('stat-day').textContent = String(history.filter((a) => a.kind !== 'digest' && a.started >= dayAgo).length);

    renderDelivery();
}

function renderDelivery() {
    if (!settings) return;
    const on = settings.ntfy.enabled && settings.ntfy.url;
    const last = settings.status;
    $('stat-delivery').textContent = on ? 'On' : 'Off';
    $('stat-delivery-card').dataset.tone = !on ? '' : last && !last.ok ? 'crit' : 'ok';
    $('stat-delivery-foot').textContent = !on ? 'Set up ntfy below'
        : !last ? 'Nothing sent yet'
            : last.ok ? `Last sent ${when(last.at)}` : `Last send failed ${when(last.at)}`;

    const status = $('notify-status');
    status.hidden = !(last && !last.ok);
    if (last && !last.ok) {
        status.textContent = `Last delivery failed: ${last.error}`;
        status.dataset.tone = 'crit';
    }
}

async function loadAlerts() {
    try {
        ({ active, history } = await getJson('/api/alerts'));
        render();
    } catch (err) {
        console.error('Alerts request failed:', err);
    }
}

/* --- Settings form ------------------------------------------------------ */

function fillForm() {
    $('ntfy-enabled').checked = settings.ntfy.enabled;
    $('ntfy-url').value = settings.ntfy.url;
    $('ntfy-token').value = '';
    $('ntfy-token').placeholder = settings.ntfy.has_token
        ? 'A token is saved; type a new one to replace it' : 'Only if the topic needs one';
    $('ntfy-token-clear').hidden = !settings.ntfy.has_token;
    $('click-url').value = settings.click_url;
    $('click-url').placeholder = `${window.location.origin}/alerts.html`;
    $('digest-hour').value = settings.digest_hour === null ? 'off' : String(settings.digest_hour);
    renderDelivery();
}

async function loadSettings() {
    try {
        settings = await getJson('/api/settings/notifications');
        fillForm();
    } catch (err) {
        console.error('Notification settings request failed:', err);
    }
}

async function send(path, method, body) {
    const response = await api(path, {
        method,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || `the server answered ${response.status}`);
    return data;
}

async function save(event) {
    event.preventDefault();
    const hour = $('digest-hour').value;
    const token = $('ntfy-token').value.trim();
    try {
        settings = await send('/api/settings/notifications', 'PUT', {
            // A blank token field keeps the saved token.
            ntfy: { enabled: $('ntfy-enabled').checked, url: $('ntfy-url').value.trim(), token: token || null },
            click_url: $('click-url').value.trim(),
            digest_hour: hour === 'off' ? null : Number(hour),
        });
        fillForm();
        toast('Notification settings saved', { tone: 'ok' });
    } catch (err) {
        toast(`Could not save: ${err.message}`, { tone: 'crit' });
    }
}

async function clearToken() {
    try {
        settings = await send('/api/settings/notifications', 'PUT', { ntfy: { token: '' } });
        fillForm();
        toast('Saved token removed', { tone: 'ok' });
    } catch (err) {
        toast(`Could not remove the token: ${err.message}`, { tone: 'crit' });
    }
}

async function sendTest() {
    const button = $('notify-test');
    button.disabled = true;
    try {
        // Tests what is typed, saved or not, so a URL can be tried before saving.
        const result = await send('/api/notifications/test', 'POST', {
            url: $('ntfy-url').value.trim(),
            token: $('ntfy-token').value.trim() || null,
        });
        toast(result.ok ? 'Test sent. Check the ntfy app.' : `Test failed: ${result.error}`,
            { tone: result.ok ? 'ok' : 'crit' });
    } catch (err) {
        toast(`Test failed: ${err.message}`, { tone: 'crit' });
    } finally {
        button.disabled = false;
    }
}

async function start() {
    if (!await ensureAuthenticated()) return;

    mountShell();

    const hours = [...Array(24).keys()]
        .map((h) => `<option value="${h}">${String(h).padStart(2, '0')}:00</option>`);
    $('digest-hour').innerHTML = `<option value="off">Off</option>${hours.join('')}`;
    $('notify-form').addEventListener('submit', save);
    $('notify-test').addEventListener('click', sendTest);
    $('ntfy-token-clear').addEventListener('click', clearToken);

    await Promise.all([loadAlerts(), loadSettings()]);

    connectStream({
        // Active alerts arrive on the stream; history and delivery status
        // change with them, so those are fetched again.
        alerts: (list) => {
            active = list;
            render();
            loadAlerts();
            loadSettings();
        },
    });

    // Keeps "since" durations current.
    setInterval(render, 30000);
}

start();
