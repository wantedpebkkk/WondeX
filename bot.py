"""
WondeX Discord Bot
A moderation, security, and ticket bot for Discord servers.
"""

import asyncio
import collections
import datetime
import re
import os
import time
import discord
from discord.ext import commands
from dashboard import bot_stats, start_dashboard_thread

# Graceful shutdown after this many seconds (just under the 355-min workflow timeout)
_RUNTIME_SECONDS = 350 * 60

# ──────────────────────────────────────────────
# Bot configuration
# ──────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.moderation = True  # Required for on_audit_log_entry_create

bot = commands.Bot(command_prefix="Wa!", intents=intents)

# Track whether the dashboard thread has been started
_dashboard_started = False

# ──────────────────────────────────────────────
# Shared per-guild configuration
# ──────────────────────────────────────────────

# Mod-log channel: {guild_id: channel_id}
_modlog_channels: dict[int, int] = {}

# Auto-role: {guild_id: role_id}
_autorole: dict[int, int] = {}

# Warning store: {guild_id: {user_id: [reason, ...]}}
_warnings: dict[int, dict[int, list[str]]] = collections.defaultdict(
    lambda: collections.defaultdict(list)
)

# Anti-spam: {guild_id: {user_id: [unix_timestamps]}}
_spam_log: dict[int, dict[int, list[float]]] = collections.defaultdict(
    lambda: collections.defaultdict(list)
)

# Anti-invite toggle: {guild_id: bool}
_antiinvite: dict[int, bool] = {}

# Anti-raid: {guild_id: [join_unix_timestamps]}
_raid_log: dict[int, list[float]] = collections.defaultdict(list)

# Regex matching common Discord invite links
_INVITE_RE = re.compile(
    r"(discord\.gg|discord\.com/invite|discordapp\.com/invite)/[a-zA-Z0-9\-]+",
    re.IGNORECASE,
)

# Anti-spam thresholds
_SPAM_LIMIT = 5       # messages
_SPAM_WINDOW = 5      # seconds

# Anti-raid thresholds
_RAID_LIMIT = 10      # joins
_RAID_WINDOW = 10     # seconds

# Maximum Discord slowmode in seconds (28 days worth of minutes in seconds = 21600 s max)
_MAX_SLOWMODE_SECONDS = 21600

# Auto-punish warns threshold
_WARN_AUTO_MUTE = 3
_WARN_AUTO_BAN = 5

# ──────────────────────────────────────────────
# Graceful shutdown helper
# ──────────────────────────────────────────────

async def _shutdown_after(seconds: int) -> None:
    """Wait *seconds* then close the bot so the workflow exits cleanly."""
    await asyncio.sleep(seconds)
    print(f"⏰  Scheduled runtime of {seconds // 60} minutes reached — shutting down gracefully.")
    await bot.close()


# ──────────────────────────────────────────────
# Events
# ──────────────────────────────────────────────

@bot.event
async def on_ready():
    global _dashboard_started
    print(f"✅  Logged in as {bot.user} (ID: {bot.user.id})")
    # Register persistent views so buttons keep working after restarts
    bot.add_view(TicketPanelView())
    bot.add_view(CloseClaimView())
    await bot.change_presence(
        activity=discord.Activity(
            type=discord.ActivityType.watching,
            name="over the server 🛡️",
        )
    )
    # Update shared dashboard stats
    bot_stats["bot_name"] = bot.user.name
    bot_stats["bot_avatar"] = str(bot.user.display_avatar.url)
    bot_stats["guild_count"] = len(bot.guilds)
    bot_stats["member_count"] = sum(g.member_count or 0 for g in bot.guilds)
    bot_stats["status"] = "online"
    # Start the web dashboard (only once across reconnects)
    if not _dashboard_started:
        _dashboard_started = True
        start_dashboard_thread()
        print("🌐  Dashboard running on http://localhost:5000")
        # Schedule a graceful shutdown just before the workflow timeout so the
        # job exits with code 0 (completed) rather than being killed.
        bot.loop.create_task(_shutdown_after(_RUNTIME_SECONDS))


@bot.event
async def on_command(ctx):
    """Count every successfully invoked command."""
    bot_stats["command_count"] += 1


@bot.event
async def on_guild_join(guild: discord.Guild):
    """Keep guild/member counts up to date."""
    bot_stats["guild_count"] = len(bot.guilds)
    bot_stats["member_count"] = sum(g.member_count or 0 for g in bot.guilds)


@bot.event
async def on_guild_remove(guild: discord.Guild):
    """Keep guild/member counts up to date."""
    bot_stats["guild_count"] = len(bot.guilds)
    bot_stats["member_count"] = sum(g.member_count or 0 for g in bot.guilds)


@bot.event
async def on_member_join(member: discord.Member):
    """Send a welcome message, assign auto-role, and check for raids."""
    guild = member.guild
    bot_stats["member_count"] = sum(g.member_count or 0 for g in bot.guilds)

    # ── Welcome message ──────────────────────────
    channel = discord.utils.get(guild.text_channels, name="general")
    if channel:
        embed = discord.Embed(
            title=f"Welcome to {guild.name}! 🎉",
            description=f"Hey {member.mention}, welcome aboard! Please read the rules.",
            color=discord.Color.green(),
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        await channel.send(embed=embed)

    # ── Auto-role ────────────────────────────────
    role_id = _autorole.get(guild.id)
    if role_id:
        role = guild.get_role(role_id)
        if role:
            try:
                await member.add_roles(role, reason="Auto-role on join")
            except discord.Forbidden:
                pass

    # ── Anti-raid ────────────────────────────────
    now = time.time()
    joins = _raid_log[guild.id]
    joins[:] = [t for t in joins if now - t < _RAID_WINDOW]
    joins.append(now)
    if len(joins) >= _RAID_LIMIT:
        joins.clear()
        # Lock every text channel for @everyone
        for ch in guild.text_channels:
            try:
                ow = ch.overwrites_for(guild.default_role)
                ow.send_messages = False
                await ch.set_permissions(guild.default_role, overwrite=ow)
            except discord.Forbidden:
                pass
        await _send_modlog(
            guild,
            discord.Embed(
                title="🚨 Anti-Raid Triggered",
                description=(
                    f"**{_RAID_LIMIT}+ members** joined within **{_RAID_WINDOW}s**.\n"
                    "All channels have been locked. Use `Wa!unlock` to restore them."
                ),
                color=discord.Color.red(),
                timestamp=datetime.datetime.now(datetime.timezone.utc),
            ).set_footer(text="WondeX Anti-Raid"),
        )


# ──────────────────────────────────────────────
# Mod-log helper
# ──────────────────────────────────────────────

async def _send_modlog(guild: discord.Guild, embed: discord.Embed) -> None:
    """Send *embed* to the configured mod-log channel (or fallback channels)."""
    ch_id = _modlog_channels.get(guild.id)
    ch = guild.get_channel(ch_id) if ch_id else None
    if ch is None:
        ch = (
            discord.utils.get(guild.text_channels, name="mod-log")
            or discord.utils.get(guild.text_channels, name="logs")
            or discord.utils.get(guild.text_channels, name="audit-log")
        )
    if ch:
        try:
            await ch.send(embed=embed)
        except discord.Forbidden:
            pass


# ──────────────────────────────────────────────
# Anti-spam & anti-invite (on_message)
# ──────────────────────────────────────────────

@bot.event
async def on_message(message: discord.Message) -> None:
    """Run anti-spam and anti-invite checks before processing commands."""
    if message.author.bot or not message.guild:
        await bot.process_commands(message)
        return

    guild = message.guild
    author = message.author

    # ── Anti-invite ──────────────────────────────
    if _antiinvite.get(guild.id) and _INVITE_RE.search(message.content):
        # Allow administrators to post invites freely
        if not author.guild_permissions.administrator:
            try:
                await message.delete()
            except discord.Forbidden:
                pass
            try:
                await author.send(
                    f"⚠️ Posting invite links is not allowed in **{guild.name}**."
                )
            except discord.Forbidden:
                pass
            await _send_modlog(
                guild,
                discord.Embed(
                    title="🔗 Invite Link Removed",
                    description=(
                        f"**User:** {author.mention} (`{author}`)\n"
                        f"**Channel:** {message.channel.mention}"
                    ),
                    color=discord.Color.orange(),
                    timestamp=datetime.datetime.now(datetime.timezone.utc),
                ).set_footer(text="WondeX Anti-Invite"),
            )
            return  # don't process commands from deleted messages

    # ── Anti-spam ────────────────────────────────
    now = time.time()
    log = _spam_log[guild.id][author.id]
    log[:] = [t for t in log if now - t < _SPAM_WINDOW]
    log.append(now)
    if len(log) >= _SPAM_LIMIT:
        log.clear()
        muted_role = discord.utils.get(guild.roles, name="Muted")
        if not muted_role:
            try:
                muted_role = await guild.create_role(name="Muted")
                for ch in guild.channels:
                    await ch.set_permissions(muted_role, send_messages=False, speak=False)
            except discord.Forbidden:
                muted_role = None
        if muted_role:
            try:
                await author.add_roles(muted_role, reason="[Anti-Spam] Message flood detected")
            except discord.Forbidden:
                pass
        await _send_modlog(
            guild,
            discord.Embed(
                title="🚫 Anti-Spam Triggered",
                description=(
                    f"**User:** {author.mention} (`{author}`)\n"
                    f"**Action:** Auto-muted for sending {_SPAM_LIMIT}+ messages in {_SPAM_WINDOW}s"
                ),
                color=discord.Color.orange(),
                timestamp=datetime.datetime.now(datetime.timezone.utc),
            ).set_footer(text="WondeX Anti-Spam"),
        )

    await bot.process_commands(message)


# ──────────────────────────────────────────────
# Moderation commands
# ──────────────────────────────────────────────

@bot.command(name="kick")
@commands.has_permissions(kick_members=True)
async def kick(ctx, member: discord.Member, *, reason: str = "No reason provided"):
    """Kick a member from the server."""
    await member.kick(reason=reason)
    embed = discord.Embed(
        title="Member Kicked",
        description=f"**{member}** has been kicked.\n**Reason:** {reason}",
        color=discord.Color.orange(),
    )
    await ctx.send(embed=embed)


@bot.command(name="ban")
@commands.has_permissions(ban_members=True)
async def ban(ctx, member: discord.Member, *, reason: str = "No reason provided"):
    """Ban a member from the server."""
    await member.ban(reason=reason)
    embed = discord.Embed(
        title="Member Banned",
        description=f"**{member}** has been banned.\n**Reason:** {reason}",
        color=discord.Color.red(),
    )
    await ctx.send(embed=embed)


@bot.command(name="unban")
@commands.has_permissions(ban_members=True)
async def unban(ctx, *, member_str: str):
    """Unban a member by username#discriminator."""
    banned_users = [entry async for entry in ctx.guild.bans()]
    for ban_entry in banned_users:
        user = ban_entry.user
        if str(user) == member_str:
            await ctx.guild.unban(user)
            embed = discord.Embed(
                title="Member Unbanned",
                description=f"**{user}** has been unbanned.",
                color=discord.Color.green(),
            )
            await ctx.send(embed=embed)
            return
    await ctx.send(f"Could not find banned user: `{member_str}`")


@bot.command(name="mute")
@commands.has_permissions(manage_roles=True)
async def mute(ctx, member: discord.Member, *, reason: str = "No reason provided"):
    """Mute a member by assigning the Muted role."""
    muted_role = discord.utils.get(ctx.guild.roles, name="Muted")
    if not muted_role:
        muted_role = await ctx.guild.create_role(name="Muted")
        for channel in ctx.guild.channels:
            await channel.set_permissions(muted_role, send_messages=False, speak=False)

    await member.add_roles(muted_role, reason=reason)
    embed = discord.Embed(
        title="Member Muted",
        description=f"**{member}** has been muted.\n**Reason:** {reason}",
        color=discord.Color.dark_grey(),
    )
    await ctx.send(embed=embed)


@bot.command(name="unmute")
@commands.has_permissions(manage_roles=True)
async def unmute(ctx, member: discord.Member):
    """Remove the Muted role from a member."""
    muted_role = discord.utils.get(ctx.guild.roles, name="Muted")
    if muted_role and muted_role in member.roles:
        await member.remove_roles(muted_role)
        embed = discord.Embed(
            title="Member Unmuted",
            description=f"**{member}** has been unmuted.",
            color=discord.Color.green(),
        )
        await ctx.send(embed=embed)
    else:
        await ctx.send(f"{member.mention} is not muted.")


@bot.command(name="warn")
@commands.has_permissions(manage_messages=True)
async def warn(ctx, member: discord.Member, *, reason: str = "No reason provided"):
    """Warn a member via DM and log in the channel. Auto-mutes at 3, auto-bans at 5."""
    _warnings[ctx.guild.id][member.id].append(reason)
    total = len(_warnings[ctx.guild.id][member.id])
    try:
        await member.send(
            f"⚠️ You have been warned in **{ctx.guild.name}**.\n**Reason:** {reason}\n"
            f"You now have **{total}** warning(s)."
        )
    except discord.Forbidden:
        pass

    embed = discord.Embed(
        title="⚠️ Member Warned",
        description=(
            f"**{member}** has been warned.\n"
            f"**Reason:** {reason}\n"
            f"**Total warnings:** {total}"
        ),
        color=discord.Color.yellow(),
    )
    await ctx.send(embed=embed)
    await _send_modlog(
        ctx.guild,
        discord.Embed(
            title="⚠️ Warn",
            description=(
                f"**User:** {member.mention} (`{member}`)\n"
                f"**By:** {ctx.author.mention}\n"
                f"**Reason:** {reason}\n"
                f"**Total warnings:** {total}"
            ),
            color=discord.Color.yellow(),
            timestamp=datetime.datetime.now(datetime.timezone.utc),
        ).set_footer(text="WondeX Mod-Log"),
    )

    # Auto-punish on threshold
    if total >= _WARN_AUTO_BAN:
        try:
            await member.ban(reason=f"[Auto-Ban] Reached {_WARN_AUTO_BAN} warnings")
            await ctx.send(f"🔨 {member.mention} was **auto-banned** for reaching {_WARN_AUTO_BAN} warnings.")
        except discord.Forbidden:
            pass
    elif total >= _WARN_AUTO_MUTE:
        muted_role = discord.utils.get(ctx.guild.roles, name="Muted")
        if not muted_role:
            muted_role = await ctx.guild.create_role(name="Muted")
            for channel in ctx.guild.channels:
                await channel.set_permissions(muted_role, send_messages=False, speak=False)
        try:
            await member.add_roles(muted_role, reason=f"[Auto-Mute] Reached {_WARN_AUTO_MUTE} warnings")
            await ctx.send(f"🔇 {member.mention} was **auto-muted** for reaching {_WARN_AUTO_MUTE} warnings.")
        except discord.Forbidden:
            pass


@bot.command(name="purge")
@commands.has_permissions(manage_messages=True)
async def purge(ctx, amount: int):
    """Delete a number of messages from the current channel."""
    if amount < 1 or amount > 100:
        await ctx.send("Please specify a number between 1 and 100.")
        return
    deleted = await ctx.channel.purge(limit=amount + 1)
    msg = await ctx.send(f"🗑️ Deleted {len(deleted) - 1} messages.")
    await msg.delete(delay=3)


@bot.command(name="timeout")
@commands.has_permissions(moderate_members=True)
async def timeout_cmd(ctx, member: discord.Member, duration: int, *, reason: str = "No reason provided"):
    """Timeout a member for *duration* minutes (max 40320 / 28 days)."""
    if member.top_role >= ctx.author.top_role and ctx.author != ctx.guild.owner:
        await ctx.send("❌ You cannot timeout someone with an equal or higher role.")
        return
    until = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=duration)
    try:
        await member.timeout(until, reason=reason)
    except discord.Forbidden:
        await ctx.send("❌ I don't have permission to timeout that member.")
        return
    embed = discord.Embed(
        title="⏰ Member Timed Out",
        description=f"**{member}** has been timed out for **{duration}m**.\n**Reason:** {reason}",
        color=discord.Color.orange(),
    )
    await ctx.send(embed=embed)
    await _send_modlog(
        ctx.guild,
        discord.Embed(
            title="⏰ Timeout",
            description=(
                f"**User:** {member.mention} (`{member}`)\n"
                f"**By:** {ctx.author.mention}\n"
                f"**Duration:** {duration} minutes\n"
                f"**Reason:** {reason}"
            ),
            color=discord.Color.orange(),
            timestamp=datetime.datetime.now(datetime.timezone.utc),
        ).set_footer(text="WondeX Mod-Log"),
    )


@bot.command(name="untimeout")
@commands.has_permissions(moderate_members=True)
async def untimeout_cmd(ctx, member: discord.Member):
    """Remove an active timeout from a member."""
    try:
        await member.timeout(None)
    except discord.Forbidden:
        await ctx.send("❌ I don't have permission to remove that timeout.")
        return
    embed = discord.Embed(
        title="⏰ Timeout Removed",
        description=f"**{member}**'s timeout has been lifted.",
        color=discord.Color.green(),
    )
    await ctx.send(embed=embed)


@bot.command(name="tempmute")
@commands.has_permissions(manage_roles=True)
async def tempmute(ctx, member: discord.Member, duration: int, *, reason: str = "No reason provided"):
    """Mute a member for *duration* minutes, then automatically unmute."""
    muted_role = discord.utils.get(ctx.guild.roles, name="Muted")
    if not muted_role:
        muted_role = await ctx.guild.create_role(name="Muted")
        for channel in ctx.guild.channels:
            await channel.set_permissions(muted_role, send_messages=False, speak=False)
    await member.add_roles(muted_role, reason=reason)
    embed = discord.Embed(
        title="🔇 Member Temp-Muted",
        description=f"**{member}** muted for **{duration}m**.\n**Reason:** {reason}",
        color=discord.Color.dark_grey(),
    )
    await ctx.send(embed=embed)

    async def _auto_unmute():
        await asyncio.sleep(duration * 60)
        if muted_role in member.roles:
            try:
                await member.remove_roles(muted_role, reason="[Temp-Mute] Duration expired")
            except discord.HTTPException:
                pass

    bot.loop.create_task(_auto_unmute())
    await _send_modlog(
        ctx.guild,
        discord.Embed(
            title="🔇 Temp-Mute",
            description=(
                f"**User:** {member.mention} (`{member}`)\n"
                f"**By:** {ctx.author.mention}\n"
                f"**Duration:** {duration} minutes\n"
                f"**Reason:** {reason}"
            ),
            color=discord.Color.dark_grey(),
            timestamp=datetime.datetime.now(datetime.timezone.utc),
        ).set_footer(text="WondeX Mod-Log"),
    )


@bot.command(name="tempban")
@commands.has_permissions(ban_members=True)
async def tempban(ctx, member: discord.Member, duration: int, *, reason: str = "No reason provided"):
    """Ban a member for *duration* minutes, then automatically unban."""
    await member.ban(reason=f"[Temp-Ban {duration}m] {reason}", delete_message_days=0)
    embed = discord.Embed(
        title="⛔ Member Temp-Banned",
        description=f"**{member}** banned for **{duration}m**.\n**Reason:** {reason}",
        color=discord.Color.red(),
    )
    await ctx.send(embed=embed)
    user_id = member.id
    guild = ctx.guild

    async def _auto_unban():
        await asyncio.sleep(duration * 60)
        try:
            await guild.unban(discord.Object(id=user_id), reason="[Temp-Ban] Duration expired")
        except discord.HTTPException:
            pass

    bot.loop.create_task(_auto_unban())
    await _send_modlog(
        ctx.guild,
        discord.Embed(
            title="⛔ Temp-Ban",
            description=(
                f"**User:** {member.mention} (`{member}`)\n"
                f"**By:** {ctx.author.mention}\n"
                f"**Duration:** {duration} minutes\n"
                f"**Reason:** {reason}"
            ),
            color=discord.Color.red(),
            timestamp=datetime.datetime.now(datetime.timezone.utc),
        ).set_footer(text="WondeX Mod-Log"),
    )


@bot.command(name="slowmode")
@commands.has_permissions(manage_channels=True)
async def slowmode(ctx, seconds: int = 0):
    """Set the slowmode delay for the current channel (0 to disable)."""
    if seconds < 0 or seconds > _MAX_SLOWMODE_SECONDS:
        await ctx.send(f"❌ Slowmode must be between 0 and {_MAX_SLOWMODE_SECONDS} seconds.")
        return
    await ctx.channel.edit(slowmode_delay=seconds)
    if seconds == 0:
        await ctx.send("✅ Slowmode **disabled** for this channel.")
    else:
        await ctx.send(f"✅ Slowmode set to **{seconds}s** in this channel.")


@bot.group(name="role", invoke_without_command=True)
@commands.has_permissions(manage_roles=True)
async def role_group(ctx):
    """Manage roles. Subcommands: add, remove."""
    await ctx.send("Usage: `Wa!role add @member @role` or `Wa!role remove @member @role`")


@role_group.command(name="add")
@commands.has_permissions(manage_roles=True)
async def role_add(ctx, member: discord.Member, role: discord.Role):
    """Add a role to a member."""
    if role >= ctx.guild.me.top_role:
        await ctx.send("❌ I cannot assign a role higher than or equal to my own top role.")
        return
    await member.add_roles(role, reason=f"Role added by {ctx.author}")
    await ctx.send(f"✅ Added **{role.name}** to {member.mention}.")


@role_group.command(name="remove")
@commands.has_permissions(manage_roles=True)
async def role_remove(ctx, member: discord.Member, role: discord.Role):
    """Remove a role from a member."""
    if role >= ctx.guild.me.top_role:
        await ctx.send("❌ I cannot remove a role higher than or equal to my own top role.")
        return
    await member.remove_roles(role, reason=f"Role removed by {ctx.author}")
    await ctx.send(f"✅ Removed **{role.name}** from {member.mention}.")


@bot.command(name="nick")
@commands.has_permissions(manage_nicknames=True)
async def nick(ctx, member: discord.Member, *, nickname: str = ""):
    """Change (or reset) a member's nickname."""
    new_nick = nickname.strip() or None
    try:
        await member.edit(nick=new_nick, reason=f"Nick changed by {ctx.author}")
    except discord.Forbidden:
        await ctx.send("❌ I don't have permission to change that member's nickname.")
        return
    if new_nick:
        await ctx.send(f"✅ Set **{member}**'s nickname to `{new_nick}`.")
    else:
        await ctx.send(f"✅ Reset **{member}**'s nickname.")


@bot.command(name="setlogchannel")
@commands.has_permissions(administrator=True)
async def setlogchannel(ctx, channel: discord.TextChannel = None):
    """Set the mod-log channel. Leave blank to use the current channel."""
    target = channel or ctx.channel
    _modlog_channels[ctx.guild.id] = target.id
    await ctx.send(f"✅ Mod-log channel set to {target.mention}.")


@bot.command(name="warnings")
@commands.has_permissions(manage_messages=True)
async def warnings_cmd(ctx, member: discord.Member):
    """Show all warnings for a member."""
    warns = _warnings[ctx.guild.id][member.id]
    if not warns:
        await ctx.send(f"{member.mention} has no warnings.")
        return
    desc = "\n".join(f"`{i + 1}.` {r}" for i, r in enumerate(warns))
    embed = discord.Embed(
        title=f"⚠️ Warnings for {member}",
        description=desc,
        color=discord.Color.yellow(),
    )
    embed.set_footer(text=f"Total: {len(warns)} warning(s)")
    await ctx.send(embed=embed)


@bot.command(name="clearwarns")
@commands.has_permissions(manage_messages=True)
async def clearwarns(ctx, member: discord.Member):
    """Clear all warnings for a member."""
    _warnings[ctx.guild.id][member.id].clear()
    await ctx.send(f"✅ Cleared all warnings for {member.mention}.")


@bot.group(name="autorole", invoke_without_command=True)
@commands.has_permissions(manage_roles=True)
async def autorole_group(ctx):
    """Configure the auto-role assigned to new members."""
    role_id = _autorole.get(ctx.guild.id)
    if role_id:
        role = ctx.guild.get_role(role_id)
        await ctx.send(f"Current auto-role: **{role.name if role else 'Role no longer exists (invalid ID)'}**")
    else:
        await ctx.send("No auto-role configured. Use `Wa!autorole set @role`.")


@autorole_group.command(name="set")
@commands.has_permissions(manage_roles=True)
async def autorole_set(ctx, role: discord.Role):
    """Set the role automatically assigned to new members."""
    _autorole[ctx.guild.id] = role.id
    await ctx.send(f"✅ Auto-role set to **{role.name}**.")


@autorole_group.command(name="remove")
@commands.has_permissions(manage_roles=True)
async def autorole_remove(ctx):
    """Remove the configured auto-role."""
    _autorole.pop(ctx.guild.id, None)
    await ctx.send("✅ Auto-role removed.")


@bot.group(name="antiinvite", invoke_without_command=True)
@commands.has_permissions(administrator=True)
async def antiinvite_group(ctx):
    """Show anti-invite status. Subcommands: on, off."""
    status = "✅ Enabled" if _antiinvite.get(ctx.guild.id) else "❌ Disabled"
    await ctx.send(f"Anti-invite is currently: {status}")


@antiinvite_group.command(name="on")
@commands.has_permissions(administrator=True)
async def antiinvite_on(ctx):
    """Enable automatic deletion of Discord invite links."""
    _antiinvite[ctx.guild.id] = True
    await ctx.send("✅ Anti-invite **enabled**. Invite links will be automatically removed.")


@antiinvite_group.command(name="off")
@commands.has_permissions(administrator=True)
async def antiinvite_off(ctx):
    """Disable anti-invite filtering."""
    _antiinvite[ctx.guild.id] = False
    await ctx.send("❌ Anti-invite **disabled**.")


# ──────────────────────────────────────────────
# Server security commands
# ──────────────────────────────────────────────

@bot.command(name="lockdown")
@commands.has_permissions(manage_channels=True)
async def lockdown(ctx):
    """Deny @everyone from sending messages in the current channel."""
    overwrite = ctx.channel.overwrites_for(ctx.guild.default_role)
    overwrite.send_messages = False
    await ctx.channel.set_permissions(ctx.guild.default_role, overwrite=overwrite)
    embed = discord.Embed(
        title="🔒 Channel Locked",
        description=f"{ctx.channel.mention} has been locked down.",
        color=discord.Color.red(),
    )
    await ctx.send(embed=embed)


@bot.command(name="unlock")
@commands.has_permissions(manage_channels=True)
async def unlock(ctx):
    """Allow @everyone to send messages in the current channel again."""
    overwrite = ctx.channel.overwrites_for(ctx.guild.default_role)
    overwrite.send_messages = None
    await ctx.channel.set_permissions(ctx.guild.default_role, overwrite=overwrite)
    embed = discord.Embed(
        title="🔓 Channel Unlocked",
        description=f"{ctx.channel.mention} has been unlocked.",
        color=discord.Color.green(),
    )
    await ctx.send(embed=embed)


@bot.command(name="serverinfo")
async def serverinfo(ctx):
    """Display information about the server."""
    guild = ctx.guild
    embed = discord.Embed(
        title=guild.name,
        description=guild.description or "No description.",
        color=discord.Color.blurple(),
    )
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    embed.add_field(name="Owner", value=str(guild.owner), inline=True)
    embed.add_field(name="Members", value=guild.member_count, inline=True)
    embed.add_field(name="Channels", value=len(guild.channels), inline=True)
    embed.add_field(name="Roles", value=len(guild.roles), inline=True)
    embed.add_field(name="Created", value=guild.created_at.strftime("%Y-%m-%d"), inline=True)
    await ctx.send(embed=embed)


@bot.command(name="userinfo")
async def userinfo(ctx, member: discord.Member = None):
    """Display information about a user."""
    member = member or ctx.author
    embed = discord.Embed(
        title=str(member),
        color=member.color,
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="ID", value=member.id, inline=True)
    embed.add_field(name="Nickname", value=member.nick or "None", inline=True)
    embed.add_field(name="Top Role", value=member.top_role.mention, inline=True)
    embed.add_field(name="Joined Server", value=member.joined_at.strftime("%Y-%m-%d"), inline=True)
    embed.add_field(name="Account Created", value=member.created_at.strftime("%Y-%m-%d"), inline=True)
    await ctx.send(embed=embed)


# ──────────────────────────────────────────────
# Anti-nuke system — Xieron-style protection
# ──────────────────────────────────────────────

# Per-guild config: {guild_id: {"enabled": bool, "whitelist": set[int]}}
_antinuke_config: dict[int, dict] = {}

# Recent action tracking: {guild_id: {user_id: {action_name: [unix_timestamps]}}}
_action_log: dict = collections.defaultdict(
    lambda: collections.defaultdict(lambda: collections.defaultdict(list))
)

# (max_count, time_window_seconds) for each watched audit log action
_AN_THRESHOLDS: dict[discord.AuditLogAction, tuple[int, int]] = {
    discord.AuditLogAction.ban:            (3, 10),
    discord.AuditLogAction.kick:           (3, 10),
    discord.AuditLogAction.channel_delete: (2, 10),
    discord.AuditLogAction.role_delete:    (2, 10),
    discord.AuditLogAction.webhook_create: (1, 10),
}


def _get_antinuke_cfg(guild_id: int) -> dict:
    if guild_id not in _antinuke_config:
        _antinuke_config[guild_id] = {"enabled": False, "whitelist": set()}
    return _antinuke_config[guild_id]


async def _punish_nuker(
    guild: discord.Guild,
    user: discord.abc.User,
    action_label: str,
) -> None:
    """Strip all roles from and ban the detected nuker, then log the event."""
    member = guild.get_member(user.id)
    if member is None:
        return
    # Never punish the server owner or the bot itself
    if member.id in (guild.owner_id, guild.me.id):
        return
    try:
        roles_to_strip = [
            r for r in member.roles
            if r != guild.default_role and r.is_assignable()
        ]
        if roles_to_strip:
            await member.remove_roles(
                *roles_to_strip, reason="[Anti-Nuke] Mass destructive action"
            )
        await guild.ban(
            member,
            reason="[Anti-Nuke] Mass destructive action detected",
            delete_message_days=0,
        )
    except discord.Forbidden:
        pass

    # Alert in mod-log / logs / system channel (whichever exists first)
    log_ch = (
        discord.utils.get(guild.text_channels, name="mod-log")
        or discord.utils.get(guild.text_channels, name="logs")
        or discord.utils.get(guild.text_channels, name="audit-log")
        or guild.system_channel
    )
    if log_ch:
        embed = discord.Embed(
            title="🚨 Anti-Nuke Triggered",
            description=(
                f"**User:** {user.mention} (`{user}`)\n"
                f"**Suspicious Action:** `{action_label}`\n"
                f"**Action Taken:** Roles stripped & banned"
            ),
            color=discord.Color.red(),
            timestamp=datetime.datetime.now(datetime.timezone.utc),
        )
        embed.set_footer(text="WondeX Anti-Nuke")
        try:
            await log_ch.send(embed=embed)
        except discord.Forbidden:
            pass


@bot.event
async def on_audit_log_entry_create(entry: discord.AuditLogEntry) -> None:
    """Monitor audit log entries and trigger anti-nuke when thresholds are exceeded."""
    guild = entry.guild
    cfg = _get_antinuke_cfg(guild.id)
    if not cfg["enabled"] or entry.action not in _AN_THRESHOLDS:
        return

    user = entry.user
    if user is None or user.id in (guild.owner_id, guild.me.id):
        return
    if user.id in cfg["whitelist"]:
        return

    limit, window = _AN_THRESHOLDS[entry.action]
    now = time.time()
    action_key = entry.action.name
    timestamps = _action_log[guild.id][user.id][action_key]

    # Prune entries outside the time window
    timestamps[:] = [t for t in timestamps if now - t < window]
    timestamps.append(now)

    if len(timestamps) >= limit:
        timestamps.clear()
        await _punish_nuker(guild, user, action_key)


# ─── Anti-nuke commands ────────────────────────

@bot.group(name="antinuke", invoke_without_command=True)
@commands.has_permissions(administrator=True)
async def antinuke_group(ctx):
    """Show anti-nuke status. Use subcommands: on, off, whitelist."""
    cfg = _get_antinuke_cfg(ctx.guild.id)
    status = "✅ Enabled" if cfg["enabled"] else "❌ Disabled"
    wl = ", ".join(f"<@{uid}>" for uid in cfg["whitelist"]) or "None"
    embed = discord.Embed(
        title="🛡️ Anti-Nuke Status",
        color=discord.Color.blurple(),
    )
    embed.add_field(name="Status", value=status, inline=False)
    embed.add_field(name="Whitelisted Users", value=wl, inline=False)
    embed.add_field(
        name="Thresholds",
        value=(
            "• **Ban / Kick:** 3 actions in 10 s\n"
            "• **Channel / Role Delete:** 2 actions in 10 s\n"
            "• **Webhook Create:** 1 in 10 s"
        ),
        inline=False,
    )
    await ctx.send(embed=embed)


@antinuke_group.command(name="on")
@commands.has_permissions(administrator=True)
async def antinuke_on(ctx):
    """Enable anti-nuke protection."""
    _get_antinuke_cfg(ctx.guild.id)["enabled"] = True
    await ctx.send("✅ Anti-nuke protection **enabled**.")


@antinuke_group.command(name="off")
@commands.has_permissions(administrator=True)
async def antinuke_off(ctx):
    """Disable anti-nuke protection."""
    _get_antinuke_cfg(ctx.guild.id)["enabled"] = False
    await ctx.send("❌ Anti-nuke protection **disabled**.")


@antinuke_group.command(name="whitelist")
@commands.has_permissions(administrator=True)
async def antinuke_whitelist(ctx, action: str, member: discord.Member):
    """Add or remove a user from the anti-nuke whitelist.

    Usage:
      Wa!antinuke whitelist add @user
      Wa!antinuke whitelist remove @user
    """
    cfg = _get_antinuke_cfg(ctx.guild.id)
    action = action.lower()
    if action == "add":
        cfg["whitelist"].add(member.id)
        await ctx.send(f"✅ {member.mention} added to the anti-nuke whitelist.")
    elif action in ("remove", "del"):
        cfg["whitelist"].discard(member.id)
        await ctx.send(f"✅ {member.mention} removed from the anti-nuke whitelist.")
    else:
        await ctx.send("❌ Invalid action. Use `add` or `remove`.")


# ──────────────────────────────────────────────
# Ticket system — Xieron-style button panel
# ──────────────────────────────────────────────

TICKET_CATEGORY_NAME = "Tickets"


class CloseClaimView(discord.ui.View):
    """Persistent view shown inside every ticket channel."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Close Ticket 🔒",
        style=discord.ButtonStyle.danger,
        custom_id="ticket:close",
    )
    async def close_ticket(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        embed = discord.Embed(
            title="Ticket Closed",
            description="This ticket has been closed and will be deleted in 5 seconds.",
            color=discord.Color.red(),
        )
        await interaction.response.send_message(embed=embed)
        await interaction.channel.delete(delay=5)

    @discord.ui.button(
        label="Claim 👋",
        style=discord.ButtonStyle.secondary,
        custom_id="ticket:claim",
    )
    async def claim_ticket(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if not interaction.user.guild_permissions.manage_channels:
            await interaction.response.send_message(
                "❌ Only staff can claim tickets.", ephemeral=True
            )
            return
        await interaction.channel.set_permissions(
            interaction.user,
            view_channel=True,
            send_messages=True,
            manage_messages=True,
        )
        await interaction.response.send_message(
            f"✅ {interaction.user.mention} has claimed this ticket."
        )
        button.disabled = True
        await interaction.message.edit(view=self)


class TicketPanelView(discord.ui.View):
    """Persistent view shown in the ticket panel channel."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Open Ticket 🎫",
        style=discord.ButtonStyle.primary,
        custom_id="ticket:open",
    )
    async def open_ticket(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        guild = interaction.guild
        user = interaction.user

        category = discord.utils.get(guild.categories, name=TICKET_CATEGORY_NAME)
        if not category:
            category = await guild.create_category(TICKET_CATEGORY_NAME)

        channel_name = f"ticket-{user.name.lower().replace(' ', '-')}"
        existing = discord.utils.get(category.channels, name=channel_name)
        if existing:
            await interaction.response.send_message(
                f"You already have an open ticket: {existing.mention}",
                ephemeral=True,
            )
            return

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            user: discord.PermissionOverwrite(view_channel=True, send_messages=True),
            guild.me: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, manage_channels=True
            ),
        }
        channel = await category.create_text_channel(channel_name, overwrites=overwrites)

        embed = discord.Embed(
            title="🎫 Support Ticket",
            description=(
                f"Welcome {user.mention}! Please describe your issue and staff will assist you shortly.\n\n"
                "Click **Close Ticket 🔒** when your issue is resolved.\n"
                "Staff can click **Claim 👋** to take ownership of this ticket."
            ),
            color=discord.Color.blurple(),
        )
        embed.set_footer(text=f"Ticket opened by {user}")
        await channel.send(embed=embed, view=CloseClaimView())

        await interaction.response.send_message(
            f"✅ Your ticket has been created: {channel.mention}", ephemeral=True
        )


@bot.command(name="ticketpanel")
@commands.has_permissions(manage_channels=True)
async def ticketpanel(ctx):
    """Post the ticket panel embed with an Open Ticket button."""
    embed = discord.Embed(
        title="🎫 Support Tickets",
        description=(
            "Need help or have a question? Click the button below to open a private support ticket.\n\n"
            "A dedicated channel will be created just for you."
        ),
        color=discord.Color.blurple(),
    )
    embed.set_footer(text=f"{ctx.guild.name} Support")
    await ctx.send(embed=embed, view=TicketPanelView())
    await ctx.message.delete()


# ──────────────────────────────────────────────
# Bot restart command
# ──────────────────────────────────────────────

@bot.command(name="restart")
@commands.has_permissions(administrator=True)
async def restart_bot(ctx):
    """Gracefully restart the bot (administrator only).

    Closes the current instance; the Auto-restart workflow will
    immediately spin up a fresh one.
    """
    embed = discord.Embed(
        title="🔄 Restarting...",
        description="The bot is shutting down and will restart automatically in a few seconds.",
        color=discord.Color.orange(),
    )
    await ctx.send(embed=embed)
    await bot.close()


# ──────────────────────────────────────────────
# Help command
# ──────────────────────────────────────────────

bot.remove_command("help")


@bot.command(name="help")
async def help_command(ctx):
    """Show all available commands."""
    embed = discord.Embed(
        title="WondeX Bot Commands",
        description="Prefix: `Wa!`",
        color=discord.Color.blurple(),
    )
    embed.add_field(
        name="🔨 Moderation",
        value=(
            "`Wa!kick <member> [reason]`\n"
            "`Wa!ban <member> [reason]`\n"
            "`Wa!unban <user#tag>`\n"
            "`Wa!mute <member> [reason]`\n"
            "`Wa!unmute <member>`\n"
            "`Wa!timeout <member> <minutes> [reason]`\n"
            "`Wa!untimeout <member>`\n"
            "`Wa!tempmute <member> <minutes> [reason]`\n"
            "`Wa!tempban <member> <minutes> [reason]`\n"
            "`Wa!warn <member> [reason]` — auto-mutes at 3, auto-bans at 5\n"
            "`Wa!warnings <member>`\n"
            "`Wa!clearwarns <member>`\n"
            "`Wa!purge <amount>`\n"
            "`Wa!nick <member> [nickname]`"
        ),
        inline=False,
    )
    embed.add_field(
        name="🛡️ Security",
        value=(
            "`Wa!lockdown` — lock current channel\n"
            "`Wa!unlock` — unlock current channel\n"
            "`Wa!slowmode [seconds]` — set channel slowmode\n"
            "`Wa!role add/remove @member @role`\n"
            "`Wa!serverinfo`\n"
            "`Wa!userinfo [member]`\n"
            "`Wa!setlogchannel [#channel]` — set mod-log channel"
        ),
        inline=False,
    )
    embed.add_field(
        name="🎫 Tickets",
        value=(
            "`Wa!ticketpanel` — post the ticket panel (staff only)\n"
            "Members click **Open Ticket 🎫** to create a private ticket\n"
            "Inside the ticket: **Close Ticket 🔒** or **Claim 👋**"
        ),
        inline=False,
    )
    embed.add_field(
        name="🛡️ Anti-Nuke",
        value=(
            "`Wa!antinuke` — show status & thresholds\n"
            "`Wa!antinuke on/off`\n"
            "`Wa!antinuke whitelist add/remove @user`\n\n"
            "**Protects:** mass ban, kick, channel/role delete, webhook spam"
        ),
        inline=False,
    )
    embed.add_field(
        name="🤖 Auto-Moderation",
        value=(
            "`Wa!antiinvite on/off` — delete Discord invite links\n"
            "`Wa!autorole set @role` — auto-assign role on join\n"
            "`Wa!autorole remove`\n\n"
            "**Always active when enabled:**\n"
            "• Anti-spam (5 msg / 5s → auto-mute)\n"
            "• Anti-raid (10 joins / 10s → full lockdown)"
        ),
        inline=False,
    )
    embed.add_field(
        name="⚙️ Bot Management",
        value="`Wa!restart` — gracefully restart the bot (admin only)",
        inline=False,
    )
    await ctx.send(embed=embed)


# ──────────────────────────────────────────────
# Error handling
# ──────────────────────────────────────────────

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ You don't have permission to use this command.")
    elif isinstance(error, commands.MemberNotFound):
        await ctx.send("❌ Member not found.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"❌ Missing argument: `{error.param.name}`")
    elif isinstance(error, commands.CommandNotFound):
        pass
    else:
        raise error


# ──────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────

if __name__ == "__main__":
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise RuntimeError(
            "DISCORD_TOKEN environment variable is not set. "
            "Add it to your GitHub repository secrets."
        )
    bot.run(token)
