/* The "Dashboard backup" panel on the Alerts page (server: backend/backups.py,
   the node that keeps the copies: agent/backup.py). */

import { api, getJson } from './api.js';
import { toast } from './toast.js';
import { esc, ago } from './format.js';
import { copy } from './clipboard.js';

const $ = (id) => document.getElementById(id);
let backup = null;

const command = (key) => 'sudo ./scripts/install-agent.sh --backup-dir /var/backups/homelab-dashboard '
    + `--backup-key ${key}`;

function render() {
    if (!backup) return;
    const { last } = backup;
    const line = $('backup-status');
    line.dataset.tone = !last ? '' : last.ok ? 'ok' : 'crit';
    line.textContent = !last ? 'Not set up yet: no node has saved a backup.'
        : `${last.ok ? 'Last backup' : 'Last backup failed'} ${ago(Date.now() / 1000 - last.at)} on ${last.host}: ${last.detail}`;
    $('backup-command').textContent = command(backup.key);
    // Open until the first backup arrives; after that it is reference.
    if (!last) $('backup-setup').open = true;
}

export async function loadBackup() {
    try {
        backup = await getJson('/api/backup');
        render();
    } catch (err) {
        console.error('Backup status request failed:', err);
    }
}

async function download() {
    const button = $('backup-download');
    button.disabled = true;
    button.textContent = 'Making a snapshot…';
    try {
        const response = await api('/api/backup/snapshot');
        if (!response.ok) {
            const data = await response.json().catch(() => ({}));
            throw new Error(data.detail || `the server answered ${response.status}`);
        }
        const name = /filename="?([^";]+)"?/.exec(response.headers.get('Content-Disposition') || '')?.[1]
            || 'homelab-dashboard.db.gz';
        const link = Object.assign(document.createElement('a'), {
            href: URL.createObjectURL(await response.blob()), download: name,
        });
        link.click();
        setTimeout(() => URL.revokeObjectURL(link.href), 10000);
    } catch (err) {
        toast(`Could not download a backup: ${err.message}`, { tone: 'crit' });
    } finally {
        button.disabled = false;
        button.textContent = 'Download a backup now';
    }
}

async function rotate() {
    if (!window.confirm('Make a new backup key? The node keeping backups needs the new one before its next run.')) return;
    try {
        const response = await api('/api/backup/key', { method: 'POST' });
        if (!response.ok) throw new Error(`the server answered ${response.status}`);
        backup = await response.json();
        render();
        toast('New backup key made. Run the command again on the backup node.', { tone: 'ok' });
    } catch (err) {
        toast(`Could not make a new key: ${err.message}`, { tone: 'crit' });
    }
}

export function startBackupPanel() {
    $('backup-download').addEventListener('click', download);
    $('backup-rotate').addEventListener('click', rotate);
    $('backup-copy').addEventListener('click', async () => {
        toast(await copy(command(backup.key)) ? 'Command copied' : 'Could not copy; select the command instead',
            { tone: 'ok' });
    });
    loadBackup();
    setInterval(render, 60000);   // keeps "6h ago" current
}
