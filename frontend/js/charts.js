/* Canvas charts. All colours are read from the CSS tokens so the palette
   still lives in exactly one place. */

const cache = new Map();

/** Resolve a CSS custom property once and remember it. */
function cssVar(name) {
    if (!cache.has(name)) {
        cache.set(name, getComputedStyle(document.documentElement).getPropertyValue(name).trim());
    }
    return cache.get(name);
}

/** Size the backing store for the display density and return the CSS box. */
function prepare(canvas) {
    const dpr = window.devicePixelRatio || 1;
    const width = canvas.clientWidth;
    const height = canvas.clientHeight;
    if (!width || !height) return null;

    if (canvas.width !== Math.round(width * dpr) || canvas.height !== Math.round(height * dpr)) {
        canvas.width = Math.round(width * dpr);
        canvas.height = Math.round(height * dpr);
    }

    const ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);
    return { ctx, width, height };
}

function withAlpha(color, alpha) {
    // Tokens are hex; convert so the area fill can fade out.
    const hex = color.replace('#', '');
    const full = hex.length === 3 ? hex.split('').map((c) => c + c).join('') : hex;
    const r = parseInt(full.slice(0, 2), 16);
    const g = parseInt(full.slice(2, 4), 16);
    const b = parseInt(full.slice(4, 6), 16);
    return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

/**
 * Small inline trend line over the caller's ring buffer of percentages.
 * The series always spans the full width: while the buffer is still filling
 * a fixed window would draw a stub pinned to the right edge, which reads as
 * a broken chart rather than a young one.
 */
export function drawSparkline(canvas, history, colorToken) {
    const box = prepare(canvas);
    if (!box || !history || history.length < 2) return;

    const { ctx, width, height } = box;
    const color = cssVar(colorToken);
    const step = width / (history.length - 1);
    const y = (value) => height - 1.5 - (Math.max(0, Math.min(100, value)) / 100) * (height - 3);

    ctx.beginPath();
    history.forEach((value, i) => {
        if (i === 0) ctx.moveTo(i * step, y(value));
        else ctx.lineTo(i * step, y(value));
    });

    // Fill under the curve first, then stroke on top of it.
    ctx.save();
    ctx.lineTo(width, height);
    ctx.lineTo(0, height);
    ctx.closePath();
    const gradient = ctx.createLinearGradient(0, 0, 0, height);
    gradient.addColorStop(0, withAlpha(color, 0.22));
    gradient.addColorStop(1, withAlpha(color, 0));
    ctx.fillStyle = gradient;
    ctx.fill();
    ctx.restore();

    ctx.beginPath();
    history.forEach((value, i) => {
        if (i === 0) ctx.moveTo(i * step, y(value));
        else ctx.lineTo(i * step, y(value));
    });
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.5;
    ctx.lineJoin = 'round';
    ctx.lineCap = 'round';
    ctx.stroke();
}

function timeLabel(unixSeconds, range) {
    const d = new Date(unixSeconds * 1000);
    if (range === '6h' || range === '24h') {
        return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false });
    }
    if (range === '7d') {
        return d.toLocaleDateString([], { weekday: 'short' });
    }
    return d.toLocaleDateString([], { month: 'short', day: 'numeric' });
}

function fullLabel(unixSeconds, range) {
    const d = new Date(unixSeconds * 1000);
    if (range === '6h' || range === '24h') {
        return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false });
    }
    return `${d.toLocaleDateString([], { month: 'short', day: 'numeric' })} ${
        d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false })}`;
}

const PAD = { top: 14, right: 10, bottom: 22, left: 38 };

// Seconds each range covers, and the server's spacing between its points
// (the raw 1-minute samples for 6h, the SQL bucket size for the rest).
const SPAN = { '6h': 21600, '24h': 86400, '7d': 604800, '30d': 2592000 };
const STEP = { '6h': 60, '24h': 300, '7d': 1800, '30d': 7200 };

// A wider spacing than this means nothing was recorded in between (the
// server was down), so the line breaks there instead of bridging the hole.
const GAP_STEPS = 2.5;

/**
 * A time series over the selected range, with a hover crosshair and readout.
 * Returns a controller so the page can push new data and force redraws.
 *
 * opts.series   [{ key, label, color }]: one line per entry, read from each
 *               point's `key`; a missing value breaks the line like a gap
 * opts.axis     value -> left-gutter label
 * opts.readout  value -> tooltip text
 * opts.minPeak  smallest top of the auto-scaled axis, so a quiet series is
 *               not magnified into noise
 * opts.max      the axis never goes above this (100 for percentages)
 */
export function createTrendChart(canvas, tip, opts) {
    const { series, axis, readout, minPeak = 0, max = Infinity } = opts;
    let points = [];
    let range = '6h';
    let hoverIndex = -1;
    let peak = 1;

    const valueOf = (p, s) => (typeof p[s.key] === 'number' ? p[s.key] : null);

    function geometry() {
        const width = canvas.clientWidth;
        const height = canvas.clientHeight;
        // Points sit at their timestamps across the whole range. Spacing them
        // evenly by index squeezed hours of downtime out of the picture. The
        // window ends at the newest point, so it uses the server's clock
        // rather than comparing it with this browser's.
        const end = points.length ? points[points.length - 1].time : 0;
        const start = end - SPAN[range];
        const plotW = width - PAD.left - PAD.right;
        return {
            width,
            height,
            plotW,
            plotH: height - PAD.top - PAD.bottom,
            start,
            xAt: (time) => PAD.left + ((time - start) / SPAN[range]) * plotW,
            timeAt: (x) => start + ((x - PAD.left) / plotW) * SPAN[range],
        };
    }

    /** Split one series into runs with no gap or missing value inside them. */
    function runs(s) {
        const limit = STEP[range] * GAP_STEPS;
        const out = [];
        let run = [];
        points.forEach((p, i) => {
            const broken = valueOf(p, s) === null || (i > 0 && p.time - points[i - 1].time > limit);
            if (broken && run.length) {
                out.push(run);
                run = [];
            }
            if (valueOf(p, s) !== null) run.push(p);
        });
        if (run.length) out.push(run);
        return out;
    }

    function draw() {
        const box = prepare(canvas);
        if (!box) return;
        const { ctx, width, height } = box;
        const g = geometry();

        const values = points.flatMap((p) => series.map((s) => valueOf(p, s))).filter((v) => v !== null);
        if (points.length < 2 || !values.length) {
            ctx.fillStyle = cssVar('--fg-faint');
            ctx.font = `500 12px ${cssVar('--font-ui') || 'Inter, sans-serif'}`;
            ctx.textAlign = 'center';
            ctx.textBaseline = 'middle';
            ctx.fillText('Accumulating telemetry…', width / 2, height / 2);
            return;
        }

        peak = Math.min(max, Math.max(minPeak, ...values) * 1.12) || 1;
        const { xAt } = g;
        const yAt = (v) => PAD.top + g.plotH - (v / peak) * g.plotH;
        const baseline = PAD.top + g.plotH;

        // Horizontal grid with value labels on the left gutter.
        ctx.strokeStyle = cssVar('--border');
        ctx.fillStyle = cssVar('--fg-faint');
        ctx.font = '500 10px Inter, sans-serif';
        ctx.textAlign = 'right';
        ctx.textBaseline = 'middle';
        ctx.lineWidth = 1;

        for (let i = 0; i <= 3; i++) {
            const value = (peak / 3) * i;
            const y = Math.round(yAt(value)) + 0.5;
            ctx.beginPath();
            ctx.moveTo(PAD.left, y);
            ctx.lineTo(width - PAD.right, y);
            ctx.stroke();
            ctx.fillText(axis(value), PAD.left - 8, y);
        }

        for (const s of series) {
            const color = cssVar(s.color);
            for (const run of runs(s)) {
                if (run.length === 1) {
                    // A lone sample between two gaps would otherwise be invisible.
                    ctx.beginPath();
                    ctx.arc(xAt(run[0].time), yAt(valueOf(run[0], s)), 2, 0, Math.PI * 2);
                    ctx.fillStyle = color;
                    ctx.fill();
                    continue;
                }

                // Area fill for a lone series only; overlapping fills of two
                // series muddy each other.
                if (series.length === 1) {
                    const gradient = ctx.createLinearGradient(0, PAD.top, 0, baseline);
                    gradient.addColorStop(0, withAlpha(color, 0.2));
                    gradient.addColorStop(1, withAlpha(color, 0));
                    ctx.beginPath();
                    ctx.moveTo(xAt(run[0].time), baseline);
                    run.forEach((p) => ctx.lineTo(xAt(p.time), yAt(valueOf(p, s))));
                    ctx.lineTo(xAt(run[run.length - 1].time), baseline);
                    ctx.closePath();
                    ctx.fillStyle = gradient;
                    ctx.fill();
                }

                ctx.beginPath();
                run.forEach((p, i) => {
                    if (i === 0) ctx.moveTo(xAt(p.time), yAt(valueOf(p, s)));
                    else ctx.lineTo(xAt(p.time), yAt(valueOf(p, s)));
                });
                ctx.strokeStyle = color;
                ctx.lineWidth = 1.75;
                ctx.lineJoin = 'round';
                ctx.lineCap = 'round';
                ctx.stroke();
            }
        }

        // Time axis: four labels evenly spaced in time across the window.
        ctx.fillStyle = cssVar('--fg-faint');
        ctx.font = '500 10px Inter, sans-serif';
        ctx.textBaseline = 'top';
        const ticks = [0, 1 / 3, 2 / 3, 1];
        ticks.forEach((f, n) => {
            const time = g.start + f * SPAN[range];
            ctx.textAlign = n === 0 ? 'left' : n === ticks.length - 1 ? 'right' : 'center';
            ctx.fillText(timeLabel(time, range), xAt(time), height - PAD.bottom + 8);
        });

        // Hover crosshair, with a ringed dot on every series that has a value.
        if (hoverIndex >= 0 && hoverIndex < points.length) {
            const p = points[hoverIndex];
            const x = xAt(p.time);
            ctx.strokeStyle = cssVar('--border-strong');
            ctx.lineWidth = 1;
            ctx.beginPath();
            ctx.moveTo(Math.round(x) + 0.5, PAD.top);
            ctx.lineTo(Math.round(x) + 0.5, baseline);
            ctx.stroke();

            for (const s of series) {
                const v = valueOf(p, s);
                if (v === null) continue;
                ctx.beginPath();
                ctx.arc(x, yAt(v), 3.5, 0, Math.PI * 2);
                ctx.fillStyle = cssVar(s.color);
                ctx.fill();
                ctx.strokeStyle = cssVar('--surface-1');
                ctx.lineWidth = 2;
                ctx.stroke();
            }
        }
    }

    /** Index of the point nearest in time to the pointer; points are sorted. */
    function nearest(time) {
        let lo = 0;
        let hi = points.length - 1;
        while (lo < hi) {
            const mid = (lo + hi) >> 1;
            if (points[mid].time < time) lo = mid + 1;
            else hi = mid;
        }
        if (lo > 0 && time - points[lo - 1].time < points[lo].time - time) lo -= 1;
        return lo;
    }

    function onMove(event) {
        if (points.length < 2) return;
        const rect = canvas.getBoundingClientRect();
        const g = geometry();
        const x = Math.max(PAD.left, Math.min(PAD.left + g.plotW, event.clientX - rect.left));
        const index = nearest(g.timeAt(x));

        if (index !== hoverIndex) {
            hoverIndex = index;
            draw();
        }

        const p = points[index];
        const present = series.filter((s) => valueOf(p, s) !== null);
        // One series needs no key; with several, a swatch names each value.
        const rows = series.length === 1
            ? present.map((s) => `<b>${readout(valueOf(p, s))}</b>`)
            : present.map((s) => `<i style="background:${cssVar(s.color)}"></i>${s.label} <b>${readout(valueOf(p, s))}</b>`);
        tip.innerHTML = `${rows.join('<br>') || '<b>No data</b>'}<br><span>${fullLabel(p.time, range)}</span>`;

        const top = Math.max(0, ...present.map((s) => valueOf(p, s)));
        tip.style.left = `${g.xAt(p.time)}px`;
        tip.style.top = `${PAD.top + g.plotH - (top / peak) * g.plotH - 10}px`;
        tip.classList.add('is-visible');
    }

    function onLeave() {
        hoverIndex = -1;
        tip.classList.remove('is-visible');
        draw();
    }

    canvas.addEventListener('mousemove', onMove);
    canvas.addEventListener('mouseleave', onLeave);
    window.addEventListener('resize', draw);

    return {
        setData(nextPoints, nextRange) {
            if (nextRange) range = nextRange;
            // The 6h buffer can run a few minutes past the window; drop those
            // so neither the line nor the hover reaches into the axis gutter.
            const all = nextPoints || [];
            const end = all.length ? all[all.length - 1].time : 0;
            points = all.filter((p) => p.time >= end - SPAN[range]);
            draw();
        },
        redraw: draw,
    };
}
