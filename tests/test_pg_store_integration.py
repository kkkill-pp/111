"""PostgreSQL 15+ psycopg Persistence & RLS Integration Tests.

- Offline structural tests ALWAYS run (verifying create_app backend factory, /healthz, and RLS role enforcement).
- Live PostgreSQL 15 tests run automatically whenever DATABASE_URL is set (e.g., GitHub Actions CI PG15 container).
"""
from __future__ import annotations

import os
import tempfile
import unittest
from uuid import uuid4

from fastapi.testclient import TestClient

from apps.api.main import create_app
from services.infrastructure.errors import AppError
from services.infrastructure.pg_store import PostgresTenantStore, create_tenant_store
from services.infrastructure.store import TenantContext, iso_now


class PostgresStoreOfflineUnitTest(unittest.TestCase):
    def test_factory_selects_postgresql_when_dsn_provided_and_in_memory_otherwise(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store_mem, kind_mem = create_tenant_store(tmp, database_url="")
            self.assertEqual(kind_mem, "in_memory")
            store_pg, kind_pg = create_tenant_store(tmp, database_url="postgresql://postgres:pw@localhost:5432/aivs")
            self.assertEqual(kind_pg, "postgresql")
            self.assertIsInstance(store_pg, PostgresTenantStore)
            self.assertTrue(store_pg.use_app_runtime_role)

    def test_healthz_reports_storage_backend(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(media_root=tmp, database_url="")
            r = TestClient(app).get("/healthz")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()["storage_backend"], "in_memory")


@unittest.skipUnless(
    os.environ.get("DATABASE_URL", "").startswith(("postgres://", "postgresql://")),
    "Set DATABASE_URL to run live PostgreSQL 15+ psycopg persistence & RLS tests",
)
class PostgresStoreLivePG15Test(unittest.TestCase):
    def test_live_pg15_rls_isolation_render_plan_immutability_and_atomic_token(self) -> None:
        dsn = os.environ["DATABASE_URL"]
        with tempfile.TemporaryDirectory() as tmp:
            pg = PostgresTenantStore(database_url=dsn, media_root=tmp, use_app_runtime_role=True)

            t_a, u_a = str(uuid4()), str(uuid4())
            t_b, u_b = str(uuid4()), str(uuid4())
            pg.seed_tenant_and_user(t_a, u_a, "PG Live Tenant A", f"a-{u_a[:6]}@example.com")
            pg.seed_tenant_and_user(t_b, u_b, "PG Live Tenant B", f"b-{u_b[:6]}@example.com")

            ctx_a = TenantContext(tenant_id=t_a, user_id=u_a, role="owner", trace_id="tr-pg-a")
            ctx_b = TenantContext(tenant_id=t_b, user_id=u_b, role="owner", trace_id="tr-pg-b")

            # 1. Persist Workspace, Project, Timeline v1, RenderPlan & AuditEvent under app_runtime + RLS
            ws_id, proj_id, tl_id = str(uuid4()), str(uuid4()), str(uuid4())
            pg.put("workspaces", {"id": ws_id, "tenant_id": t_a, "name": "PG WS A", "created_by": u_a})
            pg.put("projects", {"id": proj_id, "tenant_id": t_a, "workspace_id": ws_id, "name": "PG Proj A", "created_by": u_a})
            pg.put("timelines", {"id": tl_id, "tenant_id": t_a, "project_id": proj_id, "current_version": 1, "profile_json": {"width": 1080, "height": 1920}, "created_by": u_a})
            pg.put("timeline_versions", {
                "id": str(uuid4()), "tenant_id": t_a, "timeline_id": tl_id, "version": 1,
                "origin_command": "init", "snapshot_json": {"schema_version": "3.0", "version": 1},
                "snapshot_checksum": "a" * 64, "created_by_type": "user", "created_by_id": u_a, "undo_scope": "user_edit",
            })
            pg.add_audit(ctx_a, "user", u_a, "timeline.created", "timeline", tl_id, {"version": 1})

            # 2. Verify Tenant A reads back its project, while Tenant B gets 404 (real PG15 FORCE RLS!)
            got_a = pg.get(ctx_a, "projects", proj_id)
            self.assertEqual(got_a["name"], "PG Proj A")
            with self.assertRaises(AppError) as err_b:
                pg.get(ctx_b, "projects", proj_id)
            self.assertEqual(err_b.exception.status_code, 404)

            # 3. Verify RenderPlan DB immutability trigger (Risk #3) rejects UPDATE in real PG15
            rp_id = str(uuid4())
            pg.put("render_plans", {
                "id": rp_id, "tenant_id": t_a, "project_id": proj_id, "timeline_id": tl_id,
                "timeline_version": 1, "render_mode": "final", "plan_json": {"render_plan_version": "3.0"},
                "plan_checksum": "b" * 64, "created_by": u_a,
            })
            with self.assertRaises(Exception):
                with pg._tenant_tx(t_a) as cur:
                    cur.execute("UPDATE public.render_plans SET render_mode = 'preview' WHERE id = %s", (rp_id,))

            # 4. Verify real DB consume_approval_token_for_timeline() single-use atomic consumption + replay rejection
            prop_id, rev_id = str(uuid4()), str(uuid4())
            prop_chk = "c" * 64
            pg.put("edit_proposals", {
                "id": prop_id, "tenant_id": t_a, "timeline_id": tl_id, "base_version": 1,
                "status": "approved", "actor": {"type": "agent", "id": "agent-1"},
                "originating_user_id": u_a, "rationale": "live pg test", "risk_level": "low",
                "proposal_checksum": prop_chk, "agent_permission_level": "L1",
            })
            pg.put("human_reviews", {
                "id": rev_id, "tenant_id": t_a, "project_id": proj_id, "subject_type": "edit_proposal",
                "subject_id": prop_id, "subject_version": "1", "subject_checksum": prop_chk,
                "status": "approved", "decision": "approve", "reviewer_id": u_a,
                "requested_by_type": "user", "requested_by_id": u_a, "decided_at": iso_now(),
            })
            raw_tok = pg.issue_approval_token(ctx_a, rev_id, "edit_proposal", prop_id, "1", prop_chk, project_id=proj_id)
            consumed_rev = pg.consume_approval_token(ctx_a, tl_id, prop_id, 1, prop_chk, raw_tok)
            self.assertEqual(consumed_rev, rev_id)

            # Second consumption (replay) MUST fail 409!
            with self.assertRaises(AppError) as replay_err:
                pg.consume_approval_token(ctx_a, tl_id, prop_id, 1, prop_chk, raw_tok)
            self.assertEqual(replay_err.exception.status_code, 409)


if __name__ == "__main__":
    unittest.main()
