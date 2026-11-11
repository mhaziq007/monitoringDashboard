"""Agent: image reference parsing, registry lookups, Docker update checks, VM override."""

import ctypes
import io
import json
import subprocess
import urllib.error

import pytest

import agent


@pytest.mark.parametrize("ref, expected", [
    ("nextcloud", ("registry-1.docker.io", "library/nextcloud", "latest")),
    ("qmcgaw/gluetun", ("registry-1.docker.io", "qmcgaw/gluetun", "latest")),
    ("docker.io/library/nginx:1.27", ("registry-1.docker.io", "library/nginx", "1.27")),
    ("lscr.io/linuxserver/qbittorrent:latest", ("lscr.io", "linuxserver/qbittorrent", "latest")),
    ("ghcr.io/homarr-labs/homarr:latest", ("ghcr.io", "homarr-labs/homarr", "latest")),
    ("localhost:5000/tools/app", ("localhost:5000", "tools/app", "latest")),
])
def test_parse_image_ref(ref, expected):
    assert agent.parse_image_ref(ref) == expected


class Response(io.BytesIO):
    def __init__(self, body=b"", headers=None):
        super().__init__(body)
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


GHCR = "https://ghcr.io/v2/linuxserver/qbittorrent/manifests/latest"
CHALLENGE = 'Bearer realm="https://ghcr.io/token",service="ghcr.io",scope="repository:linuxserver/qbittorrent:pull"'


@pytest.mark.parametrize("auth_first", [False, True])
def test_lscr_redirect_to_ghcr_is_followed_with_the_token(monkeypatch, auth_first):
    def opener(req, timeout=None):
        url, auth = req.full_url, req.headers.get("Authorization")
        if url.startswith("https://lscr.io"):
            if auth_first and not auth:
                raise urllib.error.HTTPError(url, 401, "", {"WWW-Authenticate": CHALLENGE}, None)
            raise urllib.error.HTTPError(url, 307, "", {"Location": GHCR}, None)
        assert url == GHCR
        if not auth:
            raise urllib.error.HTTPError(url, 401, "", {"WWW-Authenticate": CHALLENGE}, None)
        assert auth == "Bearer tok"
        return Response(headers={"Docker-Content-Digest": "sha256:new"})

    monkeypatch.setattr(agent._registry_opener, "open", opener)
    monkeypatch.setattr(agent.urllib.request, "urlopen", lambda req, timeout=None: Response(json.dumps({"token": "tok"}).encode()))
    assert agent.registry_digest("lscr.io/linuxserver/qbittorrent:latest", {}) == "sha256:new"


def test_check_docker_updates_classifies_each_container(monkeypatch):
    inspect = "\n".join([
        "/qbittorrent\tlscr.io/linuxserver/qbittorrent:latest\tsha256:img-qb",
        "/gluetun\tqmcgaw/gluetun\tsha256:img-gl-old",
        "/byparr\tghcr.io/thephaseless/byparr:latest\tsha256:img-by",
        "/immich_redis\tdocker.io/valkey/valkey:9@sha256:abc\tsha256:img-redis",
        "/mytool\tmytool:dev\tsha256:img-local",
        "/private\tregistry.example.com/me/app:1\tsha256:img-priv",
    ])
    digests = {
        "sha256:img-qb": ["lscr.io/linuxserver/qbittorrent@sha256:new"],
        "sha256:img-gl-old": ["qmcgaw/gluetun@sha256:gl-old"],
        "qmcgaw/gluetun": ["qmcgaw/gluetun@sha256:gl-new"],     # pulled, container not recreated
        "sha256:img-by": ["ghcr.io/thephaseless/byparr@sha256:by-old"],
        "ghcr.io/thephaseless/byparr:latest": ["ghcr.io/thephaseless/byparr@sha256:by-old"],
        "sha256:img-redis": ["docker.io/valkey/valkey@sha256:abc"],
        "sha256:img-local": [],
        "sha256:img-priv": ["registry.example.com/me/app@sha256:p"],
    }

    def run(cmd, **kwargs):
        args = cmd[1:]
        if args[:2] == ["ps", "-aq"]:
            out = "a\nb\nc\nd\ne\nf\n"
        elif args[0] == "inspect":
            out = inspect
        else:
            out = json.dumps(digests.get(args[-1]))
        return subprocess.CompletedProcess(cmd, 0, stdout=out)

    remote = {"lscr.io/linuxserver/qbittorrent:latest": "sha256:new", "qmcgaw/gluetun": "sha256:gl-new",
              "ghcr.io/thephaseless/byparr:latest": "sha256:by-new"}

    def registry_digest(ref, tokens):
        if ref in remote:
            return remote[ref]
        raise urllib.error.HTTPError(ref, 401, "", {}, None)

    monkeypatch.setattr(agent.subprocess, "run", run)
    monkeypatch.setattr(agent, "registry_digest", registry_digest)
    results = agent.check_docker_updates()
    assert {name: r["update"] for name, r in results.items()} == {
        "qbittorrent": "current", "gluetun": "available", "byparr": "available",
        "immich_redis": None, "mytool": None, "private": None,
    }
    assert "recreate" in results["gluetun"]["update_note"]
    assert results["immich_redis"]["update_note"] == "Pinned to a digest"
    assert results["private"]["update_note"] == "Could not check: registry needs a login"
    assert results["byparr"]["image"] == "ghcr.io/thephaseless/byparr:latest"


@pytest.mark.parametrize("setting, expected", [("yes", True), ("1", True), ("no", False), ("off", False)])
def test_is_vm_setting_overrides_detection(monkeypatch, setting, expected):
    monkeypatch.setattr(agent, "IS_VM_SETTING", setting)
    monkeypatch.setattr(agent, "detect_vm", lambda: pytest.fail("detection should not run"))
    assert agent.resolve_is_vm() == (expected, "HOMELAB_IS_VM")


def test_is_vm_auto_falls_back_to_detection(monkeypatch):
    monkeypatch.setattr(agent, "IS_VM_SETTING", "auto")
    monkeypatch.setattr(agent, "detect_vm", lambda: "kvm")
    assert agent.resolve_is_vm() == (True, "detected: kvm")


def test_reclaimable_zfs_cache_counts_as_free(tmp_path, monkeypatch):
    arcstats = tmp_path / "arcstats"
    arcstats.write_text("13 1 0x01 123 33456 1234 5678\n"
                        "name                            type data\n"
                        "size                            4    17179869184\n"
                        "c_min                           4    1073741824\n")
    monkeypatch.setattr(agent, "ARCSTATS", str(arcstats))
    assert agent.zfs_arc_reclaimable() == 15 * 1024 ** 3   # 16 GB of cache above a 1 GB minimum
    monkeypatch.setattr(agent, "ARCSTATS", str(tmp_path / "missing"))
    assert agent.zfs_arc_reclaimable() == 0


def test_package_power_sums_the_pkg_instances_only():
    readings = {"rapl_package0_core0_core": 3512.37, "rapl_package0_pkg": 69188.12, "_total": 0.0}
    assert agent.package_watts(readings) == pytest.approx(69.18812)
    assert agent.package_watts({"rapl_package0_pkg": 40000, "rapl_package1_pkg": 30000}) == 70.0
    assert agent.package_watts({"_total": 0.0}) is None


class FakePdh:
    """Stands in for pdh.dll: answers the calls the way Windows does, from
    `samples` (instance name -> (status, milliwatts))."""

    def __init__(self, samples, fail_add=False):
        self.samples, self.fail_add, self.collected = samples, fail_add, 0
        self._keep = []

    def PdhOpenQueryW(self, source, user, query):
        query._obj.value = 1
        return 0

    def PdhAddEnglishCounterW(self, query, path, user, counter):
        assert path == r"\Energy Meter(*)\Power"
        if self.fail_add:
            return 0xC0000BB8 - (1 << 32)   # PDH_CSTATUS_NO_OBJECT, as a signed int
        counter._obj.value = 2
        return 0

    def PdhCollectQueryData(self, query):
        self.collected += 1
        return 0

    def PdhGetFormattedCounterArrayW(self, counter, fmt, size, count, buffer):
        Item = agent.WindowsEnergyMeter._Item
        items = (Item * len(self.samples))()
        for item, (name, (status, mw)) in zip(items, self.samples.items()):
            item.szName = name
            item.FmtValue.CStatus = status
            item.FmtValue.value.doubleValue = mw
        self._keep.append(items)
        count._obj.value = len(self.samples)
        if buffer is None or size._obj.value < ctypes.sizeof(items):
            size._obj.value = ctypes.sizeof(items)
            return agent.WindowsEnergyMeter.PDH_MORE_DATA - (1 << 32)
        ctypes.memmove(buffer, items, ctypes.sizeof(items))
        return 0


def test_energy_meter_reads_the_package_counter_through_pdh():
    pdh = FakePdh({"rapl_package0_core0_core": (0, 3512.37), "rapl_package0_pkg": (0, 69188.12),
                   "_total": (0, 0.0)})
    meter = agent.WindowsEnergyMeter(pdh)
    assert pdh.collected == 1                      # primed at start
    assert meter.read() == pytest.approx(69.18812)
    # A sample Windows marks invalid (e.g. too soon after the last) is skipped.
    pdh.samples["rapl_package0_pkg"] = (0xC0000BBA, 1.0)
    assert meter.read() is None


def test_without_pdh_the_agent_falls_back_to_powershell(monkeypatch):
    monkeypatch.setattr(agent, "WindowsEnergyMeter", lambda: agent.WindowsEnergyMeter(FakePdh({}, fail_add=True)))
    monkeypatch.setattr(agent.state, "energy_meter", None)
    calls = []

    def run(cmd, **kwargs):
        calls.append((cmd, kwargs["timeout"]))
        return subprocess.CompletedProcess(cmd, 0, stdout="69188.12\n", stderr="")
    monkeypatch.setattr(agent.subprocess, "run", run)
    assert agent.windows_cpu_power() == pytest.approx(69.18812)
    assert agent.state.energy_meter is False
    assert "-NoProfile" in calls[0][0] and calls[0][1] == 5
