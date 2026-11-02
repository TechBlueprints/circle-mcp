"""
Circle Go child-device client (PROTOCOL.md Part C).

A Circle Go device is a phone or tablet the parent manages through the Circle
app in Kid mode. This client plays that role: it enrolls itself as a device,
then checks in and fetches the device's rule bundle with its own device token
(`gotoken`), which is unrelated to the parent's admin token.

Enrollment (all GET, VC host, port 443, no auth):

1. `/api/REGISTER/newdevice`  -> deviceid + short-lived vctoken
2. the parent approves, either with `/api/AUTHORIZE/godevice` on the admin API
   (`GoDeviceAdminMixin.authorize_go_device`) or by entering the passcode from
   `/api/REGISTER/passcode`
3. `/api/REGISTER/godevice`   -> gotoken, refresh-token, circleid, host
4. `/api/v0.1/a/registerDevice` (port 8995, device auth) describes the device

Port-8995 calls carry `Authorization: Bearer <gotoken>` plus `circleid` and
`deviceid` query parameters. A 401 triggers one token refresh on the auth host
and a single retry; the refresh may report that the parent turned management
off (`disable-management`).

The enrolled identity and tokens persist in a JSON file (mode 0600), by default
`~/.circle-mcp/go-device.json`, overridable with CIRCLE_GO_STATE_FILE.

Stdlib only.
"""

import base64
import io
import json
import os
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from typing import Dict, Optional, Tuple

DEFAULT_VC_HOST = "vc.meetcircle-blue.co"
DEFAULT_AUTH_BASE = "https://auth.meetcircle-blue.co"
DEVICE_PORT = 8995

# What the client remembers between runs; anything else in the file is ignored.
STATE_FIELDS = (
    "app_id",
    "device_id",
    "device_name",
    "circle_id",
    "vc_host",
    "gotoken",
    "refresh_token",
    "vctoken",
    "management_disabled",
    "last_ping",
)

# Unpacked bundle file name, keyed by the suffix that selects the archive entry.
SETTINGS_ENTRIES = (
    ("bin", "settings.bin"),
    ("customs", "customs.bin"),
    ("userinfo", "userinfo.bin"),
)

# Status values from /api/v0.1/a/ping that mean the device is still managed.
MANAGED_PING_STATUSES = ("registered", "ok", "awol")

USER_INFO_PROBES = (
    "/api/USERINFO",
    "/api/USERPHOTO",
    "/api/v1.0/godevice/userinfo",
    "/api/v1.0/godevice/userphoto",
)


class GoDeviceError(RuntimeError):
    """The child-device API refused a request or answered without what we need."""


class GoDeviceAuthError(GoDeviceError):
    """The device token could not be renewed; the device has to enroll again."""


class GoDeviceUnmanagedError(GoDeviceError):
    """The parent removed this device from management."""


def default_state_path() -> str:
    return os.environ.get("CIRCLE_GO_STATE_FILE") or os.path.join(
        os.path.expanduser("~"), ".circle-mcp", "go-device.json"
    )


def load_state(path: str) -> Dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: data[k] for k in STATE_FIELDS if k in data}


def save_state(path: str, state: Dict) -> None:
    """Write the state file atomically, readable by the owner only."""
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, mode=0o700, exist_ok=True)
    tmp = f"{path}.tmp-{os.getpid()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, sort_keys=True)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _bare_host(value: str) -> str:
    """`https://vc-7.example.co:443/x` or `vc-7.example.co` -> `vc-7.example.co`."""
    value = (value or "").strip()
    if not value:
        return ""
    if "://" not in value:
        value = f"https://{value}"
    return urllib.parse.urlsplit(value).hostname or ""


def _is_true(value) -> bool:
    return value is True or str(value).strip().lower() == "true"


def _first(data: Dict, *keys: str) -> str:
    for key in keys:
        value = data.get(key)
        if value:
            return str(value).strip()
    return ""


def _decode_body(content_type: str, raw: bytes):
    """JSON when it parses, text when it decodes, else a size + base64 preview."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return {
            "bytes": len(raw),
            "content_type": content_type,
            "base64_head": base64.b64encode(raw[:96]).decode("ascii"),
        }
    try:
        return json.loads(text)
    except ValueError:
        return text[:2000]


class GoDeviceClient:
    """Acts as an enrolled Circle Go child device."""

    def __init__(
        self,
        state_path: str = "",
        vc_host: str = "",
        auth_base_url: str = "",
        user_agent: str = "circle-mcp/1.0 (go-device)",
        timeout_s: int = 30,
    ):
        self.state_path = state_path or default_state_path()
        self._auth_base = (auth_base_url or DEFAULT_AUTH_BASE).rstrip("/")
        self._user_agent = user_agent
        self._timeout_s = timeout_s
        self._ssl_ctx = None

        state = load_state(self.state_path)
        self.app_id = state.get("app_id", "")
        self.device_id = state.get("device_id", "")
        self.device_name = state.get("device_name", "")
        self.circle_id = state.get("circle_id", "")
        # A host handed out at enrollment outlives whatever default we were given.
        self.vc_host = state.get("vc_host") or _bare_host(vc_host) or DEFAULT_VC_HOST
        self.gotoken = state.get("gotoken", "")
        self.refresh_token = state.get("refresh_token", "")
        self.vctoken = state.get("vctoken", "")
        self._management_disabled = bool(state.get("management_disabled", False))
        self.last_ping = state.get("last_ping")

    # -----------------------------
    # State
    # -----------------------------

    @property
    def disable_management(self) -> bool:
        """True once a token refresh reported that the parent unmanaged this device."""
        return self._management_disabled

    @property
    def is_enrolled(self) -> bool:
        return bool(self.gotoken and self.device_id and self.circle_id)

    def to_state(self) -> Dict:
        return {
            "app_id": self.app_id,
            "device_id": self.device_id,
            "device_name": self.device_name,
            "circle_id": self.circle_id,
            "vc_host": self.vc_host,
            "gotoken": self.gotoken,
            "refresh_token": self.refresh_token,
            "vctoken": self.vctoken,
            "management_disabled": self._management_disabled,
            "last_ping": self.last_ping,
        }

    def save(self) -> None:
        save_state(self.state_path, self.to_state())

    def ensure_app_id(self) -> str:
        """The per-install id, created once and kept for the life of the state file."""
        if not self.app_id:
            self.app_id = uuid.uuid4().hex
        return self.app_id

    # -----------------------------
    # HTTP
    # -----------------------------

    def _get_ssl_context(self):
        if not self._ssl_ctx:
            self._ssl_ctx = ssl.create_default_context()
        return self._ssl_ctx

    def _fetch(self, url: str, headers: Optional[Dict] = None) -> Tuple[int, str, bytes]:
        """GET `url` -> (status, content type, body). Non-2xx raises HTTPError."""
        all_headers = {"Accept": "application/json, */*", "User-Agent": self._user_agent}
        if headers:
            all_headers.update(headers)
        req = urllib.request.Request(url, headers=all_headers, method="GET")
        with urllib.request.urlopen(req, timeout=self._timeout_s, context=self._get_ssl_context()) as resp:
            return resp.status, resp.headers.get("Content-Type", ""), resp.read()

    def _get_json(self, url: str, headers: Optional[Dict] = None) -> Dict:
        _status, content_type, raw = self._fetch(url, headers)
        body = _decode_body(content_type, raw)
        return body if isinstance(body, dict) else {"_raw": body}

    @staticmethod
    def _with_query(url: str, query: Optional[Dict]) -> str:
        if not query:
            return url
        qs = urllib.parse.urlencode({k: str(v) for k, v in query.items() if v is not None})
        return f"{url}{'&' if '?' in url else '?'}{qs}"

    def _register_url(self, path: str, query: Dict) -> str:
        return self._with_query(f"https://{self.vc_host}{path}", query)

    def _device_url(self, path: str, query: Optional[Dict] = None) -> str:
        params = dict(query or {})
        params["circleid"] = self.circle_id
        params["deviceid"] = self.device_id
        return self._with_query(f"https://{self.vc_host}:{DEVICE_PORT}{path}", params)

    def _device_fetch(self, path: str, query: Optional[Dict] = None) -> Tuple[int, str, bytes]:
        """Port-8995 GET with device auth; on 401 refreshes the token and retries once."""
        if not self.is_enrolled:
            raise GoDeviceError("No enrolled Circle Go device: run the enrollment first")
        if self._management_disabled:
            raise GoDeviceUnmanagedError("The parent turned off management for this device")
        try:
            return self._fetch(self._device_url(path, query), {"Authorization": f"Bearer {self.gotoken}"})
        except urllib.error.HTTPError as e:
            if e.code != 401:
                raise
        self.refresh_device_token()
        if self._management_disabled:
            raise GoDeviceUnmanagedError("The parent turned off management for this device")
        return self._fetch(self._device_url(path, query), {"Authorization": f"Bearer {self.gotoken}"})

    def _device_json(self, path: str, query: Optional[Dict] = None) -> Dict:
        _status, content_type, raw = self._device_fetch(path, query)
        body = _decode_body(content_type, raw)
        return body if isinstance(body, dict) else {"_raw": body}

    # -----------------------------
    # Enrollment (C.1)
    # -----------------------------

    def register_new_device(self, app_id: str, device_name: str) -> Dict:
        """Step 1: announce the device; keeps the returned deviceid and vctoken."""
        result = self._get_json(
            self._register_url("/api/REGISTER/newdevice", {"appid": app_id, "devicename": device_name})
        )
        device_id = _first(result, "deviceid")
        vctoken = _first(result, "vctoken")
        if not device_id or not vctoken:
            raise GoDeviceError(f"newdevice was refused: {result.get('error') or result}")
        self.app_id = app_id
        self.device_name = device_name
        self.device_id = device_id
        self.vctoken = vctoken
        # A fresh enrollment starts from a clean slate.
        self.circle_id = self.gotoken = self.refresh_token = ""
        self._management_disabled = False
        self.last_ping = None
        self.save()
        return result

    def request_passcode(self) -> Dict:
        """Ask the cloud for a passcode the parent can enter to approve this device."""
        if not self.vctoken:
            raise GoDeviceError("No pairing token yet: call register_new_device first")
        return self._get_json(self._register_url("/api/REGISTER/passcode", {"vctoken": self.vctoken}))

    def claim_device_token(self, sign_token: bool = False) -> Dict:
        """Step 3: once the parent approved, collect the device token and circle."""
        if not (self.app_id and self.device_id and self.vctoken):
            raise GoDeviceError("Enrollment not started: call register_new_device first")
        result = self._get_json(self._register_url("/api/REGISTER/godevice", {
            "appid": self.app_id,
            "devicename": self.device_name,
            "deviceid": self.device_id,
            "vctoken": self.vctoken,
            "sign-token": "true" if sign_token else "false",
        }))
        gotoken = _first(result, "gotoken", "access-token", "access_token")
        if not gotoken:
            raise GoDeviceError(
                f"No device token yet (has the parent approved the device?): {result.get('error') or result}"
            )
        self.gotoken = gotoken
        self.refresh_token = _first(result, "refresh-token", "refresh_token") or self.refresh_token
        self.circle_id = _first(result, "circleid") or self.circle_id
        self.vc_host = _bare_host(_first(result, "host")) or self.vc_host
        self.vctoken = _first(result, "vctoken") or self.vctoken
        self._management_disabled = False
        self.save()
        return result

    def describe_device(self, home_ssid: str, cuuid: str = "") -> Dict:
        """Step 4: tell the cloud the device name, home Wi-Fi, and Circle unit (if any)."""
        return self._device_json("/api/v0.1/a/registerDevice", {
            "devname": self.device_name,
            "homeSSID": home_ssid,
            "cuuid": cuuid,
        })

    # -----------------------------
    # Device auth (C.2)
    # -----------------------------

    def refresh_device_token(self) -> Dict:
        """Renew gotoken with the refresh token; records the disable-management flag."""
        if not self.refresh_token:
            raise GoDeviceAuthError("No refresh token stored: enroll the device again")
        try:
            result = self._get_json(
                f"{self._auth_base}/api/v1.0/refresh",
                {"Authorization": f"Bearer {self.refresh_token}"},
            )
        except urllib.error.HTTPError as e:
            raise GoDeviceAuthError(f"Device token refresh rejected: HTTP {e.code}") from e
        self._management_disabled = _is_true(result.get("disable-management"))
        gotoken = _first(result, "gotoken", "access-token", "access_token")
        if gotoken:
            self.gotoken = gotoken
            self.refresh_token = _first(result, "refresh-token", "refresh_token") or self.refresh_token
        self.save()
        if not gotoken and not self._management_disabled:
            raise GoDeviceAuthError(f"Device token refresh returned no token: {result.get('error') or result}")
        return result

    # -----------------------------
    # Check-in and settings (C.3)
    # -----------------------------

    def ping(self, device_name: str = "") -> Dict:
        """Heartbeat. Returns {"status", "managed", "at"} and remembers it.

        `managed` is True for the healthy statuses, False when the cloud no longer
        knows the device (or a refresh said management is off), None otherwise.
        """
        try:
            body = self._device_json("/api/v0.1/a/ping", {"devname": device_name or self.device_name})
        except GoDeviceUnmanagedError:
            body = {"status": "not registered", "error": "the parent turned off management for this device"}
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", errors="replace")
            if "not registered" not in text.lower():
                raise
            body = {"status": "not registered", "error": text[:200]}

        status = str(body.get("status") or "").strip()
        error = str(body.get("error") or "")
        if status.lower() in MANAGED_PING_STATUSES:
            managed = True
        elif status.lower() == "not registered" or "not registered" in error.lower():
            managed = False
        else:
            managed = None  # an answer we do not recognize: neither confirmed nor removed
        result = {"status": status or None, "managed": managed, "at": int(time.time())}
        if error:
            result["error"] = error[:200]
        self.last_ping = result
        self.save()
        return result

    def download_settings_bundle(self, dest_dir: str) -> Dict:
        """Fetch the rule bundle ZIP into dest_dir and unpack its three binaries."""
        _status, content_type, raw = self._device_fetch("/api/v0.1/a/settings")
        try:
            archive = zipfile.ZipFile(io.BytesIO(raw))
        except zipfile.BadZipFile:
            raise GoDeviceError(f"settings did not return a ZIP archive ({content_type}): {raw[:200]!r}")

        os.makedirs(dest_dir, exist_ok=True)
        zip_path = os.path.join(dest_dir, "settings.zip")
        with open(zip_path, "wb") as f:
            f.write(raw)

        files, unmatched = {}, []
        with archive:
            for info in archive.infolist():
                if info.is_dir():
                    continue
                target = next((out for suffix, out in SETTINGS_ENTRIES if info.filename.endswith(suffix)), None)
                if target is None or target in files:
                    unmatched.append(info.filename)
                    continue
                data = archive.read(info)
                # Only our own fixed names are written: entry paths never touch the filesystem.
                with open(os.path.join(dest_dir, target), "wb") as f:
                    f.write(data)
                files[target] = {"entry": info.filename, "bytes": len(data)}
        return {
            "dest_dir": os.path.abspath(dest_dir),
            "archive": os.path.abspath(zip_path),
            "archive_bytes": len(raw),
            "files": files,
            "unmatched_entries": unmatched,
        }

    def notify_guest_mode(self, profile_name: str) -> Dict:
        return self._device_json("/api/v0.1/a/notify/guestMode", {"profilename": profile_name})

    def notify_dev_options(self, profile_name: str) -> Dict:
        return self._device_json("/api/v0.1/a/notify/devOptions", {"profilename": profile_name})

    def notify_timezone_change(self) -> Dict:
        return self._device_json("/api/v0.1/a/notify/timeZoneChange")

    # -----------------------------
    # Declared-but-unused endpoints (C.4)
    # -----------------------------

    def probe_user_info(self) -> Dict:
        """Hit each C.4 endpoint once and report what came back, failures included."""
        results = {}
        for path in USER_INFO_PROBES:
            try:
                status, content_type, raw = self._device_fetch(path)
            except urllib.error.HTTPError as e:
                status = e.code
                content_type = e.headers.get("Content-Type", "") if e.headers else ""
                raw = e.read() or b""
            except urllib.error.URLError as e:
                results[path] = {"status": None, "error": str(getattr(e, "reason", e))}
                continue
            results[path] = {"status": status, "content_type": content_type, "body": _decode_body(content_type, raw)}
        return results


class GoDeviceAdminMixin:
    """Parent-side half of Circle Go enrollment, mixed into CircleClient."""

    def authorize_go_device(self, app_id: str, device_id: str) -> Optional[Dict]:
        """Approve a device that announced itself with /api/REGISTER/newdevice."""
        return self._request("/api/AUTHORIZE/godevice", query={"appid": app_id, "deviceid": device_id})
