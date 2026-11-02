"""
Circle (Meetcircle / Aura) API client.

Auth flow:

1. Get an identity JWT for the account, by one of two backends:
   - Aura: `POST {aura}/auth/session/signin` with `Authorization: Public <key>`
     and the email/password -> access_token + refresh_token
   - Cognito (accounts from before the Aura migration): USER_SRP_AUTH against
     the Circle user pool -> id_token + refresh_token (see cognito.py)
   `auth_method` picks one; "auto" tries Cognito and falls back to Aura when
   the pool does not know the user (a probe that costs no password attempt).
2. Grant an admin token (`POST {auth}/api/v2/grant/admin`) with
   {"deviceid": ..., "jwt": <identity jwt>} -> admin token
3. Call VC endpoints (`{vc}/api/...`) with `Authorization: Bearer <admin_token>`

Error semantics observed from the live API (2026-08):
- 401 {"message": "NoCredential"}       -> missing/invalid `Authorization: Public <key>` header
- 401 {"message": "InvalidCredentials"} -> key accepted, username/password rejected
  (also what a Cognito-era account gets from Aura, whatever the password)

Stdlib only; no external dependencies.
"""

import base64
import json
import ssl
import time
import urllib.parse
import urllib.request
import urllib.error
from typing import Optional, Dict

from .cognito import CognitoError, CognitoSrp

from .api.state import StateMixin, base_url

from .api.usage import UsageMixin

from .api.profiles import ProfilesMixin

from .api.schedules import SchedulesMixin

from .api.devices import DevicesMixin

from .api.godevice import GoDeviceAdminMixin


class CircleAuthError(RuntimeError):
    """Sign-in or token grant rejected by the Circle/Aura API."""


class CircleClient(StateMixin, UsageMixin, ProfilesMixin, SchedulesMixin, DevicesMixin, GoDeviceAdminMixin):
    """Read-only client for Circle parental controls."""

    DEFAULT_AURA_BASE = "https://api.aurasvc.io"
    DEFAULT_AUTH_BASE = "https://auth.meetcircle-blue.co"
    DEFAULT_VC_BASE = "https://vc.meetcircle-blue.co"
    # Circle's production Cognito user pool / mobile app client (no client secret).
    DEFAULT_COGNITO_POOL_ID = "us-west-2_TiQsgtChT"
    DEFAULT_COGNITO_CLIENT_ID = "fd7ail96985e3p3omb792ko0f"
    AUTH_METHODS = ("auto", "aura", "cognito")

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
        auth_method: str = "auto",
        cognito_pool_id: str = "",
        cognito_client_id: str = "",
    ):
        auth_method = (auth_method or "auto").strip().lower()
        if auth_method not in self.AUTH_METHODS:
            raise ValueError(f"auth_method must be one of {self.AUTH_METHODS}, not {auth_method!r}")
        self._username = username
        self._password = password
        self._device_id = device_id
        self._aura_public_key = aura_public_key
        self._admin_token = admin_token
        self._default_profile_pid = default_profile_pid
        self._aura_base = (aura_base_url or self.DEFAULT_AURA_BASE).rstrip("/")
        self._auth_base = (auth_base_url or self.DEFAULT_AUTH_BASE).rstrip("/")
        self._vc_base = base_url(vc_base_url) if vc_base_url else self.DEFAULT_VC_BASE
        # An explicit VC host (CIRCLE_VC_HOST) skips per-circle host discovery.
        self._vc_pinned = bool(vc_base_url)
        self._user_agent = user_agent
        self._timeout_s = timeout_s

        self._auth_method = auth_method
        self._cognito = CognitoSrp(
            cognito_pool_id or self.DEFAULT_COGNITO_POOL_ID,
            cognito_client_id or self.DEFAULT_COGNITO_CLIENT_ID,
        )

        self._ssl_ctx = None
        self._circle_id = None
        self._aura_access_token = None
        self._aura_refresh_token = None
        self._cognito_id_token = None
        self._cognito_refresh_token = None
        # "auto" settles on the backend that accepted the account.
        self._auth_backend = auth_method if auth_method != "auto" else None

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

        access = self._ensure_identity_jwt()
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
        self._circle_id = result.get("circleid") or result.get("circleId")
        self._admin_token = admin_token
        if admin_token:
            self._discover_vc_host()
        return admin_token or None

    @property
    def auth_backend(self) -> Optional[str]:
        """Which sign-in backend is in use ("aura"/"cognito"); None until "auto" has decided."""
        return self._auth_backend

    def _ensure_identity_jwt(self) -> Optional[str]:
        """The JWT the admin grant takes: an Aura access token or a Cognito id token."""
        if self._auth_backend == "aura":
            return self._ensure_aura_access_token()
        if self._auth_backend == "cognito":
            return self._ensure_cognito_id_token()
        # auto: Cognito first. An unknown user there is not a password failure,
        # so the probe cannot lock the account; anything else is reported as is.
        try:
            token = self._ensure_cognito_id_token()
        except CircleAuthError as e:
            if not getattr(e, "user_not_found", False):
                raise
            self._auth_backend = "aura"
            return self._ensure_aura_access_token()
        self._auth_backend = "cognito"
        return token

    def _ensure_cognito_id_token(self) -> Optional[str]:
        if self._cognito_id_token and not _jwt_is_expired(self._cognito_id_token, leeway_s=60):
            return self._cognito_id_token

        if self._cognito_refresh_token:
            try:
                tokens = self._cognito.refresh(self._cognito_refresh_token)
                if tokens.get("id_token"):
                    self._cognito_id_token = tokens["id_token"]
                    self._cognito_refresh_token = tokens.get("refresh_token") or self._cognito_refresh_token
                    return self._cognito_id_token
            except Exception:
                pass  # fall through to full sign-in

        try:
            tokens = self._cognito.sign_in(self._username, self._password)
        except CognitoError as e:
            err = CircleAuthError(f"Cognito sign-in failed: {e}")
            err.user_not_found = e.user_not_found
            raise err from e
        if not tokens.get("id_token"):
            return None
        self._cognito_id_token = tokens["id_token"]
        self._cognito_refresh_token = tokens.get("refresh_token")
        return self._cognito_id_token

    def _ensure_aura_access_token(self) -> Optional[str]:
        if self._aura_access_token and not _jwt_is_expired(self._aura_access_token, leeway_s=60):
            return self._aura_access_token

        if not self._aura_public_key:
            raise ValueError("aura_public_key is required for Aura username/password login")

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

    def get_usage_categories(
        self,
        profile_pid: Optional[str] = None,
        day: str = "0",
        ndays: str = "1",
    ) -> Dict:
        """Usage by category for a profile, plus `categoryNames`.

        The window starts `day` days ago (0 = today) and runs forward for `ndays`
        days, so the last week is day=6, ndays=7.
        """
        pid = profile_pid or self._default_profile_pid
        if not pid:
            return {"error": "No profile ID specified"}

        qs = urllib.parse.urlencode({"user": str(pid), "day": str(day), "ndays": str(ndays)})
        result = self._request(f"/api/LOGS/usage/categories?{qs}")
        return self._attach_category_names(result) if result else {"error": "Failed to fetch usage data"}


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
