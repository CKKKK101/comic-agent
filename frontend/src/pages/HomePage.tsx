import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api } from "../api";
import { TopBar } from "../components/TopBar";
import { Icon } from "../components/Icon";
import type { ProjectView } from "../types";

const STYLES = [
  { value: "日系动漫", desc: "清晰线稿 · 细腻叙事", swatch: "anime" },
  { value: "国风水墨", desc: "留白意境 · 东方韵味", swatch: "ink" },
  { value: "赛博朋克", desc: "霓虹都市 · 未来想象", swatch: "cyber" },
  { value: "水彩童话", desc: "柔和笔触 · 温暖奇遇", swatch: "water" },
];

export function HomePage() {
  const navigate = useNavigate();
  const [topic, setTopic] = useState("");
  const [style, setStyle] = useState(STYLES[0].value);
  const [customStyle, setCustomStyle] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [recent, setRecent] = useState<ProjectView[]>([]);
  const effectiveStyle = customStyle.trim() || style;
  useEffect(() => { let active = true; void api.listProjects().then(rows => { if (active) setRecent(rows.slice(0, 3)); }).catch(() => {}); return () => { active = false; }; }, []);
  const create = async () => {
    if (!topic.trim() || busy) return;
    setBusy(true); setError("");
    try { const p = await api.createProject(topic.trim(), effectiveStyle); navigate(`/project/${p.id}`); }
    catch (e) { setError(e instanceof Error ? e.message : "创建失败，请重试"); setBusy(false); }
  };
  return <div className="flex h-full flex-col"><TopBar /><main className="page-scroll"><div className="studio-page">
    <section className="home-hero">
      <div><p className="page-eyebrow"><span className="online-dot" /> YOUR NEXT STORY STARTS HERE</p><h1>把一个想法，<br />变成<span>一部好故事。</span></h1><p className="page-description">从第一句灵感，到最后一个镜头。<br />和 AI 一起，完成属于你的漫剧创作。</p></div>
      <StoryIllustration />
    </section>
    <div className="creation-layout">
      <section className="cosmic-card creation-card">
        <div className="section-heading"><h2><Icon name="edit" size={17} />写下你的故事</h2><span className="creation-hint">{topic.length} / 2000</span></div>
        <p className="page-description" style={{ fontSize: 10 }}>一个角色、一次相遇，或一个久久难忘的画面，都可以是起点。</p>
        <textarea aria-label="故事创意" className="input-cosmic story-input" placeholder="例如：一位深夜修表匠发现，每一块停摆的手表里，都藏着一段尚未说出口的告别……" maxLength={2000} value={topic} onChange={e => setTopic(e.target.value)} onKeyDown={e => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey) && !e.nativeEvent.isComposing) void create(); }} />
        {error && <p role="alert" className="mb-3 text-xs text-candy">{error}</p>}
        <div className="creation-footer"><span className="creation-hint"><span className="chip bg-star/10 text-star">{effectiveStyle}</span><span className="ml-2 hidden sm:inline">Ctrl + Enter 创建</span></span><button className="btn-candy" disabled={!topic.trim() || busy} onClick={() => void create()}><Icon name="sparkle" size={15} />{busy ? "正在创建…" : "开始我的创作"}<Icon name="arrow" size={15} /></button></div>
      </section>
      <aside className="cosmic-card style-panel"><h2 className="style-heading">选择一种视觉语言</h2><div className="style-options">{STYLES.map(s => <button key={s.value} className={`style-option${!customStyle.trim() && style === s.value ? " selected" : ""}`} onClick={() => { setStyle(s.value); setCustomStyle(""); }} aria-pressed={!customStyle.trim() && style === s.value}><span className={`style-swatch ${s.swatch}`} /><span><strong>{s.value}</strong><small>{s.desc}</small></span>{!customStyle.trim() && style === s.value && <Icon name="check" size={13} />}</button>)}</div><input aria-label="自定义视觉风格" className="input-cosmic mt-3" style={{ fontSize: 10 }} placeholder="或输入你想要的风格…" maxLength={200} value={customStyle} onChange={e => setCustomStyle(e.target.value)} /></aside>
    </div>
    <div className="workflow-note"><Icon name="layers" size={19} /><span><strong className="text-cream/80">创作的每一步，都由你决定。</strong> 大纲 → 角色 → 分镜 → 画面 → 成片，每个阶段都可以确认、修改，再继续。</span></div>
    {recent.length > 0 && <section className="recent-section"><div className="section-heading"><h2>继续上次的灵感</h2><Link to="/projects" className="text-link">全部项目<Icon name="arrow" size={13} /></Link></div><div className="recent-grid">{recent.map(p => <Link key={p.id} to={`/project/${p.id}`} className="cosmic-card recent-card"><div className="recent-card-top"><Icon name="film" size={20} /><span>PROJECT / {String(p.id).padStart(3, "0")}</span></div><strong>{p.content.outline?.title || p.title || p.topic}</strong><p>{p.style} · {p.content.shots?.length ?? 0} 个分镜</p></Link>)}</div></section>}
  </div></main></div>;
}

function Scene({ warm = false }: { warm?: boolean }) {
  return <svg viewBox="0 0 240 160" aria-hidden="true"><rect width="240" height="160" fill={warm ? "#aa7050" : "#365762"} /><circle cx="177" cy="45" r="23" fill={warm ? "#f2cf94" : "#e7dbb0"} /><path d="m0 111 34-25 36 17 36-37 43 38 24-19 67 29v46H0Z" fill={warm ? "#5c4e45" : "#27424b"} /><path d="m0 128 58-19 37 11 55-17 90 26v31H0Z" fill={warm ? "#38413e" : "#1f363b"} /><path d="M0 150h240 M130 80l-11 80 M112 107h39" stroke={warm ? "#dcb994" : "#78948c"} opacity=".55" /><path d="M27 109h54v26H27z" fill="#c8bc9c" /><path d="M31 114h11v9H31z M47 114h11v9H47z M63 114h11v9H63z" fill="#3e5d5b" /><path d="M26 136h60 M31 139h55" stroke="#9aa48c" strokeWidth="2" /><path d="M168 114v30 M166 112h4 M171 122l8 11 M164 122l-6 9" stroke="#bdc3b1" strokeWidth="3" strokeLinecap="round" /></svg>;
}
function StoryIllustration() {
  return <div className="hero-art" aria-hidden="true"><div className="story-frame back"><Scene /><div><span>01 / THE IDEA</span><span>16:9</span></div></div><div className="story-frame front"><Scene warm /><div><span>03 / THE STORY</span><span>16:9</span></div></div><div className="story-frame middle"><Scene /><div><span>02 / THE MOMENT</span><span>16:9</span></div></div><span className="hero-art-caption"><Icon name="film" size={12} /> EVERY FRAME TELLS A STORY</span></div>;
}
