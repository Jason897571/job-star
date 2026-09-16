"""面板的能力卡片编辑器（GET/PUT /api/cards）。

这份文件（data/capability_cards.yaml）有两个特殊之处，测试主要盯着它们：
  1. 它是整套系统防止简历吹牛的唯一防线——写进去的东西必须过和启动时
     同一套校验，不能出现「面板存得下、下次启动才炸」的坏卡片；
  2. 它是唯一一份含个人隐私、被 .gitignore 整目录排除的数据，没有 git
     兜底。所以覆盖前必须留备份，写入必须是原子的。
"""

from __future__ import annotations

import yaml
import pytest
from fastapi.testclient import TestClient

from jobstar.db import get_conn, init_db
from jobstar.panel.app import app, get_db

PANEL_HEADERS = {"X-Jobstar-Panel": "1"}

CARD = {
    "id": "cap-rag",
    "capability": "RAG 检索增强问答",
    "synonyms": ["RAG", "检索增强"],
    "strength": "强",
    "project": "某文档问答平台",
    "metrics": ["准确率"],
    "depth": "分块策略到召回调优全链路",
    "resume_versions": ["后端AI版"],
}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    cards_path = tmp_path / "capability_cards.yaml"
    cards_path.write_text(
        yaml.safe_dump(
            [
                {
                    "id": "cap-rag",
                    "能力": "RAG 检索增强问答",
                    "同义表述": ["RAG", "检索增强"],
                    "证据强度": "强",
                    "项目": "某文档问答平台",
                    "可量化": ["准确率"],
                    "可讲深度": "分块策略到召回调优全链路",
                    "关联简历版本": ["后端AI版"],
                }
            ],
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("JOBSTAR_DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("JOBSTAR_CARDS_PATH", str(cards_path))
    conn = get_conn(tmp_path / "t.db")
    init_db(conn)
    app.dependency_overrides[get_db] = lambda: conn
    c = TestClient(app)
    c.conn = conn
    c.cards_path = cards_path
    yield c
    app.dependency_overrides.clear()
    conn.close()


def test_read_cards_returns_ascii_field_names(client):
    body = client.get("/api/cards").json()
    assert body["error"] is None
    assert body["cards"] == [CARD]
    assert body["path"] == str(client.cards_path)


def test_write_cards_round_trips_through_the_file(client):
    two = [CARD, {**CARD, "id": "cap-k8s", "capability": "Kubernetes", "strength": "弱"}]
    resp = client.put("/api/cards", json={"cards": two}, headers=PANEL_HEADERS)
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "count": 2, "strong": 1}
    assert client.get("/api/cards").json()["cards"] == two


def test_written_file_keeps_chinese_keys_and_unescaped_chinese(client):
    """文件要由本人手工逐张校对——中文被转义成 \\uXXXX 就再也读不了了，
    而手工校对正是这份文件存在的理由。"""
    client.put("/api/cards", json={"cards": [CARD]}, headers=PANEL_HEADERS)
    text = client.cards_path.read_text(encoding="utf-8")
    assert "证据强度" in text
    assert "RAG 检索增强问答" in text
    assert "\\u" not in text


@pytest.mark.parametrize(
    "bad,message",
    [
        ([{**CARD, "strength": "很强"}], "证据强度"),
        ([{**CARD, "id": "x"}, {**CARD, "id": "x"}], "重复"),
        ([{**CARD, "synonyms": "不是列表"}], "必须是列表"),
        ([{**CARD, "synonyms": [123]}], "必须是字符串"),
        ([], "空的"),
    ],
)
def test_invalid_cards_are_rejected_and_the_file_is_untouched(client, bad, message):
    """和启动时同一套校验（evidence.validate_cards）。存得下、下次启动才炸
    是最坏的结果——那时人早就忘了自己改过什么。"""
    before = client.cards_path.read_text(encoding="utf-8")
    resp = client.put("/api/cards", json={"cards": bad}, headers=PANEL_HEADERS)
    assert resp.status_code == 400
    assert message in resp.json()["detail"]
    assert client.cards_path.read_text(encoding="utf-8") == before, "被拒绝的写入不该改动文件"


def test_write_leaves_a_timestamped_backup(client):
    """data/ 整个目录不在 git 里，没有版本历史兜底。误删一张卡片、或者
    保存时手滑，除了备份没有别的退路。"""
    client.put("/api/cards", json={"cards": [CARD]}, headers=PANEL_HEADERS)
    backups = list((client.cards_path.parent / "card-backups").glob("*.yaml"))
    assert len(backups) == 1
    assert "RAG 检索增强问答" in backups[0].read_text(encoding="utf-8")


def test_write_does_not_leave_a_temp_file_behind(client):
    """写入走「临时文件 + 原子替换」，成功之后目录里不该留下半截文件。"""
    client.put("/api/cards", json={"cards": [CARD]}, headers=PANEL_HEADERS)
    assert list(client.cards_path.parent.glob("*.tmp")) == []


def test_broken_card_file_is_reported_instead_of_500(client):
    """文件坏掉时如果直接 500，人工就卡在一个「打不开也修不了」的死角里。
    前端靠这个 error 字段禁用保存按钮——否则一个空列表就把 40 张卡片
    覆盖掉了。"""
    client.cards_path.write_text("这不是一个列表", encoding="utf-8")
    body = client.get("/api/cards").json()
    assert body["cards"] == []
    assert "顶层必须是列表" in body["error"]


def test_write_cards_requires_the_panel_header(client):
    """和其他所有变更状态的路由一样：只有面板自己的按钮能改卡片库。"""
    before = client.cards_path.read_text(encoding="utf-8")
    resp = client.put("/api/cards", json={"cards": [CARD]})
    assert resp.status_code == 403
    assert client.cards_path.read_text(encoding="utf-8") == before
