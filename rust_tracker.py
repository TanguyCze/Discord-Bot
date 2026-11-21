import re
import time
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

logger = logging.getLogger("DiscordBot.RustTracker")

DEFAULT_RUST_FORUM_ID = 1567095214185906319  # #rust-builds-and-links


def parse_bm_id(raw: str) -> Optional[str]:
    """Extracts a numeric BattleMetrics server ID from either an ID or a BattleMetrics URL."""
    s = (raw or "").strip()
    if s.isdigit():
        return s
    m = re.search(r"battlemetrics\.com/servers/[^/]+/(\d+)", s)
    if m:
        return m.group(1)
    m2 = re.search(r"(\d{5,12})", s)
    return m2.group(1) if m2 else None


def _iso_to_unix(iso_str: Optional[str]) -> Optional[int]:
    if not iso_str:
        return None
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return int(dt.timestamp())
    except Exception:
        return None


async def fetch_battlemetrics_server(server_id: str) -> Optional[Dict[str, Any]]:
    """Queries the free public BattleMetrics API for live Rust server info."""
    bm_id = parse_bm_id(server_id)
    if not bm_id:
        return None
    url = f"https://api.battlemetrics.com/servers/{bm_id}"
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return None
                payload = await resp.json()
        attrs = (payload.get("data") or {}).get("attributes") or {}
        details = attrs.get("details") or {}
        return {
            "id": bm_id,
            "name": attrs.get("name") or f"Rust Server {bm_id}",
            "status": attrs.get("status") or "unknown",
            "players": int(attrs.get("players") or 0),
            "max_players": int(attrs.get("maxPlayers") or 0),
            "queued": int(details.get("rust_queued_players") or 0),
            "map": details.get("map") or "Procedural Map",
            "ip": attrs.get("ip") or "",
            "port": attrs.get("port") or "",
            "last_wipe": details.get("rust_last_wipe") or "",
            "next_wipe": details.get("rust_next_wipe") or "",
            "url": f"https://www.battlemetrics.com/servers/rust/{bm_id}",
            "fetched_at": int(time.time()),
        }
    except Exception as e:
        logger.warning("BattleMetrics fetch failed for %s: %s", bm_id, e)
        return None


def build_rust_status_embed(servers_info: List[Dict[str, Any]]) -> discord.Embed:
    embed = discord.Embed(
        title="☢️ Rust Server Tracker & Wipe Monitor",
        color=0xCE422B,
    )
    if not servers_info:
        embed.description = "No Rust servers tracked yet. Add a BattleMetrics Server ID in the Web UI!"
        return embed

    for info in servers_info:
        status_dot = "🟢 Online" if info.get("status") == "online" else "🔴 Offline"
        players = info.get("players", 0)
        max_p = info.get("max_players", 0)
        queued = info.get("queued", 0)
        q_str = f" (+{queued} queue)" if queued > 0 else ""

        last_ts = _iso_to_unix(info.get("last_wipe"))
        next_ts = _iso_to_unix(info.get("next_wipe"))
        wipe_lines = []
        if last_ts:
            wipe_lines.append(f"🧹 **Last Wipe:** <t:{last_ts}:R>")
        if next_ts:
            wipe_lines.append(f"⏳ **Next Wipe:** <t:{next_ts}:R> (<t:{next_ts}:f>)")
        wipe_str = "\n".join(wipe_lines) if wipe_lines else "🧹 Wipe schedule: Not published"

        ip_port = f"`client.connect {info['ip']}:{info['port']}`" if info.get("ip") and info.get("port") else ""
        val = (
            f"{status_dot} • **{players}/{max_p}** players{q_str}\n"
            f"🗺️ Map: `{info.get('map', 'Unknown')}`\n"
            f"{wipe_str}\n"
            f"{ip_port}\n"
            f"🔗 [BattleMetrics]({info.get('url')})"
        )
        embed.add_field(name=info.get("name", "Rust Server")[:250], value=val[:1000], inline=False)

    embed.set_footer(text="Auto-updated live • Wipe alerts enabled")
    return embed


def setup_rust_tracker(bot: commands.Bot, config_mgr):
    """Registers the BattleMetrics background updater, /rust_status, and /base_build."""

    # Runtime cache of latest fetched stats for Web UI display
    bot.rust_cache: Dict[str, Dict[str, Any]] = {}

    async def refresh_rust_servers() -> List[Dict[str, Any]]:
        rust_cfg = config_mgr.data.setdefault("rust_tracker", {"channel_id": "", "message_id": None, "servers": []})
        servers = rust_cfg.get("servers") or []
        if not servers:
            return []

        results: List[Dict[str, Any]] = []
        dirty = False
        wipe_alerts: List[Dict[str, Any]] = []

        for srv in servers:
            bm_id = str(srv.get("id") or "").strip()
            if not bm_id:
                continue
            info = await fetch_battlemetrics_server(bm_id)
            if not info:
                continue
            bot.rust_cache[bm_id] = info
            results.append(info)

            prev_wipe = srv.get("last_wipe_seen") or ""
            curr_wipe = info.get("last_wipe") or ""
            if curr_wipe and prev_wipe and curr_wipe != prev_wipe:
                wipe_alerts.append(info)
            if curr_wipe and curr_wipe != prev_wipe:
                srv["last_wipe_seen"] = curr_wipe
                dirty = True
            if info.get("name") and srv.get("name") != info["name"]:
                srv["name"] = info["name"]
                dirty = True

        # Post / edit live status embed if a channel is configured
        cid_str = str(rust_cfg.get("channel_id") or "").strip()
        if cid_str.isdigit() and results:
            ch = bot.get_channel(int(cid_str))
            if isinstance(ch, discord.TextChannel):
                embed = build_rust_status_embed(results)
                mid = rust_cfg.get("message_id")
                edited = False
                if mid:
                    try:
                        old_msg = await ch.fetch_message(int(mid))
                        await old_msg.edit(embed=embed)
                        edited = True
                    except Exception:
                        edited = False
                if not edited:
                    try:
                        sent = await ch.send(embed=embed, silent=True)
                        rust_cfg["message_id"] = str(sent.id)
                        dirty = True
                    except Exception as e:
                        logger.warning("Failed posting Rust tracker embed: %s", e)

                # Send fresh alert if a server just wiped
                for wiped in wipe_alerts:
                    ip_cmd = f"`client.connect {wiped['ip']}:{wiped['port']}`" if wiped.get("ip") else ""
                    w_embed = discord.Embed(
                        title=f"🚨 FRESH WIPE DETECTED: {wiped['name']}",
                        description=(
                            f"The server just wiped! Grab your rock and get on!\n\n"
                            f"👥 **Players:** {wiped['players']}/{wiped['max_players']}\n"
                            f"🗺️ **Map:** {wiped['map']}\n"
                            f"{ip_cmd}\n"
                            f"🔗 [View on BattleMetrics]({wiped['url']})"
                        ),
                        color=0xE74C3C,
                    )
                    try:
                        await ch.send(embed=w_embed)
                    except Exception as e:
                        logger.warning("Failed sending Rust wipe alert: %s", e)

        if dirty:
            config_mgr.save()
        return results

    @tasks.loop(minutes=5)
    async def check_rust_servers():
        await refresh_rust_servers()

    @check_rust_servers.before_loop
    async def before_check_rust():
        await bot.wait_until_ready()

    @bot.tree.command(name="rust_status", description="Check live player count & wipe status of tracked Rust servers")
    @app_commands.describe(server="Optional BattleMetrics Server ID or URL to check on the fly")
    async def slash_rust_status(inter: discord.Interaction, server: Optional[str] = None):
        await inter.response.defer()
        if server:
            info = await fetch_battlemetrics_server(server)
            if not info:
                return await inter.followup.send("❌ Could not fetch that BattleMetrics server ID/URL.")
            return await inter.followup.send(embed=build_rust_status_embed([info]))

        results = await refresh_rust_servers()
        if not results:
            return await inter.followup.send(
                "ℹ️ No Rust servers are tracked yet. Pass a BattleMetrics ID/URL here or add one in the **☢️ Rust Tracker** tab of the Web UI!"
            )
        await inter.followup.send(embed=build_rust_status_embed(results))

    @bot.tree.command(name="base_build", description="Post a formatted Rust base build or link thread into #rust-builds-and-links")
    @app_commands.describe(
        title="Build name (e.g. '2x2 Bunker Starter' or 'Trio Circle Peak')",
        category="Tag category for the Forum post",
        link="YouTube, Fortify, or Rustrician link",
        footprint="Footprint / Group size (e.g. 2x2 Honeycombed / Solo-Duo)",
        upkeep="Estimated 24h upkeep (e.g. 4k Stone, 2k Metal, 40 HQM)",
        raid_cost="Estimated raid cost (e.g. 24 Rockets)",
        notes="Extra tips, bunker details, or electricity notes",
    )
    @app_commands.choices(
        category=[
            app_commands.Choice(name="🏠 Base Build", value="Base Build"),
            app_commands.Choice(name="⛺ Solo / Duo", value="Solo / Duo"),
            app_commands.Choice(name="🏰 Trio / Group", value="Trio / Group"),
            app_commands.Choice(name="⚡ Electricity & Industrial", value="Electricity & Industrial"),
            app_commands.Choice(name="🔗 Useful Links", value="Useful Links"),
            app_commands.Choice(name="💥 Raid & PvP", value="Raid & PvP"),
        ]
    )
    async def slash_base_build(
        inter: discord.Interaction,
        title: str,
        category: app_commands.Choice[str],
        link: str,
        footprint: str = "Not specified",
        upkeep: str = "Not specified",
        raid_cost: str = "Not specified",
        notes: str = "",
    ):
        if not inter.guild:
            return await inter.response.send_message("❌ Server only.", ephemeral=True)

        await inter.response.defer(ephemeral=True)
        forum = inter.guild.get_channel(DEFAULT_RUST_FORUM_ID)
        if not isinstance(forum, discord.ForumChannel):
            # Fallback: find any forum channel with 'rust' in name
            forum = next(
                (c for c in inter.guild.forums if "rust" in c.name.lower()),
                inter.guild.forums[0] if inter.guild.forums else None,
            )
        if not isinstance(forum, discord.ForumChannel):
            return await inter.followup.send("❌ Could not find the `#rust-builds-and-links` Forum channel.", ephemeral=True)

        applied_tags = [t for t in forum.available_tags if category.value.lower() in t.name.lower()][:2]
        body_lines = [
            f"👤 **Posted by:** {inter.user.mention}",
            f"📐 **Footprint / Size:** {footprint}",
            f"🧱 **Upkeep (24h):** {upkeep}",
            f"💥 **Raid Cost:** {raid_cost}",
            f"🔗 **Build / Video Link:** {link}",
        ]
        if notes.strip():
            body_lines.append(f"\n📝 **Notes:**\n{notes.strip()}")

        try:
            created = await forum.create_thread(
                name=title[:95],
                content="\n".join(body_lines),
                applied_tags=applied_tags,
            )
            thread_obj = created.thread if hasattr(created, "thread") else created
            await inter.followup.send(f"✅ Posted your Rust build thread: {thread_obj.mention}!", ephemeral=True)
        except Exception as e:
            await inter.followup.send(f"❌ Failed to create Forum thread: {e}", ephemeral=True)

    return check_rust_servers, refresh_rust_servers
