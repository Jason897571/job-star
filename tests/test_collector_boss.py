import subprocess

import pytest

from jobstar.collector.boss import (
    CollectError,
    _looks_like_empty_result,
    run_script,
    save_detail,
)
from jobstar.db import get_conn, init_db


def test_run_script_wraps_timeout_into_collect_error(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/browser-harness")

    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="browser-harness", timeout=180)

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(CollectError):
        run_script("print(1)")


def test_looks_like_empty_result_matches_known_markers():
    assert _looks_like_empty_result("换个搜索词试试，或者看看推荐职位")
    assert not _looks_like_empty_result("后端开发工程师 15-30K")


def _seed_job(conn, *, platform, job_id):
    conn.execute(
        "INSERT INTO jobs (platform, job_id, title, company, raw_jd, hr_name, salary_raw) "
        "VALUES (?, ?, 'title', 'company', '', '', '')",
        (platform, job_id),
    )
    conn.commit()


def test_save_detail_scopes_update_by_platform(tmp_path):
    conn = get_conn(tmp_path / "t.db")
    init_db(conn)
    _seed_job(conn, platform="boss", job_id="shared-id")
    _seed_job(conn, platform="other", job_id="shared-id")

    save_detail(conn, "shared-id", {"raw_jd": "真实 JD", "hr_name": "薛先生", "salary_raw": "15-30K"})

    boss_row = conn.execute(
        "SELECT raw_jd, hr_name, salary_raw FROM jobs WHERE platform = 'boss' AND job_id = 'shared-id'"
    ).fetchone()
    other_row = conn.execute(
        "SELECT raw_jd, hr_name, salary_raw FROM jobs WHERE platform = 'other' AND job_id = 'shared-id'"
    ).fetchone()

    assert boss_row["raw_jd"] == "真实 JD"
    assert boss_row["hr_name"] == "薛先生"
    assert boss_row["salary_raw"] == "15-30K"
    assert other_row["raw_jd"] == ""
    assert other_row["hr_name"] == ""
    assert other_row["salary_raw"] == ""


def test_save_detail_does_not_overwrite_real_values_with_empty_string(tmp_path):
    conn = get_conn(tmp_path / "t.db")
    init_db(conn)
    conn.execute(
        "INSERT INTO jobs (platform, job_id, title, company, raw_jd, hr_name, salary_raw) "
        "VALUES ('boss', 'j1', 'title', 'company', '旧 JD', '薛先生', '15-30K')"
    )
    conn.commit()

    save_detail(conn, "j1", {"raw_jd": "新 JD", "hr_name": "", "salary_raw": ""})

    row = conn.execute(
        "SELECT raw_jd, hr_name, salary_raw FROM jobs WHERE job_id = 'j1'"
    ).fetchone()
    assert row["raw_jd"] == "新 JD"
    assert row["hr_name"] == "薛先生"
    assert row["salary_raw"] == "15-30K"
