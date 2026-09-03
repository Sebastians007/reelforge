"""
Reelforge render engine.

Takes a news story + topic settings, produces a faceless video, uploads it to
YouTube as unlisted, and reports the result back to the Reelforge dashboard.

Pipeline: Claude (script + segment keywords) -> ElevenLabs (voice + word
timestamps) -> match segments to real timing -> Pexels (one image per
segment) -> FFmpeg (composite + captions + render) -> YouTube upload -> webhook.

Run by .github/workflows/make-video.yml on GitHub Actions. All inputs come
from environment variables set there.
"""

import base64
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import requests
from anthropic import Anthropic

ASPECT_DIMENSIONS = {
    "9:16": (1080, 1920),
    "16:9": (1920, 1080),
    "1:1": (1080, 1080),
}


def env(name: str, required: bool = True) -> str:
    value = os.environ.get(name, "")
    if required and not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def write_script_and_segments(story_title: str, story_summary: str) -> list[dict]:
    """Asks Claude for a spoken script split into segments, each tagged with an image keyword."""
    client = Anthropic(api_key=env("ANTHROPIC_API_KEY"))

    prompt = f"""Write a 50-60 second spoken video script about this news story.
Title: {story_title}
Summary: {story_summary}

Plain, punchy, no fluff, no "in conclusion." Split the script into 6-9 short
segments (one or two sentences each, natural pause points). For each segment
give a short visual search keyword (2-4 words) that a stock photo site would
have good results for.

Reply with ONLY valid JSON in this exact shape, no other text:
{{"segments": [{{"text": "...", "keyword": "..."}}, ...]}}"""

    response = client.messages.create(
        model="claude-sonnet-5",
        max_tokens=1000,
        messages=[{"role": "user", "content": prompt}],
    )
    text_blocks = [block.text for block in response.content if block.type == "text"]
    if not text_blocks:
        raise RuntimeError(f"Claude returned no text block: {response.content}")
    raw = text_blocks[0].strip()

    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        raise RuntimeError(f"Claude did not return JSON:\n{raw}")

    data = json.loads(match.group(0))
    segments = data["segments"]
    if not segments:
        raise RuntimeError("Claude returned zero segments")
    return segments


def synthesize_voice_with_timing(segments: list[dict], voice_id: str, out_dir: Path) -> tuple[Path, list[dict]]:
    """Calls ElevenLabs once for the whole script, gets back audio + per-character timing,
    then works out the real start/end time of each segment."""
    full_text = " ".join(s["text"] for s in segments)

    res = requests.post(
        f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/with-timestamps",
        headers={
            "xi-api-key": env("ELEVENLABS_API_KEY"),
            "content-type": "application/json",
        },
        json={"text": full_text, "model_id": "eleven_multilingual_v2"},
        timeout=120,
    )
    res.raise_for_status()
    data = res.json()

    audio_path = out_dir / "voice.mp3"
    audio_path.write_bytes(base64.b64decode(data["audio_base64"]))

    starts = data["alignment"]["character_start_times_seconds"]
    ends = data["alignment"]["character_end_times_seconds"]

    timed_segments = []
    cursor = 0
    for seg in segments:
        text = seg["text"]
        seg_start_char = cursor
        seg_end_char = cursor + len(text) - 1
        timed_segments.append(
            {
                "text": text,
                "keyword": seg["keyword"],
                "start": starts[min(seg_start_char, len(starts) - 1)],
                "end": ends[min(seg_end_char, len(ends) - 1)],
            }
        )
        cursor += len(text) + 1  # +1 for the joining space

    return audio_path, timed_segments


def fetch_images(timed_segments: list[dict], aspect_ratio: str, out_dir: Path) -> list[Path]:
    orientation = "portrait" if aspect_ratio == "9:16" else "landscape"
    images = []
    for i, seg in enumerate(timed_segments):
        res = requests.get(
            "https://api.pexels.com/v1/search",
            headers={"Authorization": env("PEXELS_API_KEY")},
            params={"query": seg["keyword"], "per_page": 1, "orientation": orientation},
            timeout=30,
        )
        res.raise_for_status()
        photos = res.json().get("photos", [])
        if not photos:
            # fall back to a generic keyword if nothing matched
            res = requests.get(
                "https://api.pexels.com/v1/search",
                headers={"Authorization": env("PEXELS_API_KEY")},
                params={"query": "news technology", "per_page": 1, "orientation": orientation},
                timeout=30,
            )
            res.raise_for_status()
            photos = res.json()["photos"]

        image_url = photos[0]["src"]["large2x"]
        image_path = out_dir / f"image_{i:02d}.jpg"
        image_path.write_bytes(requests.get(image_url, timeout=60).content)
        images.append(image_path)
    return images


def build_srt(timed_segments: list[dict], srt_path: Path) -> None:
    def fmt(t: float) -> str:
        h, rem = divmod(t, 3600)
        m, s = divmod(rem, 60)
        ms = int((s - int(s)) * 1000)
        return f"{int(h):02d}:{int(m):02d}:{int(s):02d},{ms:03d}"

    lines = []
    for i, seg in enumerate(timed_segments, start=1):
        lines.append(str(i))
        lines.append(f"{fmt(seg['start'])} --> {fmt(seg['end'])}")
        lines.append(seg["text"])
        lines.append("")
    srt_path.write_text("\n".join(lines), encoding="utf-8")


def render_video(
    timed_segments: list[dict], images: list[Path], audio_path: Path, aspect_ratio: str, out_dir: Path
) -> Path:
    width, height = ASPECT_DIMENSIONS.get(aspect_ratio, ASPECT_DIMENSIONS["9:16"])

    # One silent clip per image, held for exactly that segment's duration.
    clip_list_path = out_dir / "clips.txt"
    clip_paths = []
    for i, (seg, image) in enumerate(zip(timed_segments, images)):
        duration = max(seg["end"] - seg["start"], 0.5)
        clip_path = out_dir / f"clip_{i:02d}.mp4"
        subprocess.run(
            [
                "ffmpeg", "-y", "-loop", "1", "-i", str(image), "-t", f"{duration:.2f}",
                "-vf", f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height}",
                "-r", "30", "-pix_fmt", "yuv420p", str(clip_path),
            ],
            check=True,
            capture_output=True,
        )
        clip_paths.append(clip_path)

    clip_list_path.write_text("\n".join(f"file '{p.name}'" for p in clip_paths), encoding="utf-8")

    silent_video_path = out_dir / "silent.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(clip_list_path), "-c", "copy", str(silent_video_path)],
        check=True,
        capture_output=True,
        cwd=out_dir,
    )

    srt_path = out_dir / "captions.srt"
    build_srt(timed_segments, srt_path)

    final_path = out_dir / "final.mp4"
    srt_escaped = str(srt_path).replace("\\", "/").replace(":", "\\:")
    subtitle_style = "FontSize=16,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BorderStyle=3,Outline=2"
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-i", str(silent_video_path),
            "-i", str(audio_path),
            "-vf", f"subtitles='{srt_escaped}':force_style='{subtitle_style}'",
            "-map", "0:v:0", "-map", "1:a:0",
            "-c:v", "libx264", "-c:a", "aac", "-shortest",
            str(final_path),
        ],
        check=True,
        capture_output=True,
    )
    return final_path


def upload_to_youtube(video_path: Path, title: str, description: str) -> str:
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    creds = Credentials(
        token=None,
        refresh_token=env("YOUTUBE_REFRESH_TOKEN"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=env("YOUTUBE_CLIENT_ID"),
        client_secret=env("YOUTUBE_CLIENT_SECRET"),
    )
    youtube = build("youtube", "v3", credentials=creds)

    request = youtube.videos().insert(
        part="snippet,status",
        body={
            "snippet": {"title": title[:100], "description": description, "categoryId": "25"},
            "status": {"privacyStatus": "unlisted"},
        },
        media_body=MediaFileUpload(str(video_path), chunksize=-1, resumable=True),
    )
    response = None
    while response is None:
        _, response = request.next_chunk()
    return response["id"]


def report_result(webhook_url: str, webhook_secret: str, run_id: str, status: str, **extra) -> None:
    try:
        requests.post(
            webhook_url,
            json={"secret": webhook_secret, "run_id": int(run_id), "status": status, **extra},
            timeout=30,
        )
    except Exception as e:
        print(f"Warning: failed to call webhook: {e}", file=sys.stderr)


def main() -> None:
    run_id = env("RUN_ID")
    webhook_url = env("WEBHOOK_URL")
    webhook_secret = env("WEBHOOK_SECRET")

    try:
        story_title = env("STORY_TITLE")
        story_summary = env("STORY_SUMMARY")
        story_link = env("STORY_LINK", required=False)
        aspect_ratio = env("ASPECT_RATIO", required=False) or "9:16"
        voice_id = env("VOICE_ID")

        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)

            segments = write_script_and_segments(story_title, story_summary)
            audio_path, timed_segments = synthesize_voice_with_timing(segments, voice_id, out_dir)
            images = fetch_images(timed_segments, aspect_ratio, out_dir)
            video_path = render_video(timed_segments, images, audio_path, aspect_ratio, out_dir)

            description = f"Automated daily video. Source: {story_link}" if story_link else "Automated daily video."
            youtube_id = upload_to_youtube(video_path, story_title, description)

        report_result(webhook_url, webhook_secret, run_id, "uploaded", youtube_video_id=youtube_id)
        print(f"Done. YouTube video: https://youtu.be/{youtube_id}")

    except Exception as e:
        report_result(webhook_url, webhook_secret, run_id, "failed", error_message=str(e))
        raise


if __name__ == "__main__":
    main()
