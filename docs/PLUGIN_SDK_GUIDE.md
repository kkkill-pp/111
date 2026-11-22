# AI Video Studio v3.0 — Platform Plugin SDK Guide

AI Video Studio v3.0 separates its **immutable core** (multi-tenant PostgreSQL 15 RLS, rational-tick `TimelineService`, single-use `ApprovalToken` HITL governance, immutable `RenderPlan`, and Fencing Worker) from **hot-pluggable capability modules**.

## 1. Core Design Rules (Non-Negotiable)
1. **HITL Boundary**: Plugins may parse templates, render overlay assets, generate candidate media, or collect review annotations, but **never mutate timelines directly**. Every timeline change must compile into canonical `EditOperation[]` and pass through `TimelineService` (with human `ApprovalToken` when AI-proposed).
2. **LIC-001 License Redline**: `PluginRegistry` and `PluginLoader` enforce an allowlist (`MIT`, `Apache-2.0`, `BSD-2-Clause`, `BSD-3-Clause`, `ISC`, `MPL-2.0`, `Proprietary-Internal`). Any `GPL`/`AGPL`/proprietary-commercial plugin is rejected with `422 LICENSE_VIOLATION` **before Python import**.
3. **Zero Silent Fallback (ADR-004)**: Platform-wide disable/revocation is controlled only by the platform catalog and causes dependent routes to return `503 PLUGIN_DISABLED`. Tenant owners/admins can disable only their own tenant policy through `/api/v1/plugins/{id}/disable` or `/api/v1/tenant/plugins/{id}`; that returns `403 PLUGIN_DISABLED_FOR_TENANT` for that tenant only. MCP tool discovery and invocation use the same tenant-effective state and return JSON-RPC error `-32003` when blocked.
4. **Third-party default deny**: Packages discovered under `AIVS_PLUGINS_DIR` are imported only after pre-import license/path checks and are registered disabled. They remain unavailable until a platform operator registers a release, approves its license, and globally enables it in the persistent platform catalog.

## 2. Built-In Pluggable Modules
- `motion_template_plugin`: Parses `Motion.md` + `BrandKit` into canonical v3.0 `EditOperation[]`.
- `anchored_review_plugin`: Timecode-anchored video review comments with tenant isolation and Agent intent compilation.
- `motion_overlay_renderer_plugin`: Renders BrandKit-styled `text_hook` / `lower_third` RGBA PNG overlays for deterministic FFmpeg compositing.
- `media_gateway_plugin`: Tenant-scoped BYOK multi-modal generation (keys masked on read) with explicit HITL candidate adoption.

## 3. Building a Drop-In External Plugin
Place a folder under `AIVS_PLUGINS_DIR` (see `plugins_contrib/sample_brand_watermark_plugin/`):

```json
{
  "plugin_id": "my_custom_plugin",
  "name": "My Custom Plugin",
  "version": "1.0.0",
  "category": "template",
  "license": "Apache-2.0",
  "description": "What this plugin provides.",
  "entrypoint": "plugin.py:create_plugin"
}
```

Implement `create_plugin() -> AIVSPlugin` in `plugin.py`. Set `AIVS_PLUGINS_DIR=/path/to/plugins_contrib` before starting FastAPI. Discovery alone does not activate the plugin: platform catalog registration, license approval, and global enablement are required first. See `docs/SPRINT4_PLATFORM_REGISTRY.md`.

## 4. MCP Bridge
External coding agents (Claude Code, Codex CLI, Gemini CLI, etc.) can discover enabled plugin tools via JSON-RPC `tools/list` with `{"include_plugins": true}` and invoke `aivs_list_plugins`, `aivs_compile_motion_md`, and `aivs_list_review_comments`.
