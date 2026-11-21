import React, { useEffect, useState } from "react";
import type { ApiConnectionConfig } from "../editorStore";

export interface PluginItem {
  plugin_id: string;
  name: string;
  version: string;
  category: string;
  license: string;
  description: string;
  capabilities: string[];
  enabled: boolean;
  effective_enabled?: boolean;
  tenant_config?: Record<string, unknown>;
}

const DEMO_PLUGINS: PluginItem[] = [
  {
    plugin_id: "motion_template_plugin",
    name: "Motion.md Template & BrandKit Compiler",
    version: "1.0.0",
    category: "template",
    license: "Apache-2.0",
    description: "将 Motion.md 模板与 BrandKit 编译为标准 v3.0 编辑操作。",
    capabilities: ["motion_md.parse", "motion_md.compile_operations"],
    enabled: true,
    effective_enabled: true,
  },
  {
    plugin_id: "anchored_review_plugin",
    name: "Timecode-Anchored Review Comments",
    version: "1.0.0",
    category: "review",
    license: "Apache-2.0",
    description: "在时间轴精确时间码挂载人工审片批注并转为 Agent 改稿意图。",
    capabilities: ["review.anchored_comment", "review.to_agent_intent"],
    enabled: true,
    effective_enabled: true,
  },
  {
    plugin_id: "motion_overlay_renderer_plugin",
    name: "Declarative Motion Overlay Renderer",
    version: "1.0.0",
    category: "renderer",
    license: "Apache-2.0",
    description: "按 BrandKit 样式渲染动态花字与 Lower-Third 透明图层。",
    capabilities: ["overlay.render.text_hook", "overlay.render.lower_third"],
    enabled: true,
    effective_enabled: true,
  },
  {
    plugin_id: "media_gateway_plugin",
    name: "BYOK Multi-Modal Media Gateway",
    version: "1.0.0",
    category: "media_gen",
    license: "Apache-2.0",
    description: "租户级 BYOK 图片/音乐/语音生成与人工采纳网关。",
    capabilities: ["media_gen.image", "media_gen.audio_bgm", "media_gen.byok_providers"],
    enabled: true,
    effective_enabled: true,
  },
];

interface PluginCenterPanelProps {
  apiCfg: ApiConnectionConfig;
  onStatus: (msg: string) => void;
}

export function PluginCenterPanel({ apiCfg, onStatus }: PluginCenterPanelProps): React.ReactElement {
  const [plugins, setPlugins] = useState<PluginItem[]>(DEMO_PLUGINS);
  const [loading, setLoading] = useState<boolean>(false);
  const [brandName, setBrandName] = useState<string>("AI Video Studio");
  const [ctaText, setCtaText] = useState<string>("立即体验");
  const [primaryColor, setPrimaryColor] = useState<string>("#F59E0B");

  const fetchPlugins = async () => {
    if (!apiCfg.enabled) {
      setPlugins(DEMO_PLUGINS);
      return;
    }
    setLoading(true);
    try {
      const resp = await fetch(`${apiCfg.apiBase}/tenant/plugins`, {
        headers: {
          "X-Tenant-Id": apiCfg.tenantId,
          "X-User-Id": apiCfg.userId,
          "X-User-Role": apiCfg.role,
        },
      });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const data = await resp.json();
      setPlugins(data.plugins || []);
    } catch (err) {
      onStatus(`加载租户插件列表失败: ${(err as Error).message}`);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void fetchPlugins();
  }, [apiCfg.enabled, apiCfg.apiBase, apiCfg.tenantId]);

  const handleToggle = async (plugin: PluginItem) => {
    const current = plugin.effective_enabled ?? plugin.enabled;
    const nextEnabled = !current;
    if (!apiCfg.enabled) {
      setPlugins((prev) =>
        prev.map((p) =>
          p.plugin_id === plugin.plugin_id ? { ...p, enabled: nextEnabled, effective_enabled: nextEnabled } : p
        )
      );
      onStatus(`演示模式：已为当前租户${nextEnabled ? "启用" : "停用"}插件 ${plugin.plugin_id}`);
      return;
    }
    try {
      const resp = await fetch(`${apiCfg.apiBase}/tenant/plugins/${plugin.plugin_id}`, {
        method: "PUT",
        headers: {
          "Content-Type": "application/json",
          "X-Tenant-Id": apiCfg.tenantId,
          "X-User-Id": apiCfg.userId,
          "X-User-Role": "admin",
        },
        body: JSON.stringify({ enabled: nextEnabled }),
      });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const updated = await resp.json();
      setPlugins((prev) =>
        prev.map((p) =>
          p.plugin_id === plugin.plugin_id
            ? { ...p, enabled: updated.effective_enabled, effective_enabled: updated.effective_enabled }
            : p
        )
      );
      onStatus(`租户插件 ${plugin.plugin_id} 已${updated.effective_enabled ? "启用" : "停用"}`);
    } catch (err) {
      onStatus(`更新租户插件策略失败: ${(err as Error).message}`);
    }
  };

  const handleSaveBrandKit = async () => {
    const brandKit = { brand_name: brandName, cta_text: ctaText, primary_color: primaryColor };
    if (!apiCfg.enabled) {
      onStatus(`演示模式：已保存租户默认 BrandKit (${brandName})`);
      return;
    }
    try {
      const resp = await fetch(`${apiCfg.apiBase}/tenant/plugins/motion_template_plugin`, {
        method: "PUT",
        headers: {
          "Content-Type": "application/json",
          "X-Tenant-Id": apiCfg.tenantId,
          "X-User-Id": apiCfg.userId,
          "X-User-Role": "admin",
        },
        body: JSON.stringify({ enabled: true, config: { brand_kit: brandKit } }),
      });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      onStatus(`已保存租户默认 BrandKit: ${brandName}`);
    } catch (err) {
      onStatus(`保存 BrandKit 失败: ${(err as Error).message}`);
    }
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <span style={{ fontSize: 12, fontWeight: 600 }}>租户插件中心 ({plugins.length})</span>
        <button onClick={() => void fetchPlugins()} disabled={loading} style={{ fontSize: 11 }}>
          {loading ? "刷新中..." : "刷新"}
        </button>
      </div>
      <div style={{ background: "#0F172A", border: "1px solid #334155", borderRadius: 6, padding: 8, fontSize: 11 }}>
        <div style={{ fontWeight: 600, marginBottom: 6 }}>租户默认 BrandKit</div>
        <div style={{ display: "flex", flexDirection: "column", gap: 5 }}>
          <input value={brandName} onChange={(e) => setBrandName(e.target.value)} placeholder="品牌名" style={{ fontSize: 11, padding: 4 }} />
          <input value={ctaText} onChange={(e) => setCtaText(e.target.value)} placeholder="默认 CTA 文案" style={{ fontSize: 11, padding: 4 }} />
          <input value={primaryColor} onChange={(e) => setPrimaryColor(e.target.value)} placeholder="主色 HEX" style={{ fontSize: 11, padding: 4 }} />
          <button onClick={() => void handleSaveBrandKit()} style={{ background: "#2563EB", color: "#fff", border: "none", borderRadius: 4, padding: "4px 8px", fontSize: 11 }}>
            保存租户 BrandKit
          </button>
        </div>
      </div>
      {plugins.map((p) => {
        const active = p.effective_enabled ?? p.enabled;
        return (
          <div
            key={p.plugin_id}
            style={{
              background: "#1E293B",
              border: `1px solid ${active ? "#38BDF8" : "#334155"}`,
              borderRadius: 6,
              padding: 8,
              fontSize: 11,
            }}
          >
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 4 }}>
              <strong>{p.name}</strong>
              <button
                onClick={() => void handleToggle(p)}
                style={{
                  background: active ? "#059669" : "#475569",
                  color: "#fff",
                  border: "none",
                  borderRadius: 4,
                  padding: "2px 8px",
                  fontSize: 11,
                  cursor: "pointer",
                }}
              >
                {active ? "已启用" : "已停用"}
              </button>
            </div>
            <div style={{ color: "#94A3B8", marginBottom: 4 }}>
              {p.category} · v{p.version} · {p.license}
            </div>
            <div style={{ color: "#CBD5E1", lineHeight: 1.4 }}>{p.description}</div>
          </div>
        );
      })}
    </div>
  );
}
