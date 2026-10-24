/* Devices page: fleet summary, node grid, detail drawer, threshold alerts. */

import { ensureAuthenticated } from './auth.js';
import { mountShell } from './shell.js';
import { connectStream } from './stream.js';
import { api, getJson } from './api.js';
import { toast, chime, unlockAudioOnInteraction } from './toast.js';
import { drawSparkline, createTrendChart } from './charts.js';
import {
    OFFLINE_AFTER, TARIFF, THRESHOLDS, HISTORY_POINTS,
    effectiveRate, monthlyCost,
} from './config.js';
import {
    esc, bytes, bytesShort, speed, speedShort, uptime, uptimeShort, watts, money,
    loadTone, tempTone, latencyTone, deviceIcon,
} from './format.js';

/* --- State -------------------------------------------------------------- */

const nodes = new Map();   // hostname -> latest device payload (+ history, alerts)
const cards = new Map();   // hostname -> { el, ref }
let pinned = readPinned();
let openHost = null;
let charts = [];     // power, CPU, memory and network, all fed from `samples`
let samples = [];    // history for the selected range, oldest first
let range = '6h';

const grid = document.getElementById('node-grid');
const empty = document.getElementById('node-empty');

function readPinned() {
    try {
        return new Set(JSON.parse(localStorage.getItem('pinnedDevices') || '[]'));
    } catch {
        return new Set();
    }
}

function savePinned() {
    localStorage.setItem('pinnedDevices', JSON.stringify([...pinned]));
}

// last_seen is stamped by the server's clock, so comparing it with this
// browser's clock shifted the offline window by however far the two disagreed.
// ingest() re-anchors the server-reported age to the local clock instead.
const isOnline = (device) => (Date.now() / 1000 - device.seen_at) < OFFLINE_AFTER;
// A VM's draw is either already in its host's reading or, for a cloud
// instance, not on this lab's bill, so it counts as nothing everywhere.
const nodePower = (d) => (d.is_vm ? 0 : (d.cpu_power || 0) + (d.gpu?.power || 0));
const imageUpdates = (d) => (d.docker_containers || []).filter((c) => c.update === 'available').length;
const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;

/**
 * Average CPU, share of memory in use and total network throughput, worked
 * out exactly as the server's fleet_load() records them. VMs count here:
 * unlike power, their load is their own.
 */
function fleetLoad(list) {
    const total = (items, pick) => items.reduce((sum, item) => sum + (pick(item) || 0), 0);
    const cpus = list.filter((d) => typeof d.cpu_usage === 'number');
    const mems = list.map((d) => d.memory).filter((m) => m?.total);
    const nets = list.map((d) => d.network).filter(Boolean);
    const memUsed = total(mems, (m) => m.used);
    const memTotal = total(mems, (m) => m.total);
    return {
        cpu: cpus.length ? total(cpus, (d) => d.cpu_usage) / cpus.length : null,
        mem: mems.length ? (100 * memUsed) / memTotal : null,
        net_down: nets.length ? total(nets, (n) => n.down_speed) : null,
        net_up: nets.length ? total(nets, (n) => n.up_speed) : null,
        memUsed,
        memTotal,
    };
}

/* --- Node card ---------------------------------------------------------- */

const CARD_TEMPLATE = `
    <div class="node__head">
        <span class="node__icon"><i class="fa-solid" data-r="icon"></i></span>
        <div class="node__id">
            <div class="node__name" data-r="name"></div>
            <div class="node__addr"><span data-r="ip"></span><span class="node__latency" data-r="latency"></span></div>
        </div>
        <div class="node__actions">
            <span class="badge" data-r="updates" hidden>
                <i class="fa-solid fa-circle-arrow-up"></i><span data-r="updatesCount"></span>
            </span>
            <span class="status" data-r="status" data-state="offline">
                <i class="status__dot"></i><span data-r="statusText">Offline</span>
            </span>
            <button class="icon-btn" data-r="pin" title="Pin to top" aria-label="Pin to top">
                <i class="fa-regular fa-star"></i>
            </button>
        </div>
    </div>

    <div class="node__meta"><i class="fa-solid fa-layer-group"></i><span data-r="meta"></span></div>

    <div class="node__metrics">
        <div class="metric" data-metric="cpu" data-r="cpuMetric">
            <div class="metric__head">
                <span class="metric__label">CPU</span>
                <span class="metric__value" data-r="cpuValue">—</span>
            </div>
            <div class="meter"><i class="meter__fill" data-r="cpuBar"></i></div>
            <canvas class="metric__spark" data-r="cpuSpark"></canvas>
        </div>
        <div class="metric" data-metric="ram" data-r="ramMetric">
            <div class="metric__head">
                <span class="metric__label">Memory</span>
                <span class="metric__note" data-r="ramNote"></span>
                <span class="metric__value" data-r="ramValue">—</span>
            </div>
            <div class="meter"><i class="meter__fill" data-r="ramBar"></i></div>
        </div>
    </div>

    <div class="node__tiles">
        <div class="tile" data-r="tempTile">
            <span class="tile__label">Temp</span><span class="tile__value" data-r="tempValue">—</span>
        </div>
        <div class="tile" data-tone="power">
            <span class="tile__label">Power</span><span class="tile__value" data-r="powerValue">—</span>
        </div>
        <div class="tile">
            <span class="tile__label">Uptime</span><span class="tile__value" data-r="uptimeValue">—</span>
        </div>
        <div class="tile">
            <span class="tile__label">Down</span><span class="tile__value" data-r="netValue">—</span>
        </div>
    </div>

    <div class="node__foot">
        <div class="node__chips" data-r="chips"></div>
        <span class="node__more">Details <i class="fa-solid fa-arrow-right"></i></span>
    </div>`;

function buildCard(device) {
    const el = document.createElement('article');
    el.className = 'node';
    el.tabIndex = 0;
    el.setAttribute('role', 'button');
    el.dataset.host = device.hostname;
    el.innerHTML = CARD_TEMPLATE;

    // Cache every hook once; updates then touch nodes directly instead of
    // rebuilding the card, which is what kept the meters from animating.
    const ref = {};
    el.querySelectorAll('[data-r]').forEach((node) => { ref[node.dataset.r] = node; });

    ref.icon.classList.add(deviceIcon(device.hostname));
    ref.name.textContent = device.hostname;

    ref.pin.addEventListener('click', (event) => {
        event.stopPropagation();
        togglePin(device.hostname);
    });

    el.addEventListener('click', () => openDrawer(device.hostname));
    el.addEventListener('keydown', (event) => {
        if (event.key === 'Enter' || event.key === ' ') {
            event.preventDefault();
            openDrawer(device.hostname);
        }
    });

    const entry = { el, ref, chipKey: '' };
    cards.set(device.hostname, entry);
    grid.appendChild(el);
    return entry;
}

function updateCard(device) {
    const entry = cards.get(device.hostname) || buildCard(device);
    const { el, ref } = entry;
    const online = isOnline(device);

    el.dataset.state = online ? 'online' : 'offline';
    el.classList.toggle('is-pinned', pinned.has(device.hostname));
    ref.pin.classList.toggle('is-on', pinned.has(device.hostname));
    ref.pin.firstElementChild.className = pinned.has(device.hostname)
        ? 'fa-solid fa-star' : 'fa-regular fa-star';

    ref.status.dataset.state = online ? 'online' : 'offline';
    ref.statusText.textContent = online ? 'Online' : 'Offline';

    ref.ip.textContent = device.ip || 'Unknown address';
    if (device.latency !== undefined && device.latency !== null && online) {
        ref.latency.textContent = ` · ${device.latency.toFixed(0)} ms`;
        ref.latency.dataset.tone = latencyTone(device.latency);
    } else {
        ref.latency.textContent = '';
        ref.latency.removeAttribute('data-tone');
    }

    const cores = device.cpu_cores
        ? ` · ${device.cpu_cores.physical}P / ${device.cpu_cores.logical}L` : '';
    ref.meta.textContent = `${device.os_info || 'Unknown OS'} · ${device.cpu_model || 'Unknown CPU'}${cores}`;
    ref.meta.title = ref.meta.textContent;

    const cpu = device.cpu_usage ?? 0;
    ref.cpuValue.textContent = `${cpu.toFixed(0)}%`;
    ref.cpuBar.style.width = `${Math.min(100, cpu)}%`;
    setTone(ref.cpuMetric, 'load', loadTone(cpu));

    const ram = device.memory ? (device.memory.used / device.memory.total) * 100 : 0;
    ref.ramValue.textContent = `${ram.toFixed(0)}%`;
    ref.ramBar.style.width = `${Math.min(100, ram)}%`;
    ref.ramNote.textContent = device.memory
        ? `${bytesShort(device.memory.used)} / ${bytesShort(device.memory.total)}` : '';
    setTone(ref.ramMetric, 'load', loadTone(ram));

    const temp = device.temp;
    ref.tempValue.textContent = (temp === undefined || temp === null) ? '—' : `${temp.toFixed(0)}°C`;
    setTone(ref.tempTile, 'tone', tempTone(temp));

    const power = nodePower(device);
    ref.powerValue.textContent = power > 0 ? watts(power) : '—';
    // The tile fits six characters; the full figure is in the drawer and tooltip.
    ref.uptimeValue.textContent = uptimeShort(device.uptime);
    ref.uptimeValue.title = uptime(device.uptime);
    ref.netValue.textContent = device.network ? speedShort(device.network.down_speed) : '—';

    const packages = device.pending_updates || 0;
    const images = imageUpdates(device);
    ref.updates.hidden = !(packages + images);
    ref.updatesCount.textContent = packages + images;
    ref.updates.title = [
        packages && plural(packages, 'OS package update'),
        images && plural(images, 'container image update'),
    ].filter(Boolean).join(', ');

    // Chips describe what the drawer holds; only rebuild when that changes.
    const chips = [];
    // First, so the chip row's fade-out never hides it.
    if (device.is_vm) chips.push(['vm', 'fa-cloud', 'VM']);
    if (device.gpu) chips.push(['gpu', 'fa-microchip', 'GPU']);
    const diskCount = device.disks?.length || (device.disk ? 1 : 0);
    if (diskCount) chips.push(['disk', 'fa-hard-drive', `${diskCount} disk${diskCount > 1 ? 's' : ''}`]);
    if (device.docker_containers?.length) {
        const running = device.docker_containers.filter((c) => c.state?.toLowerCase() === 'running').length;
        chips.push(['docker', 'fa-cube', `${running}/${device.docker_containers.length}`]);
    }
    if (device.vpns?.tailscale) chips.push(['vpn', 'fa-circle-nodes', 'Tailscale']);
    if (device.vpns?.openvpn) chips.push(['vpn', 'fa-lock', 'OpenVPN']);

    const chipKey = chips.map((c) => c.join()).join('|');
    if (chipKey !== entry.chipKey) {
        entry.chipKey = chipKey;
        ref.chips.innerHTML = chips
            .map(([kind, icon, label]) =>
                `<span class="chip" data-kind="${kind}"><i class="fa-solid ${icon}"></i>${esc(label)}</span>`)
            .join('');
    }

    el.classList.toggle('is-alerting', online && hasAlert(device));

    if (device.cpuHistory?.length > 1) {
        requestAnimationFrame(() => drawSparkline(ref.cpuSpark, device.cpuHistory, '--m-cpu'));
    }
}

/** Set a data attribute only when it changes, and remove it when empty. */
function setTone(el, attribute, value) {
    if (value) el.setAttribute(`data-${attribute}`, value);
    else el.removeAttribute(`data-${attribute}`);
}

function togglePin(hostname) {
    if (pinned.has(hostname)) pinned.delete(hostname);
    else pinned.add(hostname);
    savePinned();
    nodes.forEach(updateCard);
    reorder();
}

/**
 * Pinned nodes first, then offline-last, then alphabetical.
 * Safe to call on every tick: it compares against the current DOM order and
 * only moves nodes when the order actually changed. That matters because a
 * node going stale changes its rank without any event arriving for it.
 */
function reorder() {
    const desired = [...nodes.values()]
        .sort((a, b) => {
            const pin = Number(pinned.has(b.hostname)) - Number(pinned.has(a.hostname));
            if (pin) return pin;
            const live = Number(isOnline(b)) - Number(isOnline(a));
            if (live) return live;
            return a.hostname.localeCompare(b.hostname);
        })
        .map((device) => device.hostname);

    const current = [...grid.querySelectorAll('.node')].map((el) => el.dataset.host);
    if (current.length === desired.length && current.every((h, i) => h === desired[i])) return;

    desired.forEach((hostname) => {
        const entry = cards.get(hostname);
        if (entry) grid.appendChild(entry.el);
    });
}

/* --- Alerts (dual threshold, so a metric on the line cannot flap) -------- */

function hasAlert(device) {
    return Object.values(device.alerts || {}).some(Boolean);
}

function evaluateAlerts(device) {
    const previous = device.alerts || {};
    const alerts = { ...previous };
    const tripped = [];
    const cleared = [];

    const check = (key, value, unit = '') => {
        if (value === undefined || value === null) {
            alerts[key] = false;
            return;
        }
        const { trip, clear, label } = THRESHOLDS[key];
        if (!alerts[key] && value > trip) {
            alerts[key] = true;
            tripped.push(`${label} critical at ${value.toFixed(0)}${unit}`);
        } else if (alerts[key] && value < clear) {
            alerts[key] = false;
            cleared.push(`${label} back to ${value.toFixed(0)}${unit}`);
        }
    };

    check('cpu', device.cpu_usage, '%');
    check('temp', device.temp, '°C');
    check('gpuUtil', device.gpu?.utilization, '%');
    check('gpuTemp', device.gpu?.temp, '°C');

    device.alerts = alerts;

    if (tripped.length) chime();
    tripped.forEach((message) => toast(message, { host: device.hostname, tone: 'crit' }));
    cleared.forEach((message) => toast(message, { host: device.hostname, tone: 'ok' }));
}

/* --- Fleet summary ------------------------------------------------------ */

function updateFleet() {
    const all = [...nodes.values()];
    const live = all.filter(isOnline);
    const power = live.reduce((sum, d) => sum + nodePower(d), 0);
    const drawing = live.filter((d) => !d.is_vm).length;
    const vms = live.length - drawing;
    const alerting = live.filter(hasAlert).length;
    const packages = live.reduce((sum, d) => sum + (d.pending_updates || 0), 0);
    const images = live.reduce((sum, d) => sum + imageUpdates(d), 0);

    const set = (id, value) => {
        const el = document.getElementById(id);
        if (el) el.innerHTML = value;
    };
    const tone = (id, value) => {
        const el = document.getElementById(id);
        if (el) el.dataset.tone = value;
    };

    set('stat-nodes', `${live.length}<small> / ${all.length}</small>`);
    set('stat-nodes-foot', all.length - live.length
        ? `${all.length - live.length} offline` : 'All reporting');
    tone('stat-nodes-card', all.length && live.length === all.length ? 'ok' : '');

    set('stat-alerts', String(alerting));
    set('stat-alerts-foot', alerting ? 'Needs attention' : 'Within thresholds');
    tone('stat-alerts-card', alerting ? 'crit' : '');

    set('stat-power', `${power.toFixed(0)}<small> W</small>`);
    set('stat-power-foot', `${drawing} node${drawing === 1 ? '' : 's'} drawing`
        + (vms ? ` · ${vms} VM${vms === 1 ? '' : 's'} excluded` : ''));

    set('stat-cost', money(monthlyCost(power)));
    const peakMonth = TARIFF.peakMonths.includes(new Date().getMonth() + 1);
    set('stat-cost-foot', `per month · $${effectiveRate().toFixed(2)}/kWh `
        + (peakMonth ? 'peak/off-peak blend' : 'off-peak'));

    const load = fleetLoad(live);
    const pct = (value) => (value === null ? '—' : `${value.toFixed(0)}<small>%</small>`);
    set('load-cpu', pct(load.cpu));
    set('load-cpu-sub', `Average across ${live.length} online node${live.length === 1 ? '' : 's'}`);
    set('load-mem', pct(load.mem));
    set('load-mem-sub', load.mem === null ? 'In use across online nodes'
        : `${bytes(load.memUsed)} of ${bytes(load.memTotal)} in use`);
    set('load-down', load.net_down === null ? '—' : speed(load.net_down));
    set('load-up', load.net_up === null ? '—' : speed(load.net_up));

    set('stat-updates', String(packages + images));
    set('stat-updates-foot', packages + images
        ? [packages && plural(packages, 'package'), images && plural(images, 'image')].filter(Boolean).join(' · ')
        : 'Everything current');
    tone('stat-updates-card', packages + images ? 'warn' : '');

    empty.hidden = all.length > 0;
}

/* --- Detail drawer ------------------------------------------------------ */

const drawer = document.getElementById('drawer');

function openDrawer(hostname) {
    openHost = hostname;
    drawer.classList.add('is-open');
    drawer.setAttribute('aria-hidden', 'false');
    document.body.style.overflow = 'hidden';
    cards.forEach((entry, host) => entry.el.classList.toggle('is-selected', host === hostname));
    renderDrawer();
    // A fresh node starts at the top; only the live refresh keeps its place.
    document.getElementById('drawer-body').scrollTop = 0;
}

function closeDrawer() {
    openHost = null;
    drawer.classList.remove('is-open');
    drawer.setAttribute('aria-hidden', 'true');
    document.body.style.overflow = '';
    cards.forEach((entry) => entry.el.classList.remove('is-selected'));
}

function section(title, icon, body, note = '') {
    return `
        <section>
            <div class="section__head">
                <i class="fa-solid ${icon}"></i>
                <span class="section__title">${esc(title)}</span>
                ${note ? `<span class="section__note">${note}</span>` : ''}
            </div>
            <div class="section__body">${body}</div>
        </section>`;
}

function kvItem(key, value, tone = '') {
    return `<div class="kv__item"${tone ? ` data-tone="${tone}"` : ''}>
        <span class="kv__key">${esc(key)}</span>
        <span class="kv__val">${esc(value)}</span>
    </div>`;
}

function meterRow(label, pct, note, metric) {
    return `
        <div class="metric" data-metric="${metric}"${loadTone(pct) ? ` data-load="${loadTone(pct)}"` : ''}>
            <div class="metric__head">
                <span class="metric__label">${esc(label)}</span>
                <span class="metric__value">${pct.toFixed(0)}%</span>
            </div>
            <div class="meter meter--lg"><i class="meter__fill" style="width:${Math.min(100, pct)}%"></i></div>
            ${note ? `<div class="disk__foot">${esc(note)}</div>` : ''}
        </div>`;
}

/**
 * Container list, outdated images first. The agent checks registries every
 * few hours; until its first check finishes a container has no update field.
 */
function containersSection(device) {
    const list = [...device.docker_containers]
        .sort((a, b) => (b.update === 'available') - (a.update === 'available'));
    const running = list.filter((c) => c.state?.toLowerCase() === 'running').length;
    const images = imageUpdates(device);
    const rows = list.map((c) => {
        const outdated = c.update === 'available';
        // An outdated image says what to do; otherwise the note (why it could
        // not be checked) waits in the tooltip.
        const sub = outdated ? `${c.image} · ${c.update_note}` : c.image;
        return `
            <div class="ctr" data-state="${esc((c.state || '').toLowerCase())}" title="${esc(c.update_note || '')}">
                <span class="ctr__dot"></span>
                <span class="ctr__main">
                    <span class="ctr__name">${esc(c.name)}</span>
                    ${sub ? `<span class="ctr__image">${esc(sub)}</span>` : ''}
                </span>
                ${outdated ? '<span class="ctr__update"><i class="fa-solid fa-circle-arrow-up"></i>Update</span>' : ''}
                <span class="ctr__state">${esc(c.state)}</span>
            </div>`;
    }).join('');
    return section('Containers', 'fa-cube', rows,
        `${running} of ${list.length} up${images ? ` · ${plural(images, 'update')}` : ''}`);
}

function renderDrawer() {
    if (!openHost) return;
    const device = nodes.get(openHost);
    if (!device) return closeDrawer();

    const online = isOnline(device);
    document.getElementById('drawer-name').textContent = device.hostname;
    document.getElementById('drawer-sub').textContent =
        `${device.ip || 'Unknown address'} · ${device.os_info || 'Unknown OS'}${device.is_vm ? ' · VM' : ''}`;
    const status = document.getElementById('drawer-status');
    status.dataset.state = online ? 'online' : 'offline';
    status.querySelector('span').textContent = online ? 'Online' : 'Offline';
    // An online node would be back with its next report, seconds later.
    document.getElementById('drawer-forget').hidden = online;

    const power = nodePower(device);
    const parts = [];

    parts.push(section('Overview', 'fa-gauge-high', `<div class="kv">
        ${kvItem('CPU', `${(device.cpu_usage ?? 0).toFixed(0)}%`, loadTone(device.cpu_usage ?? 0))}
        ${kvItem('Temp', device.temp != null ? `${device.temp.toFixed(1)}°C` : '—', tempTone(device.temp))}
        ${kvItem('Power', power > 0 ? watts(power) : '—', 'power')}
        ${kvItem('Uptime', uptime(device.uptime))}
        ${kvItem('Latency', device.latency != null ? `${device.latency.toFixed(0)} ms` : '—', latencyTone(device.latency))}
        ${kvItem('OS updates', device.pending_updates ?? 0, device.pending_updates ? 'warn' : '')}
    </div>`, device.is_vm ? 'VM, not in lab power'
        : power > 0 ? `${money(monthlyCost(power))}/mo` : ''));

    const ramPct = device.memory ? (device.memory.used / device.memory.total) * 100 : 0;
    parts.push(section('Compute', 'fa-microchip', `
        ${meterRow('CPU load', device.cpu_usage ?? 0,
            `${device.cpu_model || 'Unknown CPU'}${device.cpu_cores ? ` · ${device.cpu_cores.physical}P / ${device.cpu_cores.logical}L` : ''}`, 'cpu')}
        ${meterRow('Memory', ramPct,
            device.memory ? `${bytes(device.memory.used)} of ${bytes(device.memory.total)}` : '', 'ram')}`));

    if (device.gpu) {
        const gpu = device.gpu;
        const vram = gpu.mem_total ? (gpu.mem_used / gpu.mem_total) * 100 : 0;
        parts.push(section('Graphics', 'fa-display', `
            ${meterRow('GPU load', gpu.utilization ?? 0, '', 'gpu')}
            ${meterRow('VRAM', vram, `${bytes(gpu.mem_used)} of ${bytes(gpu.mem_total)}`, 'vram')}
            <div class="kv">
                ${kvItem('Temp', gpu.temp != null ? `${gpu.temp.toFixed(0)}°C` : '—', tempTone(gpu.temp))}
                ${kvItem('Power', gpu.power ? watts(gpu.power) : '—', 'power')}
            </div>`, esc(gpu.name || '')));
    }

    const disks = device.disks?.length
        ? device.disks
        : (device.disk ? [{ mount: '/', device: '', used: device.disk.used, total: device.disk.total,
                            percent: (device.disk.used / device.disk.total) * 100 }] : []);
    if (disks.length) {
        const io = device.disk_speeds
            ? `R ${speed(device.disk_speeds.read_speed)} · W ${speed(device.disk_speeds.write_speed)}` : '';
        parts.push(section('Storage', 'fa-hard-drive', disks.map((disk) => `
            <div class="disk">
                <div class="disk__head">
                    <span class="disk__mount">${esc(disk.mount)}</span>
                    <span class="disk__dev">${esc(disk.device || '')}</span>
                    <span class="disk__pct">${disk.percent.toFixed(0)}%</span>
                </div>
                <div class="meter meter--lg"><i class="meter__fill" style="width:${Math.min(100, disk.percent)}%;background:var(--m-disk)"></i></div>
                <div class="disk__foot">${bytes(disk.used)} used of ${bytes(disk.total)}</div>
            </div>`).join(''), io));
    }

    const vpnRow = device.vpns ? `
        <div class="vpn-row">
            <span class="vpn" data-active="${Boolean(device.vpns.tailscale)}"><i class="fa-solid fa-circle-nodes"></i>Tailscale</span>
            <span class="vpn" data-active="${Boolean(device.vpns.openvpn)}"><i class="fa-solid fa-lock"></i>OpenVPN</span>
        </div>` : '';
    parts.push(section('Network', 'fa-arrows-up-down', `
        <div class="kv">
            ${kvItem('Download', device.network ? speed(device.network.down_speed) : '—')}
            ${kvItem('Upload', device.network ? speed(device.network.up_speed) : '—')}
        </div>
        ${vpnRow}`));

    if (device.docker_containers?.length) {
        parts.push(containersSection(device));
    }

    // Content height is stable between ticks, so keeping scrollTop is enough
    // to make the live refresh invisible to someone reading mid-list.
    const body = document.getElementById('drawer-body');
    const scroll = body.scrollTop;
    body.innerHTML = parts.join('');
    body.scrollTop = scroll;
}

/* --- History: lab power and fleet load ----------------------------------- */

const drawCharts = () => charts.forEach((chart) => chart.setData(samples, range));

async function loadHistory(nextRange) {
    range = nextRange;
    document.querySelectorAll('#range-selector button').forEach((btn) => {
        btn.classList.toggle('is-active', btn.dataset.range === range);
    });
    document.getElementById('trend-sub').textContent = {
        '6h': 'Last 6 hours', '24h': 'Last 24 hours',
        '7d': 'Last 7 days', '30d': 'Last 30 days',
    }[range];

    try {
        samples = await getJson(`/api/power-history?range=${range}`);
        drawCharts();
    } catch (err) {
        console.error('History request failed:', err);
    }
}

/** Fold the live values into the tail of the 6h series between DB writes. */
function appendLive() {
    if (range !== '6h' || !charts.length) return;
    // The chart places points by time, and the server stamps its own points,
    // so take "now" from the newest report rather than this browser's clock:
    // a few minutes of skew would otherwise open a fake gap at the right edge.
    const now = Math.max(...[...nodes.values()].map((d) => d.last_seen || 0));
    const live = [...nodes.values()].filter(isOnline);
    const { cpu, mem, net_down, net_up } = fleetLoad(live);
    const sample = { power: live.reduce((sum, d) => sum + nodePower(d), 0), cpu, mem, net_down, net_up };
    const last = samples[samples.length - 1];

    if (!last || now - last.time >= 60) {
        samples.push({ time: now, ...sample });
        if (samples.length > 400) samples.shift();
    } else {
        Object.assign(last, sample);
    }
    drawCharts();
}

/* --- Wiring ------------------------------------------------------------- */

function ingest(device) {
    const previous = nodes.get(device.hostname);
    // Older servers send no age; fall back to the cross-clock comparison.
    device.seen_at = device.age === undefined
        ? device.last_seen
        : Date.now() / 1000 - device.age;

    // The server re-broadcasts each node every 10 s with its ping latency,
    // and replays its stored registry on reconnect. Only a genuinely newer
    // report should add a point to the sparkline.
    const history = previous?.cpuHistory || [];
    device.cpuHistory = device.last_seen > (previous?.last_seen ?? -Infinity)
        ? [...history, device.cpu_usage ?? 0].slice(-HISTORY_POINTS)
        : history;
    device.alerts = previous?.alerts;
    nodes.set(device.hostname, device);
}

function dropNode(hostname) {
    nodes.delete(hostname);
    cards.get(hostname)?.el.remove();
    cards.delete(hostname);
    if (openHost === hostname) closeDrawer();
    updateFleet();
}

async function forgetOpenNode() {
    const hostname = openHost;
    if (!hostname || !confirm(`Remove ${hostname} from the dashboard? `
        + 'It comes back if its agent reports again.')) return;
    try {
        const response = await api(`/api/devices/${encodeURIComponent(hostname)}`, { method: 'DELETE' });
        if (!response.ok && response.status !== 404) throw new Error(`responded ${response.status}`);
        dropNode(hostname);
    } catch (err) {
        console.error('Removing node failed:', err);
        toast('Could not remove node', { host: hostname, tone: 'crit' });
    }
}

async function start() {
    if (!await ensureAuthenticated()) return;

    mountShell();
    unlockAudioOnInteraction();

    const chart = (id, opts) => createTrendChart(
        document.getElementById(`${id}-canvas`), document.getElementById(`${id}-tip`), opts);
    const percentAxis = { axis: (v) => `${Math.round(v)}%`, readout: (v) => `${v.toFixed(0)}%`, minPeak: 10, max: 100 };
    charts = [
        chart('trend', {
            series: [{ key: 'power', label: 'Power', color: '--m-power' }],
            axis: (v) => `${Math.round(v)}W`,
            readout: (v) => `${v.toFixed(0)} W`,
            minPeak: 50,
        }),
        chart('cpu', { series: [{ key: 'cpu', label: 'CPU', color: '--m-cpu' }], ...percentAxis }),
        chart('mem', { series: [{ key: 'mem', label: 'Memory', color: '--m-ram' }], ...percentAxis }),
        chart('net', {
            series: [
                { key: 'net_down', label: 'Down', color: '--m-down' },
                { key: 'net_up', label: 'Up', color: '--m-up' },
            ],
            axis: speedShort,
            readout: speed,
            minPeak: 1024,
        }),
    ];

    document.getElementById('range-selector').addEventListener('click', (event) => {
        const btn = event.target.closest('button');
        if (btn) loadHistory(btn.dataset.range);
    });

    document.getElementById('drawer-close').addEventListener('click', closeDrawer);
    document.getElementById('drawer-forget').addEventListener('click', forgetOpenNode);
    drawer.querySelector('.drawer__scrim').addEventListener('click', closeDrawer);
    window.addEventListener('keydown', (event) => {
        if (event.key === 'Escape' && openHost) closeDrawer();
    });

    loadHistory('6h');
    let reconnected = false;

    connectStream({
        // Also sent on every reconnect, e.g. after a server restart. Merge it
        // rather than starting over, which threw away every sparkline.
        init: (payload) => {
            const known = new Set(payload.map((device) => device.hostname));
            [...nodes.keys()].filter((host) => !known.has(host)).forEach(dropNode);
            payload.forEach(ingest);
            nodes.forEach(updateCard);
            reorder();
            updateFleet();
            // Points appended locally while the server was away are guesses.
            if (reconnected) loadHistory(range);
            reconnected = true;
        },
        removed: ({ hostname }) => dropNode(hostname),
        metrics: (device) => {
            ingest(device);
            const stored = nodes.get(device.hostname);
            evaluateAlerts(stored);
            updateCard(stored);
            reorder();
            if (openHost === device.hostname) renderDrawer();
            updateFleet();
            appendLive();
        },
    });

    // Nothing pushes an "went offline" event, so re-evaluate staleness locally.
    setInterval(() => {
        nodes.forEach(updateCard);
        reorder();
        updateFleet();
        if (openHost) renderDrawer();
    }, 5000);

    window.addEventListener('resize', () => {
        nodes.forEach((device) => {
            const entry = cards.get(device.hostname);
            if (entry && device.cpuHistory?.length > 1) {
                drawSparkline(entry.ref.cpuSpark, device.cpuHistory, '--m-cpu');
            }
        });
    });
}

start();
