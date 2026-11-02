"""
Circle (Meetcircle / Aura) API client.

Auth flow:

1. Sign in to Aura (`POST {aura}/auth/session/signin`, `Authorization: Public <key>`)
   with the account email/password -> access_token + refresh_token
2. Grant an admin token (`POST {auth}/api/v2/grant/admin`) with
   {"deviceid": ..., "jwt": <access_token>} -> admin token
3. Call VC endpoints (`{vc}/api/...`) with `Authorization: Bearer <admin_token>`

Error semantics observed from the live API (2026-08):
- 401 {"message": "NoCredential"}       -> missing/invalid `Authorization: Public <key>` header
- 401 {"message": "InvalidCredentials"} -> key accepted, username/password rejected

Stdlib only; no external dependencies.
"""

import base64
import json
import ssl
import time
import urllib.parse
import urllib.request
import urllib.error
from typing import Optional, Dict, List

from .api.usage import UsageMixin


class CircleAuthError(RuntimeError):
    """Sign-in or token grant rejected by the Circle/Aura API."""


class CircleClient(UsageMixin):
    """Read-only client for Circle parental controls."""

    DEFAULT_AURA_BASE = "https://api.aurasvc.io"
    DEFAULT_AUTH_BASE = "https://auth.meetcircle-blue.co"
    DEFAULT_VC_BASE = "https://vc.meetcircle-blue.co"

    def __init__(
        self,
        username: str = "",
        password: str = "",
        device_id: str = "circle-mcp",
        aura_public_key: str = "",
        admin_token: str = "",
        default_profile_pid: str = "",
        aura_base_url: str = "",
        auth_base_url: str = "",
        vc_base_url: str = "",
        user_agent: str = "circle-mcp/1.0",
        timeout_s: int = 30,
    ):
        self._username = username
        self._password = password
        self._device_id = device_id
        self._aura_public_key = aura_public_key
        self._admin_token = admin_token
        self._default_profile_pid = default_profile_pid
        self._aura_base = (aura_base_url or self.DEFAULT_AURA_BASE).rstrip("/")
        self._auth_base = (auth_base_url or self.DEFAULT_AUTH_BASE).rstrip("/")
        self._vc_base = (vc_base_url or self.DEFAULT_VC_BASE).rstrip("/")
        self._user_agent = user_agent
        self._timeout_s = timeout_s

        self._ssl_ctx = None
        self._circle_id = None
        self._aura_access_token = None
        self._aura_refresh_token = None

    @property
    def is_configured(self) -> bool:
        return bool(self._admin_token or (self._username and self._password))

    def _get_ssl_context(self):
        if not self._ssl_ctx:
            self._ssl_ctx = ssl.create_default_context()
        return self._ssl_ctx

    def _http_json(
        self,
        url: str,
        method: str = "GET",
        headers: Optional[Dict] = None,
        body: Optional[Dict] = None,
    ) -> Dict:
        all_headers = {
            "Accept": "application/json",
            "User-Agent": self._user_agent,
        }
        if headers:
            all_headers.update(headers)
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            all_headers["Content-Type"] = "application/json"

        req = urllib.request.Request(url, data=data, headers=all_headers, method=method)
        with urllib.request.urlopen(req, timeout=self._timeout_s, context=self._get_ssl_context()) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"_raw": raw}

    # -----------------------------
    # Token lifecycle
    # -----------------------------

    def ensure_admin_token(self) -> Optional[str]:
        """Return a valid admin token, signing in if necessary."""
        if self._admin_token and not _jwt_is_expired(self._admin_token, leeway_s=60):
            return self._admin_token

        if not self._username or not self._password:
            return self._admin_token or None

        if not self._aura_public_key:
            raise ValueError("aura_public_key is required for username/password login")

        access = self._ensure_aura_access_token()
        if not access:
            return None

        try:
            result = self._http_json(
                f"{self._auth_base}/api/v2/grant/admin",
                method="POST",
                body={"deviceid": self._device_id, "jwt": access},
            )
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:200]
            raise CircleAuthError(f"Admin token grant failed: HTTP {e.code} {detail}") from e
        admin_token = (result.get("token") or result.get("access-token") or "").strip()
        self._circle_id = result.get("circleId")
        self._admin_token = admin_token
        return admin_token or None

    def _ensure_aura_access_token(self) -> Optional[str]:
        if self._aura_access_token and not _jwt_is_expired(self._aura_access_token, leeway_s=60):
            return self._aura_access_token

        if self._aura_refresh_token:
            try:
                result = self._http_json(
                    f"{self._aura_base}/auth/session/refresh_token",
                    method="POST",
                    headers={"Authorization": f"Public {self._aura_public_key}"},
                    body={"refresh_token": self._aura_refresh_token},
                )
                access = (result.get("access_token") or "").strip()
                refresh = (result.get("refresh_token") or "").strip()
                if access:
                    self._aura_access_token = access
                    self._aura_refresh_token = refresh or self._aura_refresh_token
                    return access
            except Exception:
                pass  # fall through to full sign-in

        try:
            result = self._http_json(
                f"{self._aura_base}/auth/session/signin",
                method="POST",
                headers={"Authorization": f"Public {self._aura_public_key}"},
                body={"alias": self._username, "password": self._password},
            )
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:200]
            raise CircleAuthError(f"Aura sign-in failed: HTTP {e.code} {detail}") from e
        access = (result.get("access_token") or "").strip()
        refresh = (result.get("refresh_token") or "").strip()
        if not access:
            return None
        self._aura_access_token = access
        self._aura_refresh_token = refresh
        return access

    # -----------------------------
    # VC data endpoints
    # -----------------------------

    def _vc_url(self, path: str, query: Optional[Dict] = None, port: Optional[int] = None) -> str:
        """Build an admin-API URL. `port` overrides the host's port (e.g. 8995)."""
        base = self._vc_base
        if port is not None:
            parsed = urllib.parse.urlsplit(base)
            base = f"{parsed.scheme}://{parsed.hostname}:{port}"
        url = f"{base}{path}"
        if query:
            qs = urllib.parse.urlencode({k: str(v) for k, v in query.items() if v is not None})
            url = f"{url}{'&' if '?' in url else '?'}{qs}"
        return url

    def _request(
        self,
        path: str,
        method: str = "GET",
        query: Optional[Dict] = None,
        body: Optional[Dict] = None,
        port: Optional[int] = None,
    ) -> Optional[Dict]:
        """Admin-API call with the bearer token; re-grants once on 401/403.

        Legacy endpoints are GET with query params (`query`); v2 endpoints take a
        JSON `body` with POST/PUT/DELETE. Non-JSON responses come back as {"_raw": ...}.
        """
        token = self.ensure_admin_token()
        if not token:
            return None

        url = self._vc_url(path, query, port)
        try:
            return self._http_json(url, method=method, headers={"Authorization": f"Bearer {token}"}, body=body)
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                self._admin_token = ""
                token = self.ensure_admin_token()
                if not token:
                    return None
                return self._http_json(url, method=method, headers={"Authorization": f"Bearer {token}"}, body=body)
            raise

    def list_profiles(self) -> List[Dict]:
        """List Circle profiles (pid, name, type)."""
        result = self._request("/api/QUERY/users")
        if not result:
            return []

        users = result.get("users") if isinstance(result, dict) else result
        if not isinstance(users, list):
            return []

        profiles = []
        for profile in users:
            if not isinstance(profile, dict):
                continue
            profiles.append({
                "pid": profile.get("pid") or profile.get("id"),
                "name": profile.get("name"),
                "type": profile.get("type") or profile.get("mode"),
                "avatarUrl": profile.get("avatarUrl") or profile.get("photo"),
            })
        return profiles

    def get_usage_categories(
        self,
        profile_pid: Optional[str] = None,
        day: str = "0",
        ndays: str = "1",
    ) -> Dict:
        """Usage by category for a profile, plus `categoryNames`. day=0 is today; ndays = days to include."""
        pid = profile_pid or self._default_profile_pid
        if not pid:
            return {"error": "No profile ID specified"}

        qs = urllib.parse.urlencode({"user": str(pid), "day": str(day), "ndays": str(ndays)})
        result = self._request(f"/api/LOGS/usage/categories?{qs}")
        return self._attach_category_names(result) if result else {"error": "Failed to fetch usage data"}

    def get_profile_usage(
        self,
        profile_pid: Optional[str] = None,
        day: str = "0",
        ndays: str = "1",
    ) -> Dict:
        """Detailed usage for a profile."""
        pid = profile_pid or self._default_profile_pid
        if not pid:
            return {"error": "No profile ID specified"}

        qs = urllib.parse.urlencode({"user": str(pid), "day": str(day), "ndays": str(ndays)})
        result = self._request(f"/api/LOGS/usage?{qs}")
        return result if result else {"error": "Failed to fetch usage data"}

    def get_circle_status(self) -> Optional[Dict]:
        """Circle device/account status."""
        return self._request("/api/QUERY/circleinfo")


def _jwt_is_expired(token: str, leeway_s: int = 0) -> bool:
    """Best-effort JWT exp check. If it's not a JWT, treat as not expired."""
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return False
        payload_b64 = parts[1].encode("utf-8")
        payload_b64 += b"=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64).decode("utf-8", errors="ignore"))
        exp = payload.get("exp")
        if not isinstance(exp, (int, float)):
            return False
        return (time.time() + leeway_s) >= float(exp)
    except Exception:
        return False
