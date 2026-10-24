/* Alerts page: what the server has raised, its history, and where it sends them. */

import { ensureAuthenticated } from './auth.js';
import { mountShell } from './shell.js';
import { connectStream } from './stream.js';
import { api, getJson } from './api.js';
import { toast } from './toast.js';
import { esc, uptime } from './format.js';
import { startBackupPanel, loadBackup } from './backup-panel.js';

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
    mem: 'fa-memory',
    container: 'fa-cube',
    checkin: 'fa-clock-rotate-left',
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

const CHANNEL_NAMES = { ntfy: 'ntfy', telegram: 'Telegram' };

const channelReady = (name) => {
    const c = settings[name];
    return c.enabled && (name === 'ntfy' ? Boolean(c.url) : Boolean(c.has_token && c.chat_id));
};

function renderDelivery() {
    if (!settings) return;
    const ready = Object.keys(CHANNEL_NAMES).filter(channelReady);
    const outcomes = ready.map((name) => settings.status?.[name]).filter(Boolean);
    const failed = ready.filter((name) => settings.status?.[name] && !settings.status[name].ok);
    const latest = Math.max(0, ...outcomes.map((o) => o.at));

    $('stat-delivery').textContent = ready.length ? ready.map((n) => CHANNEL_NAMES[n]).join(' + ') : 'Off';
    $('stat-delivery-card').dataset.tone = !ready.length ? '' : failed.length ? 'crit' : 'ok';
    $('stat-delivery-foot').textContent = !ready.length ? 'Set up a channel below'
        : failed.length ? `${failed.map((n) => CHANNEL_NAMES[n]).join(' and ')} failed last time`
            : latest ? `Last sent ${when(latest)}` : 'Nothing sent yet';

    for (const name of Object.keys(CHANNEL_NAMES)) {
        const last = settings.status?.[name];
        const line = $(`${name}-status`);
        line.hidden = !(last && !last.ok);
        if (last && !last.ok) line.textContent = `Last delivery failed ${when(last.at)}: ${last.error}`;
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
    const { ntfy, telegram } = settings;
    $('ntfy-enabled').checked = ntfy.enabled;
    $('ntfy-url').value = ntfy.url;
    $('telegram-enabled').checked = telegram.enabled;
    $('telegram-chat').value = telegram.chat_id;
    $('telegram-chats').hidden = true;
    for (const [name, empty] of [['ntfy', 'Only if the topic needs one'], ['telegram', '123456789:AA...']]) {
        $(`${name}-token`).value = '';
        $(`${name}-token`).placeholder = settings[name].has_token
            ? 'A token is saved; type a new one to replace it' : empty;
        $(`${name}-token-clear`).hidden = !settings[name].has_token;
    }
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

const typed = (id) => $(id).value.trim();

async function save(event) {
    event.preventDefault();
    const hour = $('digest-hour').value;
    try {
        settings = await send('/api/settings/notifications', 'PUT', {
            // A blank token field keeps the saved token.
            ntfy: { enabled: $('ntfy-enabled').checked, url: typed('ntfy-url'), token: typed('ntfy-token') || null },
            telegram: {
                enabled: $('telegram-enabled').checked,
                chat_id: typed('telegram-chat'),
                token: typed('telegram-token') || null,
            },
            click_url: typed('click-url'),
            digest_hour: hour === 'off' ? null : Number(hour),
        });
        fillForm();
        toast('Notification settings saved', { tone: 'ok' });
    } catch (err) {
        toast(`Could not save: ${err.message}`, { tone: 'crit' });
    }
}

async function clearToken(name) {
    try {
        settings = await send('/api/settings/notifications', 'PUT', { [name]: { token: '' } });
        fillForm();
        toast(`Saved ${CHANNEL_NAMES[name]} token removed`, { tone: 'ok' });
    } catch (err) {
        toast(`Could not remove the token: ${err.message}`, { tone: 'crit' });
    }
}

/** Tests what is typed, saved or not; blank fields fall back to the saved settings. */
async function sendTest(name) {
    const button = $(`${name}-test`);
    button.disabled = true;
    const body = name === 'ntfy'
        ? { channel: 'ntfy', url: typed('ntfy-url'), token: typed('ntfy-token') || null }
        : { channel: 'telegram', chat_id: typed('telegram-chat'), token: typed('telegram-token') || null };
    try {
        const result = await send('/api/notifications/test', 'POST', body);
        toast(result.ok ? `Test sent. Check ${name === 'ntfy' ? 'the ntfy app' : 'Telegram'}.`
            : `Test failed: ${result.error}`, { tone: result.ok ? 'ok' : 'crit' });
    } catch (err) {
        toast(`Test failed: ${err.message}`, { tone: 'crit' });
    } finally {
        button.disabled = false;
    }
}

/** Lists the chats that recently messaged the bot, so the ID is picked, not looked up. */
async function findChats() {
    const button = $('telegram-find');
    const picker = $('telegram-chats');
    button.disabled = true;
    try {
        const { chats, error } = await send('/api/notifications/telegram/chats', 'POST',
            { token: typed('telegram-token') || null });
        if (error) throw new Error(error);
        if (!chats.length) {
            toast('No chats yet: send your bot a message in Telegram, then try again.', { tone: 'warn' });
            picker.hidden = true;
            return;
        }
        $('telegram-chat').value = chats[0].id;
        picker.innerHTML = chats.map((c) =>
            `<option value="${esc(c.id)}">${esc(c.name)}${c.type && c.type !== 'private' ? ` (${esc(c.type)})` : ''} · ${esc(c.id)}</option>`).join('');
        picker.hidden = chats.length < 2;
        toast(chats.length > 1 ? 'Pick your chat from the list, then save.' : `Found ${chats[0].name}. Save to keep it.`,
            { tone: 'ok' });
    } catch (err) {
        toast(`Could not read the bot's chats: ${err.message}`, { tone: 'crit' });
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
    for (const name of Object.keys(CHANNEL_NAMES)) {
        $(`${name}-test`).addEventListener('click', () => sendTest(name));
        $(`${name}-token-clear`).addEventListener('click', () => clearToken(name));
    }
    $('telegram-find').addEventListener('click', findChats);
    $('telegram-chats').addEventListener('change', (event) => { $('telegram-chat').value = event.target.value; });

    startBackupPanel();
    await Promise.all([loadAlerts(), loadSettings()]);

    connectStream({
        // Active alerts arrive on the stream; history and delivery status
        // change with them, so those are fetched again.
        alerts: (list) => {
            active = list;
            render();
            loadAlerts();
            loadSettings();
            loadBackup();   // a backup report changes its check-in's alert
        },
    });

    // Keeps "since" durations current.
    setInterval(render, 30000);
}

start();
