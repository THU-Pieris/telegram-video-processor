import asyncio
import json
import logging
import os
import re
import shlex
import tempfile
import time
from urllib.parse import unquote, urlsplit
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import aiohttp
from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.tl.types import DocumentAttributeVideo


load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("tvp")
PROJECT_DIR = Path(__file__).resolve().parent
STATUS_UPDATE_INTERVAL_S = 2.0
URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)


def default_ffmpeg_bin() -> str:
    local_ffmpeg = PROJECT_DIR / "vendor" / "ffmpeg" / "ffmpeg"
    return str(local_ffmpeg) if local_ffmpeg.exists() else "ffmpeg"


def default_ffprobe_bin() -> str:
    local_ffprobe = PROJECT_DIR / "vendor" / "ffmpeg" / "ffprobe"
    return str(local_ffprobe) if local_ffprobe.exists() else "ffprobe"


def default_session_name() -> str:
    session_dir = PROJECT_DIR / ".state" / "telethon"
    session_dir.mkdir(parents=True, exist_ok=True)
    return str(session_dir / "tvp_user")


def format_duration(seconds: int | float) -> str:
    total = max(0, int(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def progress_bar(current: int | float, total: int | float, width: int = 12) -> str:
    if not total or total <= 0:
        return "[............]"
    ratio = max(0.0, min(1.0, float(current) / float(total)))
    filled = int(ratio * width)
    return "[" + ("#" * filled) + ("." * (width - filled)) + "]"


def scale_short_side_filter(target: int) -> str:
    # "480p/720p/1080p" means cap the shorter side to the target while
    # preserving aspect ratio (and avoiding upscaling).
    return (
        "scale="
        f"'if(gte(iw,ih),-2,min({target},iw))':"
        f"'if(gte(iw,ih),min({target},ih),-2)'"
        ":flags=lanczos,setsar=1"
    )


@dataclass
class Settings:
    api_id: int
    api_hash: str
    session_name: str
    allowed_chats: set[str]
    work_dir: Path
    ffmpeg_bin: str
    ffprobe_bin: str
    command_prefix: str
    cleanup: bool

    @classmethod
    def from_env(cls) -> "Settings":
        api_id_raw = os.getenv("API_ID")
        api_hash = os.getenv("API_HASH")
        if not api_id_raw or not api_hash:
            raise RuntimeError("API_ID and API_HASH are required")

        allowed = {
            item.strip()
            for item in os.getenv("ALLOWED_CHATS", "").split(",")
            if item.strip()
        }
        work_dir = Path(os.getenv("WORK_DIR", "./work")).expanduser()
        work_dir.mkdir(parents=True, exist_ok=True)

        return cls(
            api_id=int(api_id_raw),
            api_hash=api_hash,
            session_name=(os.getenv("SESSION_NAME", "").strip() or default_session_name()),
            allowed_chats=allowed,
            work_dir=work_dir,
            ffmpeg_bin=(os.getenv("FFMPEG_BIN", "").strip() or default_ffmpeg_bin()),
            ffprobe_bin=(os.getenv("FFPROBE_BIN", "").strip() or default_ffprobe_bin()),
            command_prefix=os.getenv("COMMAND_PREFIX", ".vp"),
            cleanup=os.getenv("CLEANUP", "true").lower() in {"1", "true", "yes", "y"},
        )


def is_video_message(msg) -> bool:
    # `msg` is a Telethon Message (e.g. reply message), not an event wrapper.
    if not getattr(msg, "media", None):
        return False

    # Native Telegram video / video note cases.
    if getattr(msg, "video", None) or getattr(msg, "video_note", None):
        return True

    file = getattr(msg, "file", None)
    if not file:
        return False
    if file.mime_type and file.mime_type.startswith("video/"):
        return True

    # Some clients upload videos as documents with a video attribute.
    document = getattr(msg, "document", None)
    attrs = getattr(document, "attributes", None) or []
    return any(isinstance(a, DocumentAttributeVideo) for a in attrs)


def parse_command(text: str, prefix: str) -> tuple[str, str]:
    text = (text or "").strip()
    if not text.startswith(prefix):
        return "", ""
    tail = text[len(prefix) :].strip()
    if not tail:
        return "720p", ""
    parts = tail.split(None, 1)
    mode = parts[0].lower()
    extra = parts[1] if len(parts) > 1 else ""
    return mode, extra


def build_ffmpeg_cmd(ffmpeg_bin: str, mode: str, src: Path, dst: Path, extra: str) -> list[str]:
    base = [ffmpeg_bin, "-y", "-hide_banner", "-progress", "pipe:1", "-nostats", "-i", str(src)]
    mode = mode.lower()

    if mode in {"mp4", "h264"}:
        return base + [
            "-map",
            "0:v:0",
            "-map",
            "0:a?",
            "-vf",
            "setsar=1",
            "-c:v",
            "libx264",
            "-preset",
            "slow",
            "-crf",
            "30",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "96k",
            "-movflags",
            "+faststart",
            str(dst),
        ]

    if mode == "480p":
        return base + [
            "-map",
            "0:v:0",
            "-map",
            "0:a?",
            "-vf",
            scale_short_side_filter(480),
            "-c:v",
            "libx264",
            "-preset",
            "slow",
            "-crf",
            "31",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "64k",
            "-movflags",
            "+faststart",
            str(dst),
        ]

    if mode == "720p":
        return base + [
            "-map",
            "0:v:0",
            "-map",
            "0:a?",
            "-vf",
            scale_short_side_filter(720),
            "-c:v",
            "libx264",
            "-preset",
            "slow",
            "-crf",
            "30",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "80k",
            "-movflags",
            "+faststart",
            str(dst),
        ]

    if mode == "1080p":
        return base + [
            "-map",
            "0:v:0",
            "-map",
            "0:a?",
            "-vf",
            scale_short_side_filter(1080),
            "-c:v",
            "libx264",
            "-preset",
            "slow",
            "-crf",
            "29",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "96k",
            "-movflags",
            "+faststart",
            str(dst),
        ]

    if mode == "mute":
        return base + [
            "-map",
            "0:v:0",
            "-vf",
            "setsar=1",
            "-c:v",
            "libx264",
            "-preset",
            "slow",
            "-crf",
            "30",
            "-pix_fmt",
            "yuv420p",
            "-an",
            "-movflags",
            "+faststart",
            str(dst),
        ]

    if mode == "copy":
        return base + [
            "-c",
            "copy",
            str(dst),
        ]

    if mode == "custom":
        if not extra.strip():
            raise ValueError("custom mode requires ffmpeg arguments after the mode")
        # User-supplied ffmpeg args are parsed shell-style. Use only in trusted chats.
        user_args = shlex.split(extra)
        return [ffmpeg_bin, "-y", "-hide_banner", "-progress", "pipe:1", "-nostats", "-i", str(src), *user_args, str(dst)]

    raise ValueError(f"Unsupported mode: {mode}")


def extract_first_http_url(text: str | None) -> str | None:
    if not text:
        return None
    match = URL_RE.search(text)
    if not match:
        return None
    return match.group(0).strip("()[]<>.,!?:;\"'")


def filename_from_url(url: str, fallback: str) -> str:
    path = urlsplit(url).path
    name = Path(unquote(path)).name.strip()
    if name and "." in name:
        return name
    return fallback


async def run_subprocess(cmd: Iterable[str]) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    text = (out or b"").decode("utf-8", errors="replace")
    return proc.returncode, text


class StatusUpdater:
    def __init__(self, message):
        self.message = message
        self._last_text = None
        self._last_edit = 0.0
        self._lock = asyncio.Lock()

    async def set(self, text: str, force: bool = False) -> None:
        now = time.monotonic()
        if not force and text == self._last_text:
            return
        if not force and (now - self._last_edit) < STATUS_UPDATE_INTERVAL_S:
            return
        async with self._lock:
            now = time.monotonic()
            if not force and text == self._last_text:
                return
            if not force and (now - self._last_edit) < STATUS_UPDATE_INTERVAL_S:
                return
            try:
                await self.message.edit(text)
                self._last_text = text
                self._last_edit = now
            except Exception:
                log.exception("Failed to edit status message")

    def progress_callback(self, stage: str):
        state = {"last": 0.0}

        def cb(current: int, total: int):
            now = time.monotonic()
            if total and current < total and (now - state["last"]) < STATUS_UPDATE_INTERVAL_S:
                return
            state["last"] = now
            pct = (current / total * 100.0) if total else 0.0
            line = f"{stage} {progress_bar(current, total)} {pct:5.1f}% ({current//1048576} / {max(total,0)//1048576} MiB)"
            asyncio.create_task(self.set(line))

        return cb


def _parse_int(value, default=0) -> int:
    try:
        if value is None:
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


async def probe_video_metadata(ffprobe_bin: str, path: Path) -> dict | None:
    cmd = [
        ffprobe_bin,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_streams",
        "-show_format",
        str(path),
    ]
    code, out = await run_subprocess(cmd)
    if code != 0:
        log.warning("ffprobe failed (%s): %s", code, out[-1000:])
        return None

    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        log.warning("ffprobe returned invalid JSON")
        return None

    streams = data.get("streams") or []
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    if not v:
        return None

    width = _parse_int(v.get("width"))
    height = _parse_int(v.get("height"))

    # Prefer stream duration; fall back to container duration.
    duration = _parse_int(v.get("duration"))
    if duration <= 0:
        duration = _parse_int((data.get("format") or {}).get("duration"))

    if width <= 0 or height <= 0:
        return None

    return {"width": width, "height": height, "duration": max(duration, 0)}


async def download_from_direct_url(url: str, dst: Path, status: StatusUpdater) -> Path:
    timeout = aiohttp.ClientTimeout(total=None, connect=30, sock_connect=30, sock_read=120)
    downloaded = 0
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, allow_redirects=True) as resp:
            if resp.status >= 400:
                raise RuntimeError(f"source URL returned HTTP {resp.status}")

            total = _parse_int(resp.headers.get("Content-Length"), default=0)
            content_type = (resp.headers.get("Content-Type") or "").lower()
            if content_type and not (
                content_type.startswith("video/")
                or "octet-stream" in content_type
                or "application/mp4" in content_type
            ):
                raise RuntimeError(f"URL is not a video response (content-type: {content_type})")

            with dst.open("wb") as f:
                async for chunk in resp.content.iter_chunked(1024 * 256):
                    if not chunk:
                        continue
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total > 0:
                        pct = min(100.0, downloaded / total * 100.0)
                        text = (
                            f"Downloading URL {progress_bar(downloaded, total)} "
                            f"{pct:5.1f}% ({downloaded//1048576} / {total//1048576} MiB)"
                        )
                    else:
                        text = f"Downloading URL {downloaded//1048576} MiB"
                    await status.set(text)

    if downloaded <= 0:
        raise RuntimeError("downloaded file is empty")

    return dst


async def run_ffmpeg_with_progress(
    cmd: Iterable[str],
    status: StatusUpdater,
    input_duration_s: int | None = None,
) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    assert proc.stdout is not None

    lines: list[str] = []
    last_processed_s = 0
    async for raw in proc.stdout:
        line = raw.decode("utf-8", errors="replace").rstrip()
        lines.append(line)
        if line.startswith("out_time_ms="):
            try:
                out_ms = int(line.split("=", 1)[1])
                last_processed_s = max(0, out_ms // 1_000_000)
            except ValueError:
                continue
            if input_duration_s and input_duration_s > 0:
                pct = min(100.0, (last_processed_s / input_duration_s) * 100.0)
                text = (
                    f"Processing {progress_bar(last_processed_s, input_duration_s)} "
                    f"{pct:5.1f}% ({format_duration(last_processed_s)} / {format_duration(input_duration_s)})"
                )
            else:
                text = f"Processing {format_duration(last_processed_s)}"
            await status.set(text)
        elif line.startswith("progress=") and line.endswith("end"):
            await status.set("Processing complete", force=True)

    code = await proc.wait()
    return code, "\n".join(lines)


def chat_allowed(event, settings: Settings) -> bool:
    if not settings.allowed_chats:
        return True

    chat_id = str(event.chat_id) if event.chat_id is not None else ""
    if chat_id in settings.allowed_chats:
        return True

    username = getattr(getattr(event, "chat", None), "username", None)
    if username and username in settings.allowed_chats:
        return True

    return False


async def process_reply(event, settings: Settings, mode: str, extra: str, status_msg=None) -> None:
    reply = await event.get_reply_message()
    if not reply:
        await event.reply("Reply to a video/file or a message containing a direct video URL.")
        return

    status_message = status_msg or await event.reply(f"Queued: `{mode}`")
    status = StatusUpdater(status_message)

    with tempfile.TemporaryDirectory(dir=settings.work_dir) as tmp_dir:
        tmp = Path(tmp_dir)
        src_path: Path
        if getattr(reply, "file", None) and is_video_message(reply):
            src_name = reply.file.name or f"input_{reply.id}"
            requested_src_path = tmp / src_name
            await status.set("Downloading from Telegram...", force=True)
            downloaded = await reply.download_media(
                file=str(requested_src_path),
                progress_callback=status.progress_callback("Downloading"),
            )
            if not downloaded:
                await status.set("Download failed.", force=True)
                return
            src_path = Path(downloaded)
        else:
            source_url = extract_first_http_url(getattr(reply, "raw_text", None) or getattr(reply, "message", None))
            if not source_url:
                await event.reply("Replied message must be a video/file or contain a direct `http(s)` video URL.")
                return
            src_name = filename_from_url(source_url, fallback=f"input_{reply.id}.mp4")
            requested_src_path = tmp / src_name
            await status.set("Downloading from direct URL...", force=True)
            try:
                src_path = await download_from_direct_url(source_url, requested_src_path, status)
            except Exception as exc:
                await status.set(f"Direct URL download failed: {exc}", force=True)
                return

        out_suffix = ".mkv" if mode == "copy" else ".mp4"
        out_path = tmp / f"{src_path.stem}_{mode}{out_suffix}"
        in_meta = await probe_video_metadata(settings.ffprobe_bin, src_path)
        if not in_meta:
            await status.set("Input is not a valid video or metadata probe failed.", force=True)
            return
        await status.set("Processing with ffmpeg...", force=True)

        try:
            cmd = build_ffmpeg_cmd(settings.ffmpeg_bin, mode, src_path, out_path, extra)
        except Exception as exc:
            await status.set(f"Invalid command: {exc}", force=True)
            return

        log.info("Running ffmpeg: %s", " ".join(shlex.quote(x) for x in cmd))
        code, ffout = await run_ffmpeg_with_progress(
            cmd,
            status,
            input_duration_s=(in_meta or {}).get("duration"),
        )
        if code != 0:
            log.error("ffmpeg failed (%s): %s", code, ffout[-4000:])
            await status.set(f"ffmpeg failed (exit {code}). Check logs.", force=True)
            return

        if not out_path.exists():
            await status.set("ffmpeg finished, but output file was not created.", force=True)
            return

        await status.set("Uploading result...", force=True)

        caption = f"Processed with `{mode}`"
        attributes = None
        if out_path.suffix.lower() == ".mp4":
            meta = await probe_video_metadata(settings.ffprobe_bin, out_path)
            if meta:
                log.info(
                    "Uploading with explicit video attrs: %sx%s, %ss",
                    meta["width"],
                    meta["height"],
                    meta["duration"],
                )
                attributes = [
                    DocumentAttributeVideo(
                        duration=meta["duration"],
                        w=meta["width"],
                        h=meta["height"],
                        supports_streaming=True,
                    )
                ]

        await event.client.send_file(
            event.chat_id,
            str(out_path),
            caption=caption,
            reply_to=reply.id,
            supports_streaming=out_path.suffix.lower() == ".mp4",
            force_document=False,
            attributes=attributes,
            progress_callback=status.progress_callback("Uploading"),
        )
        await status.set("Done.", force=True)
        try:
            # Delete both the temporary status message and the user trigger message.
            await event.client.delete_messages(event.chat_id, [status_message.id, event.message.id])
        except Exception:
            log.exception("Failed to delete status/trigger message(s)")

        if not settings.cleanup:
            saved_dir = settings.work_dir / f"job_{reply.id}"
            saved_dir.mkdir(parents=True, exist_ok=True)
            src_copy = saved_dir / src_path.name
            out_copy = saved_dir / out_path.name
            src_path.replace(src_copy)
            out_path.replace(out_copy)
            await event.reply(f"Saved files to `{saved_dir}`")


async def main() -> None:
    settings = Settings.from_env()
    client = TelegramClient(settings.session_name, settings.api_id, settings.api_hash)
    job_lock = asyncio.Lock()

    @client.on(events.NewMessage)
    async def handler(event):
        if not chat_allowed(event, settings):
            return

        mode, extra = parse_command(event.raw_text, settings.command_prefix)
        if not mode:
            return
        if not event.is_reply:
            await event.reply("Use this as a reply to a video. Example: `.vp 720p`")
            return

        try:
            queued_msg = None
            if job_lock.locked():
                queued_msg = await event.reply("Queued. Waiting for current job to finish...")
            async with job_lock:
                await process_reply(event, settings, mode, extra, status_msg=queued_msg)
        except Exception:
            log.exception("Unhandled error while processing message")
            await event.reply("Internal error. Check logs.")

    log.info("Starting client. Command prefix: %s", settings.command_prefix)
    log.info("Allowed chats: %s", sorted(settings.allowed_chats) if settings.allowed_chats else "ALL")
    await client.start()
    me = await client.get_me()
    log.info("Logged in as %s (%s)", getattr(me, "username", None) or me.first_name, me.id)
    await client.run_until_disconnected()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
