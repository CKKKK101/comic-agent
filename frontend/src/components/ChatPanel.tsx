import { useEffect, useRef, useState } from "react";

import type { ProjectView } from "../types";
import { STAGE_LABEL } from "../types";
import { Icon } from "./Icon";

interface Props {
  project: ProjectView;
  busy: boolean;
  onConfirm: (feedback: string) => Promise<boolean>;
  onCancel: () => void;
}

/** 创作记录侧栏 + 审批条

输入框只在等待确认时出现:同一输入框写修改意见(回车/提交修改),无意见点「通过」。
非等待状态不显示输入框 —— 没有独立反馈通道,门禁外发消息无意义。

防重复裁决(与后端 gateway 状态机配套):裁决成功后被批准的那条门提示消息
(id) 记下来,在下一个门提示消息出现前禁用本确认条 —— 门与门之间
(current_stage 仍停留在旧门名)的第二次点击被 UI 拦掉,而不是等后端 409。
*/
/** 已裁决标记:run 离开被确认的门后,再停到同一道门 = 新的停留(须重新可确认) */
interface AckedGate {
  runId: number;
  stage: string;
  departed: boolean;
}

export function ChatPanel({ project, busy, onConfirm, onCancel }: Props) {
  const [input, setInput] = useState("");
  const [acked, setAcked] = useState<AckedGate | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const gate = project.actions.waiting_gate;
  const runId = project.latest_run?.id ?? 0;
  const awaiting = Boolean(gate) && !busy;

  // 换项目时清掉上个项目的已裁决标记
  useEffect(() => setAcked(null), [project.id]);

  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [project.messages.length, project.actions.waiting_gate]);

  // 观察到 run 离开了被确认的门(waiting_gate 消失/换成别的门)→ departed。
  // 之后同一门再次出现 = 新一轮停留(如回炉重停),确认条必须重新可用;
  // 没离开前(stage 滞留在旧门名)保持禁用,防"第二次点击被下一道门吞掉"。
  useEffect(() => {
    if (!acked) return;
    if (acked.runId !== runId || project.actions.waiting_gate !== acked.stage) {
      setAcked((a) => (a && a.runId === runId ? { ...a, departed: true } : a));
    }
  }, [project.actions.waiting_gate, runId, acked?.runId]); // eslint-disable-line react-hooks/exhaustive-deps

  const justAcked =
    awaiting && acked !== null && !acked.departed && gate === acked.stage && acked.runId === runId;

  const approve = async (feedback: string) => {
    const ok = await onConfirm(feedback);
    if (ok) {
      setInput("");
      if (gate && runId) setAcked({ runId, stage: gate, departed: false });
    }
  };

  return (
    <section className="record-panel" aria-label="创作记录">
      {/* 头部:当前阶段说明 */}
      <div className="record-header">
        <h2><Icon name="message" size={15} />创作记录 <span className="record-count">{project.messages.length}</span></h2>
        {project.actions.can_cancel && (
          <button
            className="icon-button"
            onClick={onCancel}
            disabled={busy}
            aria-label="停止当前生成"
            title="停止当前生成"
          >
            <Icon name="stop" size={12} />
          </button>
        )}
      </div>
      <div className="record-current"><span className="online-dot" />{STAGE_LABEL[project.latest_run?.current_stage ?? ""] ?? "准备开始新的故事"}</div>

      {/* 消息流 */}
      <div
        ref={scrollRef}
        className="record-list"
      >
        {project.messages.length === 0 && (
          <div className="flex h-full flex-col items-center justify-center gap-1 text-center">
            <Icon name="sparkle" size={25} className="mb-3 text-star/60" /><p className="text-xs text-cream/50">
              {project.status === "draft"
                ? "点击「开始生成」，让故事慢慢成形"
                : "还没有消息"}
            </p>
          </div>
        )}
        {project.messages.map((msg) => (
          <div key={msg.id} className={msg.role === "user" ? "speech-bubble-user" : "speech-bubble"}>
            {msg.agent !== "user" && (
              <div className="record-agent">
                <span aria-hidden />
                {agentLabel(msg.agent)}
              </div>
            )}
            <div className="whitespace-pre-wrap break-words">{msg.content}</div>
          </div>
        ))}
      </div>

      {/* 审批条(等待人工确认时出现):单输入框写意见,通过/提交二选一 */}
      {awaiting && (
        <div className="record-approval space-y-2">
          <div className="flex items-center justify-between gap-2">
            <span className="record-approval-title mb-0">
              <Icon name="check" size={13} />{STAGE_LABEL[gate!] ?? gate}
            </span>
            <button
              className="btn-candy px-4 py-1 text-sm"
              onClick={() => void approve("")}
              disabled={busy || justAcked}
            >
              <Icon name="check" size={12} />通过
            </button>
          </div>
          <div className="flex items-end gap-2">
            {justAcked ? (
              <span className="text-xs text-mint">已提交裁决,等待执行…(下一轮确认会自动重新开启)</span>
            ) : (
              <>
                <input
                  className="input-cosmic"
                  placeholder="写下需要调整的地方…"
                  aria-label="修改意见"
                  value={input}
                  onChange={(e) => setInput(e.target.value)}
                  onKeyDown={(e) => {
                    // isComposing:中文输入法选词上屏的 Enter 不是"提交"
                    if (e.key === "Enter" && !e.nativeEvent.isComposing) void approve(input);
                  }}
                />
                <button
                  className="btn-ghost-cosmic shrink-0 px-3.5 py-2 text-sm"
                  onClick={() => void approve(input)}
                  disabled={!input.trim() || busy}
                >
                  提交修改
                </button>
              </>
            )}
          </div>
        </div>
      )}
    </section>
  );
}

function agentLabel(agent: string): string {
  if (agent === "user") return "我";
  return STAGE_LABEL[agent] ?? agent;
}
