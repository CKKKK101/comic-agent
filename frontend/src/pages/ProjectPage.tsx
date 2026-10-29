import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "../api";
import { ChatPanel } from "../components/ChatPanel";
import { StageBar } from "../components/StageBar";
import { StageBoard } from "../components/StageBoard";
import { TopBar } from "../components/TopBar";
import { Icon } from "../components/Icon";
import { useProjectPolling } from "../hooks/useProjectPolling";

export function ProjectPage() {
  const { id } = useParams();
  const projectId = Number(id ?? 0);
  const { project, error, busy, act } = useProjectPolling(projectId);
  const [activityOpen, setActivityOpen] = useState(true);
  const [mobilePanel, setMobilePanel] = useState<"content" | "activity">("content");
  const [actionError, setActionError] = useState("");
  const runAction = async (fn: () => Promise<unknown>) => {
    setActionError("");
    try { await act(fn); } catch (e) { setActionError(e instanceof Error ? e.message : "操作失败，请重试"); }
  };
  return <div className="flex h-full min-h-0 flex-col"><TopBar projectId={projectId} />
    {projectId <= 0 || !Number.isFinite(projectId) ? <div className="stage-empty">无效的项目 ID<Link to="/projects" className="btn-ghost-cosmic">返回项目列表</Link></div> : project ? <>
      <div className="workbench-title"><div className="min-w-0"><h1 title={project.topic}>{project.content.outline?.title || project.title || project.topic}</h1><div className="workbench-meta"><span><Icon name="film" size={12} />{project.style}</span><span><Icon name="layers" size={12} />{project.content.shots?.length ?? 0} 个分镜</span><span><Icon name="users" size={12} />{project.content.characters?.length ?? 0} 位角色</span>{project.content.video_dirty && <span className="text-star">成片待更新</span>}</div></div><div className="workbench-title-actions"><button className="btn-ghost-cosmic desktop-activity-toggle" onClick={() => setActivityOpen(v => !v)} aria-expanded={activityOpen}><Icon name="panel" size={14} />{activityOpen ? "收起记录" : "创作记录"}</button></div></div>
      {(actionError || (project.latest_run?.status === "failed" && project.latest_run.error)) && <div role="alert" className="mx-4 mb-3 rounded-lg border border-candy/20 bg-candy/5 px-3 py-2 text-xs text-candy">{actionError || project.latest_run?.error}</div>}
      <StageBar project={project} busy={busy} onGenerate={stage => void runAction(() => api.generate(projectId, stage))} onResume={() => void runAction(() => api.resume(projectId))} onCancel={() => void runAction(() => api.cancel(projectId))} />
      <div className="workbench-mobile-switch"><button className={mobilePanel === "content" ? "is-active" : ""} onClick={() => setMobilePanel("content")} aria-pressed={mobilePanel === "content"}><Icon name="layers" size={14} />内容舞台</button><button className={mobilePanel === "activity" ? "is-active" : ""} onClick={() => setMobilePanel("activity")} aria-pressed={mobilePanel === "activity"}><Icon name="message" size={14} />创作记录{project.actions.waiting_gate && <span className="online-dot" style={{ background: "rgb(var(--accent))" }} />}</button></div>
      <div className={`workbench-grid${!activityOpen ? " activity-collapsed" : ""}`} data-mobile-panel={mobilePanel}>
        <div className="workbench-content"><StageBoard key={project.id} project={project} busy={busy} onAction={async fn => { setActionError(""); await act(fn); }} /></div>
        {(activityOpen || mobilePanel === "activity") && <aside className="workbench-activity"><ChatPanel project={project} busy={busy} onConfirm={async feedback => {
          const runId = project.latest_run?.id;
          if (runId == null) return false;
          setActionError("");
          try { await act(() => api.confirm(projectId, runId, feedback)); return true; }
          catch (e) { setActionError(e instanceof Error ? e.message : "确认失败"); return false; }
        }} onCancel={() => void runAction(() => api.cancel(projectId))} /></aside>}
      </div>
    </> : <div className="stage-empty"><span><Icon name="film" size={24} /></span><p>{error ? `无法加载项目：${error}` : "正在准备你的创作空间…"}</p>{error && <Link to="/projects" className="btn-ghost-cosmic">返回项目列表</Link>}</div>}
  </div>;
}
