import json
from pathlib import Path

import pytest

from jobstar.collector.parse import (
    clean_text,
    dedup,
    extract_job_id,
    normalize_list_item,
    parse_coords,
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
    assert len(items) == 3, "fixture 去重后应剩 3 条（5 条原始记录：1 条重复、1 条脏数据）"
    assert all(i["job_id"] for i in items)
    assert all(i["title"] for i in items)
    assert len({i["job_id"] for i in items}) == len(items)


# --- 地点：市·区·商圈 ---
#
# 这三段是**抓详情页之前**唯一能拿到的地理信息。以前 normalize_list_item
# 只留第一段就把后两段扔了，等于把「按区域筛掉一半再决定抓谁」的能力也扔了。


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("杭州·滨江区·长河", ("杭州", "滨江区", "长河")),
        ("杭州·西湖区", ("杭州", "西湖区", "")),
        ("杭州", ("杭州", "", "")),
        ("  杭州·余杭区·仓前  ", ("杭州", "余杭区", "仓前")),
        ("", ("", "", "")),
        ("杭州··长河", ("杭州", "长河", "")),  # 空段跳过，不产生空区县
    ],
)
def test_location_is_split_into_city_district_and_area(raw, expected):
    item = normalize_list_item(
        {"url": "/job_detail/abc123~.html", "title": "后端", "city": raw}
    )
    assert (item["city"], item["district"], item["business_area"]) == expected


# --- 详情页坐标 ---
#
# Boss 的属性叫 data-lat，里面其实是「经度,纬度」，且是 GCJ-02（页面用高德）。


def test_parse_coords_reads_longitude_first():
    assert parse_coords("120.008921,30.282488") == (120.008921, 30.282488)


@pytest.mark.parametrize(
    "raw",
    [
        None, "", "120.008921", "120.008921,30.282488,5",
        "abc,def", "120.008921,",
        "8.5,47.3",      # 苏黎世：解析得出来，但不在中国范围内
        "30.28,120.00",  # 经纬度写反了——落在范围外，必须拒掉
    ],
)
def test_parse_coords_refuses_anything_it_cannot_trust(raw):
    """距离宁可不显示，也不能显示一个错的。返回 None 而不是 (0,0) 或猜一个：
    后两种会被下游当成真实坐标，算出一堆看起来精确的假距离。"""
    assert parse_coords(raw) is None
