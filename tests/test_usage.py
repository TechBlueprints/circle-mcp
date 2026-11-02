import datetime
import urllib.error
import urllib.parse

import pytest

from circle_mcp.provider import CircleClient


class StubHTTP:
    """Stands in for CircleClient._http_json: answers by URL path, records queries."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, url, method="GET", headers=None, body=None):
        parts = urllib.parse.urlsplit(url)
        query = dict(urllib.parse.parse_qsl(parts.query))
        self.calls.append((parts.path, query))
        answer = self.routes.get(parts.path)
        if answer is None:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        return answer(query) if callable(answer) else answer


def make_client(routes):
    client = CircleClient(admin_token="test-token", default_profile_pid="7")
    stub = StubHTTP(routes)
    client._http_json = stub
    return client, stub


CATEGORIES = {"categories": [{"id": 3, "name": "Social Media"}, {"id": "12", "name": "Games"}]}


# -----------------------------
# Category names
# -----------------------------

def test_category_names_cached_and_v2_404_tolerated():
    client, stub = make_client({"/api/QUERY/categories": CATEGORIES})
    assert client.category_name(3) == "Social Media"
    assert client.category_name("12") == "Games"
    assert client.category_name("999") is None
    assert client.category_name("T") == "Total (all categories)"
    paths = [p for p, _ in stub.calls]
    assert paths.count("/api/QUERY/categories") == 1


def test_category_names_merges_v2_apps():
    client, _ = make_client({
        "/api/v2/categories": [{"id": 3, "name": "Social (v2)", "apps": [{"id": "app.tiktok", "name": "TikTok"}]}],
        "/api/QUERY/categories": CATEGORIES,
    })
    names = client.category_names()
    assert names["3"] == "Social Media"
    assert names["app.tiktok"] == "TikTok"


def test_category_names_failure_not_cached():
    routes = {}
    client, _ = make_client(routes)
    assert client.category_names() == {}
    routes["/api/QUERY/categories"] = CATEGORIES
    assert client.category_names()["3"] == "Social Media"


def test_usage_categories_gets_names_and_keeps_keys():
    usage = {"result": "OK", "report": [{"type": "day", "start": "x", "data": {"3": "15", "12": "40"}}]}
    client, _ = make_client({"/api/LOGS/usage/categories": usage, "/api/QUERY/categories": CATEGORIES})
    result = client.get_usage_categories("7")
    assert result["report"][0]["data"] == {"3": "15", "12": "40"}
    assert result["categoryNames"] == {"12": "Games", "3": "Social Media"}


def test_usage_categories_empty_report():
    client, _ = make_client({"/api/LOGS/usage/categories": {"result": "OK", "report": []}})
    result = client.get_usage_categories("7")
    assert result == {"result": "OK", "report": []}


# -----------------------------
# Usage by site
# -----------------------------

def test_usage_sites_passes_query():
    client, stub = make_client({"/api/LOGS/usage/sites": {"result": "OK", "report": []}})
    assert client.get_usage_sites("7", day="2", ndays="3") == {"result": "OK", "report": []}
    assert stub.calls[-1] == ("/api/LOGS/usage/sites", {"user": "7", "day": "2", "ndays": "3"})


# -----------------------------
# Usage summary
# -----------------------------

SUMMARY_SNAPSHOT = {"users": [{"pid": "7", "name": "Lucien"}, {"pid": "8", "name": "Emilie"}]}
SUMMARY_CATEGORIES = {"categories": [
    {"id": "71", "name": "Minecraft"}, {"id": "13", "name": "Online Games"}, {"id": "65", "name": "YouTube"},
]}
SUMMARY_USAGE = {"result": "success", "report": [{
    "user": "7", "type": "usage", "ndays": "1", "subtype": "categories", "start": "11-2-2026",
    "data": {"71": "00090", "13": "00090", "65": "00025", "T": "00115"},
}]}
SUMMARY_SITES = {"result": "success", "report": [{
    "subtype": "sites", "start": "11-2-2026",
    "data": {"71": {"minecraft.net": "00090"}, "all": {"minecraft.net": "00090", "youtube.com": "00025"}},
}]}


def summary_client():
    return make_client({
        "/api/QUERY/all": SUMMARY_SNAPSHOT,
        "/api/QUERY/categories": SUMMARY_CATEGORIES,
        "/api/LOGS/usage/categories": SUMMARY_USAGE,
        "/api/LOGS/usage/sites": SUMMARY_SITES,
    })


def test_usage_summary_names_minutes_and_hours():
    client, stub = summary_client()
    result = client.get_usage_summary("Lucien", day="0", ndays="1")
    assert result["profile"] == {"pid": "7", "name": "Lucien"}
    assert result["window"] == {"start": "11-2-2026", "days": 1, "fromDaysAgo": 0, "toDaysAgo": 0}
    assert result["totalMinutes"] == 115 and result["totalHours"] == 1.92
    assert result["categories"][0] == {"id": "71", "name": "Minecraft", "minutes": 90, "hours": 1.5}
    assert [c["name"] for c in result["categories"]] == ["Minecraft", "Online Games", "YouTube"]
    assert result["sites"] == [
        {"site": "minecraft.net", "minutes": 90, "hours": 1.5},
        {"site": "youtube.com", "minutes": 25, "hours": 0.42},
    ]
    assert ("/api/LOGS/usage/categories", {"user": "7", "day": "0", "ndays": "1"}) in stub.calls


def test_usage_summary_last_week_window_and_pid_selector():
    client, _ = summary_client()
    result = client.get_usage_summary("8", day="6", ndays="7")
    assert result["profile"] == {"pid": "8", "name": "Emilie"}
    assert result["window"]["fromDaysAgo"] == 6 and result["window"]["toDaysAgo"] == 0


def test_usage_summary_unknown_name_rejected():
    client, _ = summary_client()
    with pytest.raises(ValueError):
        client.get_usage_summary("Nobody")


def test_usage_summary_survives_missing_sites_report():
    client, _ = make_client({
        "/api/QUERY/all": SUMMARY_SNAPSHOT,
        "/api/QUERY/categories": SUMMARY_CATEGORIES,
        "/api/LOGS/usage/categories": SUMMARY_USAGE,
    })
    result = client.get_usage_summary("7")
    assert result["sites"] == [] and result["totalMinutes"] == 115


# -----------------------------
# History
# -----------------------------

HISTORY = {
    "result": "OK",
    "report": [{
        "type": "history", "subtype": "allowed", "start": "x", "withCategory": True,
        "data": [
            {"uid": "aa:bb", "sites": [
                {"site": "example.com", "time": "10:15", "state": "allowed", "category": 3, "extra": "kept"},
                {"site": "game.example", "time": "11:00", "state": "allowed", "catid": "12"},
            ]},
            {"uid": "cc:dd", "sites": [{"site": "other.example", "time": "12:30", "state": "allowed"}]},
        ],
    }],
}


def test_history_flattened_with_categories():
    client, stub = make_client({"/api/LOGS/history/allowed": HISTORY, "/api/QUERY/categories": CATEGORIES})
    result = client.get_history("7")
    assert stub.calls[0] == (
        "/api/LOGS/history/allowed",
        {"user": "7", "day": "0", "time": "23:59", "maxrecords": "200", "withcat": "1"},
    )
    assert result["count"] == 3
    first, second, third = result["entries"]
    assert first == {
        "site": "example.com", "time": "10:15", "state": "allowed", "category": "3",
        "extra": "kept", "uid": "aa:bb", "categoryName": "Social Media",
    }
    assert second["category"] == "12" and second["categoryName"] == "Games" and second["catid"] == "12"
    assert third == {"site": "other.example", "time": "12:30", "state": "allowed", "uid": "cc:dd"}


def test_history_without_categories_skips_catalog():
    blocked = {"report": [{"data": [{"uid": "aa:bb", "sites": ["bad.example", {"site": "worse.example"}]}]}]}
    client, stub = make_client({"/api/LOGS/history/blocked": blocked})
    result = client.get_history("7", kind="Blocked", max_records=5, with_categories=False)
    assert stub.calls[0][1]["maxrecords"] == "5" and stub.calls[0][1]["withcat"] == "0"
    assert [e["site"] for e in result["entries"]] == ["bad.example", "worse.example"]
    assert all(e["uid"] == "aa:bb" and "categoryName" not in e for e in result["entries"])
    assert [p for p, _ in stub.calls] == ["/api/LOGS/history/blocked"]


@pytest.mark.parametrize("payload", [{"result": "OK", "report": []}, {"result": "OK"}, {"report": [{"data": None}]}])
def test_history_empty_report(payload):
    client, _ = make_client({"/api/LOGS/history/allowed": payload})
    result = client.get_history("7")
    assert result["entries"] == [] and result["count"] == 0


def test_history_rejects_unknown_kind():
    client, _ = make_client({})
    with pytest.raises(ValueError):
        client.get_history("7", kind="everything")


# -----------------------------
# Time remaining
# -----------------------------

def snapshot(**profile_overrides):
    profile = {
        "pid": "7",
        "name": "Sam",
        "mode": "Filter",
        "weekend": "06",
        "toggles": {"timeLimits": "On"},
        "timeLimits": [
            {"id": "1", "category": "3", "minutes": "60", "days": "weekdays"},
            {"id": "2", "category": "3", "minutes": "120", "days": "weekend"},
            {"id": "3", "category": "T", "minutes": "180", "days": "weekdays"},
            {"id": "4", "category": "12", "minutes": "-1", "days": "weekdays"},
            {"id": "5", "category": "12", "minutes": "90"},
        ],
        "extensions": {"limits": []},
    }
    profile.update(profile_overrides)
    return {
        "overall": {"timezone": "America/Denver", "mode": "Filter"},
        "users": [{"pid": "0", "name": "Home"}, profile],
    }


USAGE_TODAY = {"result": "OK", "report": [{"type": "day", "data": {"3": "45", "12": "100", "20": "10.5"}}]}

# 2026-11-02 is a Monday; 2026-11-01 a Sunday. 06:00 UTC is still the previous evening in Denver.
MONDAY_DENVER = datetime.datetime(2026, 11, 2, 18, 0, tzinfo=datetime.timezone.utc)
SUNDAY_DENVER = datetime.datetime(2026, 11, 2, 6, 0, tzinfo=datetime.timezone.utc)


def remaining(snap, usage=USAGE_TODAY, now=MONDAY_DENVER):
    client, stub = make_client({
        "/api/QUERY/all": snap,
        "/api/LOGS/usage/categories": usage,
        "/api/QUERY/categories": CATEGORIES,
    })
    return client.get_time_remaining("7", now=now), stub


def by_cat(result):
    return {e["category"]: e for e in result["limits"]}


def test_time_remaining_weekday():
    result, stub = remaining(snapshot())
    assert ("/api/LOGS/usage/categories", {"user": "7", "day": "0", "ndays": "1"}) in stub.calls
    assert result["weekday"] == "Monday" and result["dayType"] == "weekdays"
    assert result["date"] == "2026-11-02" and result["timezone"] == "America/Denver"
    limits = by_cat(result)
    assert set(limits) == {"3", "T", "12"}
    assert limits["3"] == {
        "category": "3", "categoryName": "Social Media", "limitMinutes": 60, "extensionMinutes": 0,
        "allowedMinutes": 60, "usedMinutes": 45, "remainingMinutes": 15, "exceeded": False,
    }
    # -1 is "no limit"; the limit with no `days` counts as a weekday limit.
    assert limits["12"]["limitMinutes"] == 90 and limits["12"]["remainingMinutes"] == 0
    assert limits["12"]["exceeded"] is True
    assert limits["T"]["usedMinutes"] == 155.5 and limits["T"]["remainingMinutes"] == 24.5
    assert result["totalUsedMinutes"] == 155.5
    assert result["paused"] is False and result["timeLimitsEnabled"] is True


def test_time_remaining_weekend_uses_circle_timezone():
    result, _ = remaining(snapshot(), now=SUNDAY_DENVER)
    assert result["weekday"] == "Sunday" and result["dayType"] == "weekend"
    limits = by_cat(result)
    assert set(limits) == {"3"}
    assert limits["3"]["limitMinutes"] == 120 and limits["3"]["remainingMinutes"] == 75


def test_time_remaining_custom_weekend_days():
    # Monday counted as a weekend day for this profile.
    result, _ = remaining(snapshot(weekend="01"))
    assert result["dayType"] == "weekend"
    assert by_cat(result)["3"]["limitMinutes"] == 120


def test_time_remaining_extensions_today_only():
    exts = {"limits": [
        {"id": "a", "category": "3", "minutes": "30", "date": "2026-11-02"},
        {"id": "b", "category": "3", "minutes": "15", "date": "2026-11-01"},
        {"id": "c", "category": "T", "minutes": 20},
        {"id": "d", "category": "12", "minutes": "10", "date": "20261102"},
    ]}
    result, _ = remaining(snapshot(extensions=exts))
    limits = by_cat(result)
    assert limits["3"]["extensionMinutes"] == 30
    assert limits["3"]["allowedMinutes"] == 90 and limits["3"]["remainingMinutes"] == 45
    assert limits["T"]["allowedMinutes"] == 200
    assert limits["12"]["allowedMinutes"] == 100 and limits["12"]["remainingMinutes"] == 0


def test_time_remaining_paused_and_toggle_off():
    result, _ = remaining(snapshot(mode="Pause", toggles={"timeLimits": "Off"}))
    assert result["paused"] is True
    assert result["timeLimitsEnabled"] is False
    assert "switched off" in result["note"]


@pytest.mark.parametrize("usage", [{"result": "OK", "report": []}, {"result": "OK"}, {"report": [{"data": None}]}])
def test_time_remaining_empty_usage_report(usage):
    result, _ = remaining(snapshot(), usage=usage)
    limits = by_cat(result)
    assert limits["3"]["usedMinutes"] == 0 and limits["3"]["remainingMinutes"] == 60
    assert result["totalUsedMinutes"] == 0


def test_time_remaining_unknown_profile_and_bad_timezone():
    snap = snapshot()
    snap["overall"]["timezone"] = "Not/AZone"
    result, _ = remaining(snap)
    assert result["timezone"] == "UTC"
    client, _ = make_client({"/api/QUERY/all": snapshot()})
    assert client.get_time_remaining("99", now=MONDAY_DENVER) == {"error": "No profile with pid 99"}


# -----------------------------
# Tool registration
# -----------------------------

def test_usage_tools_registered():
    import asyncio

    from circle_mcp.server import server

    names = {t.name for t in asyncio.run(server.list_tools())}
    assert {"get_usage_by_site", "get_history", "get_time_remaining"} <= names
