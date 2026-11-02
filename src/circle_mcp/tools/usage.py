"""Usage and history tools (PROTOCOL.md B.3). All read-only."""

from typing import Optional


def register(server, circle, lock) -> None:
    """Register the usage tools.

    `circle` is the server's call gateway, `circle(method_name, **kwargs)`: it
    runs one CircleClient method under `lock`, applies the sign-in backoff, and
    turns Circle failures into ToolErrors. Each tool here is a single client
    method, so `lock` needs no extra handling.
    """

    @server.tool()
    def get_usage_by_site(profile_pid: Optional[str] = None, day: str = "0", ndays: str = "1") -> dict:
        """Get internet usage broken down by site for a profile (raw Circle report).

        Args:
            profile_pid: Profile ID from list_profiles (falls back to configured profile_pid)
            day: Days ago the window starts (0 = today)
            ndays: Days in the window, counted forward from `day` (last 7 days: day=6, ndays=7)
        """
        return circle("get_usage_sites", profile_pid=profile_pid, day=day, ndays=ndays)

    @server.tool()
    def get_history(
        profile_pid: Optional[str] = None,
        kind: str = "allowed",
        day: str = "0",
        max_records: int = 200,
    ) -> dict:
        """Get a profile's browsing history: sites that were allowed or blocked.

        Returns a flat `entries` list; each entry has the site, time, state, the
        device `uid`, and `category` / `categoryName` when Circle reports one.

        Args:
            profile_pid: Profile ID from list_profiles (falls back to configured profile_pid)
            kind: "allowed" or "blocked"
            day: Days ago (0 = today)
            max_records: Maximum number of entries to return
        """
        return circle("get_history", profile_pid=profile_pid, kind=kind, day=day, max_records=max_records)

    @server.tool()
    def get_time_remaining(profile_pid: Optional[str] = None) -> dict:
        """Get how much screen time a profile has left today, per limited category.

        For each category with a limit today (weekday or weekend limit, in the
        circle's timezone): the limit, extra minutes granted today, minutes used,
        and minutes remaining. Also reports whether the profile is paused and
        whether time limits are switched on.

        Args:
            profile_pid: Profile ID from list_profiles (falls back to configured profile_pid)
        """
        return circle("get_time_remaining", profile_pid=profile_pid)
