# 🤖 Agent Guide & Hardware Telemetry

The HomeLab Dashboard Agent (`agent/agent.py`) is a lightweight, portable Python script designed to run as a low-overhead background daemon on each monitored machine. It extracts deep hardware telemetry, power draw, container states, storage throughput, and pending updates across Linux, Windows, and macOS.

---

## 1. Overview & Architecture

- **Execution Interval**: Every 5 seconds (`INTERVAL = 5`).
- **Communication**: Outbound HTTP `POST` to `/api/report` with `X-Agent-Key`.
- **State Tracker**: Holds previous timestamps and counter readings (`bytes_sent`, `bytes_recv`, `read_bytes`, `write_bytes`, `energy_uj`) to compute live rates of change ($\Delta \text{value} / \Delta t$).
- **Non-blocking Operations**: Subprocess queries specify short timeouts (2–3s) and utilize the Windows `CREATE_NO_WINDOW` flag (`0x08000000`) so commands never steal focus from gaming or desktop workloads. Long-running checks (OS updates) run in an isolated daemon thread every 4 hours.

```mermaid
graph TD
    subgraph "agent.py Main Loop (5s Interval)"
        IP["get_ip"]
        OS["get_os_info"]
        CPU["get_cpu_model + psutil.cpu_percent"]
        MEM["psutil.virtual_memory"]
        DISK["get_disk_status + get_disk_speeds"]
        NET["get_network_speeds"]
        SENS["get_temperature + get_cpu_power"]
        GPU["get_gpu_status (nvidia-smi)"]
        VPN["get_vpn_status"]
        DOCK["get_docker_containers"]
        UPD["query_pending_updates (4h async)"]
    end

    subgraph "Data Compilation & Delivery"
        PAYLOAD["JSON Metrics Payload"]
        SEND["urllib POST /api/report<br/>Header: X-Agent-Key"]
    end

    IP --> PAYLOAD
    OS --> PAYLOAD
    CPU --> PAYLOAD
    MEM --> PAYLOAD
    DISK --> PAYLOAD
    NET --> PAYLOAD
    SENS --> PAYLOAD
    GPU --> PAYLOAD
    VPN --> PAYLOAD
    DOCK --> PAYLOAD
    UPD --> PAYLOAD

    PAYLOAD --> SEND
```

---

## 2. Sensor Instrumentation & Telemetry Details

### 2.1 CPU Marketing Name & OS Identification
Standard Python `platform.processor()` often returns generic identifiers like `x86_64`. The agent resolves exact marketing strings:
- **Windows**: Reads the registry key `HKEY_LOCAL_MACHINE\HARDWARE\DESCRIPTION\System\CentralProcessor\0\ProcessorNameString`.
- **Linux**: Scans `/proc/cpuinfo` for the `model name` line.
- **macOS**: Queries `sysctl -n machdep.cpu.brand_string`.
- **OS String**: Scans `/etc/os-release` (`PRETTY_NAME`) on Linux, and `platform.release()` + `platform.version()` on Windows.

---

### 2.2 Power Draw & Energy Telemetry (Wattage)

The agent implements a multi-tier hardware power detection strategy:

```mermaid
flowchart TD
    Start(["Query CPU Power"]) --> IsWin{"OS is Windows?"}
    
    IsWin -- Yes --> WinPerf["Query Windows Energy Meter Counter<br/>Energy Meter (*_pkg) Power"]
    WinPerf --> WinValid{"Valid reading?"}
    WinValid -- Yes --> ReturnW(["Return Watts"])
    WinValid -- No --> FallbackTDP
    
    IsWin -- No --> LinuxRAPL{"RAPL sysfs file exists?<br/>/sys/class/powercap/.../energy_uj"}
    LinuxRAPL -- Yes --> CalcRAPL["Calculate dE / dt in Watts"]
    CalcRAPL --> RAPLValid{"Power between 0W and 500W?"}
    RAPLValid -- Yes --> ReturnW
    RAPLValid -- No --> LinuxHwmon
    LinuxRAPL -- No --> LinuxHwmon
    
    LinuxHwmon{"Linux hwmon sensor exists?<br/>fam15h_power / zenpower / coretemp"}
    LinuxHwmon -- Yes --> ReadHwmon["Read micro-watts / 1e6"]
    ReadHwmon --> ReturnW
    LinuxHwmon -- No --> FallbackTDP
    
    FallbackTDP{"CPU Model in TDP DB?<br/>GX-412TC / RX-421BD"}
    FallbackTDP -- Yes --> MathModel["Compute idle_w + load * delta_w"]
    MathModel --> ReturnW
    FallbackTDP -- No --> ReturnNull(["Return None"])
```

#### 1. Linux Running Energy RAPL (Running Average Power Limit)
Reads the cumulative micro-joules from `/sys/class/powercap/intel-rapl/intel-rapl:0/energy_uj` across Intel and modern AMD processors:
$$\text{Power (Watts)} = \frac{E_{\text{current}} - E_{\text{previous}}}{(\Delta t) \times 10^6}$$

#### 2. Linux Hardware Monitor (`hwmon`)
Inspects `/sys/class/hwmon/hwmon*/power*_input` with matching driver labels (`fam15h_power`, `zenpower`, `coretemp`, `intel_rapl`).

#### 3. Windows Performance Counters
Executes PowerShell counter query:
```powershell
(Get-Counter -Counter '\Energy Meter(*_pkg)\Power' -ErrorAction SilentlyContinue).CounterSamples.CookedValue
```
Converts milliwatts to Watts.

#### 4. Model-Based TDP Estimation Fallback
For embedded low-power appliances without hardware energy sensors (e.g., AMD embedded SoCs), the agent calculates power consumption using empirical load curves:
- **AMD GX-412TC (PC Engines APU2)**:
  $$P(\text{Watts}) = 3.5 + (6.0 - 3.5) \times \left(\frac{\text{CPU}_\%}{100}\right)$$
- **AMD RX-421BD (Merlin Falcon NAS SoC)**:
  $$P(\text{Watts}) = 6.0 + (15.0 - 6.0) \times \left(\frac{\text{CPU}_\%}{100}\right)$$
- **Generic AMD G-Series**:
  $$P(\text{Watts}) = 4.0 + (15.0 - 4.0) \times \left(\frac{\text{CPU}_\%}{100}\right)$$

---

### 2.3 CPU Temperature
- **Linux**: Queries `psutil.sensors_temperatures()` across `coretemp`, `cpu_thermal`, `acpitz`, with fallback to `/sys/class/thermal/thermal_zone0/temp`.
- **Windows**: Queries WMI namespaces `root/LibreHardwareMonitor` and `root/OpenHardwareMonitor` for sensors matching `SensorType = 'Temperature'` and name matching `*CPU Package*` or `*CPU Core*`.

---

### 2.4 NVIDIA GPU Monitoring
Executes `nvidia-smi` in CSV format without header:
```bash
nvidia-smi --query-gpu=name,utilization.gpu,utilization.memory,memory.total,memory.used,temperature.gpu,power.draw --format=csv,noheader,nounits
```
Extracts:
- Core GPU utilization (%)
- VRAM total vs. used (converted to Bytes)
- Core temperature (°C)
- Live power draw (Watts)

---

### 2.5 Storage Partitions & Disk I/O Throughput
- Scans `psutil.disk_partitions(all=False)`.
- Automatically excludes virtual and loop devices (`/proc`, `/sys`, `/dev`, `/run`, `/boot`, `/dev/loop*`).
- Records total capacity, used, free, and percentage per mount point.
- Measures live I/O speed ($\text{Bytes/sec}$) by calculating $\Delta \text{read\_bytes} / \Delta t$ and $\Delta \text{write\_bytes} / \Delta t$ using `psutil.disk_io_counters()`.

---

### 2.6 Network Throughput & VPN Interfaces
- **Bandwidth**: Computes live download and upload rates ($\text{Bytes/sec}$) via delta of `psutil.net_io_counters()`.
- **VPN State**: Checks `psutil.net_if_addrs()` for active interface prefixes:
  - **Tailscale**: `tailscale*`, `utun*`, `ts0`.
  - **OpenVPN**: `tun*`, `tap*`, `openvpn*`.

---

### 2.7 Docker Container Discovery
Executes `docker ps -a --format "{{.Names}}\t{{.State}}\t{{.Image}}"` to list all containers with their live state (`Running`, `Exited`, `Paused`) and image.

#### Image update checks
Every **6 hours** (`DOCKER_UPDATE_INTERVAL`), in a background thread, the agent checks whether each container's image is out of date. Nothing is pulled, restarted or changed.
1. `docker inspect` gives each container's configured image (`Config.Image`, e.g. `lscr.io/linuxserver/qbittorrent:latest`) and the image it actually runs; `docker image inspect` gives that image's `RepoDigests`.
2. A `HEAD /v2/<repo>/manifests/<tag>` request asks the image's registry which digest the tag points to now. Anonymous pull tokens are fetched from the registry's `WWW-Authenticate` challenge, and redirects are followed (lscr.io hands requests to ghcr.io). Docker Hub does not count these requests against its pull limit.
3. Each container then reports `update`:
   - `available`: the registry has a newer image. `update_note` says whether it is already pulled and the container only needs recreating (`docker compose up -d`).
   - `current`: the container runs what the tag points to.
   - `null`: cannot be told. The image is pinned to a digest, was built locally, or the registry needs a login or could not be reached; `update_note` says which.

The dashboard counts outdated images in each node's updates badge and in the Pending updates tile, and lists them first, marked **Update**, in the node's container list.

---

### 2.8 Pending OS Updates (Background Thread)
To ensure that package checks never block the 5-second real-time metrics push loop, the update check is performed asynchronously once every **4 hours** in a background daemon thread:
- **Ubuntu/Debian**: Reads the cached count from `/var/lib/update-notifier/updates-available` or queries `/usr/lib/update-notifier/apt-check`.
- **Windows**: Queries the Windows Update COM API:
```powershell
(New-Object -ComObject Microsoft.Update.Session).CreateUpdateSearcher().Search("IsInstalled=0 and Type='Software' and IsHidden=0").Updates.Count
```

---

### 2.9 Virtual Machine Detection
Every report carries `is_vm`. The dashboard leaves VMs out of the lab power total, the power history and the running cost: a local VM's draw is already in its host's reading, and a cloud instance (Oracle Cloud, AWS, ...) is not on the lab's electricity bill at all. VMs still report every other metric and get a **VM** chip on their card.

Detection runs once at startup:
- **Linux**: `systemd-detect-virt --vm`. Without systemd, the `hypervisor` CPU flag or a known hypervisor/cloud name in `/sys/class/dmi/id/` (`sys_vendor`, `product_name`, `chassis_asset_tag`).
- **Windows**: `Win32_ComputerSystem` manufacturer and model (e.g. `Microsoft Corporation Virtual Machine`, `QEMU`, `VMware`).
- **macOS**: `sysctl kern.hv_vmm_present`.

Set `HOMELAB_IS_VM=yes` or `no` in the agent environment to override detection (`auto` is the default). The install scripts take the same value as `--vm` (Linux) or `-Vm` (Windows). The agent logs the result at startup as `Node type: ...`.

### 2.10 Dashboard Backups (`backup.py`, opt-in)
One node's agent can keep nightly copies of the dashboard's database. It is off unless both of these are set in the agent environment (`install-agent.sh --backup-dir <dir> --backup-key <key>` writes them):

| Variable | Default | Meaning |
|---|---|---|
| `HOMELAB_BACKUP_DIR` | — | Where the copies go, e.g. `/var/backups/homelab-dashboard` (created owner-only) |
| `HOMELAB_BACKUP_KEY` | — | The backup key from the dashboard's Alerts page |
| `HOMELAB_BACKUP_HOUR` | `3` | Local hour after which the day's copy is made |
| `HOMELAB_BACKUP_KEEP` | `14` | Copies kept; older ones are deleted |

Once a day, after the hour and only if `homelab-dashboard-<today>.db.gz` is not there yet, a background thread downloads `GET /api/backup/snapshot` into a `.part` file, checks the SHA-256 the server sent and that the file unpacks to an SQLite database, and only then moves it into place. It then prunes old copies and reports the outcome to `POST /api/backup/report`. A failed run is retried an hour later.

---

## 3. Installation & Daemon Configuration

### 3.1 Linux / NAS Installation

#### Step 1: Install Python Environment
```bash
cd /opt
sudo git clone https://github.com/yourusername/homelab-dashboard.git
cd homelab-dashboard/agent
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

#### Step 2: Configure Credentials
Edit `agent.py` and set:
```python
SERVER_URL = "http://<YOUR-DASHBOARD-IP>:8000/api/report"
AGENT_KEY = "<YOUR_SERVER_AGENT_KEY>"
```

#### Step 3: Configure Systemd Daemon
Edit `homelab-agent.service`:
```ini
[Unit]
Description=HomeLab Dashboard Agent Service
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/homelab-dashboard/agent
ExecStart=/opt/homelab-dashboard/agent/.venv/bin/python agent.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Enable and start the service:
```bash
sudo cp homelab-agent.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now homelab-agent.service
```

---

### 3.2 Windows Silent Background Installation

To run the agent on Windows silently without popping up console windows or interrupting games:

1. **Install Virtual Environment**:
   ```powershell
   cd C:\Tools\homelab-dashboard\agent
   python -m venv .venv
   .\.venv\Scripts\pip.exe install -r requirements.txt
   ```
2. **Configure `agent.py`** with your `SERVER_URL` and `AGENT_KEY`.
3. **Configure Windows Task Scheduler**:
   - Open **Task Scheduler** $\rightarrow$ **Create Basic Task**.
   - **Name**: `HomeLab Agent`.
   - **Trigger**: `When I log on`.
   - **Action**: `Start a program`.
   - **Program/script**: Path to `pythonw.exe` (e.g., `C:\Tools\homelab-dashboard\agent\.venv\Scripts\pythonw.exe`). *Note: `pythonw` runs with no console window.*
   - **Add arguments**: `agent.py`.
   - **Start in**: `C:\Tools\homelab-dashboard\agent`.
   - In Task Properties, ensure **Run only when user is logged on** is selected, and under Settings, ensure **Do not start a new instance** if already running.
