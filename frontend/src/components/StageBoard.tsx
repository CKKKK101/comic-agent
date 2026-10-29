import { useEffect, useMemo, useRef, useState } from "react";
import { clsx } from "clsx";

import type { Shot, ShotImage } from "../types";
import { isPlaceholder } from "../types";
import { Icon } from "./Icon";
import type { IconName } from "./Icon";
import { ProjectLibrary, ReferencePanel, SaveCharacterButton, ShotTools } from "./CreativePanels";
import type { CreativeProps } from "./CreativePanels";

type TabKey = "overview" | "characters" | "shots" | "video" | "references" | "library";

type Props = CreativeProps;

/** 内容舞台：按产物分区浏览、编辑和预览。 */
export function StageBoard({ project, busy, onAction }: Props) {
  const content = project.content;
  const tabs: { key: TabKey; label: string; icon: IconName; count?: number }[] = [
    { key: "overview", label: "大纲", icon: "book" },
    { key: "characters", label: "角色", icon: "users", count: content.characters?.length },
    { key: "shots", label: "分镜", icon: "film", count: content.shots?.length },
    { key: "video", label: "成片", icon: "play" },
    { key: "references", label: "参考图", icon: "image", count: content.reference_images?.length },
    { key: "library", label: "角色库", icon: "folder" },
  ];

  const stage = project.latest_run?.current_stage ?? null;
  const autoTab = useMemo<TabKey | null>(() => {
    if (!stage) return null;
    if (stage.includes("outline")) return "overview";
    if (stage.includes("character")) return "characters";
    if (stage.includes("shot")) return "shots";
    if (stage.includes("compose")) return "video";
    return null;
  }, [stage]);

  const [tab, setTab] = useState<TabKey | null>(null);
  // 换项目/内容被清空(整轮重生成)后,手动选的 tab 不再压过内容事实
  useEffect(() => setTab(null), [project.id]);
  const active: TabKey | null =
    tab ?? autoTab ?? "overview";

  return (
    <section className="stage-board" aria-label="内容舞台">
      <div className="stage-tabs" aria-label="内容分区">
        {tabs.map((t) => (
          <button
            key={t.key}
            className={clsx("stage-tab", active === t.key && "is-active")}
            aria-pressed={active === t.key}
            aria-label={t.label}
            onClick={() => setTab(t.key)}
          >
            <Icon name={t.icon} size={14} />{t.label}{Boolean(t.count) && <span className="stage-tab-count">{String(t.count).padStart(2, "0")}</span>}
          </button>
        ))}
        {tabs.length === 0 && <span className="text-xs text-cream/40">内容将随生成进度出现</span>}
      </div>

      <div className="stage-body">
        {active === "overview" && <OverviewPanel project={project} />}
        {active === "characters" && <CharactersPanel project={project} busy={busy} onAction={onAction} />}
        {active === "shots" && <ShotsPanel project={project} busy={busy} onAction={onAction} />}
        {active === "video" && <VideoPanel project={project} />}
        {active === "references" && <ReferencePanel project={project} busy={busy} onAction={onAction} />}
        {active === "library" && <ProjectLibrary project={project} busy={busy} onAction={onAction} />}
      </div>
    </section>
  );
}

function PanelCard({ title, children }: { title?: string; children: React.ReactNode }) {
  return (
    <div className="cosmic-card p-4">
      {title && <h3 className="mb-2 font-comic text-base font-semibold text-star">{title}</h3>}
      {children}
    </div>
  );
}

function Empty({ note }: { note: string }) {
  return (
    <div className="stage-empty"><span><Icon name="layers" size={24} /></span>
      <p className="text-sm">{note}</p>
    </div>
  );
}

function OverviewPanel({ project }: Props) {
  const o = project.content.outline;
  if (!o) return <Empty note="大纲生成后会显示在这里" />;
  return (
    <div className="space-y-4">
      <PanelCard>
        <h2 className="font-comic text-2xl font-semibold text-cream">{o.title ?? "未命名"}</h2>
        <p className="mt-2 text-sm leading-relaxed text-cream/80">{o.logline}</p>
        <div className="mt-3 flex flex-wrap gap-1.5">
          {(o.genre ?? []).map((g) => (
            <span key={g} className="chip bg-comet/20 text-comet">{g}</span>
          ))}
          {(o.themes ?? []).map((t) => (
            <span key={t} className="chip bg-grape/20 text-grape">{t}</span>
          ))}
        </div>
        {o.setting && <Meta label="世界观" value={o.setting} />}
        {o.tone && <Meta label="基调" value={o.tone} />}
        {o.style_note && <Meta label="风格" value={o.style_note} />}
      </PanelCard>
      <PanelCard title="三幕结构">
        <ol className="space-y-2">
          {(o.acts ?? []).map((act, i) => (
            <li key={i} className="rounded-2xl bg-white/5 p-3">
              <div className="font-comic text-sm font-semibold text-comet">{act.title}</div>
              <p className="mt-0.5 text-sm text-cream/80">{act.plot}</p>
            </li>
          ))}
        </ol>
      </PanelCard>
    </div>
  );
}

function CharactersPanel({ project, busy, onAction }: Props) {
  const chars = project.content.characters ?? [];
  const images = new Map(
    (project.content.character_images ?? []).map((im) => [im.character_name, im]),
  );
  if (!chars.length) return <Empty note="角色生成后将在这里呈现，也可以先从角色库加入熟悉的面孔。" />;
  return (
    <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
      {chars.map((c, i) => {
        const img = images.get(c.name);
        return (
          <PanelCard key={i}>
            <div className="flex items-start gap-3">
              <div>
                <h3 className="font-comic text-lg font-semibold text-cream">{c.name}</h3>
                <p className="mt-0.5 text-xs font-bold text-grape">{c.personality}</p>
                <p className="mt-1.5 text-sm leading-relaxed text-cream/80">{c.appearance}</p>
                {c.quirks && <p className="mt-1 text-xs text-cream/50">{c.quirks}</p>}
              </div>
            </div>
            <div className="mt-3">
              <AssetImage url={img?.url} alt={`${c.name} 立绘`} />
            </div>
            <SaveCharacterButton project={project} index={i} busy={busy} onAction={onAction} />
          </PanelCard>
        );
      })}
    </div>
  );
}

function ShotsPanel({ project, busy, onAction }: Props) {
  const shots = project.content.shots ?? [];
  const imgs = new Map<number, ShotImage>(
    (project.content.shot_images ?? []).map((s) => [s.shot_index, s]),
  );
  const chars = project.content.characters ?? [];
  if (!shots.length) return <Empty note="故事的每一个镜头都会在这里展开，先从生成故事大纲开始。" />;
  return (
    <div className="space-y-4">
      {shots.map((s) => (
        <ShotCard key={s.index} shot={s} image={imgs.get(s.index)} characters={chars}
          tools={<ShotTools project={project} shot={s} busy={busy} onAction={onAction} />} />
      ))}
    </div>
  );
}

function ShotCard({
  shot,
  image,
  characters,
  tools,
}: {
  shot: Shot;
  image?: ShotImage;
  characters: { name: string }[];
  tools?: React.ReactNode;
}) {
  const present = (shot.character_ids ?? [])
    .map((i) => characters[i]?.name)
    .filter(Boolean)
    .join("、");
  return (
    <PanelCard>
      <div className="shot-layout">
        <AssetImage url={image?.url} alt={`第 ${shot.index} 镜`} />
        <div>
          <span className="shot-number">SHOT / {String(shot.index).padStart(2, "0")}</span>
          <div className="flex flex-wrap items-center gap-2"><h3 className="shot-title">第 {shot.index} 镜</h3>{present && <span className="chip bg-comet/10 text-comet">{present}</span>}</div>
          <p className="shot-detail"><span>场景</span>{shot.scene}</p>
          <p className="shot-detail"><span>运镜</span>{shot.camera}</p>
          <p className="shot-detail"><span>动作</span>{shot.action}</p>
          <p className="mt-2 flex items-center gap-1.5 text-[9px] text-cream/50"><Icon name="clock" size={11} />{shot.duration ?? 4} 秒</p>
          {shot.dialogue && (
            <p className="shot-dialogue">
              {shot.dialogue}
            </p>
          )}
        </div>
      </div>
      {tools}
    </PanelCard>
  );
}

function VideoPanel({ project }: Props) {
  const v = project.content.video;
  if (!v?.url) return <Empty note="成片合成后会显示在这里" />;
  const real = !isPlaceholder(v.url);
  return (
    <div className="space-y-4">
      <PanelCard>
        <h3 className="mb-2 font-comic text-base font-semibold text-star">漫剧成片</h3>
        {real ? (
          <video
            src={v.url}
            controls
            className="mx-auto max-h-[62vh] w-full rounded-2xl bg-black/40"
            playsInline
          />
        ) : (
          <div className="flex flex-col items-center gap-2 rounded-2xl border-2 border-dashed border-white/15 py-10 text-center">
            <p className="text-sm text-cream/70">占位成片(fake provider,未走真实视频合成)</p>
            <p className="break-all px-4 text-[11px] text-cream/40">{v.url}</p>
          </div>
        )}
        {v.notes && <p className="mt-2 text-xs text-cream/50">{v.notes}</p>}
      </PanelCard>
    </div>
  );
}

function AssetImage({ url, alt }: { url?: string; alt: string }) {
  const preview = useRef<HTMLDialogElement>(null);
  if (!url || isPlaceholder(url)) {
    return (
      <div className="asset-placeholder"><Icon name="image" size={25} />
        {url ? "离线预览 · 画面占位" : "等待画面生成"}
      </div>
    );
  }
  return (
    <><button className="asset-preview-button" aria-label={`预览${alt}`} title="点击查看大图" onClick={() => preview.current?.showModal()}><img src={url} alt={alt} loading="lazy" /></button><dialog ref={preview} className="image-dialog" aria-label={alt} onClick={e => { if (e.target === e.currentTarget) preview.current?.close(); }}><header><span>{alt}</span><div className="flex items-center gap-2"><a href={url} download className="icon-button" aria-label="下载图片"><Icon name="download" size={17} /></a><button className="icon-button" onClick={() => preview.current?.close()} aria-label="关闭预览"><Icon name="close" /></button></div></header><img src={url} alt={alt} /></dialog></>
  );
}

function Meta({ label, value }: { label: string; value?: string }) {
  if (!value) return null;
  return (
    <p className="mt-1.5 text-sm text-cream/80">
      <span className="font-bold text-cream/50">{label}: </span>
      {value}
    </p>
  );
}

