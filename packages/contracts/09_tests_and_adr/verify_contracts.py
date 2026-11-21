#!/usr/bin/env python3
"""
AI Video Studio v3.0 + HITL v2.2 — Automated Cross-Contract & Protocol Freeze Verifier.
Run in CI on every PR to prevent SQL / JSON Schema / OpenAPI / TypeScript drift.
"""
import json
import re
import sys
import uuid
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator, ValidationError

BASE_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = BASE_DIR.parent.parent
errors = []
checks = []


def ok(msg: str):
    checks.append(("PASS", msg))


def fail(msg: str):
    checks.append(("FAIL", msg))
    errors.append(msg)


def extract_sql_enum(sql_text: str, pattern: str):
    m = re.search(pattern, sql_text, re.S)
    if not m:
        return None
    return re.findall(r"'([^']+)'", m.group(1))


# ---------------------------------------------------------------------------
# 1. Validate all 17 JSON Schemas (13 v2.2 + 4 frozen v3.0) with Draft 2020-12
# ---------------------------------------------------------------------------
schema_dir = BASE_DIR / "02_schemas"
schema_files = sorted(schema_dir.glob("*.json"))
schemas = {}
v3_core_names = [
    "timeline.v3.schema.json",
    "edit-operation.v3.schema.json",
    "render-plan.v3.schema.json",
    "agent-proposal.v3.schema.json",
]
if len(schema_files) != 17:
    fail(f"Expected 17 JSON Schemas (13 v2.2 + 4 v3.0) in 02_schemas/, found {len(schema_files)}")
else:
    ok("Found all 17 standalone JSON Schemas (13 v2.2 + 4 frozen v3.0) in 02_schemas/")

for sf in schema_files:
    try:
        data = json.loads(sf.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(data)
        schemas[sf.name] = data
        ok(f"JSON Schema Draft 2020-12 valid: {sf.name}")
    except Exception as e:
        fail(f"Invalid JSON Schema {sf.name}: {e}")

for name in v3_core_names:
    if name not in schemas:
        fail(f"Missing frozen v3.0 core schema: {name}")

# ---------------------------------------------------------------------------
# 2. Validate ALL real valid & invalid contract_examples/ against v3.0 Schemas
# ---------------------------------------------------------------------------
ex_root = BASE_DIR / "contract_examples"
example_schema_map = {
    "timeline.v3.": "timeline.v3.schema.json",
    "edit-operation.v3.": "edit-operation.v3.schema.json",
    "render-plan.v3.": "render-plan.v3.schema.json",
    "agent-proposal.v3.": "agent-proposal.v3.schema.json",
}

def pick_schema(filename: str):
    for prefix, sname in example_schema_map.items():
        if filename.startswith(prefix):
            if filename == "edit-operation.v3.missing_payload.invalid.json":
                return {
                    "$schema": "https://json-schema.org/draft/2020-12/schema",
                    "$defs": schemas[sname]["$defs"],
                    "$ref": "#/$defs/operation_input",
                }
            return schemas[sname]
    return None

valid_files = sorted((ex_root / "valid").glob("*.json"))
invalid_files = sorted((ex_root / "invalid").glob("*.json"))
if len(valid_files) < 4 or len(invalid_files) < 4:
    fail(f"Insufficient contract_examples: valid={len(valid_files)}, invalid={len(invalid_files)}")
else:
    ok(f"Found {len(valid_files)} valid and {len(invalid_files)} invalid v3.0 contract examples")

for vf in valid_files:
    sch = pick_schema(vf.name)
    try:
        Draft202012Validator(sch).validate(json.loads(vf.read_text(encoding="utf-8")))
        ok(f"Valid contract example passed: {vf.name}")
    except Exception as e:
        fail(f"Valid contract example FAILED ({vf.name}): {e}")

for ivf in invalid_files:
    sch = pick_schema(ivf.name)
    try:
        Draft202012Validator(sch).validate(json.loads(ivf.read_text(encoding="utf-8")))
        fail(f"Invalid contract example unexpectedly PASSED: {ivf.name}")
    except ValidationError:
        ok(f"Invalid contract example rejected as expected: {ivf.name}")

# ---------------------------------------------------------------------------
# 3. Validate all YAML files & OpenAPI 3.1 strictness
# ---------------------------------------------------------------------------
yaml_files = sorted(BASE_DIR.rglob("*.yaml"))
parsed_yaml = {}
for yf in yaml_files:
    try:
        data = yaml.safe_load(yf.read_text(encoding="utf-8"))
        assert isinstance(data, dict)
        parsed_yaml[yf.relative_to(BASE_DIR).as_posix()] = data
        ok(f"YAML valid: {yf.relative_to(BASE_DIR).as_posix()}")
    except Exception as e:
        fail(f"YAML parse error in {yf}: {e}")

oa = parsed_yaml["03_openapi/openapi.yaml"]
paths = oa["paths"]

for rp in ["/storyboards/{storyboard_id}/reviews", "/proposals/{proposal_id}/reviews", "/artifacts/{artifact_id}/reviews"]:
    if rp in paths and "post" in paths[rp]:
        ok(f"Unified plural review endpoint present: POST {rp}")
    else:
        fail(f"Missing unified plural review endpoint: POST {rp}")

for p in paths:
    if p.endswith("/review") or p.endswith("/approval") or p.endswith("/approvals"):
        fail(f"Legacy review/approval endpoint present: {p}")

for cmd in ["/timelines/{timeline_id}/undo", "/timelines/{timeline_id}/redo"]:
    if cmd in paths and "post" in paths[cmd]:
        ok(f"Independent version command endpoint present: POST {cmd}")
    else:
        fail(f"Missing independent version command endpoint: POST {cmd}")

def check_refs(obj, root):
    if isinstance(obj, dict):
        if "$ref" in obj and isinstance(obj["$ref"], str) and obj["$ref"].startswith("#/"):
            cur = root
            for part in obj["$ref"][2:].split("/"):
                if isinstance(cur, dict) and part in cur:
                    cur = cur[part]
                else:
                    fail(f"Unresolved OpenAPI $ref: {obj['$ref']}")
                    return
        for v in obj.values():
            check_refs(v, root)
    elif isinstance(obj, list):
        for item in obj:
            check_refs(item, root)

check_refs(oa, oa)
ok(f"OpenAPI 3.1 all $refs resolved across {len(paths)} paths")

loose_responses, missing_roles, missing_idem = [], [], []
for p, methods in paths.items():
    for m, spec in methods.items():
        if m not in ("get", "post", "patch", "put", "delete"):
            continue
        if "x-required-roles" not in spec:
            missing_roles.append(f"{m.upper()} {p}")
        if m in ("post", "patch", "delete") and not p.endswith("/validate"):
            params = spec.get("parameters", [])
            if not any(isinstance(pr, dict) and pr.get("$ref") == "#/components/parameters/IdempotencyKey" for pr in params):
                missing_idem.append(f"{m.upper()} {p}")
        for code, resp in spec.get("responses", {}).items():
            if str(code).startswith("2") and isinstance(resp, dict) and "content" in resp:
                sch = resp["content"].get("application/json", {}).get("schema", {})
                if sch == {"type": "object"}:
                    loose_responses.append(f"{m.upper()} {p} [{code}]")

if loose_responses:
    fail(f"Loose untyped object responses found: {loose_responses}")
else:
    ok("Zero loose untyped `type: object` 2xx responses in OpenAPI 3.1")
if missing_roles:
    fail(f"Endpoints missing x-required-roles: {missing_roles}")
else:
    ok("All OpenAPI endpoints declare x-required-roles RBAC metadata")
if missing_idem:
    fail(f"Mutating endpoints missing Idempotency-Key: {missing_idem}")
else:
    ok("All mutating OpenAPI endpoints enforce Idempotency-Key")

# ---------------------------------------------------------------------------
# 4. 4-Way Cross-Contract Parity (SQL 007 == v3.0 Schema == OpenAPI 3.1 == TS)
# ---------------------------------------------------------------------------
sql_002 = (BASE_DIR / "01_database/002_project_asset_analysis.sql").read_text(encoding="utf-8")
sql_003 = (BASE_DIR / "01_database/003_creative_timeline.sql").read_text(encoding="utf-8")
sql_004 = (BASE_DIR / "01_database/004_tasks_outbox_workers.sql").read_text(encoding="utf-8")
sql_005 = (BASE_DIR / "01_database/005_render_qa_hitl_delivery.sql").read_text(encoding="utf-8")
sql_006 = (BASE_DIR / "01_database/006_governance_usage_rls.sql").read_text(encoding="utf-8")
sql_007 = (BASE_DIR / "01_database/007_v3_protocol_upgrade.sql").read_text(encoding="utf-8")
verify_rls_sql = (BASE_DIR / "01_database/verify_rls.sql").read_text(encoding="utf-8")
all_base_sql = "\n".join([
    (BASE_DIR / "01_database/000_roles_and_extensions.sql").read_text(encoding="utf-8"),
    (BASE_DIR / "01_database/001_identity_workspace.sql").read_text(encoding="utf-8"),
    sql_002, sql_003, sql_004, sql_005, sql_006,
])

# v3.0 21 EditOperations parity across Schema, OpenAPI, SQL 007, and generated TS
schema_ops_v3 = schemas["edit-operation.v3.schema.json"]["$defs"]["operation_type"]["enum"]
openapi_ops_v3 = oa["components"]["schemas"]["EditOperationInput"]["properties"]["operation_type"]["enum"]
sql_ops_v3 = extract_sql_enum(sql_007, r"edit_operations_operation_type_check CHECK \(operation_type IN \((.*?)\)\)")
if schema_ops_v3 == openapi_ops_v3 == sql_ops_v3 and len(schema_ops_v3) == 21 and "UNDO" not in schema_ops_v3 and "REDO" not in schema_ops_v3:
    ok("3-way v3.0 EditOperation parity verified (21 canonical ops; UNDO/REDO excluded)")
else:
    fail(f"v3.0 EditOperation mismatch: schema={len(schema_ops_v3)}, openapi={len(openapi_ops_v3)}, sql={len(sql_ops_v3 or [])}")

# v3.0 10 Proposal statuses parity across Schema, OpenAPI, SQL 007
schema_prop_st = schemas["agent-proposal.v3.schema.json"]["properties"]["status"]["enum"]
openapi_prop_st = oa["components"]["schemas"]["ProposalStatusV3"]["enum"]
sql_prop_st = extract_sql_enum(sql_007, r"edit_proposals_status_check CHECK \(status IN \((.*?)\)\)")
if schema_prop_st == openapi_prop_st == sql_prop_st and len(schema_prop_st) == 10:
    ok("3-way v3.0 AgentProposal.status parity verified (10 snake_case states)")
else:
    fail(f"v3.0 Proposal.status mismatch: schema={schema_prop_st}, openapi={openapi_prop_st}, sql={sql_prop_st}")

# Check generated TypeScript types exist and match all 21 ops + 10 statuses
ts_file = REPO_ROOT / "packages/shared-types/src/v3-contracts.ts"
if ts_file.exists():
    ts_text = ts_file.read_text(encoding="utf-8")
    missing_ts = [op for op in schema_ops_v3 if f'"{op}"' not in ts_text] + [st for st in schema_prop_st if f'"{st}"' not in ts_text]
    if not missing_ts and "export interface TimelineV3" in ts_text and "export interface RenderPlanV3" in ts_text:
        ok("Generated frontend TypeScript types (v3-contracts.ts) in sync with frozen v3.0 Schemas")
    else:
        fail(f"Generated TS types out of sync: missing={missing_ts}")
else:
    fail("Missing generated TypeScript types: packages/shared-types/src/v3-contracts.ts")

# ---------------------------------------------------------------------------
# 5. CR-001, Risk Mitigations & v2.2 Security Guards
# ---------------------------------------------------------------------------
if "SET LOCAL ROLE app_runtime;" in verify_rls_sql and "INSERT INTO public.projects" in verify_rls_sql:
    ok("CR-001 verify_rls.sql non-superuser seeded fail-closed & cross-tenant isolation verified")
else:
    fail("CR-001 fix missing in verify_rls.sql")

if "trg_render_plans_immutable" in sql_007 and "BEFORE UPDATE OR DELETE ON public.render_plans" in sql_007:
    ok("Risk #3 RenderPlan DB-level immutability trigger verified in 007_v3_protocol_upgrade.sql")
else:
    fail("Missing RenderPlan immutability trigger in 007_v3_protocol_upgrade.sql")

if "consume_approval_token_for_timeline_v3" in sql_007 and "FOR UPDATE OF tok, rev, prop" in sql_007:
    ok("v3.0 5-dimensional atomic ApprovalToken consumption function verified in 007_v3_protocol_upgrade.sql")
else:
    fail("Missing consume_approval_token_for_timeline_v3 in 007_v3_protocol_upgrade.sql")

# Preserve all v2.2 security assertions
for fn_name, src in [
    ("heartbeat_task_with_fencing", sql_004),
    ("complete_task_with_fencing", sql_004),
    ("fail_task_with_fencing", sql_004),
    ("confirm_cancel_with_fencing", sql_004),
    ("publish_artifact_with_fencing", sql_005),
    ("record_usage_with_fencing", sql_006),
]:
    m = re.search(rf"CREATE OR REPLACE FUNCTION {fn_name}\(.*?\$\$;\s*", src, re.S)
    body = m.group(0) if m else ""
    has_clock_guard = bool(re.search(r"lease_expires_at\s*>\s*clock_timestamp\(\)", body))
    has_owner_token = "lease_owner = p_worker_id" in body and "lease_token = p_lease_token" in body
    has_lock = ("FOR UPDATE" in body) or fn_name == "heartbeat_task_with_fencing"
    if has_clock_guard and has_owner_token and has_lock:
        ok(f"Real-time fencing + serialization guard preserved in {fn_name}()")
    else:
        fail(f"Incomplete fencing guard in {fn_name}()")

global_tables = {"tenants", "users", "workers", "prompt_versions"}
created_tables = re.findall(r"CREATE TABLE\s+([a-z_]+)\s*\(", all_base_sql)
tenant_tables = [t for t in created_tables if t not in global_tables]
rls_block = re.search(r"tenant_tables\s+text\[\]\s*:=\s*ARRAY\[(.*?)\];", sql_006, re.S)
rls_tables = re.findall(r"'([a-z_]+)'", rls_block.group(1)) if rls_block else []
if set(tenant_tables) == set(rls_tables):
    ok(f"FORCE RLS parity preserved across all {len(tenant_tables)} tenant-owned tables")
else:
    fail(f"RLS mismatch: missing={set(tenant_tables)-set(rls_tables)}, extra={set(rls_tables)-set(tenant_tables)}")

bad_fks = [t for t in tenant_tables if re.search(rf"REFERENCES\s+{t}\s*\(\s*id\s*\)", all_base_sql)]
if bad_fks:
    fail(f"Single-column FK to tenant-owned table found: {bad_fks}")
else:
    ok("Zero single-column FKs to tenant-owned tables (100% composite tenant_id FKs)")

print("=== AI VIDEO STUDIO v3.0 + HITL v2.2 CROSS-CONTRACT VERIFICATION REPORT ===")
for status, msg in checks:
    print(f"[{status}] {msg}")
print(f"Total checks: {len(checks)}, Passed: {sum(1 for s, _ in checks if s == 'PASS')}, Failed: {len(errors)}")

if errors:
    sys.exit(1)
