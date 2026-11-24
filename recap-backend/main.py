import json
import math
import os
import re
import shutil
import subprocess
import uuid
from typing import Optional

import edge_tts
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

app = FastAPI(title="Movie Recap Video Backend - Timed Edit")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

WORK_DIR = "/tmp/recap_jobs"
os.makedirs(WORK_DIR, exist_ok=True)

ALLOWED_VOICES = {
    "nilar": "my-MM-NilarNeural",
    "thiha": "my-MM-ThihaNeural",
}

ASPECT_RESOLUTIONS = {
    "16:9": (1920, 1080),
    "9:16": (1080, 1920),
    "1:1": (1080, 1080),
}


def hex_to_ass_color(hex_color: str) -> str:
    """#RRGGBB -> &H00BBGGRR (ASS subtitle color format)"""
    cleaned = hex_color.strip().lstrip("#")
    if len(cleaned) != 6:
        return "&H0000FFFF"
    rr = cleaned[0:2].upper()
    gg = cleaned[2:4].upper()
    bb = cleaned[4:6].upper()
    return f"&H00{bb}{gg}{rr}"


def get_media_duration(file_path: str) -> float:
    """ffprobe သုံးပြီး audio/video ကြာချိန် (စက္ကန့်) ရှာခြင်း"""
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "json",
        file_path,
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode == 0:
        try:
            data = json.loads(res.stdout)
            dur = float(data["format"]["duration"])
            if dur > 0:
                return dur
        except Exception:
            pass
    return 10.0


def split_script_into_chunks(script_text: str, max_chars: int = 42) -> list[str]:
    """စာသားကို စာကြောင်း/ပုဒ်ဖြတ်/အရှည်အလိုက် အပိုင်းတိုများ ခွဲခြင်း"""
    normalized = script_text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return ["ဇာတ်လမ်းအကျဉ်း"]

    # Line break များနှင့် ပုဒ်ဖြတ် (။၊!?.) များဖြင့် အကြမ်းခွဲခြင်း
    raw_segments = []
    for line in normalized.split("\n"):
        line = line.strip()
        if not line:
            continue
        parts = re.split(r"(?<=[။၊!?\.])\s*", line)
        for p in parts:
            p = p.strip()
            if p:
                raw_segments.append(p)

    if not raw_segments:
        raw_segments = [normalized]

    # စာလုံးရေ ရှည်လွန်းပါက ထပ်မံခွဲခြင်း
    chunks: list[str] = []
    for seg in raw_segments:
        if len(seg) <= max_chars:
            chunks.append(seg)
            continue

        words = seg.split(" ")
        current = ""
        for w in words:
            candidate = f"{current} {w}".strip() if current else w
            if len(candidate) <= max_chars:
                current = candidate
            else:
                if current:
                    chunks.append(current)
                # Space မပါသော မြန်မာစာသားရှည်ဖြစ်ပါက character chunk ခွဲခြင်း
                if len(w) > max_chars:
                    for i in range(0, len(w), max_chars):
                        chunks.append(w[i : i + max_chars])
                    current = ""
                else:
                    current = w
        if current:
            chunks.append(current)

    return chunks if chunks else [normalized[:max_chars]]


def format_srt_time(seconds: float) -> str:
    """စက္ကန့်ကို HH:MM:SS,mmm format ပြောင်းခြင်း"""
    if seconds < 0:
        seconds = 0.0
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int(round((seconds - math.floor(seconds)) * 1000))
    if millis >= 1000:
        secs += 1
        millis -= 1000
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def build_srt_file(chunks: list[str], total_duration: float, srt_path: str) -> None:
    """စာသားအပိုင်းများကို အသံကြာချိန်နှင့် အချိန်ကိုက် .srt ဖိုင်ဖန်တီးခြင်း"""
    weights = [max(len(c.strip()), 1) for c in chunks]
    total_weight = sum(weights)

    cursor = 0.0
    lines: list[str] = []
    for idx, (chunk, w) in enumerate(zip(chunks, weights), start=1):
        seg_dur = (w / total_weight) * total_duration
        start_t = cursor
        end_t = total_duration if idx == len(chunks) else min(total_duration, cursor + seg_dur)
        if end_t <= start_t:
            end_t = min(total_duration, start_t + 0.8)

        lines.append(str(idx))
        lines.append(f"{format_srt_time(start_t)} --> {format_srt_time(end_t)}")
        lines.append(chunk)
        lines.append("")
        cursor = end_t

    with open(srt_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def build_paced_video(
    raw_video_path: str,
    target_duration: float,
    width: int,
    height: int,
    paced_video_path: str,
    segment_sec: float = 6.0,
) -> None:
    """ဗီဒီယိုကို jump-cut အပိုင်းတိုများဖြင့် စနစ်တကျ ညှိပြီး ဖန်တီးခြင်း"""
    src_duration = get_media_duration(raw_video_path)
    num_segments = max(1, math.ceil(target_duration / segment_sec))
    seg_dir = os.path.join(os.path.dirname(paced_video_path), "segs")
    os.makedirs(seg_dir, exist_ok=True)

    seg_files: list[str] = []
    for i in range(num_segments):
        remaining = target_duration - (i * segment_sec)
        if remaining <= 0.1:
            break
        dur = min(segment_sec, remaining)
        # ဗီဒီယိုအရှည်ထက်ကျော်ပါက wrap around လုပ်ပြီး မတူညီသော အစမှတ်များ ယူခြင်း
        start_pos = ((i * segment_sec * 1.35) % max(src_duration - 0.5, 1.0))
        if start_pos + dur > src_duration:
            start_pos = 0.0

        # Subtle zoom/crop variation (1.04x) for dynamic recap pacing
        zoom_factor = 1.04 if (i % 2 == 0) else 1.02
        scaled_w = int(round(width * zoom_factor))
        scaled_h = int(round(height * zoom_factor))

        out_seg = os.path.join(seg_dir, f"seg_{i:03d}.mp4")
        vf = (
            f"scale={scaled_w}:{scaled_h}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},fps=24,format=yuv420p"
        )
        cmd = [
            "ffmpeg",
            "-y",
            "-ss",
            f"{start_pos:.2f}",
            "-t",
            f"{dur:.2f}",
            "-i",
            raw_video_path,
            "-vf",
            vf,
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-crf",
            "25",
            out_seg,
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode == 0 and os.path.exists(out_seg):
            seg_files.append(out_seg)

    if not seg_files:
        # Fallback: direct scale/crop loop
        vf = (
            f"scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},fps=24,format=yuv420p"
        )
        cmd = [
            "ffmpeg",
            "-y",
            "-stream_loop",
            "-1",
            "-i",
            raw_video_path,
            "-t",
            f"{target_duration:.2f}",
            "-vf",
            vf,
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "25",
            paced_video_path,
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            raise RuntimeError(f"Video pacing failed: {res.stderr[-500:]}")
        return

    concat_list_path = os.path.join(seg_dir, "list.txt")
    with open(concat_list_path, "w", encoding="utf-8") as f:
        for sf in seg_files:
            f.write(f"file '{sf}'\n")

    concat_cmd = [
        "ffmpeg",
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        concat_list_path,
        "-c",
        "copy",
        paced_video_path,
    ]
    res = subprocess.run(concat_cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"Concat failed: {res.stderr[-500:]}")


@app.get("/")
def health_check():
    return {"status": "ok", "message": "Movie Recap Backend (Timed Edit) is running"}


@app.post("/generate-voice")
async def generate_voice(
    text: str = Form(...),
    voice_key: str = Form("nilar"),
):
    voice_name = ALLOWED_VOICES.get(voice_key, "my-MM-NilarNeural")
    job_id = str(uuid.uuid4())
    out_mp3 = os.path.join(WORK_DIR, f"{job_id}.mp3")

    try:
        communicate = edge_tts.Communicate(text=text, voice=voice_name)
        await communicate.save(out_mp3)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"TTS Error: {str(e)}")

    return FileResponse(out_mp3, media_type="audio/mpeg", filename=f"{job_id}.mp3")


@app.post("/render-recap")
async def render_recap(
    video: UploadFile = File(...),
    script_text: str = Form(...),
    voice_key: str = Form("nilar"),
    aspect_ratio: str = Form("16:9"),
    subtitle_color: str = Form("#FFFF00"),
    subtitle_size: int = Form(42),
    channel_name: str = Form("My Recap Channel"),
    logo: Optional[UploadFile] = File(None),
):
    if aspect_ratio not in ASPECT_RESOLUTIONS:
        raise HTTPException(status_code=400, detail="Unsupported aspect_ratio")

    clean_script = script_text.strip()
    if not clean_script:
        raise HTTPException(status_code=400, detail="script_text cannot be empty")

    width, height = ASPECT_RESOLUTIONS[aspect_ratio]
    voice_name = ALLOWED_VOICES.get(voice_key, "my-MM-NilarNeural")

    job_id = str(uuid.uuid4())
    job_dir = os.path.join(WORK_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)

    raw_video_path = os.path.join(job_dir, "input_video.mp4")
    audio_path = os.path.join(job_dir, "voiceover.mp3")
    paced_video_path = os.path.join(job_dir, "paced_video.mp4")
    srt_path = os.path.join(job_dir, "captions.srt")
    logo_path = os.path.join(job_dir, "logo.png")
    output_video_path = os.path.join(job_dir, "final_recap.mp4")

    try:
        # 1. Input Video သိမ်းခြင်း
        with open(raw_video_path, "wb") as f:
            shutil.copyfileobj(video.file, f)

        # 2. မြန်မာအသံ ထုတ်ခြင်းနှင့် ကြာချိန်တွက်ခြင်း
        communicate = edge_tts.Communicate(text=clean_script, voice=voice_name)
        await communicate.save(audio_path)
        audio_duration = get_media_duration(audio_path)

        # 3. စာသားကို အချိန်ကိုက် .srt ဖိုင်အဖြစ် ပြောင်းခြင်း
        chunks = split_script_into_chunks(clean_script, max_chars=40)
        build_srt_file(chunks, audio_duration, srt_path)

        # 4. ဗီဒီယိုကို အသံကြာချိန်နှင့်ကိုက်ညီအောင် jump-cut edit လုပ်ခြင်း
        build_paced_video(
            raw_video_path=raw_video_path,
            target_duration=audio_duration,
            width=width,
            height=height,
            paced_video_path=paced_video_path,
            segment_sec=6.0,
        )

        has_logo = False
        if logo and logo.filename:
            with open(logo_path, "wb") as f:
                shutil.copyfileobj(logo.file, f)
            has_logo = True

        # 5. Subtitles + Channel Name + Logo ပေါင်းစပ် Render လုပ်ခြင်း
        ass_color = hex_to_ass_color(subtitle_color)
        # ASS font size scaling relative to 1080p height
        ass_fontsize = max(16, min(90, int(round(subtitle_size * (height / 1080.0)))))
        margin_v = max(60, int(round(height * 0.10)))
        safe_srt_path = srt_path.replace("\\", "/").replace(":", "\\:")

        sub_filter = (
            f"subtitles='{safe_srt_path}':force_style='"
            f"FontName=Noto Sans Myanmar,FontSize={ass_fontsize},"
            f"PrimaryColour={ass_color},OutlineColour=&H80000000,"
            f"BackColour=&H66000000,BorderStyle=3,Outline=2,Shadow=1,"
            f"Alignment=2,MarginV={margin_v}'"
        )

        safe_channel = (
            channel_name.strip()
            .replace("\\", "\\\\")
            .replace(":", "\\:")
            .replace("'", "\\'")
        )
        channel_fontsize = max(20, int(round(28 * (height / 1080.0))))

        base_chain = (
            f"[0:v]{sub_filter},"
            f"drawtext=text='{safe_channel}':"
            f"fontcolor=white@0.50:fontsize={channel_fontsize}:"
            f"x=w-tw-28:y=h-th-24[v_sub]"
        )

        if has_logo:
            logo_w = max(80, int(round(width * 0.10)))
            filter_complex = (
                f"{base_chain};"
                f"[1:v]scale={logo_w}:-1,format=rgba,colorchannelmixer=aa=0.80[wm];"
                f"[v_sub][wm]overlay=24:24[v_out]"
            )
            cmd = [
                "ffmpeg",
                "-y",
                "-i",
                paced_video_path,
                "-i",
                logo_path,
                "-i",
                audio_path,
                "-filter_complex",
                filter_complex,
                "-map",
                "[v_out]",
                "-map",
                "2:a",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "23",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                "-shortest",
                output_video_path,
            ]
        else:
            cmd = [
                "ffmpeg",
                "-y",
                "-i",
                paced_video_path,
                "-i",
                audio_path,
                "-filter_complex",
                base_chain.replace("[v_sub]", "[v_out]"),
                "-map",
                "[v_out]",
                "-map",
                "1:a",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "23",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                "-shortest",
                output_video_path,
            ]

        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(f"Final render failed: {proc.stderr[-600:]}")

        return FileResponse(
            output_video_path,
            media_type="video/mp4",
            filename=f"recap_{job_id}.mp4",
        )

    except HTTPException:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise
    except Exception as e:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail=str(e))
