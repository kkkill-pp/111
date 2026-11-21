/**
 * TimelineService API Client & Selection-Aware Command History Manager.
 * Combines:
 *  - Frozen v3.0 contracts (@aivs/shared-types)
 *  - opencut-classic CommandHistoryEntry (saves/restores selection snapshot on Undo/Redo)
 *  - v3.0 Optimistic Concurrency Lock (409 Conflict handling) & undo_scope boundary
 */
import type {
  EditOperationInputV3,
  TimelineV3,
  UndoScopeV3,
} from "../../../packages/shared-types/src/v3-contracts";

export interface SelectionSnapshot {
  kind: "clip" | "subtitle" | "overlay" | null;
  id: string | null;
}

export interface HistoryEntry {
  timeline: TimelineV3;
  selection: SelectionSnapshot;
  command: "init" | "edit" | "undo" | "redo";
  scope: UndoScopeV3;
  label: string;
}

export interface ApiConnectionConfig {
  enabled: boolean;
  apiBase: string;
  tenantId: string;
  userId: string;
  role: "owner" | "admin" | "editor" | "reviewer";
}

function buildHeaders(cfg: ApiConnectionConfig, idemKey?: string): Record<string, string> {
  const h: Record<string, string> = {
    "Content-Type": "application/json",
    "X-Tenant-Id": cfg.tenantId,
    "X-User-Id": cfg.userId,
    "X-User-Role": cfg.role,
    "X-Trace-Id": `web-${Date.now().toString(36)}`,
  };
  if (idemKey) h["Idempotency-Key"] = idemKey;
  return h;
}

export async function commitHumanOperations(
  cfg: ApiConnectionConfig,
  timelineId: string,
  baseVersion: number,
  operations: EditOperationInputV3[],
  idemKey: string
): Promise<{ new_version: number; snapshot_checksum: string }> {
  const resp = await fetch(`${cfg.apiBase}/timelines/${timelineId}/operations`, {
    method: "POST",
    headers: buildHeaders(cfg, idemKey),
    body: JSON.stringify({ base_version: baseVersion, operations }),
  });
  if (resp.status === 409) {
    throw new Error("409 VERSION_CONFLICT: 时间线已被其他协作者更新，请刷新最新版本后重试。");
  }
  if (!resp.ok) {
    throw new Error(`TimelineService (${resp.status}): ${await resp.text()}`);
  }
  return resp.json();
}

export async function invokeUndoRedoApi(
  cfg: ApiConnectionConfig,
  timelineId: string,
  command: "undo" | "redo",
  baseVersion: number,
  undoScope: UndoScopeV3,
  idemKey: string
): Promise<{ new_version: number; origin_command: string }> {
  const resp = await fetch(`${cfg.apiBase}/timelines/${timelineId}/${command}`, {
    method: "POST",
    headers: buildHeaders(cfg, idemKey),
    body: JSON.stringify({ base_version: baseVersion, undo_scope: undoScope }),
  });
  if (resp.status === 409) {
    throw new Error(`409 VERSION_CONFLICT: 无法在旧版本 v${baseVersion} 上执行 ${command}。`);
  }
  if (!resp.ok) {
    throw new Error(`TimelineService ${command} (${resp.status}): ${await resp.text()}`);
  }
  return resp.json();
}

export async function triggerRenderAndQa(
  cfg: ApiConnectionConfig,
  timelineId: string,
  timelineVersion: number,
  mode: "preview" | "final"
): Promise<{ artifact_id: string; artifact_checksum: string; qa_status: string }> {
  const endpoint = mode === "final" ? "renders" : "previews";
  const rRender = await fetch(`${cfg.apiBase}/timelines/${timelineId}/${endpoint}`, {
    method: "POST",
    headers: buildHeaders(cfg, `idem-${mode}-${timelineVersion}-${Date.now()}`),
    body: JSON.stringify({ timeline_version: timelineVersion }),
  });
  if (!rRender.ok) {
    throw new Error(`Render ${mode} failed (${rRender.status}): ${await rRender.text()}`);
  }
  const renderData = await rRender.json();

  const rQa = await fetch(`${cfg.apiBase}/artifacts/${renderData.artifact_id}/qa`, {
    method: "POST",
    headers: buildHeaders(cfg, `idem-qa-${renderData.artifact_id}-${Date.now()}`),
  });
  const qaData = rQa.ok ? await rQa.json() : { qa_status: "unknown" };
  return {
    artifact_id: renderData.artifact_id,
    artifact_checksum: renderData.artifact_checksum,
    qa_status: qaData.qa_status,
  };
}
