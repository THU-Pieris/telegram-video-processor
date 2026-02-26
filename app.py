import asyncio
import json
import logging
import os
import shlex
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

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
            command_prefix=os.getenv("COMMAND_PREFIX", ".vproc"),
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
        return "mp4", ""
    parts = tail.split(None, 1)
    mode = parts[0].lower()
    extra = parts[1] if len(parts) > 1 else ""
    return mode, extra


def build_ffmpeg_cmd(ffmpeg_bin: str, mode: str, src: Path, dst: Path, extra: str) -> list[str]:
    base = [ffmpeg_bin, "-y", "-hide_banner", "-i", str(src)]
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
            "medium",
            "-crf",
            "23",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
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
            "scale='min(1280,iw)':-2,setsar=1",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "24",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
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
            "medium",
            "-crf",
            "23",
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
        return [ffmpeg_bin, "-y", "-hide_banner", "-i", str(src), *user_args, str(dst)]

    raise ValueError(f"Unsupported mode: {mode}")


async def run_subprocess(cmd: Iterable[str]) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    text = (out or b"").decode("utf-8", errors="replace")
    return proc.returncode, text


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


async def process_reply(event, settings: Settings, mode: str, extra: str) -> None:
    reply = await event.get_reply_message()
    if not reply or not reply.file:
        await event.reply("Reply to a video/file with the command.")
        return
    if not is_video_message(reply):
        await event.reply("Replied message is not a video.")
        return

    await event.reply(f"Queued: `{mode}`")

    with tempfile.TemporaryDirectory(dir=settings.work_dir) as tmp_dir:
        tmp = Path(tmp_dir)
        src_name = reply.file.name or f"input_{reply.id}"
        requested_src_path = tmp / src_name

        status = await event.reply("Downloading...")
        downloaded = await reply.download_media(file=str(requested_src_path))
        if not downloaded:
            await status.edit("Download failed.")
            return

        src_path = Path(downloaded)
        out_suffix = ".mkv" if mode == "copy" else ".mp4"
        out_path = tmp / f"{src_path.stem}_{mode}{out_suffix}"
        await status.edit("Processing with ffmpeg...")

        try:
            cmd = build_ffmpeg_cmd(settings.ffmpeg_bin, mode, src_path, out_path, extra)
        except Exception as exc:
            await status.edit(f"Invalid command: {exc}")
            return

        log.info("Running ffmpeg: %s", " ".join(shlex.quote(x) for x in cmd))
        code, ffout = await run_subprocess(cmd)
        if code != 0:
            log.error("ffmpeg failed (%s): %s", code, ffout[-4000:])
            await status.edit(f"ffmpeg failed (exit {code}). Check logs.")
            return

        if not out_path.exists():
            await status.edit("ffmpeg finished, but output file was not created.")
            return

        await status.edit("Uploading result...")

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
        )
        await status.edit("Done.")

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
            await event.reply("Use this as a reply to a video. Example: `.vproc 720p`")
            return

        try:
            if job_lock.locked():
                await event.reply("Another job is running. Your request will start after it finishes.")
            async with job_lock:
                await process_reply(event, settings, mode, extra)
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
