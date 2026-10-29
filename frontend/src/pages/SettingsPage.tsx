import { useEffect, useState } from "react";
import { clsx } from "clsx";

import { api } from "../api";
import { TopBar } from "../components/TopBar";
import { Icon } from "../components/Icon";
import type { ConfigItemView } from "../types";

interface FieldMeta {
  key: string;
  label: string;
  hint?: string;
  secret?: boolean;
  type?: "text" | "number" | "checkbox";
  options?: { value: string; label: string }[];
}

/** 服务商下拉共用的"离线模板"选项:fake 是零配置默认态,须可显示、可回切。 */
const FAKE_PROVIDER = { value: "fake", label: "fake(离线模板,零配置可跑)" };

/** 配音模型选项:每模型带"默认音色";换模型时音色下拉跟随(共用同一预置池)。 */
const TTS_MODELS: { value: string; label: string; defaultVoice: string }[] = [
  {
    value: "FunAudioLLM/CosyVoice2-0.5B",
    label: "CosyVoice2(单音色,自然清晰)",
    defaultVoice: "anna",
  },
  {
    value: "fnlp/MOSS-TTSD-v0.5",
    label: "MOSS-TTSD(对白/双人剧情)",
    defaultVoice: "anna",
  },
];

/** 预置音色(实测两模型共用同一音色池;标注性别与风格) */
const TTS_VOICES: { value: string; label: string; style?: string }[] = [
  { value: "anna", label: "anna", style: "沉稳女声" },
  { value: "claire", label: "claire", style: "温柔女声" },
  { value: "bella", label: "bella", style: "激情女声" },
  { value: "alex", label: "alex", style: "男声" },
  { value: "benjamin", label: "benjamin", style: "男声" },
  { value: "charles", label: "charles", style: "男声" },
];

const GROUPS: { key: string; label: string; fields: FieldMeta[] }[] = [
  {
    key: "text",
    label: "文本生成",
    fields: [
      { key: "text_provider", label: "服务商", options: [
        FAKE_PROVIDER,
        { value: "openai", label: "OpenAI 兼容(DeepSeek 等)" },
      ]},
      { key: "text_base_url", label: "接口地址", hint: "OpenAI 兼容 base_url,如:https://api.deepseek.com/v1" },
      { key: "text_model", label: "模型", options: [{ value: "deepseek-v4-flash", label: "deepseek-v4-flash" }] },
      { key: "text_api_key", label: "API Key", secret: true },
    ],
  },
  {
    key: "image",
    label: "图像服务",
    fields: [
      { key: "image_provider", label: "服务商", options: [
        FAKE_PROVIDER,
        { value: "siliconflow", label: "SiliconFlow" },
      ]},
      { key: "image_base_url", label: "接口地址" },
      { key: "image_model", label: "生图模型", options: [{ value: "Tongyi-MAI/Z-Image-Turbo", label: "Tongyi-MAI/Z-Image-Turbo" }] },
      { key: "image_consistency_model", label: "参考图 / 身份一致性模型", hint: "上传参考图时使用；留空表示仅文字生图。", options: [
        { value: "", label: "不启用参考图编辑" },
        { value: "Qwen/Qwen-Image-Edit-2509", label: "Qwen/Qwen-Image-Edit-2509" },
      ] },
      { key: "image_size", label: "尺寸", options: [
        { value: "1024x576", label: "1024x576(16:9 横版·漫剧推荐)" },
        { value: "512x512", label: "512x512(方形)" },
        { value: "768x1024", label: "768x1024(竖版)" },
        { value: "576x1024", label: "576x1024(9:16 竖版)" },
      ]},
      { key: "image_api_key", label: "API Key", secret: true },
    ],
  },
  {
    key: "video",
    label: "视频服务",
    fields: [
      { key: "video_provider", label: "服务商", options: [
        FAKE_PROVIDER,
        { value: "ark", label: "火山方舟 Ark" },
      ]},
      { key: "doubao_base_url", label: "接口地址", hint: "留空 = 官方默认(cn-beijing);换区域/企业网关时覆盖,如 https://ark.cn-guangzhou.volces.com/api/v3" },
      { key: "doubao_api_key", label: "Ark API Key", secret: true },
      { key: "doubao_video_model", label: "接入点 / 模型", hint: "填你账号下的推理接入点(ep- 开头)或已开通的模型 ID;接入点是账号绑定的,每人不同" },
      { key: "video_duration", label: "单镜时长(秒)", options: [
        { value: "5", label: "5 秒" },
        { value: "10", label: "10 秒" },
      ]},
      { key: "video_ratio", label: "画幅", options: [
        { value: "adaptive", label: "adaptive(跟随首帧)" },
        { value: "16:9", label: "16:9" },
        { value: "9:16", label: "9:16" },
        { value: "1:1", label: "1:1" },
      ]},
      { key: "video_max_shots", label: "成片合成镜头上限(0=全部)", type: "number" },
      { key: "tts_enabled", label: "台词配音", type: "checkbox", hint: "成片合成时把分镜台词逐句合成语音铺进音轨(复用图像服务凭据;失败不阻塞成片)" },
      { key: "tts_model", label: "配音模型", options: TTS_MODELS },
      { key: "tts_voice", label: "音色", hint: "随所选模型联动;换模型后自动落到该模型默认音色" },
    ],
  },
];

export function SettingsPage() {
  const [items, setItems] = useState<Record<string, ConfigItemView> | null>(null);
  const [form, setForm] = useState<Record<string, string>>({});
  const [tab, setTab] = useState(GROUPS[0].key);
  const [saved, setSaved] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = () => {
    api
      .getConfig()
      .then((res) => {
        setItems(res.items);
        const next: Record<string, string> = {};
        for (const [k, v] of Object.entries(res.items)) {
          next[k] = v.is_sensitive ? "" : v.value;
        }
        setForm(next);
      })
      .catch((e) => setError(e instanceof Error ? e.message : "加载配置失败"));
  };

  useEffect(() => {
    load();
  }, []);

  const group = GROUPS.find((g) => g.key === tab)!;

  const save = async () => {
    if (!items || busy) return;
    const changed: Record<string, string> = {};
    for (const meta of GROUPS.flatMap((g) => g.fields)) {
      const raw = form[meta.key] ?? "";
      const current = items[meta.key]?.value ?? "";
      const currentSet = items[meta.key]?.is_set ?? false;
      if (meta.secret) {
        if (raw.trim() === "") continue; // 留空 = 不改密钥
        changed[meta.key] = raw;
      } else if (meta.type === "number" && raw.trim() === "") {
        continue; // 数字留空 = 不改(否则空串整批提交会被后端拒为非法整数;归零请填 0)
      } else if (String(raw) !== String(current)) {
        changed[meta.key] = raw;
        if (raw === "" && !currentSet && items[meta.key]?.is_sensitive === false && current === "") {
          // 空值且本来就是空 → 无需提交
          delete changed[meta.key];
        }
      }
    }
    if (Object.keys(changed).length === 0) {
      setSaved("没有改动");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const res = await api.saveConfig(changed);
      setItems(res.items);
      const next: Record<string, string> = {};
      for (const [k, v] of Object.entries(res.items)) {
        next[k] = v.is_sensitive ? "" : v.value;
      }
      setForm(next);
      setSaved("已保存并立即生效");
    } catch (e) {
      setError(e instanceof Error ? e.message : "保存失败");
    } finally {
      setBusy(false);
    }
  };

  const set = (key: string, value: string) => setForm((f) => ({ ...f, [key]: value }));

  return (
    <div className="flex h-full flex-col">
      <TopBar />
      <div className="page-scroll">
        <div className="studio-page space-y-6" style={{ maxWidth: 960 }}>
          <div>
            <p className="page-eyebrow"><Icon name="settings" size={12} />
              MAKE IT YOURS
            </p>
            <h1 className="page-heading">服务设置</h1><p className="page-description">管理文字、图像、视频和配音服务，让灵感顺利落地。</p>
          </div>

          {/* tabs */}
          <div className="flex flex-wrap gap-1.5">
            {GROUPS.map((g) => (
              <button
                key={g.key}
                className={clsx(
                  "rounded-lg border px-4 py-2 text-xs transition",
                  tab === g.key
                    ? "border-star/30 bg-star/10 text-star"
                    : "border-white/10 bg-white/5 text-cream/70 hover:bg-white/10",
                )}
                onClick={() => {
                  setTab(g.key);
                  setSaved(null);
                }}
              >
                {g.label}
              </button>
            ))}
          </div>

          <div className="cosmic-card-strong space-y-4 p-5">
            {items === null && <p className="text-sm text-cream/50">加载配置…</p>}
            {items &&
              group.fields.map((meta) => (
                <FieldRow
                  key={meta.key}
                  meta={meta}
                  view={items[meta.key]}
                  value={form[meta.key] ?? ""}
                  options={meta.key === "tts_voice" ? voiceOptions(form) : undefined}
                  onChange={(v) => handleFieldChange(meta, v, set)}
                />
              ))}

            <div className="flex flex-wrap items-center gap-3 border-t border-white/10 pt-4">
              <button className="btn-candy px-6" onClick={() => void save()} disabled={busy}>
                {busy ? "保存中…" : "保存配置"}
              </button>
              {saved && <span className="text-sm text-mint">{saved}</span>}
              {error && <span className="text-sm text-candy">{error}</span>}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

/** 音色下拉选项:按当前所选模型联动;默认音色标注"(默认)"。 */
function voiceOptions(form: Record<string, string>): { value: string; label: string }[] | undefined {
  const modelValue = form["tts_model"] || "";
  const defaultVoice = TTS_MODELS.find((m) => m.value === modelValue)?.defaultVoice;
  const current = form["tts_voice"] || "";
  const base: { value: string; label: string }[] = TTS_VOICES.map((v) => ({
    value: v.value,
    label: v.value === defaultVoice ? `${v.label}(${v.style ?? ""}·默认)` : `${v.label}(${v.style ?? ""})`,
  }));
  // 存量值不在预置池(旧自由文本)时也保留为选项,避免保存时被意外替换
  if (current && !base.some((o) => o.value === current)) {
    base.push({ value: current, label: `${current}(当前值,非预置)` });
  }
  return base;
}

/** 配音模型切换时:音色一律落到该模型默认音色(与提示文案"换模型后自动落到默认"一致)。 */
function handleFieldChange(
  meta: FieldMeta,
  value: string,
  set: (key: string, value: string) => void,
) {
  if (meta.key === "tts_model") {
    const picked = TTS_MODELS.find((m) => m.value === value);
    set(meta.key, value);
    if (picked) set("tts_voice", picked.defaultVoice);
    return;
  }
  set(meta.key, value);
}

function FieldRow({
  meta,
  view,
  value,
  onChange,
  options,
}: {
  meta: FieldMeta;
  view?: ConfigItemView;
  value: string;
  onChange: (v: string) => void;
  options?: { value: string; label: string }[];
}) {
  const isSecret = Boolean(view?.is_sensitive || meta.secret);
  const isSet = Boolean(view?.is_set);
  const inputCls = "input-cosmic font-mono text-xs";
  const selectOptions = options ?? meta.options;

  return (
    <div className="grid gap-1 sm:grid-cols-[11rem_1fr] sm:items-start">
      <div>
        <label
          className="font-comic text-sm font-semibold leading-5 text-cream/90"
          htmlFor={`cfg-${meta.key}`}
        >
          {meta.label}
        </label>
      </div>
      <div>
        {selectOptions ? (
          <select
            id={`cfg-${meta.key}`}
            className={inputCls}
            value={isSecret ? "" : value}
            onChange={(e) => onChange(e.target.value)}
            disabled={isSecret}
          >
            {selectOptions.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
        ) : meta.type === "checkbox" ? (
          <div className="flex h-9 items-center gap-2">
            <input
              id={`cfg-${meta.key}`}
              type="checkbox"
              checked={value === "true"}
              onChange={(e) => onChange(String(e.target.checked))}
              className="h-5 w-5 accent-[#ffd166]"
            />
            <span className="text-sm text-cream/75">{value === "true" ? "开" : "关"}</span>
          </div>
        ) : (
          <input
            id={`cfg-${meta.key}`}
            className={inputCls}
            type={meta.type === "number" ? "number" : "text"}
            placeholder={isSecret ? (isSet ? "已设置 · 留空保持不变" : "未设置") : ""}
            value={value}
            onChange={(e) => onChange(e.target.value)}
            autoComplete="off"
          />
        )}
        {/* 提示行固定占位:无提示也保留行高 → 各行垂直间隔恒定 */}
        <div className="mt-0.5 flex min-h-[1.05rem] items-center gap-2 text-[11px]">
          {isSet && !isSecret && <span className="text-mint/80">● 已生效</span>}
          {isSecret && (
            <span className={isSet ? "text-mint/80" : "text-cream/40"}>
              {isSet ? "● 已设置(不显示明文)" : "○ 未设置"}
            </span>
          )}
          {meta.hint && <span className="text-cream/40">{meta.hint}</span>}
        </div>
      </div>
    </div>
  );
}
