"""GoDeviceClient and the Circle Go tools, with HTTP stubbed out."""

import asyncio
import io
import json
import os
import stat
import urllib.error
import urllib.parse
import zipfile

import pytest

from circle_mcp.api import godevice
from circle_mcp.api.godevice import GoDeviceClient, GoDeviceUnmanagedError
from circle_mcp.provider import CircleClient


def _json(body, status=200):
    return status, "application/json", json.dumps(body).encode()


def _http_error(url, code, body=b""):
    return urllib.error.HTTPError(url, code, "err", {"Content-Type": "application/json"}, io.BytesIO(body))


class FakeHTTP:
    """Replaces GoDeviceClient._fetch: answers from a queue per path, records every call."""

    def __init__(self):
        self.calls = []
        self.routes = {}

    def add(self, path, *responses):
        self.routes.setdefault(path, []).extend(responses)

    def __call__(self, url, headers=None):
        parts = urllib.parse.urlsplit(url)
        query = dict(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
        self.calls.append({
            "url": url, "host": parts.hostname, "port": parts.port,
            "path": parts.path, "query": query, "headers": dict(headers or {}),
        })
        queue = self.routes.get(parts.path)
        if not queue:
            raise AssertionError(f"unexpected request {url}")
        answer = queue.pop(0)
        if isinstance(answer, int):
            raise _http_error(url, answer)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def paths(self):
        return [c["path"] for c in self.calls]


@pytest.fixture
def state_file(tmp_path):
    return str(tmp_path / "state" / "go-device.json")


@pytest.fixture
def http():
    return FakeHTTP()


def _client(state_file, http):
    client = GoDeviceClient(state_path=state_file)
    client._fetch = http
    return client


def _enrolled(state_file, http, **overrides):
    state = {
        "app_id": "app-1", "device_id": "dev-1", "device_name": "Kid Tablet",
        "circle_id": "circ-1", "vc_host": "vc-9.example.co",
        "gotoken": "go-1", "refresh_token": "ref-1",
    }
    state.update(overrides)
    godevice.save_state(state_file, state)
    return _client(state_file, http)


# -----------------------------
# Enrollment
# -----------------------------

def test_full_enrollment_handshake(state_file, http):
    http.add("/api/REGISTER/newdevice", _json({"result": "OK", "deviceid": "dev-1", "vctoken": "vc-a"}))
    http.add("/api/REGISTER/passcode", _json({"result": "OK", "passcode": "123456"}))
    http.add("/api/REGISTER/godevice", _json({
        "result": "OK", "access-token": "go-1", "refresh_token": "ref-1",
        "circleid": "circ-1", "host": "https://vc-9.example.co", "vctoken": "vc-b",
    }))
    http.add("/api/v0.1/a/registerDevice", _json({"result": "OK"}))

    client = _client(state_file, http)
    client.register_new_device("app-1", "Kid Tablet")
    assert client.request_passcode()["passcode"] == "123456"
    client.claim_device_token(sign_token=True)
    assert client.describe_device("HomeNet") == {"result": "OK"}

    newdevice, passcode, claim, describe = http.calls
    for call in (newdevice, passcode, claim):
        assert (call["host"], call["port"]) == ("vc.meetcircle-blue.co", None)
        assert "Authorization" not in call["headers"]
    assert newdevice["query"] == {"appid": "app-1", "devicename": "Kid Tablet"}
    assert passcode["query"] == {"vctoken": "vc-a"}
    assert claim["query"] == {
        "appid": "app-1", "devicename": "Kid Tablet", "deviceid": "dev-1",
        "vctoken": "vc-a", "sign-token": "true",
    }
    # The host from the claim replaces the default, on the device port.
    assert (describe["host"], describe["port"]) == ("vc-9.example.co", 8995)
    assert describe["query"] == {
        "devname": "Kid Tablet", "homeSSID": "HomeNet", "cuuid": "",
        "circleid": "circ-1", "deviceid": "dev-1",
    }

    assert client.is_enrolled
    assert (client.gotoken, client.refresh_token, client.vctoken) == ("go-1", "ref-1", "vc-b")
    saved = godevice.load_state(state_file)
    assert saved["circle_id"] == "circ-1" and saved["vc_host"] == "vc-9.example.co"
    assert saved["gotoken"] == "go-1" and saved["refresh_token"] == "ref-1"


def test_claim_token_prefers_gotoken_field(state_file, http):
    http.add("/api/REGISTER/godevice", _json({
        "gotoken": "go-main", "access-token": "go-alt", "refresh-token": "ref-main", "circleid": "c",
    }))
    client = _client(state_file, http)
    client.app_id, client.device_id, client.vctoken = "a", "d", "v"
    client.claim_device_token()
    assert (client.gotoken, client.refresh_token) == ("go-main", "ref-main")
    assert http.calls[0]["query"]["sign-token"] == "false"
    assert client.vc_host == "vc.meetcircle-blue.co"


def test_claim_before_approval_raises(state_file, http):
    http.add("/api/REGISTER/godevice", _json({"result": "fail", "error": "not authorized"}))
    client = _client(state_file, http)
    client.app_id, client.device_id, client.vctoken = "a", "d", "v"
    with pytest.raises(godevice.GoDeviceError, match="not authorized"):
        client.claim_device_token()
    assert not client.is_enrolled


def test_authorize_go_device_uses_admin_request():
    client = CircleClient(admin_token="admin-tok")
    seen = []

    def fake_http_json(url, method="GET", headers=None, body=None):
        seen.append((url, method, headers))
        return {"result": "OK"}

    client._http_json = fake_http_json
    assert client.authorize_go_device("app-1", "dev-1") == {"result": "OK"}
    url, method, headers = seen[0]
    parts = urllib.parse.urlsplit(url)
    assert (parts.hostname, parts.port, parts.path) == ("vc.meetcircle-blue.co", None, "/api/AUTHORIZE/godevice")
    assert dict(urllib.parse.parse_qsl(parts.query)) == {"appid": "app-1", "deviceid": "dev-1"}
    assert method == "GET" and headers == {"Authorization": "Bearer admin-tok"}


# -----------------------------
# Device auth
# -----------------------------

def test_device_calls_carry_bearer_and_identity_params(state_file, http):
    http.add("/api/v0.1/a/notify/guestMode", _json({"result": "OK"}))
    http.add("/api/v0.1/a/notify/devOptions", _json({"result": "OK"}))
    http.add("/api/v0.1/a/notify/timeZoneChange", _json({"result": "OK"}))
    client = _enrolled(state_file, http)
    client.notify_guest_mode("kid")
    client.notify_dev_options("kid")
    client.notify_timezone_change()

    for call in http.calls:
        assert (call["host"], call["port"]) == ("vc-9.example.co", 8995)
        assert call["headers"]["Authorization"] == "Bearer go-1"
        assert call["query"]["circleid"] == "circ-1" and call["query"]["deviceid"] == "dev-1"
    assert http.calls[0]["query"]["profilename"] == "kid"
    assert http.calls[1]["query"]["profilename"] == "kid"
    assert set(http.calls[2]["query"]) == {"circleid", "deviceid"}


def test_401_refreshes_token_and_retries_once(state_file, http):
    http.add("/api/v0.1/a/ping", 401, _json({"status": "ok"}))
    http.add("/api/v1.0/refresh", _json({"gotoken": "go-2", "refresh-token": "ref-2", "disable-management": "false"}))
    client = _enrolled(state_file, http)

    result = client.ping()
    assert result["status"] == "ok" and result["managed"] is True
    first, refresh, retry = http.calls
    assert first["headers"]["Authorization"] == "Bearer go-1"
    assert (refresh["host"], refresh["path"]) == ("auth.meetcircle-blue.co", "/api/v1.0/refresh")
    assert refresh["headers"]["Authorization"] == "Bearer ref-1"
    assert retry["headers"]["Authorization"] == "Bearer go-2"
    assert client.disable_management is False

    saved = godevice.load_state(state_file)
    assert (saved["gotoken"], saved["refresh_token"]) == ("go-2", "ref-2")
    assert saved["last_ping"]["status"] == "ok"


def test_second_401_is_not_retried_again(state_file, http):
    http.add("/api/v0.1/a/notify/timeZoneChange", 401, 401)
    http.add("/api/v1.0/refresh", _json({"access-token": "go-2"}))
    client = _enrolled(state_file, http)
    with pytest.raises(urllib.error.HTTPError) as err:
        client.notify_timezone_change()
    assert err.value.code == 401
    assert http.paths().count("/api/v1.0/refresh") == 1


def test_rejected_refresh_raises_auth_error(state_file, http):
    http.add("/api/v0.1/a/ping", 401)
    http.add("/api/v1.0/refresh", 401)
    client = _enrolled(state_file, http)
    with pytest.raises(godevice.GoDeviceAuthError):
        client.ping()


def test_disable_management_flag(state_file, http):
    http.add("/api/v0.1/a/notify/timeZoneChange", 401)
    http.add("/api/v1.0/refresh", _json({"gotoken": "go-2", "disable-management": "true"}))
    client = _enrolled(state_file, http)

    with pytest.raises(GoDeviceUnmanagedError):
        client.notify_timezone_change()
    assert client.disable_management is True
    assert godevice.load_state(state_file)["management_disabled"] is True

    # Persisted, and later device calls stop locally without touching the network.
    reloaded = _client(state_file, http)
    assert reloaded.disable_management is True
    before = len(http.calls)
    assert reloaded.ping()["managed"] is False
    assert len(http.calls) == before


# -----------------------------
# Check-in
# -----------------------------

@pytest.mark.parametrize("answer, managed", [
    (_json({"status": "registered"}), True),
    (_json({"status": "OK"}), True),
    (_json({"status": "awol"}), True),
    (_json({"status": "not registered"}), False),
    (_json({"error": "Device not registered"}), False),
    (_json({"status": "mystery"}), None),
])
def test_ping_managed_flag(state_file, http, answer, managed):
    http.add("/api/v0.1/a/ping", answer)
    client = _enrolled(state_file, http)
    assert client.ping()["managed"] is managed
    assert http.calls[0]["query"]["devname"] == "Kid Tablet"


def test_ping_error_status_saying_not_registered(state_file, http):
    client = _enrolled(state_file, http)
    http.add("/api/v0.1/a/ping", _http_error("u", 403, b'{"result":"fail","error":"device not registered"}'))
    assert client.ping()["managed"] is False

    http.add("/api/v0.1/a/ping", _http_error("u", 500, b"boom"))
    with pytest.raises(urllib.error.HTTPError):
        client.ping()


# -----------------------------
# Settings bundle
# -----------------------------

def _zip(entries):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


def test_settings_bundle_unpacked_by_suffix(state_file, http, tmp_path):
    archive = _zip({
        "rules/abc123.bin": b"S" * 10,
        "abc123.customs": b"C" * 4,
        "../escape.userinfo": b"U" * 7,
        "README.txt": b"ignored",
    })
    http.add("/api/v0.1/a/settings", (200, "application/zip", archive))
    client = _enrolled(state_file, http)
    dest = tmp_path / "bundle"

    out = client.download_settings_bundle(str(dest))
    assert (dest / "settings.zip").read_bytes() == archive
    assert (dest / "settings.bin").read_bytes() == b"S" * 10
    assert (dest / "customs.bin").read_bytes() == b"C" * 4
    assert (dest / "userinfo.bin").read_bytes() == b"U" * 7
    assert not (tmp_path / "escape.userinfo").exists()
    assert out["files"]["settings.bin"] == {"entry": "rules/abc123.bin", "bytes": 10}
    assert out["unmatched_entries"] == ["README.txt"]
    assert http.calls[0]["port"] == 8995


def test_settings_not_a_zip(state_file, http, tmp_path):
    http.add("/api/v0.1/a/settings", _json({"error": "nope"}))
    client = _enrolled(state_file, http)
    with pytest.raises(godevice.GoDeviceError, match="ZIP"):
        client.download_settings_bundle(str(tmp_path / "b"))


# -----------------------------
# C.4 probes
# -----------------------------

def test_probe_user_info_reports_each_endpoint(state_file, http):
    http.add("/api/USERINFO", _json({"info": {"username": "kid"}}))
    http.add("/api/USERPHOTO", (200, "image/png", b"\x89PNG\r\n\x1a\n\x00\xff"))
    http.add("/api/v1.0/godevice/userinfo", _http_error("u", 404, b"Not Found"))
    http.add("/api/v1.0/godevice/userphoto", urllib.error.URLError("refused"))
    client = _enrolled(state_file, http)

    out = client.probe_user_info()
    assert out["/api/USERINFO"]["status"] == 200
    assert out["/api/USERINFO"]["body"] == {"info": {"username": "kid"}}
    assert out["/api/USERPHOTO"]["body"]["bytes"] == 10
    assert out["/api/v1.0/godevice/userinfo"] == {
        "status": 404, "content_type": "application/json", "body": "Not Found",
    }
    assert out["/api/v1.0/godevice/userphoto"] == {"status": None, "error": "refused"}
    for call in http.calls:
        assert call["port"] == 8995 and call["query"] == {"circleid": "circ-1", "deviceid": "dev-1"}


# -----------------------------
# State file
# -----------------------------

def test_state_file_round_trip_and_mode(state_file, http):
    client = _enrolled(state_file, http, vctoken="vc-x")
    client.last_ping = {"status": "ok", "managed": True, "at": 1}
    client.save()

    assert stat.S_IMODE(os.stat(state_file).st_mode) == 0o600
    reloaded = GoDeviceClient(state_path=state_file, vc_host="ignored.example")
    assert reloaded.to_state() == client.to_state()
    assert reloaded.vc_host == "vc-9.example.co"
    assert sorted(os.listdir(os.path.dirname(state_file))) == ["go-device.json"]


def test_state_file_from_env_and_missing_file(monkeypatch, tmp_path):
    path = str(tmp_path / "custom.json")
    monkeypatch.setenv("CIRCLE_GO_STATE_FILE", path)
    client = GoDeviceClient()
    assert client.state_path == path
    assert not client.is_enrolled and client.vc_host == "vc.meetcircle-blue.co"
    assert godevice.load_state(path) == {}


def test_ensure_app_id_is_stable(state_file, http):
    client = _client(state_file, http)
    app_id = client.ensure_app_id()
    assert app_id and client.ensure_app_id() == app_id


# -----------------------------
# Tools
# -----------------------------

def _tool_server(monkeypatch, allow, circle=None):
    if allow:
        monkeypatch.setenv("CIRCLE_MCP_ALLOW_WRITES", "1")
    else:
        monkeypatch.delenv("CIRCLE_MCP_ALLOW_WRITES", raising=False)
    from mcp.server import MCPServer
    from circle_mcp.tools import godevice as tools

    server = MCPServer(name="t")
    tools.register(server, circle or (lambda method, **kw: None), None)
    return server


def _tool_names(monkeypatch, allow):
    return {t.name for t in asyncio.run(_tool_server(monkeypatch, allow).list_tools())}


def _call_tool(server, name, args=None):
    result = asyncio.run(server.call_tool(name, args or {}))
    assert not result.is_error, result.content
    return json.loads(result.content[0].text)


def test_write_tools_gated(monkeypatch):
    assert _tool_names(monkeypatch, allow=False) == {"go_device_status"}
    assert _tool_names(monkeypatch, allow=True) == {
        "go_device_status", "go_enroll_start", "go_enroll_approve", "go_enroll_finish",
        "go_ping", "go_download_settings", "go_probe_user_info",
    }


def test_status_tool_hides_tokens(monkeypatch, state_file, http):
    from circle_mcp.tools import godevice as tools

    monkeypatch.setattr(tools, "_client", _enrolled(state_file, http))
    status = tools._run(tools._status)
    assert status["stage"] == "enrolled" and status["has_device_token"] is True
    assert "go-1" not in json.dumps(status) and "ref-1" not in json.dumps(status)


def test_enrollment_through_tools(monkeypatch, state_file, http):
    from circle_mcp.tools import godevice as tools

    approvals = []

    def circle(method, **kwargs):
        approvals.append((method, kwargs))
        return {"result": "OK"}

    monkeypatch.setattr(tools, "_client", _client(state_file, http))
    server = _tool_server(monkeypatch, allow=True, circle=circle)
    http.add("/api/REGISTER/newdevice", _json({"deviceid": "dev-1", "vctoken": "vc-a"}))
    http.add("/api/REGISTER/passcode", _json({"passcode": "4321"}))
    http.add("/api/REGISTER/godevice", _json({"gotoken": "go-1", "refresh-token": "ref-1", "circleid": "circ-1"}))
    http.add("/api/v0.1/a/registerDevice", _json({"result": "OK"}))

    started = _call_tool(server, "go_enroll_start", {"device_name": "Kid Tablet"})
    assert started["device_id"] == "dev-1" and started["passcode"] == {"passcode": "4321"}
    assert _call_tool(server, "go_device_status")["stage"] == "awaiting approval"

    _call_tool(server, "go_enroll_approve")
    assert approvals == [("authorize_go_device", {"app_id": started["app_id"], "device_id": "dev-1"})]

    finished = _call_tool(server, "go_enroll_finish", {"home_ssid": "HomeNet"})
    assert finished["status"]["stage"] == "enrolled"

    from mcp.server.mcpserver.exceptions import ToolError

    with pytest.raises(ToolError, match="already enrolled"):
        asyncio.run(server.call_tool("go_enroll_start", {"device_name": "Other"}))
