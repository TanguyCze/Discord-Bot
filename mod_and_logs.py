import re
import time
import logging
from collections import deque
from datetime import timedelta
from typing import Deque, Dict, List, Optional

import discord
from discord import app_commands
from discord.ext import commands

logger = logging.getLogger("DiscordBot.ModLogs")

INVITE_REGEX = re.compile(
    r"(https?://)?(www\.)?(discord\.(gg|io|me|li)|discordapp\.com/invite|discord\.com/invite)/[A-Za-z0-9-]+",
    re.IGNORECASE,
)


class BotLogBuffer(logging.Handler):
    """In-memory ring buffer that captures recent bot logs for the Web UI Live Logs viewer."""

    def __init__(self, maxlen: int = 250) -> None:
        super().__init__()
        self.records: Deque[Dict[str, str]] = deque(maxlen=maxlen)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            self.records.append(
                {
                    "ts": time.strftime("%H:%M:%S", time.localtime(record.created)),
                    "level": record.levelname,
                    "logger": record.name,
                    "message": msg,
                }
            )
        except Exception:
            pass

    def get_logs(self, limit: int = 150) -> List[Dict[str, str]]:
        items = list(self.records)
        return items[-limit:]


def install_log_buffer(maxlen: int = 250) -> BotLogBuffer:
    buf = BotLogBuffer(maxlen=maxlen)
    buf.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", "%H:%M:%S"))
    root_logger = logging.getLogger()
    root_logger.addHandler(buf)
    return buf


async def _send_mod_log(bot: commands.Bot, config_mgr, guild: discord.Guild, embed: discord.Embed) -> None:
    mod_cfg = config_mgr.data.get("mod_settings", {})
    log_cid = str(mod_cfg.get("mod_log_channel_id") or "").strip()
    if not log_cid.isdigit():
        return
    ch = guild.get_channel(int(log_cid))
    if isinstance(ch, discord.TextChannel):
        try:
            await ch.send(embed=embed, silent=True)
        except Exception as e:
            logger.warning("Failed sending mod log to #%s: %s", ch.name, e)


def setup_mod_and_logs(bot: commands.Bot, config_mgr) -> None:
    """Registers welcome banner, audit/mod log listeners, auto-mod, and mod slash commands."""

    @bot.listen("on_member_join")
    async def _mod_on_member_join(member: discord.Member) -> None:
        if member.bot:
            return
        mod_cfg = config_mgr.data.get("mod_settings", {})

        # 1. Welcome Banner
        welcome_cid = str(mod_cfg.get("welcome_channel_id") or "").strip()
        if welcome_cid.isdigit():
            w_ch = member.guild.get_channel(int(welcome_cid))
            if isinstance(w_ch, discord.TextChannel):
                template = mod_cfg.get(
                    "welcome_message",
                    "Welcome {mention} to **{server}**! Head over to <#575643151268708354> to pick your community role (🥖 French, 🥩 Austrian, or 🌍 International).",
                )
                desc = (
                    template.replace("{mention}", member.mention)
                    .replace("{user}", member.display_name)
                    .replace("{server}", member.guild.name)
                    .replace("{count}", str(member.guild.member_count or 0))
                )
                embed = discord.Embed(
                    title=f"👋 Welcome, {member.display_name}!",
                    description=desc,
                    color=0x5865F2,
                )
                if member.display_avatar:
                    embed.set_thumbnail(url=member.display_avatar.url)
                embed.set_footer(text=f"Member #{member.guild.member_count or '?'}")
                try:
                    await w_ch.send(embed=embed)
                except Exception as e:
                    logger.warning("Failed sending welcome banner: %s", e)

        # 2. Mod Log
        log_embed = discord.Embed(
            title="📥 Member Joined",
            description=f"{member.mention} (`{member.name}` • ID `{member.id}`)",
            color=0x2ECC71,
        )
        if member.display_avatar:
            log_embed.set_thumbnail(url=member.display_avatar.url)
        log_embed.add_field(
            name="Account Created",
            value=f"<t:{int(member.created_at.timestamp())}:R>",
            inline=True,
        )
        await _send_mod_log(bot, config_mgr, member.guild, log_embed)

    @bot.listen("on_member_remove")
    async def _mod_on_member_remove(member: discord.Member) -> None:
        if member.bot:
            return
        roles = [r.name for r in member.roles if r.id != member.guild.id]
        roles_str = ", ".join(roles) if roles else "None"
        log_embed = discord.Embed(
            title="📤 Member Left / Removed",
            description=f"**{member.display_name}** (`{member.name}` • ID `{member.id}`)",
            color=0xE67E22,
        )
        log_embed.add_field(name="Roles", value=roles_str[:1000], inline=False)
        await _send_mod_log(bot, config_mgr, member.guild, log_embed)

    @bot.listen("on_member_ban")
    async def _mod_on_member_ban(guild: discord.Guild, user: discord.User) -> None:
        log_embed = discord.Embed(
            title="🔨 Member Banned",
            description=f"**{user.display_name}** (`{user.name}` • ID `{user.id}`)",
            color=0xE74C3C,
        )
        await _send_mod_log(bot, config_mgr, guild, log_embed)

    @bot.listen("on_message_delete")
    async def _mod_on_message_delete(message: discord.Message) -> None:
        if not message.guild or message.author.bot:
            return
        content = (message.content or "").strip()
        if not content and not message.attachments:
            return
        att_info = f"\n📎 Attachments: {len(message.attachments)}" if message.attachments else ""
        log_embed = discord.Embed(
            title="🗑️ Message Deleted",
            description=f"**Author:** {message.author.mention}\n**Channel:** {message.channel.mention}\n\n{content[:1500]}{att_info}",
            color=0xE74C3C,
        )
        await _send_mod_log(bot, config_mgr, message.guild, log_embed)

    @bot.listen("on_message_edit")
    async def _mod_on_message_edit(before: discord.Message, after: discord.Message) -> None:
        if not before.guild or before.author.bot:
            return
        if (before.content or "") == (after.content or ""):
            return
        log_embed = discord.Embed(
            title="✏️ Message Edited",
            description=f"**Author:** {before.author.mention} in {before.channel.mention} ([Jump]({after.jump_url}))",
            color=0xF1C40F,
        )
        log_embed.add_field(name="Before", value=(before.content or "*empty*")[:900], inline=False)
        log_embed.add_field(name="After", value=(after.content or "*empty*")[:900], inline=False)
        await _send_mod_log(bot, config_mgr, before.guild, log_embed)

    @bot.listen("on_message")
    async def _automod_on_message(message: discord.Message) -> None:
        if not message.guild or message.author.bot or not isinstance(message.author, discord.Member):
            return
        # Skip Admins & Mods
        perms = message.author.guild_permissions
        if perms.administrator or perms.manage_messages:
            return

        mod_cfg = config_mgr.data.get("mod_settings", {})
        content = message.content or ""
        reason: Optional[str] = None

        # 1. Anti-Discord-Invite Links
        if mod_cfg.get("anti_invite", False) and INVITE_REGEX.search(content):
            reason = "Unauthorized Discord invite link"

        # 2. Anti-Mass-Mention
        max_mentions = int(mod_cfg.get("max_mentions", 0) or 0)
        if not reason and max_mentions > 0 and len(message.mentions) >= max_mentions:
            reason = f"Mass mention spam ({len(message.mentions)} mentions)"

        # 3. Bad-word filter
        if not reason:
            bad_words = [w.strip().lower() for w in (mod_cfg.get("banned_words") or []) if w.strip()]
            lower_c = content.lower()
            for bw in bad_words:
                if bw in lower_c:
                    reason = f"Blocked word filter (`{bw}`)"
                    break

        if reason:
            try:
                await message.delete()
                warn = await message.channel.send(
                    f"⚠️ {message.author.mention}, your message was removed by Auto-Mod (**{reason}**).",
                    delete_after=7,
                )
                _ = warn
            except Exception as e:
                logger.warning("Auto-mod delete failed: %s", e)

            log_embed = discord.Embed(
                title="🛡️ Auto-Mod Action",
                description=(
                    f"**User:** {message.author.mention}\n"
                    f"**Channel:** {message.channel.mention}\n"
                    f"**Reason:** {reason}\n"
                    f"**Content:** `{content[:500]}`"
                ),
                color=0xE74C3C,
            )
            await _send_mod_log(bot, config_mgr, message.guild, log_embed)

    # ─── Moderation Slash Commands (For HRE, Gloria, Baguette du Fromage) ───

    @bot.tree.command(name="clear", description="Delete a number of recent messages in this channel (Mods/Admins)")
    @app_commands.describe(amount="Number of messages to delete (1-100)")
    async def slash_clear(inter: discord.Interaction, amount: app_commands.Range[int, 1, 100]):
        if not inter.guild or not isinstance(inter.user, discord.Member) or not isinstance(inter.channel, discord.TextChannel):
            return await inter.response.send_message("❌ Server text channel only.", ephemeral=True)
        if not (inter.user.guild_permissions.manage_messages or inter.user.guild_permissions.administrator):
            return await inter.response.send_message("❌ You need `Manage Messages` permission.", ephemeral=True)

        await inter.response.defer(ephemeral=True)
        deleted = await inter.channel.purge(limit=amount)
        await inter.followup.send(f"🧹 Deleted **{len(deleted)}** message(s).", ephemeral=True)

        log_embed = discord.Embed(
            title="🧹 Messages Purged",
            description=f"**Moderator:** {inter.user.mention}\n**Channel:** {inter.channel.mention}\n**Count:** {len(deleted)}",
            color=0x3498DB,
        )
        await _send_mod_log(bot, config_mgr, inter.guild, log_embed)

    @bot.tree.command(name="timeout", description="Temporarily mute/timeout a member (Mods/Admins)")
    @app_commands.describe(member="Member to timeout", minutes="Duration in minutes (1-10080)", reason="Reason for timeout")
    async def slash_timeout(
        inter: discord.Interaction,
        member: discord.Member,
        minutes: app_commands.Range[int, 1, 10080] = 10,
        reason: str = "No reason provided",
    ):
        if not inter.guild or not isinstance(inter.user, discord.Member):
            return await inter.response.send_message("❌ Server only.", ephemeral=True)
        if not (inter.user.guild_permissions.moderate_members or inter.user.guild_permissions.administrator):
            return await inter.response.send_message("❌ You need `Timeout Members` permission.", ephemeral=True)

        try:
            await member.timeout(timedelta(minutes=minutes), reason=f"By {inter.user}: {reason}")
            await inter.response.send_message(
                f"🔇 Timed out **{member.display_name}** for **{minutes}** minute(s). Reason: {reason}",
                ephemeral=True,
            )
            log_embed = discord.Embed(
                title="🔇 Member Timed Out",
                description=f"**Member:** {member.mention}\n**Moderator:** {inter.user.mention}\n**Duration:** {minutes} min\n**Reason:** {reason}",
                color=0xE67E22,
            )
            await _send_mod_log(bot, config_mgr, inter.guild, log_embed)
        except Exception as e:
            await inter.response.send_message(f"❌ Could not timeout member: {e}", ephemeral=True)

    @bot.tree.command(name="kick", description="Kick a member from the server (Mods/Admins)")
    @app_commands.describe(member="Member to kick", reason="Reason for kick")
    async def slash_kick(inter: discord.Interaction, member: discord.Member, reason: str = "No reason provided"):
        if not inter.guild or not isinstance(inter.user, discord.Member):
            return await inter.response.send_message("❌ Server only.", ephemeral=True)
        if not (inter.user.guild_permissions.kick_members or inter.user.guild_permissions.administrator):
            return await inter.response.send_message("❌ You need `Kick Members` permission.", ephemeral=True)

        try:
            await member.kick(reason=f"By {inter.user}: {reason}")
            await inter.response.send_message(f"👢 Kicked **{member.display_name}**. Reason: {reason}", ephemeral=True)
            log_embed = discord.Embed(
                title="👢 Member Kicked",
                description=f"**Member:** {member.display_name} (`{member.id}`)\n**Moderator:** {inter.user.mention}\n**Reason:** {reason}",
                color=0xE67E22,
            )
            await _send_mod_log(bot, config_mgr, inter.guild, log_embed)
        except Exception as e:
            await inter.response.send_message(f"❌ Could not kick member: {e}", ephemeral=True)

    @bot.tree.command(name="ban", description="Ban a member from the server (Mods/Admins)")
    @app_commands.describe(member="Member to ban", reason="Reason for ban")
    async def slash_ban(inter: discord.Interaction, member: discord.Member, reason: str = "No reason provided"):
        if not inter.guild or not isinstance(inter.user, discord.Member):
            return await inter.response.send_message("❌ Server only.", ephemeral=True)
        if not (inter.user.guild_permissions.ban_members or inter.user.guild_permissions.administrator):
            return await inter.response.send_message("❌ You need `Ban Members` permission.", ephemeral=True)

        try:
            await member.ban(reason=f"By {inter.user}: {reason}")
            await inter.response.send_message(f"🔨 Banned **{member.display_name}**. Reason: {reason}", ephemeral=True)
            log_embed = discord.Embed(
                title="🔨 Member Banned",
                description=f"**Member:** {member.display_name} (`{member.id}`)\n**Moderator:** {inter.user.mention}\n**Reason:** {reason}",
                color=0xE74C3C,
            )
            await _send_mod_log(bot, config_mgr, inter.guild, log_embed)
        except Exception as e:
            await inter.response.send_message(f"❌ Could not ban member: {e}", ephemeral=True)
