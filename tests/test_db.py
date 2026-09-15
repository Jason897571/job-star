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
