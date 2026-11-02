import asyncio
import datetime
import urllib.parse

import pytest
from mcp.server import MCPServer

from circle_mcp.api import schedules
from circle_mcp.api.schedules import day_index_string, day_names, hhmm, legacy_ok
from circle_mcp.provider import CircleClient
from circle_mcp.tools import schedules as schedules_tools

VC = "https://vc.meetcircle-blue.co"


class StubClient(CircleClient):
    """CircleClient with `_http_json` replaced by canned responses keyed on path."""

    def __init__(self, responses=None):
        super().__init__(admin_token="tok")
        self.calls = []
        self.responses = responses or {}

    def _http_json(self, url, method="GET", headers=None, body=None):
        self.calls.append({"url": url, "method": method, "headers": headers, "body": body})
        path = urllib.parse.urlsplit(url).path
        return self.responses.get(path, {"result": "success"})

    def writes(self):
        return [c for c in self.calls if not c["url"].endswith("/api/QUERY/all")]


@pytest.fixture(autouse=True)
def fixed_today(monkeypatch):
    monkeypatch.setattr(schedules, "_today_stamp", lambda: "20261102")


# -----------------------------
# Value helpers
# -----------------------------

@pytest.mark.parametrize("days, expected", [
    (["Saturday", "Sunday"], "06"),
    (["sun", "Mon", "tue", "WED", "thursday"], "01234"),
    ([6, 0], "06"),
    ([1, "2", "Wed", 3], "123"),
    ("sat,sun", "06"),
    ("12345", "12345"),
    (5, "5"),
    ([], ""),
])
def test_day_index_string(days, expected):
    assert day_index_string(days) == expected


def test_day_names_are_full_and_sunday_first():
    assert day_names(["fri", 1, "Sunday"]) == ["Sunday", "Monday", "Friday"]
    assert day_names("06") == ["Sunday", "Saturday"]


@pytest.mark.parametrize("bad", [7, -1, "Funday", "s", True, "8"])
def test_bad_days_rejected(bad):
    with pytest.raises(ValueError):
        day_index_string([bad])


@pytest.mark.parametrize("value, expected", [("7:05", "07:05"), ("21:30", "21:30"), ("00:00", "00:00"), (" 23:59 ", "23:59")])
def test_hhmm_normalizes(value, expected):
    assert hhmm(value) == expected


@pytest.mark.parametrize("bad", ["24:00", "9pm", "12:60", "1230", "", "12:5"])
def test_hhmm_rejects(bad):
    with pytest.raises(ValueError):
        hhmm(bad)


@pytest.mark.parametrize("body, ok", [
    ({"result": "success"}, True),
    ({"result": "OK"}, True),
    ({"result": True}, True),
    ({"result": "true"}, True),
    ({"result": "fail", "error": "nope"}, False),
    ({"result": False}, False),
    ({"error": "bad pid"}, False),
    ({}, True),
    ({"_raw": ""}, True),
    (None, False),
])
def test_legacy_ok(body, ok):
    assert legacy_ok(body) is ok


# -----------------------------
# Legacy parallel-list calls
# -----------------------------

def test_set_time_limits_query_string():
    c = StubClient()
    out = c.set_time_limits(
        "3",
        [
            {"category": "1010", "minutes": 60, "days": "weekdays"},
            {"category": "1010", "minutes": "120", "days": "weekend"},
            {"category": "2005", "minutes": 30, "days": "Weekday"},
        ],
        weekend_days=["Saturday", "Sunday"],
    )
    assert out == {"ok": True, "response": {"result": "success"}}
    (call,) = c.calls
    assert call["method"] == "GET" and call["body"] is None
    assert call["headers"] == {"Authorization": "Bearer tok"}
    assert call["url"] == (
        f"{VC}/api/ADD/users/user/timeLimits/limit?user.pid=3&clear=true"
        "&category=1010,1010,2005&limit=60,120,30&days=weekdays,weekend,weekdays&weekend=06&toggle=On"
    )


def test_set_time_limits_empty_clears_and_can_disable():
    c = StubClient()
    c.set_time_limits("3", [], enabled=False)
    assert c.calls[0]["url"] == f"{VC}/api/ADD/users/user/timeLimits/limit?user.pid=3&clear=true&toggle=Off"


@pytest.mark.parametrize("limit", [
    {"category": "1", "minutes": 10, "days": "monday"},
    {"category": "1", "minutes": -5},
    {"category": "1", "minutes": "lots"},
    {"minutes": 10},
    {"category": "1,2", "minutes": 10},
])
def test_set_time_limits_validates_before_sending(limit):
    c = StubClient()
    with pytest.raises(ValueError):
        c.set_time_limits("3", [limit])
    assert c.calls == []


def test_set_off_times_query_string():
    c = StubClient()
    out = c.set_off_times("3", [
        {"name": "School night", "type": "bedtime_weekday", "start": "21:00", "end": "7:00",
         "days": ["Sun", "Mon", "Tue", "Wed", "Thu"], "id": "4"},
        {"name": "Dinner", "start": "18:00", "end": "18:45", "days": [0, 1, 2, 3, 4, 5, 6], "enabled": False},
    ])
    assert out["ok"] is True
    assert c.calls[0]["url"] == (
        f"{VC}/api/ADD/users/user/flexOffTimes/offTime?user.pid=3"
        "&name=School%20night,Dinner&type=bedtime_weekday,offtime&start=21:00,18:00&end=07:00,18:45"
        "&days=01234,0123456&toggle=enabled,disabled&id=4,5&clear=true"
    )


def test_set_off_times_empty_clears():
    c = StubClient()
    c.set_off_times("3", [])
    assert c.calls[0]["url"] == f"{VC}/api/ADD/users/user/flexOffTimes/offTime?user.pid=3&clear=true"


@pytest.mark.parametrize("window", [
    {"name": "x", "start": "25:00", "end": "07:00", "days": ["Mon"]},
    {"name": "x", "start": "21:00", "end": "07:00", "days": ["Moonday"]},
    {"name": "x", "start": "21:00", "end": "07:00", "days": []},
    {"name": "x", "start": "21:00", "end": "07:00", "days": ["Mon"], "type": "nap"},
    {"name": "a,b", "start": "21:00", "end": "07:00", "days": ["Mon"]},
])
def test_set_off_times_validates_before_sending(window):
    c = StubClient()
    with pytest.raises(ValueError):
        c.set_off_times("3", [window])
    assert c.calls == []


def test_failed_legacy_result_is_not_ok():
    c = StubClient({"/api/ADD/users/user/timeLimits/limit": {"result": "fail", "error": "bad"}})
    out = c.set_time_limits("3", [])
    assert out == {"ok": False, "response": {"result": "fail", "error": "bad"}}


# -----------------------------
# Reward merges
# -----------------------------

def _snapshot(limits=None, off_times=None):
    return {"/api/QUERY/all": {"users": [
        {"pid": 2, "extensions": {"limits": [{"id": "9", "category": "1010", "minutes": "45", "date": "20261102"}]}},
        {"pid": "3", "extensions": {"limits": limits or [], "offTimes": off_times or []}},
    ]}}


def test_add_reward_minutes_keeps_other_rewards_and_sums_same_category():
    c = StubClient(_snapshot(limits=[
        {"id": "1", "category": "1010", "minutes": "30", "date": "20261102"},
        {"id": "2", "category": "2005", "minutes": 15, "date": "20261102"},
        {"id": "3", "category": "3000", "minutes": "20", "date": "20261020"},  # stale, dropped
    ]))
    out = c.add_reward_minutes("3", "2005", 10)
    assert out["ok"] is True
    assert out["rewards"] == [
        {"category": "1010", "minutes": 30, "date": "20261102"},
        {"category": "2005", "minutes": 25, "date": "20261102"},
    ]
    assert c.calls[0]["url"] == f"{VC}/api/QUERY/all"
    (write,) = c.writes()
    assert write["url"] == (
        f"{VC}/api/ADD/users/user/extensions/limits/extension?user.pid=3&clear=true"
        "&category=1010,2005&minutes=30,25&date=20261102,20261102"
    )


def test_add_reward_minutes_appends_new_category():
    c = StubClient(_snapshot(limits=[{"category": "1010", "minutes": "30", "date": "0"}]))
    c.add_reward_minutes("3", "2005", 15)
    assert c.writes()[0]["url"].endswith("&category=1010,2005&minutes=30,15&date=0,0")


def test_add_reward_minutes_without_existing_rewards():
    c = StubClient({"/api/QUERY/all": {"users": [{"pid": "3"}]}})
    c.add_reward_minutes("3", "1010", 20)
    assert c.writes()[0]["url"] == (
        f"{VC}/api/ADD/users/user/extensions/limits/extension?user.pid=3&clear=true"
        "&category=1010&minutes=20&date=0"
    )


@pytest.mark.parametrize("minutes", [0, -10, "abc"])
def test_add_reward_minutes_rejects_bad_minutes(minutes):
    c = StubClient(_snapshot())
    with pytest.raises(ValueError):
        c.add_reward_minutes("3", "1010", minutes)
    assert c.calls == []


def test_extend_off_time_replaces_same_id_and_keeps_others():
    c = StubClient(_snapshot(off_times=[
        {"id": "1", "offTimeId": "4", "startMinutes": "15", "endMinutes": "0", "date": "20261102"},
        {"id": "2", "offTimeId": "6", "startMinutes": "0", "endMinutes": "30", "date": "20261102"},
        {"id": "3", "offTimeId": "7", "startMinutes": "10", "endMinutes": "0", "date": "20260101"},
    ]))
    out = c.extend_off_time_today("3", "4", start_minutes=30)
    assert out["ok"] is True
    assert c.writes()[0]["url"] == (
        f"{VC}/api/ADD/users/user/extensions/offTimes/extension?user.pid=3&clear=true"
        "&offTimeId=6,4&startMinutes=0,30&endMinutes=30,0&date=20261102,0"
    )


def test_is_current_tolerates_timezone_skew():
    assert schedules._is_current("20261101", "20261102")
    assert schedules._is_current("20261103", "20261102")
    assert not schedules._is_current("20261030", "20261102")
    assert schedules._is_current(None, "20261102")
    assert not schedules._is_current("garbage", "20261102")


def test_today_stamp_format(monkeypatch):
    monkeypatch.undo()
    assert schedules._today_stamp() == datetime.date.today().strftime("%Y%m%d")


# -----------------------------
# v2 JSON calls
# -----------------------------

def test_list_bedtimes_and_focus_times():
    listing = [{"pid": 3, "bedtimes": [{"id": 1, "pid": 3, "name": "School", "start": "21:00"}]}]
    c = StubClient({"/api/v2/profiles/bedtimes": listing, "/api/v2/profiles/focustimes": {"profiles": []}})
    assert c.list_bedtimes() == listing
    assert c.list_focus_times() == []
    assert [x["method"] for x in c.calls] == ["GET", "GET"]
    assert c.calls[1]["url"] == f"{VC}/api/v2/profiles/focustimes"


def test_get_focus_time_defaults():
    c = StubClient({"/api/v2/focustimes/defaults": {"blocked": [1, 2]}})
    assert c.get_focus_time_defaults() == {"blocked": [1, 2]}
    assert c.calls[0]["url"] == f"{VC}/api/v2/focustimes/defaults"


def test_add_bedtime_body():
    c = StubClient({"/api/v2/profiles/3/bedtimes": {"id": 11}})
    out = c.add_bedtime("3", "School nights", "21:30", "6:45", ["Thu", "Sun", "Mon", "Tue", "Wed"])
    assert out == {"ok": True, "response": {"id": 11}}
    (call,) = c.calls
    assert call["method"] == "POST"
    assert call["url"] == f"{VC}/api/v2/profiles/3/bedtimes"
    assert call["body"] == {
        "pid": 3, "name": "School nights", "start": "21:30", "end": "06:45",
        "days": ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday"], "state": "enabled",
    }


def test_update_bedtime_body():
    c = StubClient()
    c.update_bedtime("3", "11", "Weekend", "23:00", "08:00", ["Fri", "Sat"], enabled=False)
    (call,) = c.calls
    assert call["method"] == "PUT"
    assert call["url"] == f"{VC}/api/v2/profiles/3/bedtimes/11"
    assert call["body"] == {
        "id": 11, "pid": 3, "name": "Weekend", "start": "23:00", "end": "08:00",
        "days": ["Friday", "Saturday"], "state": "disabled",
    }


def test_add_focus_time_body():
    c = StubClient()
    c.add_focus_time(
        "3", "Homework", "16:00", "18:00", [1, 2, 3, 4, 5], apply_filters=True,
        allowed_categories=["1010"], blocked_categories=[2005, "3000"],
        allowed_urls=["khanacademy.org", " "], blocked_urls=["youtube.com"],
    )
    (call,) = c.calls
    assert call["method"] == "POST"
    assert call["url"] == f"{VC}/api/v2/profiles/3/focustimes"
    assert call["body"] == {
        "pid": 3, "name": "Homework", "start": "16:00", "end": "18:00",
        "days": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"],
        "enabled": True, "applyFilters": True,
        "filters": {"allowed": [1010], "blocked": [2005, 3000],
                    "allowedURLs": ["khanacademy.org"], "blockedURLs": ["youtube.com"]},
    }


def test_update_and_delete_focus_time():
    c = StubClient()
    c.update_focus_time("3", "8", "Reading", "19:00", "20:00", ["Sun"], enabled=False)
    c.delete_focus_time("3", "8")
    put, delete = c.calls
    assert put["method"] == "PUT" and put["url"] == f"{VC}/api/v2/profiles/3/focustimes/8"
    assert put["body"] == {
        "id": 8, "pid": 3, "name": "Reading", "start": "19:00", "end": "20:00", "days": ["Sunday"],
        "enabled": False, "applyFilters": False,
        "filters": {"allowed": [], "blocked": [], "allowedURLs": [], "blockedURLs": []},
    }
    assert delete["method"] == "DELETE" and delete["body"] is None
    assert delete["url"] == f"{VC}/api/v2/profiles/3/focustimes/8"


@pytest.mark.parametrize("kwargs", [
    {"start": "4pm", "end": "18:00", "days": ["Mon"]},
    {"start": "16:00", "end": "18:00", "days": []},
    {"start": "16:00", "end": "18:00", "days": ["Caturday"]},
])
def test_v2_validates_before_sending(kwargs):
    c = StubClient()
    with pytest.raises(ValueError):
        c.add_focus_time("3", "x", **kwargs)
    with pytest.raises(ValueError):
        c.add_bedtime("3", "x", **kwargs)
    assert c.calls == []


# -----------------------------
# Tool registration / write gating
# -----------------------------

READS = {"list_bedtimes", "list_focus_times", "get_focus_time_defaults"}
WRITES = {
    "set_time_limits", "add_reward_minutes", "extend_off_time_today", "set_off_times",
    "add_bedtime", "update_bedtime", "add_focus_time", "update_focus_time", "delete_focus_time",
}


def _registered(calls=None):
    server = MCPServer(name="test")

    def circle(method, **kwargs):
        if calls is not None:
            calls.append((method, kwargs))
        return {"ok": True}

    schedules_tools.register(server, circle, None)
    return server, {t.name for t in asyncio.run(server.list_tools())}


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off"])
def test_writes_hidden_without_flag(monkeypatch, value):
    monkeypatch.setenv("CIRCLE_MCP_ALLOW_WRITES", value)
    _, names = _registered()
    assert names == READS


def test_writes_hidden_when_flag_unset(monkeypatch):
    monkeypatch.delenv("CIRCLE_MCP_ALLOW_WRITES", raising=False)
    assert _registered()[1] == READS


@pytest.mark.parametrize("value", ["1", "true", "YES", "on"])
def test_writes_registered_with_flag(monkeypatch, value):
    monkeypatch.setenv("CIRCLE_MCP_ALLOW_WRITES", value)
    assert _registered()[1] == READS | WRITES


def test_tool_forwards_to_client_method(monkeypatch):
    monkeypatch.setenv("CIRCLE_MCP_ALLOW_WRITES", "1")
    calls = []
    server, _ = _registered(calls)
    asyncio.run(server.call_tool("add_reward_minutes", {"pid": "3", "category": "1010", "minutes": 15}))
    assert calls == [("add_reward_minutes", {"pid": "3", "category": "1010", "minutes": 15})]


def test_every_tool_maps_to_a_client_method(monkeypatch):
    monkeypatch.setenv("CIRCLE_MCP_ALLOW_WRITES", "1")
    for name in READS | WRITES:
        assert callable(getattr(CircleClient, name)), name
