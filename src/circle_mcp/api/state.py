"""
Circle state reads (PROTOCOL.md B.2) and per-circle VC host discovery (B.1.4).

Mixed into CircleClient; relies on its `_request`, `_http_json`, `ensure_admin_token`
and token/host attributes. Responses are parsed tolerantly: sections may be missing,
lists may arrive bare or wrapped, and many numbers and flags arrive as strings.
"""

import base64
import json
import urllib.error
import urllib.parse
from typing import Dict, List, Optional

SNAPSHOT_SECTIONS = ("overall", "users", "devices", "hardware", "categories")

_DEVICE_FIELDS = (
    "uid", "displayName", "hostname", "ip", "manufacturer", "brand", "model", "mode",
    "type", "classification", "rank", "alias", "isGo", "newlyAdded", "defaultPlatforms", "pid",
)
_DEVICE_BOOLS = ("isGo", "newlyAdded")


def base_url(host: str) -> str:
    """`vc.example.com` or `https://vc.example.com/` -> `https://vc.example.com`."""
    host = host.strip().rstrip("/")
    if "://" not in host:
        host = f"https://{host}"
    return host


def _as_bool(value):
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "1", "yes", "on"):
            return True
        if lowered in ("false", "0", "no", "off", ""):
            return False
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return value


def _as_int(value):
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value.strip()))
        except ValueError:
            return value
    return value


def _items(result, key: str) -> List[Dict]:
    """The dict entries of `result[key]`, or of `result` itself when it is already a list."""
    if isinstance(result, dict):
        result = result.get(key)
    if not isinstance(result, list):
        return []
    return [item for item in result if isinstance(item, dict)]


def _jwt_claim(token: str, claim: str):
    try:
        payload_b64 = token.split(".")[1].encode()
        payload_b64 += b"=" * (-len(payload_b64) % 4)
        return json.loads(base64.urlsafe_b64decode(payload_b64)).get(claim)
    except Exception:
        return None


def normalize_profile(profile: Dict) -> Dict:
    pid = profile.get("pid") if profile.get("pid") is not None else profile.get("id")
    entry = {
        "pid": str(pid) if pid is not None else None,
        "name": profile.get("name"),
        "mode": profile.get("mode") or profile.get("type"),
        "ageCategory": profile.get("ageCategory"),
        "role": profile.get("role"),
    }
    avatar = profile.get("avatarUrl") or profile.get("photo")
    if avatar:
        entry["avatarUrl"] = avatar
    return entry


def normalize_device(device: Dict) -> Dict:
    entry = {k: device[k] for k in _DEVICE_FIELDS if k in device}
    for k in _DEVICE_BOOLS:
        if k in entry:
            entry[k] = _as_bool(entry[k])
    if "rank" in entry:
        entry["rank"] = _as_int(entry["rank"])
    return entry


def normalize_hardware(unit: Dict) -> Dict:
    """Summarize a Circle Home unit. The hotspot password is deliberately left out."""

    def nested(*path):
        node = unit
        for key in path:
            if not isinstance(node, dict):
                return None
            node = node.get(key)
        return node

    entry = {
        "cuuid": unit.get("cuuid"),
        "model": unit.get("model"),
        "isOnline": _as_bool(unit.get("isOnline")),
        "isEnabled": _as_bool(unit.get("isEnabled")),
        "compatibleMode": unit.get("compatibleMode"),
        "networkStatus": nested("network", "status"),
        "wifiSsid": nested("wifi", "ssid"),
        "hotspotSsid": nested("hotspot", "ssid"),
        "wifiRssi": _as_int(nested("stats", "wifi", "rssi")),
        "firmware": nested("version", "firmware"),
    }
    return {k: v for k, v in entry.items() if v is not None}


def _redact_hardware(section):
    """Copy of a raw `hardware` section without hotspot passwords."""
    if not isinstance(section, list):
        return section
    cleaned = []
    for unit in section:
        if isinstance(unit, dict) and isinstance(unit.get("hotspot"), dict):
            unit = dict(unit, hotspot={k: v for k, v in unit["hotspot"].items() if k != "password"})
        cleaned.append(unit)
    return cleaned


class StateMixin:
    """Read-only circle state: snapshot, profiles, devices, hardware, categories, badges."""

    DEFAULT_REGISTER_BASE = "https://register.meetcircle-blue.co"
    GO_PORT = 8995

    # -----------------------------
    # VC host discovery (B.1.4)
    # -----------------------------

    def _discover_vc_host(self) -> Optional[str]:
        """Point `_vc_base` at this circle's VC host. Any failure keeps the current host."""
        if getattr(self, "_vc_pinned", False) or not self._admin_token:
            return None
        circle_id = self._circle_id or _jwt_claim(self._admin_token, "circleid")
        if not circle_id:
            return None
        register = getattr(self, "_register_base", self.DEFAULT_REGISTER_BASE)
        qs = urllib.parse.urlencode({"circleid": str(circle_id), "token": self._admin_token})
        try:
            result = self._http_json(f"{register}/api/v1.0/host?{qs}")
        except (OSError, ValueError):
            return None
        host = result.get("host") if isinstance(result, dict) else None
        if not isinstance(host, str) or not host.strip():
            return None
        self._vc_base = base_url(host)
        return self._vc_base

    # -----------------------------
    # Snapshot (QUERY/all)
    # -----------------------------

    def get_snapshot(self, section: Optional[str] = None):
        """Everything from `/api/QUERY/all`, or just one section of it."""
        if section is not None and section not in SNAPSHOT_SECTIONS:
            raise ValueError(f"section must be one of {', '.join(SNAPSHOT_SECTIONS)}")
        result = self._request("/api/QUERY/all")
        if not isinstance(result, dict):
            raise RuntimeError("Circle returned no snapshot")
        snapshot = dict(result)
        if "hardware" in snapshot:
            snapshot["hardware"] = _redact_hardware(snapshot["hardware"])
        if section is None:
            return snapshot
        if section not in snapshot:
            raise RuntimeError(f"Circle snapshot has no {section!r} section")
        return snapshot[section]

    def _snapshot_section(self, section: str):
        """One QUERY/all section, or None if the call or the section is unavailable."""
        try:
            result = self._request("/api/QUERY/all")
        except OSError:
            return None
        if isinstance(result, dict):
            return result.get(section)
        return None

    def list_profiles(self) -> List[Dict]:
        """Profiles from QUERY/all `users`, falling back to `/api/QUERY/users`."""
        users = self._snapshot_section("users")
        if not isinstance(users, list):
            users = _items(self._request("/api/QUERY/users"), "users")
        return [normalize_profile(u) for u in users if isinstance(u, dict)]

    def get_circle_status(self) -> Optional[Dict]:
        """QUERY/all `overall`, falling back to `/api/QUERY/circleinfo`."""
        overall = self._snapshot_section("overall")
        if isinstance(overall, dict):
            return overall
        return self._request("/api/QUERY/circleinfo")

    # -----------------------------
    # Individual sections
    # -----------------------------

    def get_overall(self) -> Optional[Dict]:
        result = self._request("/api/QUERY/overall")
        if isinstance(result, dict) and isinstance(result.get("overall"), dict):
            return result["overall"]
        return result

    def list_devices(self) -> List[Dict]:
        return [normalize_device(d) for d in _items(self._request("/api/QUERY/devices"), "devices")]

    def list_hardware(self) -> List[Dict]:
        return [normalize_hardware(h) for h in _items(self._request("/api/QUERY/hardware"), "hardware")]

    def hardware_is_online(self, cuuid: str) -> Optional[bool]:
        result = self._request("/api/QUERY/hardware/hardware/isOnline", query={"hardware.cuuid": cuuid})
        if not isinstance(result, dict):
            return None
        for key in ("isOnline", "online", "result"):
            if key in result:
                value = _as_bool(result[key])
                return value if isinstance(value, bool) else None
        return None

    def list_categories(self) -> List[Dict]:
        """Filter categories from the legacy catalog, with names from the v2 catalog where it has them."""
        try:
            legacy = _items(self._request("/api/QUERY/categories"), "categories")
        except OSError:
            legacy = []
        try:
            newer = _items(self._request("/api/v2/categories"), "categories")
        except OSError:
            newer = []
        if not legacy and not newer:
            raise RuntimeError("Circle returned no category catalog")

        merged: Dict[str, Dict] = {}
        for cat in legacy:
            if cat.get("id") is None:
                continue
            merged[str(cat["id"])] = {
                "id": str(cat["id"]),
                "name": cat.get("name"),
                "mainCategory": cat.get("mainCategory"),
                "description": cat.get("description"),
            }
        for cat in newer:
            if cat.get("id") is None:
                continue
            entry = merged.setdefault(str(cat["id"]), {
                "id": str(cat["id"]),
                "name": None,
                "mainCategory": cat.get("mainCategory"),
                "description": cat.get("description"),
            })
            if cat.get("name"):
                entry["name"] = cat["name"]
            if not entry.get("description") and cat.get("description"):
                entry["description"] = cat["description"]
        return list(merged.values())

    def get_profile_badges(self) -> List[Dict]:
        result = self._request("/api/v2/profiles/badges")
        badges = []
        for item in _items(result, "badges") or _items(result, "profiles"):
            if item.get("pid") is None:
                continue
            entries = item.get("badges")
            badges.append({"pid": str(item["pid"]), "badges": entries if isinstance(entries, list) else []})
        return badges

    # -----------------------------
    # Managed child devices (port 8995)
    # -----------------------------

    def _dev_status(self):
        token = self.ensure_admin_token()
        if not token:
            return None
        return self._request("/api/v0.1/c/devStatus", query={"gotoken": token}, port=self.GO_PORT)

    def list_go_devices(self) -> Dict:
        """Child devices running the Circle app, from devStatus on the child-device port."""
        try:
            result = self._dev_status()
        except urllib.error.HTTPError as e:
            if e.code not in (401, 403):
                raise
            # `_request` re-granted, but the stale token was baked into `gotoken`.
            result = self._dev_status()
        if not isinstance(result, dict):
            raise RuntimeError("Circle returned no device status")
        devices = []
        for device in _items(result, "devices"):
            entry = {k: device.get(k) for k in ("deviceid", "circleid", "state", "last") if k in device}
            if "last" in entry:
                entry["last"] = _as_int(entry["last"])
            devices.append(entry)
        return {
            "authStatus": result.get("authStatus"),
            "totalDeviceCount": _as_int(result.get("totalDeviceCount", len(devices))),
            "enabledDeviceCount": _as_int(result.get("enabledDeviceCount")),
            "devices": devices,
        }
