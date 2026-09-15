import json
from pathlib import Path

from jobstar.collector.parse import (
    clean_text,
    dedup,
    extract_job_id,
    normalize_list_item,
)

FIXTURE = Path(__file__).parent / "fixtures" / "boss_list_raw.json"


def test_extract_job_id_from_full_url():
    url = "https://www.zhipin.com/job_detail/abc123def456~.html?lid=xyz"
    assert extract_job_id(url) == "abc123def456"


def test_extract_job_id_from_relative_url():
    assert extract_job_id("/job_detail/zzz999~.html") == "zzz999"


def test_extract_job_id_ignores_query_params():
    a = extract_job_id("/job_detail/same~.html?lid=1&securityId=a")
    b = extract_job_id("/job_detail/same~.html?lid=2&securityId=b")
    assert a == b == "same"


def test_extract_job_id_returns_none_for_junk():
    assert extract_job_id("") is None
    assert extract_job_id("https://www.zhipin.com/about.html") is None


def test_clean_text_collapses_whitespace():
    assert clean_text("  高级 后端\n工程师 ") == "高级 后端 工程师"


def test_clean_text_handles_none():
    assert clean_text(None) == ""


def test_normalize_keeps_city_prefix_only():
    """城市白名单比对的是「杭州」，不是「杭州·西湖区」。"""
    item = normalize_list_item(
        {"url": "/job_detail/a~.html", "title": "t", "company": "c", "city": "杭州·西湖区"}
    )
    assert item["city"] == "杭州"


def test_normalize_builds_absolute_url():
    item = normalize_list_item(
        {"url": "/job_detail/a~.html", "title": "t", "company": "c"}
    )
    assert item["url"].startswith("https://www.zhipin.com/")


def test_normalize_drops_item_without_job_id():
    assert normalize_list_item({"url": "", "title": "坏数据"}) is None


def test_normalize_drops_item_without_title():
    assert normalize_list_item({"url": "/job_detail/a~.html", "title": "  "}) is None


def test_dedup_keeps_first_occurrence_per_job_id():
    items = [
        {"job_id": "a", "title": "第一次"},
        {"job_id": "a", "title": "第二次"},
        {"job_id": "b", "title": "另一个"},
    ]
    result = dedup(items)
    assert [i["job_id"] for i in result] == ["a", "b"]
    assert result[0]["title"] == "第一次"


def test_fixture_round_trip():
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    items = dedup([i for i in (normalize_list_item(r) for r in raw) if i])
    assert len(items) >= 2, "fixture 去重后至少剩两条"
    assert all(i["job_id"] for i in items)
    assert all(i["title"] for i in items)
    assert len({i["job_id"] for i in items}) == len(items)
