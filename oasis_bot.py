"""
OASIS Discord bot - runs 24/7 and replaces PeakBot.

Features:
  - Verification button (gives Verified Player + Member, welcomes the player in #general)
  - Ping-role buttons (Update / Event / Giveaway / Sneak Peek)
  - Tickets (private channel, close button, transcript saved to #mod-log)
  - Automatic post when the OASIS game on Roblox gets updated
  - Logs: joins, leaves, deleted and edited messages
  - XP and levels (/rank, /leaderboard) with Sprout / Gardener / Oasis Master rewards
  - Join to Create voice rooms

Run:  python oasis_bot.py   (settings in .env, see .env.example)
"""
import asyncio
import datetime
import io
import json
import os
import random
import sqlite3
import sys
import time

import aiohttp
import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
load_dotenv(ENV_PATH)

# ============================== CONFIG ======================================
TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID", "0"))
DB_PATH = os.getenv("DB_PATH", "oasis_bot.db")

# Self-update from GitHub (/botupdate). Repo like "username/oasis-bot".
GITHUB_REPO = os.getenv("GITHUB_REPO", "")
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "main")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")          # only needed for a private repo
UPDATE_FILES = ["oasis_bot.py", "requirements.txt"]

PLACE_ID = 86503481593300
GAME_LINK = "https://www.roblox.com/games/86503481593300/oasis-trading"

# Channel names - must match the setup script
CH = {
    "verify":  "✅・verify",
    "roles":   "🎭・get-roles",
    "ticket":  "🎫・create-ticket",
    "general": "💬・general",
    "modlog":  "📋・mod-log",
    "updates": "📜・updates",
    "jtc":     "➕・Join to Create",
    "staff":   "🔒・staff-chat",
}
SUPPORT_CATEGORY = "🎫 SUPPORT"

STAFF = ["Owner", "Developer", "Admin", "Moderator"]
TICKET_PING_ROLE = "Moderator"
VERIFIED_ROLES = ["Verified Player", "Member"]
PING_ROLES = [  # (role, emoji)
    ("Update Ping", "🛠️"),
    ("Event Ping", "☀️"),
    ("Giveaway Ping", "🎁"),
    ("Sneak Peek Ping", "👀"),
]
TICKET_REASONS = [("Purchase problem", "💳"), ("Data loss", "💾"), ("Report a player", "🚩"),
                  ("Other", "❓")]

# XP (same curve as most level bots)
XP_PER_MESSAGE = (15, 25)
XP_COOLDOWN = 60                       # seconds between messages that give XP
LEVEL_ROLES = {5: "Sprout", 10: "Gardener", 25: "Oasis Master"}

# Roblox update watcher
UPDATE_CHECK_MINUTES = 5
UPDATE_MIN_GAP_HOURS = 6               # max one post per 6 h, so test publishes don't spam
UPDATE_PING_ROLE = "Update Ping"

SAND = 0xE8B04B
GREEN = 0x2ECC71
RED = 0xE74C3C
# ============================ END CONFIG ====================================

PO = discord.PermissionOverwrite

# ---------------- storage ----------------
db = sqlite3.connect(DB_PATH)
db.execute("CREATE TABLE IF NOT EXISTS xp (user_id INTEGER PRIMARY KEY, xp INTEGER NOT NULL DEFAULT 0,"
           " last INTEGER NOT NULL DEFAULT 0)")
db.execute("CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT)")
db.commit()


def kv_get(k, default=None):
    row = db.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
    return row[0] if row else default


def kv_set(k, v):
    db.execute("INSERT INTO kv(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))
    db.commit()


# ---------------- helpers ----------------
def channel(guild, key):
    return discord.utils.get(guild.channels, name=CH[key])


def role(guild, name):
    return discord.utils.get(guild.roles, name=name)


def is_staff(member):
    return member.guild_permissions.administrator or any(r.name in STAFF for r in member.roles)


async def log(guild, embed, file=None):
    ch = channel(guild, "modlog")
    if ch:
        await ch.send(embed=embed, file=file)


def xp_for_next(level):
    return 5 * level * level + 50 * level + 100


def level_from_xp(xp):
    level = 0
    while xp >= xp_for_next(level):
        xp -= xp_for_next(level)
        level += 1
    return level, xp   # level, xp into the current level


# ---------------- verification ----------------
class VerifyView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Verify", emoji="✅", style=discord.ButtonStyle.success,
                       custom_id="oasis:verify")
    async def verify(self, interaction: discord.Interaction, _):
        member = interaction.user
        roles = [r for r in (role(interaction.guild, n) for n in VERIFIED_ROLES) if r]
        if roles and all(r in member.roles for r in roles):
            return await interaction.response.send_message("You're already verified! 🌴", ephemeral=True)
        await member.add_roles(*roles, reason="Verified")
        await interaction.response.send_message("You're verified - welcome to OASIS! 🌴", ephemeral=True)
        general = channel(interaction.guild, "general")
        if general:
            await general.send(embed=discord.Embed(
                description=f"🌴 Welcome to the oasis, {member.mention}! Grab your ping roles in "
                            f"{channel(interaction.guild, 'roles').mention if channel(interaction.guild, 'roles') else '#get-roles'} "
                            f"and [jump into the game]({GAME_LINK})!", colour=SAND))


# ---------------- ping roles ----------------
class RolesView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        for name, emoji in PING_ROLES:
            button = discord.ui.Button(label=name, emoji=emoji, style=discord.ButtonStyle.secondary,
                                       custom_id=f"oasis:role:{name}")
            button.callback = self._toggle(name)
            self.add_item(button)

    @staticmethod
    def _toggle(name):
        async def callback(interaction: discord.Interaction):
            r = role(interaction.guild, name)
            if r is None:
                return await interaction.response.send_message("That role doesn't exist.", ephemeral=True)
            if r in interaction.user.roles:
                await interaction.user.remove_roles(r, reason="Role picker")
                msg = f"Removed **{name}** 🔕"
            else:
                await interaction.user.add_roles(r, reason="Role picker")
                msg = f"Added **{name}** 🔔"
            await interaction.response.send_message(msg, ephemeral=True)
        return callback


# ---------------- tickets ----------------
def ticket_owner_id(ch):
    if ch.topic and "ticket-owner:" in ch.topic:
        try:
            return int(ch.topic.split("ticket-owner:")[1].split()[0])
        except ValueError:
            return None
    return None


class TicketPanel(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.select(custom_id="oasis:ticket", placeholder="Choose what you need help with...",
                       options=[discord.SelectOption(label=l, emoji=e) for l, e in TICKET_REASONS])
    async def pick(self, interaction: discord.Interaction, select: discord.ui.Select):
        await open_ticket(interaction, select.values[0])


async def open_ticket(interaction: discord.Interaction, reason: str):
    guild, user = interaction.guild, interaction.user
    for ch in guild.text_channels:
        if ticket_owner_id(ch) == user.id:
            return await interaction.response.send_message(
                f"You already have an open ticket: {ch.mention}", ephemeral=True)
    await interaction.response.defer(ephemeral=True, thinking=True)

    number = int(kv_get("ticket_count", 0)) + 1
    kv_set("ticket_count", number)
    overwrites = {
        guild.default_role: PO(view_channel=False),
        user: PO(view_channel=True, send_messages=True, attach_files=True, read_message_history=True),
        guild.me: PO(view_channel=True, send_messages=True, manage_channels=True),
    }
    for name in STAFF:
        r = role(guild, name)
        if r:
            overwrites[r] = PO(view_channel=True, send_messages=True, read_message_history=True)
    ch = await guild.create_text_channel(
        f"ticket-{number:04d}", category=discord.utils.get(guild.categories, name=SUPPORT_CATEGORY),
        overwrites=overwrites, topic=f"{reason} | ticket-owner:{user.id}")

    ping = role(guild, TICKET_PING_ROLE)
    embed = discord.Embed(
        title=f"🎫 Ticket #{number:04d} - {reason}",
        description="Thanks for reaching out! Describe your problem here and staff will reply soon.\n\n"
                    "**Please include:** your Roblox username, what happened, and screenshots if you can "
                    "(for purchases: a screenshot of the transaction).",
        colour=SAND)
    await ch.send(content=f"{user.mention} {ping.mention if ping else ''}", embed=embed,
                  view=CloseTicketView(),
                  allowed_mentions=discord.AllowedMentions(users=True, roles=[ping] if ping else []))
    await interaction.followup.send(f"Your ticket is open: {ch.mention}", ephemeral=True)
    await log(guild, discord.Embed(description=f"🎫 {user.mention} opened {ch.mention} ({reason})",
                                   colour=SAND))


class CloseTicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Close ticket", emoji="🔒", style=discord.ButtonStyle.danger,
                       custom_id="oasis:ticket_close")
    async def close(self, interaction: discord.Interaction, _):
        ch = interaction.channel
        owner = ticket_owner_id(ch)
        if not (is_staff(interaction.user) or interaction.user.id == owner):
            return await interaction.response.send_message("You can't close this ticket.", ephemeral=True)
        await interaction.response.send_message("🔒 Closing this ticket in 5 seconds...")

        lines = []
        async for m in ch.history(limit=None, oldest_first=True):
            extra = " ".join(a.url for a in m.attachments)
            text = m.content or " ".join(e.title or "" for e in m.embeds)
            lines.append(f"[{m.created_at:%Y-%m-%d %H:%M}] {m.author}: {text} {extra}".rstrip())
        transcript = discord.File(io.BytesIO("\n".join(lines).encode("utf-8")), filename=f"{ch.name}.txt")
        await log(interaction.guild, discord.Embed(
            description=f"🔒 **{ch.name}** closed by {interaction.user.mention} "
                        f"(opened by <@{owner}>, {ch.topic.split(' | ')[0] if ch.topic else ''})",
            colour=RED), file=transcript)
        await asyncio.sleep(5)
        await ch.delete(reason="Ticket closed")


# ---------------- panels ----------------
async def ensure_panel(guild, key, embed, view):
    ch = channel(guild, key)
    if ch is None:
        print(f"! channel {CH[key]} not found - run the setup script first")
        return
    msg_id = kv_get(f"panel:{key}")
    if msg_id:
        try:
            await ch.fetch_message(int(msg_id))
            return
        except discord.NotFound:
            pass
    msg = await ch.send(embed=embed, view=view)
    kv_set(f"panel:{key}", msg.id)
    print(f"+ posted panel in {ch.name}")


async def post_panels(guild):
    await ensure_panel(guild, "verify", discord.Embed(
        title="✅ Verify to enter the oasis",
        description="Click the button below to unlock all OASIS channels.", colour=GREEN), VerifyView())
    await ensure_panel(guild, "roles", discord.Embed(
        title="🎭 Notification roles",
        description="Click a button to turn a ping on or off:\n\n"
                    "🛠️ **Update Ping** - new game updates\n☀️ **Event Ping** - weather events and seasons\n"
                    "🎁 **Giveaway Ping** - giveaways\n👀 **Sneak Peek Ping** - early previews",
        colour=SAND), RolesView())
    await ensure_panel(guild, "ticket", discord.Embed(
        title="🎫 Need help?",
        description="Pick a topic below and a private ticket channel will open just for you and the staff.",
        colour=SAND), TicketPanel())


# ---------------- Join to Create ----------------
def temp_rooms():
    return set(json.loads(kv_get("temp_vc", "[]")))


def save_temp_rooms(ids):
    kv_set("temp_vc", json.dumps(sorted(ids)))


# ---------------- bot ----------------
intents = discord.Intents.default()
intents.members = True
intents.message_content = True


class OasisBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        for view in (VerifyView(), RolesView(), TicketPanel(), CloseTicketView()):
            self.add_view(view)
        guild = discord.Object(id=GUILD_ID)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)
        check_game_update.start()


bot = OasisBot()


def our_guild(guild):
    return guild is not None and guild.id == GUILD_ID


@bot.event
async def on_ready():
    guild = bot.get_guild(GUILD_ID)
    if guild is None:
        print("Bot is not in the server from GUILD_ID.")
        return
    await post_panels(guild)
    # clean up voice rooms left empty while the bot was offline
    rooms = temp_rooms()
    for cid in list(rooms):
        ch = guild.get_channel(cid)
        if ch is None or not ch.members:
            if ch:
                await ch.delete()
            rooms.discard(cid)
    save_temp_rooms(rooms)
    notice = kv_get("restart_notice")
    if notice:
        kv_set("restart_notice", "")
        ch = guild.get_channel(int(notice))
        if ch:
            await ch.send(kv_get("restart_text", "✅ Bot is back online."))
    print(f"OASIS bot online as {bot.user} in {guild.name}")


# ---- logs ----
@bot.event
async def on_member_join(member):
    if our_guild(member.guild):
        await log(member.guild, discord.Embed(
            description=f"📥 {member.mention} joined (account created "
                        f"{discord.utils.format_dt(member.created_at, 'R')})", colour=GREEN))


@bot.event
async def on_member_remove(member):
    if our_guild(member.guild):
        await log(member.guild, discord.Embed(description=f"📤 **{member}** left", colour=RED))


@bot.event
async def on_message_delete(message):
    if not our_guild(message.guild) or message.author.bot or message.channel.name.startswith("ticket-"):
        return
    await log(message.guild, discord.Embed(
        description=f"🗑️ Message by {message.author.mention} deleted in {message.channel.mention}\n"
                    f">>> {message.content[:1000] or '*(no text)*'}", colour=RED))


@bot.event
async def on_message_edit(before, after):
    if not our_guild(after.guild) or after.author.bot or before.content == after.content:
        return
    await log(after.guild, discord.Embed(
        description=f"✏️ {after.author.mention} edited a message in {after.channel.mention} "
                    f"([jump]({after.jump_url}))\n**Before:** {before.content[:900]}\n"
                    f"**After:** {after.content[:900]}", colour=SAND))


# ---- XP ----
@bot.event
async def on_message(message):
    if not our_guild(message.guild) or message.author.bot or message.channel.name.startswith("ticket-"):
        return
    uid, now = message.author.id, int(time.time())
    row = db.execute("SELECT xp, last FROM xp WHERE user_id=?", (uid,)).fetchone()
    xp, last = row if row else (0, 0)
    if now - last < XP_COOLDOWN:
        return
    old_level, _ = level_from_xp(xp)
    xp += random.randint(*XP_PER_MESSAGE)
    db.execute("INSERT INTO xp(user_id, xp, last) VALUES(?,?,?) ON CONFLICT(user_id) "
               "DO UPDATE SET xp=excluded.xp, last=excluded.last", (uid, xp, now))
    db.commit()
    new_level, _ = level_from_xp(xp)
    if new_level > old_level:
        await message.channel.send(f"🎉 {message.author.mention} reached **level {new_level}**!")
        reward = max((lv for lv in LEVEL_ROLES if lv <= new_level), default=None)
        if reward is not None:
            keep = role(message.guild, LEVEL_ROLES[reward])
            remove = [r for r in (role(message.guild, n) for n in LEVEL_ROLES.values())
                      if r and r != keep and r in message.author.roles]
            if remove:
                await message.author.remove_roles(*remove, reason="Level up")
            if keep and keep not in message.author.roles:
                await message.author.add_roles(keep, reason=f"Reached level {reward}")


@bot.tree.command(name="rank", description="Show your OASIS level")
@app_commands.describe(member="Whose level to show")
async def rank(interaction: discord.Interaction, member: discord.Member | None = None):
    member = member or interaction.user
    row = db.execute("SELECT xp FROM xp WHERE user_id=?", (member.id,)).fetchone()
    total = row[0] if row else 0
    level, into = level_from_xp(total)
    need = xp_for_next(level)
    pos = db.execute("SELECT COUNT(*) FROM xp WHERE xp > ?", (total,)).fetchone()[0] + 1
    bar = "🟩" * int(10 * into / need) + "⬜" * (10 - int(10 * into / need))
    embed = discord.Embed(title=f"🌵 {member.display_name}",
                          description=f"**Level {level}** · Rank #{pos}\n{bar}\n{into} / {need} XP",
                          colour=SAND)
    embed.set_thumbnail(url=member.display_avatar.url)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="leaderboard", description="Top 10 most active players")
async def leaderboard(interaction: discord.Interaction):
    rows = db.execute("SELECT user_id, xp FROM xp ORDER BY xp DESC LIMIT 10").fetchall()
    medals = ["🥇", "🥈", "🥉"] + ["🌴"] * 7
    text = "\n".join(f"{medals[i]} <@{uid}> - level {level_from_xp(x)[0]} ({x} XP)"
                     for i, (uid, x) in enumerate(rows)) or "Nobody has XP yet!"
    await interaction.response.send_message(
        embed=discord.Embed(title="🏆 OASIS Leaderboard", description=text, colour=SAND),
        allowed_mentions=discord.AllowedMentions.none())


class UpdateModal(discord.ui.Modal, title="Post OASIS patch notes"):
    headline = discord.ui.TextInput(label="Title", placeholder="Update 1.2 - Dragon Fruit is here!",
                                    max_length=200)
    changes = discord.ui.TextInput(label="What changed (one change per line)",
                                   style=discord.TextStyle.paragraph, max_length=3500,
                                   placeholder="New plant: Dragon Fruit\nFixed sell panel overlap\n...")
    image = discord.ui.TextInput(label="Image link (optional)", required=False,
                                 placeholder="https://...png")

    async def on_submit(self, interaction: discord.Interaction):
        ch = channel(interaction.guild, "updates")
        if ch is None:
            return await interaction.response.send_message("Updates channel not found.", ephemeral=True)
        lines = [l.strip(" -•*") for l in self.changes.value.splitlines() if l.strip()]
        embed = discord.Embed(
            title=f"🛠️ {self.headline.value}",
            description="\n".join(f"• {l}" for l in lines) + f"\n\n🎮 **[Play OASIS]({GAME_LINK})**",
            colour=SAND, timestamp=datetime.datetime.now(datetime.timezone.utc))
        if self.image.value.startswith("http"):
            embed.set_image(url=self.image.value)
        ping = role(interaction.guild, UPDATE_PING_ROLE)
        msg = await ch.send(content=ping.mention if ping else None, embed=embed,
                            allowed_mentions=discord.AllowedMentions(roles=[ping] if ping else []))
        if ch.is_news():
            try:
                await msg.publish()
            except discord.HTTPException:
                pass
        kv_set("game_update_posted", time.time())   # blocks the generic auto post for 6 h
        await interaction.response.send_message(f"Posted in {ch.mention}! ✅", ephemeral=True)


@bot.tree.command(name="update", description="Post patch notes in the updates channel")
@app_commands.default_permissions(manage_guild=True)
async def update(interaction: discord.Interaction):
    await interaction.response.send_modal(UpdateModal())


BOT_DIR = os.path.dirname(os.path.abspath(__file__))


async def github_get(session, url, raw=True):
    headers = {"Accept": "application/vnd.github.raw" if raw else "application/vnd.github+json"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    async with session.get(url, headers=headers) as r:
        if r.status != 200:
            raise RuntimeError(f"GitHub answered {r.status} for {url.split('/repos/')[1]}")
        return await r.text() if raw else await r.json()


@bot.tree.command(name="botupdate", description="Download the newest bot code from GitHub and restart")
@app_commands.default_permissions(administrator=True)
async def botupdate(interaction: discord.Interaction):
    if not GITHUB_REPO:
        return await interaction.response.send_message("Set GITHUB_REPO in .env first.", ephemeral=True)
    await interaction.response.defer(ephemeral=True, thinking=True)
    api = f"https://api.github.com/repos/{GITHUB_REPO}"
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as s:
            files = {f: await github_get(s, f"{api}/contents/{f}?ref={GITHUB_BRANCH}") for f in UPDATE_FILES}
            commit = await github_get(s, f"{api}/commits/{GITHUB_BRANCH}", raw=False)
    except Exception as e:
        return await interaction.followup.send(f"❌ Download failed: {e}")

    try:
        compile(files["oasis_bot.py"], "oasis_bot.py", "exec")    # never install broken code
    except SyntaxError as e:
        return await interaction.followup.send(f"❌ New code has an error (line {e.lineno}) - not installed.")

    changed = []
    for name, text in files.items():
        path = os.path.join(BOT_DIR, name)
        old = open(path, encoding="utf-8").read() if os.path.exists(path) else ""
        if old != text:
            if old:
                with open(path + ".bak", "w", encoding="utf-8") as f:
                    f.write(old)
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            changed.append(name)
    if not changed:
        return await interaction.followup.send("✅ Already up to date.")

    if "requirements.txt" in changed:
        import subprocess
        await asyncio.to_thread(subprocess.run, [sys.executable, "-m", "pip", "install", "-q", "-r",
                                                 os.path.join(BOT_DIR, "requirements.txt")])
    message = commit["commit"]["message"].splitlines()[0]
    kv_set("restart_notice", interaction.channel_id)
    kv_set("restart_text", f"✅ Bot updated and back online: **{message}**")
    await interaction.followup.send(f"⬇️ Updated {', '.join(changed)} ({message}). Restarting...")
    os.execv(sys.executable, [sys.executable] + sys.argv)


@bot.tree.command(name="panels", description="Re-post the verify, roles and ticket panels")
@app_commands.default_permissions(administrator=True)
async def panels(interaction: discord.Interaction):
    for key in ("verify", "roles", "ticket"):
        kv_set(f"panel:{key}", "")
    await interaction.response.send_message("Re-posting panels...", ephemeral=True)
    await post_panels(interaction.guild)


# ---- Join to Create ----
@bot.event
async def on_voice_state_update(member, before, after):
    if not our_guild(member.guild):
        return
    rooms = temp_rooms()
    if after.channel and after.channel.name == CH["jtc"]:
        overwrites = dict(after.channel.overwrites)
        overwrites[member] = PO(view_channel=True, connect=True, manage_channels=True,
                                move_members=True, mute_members=True)
        room = await member.guild.create_voice_channel(
            f"🌵 {member.display_name}'s oasis", category=after.channel.category, overwrites=overwrites)
        rooms.add(room.id)
        save_temp_rooms(rooms)
        await member.move_to(room)
    if before.channel and before.channel.id in rooms and not before.channel.members:
        rooms.discard(before.channel.id)
        save_temp_rooms(rooms)
        await before.channel.delete(reason="Empty Join to Create room")


# ---- Roblox update watcher ----
@tasks.loop(minutes=UPDATE_CHECK_MINUTES)
async def check_game_update():
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s:
            universe = kv_get("universe_id")
            if not universe:
                async with s.get(f"https://apis.roblox.com/universes/v1/places/{PLACE_ID}/universe") as r:
                    universe = (await r.json())["universeId"]
                kv_set("universe_id", universe)
            async with s.get(f"https://games.roblox.com/v1/games?universeIds={universe}") as r:
                game = (await r.json())["data"][0]
    except Exception as e:      # Roblox API down or rate limited - try again next loop
        print(f"! update check failed: {e}")
        return

    updated, last = game["updated"], kv_get("game_updated")
    if last == updated:
        return
    kv_set("game_updated", updated)
    if last is None:            # first run: just remember the current version
        return
    if time.time() - float(kv_get("game_update_posted", 0)) < UPDATE_MIN_GAP_HOURS * 3600:
        return

    guild = bot.get_guild(GUILD_ID)
    ch = channel(guild, "updates") if guild else None
    if ch is None:
        return
    ping = role(guild, UPDATE_PING_ROLE)
    embed = discord.Embed(
        title="🛠️ OASIS just got updated!",
        description=f"A new version of **{game['name']}** is live.\n"
                    f"Rejoin the game to get the newest version!\n\n🎮 **[Play OASIS]({GAME_LINK})**",
        colour=SAND, timestamp=datetime.datetime.now(datetime.timezone.utc))
    msg = await ch.send(content=ping.mention if ping else None, embed=embed,
                        allowed_mentions=discord.AllowedMentions(roles=[ping] if ping else []))
    if ch.is_news():
        try:
            await msg.publish()
        except discord.HTTPException:
            pass
    kv_set("game_update_posted", time.time())
    staff = discord.utils.get(guild.channels, name=CH["staff"])
    if staff:
        await staff.send("🛠️ OASIS was updated and a short notice went to the updates channel. "
                         "Want proper patch notes? Use `/update` (no extra ping for 6 h).")


@check_game_update.before_loop
async def _wait_ready():
    await bot.wait_until_ready()


if __name__ == "__main__":
    if not TOKEN or not GUILD_ID:
        folder = os.path.dirname(ENV_PATH)
        print(f"Looking for .env at: {ENV_PATH}")
        print(f".env found: {os.path.isfile(ENV_PATH)}")
        print(f"Files in this folder: {sorted(os.listdir(folder))}")
        print(f"DISCORD_TOKEN set: {bool(TOKEN)} | GUILD_ID set: {bool(GUILD_ID)}")
        raise SystemExit("Set DISCORD_TOKEN and GUILD_ID in the .env file (or in the panel's environment variables).")
    bot.run(TOKEN)
