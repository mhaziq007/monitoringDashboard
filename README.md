# 🖥️ HomeLab Dashboard & Monitoring Fleet

A lightweight, real-time homelab monitoring suite designed specifically for resource-constrained microservers, low-power appliances (such as PC Engines APU2, Intel NUCs, Raspberry Pis), NAS systems, and workstations.

It combines an outbound **push-based telemetry model** with real-time **Server-Sent Events (SSE)**, eliminating complex network scraping and firewall configuration while delivering sub-second updates to a zero-build vanilla web UI.

---

## 📚 Documentation Index

For exhaustive technical guides, please refer to the dedicated documentation suite:

| Document | Description |
| :--- | :--- |
| **[Architecture & Design](docs/ARCHITECTURE.md)** | Telemetry data flow, SQLite persistence, downsampling strategy, and background worker concurrency. |
| **[API Reference](docs/API_REFERENCE.md)** | Full specification of all REST endpoints, authentication headers, JSON payloads, and SSE stream protocols. |
| **[Agent Guide & Telemetry](docs/AGENT_GUIDE.md)** | Multi-platform metric collection, Intel/AMD RAPL, WMI, NVIDIA GPU queries, update detection, and TDP estimation models. |
| **[Services Monitoring](docs/SERVICES_MONITORING.md)** | Probing self-hosted web applications, `services.json` schema, categories, and backend network isolation. |
| **[Frontend System & UI](docs/FRONTEND_SYSTEM.md)** | Design tokens, HTML5 Canvas charts & sparklines, Web Audio API sci-fi alarm chime, and hysteresis alerting. |
| **[Production Deployment](docs/DEPLOYMENT_GUIDE.md)** | Production setup on Linux/Windows, systemd services, Nginx reverse proxy with SSE buffering, and security hardening. |
| **[Troubleshooting & Diagnostics](docs/TROUBLESHOOTING.md)** | Step-by-step diagnostic guide for connection issues, authentication resets, sensor permissions, and audio policies. |

---

## ✨ Key Features

- **🌐 Push-Based Telemetry**: Client nodes push telemetry outbound every 5 seconds. Seamlessly operates through NATs, firewalls, and VPN tunnels without requiring open inbound ports on monitored hosts.
- **⚡ Real-Time SSE Streaming**: Live updates stream directly into your browser over an efficient persistent HTTP Server-Sent Events connection.
- **🎨 Zero-Build Modern UI**: Built with vanilla ES6 JavaScript modules, HTML5 Canvas, and a token-driven CSS architecture. Zero `npm build`, Webpack, or framework bloat required.
- **🔒 Multi-Layer Security**:
  - Administrative access secured with **PBKDF2-HMAC-SHA256** password hashing and 14-day bearer session tokens.
  - First-run setup overlay automatically initializes the master admin account and locks subsequent registration.
  - Ingestion secured via a unique, cryptographically generated `X-Agent-Key` header.
- **📊 30-Day SQLite History & Downsampling**: Lab-wide power consumption is logged every minute with automatic 30-day retention pruning. Features fast mathematical integer-bucketing downsampling (`6H`, `24H`, `7D`, `30D`).
- **🖥️ Deep Hardware Instrumentation**:
  - **CPU & RAM**: Marketing model retrieval, core count breakdown, and live utilization bars with real-time Canvas sparklines.
  - **Power Draw (Watts)**: Real-time CPU and GPU wattage tracking (via Linux RAPL / hwmon, Windows Performance Counters, or TDP load interpolation) with estimated monthly running electricity costs.
  - **Storage & I/O**: Multi-disk partition scanning with individual utilization meters and live read/write throughput speeds ($\text{MB/s}$).
  - **Network & Bandwidth**: Live download/upload transfer rates.
  - **NVIDIA GPU Monitoring**: Auto-detects NVIDIA cards with utilization, VRAM usage, temperature, and wattage.
  - **VPN & Docker**: Live state detection for Tailscale, OpenVPN, and running Docker containers.
- **🔄 Asynchronous OS Update Detection**: Runs in a non-blocking background thread once every 4 hours to check for pending packages on Linux (Ubuntu/Debian) and Windows without stalling telemetry.
- **🔔 Alarm Chimes & Hysteresis**:
  - Synthesizes sci-fi alarm tones using the browser's native **Web Audio API** (zero external audio assets).
  - Dual-threshold **hysteresis** (e.g. triggers at 85%, clears at 75%) to eliminate notification flapping.
- **📌 Customization & Device Pinning**: Pin your most critical servers to the top of the grid with zero-overhead `localStorage` persistence.

---

## 📐 System Architecture

```mermaid
sequenceDiagram
    participant Agent as Target Machine (agent.py)
    participant Backend as Dashboard Server (FastAPI)
    participant Browser as Web Browser Dashboard

    Browser->>Backend: Connect to SSE (GET /api/stream?token=...)
    Backend-->>Browser: Stream initial state (init, services_init)

    loop Every 5 seconds
        Agent->>Agent: Query CPU, RAM, Disks, GPU, Speeds, Power
        Agent->>Backend: Push telemetry (POST /api/report) [Header: X-Agent-Key]
        Backend->>Backend: Update in-memory state & enrich latency
        Backend-->>Browser: Stream live update (metrics)
        Browser->>Browser: Update DOM / Canvas sparklines / Alert checks
    end

    loop Every 30 seconds
        Backend->>Backend: Probe self-hosted HTTP services
        Backend-->>Browser: Stream updated reachability (services)
    end
```

---

## 📂 Project Structure

```text
├── agent/
│   ├── agent.py                  # Portable metric collection script
│   ├── homelab-agent.service     # Systemd service unit template for Linux
│   └── requirements.txt          # Agent dependencies (psutil)
├── backend/
│   ├── main.py                   # FastAPI server, background workers & static file server
│   ├── history.db                # SQLite database (auto-created on startup)
│   ├── services.json             # Monitored self-hosted web applications
│   ├── homelab-dashboard.service # Systemd service unit template for central server
│   └── requirements.txt          # Server dependencies (fastapi, uvicorn)
├── frontend/
│   ├── index.html                # Devices and hardware monitoring view
│   ├── services.html             # Self-hosted services health view
│   ├── css/
│   │   ├── tokens.css            # Central design tokens: colours, typography, motion
│   │   ├── base.css              # Reset, typography, utilities
│   │   ├── layout.css            # Page layout, topbar, fleet summary, grids
│   │   └── components.css        # Cards, meters, drawer, toasts, auth modal
│   └── js/
│       ├── config.js             # Thresholds, electricity tariff, timeouts
│       ├── format.js             # Value formatting, XSS escaping, tone helpers
│       ├── api.js                # Token management & authenticated fetch
│       ├── auth.js               # Registration & sign-in overlay controller
│       ├── shell.js              # Shared topbar header & link status indicator
│       ├── stream.js             # Resilient SSE client with exponential backoff
│       ├── charts.js             # Canvas sparklines & interactive power trend chart
│       ├── toast.js              # Toast notifications & Web Audio alarm synthesizer
│       ├── devices.js            # Devices page controller & alert hysteresis
│       └── services-page.js      # Services page controller & latency calculator
├── docs/
│   ├── ARCHITECTURE.md           # Deep architectural specification
│   ├── API_REFERENCE.md          # REST API & SSE protocol specification
│   ├── AGENT_GUIDE.md            # Hardware sensors & agent deployment guide
│   ├── SERVICES_MONITORING.md    # Services probing subsystem documentation
│   ├── FRONTEND_SYSTEM.md        # Design system & frontend mechanics
│   ├── DEPLOYMENT_GUIDE.md       # Production deployment, Nginx & systemd guide
│   └── TROUBLESHOOTING.md        # Comprehensive diagnostic resolutions
└── README.md                     # Project overview and index
```

---

## 🚀 Quick Start

### 1. Start the Central Dashboard Server

On your central Linux or Windows server:

```bash
# 1. Navigate to backend directory
cd backend

# 2. Create and activate a Python virtual environment
python3 -m venv .venv
source .venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Start the server
uvicorn main:app --host 0.0.0.0 --port 8000
```

Open your browser at `http://<SERVER-IP>:8000`. On the first load, create your administrator account. Once logged in, click the **Key** badge in the top bar to copy your generated **Agent Key**.

---

### 2. Deploy Client Agents

On each server, NAS, or workstation you want to monitor:

```bash
# 1. Navigate to agent directory
cd agent

# 2. Create virtual environment & install dependencies
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Edit `agent.py` to set your dashboard server IP and master Agent Key:
```python
SERVER_URL = "http://<SERVER-IP>:8000/api/report"
AGENT_KEY = "<YOUR_COPIED_AGENT_KEY>"
```

Run the agent:
```bash
python agent.py
```
Within 5 seconds, the node will appear dynamically on your dashboard grid.

### 3. Updating

After changes are merged, run this on each node (the dashboard server and every agent):
```bash
sudo ./scripts/update.sh
```
It pulls, updates Python packages, and restarts whichever of `homelab-dashboard` and `homelab-agent` runs there, even when there was nothing new to pull, so a service never keeps running old code after an earlier `git pull`. `--branch <name>` tries a branch before it is merged. If the server is running older code than its checkout, the dashboard shows a banner saying so.

---

## 🔔 Alerts & Notifications

The dashboard server checks every node and service itself, so alerts fire with no browser open, and sends them to your phone through [ntfy](https://ntfy.sh), Telegram, or both. Set them up on the **Alerts** page; each channel has its own **Send a test** button.

- **ntfy**: tick *Send alerts to ntfy*, enter your topic URL (e.g. `http://192.168.0.105:7070/homelab`) and, if the topic needs one, an access token. Subscribe to the same topic in the ntfy app.
- **Telegram**: message [@BotFather](https://t.me/BotFather), send `/newbot` and copy the token it gives you. Paste it into *Bot token*, send your new bot any message, then click **Find my chat** and pick your chat. Tick *Send alerts to Telegram* and save. Resolutions and the daily summary arrive silently; new alerts notify.

| Alert | Fires when | Resolves when |
|---|---|---|
| Node offline | No report for 3 minutes | The node reports again |
| High CPU | Above 85% for 5 minutes | Below 75% |
| High RAM | Above 90% for 5 minutes (ZFS cache counts as free) | Below 80% |
| CPU / GPU temperature | Above 80°C for 5 minutes | Below 72°C |
| Disk | Over its threshold (90% unless set per disk) | 5 points below it, or its alert is switched off |
| Service down | Two failed probes in a row (about a minute) | It answers again |
| Updates (daily) | Pending OS packages or container images, once a day at the chosen hour | — |

Every alert and its resolution goes to each channel switched on, and into the history on the Alerts page. If one channel fails, the others still deliver, and the Alerts page shows which one failed and why. The settings, including the tokens, are stored on the dashboard server; tokens are never sent back to a browser.

---

## 🛠️ Configuration & Tunables

- **Alert Thresholds & Electricity Tariff**: Edit `frontend/js/config.js` to customize CPU/GPU trip points and recovery points, and `TARIFF` for your time-of-use electricity prices (peak and off-peak $/kWh as printed on the bill, peak hours and months, and GST). The running cost uses this month's peak/off-peak blend.
- **Virtual Machines**: VMs are detected automatically and left out of the lab power and running cost, since their power is either already in the host's reading or (for cloud instances) not on your bill. Override with `HOMELAB_IS_VM=yes|no` in the agent environment, or `--vm yes|no` / `-Vm yes|no` on the install scripts.
- **Arranging Cards**: Drag a node card by its grip handle to rearrange the board (or focus the grip and use the arrow keys). The order is remembered per browser; new nodes join at the end. Cards pack into the space beside taller ones instead of leaving gaps.
- **Card Sizes**: The size button on each node card cycles **normal → large → compact**. A large card spans two columns and shows the node's history charts, disks, network, GPU and containers inline; a compact one keeps only its meters. Sizes are remembered per browser.
- **Disk Alerts**: Each disk alerts at 90% by default. In a node's Storage section (drawer or large card), the **Alert at** selector sets that disk to 80%, 95%, 98% or Off, e.g. for a media drive that is meant to fill up. The setting is stored on the server and applies in every browser, and each disk's meter colours against its own threshold.
- **Monitored Services**: Edit `backend/services.json` to add or remove self-hosted web applications.
- **Hostname Icons**: Extend the heuristic table in `frontend/js/format.js` (`deviceIcon()`) to assign custom icons based on hostname regex patterns.
- **Theme & Colors**: The topbar button cycles **Auto → Light → Dark** (Auto follows your system; the choice is remembered per browser). Both themes are CSS custom properties in `frontend/css/tokens.css`: the dark values on `:root`, the light ones under `:root[data-theme='light']`.

---

## 🧪 Development

Run the tests from the repository root:
```bash
pip install -r backend/requirements.txt -r agent/requirements.txt -r requirements-dev.txt
pytest
```
They cover the server's API (reports, devices, history, disk alert settings, service uptime) and the agent's Docker update checks and VM detection, without touching the network. GitHub Actions runs them on every pull request and push to `main`, along with a parse check of the frontend modules and shell scripts.

---

## 📝 License

Distributed under the **MIT License**. See LICENSE file for details.
