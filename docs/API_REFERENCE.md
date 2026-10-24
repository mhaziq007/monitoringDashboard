# 📡 API Reference

This document provides a comprehensive specification for the **HomeLab Dashboard API**, including endpoint definitions, authentication mechanisms, request/response formats, error codes, and real-time Server-Sent Events (SSE) data streams.

---

## 1. Authentication & Security

The API implements two layers of authentication:
1. **User Session Authentication**: Employs HTTP Bearer tokens generated via PBKDF2-SHA256 password validation. Valid for 14 days.
2. **Agent Verification**: Validates telemetry submissions using a unique shared `X-Agent-Key` header generated on the server.

### Headers

| Header | Format / Example | Description |
| :--- | :--- | :--- |
| `Authorization` | `Bearer <session_token_hex>` | Required for authenticated browser API requests. |
| `X-Agent-Key` | `3e809ca82bd1b490df35696e25314821` | Required for metric ingestion requests from `agent.py`. |

---

## 2. Authentication Endpoints

### 2.0 Server Version
- **Endpoint**: `GET /api/version` (no auth)
- **Response**: `{ "running": "879888c", "installed": "194dd94", "stale": true }`: the commit the server started with and the one its checkout is on now. `stale` means it needs a restart; both are `""` when the server does not run from a git checkout.

### 2.1 Check Registration Status
Returns whether an administrator account has already been registered.

- **Endpoint**: `GET /api/auth/status`
- **Auth Required**: No
- **Response**: `200 OK`
```json
{
  "users_exist": true
}
```

---

### 2.2 Initial Admin Registration
Registers the single master administrative account. Once an admin user exists, this endpoint is permanently locked.

- **Endpoint**: `POST /api/auth/register`
- **Auth Required**: No
- **Request Body**:
```json
{
  "username": "admin",
  "password": "SecurePassword123"
}
```
- **Constraints**:
  - `username` must be $\ge 3$ characters.
  - `password` must be $\ge 6$ characters.
- **Success Response**: `200 OK` (Automatically logs the user in)
```json
{
  "token": "4f9b8c2a1d0e...",
  "username": "admin",
  "expires_at": 1725184800.0,
  "agent_auth_key": "3e809ca82bd1b490df35696e25314821"
}
```
- **Error Responses**:
  - `400 Bad Request`: Validation failure (missing fields, username/password too short).
  - `403 Forbidden`: Registration disabled because an admin account already exists.

---

### 2.3 User Login
Authenticates an administrator and creates a new 14-day session.

- **Endpoint**: `POST /api/auth/login`
- **Auth Required**: No
- **Request Body**:
```json
{
  "username": "admin",
  "password": "SecurePassword123"
}
```
- **Success Response**: `200 OK`
```json
{
  "token": "7a3b4c5d6e7f8a9b...",
  "username": "admin",
  "expires_at": 1726394400.0,
  "agent_auth_key": "3e809ca82bd1b490df35696e25314821"
}
```
- **Error Responses**:
  - `401 Unauthorized`: Invalid username or password.

---

### 2.4 User Logout
Destroys the active session token in the database.

- **Endpoint**: `POST /api/auth/logout`
- **Auth Required**: No
- **Request Body**:
```json
{
  "token": "7a3b4c5d6e7f8a9b..."
}
```
- **Success Response**: `200 OK`
```json
{
  "status": "ok"
}
```

---

### 2.5 Retrieve Agent Master Key
Returns the master `agent_auth_key` needed to configure client agents.

- **Endpoint**: `GET /api/auth/agent-key`
- **Auth Required**: Yes (`Bearer <token>`)
- **Success Response**: `200 OK`
```json
{
  "agent_auth_key": "3e809ca82bd1b490df35696e25314821"
}
```

---

## 3. Telemetry & Device Endpoints

### 3.1 Report Metrics (Agent Ingestion)
Endpoint for client nodes to push system metrics every 5 seconds.

- **Endpoint**: `POST /api/report`
- **Auth Required**: Yes (`X-Agent-Key` header)
- **Request Payload**:
```json
{
  "hostname": "falcon-nas",
  "ip": "192.168.1.105",
  "os_info": "Ubuntu 22.04.4 LTS",
  "cpu_model": "AMD R-Series RX-421BD Radeon R7",
  "cpu_cores": {
    "physical": 4,
    "logical": 4
  },
  "cpu_usage": 14.5,
  "memory": {
    "total": 16428384256,
    "used": 4294967296,
    "free": 12133416960
  },
  "disks": [
    {
      "device": "/dev/sda1",
      "mount": "/",
      "total": 245000000000,
      "used": 45000000000,
      "free": 200000000000,
      "percent": 18.3
    },
    {
      "device": "/dev/sdb1",
      "mount": "/mnt/storage",
      "total": 4000000000000,
      "used": 2800000000000,
      "free": 1200000000000,
      "percent": 70.0
    }
  ],
  "disk_speeds": {
    "read_speed": 1048576.0,
    "write_speed": 524288.0
  },
  "network": {
    "down_speed": 2457600.0,
    "up_speed": 128000.0
  },
  "uptime": 1209600.0,
  "temp": 48.5,
  "cpu_power": 9.4,
  "gpu": {
    "name": "NVIDIA GeForce RTX 3080",
    "utilization": 22.0,
    "mem_utilization": 15.0,
    "mem_total": 10737418240,
    "mem_used": 1610612736,
    "temp": 52.0,
    "power": 85.3
  },
  "vpns": {
    "tailscale": true,
    "openvpn": false
  },
  "docker_containers": [
    { "name": "immich_server", "state": "Running", "image": "ghcr.io/immich-app/immich-server:release",
      "update": "available", "update_note": "Newer image on the registry" },
    { "name": "jellyfin", "state": "Running", "image": "jellyfin/jellyfin:latest", "update": "current", "update_note": "" },
    { "name": "postgres", "state": "Running", "image": "postgres:16", "update": null,
      "update_note": "Could not check: registry unreachable (timed out)" }
  ],
  "pending_updates": 3,
  "is_vm": false
}
```
Each container's `update` is `available`, `current`, or `null` when it cannot be checked (see the agent guide); `update_note` explains. Both are absent until the agent's first check finishes, a few seconds after it starts.

`is_vm: true` marks a virtual machine. Its `cpu_power` and `gpu.power` are left out of the power history and the dashboard's power and cost totals.

- **Success Response**: `200 OK`
```json
{
  "status": "ok"
}
```
- **Error Responses**:
  - `401 Unauthorized`: Missing or invalid `X-Agent-Key` header.
  - `200 OK` with `{"status": "error", "message": "Missing hostname"}` if hostname is null.

---

### 3.2 List All Monitored Devices
Returns current state of all known hosts, enriched with online/offline calculation.

- **Endpoint**: `GET /api/devices`
- **Auth Required**: Yes (`Bearer <token>`)
- **Success Response**: `200 OK`
```json
{
  "falcon-nas": {
    "hostname": "falcon-nas",
    "ip": "192.168.1.105",
    "online": true,
    "latency": 0.8,
    "last_seen": 1725184850.2,
    "cpu_usage": 14.5,
    "cpu_power": 9.4,
    "memory": { ... },
    "disks": [ ... ],
    "docker_containers": [ ... ],
    "pending_updates": 3
  }
}
```

---

### 3.3 Remove a Device
Drops a retired or renamed node from the dashboard and from the stored registry. A node whose agent is still running reappears with its next report.

- **Endpoint**: `DELETE /api/devices/{hostname}`
- **Auth Required**: Yes (`Bearer <token>`)
- **Success Response**: `200 OK` with `{"status": "ok"}`, and a `removed` event on the stream.
- **Error Responses**: `404 Not Found` if no node has that hostname.

---

### 3.3a Device History
One node's own CPU %, memory % and network throughput over a range, from the `node_log` table. Buckets match `/api/power-history` for the same range (1 minute for `6h`).

- **Endpoint**: `GET /api/devices/{hostname}/history?range=6h|24h|7d|30d`
- **Auth Required**: Yes (`Bearer <token>`)
- **Success Response**: `200 OK`, an array of `{ "time", "cpu", "mem", "net_down", "net_up" }` oldest first; empty for a node with no recorded history. Removing a node also deletes its history.

---

### 3.3b Disk Alert Thresholds
Each disk alerts at 90% unless set otherwise; a media drive meant to fill up can be raised or switched off. Stored in the `settings` table, so every browser (and later the server's own alerts) uses the same values.

- **Endpoints**: `GET /api/settings/disk-alerts` and `PUT /api/settings/disk-alerts`
- **Auth Required**: Yes (`Bearer <token>`)
- **PUT body**: `{ "hostname": "haziq-nas", "mount": "/mnt/jellyfin-media", "trip": 95 }`, where `trip` is a percent from 50 to 99, `"off"`, or `null` to go back to the default. Anything else is `400`.
- **Response** (both): every override, `{ "haziq-nas": { "/mnt/jellyfin-media": "off", "/mnt/restic-extra": 95 } }`. A PUT also broadcasts it as a `disk_alerts` stream event.

---

### 3.4 Power Draw & Fleet Load History
Returns time-series lab power and fleet load, downsampled based on the requested window. Each point carries:
- `power`: watts drawn by online physical nodes (VMs excluded).
- `cpu`: average CPU % across online nodes.
- `mem`: memory in use across online nodes, as a % of their combined total.
- `net_down` / `net_up`: combined network throughput of online nodes, bytes/s.

The load fields are `null` where nothing was recorded, including every sample taken before the server first recorded them.

- **Endpoint**: `GET /api/power-history`
- **Auth Required**: Yes (`Bearer <token>`)
- **Query Parameters**:
  - `range` (string, optional): One of `6h` (default), `24h`, `7d`, `30d`.
- **Downsampling Behavior**:
  - `6h`: Returns raw in-memory 1-minute buffer.
  - `24h`: SQL 5-minute averaged buckets.
  - `7d`: SQL 30-minute averaged buckets.
  - `30d`: SQL 2-hour averaged buckets.
- **Success Response**: `200 OK`
```json
[
  { "time": 1725163200.0, "power": 38.2, "cpu": 14.1, "mem": 36.2, "net_down": 2457600.0, "net_up": 128000.0 },
  { "time": 1725163500.0, "power": 41.5, "cpu": 18.9, "mem": 36.4, "net_down": 1310720.0, "net_up": 96000.0 },
  { "time": 1725163800.0, "power": 39.0, "cpu": null, "mem": null, "net_down": null, "net_up": null }
]
```

---

### 3.5a Services Uptime History
Uptime per service over a range, from the `service_log` table (one row per probe, every 30 seconds).

- **Endpoint**: `GET /api/services/history?range=24h|7d|30d`
- **Auth Required**: Yes (`Bearer <token>`)
- **Success Response**: `200 OK`, keyed by service name:
```json
{
  "Jellyfin": {
    "uptime": 99.93,
    "slot_seconds": 1800,
    "slots": [
      { "start": 1792735622.6, "up": 1.0, "latency": 6.2 },
      null,
      { "start": 1792739222.6, "up": 0.95, "latency": 7.9 }
    ]
  }
}
```
`slots` always has 48 entries, oldest first; `up` is the share of probes answered and `latency` the mean of the answered ones. A slot with no probes is `null`.

---

### 3.5 Services Status
Returns the list of monitored self-hosted web applications with reachability and latency. Internal URLs are sanitized and never exposed.

- **Endpoint**: `GET /api/services`
- **Auth Required**: Yes (`Bearer <token>`)
- **Success Response**: `200 OK`
```json
[
  {
    "name": "Jellyfin",
    "icon": "fa-play",
    "category": "Media",
    "online": true,
    "latency": 4.2
  },
  {
    "name": "Immich",
    "icon": "fa-images",
    "category": "Photos",
    "online": true,
    "latency": 12.8
  },
  {
    "name": "Uptime Kuma",
    "icon": "fa-chart-line",
    "category": "Monitoring",
    "online": true,
    "latency": 2.1
  }
]
```

---

### 3.6 Alerts
Active alerts (raised by the server and not yet resolved) and the last 100 entries of the alert log, newest first.

- **Endpoint**: `GET /api/alerts`
- **Auth Required**: Yes (`Bearer <token>`)
- **Success Response**: `200 OK`
```json
{
  "active": [
    { "key": "offline:haziq-backupoci", "kind": "offline", "host": "haziq-backupoci", "severity": "crit",
      "title": "haziq-backupoci is offline", "message": "No report for 3 min.", "started": 1792820000.0, "id": 12 }
  ],
  "history": [
    { "id": 11, "key": "service:Homarr", "kind": "service", "host": null, "severity": "crit",
      "title": "Homarr is down", "message": "No response to 2 probes in a row from the dashboard server.",
      "started": 1792810000.0, "resolved": 1792810600.0,
      "resolved_title": "Homarr is back up", "resolved_message": "Down for 10 min." }
  ]
}
```
`kind` is one of `offline`, `cpu`, `temp`, `gputemp`, `disk`, `service`, or `digest` (the daily update summary, logged with severity `info`).

### 3.7 Notification Settings
- **Endpoints**: `GET /api/settings/notifications` and `PUT /api/settings/notifications`
- **Auth Required**: Yes (`Bearer <token>`)
- **PUT body** (any subset):
```json
{
  "ntfy": { "enabled": true, "url": "http://192.168.0.105:7070/homelab", "token": "tk_..." },
  "click_url": "http://192.168.0.107:8000/alerts.html",
  "digest_hour": 9
}
```
`url` is the topic URL. An absent or `null` `token` keeps the stored one and `""` removes it. `digest_hour` is 0-23, or `null` for no daily summary. Invalid values get `400`.
- **Response** (both): the settings without the token, `{ "ntfy": { "url", "enabled", "has_token" }, "click_url", "digest_hour", "status" }`, where `status` is the outcome of the last delivery, `{ "ok", "at", "error" }`, or `null`.

### 3.8 Send a Test Notification
- **Endpoint**: `POST /api/notifications/test`
- **Auth Required**: Yes (`Bearer <token>`)
- **Body**: `{ "url": "...", "token": "..." }`, both optional; missing ones fall back to the saved settings, so a topic can be tried before saving.
- **Response**: `{ "ok": true }`, or `{ "ok": false, "error": "ntfy is unreachable (...)" }`.

---

## 4. Real-Time Streaming (SSE)

### 4.1 Server-Sent Events Stream
Establishes a persistent, unidirectionally streamed HTTP connection.

- **Endpoint**: `GET /api/stream`
- **Auth Required**: Query parameter `?token=<session_token>`
- **Content-Type**: `text/event-stream`
- **Keep-Alive Ping**: `: ping\n\n` sent every 10 seconds if no events occur.

#### Event Types

##### `init`
Sent immediately upon SSE connection, including every reconnect. Contains an array of all known device records, including offline nodes restored from the database after a server restart.
```text
event: init
data: [{"hostname": "falcon-nas", "ip": "192.168.1.105", "online": true, ...}, {"hostname": "gaming-rig", ...}]
```

##### `metrics`
Broadcast whenever a client node posts new metrics or whenever the latency sweeper updates ping times.
```text
event: metrics
data: {"hostname": "falcon-nas", "cpu_usage": 15.2, "temp": 49.0, "online": true, "latency": 1.1, ...}
```

##### `removed`
Broadcast when a node is removed with `DELETE /api/devices/{hostname}`.
```text
event: removed
data: {"hostname": "old-nas"}
```

##### `disk_alerts`
Broadcast after a disk's alert threshold changes, with every override (same shape as `GET /api/settings/disk-alerts`).

##### `services_init`
Sent upon initial connection containing the full list of monitored services.
```text
event: services_init
data: [{"name": "Jellyfin", "category": "Media", "online": true, "latency": 3.4}, ...]
```

##### `alerts`
Sent on connection and whenever an alert fires or resolves: the full list of active alerts (same shape as `active` in `GET /api/alerts`).

##### `services`
Broadcast every 30 seconds after the background service probe completes.
```text
event: services
data: [{"name": "Jellyfin", "category": "Media", "online": true, "latency": 3.4}, ...]
```
