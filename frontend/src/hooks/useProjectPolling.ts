import { useCallback, useEffect, useRef, useState } from "react";

import { api } from "../api";
import type { ProjectView } from "../types";

const POLL_MS = 2500;

/** 轮询项目视图(3s 级;后端无 WS,单机轮询足够 demo 体验) */
export function useProjectPolling(projectId: number) {
  const [project, setProject] = useState<ProjectView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false); // 任意动作进行中
  const timer = useRef<number | null>(null);
  const seq = useRef(0); // 在途防护:只认最新一次请求,旧响应不得覆盖新状态

  const refresh = useCallback(async () => {
    const id = ++seq.current;
    try {
      const data = await api.getProject(projectId);
      if (seq.current !== id) return; // 已被更新的请求取代
      setProject(data);
      setError(null);
    } catch (e) {
      if (seq.current !== id) return;
      setError(e instanceof Error ? e.message : "加载失败");
    }
  }, [projectId]);

  useEffect(() => {
    void refresh();
    timer.current = window.setInterval(() => void refresh(), POLL_MS);
    return () => {
      if (timer.current != null) window.clearInterval(timer.current);
    };
  }, [refresh]);

  /** 包一层动作:完成后立即刷一次;把 fn 的结果透传出去(调用方可据成功与否更新状态) */
  const act = useCallback(
    async <T,>(fn: () => Promise<T>): Promise<T> => {
      setBusy(true);
      try {
        return await fn();
      } finally {
        setBusy(false);
        await refresh();
      }
    },
    [refresh],
  );

  return { project, error, busy, refresh, act };
}
