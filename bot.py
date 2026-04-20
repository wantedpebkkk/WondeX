"""
WondeX Discord Bot
A moderation, security, and ticket bot for Discord servers.
"""

import os
import discord
from discord.ext import commands
from keep_alive import keep_alive

# ──────────────────────────────────────────────
# Bot configuration
# ──────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# ──────────────────────────────────────────────
# Events
# ──────────────────────────────────────────────

@bot.event
async def on_ready():
    print(f"✅  Logged in as {bot.user} (ID: {bot.user.id})")
    await bot.change_presence(
        activity=discord.Activity(
            type=discord.ActivityType.watching,
            name="over the server 🛡️",
        )
    )


@bot.event
async def on_member_join(member: discord.Member):
    """Send a welcome message when a new member joins."""
    channel = discord.utils.get(member.guild.text_channels, name="general")
    if channel:
        embed = discord.Embed(
            title=f"Welcome to {member.guild.name}! 🎉",
            description=f"Hey {member.mention}, welcome aboard! Please read the rules.",
            color=discord.Color.green(),
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        await channel.send(embed=embed)


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
    """Warn a member via DM and log in the channel."""
    try:
        await member.send(
            f"⚠️ You have been warned in **{ctx.guild.name}**.\n**Reason:** {reason}"
        )
    except discord.Forbidden:
        pass

    embed = discord.Embed(
        title="Member Warned",
        description=f"**{member}** has been warned.\n**Reason:** {reason}",
        color=discord.Color.yellow(),
    )
    await ctx.send(embed=embed)


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
    embed.set_thumbnail(url=guild.icon.url if guild.icon else discord.Embed.Empty)
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
# Ticket system
# ──────────────────────────────────────────────

TICKET_CATEGORY_NAME = "Tickets"


@bot.command(name="ticket")
async def ticket(ctx, *, topic: str = "Support"):
    """Open a new support ticket channel."""
    guild = ctx.guild
    category = discord.utils.get(guild.categories, name=TICKET_CATEGORY_NAME)
    if not category:
        category = await guild.create_category(TICKET_CATEGORY_NAME)

    channel_name = f"ticket-{ctx.author.name.lower().replace(' ', '-')}"
    existing = discord.utils.get(category.channels, name=channel_name)
    if existing:
        await ctx.send(f"You already have an open ticket: {existing.mention}")
        return

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        ctx.author: discord.PermissionOverwrite(view_channel=True, send_messages=True),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True),
    }
    channel = await category.create_text_channel(channel_name, overwrites=overwrites)

    embed = discord.Embed(
        title=f"Ticket: {topic}",
        description=(
            f"Hello {ctx.author.mention}! Staff will assist you shortly.\n"
            "Use `!closeticket` when your issue is resolved."
        ),
        color=discord.Color.blurple(),
    )
    await channel.send(embed=embed)
    await ctx.send(f"✅ Your ticket has been created: {channel.mention}")


@bot.command(name="closeticket")
async def closeticket(ctx):
    """Close the current ticket channel."""
    if (
        ctx.channel.category
        and ctx.channel.category.name == TICKET_CATEGORY_NAME
    ):
        embed = discord.Embed(
            title="Ticket Closed",
            description="This ticket has been closed and will be deleted shortly.",
            color=discord.Color.red(),
        )
        await ctx.send(embed=embed)
        await ctx.channel.delete(delay=5)
    else:
        await ctx.send("This command can only be used inside a ticket channel.")


# ──────────────────────────────────────────────
# Help command
# ──────────────────────────────────────────────

bot.remove_command("help")


@bot.command(name="help")
async def help_command(ctx):
    """Show all available commands."""
    embed = discord.Embed(
        title="WondeX Bot Commands",
        description="Prefix: `!`",
        color=discord.Color.blurple(),
    )
    embed.add_field(
        name="🔨 Moderation",
        value=(
            "`!kick <member> [reason]`\n"
            "`!ban <member> [reason]`\n"
            "`!unban <user#tag>`\n"
            "`!mute <member> [reason]`\n"
            "`!unmute <member>`\n"
            "`!warn <member> [reason]`\n"
            "`!purge <amount>`"
        ),
        inline=False,
    )
    embed.add_field(
        name="🛡️ Security",
        value=(
            "`!lockdown`\n"
            "`!unlock`\n"
            "`!serverinfo`\n"
            "`!userinfo [member]`"
        ),
        inline=False,
    )
    embed.add_field(
        name="🎫 Tickets",
        value=(
            "`!ticket [topic]`\n"
            "`!closeticket`"
        ),
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
    keep_alive()
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise RuntimeError(
            "DISCORD_TOKEN environment variable is not set. "
            "Add it to your GitHub repository secrets."
        )
    bot.run(token)
