"""Visibility bootstrap for shadow grading.

Shadow corpora ingest code/doc chunks with ``derived_sensitivity='internal'``.
Atlas retrieval correctly filters those rows unless the querying principal has
an active role. Fresh shadow orgs therefore need one runner role grant for the
configured ``default_principal_id`` before ``find_code`` / ``scan_search`` can
see any code evidence.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Optional


_ROLE_NAME = "shadow_runner"
_ROLE_DESCRIPTION = "Atlas shadow default runner visibility role."
_DEFAULT_END = "9999-12-31 23:59:59.999999+00"


@dataclass(frozen=True)
class VisibilityBootstrapResult:
    org_id: str
    principal_id: str
    role_id: str
    role_name: str
    active_grant_count: int
    db_url_var: str


def _db_url_from_env() -> tuple[str, str]:
    for name in (
        "ATLAS_SHADOW_VISIBILITY_DB_URL",
        "ATLAS_ADMIN_DB_URL",
        "ATLAS_DB_URL",
    ):
        value = os.environ.get(name)
        if value:
            return name, value
    raise RuntimeError(
        "No Atlas DB URL found; set ATLAS_SHADOW_VISIBILITY_DB_URL, "
        "ATLAS_ADMIN_DB_URL, or ATLAS_DB_URL."
    )


def _deterministic_role_id(org_id: str, principal_id: str) -> str:
    return str(uuid.uuid5(uuid.UUID(org_id), f"atlas-shadow-runner:{principal_id}"))


def ensure_shadow_runner_visibility(
    *,
    org_id: str,
    principal_id: Optional[str],
    connect: Optional[Callable[..., Any]] = None,
) -> VisibilityBootstrapResult:
    """Create/update the shadow runner role and grant it to principal.

    Idempotent by construction:
      - role_id is deterministic from ``(org_id, principal_id)``;
      - roles upsert by role_id;
      - active grant insert is skipped if an active grant already exists.
    """
    if not principal_id:
        raise ValueError("default_principal_id is required for visibility bootstrap")

    db_url_var, db_url = _db_url_from_env()
    role_id = _deterministic_role_id(org_id, principal_id)

    if connect is None:
        import psycopg2  # type: ignore

        connect = psycopg2.connect

    conn = connect(db_url)
    try:
        conn.autocommit = False
        with conn.cursor() as cur:
            cur.execute('SET LOCAL "app.org_id" = %s', (org_id,))
            cur.execute(
                """
                INSERT INTO roles (role_id, org_id, name, description)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (role_id) DO UPDATE SET
                    name = EXCLUDED.name,
                    description = EXCLUDED.description
                """,
                (role_id, org_id, _ROLE_NAME, _ROLE_DESCRIPTION),
            )
            cur.execute(
                """
                INSERT INTO principal_roles (
                    org_id, principal_id, role_id, valid_period, assertion_fact_id
                )
                SELECT %s, %s, %s, tstzrange(NOW(), %s::timestamptz, '[)'), NULL
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM principal_roles
                    WHERE org_id = %s
                      AND principal_id = %s
                      AND role_id = %s
                      AND valid_period @> NOW()
                )
                """,
                (
                    org_id,
                    principal_id,
                    role_id,
                    _DEFAULT_END,
                    org_id,
                    principal_id,
                    role_id,
                ),
            )
            cur.execute(
                """
                SELECT COUNT(*)::int
                FROM principal_roles
                WHERE org_id = %s
                  AND principal_id = %s
                  AND role_id = %s
                  AND valid_period @> NOW()
                """,
                (org_id, principal_id, role_id),
            )
            row = cur.fetchone()
            active_count = int(row[0] if row else 0)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return VisibilityBootstrapResult(
        org_id=org_id,
        principal_id=principal_id,
        role_id=role_id,
        role_name=_ROLE_NAME,
        active_grant_count=active_count,
        db_url_var=db_url_var,
    )
