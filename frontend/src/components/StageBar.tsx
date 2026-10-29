import type { ProjectView } from "../types";
import { STAGE_LABEL } from "../types";
import { Icon } from "./Icon";

interface Props { project: ProjectView; busy: boolean; onGenerate: (stage?: string) => void; onResume: () => void; onCancel: () => void; }
const steps = ["故事大纲", "角色设定", "分镜脚本", "角色形象", "分镜画面", "成片合成"];
const anchors: Record<string, number> = {
  plan_outline: 0, outline_approval: 0, plan_characters: 1, characters_approval: 1,
  plan_shots: 2, shots_approval: 2, render_characters: 3, critique_character_images: 3,
  character_images_approval: 3, render_shots: 4, critique_shot_images: 4,
  shot_images_approval: 4, compose: 5, compose_approval: 5,
};
const labels: Record<string, string> = { draft: "待创作", generating: "创作进行中", succeeded: "创作已完成", failed: "需要处理", interrupted: "已暂停" };
export function StageBar({ project, busy, onGenerate, onResume, onCancel }: Props) {
  const run = project.latest_run;
  const waiting = run?.status === "running" ? project.actions.waiting_gate : null;
  const dirty = project.content.video_dirty && !project.content.video?.url;
  const progress = dirty ? 83 : Math.round((run?.progress ?? 0) * 100);
  const current = dirty ? 5 : run?.status === "succeeded" ? 6 : anchors[run?.current_stage ?? ""] ?? -1;
  const hasContent = Boolean(project.content.outline);
  return <section className="workflow-toolbar" aria-label="创作流程">
    <div className="workflow-toolbar-top"><div className="workflow-status"><span className="online-dot" style={{ background: waiting || dirty ? "rgb(var(--accent))" : undefined }} /><strong>{waiting ? "等待你的确认" : dirty ? "成片待更新" : labels[project.status] ?? project.status}</strong><span className="workflow-progress"><span style={{ width: `${progress}%` }} /></span><span className="text-cream/40">{progress}%</span>{waiting && <span className="hidden text-star xl:inline">{STAGE_LABEL[waiting]}</span>}</div>
      <div className="workflow-actions">
        {project.actions.can_resume && <button className="btn-candy" disabled={busy} onClick={onResume}><Icon name="play" size={12} />恢复续跑</button>}
        {project.actions.can_cancel && <button className="btn-ghost-cosmic" disabled={busy} onClick={onCancel}><Icon name="stop" size={11} />停止生成</button>}
        {project.actions.can_generate && <button className={hasContent ? "btn-ghost-cosmic" : "btn-candy"} disabled={busy} onClick={() => onGenerate()}><Icon name={hasContent ? "refresh" : "sparkle"} size={12} />{hasContent ? "重新创作" : "开始生成"}</button>}
        {project.actions.can_generate && project.content.video?.url && <button className="btn-candy" onClick={() => onGenerate("compose")} disabled={busy}><Icon name="film" size={13} />重新合成成片</button>}
        {project.actions.can_complete_video && <button className="btn-candy" onClick={() => onGenerate("compose")} disabled={busy}><Icon name="film" size={13} />补生成成片</button>}
        {project.actions.video_blocker && <span className="hidden text-[9px] text-cream/40 xl:inline" title={project.actions.video_blocker}>{project.actions.video_blocker.slice(0, 18)}…</span>}
      </div>
    </div>
    <nav className="workflow-steps" aria-label="生成阶段">{steps.map((label, i) => <div key={label} className="contents"><span className={`workflow-step${i === current ? " is-current" : i < current ? " is-past" : ""}`} aria-current={i === current ? "step" : undefined}><span className="workflow-step-number">{i < current ? <Icon name="check" size={10} /> : String(i + 1).padStart(2, "0")}</span><span>{label}</span></span>{i < 5 && <span className="workflow-connector" />}</div>)}</nav>
  </section>;
}
