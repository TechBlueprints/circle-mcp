"""
Tools for devices, circle-wide settings, Circle Home hardware, subscription
status, child location requests, and domain categories (PROTOCOL.md B.5, B.6).

Read tools always register. Write tools register only when
CIRCLE_MCP_ALLOW_WRITES is truthy; each returns {"ok": bool, "response": ...}.
"""

import os
import re
import urllib.parse
import zoneinfo
from typing import Optional

from mcp.server.mcpserver.exceptions import ToolError

_TRUTHY = ("1", "true", "yes", "on")
_LOCALE_RE = re.compile(r"^[A-Za-z]{2,3}([-_][A-Za-z0-9]{2,8})*$")


def writes_enabled() -> bool:
    return os.environ.get("CIRCLE_MCP_ALLOW_WRITES", "").strip().lower() in _TRUTHY


def _required(value: Optional[str], what: str) -> str:
    value = (value or "").strip()
    if not value:
        raise ToolError(f"{what} is required")
    return value


def _check_timezone(tz: str) -> str:
    tz = _required(tz, "tz")
    known = zoneinfo.available_timezones()
    # A host without a zone database knows no zones at all; let Circle judge then.
    if known and tz not in known:
        raise ToolError(f"unknown IANA time zone {tz!r} (e.g. America/Denver)")
    return tz


def _domain_of(text: str) -> str:
    text = _required(text, "domain")
    host = urllib.parse.urlsplit(text if "://" in text else f"//{text}").hostname
    if not host:
        raise ToolError(f"cannot find a domain in {text!r}")
    return host


def register(server, circle, lock) -> None:
    """Add this module's tools. `circle(method, **kwargs)` runs a CircleClient method
    with the server's locking and error mapping; `lock` is unused here (the invoker holds it)."""

    @server.tool()
    def get_subscription_status() -> dict:
        """Circle subscription for the signed-in account: status, plan, expiry, auto-renew, trial flags."""
        return circle("get_subscription")

    @server.tool()
    def lookup_domain_category(domain: str) -> dict:
        """Which Circle category a domain falls under (the catid used in usage reports and filters).

        Args:
            domain: A domain like youtube.com; a full URL is accepted and reduced to its host
        """
        host = _domain_of(domain)
        raw = circle("lookup_domain_category", domain=host)
        results = raw.get("results") if isinstance(raw, dict) else None
        catids = [
            str(r.get("catid")) for r in results or []
            if isinstance(r, dict) and r.get("catid") is not None
        ]
        return {"domain": host, "catids": catids, "response": raw}

    if not writes_enabled():
        return

    @server.tool()
    def rename_device(mac: str, name: str) -> dict:
        """Change the display name of a device on the home network.

        Args:
            mac: Device uid (its MAC address)
            name: New display name
        """
        return circle("rename_device", uid=_required(mac, "mac"), name=_required(name, "name"))

    @server.tool()
    def pause_device(mac: str) -> dict:
        """Cut one device's internet access until resumed.

        Args:
            mac: Device uid (its MAC address)
        """
        return circle("set_device_mode", uid=_required(mac, "mac"), mode="Pause")

    @server.tool()
    def resume_device(mac: str) -> dict:
        """Restore one paused device to normal filtering.

        Args:
            mac: Device uid (its MAC address)
        """
        return circle("set_device_mode", uid=_required(mac, "mac"), mode="Filter")

    @server.tool()
    def forget_device(mac: str, confirm: bool = False) -> dict:
        """Remove a device from Circle's device list. It reappears as a new device if it reconnects.

        Args:
            mac: Device uid (its MAC address)
            confirm: Must be true; guards against accidental removal
        """
        mac = _required(mac, "mac")
        if not confirm:
            raise ToolError(f"forget_device removes {mac} from Circle; call again with confirm=true")
        return circle("forget_device", uid=mac)

    @server.tool()
    def pause_home() -> dict:
        """Pause the internet for every managed device in the home."""
        return circle("set_overall_mode", mode="Pause")

    @server.tool()
    def resume_home() -> dict:
        """Resume the internet for the whole home after pause_home."""
        return circle("set_overall_mode", mode="Filter")

    @server.tool()
    def set_circle_timezone(tz: str) -> dict:
        """Set the time zone Circle uses for schedules, limits, and day boundaries.

        Args:
            tz: IANA zone id, e.g. America/Denver
        """
        return circle("set_timezone", tz=_check_timezone(tz))

    @server.tool()
    def set_circle_locale(locale: str) -> dict:
        """Set the circle's language.

        Args:
            locale: Language code such as en or es
        """
        locale = _required(locale, "locale")
        if not _LOCALE_RE.match(locale):
            raise ToolError(f"{locale!r} does not look like a language code (e.g. en, es)")
        return circle("set_locale", locale=locale)

    @server.tool()
    def set_default_profile(pid: str) -> dict:
        """Choose the profile newly seen devices are assigned to.

        Args:
            pid: Profile id from list_profiles (0 = Home, 1 = Unmanaged)
        """
        return circle("set_default_assigned_profile", pid=_required(pid, "pid"))

    @server.tool()
    def dismiss_notification(notification_id: str) -> dict:
        """Clear one of the circle's active dashboard notifications.

        Args:
            notification_id: The notification's id from the circle's activeNotifications
        """
        return circle("dismiss_notification", notification_id=_required(notification_id, "notification_id"))

    @server.tool()
    def set_hardware_enabled(cuuid: str, enabled: bool) -> dict:
        """Turn filtering by a Circle Home unit on or off.

        Args:
            cuuid: The unit's cuuid
            enabled: true to enable, false to disable
        """
        return circle("set_hardware_enabled", cuuid=_required(cuuid, "cuuid"), enabled=enabled)

    @server.tool()
    def set_hardware_compatible_mode(cuuid: str, enabled: bool) -> dict:
        """Turn a Circle Home unit's compatible mode on or off.

        Args:
            cuuid: The unit's cuuid
            enabled: true to turn compatible mode on
        """
        return circle("set_hardware_compatible_mode", cuuid=_required(cuuid, "cuuid"), enabled=enabled)

    @server.tool()
    def reboot_hardware(cuuid: str, confirm: bool = False) -> dict:
        """Reboot a Circle Home unit; filtering through it stops until it is back up.

        Args:
            cuuid: The unit's cuuid
            confirm: Must be true; guards against an accidental reboot
        """
        cuuid = _required(cuuid, "cuuid")
        if not confirm:
            raise ToolError(f"reboot_hardware restarts unit {cuuid}; call again with confirm=true")
        return circle("reboot_hardware", cuuid=cuuid)

    @server.tool()
    def request_child_location(child_device_id: str, provider: str = "fcm") -> dict:
        """Ask a child's phone running Circle to report its location.

        Only sends the request: the location goes to the parent app by push and is not
        returned here.

        Args:
            child_device_id: The child device's id
            provider: Push provider, fcm (default) or pushy
        """
        if provider not in ("fcm", "pushy"):
            raise ToolError("provider must be fcm or pushy")
        return circle("locate_child", child_device_id=_required(child_device_id, "child_device_id"), provider=provider)
