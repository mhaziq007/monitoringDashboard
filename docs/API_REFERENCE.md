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
    { "name": "immich_server", "state": "Running" },
    { "name": "jellyfin", "state": "Running" },
    { "name": "postgres", "state": "Running" }
  ],
  "pending_updates": 3,
  "is_vm": false
}
```
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

### 3.3 Power Draw History
Returns time-series lab power wattage downsampled based on the requested window.

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
  { "time": 1725163200.0, "power": 38.2 },
  { "time": 1725163500.0, "power": 41.5 },
  { "time": 1725163800.0, "power": 39.0 }
]
```

---

### 3.4 Services Status
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

## 4. Real-Time Streaming (SSE)

### 4.1 Server-Sent Events Stream
Establishes a persistent, unidirectionally streamed HTTP connection.

- **Endpoint**: `GET /api/stream`
- **Auth Required**: Query parameter `?token=<session_token>`
- **Content-Type**: `text/event-stream`
- **Keep-Alive Ping**: `: ping\n\n` sent every 10 seconds if no events occur.

#### Event Types

##### `init`
Sent immediately upon SSE connection. Contains an array of all known device records.
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

##### `services_init`
Sent upon initial connection containing the full list of monitored services.
```text
event: services_init
data: [{"name": "Jellyfin", "category": "Media", "online": true, "latency": 3.4}, ...]
```

##### `services`
Broadcast every 30 seconds after the background service probe completes.
```text
event: services
data: [{"name": "Jellyfin", "category": "Media", "online": true, "latency": 3.4}, ...]
```
