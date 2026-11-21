/**
 * Pure TypeScript Editor State & TimelineService API Client (Phase 0/1 Prototype).
 * Strictly typed against frozen v3.0 contracts (@aivs/shared-types).
 * Enforces:
 *  - All mutations go through TimelineService POST /timelines/{id}/operations (21 canonical ops)
 *  - Optimistic concurrency lock via base_version (handles 409 Conflict)
 *  - Independent /undo and /redo endpoints with explicit undo_scope boundary
 */
import type {
  EditOperationInputV3,
  TimelineV3,
  UndoScopeV3,
} from "../../../packages/shared-types/src/v3-contracts";

export interface EditorSessionState {
  timeline: TimelineV3;
  playheadTick: number;
  zoomPxPerSecond: number;
  selectedClipId: string | null;
  lastError: string | null;
}

export function ticksToSeconds(ticks: number, num: number, den: number): number {
  return (ticks * num) / den;
}

export function formatTimecode(ticks: number, tbNum: number, tbDen: number, fps = 30): string {
  const totalSec = ticksToSeconds(ticks, tbNum, tbDen);
  const mins = Math.floor(totalSec / 60);
  const secs = Math.floor(totalSec % 60);
  const frames = Math.floor((totalSec - Math.floor(totalSec)) * fps);
  return `${String(mins).padStart(2, "0")}:${String(secs).padStart(2, "0")}:${String(frames).padStart(2, "0")}`;
}

export async function dispatchOperations(
  apiBase: string,
  timelineId: string,
  baseVersion: number,
  operations: EditOperationInputV3[],
  idempotencyKey: string,
  headers: Record<string, string>
): Promise<{ timeline: TimelineV3; newVersion: number }> {
  const resp = await fetch(`${apiBase}/timelines/${timelineId}/operations`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "Idempotency-Key": idempotencyKey,
      ...headers,
    },
    body: JSON.stringify({
      base_version: baseVersion,
      mode: "apply",
      operations,
    }),
  });
  if (resp.status === 409) {
    throw new Error("OPTIMISTIC_LOCK_CONFLICT: Timeline version changed; please reload latest version.");
  }
  if (!resp.ok) {
    throw new Error(`TimelineService error (${resp.status}): ${await resp.text()}`);
  }
  return resp.json();
}

export async function invokeUndoRedo(
  apiBase: string,
  timelineId: string,
  command: "undo" | "redo",
  baseVersion: number,
  undoScope: UndoScopeV3,
  idempotencyKey: string,
  headers: Record<string, string>
): Promise<{ timeline: TimelineV3; newVersion: number }> {
  const resp = await fetch(`${apiBase}/timelines/${timelineId}/${command}`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "Idempotency-Key": idempotencyKey,
      ...headers,
    },
    body: JSON.stringify({
      base_version: baseVersion,
      undo_scope: undoScope,
    }),
  });
  if (resp.status === 409) {
    throw new Error("OPTIMISTIC_LOCK_CONFLICT: Cannot undo/redo over newer timeline version.");
  }
  if (!resp.ok) {
    throw new Error(`TimelineService ${command} error (${resp.status}): ${await resp.text()}`);
  }
  return resp.json();
}
