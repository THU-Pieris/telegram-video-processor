import asyncio
import copy
import json
import logging
import os
import re
import shlex
import shutil
import tempfile
import time
import uuid
from urllib.parse import unquote, urlsplit
from dataclasses import asdict, dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Iterable

import aiohttp
from dotenv import load_dotenv
from telethon import TelegramClient, events, functions, types, utils
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
MAX_MEDIA_CAPTION_LEN = 1024
JOB_TMP_PREFIX = "tvp_job_"


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
    queue_file: Path

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
        queue_file = Path(os.getenv("QUEUE_FILE", "./.state/job_queue.json")).expanduser()
        queue_file.parent.mkdir(parents=True, exist_ok=True)

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
            queue_file=queue_file,
        )


@dataclass
class JobRecord:
    job_id: str
    chat_id: int
    command_message_id: int
    reply_message_id: int
    mode: str
    extra: str
    source_url: str | None
    queued_at: float
    status_message_id: int | None = None
    # Set when the replied message is part of an album that contains videos:
    # every video in the album is processed and the album is re-sent as a group.
    grouped_id: int | None = None


class JobQueueStore:
    def __init__(self, path: Path):
        self.path = path
        self.pending: list[JobRecord] = []
        self.in_progress: JobRecord | None = None
        self._lock = asyncio.Lock()

    async def load(self) -> None:
        async with self._lock:
            if not self.path.exists():
                return
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                log.exception("Failed to read queue file, starting with empty queue")
                return

            self.pending = [JobRecord(**item) for item in (data.get("pending") or [])]
            raw_in_progress = data.get("in_progress")
            self.in_progress = JobRecord(**raw_in_progress) if raw_in_progress else None

            # If the app crashed/restarted mid-job, retry it first.
            if self.in_progress is not None:
                self.pending.insert(0, self.in_progress)
                self.in_progress = None
                await self._save_locked()

    async def enqueue(self, job: JobRecord) -> int:
        async with self._lock:
            self.pending.append(job)
            await self._save_locked()
            return len(self.pending)

    async def next_job(self) -> JobRecord | None:
        async with self._lock:
            if not self.pending:
                return None
            job = self.pending.pop(0)
            self.in_progress = job
            await self._save_locked()
            return job

    async def finish_current(self) -> None:
        async with self._lock:
            self.in_progress = None
            await self._save_locked()

    async def finish_job(self, job_id: str) -> None:
        async with self._lock:
            if self.in_progress and self.in_progress.job_id == job_id:
                self.in_progress = None
                await self._save_locked()

    async def total_jobs(self) -> int:
        async with self._lock:
            return len(self.pending) + (1 if self.in_progress else 0)

    async def has_pending(self) -> bool:
        async with self._lock:
            return bool(self.pending)

    async def snapshot(self) -> tuple[JobRecord | None, list[JobRecord]]:
        async with self._lock:
            return self.in_progress, list(self.pending)

    async def remove_job(self, job_id: str) -> JobRecord | None:
        async with self._lock:
            if self.in_progress and self.in_progress.job_id == job_id:
                removed = self.in_progress
                self.in_progress = None
                await self._save_locked()
                return removed

            for idx, job in enumerate(self.pending):
                if job.job_id == job_id:
                    removed = self.pending.pop(idx)
                    await self._save_locked()
                    return removed
        return None

    async def clear_all_jobs(self) -> list[JobRecord]:
        async with self._lock:
            removed: list[JobRecord] = []
            if self.in_progress:
                removed.append(self.in_progress)
            removed.extend(self.pending)
            self.in_progress = None
            self.pending = []
            await self._save_locked()
            return removed

    async def _save_locked(self) -> None:
        data = {
            "pending": [asdict(j) for j in self.pending],
            "in_progress": asdict(self.in_progress) if self.in_progress else None,
        }
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=True, indent=2), encoding="utf-8")
        tmp.replace(self.path)


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


def message_text(msg) -> str:
    # Only trailing whitespace is stripped: leading characters would shift the
    # offsets of the message's formatting entities.
    return (getattr(msg, "message", None) or "").rstrip()


def utf16_len(text: str) -> int:
    # Telegram measures text limits in UTF-16 code units, not Python characters
    # (e.g. most emoji count as 2).
    return len(text.encode("utf-16-le")) // 2


def truncate_utf16(text: str, max_units: int) -> str:
    if utf16_len(text) <= max_units:
        return text
    # Cut on a code-unit boundary; errors="ignore" drops a dangling half of a
    # surrogate pair so we never emit a broken character.
    return text.encode("utf-16-le")[: max_units * 2].decode("utf-16-le", errors="ignore")


def clip_entities(entities, limit: int) -> list:
    # Drop/shorten formatting entities that fall past `limit` UTF-16 units.
    out = []
    for e in entities or []:
        if e.offset >= limit:
            continue
        if e.offset + e.length > limit:
            e = copy.copy(e)
            e.length = limit - e.offset
        out.append(e)
    return out


def sendable_entities(entities) -> list:
    # Received messages describe user mentions as MessageEntityMentionName, which
    # Telegram won't accept when *sending*. A tg://user link renders the same.
    out = []
    for e in entities or []:
        if isinstance(e, types.MessageEntityMentionName):
            e = types.MessageEntityTextUrl(offset=e.offset, length=e.length, url=f"tg://user?id={e.user_id}")
        out.append(e)
    return out


def compose_caption(source_text: str, source_entities, footer_text: str, footer_entities=None) -> tuple[str, list]:
    """Source caption (with formatting) + footer, within Telegram's caption limit.

    The footer always fits; the source text is truncated with "…" if needed.
    """
    sep = "\n\n" if source_text else ""
    footer_units = utf16_len(sep + footer_text)
    entities = sendable_entities(source_entities)
    if utf16_len(source_text) + footer_units > MAX_MEDIA_CAPTION_LEN:
        keep_units = max(0, MAX_MEDIA_CAPTION_LEN - footer_units - 1)
        source_text = truncate_utf16(source_text, keep_units).rstrip()
        entities = clip_entities(entities, utf16_len(source_text))
        source_text += "…"
    shift = utf16_len(source_text + sep)
    for e in footer_entities or []:
        e = copy.copy(e)
        e.offset += shift
        entities.append(e)
    return source_text + sep + footer_text, entities


def chat_username(chat) -> str | None:
    if chat is None:
        return None
    if getattr(chat, "username", None):
        return chat.username
    # Channels/users with collectible usernames list them here instead.
    return next((u.username for u in (getattr(chat, "usernames", None) or []) if getattr(u, "active", False)), None)


async def forward_origin(client: TelegramClient, msg) -> tuple[str, list]:
    """'Forwarded from <link>' text + entities for a forwarded message, else ('', [])."""
    fwd = getattr(msg, "fwd_from", None)
    if not fwd:
        return "", []
    forward = getattr(msg, "forward", None)
    peer = fwd.from_id
    prefix = "Forwarded from "
    label, entity_for = None, None

    async def resolve(cached):
        if cached is not None:
            return cached
        try:
            return await client.get_entity(peer)
        except Exception:
            log.info("Could not resolve forward origin %s", peer)
            return None

    if isinstance(peer, (types.PeerChannel, types.PeerChat)):
        chat = await resolve(getattr(forward, "chat", None))
        username = chat_username(chat)
        post = fwd.channel_post
        label = getattr(chat, "title", None) or fwd.post_author or "original chat"
        url = None
        if username and post:
            url = f"https://t.me/{username}/{post}"
        elif isinstance(peer, types.PeerChannel) and post:
            # Private channel: link works for members.
            url = f"https://t.me/c/{peer.channel_id}/{post}"
        elif username:
            url = f"https://t.me/{username}"
        if url:
            entity_for = lambda off, n: types.MessageEntityTextUrl(offset=off, length=n, url=url)
    elif isinstance(peer, types.PeerUser):
        user = await resolve(getattr(forward, "sender", None))
        username = chat_username(user)
        if username:
            label = f"@{username}"
            entity_for = lambda off, n: types.MessageEntityMention(offset=off, length=n)
        else:
            label = " ".join(x for x in (getattr(user, "first_name", None), getattr(user, "last_name", None)) if x) or "user"
            entity_for = lambda off, n: types.MessageEntityTextUrl(
                offset=off, length=n, url=f"tg://user?id={peer.user_id}"
            )
    elif fwd.from_name:
        # Sender hides their account when forwarded: name only, nothing to link to.
        label = fwd.from_name

    if not label:
        return "", []
    entities = [entity_for(utf16_len(prefix), utf16_len(label))] if entity_for else []
    return prefix + label, entities


async def build_output_caption(client: TelegramClient, mode: str, source_message) -> tuple[str, list]:
    footer = f"Processed with {mode}"
    origin, origin_entities = await forward_origin(client, source_message)
    footer_entities = []
    if origin:
        footer_entities = [copy.copy(e) for e in origin_entities]
        for e in footer_entities:
            e.offset += utf16_len(footer + "\n")
        footer = f"{footer}\n{origin}"
    return compose_caption(
        message_text(source_message), getattr(source_message, "entities", None), footer, footer_entities
    )


async def kill_process(proc: asyncio.subprocess.Process) -> None:
    # Make sure a child process never outlives the task that started it
    # (e.g. when a job is cancelled via `.vp clear`).
    if proc.returncode is not None:
        return
    try:
        proc.kill()
    except ProcessLookupError:
        return
    await proc.wait()


async def run_subprocess(cmd: Iterable[str]) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await proc.communicate()
    finally:
        await kill_process(proc)
    text = (out or b"").decode("utf-8", errors="replace")
    return proc.returncode, text


class StatusUpdater:
    def __init__(self, message):
        self.message = message
        self.prefix = ""  # e.g. "[video 2/5] " while working through an album
        self._last_text = None
        self._last_edit = 0.0
        self._lock = asyncio.Lock()

    async def set(self, text: str, force: bool = False) -> None:
        text = self.prefix + text
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
    try:
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
    finally:
        await kill_process(proc)
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


def format_job_line(job: JobRecord, state: str) -> str:
    source = "url" if job.source_url else "telegram"
    return (
        f"- `{job.job_id}` [{state}] `{job.mode}` source={source} "
        f"chat={job.chat_id} cmd={job.command_message_id}"
    )


async def delete_job_messages(client: TelegramClient, jobs: Iterable[JobRecord]) -> None:
    by_chat: dict[int, set[int]] = {}
    for job in jobs:
        msg_ids = by_chat.setdefault(job.chat_id, set())
        msg_ids.add(job.command_message_id)
        if job.status_message_id:
            msg_ids.add(job.status_message_id)

    for chat_id, ids in by_chat.items():
        try:
            await client.delete_messages(chat_id, sorted(ids))
        except Exception:
            log.exception("Failed to delete messages for cleared jobs in chat %s", chat_id)


def cleanup_stale_job_dirs(work_dir: Path) -> None:
    # Jobs run in a TemporaryDirectory that is normally removed when the job ends,
    # but a hard kill / power loss mid-job leaves it behind. At startup no job is
    # running yet, so any leftover job dir is stale. Only our own prefix is touched,
    # so a shared WORK_DIR (e.g. /tmp) is safe.
    for path in work_dir.glob(f"{JOB_TMP_PREFIX}*"):
        if not path.is_dir() or path.is_symlink():
            continue
        try:
            shutil.rmtree(path)
            log.info("Removed stale job dir: %s", path)
        except Exception:
            log.exception("Failed to remove stale job dir: %s", path)


def should_delete_source(job: JobRecord, reply) -> bool:
    # Single-message jobs: a standalone source message (video or direct-URL
    # message) is replaced by the processed result. If it belongs to an album,
    # only that one item was processed, so it is kept to avoid breaking the
    # album. (Whole-album jobs always replace the album; see process_album.)
    return not getattr(reply, "grouped_id", None)


async def get_album_messages(client: TelegramClient, chat_id: int, msg) -> list:
    # Albums hold at most 10 items with consecutive-ish ids, so a +/-10 window
    # around any member is enough to collect the whole group.
    grouped_id = getattr(msg, "grouped_id", None)
    if not grouped_id:
        return [msg]
    ids = list(range(max(1, msg.id - 10), msg.id + 11))
    found = await client.get_messages(chat_id, ids=ids)
    album = [m for m in (found or []) if m and getattr(m, "grouped_id", None) == grouped_id]
    return sorted(album, key=lambda m: m.id) or [msg]


def album_has_video(album: list) -> bool:
    return any(getattr(m, "file", None) and is_video_message(m) for m in album)


def video_attributes(meta: dict | None) -> list | None:
    if not meta:
        return None
    return [
        DocumentAttributeVideo(
            duration=meta["duration"],
            w=meta["width"],
            h=meta["height"],
            supports_streaming=True,
        )
    ]


async def download_telegram_video(msg, tmp: Path, status: StatusUpdater) -> Path | None:
    src_name = msg.file.name or f"input_{msg.id}"
    # Album items may share a file name; keep each one in its own subdir.
    item_dir = tmp / f"src_{msg.id}"
    item_dir.mkdir(exist_ok=True)
    await status.set("Downloading from Telegram...", force=True)
    downloaded = await msg.download_media(
        file=str(item_dir / src_name),
        progress_callback=status.progress_callback("Downloading"),
    )
    if not downloaded:
        await status.set("Download failed.", force=True)
        return None
    return Path(downloaded)


async def transcode(
    settings: Settings, job: JobRecord, src_path: Path, out_dir: Path, out_suffix: str, status: StatusUpdater
) -> Path | None:
    """Probe + run ffmpeg. Reports failures on the status message and returns None."""
    out_path = out_dir / f"{src_path.stem}_{job.mode}{out_suffix}"
    in_meta = await probe_video_metadata(settings.ffprobe_bin, src_path)
    if not in_meta:
        await status.set("Input is not a valid video or metadata probe failed.", force=True)
        return None
    await status.set("Processing with ffmpeg...", force=True)

    try:
        cmd = build_ffmpeg_cmd(settings.ffmpeg_bin, job.mode, src_path, out_path, job.extra)
    except Exception as exc:
        await status.set(f"Invalid command: {exc}", force=True)
        return None

    log.info("Running ffmpeg: %s", " ".join(shlex.quote(x) for x in cmd))
    code, ffout = await run_ffmpeg_with_progress(
        cmd,
        status,
        input_duration_s=in_meta.get("duration"),
    )
    if code != 0:
        log.error("ffmpeg failed (%s): %s", code, ffout[-4000:])
        await status.set(f"ffmpeg failed (exit {code}). Check logs.", force=True)
        return None

    if not out_path.exists():
        await status.set("ffmpeg finished, but output file was not created.", force=True)
        return None
    return out_path


async def delete_status_and_trigger(client: TelegramClient, job: JobRecord, status_message) -> None:
    try:
        # Delete both the temporary status message and the user trigger message.
        await client.delete_messages(job.chat_id, [status_message.id, job.command_message_id])
    except Exception:
        log.exception("Failed to delete status/trigger message(s)")


async def delete_source_messages(client: TelegramClient, job: JobRecord, ids: list[int]) -> None:
    # Separate call from the status/trigger cleanup so a permission error here
    # can't block that cleanup.
    try:
        affected = await client.delete_messages(job.chat_id, ids)
        if not any(getattr(a, "pts_count", 0) for a in (affected or [])):
            log.warning("Source message(s) %s in chat %s were not deleted (no permission?)", ids, job.chat_id)
        else:
            log.info("Deleted source message(s) %s in chat %s", ids, job.chat_id)
    except Exception:
        log.exception("Failed to delete source message(s) %s in chat %s", ids, job.chat_id)


async def save_job_files(client: TelegramClient, settings: Settings, job: JobRecord, paths: list[Path]) -> None:
    # CLEANUP=false: keep inputs/outputs instead of letting the temp dir vanish.
    saved_dir = settings.work_dir / f"job_{job.reply_message_id}"
    saved_dir.mkdir(parents=True, exist_ok=True)
    for path in paths:
        if path.exists():
            path.replace(saved_dir / path.name)
    await client.send_message(
        job.chat_id,
        f"Saved files to `{saved_dir}`",
        reply_to=job.command_message_id,
    )


async def process_job(client: TelegramClient, settings: Settings, job: JobRecord) -> None:
    status_message = None
    if job.status_message_id:
        status_message = await client.get_messages(job.chat_id, ids=job.status_message_id)
    if not status_message:
        status_message = await client.send_message(
            job.chat_id,
            f"Queued: `{job.mode}`",
            reply_to=job.command_message_id,
        )
    status = StatusUpdater(status_message)

    reply = await client.get_messages(job.chat_id, ids=job.reply_message_id)
    if not reply:
        await status.set("Source message no longer exists.", force=True)
        return

    with tempfile.TemporaryDirectory(dir=settings.work_dir, prefix=JOB_TMP_PREFIX) as tmp_dir:
        tmp = Path(tmp_dir)
        if job.grouped_id:
            await process_album(client, settings, job, reply, tmp, status, status_message)
        else:
            await process_single(client, settings, job, reply, tmp, status, status_message)


async def process_single(
    client: TelegramClient, settings: Settings, job: JobRecord, reply, tmp: Path, status: StatusUpdater, status_message
) -> None:
    src_path: Path
    if job.source_url:
        src_name = filename_from_url(job.source_url, fallback=f"input_{reply.id}.mp4")
        requested_src_path = tmp / src_name
        await status.set("Downloading from direct URL...", force=True)
        try:
            src_path = await download_from_direct_url(job.source_url, requested_src_path, status)
        except Exception as exc:
            await status.set(f"Direct URL download failed: {exc}", force=True)
            return
    else:
        if not getattr(reply, "file", None) or not is_video_message(reply):
            await status.set("Replied message is no longer a valid video.", force=True)
            return
        downloaded = await download_telegram_video(reply, tmp, status)
        if not downloaded:
            return
        src_path = downloaded

    out_suffix = ".mkv" if job.mode == "copy" else ".mp4"
    out_path = await transcode(settings, job, src_path, tmp, out_suffix, status)
    if not out_path:
        return

    await status.set("Uploading result...", force=True)

    caption, caption_entities = await build_output_caption(client, job.mode, reply)
    attributes = None
    if out_path.suffix.lower() == ".mp4":
        meta = await probe_video_metadata(settings.ffprobe_bin, out_path)
        if meta:
            log.info("Uploading with explicit video attrs: %sx%s, %ss", meta["width"], meta["height"], meta["duration"])
        attributes = video_attributes(meta)

    delete_source = should_delete_source(job, reply)
    # When the original is going away, don't reply to it (that would leave a
    # dangling "deleted message" reference). Reply to whatever the original was
    # replying to instead, which also keeps the result in the same forum topic.
    output_reply_to = reply.reply_to_msg_id if delete_source else job.reply_message_id

    await client.send_file(
        job.chat_id,
        str(out_path),
        caption=caption,
        formatting_entities=caption_entities,
        parse_mode=None,
        reply_to=output_reply_to,
        supports_streaming=out_path.suffix.lower() == ".mp4",
        force_document=False,
        attributes=attributes,
        progress_callback=status.progress_callback("Uploading"),
    )
    await status.set("Done.", force=True)
    await delete_status_and_trigger(client, job, status_message)

    if delete_source:
        await delete_source_messages(client, job, [reply.id])

    if not settings.cleanup:
        await save_job_files(client, settings, job, [src_path, out_path])


async def album_captions(client: TelegramClient, mode: str, album: list) -> list[tuple[str, list]]:
    """Original per-item captions (with formatting); the footer goes on one item.

    The footer ("Processed with ..." and, for forwarded albums, a link back to the
    original) goes on the first item that already has text (Telegram shows a lone
    caption as the album caption), or on the first item if none do.
    """
    captions = [(message_text(m), sendable_entities(getattr(m, "entities", None))) for m in album]
    target = next((i for i, (text, _) in enumerate(captions) if text), 0)
    forwarded = next((m for m in album if getattr(m, "fwd_from", None)), None)
    footer_msg = album[target]
    if forwarded is not None and not getattr(footer_msg, "fwd_from", None):
        footer_msg = forwarded
    # Build the footer from the forwarded item, but keep the target item's own text.
    text, entities = captions[target]
    stub = SimpleNamespace(message=text, entities=entities, fwd_from=getattr(footer_msg, "fwd_from", None),
                           forward=getattr(footer_msg, "forward", None))
    captions[target] = await build_output_caption(client, mode, stub)
    return captions


async def process_album(
    client: TelegramClient, settings: Settings, job: JobRecord, reply, tmp: Path, status: StatusUpdater, status_message
) -> None:
    album = await get_album_messages(client, job.chat_id, reply)
    if not album_has_video(album):
        await status.set("Album no longer contains any videos.", force=True)
        return

    peer = await client.get_input_entity(job.chat_id)
    video_count = sum(1 for m in album if getattr(m, "file", None) and is_video_message(m))
    kept_files: list[Path] = []
    media: list = []
    done_videos = 0

    for item in album:
        spoiler = bool(getattr(getattr(item, "media", None), "spoiler", False))
        if not (getattr(item, "file", None) and is_video_message(item)):
            # Photos (and anything else) are re-sent as-is by reference: no
            # download or re-upload, identical to the original.
            media.append(utils.get_input_media(item.media))
            continue

        done_videos += 1
        status.prefix = f"[video {done_videos}/{video_count}] "
        src_path = await download_telegram_video(item, tmp, status)
        if not src_path:
            return
        # Albums can't mix in a .mkv document, so `copy` stays in an mp4 container here.
        out_path = await transcode(settings, job, src_path, src_path.parent, ".mp4", status)
        if not out_path:
            return

        meta = await probe_video_metadata(settings.ffprobe_bin, out_path)
        await status.set("Uploading...", force=True)
        uploaded = await client.upload_file(str(out_path), progress_callback=status.progress_callback("Uploading"))
        attributes = (video_attributes(meta) or []) + [types.DocumentAttributeFilename(out_path.name)]
        result = await client(
            functions.messages.UploadMediaRequest(
                peer=peer,
                media=types.InputMediaUploadedDocument(
                    file=uploaded,
                    mime_type="video/mp4",
                    attributes=attributes,
                    # Marks it as a real video so a silent one isn't turned into a GIF
                    # (Telethon's own album sender always sets this too).
                    nosound_video=True,
                    spoiler=spoiler or None,
                ),
            )
        )
        media.append(types.InputMediaDocument(id=utils.get_input_document(result.document), spoiler=spoiler or None))

        if settings.cleanup:
            # Already uploaded; free disk space before the next video.
            src_path.unlink(missing_ok=True)
            out_path.unlink(missing_ok=True)
        else:
            kept_files += [src_path, out_path]

    status.prefix = ""
    await status.set("Sending album...", force=True)
    multi_media = [
        types.InputSingleMedia(media=m, message=text, entities=entities)
        for m, (text, entities) in zip(media, await album_captions(client, job.mode, album))
    ]
    # The whole album was processed, so the original album is replaced: post the
    # new one where the original was replying to (keeps threads / forum topics).
    output_reply_to = next((m.reply_to_msg_id for m in album if getattr(m, "reply_to_msg_id", None)), None)
    await client(
        functions.messages.SendMultiMediaRequest(
            peer=peer,
            multi_media=multi_media,
            reply_to=types.InputReplyToMessage(reply_to_msg_id=output_reply_to) if output_reply_to else None,
        )
    )
    await status.set("Done.", force=True)
    await delete_status_and_trigger(client, job, status_message)
    await delete_source_messages(client, job, [m.id for m in album])

    if kept_files:
        await save_job_files(client, settings, job, kept_files)


async def main() -> None:
    settings = Settings.from_env()
    cleanup_stale_job_dirs(settings.work_dir)
    client = TelegramClient(settings.session_name, settings.api_id, settings.api_hash)
    queue_store = JobQueueStore(settings.queue_file)
    await queue_store.load()
    queue_event = asyncio.Event()
    cancelled_job_ids: set[str] = set()
    active_job_id: str | None = None
    active_job_task: asyncio.Task | None = None

    if await queue_store.has_pending():
        queue_event.set()

    async def worker_loop() -> None:
        nonlocal active_job_id, active_job_task
        while True:
            await queue_event.wait()
            while True:
                job = await queue_store.next_job()
                if job is None:
                    queue_event.clear()
                    break
                active_job_id = job.job_id
                active_job_task = asyncio.create_task(process_job(client, settings, job))
                try:
                    await active_job_task
                except asyncio.CancelledError:
                    if job.job_id in cancelled_job_ids:
                        cancelled_job_ids.discard(job.job_id)
                    else:
                        # Keep `in_progress` on disk so an interrupted job can resume after restart.
                        raise
                except Exception:
                    log.exception("Unhandled error while processing job %s", job.job_id)
                    try:
                        await client.send_message(
                            job.chat_id,
                            "Internal error while processing queued job. Check logs.",
                            reply_to=job.command_message_id,
                        )
                    except Exception:
                        log.exception("Failed to send internal-error message for job %s", job.job_id)
                finally:
                    active_job_id = None
                    active_job_task = None
                await queue_store.finish_job(job.job_id)

    @client.on(events.NewMessage)
    async def handler(event):
        if not chat_allowed(event, settings):
            return

        mode, extra = parse_command(event.raw_text, settings.command_prefix)
        if not mode:
            return
        if event.chat_id is None:
            await event.reply("Unable to determine chat id for this message.")
            return

        if mode in {"jobs", "list"}:
            in_progress, pending = await queue_store.snapshot()
            lines = ["Current queue state:"]
            if in_progress:
                lines.append(format_job_line(in_progress, "in_progress"))
            else:
                lines.append("- no in-progress job")
            if pending:
                for job in pending:
                    lines.append(format_job_line(job, "pending"))
            else:
                lines.append("- no pending jobs")
            await event.reply("\n".join(lines))
            return

        if mode == "clear":
            target = (extra or "").strip()
            if target in {"all", "*"}:
                removed = await queue_store.clear_all_jobs()
                if active_job_id and active_job_task and not active_job_task.done():
                    cancelled_job_ids.add(active_job_id)
                    active_job_task.cancel()
                await delete_job_messages(client, removed)
                queue_event.clear()
                await event.reply(f"Cleared {len(removed)} job(s).")
                return

            if not target:
                await event.reply("Usage: `.vp clear all` or `.vp clear <job_id>`")
                return

            removed = await queue_store.remove_job(target)
            if not removed:
                await event.reply(f"Job not found: `{target}`")
                return
            if active_job_id == removed.job_id and active_job_task and not active_job_task.done():
                cancelled_job_ids.add(removed.job_id)
                active_job_task.cancel()
            await delete_job_messages(client, [removed])
            if not await queue_store.has_pending():
                queue_event.clear()
            await event.reply(f"Cleared job `{removed.job_id}`.")
            return

        if not event.is_reply:
            await event.reply("Use this as a reply to a video or direct URL message. Example: `.vp 720p`")
            return

        try:
            reply = await event.get_reply_message()
            if not reply:
                await event.reply("Reply to a video/file or a message containing a direct video URL.")
                return

            source_url = None
            grouped_id = None
            if getattr(reply, "grouped_id", None):
                # Replying to any item of an album (photo or video) processes the
                # whole album, as long as it contains at least one video.
                album = await get_album_messages(client, int(event.chat_id), reply)
                if album_has_video(album):
                    grouped_id = reply.grouped_id
            if not grouped_id and not (getattr(reply, "file", None) and is_video_message(reply)):
                source_url = extract_first_http_url(getattr(reply, "raw_text", None) or getattr(reply, "message", None))
                if not source_url:
                    await event.reply("Replied message must be a video/file or contain a direct `http(s)` video URL.")
                    return

            total_before = await queue_store.total_jobs()
            queue_text = "Queued. Waiting for current job to finish..." if total_before > 0 else "Queued. Starting soon..."
            queued_msg = await event.reply(queue_text)
            job = JobRecord(
                job_id=str(uuid.uuid4()),
                chat_id=int(event.chat_id),
                command_message_id=event.message.id,
                reply_message_id=reply.id,
                mode=mode,
                extra=extra,
                source_url=source_url,
                queued_at=time.time(),
                status_message_id=queued_msg.id,
                grouped_id=grouped_id,
            )
            await queue_store.enqueue(job)
            queue_event.set()
        except Exception:
            log.exception("Unhandled error while enqueuing message")
            await event.reply("Internal error. Check logs.")

    log.info("Starting client. Command prefix: %s", settings.command_prefix)
    log.info("Allowed chats: %s", sorted(settings.allowed_chats) if settings.allowed_chats else "ALL")
    log.info("Queue file: %s", settings.queue_file)
    await client.start()
    me = await client.get_me()
    log.info("Logged in as %s (%s)", getattr(me, "username", None) or me.first_name, me.id)
    asyncio.create_task(worker_loop())
    await client.run_until_disconnected()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
