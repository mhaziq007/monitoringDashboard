/* Value formatting and the small tone helpers that map a number to a colour. */

import {
    LOAD_WARN, LOAD_CRIT,
    TEMP_WARN, TEMP_CRIT,
    LATENCY_WARN, LATENCY_CRIT,
} from './config.js';

/**
 * Escape text before it goes into innerHTML. Hostnames, container names, OS
 * strings and CPU models all arrive from agents, so none of them are trusted.
 */
export function esc(value) {
    if (value === undefined || value === null) return '';
    return String(value)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

/** Stable DOM-id fragment for an arbitrary name. */
export function slug(name) {
    return String(name).replace(/[^a-zA-Z0-9]/g, '-');
}

export function bytes(value, decimals = 1) {
    if (!value) return '0 B';
    const units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'];
    const i = Math.min(Math.floor(Math.log(value) / Math.log(1024)), units.length - 1);
    return `${parseFloat((value / 1024 ** i).toFixed(decimals))} ${units[i]}`;
}

/** Compact byte size for tight spaces: "1.2T", "480G". */
export function bytesShort(value) {
    if (!value) return '0';
    const units = ['B', 'K', 'M', 'G', 'T', 'P'];
    const i = Math.min(Math.floor(Math.log(value) / Math.log(1024)), units.length - 1);
    const n = value / 1024 ** i;
    return `${n >= 100 ? n.toFixed(0) : n.toFixed(1)}${units[i]}`;
}

export function speed(bytesPerSec) {
    if (!bytesPerSec || bytesPerSec < 0) return '0 B/s';
    if (bytesPerSec >= 1024 * 1024) return `${(bytesPerSec / 1048576).toFixed(1)} MB/s`;
    if (bytesPerSec >= 1024) return `${(bytesPerSec / 1024).toFixed(0)} KB/s`;
    return `${bytesPerSec.toFixed(0)} B/s`;
}

/** Very compact speed for the 4-up tile row: "2.1M", "480K". */
export function speedShort(bytesPerSec) {
    if (!bytesPerSec || bytesPerSec < 0) return '0';
    if (bytesPerSec >= 1048576) return `${(bytesPerSec / 1048576).toFixed(1)}M`;
    if (bytesPerSec >= 1024) return `${(bytesPerSec / 1024).toFixed(0)}K`;
    return `${bytesPerSec.toFixed(0)}B`;
}

export function uptime(seconds) {
    if (!seconds) return '—';
    const d = Math.floor(seconds / 86400);
    const h = Math.floor((seconds % 86400) / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    if (d > 0) return `${d}d ${h}h`;
    if (h > 0) return `${h}h ${m}m`;
    return `${m}m`;
}

/** Uptime for the narrow card tile: never more than six characters. */
export function uptimeShort(seconds) {
    if (!seconds) return '—';
    const d = Math.floor(seconds / 86400);
    const h = Math.floor((seconds % 86400) / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    if (d >= 10) return `${d}d`;
    if (d > 0) return `${d}d ${h}h`;
    if (h >= 10) return `${h}h`;
    if (h > 0) return `${h}h ${m}m`;
    return `${m}m`;
}

/** "12s ago", "5m ago", "3h ago", "2d ago". */
export function ago(seconds) {
    const s = Math.max(0, Math.floor(seconds));
    if (s < 60) return `${s}s ago`;
    if (s < 3600) return `${Math.floor(s / 60)}m ago`;
    if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
    return `${Math.floor(s / 86400)}d ago`;
}

export function watts(value) {
    if (!value) return '—';
    return `${value.toFixed(0)} W`;
}

export function money(value) {
    return `$${value.toFixed(2)}`;
}

export function percent(value) {
    if (value === undefined || value === null) return '—';
    return `${value.toFixed(0)}%`;
}

/* --- Tone helpers: a number in, a status keyword out --------------------- */

export function loadTone(pct) {
    if (pct >= LOAD_CRIT) return 'crit';
    if (pct >= LOAD_WARN) return 'warn';
    return '';
}

export function tempTone(celsius) {
    if (celsius === undefined || celsius === null) return '';
    if (celsius >= TEMP_CRIT) return 'crit';
    if (celsius >= TEMP_WARN) return 'warn';
    return 'ok';
}

export function latencyTone(ms) {
    if (ms === undefined || ms === null) return '';
    if (ms >= LATENCY_CRIT) return 'crit';
    if (ms >= LATENCY_WARN) return 'warn';
    return 'ok';
}

/** Pick an icon from the hostname. Extend the table to add your own. */
export function deviceIcon(hostname) {
    const name = String(hostname).toLowerCase();
    const table = [
        [/gaming|desktop|\bpc\b|rig|workstation/, 'fa-desktop'],
        [/nas|storage|vault|backup/,              'fa-hard-drive'],
        [/router|switch|network|hub|gateway|apu/, 'fa-network-wired'],
        [/pi\b|raspberry|arm/,                    'fa-microchip'],
        [/laptop|book|thinkpad/,                  'fa-laptop'],
    ];
    for (const [pattern, icon] of table) {
        if (pattern.test(name)) return icon;
    }
    return 'fa-server';
}
