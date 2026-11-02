"""
Profile and filter-switch tools (PROTOCOL.md B.4, B.4.1).

All of these change state, so `register` adds nothing unless the env var
CIRCLE_MCP_ALLOW_WRITES is truthy (1/true/yes/on). Each tool returns the raw
Circle response under `response` and an `ok` flag (PROTOCOL.md A.3).
"""

import os
import urllib.error
from typing import Callable, Dict, Optional

from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations

from ..api.profiles import BUILTIN_PIDS, ON_OFF, SWITCH_STATES, choice
from ..provider import CircleAuthError

_TRUTHY = ("1", "true", "yes", "on")
_SUCCESS_RESULTS = ("true", "success", "ok")

_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False)
_DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True)


def writes_allowed() -> bool:
    return os.environ.get("CIRCLE_MCP_ALLOW_WRITES", "").strip().lower() in _TRUTHY


def is_success(response) -> bool:
    """A.3: a legacy call succeeded if it came back 200, or its `result` says so.

    Non-2xx statuses surface as exceptions before this point, so a response
    here is a 200 unless its body explicitly reports otherwise (a `result`
    outside true/success/OK, or an `error` with no `result`). None means no
    admin token was available and nothing was sent.
    """
    if response is None:
        return False
    if not isinstance(response, dict):
        return True
    if "result" in response:
        return str(response["result"]).strip().lower() in _SUCCESS_RESULTS
    return not response.get("error")


def _outcome(response, **extra) -> dict:
    return {"ok": is_success(response), **extra, "response": response}


def register(server, circle: Callable, lock) -> list:
    """Register the profile write tools on `server`; returns the names registered."""
    if not writes_allowed():
        return []

    def run(action: Callable):
        """Run `action(client)` under the Circle lock; anticipated failures become ToolErrors."""
        with lock:
            try:
                return action(circle())
            except CircleAuthError as e:
                raise ToolError(str(e)) from e
            except (RuntimeError, ValueError) as e:
                raise ToolError(str(e)) from e
            except urllib.error.HTTPError as e:
                raise ToolError(f"Circle API returned HTTP {e.code}") from e
            except (urllib.error.URLError, OSError) as e:
                raise ToolError(f"Circle API unreachable: {getattr(e, 'reason', e)}") from e

    def merged(current: Dict[str, str], changes: Dict[str, str], replace: bool) -> Dict[str, str]:
        result = {} if replace else dict(current)
        result.update({str(k).strip(): choice(v, SWITCH_STATES, "state") for k, v in changes.items()})
        return result

    @server.tool(annotations=_WRITE)
    def pause_profile(pid: str) -> dict:
        """Pause a profile's internet (profile mode `Pause`). Undo with resume_profile.

        Args:
            pid: Profile ID from list_profiles
        """
        return _outcome(run(lambda c: c.set_profile_mode(pid, "Pause")))

    @server.tool(annotations=_WRITE)
    def resume_profile(pid: str) -> dict:
        """Resume normal filtering for a paused profile (profile mode `Filter`).

        Args:
            pid: Profile ID from list_profiles
        """
        return _outcome(run(lambda c: c.set_profile_mode(pid, "Filter")))

    @server.tool(annotations=_WRITE)
    def create_profile(name: str, age_category: str, mode: str = "Filter") -> dict:
        """Create a family profile. Returns the new profile's `pid`.

        Args:
            name: Display name
            age_category: Pre-K, Kid, Teen, Adult or None (sets the default filter level)
            mode: Filter (default) or Pause
        """
        response = run(lambda c: c.create_profile(name, age_category, mode))
        new_pid = response.get("newPid") if isinstance(response, dict) else None
        return _outcome(response, pid=None if new_pid is None else str(new_pid))

    @server.tool(annotations=_WRITE)
    def rename_profile(pid: str, name: str) -> dict:
        """Rename a profile.

        Args:
            pid: Profile ID from list_profiles
            name: New display name
        """
        return _outcome(run(lambda c: c.rename_profile(pid, name)))

    @server.tool(annotations=_WRITE)
    def set_profile_age_category(pid: str, age_category: str) -> dict:
        """Change a profile's age category, which sets its default filter level.

        Args:
            pid: Profile ID from list_profiles
            age_category: Pre-K, Kid, Teen, Adult or None
        """
        return _outcome(run(lambda c: c.set_profile_age_category(pid, age_category)))

    @server.tool(annotations=_WRITE)
    def set_profile_toggles(pid: str, time_limits: Optional[str] = None, off_times: Optional[str] = None) -> dict:
        """Turn a profile's time limits and/or off times (bedtimes) on or off.

        Args:
            pid: Profile ID from list_profiles
            time_limits: On or Off; omit to leave unchanged
            off_times: On or Off; omit to leave unchanged
        """
        if time_limits is None and off_times is None:
            raise ToolError("Give time_limits and/or off_times (On or Off)")

        def apply(c):
            # validate both before sending either, so a bad value changes nothing
            if time_limits is not None:
                choice(time_limits, ON_OFF, "time_limits")
            if off_times is not None:
                choice(off_times, ON_OFF, "off_times")
            responses = {}
            if time_limits is not None:
                responses["timeLimits"] = c.set_profile_time_limits_toggle(pid, time_limits)
            if off_times is not None:
                responses["offTimes"] = c.set_profile_off_times_toggle(pid, off_times)
            return responses

        responses = run(apply)
        return {"ok": all(is_success(r) for r in responses.values()), "response": responses}

    @server.tool(annotations=_DESTRUCTIVE)
    def delete_profile(pid: str, confirm: bool = False) -> dict:
        """Permanently delete a profile. Its devices fall back to the default profile.

        Args:
            pid: Profile ID from list_profiles (the built-in Home `0` and Unmanaged `1` cannot be deleted)
            confirm: Must be true; guards against accidental deletion
        """
        if str(pid).strip() in BUILTIN_PIDS:
            raise ToolError(f"pid {str(pid).strip()} is a built-in profile (Home/Unmanaged) and cannot be deleted")
        if confirm is not True:
            raise ToolError("Refusing to delete: call again with confirm=true to delete this profile")
        return _outcome(run(lambda c: c.delete_profile(pid)))

    @server.tool(annotations=_WRITE)
    def assign_device_to_profile(pid: str, mac: str) -> dict:
        """Move a device onto a profile so that profile's rules apply to it.

        Args:
            pid: Profile ID from list_profiles
            mac: The device's MAC address (its `uid`)
        """
        return _outcome(run(lambda c: c.assign_device_to_profile(pid, mac)))

    @server.tool(annotations=_WRITE)
    def set_category_switches(pid: str, switches: Dict[str, str], replace: bool = False) -> dict:
        """Override the age-category default for filter categories on a profile.

        By default the given entries are merged into the profile's existing
        category switches; with replace=true the profile ends up with exactly
        `switches` (an empty dict clears them all).

        Args:
            pid: Profile ID from list_profiles
            switches: Category id -> On (allowed), Off (blocked) or Unmanaged
            replace: Send exactly `switches` instead of merging
        """
        def apply(c):
            merged({}, switches, True)  # validate before fetching
            current = {} if replace else c.current_category_switches(pid)
            final = merged(current, switches, replace)
            return final, c.replace_category_switches(pid, final)

        final, response = run(apply)
        return _outcome(response, switches=final)

    @server.tool(annotations=_WRITE)
    def set_platform_switches(pid: str, switches: Dict[str, str], replace: bool = False) -> dict:
        """Override the defaults for platforms (apps such as YouTube or TikTok) on a profile.

        By default the given entries are merged into the profile's existing
        platform switches; with replace=true the profile ends up with exactly
        `switches`.

        Args:
            pid: Profile ID from list_profiles
            switches: Platform id -> On (allowed), Off (blocked) or Unmanaged
            replace: Send exactly `switches` instead of merging
        """
        def apply(c):
            merged({}, switches, True)  # validate before fetching
            current = {} if replace else c.current_platform_switches(pid)
            final = merged(current, switches, replace)
            return final, c.replace_platform_switches(pid, final)

        final, response = run(apply)
        return _outcome(response, switches=final)

    @server.tool(annotations=_DESTRUCTIVE)
    def remove_platform_switch(pid: str, platform_id: str) -> dict:
        """Drop one platform override so the profile's age-category default applies again.

        Args:
            pid: Profile ID from list_profiles
            platform_id: Platform id to remove
        """
        return _outcome(run(lambda c: c.delete_platform_switch(pid, platform_id)))

    @server.tool(annotations=_WRITE)
    def set_custom_site(pid: str, site: str, toggle: str) -> dict:
        """Always allow (On) or always block (Off) a website for a profile.

        Other custom sites on the profile are kept.

        Args:
            pid: Profile ID from list_profiles
            site: Domain, e.g. example.com
            toggle: On (allow) or Off (block)
        """
        def apply(c):
            name, value = str(site).strip(), choice(toggle, ON_OFF, "toggle")
            final = {k: v for k, v in c.current_custom_switches(pid).items() if k.lower() != name.lower()}
            final[name] = value
            return final, c.replace_custom_switches(pid, final)

        final, response = run(apply)
        return _outcome(response, sites=final)

    @server.tool(annotations=_DESTRUCTIVE)
    def remove_custom_site(pid: str, site: str) -> dict:
        """Remove a website from a profile's custom allow/block list.

        Args:
            pid: Profile ID from list_profiles
            site: Domain to remove
        """
        return _outcome(run(lambda c: c.delete_custom_switch(pid, site)))

    return [
        "pause_profile", "resume_profile", "create_profile", "rename_profile",
        "set_profile_age_category", "set_profile_toggles", "delete_profile",
        "assign_device_to_profile", "set_category_switches", "set_platform_switches",
        "remove_platform_switch", "set_custom_site", "remove_custom_site",
    ]
