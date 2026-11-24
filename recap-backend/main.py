import os
import shutil
import subprocess
import uuid
from typing import Optional

import edge_tts
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

app = FastAPI(title="Movie Recap Video Backend")

# Lovable (သို့မဟုတ် အခြား Frontend) မှ ချိတ်ဆက်ခွင့်ပြုရန်
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


def hex_to_ffmpeg_color(hex_color: str) -> str:
    """#FFFFFF -> 0xFFFFFF format ပြောင်းပေးခြင်း"""
    cleaned = hex_color.strip().lstrip("#")
    if len(cleaned) != 6:
        return "0xFFFFFF"
    return f"0x{cleaned.upper()}"


@app.get("/")
def health_check():
    return {"status": "ok", "message": "Movie Recap Backend is running"}


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
    aspect_ratio: str = Form("16:9"),          # "16:9", "9:16", "1:1"
    subtitle_color: str = Form("#FFFF00"),       # Hex color
    subtitle_size: int = Form(42),
    channel_name: str = Form("My Recap Channel"),
    logo: Optional[UploadFile] = File(None),
):
    if aspect_ratio not in ASPECT_RESOLUTIONS:
        raise HTTPException(status_code=400, detail="Unsupported aspect_ratio")

    width, height = ASPECT_RESOLUTIONS[aspect_ratio]
    voice_name = ALLOWED_VOICES.get(voice_key, "my-MM-NilarNeural")

    job_id = str(uuid.uuid4())
    job_dir = os.path.join(WORK_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)

    raw_video_path = os.path.join(job_dir, "input_video.mp4")
    audio_path = os.path.join(job_dir, "voiceover.mp3")
    output_video_path = os.path.join(job_dir, "final_recap.mp4")
    logo_path = os.path.join(job_dir, "logo.png")
    subtitle_txt_path = os.path.join(job_dir, "subtitle.txt")

    # 1. Input Video သိမ်းခြင်း
    with open(raw_video_path, "wb") as f:
        shutil.copyfileobj(video.file, f)

    # 2. မြန်မာအသံ (Edge-TTS) ထုတ်ခြင်း
    try:
        communicate = edge_tts.Communicate(text=script_text, voice=voice_name)
        await communicate.save(audio_path)
    except Exception as e:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail=f"Voice generation failed: {str(e)}")

    # 3. စာတန်းဖိုင် (UTF-8) သိမ်းခြင်း
    safe_script = script_text.replace("\r\n", "\n").strip()
    with open(subtitle_txt_path, "w", encoding="utf-8") as f:
        f.write(safe_script)

    has_logo = False
    if logo and logo.filename:
        with open(logo_path, "wb") as f:
            shutil.copyfileobj(logo.file, f)
        has_logo = True

    ffmpeg_sub_color = hex_to_ffmpeg_color(subtitle_color)
    safe_channel = channel_name.replace(":", "\\:").replace("'", "\\'")
    font_file = "/usr/share/fonts/truetype/myanmar/NotoSansMyanmar-Regular.ttf"
    use_font_path = os.path.exists(font_file)
    font_arg = f"fontfile='{font_file}':" if use_font_path else ""

    # 4. Aspect Ratio (Scale + Crop to fill) + Watermark + Subtitle
    base_filter = (
        f"[0:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},"
        f"drawtext={font_arg}text='{safe_channel}':"
        f"fontcolor=white@0.45:fontsize=26:x=w-tw-30:y=h-th-25,"
        f"drawtext={font_arg}textfile='{subtitle_txt_path}':reload=1:"
        f"fontcolor={ffmpeg_sub_color}:fontsize={subtitle_size}:"
        f"borderw=3:bordercolor=black@0.85:box=1:boxcolor=black@0.45:boxborderw=10:"
        f"x=(w-text_w)/2:y=h-th-110[v_base]"
    )

    if has_logo:
        filter_complex = (
            f"{base_filter};"
            f"[1:v]scale=140:-1,format=rgba,colorchannelmixer=aa=0.75[wm];"
            f"[v_base][wm]overlay=25:25[v_out]"
        )
        cmd = [
            "ffmpeg",
            "-y",
            "-i", raw_video_path,
            "-i", logo_path,
            "-i", audio_path,
            "-filter_complex", filter_complex,
            "-map", "[v_out]",
            "-map", "2:a",
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "24",
            "-c:a", "aac",
            "-b:a", "128k",
            "-shortest",
            output_video_path,
        ]
    else:
        cmd = [
            "ffmpeg",
            "-y",
            "-i", raw_video_path,
            "-i", audio_path,
            "-filter_complex", base_filter.replace("[v_base]", "[v_out]"),
            "-map", "[v_out]",
            "-map", "1:a",
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "24",
            "-c:a", "aac",
            "-b:a", "128k",
            "-shortest",
            output_video_path,
        ]

    process = subprocess.run(cmd, capture_output=True, text=True)
    if process.returncode != 0:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(
            status_code=500,
            detail=f"FFmpeg error: {process.stderr[-600:]}",
        )

    return FileRespmain.pyonse(
        output_video_path,
        media_type="video/mp4",
        filename=f"recap_{job_id}.mp4",
    )
