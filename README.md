# Telegram Video Processor (Telethon + FFmpeg)

Uses a Telegram **user account session** (via Telethon / MTProto) to avoid Bot API file-size limits.

Workflow:
1. Reply to a video in Telegram with a command like `.vproc 720p`
2. The VPS downloads the video
3. Runs `ffmpeg`
4. Uploads the result back to the same chat

## Requirements

- Python 3.10+
- `ffmpeg` installed on the VPS
- Telegram `api_id` + `api_hash` from https://my.telegram.org

## Setup

```bash
cd /home/pieris/telegram-video-processor
mkdir -p scripts
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

The app now prefers `./vendor/ffmpeg/ffmpeg` automatically if it exists.

Install the latest static FFmpeg (Linux x86_64) into the project:

```bash
cd /home/pieris/telegram-video-processor
bash scripts/install_ffmpeg_local.sh
```

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

- `.vproc mp4` -> H.264 + AAC MP4 (default if mode omitted)
- `.vproc 720p` -> downscale to max width 1280
- `.vproc mute` -> remove audio
- `.vproc copy` -> stream copy (no re-encode, outputs `.mkv`)
- `.vproc custom <ffmpeg args>` -> trusted chats only

Examples:

```text
.vproc
.vproc 720p
.vproc custom -vf "transpose=1" -c:v libx264 -crf 24 -c:a aac
```

## Notes

- `custom` mode is powerful but unsafe if untrusted users can trigger it.
- Use `ALLOWED_CHATS` to restrict which chats are allowed to run commands.
- This is a simple single-process worker; jobs run one at a time.
- Re-encode modes now force square pixels (`SAR=1`) to avoid stretched playback in Telegram.

## Optional: systemd service

Create `/etc/systemd/system/tvp.service`:

```ini
[Unit]
Description=Telegram Video Processor
After=network.target

[Service]
Type=simple
User=pieris
WorkingDirectory=/home/pieris/telegram-video-processor
EnvironmentFile=/home/pieris/telegram-video-processor/.env
ExecStart=/home/pieris/telegram-video-processor/.venv/bin/python /home/pieris/telegram-video-processor/app.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

Then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now tvp
sudo systemctl status tvp
```
