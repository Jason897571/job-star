import pytest
from fastapi.testclient import TestClient

from jobstar.actions import PENDING, SENDING, SKIPPED, approve, enqueue, mark_sending
from jobstar.config import SETTING_DEFAULTS
from jobstar.db import get_conn, init_db
from jobstar.panel.app import PANEL_HEADER, app, get_db

# 面板自己的 JS（见 static/index.html 的 api() 助手）在每个请求上都带这个
# 头；下面除了专门测试「没带头会被拒」的用例外，都用它模拟面板本身发出
# 的请求。
PANEL_HEADERS = {PANEL_HEADER: "1"}


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
        f"/api/actions/{action_id}/approve",
        json={"greeting": "改写后的稿子"},
        headers=PANEL_HEADERS,
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
    client.post(f"/api/actions/{action_id}/skip", headers=PANEL_HEADERS)
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
    resp = client.post(
        f"/api/actions/{action_id}/approve", json={}, headers=PANEL_HEADERS
    )
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
    client.post(
        "/api/label", json={"job_id": "j1", "would_apply": True}, headers=PANEL_HEADERS
    )
    assert client.get("/api/label/next").json() == {"job_id": None}


def test_label_is_idempotent(client):
    client.post(
        "/api/label", json={"job_id": "j1", "would_apply": True}, headers=PANEL_HEADERS
    )
    client.post(
        "/api/label", json={"job_id": "j1", "would_apply": False}, headers=PANEL_HEADERS
    )
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
    client.post(
        "/api/label", json={"job_id": "j1", "would_apply": True}, headers=PANEL_HEADERS
    )
    client.post(
        "/api/label", json={"job_id": "j2", "would_apply": False}, headers=PANEL_HEADERS
    )
    body = client.get("/api/threshold").json()
    assert body["would_apply"]["count"] == 1
    assert body["would_apply"]["min"] == 82.0
    assert body["would_not_apply"]["max"] == 41.0
    assert body["labeled_total"] == 2


def test_settings_roundtrip(client):
    client.put(
        "/api/settings",
        json={"daily_greeting_limit": 10, "score_threshold": 70},
        headers=PANEL_HEADERS,
    )
    body = client.get("/api/settings").json()
    assert body["daily_greeting_limit"] == 10
    assert body["score_threshold"] == 70


def test_settings_rejects_unknown_key(client):
    resp = client.put(
        "/api/settings", json={"不存在的项": 1}, headers=PANEL_HEADERS
    )
    assert resp.status_code == 400


# --- 下面这些是本任务针对 SENDING 状态新增的测试。任务简报写于 SENDING
# 状态引入之前，不知道执行器会把 approved 原子认领成 sending；下面覆盖
# 简报没有覆盖到的后果：面板对「已被执行器认领」的行必须给出人话提示，
# 而不是把 InvalidTransition 的内部措辞直接透传成 409；「发送结果不确定」
# 的行必须在面板上可见，而不是悄悄挂在 sending 状态里没人知道。


def test_skip_on_sending_row_gives_friendly_message(client):
    """人工点「跳过」时这一行已经被执行器认领成 sending（例如两个人同时
    开着面板，或执行器恰好在人工点击前抢先跑了一轮）。这不该是一条看不懂
    的「动作 X 不能从 sending 变成 skipped」，而是清楚地说「来不及」。"""
    action_id = enqueue(
        client.conn, type="send_greeting", job_id="j1", payload={"greeting": "稿"}
    )
    approve(client.conn, action_id)
    mark_sending(client.conn, action_id)
    resp = client.post(f"/api/actions/{action_id}/skip", headers=PANEL_HEADERS)
    assert resp.status_code == 409
    assert "来不及跳过" in resp.json()["detail"]
    row = client.conn.execute(
        "SELECT status FROM actions WHERE id=?", (action_id,)
    ).fetchone()
    assert row["status"] == SENDING


def test_approve_on_sending_row_gives_friendly_message(client):
    """同样地，对一行已经在 sending 的动作重新点「确认」也必须给人话提示——
    sending 不允许被 approve 认领（见 actions._ALLOWED_FROM），这是故意的：
    这一行的发送结果还不确定，不能允许人工趁着这个窗口再批准一次，酿成
    执行器和人工都以为自己是「第一次」批准同一条消息。"""
    action_id = enqueue(
        client.conn, type="send_greeting", job_id="j1", payload={"greeting": "稿"}
    )
    approve(client.conn, action_id)
    mark_sending(client.conn, action_id)
    resp = client.post(
        f"/api/actions/{action_id}/approve", json={}, headers=PANEL_HEADERS
    )
    assert resp.status_code == 409
    assert "来不及" in resp.json()["detail"]


def test_health_reports_uncertain_count(client):
    """「消息可能已发出，请人工到 Boss 对话列表核实」的行（sending + error）
    必须被面板统计出来，不能在队列视图消失后就没人知道。"""
    action_id = enqueue(
        client.conn, type="send_greeting", job_id="j1", payload={"greeting": "稿"}
    )
    approve(client.conn, action_id)
    mark_sending(client.conn, action_id)
    client.conn.execute(
        "UPDATE actions SET error=? WHERE id=?",
        ("消息可能已发出，请人工到 Boss 对话列表核实后再决定", action_id),
    )
    client.conn.commit()
    body = client.get("/api/health").json()
    assert body["uncertain"] == 1


def test_queue_surfaces_uncertain_rows_separately_from_items(client):
    """待确认队列不该把「发送结果不确定」的行当成待确认（它们既不是
    pending，也不应该被允许重新批准/跳过）；但也不能对人工完全隐藏——
    面板必须给出岗位信息和错误提示，让人工能点开原页面去 Boss 上核实。"""
    uncertain_id = enqueue(
        client.conn, type="send_greeting", job_id="j1", payload={"greeting": "稿A"}
    )
    approve(client.conn, uncertain_id)
    mark_sending(client.conn, uncertain_id)
    client.conn.execute(
        "UPDATE actions SET error=? WHERE id=?",
        ("消息可能已发出，请人工到 Boss 对话列表核实后再决定", uncertain_id),
    )
    client.conn.commit()

    client.conn.execute(
        "INSERT INTO jobs (platform, job_id, title, company, raw_jd, status) "
        "VALUES ('boss','j2','另一个岗','B公司','JD','scored')"
    )
    client.conn.commit()
    pending_id = enqueue(
        client.conn, type="send_greeting", job_id="j2", payload={"greeting": "稿B"}
    )

    body = client.get("/api/queue").json()
    assert [i["action_id"] for i in body["items"]] == [pending_id]

    assert len(body["uncertain"]) == 1
    row = body["uncertain"][0]
    assert row["action_id"] == uncertain_id
    assert row["job_id"] == "j1"
    assert row["title"] == "AI 后端工程师"
    assert row["url"] == "https://x/job_detail/j1~.html"
    assert "人工到 Boss 对话列表核实" in row["error"]


# --- review finding 1：CSRF 防护。面板绑定在 127.0.0.1，挡得住远程攻击者，
# 但挡不住同一浏览器里另一个页面发起的跨源简单请求（不带自定义头的
# fetch 不需要 CORS 预检）。下面证明每条变更状态的路由都要求
# X-Jobstar-Panel 头，并且请求即使带了这个头，若 Origin 头存在且不是本
# 面板自己的源，也照样被拒。


def test_approve_without_panel_header_is_rejected_and_row_unchanged(client):
    action_id = enqueue(
        client.conn, type="send_greeting", job_id="j1", payload={"greeting": "稿"}
    )
    resp = client.post(f"/api/actions/{action_id}/approve", json={})
    assert resp.status_code == 403
    row = client.conn.execute(
        "SELECT status FROM actions WHERE id=?", (action_id,)
    ).fetchone()
    assert row["status"] == PENDING


def test_skip_without_panel_header_is_rejected_and_row_unchanged(client):
    action_id = enqueue(
        client.conn, type="send_greeting", job_id="j1", payload={"greeting": "稿"}
    )
    resp = client.post(f"/api/actions/{action_id}/skip")
    assert resp.status_code == 403
    row = client.conn.execute(
        "SELECT status FROM actions WHERE id=?", (action_id,)
    ).fetchone()
    assert row["status"] == PENDING


def test_label_without_panel_header_is_rejected(client):
    resp = client.post("/api/label", json={"job_id": "j1", "would_apply": True})
    assert resp.status_code == 403
    row = client.conn.execute(
        "SELECT * FROM labels WHERE job_id='j1'"
    ).fetchone()
    assert row is None


def test_settings_without_panel_header_is_rejected(client):
    resp = client.put("/api/settings", json={"daily_greeting_limit": 1})
    assert resp.status_code == 403
    body = client.get("/api/settings").json()
    assert body["daily_greeting_limit"] == SETTING_DEFAULTS["daily_greeting_limit"]


def test_approve_with_panel_header_succeeds(client):
    action_id = enqueue(
        client.conn, type="send_greeting", job_id="j1", payload={"greeting": "稿"}
    )
    resp = client.post(
        f"/api/actions/{action_id}/approve", json={}, headers=PANEL_HEADERS
    )
    assert resp.status_code == 200
    row = client.conn.execute(
        "SELECT status FROM actions WHERE id=?", (action_id,)
    ).fetchone()
    assert row["status"] == "approved"


def test_mutating_request_with_foreign_origin_is_rejected_even_with_header(client):
    action_id = enqueue(
        client.conn, type="send_greeting", job_id="j1", payload={"greeting": "稿"}
    )
    resp = client.post(
        f"/api/actions/{action_id}/approve",
        json={},
        headers={**PANEL_HEADERS, "origin": "https://evil.example"},
    )
    assert resp.status_code == 403
    row = client.conn.execute(
        "SELECT status FROM actions WHERE id=?", (action_id,)
    ).fetchone()
    assert row["status"] == PENDING


def test_mutating_request_with_own_origin_is_accepted(client):
    action_id = enqueue(
        client.conn, type="send_greeting", job_id="j1", payload={"greeting": "稿"}
    )
    resp = client.post(
        f"/api/actions/{action_id}/approve",
        json={},
        headers={**PANEL_HEADERS, "origin": "http://testserver"},
    )
    assert resp.status_code == 200


# --- review finding 2：PUT /api/settings 只校验键名认不认识，从不校验值的
# 形状。下面证明每个坏值都在写入时被 400 挡下，并且命名了出问题的键；
# 同时证明现有测试用过的合法值都还能正常写入。


def test_settings_rejects_non_mapping_dimension_weights(client):
    resp = client.put(
        "/api/settings", json={"dimension_weights": "oops"}, headers=PANEL_HEADERS
    )
    assert resp.status_code == 400
    assert "dimension_weights" in resp.json()["detail"]
    body = client.get("/api/settings").json()
    assert body["dimension_weights"] == SETTING_DEFAULTS["dimension_weights"]


def test_settings_rejects_dimension_weights_missing_a_dimension(client):
    incomplete = dict(SETTING_DEFAULTS["dimension_weights"])
    del incomplete["bonus"]
    resp = client.put(
        "/api/settings",
        json={"dimension_weights": incomplete},
        headers=PANEL_HEADERS,
    )
    assert resp.status_code == 400
    assert "dimension_weights" in resp.json()["detail"]


def test_settings_rejects_dimension_weights_with_non_numeric_value(client):
    bad = dict(SETTING_DEFAULTS["dimension_weights"])
    bad["skills"] = "很多"
    resp = client.put(
        "/api/settings", json={"dimension_weights": bad}, headers=PANEL_HEADERS
    )
    assert resp.status_code == 400
    assert "dimension_weights" in resp.json()["detail"]


def test_settings_rejects_all_zero_dimension_weights(client):
    zeros = {d: 0 for d in SETTING_DEFAULTS["dimension_weights"]}
    resp = client.put(
        "/api/settings", json={"dimension_weights": zeros}, headers=PANEL_HEADERS
    )
    assert resp.status_code == 400
    assert "dimension_weights" in resp.json()["detail"]


def test_settings_rejects_out_of_range_score_threshold(client):
    resp = client.put(
        "/api/settings", json={"score_threshold": 150}, headers=PANEL_HEADERS
    )
    assert resp.status_code == 400
    assert "score_threshold" in resp.json()["detail"]


def test_settings_rejects_negative_daily_greeting_limit(client):
    resp = client.put(
        "/api/settings", json={"daily_greeting_limit": -1}, headers=PANEL_HEADERS
    )
    assert resp.status_code == 400
    assert "daily_greeting_limit" in resp.json()["detail"]


def test_settings_rejects_gate_rules_with_bad_city_whitelist(client):
    bad_rules = {**SETTING_DEFAULTS["gate_rules"], "city_whitelist": "杭州"}
    resp = client.put(
        "/api/settings", json={"gate_rules": bad_rules}, headers=PANEL_HEADERS
    )
    assert resp.status_code == 400
    assert "gate_rules" in resp.json()["detail"]


def test_settings_still_accepts_legitimate_values(client):
    """覆盖现有测试和场景里实际写过的合法值，确保校验没有误伤它们。"""
    resp = client.put(
        "/api/settings",
        json={
            "daily_greeting_limit": 10,
            "score_threshold": 70,
            "gate_rules": {"city_whitelist": ["杭州", "上海"], "salary_min": 30},
        },
        headers=PANEL_HEADERS,
    )
    assert resp.status_code == 200


# --- Task 13：采集健康横幅。设计文档 §7 的错误处理表要求「面板顶部横幅
# 提示」（登录态失效）和「面板汇总提示」（页面结构变更导致的抓取失败）。
# collect 子命令中止或产生 errors 时，把状态写进 last_collect_error /
# last_collect_at 这两个 setting（Task 13 在 config.py 里补的），/api/health
# 读出来给面板顶部横幅用。


def test_health_surfaces_collect_error(client):
    from jobstar.config import set_setting

    set_setting(client.conn, "last_collect_error", "Boss 登录态失效，采集已中止。")
    set_setting(client.conn, "last_collect_at", "2026-09-14 10:00:00")
    body = client.get("/api/health").json()
    assert "登录态失效" in body["last_collect_error"]
    assert body["last_collect_at"] == "2026-09-14 10:00:00"


def test_health_reports_quota_reset_time(client):
    """quota_resets_at 是下一个本地零点（配额按本地日期计数）。"""
    body = client.get("/api/health").json()
    assert body["quota_resets_at"].endswith("00:00:00")


def test_health_still_reports_uncertain_count_alongside_collect_health(client):
    """/api/health 的改动不能把 Task 12 加的 uncertain 字段挤掉——两者是
    独立的两行面板提示（发送结果不确定 vs 采集异常），横幅要同时显示。"""
    action_id = enqueue(
        client.conn, type="send_greeting", job_id="j1", payload={"greeting": "稿"}
    )
    approve(client.conn, action_id)
    mark_sending(client.conn, action_id)
    client.conn.execute(
        "UPDATE actions SET error=? WHERE id=?",
        ("消息可能已发出，请人工到 Boss 对话列表核实后再决定", action_id),
    )
    client.conn.commit()
    body = client.get("/api/health").json()
    assert body["uncertain"] == 1


def test_clear_error_resets_banner(client):
    """清除按钮是状态变更路由，和 approve/skip/label/settings 一样必须走
    CSRF 依赖——面板自己的 api() 助手已经带了这个头（见 index.html）。"""
    from jobstar.config import set_setting

    set_setting(client.conn, "last_collect_error", "页面结构变更，3 个岗位解析失败")
    resp = client.post("/api/health/clear-error", headers=PANEL_HEADERS)
    assert resp.status_code == 200
    assert client.get("/api/health").json()["last_collect_error"] is None


def test_clear_error_without_panel_header_is_rejected(client):
    from jobstar.config import set_setting

    set_setting(client.conn, "last_collect_error", "页面结构变更，3 个岗位解析失败")
    resp = client.post("/api/health/clear-error")
    assert resp.status_code == 403
    assert (
        client.get("/api/health").json()["last_collect_error"]
        == "页面结构变更，3 个岗位解析失败"
    )


def test_index_renders_banner_placeholder(client):
    assert "last_collect_error" in client.get("/").text
