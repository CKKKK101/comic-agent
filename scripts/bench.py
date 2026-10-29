"""comic-agent 基准:一键复现实测数字(自起自停基准服务,零外部调用、零花费)。

覆盖:全流程耗时 / 消息幂等 / 并发保护 / 读模型延迟 / 人工门推进 /
定向回炉的重跑范围 / 崩溃恢复(kill -9 → 重启 interrupted → resume 零重放)。

    uv run python scripts/bench.py          # 跑完自动清理 .bench.db
    uv run python scripts/bench.py --keep   # 保留基准库,便于排查

与开发环境隔离:独立端口(18777)+ 独立库(.bench.db)+ 强制 fake provider,
不动你的开发库、不碰 Docker 容器;所有等待都带超时,断言失败即非零退出。
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / ".bench.db"
PORT = 18777
BASE = f"http://127.0.0.1:{PORT}/api/v1"
HEALTH = f"http://127.0.0.1:{PORT}/health"

client = httpx.Client(timeout=30)
report: list[tuple[str, str]] = []


def log(section: str, text: str) -> None:
    report.append((section, text))
    print(f"[{section}] {text}", flush=True)


# ---------------------------------------------------------------------------
# 基准服务(自起自停;kill() 等价 SIGKILL,用于崩溃恢复段)
# ---------------------------------------------------------------------------

def start_server() -> subprocess.Popen:
    env = {
        **os.environ,
        "DATABASE_URL": "sqlite+aiosqlite:///./.bench.db",
        "TEXT_PROVIDER": "fake",
        "IMAGE_PROVIDER": "fake",
        "VIDEO_PROVIDER": "fake",
        "TTS_ENABLED": "0",
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(PORT), "--log-level", "warning"],
        cwd=ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            if httpx.get(HEALTH, timeout=1).status_code == 200:
                return proc
        except httpx.HTTPError:
            time.sleep(0.2)
    proc.kill()
    raise RuntimeError("基准服务启动失败(30s 内 /health 未就绪)")


def wait_server_down(timeout: float = 10) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            httpx.get(HEALTH, timeout=1)
        except httpx.HTTPError:
            return
        time.sleep(0.2)
    raise RuntimeError("基准服务未按预期退出")


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------

def create(topic: str) -> int:
    return client.post(f"{BASE}/projects", json={"topic": topic}).json()["id"]


def view(pid: int) -> dict:
    return client.get(f"{BASE}/projects/{pid}").json()


def agent_counts(pid: int) -> dict[str, int]:
    con = sqlite3.connect(DB)
    try:
        rows = con.execute(
            "SELECT agent, COUNT(*) FROM message WHERE project_id=? GROUP BY agent", (pid,)
        ).fetchall()
    finally:
        con.close()
    return {a: n for a, n in rows}


def wait_for(pid: int, pred, timeout: float, what: str):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = view(pid)["latest_run"]
        if pred(last):
            return last
        time.sleep(0.02)
    raise TimeoutError(f"等待「{what}」超时,最后 run={last}")


def confirm(pid: int, run_id: int, feedback: str) -> int:
    """确认门;返回重试次数。门已可见但 wait 未进入的毫秒窗口会 409,重试即成功;
    run 已结束返回 -1(最后一道门确认后的收尾窗口属合法终局)。"""
    retries = 0
    deadline = time.time() + 5
    while time.time() < deadline:
        resp = client.post(
            f"{BASE}/projects/{pid}/confirm", json={"run_id": run_id, "feedback": feedback}
        )
        if resp.status_code == 200:
            return retries
        assert resp.status_code == 409, resp.text
        if view(pid)["latest_run"]["status"] != "running":
            return -1
        retries += 1
        time.sleep(0.02)
    raise AssertionError("confirm 持续 409")


def pct(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(len(xs) * p))], 1)


# ---------------------------------------------------------------------------
# 各段测量
# ---------------------------------------------------------------------------

def m1_full_run() -> int:
    pid = create("基准:咖啡师与时间旅行者")
    t0 = time.perf_counter()
    client.post(f"{BASE}/projects/{pid}/generate", json={"auto_mode": True})
    wait_for(pid, lambda r: r["status"] != "running", 60, "auto 全流程到终态")
    log("流程", f"fake 全流程(auto,6 门)= {time.perf_counter() - t0:.2f}s")

    content = view(pid)["content"]
    log(
        "规模",
        f"内容 = {len(content.get('characters') or [])} 角色 / {len(content.get('shots') or [])} 镜 / "
        f"{len(content.get('character_images') or [])} 立绘 / {len(content.get('shot_images') or [])} 分镜图",
    )
    counts = agent_counts(pid)
    dup = {a: n for a, n in counts.items() if n > 1 and not a.endswith("_approval") and a != "user"}
    log("幂等", f"阶段消息 {sum(counts.values())} 条,重复 = {dup or '无'}")
    assert not dup, f"出现重复消息: {dup}"
    return pid


def m2_concurrency() -> None:
    pid = create("基准:并发保护")
    codes: list[int] = []
    lock = threading.Lock()

    def fire() -> None:
        r = client.post(f"{BASE}/projects/{pid}/generate", json={"auto_mode": True})
        with lock:
            codes.append(r.status_code)

    threads = [threading.Thread(target=fire) for _ in range(20)]
    t0 = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    window = time.perf_counter() - t0
    hist = {c: codes.count(c) for c in sorted(set(codes))}
    log("并发", f"20 并发 generate → {hist},仲裁窗口 {window:.2f}s")
    assert hist.get(200) == 1 and hist.get(409) == 19, hist
    wait_for(pid, lambda r: r["status"] != "running", 30, "并发段的 run 收尾")


def m3_latency() -> None:
    pids = [create(f"基准:延迟样本{i}") for i in range(30)]
    detail: list[float] = []
    for i in range(100):
        t0 = time.perf_counter()
        client.get(f"{BASE}/projects/{pids[i % len(pids)]}")
        detail.append((time.perf_counter() - t0) * 1000)
    listing: list[float] = []
    for _ in range(20):
        t0 = time.perf_counter()
        client.get(f"{BASE}/projects")
        listing.append((time.perf_counter() - t0) * 1000)
    log(
        "延迟",
        f"详情(100 次)p50 {pct(detail, 0.5)}ms / p95 {pct(detail, 0.95)}ms;"
        f"列表 30 项目(20 次)p50 {pct(listing, 0.5)}ms / p95 {pct(listing, 0.95)}ms",
    )


def m4_manual_and_rework(pid: int) -> None:
    run_id = client.post(f"{BASE}/projects/{pid}/generate", json={"auto_mode": False}).json()["run_id"]
    confirms: list[tuple[str, int]] = []
    timeline: list[str] = []
    deadline = time.time() + 30
    while time.time() < deadline:
        run = view(pid)["latest_run"]
        assert run["status"] == "running", f"手动 run 意外结束: {run}"
        stage = run["current_stage"] or ""
        if not timeline or timeline[-1] != stage:
            timeline.append(stage)
        if stage == "shot_images_approval":
            break
        if stage.endswith("_approval"):
            confirms.append((stage, confirm(pid, run_id, "")))
            # 确认成功后必须等 run 真正离开这道门再继续:DB 阶段名存在滞后窗口,
            # 否则会对同一道门重复确认,重试会顺着窗口提前消费下一道门的停靠
            while (view(pid)["latest_run"]["current_stage"] or "") == stage:
                time.sleep(0.01)
        time.sleep(0.01)
    log("人工门", f"阶段轨迹 = {' → '.join(timeline)}")
    log(
        "人工门",
        f"确认 {len(confirms)} 次 = {[(g, f'重试{r}次') for g, r in confirms]}(毫秒竞态窗口需重试)",
    )

    before = agent_counts(pid)
    assert confirm(pid, run_id, "第2镜的画面改成雨夜霓虹街头") >= 0
    wait_for(pid, lambda r: agent_counts(pid).get("review", 0) > before.get("review", 0), 15, "review 分诊")
    t0 = time.perf_counter()
    wait_for(
        pid,
        lambda r: r["current_stage"] == "shot_images_approval"
        and agent_counts(pid).get("render_shots", 0) > before.get("render_shots", 0),
        15,
        "回炉后重新停门",
    )
    after = agent_counts(pid)
    gained = {a: [before.get(a, 0), n] for a, n in after.items() if n > before.get(a, 0)}
    untouched = [
        a
        for a in ("plan_outline", "plan_characters", "plan_shots", "render_characters")
        if after.get(a) == before.get(a)
    ]
    log("回炉", f"{time.perf_counter() - t0:.2f}s 回到门;重跑 = {gained}")
    log("回炉", f"前置 4 生产阶段零重跑 = {untouched}")
    assert len(untouched) == 4, f"前置阶段被重跑了: {untouched}"
    client.post(f"{BASE}/projects/{pid}/cancel")


def m5_crash_resume(proc: subprocess.Popen) -> subprocess.Popen:
    pid = create("基准:崩溃恢复")
    run_id = client.post(f"{BASE}/projects/{pid}/generate", json={"auto_mode": False}).json()["run_id"]
    wait_for(pid, lambda r: r["current_stage"] == "outline_approval", 20, "停在首门")
    before = agent_counts(pid)

    proc.kill()  # SIGKILL 等价:不给收尾机会(与断电/崩溃同语义)
    wait_server_down()
    proc = start_server()  # 重启:启动清扫把遗留 running 标 interrupted

    run = view(pid)["latest_run"]
    assert run["status"] == "interrupted", run
    log("崩溃", f"kill -9 → 重启后 status={run['status']}, stage={run['current_stage']}")

    t0 = time.perf_counter()
    assert client.post(f"{BASE}/projects/{pid}/resume").status_code == 200
    log("崩溃", f"resume 请求 {time.perf_counter() - t0:.3f}s")

    t0 = time.perf_counter()
    while True:
        run = view(pid)["latest_run"]
        if run["status"] != "running":
            break
        if (run["current_stage"] or "").endswith("_approval") and confirm(pid, run_id, "") < 0:
            break  # run 已结束(最后一道门确认后的收尾窗口)
        time.sleep(0.02)
    after = agent_counts(pid)
    log("崩溃", f"续跑到完成 {time.perf_counter() - t0:.2f}s")
    log(
        "崩溃",
        f"已完成阶段零重放:plan_outline {before.get('plan_outline')}→{after.get('plan_outline')},"
        f"outline_approval {before.get('outline_approval')}→{after.get('outline_approval')}",
    )
    assert after.get("plan_outline") == before.get("plan_outline") == 1
    assert view(pid)["latest_run"]["status"] == "succeeded"
    return proc


def main() -> int:
    parser = argparse.ArgumentParser(description="comic-agent 基准")
    parser.add_argument("--keep", action="store_true", help="保留 .bench.db 便于排查")
    args = parser.parse_args()

    for suffix in ("", "-wal", "-shm"):
        (ROOT / f".bench.db{suffix}").unlink(missing_ok=True)

    proc = start_server()
    try:
        pid = m1_full_run()
        m2_concurrency()
        m3_latency()
        m4_manual_and_rework(pid)
        proc = m5_crash_resume(proc)
    finally:
        proc.kill()
        wait_server_down()
        if not args.keep:
            for suffix in ("", "-wal", "-shm"):
                (ROOT / f".bench.db{suffix}").unlink(missing_ok=True)

    print("\n================ 实测汇总 ================")
    for section, text in report:
        print(f"{section}: {text}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
