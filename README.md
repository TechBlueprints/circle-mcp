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
| `list_profiles` | Family profiles (pid, name, type) |
| `get_usage` | Detailed usage for a profile (`day`=days ago, `ndays`=span) |
| `get_usage_categories` | Usage by category (games, social, video, ...) |
| `get_circle_status` | Circle device/account status |

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
```

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
   `{"deviceid": <device_id>, "jwt": <access_token>}` → admin token
3. Data endpoints on `https://vc.meetcircle-blue.co` with `Authorization: Bearer <admin_token>`:
   - `/api/LOGS/usage/categories?user=<pid>&day=<d>&ndays=<n>` (verified live)
   - `/api/LOGS/usage?user=<pid>&day=<d>&ndays=<n>`
   - `/api/QUERY/users`, `/api/QUERY/circleinfo` (unverified)

Error semantics on signin:

- `401 {"message": "NoCredential"}` — missing/invalid `Authorization: Public <key>` header
- `401 {"message": "InvalidCredentials"}` — key accepted, username/password rejected
