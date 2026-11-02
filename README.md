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

Every request except `/health` needs `Authorization: Bearer <key>`; anything else gets 401.
Keys are compared in constant time; the `id` part only names the key in the startup log.

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

## API notes (2026-08)

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
