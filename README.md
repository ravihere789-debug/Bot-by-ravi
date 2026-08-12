# Telegram Campaign Bot

## Requirements
- Python 3.10 or higher
- pip

---

## Setup

### 1. Install dependencies
```bash
pip install -r requirements.txt
```

### 2. Set environment variables

**Option A — .env file (recommended)**

Copy `.env.example` to `.env` and fill in your values:
```bash
cp .env.example .env
```

Then edit `.env`:
```
TELEGRAM_BOT_TOKEN=your_bot_token_here
PYROGRAM_API_ID=your_api_id_here
PYROGRAM_API_HASH=your_api_hash_here
```

The bot loads `.env` automatically on startup.

**Option B — export directly in terminal**
```bash
export TELEGRAM_BOT_TOKEN="your_bot_token_here"
export PYROGRAM_API_ID="your_api_id_here"
export PYROGRAM_API_HASH="your_api_hash_here"
```

---

### 3. Run the bot
```bash
python bot.py
```

---

## Where to get credentials

| Variable | Where to get it |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Message [@BotFather](https://t.me/BotFather) → `/newbot` |
| `PYROGRAM_API_ID` | [https://my.telegram.org/apps](https://my.telegram.org/apps) |
| `PYROGRAM_API_HASH` | Same page as API ID |

---

## Running 24/7

**Linux (systemd)**
```bash
# /etc/systemd/system/tgbot.service
[Unit]
Description=Telegram Campaign Bot
After=network.target

[Service]
WorkingDirectory=/path/to/bot
EnvironmentFile=/path/to/bot/.env
ExecStart=/usr/bin/python3 bot.py
Restart=always

[Install]
WantedBy=multi-user.target
```
```bash
sudo systemctl enable tgbot && sudo systemctl start tgbot
```

**Screen (simple)**
```bash
screen -S tgbot python bot.py
# Detach: Ctrl+A then D
# Reattach: screen -r tgbot
```

**Docker**
```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY . .
RUN pip install -r requirements.txt
CMD ["python", "bot.py"]
```
```bash
docker build -t tgbot .
docker run -d --env-file .env --name tgbot tgbot
```
