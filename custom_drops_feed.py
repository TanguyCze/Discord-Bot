"""
Standalone Custom Twitch Drops Feed Script (Anti-Spam)
------------------------------------------------------
Can be used standalone (`python custom_drops_feed.py`) or alongside `discord_bot.py`.
Checks only the games YOU care about and sends alerts to:
  1. Your Private Discord User DM (via Bot Token + your User ID), OR
  2. A Private Discord Channel (via Bot Token + Channel ID), OR
  3. A Private Discord Webhook URL.

Includes persistent cooldown tracking in `bot_data.json` so it NEVER spams!
"""

import os
import json
import time
import asyncio
from urllib.parse import quote
from typing import List, Dict, Optional
import aiohttp
from dotenv import load_dotenv

load_dotenv()

TWITCH_CLIENT_ID = os.getenv("TWITCH_CLIENT_ID", "")
TWITCH_CLIENT_SECRET = os.getenv("TWITCH_CLIENT_SECRET", "")
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")

# Optional standalone overrides via .env
PERSONAL_USER_ID = os.getenv("PERSONAL_DISCORD_USER_ID", "")
PERSONAL_CHANNEL_ID = os.getenv("PERSONAL_DROPS_CHANNEL_ID", "")
PERSONAL_WEBHOOK_URL = os.getenv("PERSONAL_DROPS_WEBHOOK_URL", "")
PERSONAL_GAMES = [
    g.strip()
    for g in os.getenv("PERSONAL_DROPS_GAMES", "Rust,League of Legends,Dark and Darker").split(",")
    if g.strip()
]
COOLDOWN_HOURS = int(os.getenv("DROPS_COOLDOWN_HOURS", "48"))

STATE_FILE = os.getenv("BOT_CONFIG_PATH", "bot_data.json")


def load_state() -> dict:
    if os.path.isfile(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"campaign_history": {}}


def save_state(state: dict) -> None:
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        print(f"⚠️ Failed saving state: {e}")


async def get_twitch_token(session: aiohttp.ClientSession) -> Optional[str]:
    url = "https://id.twitch.tv/oauth2/token"
    params = {
        "client_id": TWITCH_CLIENT_ID,
        "client_secret": TWITCH_CLIENT_SECRET,
        "grant_type": "client_credentials",
    }
    async with session.post(url, params=params) as resp:
        if resp.status != 200:
            return None
        data = await resp.json()
        return data.get("access_token")


async def check_game_drops(session: aiohttp.ClientSession, headers: Dict[str, str], game_name: str) -> Optional[dict]:
    async with session.get("https://api.twitch.tv/helix/games", headers=headers, params={"name": game_name}) as resp:
        if resp.status != 200:
            return None
        games = (await resp.json()).get("data") or []
        if not games:
            return None
        game_id = games[0]["id"]

    async with session.get(
        "https://api.twitch.tv/helix/streams",
        headers=headers,
        params={"game_id": game_id, "first": "20"},
    ) as resp:
        if resp.status != 200:
            return None
        streams = (await resp.json()).get("data") or []

    drop_streams = []
    for s in streams:
        tags = {t.lower().replace(" ", "") for t in (s.get("tags") or []) if isinstance(t, str)}
        if "dropsenabled" in tags:
            drop_streams.append(s)

    min_req = 1 if len(streams) < 4 else 2
    active = len(drop_streams) >= min_req
    top = drop_streams[0] if drop_streams else None
    return {
        "game": game_name,
        "active": active,
        "count": len(drop_streams),
        "top_streamer": top.get("user_name") if top else None,
        "top_viewers": top.get("viewer_count") if top else 0,
    }


async def send_private_alert(session: aiohttp.ClientSession, embed: dict) -> None:
    # 1. Webhook delivery
    if PERSONAL_WEBHOOK_URL:
        await session.post(PERSONAL_WEBHOOK_URL, json={"embeds": [embed]})
        return

    if not DISCORD_TOKEN:
        return

    discord_headers = {
        "Authorization": f"Bot {DISCORD_TOKEN}",
        "Content-Type": "application/json",
    }

    # 2. Direct Message (DM) to Personal User ID
    if PERSONAL_USER_ID:
        dm_resp = await session.post(
            "https://discord.com/api/v10/users/@me/channels",
            headers=discord_headers,
            json={"recipient_id": PERSONAL_USER_ID},
        )
        if dm_resp.status == 200:
            dm_chan_id = (await dm_resp.json()).get("id")
            if dm_chan_id:
                await session.post(
                    f"https://discord.com/api/v10/channels/{dm_chan_id}/messages",
                    headers=discord_headers,
                    json={"embeds": [embed]},
                )
                return

    # 3. Private Channel ID delivery
    if PERSONAL_CHANNEL_ID:
        await session.post(
            f"https://discord.com/api/v10/channels/{PERSONAL_CHANNEL_ID}/messages",
            headers=discord_headers,
            json={"embeds": [embed]},
        )


async def run_custom_feed_once() -> None:
    if not (TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET):
        print("❌ Missing TWITCH_CLIENT_ID or TWITCH_CLIENT_SECRET in .env")
        return

    state = load_state()
    history = state.setdefault("campaign_history", {})
    now_ts = time.time()
    cooldown_sec = COOLDOWN_HOURS * 3600
    changed = False

    async with aiohttp.ClientSession() as session:
        token = await get_twitch_token(session)
        if not token:
            print("❌ Failed to authenticate with Twitch.")
            return

        headers = {"Client-ID": TWITCH_CLIENT_ID, "Authorization": f"Bearer {token}"}

        for game in PERSONAL_GAMES:
            info = await check_game_drops(session, headers, game)
            if not info:
                continue

            hkey = f"standalone::{game}"
            entry = history.setdefault(hkey, {"active": False, "last_notified_ts": 0.0})

            if info["active"]:
                elapsed = now_ts - float(entry.get("last_notified_ts") or 0.0)
                if not entry.get("active") and elapsed >= cooldown_sec:
                    encoded = quote(game)
                    embed = {
                        "title": f"🎁 Personal Drops Alert: {game}",
                        "description": (
                            f"**{info['count']}** streams are live with Drops enabled for **{game}**!\n"
                            f"🔥 Top stream: **{info['top_streamer']}** (`{info['top_viewers']}` viewers)\n\n"
                            f"👉 [Open {game} Drops on Twitch](https://www.twitch.tv/directory/game/{encoded}?tl=c2542d6d-cd10-4532-919b-3d19f30a768b)"
                        ),
                        "color": 0x9146FF,
                    }
                    await send_private_alert(session, embed)
                    entry["last_notified_ts"] = now_ts
                    print(f"✅ Sent private drops alert for {game}")

                entry["active"] = True
                changed = True
            else:
                if entry.get("active"):
                    entry["active"] = False
                    changed = True

    if changed:
        save_state(state)


if __name__ == "__main__":
    asyncio.run(run_custom_feed_once())
