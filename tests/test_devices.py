"""Devices / circle settings / hardware / other-host calls, with HTTP stubbed out."""

import asyncio
import io
import json
import urllib.error
import urllib.parse

import pytest
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from circle_mcp.provider import CircleClient
from circle_mcp.tools import devices as devices_tools

AURA_JWT = "aura-access"
ADMIN = "admin-token"


class FakeHttp:
    """Stands in for CircleClient._http_json: records calls, answers by host/path."""

    def __init__(self):
        self.calls = []
        self.fail_once = {}  # url prefix -> HTTP status to raise the first time
        self.replies = {}  # url prefix -> body
        self.grants = 0
        self.signins = 0

    def __call__(self, url, method="GET", headers=None, body=None):
        self.calls.append({"url": url, "method": method, "headers": dict(headers or {}), "body": body})
        for prefix, code in list(self.fail_once.items()):
            if url.startswith(prefix):
                del self.fail_once[prefix]
                raise urllib.error.HTTPError(url, code, "nope", {}, io.BytesIO(b"{}"))
        if url.endswith("/auth/session/signin"):
            self.signins += 1
            return {"access_token": AURA_JWT, "refresh_token": "r"}
        if url.endswith("/api/v2/grant/admin"):
            self.grants += 1
            return {"token": ADMIN, "circleId": "circle-1"}
        for prefix, reply in self.replies.items():
            if url.startswith(prefix):
                return reply
        return {"result": "success"}

    def api_calls(self):
        return [c for c in self.calls if "/auth/session/" not in c["url"] and "/grant/admin" not in c["url"]]

    def last(self):
        return self.api_calls()[-1]


def split(url):
    parts = urllib.parse.urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}{parts.path}", dict(urllib.parse.parse_qsl(parts.query))


@pytest.fixture
def http():
    return FakeHttp()


@pytest.fixture
def client(http):
    c = CircleClient(username="u@example.com", password="pw", device_id="parent-dev", aura_public_key="pk")
    c._http_json = http
    return c


@pytest.fixture
def admin_only_client(http):
    c = CircleClient(admin_token=ADMIN, device_id="parent-dev")
    c._http_json = http
    return c


# -----------------------------
# Admin-API writes (B.5)
# -----------------------------

VC = "https://vc.meetcircle-blue.co"


@pytest.mark.parametrize("method, kwargs, path, query", [
    ("rename_device", {"uid": "aa:bb:cc:dd:ee:ff", "name": "Den TV"},
     "/api/UPDATE/devices/device/displayName", {"device.uid": "aa:bb:cc:dd:ee:ff", "value": "Den TV"}),
    ("set_device_mode", {"uid": "aa:bb", "mode": "Pause"},
     "/api/UPDATE/devices/device/mode", {"device.uid": "aa:bb", "value": "Pause"}),
    ("set_device_mode", {"uid": "aa:bb", "mode": "Filter"},
     "/api/UPDATE/devices/device/mode", {"device.uid": "aa:bb", "value": "Filter"}),
    ("forget_device", {"uid": "aa:bb"}, "/api/DELETE/devices/device", {"device.uid": "aa:bb"}),
    ("set_overall_mode", {"mode": "Pause"}, "/api/UPDATE/overall/mode", {"value": "Pause"}),
    ("set_timezone", {"tz": "America/Denver"}, "/api/UPDATE/overall/timezone", {"value": "America/Denver"}),
    ("set_locale", {"locale": "es"}, "/api/UPDATE/overall/locale", {"value": "es"}),
    ("set_default_assigned_profile", {"pid": "1"}, "/api/UPDATE/overall/defaultAssignedProfile", {"value": "1"}),
    ("dismiss_notification", {"notification_id": "42"},
     "/api/ADD/overall/activeNotifications/notification", {"id": "42", "clear": "true"}),
    ("set_hardware_enabled", {"cuuid": "hw-1", "enabled": False},
     "/api/UPDATE/hardware/hardware/isEnabled", {"hardware.cuuid": "hw-1", "value": "false"}),
    ("set_hardware_compatible_mode", {"cuuid": "hw-1", "enabled": True},
     "/api/UPDATE/hardware/hardware/compatibleMode", {"hardware.cuuid": "hw-1", "value": "true"}),
    ("reboot_hardware", {"cuuid": "hw-1"}, "/api/POWER/reboot", {"cuuid": "hw-1"}),
])
def test_admin_writes_hit_vc_with_admin_bearer(client, http, method, kwargs, path, query):
    result = getattr(client, method)(**kwargs)

    call = http.last()
    assert split(call["url"]) == (VC + path, query)
    assert call["method"] == "GET"
    assert call["body"] is None
    assert call["headers"]["Authorization"] == f"Bearer {ADMIN}"
    assert result == {"ok": True, "response": {"result": "success"}}


def test_mode_values_are_checked_before_any_request(client, http):
    with pytest.raises(ValueError):
        client.set_device_mode("aa:bb", "Off")
    with pytest.raises(ValueError):
        client.set_overall_mode("paused")
    assert http.api_calls() == []


@pytest.mark.parametrize("raw, ok", [
    ({"result": "success"}, True),
    ({"result": True}, True),
    ({"result": "OK"}, True),
    ({"result": "true"}, True),
    ({}, True),
    ({"_raw": "done"}, True),
    ({"result": "fail", "error": "no such device"}, False),
    ({"result": False}, False),
    ({"error": "bad value"}, False),
    (None, False),
])
def test_write_result_ok_flag(raw, ok):
    assert CircleClient._write_result(raw) == {"ok": ok, "response": raw}


def test_failed_write_reports_not_ok(client, http):
    http.replies[f"{VC}/api/UPDATE/devices/device/displayName"] = {"result": "fail", "error": "unknown uid"}
    assert client.rename_device("zz", "x") == {"ok": False, "response": {"result": "fail", "error": "unknown uid"}}


# -----------------------------
# Other hosts (B.6)
# -----------------------------

ACCOUNT = "https://accounts.meetcircle-blue.co/api/v1.0/account/subscribe"


def test_subscription_uses_aura_jwt_not_admin_token(client, http):
    http.replies[ACCOUNT] = {"ok": True, "status": "active", "account-data": {"subs": []}}

    assert client.get_subscription()["status"] == "active"

    call = http.last()
    assert call["url"] == ACCOUNT
    assert call["method"] == "GET"
    assert call["headers"]["Authorization"] == f"Bearer {AURA_JWT}"


def test_subscription_signs_in_again_after_401(client, http):
    client._aura_access_token = "stale"
    http.fail_once[ACCOUNT] = 401

    client.get_subscription()

    tries = [c for c in http.calls if c["url"] == ACCOUNT]
    assert [c["headers"]["Authorization"] for c in tries] == ["Bearer stale", f"Bearer {AURA_JWT}"]
    assert http.signins == 1


def test_subscription_needs_username_password(admin_only_client, http):
    with pytest.raises(RuntimeError, match="Aura sign-in"):
        admin_only_client.get_subscription()
    assert http.calls == []


def test_locate_child_query_and_admin_bearer(client, http):
    http.replies["https://location.meetcircle-blue.co/"] = {"success": "sent"}

    result = client.locate_child("kid-phone", provider="pushy")

    call = http.last()
    assert split(call["url"]) == ("https://location.meetcircle-blue.co/api/v1.0/locatechild", {
        "circleid": "circle-1",
        "parent_deviceid": "parent-dev",
        "child_deviceid": "kid-phone",
        "provider": "pushy",
    })
    assert call["headers"]["Authorization"] == f"Bearer {ADMIN}"
    assert result == {"ok": True, "response": {"success": "sent"}}


def test_locate_child_regrants_after_401(client, http):
    client.ensure_admin_token()
    http.fail_once["https://location.meetcircle-blue.co/"] = 401

    client.locate_child("kid-phone")

    assert http.grants == 2
    tries = [c for c in http.calls if "locatechild" in c["url"]]
    assert len(tries) == 2


def test_locate_child_needs_circle_id(admin_only_client, http):
    with pytest.raises(RuntimeError, match="circle id"):
        admin_only_client.locate_child("kid-phone")
    assert http.calls == []


def test_domain_lookup_is_unauthenticated(client, http):
    http.replies["https://urldb.meetcircle-blue.co/"] = {"status": "ok", "results": [{"domain": "youtube.com", "catid": 7}]}

    client.lookup_domain_category("youtube.com")

    call = http.calls[-1]
    assert split(call["url"]) == (
        "https://urldb.meetcircle-blue.co/circle/v1/category", {"d": "youtube.com", "model": "android"},
    )
    assert "Authorization" not in call["headers"]
    assert http.signins == 0 and http.grants == 0


# -----------------------------
# Tools: gating, guards, validation
# -----------------------------

READ_TOOLS = {"get_subscription_status", "lookup_domain_category"}
WRITE_TOOLS = {
    "rename_device", "pause_device", "resume_device", "forget_device", "pause_home", "resume_home",
    "set_circle_timezone", "set_circle_locale", "set_default_profile", "dismiss_notification",
    "set_hardware_enabled", "set_hardware_compatible_mode", "reboot_hardware", "request_child_location",
}


class Invoker:
    """Stands in for server._call: records (method, kwargs), returns a canned write result."""

    def __init__(self, reply=None):
        self.calls = []
        self.reply = reply if reply is not None else {"ok": True, "response": {"result": "success"}}

    def __call__(self, method, **kwargs):
        self.calls.append((method, kwargs))
        return self.reply


def build(monkeypatch, writes, reply=None):
    if writes is None:
        monkeypatch.delenv("CIRCLE_MCP_ALLOW_WRITES", raising=False)
    else:
        monkeypatch.setenv("CIRCLE_MCP_ALLOW_WRITES", writes)
    srv = MCPServer(name="test")
    invoker = Invoker(reply)
    devices_tools.register(srv, invoker, None)
    return srv, invoker


def names(srv):
    return {t.name for t in asyncio.run(srv.list_tools())}


def call(srv, tool, args=None):
    result = asyncio.run(srv.call_tool(tool, args or {}))
    return json.loads(result.content[0].text)


@pytest.mark.parametrize("value", [None, "", "0", "false", "no", "off", "maybe"])
def test_writes_hidden_unless_enabled(monkeypatch, value):
    srv, _ = build(monkeypatch, value)
    assert names(srv) == READ_TOOLS


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_writes_registered_when_enabled(monkeypatch, value):
    srv, _ = build(monkeypatch, value)
    assert names(srv) == READ_TOOLS | WRITE_TOOLS


@pytest.mark.parametrize("tool, args, method, kwargs", [
    ("rename_device", {"mac": "aa:bb", "name": "Den TV"}, "rename_device", {"uid": "aa:bb", "name": "Den TV"}),
    ("pause_device", {"mac": "aa:bb"}, "set_device_mode", {"uid": "aa:bb", "mode": "Pause"}),
    ("resume_device", {"mac": "aa:bb"}, "set_device_mode", {"uid": "aa:bb", "mode": "Filter"}),
    ("forget_device", {"mac": "aa:bb", "confirm": True}, "forget_device", {"uid": "aa:bb"}),
    ("pause_home", {}, "set_overall_mode", {"mode": "Pause"}),
    ("resume_home", {}, "set_overall_mode", {"mode": "Filter"}),
    ("set_circle_timezone", {"tz": "Europe/Berlin"}, "set_timezone", {"tz": "Europe/Berlin"}),
    ("set_circle_locale", {"locale": "es"}, "set_locale", {"locale": "es"}),
    ("set_default_profile", {"pid": "1"}, "set_default_assigned_profile", {"pid": "1"}),
    ("dismiss_notification", {"notification_id": "42"}, "dismiss_notification", {"notification_id": "42"}),
    ("set_hardware_enabled", {"cuuid": "hw", "enabled": False}, "set_hardware_enabled", {"cuuid": "hw", "enabled": False}),
    ("set_hardware_compatible_mode", {"cuuid": "hw", "enabled": True},
     "set_hardware_compatible_mode", {"cuuid": "hw", "enabled": True}),
    ("reboot_hardware", {"cuuid": "hw", "confirm": True}, "reboot_hardware", {"cuuid": "hw"}),
    ("request_child_location", {"child_device_id": "kid"}, "locate_child", {"child_device_id": "kid", "provider": "fcm"}),
])
def test_write_tools_call_client(monkeypatch, tool, args, method, kwargs):
    srv, invoker = build(monkeypatch, "1")
    assert call(srv, tool, args) == {"ok": True, "response": {"result": "success"}}
    assert invoker.calls == [(method, kwargs)]


@pytest.mark.parametrize("tool, args", [
    ("forget_device", {"mac": "aa:bb"}),
    ("forget_device", {"mac": "aa:bb", "confirm": False}),
    ("reboot_hardware", {"cuuid": "hw"}),
    ("reboot_hardware", {"cuuid": "hw", "confirm": False}),
])
def test_confirm_guards(monkeypatch, tool, args):
    srv, invoker = build(monkeypatch, "1")
    with pytest.raises(ToolError, match="confirm=true"):
        call(srv, tool, args)
    assert invoker.calls == []


@pytest.mark.parametrize("tool, args", [
    ("set_circle_timezone", {"tz": "Mars/Olympus"}),
    ("set_circle_timezone", {"tz": ""}),
    ("set_circle_locale", {"locale": "english please"}),
    ("rename_device", {"mac": " ", "name": "x"}),
    ("request_child_location", {"child_device_id": "kid", "provider": "sms"}),
])
def test_bad_arguments_never_reach_circle(monkeypatch, tool, args):
    srv, invoker = build(monkeypatch, "1")
    with pytest.raises(ToolError):
        call(srv, tool, args)
    assert invoker.calls == []


@pytest.mark.parametrize("given, host", [
    ("youtube.com", "youtube.com"),
    ("https://www.YouTube.com/watch?v=x", "www.youtube.com"),
    ("tiktok.com/@someone", "tiktok.com"),
])
def test_lookup_domain_category_tool(monkeypatch, given, host):
    srv, invoker = build(monkeypatch, None, reply={"status": "ok", "results": [{"domain": host, "catid": "12"}]})
    out = call(srv, "lookup_domain_category", {"domain": given})
    assert invoker.calls == [("lookup_domain_category", {"domain": host})]
    assert out["domain"] == host and out["catids"] == ["12"]


def test_lookup_domain_category_tolerates_odd_shapes(monkeypatch):
    srv, _ = build(monkeypatch, None, reply={"_raw": "not json"})
    assert call(srv, "lookup_domain_category", {"domain": "example.com"})["catids"] == []


def test_subscription_tool(monkeypatch):
    srv, invoker = build(monkeypatch, None, reply={"ok": True, "status": "active"})
    assert call(srv, "get_subscription_status") == {"ok": True, "status": "active"}
    assert invoker.calls == [("get_subscription", {})]


def test_server_wires_tools_through_real_client(monkeypatch, http):
    """End to end through server._call with a real CircleClient and stubbed HTTP."""
    from circle_mcp import server as srv_mod

    c = CircleClient(admin_token=ADMIN)
    c._http_json = http
    monkeypatch.setattr(srv_mod, "_client", c)
    http.replies["https://urldb.meetcircle-blue.co/"] = {"results": [{"domain": "x.com", "catid": 3}]}

    out = call(srv_mod.server, "lookup_domain_category", {"domain": "x.com"})
    assert out["catids"] == ["3"]
