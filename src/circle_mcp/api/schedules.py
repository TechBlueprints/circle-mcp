"""
Schedules: time limits, reward minutes, off times, bedtimes v2, focus times v2
(PROTOCOL.md B.4.2 - B.4.5).

Legacy calls are GET with query params; every list-valued param is a comma
list kept parallel with its siblings, and `clear=true` makes the call replace
the whole set. v2 calls are JSON with POST/PUT/DELETE.

Day encodings (PROTOCOL.md A.4):
- legacy day-index strings: Sunday=0 .. Saturday=6, concatenated ("12345")
- v2 bodies: full English day names ("Monday")
Time-limit entries do not take day indices: each one names a bucket,
"weekdays" or "weekend", and the profile's `weekend` index string decides
which days fall in each bucket.

Reward entries carry a `date`: "0" asks the server to stamp today, and
entries it hands back are dated YYYYMMDD.
"""

import datetime
import re
import urllib.parse
from typing import Dict, Iterable, List, Optional, Union

DAY_NAMES = ("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")
LIMIT_BUCKETS = ("weekdays", "weekend")
OFF_TIME_TYPES = ("bedtime_weekday", "bedtime_weekend", "offtime")

_HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")

DayList = Union[str, int, Iterable[Union[str, int]]]


# -----------------------------
# Value helpers
# -----------------------------

def _one_day(day: Union[str, int]) -> int:
    if isinstance(day, bool):
        raise ValueError(f"not a day: {day!r}")
    if isinstance(day, int):
        if 0 <= day <= 6:
            return day
        raise ValueError(f"day index {day} out of range (Sunday=0 .. Saturday=6)")
    text = str(day).strip()
    if text.isdigit():
        return _one_day(int(text))
    lowered = text.lower()
    for n, name in enumerate(DAY_NAMES):
        if lowered == name.lower() or (len(lowered) >= 3 and name.lower().startswith(lowered)):
            return n
    raise ValueError(f"unknown day {day!r}; use a weekday name or 0-6 with Sunday=0")


def day_indices(days: DayList) -> List[int]:
    """Sorted, de-duplicated day indices from names, ints, or a string of them.

    Accepts a list (["Sat", "Sunday", 3]), a comma list ("sat,sun"), or a
    legacy index string ("06").
    """
    if isinstance(days, (int, bool)):
        items = [days]
    elif isinstance(days, str):
        text = days.strip()
        if text.isdigit():
            items = list(text)
        else:
            items = [p for p in (s.strip() for s in text.split(",")) if p]
    else:
        items = list(days)
    return sorted({_one_day(d) for d in items})


def day_index_string(days: DayList) -> str:
    """Legacy form: Sunday=0 .. Saturday=6, concatenated ("06")."""
    return "".join(str(n) for n in day_indices(days))


def day_names(days: DayList) -> List[str]:
    """v2 form: full English names, Sunday first."""
    return [DAY_NAMES[n] for n in day_indices(days)]


def hhmm(value: str) -> str:
    """Validate a 24-hour time and normalize it to zero-padded HH:mm."""
    m = _HHMM.match(str(value).strip())
    if not m:
        raise ValueError(f"time {value!r} is not HH:mm (24-hour)")
    return f"{int(m.group(1)):02d}:{m.group(2)}"


def _limit_bucket(value: str) -> str:
    text = str(value).strip().lower()
    aliases = {"weekday": "weekdays", "weekdays": "weekdays", "weekend": "weekend", "weekends": "weekend"}
    if text not in aliases:
        raise ValueError(f"time-limit days must be 'weekdays' or 'weekend', not {value!r}")
    return aliases[text]


def _list_param(values: Iterable) -> str:
    return ",".join(str(v) for v in values)


def _no_commas(label: str, value) -> str:
    text = str(value).strip()
    if "," in text:
        raise ValueError(f"{label} {text!r} may not contain a comma (it is sent in a comma list)")
    return text


def _whole_minutes(label: str, value, allow_negative: bool = False) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label} must be a whole number of minutes, not {value!r}") from None
    if n < 0 and not allow_negative:
        raise ValueError(f"{label} must not be negative")
    return n


def _int_id(label: str, value) -> Union[int, str]:
    """v2 bodies carry numeric ids; keep anything non-numeric as-is."""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        if value in (None, ""):
            raise ValueError(f"{label} is required") from None
        return value


def legacy_ok(result) -> bool:
    """PROTOCOL.md A.3 for a body that came back with HTTP 2xx.

    An explicit `result` decides; without one, an `error` field means failure.
    """
    if not isinstance(result, dict):
        return result is not None
    if "result" in result:
        flag = result["result"]
        if isinstance(flag, bool):
            return flag
        return str(flag).strip().lower() in ("true", "success", "ok")
    return not result.get("error")


def _today_stamp() -> str:
    return datetime.date.today().strftime("%Y%m%d")


def _is_current(date_value, today: str) -> bool:
    """Keep rewards dated today (or "0"); a day either side covers timezone skew."""
    text = str(date_value if date_value is not None else "0").strip()
    if text in ("", "0"):
        return True
    try:
        then = datetime.datetime.strptime(text, "%Y%m%d").date()
        now = datetime.datetime.strptime(today, "%Y%m%d").date()
    except ValueError:
        return False
    return abs((then - now).days) <= 1


# -----------------------------
# Client mixin
# -----------------------------

class SchedulesMixin:
    """Time limits, rewards, off times, bedtimes and focus times. Needs `_request`."""

    def _legacy_get(self, path: str, params: Dict) -> Optional[Dict]:
        # commas and colons stay literal so the parallel lists read as the app sends them
        qs = urllib.parse.urlencode(params, safe=",:", quote_via=urllib.parse.quote)
        return self._request(f"{path}?{qs}")

    def _schedules_profile(self, pid: str) -> Dict:
        """The profile's entry in /api/QUERY/all (empty dict when absent)."""
        snapshot = self._request("/api/QUERY/all") or {}
        users = snapshot.get("users") if isinstance(snapshot, dict) else None
        for user in users if isinstance(users, list) else []:
            if isinstance(user, dict) and str(user.get("pid")) == str(pid):
                return user
        return {}

    @staticmethod
    def _extensions(profile: Dict, kind: str) -> List[Dict]:
        ext = profile.get("extensions")
        items = ext.get(kind) if isinstance(ext, dict) else None
        return [e for e in items if isinstance(e, dict)] if isinstance(items, list) else []

    # ---- B.4.2 time limits and rewards

    def set_time_limits(
        self,
        pid: str,
        limits: List[Dict],
        weekend_days: Optional[DayList] = None,
        enabled: bool = True,
    ) -> Dict:
        """Replace every time limit on a profile.

        limits: [{category, minutes, days}] with days "weekdays" or "weekend".
        weekend_days: which days are the weekend (names or 0-6, Sunday=0);
        omit to leave the profile's current weekend alone.
        """
        params: Dict = {"user.pid": str(pid), "clear": "true"}
        categories, minutes, buckets = [], [], []
        for n, limit in enumerate(limits or []):
            if not isinstance(limit, dict):
                raise ValueError(f"limits[{n}] must be an object with category, minutes, days")
            categories.append(_no_commas("category", limit.get("category", "")))
            if not categories[-1]:
                raise ValueError(f"limits[{n}] is missing a category")
            minutes.append(_whole_minutes(f"limits[{n}].minutes", limit.get("minutes")))
            buckets.append(_limit_bucket(limit.get("days", "weekdays")))
        if categories:
            params["category"] = _list_param(categories)
            params["limit"] = _list_param(minutes)
            params["days"] = _list_param(buckets)
        if weekend_days is not None:
            params["weekend"] = day_index_string(weekend_days)
        params["toggle"] = "On" if enabled else "Off"
        result = self._legacy_get("/api/ADD/users/user/timeLimits/limit", params)
        return {"ok": legacy_ok(result), "response": result}

    def add_reward_minutes(self, pid: str, category: str, minutes: int) -> Dict:
        """Give extra minutes today in one category, keeping today's other rewards.

        The endpoint replaces the whole reward list, so the current one is read
        from /api/QUERY/all first; minutes for a category that already has a
        reward today are added to it.
        """
        category = _no_commas("category", category)
        if not category:
            raise ValueError("category is required")
        minutes = _whole_minutes("minutes", minutes)
        if minutes == 0:
            raise ValueError("minutes must be more than zero")

        today = _today_stamp()
        kept: List[Dict] = []
        merged = False
        for ext in self._extensions(self._schedules_profile(pid), "limits"):
            if not _is_current(ext.get("date"), today):
                continue
            entry = {
                "category": str(ext.get("category", "")),
                "minutes": _whole_minutes("existing minutes", ext.get("minutes", 0) or 0, allow_negative=True),
                "date": str(ext.get("date") or "0"),
            }
            if entry["category"] == category and not merged:
                entry["minutes"] += minutes
                merged = True
            kept.append(entry)
        if not merged:
            kept.append({"category": category, "minutes": minutes, "date": "0"})

        params = {
            "user.pid": str(pid),
            "clear": "true",
            "category": _list_param(e["category"] for e in kept),
            "minutes": _list_param(e["minutes"] for e in kept),
            "date": _list_param(e["date"] for e in kept),
        }
        result = self._legacy_get("/api/ADD/users/user/extensions/limits/extension", params)
        return {"ok": legacy_ok(result), "response": result, "rewards": kept}

    def extend_off_time_today(
        self, pid: str, off_time_id: str, start_minutes: int = 0, end_minutes: int = 0
    ) -> Dict:
        """Shift today's window of one off time; other off times' shifts are kept.

        start_minutes / end_minutes move the window's start and end later by
        that many minutes (negative moves them earlier). Replaces any shift
        already set today for the same off time.
        """
        off_time_id = _no_commas("off_time_id", off_time_id)
        if not off_time_id:
            raise ValueError("off_time_id is required")
        start_minutes = _whole_minutes("start_minutes", start_minutes, allow_negative=True)
        end_minutes = _whole_minutes("end_minutes", end_minutes, allow_negative=True)

        today = _today_stamp()
        kept: List[Dict] = []
        for ext in self._extensions(self._schedules_profile(pid), "offTimes"):
            if not _is_current(ext.get("date"), today) or str(ext.get("offTimeId")) == off_time_id:
                continue
            kept.append({
                "offTimeId": str(ext.get("offTimeId", "")),
                "startMinutes": _whole_minutes("existing startMinutes", ext.get("startMinutes", 0) or 0, True),
                "endMinutes": _whole_minutes("existing endMinutes", ext.get("endMinutes", 0) or 0, True),
                "date": str(ext.get("date") or "0"),
            })
        kept.append({"offTimeId": off_time_id, "startMinutes": start_minutes, "endMinutes": end_minutes, "date": "0"})

        params = {"user.pid": str(pid), "clear": "true"}
        for key in ("offTimeId", "startMinutes", "endMinutes", "date"):
            params[key] = _list_param(e[key] for e in kept)
        result = self._legacy_get("/api/ADD/users/user/extensions/offTimes/extension", params)
        return {"ok": legacy_ok(result), "response": result, "extensions": kept}

    # ---- B.4.3 legacy off times

    def set_off_times(self, pid: str, windows: List[Dict]) -> Dict:
        """Replace every legacy off-time window on a profile.

        windows: [{name, start, end, days, type?, enabled?, id?}]; type is
        bedtime_weekday, bedtime_weekend or offtime (default). Windows
        without an id are numbered after the highest id given.
        """
        rows = []
        for n, w in enumerate(windows or []):
            if not isinstance(w, dict):
                raise ValueError(f"windows[{n}] must be an object")
            kind = str(w.get("type") or "offtime").strip()
            if kind not in OFF_TIME_TYPES:
                raise ValueError(f"windows[{n}].type must be one of {', '.join(OFF_TIME_TYPES)}")
            days = day_index_string(w.get("days") if w.get("days") is not None else [])
            if not days:
                raise ValueError(f"windows[{n}] needs at least one day")
            rows.append({
                "name": _no_commas(f"windows[{n}].name", w.get("name") or kind),
                "type": kind,
                "start": hhmm(w.get("start", "")),
                "end": hhmm(w.get("end", "")),
                "days": days,
                "toggle": "enabled" if w.get("enabled", True) else "disabled",
                "id": _no_commas(f"windows[{n}].id", w["id"]) if w.get("id") not in (None, "") else None,
            })

        next_id = 1 + max((int(r["id"]) for r in rows if r["id"] and r["id"].isdigit()), default=0)
        for r in rows:
            if r["id"] is None:
                r["id"] = str(next_id)
                next_id += 1

        params: Dict = {"user.pid": str(pid)}
        if rows:
            for key in ("name", "type", "start", "end", "days", "toggle", "id"):
                params[key] = _list_param(r[key] for r in rows)
        params["clear"] = "true"
        result = self._legacy_get("/api/ADD/users/user/flexOffTimes/offTime", params)
        return {"ok": legacy_ok(result), "response": result}

    # ---- B.4.4 bedtimes v2

    def list_bedtimes(self) -> List:
        """[{pid, bedtimes:[BedTime]}] for every profile."""
        result = self._request("/api/v2/profiles/bedtimes")
        return _unwrap_list(result, "bedtimes")

    @staticmethod
    def _bedtime_body(pid, name, start, end, days, enabled, bedtime_id=None) -> Dict:
        names = day_names(days)
        if not names:
            raise ValueError("a bedtime needs at least one day")
        body = {
            "pid": _int_id("pid", pid),
            "name": str(name or "Bedtime"),
            "start": hhmm(start),
            "end": hhmm(end),
            "days": names,
            "state": "enabled" if enabled else "disabled",
        }
        if bedtime_id is not None:
            body = {"id": _int_id("bedtime_id", bedtime_id), **body}
        return body

    def add_bedtime(self, pid: str, name: str, start: str, end: str, days: DayList, enabled: bool = True) -> Dict:
        body = self._bedtime_body(pid, name, start, end, days, enabled)
        result = self._request(f"/api/v2/profiles/{_path_id(pid)}/bedtimes", method="POST", body=body)
        return {"ok": True, "response": result}

    def update_bedtime(
        self, pid: str, bedtime_id: str, name: str, start: str, end: str, days: DayList, enabled: bool = True
    ) -> Dict:
        body = self._bedtime_body(pid, name, start, end, days, enabled, bedtime_id)
        path = f"/api/v2/profiles/{_path_id(pid)}/bedtimes/{_path_id(bedtime_id)}"
        result = self._request(path, method="PUT", body=body)
        return {"ok": True, "response": result}

    # ---- B.4.5 focus times v2

    def list_focus_times(self) -> List:
        """[{pid, focustimes:[FocusTime]}] for every profile."""
        result = self._request("/api/v2/profiles/focustimes")
        return _unwrap_list(result, "focustimes")

    def get_focus_time_defaults(self) -> Optional[Dict]:
        return self._request("/api/v2/focustimes/defaults")

    @staticmethod
    def _focus_body(
        pid, name, start, end, days, enabled, apply_filters,
        allowed_categories, blocked_categories, allowed_urls, blocked_urls, focus_id=None,
    ) -> Dict:
        names = day_names(days)
        if not names:
            raise ValueError("a focus time needs at least one day")
        body = {
            "pid": _int_id("pid", pid),
            "name": str(name or "Focus time"),
            "start": hhmm(start),
            "end": hhmm(end),
            "days": names,
            "enabled": bool(enabled),
            "applyFilters": bool(apply_filters),
            "filters": {
                "allowed": [_int_id("allowed category", c) for c in allowed_categories or []],
                "blocked": [_int_id("blocked category", c) for c in blocked_categories or []],
                "allowedURLs": [str(u).strip() for u in allowed_urls or [] if str(u).strip()],
                "blockedURLs": [str(u).strip() for u in blocked_urls or [] if str(u).strip()],
            },
        }
        if focus_id is not None:
            body = {"id": _int_id("focus_time_id", focus_id), **body}
        return body

    def add_focus_time(
        self, pid: str, name: str, start: str, end: str, days: DayList, enabled: bool = True,
        apply_filters: bool = False, allowed_categories: Optional[List] = None,
        blocked_categories: Optional[List] = None, allowed_urls: Optional[List[str]] = None,
        blocked_urls: Optional[List[str]] = None,
    ) -> Dict:
        body = self._focus_body(
            pid, name, start, end, days, enabled, apply_filters,
            allowed_categories, blocked_categories, allowed_urls, blocked_urls,
        )
        result = self._request(f"/api/v2/profiles/{_path_id(pid)}/focustimes", method="POST", body=body)
        return {"ok": True, "response": result}

    def update_focus_time(
        self, pid: str, focus_time_id: str, name: str, start: str, end: str, days: DayList,
        enabled: bool = True, apply_filters: bool = False, allowed_categories: Optional[List] = None,
        blocked_categories: Optional[List] = None, allowed_urls: Optional[List[str]] = None,
        blocked_urls: Optional[List[str]] = None,
    ) -> Dict:
        body = self._focus_body(
            pid, name, start, end, days, enabled, apply_filters,
            allowed_categories, blocked_categories, allowed_urls, blocked_urls, focus_time_id,
        )
        path = f"/api/v2/profiles/{_path_id(pid)}/focustimes/{_path_id(focus_time_id)}"
        result = self._request(path, method="PUT", body=body)
        return {"ok": True, "response": result}

    def delete_focus_time(self, pid: str, focus_time_id: str) -> Dict:
        path = f"/api/v2/profiles/{_path_id(pid)}/focustimes/{_path_id(focus_time_id)}"
        result = self._request(path, method="DELETE")
        return {"ok": True, "response": result}


def _path_id(value) -> str:
    text = str(value if value is not None else "").strip()
    if not text:
        raise ValueError("an id is required")
    return urllib.parse.quote(text, safe="")


def _unwrap_list(result, key: str) -> List:
    """The v2 listings are bare arrays; tolerate an object wrapper too."""
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        for k in ("profiles", key, "data"):
            if isinstance(result.get(k), list):
                return result[k]
    return []
