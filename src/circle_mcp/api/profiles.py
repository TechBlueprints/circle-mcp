"""
Profile management and filter switches (PROTOCOL.md B.4, B.4.1).

Every call here changes state on the Circle. They are all legacy endpoints:
GET with query parameters, nested targets addressed as `user.pid=<pid>`.

The "add switch" endpoints are sent the way the app sends them: `clear=true`
plus the complete set as comma-separated parallel lists (ids in one parameter,
states in the other), so each call replaces the profile's whole switch list.
Callers that want to change a single entry merge with `profile_record()` first.
"""

import re
from typing import Dict, List, Optional, Tuple

AGE_CATEGORIES = ("Pre-K", "Kid", "Teen", "Adult", "None")
PROFILE_MODES = ("Filter", "Pause")
ON_OFF = ("On", "Off")
SWITCH_STATES = ("On", "Off", "Unmanaged")

# pid 0 is the Home profile and 1 the Unmanaged profile (PROTOCOL.md A.4)
BUILTIN_PIDS = ("0", "1")

_MAC_RE = re.compile(r"^[0-9A-Fa-f]{2}([:-]?)[0-9A-Fa-f]{2}(\1[0-9A-Fa-f]{2}){4}$")


def choice(value, allowed: Tuple[str, ...], label: str) -> str:
    """Match `value` case-insensitively against `allowed`; return the canonical spelling."""
    text = str(value).strip()
    for option in allowed:
        if text.lower() == option.lower():
            return option
    raise ValueError(f"{label} must be one of {', '.join(allowed)} (got {value!r})")


def _pid(pid) -> str:
    text = str(pid).strip()
    if not text:
        raise ValueError("pid is required")
    return text


def _list_item(value, label: str) -> str:
    """One entry of a comma-joined list; a comma inside it would split the lists apart."""
    text = str(value).strip()
    if not text:
        raise ValueError(f"{label} must not be empty")
    if "," in text:
        raise ValueError(f"{label} must not contain a comma (got {value!r})")
    return text


class ProfilesMixin:
    """Create, edit and delete profiles; manage their filter switches."""

    # -----------------------------
    # Profile record (for merges)
    # -----------------------------

    def profile_record(self, pid) -> Dict:
        """The profile's entry from `/api/QUERY/all` `users[]`; ValueError if absent."""
        pid = _pid(pid)
        snapshot = self._request("/api/QUERY/all")
        users = snapshot.get("users") if isinstance(snapshot, dict) else None
        for user in users if isinstance(users, list) else []:
            if isinstance(user, dict) and str(user.get("pid", "")).strip() == pid:
                return user
        raise ValueError(f"No Circle profile with pid {pid}")

    @staticmethod
    def _switch_map(entries, key: str, value_key: str) -> Dict[str, str]:
        """[{key: id, value_key: state}, ...] -> {id: state}, skipping malformed items."""
        found = {}
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, dict) or entry.get(key) is None or entry.get(value_key) is None:
                continue
            ident = str(entry[key]).strip()
            if ident:
                found[ident] = str(entry[value_key]).strip()
        return found

    def current_category_switches(self, pid) -> Dict[str, str]:
        return self._switch_map(self.profile_record(pid).get("categorySwitches"), "category", "state")

    def current_platform_switches(self, pid) -> Dict[str, str]:
        return self._switch_map(self.profile_record(pid).get("platformSwitches"), "platform", "state")

    def current_custom_switches(self, pid) -> Dict[str, str]:
        return self._switch_map(self.profile_record(pid).get("customSwitches"), "site", "toggle")

    # -----------------------------
    # Profiles
    # -----------------------------

    def create_profile(self, name: str, age_category: str, mode: str = "Filter") -> Optional[Dict]:
        """Create a profile. The response carries the new profile's id in `newPid`."""
        name = str(name).strip()
        if not name:
            raise ValueError("name is required")
        return self._request("/api/ADD/users/user", query={
            "name": name,
            "ageCategory": choice(age_category, AGE_CATEGORIES, "age_category"),
            "mode": choice(mode, PROFILE_MODES, "mode"),
            "toggles.defaultPlatforms": "Off",
        })

    def _update_user(self, pid, field: str, value: str) -> Optional[Dict]:
        return self._request(f"/api/UPDATE/users/user/{field}", query={"user.pid": _pid(pid), "value": value})

    def rename_profile(self, pid, name: str) -> Optional[Dict]:
        name = str(name).strip()
        if not name:
            raise ValueError("name is required")
        return self._update_user(pid, "name", name)

    def set_profile_age_category(self, pid, age_category: str) -> Optional[Dict]:
        return self._update_user(pid, "ageCategory", choice(age_category, AGE_CATEGORIES, "age_category"))

    def set_profile_mode(self, pid, mode: str) -> Optional[Dict]:
        """`Filter` is normal filtering; `Pause` cuts the profile's internet."""
        return self._update_user(pid, "mode", choice(mode, PROFILE_MODES, "mode"))

    def set_profile_time_limits_toggle(self, pid, value: str) -> Optional[Dict]:
        return self._update_user(pid, "toggles/timeLimits", choice(value, ON_OFF, "time_limits"))

    def set_profile_off_times_toggle(self, pid, value: str) -> Optional[Dict]:
        return self._update_user(pid, "toggles/offTimes", choice(value, ON_OFF, "off_times"))

    def delete_profile(self, pid) -> Optional[Dict]:
        pid = _pid(pid)
        if pid in BUILTIN_PIDS:
            raise ValueError(f"pid {pid} is a built-in profile (Home/Unmanaged) and cannot be deleted")
        return self._request("/api/DELETE/users/user", query={"user.pid": pid})

    def assign_device_to_profile(self, pid, mac: str) -> Optional[Dict]:
        mac = str(mac).strip()
        if not _MAC_RE.match(mac):
            raise ValueError(f"mac must be a device MAC address such as aa:bb:cc:dd:ee:ff (got {mac!r})")
        return self._request(
            "/api/ADD/users/user/relatedDevices/relatedDevice",
            query={"user.pid": _pid(pid), "mac": mac},
        )

    # -----------------------------
    # Filter switches
    # -----------------------------

    @staticmethod
    def _parallel(switches: Dict, id_label: str, states: Tuple[str, ...], state_label: str) -> Tuple[str, str]:
        ids: List[str] = []
        values: List[str] = []
        for ident, state in switches.items():
            ids.append(_list_item(ident, id_label))
            values.append(choice(state, states, state_label))
        return ",".join(ids), ",".join(values)

    def replace_category_switches(self, pid, switches: Dict[str, str]) -> Optional[Dict]:
        """Set the profile's category switches to exactly `switches` ({category id: state})."""
        ids, states = self._parallel(switches, "category id", SWITCH_STATES, "state")
        return self._request("/api/ADD/users/user/categorySwitches/switch", query={
            "user.pid": _pid(pid), "clear": "true", "category": ids, "state": states,
        })

    def replace_platform_switches(self, pid, switches: Dict[str, str]) -> Optional[Dict]:
        """Set the profile's platform (app) switches to exactly `switches` ({platform id: state})."""
        ids, states = self._parallel(switches, "platform id", SWITCH_STATES, "state")
        return self._request("/api/ADD/users/user/platformSwitches/switch", query={
            "user.pid": _pid(pid), "clear": "true", "platform": ids, "state": states,
        })

    def delete_platform_switch(self, pid, platform_id) -> Optional[Dict]:
        return self._request("/api/DELETE/users/user/platformSwitches/switch", query={
            "user.pid": _pid(pid), "switch.platform": _list_item(platform_id, "platform id"),
        })

    def replace_custom_switches(self, pid, switches: Dict[str, str]) -> Optional[Dict]:
        """Set the profile's custom sites to exactly `switches` ({site: On|Off})."""
        sites, toggles = self._parallel(switches, "site", ON_OFF, "toggle")
        return self._request("/api/ADD/users/user/customSwitches/switch", query={
            "user.pid": _pid(pid), "site": sites, "toggle": toggles, "clear": "true",
        })

    def delete_custom_switch(self, pid, site: str) -> Optional[Dict]:
        return self._request("/api/DELETE/users/user/customSwitches/switch", query={
            "user.pid": _pid(pid), "switch.site": _list_item(site, "site"),
        })
