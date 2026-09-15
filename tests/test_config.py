import pytest

from jobstar.config import SETTING_DEFAULTS, get_setting, get_settings, set_setting
from jobstar.db import get_conn, init_db


@pytest.fixture()
def conn(tmp_path):
    c = get_conn(tmp_path / "t.db")
    init_db(c)
    return c


def test_get_setting_falls_back_to_default(conn):
    assert get_setting(conn, "daily_greeting_limit") == 25


def test_score_threshold_defaults_to_none(conn):
    """设计文档 §5.3：第一版交付时不设阈值，一条消息都不发。"""
    assert get_setting(conn, "score_threshold") is None


def test_set_then_get_roundtrips_structured_values(conn):
    rules = {"city_whitelist": ["杭州", "上海"], "salary_min": 30}
    set_setting(conn, "gate_rules", rules)
    assert get_setting(conn, "gate_rules") == rules


def test_dimension_weights_default_sums_to_one():
    total = sum(SETTING_DEFAULTS["dimension_weights"].values())
    assert abs(total - 1.0) < 1e-9


def test_unknown_setting_key_raises(conn):
    with pytest.raises(KeyError):
        get_setting(conn, "no_such_key")


def test_get_settings_reads_backend_from_env(monkeypatch):
    monkeypatch.setenv("JOBSTAR_LLM_BACKEND", "gateway")
    monkeypatch.setenv("JOBSTAR_GATEWAY_BASE_URL", "https://example.test/v1")
    s = get_settings()
    assert s.llm_backend == "gateway"
    assert s.gateway_base_url == "https://example.test/v1"


def test_get_settings_rejects_unknown_backend(monkeypatch):
    monkeypatch.setenv("JOBSTAR_LLM_BACKEND", "openai")
    with pytest.raises(ValueError):
        get_settings()


# --- review finding 2：值的形状校验要放在 set_setting 本身，而不是只在面板
# 路由里挡——这样 CLI（或任何未来的调用方）直接调 set_setting 也一样受保护，
# 不需要每个调用点自己重复校验。


def test_set_setting_rejects_bad_value_shape(conn):
    """`set_setting` 本身要抛 ValueError，不能指望调用方（比如面板路由）
    自己去校验值的形状——CLI 也是直接调 set_setting，同样要被挡住。"""
    with pytest.raises(ValueError):
        set_setting(conn, "dimension_weights", "oops")


def test_set_setting_accepts_legitimate_values(conn):
    """现有代码路径（CLI、pipeline、executor 的测试）实际写过的合法值都
    不能被新加的校验误伤。"""
    set_setting(conn, "daily_greeting_limit", 25)
    set_setting(conn, "score_threshold", None)
    set_setting(conn, "score_threshold", 70)
    set_setting(conn, "gate_rules", {"city_whitelist": ["杭州", "上海"], "salary_min": 30})
