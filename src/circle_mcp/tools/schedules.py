"""
Schedule tools: bedtimes, focus times, off times, time limits, reward minutes.

The three listings always register. Everything that changes a profile only
registers when CIRCLE_MCP_ALLOW_WRITES is truthy (1/true/yes/on).

Days are weekday names ("Monday", "sat") or indices with Sunday = 0; times
are 24-hour HH:mm.
"""

import os
from typing import Dict, List, Optional, Union

Day = Union[str, int]


def writes_allowed() -> bool:
    return os.environ.get("CIRCLE_MCP_ALLOW_WRITES", "").strip().lower() in ("1", "true", "yes", "on")


def register(server, circle, lock) -> None:
    """Add the schedule tools to `server`; `circle(method, **kwargs)` runs a client call.

    `lock` is held inside `circle`, so it is not taken here.
    """

    @server.tool()
    def list_bedtimes() -> list:
        """List bedtimes for every profile: [{pid, bedtimes:[{id, pid, name, start, end, days, state}]}]."""
        return circle("list_bedtimes")

    @server.tool()
    def list_focus_times() -> list:
        """List focus times for every profile: [{pid, focustimes:[{id, name, start, end, days, enabled,
        applyFilters, filters}]}]."""
        return circle("list_focus_times")

    @server.tool()
    def get_focus_time_defaults() -> dict:
        """Get the default filter sets Circle suggests for a new focus time."""
        result = circle("get_focus_time_defaults")
        if result is None:
            return {"error": "Failed to fetch focus time defaults"}
        return result if isinstance(result, dict) else {"defaults": result}

    if not writes_allowed():
        return

    @server.tool()
    def set_time_limits(
        pid: str,
        limits: List[Dict],
        weekend_days: Optional[List[Day]] = None,
        enabled: bool = True,
    ) -> dict:
        """Replace ALL daily time limits on a profile (limits not listed are removed).

        Args:
            pid: Profile ID from list_profiles
            limits: [{"category": <category id>, "minutes": <daily minutes>, "days": "weekdays"|"weekend"}]
            weekend_days: Days that count as the weekend, e.g. ["Saturday", "Sunday"]; omit to keep the current ones
            enabled: Master switch for time limits on this profile
        """
        return circle("set_time_limits", pid=pid, limits=limits, weekend_days=weekend_days, enabled=enabled)

    @server.tool()
    def add_reward_minutes(pid: str, category: str, minutes: int) -> dict:
        """Give a profile extra minutes today in one category. Today's other rewards are kept,
        and minutes add to any reward that category already has today.

        Args:
            pid: Profile ID from list_profiles
            category: Category id the time limit is on
            minutes: Extra minutes to add for today
        """
        return circle("add_reward_minutes", pid=pid, category=category, minutes=minutes)

    @server.tool()
    def extend_off_time_today(pid: str, off_time_id: str, start_minutes: int = 0, end_minutes: int = 0) -> dict:
        """Shift today's window of one off time / bedtime (e.g. a later bedtime tonight).

        Args:
            pid: Profile ID from list_profiles
            off_time_id: Id of the profile's off time (flexOffTimes id)
            start_minutes: Minutes to move the start later (negative = earlier)
            end_minutes: Minutes to move the end later (negative = earlier)
        """
        return circle(
            "extend_off_time_today", pid=pid, off_time_id=off_time_id,
            start_minutes=start_minutes, end_minutes=end_minutes,
        )

    @server.tool()
    def set_off_times(pid: str, windows: List[Dict]) -> dict:
        """Replace ALL legacy off-time windows on a profile (windows not listed are removed).

        Args:
            pid: Profile ID from list_profiles
            windows: [{"name", "start": "HH:mm", "end": "HH:mm", "days": ["Monday", ...],
                "type": "offtime"|"bedtime_weekday"|"bedtime_weekend", "enabled": true, "id": <keep to update>}]
        """
        return circle("set_off_times", pid=pid, windows=windows)

    @server.tool()
    def add_bedtime(pid: str, name: str, start: str, end: str, days: List[Day], enabled: bool = True) -> dict:
        """Create a bedtime on a profile.

        Args:
            pid: Profile ID from list_profiles
            name: Label for the bedtime
            start: Start time, HH:mm 24-hour
            end: End time, HH:mm 24-hour (may be past midnight)
            days: Days it applies, e.g. ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday"]
            enabled: Whether it is active
        """
        return circle("add_bedtime", pid=pid, name=name, start=start, end=end, days=days, enabled=enabled)

    @server.tool()
    def update_bedtime(
        pid: str, bedtime_id: str, name: str, start: str, end: str, days: List[Day], enabled: bool = True
    ) -> dict:
        """Replace an existing bedtime (send every field; get current values from list_bedtimes).

        Args:
            pid: Profile ID the bedtime belongs to
            bedtime_id: Bedtime id from list_bedtimes
            name: Label for the bedtime
            start: Start time, HH:mm 24-hour
            end: End time, HH:mm 24-hour
            days: Days it applies
            enabled: Whether it is active
        """
        return circle(
            "update_bedtime", pid=pid, bedtime_id=bedtime_id, name=name,
            start=start, end=end, days=days, enabled=enabled,
        )

    @server.tool()
    def add_focus_time(
        pid: str,
        name: str,
        start: str,
        end: str,
        days: List[Day],
        enabled: bool = True,
        apply_filters: bool = False,
        allowed_categories: Optional[List[int]] = None,
        blocked_categories: Optional[List[int]] = None,
        allowed_urls: Optional[List[str]] = None,
        blocked_urls: Optional[List[str]] = None,
    ) -> dict:
        """Create a focus time (e.g. homework hours) on a profile.

        Args:
            pid: Profile ID from list_profiles
            name: Label for the focus time
            start: Start time, HH:mm 24-hour
            end: End time, HH:mm 24-hour
            days: Days it applies
            enabled: Whether it is active
            apply_filters: Use the allowed/blocked lists below during the window
            allowed_categories: Category ids allowed during the window
            blocked_categories: Category ids blocked during the window
            allowed_urls: Sites allowed during the window
            blocked_urls: Sites blocked during the window
        """
        return circle(
            "add_focus_time", pid=pid, name=name, start=start, end=end, days=days, enabled=enabled,
            apply_filters=apply_filters, allowed_categories=allowed_categories,
            blocked_categories=blocked_categories, allowed_urls=allowed_urls, blocked_urls=blocked_urls,
        )

    @server.tool()
    def update_focus_time(
        pid: str,
        focus_time_id: str,
        name: str,
        start: str,
        end: str,
        days: List[Day],
        enabled: bool = True,
        apply_filters: bool = False,
        allowed_categories: Optional[List[int]] = None,
        blocked_categories: Optional[List[int]] = None,
        allowed_urls: Optional[List[str]] = None,
        blocked_urls: Optional[List[str]] = None,
    ) -> dict:
        """Replace an existing focus time (send every field; get current values from list_focus_times).

        Args:
            pid: Profile ID the focus time belongs to
            focus_time_id: Focus time id from list_focus_times
            name: Label for the focus time
            start: Start time, HH:mm 24-hour
            end: End time, HH:mm 24-hour
            days: Days it applies
            enabled: Whether it is active
            apply_filters: Use the allowed/blocked lists below during the window
            allowed_categories: Category ids allowed during the window
            blocked_categories: Category ids blocked during the window
            allowed_urls: Sites allowed during the window
            blocked_urls: Sites blocked during the window
        """
        return circle(
            "update_focus_time", pid=pid, focus_time_id=focus_time_id, name=name, start=start, end=end,
            days=days, enabled=enabled, apply_filters=apply_filters, allowed_categories=allowed_categories,
            blocked_categories=blocked_categories, allowed_urls=allowed_urls, blocked_urls=blocked_urls,
        )

    @server.tool()
    def delete_focus_time(pid: str, focus_time_id: str) -> dict:
        """Delete a focus time from a profile.

        Args:
            pid: Profile ID the focus time belongs to
            focus_time_id: Focus time id from list_focus_times
        """
        return circle("delete_focus_time", pid=pid, focus_time_id=focus_time_id)
