# 🚀 Production Deployment & Hardening Guide

This guide covers complete deployment instructions for running the **HomeLab Dashboard** in production on bare-metal servers, microservers (e.g. PC Engines APU2, Intel NUC), virtual machines, or Raspberry Pis.

---

## 1. System Requirements & Architecture

- **Dashboard Server**:
  - Linux (Ubuntu 20.04+, Debian 11+, Arch Linux, Alpine) or Windows Server.
  - Python 3.9 or higher.
  - 100 MB RAM, 100 MB disk space.
- **Client Agents**:
  - Any machine running Linux, Windows 10/11, or macOS.
  - Python 3.8 or higher.
  - `psutil` dependency.

---

## 2. Server Installation (FastAPI Dashboard)

### Scripted install (recommended)
`scripts/install-server.sh` does everything in this section: it builds the
virtualenv, generates the systemd unit with the real absolute paths of your
checkout, starts the service, and prints the agent key the nodes will need.

```bash
sudo mkdir -p /opt/homelab-dashboard
sudo chown -R $USER:$USER /opt/homelab-dashboard
git clone https://github.com/yourusername/homelab-dashboard.git /opt/homelab-dashboard
sudo /opt/homelab-dashboard/scripts/install-server.sh
```

Options: `--user <name>` to pick the service account, `--port <port>` to change
the listen port. Re-running it is safe and picks up code changes after a pull.

Clone the whole repository, not just `backend/`: `main.py` serves the UI from
`../frontend`, so the two directories must stay siblings.

The manual steps below are the same thing by hand.

### Step 1: Clone Repository & Create Virtual Environment
```bash
sudo mkdir -p /opt/homelab-dashboard
sudo chown -R $USER:$USER /opt/homelab-dashboard
cd /opt/homelab-dashboard

git clone https://github.com/yourusername/homelab-dashboard.git .
cd backend

python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

### Step 2: Configure Systemd Service
Create `/etc/systemd/system/homelab-dashboard.service`:

```ini
[Unit]
Description=HomeLab Dashboard Central Server
After=network.target

[Service]
Type=simple
User=www-data
Group=www-data
WorkingDirectory=/opt/homelab-dashboard/backend
ExecStart=/opt/homelab-dashboard/backend/.venv/bin/uvicorn main:app --host 127.0.0.1 --port 8000 --timeout-graceful-shutdown 1
Restart=always
RestartSec=5

# Security Sandbox Options
ProtectSystem=full
ProtectHome=true
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
```

Ensure permissions are assigned:
```bash
sudo chown -R www-data:www-data /opt/homelab-dashboard
sudo systemctl daemon-reload
sudo systemctl enable --now homelab-dashboard.service
```

Check service status:
```bash
sudo systemctl status homelab-dashboard.service
```

---

## 3. Reverse Proxy Configuration (Nginx / Caddy)

Because the dashboard uses **Server-Sent Events (SSE)**, reverse proxies must be explicitly configured to **disable proxy buffering**. If buffering is enabled, SSE chunks will be delayed or stalled until a buffer fills.

### 3.1 Nginx Configuration
Create `/etc/nginx/sites-available/homelab-dashboard`:

```nginx
server {
    listen 80;
    server_name dashboard.homelab.local;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl http2;
    server_name dashboard.homelab.local;

    ssl_certificate     /etc/ssl/certs/dashboard.crt;
    ssl_certificate_key /etc/ssl/private/dashboard.key;

    # SSL Security Parameters
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers HIGH:!aNULL:!MD5;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # Critical SSE Stream Buffering Directives
        proxy_http_version 1.1;
        proxy_set_header Connection '';
        proxy_buffering off;
        proxy_cache off;
        chunked_transfer_encoding off;
        proxy_read_timeout 86400s;
        proxy_send_timeout 86400s;
    }
}
```

Enable the configuration and reload Nginx:
```bash
sudo ln -s /etc/nginx/sites-available/homelab-dashboard /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx
```

---

### 3.2 Caddy Configuration
Caddy handles SSE natively without buffering adjustments:

```caddyfile
dashboard.homelab.local {
    reverse_proxy 127.0.0.1:8000
}
```

---

## 4. Agent Installation Across Monitored Nodes

Install an agent on **every** machine you want on the dashboard, including the
server box itself — the server does not collect its own metrics.

### Scripted install (recommended)
```bash
git clone https://github.com/yourusername/homelab-dashboard.git /opt/homelab-agent
sudo /opt/homelab-agent/scripts/install-agent.sh --server http://<server-ip>:8000 --key <agent-key>
```

On Windows nodes, from an elevated PowerShell prompt:
```powershell
git clone https://github.com/yourusername/homelab-dashboard.git C:\Tools\homelab-agent
C:\Tools\homelab-agent\scripts\install-agent.ps1 -Server http://<server-ip>:8000 -Key <agent-key>
```

Both write the configuration outside `agent.py` (`/etc/homelab-agent.env` on
Linux, a generated `run-agent.cmd` on Windows), so the checkout stays clean and
`git pull` + re-running the script is enough to update a node.

The manual steps below are the same thing by hand.

### 4.1 Linux Nodes (Ubuntu / Debian / Arch)
1. Copy the `agent/` folder to `/opt/homelab-agent`:
   ```bash
   sudo mkdir -p /opt/homelab-agent
   sudo cp -r agent/* /opt/homelab-agent/
   cd /opt/homelab-agent
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```
2. Write the config to `/etc/homelab-agent.env` (the agent reads it from the
   environment; do not edit `agent.py`):
   ```bash
   sudo tee /etc/homelab-agent.env >/dev/null <<'ENV'
   HOMELAB_SERVER_URL=https://dashboard.homelab.local/api/report
   HOMELAB_AGENT_KEY=<YOUR_SERVER_AGENT_KEY>
   HOMELAB_INTERVAL=5
   # yes / no / auto: VMs are left out of the power and cost totals
   HOMELAB_IS_VM=auto
   ENV
   sudo chmod 600 /etc/homelab-agent.env
   ```
3. Set up the systemd unit `/etc/systemd/system/homelab-agent.service`:
   ```ini
   [Unit]
   Description=HomeLab Dashboard Agent
   After=network.target

   [Service]
   Type=simple
   User=root
   WorkingDirectory=/opt/homelab-agent
   EnvironmentFile=/etc/homelab-agent.env
   ExecStart=/opt/homelab-agent/.venv/bin/python agent.py
   Restart=always
   RestartSec=5

   [Install]
   WantedBy=multi-user.target
   ```
4. Enable and start the agent:
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable --now homelab-agent.service
   ```

---

### 4.2 Windows Nodes (Gaming Rig / Workstation)
1. Install Python 3.10+ on Windows.
2. Clone or copy `agent/` to `C:\Tools\homelab-agent`.
3. Open PowerShell:
   ```powershell
   cd C:\Tools\homelab-agent
   python -m venv .venv
   .\.venv\Scripts\pip.exe install -r requirements.txt
   ```
4. Set `HOMELAB_SERVER_URL` and `HOMELAB_AGENT_KEY` in the environment the task
   runs under (the PowerShell installer generates a `run-agent.cmd` that does
   this). Do not edit `agent.py`.
5. Add to **Windows Task Scheduler**:
   - Action: `Start a Program`
   - Program/script: `C:\Tools\homelab-agent\.venv\Scripts\pythonw.exe` (executes silently without console).
   - Add arguments: `agent.py`
   - Start in: `C:\Tools\homelab-agent`
   - Trigger: `At log on` of user.

---

## 5. Security & Maintenance

### 5.1 Database Permissions & Backup
The SQLite database stores user password hashes, active sessions, and power history in `backend/history.db`.
- Restrict file permissions:
  ```bash
  chmod 600 /opt/homelab-dashboard/backend/history.db
  ```
- Backup SQLite safely online using `.backup`:
  ```bash
  sqlite3 /opt/homelab-dashboard/backend/history.db ".backup '/backup/history-$(date +%F).db'"
  ```

### 5.2 Firewall Rules (`ufw`)
Only port 80/443 should be exposed externally. Port 8000 remains bound to `127.0.0.1`:
```bash
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow ssh
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw enable
```
