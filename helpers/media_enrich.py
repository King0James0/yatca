"""
YATCA media enrichment.

Turns inbound voice / audio / video / document attachments into text the
agent's model can actually use, *before* the message is handed to A0:

  - voice / audio  -> transcribed via A0's built-in Whisper STT plugin,
                      in-process. Reuses the model size + language already
                      configured in Settings; no extra dependency, no second
                      model download.
  - video          -> audio transcribed (Whisper, ffmpeg pulls the stream) +
                      frames sampled evenly across the clip as images for
                      vision. The frame count scales with the video's length and
                      is capped by the chat model's `max_embeds` (read live at
                      runtime), so it never exceeds the user's image limit.
  - documents      -> PDF / DOCX / TXT text extracted inline
                      (pdfminer / python-docx, capped at _DOC_MAX_CHARS).

Designed to degrade gracefully: any failure leaves the original attachment in
place and simply adds no extra text, so the agent still receives the raw file.

Enabled per-bot via the `enrich_media` config flag (default on). Images are
left untouched -- the model's own vision handles those.
"""
import base64
import math
import os
import subprocess

from helpers import files
from helpers.print_style import PrintStyle

# clean_env: least-privilege env for the ffmpeg/ffprobe child (it must NOT inherit A0's runtime secrets).
# Multi-name shim + inline fallback (identical allowlist) so a missing import can't re-leak or break.
clean_env = None  # type: ignore[assignment]
for _se_name in ("usr.plugins.yatca.helpers.secure_env",
                 "plugins.yatca.helpers.secure_env",
                 "helpers.secure_env", "secure_env"):
    try:
        import importlib
        clean_env = importlib.import_module(_se_name).clean_env  # type: ignore
        break
    except Exception:  # pragma: no cover
        continue
if clean_env is None:  # pragma: no cover - import fallback; identical to secure_env.clean_env
    def clean_env(extra=None, *, allow=(), proxy=True):  # type: ignore[misc]
        _k = {"PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE",
              "TZ", "DISPLAY", "XDG_CONFIG_HOME", "XDG_RUNTIME_DIR", "XDG_CACHE_HOME",
              "XDG_DATA_HOME", "TMPDIR", "TMP", "TEMP"} | set(allow)
        if proxy:
            _k |= {"HTTP_PROXY", "HTTPS_PROXY", "FTP_PROXY", "ALL_PROXY", "NO_PROXY",
                   "http_proxy", "https_proxy", "ftp_proxy", "all_proxy", "no_proxy"}
        _e = {k: os.environ[k] for k in _k if k in os.environ}
        if extra:
            _e.update({k: v for k, v in extra.items() if v is not None})
        return _e


_AUDIO_EXTS = {".ogg", ".oga", ".opus", ".mp3", ".m4a", ".wav", ".flac", ".aac", ".weba"}
_VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff"}
_DOC_EXTS = {".txt", ".md", ".pdf", ".docx", ".doc"}
_DOC_MAX_CHARS = 8000

# One frame is sampled per this many seconds of video (per-bot override:
# `video_frame_interval_s`). The resulting count is then capped by the chat
# model's max_embeds so we never exceed the user's configured image limit.
_VIDEO_FRAME_INTERVAL_DEFAULT = 5
# Defensive fallback used ONLY when the model's max_embeds can't be read
# (e.g. an A0 build without the _model_config plugin). Kept conservative so we
# don't flood an instance whose real limit we couldn't determine.
_MAX_EMBEDS_FALLBACK = 6


# ---------------------------------------------------------------------------
#  Small helpers
# ---------------------------------------------------------------------------

def _safe_remove(path: str | None):
    if not path:
        return
    try:
        if os.path.isfile(path):
            os.remove(path)
    except OSError:
        pass


def _to_ref(ref_sibling: str, local_new: str) -> str:
    """Build a dockerized ref path for a newly-created file that lives in the
    same download dir as an existing attachment's local file."""
    return os.path.join(os.path.dirname(ref_sibling), os.path.basename(local_new))


# ---------------------------------------------------------------------------
#  Transcription (reuses A0's built-in Whisper STT plugin)
# ---------------------------------------------------------------------------

async def _transcribe_file(local_path: str) -> str:
    """Transcribe an audio (or audio-bearing video) file via A0's Whisper STT.
    Whisper decodes via ffmpeg, so any container format works. Returns '' on
    any failure or if the STT plugin is disabled/absent."""
    try:
        from plugins._whisper_stt.helpers import runtime
    except Exception:
        return ""
    try:
        if not runtime.is_globally_enabled():
            return ""
        with open(local_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        result = await runtime.transcribe(b64)
        return str((result or {}).get("text") or "").strip()
    except Exception as e:
        PrintStyle.error(f"YATCA media_enrich: transcription failed: {e}")
        return ""


# ---------------------------------------------------------------------------
#  Document text extraction
# ---------------------------------------------------------------------------

def _extract_doc_text(local_path: str, filename: str) -> str:
    ext = os.path.splitext(filename.lower())[1]
    try:
        if ext in (".txt", ".md"):
            with open(local_path, "r", errors="replace") as f:
                return f.read()[:_DOC_MAX_CHARS].strip()
        if ext == ".pdf":
            from pdfminer.high_level import extract_text
            return (extract_text(local_path) or "").strip()[:_DOC_MAX_CHARS]
        if ext in (".docx", ".doc"):
            from docx import Document
            doc = Document(local_path)
            return "\n".join(p.text for p in doc.paragraphs).strip()[:_DOC_MAX_CHARS]
    except Exception as e:
        PrintStyle.error(f"YATCA media_enrich: doc extraction failed for {filename}: {e}")
    return ""


# ---------------------------------------------------------------------------
#  Model image limit (max_embeds) — read live from the user's chat model config
# ---------------------------------------------------------------------------

def _get_max_embeds() -> int:
    """The chat model's per-message image cap, read from A0's model config.

    This is the user's configured limit (10 in a default install). We read it
    live so the plugin never hardcodes an image budget — every instance gets its
    own. Falls back to a conservative default only if the config can't be read.
    """
    try:
        from plugins._model_config.helpers.model_config import get_chat_model_config
        cfg = get_chat_model_config() or {}
        val = cfg.get("max_embeds")
        if isinstance(val, int) and val > 0:
            return val
    except Exception:
        pass
    return _MAX_EMBEDS_FALLBACK


# ---------------------------------------------------------------------------
#  Video frame extraction
# ---------------------------------------------------------------------------

def _video_duration(local_path: str) -> float:
    """Video length in seconds via ffprobe. Returns 0.0 if it can't be read."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", local_path],
            capture_output=True, timeout=30, text=True,
            env=clean_env(),  # least-privilege: ffprobe decodes untrusted media, gets no A0 secrets
        )
        return max(0.0, float(out.stdout.strip()))
    except Exception:
        return 0.0


def _plan_frame_count(duration: float, interval: float, budget: int) -> int:
    """How many frames to sample: one per `interval` seconds, capped at `budget`.

    Longer video -> more frames; always >=1 when there's budget, never over it.
    If duration is unknown (0), fall back to a single frame.
    """
    if budget < 1:
        return 0
    if duration <= 0 or interval <= 0:
        return 1
    return min(budget, max(1, math.ceil(duration / interval)))


def _extract_video_frames(local_path: str, n: int, duration: float) -> list[str]:
    """Sample `n` frames evenly across the clip as jpgs. Returns local paths.

    Frames are centered in their slice (t = duration*(i+0.5)/n), which spreads
    them across the whole video and avoids both the dead frame at t=0 and any
    seek past the end on very short clips (the old fixed 1s seek failed there).
    """
    if n <= 0:
        return []
    base = os.path.splitext(local_path)[0]
    paths: list[str] = []
    for i in range(n):
        ts = (duration * (i + 0.5) / n) if duration > 0 else 1.0
        frame_path = f"{base}.frame{i + 1:02d}.jpg"
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-ss", f"{ts:.3f}", "-i", local_path,
                 "-frames:v", "1", frame_path],
                capture_output=True, timeout=30,
                env=clean_env(),  # least-privilege: ffmpeg decodes untrusted media, gets no A0 secrets
            )
            if os.path.isfile(frame_path) and os.path.getsize(frame_path) > 0:
                paths.append(frame_path)
        except Exception as e:
            PrintStyle.error(f"YATCA media_enrich: frame extraction failed at {ts:.1f}s: {e}")
    return paths


# ---------------------------------------------------------------------------
#  Entry point
# ---------------------------------------------------------------------------

async def enrich(attachments: list[str], message, bot_cfg: dict | None = None) -> tuple[str, list[str]]:
    """Process downloaded attachments.

    `attachments` are the dockerized reference paths returned by
    handler._download_attachments. Returns (extra_text, final_attachment_paths):
      - extra_text: transcripts / extracted document text to append to the body
      - final_attachment_paths: attachments to actually send to the agent
        (raw audio that was transcribed is dropped; raw video is replaced by
        frames sampled across it; documents are kept alongside their text)

    Video frames are budgeted against the chat model's `max_embeds` (read live),
    minus any other images already in this message, so the total images sent
    never exceed the user's configured per-message limit.
    """
    if not attachments:
        return "", attachments

    extra_parts: list[str] = []
    final: list[str] = []

    # Image budget for sampled video frames: the model's own max_embeds, less
    # whatever images the user already attached in this same message. Shared
    # across multiple videos in one message (rare) so the cap holds overall.
    interval = _VIDEO_FRAME_INTERVAL_DEFAULT
    if bot_cfg:
        iv = bot_cfg.get("video_frame_interval_s")
        if isinstance(iv, (int, float)) and iv > 0:
            interval = float(iv)
    max_embeds = _get_max_embeds()
    reserved_images = sum(
        1 for r in attachments if os.path.splitext(r.lower())[1] in _IMAGE_EXTS
    )
    frame_budget = max(0, max_embeds - reserved_images)

    for ref_path in attachments:
        local_path = files.fix_dev_path(ref_path)
        ext = os.path.splitext(local_path.lower())[1]
        filename = os.path.basename(ref_path)

        if ext in _AUDIO_EXTS:
            text = await _transcribe_file(local_path)
            if text:
                extra_parts.append(f"[Transcribed audio]: {text}")
                _safe_remove(local_path)  # raw audio is useless to the model
            else:
                final.append(ref_path)  # keep raw if transcription unavailable

        elif ext in _VIDEO_EXTS:
            transcript = await _transcribe_file(local_path)
            duration = _video_duration(local_path)
            n_frames = _plan_frame_count(duration, interval, frame_budget)
            frame_paths = _extract_video_frames(local_path, n_frames, duration)
            frame_refs = [_to_ref(ref_path, fp) for fp in frame_paths]
            frame_budget -= len(frame_refs)  # keep the cap across multiple videos

            if transcript:
                extra_parts.append(f"[Video -- transcribed audio]: {transcript}")
            if frame_refs:
                if len(frame_refs) == 1:
                    extra_parts.append("[A frame from the video is attached as an image for visual analysis.]")
                else:
                    extra_parts.append(
                        f"[{len(frame_refs)} frames sampled evenly across the video are attached "
                        f"as images, in chronological order, for visual analysis.]"
                    )
                final.extend(frame_refs)

            if transcript or frame_refs:
                _safe_remove(local_path)  # raw video no longer needed
            else:
                final.append(ref_path)  # fallback: keep raw video

        elif ext in _DOC_EXTS:
            text = _extract_doc_text(local_path, filename)
            if text:
                extra_parts.append(f"[Document '{filename}']:\n{text}")
            final.append(ref_path)  # always keep the file so the agent can open it

        else:
            final.append(ref_path)  # images and anything else pass through

    return "\n".join(extra_parts), final
