/* Check-ins on the Services page: backups and scheduled jobs that call a
   ping URL when they finish. The server alerts when one is late or reports
   a failure (backend/checkins.py). */

import { api, getJson } from './api.js';
import { toast } from './toast.js';
import { esc, ago } from './format.js';

const EVERY = [[3600, '1 hour'], [21600, '6 hours'], [43200, '12 hours'], [86400, '1 day'], [604800, '1 week']];
const GRACE = [[900, '15 minutes'], [3600, '1 hour'], [21600, '6 hours'], [86400, '1 day']];
const STATES = {
    new: ['Waiting', 'No check-in yet'],
    ok: ['OK', ''],
    late: ['Late', ''],
    failed: ['Failed', ''],
};

const list = document.getElementById('checkin-list');
const empty = document.getElementById('checkin-empty');
const form = document.getElementById('checkin-form');
let checks = [];

function period(seconds) {
    const known = [...EVERY, ...GRACE].find(([value]) => value === seconds);
    if (known) return known[1];
    const units = [[604800, 'week'], [86400, 'day'], [3600, 'hour'], [60, 'minute']];
    const [size, unit] = units.find(([s]) => seconds % s === 0) || [1, 'second'];
    const n = seconds / size;
    return `${n} ${unit}${n === 1 ? '' : 's'}`;
}

function options(choices, selected) {
    const all = choices.some(([value]) => value === selected) ? choices : [...choices, [selected, period(selected)]];
    return all.map(([value, label]) =>
        `<option value="${value}"${value === selected ? ' selected' : ''}>${esc(label)}</option>`).join('');
}

const pingUrl = (check) => `${window.location.origin}/ping/${check.token}`;

function row(check) {
    const now = Date.now() / 1000;
    const [label] = STATES[check.state] || [check.state];
    const last = check.last_ping == null ? 'No check-in yet'
        : `Last ${check.last_status === 'fail' ? 'reported a failure' : 'checked in'} ${ago(now - check.last_ping)}`;
    return `
        <article class="checkin" data-state="${esc(check.state)}" data-id="${check.id}">
            <span class="status checkin__status"><i class="status__dot"></i><span>${esc(label)}</span></span>
            <div class="checkin__body">
                <div class="checkin__name">${esc(check.name)}</div>
                <div class="checkin__meta">
                    ${esc(last)} · runs every
                    <select data-field="every" aria-label="Runs every">${options(EVERY, check.every)}</select>
                    · alerts when
                    <select data-field="grace" aria-label="Alert when this late">${options(GRACE, check.grace)}</select> late
                </div>
                ${check.state === 'failed' && check.last_note ? `<pre class="checkin__note">${esc(check.last_note)}</pre>` : ''}
                <div class="checkin__url">
                    <code>${esc(pingUrl(check))}</code>
                    <button type="button" class="checkin__copy" data-copy>Copy</button>
                </div>
            </div>
            <button type="button" class="icon-btn icon-btn--danger" data-delete
                    title="Delete this check-in" aria-label="Delete ${esc(check.name)}">
                <i class="fa-solid fa-trash-can"></i>
            </button>
        </article>`;
}

function render() {
    empty.hidden = checks.length > 0;
    // Leave the list alone while one of its selects is open or focused.
    if (list.contains(document.activeElement) && document.activeElement.tagName === 'SELECT') return;
    const html = checks.map(row).join('');
    if (list.dataset.html !== html) {
        list.innerHTML = html;
        list.dataset.html = html;
    }
}

async function load() {
    try {
        checks = await getJson('/api/checkins');
        render();
    } catch (err) {
        console.error('Check-ins request failed:', err);
    }
}

async function send(path, method, body) {
    const response = await api(path, {
        method,
        headers: { 'Content-Type': 'application/json' },
        body: body ? JSON.stringify(body) : undefined,
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
        throw new Error(data.detail || (response.status === 405
            ? 'the dashboard server is running older code; run scripts/update.sh on it'
            : `the server answered ${response.status}`));
    }
    return data;
}

/** The clipboard API needs HTTPS (or localhost); a LAN address over HTTP gets the old way. */
async function copy(text) {
    try {
        await navigator.clipboard.writeText(text);
        return true;
    } catch {
        const area = Object.assign(document.createElement('textarea'), { value: text });
        area.style.cssText = 'position:fixed;opacity:0';
        document.body.appendChild(area);
        area.select();
        const ok = document.execCommand('copy');
        area.remove();
        return ok;
    }
}

async function add(event) {
    event.preventDefault();
    const name = document.getElementById('checkin-name').value.trim();
    try {
        const check = await send('/api/checkins', 'POST', {
            name,
            every: Number(document.getElementById('checkin-every').value),
            grace: Number(document.getElementById('checkin-grace').value),
        });
        form.reset();
        form.hidden = true;
        await load();
        const copied = await copy(pingUrl(check));
        toast(copied ? `Added ${check.name}. Its ping URL is copied; call it from the job when it finishes.`
            : `Added ${check.name}. Call its ping URL from the job when it finishes.`, { tone: 'ok' });
    } catch (err) {
        toast(`Could not add the check-in: ${err.message}`, { tone: 'crit' });
    }
}

async function onClick(event) {
    const item = event.target.closest('.checkin');
    const check = item && checks.find((c) => String(c.id) === item.dataset.id);
    if (!check) return;
    if (event.target.closest('[data-copy]')) {
        toast(await copy(pingUrl(check)) ? 'Ping URL copied' : 'Could not copy; select the URL instead',
            { tone: 'ok' });
    } else if (event.target.closest('[data-delete]')) {
        if (!window.confirm(`Delete ${check.name}? Its ping URL stops working.`)) return;
        try {
            await send(`/api/checkins/${check.id}`, 'DELETE');
            await load();
        } catch (err) {
            toast(`Could not delete: ${err.message}`, { tone: 'crit' });
        }
    }
}

async function onChange(event) {
    const select = event.target.closest('select[data-field]');
    const item = select?.closest('.checkin');
    if (!item) return;
    select.blur();   // lets the list refresh again
    try {
        await send(`/api/checkins/${item.dataset.id}`, 'PUT', { [select.dataset.field]: Number(select.value) });
        await load();
    } catch (err) {
        toast(`Could not save: ${err.message}`, { tone: 'crit' });
    }
}

export function startCheckins() {
    document.getElementById('checkin-every').innerHTML = options(EVERY, 86400);
    document.getElementById('checkin-grace').innerHTML = options(GRACE, 3600);
    document.getElementById('checkin-add').addEventListener('click', () => {
        form.hidden = !form.hidden;
        if (!form.hidden) document.getElementById('checkin-name').focus();
    });
    form.addEventListener('submit', add);
    list.addEventListener('click', onClick);
    list.addEventListener('change', onChange);
    load();
    setInterval(load, 30000);
}
