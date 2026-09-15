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
    "my_years": 5,
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


def set_setting(conn: sqlite3.Connection, key: str, value: Any) -> None:
    if key not in SETTING_DEFAULTS:
        raise KeyError(f"未知配置项 {key!r}，可用项：{sorted(SETTING_DEFAULTS)}")
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, json.dumps(value, ensure_ascii=False)),
    )
    conn.commit()
