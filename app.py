"""ClipForge pipeline: URL -> download -> transcribe -> pick clips -> cut 9:16."""
import json
import os
import subprocess
from pathlib import Path

import yt_dlp
from faster_whisper import WhisperModel

DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
_model = None


def get_model() -> WhisperModel:
    global _model
    if _model is None:
        _model = WhisperModel(
            os.getenv("WHISPER_MODEL", "small"), device="cpu", compute_type="int8"
        )
    return _model


def download_video(url: str, out_dir: Path):
    out_dir.mkdir(parents=True, exist_ok=True)
    opts = {
        "format": "bv*[height<=720]+ba/b[height<=720]/b",
        "merge_output_format": "mp4",
        "outtmpl": str(out_dir / "source.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
    return out_dir / "source.mp4", info.get("title", ""), info.get("duration", 0)


def transcribe(video: Path):
    segments, _ = get_model().transcribe(
        str(video), word_timestamps=True, vad_filter=True
    )
    out = []
    for s in segments:
        out.append(
            {
                "start": s.start,
                "end": s.end,
                "text": s.text.strip(),
                "words": [
                    {"w": w.word.strip(), "start": w.start, "end": w.end}
                    for w in (s.words or [])
                ],
            }
        )
    return out


def _pick_with_claude(segments, n, min_s, max_s):
    import anthropic

    lines = "\n".join(
        f"[{s['start']:.1f}-{s['end']:.1f}] {s['text']}" for s in segments
    )
    prompt = (
        f"Below is a timestamped transcript of a long video. Pick the {n} most "
        f"engaging, self-contained moments for TikTok/Reels/Shorts. Each clip must "
        f"be {min_s}-{max_s} seconds, start at the beginning of a sentence with a "
        f"strong hook, and end at a natural stopping point.\n"
        f'Reply with ONLY a JSON array: [{{"start": float, "end": float, '
        f'"title": str, "reason": str}}]\n\nTranscript:\n{lines[:60000]}'
    )
    client = anthropic.Anthropic()
    msg = client.messages.create(
        model="claude-sonnet-5-5",
        max_tokens=1500,
        messages=[{"role": "user", "content": prompt}],
    )
    text = msg.content[0].text.strip().replace("```json", "").replace("```", "")
    return json.loads(text)


def _pick_fallback(duration, n, clip_len=45):
    if duration <= clip_len:
        return [{"start": 0, "end": duration, "title": "Clip 1", "reason": ""}]
    step = max((duration - clip_len) / max(n - 1, 1), clip_len)
    clips, t = [], 0.0
    while t + clip_len <= duration and len(clips) < n:
        clips.append(
            {"start": t, "end": t + clip_len, "title": f"Clip {len(clips)+1}", "reason": ""}
        )
        t += step
    return clips


def pick_clips(segments, duration, n=5, min_s=20, max_s=60):
    clips = None
    if os.getenv("ANTHROPIC_API_KEY") and segments:
        try:
            clips = _pick_with_claude(segments, n, min_s, max_s)
        except Exception as e:
            print("Claude pick failed, using fallback:", e)
    if not clips:
        clips = _pick_fallback(duration, n)
    valid = []
    for c in clips:
        start = max(0.0, float(c["start"]))
        end = min(float(duration), float(c["end"])) if duration else float(c["end"])
        if end - start >= 5:
            valid.append({**c, "start": start, "end": end})
    return valid[:n]


def cut_clip(src: Path, start: float, end: float, out: Path):
    """Cut + center-crop to 9:16 (1080x1920). Face tracking comes in step 2."""
    vf = "crop='min(iw,ih*9/16)':'min(ih,iw*16/9)',scale=1080:1920,setsar=1"
    cmd = [
        "ffmpeg", "-y", "-ss", f"{start:.2f}", "-i", str(src),
        "-t", f"{end - start:.2f}", "-vf", vf,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(out),
    ]
    subprocess.run(cmd, check=True, capture_output=True)


def run_job(job_id: str, url: str, jobs: dict):
    job = jobs[job_id]
    work = DATA_DIR / "jobs" / job_id
    try:
        job.update(status="downloading", progress=5)
        src, title, duration = download_video(url, work)
        job["title"] = title

        job.update(status="transcribing", progress=25)
        segments = transcribe(src)
        (work / "transcript.json").write_text(json.dumps(segments, ensure_ascii=False))

        job.update(status="selecting", progress=60)
        picks = pick_clips(segments, duration, n=int(os.getenv("CLIP_COUNT", 5)))

        job.update(status="rendering", progress=65)
        clips = []
        for i, c in enumerate(picks, 1):
            name = f"clip_{i}.mp4"
            cut_clip(src, c["start"], c["end"], work / name)
            clips.append({**c, "file": f"/files/{job_id}/{name}"})
            job["progress"] = 65 + int(30 * i / len(picks))

        job.update(status="done", progress=100, clips=clips)
    except Exception as e:
        job.update(status="error", error=str(e))

# ---------------- API ----------------
import os
import uuid
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel


app = FastAPI(title="ClipForge AI")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)

JOBS_DIR = Path(DATA_DIR) / "jobs"
JOBS_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/files", StaticFiles(directory=JOBS_DIR), name="files")

jobs: dict = {}  # in-memory for now; Redis in step 4


class JobIn(BaseModel):
    url: str


@app.post("/api/jobs")
def create_job(body: JobIn, bg: BackgroundTasks):
    if not body.url.startswith(("http://", "https://")):
        raise HTTPException(400, "Invalid URL")
    job_id = uuid.uuid4().hex[:12]
    jobs[job_id] = {"id": job_id, "status": "queued", "progress": 0, "clips": []}
    bg.add_task(run_job, job_id, body.url, jobs)
    return jobs[job_id]


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    if job_id not in jobs:
        raise HTTPException(404, "Job not found")
    return jobs[job_id]


@app.get("/health")
def health():
    return {"ok": True}
