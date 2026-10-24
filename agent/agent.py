import json
import os
import platform
import re
import socket
import subprocess
import threading
import time
import urllib.parse
import urllib.request
import urllib.error
import psutil

# Configuration. The install script writes these into an environment file
# (/etc/homelab-agent.env on Linux) so this source file stays identical on
# every node and can be updated with a plain git pull.
SERVER_URL = os.environ.get("HOMELAB_SERVER_URL", "http://192.168.1.106:8000/api/report")
AGENT_KEY = os.environ.get("HOMELAB_AGENT_KEY", "")
INTERVAL = int(os.environ.get("HOMELAB_INTERVAL", "5"))  # Reporting interval in seconds
# yes / no / auto (default). VMs are left out of the dashboard's power and cost
# totals; auto-detection covers local hypervisors and cloud instances alike.
IS_VM_SETTING = os.environ.get("HOMELAB_IS_VM", "auto").strip().lower()

# Prevent console window popping up/stealing focus when spawning subprocesses on Windows
creationflags = 0x08000000 if os.name == 'nt' else 0

class AgentState:
    def __init__(self):
        self.last_net_bytes_sent = None
        self.last_net_bytes_recv = None
        self.last_net_time = None
        self.last_disk_read = None
        self.last_disk_write = None
        self.last_disk_time = None
        self.last_rapl_energy = None
        self.last_rapl_time = None
        self.pending_updates = 0
        self.last_update_check = 0.0
        self.is_vm = False
        self.docker_updates = {}
        self.last_docker_check = 0.0

state = AgentState()

def get_ip():
    """Get the primary local IP address of this machine."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # Doesn't need to be reachable
        s.connect(('10.255.255.255', 1))
        ip = s.getsockname()[0]
    except Exception:
        ip = '127.0.0.1'
    finally:
        s.close()
    return ip

def get_cpu_model():
    """Retrieve the marketing name of the CPU."""
    try:
        sys_name = platform.system()
        if sys_name == "Windows":
            import winreg
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0")
            name, _ = winreg.QueryValueEx(key, "ProcessorNameString")
            return name.strip()
        elif sys_name == "Linux":
            with open("/proc/cpuinfo", "r") as f:
                for line in f:
                    if "model name" in line:
                        return line.split(":")[1].strip()
        elif sys_name == "Darwin":
            return subprocess.check_output(["sysctl", "-n", "machdep.cpu.brand_string"]).decode().strip()
    except Exception:
        pass
    return platform.processor()

def get_os_info():
    """Retrieve detailed Operating System release and build info."""
    try:
        sys_name = platform.system()
        if sys_name == "Windows":
            release = platform.release()
            version = platform.version()
            return f"Windows {release} (Build {version.split('.')[-1]})"
        elif sys_name == "Linux":
            if os.path.exists("/etc/os-release"):
                with open("/etc/os-release") as f:
                    for line in f:
                        if line.startswith("PRETTY_NAME="):
                            return line.split("=")[1].replace('"', '').strip()
            return f"Linux {platform.release()}"
        elif sys_name == "Darwin":
            return f"macOS {platform.mac_ver()[0]}"
    except Exception:
        pass
    return f"{platform.system()} {platform.release()}"

# Strings that only virtual hardware puts in its DMI (Linux) or WMI (Windows)
# vendor and model fields. OCI tags its instances "OracleCloud.com".
VM_MARKERS = ("kvm", "qemu", "vmware", "virtualbox", "innotek", "xen", "bochs",
              "virtual machine", "amazon ec2", "google compute engine",
              "openstack", "oraclecloud")

def detect_vm():
    """Return what gave this machine away as a virtual machine, or None."""
    sys_name = platform.system()
    try:
        if sys_name == "Linux":
            try:
                # --vm leaves containers out: an agent in a container is
                # usually the only one reporting for that physical machine.
                res = subprocess.run(["systemd-detect-virt", "--vm"], stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, text=True, timeout=5)
                # Prints "none" and exits non-zero on bare metal.
                return res.stdout.strip() if res.returncode == 0 else None
            except FileNotFoundError:
                pass

            # No systemd (Alpine, some NAS firmware). x86 guests carry the
            # hypervisor CPU flag; ARM ones (OCI Ampere) only show up in DMI.
            with open("/proc/cpuinfo", "r") as f:
                for line in f:
                    if line.startswith("flags") and "hypervisor" in line.split():
                        return "hypervisor"
            dmi = []
            for field in ("sys_vendor", "product_name", "chassis_asset_tag"):
                path = f"/sys/class/dmi/id/{field}"
                if os.path.exists(path):
                    with open(path, "r") as f:
                        dmi.append(f.read().strip())
            text = " ".join(dmi)
            if any(marker in text.lower() for marker in VM_MARKERS):
                return text
        elif sys_name == "Windows":
            # Not HypervisorPresent: that is also true on a physical host
            # running Hyper-V, WSL2 or VBS.
            cmd = ['powershell', '-Command', '$c = Get-CimInstance Win32_ComputerSystem; "$($c.Manufacturer) $($c.Model)"']
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10, creationflags=creationflags)
            text = res.stdout.strip()
            if any(marker in text.lower() for marker in VM_MARKERS):
                return text
        elif sys_name == "Darwin":
            res = subprocess.run(["sysctl", "-n", "kern.hv_vmm_present"], stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, text=True, timeout=5)
            if res.stdout.strip() == "1":
                return "hypervisor"
    except Exception:
        pass
    return None

def resolve_is_vm():
    """Return (is_vm, reason), with HOMELAB_IS_VM overriding detection."""
    if IS_VM_SETTING in ("yes", "true", "1", "on"):
        return True, "HOMELAB_IS_VM"
    if IS_VM_SETTING in ("no", "false", "0", "off"):
        return False, "HOMELAB_IS_VM"
    if IS_VM_SETTING not in ("", "auto"):
        print(f"Ignoring HOMELAB_IS_VM={IS_VM_SETTING!r}; expected yes, no or auto.")
    found = detect_vm()
    return found is not None, f"detected: {found}" if found else "detected"

def query_pending_updates():
    """Retrieve the number of pending OS updates."""
    sys_name = platform.system()
    if sys_name == "Windows":
        try:
            # Native COM call to query pending Windows updates
            cmd = ['powershell', '-Command', "(New-Object -ComObject Microsoft.Update.Session).CreateUpdateSearcher().Search(\"IsInstalled=0 and Type='Software' and IsHidden=0\").Updates.Count"]
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10, creationflags=creationflags)
            if res.returncode == 0 and res.stdout.strip():
                return int(res.stdout.strip().split('\n')[0])
        except Exception:
            pass
    elif sys_name == "Linux":
        try:
            # 1. On Ubuntu/Debian, try update-notifier cache first
            path = "/var/lib/update-notifier/updates-available"
            if os.path.exists(path):
                with open(path, "r") as f:
                    content = f.read()
                match = re.search(r'(\d+)\s+updates?\s+can\s+be\s+applied', content, re.IGNORECASE)
                if match:
                    return int(match.group(1))
            
            # 2. Try the faster apt-check executable on Ubuntu
            apt_check = "/usr/lib/update-notifier/apt-check"
            if os.path.exists(apt_check):
                res = subprocess.run([apt_check], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=5)
                if res.returncode == 0 and res.stderr.strip():
                    parts = res.stderr.strip().split(';')
                    if len(parts) >= 1:
                        return int(parts[0])
        except Exception:
            pass
    return 0

def get_temperature():
    """Retrieve CPU temperature in Celsius (Linux/macOS native, Windows WMI fallback)."""
    try:
        # 1. Try psutil sensors
        if hasattr(psutil, "sensors_temperatures"):
            temps = psutil.sensors_temperatures()
            if temps:
                for key in ['coretemp', 'cpu_thermal', 'cpu-thermal', 'acpitz']:
                    if key in temps and temps[key]:
                        return temps[key][0].current
                for entries in temps.values():
                    if entries and entries[0].current:
                        return entries[0].current

        # 2. Try sysfs on Linux
        if os.path.exists("/sys/class/thermal/thermal_zone0/temp"):
            with open("/sys/class/thermal/thermal_zone0/temp", "r") as f:
                return float(f.read().strip()) / 1000.0

        # 3. Try WMI LibreHardwareMonitor/OpenHardwareMonitor on Windows
        if platform.system() == "Windows":
            # Query LibreHardwareMonitor
            cmd = ['powershell', '-Command', "Get-CimInstance -Namespace root/LibreHardwareMonitor -ClassName Sensor | Where-Object { $_.SensorType -eq 'Temperature' -and ($_.Name -like '*CPU Package*' -or $_.Name -like '*CPU Core*') } | Select-Object -ExpandProperty Value"]
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=2, creationflags=creationflags)
            if res.returncode == 0 and res.stdout.strip():
                return float(res.stdout.strip().split('\n')[0])
            
            # Query OpenHardwareMonitor
            cmd = ['powershell', '-Command', "Get-CimInstance -Namespace root/OpenHardwareMonitor -ClassName Sensor | Where-Object { $_.SensorType -eq 'Temperature' -and ($_.Name -like '*CPU Package*' -or $_.Name -like '*CPU Core*') } | Select-Object -ExpandProperty Value"]
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=2, creationflags=creationflags)
            if res.returncode == 0 and res.stdout.strip():
                return float(res.stdout.strip().split('\n')[0])
    except Exception:
        pass
    return None

def get_gpu_status():
    """Query NVIDIA GPU usage and stats using nvidia-smi if available."""
    try:
        # Added power.draw to the nvidia-smi query
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,utilization.gpu,utilization.memory,memory.total,memory.used,temperature.gpu,power.draw", "--format=csv,noheader,nounits"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=3,
            creationflags=creationflags
        )
        if result.returncode == 0 and result.stdout.strip():
            parts = result.stdout.strip().split(", ")
            if len(parts) == 7:
                return {
                    "name": parts[0],
                    "utilization": float(parts[1]),
                    "mem_utilization": float(parts[2]),
                    "mem_total": float(parts[3]) * 1024 * 1024,  # Convert MB to Bytes
                    "mem_used": float(parts[4]) * 1024 * 1024,
                    "temp": float(parts[5]),
                    "power": float(parts[6])  # Power draw in Watts
                }
    except Exception:
        pass
    return None

def get_cpu_power():
    """Retrieve the real-time CPU package power draw in Watts."""
    sys_name = platform.system()
    if sys_name == "Windows":
        try:
            # Query the CPU RAPL package power counter in milliwatts
            cmd = ['powershell', '-Command', '(Get-Counter -Counter \'\\Energy Meter(*_pkg)\\Power\' -ErrorAction SilentlyContinue).CounterSamples.CookedValue']
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=2, creationflags=creationflags)
            if res.returncode == 0 and res.stdout.strip():
                # Value is in milliwatts, return as Watts
                return float(res.stdout.strip().split('\n')[0]) / 1000.0
        except Exception:
            pass
    elif sys_name == "Linux":
        # 1. Query Linux RAPL package 0 energy counter (Intel/AMD)
        try:
            path = "/sys/class/powercap/intel-rapl/intel-rapl:0/energy_uj"
            if os.path.exists(path):
                with open(path, "r") as f:
                    energy = float(f.read().strip())
                now = time.time()
                
                if state.last_rapl_energy is None:
                    state.last_rapl_energy = energy
                    state.last_rapl_time = now
                    return None
                    
                dt = now - state.last_rapl_time
                power = 0.0
                if dt > 0:
                    power = (energy - state.last_rapl_energy) / (dt * 1000000.0)
                    
                state.last_rapl_energy = energy
                state.last_rapl_time = now
                
                # Sanity limit check (0 to 500W)
                if 0.0 <= power <= 500.0:
                    return power
        except Exception:
            pass

        # 2. Try sysfs hwmon files (readable without root and supports drivers like fam15h_power, zenpower, coretemp)
        try:
            import glob
            for hwmon_dir in glob.glob("/sys/class/hwmon/hwmon*"):
                name_path = os.path.join(hwmon_dir, "name")
                if os.path.exists(name_path):
                    with open(name_path, "r") as nf:
                        hwmon_name = nf.read().strip().lower()
                    # Skip non-CPU monitors (GPUs, NVME drives, etc.)
                    if any(x in hwmon_name for x in ["gpu", "amdgpu", "nouveau", "nvidia", "nvme"]):
                        continue
                    
                    for power_file in glob.glob(os.path.join(hwmon_dir, "power*_input")):
                        label_path = power_file.replace("_input", "_label")
                        if os.path.exists(label_path):
                            with open(label_path, "r") as lf:
                                label = lf.read().strip().lower()
                            if any(x in label for x in ["pkg", "package", "cpu"]):
                                with open(power_file, "r") as pf:
                                    val = float(pf.read().strip())
                                    return val / 1000000.0
                                    
                        if hwmon_name in ["fam15h_power", "zenpower", "coretemp", "intel_rapl"]:
                            with open(power_file, "r") as pf:
                                val = float(pf.read().strip())
                                if val > 0:
                                    return val / 1000000.0
        except Exception:
            pass
            
    return None

def estimate_cpu_power(cpu_model, cpu_usage):
    """Estimate CPU power draw based on CPU model and usage when no hardware sensors exist."""
    if not cpu_model:
        return None
        
    model = cpu_model.lower()
    
    # AMD GX-412TC SOC (PC Engines APU2)
    # TDP is 6W. Idle draw is ~3.5W, max load is ~6W.
    if "gx-412tc" in model:
        idle_w = 3.5
        max_w = 6.0
        return idle_w + (max_w - idle_w) * (cpu_usage / 100.0)
        
    # AMD R-Series RX-421BD (Merlin Falcon NAS SoC)
    # TDP is configurable 12W - 35W (typically 15W or 35W).
    # Assuming standard 15W mode. Idle is ~6W, max is ~15W.
    if "rx-421bd" in model:
        idle_w = 6.0
        max_w = 15.0
        return idle_w + (max_w - idle_w) * (cpu_usage / 100.0)

    # Other AMD G-Series or GX Embedded CPUs
    if "gx-" in model or "g-series" in model:
        idle_w = 4.0
        max_w = 15.0
        return idle_w + (max_w - idle_w) * (cpu_usage / 100.0)
        
    return None

def get_disk_status():
    """Retrieve status for all local disk partitions."""
    disks = []
    try:
        for part in psutil.disk_partitions(all=False):
            if 'cdrom' in part.opts or part.fstype == '':
                continue
            # Skip virtual file systems and loop devices on Linux
            if os.name != 'nt':
                if part.mountpoint.startswith(('/proc', '/sys', '/dev', '/run', '/boot')):
                    continue
                if 'loop' in part.device or part.device.startswith('/dev/loop'):
                    continue
            
            try:
                usage = psutil.disk_usage(part.mountpoint)
                if usage.total == 0:
                    continue
                disks.append({
                    "device": part.device,
                    "mount": part.mountpoint,
                    "total": usage.total,
                    "used": usage.used,
                    "free": usage.free,
                    "percent": usage.percent
                })
            except PermissionError:
                continue
            except Exception:
                continue
    except Exception:
        pass
    return disks

def get_disk_speeds():
    """Calculate average disk read and write speeds in Bytes/sec."""
    try:
        # Check if disk IO counters is supported on this system
        counters = psutil.disk_io_counters()
        now = time.time()
        
        if state.last_disk_read is None:
            state.last_disk_read = counters.read_bytes
            state.last_disk_write = counters.write_bytes
            state.last_disk_time = now
            return {"read_speed": 0.0, "write_speed": 0.0}
            
        dt = now - state.last_disk_time
        if dt <= 0:
            return {"read_speed": 0.0, "write_speed": 0.0}
            
        read_speed = (counters.read_bytes - state.last_disk_read) / dt
        write_speed = (counters.write_bytes - state.last_disk_write) / dt
        
        state.last_disk_read = counters.read_bytes
        state.last_disk_write = counters.write_bytes
        state.last_disk_time = now
        
        return {
            "read_speed": read_speed,
            "write_speed": write_speed
        }
    except Exception:
        return {"read_speed": 0.0, "write_speed": 0.0}

def get_network_speeds():
    """Calculate average network download and upload speeds in Bytes/sec."""
    try:
        counters = psutil.net_io_counters()
        now = time.time()
        
        if state.last_net_bytes_sent is None:
            state.last_net_bytes_sent = counters.bytes_sent
            state.last_net_bytes_recv = counters.bytes_recv
            state.last_net_time = now
            return {"down_speed": 0.0, "up_speed": 0.0}
            
        dt = now - state.last_net_time
        if dt <= 0:
            return {"down_speed": 0.0, "up_speed": 0.0}
            
        down_speed = (counters.bytes_recv - state.last_net_bytes_recv) / dt
        up_speed = (counters.bytes_sent - state.last_net_bytes_sent) / dt
        
        state.last_net_bytes_sent = counters.bytes_sent
        state.last_net_bytes_recv = counters.bytes_recv
        state.last_net_time = now
        
        return {
            "down_speed": down_speed,
            "up_speed": up_speed
        }
    except Exception:
        return {"down_speed": 0.0, "up_speed": 0.0}

def get_vpn_status():
    """Check for active Tailscale and OpenVPN interfaces."""
    vpns = {
        "tailscale": False,
        "openvpn": False
    }
    try:
        interfaces = psutil.net_if_addrs().keys()
        for iface in interfaces:
            iface_lower = iface.lower()
            
            # Tailscale check
            if "tailscale" in iface_lower or iface_lower.startswith("utun") or iface_lower == "ts0":
                vpns["tailscale"] = True
                
            # OpenVPN check
            if "tun" in iface_lower or "tap" in iface_lower or "openvpn" in iface_lower:
                vpns["openvpn"] = True
    except Exception:
        pass
    return vpns

def get_docker_containers():
    """Retrieve Docker container list using CLI subprocess call."""
    containers = []
    try:
        result = subprocess.run(
            ["docker", "ps", "-a", "--format", "{{.Names}}\t{{.State}}\t{{.Image}}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=3,
            creationflags=creationflags
        )
        if result.returncode == 0 and result.stdout.strip():
            lines = result.stdout.strip().split("\n")
            for line in lines:
                parts = line.split("\t")
                if len(parts) == 3:
                    name, status, image = parts
                    container = {"name": name, "state": status.capitalize(), "image": image}
                    # Adds update, update_note and the configured image name,
                    # which docker ps swaps for an ID once the tag has moved.
                    container.update(state.docker_updates.get(name, {}))
                    containers.append(container)
    except Exception:
        pass
    return containers

# How often to ask the registries whether each container's image has moved
# on. Manifest HEAD requests do not count against Docker Hub's pull limit.
DOCKER_UPDATE_INTERVAL = 6 * 3600

MANIFEST_TYPES = ", ".join([
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
])

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Surface redirects so the caller can follow them with its token."""
    def redirect_request(self, *args, **kwargs):
        return None

_registry_opener = urllib.request.build_opener(_NoRedirect)

def parse_image_ref(ref):
    """Split an image reference into (registry host, repository, tag).

    Follows Docker's rules: without a registry host the image is on Docker
    Hub, where a bare name such as "nextcloud" lives under library/.
    """
    name, tag = ref, "latest"
    if ":" in ref.rsplit("/", 1)[-1]:
        name, tag = ref.rsplit(":", 1)
    first, _, rest = name.partition("/")
    if rest and ("." in first or ":" in first or first == "localhost"):
        registry, repo = first, rest
    else:
        registry, repo = "docker.io", name
    if registry == "docker.io":
        registry = "registry-1.docker.io"
        if "/" not in repo:
            repo = f"library/{repo}"
    return registry, repo, tag

def _bearer_token(challenge, cache):
    """Fetch an anonymous pull token for a WWW-Authenticate Bearer challenge."""
    if not challenge.lower().startswith("bearer "):
        return None
    params = dict(re.findall(r'(\w+)="([^"]*)"', challenge))
    realm = params.pop("realm", None)
    if not realm:
        return None
    url = f"{realm}?{urllib.parse.urlencode(params)}"
    if url not in cache:
        req = urllib.request.Request(url, headers={"User-Agent": "HomeLab-Dashboard-Agent"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read())
        cache[url] = body.get("token") or body.get("access_token")
    return cache[url]

def registry_digest(ref, tokens):
    """Digest the registry serves for this image's tag right now."""
    registry, repo, tag = parse_image_ref(ref)
    url = f"https://{registry}/v2/{repo}/manifests/{tag}"
    token = None
    for _ in range(5):
        headers = {"Accept": MANIFEST_TYPES, "User-Agent": "HomeLab-Dashboard-Agent"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(url, method="HEAD", headers=headers)
        try:
            with _registry_opener.open(req, timeout=10) as resp:
                return resp.headers.get("Docker-Content-Digest")
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308) and e.headers.get("Location"):
                # lscr.io, for one, hands manifest requests over to ghcr.io.
                url = urllib.parse.urljoin(url, e.headers["Location"])
                continue
            if e.code == 401 and token is None:
                token = _bearer_token(e.headers.get("WWW-Authenticate", ""), tokens)
                if token:
                    continue
            raise
    raise RuntimeError("too many redirects")

def _registry_error(err):
    if isinstance(err, urllib.error.HTTPError):
        return {401: "registry needs a login", 403: "registry refused access",
                404: "tag not found on the registry"}.get(err.code, f"registry answered {err.code}")
    if isinstance(err, urllib.error.URLError):
        return f"registry unreachable ({err.reason})"
    return str(err) or type(err).__name__

def check_docker_updates():
    """Compare each container's image with what its registry serves now.

    Returns {container name: {"image", "update", "update_note"}}, where update
    is "available", "current", or None when it cannot be told. Nothing is
    pulled or restarted.
    """
    def docker(*args):
        return subprocess.run(["docker", *args], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              text=True, timeout=60, creationflags=creationflags).stdout

    def repo_digests(image):
        try:
            found = json.loads(docker("image", "inspect", "--format", "{{json .RepoDigests}}", image) or "null")
        except ValueError:
            return set()
        return {d.split("@", 1)[1] for d in found or [] if "@" in d}

    ids = docker("ps", "-aq", "--no-trunc").split()
    if not ids:
        return {}
    # Config.Image is the reference the container was created from; .Image is
    # the image it actually runs, which lags behind after a pull until the
    # container is recreated.
    rows = [line.split("\t") for line in
            docker("inspect", "--format", "{{.Name}}\t{{.Config.Image}}\t{{.Image}}", *ids).splitlines()]

    tokens, latest, results = {}, {}, {}
    for row in rows:
        if len(row) != 3:
            continue
        name, ref, image_id = row[0].lstrip("/"), row[1], row[2]
        result = {"image": ref, "update": None, "update_note": ""}
        results[name] = result
        if "@sha256:" in ref:
            result["update_note"] = "Pinned to a digest"
            continue
        running = repo_digests(image_id)
        if not running:
            result["update_note"] = "Built locally; nothing to compare against"
            continue
        if ref not in latest:
            try:
                latest[ref] = registry_digest(ref, tokens)
            except Exception as e:
                latest[ref] = e
        remote = latest[ref]
        if isinstance(remote, Exception) or not remote:
            result["update_note"] = f"Could not check: {_registry_error(remote) if remote else 'no digest returned'}"
        elif remote in running:
            result["update"] = "current"
        else:
            result["update"] = "available"
            result["update_note"] = ("Newer image already pulled; recreate the container to use it"
                                     if remote in repo_digests(ref) else "Newer image on the registry")
    return results

def gather_metrics():
    """Gather all detailed system, GPU, storage, VPN, and Docker metrics."""
    hostname = socket.gethostname()
    ip = get_ip()
    os_info = get_os_info()
    cpu_model = get_cpu_model()
    
    # Core counts
    cpu_cores_logical = psutil.cpu_count(logical=True)
    cpu_cores_physical = psutil.cpu_count(logical=False)
    
    # CPU Usage (blocks for 0.5s)
    cpu_usage = psutil.cpu_percent(interval=0.5)
    
    # Virtual Memory
    mem = psutil.virtual_memory()
    memory_info = {
        "total": mem.total,
        "used": mem.used,
        "free": mem.available
    }
    
    # Disks (Multiple) and speeds
    disks = get_disk_status()
    disk_speeds = get_disk_speeds()
    
    # Network bandwidth
    network = get_network_speeds()
    
    # Uptime
    uptime = time.time() - psutil.boot_time()
    
    # Temp and power
    temp = get_temperature()
    cpu_power = get_cpu_power()
    if cpu_power is None:
        cpu_power = estimate_cpu_power(cpu_model, cpu_usage)
    
    # GPU (RTX / Nvidia)
    gpu = get_gpu_status()
    
    # VPN
    vpns = get_vpn_status()
    
    # Docker
    docker_containers = get_docker_containers()
    
    # Check for pending OS updates asynchronously every 4 hours (14400s)
    now = time.time()
    if now - state.last_update_check >= 14400:
        state.last_update_check = now
        import threading
        def run_check():
            try:
                state.pending_updates = query_pending_updates()
            except Exception:
                pass
        threading.Thread(target=run_check, daemon=True).start()

    if docker_containers and now - state.last_docker_check >= DOCKER_UPDATE_INTERVAL:
        state.last_docker_check = now
        def run_docker_check():
            try:
                state.docker_updates = check_docker_updates()
            except Exception as e:
                print(f"Docker update check failed: {e}")
        threading.Thread(target=run_docker_check, daemon=True).start()

    return {
        "hostname": hostname,
        "ip": ip,
        "os_info": os_info,
        "cpu_model": cpu_model,
        "cpu_cores": {
            "physical": cpu_cores_physical,
            "logical": cpu_cores_logical
        },
        "cpu_usage": cpu_usage,
        "memory": memory_info,
        "disks": disks,
        "disk_speeds": disk_speeds,
        "network": network,
        "uptime": uptime,
        "temp": temp,
        "cpu_power": cpu_power,
        "gpu": gpu,
        "vpns": vpns,
        "docker_containers": docker_containers,
        "pending_updates": state.pending_updates,
        "is_vm": state.is_vm
    }

def send_metrics(data):
    """POST JSON payload to server with verification key."""
    payload = json.dumps(data).encode("utf-8")
    req = urllib.request.Request(
        SERVER_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "X-Agent-Key": AGENT_KEY
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=3) as response:
            response.read()
            return True
    except urllib.error.URLError as e:
        print(f"Failed to connect to dashboard server: {e.reason}")
    except Exception as e:
        print(f"Error sending metrics: {e}")
    return False

def main():
    if not AGENT_KEY:
        print("HOMELAB_AGENT_KEY is not set. The server will reject every report.")
        print("Set it in the agent environment file, or re-run the install script.")
        return

    state.is_vm, reason = resolve_is_vm()

    print(f"Starting Enhanced Home Lab Dashboard Agent...")
    print(f"Target Server: {SERVER_URL}")
    print(f"Node type: {'virtual machine' if state.is_vm else 'physical'} ({reason})")
    print(f"Press Ctrl+C to exit.")
    
    while True:
        try:
            start_time = time.time()
            
            # Gather and push
            metrics = gather_metrics()
            success = send_metrics(metrics)
            
            if success:
                print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Metrics sent successfully.")
            
            # Account for processing time
            elapsed = time.time() - start_time
            sleep_time = max(0.1, INTERVAL - elapsed)
            time.sleep(sleep_time)
            
        except KeyboardInterrupt:
            print("\nAgent stopped.")
            break
        except Exception as e:
            print(f"Unexpected error: {e}")
            time.sleep(INTERVAL)

if __name__ == "__main__":
    main()
