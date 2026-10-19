import os
import time
import random
import asyncio
import logging
import tempfile
import html
from urllib.parse import quote
from typing import List, Dict, Set, Optional, Union

import discord
import aiohttp
from discord import app_commands
from discord.ext import tasks, commands
from yt_dlp import YoutubeDL
from dotenv import load_dotenv
from discord.opus import load_opus, is_loaded

###############################################################################
# ───────────────────────────────── LOGGING ────────────────────────────────── #
###############################################################################

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("DiscordBot")

###############################################################################
# ──────────────────────────────── CONSTANTS ───────────────────────────────── #
###############################################################################

JOIN_EMOJI       = "✋"
TEAM_A_EMOJI     = "🅰️"
TEAM_B_EMOJI     = "🅱️"
NUM_EMOJIS       = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
LETTER_EMOJIS    = ["🇦", "🇧", "🇨", "🇩", "🇪", "🇫"]
QUIZ_QTY         = 5
QUIZ_TIMEOUT     = 20
TEAM_TIMEOUT     = 30
POLL_DURATION    = 30
DEFAULT_VOLUME   = 0.2
AUTO_LEAVE_DELAY = 60
STREAM_URL_TTL   = 1800  # Re-extract YouTube stream URLs older than 30 minutes

###############################################################################
# ────────────────────────────── INITIALISATION ────────────────────────────── #
###############################################################################

# 0. Read-Only Environment Fixes for Docker / Linux Containers
TMP_DIR = "/tmp" if os.name != "nt" else tempfile.gettempdir()
if os.name != "nt":
    current_home = os.environ.get("HOME", "")
    if not current_home or not os.access(current_home, os.W_OK):
        os.environ["HOME"] = TMP_DIR
    os.environ.setdefault("XDG_CACHE_HOME", os.path.join(TMP_DIR, ".cache"))

# 1. Load Opus Library (Required for Voice)
if not is_loaded():
    try:
        discord.opus._load_default()
    except Exception:
        pass

if not is_loaded():
    opus_candidates = [
        "libopus.so.0",
        "libopus.so",
        "/usr/lib/x86_64-linux-gnu/libopus.so.0",
        "/usr/lib/aarch64-linux-gnu/libopus.so.0",
        "libopus.0.dylib",
        "/opt/homebrew/lib/libopus.dylib",
        "opus",
        "libopus-0.dll",
    ]
    for opus_lib in opus_candidates:
        try:
            load_opus(opus_lib)
            logger.info("✅ Successfully loaded Opus from: %s", opus_lib)
            break
        except Exception:
            continue
    else:
        logger.warning("⚠️ Could not pre-load Opus library. Voice may not work unless loaded by system.")


def env_int(key: str, default: int = 0) -> int:
    """Safely parse an integer environment variable without crashing on empty strings."""
    raw = os.getenv(key)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        logger.warning("⚠️ Invalid integer for %s=%r; defaulting to %d", key, raw, default)
        return default


# 2. Load Environment Variables
load_dotenv()
TOKEN                    = os.getenv("DISCORD_TOKEN")
LOBBY_CHANNEL_ID         = env_int("LOBBY_CHANNEL_ID", 0)
TEST_GUILD_ID            = env_int("TEST_GUILD_ID", 0)

# --- TWITCH CONFIGURATION ---
TWITCH_CLIENT_ID         = os.getenv("TWITCH_CLIENT_ID")
TWITCH_CLIENT_SECRET     = os.getenv("TWITCH_CLIENT_SECRET")
TWITCH_NOTIFY_CHANNEL_ID = env_int("TWITCH_NOTIFY_CHANNEL_ID", 0)

if not TOKEN:
    raise RuntimeError("❌ DISCORD_TOKEN is missing from .env file!")

# 3. FFmpeg Configuration
FFMPEG_EXE = os.getenv("FFMPEG_PATH") or "ffmpeg"

# 4. Bot Setup
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states    = True
intents.members         = True

bot = commands.Bot(command_prefix="!", intents=intents)

# 5. yt-dlp Options
YDL_OPTS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "default_search": "ytsearch",
    "source_address": "0.0.0.0",
    "cachedir": os.path.join(TMP_DIR, "yt-dlp-cache"),
    "extractor_args": {"youtube": ["player_client=android,web"]},  # Bypasses YouTube 403 blocks
}

# Add cookie file if provided in env
if cookie_path := os.getenv("YTDL_COOKIE"):
    if os.path.isfile(cookie_path):
        YDL_OPTS["cookiefile"] = cookie_path

# Runtime State
bot.song_queue: Dict[int, List[dict]]          = {}
bot.current_song: Dict[int, dict]              = {}
bot.looping: Dict[int, bool]                   = {}
bot.volumes: Dict[int, float]                  = {}
bot.dynamic_channels: Set[int]                 = set()
bot.auto_leave_tasks: Dict[int, asyncio.Task]  = {}
bot.music_locks: Dict[int, asyncio.Lock]       = {}
bot._synced: bool                              = False

# Twitch Runtime State
bot.twitch_games: List[str]                    = ["Rust", "League of Legends", "Dark and Darker"]
bot.active_campaigns: Dict[str, bool]          = {}
bot.twitch_game_ids: Dict[str, str]            = {}
bot.twitch_token: Optional[str]                = None
bot.twitch_token_expiry: float                 = 0.0

###############################################################################
# ──────────────────────────────── UTILITIES ───────────────────────────────── #
###############################################################################


def get_music_lock(gid: int) -> asyncio.Lock:
    if gid not in bot.music_locks:
        bot.music_locks[gid] = asyncio.Lock()
    return bot.music_locks[gid]


def cancel_auto_leave(gid: int) -> None:
    task = bot.auto_leave_tasks.pop(gid, None)
    if task and not task.done():
        task.cancel()


def clear_state(gid: int) -> None:
    cancel_auto_leave(gid)
    if gid in bot.song_queue:
        bot.song_queue[gid].clear()
        bot.song_queue.pop(gid, None)
    bot.current_song.pop(gid, None)
    bot.looping.pop(gid, None)
    bot.volumes.pop(gid, None)


def format_duration(seconds: Optional[ Union[int, float] ]) -> str:
    if not seconds:
        return "Live / Unknown"
    seconds = int(seconds)
    mins, secs = divmod(seconds, 60)
    hours, mins = divmod(mins, 60)
    if hours > 0:
        return f"{hours}:{mins:02d}:{secs:02d}"
    return f"{mins}:{secs:02d}"


async def get_song_info(query: str, requester: Optional[str] = None) -> Optional[dict]:
    try:
        loop = asyncio.get_running_loop()
        with YoutubeDL(YDL_OPTS) as ydl:
            info = await loop.run_in_executor(None, lambda: ydl.extract_info(query, download=False))

        if not info:
            return None

        if "entries" in info:
            entries = [e for e in (info.get("entries") or []) if e]
            if not entries:
                return None
            info = entries[0]

        stream_url = info.get("url")
        if not stream_url:
            return None

        user_agent = info.get("http_headers", {}).get(
            "User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
        )

        return {
            "title": info.get("title", "Unknown"),
            "stream_url": stream_url,
            "webpage_url": info.get("webpage_url") or query,
            "query": query,
            "duration": info.get("duration"),
            "thumbnail": info.get("thumbnail"),
            "user_agent": user_agent,
            "requester": requester,
            "extracted_at": time.monotonic(),
        }
    except Exception as e:
        logger.error("❌ YTDL Error for query %r: %s", query, e)
        return None


async def cleanup_voice_client(guild: discord.Guild) -> None:
    clear_state(guild.id)
    vc = guild.voice_client
    if vc:
        try:
            if vc.is_playing() or vc.is_paused():
                vc.stop()
            await vc.disconnect(force=True)
        except Exception as e:
            logger.error("Error disconnecting from voice in guild %s: %s", guild.id, e)


async def ensure_voice(ctx_like: Union[commands.Context, discord.Interaction]) -> Optional[discord.VoiceClient]:
    if not ctx_like.guild:
        return None

    user = getattr(ctx_like, "author", getattr(ctx_like, "user", None))
    if not isinstance(user, discord.Member) or not user.voice or not user.voice.channel:
        return None

    channel = user.voice.channel
    guild = ctx_like.guild
    vc = guild.voice_client

    if vc:
        if vc.is_connected():
            if vc.channel and vc.channel.id == channel.id:
                return vc
            try:
                await vc.move_to(channel)
                return vc
            except Exception as e:
                logger.warning("Failed to move voice channel, reconnecting: %s", e)
                await cleanup_voice_client(guild)
        else:
            await cleanup_voice_client(guild)

    try:
        return await channel.connect(timeout=20.0, self_deaf=True)
    except Exception as e:
        logger.error("Voice connection error in guild %s: %s", guild.id, e)
        return None


###############################################################################
# ──────────────────────────── MUSIC CORE LOGIC ────────────────────────────── #
###############################################################################


def schedule_auto_leave(guild: discord.Guild) -> None:
    cancel_auto_leave(guild.id)
    bot.auto_leave_tasks[guild.id] = asyncio.create_task(delayed_auto_leave(guild))


async def play_next(guild: discord.Guild) -> None:
    vc = guild.voice_client
    if not vc or not vc.is_connected():
        clear_state(guild.id)
        return

    queue = bot.song_queue.get(guild.id, [])
    if not queue:
        bot.current_song.pop(guild.id, None)
        schedule_auto_leave(guild)
        return

    cancel_auto_leave(guild.id)
    song = queue.pop(0)

    # Refresh expired stream URLs if queued for a long time
    age = time.monotonic() - song.get("extracted_at", 0)
    if age > STREAM_URL_TTL or not song.get("stream_url"):
        refreshed = await get_song_info(
            song.get("webpage_url") or song.get("query", ""),
            requester=song.get("requester"),
        )
        if refreshed:
            song.update(refreshed)

    bot.current_song[guild.id] = song

    def _after_playback(error: Optional[Exception]) -> None:
        if error:
            logger.error("Playback error in guild %s: %s", guild.id, error)

        # Only re-queue on loop if the song wasn't explicitly stopped/cleared
        if bot.looping.get(guild.id) and bot.current_song.get(guild.id) is song:
            bot.song_queue.setdefault(guild.id, []).insert(0, song)

        if guild.voice_client:
            fut = asyncio.run_coroutine_threadsafe(play_next(guild), bot.loop)
            fut.add_done_callback(
                lambda f: logger.error("Error in play_next: %s", f.exception())
                if not f.cancelled() and f.exception()
                else None
            )

    try:
        user_agent = song.get("user_agent", "Mozilla/5.0")
        before_options = (
            f'-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 '
            f'-nostdin -user_agent "{user_agent}"'
        )

        source = discord.PCMVolumeTransformer(
            discord.FFmpegPCMAudio(
                song["stream_url"],
                executable=FFMPEG_EXE,
                before_options=before_options,
                options="-vn",
            ),
            volume=bot.volumes.get(guild.id, DEFAULT_VOLUME),
        )
        vc.play(source, after=_after_playback)

    except Exception as e:
        logger.error("❌ Playback failed in guild %s: %s", guild.id, e)
        await play_next(guild)


async def delayed_auto_leave(guild: discord.Guild) -> None:
    try:
        await asyncio.sleep(AUTO_LEAVE_DELAY)
        vc = guild.voice_client
        if vc and not vc.is_playing() and not vc.is_paused():
            if not bot.song_queue.get(guild.id):
                await cleanup_voice_client(guild)
    except asyncio.CancelledError:
        pass


###############################################################################
# ───────────────────────────── MUSIC COMMANDS ─────────────────────────────── #
###############################################################################


async def _send_reply(ctx_or_inter: Union[commands.Context, discord.Interaction], content: Optional[str] = None, *, embed: Optional[discord.Embed] = None) -> None:
    if isinstance(ctx_or_inter, discord.Interaction):
        if ctx_or_inter.response.is_done():
            await ctx_or_inter.followup.send(content=content, embed=embed)
        else:
            await ctx_or_inter.response.send_message(content=content, embed=embed)
    else:
        await ctx_or_inter.send(content=content, embed=embed)


async def _play_logic(ctx_or_inter: Union[commands.Context, discord.Interaction], query: str) -> None:
    if not ctx_or_inter.guild:
        return await _send_reply(ctx_or_inter, "❌ Music commands can only be used in a server.")

    async with get_music_lock(ctx_or_inter.guild.id):
        vc = await ensure_voice(ctx_or_inter)
        if not vc:
            return await _send_reply(ctx_or_inter, "❌ You must be in a voice channel.")

        gid = ctx_or_inter.guild.id
        bot.song_queue.setdefault(gid, [])
        bot.looping.setdefault(gid, False)
        bot.volumes.setdefault(gid, DEFAULT_VOLUME)

        user = getattr(ctx_or_inter, "author", getattr(ctx_or_inter, "user", None))
        requester = user.display_name if user else None

        song = await get_song_info(query, requester=requester)
        if not song:
            return await _send_reply(ctx_or_inter, "❌ Could not find track.")

        bot.song_queue[gid].append(song)
        cancel_auto_leave(gid)

        duration_str = format_duration(song.get("duration"))
        await _send_reply(
            ctx_or_inter,
            f"🎵 Added to queue: **{song['title']}** (`{duration_str}`)",
        )

        if not vc.is_playing() and not vc.is_paused():
            await play_next(ctx_or_inter.guild)


# ─── Slash Music Commands ───


@bot.tree.command(name="play", description="Queue a song from YouTube (URL or search query)")
async def slash_play(inter: discord.Interaction, query: str):
    await inter.response.defer(thinking=True)
    await _play_logic(inter, query)


@bot.tree.command(name="skip", description="Skip current song")
async def slash_skip(inter: discord.Interaction):
    vc = inter.guild.voice_client if inter.guild else None
    if vc and (vc.is_playing() or vc.is_paused()):
        # Prevent single-track loop from replaying the skipped track
        if bot.looping.get(inter.guild.id):
            bot.current_song.pop(inter.guild.id, None)
        vc.stop()
        await inter.response.send_message("⏭️ Skipped.")
    else:
        await inter.response.send_message("❌ Nothing is playing.")


@bot.tree.command(name="queue", description="View song queue")
async def slash_queue(inter: discord.Interaction):
    if not inter.guild:
        return await inter.response.send_message("❌ Server only.")

    q = bot.song_queue.get(inter.guild.id, [])
    current = bot.current_song.get(inter.guild.id)

    if not q and not current:
        return await inter.response.send_message("📭 Queue is empty.")

    msg_lines = []
    if current:
        dur = format_duration(current.get("duration"))
        loop_badge = " 🔁 *(Looping)*" if bot.looping.get(inter.guild.id) else ""
        msg_lines.append(f"▶️ **Now Playing:** {current['title']} (`{dur}`){loop_badge}\n")

    if q:
        msg_lines.append("**Up Next:**")
        for i, s in enumerate(q[:10]):
            dur = format_duration(s.get("duration"))
            msg_lines.append(f"`{i+1}.` {s['title']} (`{dur}`)")
        if len(q) > 10:
            msg_lines.append(f"... and {len(q) - 10} more")

    vol_pct = int(bot.volumes.get(inter.guild.id, DEFAULT_VOLUME) * 100)
    embed = discord.Embed(
        title="🎵 Music Queue",
        description="\n".join(msg_lines),
        color=0x3498DB,
    )
    embed.set_footer(text=f"Total queued: {len(q)} | Volume: {vol_pct}%")
    await inter.response.send_message(embed=embed)


@bot.tree.command(name="leave", description="Disconnect bot from voice")
async def slash_leave(inter: discord.Interaction):
    if inter.guild and inter.guild.voice_client:
        await cleanup_voice_client(inter.guild)
        await inter.response.send_message("👋 Bye!")
    else:
        await inter.response.send_message("❌ Not connected.")


@bot.tree.command(name="pause", description="Pause the current song")
async def slash_pause(inter: discord.Interaction):
    vc = inter.guild.voice_client if inter.guild else None
    if vc and vc.is_playing():
        vc.pause()
        await inter.response.send_message("⏸️ Paused.")
    else:
        await inter.response.send_message("❌ Nothing is playing to pause.")


@bot.tree.command(name="resume", description="Resume playback")
async def slash_resume(inter: discord.Interaction):
    vc = inter.guild.voice_client if inter.guild else None
    if vc and vc.is_paused():
        vc.resume()
        await inter.response.send_message("▶️ Resumed.")
    else:
        await inter.response.send_message("❌ Nothing is paused.")


@bot.tree.command(name="stop", description="Stop music and clear queue")
async def slash_stop(inter: discord.Interaction):
    vc = inter.guild.voice_client if inter.guild else None
    if vc and inter.guild:
        gid = inter.guild.id
        if gid in bot.song_queue:
            bot.song_queue[gid].clear()
        bot.current_song.pop(gid, None)
        bot.looping[gid] = False
        if vc.is_playing() or vc.is_paused():
            vc.stop()
        await inter.response.send_message("🛑 Stopped and queue cleared.")
    else:
        await inter.response.send_message("❌ Not connected.")


@bot.tree.command(name="loop", description="Toggle looping for the current song")
async def slash_loop(inter: discord.Interaction):
    if not inter.guild:
        return await inter.response.send_message("❌ Server only.")
    gid = inter.guild.id
    new_state = not bot.looping.get(gid, False)
    bot.looping[gid] = new_state
    status = "🔁 Looping **enabled** for current track." if new_state else "➡️ Looping **disabled**."
    await inter.response.send_message(status)


@bot.tree.command(name="volume", description="Set playback volume (1-200%)")
@app_commands.describe(percent="Volume percentage between 1 and 200 (default 20)")
async def slash_volume(inter: discord.Interaction, percent: app_commands.Range[int, 1, 200]):
    if not inter.guild:
        return await inter.response.send_message("❌ Server only.")
    gid = inter.guild.id
    vol = percent / 100.0
    bot.volumes[gid] = vol

    vc = inter.guild.voice_client
    if vc and isinstance(vc.source, discord.PCMVolumeTransformer):
        vc.source.volume = vol

    await inter.response.send_message(f"🔊 Volume set to **{percent}%**.")


@bot.tree.command(name="shuffle", description="Shuffle the current queue")
async def slash_shuffle(inter: discord.Interaction):
    if not inter.guild:
        return await inter.response.send_message("❌ Server only.")
    queue = bot.song_queue.get(inter.guild.id, [])
    if len(queue) < 2:
        return await inter.response.send_message("❌ Not enough songs to shuffle.")

    random.shuffle(queue)
    await inter.response.send_message("🔀 Queue shuffled!")


@bot.tree.command(name="remove", description="Remove a specific song number from queue")
async def slash_remove(inter: discord.Interaction, number: int):
    if not inter.guild:
        return await inter.response.send_message("❌ Server only.")
    queue = bot.song_queue.get(inter.guild.id, [])
    if not queue:
        return await inter.response.send_message("❌ Queue is empty.")

    idx = number - 1
    if 0 <= idx < len(queue):
        removed = queue.pop(idx)
        await inter.response.send_message(f"🗑️ Removed: **{removed['title']}**")
    else:
        await inter.response.send_message(f"❌ Invalid number. Queue has {len(queue)} songs.")


@bot.tree.command(name="nowplaying", description="Show current song details")
async def slash_nowplaying(inter: discord.Interaction):
    if not inter.guild:
        return await inter.response.send_message("❌ Server only.")
    song = bot.current_song.get(inter.guild.id)
    vc = inter.guild.voice_client
    if song and vc and (vc.is_playing() or vc.is_paused()):
        state_str = "⏸️ Paused" if vc.is_paused() else "🎶 Now Playing"
        embed = discord.Embed(
            title=state_str,
            description=f"**[{song['title']}]({song.get('webpage_url', '')})**" if song.get("webpage_url", "").startswith("http") else f"**{song['title']}**",
            color=0x1DB954,
        )
        embed.add_field(name="⏱️ Duration", value=format_duration(song.get("duration")), inline=True)
        embed.add_field(
            name="🔁 Loop",
            value="On" if bot.looping.get(inter.guild.id) else "Off",
            inline=True,
        )
        if song.get("requester"):
            embed.add_field(name="👤 Requested by", value=song["requester"], inline=True)
        if song.get("thumbnail"):
            embed.set_thumbnail(url=song["thumbnail"])
        await inter.response.send_message(embed=embed)
    else:
        await inter.response.send_message("❌ Nothing is playing.")


# ─── Fallback Prefix Commands ───


@bot.command()
async def play(ctx: commands.Context, *, query: str):
    await _play_logic(ctx, query)


@bot.command()
async def skip(ctx: commands.Context):
    vc = ctx.guild.voice_client if ctx.guild else None
    if vc and (vc.is_playing() or vc.is_paused()):
        if bot.looping.get(ctx.guild.id):
            bot.current_song.pop(ctx.guild.id, None)
        vc.stop()
        await ctx.send("⏭️ Skipped.")
    else:
        await ctx.send("❌ Nothing is playing.")


@bot.command()
async def stop(ctx: commands.Context):
    vc = ctx.guild.voice_client if ctx.guild else None
    if vc and ctx.guild:
        gid = ctx.guild.id
        if gid in bot.song_queue:
            bot.song_queue[gid].clear()
        bot.current_song.pop(gid, None)
        bot.looping[gid] = False
        if vc.is_playing() or vc.is_paused():
            vc.stop()
        await ctx.send("🛑 Stopped and queue cleared.")


@bot.command()
async def leave(ctx: commands.Context):
    if ctx.guild and ctx.guild.voice_client:
        await cleanup_voice_client(ctx.guild)
        await ctx.send("👋 Bye!")


@bot.command()
@commands.has_permissions(administrator=True)
async def sync(ctx: commands.Context):
    """Manually sync commands globally (Administrators only)."""
    await ctx.send("🔄 Syncing commands...")
    await bot.tree.sync()
    await ctx.send("✅ Commands synced globally.")


@sync.error
async def sync_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ You must be a Server Administrator to run `!sync`.")


###############################################################################
# ───────────────────────────── POLL & TEAM ────────────────────────────────── #
###############################################################################


@bot.tree.command(name="poll", description="Create a reaction poll")
@app_commands.describe(
    question="The poll question",
    options="Options separated by commas (e.g. Yes, No, Maybe)",
    duration="Duration in seconds (10-3600, default 30)",
)
async def slash_poll(
    inter: discord.Interaction,
    question: str,
    options: str,
    duration: app_commands.Range[int, 10, 3600] = POLL_DURATION,
):
    opts = [o.strip() for o in options.split(",") if o.strip()]
    if len(opts) < 2 or len(opts) > 10:
        return await inter.response.send_message("❌ Need between 2 and 10 options.", ephemeral=True)

    embed = discord.Embed(title=f"📊 {question}", color=0x00B0F4)
    desc = [f"{NUM_EMOJIS[i]} {opt}" for i, opt in enumerate(opts)]
    embed.description = "\n".join(desc)
    embed.set_footer(text=f"Poll ends in {duration}s • React below to vote!")

    await inter.response.send_message(embed=embed)
    msg = await inter.original_response()

    for i in range(len(opts)):
        await msg.add_reaction(NUM_EMOJIS[i])

    await asyncio.sleep(duration)

    if not inter.channel:
        return

    try:
        msg = await inter.channel.fetch_message(msg.id)
        results = []
        total_votes = 0

        for i, opt in enumerate(opts):
            react = discord.utils.get(msg.reactions, emoji=NUM_EMOJIS[i])
            if react:
                voters = [u async for u in react.users() if not u.bot]
                count = len(voters)
            else:
                count = 0
            total_votes += count
            results.append((opt, count))

        results.sort(key=lambda x: x[1], reverse=True)
        res_lines = []
        for opt, count in results:
            pct = (count / total_votes * 100) if total_votes > 0 else 0
            filled = int(round(pct / 10))
            bar = "🟦" * filled + "⬜" * (10 - filled)
            res_lines.append(f"**{opt}** — `{count}` vote(s) (`{pct:.0f}%`)\n{bar}")

        res_embed = discord.Embed(
            title=f"📊 Poll Results: {question}",
            description="\n\n".join(res_lines),
            color=0x00FF00,
        )
        res_embed.set_footer(text=f"Total votes cast: {total_votes}")
        await inter.followup.send(embed=res_embed)
    except Exception as e:
        logger.error("Poll error: %s", e)


@bot.tree.command(name="teams", description="Split users into two random teams")
@app_commands.describe(timeout="Sign-up duration in seconds (10-300, default 30)")
async def slash_teams(
    inter: discord.Interaction,
    timeout: app_commands.Range[int, 10, 300] = TEAM_TIMEOUT,
):
    embed = discord.Embed(
        title="⚽ Team Sign-up",
        description=f"React with {JOIN_EMOJI} to join! ({timeout}s)",
        color=0xF1C40F,
    )
    await inter.response.send_message(embed=embed)
    msg = await inter.original_response()
    await msg.add_reaction(JOIN_EMOJI)

    await asyncio.sleep(timeout)

    if not inter.channel:
        return

    try:
        msg = await inter.channel.fetch_message(msg.id)
        react = discord.utils.get(msg.reactions, emoji=JOIN_EMOJI)
        users = [u.display_name async for u in react.users() if not u.bot] if react else []

        if len(users) < 2:
            return await inter.followup.send("❌ Not enough players signed up (need at least 2).")

        random.shuffle(users)
        mid = len(users) // 2
        if len(users) % 2 != 0 and random.choice([True, False]):
            mid += 1

        t1, t2 = users[:mid], users[mid:]

        embed = discord.Embed(title="⚽ Teams Generated", color=0x2ECC71)
        embed.add_field(name=f"{TEAM_A_EMOJI} Team A ({len(t1)})", value="\n".join(t1), inline=True)
        embed.add_field(name=f"{TEAM_B_EMOJI} Team B ({len(t2)})", value="\n".join(t2), inline=True)
        await inter.followup.send(embed=embed)
    except Exception as e:
        logger.error("Team error: %s", e)


###############################################################################
# ───────────────────────────── QUIZ COMMAND ──────────────────────────────── #
###############################################################################


@bot.tree.command(name="quiz", description="Start a multi-question trivia quiz")
async def slash_quiz(inter: discord.Interaction):
    await inter.response.defer()

    url = f"https://the-trivia-api.com/api/questions?limit={QUIZ_QTY}"
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                else:
                    return await inter.followup.send("❌ Trivia API returned an error.")
    except Exception as e:
        logger.error("Quiz API error: %s", e)
        return await inter.followup.send("❌ Failed to contact Trivia API.")

    scores: Dict[int, int] = {}
    total_q = len(data)

    for i, q in enumerate(data):
        question = html.unescape(str(q.get("question", "")))
        correct = html.unescape(str(q.get("correctAnswer", "")))
        wrongs = [html.unescape(str(w)) for w in q.get("incorrectAnswers", [])]

        options = wrongs + [correct]
        random.shuffle(options)

        try:
            correct_idx = options.index(correct)
        except ValueError:
            continue

        desc = f"**{question}**\n\n"
        for idx, opt in enumerate(options):
            desc += f"{LETTER_EMOJIS[idx]} {opt}\n"

        embed = discord.Embed(
            title=f"❓ Question {i + 1}/{total_q}",
            description=desc,
            color=0x9B59B6,
        )
        embed.set_footer(text=f"Time: {QUIZ_TIMEOUT}s • React with ONE letter only!")

        q_msg = await inter.followup.send(embed=embed, wait=True)

        for j in range(len(options)):
            await q_msg.add_reaction(LETTER_EMOJIS[j])

        await asyncio.sleep(QUIZ_TIMEOUT)

        if not inter.channel:
            break

        try:
            q_msg = await inter.channel.fetch_message(q_msg.id)

            # Track all reactions per user so multi-reacting can't be used to cheat
            user_choices: Dict[int, List[int]] = {}
            user_names: Dict[int, str] = {}

            for idx in range(len(options)):
                reaction = discord.utils.get(q_msg.reactions, emoji=LETTER_EMOJIS[idx])
                if reaction:
                    async for u in reaction.users():
                        if not u.bot:
                            user_choices.setdefault(u.id, []).append(idx)
                            user_names[u.id] = u.display_name

            correct_users = []
            for uid, chosen_indices in user_choices.items():
                if len(chosen_indices) == 1 and chosen_indices[0] == correct_idx:
                    scores[uid] = scores.get(uid, 0) + 1
                    correct_users.append(user_names[uid])

            await inter.followup.send(
                f"✅ Answer: **{correct}**\n"
                f"Correct: {', '.join(correct_users) if correct_users else 'None'}"
            )
            await asyncio.sleep(2)
        except discord.NotFound:
            pass
        except Exception as e:
            logger.error("Error evaluating quiz question: %s", e)

    if not scores:
        await inter.followup.send("🏁 Quiz over. No points scored!")
        return

    sorted_scores = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    res_lines = [
        f"`{rank}.` <@{uid}> — **{score}** pt(s)"
        for rank, (uid, score) in enumerate(sorted_scores, start=1)
    ]
    lb_embed = discord.Embed(
        title="🏆 Quiz Leaderboard",
        description="\n".join(res_lines),
        color=0xF1C40F,
    )
    await inter.followup.send(embed=lb_embed)


###############################################################################
# ───────────────────────────── TWITCH DROPS ──────────────────────────────── #
###############################################################################


async def get_twitch_token(session: aiohttp.ClientSession) -> Optional[str]:
    now = time.monotonic()
    if bot.twitch_token and now < bot.twitch_token_expiry:
        return bot.twitch_token

    url = "https://id.twitch.tv/oauth2/token"
    params = {
        "client_id": TWITCH_CLIENT_ID,
        "client_secret": TWITCH_CLIENT_SECRET,
        "grant_type": "client_credentials",
    }
    async with session.post(url, params=params) as resp:
        if resp.status != 200:
            body = await resp.text()
            logger.error("❌ Twitch OAuth failed (%d): %s", resp.status, body)
            return None
        data = await resp.json()
        token = data.get("access_token")
        expires_in = int(data.get("expires_in", 3600))
        if token:
            bot.twitch_token = token
            bot.twitch_token_expiry = now + max(60, expires_in - 300)
        return token


async def get_game_id(session: aiohttp.ClientSession, headers: Dict[str, str], game_name: str) -> Optional[str]:
    """Translates a game name like 'Rust' into Twitch's internal Game ID (with caching)."""
    if game_name in bot.twitch_game_ids:
        return bot.twitch_game_ids[game_name]

    url = "https://api.twitch.tv/helix/games"
    async with session.get(url, headers=headers, params={"name": game_name}) as resp:
        if resp.status != 200:
            return None
        data = await resp.json()
        games = data.get("data") or []
        if games:
            gid = games[0]["id"]
            bot.twitch_game_ids[game_name] = gid
            return gid
        return None


@tasks.loop(minutes=10)
async def check_twitch_campaigns():
    if not (TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET and TWITCH_NOTIFY_CHANNEL_ID):
        return

    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            token = await get_twitch_token(session)
            if not token:
                return

            headers = {
                "Client-ID": TWITCH_CLIENT_ID,
                "Authorization": f"Bearer {token}",
            }

            for game_name in list(bot.twitch_games):
                game_id = await get_game_id(session, headers, game_name)
                if not game_id:
                    continue

                url = "https://api.twitch.tv/helix/streams"
                params = {"game_id": game_id, "first": "20"}
                async with session.get(url, headers=headers, params=params) as resp:
                    if resp.status == 401:
                        # Force token refresh next cycle
                        bot.twitch_token = None
                        return
                    if resp.status != 200:
                        continue

                    data = await resp.json()
                    campaign_active = False

                    for stream in data.get("data") or []:
                        raw_tags = stream.get("tags") or []
                        normalized_tags = {t.lower().replace(" ", "") for t in raw_tags if isinstance(t, str)}
                        if "dropsenabled" in normalized_tags:
                            campaign_active = True
                            break

                    if campaign_active and not bot.active_campaigns.get(game_name):
                        channel = bot.get_channel(TWITCH_NOTIFY_CHANNEL_ID)
                        if not channel:
                            try:
                                channel = await bot.fetch_channel(TWITCH_NOTIFY_CHANNEL_ID)
                            except Exception:
                                channel = None

                        if channel:
                            encoded_game = quote(game_name)
                            embed = discord.Embed(
                                title=f"🚨 A {game_name} Drop Campaign just started!",
                                description=(
                                    f"Multiple top streamers are currently live with drops enabled for **{game_name}**.\n\n"
                                    f"[Click here to browse {game_name} streams]"
                                    f"(https://www.twitch.tv/directory/game/{encoded_game}?tl=c2542d6d-cd10-4532-919b-3d19f30a768b)"
                                ),
                                color=0x9146FF,
                            )
                            await channel.send(embed=embed)

                        bot.active_campaigns[game_name] = True

                    elif not campaign_active and bot.active_campaigns.get(game_name):
                        bot.active_campaigns[game_name] = False

    except Exception as e:
        logger.error("❌ Twitch Campaign Checker Error: %s", e)


@check_twitch_campaigns.before_loop
async def before_check_twitch():
    await bot.wait_until_ready()


@bot.tree.command(name="drops", description="View or manage games monitored for Twitch Drops")
@app_commands.describe(
    action="Choose whether to list, add, or remove a game",
    game_name="Game name on Twitch (required for add/remove)",
)
@app_commands.choices(
    action=[
        app_commands.Choice(name="list", value="list"),
        app_commands.Choice(name="add", value="add"),
        app_commands.Choice(name="remove", value="remove"),
    ]
)
async def slash_drops(
    inter: discord.Interaction,
    action: app_commands.Choice[str],
    game_name: Optional[str] = None,
):
    act = action.value
    if act == "list":
        if not bot.twitch_games:
            return await inter.response.send_message("📭 No Twitch games are currently monitored.")
        lines = []
        for g in bot.twitch_games:
            active = "🟢 Drops Active" if bot.active_campaigns.get(g) else "⚪ Idle"
            lines.append(f"• **{g}** — {active}")
        embed = discord.Embed(
            title="🎁 Monitored Twitch Drops Games",
            description="\n".join(lines),
            color=0x9146FF,
        )
        return await inter.response.send_message(embed=embed)

    if not inter.guild or not isinstance(inter.user, discord.Member):
        return await inter.response.send_message("❌ Server only.", ephemeral=True)

    if not (inter.user.guild_permissions.manage_guild or inter.user.guild_permissions.administrator):
        return await inter.response.send_message("❌ You need `Manage Server` permission to edit monitored games.", ephemeral=True)

    if not game_name or not game_name.strip():
        return await inter.response.send_message("❌ Please provide a `game_name`.", ephemeral=True)

    clean_name = game_name.strip()
    if act == "add":
        if any(g.lower() == clean_name.lower() for g in bot.twitch_games):
            return await inter.response.send_message(f"ℹ️ **{clean_name}** is already being monitored.")
        bot.twitch_games.append(clean_name)
        return await inter.response.send_message(f"✅ Added **{clean_name}** to Twitch Drops monitor.")

    if act == "remove":
        for existing in list(bot.twitch_games):
            if existing.lower() == clean_name.lower():
                bot.twitch_games.remove(existing)
                bot.active_campaigns.pop(existing, None)
                return await inter.response.send_message(f"🗑️ Removed **{existing}** from Twitch Drops monitor.")
        return await inter.response.send_message(f"❌ **{clean_name}** was not in the monitored list.")


###############################################################################
# ───────────────────────────── GLOBAL EVENTS ──────────────────────────────── #
###############################################################################


@bot.event
async def on_voice_state_update(
    member: discord.Member,
    before: discord.VoiceState,
    after: discord.VoiceState,
):
    # If the bot itself was disconnected from voice externally, clean up state
    if member.id == bot.user.id:
        if before.channel and not after.channel:
            clear_state(member.guild.id)
        return

    if member.bot:
        return

    # 1. Join Lobby -> Create Dynamic Channel
    if LOBBY_CHANNEL_ID and after.channel and after.channel.id == LOBBY_CHANNEL_ID:
        chan = None
        try:
            cat = after.channel.category
            overwrites = {
                member: discord.PermissionOverwrite(
                    manage_channels=True,
                    move_members=True,
                    connect=True,
                )
            }
            chan = await member.guild.create_voice_channel(
                name=f"🔊 {member.display_name}",
                category=cat,
                overwrites=overwrites,
            )
            bot.dynamic_channels.add(chan.id)
            await member.move_to(chan)
        except Exception as e:
            logger.error("Lobby dynamic channel error: %s", e)
            # Clean up orphaned channel if moving the member failed
            if chan and len([m for m in chan.members if not m.bot]) == 0:
                try:
                    await chan.delete()
                except Exception:
                    pass
                bot.dynamic_channels.discard(chan.id)

    # 2. Leave Dynamic Channel -> Delete if no human members remain
    if before.channel and before.channel.id in bot.dynamic_channels:
        human_members = [m for m in before.channel.members if not m.bot]
        if len(human_members) == 0:
            try:
                if member.guild.voice_client and member.guild.voice_client.channel == before.channel:
                    await cleanup_voice_client(member.guild)
                await before.channel.delete()
            except Exception as e:
                logger.warning("Failed deleting dynamic channel %s: %s", before.channel.id, e)
            finally:
                bot.dynamic_channels.discard(before.channel.id)

    # 3. Auto-leave music voice channel if all humans left
    vc = member.guild.voice_client
    if vc and vc.channel and before.channel and vc.channel.id == before.channel.id:
        human_members = [m for m in vc.channel.members if not m.bot]
        if len(human_members) == 0:
            schedule_auto_leave(member.guild)


@bot.event
async def on_ready():
    logger.info("✅ Logged in as %s (ID: %s)", bot.user, bot.user.id)

    if not check_twitch_campaigns.is_running():
        check_twitch_campaigns.start()
        logger.info("✅ Twitch Campaign Checker started.")

    if not bot._synced:
        if TEST_GUILD_ID:
            try:
                guild = discord.Object(id=TEST_GUILD_ID)
                bot.tree.copy_global_to(guild=guild)
                await bot.tree.sync(guild=guild)
                bot._synced = True
                logger.info("🔄 Commands synced to Test Guild (%d)", TEST_GUILD_ID)
            except Exception as e:
                logger.error("❌ Failed to sync test guild: %s", e)
        else:
            logger.info("ℹ️ No TEST_GUILD_ID set. Use !sync (Admin) to sync globally.")


if __name__ == "__main__":
    bot.run(TOKEN)