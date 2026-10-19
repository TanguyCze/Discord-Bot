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

### 🎁 Twitch Drops Monitor
- Checks Twitch Helix API every 10 minutes for active `DropsEnabled` streams across monitored games.
- Sends an alert embed to `TWITCH_NOTIFY_CHANNEL_ID` when a new Drops campaign starts.
- `/drops list|add|remove [game_name]` — View or manage the list of monitored Twitch games at runtime.

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
