# circle-mcp

MCP server exposing read-only [Circle](https://meetcircle.com) (Meetcircle/Aura)
parental-control data: family profiles and per-profile internet usage. Runs over
stdio (default) or streamable HTTP behind bearer keys (see [HTTP / container](#http--container)).

Extracted from the airstream-dashboard Circle provider, which is where the API
client and its auth flow come from. Stdlib-only client; the only dependency is
the `mcp` SDK.

## Tools

| Tool | Purpose |
|------|---------|
| `list_profiles` | Family profiles (pid, name, mode, ageCategory, role, avatarUrl) |
| `get_usage` | Detailed usage for a profile (`day`=days ago, `ndays`=span) |
| `get_usage_categories` | Usage by category (games, social, video, ...) |
| `get_circle_status` | Circle-wide status (name, mode, enabled, timezone, firmware) |
| `get_circle_snapshot` | Everything in one call; `section` = `overall`, `users`, `devices`, `hardware` or `categories` for one part. Hotspot passwords are removed |
| `list_devices` | Devices on the network (uid/MAC, names, ip, maker, mode, assigned pid, `isGo`) |
| `list_hardware` | Circle Home units (cuuid, model, online/enabled, network, Wi-Fi, firmware); `check_online=true` adds a live online check per unit |
| `list_categories` | Filter category catalog (id, name, mainCategory, description), to name the ids in usage reports |
| `list_go_devices` | Child phones/tablets running the Circle app: counts plus each device's id, state, last check-in |
| `get_profile_badges` | Badges the Circle app shows per profile, `[{pid, badges}]` |
| `get_usage_by_site` | Usage by site for a profile (raw Circle report) |
| `get_history` | Allowed or blocked sites visited (`kind`), flattened: site, time, state, category, device uid |
| `get_time_remaining` | Minutes left today per limited category (limit + today's extensions − used), paused / limits-on flags |

### Profile writes (need `CIRCLE_MCP_ALLOW_WRITES`)

These change the Circle and are only registered when `CIRCLE_MCP_ALLOW_WRITES`
is `1`/`true`/`yes`/`on`; with it unset the server stays read-only. Each returns
the raw Circle response as `response` plus an `ok` flag.

| Tool | Purpose |
|------|---------|
| `pause_profile` / `resume_profile` | Pause a profile's internet / go back to normal filtering |
| `create_profile` | New profile (`name`, `age_category`, `mode`); returns its `pid` |
| `rename_profile` | Change a profile's name |
| `set_profile_age_category` | `Pre-K`, `Kid`, `Teen`, `Adult` or `None` |
| `set_profile_toggles` | Turn time limits and/or off times `On`/`Off` |
| `delete_profile` | Delete a profile; needs `confirm=true`, refuses the built-in `0` (Home) and `1` (Unmanaged) |
| `assign_device_to_profile` | Move a device (by MAC) onto a profile |
| `set_category_switches` | Per-category `On`/`Off`/`Unmanaged` overrides; merged into the current set unless `replace=true` |
| `set_platform_switches` | Same for platforms (apps); merged unless `replace=true` |
| `remove_platform_switch` | Drop one platform override |
| `set_custom_site` | Always allow (`On`) or block (`Off`) a site; other custom sites are kept |
| `remove_custom_site` | Drop a site from the custom list |
| `list_bedtimes` | Bedtimes for every profile |
| `list_focus_times` | Focus times (homework hours etc.) for every profile |
| `get_focus_time_defaults` | Default filter sets for a new focus time |
| `set_time_limits` ✏️ | Replace a profile's daily time limits and weekend days |
| `add_reward_minutes` ✏️ | Extra minutes today in one category (keeps today's other rewards) |
| `extend_off_time_today` ✏️ | Move today's start/end of one off time (e.g. later bedtime tonight) |
| `set_off_times` ✏️ | Replace a profile's legacy off-time windows |
| `add_bedtime` / `update_bedtime` ✏️ | Create or replace a bedtime |
| `add_focus_time` / `update_focus_time` / `delete_focus_time` ✏️ | Create, replace, or remove a focus time |
✏️ = changes settings on the Circle; registered only when `CIRCLE_MCP_ALLOW_WRITES`
is `1`/`true`/`yes`/`on`. Days are weekday names or indices with Sunday = 0;
times are 24-hour `HH:mm`.
| `get_subscription_status` | Subscription plan, expiry, renewal and trial flags (needs username/password, not just an admin token) |
| `lookup_domain_category` | Circle category id(s) for a domain or URL |
Write tools register only when `CIRCLE_MCP_ALLOW_WRITES` is `1`/`true`/`yes`/`on`. Each returns
`{"ok": bool, "response": <Circle's reply>}`.
| Write tool | Purpose |
|------------|---------|
| `rename_device` | Rename a device (`mac` = its uid) |
| `pause_device` / `resume_device` | Cut / restore one device's internet |
| `forget_device` | Remove a device from Circle (needs `confirm=true`) |
| `pause_home` / `resume_home` | Pause / resume the internet for the whole home |
| `set_circle_timezone` | Circle's time zone (IANA id, validated) |
| `set_circle_locale` | Circle's language (`en`, `es`, ...) |
| `set_default_profile` | Profile newly seen devices land in |
| `dismiss_notification` | Clear an active dashboard notification |
| `set_hardware_enabled` | Enable / disable a Circle Home unit (`cuuid`) |
| `set_hardware_compatible_mode` | Toggle a Circle Home unit's compatible mode |
| `reboot_hardware` | Reboot a Circle Home unit (needs `confirm=true`) |
| `request_child_location` | Ask a child's phone to report its location; the answer goes to the parent app by push |
| `go_device_status` | Enrolled Circle Go child device: stage, ids, last ping, managed flag ([Child-device mode](#child-device-mode)) |
| `go_enroll_start` ✍️ | Announce a new child device and request a pairing passcode |
| `go_enroll_approve` ✍️ | Approve the pending device from the parent account |
| `go_enroll_finish` ✍️ | Claim the device token and describe the device (home Wi-Fi) |
| `go_ping` ✍️ | Check in as the device; reports whether the parent still manages it |
| `go_download_settings` ✍️ | Download and unpack the device's rule bundle |
| `go_probe_user_info` ✍️ | Probe the four undocumented user-info endpoints (status + body each) |
✍️ = registered only when `CIRCLE_MCP_ALLOW_WRITES` is truthy (`1`, `true`, `yes`, `on`).

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
```

Tests (stubbed HTTP, no credentials needed):

```bash
.venv/bin/pip install -e '.[dev]' && .venv/bin/python -m pytest
```
Tests: `.venv/bin/pip install -e '.[dev]'` then `.venv/bin/python -m pytest`.

## Configuration

Environment variables win; a secrets file fills the rest:

| Env var | Secrets-file key | Purpose |
|---------|------------------|---------|
| `CIRCLE_USERNAME` | `circle.username` | Account email |
| `CIRCLE_PASSWORD` | `circle.password` | Account password |
| `CIRCLE_DEVICE_ID` | `circle.device_id` | Stable device identifier (any unique string) |
| `CIRCLE_AURA_PUBLIC_KEY` | `circle.aura_public_key` | Aura API client key (`circle_mobile_auth:...`) |
| `CIRCLE_ADMIN_TOKEN` | `circle.admin_token` | Optional pre-authenticated token (bypasses login) |
| `CIRCLE_PROFILE_PID` | `circle.profile_pid` | Optional default profile for usage tools |
| `CIRCLE_VC_HOST` | `circle.vc_host` | Optional VC API host (`vc.example.com` or a full `https://` URL). Skips the per-circle host lookup |

`CIRCLE_SECRETS_FILE` points at a conf file with `circle.<key> = <value>` lines,
which keeps credentials out of your MCP client config.

Claude Code registration: copy `.mcp.json.example` to `.mcp.json` and fill in your
credentials (or point `CIRCLE_SECRETS_FILE` at a conf file holding them). `.mcp.json`
is gitignored, since it typically embeds credentials or machine-specific paths.

## HTTP / container

`--transport streamable-http` (or `CIRCLE_MCP_TRANSPORT=streamable-http`) serves MCP at
`/mcp` with stateless JSON responses, plus an unauthenticated `GET /health`.

| Env var | Default | Purpose |
|---------|---------|---------|
| `CIRCLE_MCP_TRANSPORT` | `stdio` | `stdio` or `streamable-http` (`--transport`) |
| `CIRCLE_MCP_HOST` / `CIRCLE_MCP_PORT` | `0.0.0.0` / `8080` | Bind address (`--host`, `--port`) |
| `CIRCLE_MCP_KEYS` | (none) | Comma list of accepted bearer keys, each `key` or `id:key`. **Required**: the HTTP transport refuses to start without one |
| `CIRCLE_MCP_ALLOWED_HOSTS` | localhost only | Comma list of accepted `Host` headers (DNS-rebinding protection, 421 otherwise); `*` turns the check off |
| `CIRCLE_MCP_AUTH_BACKOFF_S` | `900` | After Circle rejects the sign-in, refuse further sign-in attempts for this long (repeated bad logins could lock the account) |

Every request except `/health` needs `Authorization: Bearer <key>`; anything else gets 401.
Keys are compared in constant time; the `id` part only names the key in the startup log.

Circle failures (not configured, sign-in rejected, API errors) come back to the client as
tool errors with the reason; after a rejected sign-in `/health` also shows `circle_auth_error`.
The backoff applies to every transport.

Container (`Containerfile`: python:3.13-slim, non-root uid 10001, HEALTHCHECK on `/health`,
`CIRCLE_SECRETS_FILE=/config/secrets.conf`):

```bash
podman build --format docker -t localhost/circle-mcp:latest .
podman run -d --name circle-mcp --restart unless-stopped \
  -e CIRCLE_MCP_KEYS="client1:$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')" \
  -e CIRCLE_MCP_ALLOWED_HOSTS="localhost:8080,127.0.0.1:8080,myhost.example:3005" \
  -p 3005:8080 -v /path/to/secrets.conf:/config/secrets.conf:ro,Z \
  localhost/circle-mcp:latest
```

`--format docker` matters: podman's default OCI format drops `HEALTHCHECK`. The secrets file
only needs the `circle.*` lines from [Configuration](#configuration).

Client registration (Claude Code `.mcp.json` or any streamable-HTTP client):

```json
{"type": "http", "url": "http://myhost.example:3005/mcp",
 "headers": {"Authorization": "Bearer <key>"}}
```

## Child-device mode

Besides the parent API, Circle has a separate API for managed phones and tablets
running the Circle app in Kid mode ("Circle Go", [PROTOCOL.md](PROTOCOL.md) Part C).
The `go_*` tools let this server enroll itself as such a device. That creates a
real device in the parent's Circle, so everything except `go_device_status` needs
`CIRCLE_MCP_ALLOW_WRITES`.

Enrollment:

1. `go_enroll_start(device_name)` announces the device and asks for a pairing passcode.
2. The parent approves it: `go_enroll_approve` does it with the parent account this
   server is configured for, or the parent types the passcode into their Circle app.
3. `go_enroll_finish(home_ssid)` collects the device token and registers the device's
   home Wi-Fi name. `go_device_status` should then show `stage: enrolled`.

After that, `go_ping` checks in (`managed: false` means the parent removed the device),
`go_download_settings` fetches the rule bundle (three opaque binaries for Circle's filter
engine, not readable rules), and `go_probe_user_info` reports what the four declared but
unused user-info endpoints return. The device token renews itself on a 401; if the
renewal says management was turned off, device calls stop and the status says `unmanaged`.
Run `go_enroll_start` with `replace=true` to start over.

| Env var | Default | Purpose |
|---------|---------|---------|
| `CIRCLE_GO_STATE_FILE` | `~/.circle-mcp/go-device.json` | Device identity and tokens (written mode 0600; keep it private) |
| `CIRCLE_GO_SETTINGS_DIR` | `go-settings/` next to the state file | Where `go_download_settings` writes the bundle |

In a container, mount a writable volume for the state file, or the enrollment is lost on restart.

## Deployment runbook

How to install, run, talk to, update and retire the HTTP server. Hostnames, ports and keys
below are examples; a private ops repo normally wraps these steps in a script (ours is
`home-setup/scripts/install-circle-mcp-on-podman.sh`, which reads the key and the `circle.*`
lines from a secrets repo and does everything in "Install" and "Update" idempotently).

### Install (first time, on the container host)

1. Put the Circle credentials in a file the container can read, `circle.*` lines only:
   ```ini
   circle.username = you@example.com
   circle.password = ...
   circle.device_id = circle-mcp
   circle.aura_public_key = circle_mobile_auth:...
   ```
   The container runs as uid 10001, so the file must be readable by that uid
   (`chown 10001 secrets.conf && chmod 0400 secrets.conf`).
2. Generate one bearer key per client: `python3 -c 'import secrets;print(secrets.token_urlsafe(32))'`.
   Record it with the client's name in your secrets store; it is not recoverable from the server.
3. Ship the source and build (from a checkout, `HEAD` or a tag):
   ```bash
   REV=$(git rev-parse --short HEAD)
   git archive --format=tar HEAD | ssh root@host "mkdir -p /root/circle-mcp-src-$REV && tar -x -C /root/circle-mcp-src-$REV"
   ssh root@host "cd /root/circle-mcp-src-$REV && podman build --format docker -t localhost/circle-mcp:$REV . && podman tag localhost/circle-mcp:$REV localhost/circle-mcp:latest"
   ```
4. Run it (see [HTTP / container](#http--container) for every env var). `CIRCLE_MCP_ALLOWED_HOSTS`
   must list every `Host:` header a client will send, with and without the port, or the client
   gets HTTP 421. Open the port in the host firewall (`firewall-cmd --add-port=3005/tcp --permanent`).
5. Check: `curl -s http://host:3005/health` → `{"status":"ok","circle_configured":true,"mcp_keys":1}`.

### Talk to it

```bash
# list tools (expect 4)
curl -s -X POST http://host:3005/mcp -H 'Authorization: Bearer <key>' \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
# call one
curl -s -X POST http://host:3005/mcp -H 'Authorization: Bearer <key>' \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"list_profiles","arguments":{}}}'
```

Responses are plain JSON (stateless; no session header needed). Register the server in a client as
shown above; for a Hermes Agent gateway:

```yaml
mcp_servers:
  circle:
    url: http://host:3005/mcp
    headers:
      Authorization: Bearer ${CIRCLE_MCP_KEY}   # resolved from the gateway's env at connect time
    tools: {resources: false, prompts: false}
```

### Read the logs and understand the errors

- `podman logs --tail 50 circle-mcp`: one line per request plus the key id that authenticated.
- `401 unauthorized`: no or wrong bearer key. `421 Misdirected Request`: the `Host:` header is not in
  `CIRCLE_MCP_ALLOWED_HOSTS`. Both are the server, not Circle.
- A tool error `Aura sign-in failed: HTTP 401 {"message":"InvalidCredentials"}` is Circle rejecting
  the account email/password (the API key was accepted); `NoCredential` means the
  `aura_public_key` is wrong. After one rejection the server refuses to retry for
  `CIRCLE_MCP_AUTH_BACKOFF_S` (15 min) and `/health` shows `circle_auth_error`; fix the
  credentials file and restart the container to clear it. Repeated bad logins can lock the
  Circle account, so do not loop.

### Update (new code or new credentials)

Rebuild with a new `REV` as in Install, then recreate the container with the same `podman run`
line (`podman stop -t 10 circle-mcp && podman rm circle-mcp` first). Keep the previous image tag
for rollback. Changing only `secrets.conf` or the env file needs just the stop/rm/run.

### Rotate a bearer key

Generate a new key, replace it in `CIRCLE_MCP_KEYS` (several keys may be listed during the
switch), recreate the container, update the client, then drop the old key and recreate again.
Clients holding the old key get 401 immediately.

### Retire

`podman stop circle-mcp && podman rm circle-mcp`, remove the firewall port, delete the secrets
file and the `/root/circle-mcp-src-*` directories, and revoke the keys in the clients.

## API notes (2026-08)

The full endpoint catalog (parent and child-device surfaces, with MCP coverage)
is in [PROTOCOL.md](PROTOCOL.md). The notes below cover only what the server
implements today.


Auth flow:

1. `POST https://api.aurasvc.io/auth/session/signin` with header
   `Authorization: Public <aura_public_key>` and body `{"alias": <email>, "password": ...}`
   → `access_token` + `refresh_token` (refresh via `/auth/session/refresh_token`)
2. `POST https://auth.meetcircle-blue.co/api/v2/grant/admin` with
   `{"deviceid": <device_id>, "jwt": <access_token>}` → admin token and `circleid`
3. After each fresh grant, `GET https://register.meetcircle-blue.co/api/v1.0/host?circleid=<id>&token=<admin_token>`;
   a non-empty `host` replaces the default VC host. Any failure keeps the default
   (`vc.meetcircle-blue.co`); `CIRCLE_VC_HOST` skips the lookup.
4. Data endpoints on the VC host with `Authorization: Bearer <admin_token>`:
   - `/api/LOGS/usage/categories?user=<pid>&day=<d>&ndays=<n>` (verified live)
   - `/api/LOGS/usage?user=<pid>&day=<d>&ndays=<n>`
   - `/api/QUERY/all` (profiles and status), `/api/QUERY/devices`, `/api/QUERY/hardware`,
     `/api/QUERY/hardware/hardware/isOnline`, `/api/QUERY/categories`, `/api/v2/categories`,
     `/api/v2/profiles/badges`
   - `/api/QUERY/users`, `/api/QUERY/circleinfo`: fallbacks when `/api/QUERY/all` fails
   - `/api/v0.1/c/devStatus?gotoken=<admin_token>` on port **8995** of the same host

Error semantics on signin:

- `401 {"message": "NoCredential"}` — missing/invalid `Authorization: Public <key>` header
- `401 {"message": "InvalidCredentials"}` — key accepted, username/password rejected
