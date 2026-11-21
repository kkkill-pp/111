import React, { useState } from "react";
import type { EditOperationInputV3, TimelineV3 } from "../../../../packages/shared-types/src/v3-contracts";
import type { ApiConnectionConfig } from "../editorStore";
import { buildOp } from "../engine/operations";

interface ProposalItem {
  operation_id: string;
  operation_type: EditOperationInputV3["operation_type"];
  payload: Record<string, unknown>;
  reason: string;
  risk_level: "low" | "medium" | "high" | "critical";
}

interface PendingProposal {
  proposal_id: string;
  proposal_checksum: string;
  base_version: number;
  rationale: string;
  permission_level: "L1" | "L2" | "L3";
  operations: ProposalItem[];
}

interface AgentProposalPanelProps {
  timeline: TimelineV3;
  apiCfg: ApiConnectionConfig;
  onApplyLocalOps: (ops: EditOperationInputV3[], label: string) => void;
  onStatus: (msg: string) => void;
}

export function AgentProposalPanel({
  timeline,
  apiCfg,
  onApplyLocalOps,
  onStatus,
}: AgentProposalPanelProps): React.ReactElement {
  const [intent, setIntent] = useState<string>("收紧开头 1 秒并添加黄金钩子字幕");
  const [permissionLevel, setPermissionLevel] = useState<"L0" | "L1" | "L2">("L1");
  const [proposal, setProposal] = useState<PendingProposal | null>(null);
  const [selectedIds, setSelectedIds] = useState<Record<string, boolean>>({});

  const handleGenerate = async () => {
    if (permissionLevel === "L0") {
      onStatus("L0（只读权限）禁止生成修改提案；请切换至 L1/L2。");
      return;
    }
    if (apiCfg.enabled) {
      try {
        const resp = await fetch(`${apiCfg.apiBase}/timelines/${timeline.timeline_id}/agent-proposals`, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-Tenant-Id": apiCfg.tenantId,
            "X-User-Id": apiCfg.userId,
            "X-User-Role": apiCfg.role,
            "Idempotency-Key": `idem-ag-${Date.now()}`,
          },
          body: JSON.stringify({ base_version: timeline.version, user_intent: intent, permission_level: permissionLevel }),
        });
        if (!resp.ok) throw new Error(await resp.text());
        const data = await resp.json();
        const ops: ProposalItem[] = (data.diff?.changes || []).map((c: Record<string, unknown>) => ({
          operation_id: String(c.operation_id),
          operation_type: String(c.type) as EditOperationInputV3["operation_type"],
          payload: c,
          reason: `AI 提案项 (${String(c.type)})`,
          risk_level: "medium",
        }));
        setProposal({
          proposal_id: data.proposal_id,
          proposal_checksum: data.proposal_checksum,
          base_version: timeline.version,
          rationale: `基于意图「${intent}」生成 ${ops.length} 项可逆操作（等待人工审阅）`,
          permission_level: permissionLevel,
          operations: ops,
        });
        setSelectedIds(Object.fromEntries(ops.map((o) => [o.operation_id, true])));
        onStatus(`已生成 AI 提案 ${data.proposal_id.slice(0, 8)}（时间线仍停留在 v${timeline.version}，等待审批）`);
        return;
      } catch (err) {
        onStatus(`生成提案失败: ${(err as Error).message}`);
        return;
      }
    }

    // Offline Demo Proposal (strictly follows AgentProposalV3 HITL preview)
    const vTrack = timeline.tracks.find((t) => t.type === "video");
    const sTrack = timeline.tracks.find((t) => t.type === "subtitle");
    const firstClip = vTrack && vTrack.type === "video" ? vTrack.clips[0] : null;
    const ops: ProposalItem[] = [];
    if (firstClip) {
      const trimOp = buildOp("TRIM_CLIP", {
        clip_id: firstClip.id,
        new_source_in: firstClip.source_range.in + 30000,
        new_source_out: firstClip.source_range.out,
      });
      ops.push({
        operation_id: trimOp.operation_id,
        operation_type: "TRIM_CLIP",
        payload: trimOp.payload,
        reason: `裁去片头 1.0s 停顿以提升前 3 秒完播率`,
        risk_level: "medium",
      });
    }
    if (sTrack) {
      const subOp = buildOp("ADD_SUBTITLE", {
        track_id: sTrack.id,
        timeline_start: 0,
        duration: 60000,
        text: "3秒看懂核心卖点",
        safe_zone: "bottom",
      });
      ops.push({
        operation_id: subOp.operation_id,
        operation_type: "ADD_SUBTITLE",
        payload: subOp.payload,
        reason: "在片头叠加高对比度钩子字幕",
        risk_level: "low",
      });
    }
    setProposal({
      proposal_id: "demo-prop-0001",
      proposal_checksum: "a".repeat(64),
      base_version: timeline.version,
      rationale: `演示模式（${permissionLevel}）：针对「${intent}」提出 ${ops.length} 项可审阅修改`,
      permission_level: permissionLevel,
      operations: ops,
    });
    setSelectedIds(Object.fromEntries(ops.map((o) => [o.operation_id, true])));
    onStatus(`已生成 AI 提案（待审批，时间线保持 v${timeline.version} 未被修改）`);
  };

  const handleApproveAndApply = async () => {
    if (!proposal) return;
    const chosen = proposal.operations.filter((o) => selectedIds[o.operation_id]);
    if (chosen.length === 0) {
      onStatus("请至少勾选一项操作后再批准应用");
      return;
    }
    if (apiCfg.enabled) {
      try {
        const rRev = await fetch(`${apiCfg.apiBase}/proposals/${proposal.proposal_id}/reviews`, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-Tenant-Id": apiCfg.tenantId,
            "X-User-Id": apiCfg.userId,
            "X-User-Role": apiCfg.role,
            "Idempotency-Key": `idem-rev-${Date.now()}`,
          },
          body: JSON.stringify({
            subject_version: String(proposal.base_version),
            subject_checksum: proposal.proposal_checksum,
            decision: "approve",
            selected_items: chosen.map((c) => c.operation_id),
          }),
        });
        if (!rRev.ok) throw new Error(await rRev.text());
        const revData = await rRev.json();
        const rApp = await fetch(`${apiCfg.apiBase}/timelines/${timeline.timeline_id}/operations`, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-Tenant-Id": apiCfg.tenantId,
            "X-User-Id": apiCfg.userId,
            "X-User-Role": apiCfg.role,
            "Idempotency-Key": `idem-app-${Date.now()}`,
          },
          body: JSON.stringify({
            base_version: proposal.base_version,
            proposal_id: proposal.proposal_id,
            approval_token: revData.approval_token,
          }),
        });
        if (!rApp.ok) throw new Error(await rApp.text());
        const appData = await rApp.json();
        setProposal(null);
        onStatus(`已通过单次 ApprovalToken 应用 AI 提案 → v${appData.new_version}`);
        return;
      } catch (err) {
        onStatus(`审批/应用失败: ${(err as Error).message}`);
        return;
      }
    }

    onApplyLocalOps(
      chosen.map((c) => ({ operation_id: c.operation_id, operation_type: c.operation_type, payload: c.payload, reversible: true })),
      `批准 AI 提案 (${chosen.length} 项)`
    );
    setProposal(null);
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 10, fontSize: 12 }}>
      <label>
        Agent 权限等级:
        <select value={permissionLevel} onChange={(e) => setPermissionLevel(e.target.value as "L0" | "L1" | "L2")} style={{ width: "100%", marginTop: 4 }}>
          <option value="L0">L0 — 只读（禁止修改提案）</option>
          <option value="L1">L1 — 生成提案（需人工审批）</option>
          <option value="L2">L2 — 限定范围执行（带单次 Token）</option>
        </select>
      </label>
      <label>
        创作意图:
        <textarea value={intent} onChange={(e) => setIntent(e.target.value)} rows={2} style={{ width: "100%", marginTop: 4, boxSizing: "border-box" }} />
      </label>
      <button onClick={() => void handleGenerate()} style={{ background: "#4F46E5", color: "#fff", border: "none", padding: "6px 10px", borderRadius: 4 }}>
        🤖 生成可解释 AI 提案
      </button>

      {proposal && (
        <div style={{ padding: 8, background: "#1E293B", borderRadius: 6, border: "1px solid #334155" }}>
          <div style={{ fontWeight: 600, color: "#38BDF8", marginBottom: 4 }}>
            待审提案 ({proposal.permission_level} · 基准 v{proposal.base_version})
          </div>
          <div style={{ fontSize: 11, color: "#CBD5E1", marginBottom: 8 }}>{proposal.rationale}</div>
          {proposal.operations.map((op) => (
            <label key={op.operation_id} style={{ display: "flex", gap: 6, alignItems: "flex-start", marginBottom: 6, fontSize: 11 }}>
              <input
                type="checkbox"
                checked={!!selectedIds[op.operation_id]}
                onChange={(e) => setSelectedIds({ ...selectedIds, [op.operation_id]: e.target.checked })}
              />
              <span>
                <strong>{op.operation_type}</strong> [{op.risk_level}]：{op.reason}
              </span>
            </label>
          ))}
          <div style={{ display: "flex", gap: 6, marginTop: 8 }}>
            <button onClick={() => void handleApproveAndApply()} style={{ flex: 1, background: "#059669", color: "#fff", border: "none", padding: 6, borderRadius: 4 }}>
              ✓ 批准并应用
            </button>
            <button
              onClick={() => {
                setProposal(null);
                onStatus("已拒绝 AI 提案，时间线未发生任何变更");
              }}
              style={{ flex: 1, background: "#991B1B", color: "#fff", border: "none", padding: 6, borderRadius: 4 }}
            >
              ✕ 拒绝
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
