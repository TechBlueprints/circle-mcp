"""State reads (B.2) and VC host discovery (B.1.4), with `_http_json` stubbed out."""

import asyncio
import io
import json
import urllib.error
import urllib.parse

import pytest

from circle_mcp import server
from circle_mcp.provider import CircleClient


class FakeHTTP:
    """Answers `_http_json` calls by path; records every call."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, url, method="GET", headers=None, body=None):
        self.calls.append({"url": url, "method": method, "headers": headers or {}, "body": body})
        path = urllib.parse.urlsplit(url).path
        answer = self.routes.get(path)
        if answer is None:
            raise urllib.error.HTTPError(url, 404, "not found", {}, io.BytesIO(b"{}"))
        if callable(answer):
            answer = answer(url)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def urls(self, path):
        return [c["url"] for c in self.calls if urllib.parse.urlsplit(c["url"]).path == path]


def http_error(code):
    return urllib.error.HTTPError("https://x", code, "err", {}, io.BytesIO(b"{}"))


def make_client(routes, **kwargs):
    client = CircleClient(admin_token=kwargs.pop("admin_token", "static-token"), **kwargs)
    client._http_json = FakeHTTP(routes)
    return client


SNAPSHOT = {
    "overall": {"circleName": "Home", "mode": "Filter", "enabled": "true", "timezone": "America/Denver"},
    "users": [
        {"pid": 0, "name": "Home", "mode": "Filter", "ageCategory": "None", "role": "home"},
        {"pid": "12", "name": "Kid", "mode": "Pause", "ageCategory": "Teen", "role": "user",
         "avatarUrl": "https://img/kid.png", "timeLimits": []},
        "junk",
    ],
    "devices": [{"uid": "aa:bb", "displayName": "iPad", "isGo": "true", "pid": "12"}],
    "hardware": [{"cuuid": "c1", "hotspot": {"ssid": "setup", "password": "secret"}}],
    "categories": [{"id": 1, "name": "Games"}],
    "version": "5",
}


def run_tool(name, args=None):
    return asyncio.run(server.server.call_tool(name, args or {}))


def tool_json(result, many=False):
    """Decode a CallToolResult. A list return comes back as one text block per item."""
    assert not result.is_error, result.content
    items = [json.loads(block.text) for block in result.content]
    return items if many else items[0]


@pytest.fixture
def tool_client(monkeypatch):
    """Install a stub client behind the MCP tools."""

    def install(routes):
        client = make_client(routes)
        monkeypatch.setattr(server, "_client", client)
        monkeypatch.setattr(server, "_auth_error", "")
        return client

    return install


# -----------------------------
# Snapshot, profiles, status
# -----------------------------

def test_snapshot_full_redacts_hotspot_password(tool_client):
    client = tool_client({"/api/QUERY/all": SNAPSHOT})
    data = tool_json(run_tool("get_circle_snapshot"))
    assert data["overall"]["circleName"] == "Home"
    assert data["hardware"][0]["hotspot"] == {"ssid": "setup"}
    assert SNAPSHOT["hardware"][0]["hotspot"]["password"] == "secret"  # source left intact
    assert client._http_json.calls[0]["headers"]["Authorization"] == "Bearer static-token"


def test_snapshot_section(tool_client):
    tool_client({"/api/QUERY/all": SNAPSHOT})
    assert tool_json(run_tool("get_circle_snapshot", {"section": "devices"}), many=True)[0]["uid"] == "aa:bb"


def test_snapshot_bad_section_is_error():
    client = make_client({"/api/QUERY/all": SNAPSHOT})
    with pytest.raises(ValueError):
        client.get_snapshot("bogus")
    assert client._http_json.calls == []


def test_list_profiles_from_snapshot(tool_client):
    client = tool_client({"/api/QUERY/all": SNAPSHOT})
    profiles = tool_json(run_tool("list_profiles"), many=True)
    assert profiles == [
        {"pid": "0", "name": "Home", "mode": "Filter", "ageCategory": "None", "role": "home"},
        {"pid": "12", "name": "Kid", "mode": "Pause", "ageCategory": "Teen", "role": "user",
         "avatarUrl": "https://img/kid.png"},
    ]
    assert not client._http_json.urls("/api/QUERY/users")


def test_list_profiles_falls_back_to_query_users():
    client = make_client({
        "/api/QUERY/all": http_error(500),
        "/api/QUERY/users": {"users": [{"id": 7, "name": "Old", "type": "Filter", "photo": "p.png"}]},
    })
    assert client.list_profiles() == [
        {"pid": "7", "name": "Old", "mode": "Filter", "ageCategory": None, "role": None, "avatarUrl": "p.png"},
    ]


def test_list_profiles_falls_back_when_users_missing():
    client = make_client({"/api/QUERY/all": {"overall": {}}, "/api/QUERY/users": [{"pid": "3", "name": "Bare"}]})
    assert [p["pid"] for p in client.list_profiles()] == ["3"]


def test_circle_status_from_overall(tool_client):
    tool_client({"/api/QUERY/all": SNAPSHOT})
    assert tool_json(run_tool("get_circle_status"))["timezone"] == "America/Denver"


def test_circle_status_falls_back_to_circleinfo():
    client = make_client({"/api/QUERY/all": {"_raw": "<html>"}, "/api/QUERY/circleinfo": {"name": "legacy"}})
    assert client.get_circle_status() == {"name": "legacy"}


# -----------------------------
# Devices, hardware, categories, badges
# -----------------------------

def test_list_devices(tool_client):
    tool_client({"/api/QUERY/devices": {
        "overall": {},
        "devices": [{"uid": "aa:bb", "displayName": "iPad", "ip": "10.0.0.5", "isGo": "false",
                     "newlyAdded": "1", "rank": "3", "pid": "12", "unknownField": "x"}],
    }})
    assert tool_json(run_tool("list_devices"), many=True) == [
        {"uid": "aa:bb", "displayName": "iPad", "ip": "10.0.0.5", "isGo": False, "newlyAdded": True,
         "rank": 3, "pid": "12", "profilePid": "12", "profileName": None},
    ]


OWNED_SNAPSHOT = {
    "overall": {},
    "users": [
        {"pid": 0, "name": "Home", "relatedDevices": []},
        {"pid": "2", "name": "Lucien", "relatedDevices": [{"uid": "aa:01"}, {"uid": "aa:02"}]},
        {"pid": "3", "name": "Emilie", "relatedDevices": [{"uid": "aa:03"}]},
    ],
    "devices": [
        {"uid": "aa:01", "displayName": "lucien-ws"},
        {"uid": "aa:02", "displayName": "Luciens-Air"},
        {"uid": "aa:03", "displayName": "emiliews"},
        {"uid": "aa:04", "displayName": "orphan"},
    ],
}


def test_list_devices_joins_owner_from_profiles(tool_client):
    client = tool_client({"/api/QUERY/all": OWNED_SNAPSHOT})
    devices = tool_json(run_tool("list_devices"), many=True)
    assert [(d["displayName"], d["profilePid"], d["profileName"]) for d in devices] == [
        ("lucien-ws", "2", "Lucien"), ("Luciens-Air", "2", "Lucien"), ("emiliews", "3", "Emilie"),
        ("orphan", None, None),
    ]
    assert not client._http_json.urls("/api/QUERY/devices")  # one QUERY/all call does it


@pytest.mark.parametrize("selector", ["2", "lucien", "Lucien"])
def test_list_devices_filters_by_pid_or_name(tool_client, selector):
    tool_client({"/api/QUERY/all": OWNED_SNAPSHOT})
    devices = tool_json(run_tool("list_devices", {"profile_pid": selector}), many=True)
    assert [d["displayName"] for d in devices] == ["lucien-ws", "Luciens-Air"]


def test_list_hardware_with_live_online(tool_client):
    client = tool_client({
        "/api/QUERY/hardware": {"hardware": [{
            "cuuid": "c1", "model": "Circle Home Plus", "isOnline": "true", "isEnabled": "false",
            "network": {"status": "connected"}, "wifi": {"ssid": "home"},
            "hotspot": {"ssid": "setup", "password": "secret"},
            "stats": {"wifi": {"rssi": "-52"}}, "version": {"firmware": "2.1"},
        }]},
        "/api/QUERY/hardware/hardware/isOnline": {"result": "OK", "isOnline": "false"},
    })
    units = tool_json(run_tool("list_hardware", {"check_online": True}), many=True)
    assert units == [{
        "cuuid": "c1", "model": "Circle Home Plus", "isOnline": True, "isEnabled": False,
        "networkStatus": "connected", "wifiSsid": "home", "hotspotSsid": "setup", "wifiRssi": -52,
        "firmware": "2.1", "isOnlineLive": False,
    }]
    online_url = client._http_json.urls("/api/QUERY/hardware/hardware/isOnline")[0]
    assert urllib.parse.parse_qs(urllib.parse.urlsplit(online_url).query) == {"hardware.cuuid": ["c1"]}


def test_list_hardware_skips_live_check_by_default(tool_client):
    client = tool_client({"/api/QUERY/hardware": [{"cuuid": "c1"}]})
    assert tool_json(run_tool("list_hardware"), many=True) == [{"cuuid": "c1"}]
    assert len(client._http_json.calls) == 1


def test_list_categories_merges_v2_names(tool_client):
    tool_client({
        "/api/QUERY/categories": {"categories": [
            {"id": 1, "name": "games", "mainCategory": "Entertainment", "description": "Game sites"},
            {"id": "2", "name": "social", "mainCategory": "Social"},
        ]},
        "/api/v2/categories": [
            {"id": "1", "name": "Games", "hidden": False, "apps": []},
            {"id": 9, "name": "Brand New", "description": "v2 only"},
            {"name": "no id"},
        ],
    })
    assert tool_json(run_tool("list_categories"), many=True) == [
        {"id": "1", "name": "Games", "mainCategory": "Entertainment", "description": "Game sites"},
        {"id": "2", "name": "social", "mainCategory": "Social", "description": None},
        {"id": "9", "name": "Brand New", "mainCategory": None, "description": "v2 only"},
    ]


def test_list_categories_survives_v2_failure():
    client = make_client({"/api/QUERY/categories": [{"id": 4, "name": "news"}], "/api/v2/categories": http_error(500)})
    assert client.list_categories() == [{"id": "4", "name": "news", "mainCategory": None, "description": None}]


def test_list_categories_errors_when_both_fail():
    client = make_client({})
    with pytest.raises(RuntimeError):
        client.list_categories()


def test_profile_badges(tool_client):
    tool_client({"/api/v2/profiles/badges": [
        {"pid": 12, "badges": [{"type": "request"}]},
        {"pid": "0", "badges": None},
        {"badges": []},
    ]})
    assert tool_json(run_tool("get_profile_badges"), many=True) == [
        {"pid": "12", "badges": [{"type": "request"}]},
        {"pid": "0", "badges": []},
    ]


# -----------------------------
# Circle Go devices (port 8995)
# -----------------------------

DEV_STATUS = {
    "authStatus": "OK", "totalDeviceCount": "2", "enabledDeviceCount": "1",
    "devices": [{"deviceid": "d1", "circleid": "cid", "state": "enabled", "last": "1730000000"}],
}


def test_go_devices_uses_port_8995_and_gotoken(tool_client):
    client = tool_client({"/api/v0.1/c/devStatus": DEV_STATUS})
    data = tool_json(run_tool("list_go_devices"))
    assert data == {
        "authStatus": "OK", "totalDeviceCount": 2, "enabledDeviceCount": 1,
        "devices": [{"deviceid": "d1", "circleid": "cid", "state": "enabled", "last": 1730000000}],
    }
    url = urllib.parse.urlsplit(client._http_json.calls[0]["url"])
    assert (url.scheme, url.hostname, url.port, url.path) == ("https", "vc.meetcircle-blue.co", 8995, "/api/v0.1/c/devStatus")
    assert urllib.parse.parse_qs(url.query) == {"gotoken": ["static-token"]}


def test_go_devices_follows_discovered_host():
    client = make_client({"/api/v0.1/c/devStatus": DEV_STATUS}, vc_base_url="vc7.example.net")
    client.list_go_devices()
    assert client._http_json.calls[0]["url"].startswith("https://vc7.example.net:8995/api/v0.1/c/devStatus?")


def test_go_devices_regrants_with_fresh_gotoken():
    client = make_client({})
    tokens = iter(["fresh-1", "fresh-2"])

    def regrant():
        if not client._admin_token:
            client._admin_token = next(tokens)
        return client._admin_token

    client.ensure_admin_token = regrant

    def dev_status(url):
        if "gotoken=fresh-1" in url:
            return DEV_STATUS
        return http_error(401)

    client._http_json.routes["/api/v0.1/c/devStatus"] = dev_status
    assert client.list_go_devices()["totalDeviceCount"] == 2
    gotokens = [urllib.parse.parse_qs(urllib.parse.urlsplit(u).query)["gotoken"][0]
                for u in client._http_json.urls("/api/v0.1/c/devStatus")]
    assert gotokens == ["static-token", "static-token", "fresh-1"]


# -----------------------------
# VC host discovery (B.1.4)
# -----------------------------

def grant_routes(host_answer):
    return {
        "/auth/session/signin": {"access_token": "aura-access", "refresh_token": "r"},
        "/api/v2/grant/admin": {"ok": True, "token": "admin-jwt", "circleid": "circle-42"},
        "/api/v1.0/host": host_answer,
        "/api/QUERY/overall": {"overall": {"circleName": "Home"}},
    }


def login_client(routes, **kwargs):
    return make_client(routes, admin_token="", username="u@example.com", password="pw",
                       aura_public_key="circle_mobile_auth:test", **kwargs)


def test_host_discovery_after_grant():
    client = login_client(grant_routes({"host": "vc3.meetcircle-blue.co"}))
    assert client.get_overall() == {"circleName": "Home"}
    lookup = urllib.parse.urlsplit(client._http_json.urls("/api/v1.0/host")[0])
    assert lookup.hostname == "register.meetcircle-blue.co"
    assert urllib.parse.parse_qs(lookup.query) == {"circleid": ["circle-42"], "token": ["admin-jwt"]}
    assert client._http_json.urls("/api/QUERY/overall") == ["https://vc3.meetcircle-blue.co/api/QUERY/overall"]


@pytest.mark.parametrize("answer", [
    {"host": ""},
    {},
    {"_raw": "not json"},
    http_error(500),
    urllib.error.URLError("dns failure"),
])
def test_host_discovery_failure_keeps_default(answer):
    client = login_client(grant_routes(answer))
    assert client.ensure_admin_token() == "admin-jwt"
    client.get_overall()
    assert client._http_json.urls("/api/QUERY/overall") == ["https://vc.meetcircle-blue.co/api/QUERY/overall"]


def test_vc_host_override_skips_discovery():
    client = login_client(grant_routes({"host": "vc3.meetcircle-blue.co"}), vc_base_url="vc-pinned.example.com")
    client.get_overall()
    assert client._http_json.urls("/api/v1.0/host") == []
    assert client._http_json.urls("/api/QUERY/overall") == ["https://vc-pinned.example.com/api/QUERY/overall"]


def test_static_token_does_not_trigger_discovery():
    client = make_client(grant_routes({"host": "vc3.meetcircle-blue.co"}))
    client.get_overall()
    assert client._http_json.urls("/api/v1.0/host") == []


def test_server_reads_vc_host_env(monkeypatch):
    monkeypatch.setenv("CIRCLE_VC_HOST", "vc-env.example.com")
    monkeypatch.setenv("CIRCLE_ADMIN_TOKEN", "t")
    monkeypatch.delenv("CIRCLE_SECRETS_FILE", raising=False)
    monkeypatch.setattr(server, "_client", None)
    client = server._circle()
    assert client._vc_base == "https://vc-env.example.com"
    assert client._vc_pinned is True
