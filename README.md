# Telegram Video Processor (Telethon + FFmpeg)

Uses a Telegram **user account session** (via Telethon / MTProto) to avoid Bot API file-size limits.

Workflow:
1. Reply to a video in Telegram with a command like `.vp 720p`
2. The VPS downloads the video
3. Runs `ffmpeg`
4. Uploads the result back to the same chat

## Requirements

- Python 3.10+
- `ffmpeg` installed on the VPS
- Telegram `api_id` + `api_hash` from https://my.telegram.org

## Setup

```bash
cd ~/telegram-video-processor
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env`:
- `API_ID`
- `API_HASH`
- optionally `ALLOWED_CHATS` (recommended for safety)

## Use a local (project) FFmpeg binary

The app prefers `./vendor/ffmpeg/ffmpeg` automatically if it exists.

If you want to override the binary path explicitly, set `FFMPEG_BIN=/path/to/ffmpeg` in `.env`.

## First Run (Login)

```bash
python app.py
```

Telethon will prompt for:
- phone number
- login code
- password (if 2FA enabled)

It stores a local session file (default `.state/telethon/tvp_user.session`).

## Usage in Telegram

Reply to a video message with:

- `.vp 480p` -> small-size 480p preset (lower quality)
- `.vp 720p` -> small-size 720p preset (default if mode omitted)
- `.vp 1080p` -> small-size 1080p preset (lower quality)
- `.vp mp4` -> generic small-size H.264 + AAC MP4
- `.vp mute` -> remove audio (re-encode)
- `.vp copy` -> stream copy (no re-encode, outputs `.mkv`)
- `.vp custom <ffmpeg args>` -> trusted chats only

Examples:

```text
.vp
.vp 480p
.vp 720p
.vp 1080p
.vp custom -vf "transpose=1" -c:v libx264 -crf 24 -c:a aac
```

## Notes

- `custom` mode is powerful but unsafe if untrusted users can trigger it.
- Use `ALLOWED_CHATS` to restrict which chats are allowed to run commands.
- This is a simple single-process worker; jobs run one at a time.
- Re-encode presets prioritize smaller file size over visual quality.
- The worker updates a temporary progress message (download/process/upload) and deletes it after a successful run.
