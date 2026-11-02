"""
Usage and history reads (PROTOCOL.md B.3), plus a cached category-name lookup.

Time remaining is not an endpoint: it is derived from the profile's
`timeLimits` and today's `extensions.limits` (from /api/QUERY/all) minus
today's minutes in /api/LOGS/usage/categories.

Responses are parsed loosely: numbers often arrive as strings, and the
per-entry history shape is unverified, so unknown fields are passed through.
"""

import datetime
import urllib.error
from typing import Optional, Dict, List

HISTORY_KINDS = ("allowed", "blocked")

# Time-limit category id meaning "all categories combined".
TOTAL_CATEGORY = "T"

# Profile `weekend` default when the field is missing: Saturday and Sunday.
_DEFAULT_WEEKEND = "06"

_WEEKDAY_NAMES = ("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")

# Keys a history entry might carry its category id under.
_CATEGORY_KEYS = ("category", "catid", "categoryId", "cat")


class UsageMixin:
    """Mixed into CircleClient; relies on its `_request` and `_default_profile_pid`."""

    # -----------------------------
    # Category names
    # -----------------------------

    def category_names(self, refresh: bool = False) -> Dict[str, str]:
        """Category id -> name, fetched once from the legacy and v2 catalogs.

        A failed fetch is not cached, so the next call tries again.
        """
        cached = getattr(self, "_category_name_cache", None)
        if cached is not None and not refresh:
            return cached

        names: Dict[str, str] = {}
        fetched = False
        # v2 first so the legacy catalog's names win where both have an id.
        for path in ("/api/v2/categories", "/api/QUERY/categories"):
            try:
                result = self._request(path)
            except (urllib.error.URLError, OSError, ValueError):
                continue
            items = _list_under(result, "categories")
            if items is None:
                continue
            fetched = True
            for item in items:
                _collect_names(item, names)

        if fetched:
            self._category_name_cache = names
        return names

    def category_name(self, category_id) -> Optional[str]:
        if category_id is None:
            return None
        key = str(category_id)
        if key == TOTAL_CATEGORY:
            return "Total (all categories)"
        return self.category_names().get(key)

    def _attach_category_names(self, usage: Dict) -> Dict:
        """Add `categoryNames` (id -> name) for the ids in a usage report, in place."""
        if not isinstance(usage, dict):
            return usage
        ids = set()
        for report in _list_under(usage, "report") or []:
            data = report.get("data") if isinstance(report, dict) else None
            if isinstance(data, dict):
                ids.update(str(k) for k in data)
        if ids:
            usage["categoryNames"] = {i: self.category_name(i) for i in sorted(ids)}
        return usage

    # -----------------------------
    # Usage and history
    # -----------------------------

    def _usage_pid(self, profile_pid) -> str:
        pid = profile_pid or self._default_profile_pid
        if not pid:
            raise ValueError("No profile ID specified")
        return str(pid)

    def get_usage_sites(self, profile_pid: Optional[str] = None, day: str = "0", ndays: str = "1") -> Dict:
        """Per-site usage for a profile. The shape is unverified, so it is returned as-is."""
        pid = self._usage_pid(profile_pid)
        result = self._request("/api/LOGS/usage/sites", query={"user": pid, "day": day, "ndays": ndays})
        return result if result else {"error": "Failed to fetch site usage"}

    def resolve_profile(self, profile) -> Dict:
        """{"pid", "name"} for a pid or a profile name (case-insensitive); unknown names raise."""
        text = str(profile).strip()
        users = self._snapshot_section("users") if hasattr(self, "_snapshot_section") else None
        for user in users or []:
            if not isinstance(user, dict) or user.get("pid") is None:
                continue
            name = str(user.get("name") or "")
            if str(user["pid"]) == text or name.lower() == text.lower():
                return {"pid": str(user["pid"]), "name": name or None}
        if text.isdigit():
            return {"pid": text, "name": None}
        raise ValueError(f"no profile named {profile!r}; use list_profiles")

    def get_usage_summary(self, profile_pid: Optional[str] = None, day: str = "0", ndays: str = "1") -> Dict:
        """Minutes by category and by site over a window, with names and hours.

        The window runs forward from `day` days ago for `ndays` days, so
        day=0/ndays=1 is today and day=6/ndays=7 is the last week. Category
        minutes overlap (an app category and its parent both count the same
        traffic); `totalMinutes` is Circle's deduplicated total.
        """
        profile = self.resolve_profile(profile_pid or self._default_profile_pid or "")
        pid = profile["pid"]
        categories = self.get_usage_categories(profile_pid=pid, day=day, ndays=ndays) or {}
        report = (_list_under(categories, "report") or [None])[0]
        data = report.get("data") if isinstance(report, dict) else None
        names = categories.get("categoryNames") if isinstance(categories, dict) else None
        rows = []
        total = 0
        for cid, minutes in (data or {}).items():
            minutes = _minutes(minutes)
            if str(cid) == TOTAL_CATEGORY:
                total = minutes
                continue
            rows.append({"id": str(cid), "name": (names or {}).get(str(cid)), "minutes": minutes, "hours": _hours(minutes)})
        rows.sort(key=lambda r: (-r["minutes"], r["name"] or ""))

        sites = []
        try:
            site_report = (_list_under(self.get_usage_sites(profile_pid=pid, day=day, ndays=ndays), "report") or [None])[0]
        except (OSError, ValueError):
            site_report = None
        site_data = site_report.get("data") if isinstance(site_report, dict) else None
        for site, minutes in ((site_data or {}).get("all") or {}).items():
            minutes = _minutes(minutes)
            sites.append({"site": site, "minutes": minutes, "hours": _hours(minutes)})
        sites.sort(key=lambda r: (-r["minutes"], r["site"]))

        from_days = _int_or(day, 0)
        span = max(_int_or(ndays, 1), 1)
        return {
            "profile": profile,
            "window": {
                "start": report.get("start") if isinstance(report, dict) else None,
                "days": span,
                "fromDaysAgo": from_days,
                "toDaysAgo": max(from_days - span + 1, 0),
            },
            "totalMinutes": total,
            "totalHours": _hours(total),
            "categories": rows,
            "sites": sites,
            "note": "Category minutes overlap (an app and its parent category both count); totalMinutes is deduplicated.",
        }

    def get_history(
        self,
        profile_pid: Optional[str] = None,
        kind: str = "allowed",
        day: str = "0",
        time: str = "23:59",
        max_records: int = 200,
        with_categories: bool = True,
    ) -> Dict:
        """Allowed or blocked browsing history, flattened to one entry per visit."""
        pid = self._usage_pid(profile_pid)
        kind = (kind or "").strip().lower()
        if kind not in HISTORY_KINDS:
            raise ValueError(f"kind must be one of {', '.join(HISTORY_KINDS)}, not {kind!r}")

        result = self._request(
            f"/api/LOGS/history/{kind}",
            query={
                "user": pid,
                "day": day,
                "time": time,
                "maxrecords": int(max_records),
                "withcat": 1 if with_categories else 0,
            },
        )
        if not result:
            return {"error": "Failed to fetch history"}
        if isinstance(result, dict) and result.get("error") and not result.get("report"):
            return result

        entries = flatten_history(result)
        if with_categories:
            for entry in entries:
                if "category" in entry and "categoryName" not in entry:
                    entry["categoryName"] = self.category_name(entry["category"])
        return {"profile_pid": pid, "kind": kind, "day": str(day), "count": len(entries), "entries": entries}

    # -----------------------------
    # Time remaining
    # -----------------------------

    def get_time_remaining(self, profile_pid: Optional[str] = None, now: Optional[datetime.datetime] = None) -> Dict:
        """Today's per-category limit, extensions, usage and remaining minutes for a profile."""
        pid = self._usage_pid(profile_pid)

        snapshot = self._request("/api/QUERY/all")
        if not isinstance(snapshot, dict):
            return {"error": "Failed to fetch circle state"}
        profile = next(
            (u for u in _list_under(snapshot, "users") or [] if isinstance(u, dict) and str(u.get("pid")) == pid),
            None,
        )
        if profile is None:
            return {"error": f"No profile with pid {pid}"}
        overall = snapshot.get("overall") if isinstance(snapshot.get("overall"), dict) else {}

        tz, tz_name = _zone(overall.get("timezone"))
        today_dt = (now or datetime.datetime.now(datetime.timezone.utc)).astimezone(tz)
        weekday = (today_dt.weekday() + 1) % 7  # Sunday = 0
        weekend = str(profile.get("weekend") or _DEFAULT_WEEKEND)
        day_type = "weekend" if str(weekday) in weekend else "weekdays"

        limits: Dict[str, float] = {}
        for limit in profile.get("timeLimits") or []:
            if not isinstance(limit, dict) or limit.get("category") is None:
                continue
            if not _limit_applies(limit.get("days"), day_type, weekday):
                continue
            minutes = _num(limit.get("minutes"))
            if minutes is None or minutes < 0:
                continue  # -1 / missing = no limit
            cat = str(limit["category"])
            limits[cat] = min(minutes, limits.get(cat, minutes))

        extensions: Dict[str, float] = {}
        ext_root = profile.get("extensions") if isinstance(profile.get("extensions"), dict) else {}
        for ext in ext_root.get("limits") or []:
            if not isinstance(ext, dict) or ext.get("category") is None:
                continue
            if not _is_today(ext.get("date"), today_dt.date(), tz):
                continue
            cat = str(ext["category"])
            extensions[cat] = extensions.get(cat, 0) + (_num(ext.get("minutes")) or 0)

        usage = self._request("/api/LOGS/usage/categories", query={"user": pid, "day": "0", "ndays": "1"})
        used = _usage_minutes(usage)

        entries = []
        for cat in sorted(limits):
            limit_min = limits[cat]
            ext_min = extensions.get(cat, 0)
            used_min = sum(used.values()) if cat == TOTAL_CATEGORY else used.get(cat, 0)
            allowed = limit_min + ext_min
            entries.append({
                "category": cat,
                "categoryName": self.category_name(cat),
                "limitMinutes": _tidy(limit_min),
                "extensionMinutes": _tidy(ext_min),
                "allowedMinutes": _tidy(allowed),
                "usedMinutes": _tidy(used_min),
                "remainingMinutes": _tidy(max(allowed - used_min, 0)),
                "exceeded": used_min >= allowed,
            })

        toggles = profile.get("toggles") if isinstance(profile.get("toggles"), dict) else {}
        mode = profile.get("mode")
        result = {
            "profile_pid": pid,
            "name": profile.get("name"),
            "mode": mode,
            "paused": str(mode).lower() == "pause",
            "circlePaused": str(overall.get("mode")).lower() == "pause",
            "timeLimitsEnabled": str(toggles.get("timeLimits")).lower() == "on",
            "timezone": tz_name,
            "date": today_dt.date().isoformat(),
            "weekday": _WEEKDAY_NAMES[weekday],
            "dayType": day_type,
            "totalUsedMinutes": _tidy(sum(used.values())),
            "limits": entries,
        }
        if not isinstance(usage, dict):
            result["warning"] = "Today's usage could not be fetched; used minutes are shown as 0"
        elif entries and not result["timeLimitsEnabled"]:
            result["note"] = "Time limits are configured but switched off for this profile"
        return result


# -----------------------------
# Parsing helpers
# -----------------------------

def flatten_history(result) -> List[Dict]:
    """report[].data[].sites[] -> flat entries, each tagged with its device `uid`.

    Recognized fields are normalized (`site`, `time`, `state`, `category`);
    everything else on an entry is kept unchanged.
    """
    entries = []
    for report in _list_under(result, "report") or []:
        if not isinstance(report, dict):
            continue
        groups = report.get("data")
        if isinstance(groups, dict):
            groups = [groups]
        for group in groups or []:
            if not isinstance(group, dict):
                continue
            uid = group.get("uid")
            for raw in group.get("sites") or []:
                if isinstance(raw, dict):
                    entry = dict(raw)
                elif raw is not None:
                    entry = {"site": raw}
                else:
                    continue
                if "category" not in entry:
                    for key in _CATEGORY_KEYS[1:]:
                        if entry.get(key) not in (None, ""):
                            entry["category"] = entry[key]
                            break
                if entry.get("category") not in (None, ""):
                    entry["category"] = str(entry["category"])
                else:
                    entry.pop("category", None)
                entry.setdefault("uid", uid)
                entries.append(entry)
    return entries


def _minutes(value) -> int:
    """Circle reports minutes as zero-padded strings ("00025")."""
    try:
        return int(str(value).strip() or 0)
    except ValueError:
        return 0


def _hours(minutes: int) -> float:
    return round(minutes / 60, 2)


def _int_or(value, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _list_under(result, key: str) -> Optional[List]:
    """`result[key]` when it is a list, `result` itself when it is a list, else None."""
    if isinstance(result, list):
        return result
    if isinstance(result, dict) and isinstance(result.get(key), list):
        return result[key]
    return None


def _collect_names(item, names: Dict[str, str]) -> None:
    if not isinstance(item, dict):
        return
    cid, name = item.get("id"), item.get("name")
    if cid is not None and name:
        names[str(cid)] = str(name)
    for app in item.get("apps") or []:
        if isinstance(app, dict) and app.get("id") is not None and app.get("name"):
            names.setdefault(str(app["id"]), str(app["name"]))


def _num(value) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _tidy(value: float):
    """Whole minutes as int, fractional ones rounded to a tenth."""
    return int(value) if float(value).is_integer() else round(value, 1)


def _usage_minutes(usage) -> Dict[str, float]:
    """Category id -> minutes from the first usage report (the day asked for)."""
    reports = _list_under(usage, "report") or []
    data = reports[0].get("data") if reports and isinstance(reports[0], dict) else None
    if not isinstance(data, dict):
        return {}
    return {str(k): _num(v) or 0 for k, v in data.items()}


def _limit_applies(days, day_type: str, weekday: int) -> bool:
    """A limit's `days` is `weekdays` (also the default) or `weekend`; tolerate index strings too."""
    text = str(days or "weekdays").strip().lower()
    if text in ("weekdays", "weekday"):
        return day_type == "weekdays"
    if text in ("weekend", "weekends"):
        return day_type == "weekend"
    if text in ("everyday", "all", "daily"):
        return True
    if text.isdigit():
        return str(weekday) in text
    return day_type == "weekdays"


def _zone(name):
    """(tzinfo, name) for an IANA zone; UTC when missing or unknown."""
    if name:
        try:
            from zoneinfo import ZoneInfo

            return ZoneInfo(str(name)), str(name)
        except Exception:
            pass
    return datetime.timezone.utc, "UTC"


def _is_today(value, today: datetime.date, tz) -> bool:
    """Whether an extension's `date` is today. Missing or unreadable dates count as today."""
    if value in (None, ""):
        return True
    num = _num(value)
    text = str(value).strip()
    if num is not None and not ("-" in text or "/" in text) and len(text) != 8:
        seconds = num / 1000 if num > 1e11 else num
        try:
            return datetime.datetime.fromtimestamp(seconds, tz).date() == today
        except (OverflowError, OSError, ValueError):
            return True
    for fmt, width in (("%Y-%m-%d", 10), ("%Y%m%d", 8), ("%m/%d/%Y", 10)):
        try:
            return datetime.datetime.strptime(text[:width], fmt).date() == today
        except ValueError:
            continue
    return True
