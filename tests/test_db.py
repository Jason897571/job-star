import sqlite3

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


def test_an_old_database_gets_the_new_columns_added(tmp_path):
    """SCHEMA 里的 `CREATE TABLE IF NOT EXISTS` 对已经存在的表什么都不做。
    区域和坐标是后加的列——不单独补的话，老库升级上来会在第一次查询时炸，
    而这个项目的库是本地唯一一份数据，没有重建的退路。"""
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    # 建一个「加列之前」形状的 jobs 表，并塞一行真实数据
    conn.executescript(
        "CREATE TABLE jobs ("
        " id INTEGER PRIMARY KEY, platform TEXT NOT NULL DEFAULT 'boss',"
        " job_id TEXT NOT NULL, title TEXT NOT NULL, company TEXT NOT NULL,"
        " raw_jd TEXT NOT NULL DEFAULT '', city TEXT, salary_raw TEXT,"
        " hr_name TEXT, url TEXT,"
        " collected_at TEXT NOT NULL DEFAULT (datetime('now')),"
        " detail_fetched INTEGER NOT NULL DEFAULT 0,"
        " status TEXT NOT NULL DEFAULT 'new', UNIQUE (platform, job_id));"
    )
    conn.execute(
        "INSERT INTO jobs (job_id, title, company, city) VALUES ('j1','后端','A','杭州')"
    )
    conn.commit()

    init_db(conn)

    cols = {r["name"] for r in conn.execute("PRAGMA table_info(jobs)")}
    assert {"district", "business_area", "address", "lng", "lat"} <= cols
    row = conn.execute("SELECT * FROM jobs WHERE job_id='j1'").fetchone()
    assert row["title"] == "后端", "既有数据不能被动"
    assert row["district"] is None, "新列对老行是 NULL，不是瞎填的值"

    init_db(conn)  # 再跑一次不该报「列已存在」
    conn.close()
