# 🤖 Multi-Feature Discord Bot

A feature-rich Discord bot built with `discord.py` featuring YouTube music playback (`yt-dlp` + `FFmpeg`), dynamic "Join-to-Create" voice channels, interactive polls, random team generator, trivia quizzes, and an automated Twitch Drops campaign notifier.

---

## ✨ Features

### 🎵 Music Player
- `/play <query>` (or `!play`) — Play or queue a song from YouTube via search query or URL. Automatically refreshes expired stream URLs in long queues.
- `/pause` & `/resume` — Pause and resume current playback.
- `/skip` (or `!skip`) — Skip the current track.
- `/stop` (or `!stop`) — Stop playback and clear the queue.
- `/loop` — Toggle single-track looping.
- `/volume <1-200>` — Adjust playback volume in real time (default `20%`).
- `/queue` — View the current song, loop status, volume, and upcoming tracks with durations.
- `/nowplaying` — Display an embed with current track link, thumbnail, duration, and requester.
- `/shuffle` — Shuffle the current queue.
- `/remove <number>` — Remove a specific track from the queue.
- `/leave` (or `!leave`) — Disconnect the bot and clean up state.
- **Smart Auto-Leave** — Automatically leaves after 60 seconds of inactivity or when all human members leave the voice channel.

### 🔊 Dynamic Voice Channels ("Join to Create")
- Joining the configured `LOBBY_CHANNEL_ID` automatically creates a personal voice channel (`🔊 Username`) in the same category and moves the member in.
- Automatically deletes the temporary channel once all human members leave.

### 🎉 Community & Mini-Games
- `/poll <question> <options> [duration]` — Create a reaction poll (2–10 comma-separated options) with custom duration and visual progress bar results.
- `/teams [timeout]` — Open a reaction sign-up and split participants into two balanced random teams.
- `/quiz` — Run a 5-question multiple-choice trivia game via The Trivia API with anti-cheat single-choice validation and a final leaderboard.

### 🎁 Anti-Spam Custom Twitch Drops Feeds & Web UI
- **Built-in Web Control Panel (`http://<server-ip>:2457`)**:
  - **Custom Drops Feeds**: Route specific games you care about to your **Private DM**, a **Specific Channel**, or click **Create Private Channel** to automatically create a locked channel visible only to you and the bot.
  - **Strict Anti-Spam Engine**:
    - Persists campaign state & timestamps in `bot_data.json` so Pterodactyl restarts **never** re-send duplicate alerts.
    - Configurable **Cooldown Lock** (default `48h` between alerts for the same game) and **Hysteresis** (`6` consecutive empty checks before resetting).
    - **Single Message Mode**: Option to edit one persistent embed in-place instead of posting new messages.
  - **Channel & Category Manager**: Create Text, Voice, or Category channels in the exact Category you choose (Public or Private), move channels between categories, or configure multiple **Join-to-Create** voice lobbies with custom target categories.
- `/my_drops_feed` — Subscribe to your chosen games via **DM** or auto-create a **Private Channel** in your chosen category directly from Discord.
- `/create_channel` — Create a Text or Voice channel inside any chosen Category (with optional Private visibility).
- `/panel` — View Web UI port and active anti-spam settings.
- `/drops list|add|remove [game_name]` — View or manage monitored Twitch games.
- [`custom_drops_feed.py`](./custom_drops_feed.py) — Standalone custom drops feed script that can also be run independently via cron/CLI.

---

## 🚀 Setup & Installation

### 1. Prerequisites
- **Python 3.10+**
- **FFmpeg** installed and available in `PATH` (or configured via `FFMPEG_PATH` in `.env`)
- **Opus** library (bundled on Windows with `discord.py[voice]`; `libopus0` on Linux/Docker)

### 2. Install Dependencies
```bash
pip install -r requirements.txt
```

### 3. Configure Environment Variables
Copy `.env.example` to `.env` and fill in your credentials:
```bash
cp .env.example .env
```

### 4. Run the Bot
```bash
python discord_bot.py
```
To sync slash commands globally in Discord, a Server Administrator can run `!sync`.

---

## 🦖 Pterodactyl Auto-Update Setup

Depending on how your Pterodactyl panel/egg is configured, choose any of the 3 methods below:

### Method 1: Custom Startup Command (in Pterodactyl **Startup** Tab)
If you can edit the **Startup Command** field, paste this one-liner. On every restart, it pulls the latest code from GitHub (leaving `.env` untouched), updates `requirements.txt` (keeping `yt-dlp` up to date), and starts the bot:

```bash
/bin/bash -c 'export HOME=/tmp; git config --global --add safe.directory /home/container; if [ -d .git ]; then git fetch origin main && git reset --hard origin/main; else git init && git remote add origin https://github.com/TanguyCze/Discord-Bot.git && git fetch origin main && git checkout -f -B main origin/main; fi && pip install -U --no-cache-dir -r requirements.txt && python3 discord_bot.py'
```

### Method 2: If "Startup Command" is Locked (`App py file` only)
If your host locks the Startup Command and only lets you set the **App py file** (main Python file):
1. Upload [`updater.py`](./updater.py) once to `/home/container`.
2. In Pterodactyl **Startup** tab, set **App py file** to:
   ```text
   updater.py
   ```
   Every time you click **Start** or **Restart**, `updater.py` will automatically pull the latest `discord_bot.py` and `requirements.txt` from GitHub, install/upgrade packages, and launch `discord_bot.py`.

### Method 3: Built-in Pterodactyl Python Egg Git Variables
If your Pterodactyl Python Egg already has Git fields in the **Startup** tab:
- **Git Repo Address**: `https://github.com/TanguyCze/Discord-Bot.git`
- **Install Branch**: `main`
- **Auto Update**: `1` (Enabled)
- **App py file**: `discord_bot.py`
- **Requirements file**: `requirements.txt`


