# Boss 直聘岗位匹配与沟通助手 · 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现设计文档 `docs/superpowers/specs/2026-09-14-boss-auto-apply-design.md` 中的流 A：Boss 直聘岗位采集 → 硬门禁 → LLM 维度打分 → 话术草稿 → 待确认队列 → 人工确认 → 执行发送 → 留痕。

**Architecture:** 决策器是纯函数层（归一化 / 门禁 / 打分 / 话术），不持有浏览器句柄，可对历史岗位离线回放；执行器只消费 `actions` 表中 `approved` 状态的动作，通过 browser-harness 驱动本地 Chrome。两层之间唯一的接口是 SQLite。LLM 调用收敛到单个 `call_json()` 函数，背后两个可切换后端：本地 `claude` CLI（headless，复用 Claude Code 登录态，零配置）和 OpenAI 兼容网关（httpx 直调）。

**Tech Stack:** Python 3.12 + uv、SQLite（stdlib `sqlite3`）、httpx、FastAPI + uvicorn、PyYAML、pytest；浏览器层复用已安装的 browser-harness（CDP 连本地 Chrome）。

## Global Constraints

- **Python 版本固定 3.12**：`requires-python = ">=3.12,<3.14"`，仓库根写 `.python-version` 内容为 `3.12`。本机 `python3` 是 3.14.7，FastAPI/pydantic 在 3.14 上 wheel 不齐，一律用 `uv run` 而不是系统 python。
- **绝不自动发送**：任何对外发送动作必须来自 `actions` 表中 `status='approved'` 的行，而 `approved` 只能由面板上的人工点击写入。代码中不存在把 `pending` 直接变 `sent` 的路径。
- **第一版不设阈值**：`score_threshold` 默认值是 `None`。`None` 时打分结果只进标注视图，不生成 `actions` 行。这是设计文档 §5.3 的冷启动要求。
- **每个维度必须有证据卡片**：打分器返回的维度若 `card_ids` 为空，该维度分数在**代码里**强制归零，不依赖 prompt 遵守。
- **弱证据不得作为主要依据**：`strength == 弱` 的卡片若是某维度唯一引用，该维度分数在代码里封顶 40 分。
- **LLM 不合 schema 只重试一次**（设计文档 §7）：第二次仍失败则抛 `LLMSchemaError`，岗位标记 `scoring_failed`，不猜测分数。
- **模型档位**：归一化用 `fast` 档（Sonnet / 网关的 fast 模型），打分和话术用 `strong` 档（Opus / 网关的 strong 模型）。档位名只有 `"fast"` 和 `"strong"` 两个字符串。
- **能力卡片 YAML 用中文键**（设计文档 §4.4 规定的格式，且需要本人逐张校对），代码里映射成 ASCII 字段名；映射表只存在于 `evidence.py` 一处。
- **所有金额单位是千元/月**（Boss 的 `20-35K` 存成 `salary_min=20, salary_max=35`）。
- 提交信息用中文，格式 `feat: xxx` / `test: xxx` / `fix: xxx` / `docs: xxx`（仓库既有历史已用 `docs:`）。

## File Structure

| 文件 | 职责 |
|---|---|
| `pyproject.toml` / `.python-version` / `.env.example` | 工程配置与依赖 |
| `src/jobstar/models.py` | 所有跨模块的 dataclass：`CapabilityCard` `JobRequirements` `GateResult` `DimensionScore` `ScoreResult` |
| `src/jobstar/db.py` | SQLite 建表与连接 |
| `src/jobstar/config.py` | 进程级配置（env）+ 面板可配置项（`settings` 表） |
| `src/jobstar/llm/__init__.py` | `call_json()`：JSON 提取、重试一次、后端分发 |
| `src/jobstar/llm/claude_cli.py` | 后端 A：subprocess 调本地 `claude -p` |
| `src/jobstar/llm/gateway.py` | 后端 B：httpx 调 OpenAI 兼容 `/chat/completions` |
| `src/jobstar/evidence.py` | 能力卡片 YAML 加载与校验、`FullDumpStore` |
| `src/jobstar/gate.py` | 硬门禁纯规则 |
| `src/jobstar/normalizer.py` | JD 自由文本 → `JobRequirements` |
| `src/jobstar/scorer.py` | `JobRequirements` + 卡片 → `ScoreResult` |
| `src/jobstar/pitch.py` | 打招呼话术草稿 |
| `src/jobstar/actions.py` | 待确认队列状态机与每日配额 |
| `src/jobstar/collector/parse.py` | HTML → dict 的纯函数（对固化 fixture 做测试） |
| `src/jobstar/collector/boss.py` | browser-harness 驱动：搜索列表 + 详情页 |
| `src/jobstar/executor.py` | 只消费 `approved` 动作，节流 / 配额 / 失败留痕 |
| `src/jobstar/panel/app.py` + `panel/static/index.html` | FastAPI + 单页面板（队列 / 拆解 / 标注 / 设置） |
| `src/jobstar/cli.py` | `jobstar collect|score|serve|cards` 入口 |
| `data/capability_cards.yaml` | 由 MASTER.md 提炼、本人校对的能力卡片 |
| `tests/` | 纯函数层快照测试 + fixtures |

---

## Task 1: 工程骨架与数据层

**Files:**
- Create: `pyproject.toml`, `.python-version`, `.gitignore`（追加）, `.env.example`
- Create: `src/jobstar/__init__.py`, `src/jobstar/models.py`, `src/jobstar/db.py`, `src/jobstar/config.py`
- Test: `tests/test_db.py`, `tests/test_config.py`

**Interfaces:**
- Consumes: 无（第一个任务）
- Produces:
  - `jobstar.models`：`Strength`(Enum 强/中/弱)、`CapabilityCard`、`JobRequirements`、`GateResult`、`DimensionScore`、`ScoreResult`、`DIMENSIONS: tuple[str, ...]`
  - `jobstar.db`：`get_conn(path: Path | None = None) -> sqlite3.Connection`、`init_db(conn) -> None`
  - `jobstar.config`：`Settings` dataclass、`get_settings() -> Settings`、`get_setting(conn, key) -> Any`、`set_setting(conn, key, value) -> None`、`SETTING_DEFAULTS: dict`

- [ ] **Step 1: 初始化工程**

```bash
cd <仓库根目录>
printf '3.12\n' > .python-version
uv venv --python 3.12
```

写 `pyproject.toml`：

```toml
[project]
name = "jobstar"
version = "0.1.0"
description = "Boss 直聘岗位匹配与沟通助手"
requires-python = ">=3.12,<3.14"
dependencies = [
    "httpx>=0.27",
    "pyyaml>=6.0",
    "fastapi>=0.115",
    "uvicorn>=0.30",
]

[project.scripts]
jobstar = "jobstar.cli:main"

[dependency-groups]
dev = ["pytest>=8.0"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/jobstar"]

[tool.pytest.ini_options]
testpaths = ["tests"]
```

```bash
uv sync
```

- [ ] **Step 2: 追加 .gitignore 与 .env.example**

在 `.gitignore` 末尾追加：

```
# jobstar
data/*.db
data/fixtures/live/
.env
```

写 `.env.example`：

```bash
# LLM 后端：claude_cli（调本地 claude CLI，复用 Claude Code 登录态）或 gateway（OpenAI 兼容网关）
JOBSTAR_LLM_BACKEND=claude_cli

# 仅 gateway 后端需要
JOBSTAR_GATEWAY_BASE_URL=
JOBSTAR_GATEWAY_API_KEY=
JOBSTAR_GATEWAY_MODEL_FAST=claude-sonnet-5
JOBSTAR_GATEWAY_MODEL_STRONG=claude-opus-5

# 路径（留空用默认值）
JOBSTAR_DB_PATH=
JOBSTAR_CARDS_PATH=
```

- [ ] **Step 3: 写 models.py**

```python
"""跨模块共享的数据结构。所有字段都是不可变的。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

# 打分维度，顺序固定；权重配置和 LLM 输出都以此为准
DIMENSIONS: tuple[str, ...] = ("skills", "industry", "duties", "years", "bonus")

DIMENSION_LABELS: dict[str, str] = {
    "skills": "技能匹配",
    "industry": "行业匹配",
    "duties": "岗位职责匹配",
    "years": "经验年限",
    "bonus": "加分项",
}


class Strength(str, Enum):
    """能力卡片的证据强度。这是本系统防止简历吹牛的唯一防线。"""

    STRONG = "强"  # 能讲 30 分钟
    MEDIUM = "中"  # 能讲 5 分钟
    WEAK = "弱"  # 仅接触


@dataclass(frozen=True)
class CapabilityCard:
    id: str
    capability: str  # 能力
    synonyms: tuple[str, ...]  # 同义表述
    strength: Strength  # 证据强度
    project: str  # 项目
    metrics: tuple[str, ...]  # 可量化
    depth: str  # 可讲深度
    resume_versions: tuple[str, ...]  # 关联简历版本


@dataclass(frozen=True)
class JobRequirements:
    """JD 自由文本的结构化抽取结果。薪资单位是千元/月。"""

    job_id: str
    city: str | None
    degree: str | None  # 不限 / 大专 / 本科 / 硕士 / 博士
    years_min: int | None
    years_max: int | None
    salary_min: int | None
    salary_max: int | None
    skills: tuple[str, ...]
    industry: str | None
    company_size: str | None
    category: str | None


@dataclass(frozen=True)
class GateResult:
    passed: bool
    reject_reason: str | None


@dataclass(frozen=True)
class DimensionScore:
    name: str  # DIMENSIONS 之一
    score: float  # 0-100
    card_ids: tuple[str, ...]  # 为空则该维度记 0 分
    reason: str  # 命中理由
    gap: str  # 缺口说明


@dataclass(frozen=True)
class ScoreResult:
    job_id: str
    total: float  # 加权总分 0-100
    dimensions: tuple[DimensionScore, ...]
    scorer_version: str
```

- [ ] **Step 4: 写 db.py 的失败测试**

写 `tests/test_db.py`：

```python
from jobstar.db import get_conn, init_db

EXPECTED_TABLES = {
    "jobs",
    "requirements",
    "gate_results",
    "scores",
    "actions",
    "applications",
    "labels",
    "settings",
}


def test_init_db_creates_all_tables(tmp_path):
    conn = get_conn(tmp_path / "t.db")
    init_db(conn)
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    assert EXPECTED_TABLES <= {r["name"] for r in rows}


def test_job_id_is_unique_per_platform(tmp_path):
    import sqlite3

    conn = get_conn(tmp_path / "t.db")
    init_db(conn)
    insert = (
        "INSERT INTO jobs (platform, job_id, title, company, raw_jd) "
        "VALUES ('boss', 'abc123', '后端工程师', '某公司', '')"
    )
    conn.execute(insert)
    conn.commit()
    try:
        conn.execute(insert)
        conn.commit()
    except sqlite3.IntegrityError:
        return
    raise AssertionError("重复 job_id 应当被唯一约束挡住")


def test_init_db_is_idempotent(tmp_path):
    conn = get_conn(tmp_path / "t.db")
    init_db(conn)
    init_db(conn)  # 第二次不应抛错


def test_rows_are_dict_like(tmp_path):
    conn = get_conn(tmp_path / "t.db")
    init_db(conn)
    conn.execute(
        "INSERT INTO jobs (platform, job_id, title, company, raw_jd) "
        "VALUES ('boss', 'x', 't', 'c', 'jd')"
    )
    conn.commit()
    row = conn.execute("SELECT title FROM jobs").fetchone()
    assert row["title"] == "t"
```

- [ ] **Step 5: 跑测试确认失败**

Run: `uv run pytest tests/test_db.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'jobstar.db'`

- [ ] **Step 6: 写 db.py**

```python
"""SQLite 数据层。所有表在 init_db 里一次建好。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

DEFAULT_DB_PATH = Path(__file__).resolve().parents[2] / "data" / "jobstar.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id              INTEGER PRIMARY KEY,
    platform        TEXT NOT NULL DEFAULT 'boss',
    job_id          TEXT NOT NULL,
    title           TEXT NOT NULL,
    company         TEXT NOT NULL,
    raw_jd          TEXT NOT NULL DEFAULT '',
    city            TEXT,
    salary_raw      TEXT,
    hr_name         TEXT,
    url             TEXT,
    collected_at    TEXT NOT NULL DEFAULT (datetime('now')),
    detail_fetched  INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'new',
    UNIQUE (platform, job_id)
);

CREATE TABLE IF NOT EXISTS requirements (
    job_id        TEXT PRIMARY KEY,
    city          TEXT,
    degree        TEXT,
    years_min     INTEGER,
    years_max     INTEGER,
    salary_min    INTEGER,
    salary_max    INTEGER,
    skills        TEXT NOT NULL DEFAULT '[]',
    industry      TEXT,
    company_size  TEXT,
    category      TEXT
);

CREATE TABLE IF NOT EXISTS gate_results (
    job_id        TEXT PRIMARY KEY,
    passed        INTEGER NOT NULL,
    reject_reason TEXT,
    checked_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS scores (
    job_id         TEXT PRIMARY KEY,
    total          REAL,
    dimensions     TEXT NOT NULL DEFAULT '[]',
    scored_at      TEXT NOT NULL DEFAULT (datetime('now')),
    scorer_version TEXT NOT NULL,
    error          TEXT
);

CREATE TABLE IF NOT EXISTS actions (
    id          INTEGER PRIMARY KEY,
    type        TEXT NOT NULL,
    job_id      TEXT NOT NULL,
    payload     TEXT NOT NULL DEFAULT '{}',
    status      TEXT NOT NULL DEFAULT 'pending',
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    decided_at  TEXT,
    sent_at     TEXT,
    error       TEXT,
    UNIQUE (type, job_id)
);

CREATE TABLE IF NOT EXISTS applications (
    id             INTEGER PRIMARY KEY,
    job_id         TEXT NOT NULL,
    action_id      INTEGER NOT NULL,
    hr_name        TEXT,
    greeting_text  TEXT NOT NULL,
    score_snapshot TEXT NOT NULL DEFAULT '{}',
    sent_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS labels (
    job_id       TEXT PRIMARY KEY,
    would_apply  INTEGER NOT NULL,
    labeled_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs (status);
CREATE INDEX IF NOT EXISTS idx_actions_status ON actions (status);
"""


def get_conn(path: Path | None = None) -> sqlite3.Connection:
    """打开数据库连接。行以 sqlite3.Row 返回，支持按列名取值。"""
    db_path = Path(path) if path is not None else DEFAULT_DB_PATH
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()
```

- [ ] **Step 7: 跑测试确认通过**

Run: `uv run pytest tests/test_db.py -v`
Expected: 4 passed

- [ ] **Step 8: 写 config.py 的失败测试**

写 `tests/test_config.py`：

```python
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
```

- [ ] **Step 9: 跑测试确认失败**

Run: `uv run pytest tests/test_config.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'jobstar.config'`

- [ ] **Step 10: 写 config.py**

```python
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
```

- [ ] **Step 11: 跑全部测试**

Run: `uv run pytest -v`
Expected: 11 passed

- [ ] **Step 12: 提交**

```bash
git add pyproject.toml uv.lock .python-version .gitignore .env.example src tests
git commit -m "feat: 工程骨架、SQLite 数据层与配置读写"
```

---

## Task 2: LLM 双后端

**Files:**
- Create: `src/jobstar/llm/__init__.py`, `src/jobstar/llm/claude_cli.py`, `src/jobstar/llm/gateway.py`
- Test: `tests/test_llm.py`

**Interfaces:**
- Consumes: `jobstar.config.get_settings()`（Task 1）
- Produces:
  - `jobstar.llm.call_json(*, system: str, user: str, tier: str) -> dict` — `tier` 只能是 `"fast"` 或 `"strong"`
  - `jobstar.llm.extract_json(text: str) -> dict`
  - `jobstar.llm.LLMBackendError`、`jobstar.llm.LLMSchemaError`

**背景（已实测，不要重新试）：** 本机 `claude` 版本 2.1.220，`claude -p` 从 stdin 读 prompt，`--output-format json` 返回一个信封，正文在 `.result` 字段。即使 system prompt 明确要求「不要 markdown 围栏」，它仍然会返回 ` ```json ... ``` `，所以**围栏剥离是必须的**。加上 `--system-prompt --exclude-dynamic-system-prompt-sections --setting-sources '' --strict-mcp-config --mcp-config '{"mcpServers":{}}' --allowed-tools ''` 后单次往返约 4 秒；不加这些参数会把全局 CLAUDE.md 和 skills 一起塞进上下文，慢一倍且每次多烧约 24K token。

- [ ] **Step 1: 写失败测试**

写 `tests/test_llm.py`：

```python
import json

import pytest

from jobstar.llm import LLMSchemaError, call_json, extract_json


def test_extract_json_handles_bare_object():
    assert extract_json('{"a": 1}') == {"a": 1}


def test_extract_json_strips_json_fence():
    """claude CLI 实测会加围栏，即使 system prompt 明确禁止。"""
    raw = '```json\n{"city": "杭州", "salary_min": 30}\n```'
    assert extract_json(raw) == {"city": "杭州", "salary_min": 30}


def test_extract_json_strips_bare_fence():
    assert extract_json("```\n{\"a\": 1}\n```") == {"a": 1}


def test_extract_json_tolerates_surrounding_whitespace():
    assert extract_json('\n\n  {"a": 1}  \n') == {"a": 1}


def test_extract_json_rejects_non_object():
    with pytest.raises(json.JSONDecodeError):
        extract_json("这不是 JSON")


def test_call_json_retries_once_then_raises(monkeypatch):
    calls = []

    def fake_call(*, system, user, tier, timeout=180):
        calls.append(user)
        return "还是不合法"

    monkeypatch.setattr("jobstar.llm._resolve_backend", lambda: fake_call)
    with pytest.raises(LLMSchemaError):
        call_json(system="s", user="u", tier="fast")
    assert len(calls) == 2, "设计文档 §7：重试一次，不是无限重试"
    assert "JSON" in calls[1], "第二次应当把「上次不是合法 JSON」的提示追加进去"


def test_call_json_succeeds_on_retry(monkeypatch):
    outputs = iter(["坏的", '{"ok": true}'])

    def fake_call(*, system, user, tier, timeout=180):
        return next(outputs)

    monkeypatch.setattr("jobstar.llm._resolve_backend", lambda: fake_call)
    assert call_json(system="s", user="u", tier="fast") == {"ok": True}


def test_call_json_rejects_bad_tier(monkeypatch):
    monkeypatch.setattr("jobstar.llm._resolve_backend", lambda: None)
    with pytest.raises(ValueError):
        call_json(system="s", user="u", tier="cheap")


def test_claude_cli_unwraps_envelope(monkeypatch):
    import jobstar.llm.claude_cli as mod

    envelope = {"is_error": False, "result": '{"a": 1}'}
    captured = {}

    class FakeProc:
        returncode = 0
        stdout = json.dumps(envelope)
        stderr = ""

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["input"] = kwargs.get("input")
        return FakeProc()

    monkeypatch.setattr(mod.shutil, "which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    out = mod.call(system="sys", user="用户内容", tier="strong")
    assert out == '{"a": 1}'
    assert captured["input"] == "用户内容", "prompt 必须走 stdin，不走 argv"
    assert "--model" in captured["cmd"]
    assert captured["cmd"][captured["cmd"].index("--model") + 1] == "opus"


def test_claude_cli_raises_on_error_envelope(monkeypatch):
    import jobstar.llm.claude_cli as mod
    from jobstar.llm import LLMBackendError

    class FakeProc:
        returncode = 0
        stdout = json.dumps({"is_error": True, "result": "配额用尽"})
        stderr = ""

    monkeypatch.setattr(mod.shutil, "which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr(mod.subprocess, "run", lambda cmd, **kw: FakeProc())
    with pytest.raises(LLMBackendError, match="配额用尽"):
        mod.call(system="s", user="u", tier="fast")


def test_gateway_posts_openai_shape(monkeypatch):
    import jobstar.llm.gateway as mod

    captured = {}

    class FakeResp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": '{"a": 1}'}}]}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured["json"] = kwargs["json"]
        captured["headers"] = kwargs["headers"]
        return FakeResp()

    monkeypatch.setenv("JOBSTAR_LLM_BACKEND", "gateway")
    monkeypatch.setenv("JOBSTAR_GATEWAY_BASE_URL", "https://gw.test/v1/")
    monkeypatch.setenv("JOBSTAR_GATEWAY_API_KEY", "sk-test")
    monkeypatch.setenv("JOBSTAR_GATEWAY_MODEL_STRONG", "big-model")
    monkeypatch.setattr(mod.httpx, "post", fake_post)

    assert mod.call(system="s", user="u", tier="strong") == '{"a": 1}'
    assert captured["url"] == "https://gw.test/v1/chat/completions"
    assert captured["json"]["model"] == "big-model"
    assert captured["json"]["messages"][0] == {"role": "system", "content": "s"}
    assert captured["headers"]["Authorization"] == "Bearer sk-test"


def test_gateway_requires_base_url(monkeypatch):
    import jobstar.llm.gateway as mod
    from jobstar.llm import LLMBackendError

    monkeypatch.setenv("JOBSTAR_LLM_BACKEND", "gateway")
    monkeypatch.setenv("JOBSTAR_GATEWAY_BASE_URL", "")
    with pytest.raises(LLMBackendError, match="JOBSTAR_GATEWAY_BASE_URL"):
        mod.call(system="s", user="u", tier="fast")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_llm.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'jobstar.llm'`

- [ ] **Step 3: 写 llm/__init__.py**

```python
"""LLM 调用的唯一入口。上层只认 call_json，不关心后端是哪个。"""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Protocol

VALID_TIERS = ("fast", "strong")

_RETRY_HINT = "\n\n上一次输出不是合法 JSON。只输出 JSON 对象本身，不要 markdown 围栏，不要任何解释文字。"


class LLMBackendError(RuntimeError):
    """后端本身失败：CLI 退出码非零、网关 5xx、配置缺失。"""


class LLMSchemaError(RuntimeError):
    """重试一次后仍拿不到合法 JSON。设计文档 §7：不猜测，交人工处理。"""


class Backend(Protocol):
    def __call__(
        self, *, system: str, user: str, tier: str, timeout: int = 180
    ) -> str: ...


_FENCE = re.compile(r"\A\s*```(?:json)?\s*\n(.*?)\n?\s*```\s*\Z", re.DOTALL)


def extract_json(text: str) -> dict[str, Any]:
    """剥掉可能存在的 markdown 围栏后解析。claude CLI 实测常带围栏。

    语法合法但形状不对（数组、标量）同样算不合 schema —— 否则下游会拿到
    一个假装是 dict 的东西，在几层之外炸出无关的 TypeError。
    """
    match = _FENCE.match(text)
    if match is not None:
        text = match.group(1)
    parsed = json.loads(text.strip())
    if not isinstance(parsed, dict):
        raise ValueError(f"LLM 返回的是 {type(parsed).__name__}，不是 JSON 对象")
    return parsed


def _resolve_backend() -> Callable[..., str]:
    from jobstar.config import get_settings

    settings = get_settings()
    if settings.llm_backend == "claude_cli":
        from jobstar.llm import claude_cli

        return claude_cli.call
    from jobstar.llm import gateway

    return gateway.call


def call_json(*, system: str, user: str, tier: str) -> dict[str, Any]:
    """调用 LLM 并返回解析后的 JSON 对象。不合 schema 重试一次，再失败就抛。"""
    if tier not in VALID_TIERS:
        raise ValueError(f"tier 只能是 {VALID_TIERS} 之一，实际是 {tier!r}")
    backend = _resolve_backend()
    prompt = user
    last_error: Exception | None = None
    last_raw = ""
    for _ in range(2):
        last_raw = backend(system=system, user=prompt, tier=tier)
        try:
            return extract_json(last_raw)
        except ValueError as exc:  # JSONDecodeError 是 ValueError 的子类
            last_error = exc
            prompt = user + _RETRY_HINT
    raise LLMSchemaError(
        f"重试一次后仍不是合法 JSON。最后一次输出前 300 字：{last_raw[:300]!r}"
    ) from last_error
```

- [ ] **Step 4: 写 llm/claude_cli.py**

```python
"""后端 A：调本地 claude CLI 的 headless 模式，复用 Claude Code 的登录态。

不需要 ANTHROPIC_API_KEY。代价是每次往返约 4 秒，且 CLI 固定会带上自己的
工具定义（约 23K token）。批量跑几百个岗位时建议改用 gateway 后端。
"""

from __future__ import annotations

import json
import shutil
import subprocess

from jobstar.llm import LLMBackendError

_MODEL = {"fast": "sonnet", "strong": "opus"}

# 这些参数把 CLI 的默认上下文剥到最小：不加载全局 CLAUDE.md、skills、MCP、工具。
_TRIM_FLAGS = [
    "--exclude-dynamic-system-prompt-sections",
    "--setting-sources",
    "",
    "--strict-mcp-config",
    "--mcp-config",
    '{"mcpServers":{}}',
    "--allowed-tools",
    "",
]


def call(*, system: str, user: str, tier: str, timeout: int = 180) -> str:
    exe = shutil.which("claude")
    if exe is None:
        raise LLMBackendError(
            "claude CLI 不在 PATH 上。安装 Claude Code，或把 "
            "JOBSTAR_LLM_BACKEND 改成 gateway。"
        )
    cmd = [
        exe,
        "-p",
        "--output-format",
        "json",
        "--model",
        _MODEL[tier],
        "--system-prompt",
        system,
        *_TRIM_FLAGS,
    ]
    try:
        proc = subprocess.run(
            cmd, input=user, capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired as exc:
        raise LLMBackendError(f"claude CLI 超时（{timeout}s）") from exc
    if proc.returncode != 0:
        raise LLMBackendError(
            f"claude CLI 退出码 {proc.returncode}：{proc.stderr[:500]}"
        )
    try:
        envelope = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise LLMBackendError(
            f"claude CLI 输出不是 JSON 信封：{proc.stdout[:300]!r}"
        ) from exc
    if envelope.get("is_error"):
        raise LLMBackendError(f"claude CLI 报错：{str(envelope.get('result'))[:500]}")
    return envelope["result"]
```

- [ ] **Step 5: 写 llm/gateway.py**

```python
"""后端 B：OpenAI 兼容网关，直调 /chat/completions。"""

from __future__ import annotations

import httpx

from jobstar.llm import LLMBackendError


def call(*, system: str, user: str, tier: str, timeout: int = 180) -> str:
    from jobstar.config import get_settings

    settings = get_settings()
    if not settings.gateway_base_url:
        raise LLMBackendError("gateway 后端需要设置 JOBSTAR_GATEWAY_BASE_URL")
    model = (
        settings.gateway_model_fast
        if tier == "fast"
        else settings.gateway_model_strong
    )
    url = settings.gateway_base_url.rstrip("/") + "/chat/completions"
    try:
        resp = httpx.post(
            url,
            headers={
                "Authorization": f"Bearer {settings.gateway_api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "response_format": {"type": "json_object"},
            },
            timeout=timeout,
        )
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise LLMBackendError(f"网关请求失败：{exc}") from exc
    try:
        return resp.json()["choices"][0]["message"]["content"]
    except (KeyError, IndexError, ValueError) as exc:
        raise LLMBackendError(f"网关响应结构异常：{resp.text[:300]!r}") from exc
```

- [ ] **Step 6: 跑测试确认通过**

Run: `uv run pytest tests/test_llm.py -v`
Expected: 12 passed

> **实施后修正（评审发现）**：上面 Step 1 的测试块漏了「语法合法但形状不对」这一类。
> `extract_json` 原版只挡 `JSONDecodeError`，模型返回一个 JSON 数组或标量时会被当成
> dict 原样放行，绕开重试和 `LLMSchemaError`，等于架空了本任务要保证的 §7 约束。
> 已按上面 Step 3 的最终代码修正（`isinstance(parsed, dict)` 校验 + `except ValueError`），
> 并补了 4 个测试（数组、标量、非 dict 重试两次后抛、`_TRIM_FLAGS` 进 argv）。
> 修正后 `tests/test_llm.py` 共 15 个用例。

- [ ] **Step 7: 对真实 claude CLI 做一次冒烟（不进测试套件）**

```bash
uv run python -c "
from jobstar.llm import call_json
print(call_json(
    system='你是结构化抽取器。只输出 JSON 对象。',
    user='抽成 JSON，字段 city 和 salary_min：杭州的后端岗，30k 以上。',
    tier='fast',
))
"
```
Expected: 打印出类似 `{'city': '杭州', 'salary_min': 30000}` 的 dict。若报 `claude CLI 不在 PATH 上`，说明环境有问题，先解决再继续。

- [ ] **Step 8: 提交**

```bash
git add src/jobstar/llm tests/test_llm.py
git commit -m "feat: LLM 双后端（本地 claude CLI 与 OpenAI 兼容网关）"
```

---

## Task 3: 能力卡片库

**Files:**
- Create: `src/jobstar/evidence.py`
- Create: `data/capability_cards.yaml`（由 agent 读 MASTER.md 产出初稿）
- Test: `tests/test_evidence.py`, `tests/fixtures/cards_ok.yaml`, `tests/fixtures/cards_bad.yaml`

**Interfaces:**
- Consumes: `jobstar.models.CapabilityCard`、`jobstar.models.Strength`（Task 1）
- Produces:
  - `jobstar.evidence.load_cards(path: Path) -> tuple[CapabilityCard, ...]`
  - `jobstar.evidence.CardValidationError`
  - `jobstar.evidence.FullDumpStore` — 实现 `retrieve(jd) -> tuple[CapabilityCard, ...]`，第一版忽略 jd 返回全部
  - `jobstar.evidence.cards_to_prompt_block(cards) -> str` — 打分器注入用的紧凑文本

- [ ] **Step 1: 写 fixture**

写 `tests/fixtures/cards_ok.yaml`：

```yaml
- id: cap-rag-telecom
  能力: RAG / 文档智能问答
  同义表述: [检索增强, 知识库问答, 文档智能, 向量检索]
  证据强度: 强
  项目: 电信扫描件 OCR + RAG
  可量化: [准确率, 文档量, 演示层级]
  可讲深度: OCR 预处理、分块策略、召回优化、领导演示答疑
  关联简历版本: [后端AI版, 财务AI专家版]

- id: cap-k8s
  能力: Kubernetes 编排
  同义表述: [k8s, 容器编排]
  证据强度: 弱
  项目: 内部工具部署
  可量化: []
  可讲深度: 跟着文档跑通过 deployment
  关联简历版本: []
```

写 `tests/fixtures/cards_bad.yaml`：

```yaml
- id: cap-x
  能力: 某能力
  同义表述: []
  证据强度: 很强
  项目: 某项目
  可量化: []
  可讲深度: ""
  关联简历版本: []
```

- [ ] **Step 2: 写失败测试**

写 `tests/test_evidence.py`：

```python
from pathlib import Path

import pytest

from jobstar.evidence import (
    CardValidationError,
    FullDumpStore,
    cards_to_prompt_block,
    load_cards,
)
from jobstar.models import Strength

FIXTURES = Path(__file__).parent / "fixtures"


def test_load_cards_maps_chinese_keys_to_ascii_fields():
    cards = load_cards(FIXTURES / "cards_ok.yaml")
    assert len(cards) == 2
    first = cards[0]
    assert first.id == "cap-rag-telecom"
    assert first.capability == "RAG / 文档智能问答"
    assert "向量检索" in first.synonyms
    assert first.strength is Strength.STRONG
    assert first.project == "电信扫描件 OCR + RAG"
    assert first.resume_versions == ("后端AI版", "财务AI专家版")


def test_load_cards_rejects_invalid_strength():
    with pytest.raises(CardValidationError, match="证据强度"):
        load_cards(FIXTURES / "cards_bad.yaml")


def test_load_cards_rejects_duplicate_ids(tmp_path):
    p = tmp_path / "dup.yaml"
    p.write_text(
        "- id: a\n  能力: x\n  同义表述: []\n  证据强度: 强\n  项目: p\n"
        "  可量化: []\n  可讲深度: d\n  关联简历版本: []\n"
        "- id: a\n  能力: y\n  同义表述: []\n  证据强度: 中\n  项目: p\n"
        "  可量化: []\n  可讲深度: d\n  关联简历版本: []\n",
        encoding="utf-8",
    )
    with pytest.raises(CardValidationError, match="重复"):
        load_cards(p)


def test_load_cards_rejects_missing_required_key(tmp_path):
    p = tmp_path / "missing.yaml"
    p.write_text("- id: a\n  能力: x\n", encoding="utf-8")
    with pytest.raises(CardValidationError, match="缺少"):
        load_cards(p)


def test_full_dump_store_ignores_jd_and_returns_all():
    cards = load_cards(FIXTURES / "cards_ok.yaml")
    store = FullDumpStore(cards)
    assert store.retrieve(None) == cards


def test_prompt_block_includes_id_strength_and_synonyms():
    cards = load_cards(FIXTURES / "cards_ok.yaml")
    block = cards_to_prompt_block(cards)
    assert "cap-rag-telecom" in block
    assert "强" in block
    assert "向量检索" in block


def test_prompt_block_stays_small():
    """全量注入的前提是总量小。超了就该重新评估检索方式（设计文档 §5.2）。"""
    cards = load_cards(FIXTURES / "cards_ok.yaml")
    assert len(cards_to_prompt_block(cards)) < 4000


def test_real_cards_file_is_loadable():
    """校对后的真实卡片库必须始终能加载。"""
    from jobstar.config import get_settings

    path = get_settings().cards_path
    if not path.exists():
        pytest.skip("卡片库尚未生成")
    cards = load_cards(path)
    assert len(cards) >= 20, "MASTER.md 至少能提炼出 20 张卡片"
    assert any(c.strength is Strength.STRONG for c in cards)
```

- [ ] **Step 3: 跑测试确认失败**

Run: `uv run pytest tests/test_evidence.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'jobstar.evidence'`

- [ ] **Step 4: 写 evidence.py**

```python
"""能力卡片库：把 MASTER.md 提炼出的 YAML 加载成结构化卡片。

YAML 用中文键是刻意的 —— 这份文件要由本人逐张校对「证据强度」列。
中文键到 ASCII 字段名的映射只存在于本文件的 _KEY_MAP 一处。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from jobstar.models import CapabilityCard, JobRequirements, Strength

_KEY_MAP = {
    "id": "id",
    "能力": "capability",
    "同义表述": "synonyms",
    "证据强度": "strength",
    "项目": "project",
    "可量化": "metrics",
    "可讲深度": "depth",
    "关联简历版本": "resume_versions",
}

_TUPLE_FIELDS = {"synonyms", "metrics", "resume_versions"}


class CardValidationError(ValueError):
    """卡片 YAML 结构不合法。宁可启动即失败，也不要带着坏卡片去打分。"""


def _build_card(raw: dict[str, Any], index: int) -> CapabilityCard:
    missing = [k for k in _KEY_MAP if k not in raw]
    if missing:
        raise CardValidationError(f"第 {index + 1} 张卡片缺少字段：{missing}")
    fields: dict[str, Any] = {}
    for cn_key, field in _KEY_MAP.items():
        value = raw[cn_key]
        if field in _TUPLE_FIELDS:
            fields[field] = tuple(value or ())
        elif field == "strength":
            try:
                fields[field] = Strength(str(value).strip())
            except ValueError as exc:
                allowed = [s.value for s in Strength]
                raise CardValidationError(
                    f"第 {index + 1} 张卡片的证据强度是 {value!r}，只能是 {allowed}"
                ) from exc
        else:
            fields[field] = str(value)
    return CapabilityCard(**fields)


def load_cards(path: Path) -> tuple[CapabilityCard, ...]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise CardValidationError(f"{path} 顶层必须是列表，实际是 {type(data).__name__}")
    cards = tuple(_build_card(raw, i) for i, raw in enumerate(data))
    seen: set[str] = set()
    for card in cards:
        if card.id in seen:
            raise CardValidationError(f"卡片 id 重复：{card.id}")
        seen.add(card.id)
    return cards


@dataclass(frozen=True)
class FullDumpStore:
    """第一版检索实现：忽略 jd，返回全部卡片。

    设计文档 §5.2：当前数据量下全量注入优于 top-k 召回。卡片数超过 200 张或
    摘要总量超过 30KB 时，换成 VectorStore，打分器无需改动。
    """

    cards: tuple[CapabilityCard, ...]

    def retrieve(self, jd: JobRequirements | None) -> tuple[CapabilityCard, ...]:
        return self.cards


def cards_to_prompt_block(cards: tuple[CapabilityCard, ...]) -> str:
    """渲染成注入 prompt 的紧凑文本。同义表述必须保留 —— 它是语义对齐的主力。"""
    lines: list[str] = []
    for card in cards:
        synonyms = "、".join(card.synonyms) if card.synonyms else "无"
        metrics = "、".join(card.metrics) if card.metrics else "无"
        lines.append(
            f"[{card.id}] {card.capability}｜证据强度:{card.strength.value}\n"
            f"  同义表述: {synonyms}\n"
            f"  项目: {card.project}\n"
            f"  可量化: {metrics}\n"
            f"  可讲深度: {card.depth}"
        )
    return "\n".join(lines)
```

- [ ] **Step 5: 跑测试确认通过**

Run: `uv run pytest tests/test_evidence.py -v`
Expected: 7 passed, 1 skipped（真实卡片库尚未生成）

- [ ] **Step 6: 生成卡片库初稿**

这一步由实施 agent 自己读文件完成，不写成运行时功能（一次性投入）。

1. 读 `<简历主库路径>` 全文（70KB），重点是：
   - 三、技能全集（全部条目）
   - 四、工作经历（岗位职责视角）
   - 五、项目经历（全部条目）
   - 十一、客户案例卡（全部条目）
   - **九、⚠️ 高危项与已废弃表述** —— 这一节里列为「已确认不实 / 已废弃」的表述**绝对不能**进卡片库
2. 按 §4.4 的格式产出 30-50 张卡片写入 `data/capability_cards.yaml`。规则：
   - 一张卡片 = 一个可被 JD 提问的能力点，不是一个项目（一个项目可以拆出多张卡）
   - `证据强度` 先按 MASTER.md 的 ⭐/○/⚠️ 标记给初值：⭐→强，○→中，⚠️→弱
   - `同义表述` 尽量写全，包括英文缩写、中文别名、上位词（这是语义对齐质量的主要杠杆）
   - `可量化` 只填 MASTER.md 第八节「数字与事实清单」里口径明确的数字名，不要编数字
3. 跑校验：

```bash
uv run python -c "
from jobstar.evidence import load_cards, cards_to_prompt_block
from jobstar.config import get_settings
cards = load_cards(get_settings().cards_path)
print(f'{len(cards)} 张卡片，prompt 块 {len(cards_to_prompt_block(cards))} 字符')
from collections import Counter
print(Counter(c.strength.value for c in cards))
"
```
Expected: 30-50 张，prompt 块在 3000-8000 字符之间

- [ ] **Step 7: 交给本人校对「证据强度」列**

停下来，告诉用户：卡片库初稿已生成在 `data/capability_cards.yaml`，共 N 张。**请逐张校对 `证据强度` 字段**（强=能讲 30 分钟 / 中=能讲 5 分钟 / 弱=仅接触），约需一小时。这是设计文档 §4.4 要求的一次性人工投入，也是本系统防止简历吹牛的唯一防线。校对完成后继续。

- [ ] **Step 8: 跑全部测试**

Run: `uv run pytest -v`
Expected: 全绿，`test_real_cards_file_is_loadable` 不再 skip

- [ ] **Step 9: 提交**

```bash
git add src/jobstar/evidence.py tests/test_evidence.py tests/fixtures data/capability_cards.yaml
git commit -m "feat: 能力卡片库加载、校验与全量检索实现"
```

---

## Task 4: 硬门禁

**Files:**
- Create: `src/jobstar/gate.py`
- Test: `tests/test_gate.py`

**Interfaces:**
- Consumes: `jobstar.models.JobRequirements`、`jobstar.models.GateResult`（Task 1）；`jobstar.config.get_setting(conn, "gate_rules")`（Task 1）
- Produces:
  - `jobstar.gate.check(req: JobRequirements, rules: dict, company: str = "") -> GateResult`
  - `jobstar.gate.DEGREE_ORDER: dict[str, int]`
  - `jobstar.gate.save_result(conn, job_id: str, result: GateResult) -> None`

- [ ] **Step 1: 写失败测试**

写 `tests/test_gate.py`：

```python
import pytest

from jobstar.config import SETTING_DEFAULTS
from jobstar.gate import check, save_result
from jobstar.db import get_conn, init_db
from jobstar.models import JobRequirements

RULES = SETTING_DEFAULTS["gate_rules"]


def make_req(**overrides) -> JobRequirements:
    base = dict(
        job_id="j1",
        city="杭州",
        degree="本科",
        years_min=3,
        years_max=5,
        salary_min=30,
        salary_max=50,
        skills=("Python",),
        industry="互联网",
        company_size="100-499人",
        category="后端开发",
    )
    base.update(overrides)
    return JobRequirements(**base)


def test_passes_a_matching_job():
    assert check(make_req(), RULES).passed is True


def test_rejects_city_outside_whitelist():
    result = check(make_req(city="北京"), RULES)
    assert result.passed is False
    assert "城市" in result.reject_reason


def test_rejects_salary_below_floor():
    """薪资上限低于门槛才算不合格 —— 20-24K 上限 24 < 25，拒。"""
    result = check(make_req(salary_min=20, salary_max=24), RULES)
    assert result.passed is False
    assert "薪资" in result.reject_reason


def test_accepts_salary_whose_ceiling_clears_floor():
    """18-35K 上限够得着，不拒 —— 谈薪空间留给人工判断。"""
    assert check(make_req(salary_min=18, salary_max=35), RULES).passed is True


def test_rejects_years_above_ceiling():
    result = check(make_req(years_min=12, years_max=None), RULES)
    assert result.passed is False
    assert "年限" in result.reject_reason


def test_rejects_blacklisted_company():
    rules = {**RULES, "company_blacklist": ["某黑名单公司"]}
    result = check(make_req(), rules, company="某黑名单公司杭州分部")
    assert result.passed is False
    assert "黑名单" in result.reject_reason


def test_degree_not_enforced_when_soft():
    """degree_hard 关着时，学历不参与判断。"""
    rules = {**RULES, "degree_hard": False}
    assert check(make_req(degree="博士"), rules).passed is True


def test_degree_enforced_when_hard():
    rules = {**RULES, "degree_hard": True, "my_degree": "硕士"}
    assert check(make_req(degree="本科"), rules).passed is True
    result = check(make_req(degree="博士"), rules)
    assert result.passed is False
    assert "学历" in result.reject_reason


def test_unknown_fields_do_not_reject():
    """归一化失败留下的 None 不应当被当成不合格 —— 宁可放过去让打分器看。"""
    req = make_req(city=None, salary_min=None, salary_max=None, years_min=None)
    assert check(req, RULES).passed is True


def test_reject_reason_is_none_when_passed():
    assert check(make_req(), RULES).reject_reason is None


def test_save_result_persists_and_is_idempotent(tmp_path):
    conn = get_conn(tmp_path / "t.db")
    init_db(conn)
    result = check(make_req(city="北京"), RULES)
    save_result(conn, "j1", result)
    save_result(conn, "j1", result)  # 重跑不应炸
    row = conn.execute("SELECT * FROM gate_results WHERE job_id='j1'").fetchone()
    assert row["passed"] == 0
    assert "城市" in row["reject_reason"]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_gate.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'jobstar.gate'`

- [ ] **Step 3: 写 gate.py**

```python
"""硬门禁：纯规则过滤，不调用 LLM。

设计目标是刷掉 60-80% 明显不合的岗位而不花 token。因此判断偏宽松：
字段为 None（归一化没抽出来）一律放过，交给打分器去看原文。
"""

from __future__ import annotations

import sqlite3
from typing import Any

from jobstar.models import GateResult, JobRequirements

DEGREE_ORDER: dict[str, int] = {
    "不限": 0,
    "中专": 1,
    "高中": 1,
    "大专": 2,
    "本科": 3,
    "硕士": 4,
    "博士": 5,
}


def check(req: JobRequirements, rules: dict[str, Any], company: str = "") -> GateResult:
    """返回是否通过 + 拒绝原因。原因要落库，便于事后检查门禁是否过严。"""
    whitelist = rules.get("city_whitelist") or []
    if whitelist and req.city and req.city not in whitelist:
        return GateResult(False, f"城市不在白名单：{req.city}")

    salary_floor = rules.get("salary_min")
    ceiling = req.salary_max if req.salary_max is not None else req.salary_min
    if salary_floor is not None and ceiling is not None and ceiling < salary_floor:
        return GateResult(
            False, f"薪资上限 {ceiling}K 低于门槛 {salary_floor}K"
        )

    years_max_rule = rules.get("years_max")
    if years_max_rule is not None and req.years_min is not None:
        if req.years_min > years_max_rule:
            return GateResult(
                False, f"要求年限下限 {req.years_min} 年高于上限设置 {years_max_rule} 年"
            )

    years_min_rule = rules.get("years_min")
    if years_min_rule is not None and req.years_max is not None:
        if req.years_max < years_min_rule:
            return GateResult(
                False, f"要求年限上限 {req.years_max} 年低于下限设置 {years_min_rule} 年"
            )

    if rules.get("degree_hard") and req.degree:
        mine = DEGREE_ORDER.get(rules.get("my_degree", "硕士"), 4)
        needed = DEGREE_ORDER.get(req.degree)
        if needed is not None and needed > mine:
            return GateResult(False, f"学历卡死且要求 {req.degree} 高于本人学历")

    for banned in rules.get("company_blacklist") or []:
        if banned and banned in company:
            return GateResult(False, f"公司在黑名单：{banned}")

    return GateResult(True, None)


def save_result(conn: sqlite3.Connection, job_id: str, result: GateResult) -> None:
    conn.execute(
        "INSERT INTO gate_results (job_id, passed, reject_reason) VALUES (?, ?, ?) "
        "ON CONFLICT(job_id) DO UPDATE SET "
        "passed = excluded.passed, "
        "reject_reason = excluded.reject_reason, "
        "checked_at = datetime('now')",
        (job_id, int(result.passed), result.reject_reason),
    )
    conn.commit()
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_gate.py -v`
Expected: 11 passed

- [ ] **Step 5: 提交**

```bash
git add src/jobstar/gate.py tests/test_gate.py
git commit -m "feat: 硬门禁纯规则过滤与拒绝原因留痕"
```

---

## Task 5: 归一化器

**Files:**
- Create: `src/jobstar/normalizer.py`
- Test: `tests/test_normalizer.py`

**Interfaces:**
- Consumes: `jobstar.llm.call_json`（Task 2）、`jobstar.models.JobRequirements`（Task 1）
- Produces:
  - `jobstar.normalizer.parse_salary_raw(text: str | None) -> tuple[int | None, int | None]`
  - `jobstar.normalizer.normalize(*, job_id, title, raw_jd, city_hint=None, salary_hint=None) -> JobRequirements`
  - `jobstar.normalizer.save_requirements(conn, req: JobRequirements) -> None`
  - `jobstar.normalizer.load_requirements(conn, job_id: str) -> JobRequirements | None`

- [ ] **Step 1: 写失败测试**

写 `tests/test_normalizer.py`：

```python
import pytest

from jobstar.db import get_conn, init_db
from jobstar.models import JobRequirements
from jobstar.normalizer import (
    load_requirements,
    normalize,
    parse_salary_raw,
    save_requirements,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("20-35K", (20, 35)),
        ("20-35K·13薪", (20, 35)),
        ("20-35K·16薪", (20, 35)),
        ("30K-50K", (30, 50)),
        ("15K", (15, 15)),
        ("面议", (None, None)),
        ("", (None, None)),
        (None, (None, None)),
        ("1.5-2万", (15, 20)),
        ("8-12万·年", (None, None)),
    ],
)
def test_parse_salary_raw(raw, expected):
    assert parse_salary_raw(raw) == expected


def _stub_llm(monkeypatch, payload):
    calls = []

    def fake(*, system, user, tier):
        calls.append({"system": system, "user": user, "tier": tier})
        return payload

    monkeypatch.setattr("jobstar.normalizer.call_json", fake)
    return calls


def test_normalize_builds_requirements(monkeypatch):
    calls = _stub_llm(
        monkeypatch,
        {
            "city": "杭州",
            "degree": "本科",
            "years_min": 3,
            "years_max": 5,
            "salary_min": 25,
            "salary_max": 40,
            "skills": ["Python", "FastAPI", "RAG"],
            "industry": "人工智能",
            "company_size": "100-499人",
            "category": "后端开发",
        },
    )
    req = normalize(job_id="j1", title="后端工程师", raw_jd="负责……")
    assert isinstance(req, JobRequirements)
    assert req.job_id == "j1"
    assert req.skills == ("Python", "FastAPI", "RAG")
    assert req.years_min == 3
    assert calls[0]["tier"] == "fast", "归一化是结构化抽取，用便宜档"


def test_normalize_prefers_hints_over_llm_guess(monkeypatch):
    """列表页已经给了城市和薪资，比让模型从 JD 里猜更可靠。"""
    _stub_llm(monkeypatch, {"city": "北京", "salary_min": 10, "salary_max": 15})
    req = normalize(
        job_id="j1",
        title="t",
        raw_jd="jd",
        city_hint="杭州",
        salary_hint="30-50K",
    )
    assert req.city == "杭州"
    assert (req.salary_min, req.salary_max) == (30, 50)


def test_normalize_tolerates_missing_fields(monkeypatch):
    _stub_llm(monkeypatch, {"city": "杭州"})
    req = normalize(job_id="j1", title="t", raw_jd="jd")
    assert req.degree is None
    assert req.skills == ()


def test_normalize_coerces_bad_types(monkeypatch):
    """模型偶尔把年限写成字符串或把 skills 写成字符串。"""
    _stub_llm(
        monkeypatch,
        {"years_min": "3", "years_max": "五", "skills": "Python、Go"},
    )
    req = normalize(job_id="j1", title="t", raw_jd="jd")
    assert req.years_min == 3
    assert req.years_max is None
    assert req.skills == ("Python、Go",)


def test_normalize_injects_jd_into_prompt(monkeypatch):
    calls = _stub_llm(monkeypatch, {})
    normalize(job_id="j1", title="高级后端", raw_jd="要求熟悉 LangGraph")
    assert "LangGraph" in calls[0]["user"]
    assert "高级后端" in calls[0]["user"]


def test_save_and_load_roundtrip(tmp_path, monkeypatch):
    conn = get_conn(tmp_path / "t.db")
    init_db(conn)
    _stub_llm(monkeypatch, {"city": "杭州", "skills": ["Go"], "salary_min": 30})
    req = normalize(job_id="j1", title="t", raw_jd="jd")
    save_requirements(conn, req)
    save_requirements(conn, req)  # 重跑不炸
    loaded = load_requirements(conn, "j1")
    assert loaded == req


def test_load_requirements_returns_none_when_absent(tmp_path):
    conn = get_conn(tmp_path / "t.db")
    init_db(conn)
    assert load_requirements(conn, "nope") is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_normalizer.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'jobstar.normalizer'`

- [ ] **Step 3: 写 normalizer.py**

```python
"""把 JD 自由文本抽成结构化的 JobRequirements。一次 LLM 调用，便宜档。"""

from __future__ import annotations

import json
import re
import sqlite3

from jobstar.llm import call_json
from jobstar.models import JobRequirements

SYSTEM = """你是招聘 JD 的结构化抽取器。读一段岗位描述，输出一个 JSON 对象。

字段与取值约定：
- city: 工作城市，只写城市名（如「杭州」），抽不到写 null
- degree: 学历要求，只能是 "不限"/"大专"/"本科"/"硕士"/"博士" 之一，抽不到写 null
- years_min / years_max: 经验年限的下限和上限，整数。「3-5年」→ 3 和 5；
  「3年以上」→ 3 和 null；「不限」→ 0 和 null；抽不到写 null
- salary_min / salary_max: 月薪，单位千元。「20-35K」→ 20 和 35。抽不到写 null
- skills: 技能要求列表，字符串数组。只写 JD 里明确提到的技术名词，不要推断
- industry: 公司所属行业，抽不到写 null
- company_size: 公司规模，抽不到写 null
- category: 岗位类别（如「后端开发」「解决方案架构师」），抽不到写 null

只输出 JSON 对象本身。不要 markdown 围栏，不要解释。抽不到的字段写 null，不要编。"""

# 第一个 K 是可选的：Boss 上「20-35K」和「30K-50K」两种写法都有
_SALARY_K = re.compile(r"(\d+(?:\.\d+)?)\s*[Kk千]?\s*[-~]\s*(\d+(?:\.\d+)?)\s*[Kk千]")
_SALARY_K_SINGLE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*[Kk千]")
_SALARY_W = re.compile(r"(\d+(?:\.\d+)?)\s*[-~]\s*(\d+(?:\.\d+)?)\s*万")


def parse_salary_raw(text: str | None) -> tuple[int | None, int | None]:
    """把 Boss 的薪资串解析成千元/月区间。解析不出来返回 (None, None)。

    年薪（「8-12万·年」）故意不解析 —— 换算口径不确定，交给 LLM 从 JD 里看。
    """
    if not text:
        return (None, None)
    if "年" in text:
        return (None, None)
    match = _SALARY_K.search(text)
    if match:
        return (int(float(match.group(1))), int(float(match.group(2))))
    match = _SALARY_W.search(text)
    if match:
        return (int(float(match.group(1)) * 10), int(float(match.group(2)) * 10))
    match = _SALARY_K_SINGLE.match(text)
    if match:
        value = int(float(match.group(1)))
        return (value, value)
    return (None, None)


def _as_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value.strip()))
        except ValueError:
            return None
    return None


def _as_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _as_tuple(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,) if value.strip() else ()
    if isinstance(value, (list, tuple)):
        return tuple(str(v).strip() for v in value if str(v).strip())
    return ()


def normalize(
    *,
    job_id: str,
    title: str,
    raw_jd: str,
    city_hint: str | None = None,
    salary_hint: str | None = None,
) -> JobRequirements:
    """抽取结构化需求。列表页给的 city_hint / salary_hint 优先于模型的推断。"""
    user = f"岗位名称：{title}\n\n岗位描述：\n{raw_jd}"
    data = call_json(system=SYSTEM, user=user, tier="fast")

    salary_min, salary_max = parse_salary_raw(salary_hint)
    if salary_min is None:
        salary_min = _as_int(data.get("salary_min"))
        salary_max = _as_int(data.get("salary_max"))

    return JobRequirements(
        job_id=job_id,
        city=_as_str(city_hint) or _as_str(data.get("city")),
        degree=_as_str(data.get("degree")),
        years_min=_as_int(data.get("years_min")),
        years_max=_as_int(data.get("years_max")),
        salary_min=salary_min,
        salary_max=salary_max,
        skills=_as_tuple(data.get("skills")),
        industry=_as_str(data.get("industry")),
        company_size=_as_str(data.get("company_size")),
        category=_as_str(data.get("category")),
    )


def save_requirements(conn: sqlite3.Connection, req: JobRequirements) -> None:
    conn.execute(
        "INSERT INTO requirements "
        "(job_id, city, degree, years_min, years_max, salary_min, salary_max, "
        " skills, industry, company_size, category) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(job_id) DO UPDATE SET "
        "city=excluded.city, degree=excluded.degree, "
        "years_min=excluded.years_min, years_max=excluded.years_max, "
        "salary_min=excluded.salary_min, salary_max=excluded.salary_max, "
        "skills=excluded.skills, industry=excluded.industry, "
        "company_size=excluded.company_size, category=excluded.category",
        (
            req.job_id,
            req.city,
            req.degree,
            req.years_min,
            req.years_max,
            req.salary_min,
            req.salary_max,
            json.dumps(list(req.skills), ensure_ascii=False),
            req.industry,
            req.company_size,
            req.category,
        ),
    )
    conn.commit()


def load_requirements(
    conn: sqlite3.Connection, job_id: str
) -> JobRequirements | None:
    row = conn.execute(
        "SELECT * FROM requirements WHERE job_id = ?", (job_id,)
    ).fetchone()
    if row is None:
        return None
    return JobRequirements(
        job_id=row["job_id"],
        city=row["city"],
        degree=row["degree"],
        years_min=row["years_min"],
        years_max=row["years_max"],
        salary_min=row["salary_min"],
        salary_max=row["salary_max"],
        skills=tuple(json.loads(row["skills"])),
        industry=row["industry"],
        company_size=row["company_size"],
        category=row["category"],
    )
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_normalizer.py -v`
Expected: 17 passed（10 个 parametrize + 7 个）

- [ ] **Step 5: 提交**

```bash
git add src/jobstar/normalizer.py tests/test_normalizer.py
git commit -m "feat: JD 归一化器与薪资串解析"
```

---

## Task 6: 打分器

**Files:**
- Create: `src/jobstar/scorer.py`
- Test: `tests/test_scorer.py`

**Interfaces:**
- Consumes: `jobstar.llm.call_json`（Task 2）、`jobstar.evidence.cards_to_prompt_block`（Task 3）、`jobstar.models`（Task 1）
- Produces:
  - `jobstar.scorer.SCORER_VERSION: str`
  - `jobstar.scorer.WEAK_EVIDENCE_CAP: float`
  - `jobstar.scorer.score(req, cards, weights, scorer_version=SCORER_VERSION) -> ScoreResult`
  - `jobstar.scorer.save_score(conn, result: ScoreResult) -> None`
  - `jobstar.scorer.save_failure(conn, job_id: str, error: str) -> None`
  - `jobstar.scorer.load_score(conn, job_id: str) -> ScoreResult | None`

- [ ] **Step 1: 写失败测试**

写 `tests/test_scorer.py`：

```python
import pytest

from jobstar.config import SETTING_DEFAULTS
from jobstar.db import get_conn, init_db
from jobstar.models import CapabilityCard, JobRequirements, Strength
from jobstar.scorer import (
    SCORER_VERSION,
    WEAK_EVIDENCE_CAP,
    load_score,
    save_failure,
    save_score,
    score,
)

WEIGHTS = SETTING_DEFAULTS["dimension_weights"]

CARDS = (
    CapabilityCard(
        id="cap-rag",
        capability="RAG",
        synonyms=("检索增强",),
        strength=Strength.STRONG,
        project="电信 OCR + RAG",
        metrics=("准确率",),
        depth="分块策略、召回优化",
        resume_versions=("后端AI版",),
    ),
    CapabilityCard(
        id="cap-k8s",
        capability="Kubernetes",
        synonyms=("k8s",),
        strength=Strength.WEAK,
        project="内部工具部署",
        metrics=(),
        depth="跑通过 deployment",
        resume_versions=(),
    ),
)

REQ = JobRequirements(
    job_id="j1",
    city="杭州",
    degree="本科",
    years_min=3,
    years_max=5,
    salary_min=30,
    salary_max=50,
    skills=("RAG", "Kubernetes"),
    industry="人工智能",
    company_size="100-499人",
    category="后端开发",
)


def _dims(**overrides):
    base = {
        name: {"score": 80, "card_ids": ["cap-rag"], "reason": "命中", "gap": "无"}
        for name in WEIGHTS
    }
    base.update(overrides)
    return {"dimensions": base}


def _stub(monkeypatch, payload):
    calls = []

    def fake(*, system, user, tier):
        calls.append({"system": system, "user": user, "tier": tier})
        return payload

    monkeypatch.setattr("jobstar.scorer.call_json", fake)
    return calls


def test_score_computes_weighted_total(monkeypatch):
    _stub(monkeypatch, _dims())
    result = score(REQ, CARDS, WEIGHTS)
    assert result.total == pytest.approx(80.0)
    assert result.scorer_version == SCORER_VERSION
    assert {d.name for d in result.dimensions} == set(WEIGHTS)


def test_score_uses_strong_tier(monkeypatch):
    calls = _stub(monkeypatch, _dims())
    score(REQ, CARDS, WEIGHTS)
    assert calls[0]["tier"] == "strong"


def test_dimension_without_evidence_is_zeroed(monkeypatch):
    """设计文档 §7：某维度无证据卡片则记 0 分。这条在代码里强制，不靠 prompt。"""
    _stub(
        monkeypatch,
        _dims(skills={"score": 95, "card_ids": [], "reason": "感觉很搭", "gap": ""}),
    )
    result = score(REQ, CARDS, WEIGHTS)
    skills = next(d for d in result.dimensions if d.name == "skills")
    assert skills.score == 0.0


def test_unknown_card_id_is_dropped_and_zeroes_dimension(monkeypatch):
    """模型编了一个不存在的卡片 id，等价于无证据。"""
    _stub(
        monkeypatch,
        _dims(
            industry={
                "score": 90,
                "card_ids": ["cap-does-not-exist"],
                "reason": "编的",
                "gap": "",
            }
        ),
    )
    result = score(REQ, CARDS, WEIGHTS)
    industry = next(d for d in result.dimensions if d.name == "industry")
    assert industry.card_ids == ()
    assert industry.score == 0.0


def test_weak_only_evidence_is_capped(monkeypatch):
    """弱证据卡片不得作为某维度的主要匹配依据，只能是加分项。"""
    _stub(
        monkeypatch,
        _dims(
            skills={"score": 95, "card_ids": ["cap-k8s"], "reason": "会 k8s", "gap": ""}
        ),
    )
    result = score(REQ, CARDS, WEIGHTS)
    skills = next(d for d in result.dimensions if d.name == "skills")
    assert skills.score == WEAK_EVIDENCE_CAP


def test_weak_plus_strong_evidence_is_not_capped(monkeypatch):
    _stub(
        monkeypatch,
        _dims(
            skills={
                "score": 95,
                "card_ids": ["cap-k8s", "cap-rag"],
                "reason": "两项都命中",
                "gap": "",
            }
        ),
    )
    result = score(REQ, CARDS, WEIGHTS)
    skills = next(d for d in result.dimensions if d.name == "skills")
    assert skills.score == 95.0


def test_missing_dimension_becomes_zero(monkeypatch):
    payload = _dims()
    del payload["dimensions"]["bonus"]
    _stub(monkeypatch, payload)
    result = score(REQ, CARDS, WEIGHTS)
    bonus = next(d for d in result.dimensions if d.name == "bonus")
    assert bonus.score == 0.0
    assert "缺失" in bonus.gap


def test_out_of_range_scores_are_clamped(monkeypatch):
    _stub(
        monkeypatch,
        _dims(
            skills={"score": 130, "card_ids": ["cap-rag"], "reason": "", "gap": ""},
            years={"score": -20, "card_ids": ["cap-rag"], "reason": "", "gap": ""},
        ),
    )
    result = score(REQ, CARDS, WEIGHTS)
    by_name = {d.name: d.score for d in result.dimensions}
    assert by_name["skills"] == 100.0
    assert by_name["years"] == 0.0


def test_prompt_contains_cards_and_jd(monkeypatch):
    calls = _stub(monkeypatch, _dims())
    score(REQ, CARDS, WEIGHTS)
    user = calls[0]["user"]
    assert "cap-rag" in user
    assert "检索增强" in user, "同义表述必须进 prompt —— 它是语义对齐的主力"
    assert "后端开发" in user


def test_prompt_states_the_evidence_rule(monkeypatch):
    calls = _stub(monkeypatch, _dims())
    score(REQ, CARDS, WEIGHTS)
    assert "证据" in calls[0]["system"]
    assert "弱" in calls[0]["system"]


def test_save_and_load_roundtrip(tmp_path, monkeypatch):
    conn = get_conn(tmp_path / "t.db")
    init_db(conn)
    _stub(monkeypatch, _dims())
    result = score(REQ, CARDS, WEIGHTS)
    save_score(conn, result)
    save_score(conn, result)  # 重跑不炸
    loaded = load_score(conn, "j1")
    assert loaded == result


def test_save_failure_marks_job_scoring_failed(tmp_path):
    conn = get_conn(tmp_path / "t.db")
    init_db(conn)
    conn.execute(
        "INSERT INTO jobs (platform, job_id, title, company, raw_jd) "
        "VALUES ('boss', 'j1', 't', 'c', 'jd')"
    )
    conn.commit()
    save_failure(conn, "j1", "重试一次后仍不是合法 JSON")
    row = conn.execute("SELECT * FROM scores WHERE job_id='j1'").fetchone()
    assert row["total"] is None
    assert "JSON" in row["error"]
    job = conn.execute("SELECT status FROM jobs WHERE job_id='j1'").fetchone()
    assert job["status"] == "scoring_failed"


def test_load_score_ignores_failed_rows(tmp_path):
    conn = get_conn(tmp_path / "t.db")
    init_db(conn)
    save_failure(conn, "j1", "boom")
    assert load_score(conn, "j1") is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_scorer.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'jobstar.scorer'`

- [ ] **Step 3: 写 scorer.py**

```python
"""维度打分器。纯函数：不写库、不碰浏览器，可对历史岗位批量回放。

两条约束在代码里强制执行，不依赖 prompt 遵守：
1. 某维度没有引用到真实存在的卡片 → 该维度记 0 分
2. 某维度只引用了「弱」证据卡片 → 该维度分数封顶 WEAK_EVIDENCE_CAP
"""

from __future__ import annotations

import json
import sqlite3

from jobstar.evidence import cards_to_prompt_block
from jobstar.llm import call_json
from jobstar.models import (
    DIMENSION_LABELS,
    DIMENSIONS,
    CapabilityCard,
    DimensionScore,
    JobRequirements,
    ScoreResult,
    Strength,
)

# 改 prompt 或改权重时手动 bump，用于区分不同版本产生的分数、支持回归对比
SCORER_VERSION = "v1"

WEAK_EVIDENCE_CAP = 40.0

SYSTEM = f"""你是岗位匹配评估器。给定一个岗位的结构化需求和候选人的能力卡片库，
对每个维度打分并给出证据。

维度（必须全部给出，一个都不能少）：
{chr(10).join(f"- {name}: {label}" for name, label in DIMENSION_LABELS.items())}

每个维度输出四个字段：
- score: 0-100 的整数
- card_ids: 支撑这个分数的能力卡片 id 数组。**必须是卡片库里真实存在的 id。**
  找不到能支撑的卡片就给空数组，不要编 id，不要硬凑。
- reason: 命中理由，一两句话，引用卡片里的具体内容
- gap: 缺口说明。JD 要求了但卡片库里没有的东西。没缺口写「无」

打分纪律：
- 每个维度的分数必须有卡片作为证据。没有证据的维度会被判 0 分。
- 「证据强度」是候选人自评的可讲深度。**强=能讲 30 分钟，中=能讲 5 分钟，弱=仅接触。**
  只有「弱」证据支撑的维度不算真匹配，分数会被封顶 {int(WEAK_EVIDENCE_CAP)} 分。
  所以如果某维度只能靠弱卡片撑，如实给低分并在 gap 里写清楚。
- JD 措辞和卡片措辞不一样是常态。先看卡片的「同义表述」再判断是否命中。

输出格式（只输出这个 JSON 对象本身，不要 markdown 围栏，不要解释）：
{{"dimensions": {{"skills": {{"score": 0, "card_ids": [], "reason": "", "gap": ""}}, ...}}}}"""


def _render_requirements(req: JobRequirements) -> str:
    skills = "、".join(req.skills) if req.skills else "未提取到"
    years = (
        f"{req.years_min}-{req.years_max}"
        if req.years_min is not None and req.years_max is not None
        else (f"{req.years_min}年以上" if req.years_min is not None else "不限")
    )
    salary = (
        f"{req.salary_min}-{req.salary_max}K"
        if req.salary_min is not None
        else "未提取到"
    )
    return (
        f"岗位类别: {req.category or '未提取到'}\n"
        f"行业: {req.industry or '未提取到'}\n"
        f"城市: {req.city or '未提取到'}\n"
        f"学历要求: {req.degree or '未提取到'}\n"
        f"经验年限: {years}\n"
        f"薪资: {salary}\n"
        f"公司规模: {req.company_size or '未提取到'}\n"
        f"技能要求: {skills}"
    )


def _clean_dimension(
    name: str, raw: object, card_index: dict[str, CapabilityCard]
) -> DimensionScore:
    if not isinstance(raw, dict):
        return DimensionScore(name, 0.0, (), "", "模型未返回该维度（缺失）")

    ids = raw.get("card_ids") or []
    if isinstance(ids, str):
        ids = [ids]
    valid = tuple(str(i) for i in ids if str(i) in card_index)

    reason = str(raw.get("reason") or "")
    gap = str(raw.get("gap") or "")

    try:
        value = float(raw.get("score") or 0)
    except (TypeError, ValueError):
        value = 0.0
    value = max(0.0, min(100.0, value))

    if not valid:
        return DimensionScore(name, 0.0, (), reason, gap or "无证据卡片支撑")

    if all(card_index[i].strength is Strength.WEAK for i in valid):
        value = min(value, WEAK_EVIDENCE_CAP)

    return DimensionScore(name, value, valid, reason, gap)


def score(
    req: JobRequirements,
    cards: tuple[CapabilityCard, ...],
    weights: dict[str, float],
    scorer_version: str = SCORER_VERSION,
) -> ScoreResult:
    user = (
        "## 岗位需求\n"
        f"{_render_requirements(req)}\n\n"
        "## 候选人能力卡片库\n"
        f"{cards_to_prompt_block(cards)}"
    )
    data = call_json(system=SYSTEM, user=user, tier="strong")
    raw_dims = data.get("dimensions") or {}

    card_index = {c.id: c for c in cards}
    dimensions = tuple(
        _clean_dimension(name, raw_dims.get(name), card_index) for name in DIMENSIONS
    )

    weight_sum = sum(weights.get(d.name, 0.0) for d in dimensions)
    if weight_sum <= 0:
        total = 0.0
    else:
        total = (
            sum(d.score * weights.get(d.name, 0.0) for d in dimensions) / weight_sum
        )

    return ScoreResult(
        job_id=req.job_id,
        total=round(total, 2),
        dimensions=dimensions,
        scorer_version=scorer_version,
    )


def save_score(conn: sqlite3.Connection, result: ScoreResult) -> None:
    payload = [
        {
            "name": d.name,
            "score": d.score,
            "card_ids": list(d.card_ids),
            "reason": d.reason,
            "gap": d.gap,
        }
        for d in result.dimensions
    ]
    conn.execute(
        "INSERT INTO scores (job_id, total, dimensions, scorer_version, error) "
        "VALUES (?, ?, ?, ?, NULL) "
        "ON CONFLICT(job_id) DO UPDATE SET "
        "total=excluded.total, dimensions=excluded.dimensions, "
        "scorer_version=excluded.scorer_version, error=NULL, "
        "scored_at=datetime('now')",
        (
            result.job_id,
            result.total,
            json.dumps(payload, ensure_ascii=False),
            result.scorer_version,
        ),
    )
    conn.execute("UPDATE jobs SET status='scored' WHERE job_id=?", (result.job_id,))
    conn.commit()


def save_failure(conn: sqlite3.Connection, job_id: str, error: str) -> None:
    """设计文档 §7：LLM 返回不合 schema 重试仍失败 → 标记 scoring_failed，不猜分数。"""
    conn.execute(
        "INSERT INTO scores (job_id, total, dimensions, scorer_version, error) "
        "VALUES (?, NULL, '[]', ?, ?) "
        "ON CONFLICT(job_id) DO UPDATE SET "
        "total=NULL, error=excluded.error, scored_at=datetime('now')",
        (job_id, SCORER_VERSION, error),
    )
    conn.execute(
        "UPDATE jobs SET status='scoring_failed' WHERE job_id=?", (job_id,)
    )
    conn.commit()


def load_score(conn: sqlite3.Connection, job_id: str) -> ScoreResult | None:
    row = conn.execute(
        "SELECT * FROM scores WHERE job_id = ? AND total IS NOT NULL", (job_id,)
    ).fetchone()
    if row is None:
        return None
    dimensions = tuple(
        DimensionScore(
            name=d["name"],
            score=d["score"],
            card_ids=tuple(d["card_ids"]),
            reason=d["reason"],
            gap=d["gap"],
        )
        for d in json.loads(row["dimensions"])
    )
    return ScoreResult(
        job_id=row["job_id"],
        total=row["total"],
        dimensions=dimensions,
        scorer_version=row["scorer_version"],
    )
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_scorer.py -v`
Expected: 13 passed

- [ ] **Step 5: 提交**

```bash
git add src/jobstar/scorer.py tests/test_scorer.py
git commit -m "feat: 维度打分器，证据约束与弱证据封顶在代码层强制"
```

---

## Task 7: 话术生成器

**Files:**
- Create: `src/jobstar/pitch.py`
- Test: `tests/test_pitch.py`

**Interfaces:**
- Consumes: `jobstar.llm.call_json`（Task 2）、`jobstar.models.ScoreResult` / `CapabilityCard` / `JobRequirements`（Task 1）
- Produces:
  - `jobstar.pitch.MAX_CHARS: int`
  - `jobstar.pitch.write_pitch(*, req, result, cards, title, company) -> str`

- [ ] **Step 1: 写失败测试**

写 `tests/test_pitch.py`：

```python
import pytest

from jobstar.models import (
    CapabilityCard,
    DimensionScore,
    JobRequirements,
    ScoreResult,
    Strength,
)
from jobstar.pitch import MAX_CHARS, write_pitch

CARDS = (
    CapabilityCard(
        id="cap-rag",
        capability="RAG",
        synonyms=("检索增强",),
        strength=Strength.STRONG,
        project="电信 OCR + RAG",
        metrics=("准确率",),
        depth="分块策略、召回优化",
        resume_versions=("后端AI版",),
    ),
    CapabilityCard(
        id="cap-gw",
        capability="大模型网关",
        synonyms=("LLM Gateway",),
        strength=Strength.STRONG,
        project="大模型聚合平台",
        metrics=("日均 token",),
        depth="多供应商聚合、限流、计费",
        resume_versions=("后端AI版",),
    ),
)

REQ = JobRequirements(
    job_id="j1",
    city="杭州",
    degree="本科",
    years_min=3,
    years_max=5,
    salary_min=30,
    salary_max=50,
    skills=("RAG", "LangGraph"),
    industry="人工智能",
    company_size="100-499人",
    category="后端开发",
)

RESULT = ScoreResult(
    job_id="j1",
    total=82.0,
    dimensions=(
        DimensionScore("skills", 90, ("cap-rag",), "RAG 命中", "缺 LangGraph"),
        DimensionScore("industry", 85, ("cap-gw",), "同为 AI 平台", "无"),
        DimensionScore("duties", 80, ("cap-gw",), "职责重合", "无"),
        DimensionScore("years", 70, ("cap-rag",), "年限够", "无"),
        DimensionScore("bonus", 0, (), "", "无证据卡片支撑"),
    ),
    scorer_version="v1",
)


def _stub(monkeypatch, text):
    calls = []

    def fake(*, system, user, tier):
        calls.append({"system": system, "user": user, "tier": tier})
        return {"greeting": text}

    monkeypatch.setattr("jobstar.pitch.call_json", fake)
    return calls


def test_returns_greeting_text(monkeypatch):
    _stub(monkeypatch, "您好，看到贵司在做 RAG 检索问答……")
    out = write_pitch(req=REQ, result=RESULT, cards=CARDS, title="AI 后端", company="某司")
    assert out.startswith("您好")


def test_uses_strong_tier(monkeypatch):
    calls = _stub(monkeypatch, "文案")
    write_pitch(req=REQ, result=RESULT, cards=CARDS, title="AI 后端", company="某司")
    assert calls[0]["tier"] == "strong"


def test_prompt_only_includes_cards_that_were_actually_cited(monkeypatch):
    """话术只能拿打分时命中的卡片说事，不能翻出没命中的去吹。"""
    calls = _stub(monkeypatch, "文案")
    write_pitch(req=REQ, result=RESULT, cards=CARDS, title="AI 后端", company="某司")
    assert "cap-rag" in calls[0]["user"]
    assert "cap-gw" in calls[0]["user"]


def test_prompt_excludes_uncited_cards(monkeypatch):
    calls = _stub(monkeypatch, "文案")
    result = ScoreResult(
        job_id="j1",
        total=50,
        dimensions=(DimensionScore("skills", 80, ("cap-rag",), "r", "g"),),
        scorer_version="v1",
    )
    write_pitch(req=REQ, result=result, cards=CARDS, title="AI 后端", company="某司")
    assert "cap-rag" in calls[0]["user"]
    assert "cap-gw" not in calls[0]["user"]


def test_prompt_carries_jd_keywords_as_hooks(monkeypatch):
    """设计文档 §4.6：要引用 JD 中的具体关键词作为钩子。"""
    calls = _stub(monkeypatch, "文案")
    write_pitch(req=REQ, result=RESULT, cards=CARDS, title="AI 后端", company="某司")
    user = calls[0]["user"]
    assert "LangGraph" in user
    assert "某司" in user


def test_system_prompt_forbids_template_openings(monkeypatch):
    """前 20 字相似度是风控信号之一。"""
    calls = _stub(monkeypatch, "文案")
    write_pitch(req=REQ, result=RESULT, cards=CARDS, title="AI 后端", company="某司")
    assert "开头" in calls[0]["system"]


def test_truncates_overlong_output(monkeypatch):
    _stub(monkeypatch, "啊" * (MAX_CHARS + 200))
    out = write_pitch(req=REQ, result=RESULT, cards=CARDS, title="t", company="c")
    assert len(out) <= MAX_CHARS


def test_strips_surrounding_quotes(monkeypatch):
    _stub(monkeypatch, '"您好，我对这个岗位很感兴趣"')
    out = write_pitch(req=REQ, result=RESULT, cards=CARDS, title="t", company="c")
    assert not out.startswith('"')


def test_raises_when_model_returns_empty(monkeypatch):
    _stub(monkeypatch, "   ")
    with pytest.raises(ValueError, match="空"):
        write_pitch(req=REQ, result=RESULT, cards=CARDS, title="t", company="c")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_pitch.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'jobstar.pitch'`

- [ ] **Step 3: 写 pitch.py**

```python
"""打招呼话术草稿。只拿打分时真正命中的卡片说事。"""

from __future__ import annotations

from jobstar.llm import call_json
from jobstar.models import CapabilityCard, JobRequirements, ScoreResult

# Boss 的打招呼输入框实际限制在 500 字以内，留出余量
MAX_CHARS = 400

SYSTEM = """你替一位求职者写 Boss 直聘的打招呼开场白。

硬性要求：
- 120-200 字，一段话，不分点，不用 markdown
- 必须引用 JD 里出现过的具体关键词作为钩子，让对方一眼看出你读过这条 JD
- 必须点到候选人「命中的能力卡片」里的具体项目和可量化结果
- 开头不要用模板句式（不要以「您好，我看到贵公司」「您好，我对这个岗位」这类
  高频开头起手），每条消息的前 20 个字都应当因岗位而异
- 不要写卡片库里没有的经历，不要编数字
- 不要写「期待回复」「盼复」这类空话结尾
- 语气平实专业，不谄媚，不用感叹号

输出格式（只输出这个 JSON 对象本身，不要 markdown 围栏）：
{"greeting": "……"}"""


def write_pitch(
    *,
    req: JobRequirements,
    result: ScoreResult,
    cards: tuple[CapabilityCard, ...],
    title: str,
    company: str,
) -> str:
    # 只收集「被记了分」的维度引用的卡片。打分器允许一个维度 card_ids 非空但
    # 分数为 0（模型自己给了 0 分），那等于没被采信，不能拿来当话术素材。
    cited: set[str] = set()
    for dim in result.dimensions:
        if dim.score > 0:
            cited.update(dim.card_ids)
    card_index = {c.id: c for c in cards}

    card_lines = [
        f"[{cid}] {card_index[cid].capability}｜项目: {card_index[cid].project}"
        f"｜可量化: {'、'.join(card_index[cid].metrics) or '无'}"
        f"｜可讲深度: {card_index[cid].depth}"
        for cid in sorted(cited)
        if cid in card_index
    ]
    hit_lines = [
        f"- {d.name}: {d.score} 分｜{d.reason}"
        for d in result.dimensions
        if d.score > 0 and d.reason
    ]

    user = (
        f"## 岗位\n公司：{company}\n职位：{title}\n"
        f"城市：{req.city or '未知'}｜行业：{req.industry or '未知'}\n"
        f"JD 技能关键词：{'、'.join(req.skills) or '无'}\n\n"
        f"## 命中的维度（总分 {result.total}）\n"
        + ("\n".join(hit_lines) or "无")
        + "\n\n## 可以拿来说事的能力卡片（只能用这些）\n"
        + ("\n".join(card_lines) or "无")
    )

    data = call_json(system=SYSTEM, user=user, tier="strong")
    text = str(data.get("greeting") or "").strip().strip('"').strip("“”").strip()
    if not text:
        raise ValueError("话术生成器返回空文本")
    return text[:MAX_CHARS]
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_pitch.py -v`
Expected: 9 passed

- [ ] **Step 5: 提交**

```bash
git add src/jobstar/pitch.py tests/test_pitch.py
git commit -m "feat: 打招呼话术生成器，只引用命中的能力卡片"
```

---

## Task 8: 待确认队列

**Files:**
- Create: `src/jobstar/actions.py`
- Test: `tests/test_actions.py`

**Interfaces:**
- Consumes: `jobstar.db`（Task 1）、`jobstar.config.get_setting`（Task 1）
- Produces:
  - `jobstar.actions.ActionStatus`（常量字符串：`PENDING` `APPROVED` `SKIPPED` `SENT` `FAILED`）
  - `jobstar.actions.InvalidTransition`
  - `jobstar.actions.enqueue(conn, *, type: str, job_id: str, payload: dict) -> int`
  - `jobstar.actions.approve(conn, action_id: int, payload_override: dict | None = None) -> None`
  - `jobstar.actions.skip(conn, action_id: int) -> None`
  - `jobstar.actions.mark_sent(conn, action_id: int) -> None`
  - `jobstar.actions.mark_failed(conn, action_id: int, error: str) -> None`
  - `jobstar.actions.list_by_status(conn, status: str) -> list[sqlite3.Row]`
  - `jobstar.actions.sent_today(conn) -> int`
  - `jobstar.actions.remaining_quota(conn) -> int`

- [ ] **Step 1: 写失败测试**

写 `tests/test_actions.py`：

```python
import pytest

from jobstar.actions import (
    APPROVED,
    FAILED,
    PENDING,
    SENT,
    SKIPPED,
    InvalidTransition,
    approve,
    enqueue,
    list_by_status,
    mark_failed,
    mark_sent,
    remaining_quota,
    sent_today,
    skip,
)
from jobstar.config import set_setting
from jobstar.db import get_conn, init_db


@pytest.fixture()
def conn(tmp_path):
    c = get_conn(tmp_path / "t.db")
    init_db(c)
    return c


def _new(conn, job_id="j1"):
    return enqueue(
        conn, type="send_greeting", job_id=job_id, payload={"greeting": "你好"}
    )


def test_enqueue_starts_pending(conn):
    action_id = _new(conn)
    row = conn.execute("SELECT * FROM actions WHERE id=?", (action_id,)).fetchone()
    assert row["status"] == PENDING
    assert row["decided_at"] is None


def test_enqueue_is_idempotent_per_job_and_type(conn):
    first = _new(conn)
    second = _new(conn)
    assert first == second
    assert len(list_by_status(conn, PENDING)) == 1


def test_approve_moves_to_approved_and_stamps_decided_at(conn):
    action_id = _new(conn)
    approve(conn, action_id)
    row = conn.execute("SELECT * FROM actions WHERE id=?", (action_id,)).fetchone()
    assert row["status"] == APPROVED
    assert row["decided_at"] is not None


def test_approve_can_rewrite_payload(conn):
    """面板上「改写后确认」走这条路径。"""
    action_id = _new(conn)
    approve(conn, action_id, {"greeting": "改写后的文案"})
    row = conn.execute("SELECT payload FROM actions WHERE id=?", (action_id,)).fetchone()
    assert "改写后的文案" in row["payload"]


def test_skip_moves_to_skipped(conn):
    action_id = _new(conn)
    skip(conn, action_id)
    row = conn.execute("SELECT status FROM actions WHERE id=?", (action_id,)).fetchone()
    assert row["status"] == SKIPPED


def test_pending_cannot_jump_to_sent(conn):
    """这是「绝不自动发送」的结构性保证：只有 approved 能变 sent。"""
    action_id = _new(conn)
    with pytest.raises(InvalidTransition):
        mark_sent(conn, action_id)


def test_approved_can_be_sent(conn):
    action_id = _new(conn)
    approve(conn, action_id)
    mark_sent(conn, action_id)
    row = conn.execute("SELECT * FROM actions WHERE id=?", (action_id,)).fetchone()
    assert row["status"] == SENT
    assert row["sent_at"] is not None


def test_approved_can_fail_with_reason(conn):
    action_id = _new(conn)
    approve(conn, action_id)
    mark_failed(conn, action_id, "找不到打招呼按钮")
    row = conn.execute("SELECT * FROM actions WHERE id=?", (action_id,)).fetchone()
    assert row["status"] == FAILED
    assert "打招呼按钮" in row["error"]


def test_failed_can_be_re_approved(conn):
    """失败不自动重试，但人工可以在面板上重新批准。"""
    action_id = _new(conn)
    approve(conn, action_id)
    mark_failed(conn, action_id, "boom")
    approve(conn, action_id)
    row = conn.execute("SELECT status FROM actions WHERE id=?", (action_id,)).fetchone()
    assert row["status"] == APPROVED


def test_sent_is_terminal(conn):
    action_id = _new(conn)
    approve(conn, action_id)
    mark_sent(conn, action_id)
    with pytest.raises(InvalidTransition):
        approve(conn, action_id)
    with pytest.raises(InvalidTransition):
        skip(conn, action_id)


def test_skipped_can_be_reopened(conn):
    action_id = _new(conn)
    skip(conn, action_id)
    approve(conn, action_id)
    row = conn.execute("SELECT status FROM actions WHERE id=?", (action_id,)).fetchone()
    assert row["status"] == APPROVED


def test_sent_today_counts_only_today(conn):
    for i in range(3):
        aid = _new(conn, job_id=f"j{i}")
        approve(conn, aid)
        mark_sent(conn, aid)
    conn.execute("UPDATE actions SET sent_at='2020-01-01 00:00:00' WHERE job_id='j0'")
    conn.commit()
    assert sent_today(conn) == 2


def test_remaining_quota_respects_setting(conn):
    set_setting(conn, "daily_greeting_limit", 2)
    assert remaining_quota(conn) == 2
    aid = _new(conn)
    approve(conn, aid)
    mark_sent(conn, aid)
    assert remaining_quota(conn) == 1


def test_remaining_quota_never_negative(conn):
    set_setting(conn, "daily_greeting_limit", 1)
    for i in range(3):
        aid = _new(conn, job_id=f"j{i}")
        approve(conn, aid)
        mark_sent(conn, aid)
    assert remaining_quota(conn) == 0


def test_unknown_action_id_raises(conn):
    with pytest.raises(InvalidTransition, match="不存在"):
        approve(conn, 9999)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_actions.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'jobstar.actions'`

- [ ] **Step 3: 写 actions.py**

```python
"""待确认队列。状态机是「绝不自动发送」的结构性保证。

pending → approved → sent
pending → skipped
approved → failed

只有 approved 能变 sent，而 approved 只由面板上的人工点击写入。
"""

from __future__ import annotations

import json
import sqlite3

PENDING = "pending"
APPROVED = "approved"
SKIPPED = "skipped"
SENT = "sent"
FAILED = "failed"

# 目标状态 -> 允许的来源状态
_ALLOWED_FROM: dict[str, frozenset[str]] = {
    APPROVED: frozenset({PENDING, SKIPPED, FAILED}),
    SKIPPED: frozenset({PENDING, APPROVED}),
    SENT: frozenset({APPROVED}),
    FAILED: frozenset({APPROVED}),
}


class InvalidTransition(RuntimeError):
    """状态机不允许的迁移，或动作不存在。"""


def _current_status(conn: sqlite3.Connection, action_id: int) -> str:
    row = conn.execute(
        "SELECT status FROM actions WHERE id = ?", (action_id,)
    ).fetchone()
    if row is None:
        raise InvalidTransition(f"动作 {action_id} 不存在")
    return row["status"]


def _require(conn: sqlite3.Connection, action_id: int, target: str) -> None:
    current = _current_status(conn, action_id)
    if current not in _ALLOWED_FROM[target]:
        raise InvalidTransition(f"动作 {action_id} 不能从 {current} 变成 {target}")


def enqueue(
    conn: sqlite3.Connection, *, type: str, job_id: str, payload: dict
) -> int:
    """入队一个待确认动作。同一 (type, job_id) 重复入队返回已有行的 id。"""
    blob = json.dumps(payload, ensure_ascii=False)
    conn.execute(
        "INSERT INTO actions (type, job_id, payload) VALUES (?, ?, ?) "
        "ON CONFLICT(type, job_id) DO NOTHING",
        (type, job_id, blob),
    )
    conn.commit()
    row = conn.execute(
        "SELECT id FROM actions WHERE type = ? AND job_id = ?", (type, job_id)
    ).fetchone()
    return int(row["id"])


def approve(
    conn: sqlite3.Connection, action_id: int, payload_override: dict | None = None
) -> None:
    _require(conn, action_id, APPROVED)
    if payload_override is not None:
        conn.execute(
            "UPDATE actions SET status=?, decided_at=datetime('now'), "
            "payload=?, error=NULL WHERE id=?",
            (APPROVED, json.dumps(payload_override, ensure_ascii=False), action_id),
        )
    else:
        conn.execute(
            "UPDATE actions SET status=?, decided_at=datetime('now'), error=NULL "
            "WHERE id=?",
            (APPROVED, action_id),
        )
    conn.commit()


def skip(conn: sqlite3.Connection, action_id: int) -> None:
    _require(conn, action_id, SKIPPED)
    conn.execute(
        "UPDATE actions SET status=?, decided_at=datetime('now') WHERE id=?",
        (SKIPPED, action_id),
    )
    conn.commit()


def mark_sent(conn: sqlite3.Connection, action_id: int) -> None:
    _require(conn, action_id, SENT)
    conn.execute(
        "UPDATE actions SET status=?, sent_at=datetime('now') WHERE id=?",
        (SENT, action_id),
    )
    conn.commit()


def mark_failed(conn: sqlite3.Connection, action_id: int, error: str) -> None:
    """设计文档 §4.8：失败不自动重试，写原因等人工决定。"""
    _require(conn, action_id, FAILED)
    conn.execute(
        "UPDATE actions SET status=?, error=? WHERE id=?", (FAILED, error, action_id)
    )
    conn.commit()


def list_by_status(conn: sqlite3.Connection, status: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM actions WHERE status = ? ORDER BY created_at", (status,)
    ).fetchall()


def sent_today(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM actions "
        "WHERE status = ? AND date(sent_at) = date('now', 'localtime')",
        (SENT,),
    ).fetchone()
    return int(row["n"])


def remaining_quota(conn: sqlite3.Connection) -> int:
    from jobstar.config import get_setting

    limit = int(get_setting(conn, "daily_greeting_limit"))
    return max(0, limit - sent_today(conn))
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_actions.py -v`
Expected: 15 passed

- [ ] **Step 5: 跑全套回归**

Run: `uv run pytest -v`
Expected: 全绿

- [ ] **Step 6: 提交**

```bash
git add src/jobstar/actions.py tests/test_actions.py
git commit -m "feat: 待确认队列状态机与每日配额"
```

---

## Task 9: 采集器

**Files:**
- Create: `src/jobstar/collector/__init__.py`, `src/jobstar/collector/parse.py`, `src/jobstar/collector/boss.py`
- Test: `tests/test_collector_parse.py`, `tests/fixtures/boss_list_raw.json`

**Interfaces:**
- Consumes: `jobstar.db`（Task 1）
- Produces:
  - `jobstar.collector.parse.extract_job_id(url: str) -> str | None`
  - `jobstar.collector.parse.clean_text(value: object) -> str`
  - `jobstar.collector.parse.normalize_list_item(raw: dict) -> dict | None`
  - `jobstar.collector.parse.dedup(items: list[dict]) -> list[dict]`
  - `jobstar.collector.boss.SELECTORS: dict[str, str]`
  - `jobstar.collector.boss.LoginRequired`
  - `jobstar.collector.boss.run_script(script: str, timeout: int = 180) -> str`
  - `jobstar.collector.boss.fetch_list(*, keyword, city_code, pages=1) -> list[dict]`
  - `jobstar.collector.boss.fetch_detail(url: str) -> dict`
  - `jobstar.collector.boss.save_jobs(conn, items: list[dict]) -> int`
  - `jobstar.collector.boss.save_detail(conn, job_id: str, detail: dict) -> None`

**设计取舍（先读这段再动手）：** 不写「HTML → dict」的 Python 解析器。抓取在浏览器里用 JS 直接取值，落地的是 JSON；`parse.py` 只做**纯后处理**（从 URL 抠 job_id、清空白、去重）。这样避免「线上用 JS 取值、测试用 BeautifulSoup 取值」两套路径长期漂移。DOM 选择器集中在 `boss.SELECTORS` 一个字典里，页面改版时改那一处。选择器本身靠 Step 1 的活体校验确认，靠 Step 8 的冒烟测试守住。

- [ ] **Step 1: 先去活体页面把选择器校准出来**

这一步**必须先做**，下面代码里的选择器是起点不是结论。

```bash
browser-harness <<'PY'
new_tab("https://www.zhipin.com/web/geek/jobs?query=后端开发&city=101210100")
wait_for_load()
print(page_info())
PY
```

若 `page_info()` 显示是登录页，停下来告诉用户：需要先在 Chrome 里扫码登录 Boss 直聘，登录后再继续（设计文档 §5.4 明确这条风险未验证）。

登录态正常后，把一张岗位卡片的结构打出来：

```bash
browser-harness <<'PY'
html = js("""
  const card = document.querySelector('.job-card-wrapper') || document.querySelector('li.job-card-box');
  return card ? card.outerHTML : '找不到岗位卡片';
""")
print(html[:3000])
PY
```

对照输出，把下面 `SELECTORS` 里的每个值改成真实存在的选择器。**改完再往下走。**

- [ ] **Step 2: 写 fixture**

把 Step 1 的 JS 提取结果存成 `tests/fixtures/boss_list_raw.json`（下面是格式示例，用真实抓到的内容替换，保留至少 3 条，其中 1 条含脏数据）：

```json
[
  {
    "url": "https://www.zhipin.com/job_detail/abc123def456~.html?lid=xyz&securityId=aaa",
    "title": "  高级后端开发工程师  ",
    "company": "某某科技有限公司",
    "city": "杭州·西湖区",
    "salary": "25-45K·14薪",
    "hr": "张女士 · HRBP",
    "tags": ["3-5年", "本科"]
  },
  {
    "url": "https://www.zhipin.com/job_detail/abc123def456~.html?lid=other",
    "title": "高级后端开发工程师",
    "company": "某某科技有限公司",
    "city": "杭州",
    "salary": "25-45K",
    "hr": "张女士",
    "tags": []
  },
  {
    "url": "/job_detail/zzz999~.html",
    "title": "AI 应用工程师",
    "company": "另一家公司",
    "city": "杭州·滨江区",
    "salary": "面议",
    "hr": "",
    "tags": ["经验不限"]
  },
  {
    "url": "",
    "title": "坏数据",
    "company": "",
    "city": "",
    "salary": "",
    "hr": "",
    "tags": []
  }
]
```

- [ ] **Step 3: 写失败测试**

写 `tests/test_collector_parse.py`：

```python
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
```

- [ ] **Step 4: 跑测试确认失败**

Run: `uv run pytest tests/test_collector_parse.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'jobstar.collector'`

- [ ] **Step 5: 写 collector/parse.py**

```python
"""抓取结果的纯后处理。没有 DOM 逻辑 —— 取值在浏览器里用 JS 做。"""

from __future__ import annotations

import re

BASE = "https://www.zhipin.com"

_JOB_ID = re.compile(r"/job_detail/([A-Za-z0-9_-]+)")
_WS = re.compile(r"\s+")


def extract_job_id(url: str | None) -> str | None:
    """从详情页 URL 抠出平台唯一 id。查询串每次刷新都变，必须丢掉。"""
    if not url:
        return None
    match = _JOB_ID.search(url)
    return match.group(1) if match else None


def clean_text(value: object) -> str:
    if value is None:
        return ""
    return _WS.sub(" ", str(value)).strip()


def normalize_list_item(raw: dict) -> dict | None:
    """把一条列表页原始记录整理成入库形状。不合格返回 None。"""
    url = clean_text(raw.get("url"))
    job_id = extract_job_id(url)
    title = clean_text(raw.get("title"))
    if not job_id or not title:
        return None
    if url.startswith("/"):
        url = BASE + url
    city = clean_text(raw.get("city")).split("·")[0]
    tags = [clean_text(t) for t in (raw.get("tags") or []) if clean_text(t)]
    return {
        "job_id": job_id,
        "url": url,
        "title": title,
        "company": clean_text(raw.get("company")),
        "city": city,
        "salary_raw": clean_text(raw.get("salary")),
        "hr_name": clean_text(raw.get("hr")),
        "tags": tags,
    }


def dedup(items: list[dict]) -> list[dict]:
    """按 job_id 去重，保留第一次出现的。"""
    seen: set[str] = set()
    out: list[dict] = []
    for item in items:
        job_id = item.get("job_id")
        if not job_id or job_id in seen:
            continue
        seen.add(job_id)
        out.append(item)
    return out
```

- [ ] **Step 6: 写 collector/boss.py**

把 Step 1 校准出来的选择器填进 `SELECTORS`。

```python
"""Boss 直聘采集器。通过 browser-harness 驱动本地 Chrome，复用已登录的会话。

两阶段抓取（设计文档 §4.1）：先抓列表页摘要，经硬门禁筛掉大部分后，
只对幸存岗位抓详情页。详情页请求量因此压到约 1/5 —— 既是性能优化也是风控措施。
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
from pathlib import Path

from jobstar.collector.parse import dedup, normalize_list_item

BASE = "https://www.zhipin.com"

# 页面改版时只改这里。这些值必须先用活体页面校准过（见实施计划 Task 9 Step 1）。
SELECTORS: dict[str, str] = {
    "card": ".job-card-wrapper, li.job-card-box",
    "link": "a.job-card-left, a[href*='/job_detail/']",
    "title": ".job-name",
    "company": ".company-name",
    "city": ".job-area",
    "salary": ".salary",
    "hr": ".info-public",
    "tag": ".tag-list li, .job-card-footer .tag-list li",
    "detail_jd": ".job-sec-text, .job-detail-section .text",
    "detail_company": ".sider-company, .company-info",
}

# 登录墙的特征串。命中就中止采集，面板顶部挂横幅（设计文档 §7）。
LOGIN_MARKERS = ("请先登录", "login", "/web/user/?ka=header-login")


class LoginRequired(RuntimeError):
    """Boss 登录态失效。采集中止，不静默失败。"""


class CollectError(RuntimeError):
    pass


def run_script(script: str, timeout: int = 180) -> str:
    """跑一段 browser-harness 脚本，返回 stdout。"""
    exe = shutil.which("browser-harness")
    if exe is None:
        raise CollectError("browser-harness 不在 PATH 上")
    proc = subprocess.run(
        [exe], input=script, capture_output=True, text=True, timeout=timeout
    )
    if proc.returncode != 0:
        raise CollectError(
            f"browser-harness 退出码 {proc.returncode}：{proc.stderr[:800]}"
        )
    return proc.stdout


def _extract_js() -> str:
    return f"""
  const cards = document.querySelectorAll({SELECTORS["card"]!r});
  const pick = (root, sel) => {{
    const el = root.querySelector(sel);
    return el ? el.innerText : '';
  }};
  return JSON.stringify(Array.from(cards).map(card => {{
    const link = card.querySelector({SELECTORS["link"]!r});
    return {{
      url: link ? link.getAttribute('href') : '',
      title: pick(card, {SELECTORS["title"]!r}),
      company: pick(card, {SELECTORS["company"]!r}),
      city: pick(card, {SELECTORS["city"]!r}),
      salary: pick(card, {SELECTORS["salary"]!r}),
      hr: pick(card, {SELECTORS["hr"]!r}),
      tags: Array.from(card.querySelectorAll({SELECTORS["tag"]!r})).map(t => t.innerText),
    }};
  }}));
"""


def _guard_login(page_text: str) -> None:
    lowered = page_text.lower()
    if any(marker.lower() in lowered for marker in LOGIN_MARKERS):
        raise LoginRequired(
            "Boss 登录态失效，采集已中止。请在 Chrome 里重新扫码登录后重跑。"
        )


def search_url(keyword: str, city_code: str, page: int = 1) -> str:
    from urllib.parse import quote

    return (
        f"{BASE}/web/geek/jobs?query={quote(keyword)}"
        f"&city={city_code}&page={page}"
    )


def fetch_list(*, keyword: str, city_code: str, pages: int = 1) -> list[dict]:
    """抓 pages 页搜索结果，返回去重后的列表页摘要。"""
    collected: list[dict] = []
    for page in range(1, pages + 1):
        url = search_url(keyword, city_code, page)
        script = (
            f"new_tab({url!r})\n"
            "wait_for_load()\n"
            "info = page_info()\n"
            "print('###PAGEINFO###' + str(info))\n"
            f"print('###DATA###' + js({_extract_js()!r}))\n"
        )
        out = run_script(script)
        head, _, data = out.partition("###DATA###")
        _guard_login(head)
        try:
            raw = json.loads(data.strip())
        except json.JSONDecodeError as exc:
            raise CollectError(f"第 {page} 页提取结果不是 JSON：{data[:300]!r}") from exc
        collected.extend(i for i in (normalize_list_item(r) for r in raw) if i)
    return dedup(collected)


def fetch_detail(url: str) -> dict:
    """抓单个岗位详情页的 JD 全文。"""
    detail_js = (
        f"const jd = document.querySelector({SELECTORS['detail_jd']!r});"
        f"const co = document.querySelector({SELECTORS['detail_company']!r});"
        "return JSON.stringify({"
        "  raw_jd: jd ? jd.innerText : '',"
        "  company_info: co ? co.innerText : '',"
        "});"
    )
    script = (
        f"goto_url({url!r})\n"
        "wait_for_load()\n"
        "print('###PAGEINFO###' + str(page_info()))\n"
        f"print('###DATA###' + js({detail_js!r}))\n"
    )
    out = run_script(script)
    head, _, data = out.partition("###DATA###")
    _guard_login(head)
    try:
        return json.loads(data.strip())
    except json.JSONDecodeError as exc:
        raise CollectError(f"详情页提取结果不是 JSON：{data[:300]!r}") from exc


def save_jobs(conn: sqlite3.Connection, items: list[dict]) -> int:
    """写入列表页摘要。已存在的 job_id 跳过（设计文档 §4.1 去重规则）。"""
    inserted = 0
    for item in items:
        cursor = conn.execute(
            "INSERT INTO jobs (platform, job_id, title, company, raw_jd, city, "
            " salary_raw, hr_name, url) "
            "VALUES ('boss', ?, ?, ?, '', ?, ?, ?, ?) "
            "ON CONFLICT(platform, job_id) DO NOTHING",
            (
                item["job_id"],
                item["title"],
                item["company"],
                item["city"],
                item["salary_raw"],
                item["hr_name"],
                item["url"],
            ),
        )
        inserted += cursor.rowcount or 0
    conn.commit()
    return inserted


def save_detail(conn: sqlite3.Connection, job_id: str, detail: dict) -> None:
    conn.execute(
        "UPDATE jobs SET raw_jd = ?, detail_fetched = 1 WHERE job_id = ?",
        (detail.get("raw_jd", ""), job_id),
    )
    conn.commit()


def dump_failure(job_id: str, payload: str, out_dir: Path) -> Path:
    """抓取失败时留原始片段，便于事后定位页面改版（设计文档 §7）。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{job_id}.txt"
    path.write_text(payload[:20000], encoding="utf-8")
    return path
```

- [ ] **Step 7: 跑测试确认通过**

Run: `uv run pytest tests/test_collector_parse.py -v`
Expected: 12 passed

- [ ] **Step 8: 活体冒烟（不进测试套件）**

```bash
uv run python -c "
from jobstar.collector.boss import fetch_list
items = fetch_list(keyword='后端开发', city_code='101210100', pages=1)
print(f'{len(items)} 条')
for i in items[:3]:
    print(i['job_id'], i['title'], i['company'], i['city'], i['salary_raw'])
"
```
Expected: 打印 15-30 条，`job_id` 非空、`city` 是「杭州」而不是「杭州·西湖区」。
若返回 0 条，回到 Step 1 重新校准 `SELECTORS`；若抛 `LoginRequired`，先去 Chrome 扫码。

抓完后关掉为本次任务开的标签页。

- [ ] **Step 9: 提交**

```bash
git add src/jobstar/collector tests/test_collector_parse.py tests/fixtures/boss_list_raw.json
git commit -m "feat: Boss 列表页与详情页采集器，两阶段抓取与登录态守卫"
```

---

## Task 10: 执行器

**Files:**
- Create: `src/jobstar/executor.py`
- Test: `tests/test_executor.py`

**Interfaces:**
- Consumes: `jobstar.actions`（Task 8）、`jobstar.collector.boss.run_script`（Task 9）、`jobstar.config.get_setting`（Task 1）
- Produces:
  - `jobstar.executor.ExecutionReport` dataclass：`sent: int`、`failed: int`、`quota_hit: bool`、`errors: list[str]`
  - `jobstar.executor.human_delay(rng) -> float`
  - `jobstar.executor.send_greeting(job_url: str, text: str) -> None`（真实浏览器动作）
  - `jobstar.executor.run_queue(conn, *, send_fn=send_greeting, sleep_fn=time.sleep, rng=None) -> ExecutionReport`

- [ ] **Step 1: 写失败测试**

写 `tests/test_executor.py`：

```python
import random
import statistics

import pytest

from jobstar.actions import APPROVED, FAILED, SENT, approve, enqueue
from jobstar.config import set_setting
from jobstar.db import get_conn, init_db
from jobstar.executor import ExecutionReport, human_delay, run_queue


@pytest.fixture()
def conn(tmp_path):
    c = get_conn(tmp_path / "t.db")
    init_db(c)
    return c


def _approved(conn, job_id, greeting="你好"):
    conn.execute(
        "INSERT INTO jobs (platform, job_id, title, company, raw_jd, url, hr_name) "
        "VALUES ('boss', ?, ?, 'c', 'jd', ?, '张女士')",
        (job_id, f"岗位{job_id}", f"https://www.zhipin.com/job_detail/{job_id}~.html"),
    )
    conn.commit()
    action_id = enqueue(
        conn, type="send_greeting", job_id=job_id, payload={"greeting": greeting}
    )
    approve(conn, action_id)
    return action_id


def test_sends_all_approved_actions(conn):
    for i in range(3):
        _approved(conn, f"j{i}")
    sent = []
    report = run_queue(
        conn,
        send_fn=lambda url, text: sent.append((url, text)),
        sleep_fn=lambda _: None,
    )
    assert isinstance(report, ExecutionReport)
    assert report.sent == 3
    assert len(sent) == 3
    rows = conn.execute("SELECT status FROM actions").fetchall()
    assert {r["status"] for r in rows} == {SENT}


def test_ignores_pending_actions(conn):
    """绝不自动发送：pending 的动作执行器根本看不见。"""
    conn.execute(
        "INSERT INTO jobs (platform, job_id, title, company, raw_jd) "
        "VALUES ('boss', 'p1', 't', 'c', 'jd')"
    )
    conn.commit()
    enqueue(conn, type="send_greeting", job_id="p1", payload={"greeting": "你好"})
    calls = []
    report = run_queue(
        conn, send_fn=lambda u, t: calls.append(u), sleep_fn=lambda _: None
    )
    assert report.sent == 0
    assert calls == []


def test_stops_at_daily_quota(conn):
    set_setting(conn, "daily_greeting_limit", 2)
    for i in range(5):
        _approved(conn, f"j{i}")
    report = run_queue(conn, send_fn=lambda u, t: None, sleep_fn=lambda _: None)
    assert report.sent == 2
    assert report.quota_hit is True
    left = conn.execute(
        "SELECT COUNT(*) AS n FROM actions WHERE status=?", (APPROVED,)
    ).fetchone()
    assert left["n"] == 3, "超额的动作留在队列里，不丢"


def test_failure_marks_failed_and_continues(conn):
    _approved(conn, "bad")
    _approved(conn, "good")

    def flaky(url, text):
        if "bad" in url:
            raise RuntimeError("找不到打招呼按钮")

    report = run_queue(conn, send_fn=flaky, sleep_fn=lambda _: None)
    assert report.sent == 1
    assert report.failed == 1
    assert any("打招呼按钮" in e for e in report.errors)
    row = conn.execute(
        "SELECT status, error FROM actions WHERE job_id='bad'"
    ).fetchone()
    assert row["status"] == FAILED
    assert "打招呼按钮" in row["error"]


def test_failure_is_not_auto_retried(conn):
    """设计文档 §4.8：失败不自动重试，人工在面板决定。"""
    _approved(conn, "bad")
    calls = []

    def always_fail(url, text):
        calls.append(url)
        raise RuntimeError("boom")

    run_queue(conn, send_fn=always_fail, sleep_fn=lambda _: None)
    assert len(calls) == 1


def test_writes_application_ledger(conn):
    _approved(conn, "j1", greeting="定制化的开场白")
    conn.execute(
        "INSERT INTO scores (job_id, total, dimensions, scorer_version) "
        "VALUES ('j1', 82.0, '[]', 'v1')"
    )
    conn.commit()
    run_queue(conn, send_fn=lambda u, t: None, sleep_fn=lambda _: None)
    row = conn.execute("SELECT * FROM applications WHERE job_id='j1'").fetchone()
    assert row is not None
    assert row["greeting_text"] == "定制化的开场白"
    assert row["hr_name"] == "张女士"
    assert "82" in row["score_snapshot"]


def test_sleeps_between_sends_but_not_before_first(conn):
    for i in range(3):
        _approved(conn, f"j{i}")
    delays = []
    run_queue(conn, send_fn=lambda u, t: None, sleep_fn=delays.append)
    assert len(delays) == 2, "3 次发送之间只有 2 个间隔"
    assert all(d > 0 for d in delays)


def test_human_delay_is_long_tailed_not_uniform():
    """设计文档 §5.4：延迟分布要模拟人类长尾，不是均匀间隔。"""
    rng = random.Random(42)
    samples = [human_delay(rng) for _ in range(2000)]
    assert min(samples) >= 5.0
    assert max(samples) <= 600.0
    median = statistics.median(samples)
    mean = statistics.mean(samples)
    assert mean > median * 1.15, "均值应显著高于中位数（右偏长尾）"


def test_human_delay_is_reproducible_with_seed():
    assert human_delay(random.Random(7)) == human_delay(random.Random(7))


def test_empty_queue_is_a_noop(conn):
    report = run_queue(conn, send_fn=lambda u, t: None, sleep_fn=lambda _: None)
    assert report == ExecutionReport(sent=0, failed=0, quota_hit=False, errors=[])
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_executor.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'jobstar.executor'`

- [ ] **Step 3: 写 executor.py**

```python
"""执行器：只消费 approved 状态的动作，不做任何判断。

与决策器彻底分离带来的性质：执行器可以独立调节流和风控，不受打分逻辑变更影响；
决策器可以离线回放历史岗位调权重，而不可能误发消息。
"""

from __future__ import annotations

import json
import math
import random
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Callable

from jobstar import actions
from jobstar.collector.boss import run_script

# 对数正态参数：中位数 e^3.6 ≈ 36 秒，长尾能拉到几分钟
_DELAY_MU = 3.6
_DELAY_SIGMA = 0.9
_DELAY_MIN = 5.0
_DELAY_MAX = 600.0


@dataclass
class ExecutionReport:
    sent: int = 0
    failed: int = 0
    quota_hit: bool = False
    errors: list[str] = field(default_factory=list)


def human_delay(rng: random.Random) -> float:
    """动作之间的随机延迟。对数正态而非均匀分布 —— 均匀间隔本身就是风控信号。"""
    value = math.exp(rng.normalvariate(_DELAY_MU, _DELAY_SIGMA))
    return max(_DELAY_MIN, min(_DELAY_MAX, value))


# 聊天框与发送按钮的选择器。与 collector.boss.SELECTORS 同源，页面改版时一起改。
# 单独提成常量是为了避免在 f-string 里嵌套三引号 —— 那会提前终止外层字符串。
_FOCUS_INPUT_JS = (
    "const box = document.querySelector("
    "'#chat-input, textarea.input-area, div[contenteditable=true]');"
    "if (!box) return 'no-input';"
    "box.focus();"
    "return 'ok';"
)

_CLICK_SEND_JS = (
    "const btn = document.querySelector('.btn-send, button[type=submit]');"
    "if (!btn) return 'no-send-button';"
    "btn.click();"
    "return 'ok';"
)


def send_greeting(job_url: str, text: str) -> None:
    """真实浏览器动作：打开岗位详情页，点「立即沟通」，填文案，发送。

    按钮用无障碍树按名字找而不是靠 CSS 类名 —— Boss 的类名比按钮文案更容易变。
    """
    payload = json.dumps({"url": job_url, "text": text}, ensure_ascii=False)
    script = "\n".join(
        [
            "import json",
            f"args = json.loads({payload!r})",
            'goto_url(args["url"])',
            "wait_for_load()",
            'nodes = cdp("Accessibility.getFullAXTree")["nodes"]',
            "target = None",
            "for n in nodes:",
            '    name = (n.get("name") or {}).get("value") or ""',
            '    role = (n.get("role") or {}).get("value") or ""',
            '    if role == "button" and ("立即沟通" in name or "继续沟通" in name):',
            '        target = n["backendDOMNodeId"]',
            "        break",
            "if target is None:",
            '    raise SystemExit("找不到「立即沟通」按钮")',
            'quad = cdp("DOM.getBoxModel", backendNodeId=target)["model"]["content"]',
            "click_at_xy(sum(quad[0::2]) / 4, sum(quad[1::2]) / 4)",
            "wait_for_load()",
            f"ok = js({_FOCUS_INPUT_JS!r})",
            'if ok != "ok":',
            '    raise SystemExit("找不到聊天输入框: " + str(ok))',
            'cdp("Input.insertText", text=args["text"])',
            f"sent = js({_CLICK_SEND_JS!r})",
            'if sent != "ok":',
            '    raise SystemExit("找不到发送按钮: " + str(sent))',
            'print("SENT_OK")',
        ]
    )
    out = run_script(script)
    if "SENT_OK" not in out:
        raise RuntimeError(f"发送未确认成功：{out[-500:]}")


def _record_application(
    conn: sqlite3.Connection, action_row: sqlite3.Row, greeting: str
) -> None:
    job = conn.execute(
        "SELECT hr_name FROM jobs WHERE job_id = ?", (action_row["job_id"],)
    ).fetchone()
    score = conn.execute(
        "SELECT total, dimensions, scorer_version FROM scores WHERE job_id = ?",
        (action_row["job_id"],),
    ).fetchone()
    snapshot = (
        {
            "total": score["total"],
            "dimensions": json.loads(score["dimensions"]),
            "scorer_version": score["scorer_version"],
        }
        if score is not None
        else {}
    )
    conn.execute(
        "INSERT INTO applications (job_id, action_id, hr_name, greeting_text, "
        " score_snapshot) VALUES (?, ?, ?, ?, ?)",
        (
            action_row["job_id"],
            action_row["id"],
            job["hr_name"] if job else None,
            greeting,
            json.dumps(snapshot, ensure_ascii=False),
        ),
    )
    conn.commit()


def run_queue(
    conn: sqlite3.Connection,
    *,
    send_fn: Callable[[str, str], None] = send_greeting,
    sleep_fn: Callable[[float], None] = time.sleep,
    rng: random.Random | None = None,
) -> ExecutionReport:
    rng = rng or random.Random()
    report = ExecutionReport()
    queue = actions.list_by_status(conn, actions.APPROVED)

    for index, row in enumerate(queue):
        if actions.remaining_quota(conn) <= 0:
            report.quota_hit = True
            break
        if index > 0:
            sleep_fn(human_delay(rng))

        payload = json.loads(row["payload"])
        greeting = payload.get("greeting", "")
        job = conn.execute(
            "SELECT url FROM jobs WHERE job_id = ?", (row["job_id"],)
        ).fetchone()
        url = (job["url"] if job else None) or ""

        try:
            send_fn(url, greeting)
        except Exception as exc:  # 任何失败都只留痕，不自动重试
            actions.mark_failed(conn, row["id"], str(exc))
            report.failed += 1
            report.errors.append(f"{row['job_id']}: {exc}")
            continue

        actions.mark_sent(conn, row["id"])
        _record_application(conn, row, greeting)
        report.sent += 1

    return report
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_executor.py -v`
Expected: 10 passed

若 `test_human_delay_is_long_tailed_not_uniform` 失败，调 `_DELAY_SIGMA`（增大右偏）而不是改断言。

- [ ] **Step 5: 提交**

```bash
git add src/jobstar/executor.py tests/test_executor.py
git commit -m "feat: 执行器，配额闸门、长尾延迟与失败留痕"
```

---

## Task 11: 管道编排与 CLI

**Files:**
- Create: `src/jobstar/pipeline.py`, `src/jobstar/cli.py`
- Test: `tests/test_pipeline.py`

**Interfaces:**
- Consumes: Task 3-10 的全部模块
- Produces:
  - `jobstar.pipeline.parse_tags(tags) -> dict`（从列表页标签抠年限和学历）
  - `jobstar.pipeline.prelim_requirements(row, tags: list[str] | None = None) -> JobRequirements`
  - `jobstar.pipeline.CollectReport` / `ScoreReport` dataclass
  - `jobstar.pipeline.run_collect(conn, *, keyword, city_code, pages=1, fetch_fn=None, detail_fn=None) -> CollectReport`
  - `jobstar.pipeline.run_score(conn, *, limit=None) -> ScoreReport`
  - `jobstar.pipeline.maybe_enqueue(conn, req, result, title, company) -> int | None`
  - `jobstar.cli.main(argv=None) -> int`

**为什么要「预门禁」：** 两阶段抓取的意义是**先筛后抓详情**。但硬门禁要 `JobRequirements`，而完整的 `JobRequirements` 要 JD 全文 —— 鸡生蛋。解法是从列表页已有的字段（城市、薪资串、`3-5年` / `本科` 这类标签）用纯正则拼一个**预备版** `JobRequirements` 跑第一遍门禁，幸存者才去抓详情页、才花 token 做完整归一化。

- [ ] **Step 1: 写失败测试**

写 `tests/test_pipeline.py`：

```python
import json

import pytest

from jobstar.actions import PENDING, list_by_status
from jobstar.config import set_setting
from jobstar.db import get_conn, init_db
from jobstar.models import DimensionScore, JobRequirements, ScoreResult
from jobstar.pipeline import (
    maybe_enqueue,
    parse_tags,
    prelim_requirements,
    run_collect,
    run_score,
)


@pytest.fixture()
def conn(tmp_path):
    c = get_conn(tmp_path / "t.db")
    init_db(c)
    return c


@pytest.mark.parametrize(
    "tags,expected",
    [
        (["3-5年", "本科"], {"years_min": 3, "years_max": 5, "degree": "本科"}),
        (["5年以上", "硕士"], {"years_min": 5, "years_max": None, "degree": "硕士"}),
        (["经验不限", "学历不限"], {"years_min": 0, "years_max": None, "degree": "不限"}),
        (["1年以内"], {"years_min": 0, "years_max": 1, "degree": None}),
        (["在校/应届"], {"years_min": 0, "years_max": 1, "degree": None}),
        ([], {"years_min": None, "years_max": None, "degree": None}),
    ],
)
def test_parse_tags(tags, expected):
    assert parse_tags(tags) == expected


def test_prelim_requirements_uses_list_page_fields_only(conn):
    conn.execute(
        "INSERT INTO jobs (platform, job_id, title, company, raw_jd, city, salary_raw) "
        "VALUES ('boss', 'j1', 't', 'c', '', '杭州', '25-45K·14薪')"
    )
    conn.commit()
    row = conn.execute("SELECT * FROM jobs WHERE job_id='j1'").fetchone()
    req = prelim_requirements(row, tags=["3-5年", "本科"])
    assert req.city == "杭州"
    assert (req.salary_min, req.salary_max) == (25, 45)
    assert req.years_min == 3
    assert req.degree == "本科"
    assert req.skills == (), "预门禁阶段不做技能抽取"


def test_run_collect_only_fetches_detail_for_gate_survivors(conn):
    """两阶段抓取：详情页请求量应当远小于列表条数。"""
    listed = [
        {
            "job_id": "keep",
            "url": "https://x/job_detail/keep~.html",
            "title": "后端",
            "company": "A",
            "city": "杭州",
            "salary_raw": "30-50K",
            "hr_name": "张",
            "tags": ["3-5年", "本科"],
        },
        {
            "job_id": "drop-city",
            "url": "https://x/job_detail/drop-city~.html",
            "title": "后端",
            "company": "B",
            "city": "北京",
            "salary_raw": "30-50K",
            "hr_name": "李",
            "tags": ["3-5年"],
        },
        {
            "job_id": "drop-salary",
            "url": "https://x/job_detail/drop-salary~.html",
            "title": "后端",
            "company": "C",
            "city": "杭州",
            "salary_raw": "8-12K",
            "hr_name": "王",
            "tags": ["1年以内"],
        },
    ]
    detail_calls = []

    report = run_collect(
        conn,
        keyword="后端",
        city_code="101210100",
        fetch_fn=lambda **kw: listed,
        detail_fn=lambda url: detail_calls.append(url) or {"raw_jd": "JD 全文"},
    )
    assert report.listed == 3
    assert report.gated_out == 2
    assert report.detail_fetched == 1
    assert len(detail_calls) == 1
    assert "keep" in detail_calls[0]

    kept = conn.execute("SELECT raw_jd FROM jobs WHERE job_id='keep'").fetchone()
    assert kept["raw_jd"] == "JD 全文"
    reason = conn.execute(
        "SELECT reject_reason FROM gate_results WHERE job_id='drop-city'"
    ).fetchone()
    assert "城市" in reason["reject_reason"]


def test_run_collect_skips_already_collected(conn):
    item = {
        "job_id": "j1",
        "url": "https://x/job_detail/j1~.html",
        "title": "后端",
        "company": "A",
        "city": "杭州",
        "salary_raw": "30-50K",
        "hr_name": "张",
        "tags": [],
    }
    run_collect(
        conn,
        keyword="k",
        city_code="c",
        fetch_fn=lambda **kw: [item],
        detail_fn=lambda url: {"raw_jd": "第一次"},
    )
    report = run_collect(
        conn,
        keyword="k",
        city_code="c",
        fetch_fn=lambda **kw: [item],
        detail_fn=lambda url: {"raw_jd": "第二次"},
    )
    assert report.new == 0
    assert report.detail_fetched == 0
    row = conn.execute("SELECT raw_jd FROM jobs WHERE job_id='j1'").fetchone()
    assert row["raw_jd"] == "第一次"


def test_run_score_marks_failure_without_guessing(conn, monkeypatch):
    from jobstar.llm import LLMSchemaError

    conn.execute(
        "INSERT INTO jobs (platform, job_id, title, company, raw_jd, detail_fetched, city) "
        "VALUES ('boss', 'j1', 't', 'c', 'JD', 1, '杭州')"
    )
    conn.execute("INSERT INTO gate_results (job_id, passed) VALUES ('j1', 1)")
    conn.commit()

    def boom(**kwargs):
        raise LLMSchemaError("重试一次后仍不是合法 JSON")

    monkeypatch.setattr("jobstar.pipeline.normalize", boom)
    report = run_score(conn)
    assert report.failed == 1
    assert report.scored == 0
    row = conn.execute("SELECT status FROM jobs WHERE job_id='j1'").fetchone()
    assert row["status"] == "scoring_failed"


def test_maybe_enqueue_does_nothing_when_threshold_is_none(conn):
    """设计文档 §5.3：第一版交付时不设阈值，一条消息都不发。"""
    req = JobRequirements(
        "j1", "杭州", None, None, None, None, None, (), None, None, None
    )
    result = ScoreResult("j1", 95.0, (DimensionScore("skills", 95, ("a",), "", ""),), "v1")
    assert maybe_enqueue(conn, req, result, title="t", company="c") is None
    assert list_by_status(conn, PENDING) == []


def test_maybe_enqueue_creates_pending_action_above_threshold(conn, monkeypatch):
    set_setting(conn, "score_threshold", 70)
    monkeypatch.setattr("jobstar.pipeline.load_cards", lambda path: ())
    monkeypatch.setattr("jobstar.pipeline.write_pitch", lambda **kw: "定制开场白")
    req = JobRequirements(
        "j1", "杭州", None, None, None, None, None, (), None, None, None
    )
    result = ScoreResult("j1", 82.0, (DimensionScore("skills", 82, ("a",), "", ""),), "v1")
    action_id = maybe_enqueue(conn, req, result, title="t", company="c")
    assert action_id is not None
    row = conn.execute("SELECT * FROM actions WHERE id=?", (action_id,)).fetchone()
    assert row["status"] == PENDING
    assert json.loads(row["payload"])["greeting"] == "定制开场白"


def test_maybe_enqueue_skips_below_threshold(conn):
    set_setting(conn, "score_threshold", 70)
    req = JobRequirements(
        "j1", "杭州", None, None, None, None, None, (), None, None, None
    )
    result = ScoreResult("j1", 55.0, (), "v1")
    assert maybe_enqueue(conn, req, result, title="t", company="c") is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_pipeline.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'jobstar.pipeline'`

- [ ] **Step 3: 写 pipeline.py**

```python
"""管道编排：把采集、门禁、归一化、打分、话术串起来。

采集阶段跑两遍门禁：
  第一遍用列表页字段拼的「预备版」需求，刷掉大部分，省下详情页请求和 token；
  第二遍用完整归一化结果，刷掉列表页看不出来的（比如 JD 里才写的学历硬要求）。
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Callable

from jobstar import actions, gate
from jobstar.config import get_setting, get_settings
from jobstar.evidence import load_cards
from jobstar.llm import LLMBackendError, LLMSchemaError
from jobstar.models import JobRequirements
from jobstar.normalizer import (
    normalize,
    parse_salary_raw,
    save_requirements,
)
from jobstar.pitch import write_pitch
from jobstar.scorer import save_failure, save_score, score

_YEARS_RANGE = re.compile(r"(\d+)\s*[-~]\s*(\d+)\s*年")
_YEARS_MIN = re.compile(r"(\d+)\s*年以上")
_YEARS_MAX = re.compile(r"(\d+)\s*年以内")
_DEGREES = ("博士", "硕士", "本科", "大专", "中专", "高中")


@dataclass
class CollectReport:
    listed: int = 0
    new: int = 0
    gated_out: int = 0
    detail_fetched: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass
class ScoreReport:
    scored: int = 0
    gated_out: int = 0
    failed: int = 0
    enqueued: int = 0
    errors: list[str] = field(default_factory=list)


def parse_tags(tags: list[str]) -> dict:
    """从列表页标签里抠年限和学历。抠不出来的字段是 None。"""
    out: dict[str, object] = {"years_min": None, "years_max": None, "degree": None}
    for tag in tags:
        text = str(tag).strip()
        if "学历不限" in text:
            out["degree"] = "不限"
        else:
            for degree in _DEGREES:
                if degree in text:
                    out["degree"] = degree
                    break
        if "经验不限" in text:
            out["years_min"] = 0
        elif "应届" in text or "在校" in text:
            out["years_min"], out["years_max"] = 0, 1
        else:
            match = _YEARS_RANGE.search(text)
            if match:
                out["years_min"] = int(match.group(1))
                out["years_max"] = int(match.group(2))
                continue
            match = _YEARS_MIN.search(text)
            if match:
                out["years_min"] = int(match.group(1))
                continue
            match = _YEARS_MAX.search(text)
            if match:
                out["years_min"], out["years_max"] = 0, int(match.group(1))
    return out


def prelim_requirements(
    row: sqlite3.Row, tags: list[str] | None = None
) -> JobRequirements:
    """只用列表页字段拼一个预备版需求，给第一遍门禁用。不调 LLM。"""
    parsed = parse_tags(tags or [])
    salary_min, salary_max = parse_salary_raw(row["salary_raw"])
    return JobRequirements(
        job_id=row["job_id"],
        city=row["city"],
        degree=parsed["degree"],
        years_min=parsed["years_min"],
        years_max=parsed["years_max"],
        salary_min=salary_min,
        salary_max=salary_max,
        skills=(),
        industry=None,
        company_size=None,
        category=None,
    )


def run_collect(
    conn: sqlite3.Connection,
    *,
    keyword: str,
    city_code: str,
    pages: int = 1,
    fetch_fn: Callable[..., list[dict]] | None = None,
    detail_fn: Callable[[str], dict] | None = None,
) -> CollectReport:
    from jobstar.collector import boss

    fetch_fn = fetch_fn or boss.fetch_list
    detail_fn = detail_fn or boss.fetch_detail

    report = CollectReport()
    items = fetch_fn(keyword=keyword, city_code=city_code, pages=pages)
    report.listed = len(items)
    report.new = boss.save_jobs(conn, items)

    rules = dict(get_setting(conn, "gate_rules"))
    rules.setdefault("my_degree", get_setting(conn, "my_degree"))

    for item in items:
        row = conn.execute(
            "SELECT * FROM jobs WHERE job_id = ?", (item["job_id"],)
        ).fetchone()
        if row is None or row["detail_fetched"]:
            continue

        req = prelim_requirements(row, item.get("tags"))
        result = gate.check(req, rules, company=row["company"] or "")
        gate.save_result(conn, row["job_id"], result)
        if not result.passed:
            report.gated_out += 1
            conn.execute(
                "UPDATE jobs SET status='gated_out' WHERE job_id=?", (row["job_id"],)
            )
            conn.commit()
            continue

        try:
            detail = detail_fn(row["url"])
        except Exception as exc:
            report.errors.append(f"{row['job_id']}: {exc}")
            continue
        boss.save_detail(conn, row["job_id"], detail)
        report.detail_fetched += 1

    return report


def maybe_enqueue(
    conn: sqlite3.Connection,
    req: JobRequirements,
    result,
    *,
    title: str,
    company: str,
) -> int | None:
    """总分达到阈值才生成待确认动作。阈值为 None（冷启动期）时永远不生成。"""
    threshold = get_setting(conn, "score_threshold")
    if threshold is None or result.total < float(threshold):
        return None
    cards = load_cards(get_settings().cards_path)
    greeting = write_pitch(
        req=req, result=result, cards=cards, title=title, company=company
    )
    return actions.enqueue(
        conn,
        type="send_greeting",
        job_id=req.job_id,
        payload={"greeting": greeting, "title": title, "company": company},
    )


def run_score(conn: sqlite3.Connection, *, limit: int | None = None) -> ScoreReport:
    """对已抓详情、已过第一遍门禁、还没打过分的岗位做归一化 + 门禁 + 打分。"""
    report = ScoreReport()
    sql = (
        "SELECT j.* FROM jobs j "
        "JOIN gate_results g ON g.job_id = j.job_id AND g.passed = 1 "
        "LEFT JOIN scores s ON s.job_id = j.job_id "
        "WHERE j.detail_fetched = 1 AND s.job_id IS NULL "
        "ORDER BY j.collected_at"
    )
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    rows = conn.execute(sql).fetchall()

    cards = load_cards(get_settings().cards_path)
    weights = get_setting(conn, "dimension_weights")
    rules = dict(get_setting(conn, "gate_rules"))
    rules.setdefault("my_degree", get_setting(conn, "my_degree"))

    for row in rows:
        try:
            req = normalize(
                job_id=row["job_id"],
                title=row["title"],
                raw_jd=row["raw_jd"],
                city_hint=row["city"],
                salary_hint=row["salary_raw"],
            )
        except (LLMSchemaError, LLMBackendError) as exc:
            save_failure(conn, row["job_id"], f"归一化失败：{exc}")
            report.failed += 1
            report.errors.append(f"{row['job_id']}: {exc}")
            continue
        save_requirements(conn, req)

        gate_result = gate.check(req, rules, company=row["company"] or "")
        gate.save_result(conn, row["job_id"], gate_result)
        if not gate_result.passed:
            report.gated_out += 1
            conn.execute(
                "UPDATE jobs SET status='gated_out' WHERE job_id=?", (row["job_id"],)
            )
            conn.commit()
            continue

        try:
            result = score(req, cards, weights)
        except (LLMSchemaError, LLMBackendError) as exc:
            save_failure(conn, row["job_id"], f"打分失败：{exc}")
            report.failed += 1
            report.errors.append(f"{row['job_id']}: {exc}")
            continue

        save_score(conn, result)
        report.scored += 1

        try:
            if maybe_enqueue(
                conn, req, result, title=row["title"], company=row["company"] or ""
            ) is not None:
                report.enqueued += 1
        except (LLMSchemaError, LLMBackendError, ValueError) as exc:
            report.errors.append(f"{row['job_id']} 话术生成失败: {exc}")

    return report
```

- [ ] **Step 4: 写 cli.py**

```python
"""命令行入口。`jobstar <子命令>`。"""

from __future__ import annotations

import argparse
import sys

from jobstar.config import get_settings
from jobstar.db import get_conn, init_db


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jobstar")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="建库")

    p_collect = sub.add_parser("collect", help="采集岗位（列表页 → 预门禁 → 详情页）")
    p_collect.add_argument("--keyword", required=True)
    p_collect.add_argument("--city", default="101210100", help="Boss 城市码，默认杭州")
    p_collect.add_argument("--pages", type=int, default=1)

    p_score = sub.add_parser("score", help="归一化 + 门禁 + 打分")
    p_score.add_argument("--limit", type=int, default=None)

    sub.add_parser("send", help="执行队列中已批准的动作")

    p_serve = sub.add_parser("serve", help="启动本地面板")
    p_serve.add_argument("--port", type=int, default=8777)

    sub.add_parser("cards", help="校验能力卡片库")

    args = parser.parse_args(argv)
    settings = get_settings()
    conn = get_conn(settings.db_path)
    init_db(conn)

    if args.cmd == "init":
        print(f"已建库：{settings.db_path}")
        return 0

    if args.cmd == "collect":
        from jobstar.pipeline import run_collect

        report = run_collect(
            conn, keyword=args.keyword, city_code=args.city, pages=args.pages
        )
        print(
            f"列表 {report.listed} 条，新增 {report.new}，"
            f"预门禁刷掉 {report.gated_out}，抓详情 {report.detail_fetched}"
        )
        for err in report.errors:
            print(f"  ! {err}", file=sys.stderr)
        return 0

    if args.cmd == "score":
        from jobstar.pipeline import run_score

        report = run_score(conn, limit=args.limit)
        print(
            f"打分 {report.scored}，门禁刷掉 {report.gated_out}，"
            f"失败 {report.failed}，入队 {report.enqueued}"
        )
        for err in report.errors:
            print(f"  ! {err}", file=sys.stderr)
        return 0

    if args.cmd == "send":
        from jobstar.executor import run_queue

        report = run_queue(conn)
        print(f"已发送 {report.sent}，失败 {report.failed}，触达配额 {report.quota_hit}")
        for err in report.errors:
            print(f"  ! {err}", file=sys.stderr)
        return 0

    if args.cmd == "serve":
        import uvicorn

        from jobstar.panel.app import app

        uvicorn.run(app, host="127.0.0.1", port=args.port)
        return 0

    if args.cmd == "cards":
        from jobstar.evidence import cards_to_prompt_block, load_cards

        cards = load_cards(settings.cards_path)
        block = cards_to_prompt_block(cards)
        print(f"{len(cards)} 张卡片，prompt 块 {len(block)} 字符")
        from collections import Counter

        for strength, count in Counter(c.strength.value for c in cards).items():
            print(f"  证据强度 {strength}: {count} 张")
        if len(cards) > 200 or len(block) > 30000:
            print("⚠️  已超过设计文档 §5.2 的重新评估阈值，考虑换成向量检索")
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 5: 跑测试确认通过**

Run: `uv run pytest tests/test_pipeline.py -v`
Expected: 14 passed（6 个 parametrize + 8 个）

- [ ] **Step 6: 验证 CLI 可用**

```bash
uv run jobstar init
uv run jobstar cards
```
Expected: 打印库路径，以及卡片数与证据强度分布

- [ ] **Step 7: 提交**

```bash
git add src/jobstar/pipeline.py src/jobstar/cli.py tests/test_pipeline.py
git commit -m "feat: 管道编排（两阶段门禁）与 CLI 入口"
```

---

## Task 12: Web 面板

**Files:**
- Create: `src/jobstar/panel/__init__.py`, `src/jobstar/panel/app.py`, `src/jobstar/panel/static/index.html`
- Test: `tests/test_panel.py`

**Interfaces:**
- Consumes: `jobstar.actions`（Task 8）、`jobstar.scorer.load_score`（Task 6）、`jobstar.config`（Task 1）、`jobstar.evidence.load_cards`（Task 3）
- Produces:
  - `jobstar.panel.app.app`（FastAPI 实例）
  - `jobstar.panel.app.get_db()`（依赖注入点，测试里覆盖）

**路由清单：**

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/` | 单页面板 |
| GET | `/api/health` | 登录态横幅、剩余配额、打分失败数 |
| GET | `/api/queue` | 待确认队列（岗位摘要 + 总分 + 话术草稿） |
| POST | `/api/actions/{id}/approve` | 确认（可带改写后的 greeting） |
| POST | `/api/actions/{id}/skip` | 跳过 |
| GET | `/api/jobs/{job_id}` | 分数拆解：各维度分数、引用卡片全文、缺口 |
| GET | `/api/label/next` | 下一条未标注的已打分岗位 |
| POST | `/api/label` | 写标注 |
| GET | `/api/threshold` | 标注分布，用于反推阈值（设计文档 §5.3 第 3 步） |
| GET / PUT | `/api/settings` | 面板可配置项 |

- [ ] **Step 1: 写失败测试**

写 `tests/test_panel.py`：

```python
import pytest
from fastapi.testclient import TestClient

from jobstar.actions import PENDING, SKIPPED, approve, enqueue
from jobstar.db import get_conn, init_db
from jobstar.panel.app import app, get_db


@pytest.fixture()
def client(tmp_path):
    conn = get_conn(tmp_path / "t.db")
    init_db(conn)
    conn.execute(
        "INSERT INTO jobs (platform, job_id, title, company, raw_jd, city, "
        " salary_raw, url, detail_fetched, status) "
        "VALUES ('boss','j1','AI 后端工程师','某某科技','JD 全文','杭州',"
        " '30-50K','https://x/job_detail/j1~.html',1,'scored')"
    )
    conn.execute(
        "INSERT INTO scores (job_id, total, dimensions, scorer_version) "
        "VALUES ('j1', 82.0, "
        "'[{\"name\":\"skills\",\"score\":90,\"card_ids\":[\"cap-rag\"],"
        "\"reason\":\"RAG 命中\",\"gap\":\"缺 LangGraph\"}]', 'v1')"
    )
    conn.commit()
    app.dependency_overrides[get_db] = lambda: conn
    with TestClient(app) as c:
        c.conn = conn
        yield c
    app.dependency_overrides.clear()


def test_index_serves_html(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "待确认" in resp.text


def test_health_reports_quota_and_failures(client):
    client.conn.execute(
        "INSERT INTO scores (job_id, total, dimensions, scorer_version, error) "
        "VALUES ('bad', NULL, '[]', 'v1', '重试后仍不合 schema')"
    )
    client.conn.commit()
    body = client.get("/api/health").json()
    assert body["remaining_quota"] == 25
    assert body["scoring_failed"] == 1


def test_queue_joins_job_score_and_greeting(client):
    enqueue(
        client.conn,
        type="send_greeting",
        job_id="j1",
        payload={"greeting": "定制开场白"},
    )
    items = client.get("/api/queue").json()["items"]
    assert len(items) == 1
    assert items[0]["title"] == "AI 后端工程师"
    assert items[0]["company"] == "某某科技"
    assert items[0]["total"] == 82.0
    assert items[0]["greeting"] == "定制开场白"


def test_approve_moves_action_and_can_rewrite(client):
    action_id = enqueue(
        client.conn, type="send_greeting", job_id="j1", payload={"greeting": "原稿"}
    )
    resp = client.post(
        f"/api/actions/{action_id}/approve", json={"greeting": "改写后的稿子"}
    )
    assert resp.status_code == 200
    row = client.conn.execute(
        "SELECT status, payload FROM actions WHERE id=?", (action_id,)
    ).fetchone()
    assert row["status"] == "approved"
    assert "改写后的稿子" in row["payload"]


def test_skip_moves_action(client):
    action_id = enqueue(
        client.conn, type="send_greeting", job_id="j1", payload={"greeting": "稿"}
    )
    client.post(f"/api/actions/{action_id}/skip")
    row = client.conn.execute(
        "SELECT status FROM actions WHERE id=?", (action_id,)
    ).fetchone()
    assert row["status"] == SKIPPED


def test_approve_rejects_invalid_transition(client):
    action_id = enqueue(
        client.conn, type="send_greeting", job_id="j1", payload={"greeting": "稿"}
    )
    approve(client.conn, action_id)
    client.conn.execute(
        "UPDATE actions SET status='sent', sent_at=datetime('now') WHERE id=?",
        (action_id,),
    )
    client.conn.commit()
    resp = client.post(f"/api/actions/{action_id}/approve", json={})
    assert resp.status_code == 409


def test_job_detail_returns_dimension_breakdown_with_cards(client, monkeypatch):
    from jobstar.models import CapabilityCard, Strength

    card = CapabilityCard(
        id="cap-rag",
        capability="RAG",
        synonyms=("检索增强",),
        strength=Strength.STRONG,
        project="电信 OCR + RAG",
        metrics=("准确率",),
        depth="分块策略",
        resume_versions=("后端AI版",),
    )
    monkeypatch.setattr("jobstar.panel.app.load_cards", lambda path: (card,))
    body = client.get("/api/jobs/j1").json()
    assert body["total"] == 82.0
    dim = body["dimensions"][0]
    assert dim["label"] == "技能匹配"
    assert dim["gap"] == "缺 LangGraph"
    assert dim["cards"][0]["capability"] == "RAG"
    assert dim["cards"][0]["strength"] == "强"


def test_job_detail_404s_for_unknown_job(client):
    assert client.get("/api/jobs/nope").status_code == 404


def test_label_next_returns_unlabeled_scored_job(client):
    body = client.get("/api/label/next").json()
    assert body["job_id"] == "j1"
    assert body["total"] == 82.0
    assert body["raw_jd"] == "JD 全文"


def test_label_next_returns_null_when_all_labeled(client):
    client.post("/api/label", json={"job_id": "j1", "would_apply": True})
    assert client.get("/api/label/next").json() == {"job_id": None}


def test_label_is_idempotent(client):
    client.post("/api/label", json={"job_id": "j1", "would_apply": True})
    client.post("/api/label", json={"job_id": "j1", "would_apply": False})
    row = client.conn.execute("SELECT would_apply FROM labels WHERE job_id='j1'").fetchone()
    assert row["would_apply"] == 0


def test_threshold_endpoint_reports_both_groups(client):
    client.conn.execute(
        "INSERT INTO jobs (platform, job_id, title, company, raw_jd, status) "
        "VALUES ('boss','j2','另一个岗','B','JD','scored')"
    )
    client.conn.execute(
        "INSERT INTO scores (job_id, total, dimensions, scorer_version) "
        "VALUES ('j2', 41.0, '[]', 'v1')"
    )
    client.conn.commit()
    client.post("/api/label", json={"job_id": "j1", "would_apply": True})
    client.post("/api/label", json={"job_id": "j2", "would_apply": False})
    body = client.get("/api/threshold").json()
    assert body["would_apply"]["count"] == 1
    assert body["would_apply"]["min"] == 82.0
    assert body["would_not_apply"]["max"] == 41.0
    assert body["labeled_total"] == 2


def test_settings_roundtrip(client):
    client.put("/api/settings", json={"daily_greeting_limit": 10, "score_threshold": 70})
    body = client.get("/api/settings").json()
    assert body["daily_greeting_limit"] == 10
    assert body["score_threshold"] == 70


def test_settings_rejects_unknown_key(client):
    resp = client.put("/api/settings", json={"不存在的项": 1})
    assert resp.status_code == 400
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_panel.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'jobstar.panel'`

- [ ] **Step 3: 写 panel/app.py**

```python
"""本地 Web 面板：唯一的人工入口。四个视图共用这套 API。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from fastapi import Body, Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse

from jobstar import actions
from jobstar.config import (
    SETTING_DEFAULTS,
    get_setting,
    get_settings,
    set_setting,
)
from jobstar.db import get_conn, init_db
from jobstar.evidence import load_cards
from jobstar.models import DIMENSION_LABELS

STATIC = Path(__file__).parent / "static"

app = FastAPI(title="jobstar 面板")


def get_db() -> sqlite3.Connection:
    conn = get_conn(get_settings().db_path)
    init_db(conn)
    return conn


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/health")
def health(conn: sqlite3.Connection = Depends(get_db)) -> dict:
    failed = conn.execute(
        "SELECT COUNT(*) AS n FROM scores WHERE error IS NOT NULL"
    ).fetchone()["n"]
    return {
        "remaining_quota": actions.remaining_quota(conn),
        "sent_today": actions.sent_today(conn),
        "daily_limit": get_setting(conn, "daily_greeting_limit"),
        "score_threshold": get_setting(conn, "score_threshold"),
        "scoring_failed": failed,
        "pending": len(actions.list_by_status(conn, actions.PENDING)),
    }


@app.get("/api/queue")
def queue(conn: sqlite3.Connection = Depends(get_db)) -> dict:
    rows = conn.execute(
        "SELECT a.id, a.job_id, a.payload, j.title, j.company, j.city, "
        "       j.salary_raw, j.url, s.total "
        "FROM actions a "
        "JOIN jobs j ON j.job_id = a.job_id "
        "LEFT JOIN scores s ON s.job_id = a.job_id "
        "WHERE a.status = ? ORDER BY s.total DESC NULLS LAST, a.created_at",
        (actions.PENDING,),
    ).fetchall()
    items = []
    for row in rows:
        payload = json.loads(row["payload"])
        items.append(
            {
                "action_id": row["id"],
                "job_id": row["job_id"],
                "title": row["title"],
                "company": row["company"],
                "city": row["city"],
                "salary_raw": row["salary_raw"],
                "url": row["url"],
                "total": row["total"],
                "greeting": payload.get("greeting", ""),
            }
        )
    return {"items": items}


@app.post("/api/actions/{action_id}/approve")
def approve_action(
    action_id: int,
    body: dict = Body(default={}),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    row = conn.execute(
        "SELECT payload FROM actions WHERE id = ?", (action_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(404, "动作不存在")
    override = None
    if body.get("greeting"):
        payload = json.loads(row["payload"])
        payload["greeting"] = body["greeting"]
        override = payload
    try:
        actions.approve(conn, action_id, override)
    except actions.InvalidTransition as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"ok": True}


@app.post("/api/actions/{action_id}/skip")
def skip_action(
    action_id: int, conn: sqlite3.Connection = Depends(get_db)
) -> dict:
    try:
        actions.skip(conn, action_id)
    except actions.InvalidTransition as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"ok": True}


@app.get("/api/jobs/{job_id}")
def job_detail(job_id: str, conn: sqlite3.Connection = Depends(get_db)) -> dict:
    job = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
    if job is None:
        raise HTTPException(404, "岗位不存在")
    score_row = conn.execute(
        "SELECT * FROM scores WHERE job_id = ?", (job_id,)
    ).fetchone()
    gate_row = conn.execute(
        "SELECT * FROM gate_results WHERE job_id = ?", (job_id,)
    ).fetchone()

    card_index = {c.id: c for c in load_cards(get_settings().cards_path)}
    dimensions = []
    for dim in json.loads(score_row["dimensions"]) if score_row else []:
        dimensions.append(
            {
                "name": dim["name"],
                "label": DIMENSION_LABELS.get(dim["name"], dim["name"]),
                "score": dim["score"],
                "reason": dim["reason"],
                "gap": dim["gap"],
                "cards": [
                    {
                        "id": cid,
                        "capability": card_index[cid].capability,
                        "strength": card_index[cid].strength.value,
                        "project": card_index[cid].project,
                        "depth": card_index[cid].depth,
                    }
                    for cid in dim["card_ids"]
                    if cid in card_index
                ],
            }
        )
    return {
        "job_id": job_id,
        "title": job["title"],
        "company": job["company"],
        "city": job["city"],
        "salary_raw": job["salary_raw"],
        "url": job["url"],
        "raw_jd": job["raw_jd"],
        "status": job["status"],
        "total": score_row["total"] if score_row else None,
        "scorer_version": score_row["scorer_version"] if score_row else None,
        "error": score_row["error"] if score_row else None,
        "gate_passed": bool(gate_row["passed"]) if gate_row else None,
        "gate_reason": gate_row["reject_reason"] if gate_row else None,
        "dimensions": dimensions,
    }


@app.get("/api/label/next")
def label_next(conn: sqlite3.Connection = Depends(get_db)) -> dict:
    row = conn.execute(
        "SELECT j.job_id, j.title, j.company, j.city, j.salary_raw, j.raw_jd, "
        "       j.url, s.total "
        "FROM jobs j JOIN scores s ON s.job_id = j.job_id "
        "LEFT JOIN labels l ON l.job_id = j.job_id "
        "WHERE s.total IS NOT NULL AND l.job_id IS NULL "
        "ORDER BY j.collected_at LIMIT 1"
    ).fetchone()
    if row is None:
        return {"job_id": None}
    return dict(row)


@app.post("/api/label")
def write_label(
    body: dict = Body(...), conn: sqlite3.Connection = Depends(get_db)
) -> dict:
    job_id = body.get("job_id")
    if not job_id:
        raise HTTPException(400, "缺少 job_id")
    conn.execute(
        "INSERT INTO labels (job_id, would_apply) VALUES (?, ?) "
        "ON CONFLICT(job_id) DO UPDATE SET "
        "would_apply = excluded.would_apply, labeled_at = datetime('now')",
        (job_id, int(bool(body.get("would_apply")))),
    )
    conn.commit()
    return {"ok": True}


def _stats(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"count": 0, "min": None, "max": None, "mean": None}
    return {
        "count": len(values),
        "min": min(values),
        "max": max(values),
        "mean": round(sum(values) / len(values), 2),
    }


@app.get("/api/threshold")
def threshold(conn: sqlite3.Connection = Depends(get_db)) -> dict:
    """标注分布，用于反推阈值（设计文档 §5.3 第 3 步）。"""
    rows = conn.execute(
        "SELECT l.would_apply, s.total FROM labels l "
        "JOIN scores s ON s.job_id = l.job_id WHERE s.total IS NOT NULL"
    ).fetchall()
    yes = [r["total"] for r in rows if r["would_apply"]]
    no = [r["total"] for r in rows if not r["would_apply"]]
    return {
        "labeled_total": len(rows),
        "would_apply": _stats(yes),
        "would_not_apply": _stats(no),
        "scores_yes": sorted(yes),
        "scores_no": sorted(no),
    }


@app.get("/api/settings")
def read_settings(conn: sqlite3.Connection = Depends(get_db)) -> dict:
    return {key: get_setting(conn, key) for key in SETTING_DEFAULTS}


@app.put("/api/settings")
def write_settings(
    body: dict = Body(...), conn: sqlite3.Connection = Depends(get_db)
) -> dict:
    unknown = [k for k in body if k not in SETTING_DEFAULTS]
    if unknown:
        raise HTTPException(400, f"未知配置项：{unknown}")
    for key, value in body.items():
        set_setting(conn, key, value)
    return {"ok": True}
```

- [ ] **Step 4: 写 panel/static/index.html**

```html
<!doctype html>
<meta charset="utf-8" />
<title>jobstar 面板</title>
<style>
  :root { color-scheme: light dark; }
  body { font: 15px/1.6 -apple-system, "PingFang SC", sans-serif; margin: 0; padding: 20px; max-width: 900px; }
  nav button { padding: 6px 14px; margin-right: 6px; cursor: pointer; }
  nav button.on { font-weight: 700; }
  .bar { padding: 8px 12px; border-radius: 6px; background: #f2f2f2; margin-bottom: 16px; font-size: 13px; }
  .card { border: 1px solid #ddd; border-radius: 8px; padding: 14px; margin-bottom: 14px; }
  .meta { color: #666; font-size: 13px; }
  .score { float: right; font-size: 22px; font-weight: 700; }
  textarea { width: 100%; min-height: 110px; font: inherit; padding: 8px; box-sizing: border-box; }
  .row { display: flex; gap: 8px; margin-top: 8px; }
  .gap { color: #b35; }
  .jd { white-space: pre-wrap; max-height: 320px; overflow: auto; background: #fafafa; padding: 10px; border-radius: 6px; }
  label { display: block; margin: 8px 0; }
  label input { width: 320px; }
  @media (prefers-color-scheme: dark) {
    .bar, .jd { background: #222; }
    .card { border-color: #444; }
  }
</style>

<nav>
  <button data-view="queue" class="on">待确认队列</button>
  <button data-view="label">标注</button>
  <button data-view="threshold">阈值</button>
  <button data-view="settings">设置</button>
</nav>
<div class="bar" id="bar"></div>
<div id="main"></div>

<script>
const $ = (s) => document.querySelector(s);
const api = (p, o) => fetch(p, o).then(r => r.ok ? r.json() : r.json().then(e => Promise.reject(e)));
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, c => ({ "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;" }[c]));
let view = "queue";

async function bar() {
  const h = await api("/api/health");
  const thr = h.score_threshold === null ? "未设（冷启动期，不生成动作）" : h.score_threshold;
  $("#bar").innerHTML =
    `今日已发 ${h.sent_today}/${h.daily_limit}，剩余 ${h.remaining_quota}　|　` +
    `待确认 ${h.pending}　|　阈值 ${esc(thr)}` +
    (h.scoring_failed ? `　|　<b class="gap">打分失败 ${h.scoring_failed} 条待人工处理</b>` : "");
}

async function renderQueue() {
  const { items } = await api("/api/queue");
  if (!items.length) { $("#main").innerHTML = "<p>队列为空。</p>"; return; }
  $("#main").innerHTML = items.map(i => `
    <div class="card" data-id="${i.action_id}">
      <span class="score">${i.total ?? "-"}</span>
      <b>${esc(i.title)}</b> · ${esc(i.company)}
      <div class="meta">${esc(i.city)} · ${esc(i.salary_raw)} ·
        <a href="${esc(i.url)}" target="_blank">原页面</a> ·
        <a href="#" data-detail="${esc(i.job_id)}">分数拆解</a></div>
      <textarea>${esc(i.greeting)}</textarea>
      <div class="row">
        <button data-act="approve">确认发送</button>
        <button data-act="skip">跳过</button>
      </div>
      <div class="detail"></div>
    </div>`).join("");
}

async function renderLabel() {
  const j = await api("/api/label/next");
  if (!j.job_id) { $("#main").innerHTML = "<p>没有待标注的岗位了。</p>"; return; }
  $("#main").innerHTML = `
    <div class="card" data-label="${esc(j.job_id)}">
      <span class="score">${j.total}</span>
      <b>${esc(j.title)}</b> · ${esc(j.company)}
      <div class="meta">${esc(j.city)} · ${esc(j.salary_raw)}</div>
      <div class="jd">${esc(j.raw_jd)}</div>
      <div class="row">
        <button data-label-yes>会投 (y)</button>
        <button data-label-no>不会投 (n)</button>
      </div>
      <div class="meta">键盘 y / n 可以快速翻页</div>
    </div>`;
}

async function renderThreshold() {
  const t = await api("/api/threshold");
  const f = (g) => g.count ? `${g.count} 条，区间 ${g.min} – ${g.max}，均值 ${g.mean}` : "无";
  $("#main").innerHTML = `
    <div class="card">
      <p>已标注 <b>${t.labeled_total}</b> 条（设计文档建议 30-50 条后再定阈值）。</p>
      <p>会投：${f(t.would_apply)}</p>
      <p>不会投：${f(t.would_not_apply)}</p>
      <p class="meta">会投组的下界和不会投组的上界之间，就是阈值的候选区间。
        若两组大量重叠，说明某个维度权重不对，先去设置里调权重再重跑打分。</p>
      <p class="meta">会投分数：${t.scores_yes.join(", ") || "无"}</p>
      <p class="meta">不会投分数：${t.scores_no.join(", ") || "无"}</p>
    </div>`;
}

async function renderSettings() {
  const s = await api("/api/settings");
  $("#main").innerHTML = `<div class="card">` + Object.entries(s).map(([k, v]) =>
    `<label>${esc(k)}<br><input data-key="${esc(k)}" value='${esc(JSON.stringify(v))}'></label>`
  ).join("") + `<button id="save">保存</button>
    <div class="meta">值按 JSON 解析。score_threshold 填 null 表示冷启动期不生成动作。</div></div>`;
  $("#save").onclick = async () => {
    const body = {};
    for (const el of document.querySelectorAll("[data-key]")) {
      try { body[el.dataset.key] = JSON.parse(el.value); }
      catch { alert(`${el.dataset.key} 不是合法 JSON`); return; }
    }
    await api("/api/settings", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    await bar(); alert("已保存");
  };
}

const VIEWS = { queue: renderQueue, label: renderLabel, threshold: renderThreshold, settings: renderSettings };
async function render() { await bar(); await VIEWS[view](); }

document.addEventListener("click", async (e) => {
  const navBtn = e.target.closest("nav button");
  if (navBtn) {
    document.querySelectorAll("nav button").forEach(b => b.classList.toggle("on", b === navBtn));
    view = navBtn.dataset.view; return render();
  }
  const act = e.target.dataset.act;
  if (act) {
    const card = e.target.closest(".card");
    const id = card.dataset.id;
    const greeting = card.querySelector("textarea").value;
    try {
      await api(`/api/actions/${id}/${act}`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(act === "approve" ? { greeting } : {}),
      });
    } catch (err) { alert(err.detail || "操作失败"); }
    return render();
  }
  const detail = e.target.dataset.detail;
  if (detail) {
    e.preventDefault();
    const box = e.target.closest(".card").querySelector(".detail");
    const d = await api(`/api/jobs/${detail}`);
    box.innerHTML = d.dimensions.map(dim => `
      <div style="margin-top:10px">
        <b>${esc(dim.label)}: ${dim.score}</b><br>
        <span class="meta">${esc(dim.reason)}</span><br>
        ${dim.cards.map(c => `<span class="meta">[${esc(c.id)}] ${esc(c.capability)}（证据${esc(c.strength)}）· ${esc(c.project)}</span><br>`).join("")}
        <span class="gap">缺口：${esc(dim.gap)}</span>
      </div>`).join("");
    return;
  }
  const yes = e.target.hasAttribute("data-label-yes");
  const no = e.target.hasAttribute("data-label-no");
  if (yes || no) return label(yes);
});

async function label(wouldApply) {
  const card = document.querySelector("[data-label]");
  if (!card) return;
  await api("/api/label", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ job_id: card.dataset.label, would_apply: wouldApply }),
  });
  render();
}

document.addEventListener("keydown", (e) => {
  if (view !== "label" || e.target.tagName === "TEXTAREA" || e.target.tagName === "INPUT") return;
  if (e.key === "y") label(true);
  if (e.key === "n") label(false);
});

render();
</script>
```

- [ ] **Step 5: 跑测试确认通过**

Run: `uv run pytest tests/test_panel.py -v`
Expected: 14 passed

- [ ] **Step 6: 跑全套回归**

Run: `uv run pytest -v`
Expected: 全绿，约 120 个用例

- [ ] **Step 7: 起面板看一眼**

```bash
uv run jobstar serve
```
浏览器打开 http://127.0.0.1:8777 ，确认四个视图都能切换、顶部横幅显示配额和「阈值：未设（冷启动期，不生成动作）」。

- [ ] **Step 8: 提交**

```bash
git add src/jobstar/panel tests/test_panel.py
git commit -m "feat: 本地 Web 面板（待确认队列、分数拆解、标注、阈值反推、设置）"
```

---

## Task 13: 采集健康横幅

设计文档 §7 的错误处理表里有两行要求「面板顶部横幅提示」「面板汇总提示」，前 12 个任务只做到了「采集中止不静默失败」，横幅本身没接。这个任务补上。

**Files:**
- Modify: `src/jobstar/config.py`（`SETTING_DEFAULTS` 加两项）
- Modify: `src/jobstar/cli.py`（collect 子命令记录采集健康状态）
- Modify: `src/jobstar/panel/app.py`（`/api/health` 增加字段、新增清除端点）
- Modify: `src/jobstar/panel/static/index.html`（横幅渲染）
- Test: `tests/test_panel.py`（追加）, `tests/test_config.py`（追加）

**Interfaces:**
- Consumes: Task 1 的 `SETTING_DEFAULTS` / `get_setting` / `set_setting`、Task 9 的 `LoginRequired`
- Produces:
  - `SETTING_DEFAULTS` 新增 `last_collect_error: None`、`last_collect_at: None`
  - `GET /api/health` 新增 `last_collect_error`、`last_collect_at`、`quota_resets_at`
  - `POST /api/health/clear-error`

- [ ] **Step 1: 写失败测试**

在 `tests/test_config.py` 末尾追加：

```python
def test_collect_health_settings_default_to_none(conn):
    assert get_setting(conn, "last_collect_error") is None
    assert get_setting(conn, "last_collect_at") is None
```

在 `tests/test_panel.py` 末尾追加：

```python
def test_health_surfaces_collect_error(client):
    from jobstar.config import set_setting

    set_setting(client.conn, "last_collect_error", "Boss 登录态失效，采集已中止。")
    set_setting(client.conn, "last_collect_at", "2026-09-14 10:00:00")
    body = client.get("/api/health").json()
    assert "登录态失效" in body["last_collect_error"]
    assert body["last_collect_at"] == "2026-09-14 10:00:00"


def test_health_reports_quota_reset_time(client):
    body = client.get("/api/health").json()
    assert body["quota_resets_at"].endswith("00:00:00")


def test_clear_error_resets_banner(client):
    from jobstar.config import set_setting

    set_setting(client.conn, "last_collect_error", "页面结构变更，3 个岗位解析失败")
    client.post("/api/health/clear-error")
    assert client.get("/api/health").json()["last_collect_error"] is None


def test_index_renders_banner_placeholder(client):
    assert "last_collect_error" in client.get("/").text
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_panel.py tests/test_config.py -v`
Expected: 5 个新用例 FAIL（`KeyError: 'last_collect_error'` 等）

- [ ] **Step 3: 改 config.py**

在 `SETTING_DEFAULTS` 字典里，`my_years` 那一行后面加两项：

```python
    "my_degree": "硕士",
    "my_years": 5,
    # 采集健康状态。由 CLI 的 collect 子命令写入，面板顶部横幅读取。
    "last_collect_error": None,
    "last_collect_at": None,
```

- [ ] **Step 4: 改 cli.py 的 collect 分支**

把 `if args.cmd == "collect":` 整个分支替换成：

```python
    if args.cmd == "collect":
        from jobstar.collector.boss import CollectError, LoginRequired
        from jobstar.config import set_setting
        from jobstar.pipeline import run_collect

        try:
            report = run_collect(
                conn, keyword=args.keyword, city_code=args.city, pages=args.pages
            )
        except (LoginRequired, CollectError) as exc:
            set_setting(conn, "last_collect_error", str(exc))
            set_setting(conn, "last_collect_at", None)
            print(f"采集中止：{exc}", file=sys.stderr)
            return 2

        summary = (
            f"列表 {report.listed} 条，新增 {report.new}，"
            f"预门禁刷掉 {report.gated_out}，抓详情 {report.detail_fetched}"
        )
        print(summary)
        for err in report.errors:
            print(f"  ! {err}", file=sys.stderr)

        from datetime import datetime

        set_setting(conn, "last_collect_at", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        if report.errors:
            set_setting(
                conn,
                "last_collect_error",
                f"{len(report.errors)} 个岗位抓取失败（可能是页面结构变更）："
                + "；".join(report.errors[:3]),
            )
        elif report.listed == 0:
            set_setting(
                conn,
                "last_collect_error",
                "列表页返回 0 条。检查登录态，或 boss.SELECTORS 是否需要重新校准。",
            )
        else:
            set_setting(conn, "last_collect_error", None)
        return 0
```

- [ ] **Step 5: 改 panel/app.py**

把 `health` 函数替换成：

```python
@app.get("/api/health")
def health(conn: sqlite3.Connection = Depends(get_db)) -> dict:
    from datetime import datetime, timedelta

    failed = conn.execute(
        "SELECT COUNT(*) AS n FROM scores WHERE error IS NOT NULL"
    ).fetchone()["n"]
    tomorrow = (datetime.now() + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return {
        "remaining_quota": actions.remaining_quota(conn),
        "sent_today": actions.sent_today(conn),
        "daily_limit": get_setting(conn, "daily_greeting_limit"),
        "quota_resets_at": tomorrow.strftime("%Y-%m-%d %H:%M:%S"),
        "score_threshold": get_setting(conn, "score_threshold"),
        "scoring_failed": failed,
        "pending": len(actions.list_by_status(conn, actions.PENDING)),
        "last_collect_error": get_setting(conn, "last_collect_error"),
        "last_collect_at": get_setting(conn, "last_collect_at"),
    }


@app.post("/api/health/clear-error")
def clear_collect_error(conn: sqlite3.Connection = Depends(get_db)) -> dict:
    set_setting(conn, "last_collect_error", None)
    return {"ok": True}
```

- [ ] **Step 6: 改 panel/static/index.html 的 bar()**

把 `bar()` 函数替换成：

```js
async function bar() {
  const h = await api("/api/health");
  const thr = h.score_threshold === null ? "未设（冷启动期，不生成动作）" : h.score_threshold;
  const lines = [
    `今日已发 ${h.sent_today}/${h.daily_limit}，剩余 ${h.remaining_quota}` +
      `（${esc(h.quota_resets_at)} 重置）　|　待确认 ${h.pending}　|　阈值 ${esc(thr)}` +
      (h.scoring_failed ? `　|　<b class="gap">打分失败 ${h.scoring_failed} 条待人工处理</b>` : ""),
  ];
  if (h.last_collect_error) {
    lines.push(
      `<b class="gap">⚠️ 采集异常：${esc(h.last_collect_error)}</b> ` +
      `<button id="clear-err">知道了</button>`
    );
  } else if (h.last_collect_at) {
    lines.push(`<span class="meta">上次采集 ${esc(h.last_collect_at)}</span>`);
  }
  $("#bar").innerHTML = lines.join("<br>");
  const btn = $("#clear-err");
  if (btn) btn.onclick = async () => { await api("/api/health/clear-error", { method: "POST" }); bar(); };
}
```

- [ ] **Step 7: 跑全套回归**

Run: `uv run pytest -v`
Expected: 全绿，比 Task 12 多 5 个用例

- [ ] **Step 8: 提交**

```bash
git add src/jobstar/config.py src/jobstar/cli.py src/jobstar/panel tests/test_panel.py tests/test_config.py
git commit -m "feat: 面板顶部采集健康横幅与配额重置时间"
```

---

## 设计文档覆盖对照

| 设计文档章节 | 落在哪个任务 |
|---|---|
| §4.1 采集器（两阶段、去重） | Task 9 + Task 11（预门禁） |
| §4.2 归一化器 | Task 5 |
| §4.3 硬门禁（5 条规则 + 拒绝原因落库） | Task 4 |
| §4.4 能力卡片库（证据强度、EvidenceStore Protocol） | Task 3 + Task 6（约束在代码层执行） |
| §4.5 打分器（维度、证据约束、纯函数） | Task 6 |
| §4.6 话术生成器（JD 关键词钩子、避免模板开头） | Task 7 |
| §4.7 待确认队列（状态机、type 字段预留） | Task 8 |
| §4.8 执行器（配额、长尾延迟、失败不重试） | Task 10 |
| §4.9 Web 面板（四视图） | Task 12 + Task 13 |
| §5.1 拆维度 + 门禁前置 | Task 4 + Task 6 |
| §5.2 不用向量检索（FullDumpStore + 重评估阈值告警） | Task 3 + `jobstar cards` |
| §5.3 阈值冷启动 | Task 12 `/api/threshold` + 运行手册 |
| §5.4 账号风险控制 | Task 9（两阶段）+ Task 10（延迟、配额）+ Task 7（话术） |
| §6 数据模型（8 张表、scorer_version） | Task 1 |
| §7 错误处理（6 行） | 登录态 Task 9/13、结构变更 Task 9/13、LLM schema Task 2/6、发送失败 Task 10、配额 Task 10/13、无证据维度 Task 6 |
| §8 测试策略 | 每个任务的 TDD 步骤 + Task 12 阈值视图作回归集 |
| §9 流 B / 流 C | 明确不做；`actions.type` 字段已预留 |


---

## 冷启动运行手册（交付后第一周）

代码全部跑通后，按设计文档 §5.3 走这个流程：

1. **确认阈值为空**：`uv run jobstar serve` 看顶部横幅是「阈值：未设」。这一周一条消息都不会发。
2. **每天采集 + 打分**：
   ```bash
   uv run jobstar collect --keyword "解决方案架构师" --city 101210100 --pages 3
   uv run jobstar collect --keyword "AI 后端" --city 101210100 --pages 3
   uv run jobstar score
   ```
3. **在面板「标注」视图用 y / n 标 30-50 条**。这批标注同时是回归测试集。
4. **看「阈值」视图反推阈值**：会投组的分数下界和不会投组的上界之间取值。两组大量重叠说明某维度权重过高，去设置里调 `dimension_weights` 后重跑 `jobstar score`（先 `DELETE FROM scores` 清掉旧分）。
5. **定好阈值后**在设置里把 `score_threshold` 从 `null` 改成数字，此后打分会自动生成待确认动作。
6. **确认后发送**：面板里逐条点「确认发送」，然后 `uv run jobstar send`。

**回归纪律**：此后任何 prompt 或权重改动，把 `scorer_version` bump 一位，对这批已标注岗位重跑打分，一致率上升才允许合入。这是防止系统越调越差的唯一机制。

## 已知的未验证风险

- **Boss 登录态有效期未实测**（设计文档 §5.4）。`LoginRequired` 会在采集时抛出并写进 `last_collect_error`，面板顶部横幅会显示（Task 13）。但这是**被动**告知 —— 只有真的去采集才发现登录已失效，没有主动的活体健康检查。若第一周发现扫码频率高得离谱，再考虑加定时探测。
- **`SELECTORS` 是页面快照的产物**，Boss 改版会让采集静默返回 0 条。Task 9 Step 8 的冒烟命令是唯一的早期信号，建议每次采集前先跑一次。
- **claude CLI 后端每次固定烧约 23K token 上下文**。批量跑几百个岗位时换成 gateway 后端更划算 —— 改 `.env` 里一个字段即可。
