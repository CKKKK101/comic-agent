"""video_merger 单测:命令构造(monkeypatch _run_checked)+ 真实 ffmpeg 拼接(机器装有 ffmpeg)。

真实拼接依赖本机 ffmpeg,机器没有就整条跳过(CI/演示机都有);
copy 直拼失败 → 重编码降级路径用假 _run 验证命令形态。
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from app.services import video_merger as merger
from app.services.media import MediaError

_FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
_FFPROBE = shutil.which("ffprobe") or "ffprobe"


def test_merge_empty_segments_raises(tmp_path) -> None:
    with pytest.raises(MediaError) as ei:
        _merge_sync([], tmp_path / "out.mp4")
    assert "视频片段" in (ei.value.user_message or "")


def test_merge_missing_ffmpeg_raises_user_facing(tmp_path) -> None:
    seg = tmp_path / "a.mp4"
    seg.write_bytes(b"x")
    with pytest.raises(MediaError) as ei:
        _merge_sync([seg], tmp_path / "out.mp4", ffmpeg="no-such-ffmpeg-binary")
    assert "ffmpeg" in str(ei.value)
    assert "安装" in (ei.value.user_message or "")


async def test_copy_success_skips_reencode(monkeypatch, tmp_path) -> None:
    calls: list[list[str]] = []
    list_content: list[str] = []

    async def fake_run(cmd: list[str], *, ffmpeg: str) -> tuple[int, str]:
        calls.append(cmd)
        # concat list 在 _run_checked 调用时尚存在(执行完会被清理)→ 在此读取
        list_path = cmd[cmd.index("-i") + 1]
        list_content.append(Path(list_path).read_text(encoding="utf-8"))
        Path(cmd[-1]).write_bytes(b"merged")  # 让"输出存在且非空"成立
        return 0, ""

    monkeypatch.setattr(merger, "_run_checked", fake_run)
    segs = [tmp_path / "a.mp4", tmp_path / "b.mp4"]
    for s in segs:
        s.write_bytes(b"x")
    out = tmp_path / "v" / "out.mp4"

    await merger.merge_videos(segs, out)

    assert len(calls) == 1, "copy 成功不应触发重编码降级"
    cmd = calls[0]
    assert cmd[0] == "ffmpeg" and cmd[1] == "-y"
    assert "-f" in cmd and cmd[cmd.index("-f") + 1] == "concat"
    assert "-safe" in cmd and cmd[cmd.index("-safe") + 1] == "0"
    assert "-c" in cmd and cmd[cmd.index("-c") + 1] == "copy"
    assert list_content[0].count("file '") == 2
    assert not Path(cmd[cmd.index("-i") + 1]).exists(), "concat list 用完即清理"
    assert out.read_bytes() == b"merged"


async def test_copy_failure_falls_back_to_reencode(monkeypatch, tmp_path) -> None:
    calls: list[list[str]] = []

    async def fake_run(cmd: list[str], *, ffmpeg: str) -> tuple[int, str]:
        calls.append(cmd)
        if "-c" in cmd and cmd[cmd.index("-c") + 1] == "copy":
            return 1, "non-monotonous DTS"  # copy 失败(常见:参数不一致)
        Path(cmd[-1]).write_bytes(b"re-encoded")  # 重编码成功
        return 0, ""

    monkeypatch.setattr(merger, "_run_checked", fake_run)
    segs = [tmp_path / "a.mp4", tmp_path / "b.mp4"]
    for s in segs:
        s.write_bytes(b"x")

    await merger.merge_videos(segs, tmp_path / "out.mp4")

    assert len(calls) == 2
    recode = calls[1]
    assert "-filter_complex" in recode
    fv = recode[recode.index("-filter_complex") + 1]
    assert fv == "[0:v][1:v]concat=n=2:v=1:a=0[v]"  # 逐输入打标签,丢音轨
    assert recode[recode.index("-map") + 1] == "[v]"
    assert "libx264" in recode and "yuv420p" in recode
    assert Path(recode[-1]).read_bytes() == b"re-encoded"


# ---------------------------------------------------------------------------
# 真实 ffmpeg 集成(本机有 ffmpeg 才跑)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="需要本机 ffmpeg")
def test_real_merge_two_clips(tmp_path) -> None:
    a, b = _make_clip(tmp_path, "blue"), _make_clip(tmp_path, "red")
    out = tmp_path / "final.mp4"
    _merge_sync([a, b], out)
    assert out.exists() and out.stat().st_size > 1_000
    duration = _probe_duration(out)
    assert 0.5 <= duration <= 0.8, f"拼接时长应≈两段之和,实际 {duration:.2f}s"


def _merge_sync(segments: list[Path], output: Path, *, ffmpeg: str = "ffmpeg") -> None:
    import asyncio

    asyncio.run(merger.merge_videos(segments, output, ffmpeg=ffmpeg))


# ---------------------------------------------------------------------------
# 台词配音:probe_duration / build_voiceover / mux_with_audio
# ---------------------------------------------------------------------------

async def test_build_voiceover_command_shape(monkeypatch, tmp_path) -> None:
    """混音命令形态:逐输入 adelay(毫秒) → amix,单音轨 aac 输出。"""
    calls: list[list[str]] = []

    async def fake_run(cmd: list[str], *, ffmpeg: str) -> tuple[int, str]:
        calls.append(cmd)
        Path(cmd[-1]).write_bytes(b"mixed-audio")
        return 0, ""

    monkeypatch.setattr(merger, "_run_checked", fake_run)
    a, b = tmp_path / "a.mp3", tmp_path / "b.mp3"
    a.write_bytes(b"x")
    b.write_bytes(b"x")
    out = tmp_path / "vo" / "voice.m4a"

    await merger.build_voiceover([a, b], [0, 3200], out, total_ms=9000)

    assert len(calls) == 1
    cmd = calls[0]
    assert cmd[cmd.index("-i") + 1] == str(a)
    assert cmd[-1] == str(out)
    fv = cmd[cmd.index("-filter_complex") + 1]
    assert fv.startswith("[0:a]adelay=0|0[d0];[1:a]adelay=3200|3200[d1];")
    assert fv.endswith("[d0][d1]amix=inputs=2:normalize=0,atrim=0:9.000[aout]")
    assert cmd[cmd.index("-map") + 1] == "[aout]"
    assert "aac" in cmd


async def test_build_voiceover_without_total_no_trim(monkeypatch, tmp_path) -> None:
    """未给 total_ms(旧调用)→ 不加 atrim,保持兼容。"""
    calls: list[list[str]] = []

    async def fake_run(cmd: list[str], *, ffmpeg: str) -> tuple[int, str]:
        calls.append(cmd)
        Path(cmd[-1]).write_bytes(b"x")
        return 0, ""

    monkeypatch.setattr(merger, "_run_checked", fake_run)
    a = tmp_path / "a.mp3"
    a.write_bytes(b"x")
    await merger.build_voiceover([a], [0], tmp_path / "o.m4a")
    fv = calls[0][calls[0].index("-filter_complex") + 1]
    assert "atrim" not in fv


async def test_build_voiceover_mismatch_raises(tmp_path) -> None:
    with pytest.raises(MediaError):
        await merger.build_voiceover([tmp_path / "a.mp3"], [0, 100], tmp_path / "o.m4a")


async def test_mux_with_audio_command_shape(monkeypatch, tmp_path) -> None:
    """并入命令形态:视频流 copy 不重编码,音频 copy,-shortest。"""
    calls: list[list[str]] = []

    async def fake_run(cmd: list[str], *, ffmpeg: str) -> tuple[int, str]:
        calls.append(cmd)
        Path(cmd[-1]).write_bytes(b"voiced")
        return 0, ""

    monkeypatch.setattr(merger, "_run_checked", fake_run)
    video, audio, out = tmp_path / "v.mp4", tmp_path / "a.m4a", tmp_path / "final.mp4"
    video.write_bytes(b"v")
    audio.write_bytes(b"a")

    await merger.mux_with_audio(video, audio, out)

    cmd = calls[0]
    assert cmd[cmd.index("-i") + 1] == str(video)
    assert "-map" in cmd and cmd[cmd.index("-map") + 1] == "0:v"
    assert cmd[cmd.index("-c:v") + 1] == "copy"
    assert cmd[cmd.index("-c:a") + 1] == "copy"
    assert "-shortest" not in cmd, "不能用 -shortest:音轨短会连视频一起裁"
    assert out.read_bytes() == b"voiced"


async def test_probe_duration_missing_probe_returns_none(tmp_path) -> None:
    assert await merger.probe_duration(tmp_path / "nope.mp4", ffprobe="no-such-ffprobe") is None


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="需要本机 ffmpeg")
def test_real_voiceover_pipeline(tmp_path) -> None:
    """真机端到端:静音视频 + 两句错位音频 → 混音并入 → 视频带音轨。"""
    import asyncio

    video = _make_clip(tmp_path, "green")
    tones = []
    for i, ms in enumerate((0, 400)):
        tone = tmp_path / f"tone{i}.wav"
        result = subprocess.run(
            [
                _FFMPEG, "-y",
                "-f", "lavfi", "-i", f"sine=frequency={300 + i * 100}:duration=0.25",
                str(tone),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr[-300:]
        tones.append(tone)

    audio_out = tmp_path / "vo.m4a"
    voiced = tmp_path / "voiced.mp4"

    async def run() -> None:
        video_dur = _probe_duration(video)  # ~0.33s
        # 第二句在 0.4s 起、0.25s 长 → 混音天然 ~0.65s,超出成片总长必须被裁掉
        await merger.build_voiceover(tones, [0, 400], audio_out, total_ms=int(video_dur * 1000))
        await merger.mux_with_audio(video, audio_out, voiced)

    asyncio.run(run())

    assert voiced.exists() and voiced.stat().st_size > 1_000
    # 视频是权威:成片时长 == 静音源,音轨未把画面拖长或裁短
    assert abs(_probe_duration(voiced) - _probe_duration(video)) < 0.15
    # 音轨被 atrim 到成片长度(不残留 0.65s 尾巴;± 为探针浮点噪声)
    assert abs(_probe_duration(audio_out) - _probe_duration(video)) < 0.2
    # 成片确实带了音轨
    result = subprocess.run(
        [
            _FFPROBE, "-v", "error",
            "-select_streams", "a",
            "-show_entries", "stream=codec_name",
            "-of", "csv=p=0",
            str(voiced),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0 and result.stdout.strip(), "并入后成片应含音轨"


def _make_clip(tmp_path: Path, color: str) -> Path:
    path = tmp_path / f"{color}.mp4"
    result = subprocess.run(
        [
            _FFMPEG, "-y",
            "-f", "lavfi", "-i", f"color=c={color}:s=256x144:r=12",
            "-t", "0.3",
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-500:]
    return path


def _probe_duration(path: Path) -> float:
    result = subprocess.run(
        [
            _FFPROBE, "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return float(result.stdout.strip() or 0)
