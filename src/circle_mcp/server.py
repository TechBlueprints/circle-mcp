"""
MCP server (stdio) exposing Circle parental-control data.

Configuration, in order of precedence:

1. Environment variables: CIRCLE_USERNAME, CIRCLE_PASSWORD, CIRCLE_DEVICE_ID,
   CIRCLE_AURA_PUBLIC_KEY, CIRCLE_ADMIN_TOKEN, CIRCLE_PROFILE_PID
2. A secrets file with `circle.<key> = <value>` lines (the airstream-dashboard
   secrets.conf format), located via CIRCLE_SECRETS_FILE.

Run: circle-mcp   (or: python -m circle_mcp.server)
"""

import os
from typing import Optional

from mcp.server import MCPServer

from .provider import CircleClient

_SECRET_KEYS = ("username", "password", "device_id", "aura_public_key", "admin_token", "profile_pid")

_client = None


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
        raise RuntimeError(
            "Circle login failed (the API rejected the stored credentials — "
            "check the circle username/password/aura_public_key)"
        )
    return _client


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
    return _circle().list_profiles()


@server.tool()
def get_usage(profile_pid: Optional[str] = None, day: str = "0", ndays: str = "1") -> dict:
    """Get detailed internet usage for a profile.

    Args:
        profile_pid: Profile ID from list_profiles (falls back to configured profile_pid)
        day: Days ago to start (0 = today)
        ndays: Number of days to include
    """
    return _circle().get_profile_usage(profile_pid=profile_pid, day=day, ndays=ndays)


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
    return _circle().get_usage_categories(profile_pid=profile_pid, day=day, ndays=ndays)


@server.tool()
def get_circle_status() -> dict:
    """Get Circle device/account status."""
    result = _circle().get_circle_status()
    return result if result is not None else {"error": "Failed to fetch Circle status"}


def main():
    server.run("stdio")


if __name__ == "__main__":
    main()
