import json
from pathlib import Path

import pytest

from jobstar.collector.parse import (
    clean_text,
    dedup,
    extract_job_id,
    decode_obfuscated_salary,
    decode_salaries,
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


# --- 列表页薪资的字体混淆 ---
#
# 数字被换成 Unicode 私用区码位，靠 kanzhun-mix 渲染成人眼可读的数字。
# innerText 拿到的就是这些码位，看起来像 "-K·薪"——数字一直在，只是不是
# ASCII。映射 0xE031+n → n 是 2026-09-16 逆向出来的，在两个独立会话上
# 各验证过一遍（31/31）。


def _pua(text: str) -> str:
    """把 ASCII 数字换成对应的私用区码位，模拟页面上真实拿到的串。"""
    return "".join(chr(0xE031 + int(ch)) if ch.isdigit() else ch for ch in text)


@pytest.mark.parametrize(
    "plain",
    ["25-45K·14薪", "30-50K", "20-30K·16薪", "65-95K·16薪", "8-12K"],
)
def test_obfuscated_salary_decodes_back_to_plaintext(plain):
    assert decode_obfuscated_salary(_pua(plain)) == plain


def test_a_real_captured_string_decodes(): 
    """2026-09-16 从真实列表页抓到的原串。"""
    assert decode_obfuscated_salary("-K·薪") == "20-30K·16薪"


@pytest.mark.parametrize(
    "raw,why",
    [
        ("-K", "出现没见过的码位 → 映射可能已经变了"),
        (_pua("45-25K"), "下限大于上限 → 解出来的数字不可信"),
        (_pua("0-30K"), "下限是 0"),
        ("500-1000元/天", "日薪，不是本解码器认识的形状"),
        ("面议", "根本没有数字"),
        ("", "空串"),
    ],
)
def test_untrustworthy_salaries_come_back_as_none(raw, why):
    """解不出来就当「不知道薪资」。门禁对字段缺失一律放过，于是退化成改这版
    之前的行为；而一个解错的数字会让门禁照着假数据筛掉真岗位。"""
    assert decode_obfuscated_salary(raw) is None, why


def test_an_impossible_month_count_is_rejected():
    """映射平移之后 `25-45K·14薪` 会解成 `36-56K·25薪`——形状合法、数字看着
    也正常，只有「25薪」荒谬。月数这道闸是单条校验里唯一挡得住它的东西。"""
    rotated = "".join(chr(ord(ch) + 1) if 0xE031 <= ord(ch) <= 0xE03A else ch
                      for ch in _pua("25-45K·14薪"))
    assert decode_obfuscated_salary(rotated) is None


def _item(salary, job_id="abc123"):
    return normalize_list_item(
        {"url": f"/job_detail/{job_id}~.html", "title": "后端",
         "city": "杭州·滨江区", "salary": salary}
    )


def test_a_page_of_salaries_is_decoded_so_the_pre_gate_can_use_them():
    """这是整件事的意义：薪资在列表页就能读，预门禁因此能在**抓详情页之前**
    按薪资刷掉岗位——那是唯一能省下页面请求的地方。"""
    items = decode_salaries([
        _item(_pua("25-45K·14薪"), "a"), _item(_pua("30-50K"), "b"),
        _item("面议", "c"),
    ])
    assert [i["salary_raw"] for i in items] == ["25-45K·14薪", "30-50K", "面议"]


def test_a_rotated_mapping_makes_the_whole_page_fall_back_to_raw():
    """单条校验挡不住「整套映射被换了」：平移后 `30-50K` 变成 `41-61K`，
    形状和数值都挑不出毛病。但一整页里带「薪」的那些会解出不可能的月数而
    失败——成功率掉下去，就该整批不信，而不是把混着真假的结果交出去。"""
    def rot(t):
        return "".join(chr(ord(c) + 1) if 0xE031 <= ord(c) <= 0xE03A else c
                       for c in _pua(t))
    raws = [rot("25-45K·14薪"), rot("30-60K·16薪"), rot("20-30K·13薪"), rot("30-50K")]
    items = decode_salaries([_item(r, f"j{i}") for i, r in enumerate(raws)])
    assert [i["salary_raw"] for i in items] == raws, "整批退回原串，一条都不采信"


def test_a_page_where_most_decode_is_trusted_even_if_one_is_odd():
    """一两条解不出来是常态（面议、日薪）。不能因为个别失败就否定整页。"""
    items = decode_salaries([
        _item(_pua("25-45K·14薪"), "a"), _item(_pua("30-50K"), "b"),
        _item(_pua("20-40K·15薪"), "c"), _item("-K", "d"),
    ])
    assert items[0]["salary_raw"] == "25-45K·14薪"
    assert items[3]["salary_raw"] == "-K", "解不出的那条保持原样"


def test_normalize_keeps_the_raw_string_and_leaves_decoding_to_the_batch():
    """留着原串才看得出是「混淆没解开」还是「页面真的没给薪资」。"""
    assert _item("面议")["salary_raw"] == "面议"
    assert _item(_pua("25-45K"))["salary_raw"] == _pua("25-45K")
