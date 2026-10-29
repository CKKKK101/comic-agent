"""agents 媒体接线单测:分镜媒体提示词 schema、渲染双路、成片双路(全离线)。

协议回归已由 test_media.py 固化;这里只验证编排接线:
- normalize/fake 的 image_prompt/video_prompt 契约(LLM 自写优先,缺键=拼装兜底);
- render:siliconflow 真调生成+落盘;fake 出占位 URL;prompt 两路同源;
- produce_video:ark 逐镜图生视频 → ffmpeg 拼接;fake 保持占位成片。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app import agents as agents_module
from app.agents import (
    _normalize_shots,
    produce_video,
    render_character_images,
    render_shot_images,
)
from app.config import get_settings
from app.models.project import ComicProject
from app.prompts.image_prompts import (
    build_shot_image_prompt,
    build_video_prompt,
    resolve_shot_image_prompt,
    resolve_video_prompt,
)
from app.services.media import MediaError


def _project(content: dict[str, Any], *, style: str = "赛博朋克") -> ComicProject:
    return ComicProject(topic="t", style=style, content=content)


def _image_settings(monkeypatch) -> Any:
    s = get_settings()
    monkeypatch.setattr(s, "image_provider", "siliconflow")
    return s


def _video_settings(monkeypatch, **over: Any) -> Any:
    s = get_settings()
    monkeypatch.setattr(s, "video_provider", "ark")
    monkeypatch.setattr(s, "doubao_api_key", "ark-key")
    monkeypatch.setattr(s, "doubao_video_model", "doubao-seedance-test")
    for key, value in over.items():
        monkeypatch.setattr(s, key, value)
    return s


# ---------------------------------------------------------------------------
# 分镜媒体提示词 schema 契约
# ---------------------------------------------------------------------------

def test_normalize_shots_keeps_only_authored_media_prompts() -> None:
    data = {
        "shots": [
            {"scene": "A", "camera": "B", "action": "C", "duration": 4,
             "image_prompt": "  " * 1,  # 空白 → 不落库
             "video_prompt": "把画面推近"},
            {"scene": "D", "camera": "E", "action": "F",
             "image_prompt": "超长" * 500,  # 截断
             "video_prompt": "超长" * 500},
            {"scene": "G", "action": "H"},
        ]
    }
    shots = _normalize_shots(data, n_char=2)
    assert "image_prompt" not in shots[0] and "video_prompt" in shots[0]
    assert shots[1]["image_prompt"].endswith("超长") and len(shots[1]["image_prompt"]) <= 600
    assert "image_prompt" not in shots[2] and "video_prompt" not in shots[2]


def test_fake_shots_carry_non_empty_media_prompts() -> None:
    import asyncio

    project = _project({"outline": {"title": "X"}})
    result = asyncio.run(agents_module.produce_shots(project, get_settings()))
    for shot in result["shots"]:
        image_prompt = shot["image_prompt"]
        assert image_prompt and shot["video_prompt"], "fake 分镜必须有媒体提示词样板"
        # 语种纪律(image=英文锁 + 风格引用;video=中文正文)
        assert "cel shading" in image_prompt, "image_prompt 以英文风格媒介锁开头"
        assert "赛博朋克" in image_prompt, "风格 token 保留在 image_prompt 里"
        assert shot["video_prompt"].startswith("场景:"), "video_prompt 走中文(seedance 母语)"


def test_shots_user_context_includes_appearance() -> None:
    project = _project(
        {
            "outline": {"title": "T", "logline": "L", "acts": [{"title": "a", "plot": "p"}]},
            "characters": [{"name": "夜莺", "personality": "冷静", "appearance": "银色短发"}],
        }
    )
    text = agents_module._shots_user(project)
    assert "银色短发" in text, "外貌要进 LLM 上下文,媒体提示词才能锁形象"


def test_media_prompt_fallback_pure() -> None:
    characters = [{"name": "夜莺", "appearance": "银色短发,红色义眼"}]
    shot = {"scene": "雨夜天台", "camera": "俯拍", "action": "对峙", "character_ids": [0]}
    fallback = build_shot_image_prompt("赛博朋克", shot, characters=characters)
    assert "cel shading" in fallback, "image 兜底带英文风格媒介锁(Z-Image 语种=英语)"
    assert "雨夜天台" in fallback and "银色短发" in fallback and "赛博朋克" in fallback
    assert "no text" in fallback, "image 兜底带禁文字锁"
    assert build_video_prompt(shot).startswith("场景"), "video 兜底走中文(seedance 母语)"

    authored = {"image_prompt": "自定义首帧描述", "video_prompt": "自定义运镜"}
    assert resolve_shot_image_prompt("风格", authored) == "自定义首帧描述"
    assert resolve_video_prompt(authored) == "自定义运镜"


# ---------------------------------------------------------------------------
# 渲染双路
# ---------------------------------------------------------------------------

async def test_render_character_images_siliconflow(monkeypatch) -> None:
    settings = _image_settings(monkeypatch)
    project = _project(
        {
            "characters": [
                {"name": "夜莺", "appearance": "银色短发", "personality": "冷静"},
                {"name": "老猫", "appearance": "络腮胡", "personality": "狡猾"},
            ]
        }
    )
    prompts: list[str] = []

    class StubGen:
        async def generate(self, prompt: str) -> str:
            prompts.append(prompt)
            return f"https://cdn.sf/{len(prompts)}.png"

    async def fake_download(url: str, *, subdir: str) -> str:
        assert url.startswith("https://cdn.sf/") and subdir == "images"
        return "/static/images/x.png"

    monkeypatch.setattr(agents_module, "get_image_client", lambda s: StubGen())
    monkeypatch.setattr(agents_module, "download_to_static", fake_download)

    out = await render_character_images(project, settings)
    imgs = out["character_images"]
    assert [i["url"] for i in imgs] == ["/static/images/x.png"] * 2
    assert "赛博朋克" in prompts[0] and "银色短发" in prompts[0] and "夜莺" in prompts[0]


async def test_render_character_images_fake_default() -> None:
    project = _project({"characters": [{"name": "A", "appearance": "黑发"}]})
    out = await render_character_images(project, get_settings())
    assert out["character_images"][0]["url"].startswith("fake://")
    assert "黑发" in out["character_images"][0]["prompt"]


async def test_render_shot_images_prefers_llm_authored_prompt(monkeypatch) -> None:
    settings = _image_settings(monkeypatch)
    project = _project(
        {"shots": [{"index": 1, "scene": "S", "image_prompt": "LLM 自写的首帧描述"}]}
    )
    received: list[str] = []

    class StubGen:
        async def generate(self, prompt: str) -> str:
            received.append(prompt)
            return "https://cdn.sf/1.png"

    monkeypatch.setattr(agents_module, "get_image_client", lambda s: StubGen())

    async def fake_download(url: str, *, subdir: str) -> str:
        return "/static/images/1.png"

    monkeypatch.setattr(agents_module, "download_to_static", fake_download)
    out = await render_shot_images(project, settings)
    assert received == ["LLM 自写的首帧描述"], "LLM 写了就用 LLM 的,不做二次拼装"
    assert out["shot_images"][0]["url"] == "/static/images/1.png"


async def test_render_shot_images_fallback_composes_identity(monkeypatch) -> None:
    settings = _image_settings(monkeypatch)
    project = _project(
        {
            "characters": [{"name": "夜莺", "appearance": "银色短发,红色义眼"}],
            "shots": [{"index": 1, "scene": "雨夜天台", "camera": "俯拍", "action": "对峙", "character_ids": [0]}],
        }
    )
    received: list[str] = []

    class StubGen:
        async def generate(self, prompt: str) -> str:
            received.append(prompt)
            return "https://cdn.sf/1.png"

    monkeypatch.setattr(agents_module, "get_image_client", lambda s: StubGen())

    async def fake_download(url: str, *, subdir: str) -> str:
        return "/static/images/1.png"

    monkeypatch.setattr(agents_module, "download_to_static", fake_download)
    out = await render_shot_images(project, settings)
    prompt = received[0]
    assert "雨夜天台" in prompt and "夜莺" in prompt and "银色短发" in prompt
    assert "赛博朋克" in prompt
    assert out["shot_images"][0]["url"] == "/static/images/1.png"


# ---------------------------------------------------------------------------
# 成片双路
# ---------------------------------------------------------------------------

async def test_produce_video_fake_default_keeps_preview() -> None:
    out = await produce_video(_project({}), get_settings())
    assert out["video"]["url"] == "fake://video/preview.mp4"


async def test_produce_video_ark_rejects_placeholder_shot_images(monkeypatch) -> None:
    settings = _video_settings(monkeypatch)
    project = _project(
        {"shot_images": [{"shot_index": 1, "url": "fake://shot/1/v1.png"}], "shots": []}
    )
    with pytest.raises(MediaError) as ei:
        await produce_video(project, settings)
    assert "本地" in (ei.value.user_message or "")


async def test_produce_video_ark_merges_capped_segments(monkeypatch, tmp_path: Path) -> None:
    settings = _video_settings(monkeypatch, video_max_shots=2)
    # 真实落盘的分镜图 + 可用的图片目录(disk_path 会检查文件存在)
    import app.services.media as media_module

    monkeypatch.setattr(media_module, "STATIC_ROOT", tmp_path)
    monkeypatch.setattr(agents_module, "STATIC_ROOT", tmp_path)
    images_dir = tmp_path / "images"
    images_dir.mkdir(parents=True)
    for name in ("s1.png", "s2.png", "s3.png"):
        (images_dir / name).write_bytes(b"PNG")

    project = _project(
        {
            "characters": [{"name": "夜莺", "appearance": "银色短发"}],
            "shots": [
                {"index": 1, "scene": "雪地", "camera": "中景", "action": "抬头", "duration": 4.0,
                 "character_ids": [0]},
                {"index": 2, "scene": "雪地", "camera": "特写", "action": "落泪", "duration": 5.0,
                 "character_ids": [0]},
                {"index": 3, "scene": "雪地", "camera": "全景", "action": "走远", "duration": 4.0,
                 "character_ids": [0]},
            ],
            "shot_images": [
                {"shot_index": i, "url": f"/static/images/s{i}.png"}
                for i in (1, 2, 3)
            ],
        }
    )
    video_calls: list[tuple[str, str]] = []

    class StubVideo:
        async def generate_video(self, *, prompt: str, image_static_url: str | None) -> str:
            video_calls.append((prompt, image_static_url or ""))
            return f"https://media.example/seg{len(video_calls)}.mp4"

    async def fake_download(url: str, *, subdir: str) -> str:
        # generate_video 刚 append 过 → len == 当前第几段
        name = f"seg-{len(video_calls)}.mp4"
        (tmp_path / "videos").mkdir(exist_ok=True)
        (tmp_path / "videos" / name).write_bytes(b"MP4")
        return f"/static/videos/{name}"

    merged: list[Any] = []

    async def fake_merge(segments: list[Path], output: Path) -> None:
        merged.append((segments, output))

    monkeypatch.setattr(agents_module, "get_video_client", lambda s: StubVideo())
    monkeypatch.setattr(agents_module, "download_to_static", fake_download)
    monkeypatch.setattr(agents_module, "merge_videos", fake_merge)

    out = await produce_video(project, settings)
    video = out["video"]
    assert video["url"].startswith("/static/videos/final-") and video["url"].endswith(".mp4")
    assert video["voiceover_url"] is None
    assert "ffmpeg" in video["notes"] and "2 镜" in video["notes"]

    # 只合成前 2 镜(video_max_shots=2),逐镜图生视频,视频 prompt 带出场角色身份
    assert len(video_calls) == 2
    assert video_calls[0][1] == "/static/images/s1.png"
    assert "夜莺" in video_calls[0][0] and "银色短发" in video_calls[0][0]
    assert video_calls[1][1] == "/static/images/s2.png"
    # ffmpeg 收到的是本地片段路径,顺序与分镜一致
    (segments, output) = merged[0]
    assert [s.name for s in segments] == ["seg-1.mp4", "seg-2.mp4"]
    assert output.parent == tmp_path / "videos"


# ---------------------------------------------------------------------------
# 内容审核 20021 抖动:变化性重试
# ---------------------------------------------------------------------------

async def test_character_render_retries_condensed_variant_on_20021(monkeypatch) -> None:
    settings = _image_settings(monkeypatch)
    project = _project({"characters": [{"name": "站员", "appearance": "银灰色短发,制服,旧式挂表"}]})
    calls: list[str] = []

    class StubGen:
        async def generate(self, prompt: str) -> str:
            calls.append(prompt)
            if "no extra characters" in prompt:  # 完整版(含构图尾句)被 20021 拦
                raise MediaError('image API HTTP 451: {"code":20021}', user_message="x")
            return "https://cdn.sf/ok.png"

    async def fake_download(url: str, *, subdir: str) -> str:
        return "/static/images/ok.png"

    monkeypatch.setattr(agents_module, "get_image_client", lambda s: StubGen())
    monkeypatch.setattr(agents_module, "download_to_static", fake_download)

    out = await render_character_images(project, settings)
    im = out["character_images"][0]
    assert len(calls) == 2, "完整版被拦后应降级一次即成功"
    assert "no extra characters" not in im["prompt"], "落库的应是生效变体"
    assert im["url"] == "/static/images/ok.png"


async def test_character_render_exhausted_20021_raises_user_facing(monkeypatch) -> None:
    settings = _image_settings(monkeypatch)
    project = _project({"characters": [{"name": "A", "appearance": "黑发"}]})

    class StubGen:
        async def generate(self, prompt: str) -> str:
            raise MediaError('image API HTTP 451: {"code":20021}', user_message="x")

    monkeypatch.setattr(agents_module, "get_image_client", lambda s: StubGen())

    with pytest.raises(MediaError) as ei:
        await render_character_images(project, settings)
    assert "内容审核" in (ei.value.user_message or ""), "全部变体被拦要给用户明确文案"


# ---------------------------------------------------------------------------
# 单镜画面重写轮:反馈 → 同一 shot agent 重写 image_prompt → 重渲
# ---------------------------------------------------------------------------

async def test_shot_feedback_uses_author_rewrite_not_tail(monkeypatch) -> None:
    """渲染门反馈应走"作者重写 image_prompt",而不是机械拼反馈尾巴。"""
    settings = get_settings()
    monkeypatch.setattr(settings, "text_provider", "openai")
    settings2 = _image_settings(monkeypatch)  # image siliconflow
    project = _project(
        {
            "characters": [{"name": "佐藤晴人", "appearance": "黑发黄瞳"}],
            "shots": [{"index": 5, "scene": "车祸现场", "camera": "侧面中景",
                       "action": "推开孩子", "image_prompt": "old vague prompt",
                       "character_ids": [0]}],
        }
    )
    rewritten = ("anime comic style, clean line art, cel shading. Side view low camera. "
                 "The car occupies right half of frame facing right; Sato stands in front of "
                 "the car's hood at frame center-left, facing left, pushing the boy to the far left.")
    calls: list[str] = []

    class _RewriteLLM:
        async def chat_json(self, **kw) -> dict:
            calls.append(kw.get("user", ""))
            return {"shots": [{"index": 5, "scene": "SAME", "action": "SAME",
                               "image_prompt": rewritten}]}

    class StubGen:
        async def generate(self, prompt: str) -> str:
            calls.append(prompt)
            return "https://cdn.sf/5.png"

    async def fake_download(url: str, *, subdir: str) -> str:
        return "/static/images/5.png"

    monkeypatch.setattr("app.agents.get_llm", lambda s: _RewriteLLM())
    monkeypatch.setattr(agents_module, "get_image_client", lambda s: StubGen())
    monkeypatch.setattr(agents_module, "download_to_static", fake_download)

    out = await render_shot_images(project, settings2, feedback="第5镜:男人站车前,面朝车头")
    prompt_used = out["shot_images"][0]["prompt"]
    assert prompt_used == rewritten, "应使用作者重写版,而非旧 prompt+反馈尾巴"
    assert "用户反馈" not in prompt_used


async def test_shot_specific_feedback_splits_per_shot() -> None:
    from app.agents import _shot_specific_feedback

    fb = "第2镜构图太满。第5镜男人位置应在车前,面朝车头;小孩在他左侧。"
    assert "构图太满" in _shot_specific_feedback(fb, 2)
    s5 = _shot_specific_feedback(fb, 5)
    assert "位置应在车前" in s5 and "构图太满" not in s5, "第5镜不应看到第2镜的句子"


async def test_fresh_render_with_existing_images_does_not_reuse(monkeypatch) -> None:
    """空反馈的首次渲染即使已有旧图也必须整组重渲(复用只属于反馈轮)。"""
    settings = _image_settings(monkeypatch)
    project = _project(
        {"shots": [{"index": 1, "scene": "S", "action": "A"}],
         "shot_images": [{"shot_index": 1, "url": "/static/images/old.png", "prompt": "old"}]}
    )
    calls: list[str] = []

    class StubGen:
        async def generate(self, prompt: str, **kw) -> str:
            calls.append(prompt)
            return "https://cdn.sf/new.png"

    async def fake_download(url: str, *, subdir: str) -> str:
        return "/static/images/new.png"

    monkeypatch.setattr(agents_module, "get_image_client", lambda s: StubGen())
    monkeypatch.setattr(agents_module, "download_to_static", fake_download)
    out = await render_shot_images(project, settings)  # feedback=None
    assert len(calls) == 1 and out["shot_images"][0]["url"] == "/static/images/new.png"
