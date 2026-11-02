"""
Read tools for circle state (PROTOCOL.md B.2). All read-only, always registered.

`register(server, circle, lock)`: `circle(method, **kwargs)` runs one CircleClient
method under the shared lock with server.py's error handling; `lock` is that same
lock and must not be held around `circle(...)` calls.
"""

from typing import Optional


def register(server, circle, lock) -> None:
    @server.tool()
    def get_circle_snapshot(section: Optional[str] = None):
        """Get the full circle snapshot (profiles, devices, Circle Home units, settings, categories).

        Large; prefer the list_* tools unless you need the raw detail.

        Args:
            section: Return only one part: overall, users, devices, hardware, or categories
        """
        return circle("get_snapshot", section=section)

    @server.tool()
    def list_devices() -> list:
        """List devices on the home network: uid (MAC), names, ip, maker/model, mode, the
        assigned profile pid, and isGo (managed by the Circle app on the child's phone)."""
        return circle("list_devices")

    @server.tool()
    def list_hardware(check_online: bool = False) -> list:
        """List Circle Home units: cuuid, model, online/enabled flags, network status, Wi-Fi, firmware.

        Args:
            check_online: Also ask Circle for each unit's live online state (one extra call per unit)
        """
        units = circle("list_hardware")
        if check_online:
            for unit in units:
                if unit.get("cuuid"):
                    unit["isOnlineLive"] = circle("hardware_is_online", cuuid=unit["cuuid"])
        return units

    @server.tool()
    def list_categories() -> list:
        """List filter categories (id, name, mainCategory, description). Use it to name the
        category ids in usage reports."""
        return circle("list_categories")

    @server.tool()
    def list_go_devices() -> dict:
        """List child phones/tablets managed by the Circle app ("Circle Go"): counts plus each
        device's id, state, and last check-in."""
        return circle("list_go_devices")

    @server.tool()
    def get_profile_badges() -> list:
        """Get the badges the Circle app shows on each profile, as [{pid, badges}]."""
        return circle("get_profile_badges")
