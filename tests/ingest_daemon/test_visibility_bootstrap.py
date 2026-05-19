from __future__ import annotations

from atlas_shadow.ingest_daemon import entrypoint as entrypoint_mod
from atlas_shadow.ingest_daemon import visibility_bootstrap as vb


class FakeCursor:
    def __init__(self):
        self.statements = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params=None):
        self.statements.append((sql, params))

    def fetchone(self):
        return (1,)


class FakeConn:
    def __init__(self):
        self.cursor_obj = FakeCursor()
        self.committed = False
        self.rolled_back = False
        self.closed = False
        self.autocommit = True

    def cursor(self):
        return self.cursor_obj

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


def test_visibility_bootstrap_upserts_role_and_active_grant(monkeypatch):
    conn = FakeConn()
    monkeypatch.setenv("ATLAS_ADMIN_DB_URL", "postgres://example")

    result = vb.ensure_shadow_runner_visibility(
        org_id="36a5bde2-9de3-4278-b18c-1e2da1916034",
        principal_id="00000000-0000-4000-8000-000000000222",
        connect=lambda url: conn,
    )

    assert result.role_name == "shadow_runner"
    assert result.active_grant_count == 1
    assert result.db_url_var == "ATLAS_ADMIN_DB_URL"
    assert conn.committed is True
    assert conn.closed is True
    sql = "\n".join(stmt for stmt, _ in conn.cursor_obj.statements)
    assert "INSERT INTO roles" in sql
    assert "INSERT INTO principal_roles" in sql
    assert "valid_period @> NOW()" in sql


def test_visibility_bootstrap_requires_principal(monkeypatch):
    monkeypatch.setenv("ATLAS_ADMIN_DB_URL", "postgres://example")
    try:
        vb.ensure_shadow_runner_visibility(
            org_id="36a5bde2-9de3-4278-b18c-1e2da1916034",
            principal_id=None,
            connect=lambda url: FakeConn(),
        )
    except ValueError as exc:
        assert "default_principal_id" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected ValueError")


def test_cmd_bootstrap_visibility_prints_json(monkeypatch, daemon_config, capsys):
    class Result:
        __dict__ = {
            "org_id": daemon_config.continuous_shadow_org_id,
            "principal_id": daemon_config.default_principal_id,
            "role_id": "role-1",
            "role_name": "shadow_runner",
            "active_grant_count": 1,
            "db_url_var": "ATLAS_ADMIN_DB_URL",
        }

    monkeypatch.setattr(
        entrypoint_mod.visibility_bootstrap_mod,
        "ensure_shadow_runner_visibility",
        lambda **kwargs: Result(),
    )

    rc = entrypoint_mod.cmd_bootstrap_visibility(daemon_config)
    assert rc == 0
    out = capsys.readouterr().out
    assert "shadow_runner" in out
    assert "active_grant_count" in out
