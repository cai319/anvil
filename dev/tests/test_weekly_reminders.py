"""每日复盘 reminder：旧角色启动回填、旧周复盘迁移、删除角色清理。"""
from __future__ import annotations

import pytest

from anvil import config
from anvil.db import connect
from anvil.services import roles as roles_service


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path / "agents")
    c = connect(str(tmp_path / "roles.db"))
    yield c
    c.close()


def _daily_count(conn, slug: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) AS n FROM reminders WHERE role_slug=? AND kind='cron' "
        "AND prompt=?", (slug, roles_service.DAILY_REVIEW_PROMPT)).fetchone()["n"]


def test_backfill_daily_reminder_for_existing_roles(conn):
    roles_service.create(conn, slug="dev", display_name="Dev",
                         definition={"runtime": {"id": "claude-code"}})
    # 模拟旧角色：创建时还没有复盘 reminder
    conn.execute("DELETE FROM reminders WHERE role_slug='dev'")
    conn.commit()
    assert _daily_count(conn, "dev") == 0

    assert roles_service.ensure_daily_review_reminders(conn) == 1
    assert _daily_count(conn, "dev") == 1
    row = conn.execute(
        "SELECT cron_expr FROM reminders WHERE role_slug='dev'").fetchone()
    assert row["cron_expr"] == roles_service.DAILY_REVIEW_CRON
    # 幂等
    assert roles_service.ensure_daily_review_reminders(conn) == 0
    assert _daily_count(conn, "dev") == 1


def test_weekly_review_is_migrated_to_daily(conn):
    """旧周复盘（周一 09:00 + 旧文案）应升级为每天 09:00。"""
    roles_service.create(conn, slug="dev", display_name="Dev",
                         definition={"runtime": {"id": "claude-code"}})
    conn.execute(
        "UPDATE reminders SET cron_expr='0 9 * * 1', "
        "prompt='回顾本周完成的任务与 notes，旧版文案。' WHERE role_slug='dev'")
    conn.commit()
    assert roles_service.ensure_daily_review_reminders(conn) == 0
    row = conn.execute(
        "SELECT cron_expr, prompt FROM reminders WHERE role_slug='dev'").fetchone()
    assert row["cron_expr"] == roles_service.DAILY_REVIEW_CRON
    assert row["prompt"] == roles_service.DAILY_REVIEW_PROMPT


def test_daily_reminder_dedupes_old_prompt_versions(conn):
    """prompt 文案演进后不能让同一角色出现两条复盘 reminder。"""
    roles_service.create(conn, slug="dev", display_name="Dev",
                         definition={"runtime": {"id": "claude-code"}})
    conn.execute(
        "INSERT INTO reminders (id, role_slug, kind, cron_expr, prompt, status, "
        "next_fire_at, created_at) VALUES (?,?,?,?,?,?,?,?)",
        ("old-daily", "dev", "cron", "0 9 * * *",
         "回顾本周完成的任务与 notes，旧版文案。", "pending",
         "2026-10-26T09:00:00+08:00", "2026-10-19T00:00:00+00:00"),
    )
    conn.commit()
    assert roles_service.ensure_daily_review_reminders(conn) == 0
    rows = conn.execute(
        "SELECT prompt FROM reminders WHERE role_slug='dev' AND kind='cron'").fetchall()
    assert len(rows) == 1
    assert rows[0]["prompt"] == roles_service.DAILY_REVIEW_PROMPT


def test_backfill_preserves_custom_schedule(conn):
    """用户把复盘改成其它时间后，启动回填不能按默认 cron 再建一条。"""
    roles_service.create(conn, slug="dev", display_name="Dev",
                         definition={"runtime": {"id": "claude-code"}})
    conn.execute(
        "UPDATE reminders SET cron_expr='0 10 * * 2' "
        "WHERE role_slug='dev' AND prompt LIKE '回顾最近%'")
    conn.commit()
    assert roles_service.ensure_daily_review_reminders(conn) == 0
    rows = conn.execute(
        "SELECT cron_expr FROM reminders WHERE role_slug='dev'").fetchall()
    assert len(rows) == 1
    assert rows[0]["cron_expr"] == "0 10 * * 2"


def test_delete_role_removes_its_reminders(conn):
    roles_service.create(conn, slug="dev", display_name="Dev",
                         definition={"runtime": {"id": "claude-code"}})
    assert _daily_count(conn, "dev") == 1
    roles_service.delete(conn, "dev")
    assert _daily_count(conn, "dev") == 0
