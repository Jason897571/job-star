"""两类配置：进程级（env，启动时定）和面板可配置项（settings 表，随时可改）。"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jobstar.db import DEFAULT_DB_PATH

_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CARDS_PATH = _REPO_ROOT / "data" / "capability_cards.yaml"

VALID_BACKENDS = ("claude_cli", "gateway")

# 面板可配置项的默认值。score_threshold 为 None 表示冷启动期，不生成任何动作。
SETTING_DEFAULTS: dict[str, Any] = {
    "daily_greeting_limit": 25,
    "score_threshold": None,
    "gate_rules": {
        "city_whitelist": ["杭州"],
        "salary_min": 25,  # 千元/月
        "degree_hard": False,
        "years_min": 0,
        "years_max": 10,
        "company_blacklist": [],
    },
    "dimension_weights": {
        "skills": 0.35,
        "industry": 0.15,
        "duties": 0.25,
        "years": 0.15,
        "bonus": 0.10,
    },
    "my_degree": "硕士",
    # 采集健康状态。由 CLI 的 collect 子命令写入，面板顶部横幅读取（Task 13）。
    # last_collect_at 记录「最近一次尝试」（不论成败），last_collect_ok_at
    # 只在真正成功的那次采集上前进——两者不相等时，说明最近一次尝试其实
    # 失败了，横幅不能据此宣称「未见异常」（fix round 1，review finding 1）。
    "last_collect_error": None,
    "last_collect_at": None,
    "last_collect_ok_at": None,
    # 面板「采集」页上次用的搜索条件，纯粹为了下次打开时预填表单。
    # 城市码是 Boss 的 city code（杭州 101210100）。
    "last_search": {"keywords": [], "city": "101210100", "pages": 1},
}


@dataclass(frozen=True)
class Settings:
    llm_backend: str
    gateway_base_url: str
    gateway_api_key: str
    gateway_model_fast: str
    gateway_model_strong: str
    db_path: Path
    cards_path: Path


def _load_dotenv() -> None:
    """极简 .env 读取：已存在的环境变量优先，不覆盖。"""
    env_file = _REPO_ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def get_settings() -> Settings:
    _load_dotenv()
    backend = os.environ.get("JOBSTAR_LLM_BACKEND", "claude_cli").strip()
    if backend not in VALID_BACKENDS:
        raise ValueError(
            f"JOBSTAR_LLM_BACKEND 只能是 {VALID_BACKENDS} 之一，实际是 {backend!r}"
        )
    db_path = os.environ.get("JOBSTAR_DB_PATH", "").strip()
    cards_path = os.environ.get("JOBSTAR_CARDS_PATH", "").strip()
    return Settings(
        llm_backend=backend,
        gateway_base_url=os.environ.get("JOBSTAR_GATEWAY_BASE_URL", "").strip(),
        gateway_api_key=os.environ.get("JOBSTAR_GATEWAY_API_KEY", "").strip(),
        gateway_model_fast=os.environ.get(
            "JOBSTAR_GATEWAY_MODEL_FAST", "claude-sonnet-5"
        ).strip(),
        gateway_model_strong=os.environ.get(
            "JOBSTAR_GATEWAY_MODEL_STRONG", "claude-opus-5"
        ).strip(),
        db_path=Path(db_path) if db_path else DEFAULT_DB_PATH,
        cards_path=Path(cards_path) if cards_path else DEFAULT_CARDS_PATH,
    )


def get_setting(conn: sqlite3.Connection, key: str) -> Any:
    if key not in SETTING_DEFAULTS:
        raise KeyError(f"未知配置项 {key!r}，可用项：{sorted(SETTING_DEFAULTS)}")
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if row is None:
        return SETTING_DEFAULTS[key]
    return json.loads(row["value"])


def _is_number(value: Any) -> bool:
    """True/False 是 int 的子类，但这里的字段都不该接受布尔值。"""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_str_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(v, str) for v in value)


def validate_setting_value(key: str, value: Any) -> None:
    """写入前校验值的*形状*（get_setting 只查过键名，没查过值本身）。

    review finding 2：`PUT /api/settings` 曾经只挡未知键，不挡值的形状——
    `{"dimension_weights": "oops"}` 能写进去，等到打分器里
    `weights.get(d.name, 0.0)` 才炸成 AttributeError，那时人工早就忘了是
    哪次编辑引入的。这里把校验挪到写入的时刻，配 set_setting 调用，让
    CLI 和面板共用同一份规则，而不是只有面板路由自己挡。

    不认识的键仍然由调用方（get_setting/set_setting）抛 KeyError；这里
    只负责“键是认识的，但值不对”的情况，一律抛 ValueError。
    """
    if key == "dimension_weights":
        if not isinstance(value, dict):
            raise ValueError(f"{key} 必须是字典（维度名 -> 权重）")
        from jobstar.models import DIMENSIONS

        if set(value) != set(DIMENSIONS):
            raise ValueError(f"{key} 的键必须恰好是 {sorted(DIMENSIONS)}")
        for dim, weight in value.items():
            if not _is_number(weight) or weight < 0:
                raise ValueError(f"{key}.{dim} 必须是非负数字，实际是 {weight!r}")
        if sum(value.values()) == 0:
            raise ValueError(f"{key} 不能全部为 0（会导致所有总分恒为 0）")

    elif key == "gate_rules":
        if not isinstance(value, dict):
            raise ValueError(f"{key} 必须是字典")
        if "city_whitelist" in value and not _is_str_list(value["city_whitelist"]):
            raise ValueError(f"{key}.city_whitelist 必须是字符串列表")
        if "company_blacklist" in value and not _is_str_list(
            value["company_blacklist"]
        ):
            raise ValueError(f"{key}.company_blacklist 必须是字符串列表")
        for sub in ("salary_min", "years_min", "years_max"):
            if sub in value and value[sub] is not None and not _is_number(value[sub]):
                raise ValueError(f"{key}.{sub} 必须是数字或 null")
        if "degree_hard" in value and not isinstance(value["degree_hard"], bool):
            raise ValueError(f"{key}.degree_hard 必须是布尔值")
        # 未知子键（例如 my_degree）允许——它们在别处（pipeline._gate_rules）
        # 被合并进这份规则，不属于 gate_rules 自身的 schema。

    elif key == "score_threshold":
        if value is not None and not (_is_number(value) and 0 <= value <= 100):
            raise ValueError(f"{key} 必须是 0-100 的数字，或 null（冷启动期）")

    elif key == "daily_greeting_limit":
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError(f"{key} 必须是非负整数")

    elif key == "my_degree":
        from jobstar.gate import DEGREE_ORDER

        if value not in DEGREE_ORDER:
            raise ValueError(f"{key} 必须是 {sorted(DEGREE_ORDER)} 之一")

    elif key in ("last_collect_error", "last_collect_at", "last_collect_ok_at"):
        if value is not None and not isinstance(value, str):
            raise ValueError(f"{key} 必须是字符串或 null")

    elif key == "last_search":
        if not isinstance(value, dict):
            raise ValueError(f"{key} 必须是字典")
        if not _is_str_list(value.get("keywords", [])):
            raise ValueError(f"{key}.keywords 必须是字符串列表")
        city = value.get("city", "")
        if not isinstance(city, str) or not city.isdigit():
            raise ValueError(f"{key}.city 必须是纯数字的城市码字符串")
        pages = value.get("pages", 1)
        if not isinstance(pages, int) or isinstance(pages, bool) or not 1 <= pages <= 10:
            raise ValueError(f"{key}.pages 必须是 1-10 的整数")


def set_setting(conn: sqlite3.Connection, key: str, value: Any) -> None:
    if key not in SETTING_DEFAULTS:
        raise KeyError(f"未知配置项 {key!r}，可用项：{sorted(SETTING_DEFAULTS)}")
    validate_setting_value(key, value)
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, json.dumps(value, ensure_ascii=False)),
    )
    conn.commit()
