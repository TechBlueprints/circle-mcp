"""
Circle Go child-device tools (PROTOCOL.md Part C).

`go_device_status` is always available. Everything that talks to the cloud as
the child device is registered only when CIRCLE_MCP_ALLOW_WRITES is truthy:
enrolling creates a real device in the parent's circle, and check-ins are
visible to the parent.

Enrollment flow: go_enroll_start -> parent approves (go_enroll_approve, or types
the passcode into their Circle app) -> go_enroll_finish.
"""

import os
import threading
import urllib.error
from typing import Optional

from mcp.server.mcpserver.exceptions import ToolError

from ..api.godevice import GoDeviceClient, GoDeviceError

_client: Optional[GoDeviceClient] = None
# The device client keeps tokens in memory and on disk; one call at a time.
_go_lock = threading.Lock()


def _writes_allowed() -> bool:
    return os.environ.get("CIRCLE_MCP_ALLOW_WRITES", "").strip().lower() in ("1", "true", "yes", "on")


def _go() -> GoDeviceClient:
    global _client
    if _client is None:
        _client = GoDeviceClient()
    return _client


def _settings_dir(client: GoDeviceClient) -> str:
    return os.environ.get("CIRCLE_GO_SETTINGS_DIR") or os.path.join(
        os.path.dirname(os.path.abspath(client.state_path)), "go-settings"
    )


def _run(fn):
    """Run fn(client) under the device lock; expected failures become ToolErrors."""
    with _go_lock:
        try:
            return fn(_go())
        except (GoDeviceError, ValueError) as e:
            raise ToolError(str(e)) from e
        except urllib.error.HTTPError as e:
            raise ToolError(f"Circle device API returned HTTP {e.code}") from e
        except (urllib.error.URLError, OSError) as e:
            raise ToolError(f"Circle device API unreachable: {getattr(e, 'reason', e)}") from e


def _stage(client: GoDeviceClient) -> str:
    if client.disable_management:
        return "unmanaged"
    if client.is_enrolled:
        return "enrolled"
    if client.device_id and client.vctoken:
        return "awaiting approval"
    return "not enrolled"


def _status(client: GoDeviceClient) -> dict:
    last = client.last_ping or {}
    managed = False if client.disable_management else last.get("managed")
    return {
        "stage": _stage(client),
        "device_name": client.device_name or None,
        "device_id": client.device_id or None,
        "app_id": client.app_id or None,
        "circle_id": client.circle_id or None,
        "vc_host": client.vc_host,
        "has_device_token": bool(client.gotoken),
        "has_refresh_token": bool(client.refresh_token),
        "disable_management": client.disable_management,
        "managed": managed,
        "last_ping": client.last_ping,
        "state_file": client.state_path,
        "writes_enabled": _writes_allowed(),
    }


def register(server, circle, lock) -> None:
    """Add the Circle Go tools. `circle(method, **kwargs)` runs a CircleClient call."""

    @server.tool()
    def go_device_status() -> dict:
        """Show the enrolled Circle Go child device: identity, enrollment stage, last ping, managed flag.

        Tokens are never returned, only whether they are present.
        """
        return _run(_status)

    if not _writes_allowed():
        return

    @server.tool()
    def go_enroll_start(device_name: str, replace: bool = False) -> dict:
        """Start enrolling this server as a Circle Go child device (step 1) and request a pairing passcode.

        The parent then approves it: either go_enroll_approve (uses the parent account this
        server is signed in to) or by typing the returned passcode into their Circle app.
        Finish with go_enroll_finish.

        Args:
            device_name: Name the device shows under in the parent's Circle app
            replace: Start over even if a device is already enrolled (its tokens are discarded)
        """
        def start(client: GoDeviceClient) -> dict:
            if client.is_enrolled and not replace:
                raise ToolError(
                    f"A device ({client.device_name}, {client.device_id}) is already enrolled; "
                    "pass replace=true to enroll a new one"
                )
            client.register_new_device(client.ensure_app_id(), device_name)
            out = {"device_name": client.device_name, "device_id": client.device_id, "app_id": client.app_id}
            try:
                out["passcode"] = client.request_passcode()
            except (GoDeviceError, urllib.error.URLError) as e:
                out["passcode_error"] = str(getattr(e, "reason", e))
            out["next"] = "Approve with go_enroll_approve or the passcode, then call go_enroll_finish"
            return out

        return _run(start)

    @server.tool()
    def go_enroll_approve() -> dict:
        """Approve the pending Circle Go device from the parent account (enrollment step 2)."""
        with _go_lock:
            client = _go()
            app_id, device_id = client.app_id, client.device_id
        if not (app_id and device_id):
            raise ToolError("No device is waiting for approval: run go_enroll_start first")
        result = circle("authorize_go_device", app_id=app_id, device_id=device_id)
        return result if result is not None else {"error": "Approval request failed"}

    @server.tool()
    def go_enroll_finish(home_ssid: str, cuuid: str = "") -> dict:
        """Finish enrolling after the parent approved: claim the device token and describe the device.

        Args:
            home_ssid: Wi-Fi network name the device calls home
            cuuid: Circle hardware unit id, if the home has one (may be empty)
        """
        def finish(client: GoDeviceClient) -> dict:
            client.claim_device_token()
            described = client.describe_device(home_ssid, cuuid)
            return {"status": _status(client), "registerDevice": described}

        return _run(finish)

    @server.tool()
    def go_ping() -> dict:
        """Check in as the child device; reports the cloud's status and whether the device is still managed."""
        return _run(lambda client: client.ping())

    @server.tool()
    def go_download_settings() -> dict:
        """Download the device's rule bundle and unpack settings.bin, customs.bin, userinfo.bin.

        The files are opaque binaries for Circle's filter engine; this reports where they were
        saved (CIRCLE_GO_SETTINGS_DIR, default next to the state file) and their sizes.
        """
        return _run(lambda client: client.download_settings_bundle(_settings_dir(client)))

    @server.tool()
    def go_probe_user_info() -> dict:
        """Probe the four undocumented user-info endpoints with the device token and report status and body of each."""
        return _run(lambda client: client.probe_user_info())
