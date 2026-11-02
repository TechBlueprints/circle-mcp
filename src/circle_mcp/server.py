"""
MCP server exposing Circle parental-control data (stdio or streamable HTTP).

Circle configuration, in order of precedence:

1. Environment variables: CIRCLE_USERNAME, CIRCLE_PASSWORD, CIRCLE_DEVICE_ID,
   CIRCLE_AURA_PUBLIC_KEY, CIRCLE_ADMIN_TOKEN, CIRCLE_PROFILE_PID
2. A secrets file with `circle.<key> = <value>` lines (the airstream-dashboard
   secrets.conf format), located via CIRCLE_SECRETS_FILE.

Transport (CLI flags win over env):

- `--transport stdio|streamable-http` / CIRCLE_MCP_TRANSPORT (default stdio)
- `--host` / CIRCLE_MCP_HOST, `--port` / CIRCLE_MCP_PORT (HTTP; default 0.0.0.0:8080)
- CIRCLE_MCP_KEYS: comma list of accepted bearer keys, each `key` or `id:key`.
  The HTTP transport refuses to start without at least one.
- CIRCLE_MCP_ALLOWED_HOSTS: comma list of allowed Host headers (DNS-rebinding
  protection), or `*` to turn the check off. Unset = localhost only.

HTTP serves MCP at /mcp (every request needs `Authorization: Bearer <key>`)
and an unauthenticated GET /health.

Run: circle-mcp   (or: python -m circle_mcp.server)
"""

import argparse
import hmac
import json
import logging
import os
import threading
import time
import urllib.error
from typing import Optional

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from .provider import CircleAuthError, CircleClient
from .tools import usage as usage_tools

log = logging.getLogger("circle_mcp")

_SECRET_KEYS = ("username", "password", "device_id", "aura_public_key", "admin_token", "profile_pid")

_client = None
# Sync tools run in worker threads under HTTP; the client's token state is not
# thread-safe, so Circle calls are serialized.
_lock = threading.Lock()
# After a rejected sign-in, do not try again for a while: repeated bad logins
# could lock the Circle account. CIRCLE_MCP_AUTH_BACKOFF_S, default 15 minutes.
_AUTH_BACKOFF_S = int(os.environ.get("CIRCLE_MCP_AUTH_BACKOFF_S", "900"))
_auth_error = ""
_auth_retry_at = 0.0


def _read_secrets_file(path: str) -> dict:
    """Parse `circle.<key> = <value>` lines; ignores sections, comments, other keys."""
    values = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith(("#", ";", "[")) or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                if key.startswith("circle."):
                    values[key[len("circle."):]] = value.strip()
    except OSError:
        pass
    return values


def _load_config() -> dict:
    values = {}
    secrets_file = os.environ.get("CIRCLE_SECRETS_FILE", "")
    if secrets_file:
        values.update(_read_secrets_file(secrets_file))
    for key in _SECRET_KEYS:
        env = os.environ.get(f"CIRCLE_{key.upper()}", "")
        if env:
            values[key] = env
    return values


def _circle() -> CircleClient:
    """Build (once) and return the CircleClient, or raise with a clear message."""
    global _client
    if _client is None:
        cfg = _load_config()
        client = CircleClient(
            username=cfg.get("username", ""),
            password=cfg.get("password", ""),
            device_id=cfg.get("device_id", "circle-mcp"),
            aura_public_key=cfg.get("aura_public_key", ""),
            admin_token=cfg.get("admin_token", ""),
            default_profile_pid=cfg.get("profile_pid", ""),
        )
        if not client.is_configured:
            raise RuntimeError(
                "Circle is not configured: set CIRCLE_USERNAME/CIRCLE_PASSWORD "
                "(or CIRCLE_ADMIN_TOKEN) via env, or point CIRCLE_SECRETS_FILE at a "
                "secrets.conf with circle.* keys"
            )
        _client = client
    if _client.ensure_admin_token() is None:
        raise CircleAuthError(
            "Circle login failed (the API rejected the stored credentials — "
            "check the circle username/password/aura_public_key)"
        )
    return _client


def _call(method: str, **kwargs):
    """Run one CircleClient call under the lock; anticipated failures become ToolErrors."""
    global _auth_error, _auth_retry_at
    with _lock:
        now = time.time()
        if _auth_error and now < _auth_retry_at:
            raise ToolError(
                f"{_auth_error} (not retrying sign-in until "
                f"{time.strftime('%H:%M:%S %Z', time.localtime(_auth_retry_at))} to avoid locking the account)"
            )
        try:
            result = getattr(_circle(), method)(**kwargs)
        except CircleAuthError as e:
            _auth_error, _auth_retry_at = str(e), now + _AUTH_BACKOFF_S
            raise ToolError(str(e)) from e
        except (RuntimeError, ValueError) as e:
            raise ToolError(str(e)) from e
        except urllib.error.HTTPError as e:
            raise ToolError(f"Circle API returned HTTP {e.code} for {method}") from e
        except (urllib.error.URLError, OSError) as e:
            raise ToolError(f"Circle API unreachable: {getattr(e, 'reason', e)}") from e
        _auth_error, _auth_retry_at = "", 0.0
        return result


server = MCPServer(
    name="circle",
    instructions=(
        "Read-only access to Circle (Meetcircle/Aura) parental controls: family "
        "profiles and per-profile internet usage. Days are relative: day=0 is "
        "today, day=1 yesterday; ndays is how many days to include."
    ),
)


@server.tool()
def list_profiles() -> list:
    """List Circle family profiles (pid, name, type). Use the pid with the usage tools."""
    return _call("list_profiles")


@server.tool()
def get_usage(profile_pid: Optional[str] = None, day: str = "0", ndays: str = "1") -> dict:
    """Get detailed internet usage for a profile.

    Args:
        profile_pid: Profile ID from list_profiles (falls back to configured profile_pid)
        day: Days ago to start (0 = today)
        ndays: Number of days to include
    """
    return _call("get_profile_usage", profile_pid=profile_pid, day=day, ndays=ndays)


@server.tool()
def get_usage_categories(
    profile_pid: Optional[str] = None, day: str = "0", ndays: str = "1"
) -> dict:
    """Get internet usage broken down by category (e.g. games, social, video) for a profile.

    Args:
        profile_pid: Profile ID from list_profiles (falls back to configured profile_pid)
        day: Days ago to start (0 = today)
        ndays: Number of days to include
    """
    return _call("get_usage_categories", profile_pid=profile_pid, day=day, ndays=ndays)


@server.tool()
def get_circle_status() -> dict:
    """Get Circle device/account status."""
    result = _call("get_circle_status")
    return result if result is not None else {"error": "Failed to fetch Circle status"}


usage_tools.register(server, _call, _lock)


# -----------------------------
# Streamable HTTP transport
# -----------------------------

_PUBLIC_PATHS = ("/health",)


def _parse_keys(raw: str) -> dict:
    """`key` or `id:key` items, comma separated -> {key: id}."""
    keys = {}
    for n, item in enumerate((i.strip() for i in raw.split(",")), start=1):
        if not item:
            continue
        key_id, sep, key = item.partition(":")
        if not sep:
            key_id, key = f"key{n}", item
        key = key.strip()
        if key:
            keys[key] = key_id.strip() or f"key{n}"
    return keys


def _transport_security(raw: str):
    """Host-header allow-list for DNS-rebinding protection (see module docstring)."""
    from mcp.server.transport_security import TransportSecuritySettings

    hosts = [h.strip() for h in raw.split(",") if h.strip()]
    if "*" in hosts:
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)
    if not hosts:
        hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
    origins = [f"{scheme}://{h}" for h in hosts for scheme in ("http", "https")]
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,
        allowed_origins=origins,
    )


class BearerKeyMiddleware:
    """Pure-ASGI guard: every HTTP request outside the public paths needs a known key."""

    def __init__(self, app, keys: dict, public_paths=_PUBLIC_PATHS):
        self.app = app
        self._keys = keys
        self._public = public_paths

    def _lookup(self, token: str) -> Optional[str]:
        found = None
        for key, key_id in self._keys.items():
            # compare against every key so timing does not reveal which one matched
            if hmac.compare_digest(token.encode(), key.encode()):
                found = key_id
        return found

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if any(path == p or path.startswith(p + "/") for p in self._public):
            await self.app(scope, receive, send)
            return

        token = None
        for name, value in scope.get("headers", []):
            if name == b"authorization":
                text = value.decode("latin-1").strip()
                if text[:7].lower() == "bearer ":
                    token = text[7:].strip()
                break

        if token and self._lookup(token) is not None:
            await self.app(scope, receive, send)
            return

        detail = "invalid key" if token else "missing Authorization: Bearer <key>"
        body = json.dumps({"error": "unauthorized", "detail": detail}).encode()
        await send({
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"www-authenticate", b'Bearer realm="circle-mcp"'),
                (b"content-length", str(len(body)).encode()),
            ],
        })
        await send({"type": "http.response.body", "body": body})


def build_http_app(keys: dict, allowed_hosts: str = ""):
    """Starlette app for streamable HTTP, wrapped in the bearer-key guard."""
    from starlette.responses import JSONResponse

    async def health(_request):
        cfg = _load_config()
        configured = bool(cfg.get("admin_token") or (cfg.get("username") and cfg.get("password")))
        body = {"status": "ok", "circle_configured": configured, "mcp_keys": len(keys)}
        if _auth_error:
            body["circle_auth_error"] = _auth_error
        return JSONResponse(body)

    app = server.streamable_http_app(
        stateless_http=True,
        json_response=True,
        transport_security=_transport_security(allowed_hosts),
        host="0.0.0.0",
    )
    app.router.add_route("/health", health, methods=["GET"])
    return BearerKeyMiddleware(app, keys)


def _run_http(host: str, port: int) -> None:
    import uvicorn

    keys = _parse_keys(os.environ.get("CIRCLE_MCP_KEYS", ""))
    if not keys:
        raise SystemExit(
            "circle-mcp: the streamable-http transport needs at least one bearer key "
            "in CIRCLE_MCP_KEYS (comma list of `key` or `id:key`); refusing to start"
        )
    allowed = os.environ.get("CIRCLE_MCP_ALLOWED_HOSTS", "")
    app = build_http_app(keys, allowed)
    log.warning(
        "circle-mcp streamable-http on %s:%s path=/mcp keys=%s allowed_hosts=%s",
        host, port, ",".join(sorted(keys.values())), allowed or "localhost-only",
    )
    uvicorn.run(app, host=host, port=port, log_level="info")


def main():
    parser = argparse.ArgumentParser(prog="circle-mcp", description=__doc__.split("\n\n")[0].strip())
    parser.add_argument(
        "--transport",
        choices=("stdio", "streamable-http", "http"),
        default=os.environ.get("CIRCLE_MCP_TRANSPORT", "stdio"),
        help="stdio (default) or streamable-http (env CIRCLE_MCP_TRANSPORT)",
    )
    parser.add_argument("--host", default=os.environ.get("CIRCLE_MCP_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("CIRCLE_MCP_PORT", "8080")))
    args = parser.parse_args()
    if args.transport not in ("stdio", "streamable-http", "http"):
        parser.error(f"unknown transport {args.transport!r} (CIRCLE_MCP_TRANSPORT)")

    if args.transport == "stdio":
        server.run("stdio")
    else:
        _run_http(args.host, args.port)


if __name__ == "__main__":
    main()
