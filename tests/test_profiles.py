"""Profile and filter-switch writes (PROTOCOL.md B.4, B.4.1) against a stubbed HTTP layer."""

import asyncio
import json
import threading
import urllib.parse

import pytest
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from circle_mcp.provider import CircleClient
from circle_mcp.tools import profiles as profile_tools

SNAPSHOT = {
    "users": [
        {"pid": "0", "name": "Home"},
        {
            "pid": 7,  # numbers may arrive unquoted
            "name": "Sam",
            "categorySwitches": [{"category": 12, "state": "Off"}, {"category": "30", "state": "On"}, "junk"],
            "platformSwitches": [{"platform": "101", "state": "Off"}, {"platform": None, "state": "On"}],
            "customSwitches": [{"site": "Example.com", "toggle": "Off"}, {"site": "kids.org", "toggle": "On"}],
        },
    ]
}


class StubClient(CircleClient):
    def __init__(self, snapshot=None, reply=None):
        super().__init__(admin_token="test-token")
        self.calls = []
        self._snapshot = snapshot if snapshot is not None else SNAPSHOT
        self._reply = reply if reply is not None else {"result": "success"}

    def _http_json(self, url, method="GET", headers=None, body=None):
        parts = urllib.parse.urlsplit(url)
        query = dict(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
        self.calls.append((method, parts.path, query))
        if parts.path == "/api/QUERY/all":
            return self._snapshot
        return self._reply

    @property
    def writes(self):
        return [c for c in self.calls if c[1] != "/api/QUERY/all"]


@pytest.fixture
def client():
    return StubClient()


@pytest.fixture
def tools(client, monkeypatch):
    monkeypatch.setenv("CIRCLE_MCP_ALLOW_WRITES", "1")
    server = MCPServer(name="test")
    profile_tools.register(server, lambda: client, threading.Lock())

    def call(tool, /, **args):
        result = asyncio.run(server.call_tool(tool, args))
        return json.loads(result.content[0].text)

    return call


# -----------------------------
# Registration gate
# -----------------------------

@pytest.mark.parametrize("value", [None, "", "0", "false", "no"])
def test_no_write_tools_without_flag(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("CIRCLE_MCP_ALLOW_WRITES", raising=False)
    else:
        monkeypatch.setenv("CIRCLE_MCP_ALLOW_WRITES", value)
    server = MCPServer(name="test")
    assert profile_tools.register(server, lambda: StubClient(), threading.Lock()) == []
    assert asyncio.run(server.list_tools()) == []


@pytest.mark.parametrize("value", ["1", "true", "YES", "on"])
def test_write_tools_with_flag(monkeypatch, value):
    monkeypatch.setenv("CIRCLE_MCP_ALLOW_WRITES", value)
    server = MCPServer(name="test")
    names = profile_tools.register(server, lambda: StubClient(), threading.Lock())
    listed = {t.name for t in asyncio.run(server.list_tools())}
    assert listed == set(names)
    assert len(names) == 13


# -----------------------------
# Exact query parameters per call
# -----------------------------

def test_create_profile(client, tools):
    client._reply = {"result": "success", "newPid": 9}
    out = tools("create_profile", name=" Robin ", age_category="teen")
    assert client.writes == [("GET", "/api/ADD/users/user", {
        "name": "Robin", "ageCategory": "Teen", "mode": "Filter", "toggles.defaultPlatforms": "Off",
    })]
    assert out == {"ok": True, "pid": "9", "response": {"result": "success", "newPid": 9}}


@pytest.mark.parametrize("tool,args,path,query", [
    ("pause_profile", {"pid": "7"}, "/api/UPDATE/users/user/mode", {"user.pid": "7", "value": "Pause"}),
    ("resume_profile", {"pid": "7"}, "/api/UPDATE/users/user/mode", {"user.pid": "7", "value": "Filter"}),
    ("rename_profile", {"pid": "7", "name": "Sammy"}, "/api/UPDATE/users/user/name", {"user.pid": "7", "value": "Sammy"}),
    ("set_profile_age_category", {"pid": "7", "age_category": "pre-k"},
     "/api/UPDATE/users/user/ageCategory", {"user.pid": "7", "value": "Pre-K"}),
    ("delete_profile", {"pid": "7", "confirm": True}, "/api/DELETE/users/user", {"user.pid": "7"}),
    ("assign_device_to_profile", {"pid": "7", "mac": "AA:bb:cc:dd:ee:01"},
     "/api/ADD/users/user/relatedDevices/relatedDevice", {"user.pid": "7", "mac": "AA:bb:cc:dd:ee:01"}),
    ("remove_platform_switch", {"pid": "7", "platform_id": "101"},
     "/api/DELETE/users/user/platformSwitches/switch", {"user.pid": "7", "switch.platform": "101"}),
    ("remove_custom_site", {"pid": "7", "site": "example.com"},
     "/api/DELETE/users/user/customSwitches/switch", {"user.pid": "7", "switch.site": "example.com"}),
])
def test_single_call_tools(client, tools, tool, args, path, query):
    out = tools(tool, **args)
    assert client.calls == [("GET", path, query)]
    assert out["ok"] is True


def test_set_profile_toggles_both(client, tools):
    out = tools("set_profile_toggles", pid="7", time_limits="on", off_times="Off")
    assert client.writes == [
        ("GET", "/api/UPDATE/users/user/toggles/timeLimits", {"user.pid": "7", "value": "On"}),
        ("GET", "/api/UPDATE/users/user/toggles/offTimes", {"user.pid": "7", "value": "Off"}),
    ]
    assert out["ok"] is True and set(out["response"]) == {"timeLimits", "offTimes"}


def test_set_profile_toggles_validates_before_sending(client, tools):
    with pytest.raises(ToolError, match="off_times"):
        tools("set_profile_toggles", pid="7", time_limits="On", off_times="sometimes")
    with pytest.raises(ToolError):
        tools("set_profile_toggles", pid="7")
    assert client.calls == []


@pytest.mark.parametrize("tool,args", [
    ("create_profile", {"name": "X", "age_category": "Toddler"}),
    ("create_profile", {"name": "X", "age_category": "Kid", "mode": "Off"}),
    ("set_profile_age_category", {"pid": "7", "age_category": "Senior"}),
    ("assign_device_to_profile", {"pid": "7", "mac": "not-a-mac"}),
    ("set_category_switches", {"pid": "7", "switches": {"12": "Blocked"}, "replace": True}),
    ("set_platform_switches", {"pid": "7", "switches": {"1,2": "On"}, "replace": True}),
    ("set_custom_site", {"pid": "7", "site": "a.com", "toggle": "Unmanaged"}),
    ("set_custom_site", {"pid": "7", "site": "a.com,b.com", "toggle": "On"}),
])
def test_invalid_values_send_nothing(client, tools, tool, args):
    with pytest.raises(ToolError):
        tools(tool, **args)
    assert client.writes == []


# -----------------------------
# Built-in profile guard
# -----------------------------

@pytest.mark.parametrize("pid", ["0", "1", " 1 "])
def test_delete_refuses_builtin(client, tools, pid):
    with pytest.raises(ToolError, match="built-in"):
        tools("delete_profile", pid=pid, confirm=True)
    assert client.calls == []


def test_delete_requires_confirm(client, tools):
    with pytest.raises(ToolError, match="confirm"):
        tools("delete_profile", pid="7")
    with pytest.raises(ToolError, match="confirm"):
        tools("delete_profile", pid="7", confirm=False)
    assert client.calls == []


def test_client_delete_guard(client):
    with pytest.raises(ValueError):
        client.delete_profile("0")
    assert client.calls == []


# -----------------------------
# Switch merge vs replace
# -----------------------------

def test_category_switches_merge(client, tools):
    out = tools("set_category_switches", pid="7", switches={"12": "On", "44": "unmanaged"})
    assert client.calls[0][1] == "/api/QUERY/all"
    assert client.writes == [("GET", "/api/ADD/users/user/categorySwitches/switch", {
        "user.pid": "7", "clear": "true", "category": "12,30,44", "state": "On,On,Unmanaged",
    })]
    assert out["switches"] == {"12": "On", "30": "On", "44": "Unmanaged"}
    assert out["ok"] is True


def test_category_switches_replace_skips_fetch(client, tools):
    tools("set_category_switches", pid="7", switches={"5": "Off"}, replace=True)
    assert client.calls == [("GET", "/api/ADD/users/user/categorySwitches/switch", {
        "user.pid": "7", "clear": "true", "category": "5", "state": "Off",
    })]


def test_category_switches_replace_empty_clears(client, tools):
    tools("set_category_switches", pid="7", switches={}, replace=True)
    assert client.calls == [("GET", "/api/ADD/users/user/categorySwitches/switch", {
        "user.pid": "7", "clear": "true", "category": "", "state": "",
    })]


def test_platform_switches_merge(client, tools):
    tools("set_platform_switches", pid="7", switches={"202": "On"})
    assert client.writes == [("GET", "/api/ADD/users/user/platformSwitches/switch", {
        "user.pid": "7", "clear": "true", "platform": "101,202", "state": "Off,On",
    })]


def test_custom_site_merges_case_insensitively(client, tools):
    out = tools("set_custom_site", pid="7", site="example.com", toggle="on")
    assert client.writes == [("GET", "/api/ADD/users/user/customSwitches/switch", {
        "user.pid": "7", "site": "kids.org,example.com", "toggle": "On,On", "clear": "true",
    })]
    assert out["sites"] == {"kids.org": "On", "example.com": "On"}


def test_merge_unknown_profile(client, tools):
    with pytest.raises(ToolError, match="No Circle profile"):
        tools("set_category_switches", pid="99", switches={"1": "On"})
    assert client.writes == []


# -----------------------------
# ok flag (PROTOCOL.md A.3)
# -----------------------------

@pytest.mark.parametrize("reply,ok", [
    ({"result": True}, True),
    ({"result": "OK"}, True),
    ({"result": "Success"}, True),
    ({"result": "fail", "error": "nope"}, False),
    ({"result": False}, False),
    ({"error": "bad pid"}, False),
    ({}, True),
    ({"_raw": "done"}, True),
])
def test_ok_flag(tools, client, reply, ok):
    client._reply = reply
    assert tools("pause_profile", pid="7") == {"ok": ok, "response": reply}


def test_ok_false_without_token(monkeypatch):
    monkeypatch.setenv("CIRCLE_MCP_ALLOW_WRITES", "1")
    server = MCPServer(name="test")
    profile_tools.register(server, lambda: CircleClient(), threading.Lock())
    result = asyncio.run(server.call_tool("pause_profile", {"pid": "7"}))
    assert json.loads(result.content[0].text) == {"ok": False, "response": None}
