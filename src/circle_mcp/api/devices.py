"""
Devices, circle-wide settings, Circle Home hardware (PROTOCOL.md B.5), and the
parent-side calls on hosts other than `vc` (B.6).

Every B.5 call is a legacy GET with query parameters on the admin API. Writes
come back as {"ok": bool, "response": <raw body>}; see `_write_result`.

Deliberately absent: account deletion, hardware deletion, hardware pairing,
and the child-device approval call.
"""

import urllib.error
import urllib.parse
from typing import Callable, Dict, Optional

_OK_RESULTS = ("true", "success", "ok")


def _flag(value: bool) -> str:
    return "true" if value else "false"


class DevicesMixin:
    """CircleClient mixin: device/circle/hardware writes and the account, location, urldb hosts."""

    ACCOUNT_BASE = "https://accounts.meetcircle-blue.co"
    LOCATION_BASE = "https://location.meetcircle-blue.co"
    URLDB_BASE = "https://urldb.meetcircle-blue.co"

    # -----------------------------
    # Envelope
    # -----------------------------

    @staticmethod
    def _write_result(raw) -> Dict:
        """Wrap a write response with an `ok` flag (PROTOCOL.md A.3).

        Reaching here means HTTP 2xx (urllib raises otherwise), so the call counts
        as accepted unless the body says otherwise: a `result` that is not
        true/success/OK, or a non-empty `error`. No response at all (no admin
        token) is not ok.
        """
        if raw is None:
            return {"ok": False, "response": None}
        ok = True
        if isinstance(raw, dict):
            if "result" in raw and str(raw["result"]).strip().lower() not in _OK_RESULTS:
                ok = False
            if raw.get("error"):
                ok = False
        return {"ok": ok, "response": raw}

    def _write(self, path: str, query: Dict) -> Dict:
        return self._write_result(self._request(path, query=query))

    # -----------------------------
    # Devices (keyed by device.uid, a MAC address)
    # -----------------------------

    def rename_device(self, uid: str, name: str) -> Dict:
        return self._write("/api/UPDATE/devices/device/displayName", {"device.uid": uid, "value": name})

    def set_device_mode(self, uid: str, mode: str) -> Dict:
        """mode is `Filter` (normal) or `Pause` (no internet)."""
        if mode not in ("Filter", "Pause"):
            raise ValueError(f"device mode must be Filter or Pause, not {mode!r}")
        return self._write("/api/UPDATE/devices/device/mode", {"device.uid": uid, "value": mode})

    def forget_device(self, uid: str) -> Dict:
        return self._write("/api/DELETE/devices/device", {"device.uid": uid})

    # -----------------------------
    # Circle-wide settings
    # -----------------------------

    def set_overall_mode(self, mode: str) -> Dict:
        """Pause (`Pause`) or resume (`Filter`) the internet for the whole home."""
        if mode not in ("Filter", "Pause"):
            raise ValueError(f"circle mode must be Filter or Pause, not {mode!r}")
        return self._write("/api/UPDATE/overall/mode", {"value": mode})

    def set_timezone(self, tz: str) -> Dict:
        return self._write("/api/UPDATE/overall/timezone", {"value": tz})

    def set_locale(self, locale: str) -> Dict:
        return self._write("/api/UPDATE/overall/locale", {"value": locale})

    def set_default_assigned_profile(self, pid: str) -> Dict:
        return self._write("/api/UPDATE/overall/defaultAssignedProfile", {"value": pid})

    def dismiss_notification(self, notification_id: str) -> Dict:
        return self._write(
            "/api/ADD/overall/activeNotifications/notification",
            {"id": notification_id, "clear": "true"},
        )

    # -----------------------------
    # Circle Home hardware (keyed by cuuid)
    # -----------------------------

    def set_hardware_enabled(self, cuuid: str, enabled: bool) -> Dict:
        return self._write(
            "/api/UPDATE/hardware/hardware/isEnabled",
            {"hardware.cuuid": cuuid, "value": _flag(enabled)},
        )

    def set_hardware_compatible_mode(self, cuuid: str, enabled: bool) -> Dict:
        return self._write(
            "/api/UPDATE/hardware/hardware/compatibleMode",
            {"hardware.cuuid": cuuid, "value": _flag(enabled)},
        )

    def reboot_hardware(self, cuuid: str) -> Dict:
        return self._write("/api/POWER/reboot", {"cuuid": cuuid})

    # -----------------------------
    # Other hosts
    # -----------------------------

    def _bearer_get(self, url: str, token: Callable[[], Optional[str]], forget: Callable[[], None]) -> Dict:
        """GET `url` with a bearer from `token()`; on 401/403 call `forget()` and try once more."""
        bearer = token()
        if not bearer:
            raise RuntimeError(f"no credential available for {urllib.parse.urlsplit(url).hostname}")
        try:
            return self._http_json(url, headers={"Authorization": f"Bearer {bearer}"})
        except urllib.error.HTTPError as e:
            if e.code not in (401, 403):
                raise
        forget()
        bearer = token()
        if not bearer:
            raise RuntimeError(f"no credential available for {urllib.parse.urlsplit(url).hostname}")
        return self._http_json(url, headers={"Authorization": f"Bearer {bearer}"})

    def _aura_token_for_account(self) -> Optional[str]:
        if not self._aura_access_token and not (self._username and self._password):
            raise RuntimeError(
                "the account host needs an Aura sign-in; configure CIRCLE_USERNAME/CIRCLE_PASSWORD "
                "(an admin token alone is not enough)"
            )
        return self._ensure_aura_access_token()

    def _forget_aura_token(self) -> None:
        self._aura_access_token = None

    def _forget_admin_token(self) -> None:
        self._admin_token = ""

    def get_subscription(self) -> Dict:
        """Subscription state from the account host (Aura JWT, not the admin token)."""
        return self._bearer_get(
            f"{self.ACCOUNT_BASE}/api/v1.0/account/subscribe",
            self._aura_token_for_account,
            self._forget_aura_token,
        )

    def locate_child(self, child_device_id: str, provider: str = "fcm") -> Dict:
        """Ask a child device to report its location; the answer arrives by push, not here."""
        if not self.ensure_admin_token():
            raise RuntimeError("no admin token")
        if not self._circle_id:
            raise RuntimeError(
                "circle id unknown: it comes from the admin grant, so sign in with "
                "username/password rather than a pre-issued admin token"
            )
        qs = urllib.parse.urlencode({
            "circleid": self._circle_id,
            "parent_deviceid": self._device_id,
            "child_deviceid": child_device_id,
            "provider": provider,
        })
        return self._write_result(self._bearer_get(
            f"{self.LOCATION_BASE}/api/v1.0/locatechild?{qs}",
            self.ensure_admin_token,
            self._forget_admin_token,
        ))

    def lookup_domain_category(self, domain: str) -> Dict:
        """Circle's category id(s) for a domain. Unauthenticated."""
        qs = urllib.parse.urlencode({"d": domain, "model": "android"})
        return self._http_json(f"{self.URLDB_BASE}/circle/v1/category?{qs}")
