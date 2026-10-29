import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import type { LibraryCharacter, ProjectView, ReferenceImage, Shot } from "../types";
import { Icon } from "./Icon";

export interface CreativeProps {
  project: ProjectView;
  busy?: boolean;
  onAction?: (fn: () => Promise<unknown>) => Promise<void>;
}

function useAction(onAction?: CreativeProps["onAction"]) {
  const [message, setMessage] = useState("");
  const [pending, setPending] = useState(false);
  const run = async (fn: () => Promise<unknown>, success: string) => {
    setPending(true); setMessage("");
    try {
      if (onAction) await onAction(fn); else await fn();
      setMessage(success); return true;
    } catch (e) { setMessage(e instanceof Error ? e.message : "操作失败，请重试"); return false; }
    finally { setPending(false); }
  };
  return { run, message, pending };
}

function Notice({ message }: { message: string }) {
  return message ? <p role="status" className="rounded-xl bg-comet/10 px-3 py-2 text-sm text-comet">{message}</p> : null;
}

export function ReferencePanel({ project, busy, onAction }: CreativeProps) {
  const [kind, setKind] = useState<ReferenceImage["kind"]>("style");
  const [target, setTarget] = useState("");
  const { run, message, pending } = useAction(onAction);
  const disabled = busy || pending || !project.actions.can_manage_references;
  const options = kind === "character"
    ? (project.content.characters ?? []).map((c, i) => ({ value: i, label: c.name }))
    : (project.content.shots ?? []).map(s => ({ value: s.index, label: `第 ${s.index} 镜 · ${s.scene}` }));
  const targetValue = target || String(options[0]?.value ?? "");
  return <div className="space-y-4">
    <div className="cosmic-card space-y-3 p-4">
      <h3 className="font-comic text-lg text-star">参考图素材</h3>
      <p className="text-sm text-cream/60">上传风格、角色或构图参考，后续生成对应画面时使用。PNG、JPEG、WebP，单张不超过 8 MB。</p>
      <div className="flex flex-wrap gap-2">
        <label className="min-w-32 flex-1 text-xs text-cream/60">用途
          <select aria-label="参考图用途" className="input-cosmic mt-1" value={kind} disabled={disabled} onChange={e => { setKind(e.target.value as ReferenceImage["kind"]); setTarget(""); }}>
            <option value="style">整体风格</option><option value="character">指定角色</option><option value="shot">指定分镜</option>
          </select>
        </label>
        {kind !== "style" && <label className="min-w-32 flex-1 text-xs text-cream/60">应用对象
          <select aria-label="参考图应用对象" className="input-cosmic mt-1" value={targetValue} onChange={e => setTarget(e.target.value)} disabled={disabled}>
            {!options.length && <option value="">请先生成{kind === "character" ? "角色" : "分镜"}</option>}
            {options.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
          </select>
        </label>}
      </div>
      <label className="block text-sm text-cream/80">{pending ? "正在上传…" : "选择参考图片"}
        <input aria-label="上传参考图" type="file" accept="image/png,image/jpeg,image/webp" className="mt-2 block w-full text-sm file:mr-3 file:rounded-full file:border-0 file:bg-star file:px-4 file:py-2 file:text-space-900" disabled={disabled || (kind !== "style" && !options.length)} onChange={e => {
          const file = e.target.files?.[0]; e.target.value = "";
          if (!file) return;
          void run(async () => { if (file.size > 8 * 1024 * 1024) throw new Error("图片不能超过 8 MB"); await api.uploadReference(project.id, file, kind, kind === "style" ? undefined : Number(targetValue)); }, "参考图已保存，将用于下次生成或单镜重生成");
        }} />
      </label>
      <p className="text-xs text-cream/50">真实参考生图需在设置页启用一致性编辑模型，每次最多使用 3 张参考图，优先使用指定分镜和角色的参考。离线模式仅演示素材管理和流程。已有画面不会因上传自动改变。</p>
      {disabled && project.actions.edit_blocker && <p className="text-xs text-star">{project.actions.edit_blocker}</p>}
      <Notice message={message} />
    </div>
    <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
      {(project.content.reference_images ?? []).map(ref => <div key={ref.id} className="cosmic-card overflow-hidden p-3">
        <img src={ref.url} alt="创作参考图" className="aspect-video w-full rounded-xl bg-black/20 object-contain" />
        <div className="mt-3 flex items-center justify-between gap-2">
          <span className="text-sm text-cream/80">{ref.kind === "style" ? "整体风格" : ref.kind === "shot" ? `第 ${ref.target_index} 镜` : project.content.characters?.[ref.target_index ?? 0]?.name ?? "角色参考"}</span>
          <button className="btn-ghost-cosmic text-xs" disabled={disabled} onClick={() => void run(() => api.deleteReference(project.id, ref.id), "参考图已移除")}>移除</button>
        </div>
      </div>)}
    </div>
    {!project.content.reference_images?.length && <p className="py-10 text-center text-sm text-cream/40">还没有参考图，先上传一张喜欢的风格或角色形象。</p>}
  </div>;
}

const blankCharacter = { name: "", personality: "", appearance: "", quirks: "", style: "" };
export function LibraryBrowser({ onUse, disabled = false }: { onUse?: (id: number) => Promise<unknown>; disabled?: boolean }) {
  const [characters, setCharacters] = useState<LibraryCharacter[]>([]);
  const [query, setQuery] = useState("");
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState("");
  const [editing, setEditing] = useState<number | "new" | null>(null);
  const [draft, setDraft] = useState(blankCharacter);
  const [deleting, setDeleting] = useState<number | null>(null);
  const { run, message, pending } = useAction();
  const reload = useCallback(async () => {
    try { setCharacters(await api.listCharacters()); setLoadError(""); }
    catch (e) { setLoadError(e instanceof Error ? e.message : "角色库加载失败"); }
    finally { setLoading(false); }
  }, []);
  useEffect(() => { void reload(); }, [reload]);
  const filtered = characters.filter(c => `${c.name} ${c.appearance} ${c.style}`.toLowerCase().includes(query.toLowerCase()));
  return <div className="space-y-4">
    <div className="flex flex-wrap items-center gap-3">
      <div className="relative min-w-40 flex-1"><Icon name="search" size={15} className="absolute left-3 top-3 text-cream/40" /><input aria-label="搜索角色" className="input-cosmic pl-9" placeholder="搜索名字、外貌或风格" value={query} onChange={e => setQuery(e.target.value)} /></div>
      <button className="btn-candy" disabled={pending} onClick={() => { setEditing("new"); setDraft(blankCharacter); }}><Icon name="plus" size={14} />新建角色</button>
    </div>
    <Notice message={message || loadError} />
    {loadError && <button className="btn-ghost-cosmic" onClick={() => void reload()}>重新加载</button>}
    {editing != null && <form className="cosmic-card space-y-3 p-4" onSubmit={e => { e.preventDefault(); void run(async () => {
      if (editing === "new") await api.createCharacter(draft); else await api.updateCharacter(editing, draft);
      setEditing(null); await reload();
    }, "角色已保存"); }}>
      <h3 className="text-lg font-semibold text-star">{editing === "new" ? "新建角色" : "编辑角色"}</h3>
      {([['name', '名字'], ['personality', '性格'], ['appearance', '外貌设定'], ['quirks', '习惯与特征'], ['style', '视觉风格']] as const).map(([key, label]) => <label key={key} className="block text-xs text-cream/70">{label}
        <input className="input-cosmic mt-1" required={key === "name"} maxLength={key === "name" ? 100 : key === "style" ? 200 : 2000} value={draft[key]} onChange={e => setDraft({ ...draft, [key]: e.target.value })} />
      </label>)}
      <div className="flex gap-2"><button className="btn-candy" disabled={pending || !draft.name.trim()}>保存角色</button><button type="button" className="btn-ghost-cosmic" disabled={pending} onClick={() => setEditing(null)}>取消</button></div>
    </form>}
    {loading && <p className="py-10 text-center text-cream/50">正在加载角色库…</p>}
    {!loading && !loadError && !filtered.length && <div className="cosmic-card p-10 text-center text-sm text-cream/50">{characters.length ? "没有找到匹配的角色" : "角色库还是空的。新建角色，或在项目的角色卡片上点击「存入角色库」。"}</div>}
    <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
      {filtered.map(c => <article key={c.id} className="cosmic-card space-y-3 p-4">
        {c.image_url ? <img src={c.image_url} alt={`${c.name} 形象`} className="aspect-[4/3] w-full rounded-2xl bg-black/20 object-contain" /> : <div className="flex aspect-[4/3] items-center justify-center rounded-2xl bg-grape/10 text-4xl text-grape">{c.name.slice(0, 1)}</div>}
        <div><h3 className="text-lg font-semibold text-star">{c.name}</h3><p className="text-xs text-grape">{c.personality}{c.style ? ` · ${c.style}` : ""}</p></div>
        <p className="whitespace-pre-wrap text-sm text-cream/80">{c.appearance || "尚未填写外貌设定"}</p>
        {c.quirks && <p className="text-xs text-cream/50">{c.quirks}</p>}
        <div className="flex flex-wrap gap-2">
          {onUse && <button className="btn-candy text-xs" disabled={disabled || pending} onClick={() => void run(() => onUse(c.id), `已将「${c.name}」加入项目`)}>加入项目</button>}
          <button className="btn-ghost-cosmic text-xs" disabled={pending} onClick={() => { setEditing(c.id); setDraft({ name: c.name, personality: c.personality, appearance: c.appearance, quirks: c.quirks ?? "", style: c.style }); }}>编辑</button>
          <button className="btn-ghost-cosmic text-xs" disabled={pending} onClick={() => setDeleting(c.id)}>删除</button>
        </div>
        <label className="block text-xs text-cream/60">上传 / 替换角色形象
          <input aria-label={`上传${c.name}的形象`} type="file" accept="image/png,image/jpeg,image/webp" className="mt-1 block w-full text-xs" disabled={pending} onChange={e => {
            const file = e.target.files?.[0]; e.target.value = "";
            if (file) void run(async () => { if (file.size > 8 * 1024 * 1024) throw new Error("图片不能超过 8 MB"); await api.uploadCharacterImage(c.id, file); await reload(); }, "角色形象已更新");
          }} />
        </label>
        {deleting === c.id && <div className="space-y-2 rounded-xl bg-candy/10 p-3 text-xs"><p>删除「{c.name}」？已导入项目的角色仍会保留。</p><button className="btn-ghost-cosmic" disabled={pending} onClick={() => void run(async () => { await api.deleteCharacter(c.id); setDeleting(null); await reload(); }, "角色已删除")}>确定删除</button><button className="ml-2" onClick={() => setDeleting(null)}>保留</button></div>}
      </article>)}
    </div>
  </div>;
}

export function ProjectLibrary({ project, busy, onAction }: CreativeProps) {
  return <div className="space-y-3">
    <p className="text-sm text-cream/60">加入的角色会保留名字、设定与形象，并参与后续分镜创作。可在开始生成前或角色确认阶段加入。</p>
    {!project.actions.can_manage_cast && <p className="text-xs text-star">{project.actions.edit_blocker}</p>}
    <LibraryBrowser disabled={busy || !project.actions.can_manage_cast} onUse={async id => { if (onAction) await onAction(() => api.importCharacter(project.id, id)); }} />
  </div>;
}

export function SaveCharacterButton({ project, index, busy, onAction }: CreativeProps & { index: number }) {
  const { run, message, pending } = useAction(onAction);
  return <div className="mt-3 space-y-2">
    <button className="btn-ghost-cosmic text-xs" disabled={busy || pending || !project.actions.can_manage_references} onClick={() => void run(() => api.saveCharacter(project.id, index), "已存入角色库，可在其他项目复用")}>存入角色库</button>
    <Notice message={message} />
  </div>;
}

export function ShotTools({ project, shot, busy, onAction }: CreativeProps & { shot: Shot }) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(shot);
  const [feedback, setFeedback] = useState("");
  const [showFeedback, setShowFeedback] = useState(false);
  const { run, message, pending } = useAction(onAction);
  const disabled = busy || pending || !project.actions.can_edit_shots;
  const stale = project.content.shot_images?.find(im => im.shot_index === shot.index)?.stale;
  return <div className="shot-tools space-y-3">
    {stale && <p className="text-xs text-star">脚本已修改，当前画面需要重新生成。</p>}
    <div className="shot-actions">
      <button className="btn-ghost-cosmic" disabled={disabled} onClick={() => { setDraft({ ...shot }); setEditing(!editing); }} aria-expanded={editing}><Icon name="edit" size={13} />{editing ? "收起编辑" : "编辑分镜"}</button>
      <div><button className="text-link" disabled={disabled} onClick={() => setShowFeedback(v => !v)} aria-expanded={showFeedback}>重画要求{feedback && <span className="online-dot" />}</button><button className="btn-ghost-cosmic" disabled={disabled} onClick={() => void run(async () => { await api.regenerateShot(project.id, shot.index, feedback); setFeedback(""); }, "本镜画面已更新，其他镜头保持不变")}><Icon name="refresh" size={13} />{pending ? "处理中…" : "重新生成本镜"}</button></div>
    </div>
    {showFeedback && <input aria-label={`第${shot.index}镜重生成要求`} className="input-cosmic shot-feedback" placeholder="希望这个镜头有什么变化？例如：改为雨夜、增加近景…" maxLength={1000} value={feedback} disabled={disabled} onChange={e => setFeedback(e.target.value)} />}
    {!project.actions.can_edit_shots && <p className="text-xs text-cream/40">可在分镜确认、画面确认或本轮结束后编辑。</p>}
    {editing && <form className="space-y-3 rounded-2xl bg-white/5 p-3" onSubmit={e => { e.preventDefault(); void run(async () => {
      const { index: _index, ...fields } = draft;
      await api.editShot(project.id, shot.index, fields); setEditing(false);
    }, "分镜已保存；修改画面内容后请重新生成本镜"); }}>
      {([['scene', '场景'], ['camera', '镜头运动'], ['action', '画面动作'], ['dialogue', '对白'], ['image_prompt', '画面提示词（留空自动生成）'], ['video_prompt', '视频提示词（留空自动生成）']] as const).map(([key, label]) => <label key={key} className="block text-xs text-cream/60">{label}
        <textarea aria-label={label} className="input-cosmic mt-1 min-h-16" value={draft[key] ?? ""} maxLength={key === "scene" ? 2000 : key === "camera" ? 1000 : 4000} disabled={disabled} onChange={e => setDraft({ ...draft, [key]: e.target.value, ...(["scene", "camera", "action"].includes(key) ? { image_prompt: "", video_prompt: "" } : key === "dialogue" ? { video_prompt: "" } : {}) })} />
      </label>)}
      <label className="block text-xs text-cream/60">时长（秒）<input aria-label="镜头时长" type="number" min="0.1" max="60" step="0.1" className="input-cosmic mt-1" value={draft.duration ?? 4} disabled={disabled} onChange={e => setDraft({ ...draft, duration: Number(e.target.value) })} /></label>
      <fieldset className="flex flex-wrap gap-3"><legend className="mb-2 text-xs text-cream/60">出场角色</legend>{(project.content.characters ?? []).map((c, i) => <label key={i} className="flex items-center gap-1 text-sm"><input type="checkbox" disabled={disabled} checked={draft.character_ids.includes(i)} onChange={e => setDraft({ ...draft, character_ids: e.target.checked ? [...draft.character_ids, i] : draft.character_ids.filter(v => v !== i), image_prompt: "", video_prompt: "" })} />{c.name}</label>)}</fieldset>
      <div className="flex gap-2"><button className="btn-candy text-xs" disabled={disabled}>保存分镜</button><button type="button" className="btn-ghost-cosmic text-xs" onClick={() => setEditing(false)}>取消</button></div>
    </form>}
    <Notice message={message} />
  </div>;
}
