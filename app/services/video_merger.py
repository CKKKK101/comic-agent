"""视频片段合成:ffmpeg 把多段短视频拼成一部成片(漫剧画面拼接,无音轨工程)。

策略(先快后稳):
1. concat demuxer + `-c copy`:同源同参数片段(seedance 同模型产物)几乎总是
   直接成功 —— 零重编码,最快;
2. 失败(码流参数不一致等)→ concat filter 重编码(h264/yuv420p,丢音轨):
   保证任何片段组合都能出片。

错误分层:ffmpeg 缺失/超时/合成失败都抛带 user_message 的 MediaError;
list 文件放输出旁(uuid 文件名无特殊字符),结束后清理。
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from app.services.media import MediaError

logger = logging.getLogger(__name__)

MERGE_TIMEOUT_S = 600.0


async def merge_videos(segments: list[Path], output: Path, *, ffmpeg: str = "ffmpeg") -> None:
    """把 segments(本地 mp4 路径)按顺序拼成 output;copy 失败自动降级重编码。"""
    if not segments:
        raise MediaError("merge_videos: 没有可合成的片段", user_message="没有可合成的视频片段,请重试")

    output.parent.mkdir(parents=True, exist_ok=True)
    list_file = output.with_name(f"{output.name}.concat.txt")
    try:
        list_file.write_text(
            "".join(f"file '{s.as_posix()}'\n" for s in segments), encoding="utf-8"
        )

        # 1) 零重编码直拼
        copy_cmd = [
            ffmpeg, "-y",
            "-f", "concat", "-safe", "0", "-i", str(list_file),
            "-c", "copy", str(output),
        ]
        code, err = await _run_checked(copy_cmd, ffmpeg=ffmpeg)
        if code == 0 and output.exists() and output.stat().st_size > 0:
            logger.info("ffmpeg copy 拼接成功:%d 段 → %s", len(segments), output.name)
            return
        if output.exists():
            output.unlink()
        logger.warning("ffmpeg copy 拼接失败(code=%s),降级重编码:%s", code, err[-300:])

        # 2) 重编码降级(漫剧音轨未接入,画面拼接即可)
        inputs: list[str] = []
        for s in segments:
            inputs += ["-i", str(s)]
        labels = "".join(f"[{i}:v]" for i in range(len(segments)))
        filter_complex = f"{labels}concat=n={len(segments)}:v=1:a=0[v]"
        recode_cmd = [
            ffmpeg, "-y",
            *inputs,
            "-filter_complex", filter_complex,
            "-map", "[v]", "-c:v", "libx264", "-pix_fmt", "yuv420p",
            str(output),
        ]
        code, err = await _run_checked(recode_cmd, ffmpeg=ffmpeg)
        if code != 0 or not output.exists():
            if output.exists():
                output.unlink()
            raise MediaError(
                f"ffmpeg 重编码拼接失败(code={code}): {err[-800:]}",
                user_message="视频合成失败,请重试或查看日志",
            )
        logger.info("ffmpeg 重编码拼接成功:%d 段 → %s", len(segments), output.name)
    finally:
        if list_file.exists():
            list_file.unlink()


async def probe_duration(path: Path, *, ffprobe: str = "ffprobe") -> float | None:
    """探测媒体时长(秒)。失败返回 None(调用方兜底),不抛 —— 对齐是尽力而为。"""
    try:
        proc = await asyncio.create_subprocess_exec(
            ffprobe,
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (FileNotFoundError, OSError):
        logger.warning("找不到 ffprobe(%s),时长探测跳过", ffprobe)
        return None
    try:
        out, _err = await asyncio.wait_for(proc.communicate(), timeout=30.0)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return None
    if proc.returncode != 0:
        return None
    try:
        value = float(out.decode("utf-8", "replace").strip() or 0)
    except ValueError:
        return None
    return value if value > 0 else None


async def build_voiceover(
    audio_segments: list[Path],
    offsets_ms: list[int],
    output: Path,
    *,
    total_ms: int | None = None,
    ffmpeg: str = "ffmpeg",
) -> None:
    """把多段配音按起始毫秒错位混合成一条音轨(m4a)。

    adelay 负责"第 N 句在成片时间轴的第 N 镜起点响起",amix 混成单轨。
    total_ms = 成片总时长:超出的配音尾巴被 atrim 裁掉 —— 保证音轨
    【永远不会比视频长】,成片长度由画面说了算(见 mux_with_audio)。
    """
    if not audio_segments or len(audio_segments) != len(offsets_ms):
        raise MediaError(
            "build_voiceover: 配音段与偏移数不一致", user_message="配音合成参数错误,请重试"
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    inputs: list[str] = []
    for path in audio_segments:
        inputs += ["-i", str(path)]
    delays = "".join(f"[{i}:a]adelay={offsets_ms[i]}|{offsets_ms[i]}[d{i}];" for i in range(len(audio_segments)))
    mix_labels = "".join(f"[d{i}]" for i in range(len(audio_segments)))
    trim = f",atrim=0:{(total_ms or 0) / 1000:.3f}" if total_ms else ""
    filter_complex = f"{delays}{mix_labels}amix=inputs={len(audio_segments)}:normalize=0{trim}[aout]"
    cmd = [
        ffmpeg, "-y",
        *inputs,
        "-filter_complex", filter_complex,
        "-map", "[aout]",
        "-c:a", "aac",
        "-b:a", "192k",
        str(output),
    ]
    code, err = await _run_checked(cmd, ffmpeg=ffmpeg)
    if code != 0 or not output.exists():
        if output.exists():
            output.unlink()
        raise MediaError(
            f"配音混音失败(code={code}): {err[-500:]}",
            user_message="台词配音混合失败,成片保持静音",
        )
    logger.info("配音混音成功:%d 段 → %s", len(audio_segments), output.name)


async def mux_with_audio(video: Path, audio: Path, output: Path, *, ffmpeg: str = "ffmpeg") -> None:
    """把音轨并入成片:视频流 copy 不重编码,音频 copy。

    【不用 -shortest】:-shortest 会在音轨比视频短时把视频一起裁掉(实测:
    3×5s 成片被裁成最后一句台词结束的 ~10s)。音轨已在 build_voiceover
    里 atrim 到成片总长,这里输出时长 = max(两流) = 视频全长。
    """
    cmd = [
        ffmpeg, "-y",
        "-i", str(video),
        "-i", str(audio),
        "-map", "0:v",
        "-map", "1:a",
        "-c:v", "copy",
        "-c:a", "copy",
        str(output),
    ]
    code, err = await _run_checked(cmd, ffmpeg=ffmpeg)
    if code != 0 or not output.exists():
        if output.exists():
            output.unlink()
        raise MediaError(
            f"成片并入音轨失败(code={code}): {err[-500:]}",
            user_message="把配音并入成片失败,成片保持静音",
        )
    logger.info("配音并入成片成功:%s → %s", video.name, output.name)


async def _run_checked(cmd: list[str], *, ffmpeg: str) -> tuple[int, str]:
    """执行 ffmpeg,返回 (退出码, 错误输出尾部);缺失/超时转成带用户文案的 MediaError。"""
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
    except FileNotFoundError as exc:
        raise MediaError(
            f"找不到 ffmpeg 可执行文件: {ffmpeg}",
            user_message="未检测到 ffmpeg,请先安装并加入 PATH 后重试",
        ) from exc
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=MERGE_TIMEOUT_S)
    except TimeoutError as exc:
        proc.kill()
        await proc.wait()
        raise MediaError(
            f"ffmpeg 执行超时(>{MERGE_TIMEOUT_S}s): {' '.join(cmd[:5])}…",
            user_message="视频合成超时,请重试",
        ) from exc
    code = proc.returncode if proc.returncode is not None else -1
    tail = (err or out).decode("utf-8", "replace")[-2000:]
    return code, tail
