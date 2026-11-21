# ADR-001：人机协同（HITL）作为底层架构核心能力

**状态：** Accepted（v2.0 强化）  
**日期：** 2026-11-21

## 背景
AI 擅长素材理解、候选生成与可逆操作建议，但创作意图、内容取舍、品牌边界与发布责任必须由人掌控。全自动静默修改会破坏信任与可追溯性。

## 决策
将人机协同内建为跨数据库、API、Agent、任务引擎与前端的底层架构机制，统一执行：
`Proposal -> Validation -> Diff -> Human Review (/reviews) -> Single-Use ApprovalToken -> Atomic Apply -> Preview -> Final Render -> QA -> Human Review (/reviews) -> Delivery`。

## 硬规则
- Storyboard 与 EditProposal 均不是生产 Timeline，须经人工审阅决策。
- 统一采用复数 `/reviews` 资源接口（`/storyboards/{id}/reviews`、`/proposals/{id}/reviews`、`/artifacts/{id}/reviews`）。
- 审阅初始态为 `pending`，无响应或超时（`expired`）绝不视为同意。
- 审批强绑定不可变版本与 SHA-256 校验和；任何变更自动触发 `superseded`。
- 用户可逐项接受、拒绝、微调 AI 提案，且随时可人工接管或一键撤销（追加反向版本）。
