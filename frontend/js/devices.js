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
    esc, bytes, bytesShort, speed, speedShort, uptime, uptimeShort, ago, watts, money,
    loadTone, tempTone, latencyTone, deviceIcon,
} from './format.js';

/* --- State -------------------------------------------------------------- */

const nodes = new Map();   // hostname -> latest device payload (+ history, alerts)
const cards = new Map();   // hostname -> { el, ref }
let order = readOrder();     // hostnames in the order the user arranged them
let dragging = null;         // the card being dragged, while a drag is in progress
let sizes = readSizes();     // hostname -> 'large' | 'compact'; absent means normal
let openHost = null;
let drawerCharts = [];
const nodeHistory = new Map();   // hostname -> that node's samples for the selected range
let charts = [];     // power, CPU, memory and network, all fed from `samples`
let samples = [];    // history for the selected range, oldest first
let range = '6h';

const grid = document.getElementById('node-grid');
const empty = document.getElementById('node-empty');

function readOrder() {
    try {
        const saved = localStorage.getItem('nodeOrder');
        // Dragging replaced "pin to top"; nodes pinned before start the order.
        return JSON.parse(saved || localStorage.getItem('pinnedDevices') || '[]');
    } catch {
        return [];
    }
}

function saveOrder() {
    order = [...grid.querySelectorAll('.node')].map((el) => el.dataset.host);
    try {
        localStorage.setItem('nodeOrder', JSON.stringify(order));
        localStorage.removeItem('pinnedDevices');
    } catch {
        // Storage blocked: the arrangement lasts until the page reloads.
    }
}

function readSizes() {
    try {
        return JSON.parse(localStorage.getItem('nodeSizes') || '{}');
    } catch {
        return {};
    }
}

// Clicking the size button steps through these. A large card spans two
// columns and shows what the drawer would, so a node you watch closely
// needs no clicking; a compact one keeps just its meters.
const SIZES = {
    normal:  { next: 'large',   icon: 'fa-maximize', title: 'Larger: show details on the card' },
    large:   { next: 'compact', icon: 'fa-minimize', title: 'Compact: meters only' },
    compact: { next: 'normal',  icon: 'fa-expand',   title: 'Normal size' },
};

const PERCENT_CHART = { axis: (v) => `${Math.round(v)}%`, readout: (v) => `${v.toFixed(0)}%`, minPeak: 10, max: 100 };
const LOAD_CHARTS = {
    cpu: { series: [{ key: 'cpu', label: 'CPU', color: '--m-cpu' }], ...PERCENT_CHART },
    mem: { series: [{ key: 'mem', label: 'Memory', color: '--m-ram' }], ...PERCENT_CHART },
    net: {
        series: [
            { key: 'net_down', label: 'Down', color: '--m-down' },
            { key: 'net_up', label: 'Up', color: '--m-up' },
        ],
        axis: speedShort,
        readout: speed,
        minPeak: 1024,
    },
};

/** Charts for one node's own history, on the given canvas/tooltip pairs. */
const nodeCharts = (cpu, mem, net) => [
    createTrendChart(cpu[0], cpu[1], LOAD_CHARTS.cpu),
    createTrendChart(mem[0], mem[1], LOAD_CHARTS.mem),
    createTrendChart(net[0], net[1], LOAD_CHARTS.net),
];

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
            <button class="icon-btn" data-r="size"><i class="fa-solid"></i></button>
            <button class="icon-btn node__grip" data-r="grip"
                    title="Drag to rearrange (or focus and use the arrow keys)" aria-label="Move card">
                <i class="fa-solid fa-grip-vertical"></i>
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
        <div class="metric" data-metric="disk" data-r="diskMetric" hidden>
            <div class="metric__head">
                <span class="metric__label">Disk</span>
                <span class="metric__note" data-r="diskNote"></span>
                <span class="metric__value" data-r="diskValue">—</span>
            </div>
            <div class="meter"><i class="meter__fill" data-r="diskBar"></i></div>
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
    </div>

    <div class="node__extra" data-r="extra">
        <div class="node__history">
            <div class="mini">
                <div class="mini__label">CPU</div>
                <div class="trend__body mini__body">
                    <canvas class="trend__canvas" data-r="histCpu"></canvas><div class="trend__tip" data-r="histCpuTip"></div>
                </div>
            </div>
            <div class="mini">
                <div class="mini__label">Memory</div>
                <div class="trend__body mini__body">
                    <canvas class="trend__canvas" data-r="histMem"></canvas><div class="trend__tip" data-r="histMemTip"></div>
                </div>
            </div>
            <div class="mini">
                <div class="mini__label">Network <span class="legend__item"><i style="background: var(--m-down)"></i>Down</span><span class="legend__item"><i style="background: var(--m-up)"></i>Up</span></div>
                <div class="trend__body mini__body">
                    <canvas class="trend__canvas" data-r="histNet"></canvas><div class="trend__tip" data-r="histNetTip"></div>
                </div>
            </div>
        </div>
        <div class="node__details" data-r="details"></div>
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

    ref.grip.addEventListener('pointerdown', (event) => startDrag(event, el));
    ref.grip.addEventListener('click', (event) => event.stopPropagation());
    ref.grip.addEventListener('keydown', (event) => nudgeCard(event, el));
    ref.size.addEventListener('click', (event) => {
        event.stopPropagation();
        resizeCard(device.hostname);
    });
    // A large card already shows the drawer's content; reading or hovering
    // its charts should not open the drawer on top of it.
    ref.extra.addEventListener('click', (event) => event.stopPropagation());

    el.addEventListener('click', () => openDrawer(device.hostname));
    el.addEventListener('keydown', (event) => {
        // Enter on the pin or size button bubbles up here too.
        if (event.target !== el) return;
        if (event.key === 'Enter' || event.key === ' ') {
            event.preventDefault();
            openDrawer(device.hostname);
        }
    });

    const entry = { el, ref, chipKey: '' };
    cards.set(device.hostname, entry);
    grid.appendChild(el);
    cardSizer.observe(el);
    return entry;
}

function updateCard(device) {
    const entry = cards.get(device.hostname) || buildCard(device);
    const { el, ref } = entry;
    const online = isOnline(device);

    el.dataset.state = online ? 'online' : 'offline';

    const size = sizes[device.hostname] || 'normal';
    el.dataset.size = size;
    ref.size.firstElementChild.className = `fa-solid ${SIZES[size].icon}`;
    ref.size.title = SIZES[size].title;
    ref.size.setAttribute('aria-label', SIZES[size].title);

    ref.status.dataset.state = online ? 'online' : 'offline';
    ref.statusText.textContent = online ? 'Online' : 'Offline';

    ref.ip.textContent = device.ip || 'Unknown address';
    if (device.latency !== undefined && device.latency !== null && online) {
        ref.latency.textContent = ` · ${device.latency.toFixed(0)} ms`;
        ref.latency.dataset.tone = latencyTone(device.latency);
    } else if (!online && device.seen_at) {
        ref.latency.textContent = ` · last seen ${ago(Date.now() / 1000 - device.seen_at)}`;
        ref.latency.removeAttribute('data-tone');
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

    const disk = urgentDisk(device);
    ref.diskMetric.hidden = !disk;
    if (disk) {
        ref.diskValue.textContent = `${disk.percent.toFixed(0)}%`;
        ref.diskBar.style.width = `${Math.min(100, disk.percent)}%`;
        ref.diskNote.textContent = `${disk.mount} · ${bytesShort(disk.used)} / ${bytesShort(disk.total)}`;
        ref.diskNote.title = 'The disk nearest its alert threshold';
        setTone(ref.diskMetric, 'load', diskTone(disk.percent, diskTrip(device.hostname, disk.mount)));
    }

    if (size === 'large') {
        const html = [storageSection(device), networkSection(device), gpuSection(device),
            device.docker_containers?.length ? containersSection(device) : ''].join('');
        // Rebuilt only when something shown changed, so a hover tooltip on a
        // container is not wiped every few seconds.
        // Nor while a disk's alert dropdown in it has focus, which would close it.
        if (html !== entry.detailsHtml && !ref.details.contains(document.activeElement)) {
            ref.details.innerHTML = html;
            entry.detailsHtml = html;
        }
    }

    el.classList.toggle('is-alerting', online && hasAlert(device));

    if (device.cpuHistory?.length > 1) {
        requestAnimationFrame(() => drawSparkline(ref.cpuSpark, device.cpuHistory, '--m-cpu'));
    }
}

function resizeCard(hostname) {
    const next = SIZES[sizes[hostname] || 'normal'].next;
    if (next === 'normal') delete sizes[hostname];
    else sizes[hostname] = next;
    localStorage.setItem('nodeSizes', JSON.stringify(sizes));

    const entry = cards.get(hostname);
    entry.detailsHtml = '';
    updateCard(nodes.get(hostname));
    if (next === 'large') showNodeHistory(hostname);
    // The sparkline canvas changes width with the card.
    const device = nodes.get(hostname);
    if (device.cpuHistory?.length > 1) drawSparkline(entry.ref.cpuSpark, device.cpuHistory, '--m-cpu');
}

/* --- Board layout: packing and drag to rearrange ------------------------- */

// The grid's rows are this many pixels tall (layout.css); each card spans
// as many as its height needs, so short cards stack beside a tall large one
// instead of leaving a hole under themselves.
const ROW_UNIT = 4;

const cardSizer = new ResizeObserver((entries) => {
    for (const { target } of entries) {
        const gap = parseFloat(getComputedStyle(target).marginBottom) || 0;
        target.style.gridRowEnd = `span ${Math.ceil((target.offsetHeight + gap) / ROW_UNIT)}`;
    }
});

/**
 * Dragging moves the card itself through the grid, so the board reflows
 * live under the pointer. Pointer events rather than HTML drag and drop, so
 * it works with touch too.
 */
function startDrag(event, el) {
    if (event.button !== 0) return;
    event.preventDefault();
    event.stopPropagation();
    const id = event.pointerId;
    dragging = { el, x: event.clientX, y: event.clientY, last: null };
    el.classList.add('is-dragging');
    document.body.classList.add('is-arranging');

    // Listened for on the window, not the grip: moving the card through the
    // grid detaches the grip, which drops any pointer capture it held.
    const move = (e) => {
        if (e.pointerId !== id) return;
        dragging.x = e.clientX;
        dragging.y = e.clientY;
        dragOver();
    };
    const end = (e) => {
        if (e.pointerId !== id) return;
        window.removeEventListener('pointermove', move);
        window.removeEventListener('pointerup', end);
        window.removeEventListener('pointercancel', end);
        el.classList.remove('is-dragging');
        document.body.classList.remove('is-arranging');
        dragging = null;
        saveOrder();
        // Letting go over the dragged card would click it open.
        suppressClick = true;
        setTimeout(() => { suppressClick = false; }, 0);
    };
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', end);
    window.addEventListener('pointercancel', end);
    requestAnimationFrame(autoScroll);
}

let suppressClick = false;
window.addEventListener('click', (event) => {
    if (!suppressClick) return;
    event.stopPropagation();
    event.preventDefault();
}, true);

/** Move the dragged card before or after the card under the pointer. */
function dragOver() {
    const { el, x, y } = dragging;
    const over = document.elementFromPoint(x, y)?.closest('.node');
    if (!over || over === el || over.parentElement !== grid) return;
    const rect = over.getBoundingClientRect();
    const before = x < rect.left + rect.width / 2;
    // Moving shifts the cards; without this the pair under the pointer can
    // swap back and forth on every pointer move.
    const key = `${over.dataset.host}:${before}`;
    if (dragging.last === key) return;
    dragging.last = key;
    grid.insertBefore(el, before ? over : over.nextSibling);
}

/** Scroll while the pointer is held near the top or bottom of the window. */
function autoScroll() {
    if (!dragging) return;
    const edge = 72;
    const step = dragging.y < edge ? -14 : dragging.y > window.innerHeight - edge ? 14 : 0;
    if (step) {
        window.scrollBy(0, step);
        dragOver();
    }
    requestAnimationFrame(autoScroll);
}

/** Arrow keys on the grip move its card one place, for keyboard users. */
function nudgeCard(event, el) {
    const back = event.key === 'ArrowLeft' || event.key === 'ArrowUp';
    const forward = event.key === 'ArrowRight' || event.key === 'ArrowDown';
    if (!back && !forward) return;
    event.preventDefault();
    const sibling = back ? el.previousElementSibling : el.nextElementSibling;
    if (!sibling?.classList.contains('node')) return;
    grid.insertBefore(el, back ? sibling : sibling.nextSibling);
    saveOrder();
    event.currentTarget.focus();   // moving the card drops focus
}

/** Disks as reported, falling back to the single "disk" older agents send. */
function diskList(device) {
    if (device.disks?.length) return device.disks;
    if (!device.disk) return [];
    return [{ mount: '/', device: '', used: device.disk.used, total: device.disk.total,
              percent: (device.disk.used / device.disk.total) * 100 }];
}


/** Set a data attribute only when it changes, and remove it when empty. */
function setTone(el, attribute, value) {
    if (value) el.setAttribute(`data-${attribute}`, value);
    else el.removeAttribute(`data-${attribute}`);
}

/**
 * The user's arrangement first; nodes never placed by hand follow, online
 * before offline, then by name. Safe to call on every tick: it compares
 * against the current DOM order and only moves nodes when it changed.
 */
function reorder() {
    if (dragging) return;   // the pointer owns the order mid-drag
    const rank = new Map(order.map((host, i) => [host, i]));
    const desired = [...nodes.values()]
        .sort((a, b) => {
            const ra = rank.get(a.hostname) ?? Infinity;
            const rb = rank.get(b.hostname) ?? Infinity;
            if (ra !== rb) return ra < rb ? -1 : 1;
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

    const check = (key, value, unit = '', limits = THRESHOLDS[key]) => {
        if (value === undefined || value === null) {
            alerts[key] = false;
            return;
        }
        const { trip, clear, label } = limits;
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

    // Every disk against its own threshold. A disk switched off, or no longer
    // reported, has its alert cleared.
    const diskKeys = new Set();
    diskList(device).forEach((disk) => {
        const key = `disk:${disk.mount}`;
        const trip = diskTrip(device.hostname, disk.mount);
        diskKeys.add(key);
        if (trip === null) alerts[key] = false;
        else check(key, disk.percent, '%', { trip, clear: trip - 5, label: `Disk ${disk.mount}` });
    });
    Object.keys(alerts)
        .filter((key) => key.startsWith('disk:') && !diskKeys.has(key))
        .forEach((key) => { alerts[key] = false; });

    device.alerts = alerts;

    if (tripped.length) chime();
    tripped.forEach((message) => toast(message, { host: device.hostname, tone: 'crit' }));
    cleared.forEach((message) => toast(message, { host: device.hostname, tone: 'ok' }));
}

/* --- Per-disk alert thresholds ------------------------------------------ */

// hostname -> mount -> percent or 'off', kept on the server so every browser
// (and later the server's own alerts) uses the same settings.
let diskAlerts = {};
const DISK_CHOICES = [null, 80, 95, 98, 'off'];   // null is the default

/** The percent this disk alerts at, or null when its alert is off. */
function diskTrip(hostname, mount) {
    const set = diskAlerts[hostname]?.[mount];
    if (set === 'off') return null;
    return typeof set === 'number' ? set : THRESHOLDS.disk.trip;
}

/** Meter tone against the disk's own threshold: amber from 15 points short. */
function diskTone(percent, trip) {
    if (trip === null) return '';
    if (percent >= trip) return 'crit';
    return percent >= trip - 15 ? 'warn' : '';
}

/** The disk nearest its threshold, or the fullest when every alert is off. */
function urgentDisk(device) {
    const disks = diskList(device);
    const watched = disks.filter((d) => diskTrip(device.hostname, d.mount) !== null);
    const score = (d) => d.percent / (diskTrip(device.hostname, d.mount) ?? 100);
    return (watched.length ? watched : disks).reduce((top, d) => (!top || score(d) > score(top) ? d : top), null);
}

function diskAlertSelect(hostname, mount) {
    const set = diskAlerts[hostname]?.[mount] ?? null;
    const choices = DISK_CHOICES.includes(set) ? DISK_CHOICES : [...DISK_CHOICES, set];
    const options = choices.map((value) => {
        const label = value === null ? `${THRESHOLDS.disk.trip}% (default)` : value === 'off' ? 'Off' : `${value}%`;
        return `<option value="${value ?? ''}"${value === set ? ' selected' : ''}>${label}</option>`;
    }).join('');
    return `
        <label class="disk__alert" title="Alert when this disk is fuller than this">
            Alert at
            <select data-disk-alert data-host="${esc(hostname)}" data-mount="${esc(mount)}">${options}</select>
        </label>`;
}

function applyDiskAlerts(map) {
    diskAlerts = map || {};
    // Re-check now, so switching a disk off clears its alert straight away.
    // Offline nodes only hold stale figures, which must not raise alerts.
    nodes.forEach((device) => {
        if (isOnline(device)) evaluateAlerts(device);
        updateCard(device);
    });
    updateFleet();
    renderDrawer();
}

async function loadDiskAlerts() {
    try {
        applyDiskAlerts(await getJson('/api/settings/disk-alerts'));
    } catch (err) {
        console.error('Disk alert settings request failed:', err);
    }
}

async function saveDiskAlert(select) {
    const { host, mount } = select.dataset;
    const trip = select.value === '' ? null : select.value === 'off' ? 'off' : Number(select.value);
    select.blur();   // lets the section refresh again
    try {
        const response = await api('/api/settings/disk-alerts', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ hostname: host, mount, trip }),
        });
        if (!response.ok) {
            // A server not restarted since the update has no such endpoint:
            // the request falls through to the static files, which answer
            // 405 to a PUT. The page itself is new, so this is easy to hit.
            throw new Error(response.status === 404 || response.status === 405
                ? 'the dashboard server is running older code. Restart it: sudo systemctl restart homelab-dashboard'
                : `the server answered ${response.status}`);
        }
        applyDiskAlerts(await response.json());
    } catch (err) {
        console.error('Saving the disk alert failed:', err);
        toast(`Could not save the disk alert: ${err.message}`, { host, tone: 'crit' });
    }
}

/* --- Fleet summary ------------------------------------------------------ */

let serverAlerts = [];   // active alerts raised by the server, sent on the stream

function updateFleet() {
    const all = [...nodes.values()];
    const live = all.filter(isOnline);
    const power = live.reduce((sum, d) => sum + nodePower(d), 0);
    const drawing = live.filter((d) => !d.is_vm).length;
    const vms = live.length - drawing;
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

    // The server's alerts, the ones that reach your phone: they include
    // offline nodes and down services, which cards alone cannot show.
    set('stat-alerts', String(serverAlerts.length));
    set('stat-alerts-foot', serverAlerts.length
        ? esc(serverAlerts[0].title) + (serverAlerts.length > 1 ? ` and ${serverAlerts.length - 1} more` : '')
        : 'All clear');
    tone('stat-alerts-card', !serverAlerts.length ? ''
        : serverAlerts.some((a) => a.severity === 'crit') ? 'crit' : 'warn');

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
    showNodeHistory(hostname);
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
    return section('Containers', 'fa-cube', `<div class="ctr-list">${rows}</div>`,
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

    parts.push(gpuSection(device), storageSection(device), networkSection(device));
    if (device.docker_containers?.length) {
        parts.push(containersSection(device));
    }

    // Content height is stable between ticks, so keeping scrollTop is enough
    // to make the live refresh invisible to someone reading mid-list.
    // The history charts above these sections stay put; only they refresh.
    const body = document.getElementById('drawer-body');
    const sections = document.getElementById('drawer-sections');
    // Rebuilding under a disk's open alert dropdown would close it.
    if (sections.contains(document.activeElement)) return;
    const scroll = body.scrollTop;
    sections.innerHTML = parts.join('');
    body.scrollTop = scroll;
}

/* Detail sections shared by the drawer and large cards. */

function gpuSection(device) {
    const gpu = device.gpu;
    if (!gpu) return '';
    const vram = gpu.mem_total ? (gpu.mem_used / gpu.mem_total) * 100 : 0;
    return section('Graphics', 'fa-display', `
        ${meterRow('GPU load', gpu.utilization ?? 0, '', 'gpu')}
        ${meterRow('VRAM', vram, `${bytes(gpu.mem_used)} of ${bytes(gpu.mem_total)}`, 'vram')}
        <div class="kv">
            ${kvItem('Temp', gpu.temp != null ? `${gpu.temp.toFixed(0)}°C` : '—', tempTone(gpu.temp))}
            ${kvItem('Power', gpu.power ? watts(gpu.power) : '—', 'power')}
        </div>`, esc(gpu.name || ''));
}

function storageSection(device) {
    const disks = diskList(device);
    if (!disks.length) return '';
    const io = device.disk_speeds
        ? `R ${speed(device.disk_speeds.read_speed)} · W ${speed(device.disk_speeds.write_speed)}` : '';
    return section('Storage', 'fa-hard-drive', disks.map((disk) => {
        const tone = diskTone(disk.percent, diskTrip(device.hostname, disk.mount));
        return `
        <div class="disk"${tone ? ` data-tone="${tone}"` : ''}>
            <div class="disk__head">
                <span class="disk__mount">${esc(disk.mount)}</span>
                <span class="disk__dev">${esc(disk.device || '')}</span>
                <span class="disk__pct">${disk.percent.toFixed(0)}%</span>
            </div>
            <div class="meter meter--lg"><i class="meter__fill" style="width:${Math.min(100, disk.percent)}%"></i></div>
            <div class="disk__foot">
                <span>${bytes(disk.used)} of ${bytes(disk.total)}</span>
                ${diskAlertSelect(device.hostname, disk.mount)}
            </div>
        </div>`;
    }).join(''), io);
}

function networkSection(device) {
    const vpnRow = device.vpns ? `
        <div class="vpn-row">
            <span class="vpn" data-active="${Boolean(device.vpns.tailscale)}"><i class="fa-solid fa-circle-nodes"></i>Tailscale</span>
            <span class="vpn" data-active="${Boolean(device.vpns.openvpn)}"><i class="fa-solid fa-lock"></i>OpenVPN</span>
        </div>` : '';
    return section('Network', 'fa-arrows-up-down', `
        <div class="kv">
            ${kvItem('Download', device.network ? speed(device.network.down_speed) : '—')}
            ${kvItem('Upload', device.network ? speed(device.network.up_speed) : '—')}
        </div>
        ${vpnRow}`);
}

/* --- One node's own history (large cards and the drawer) ----------------- */

/** Hosts whose history is on screen: large cards and the open drawer. */
const historyHosts = () => new Set([
    ...[...nodes.keys()].filter((host) => sizes[host] === 'large'),
    ...(openHost ? [openHost] : []),
]);

async function loadNodeHistory(hostname) {
    const asked = range;
    try {
        const data = await getJson(`/api/devices/${encodeURIComponent(hostname)}/history?range=${asked}`);
        if (asked !== range) return;   // the range changed while this was in flight
        nodeHistory.set(hostname, data);
    } catch (err) {
        console.error(`History for ${hostname} failed:`, err);
        return;
    }
    drawNodeHistory(hostname);
}

function drawNodeHistory(hostname) {
    const data = nodeHistory.get(hostname) || [];
    const entry = cards.get(hostname);
    if (entry && sizes[hostname] === 'large') {
        entry.charts ||= nodeCharts(
            [entry.ref.histCpu, entry.ref.histCpuTip],
            [entry.ref.histMem, entry.ref.histMemTip],
            [entry.ref.histNet, entry.ref.histNetTip]);
        entry.charts.forEach((chart) => chart.setData(data, range));
    }
    if (openHost === hostname) drawerCharts.forEach((chart) => chart.setData(data, range));
}

/** Draw what is cached straight away, then fetch the latest. */
function showNodeHistory(hostname) {
    drawNodeHistory(hostname);
    loadNodeHistory(hostname);
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
        document.getElementById('drawer-history-note').textContent =
            document.getElementById('trend-sub').textContent;
        historyHosts().forEach(loadNodeHistory);
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
    nodeHistory.delete(hostname);
    if (cards.has(hostname)) cardSizer.unobserve(cards.get(hostname).el);
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
    drawerCharts = nodeCharts(
        ['drawer-cpu-canvas', 'drawer-cpu-tip'].map((id) => document.getElementById(id)),
        ['drawer-mem-canvas', 'drawer-mem-tip'].map((id) => document.getElementById(id)),
        ['drawer-net-canvas', 'drawer-net-tip'].map((id) => document.getElementById(id)));
    charts = [
        chart('trend', {
            series: [{ key: 'power', label: 'Power', color: '--m-power' }],
            axis: (v) => `${Math.round(v)}W`,
            readout: (v) => `${v.toFixed(0)} W`,
            minPeak: 50,
        }),
        chart('cpu', LOAD_CHARTS.cpu),
        chart('mem', LOAD_CHARTS.mem),
        chart('net', LOAD_CHARTS.net),
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
    // Before the first reports arrive, so a disk set to "off" never alerts.
    await loadDiskAlerts();
    document.addEventListener('change', (event) => {
        const select = event.target.closest('select[data-disk-alert]');
        if (select) saveDiskAlert(select);
    });
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
            if (reconnected) {
                loadHistory(range);
                loadDiskAlerts();   // changes made while the stream was down
            } else {
                historyHosts().forEach(loadNodeHistory);
            }
            reconnected = true;
        },
        removed: ({ hostname }) => dropNode(hostname),
        alerts: (list) => {
            serverAlerts = list || [];
            updateFleet();
        },
        disk_alerts: applyDiskAlerts,
        metrics: (device) => {
            ingest(device);
            const stored = nodes.get(device.hostname);
            // The ping sweep re-broadcasts offline nodes too; their last
            // figures are stale and must not raise alerts.
            if (isOnline(stored)) evaluateAlerts(stored);
            updateCard(stored);
            reorder();
            if (openHost === device.hostname) renderDrawer();
            updateFleet();
            appendLive();
        },
    });

    // The server records each node once a minute.
    setInterval(() => historyHosts().forEach(loadNodeHistory), 60000);

    // Nothing pushes an "went offline" event, so re-evaluate staleness locally.
    setInterval(() => {
        nodes.forEach(updateCard);
        reorder();
        updateFleet();
        if (openHost) renderDrawer();
    }, 5000);

    const redrawSparklines = () => {
        nodes.forEach((device) => {
            const entry = cards.get(device.hostname);
            if (entry && device.cpuHistory?.length > 1) {
                drawSparkline(entry.ref.cpuSpark, device.cpuHistory, '--m-cpu');
            }
        });
    };
    window.addEventListener('resize', redrawSparklines);
    window.addEventListener('themechange', redrawSparklines);
}

start();
