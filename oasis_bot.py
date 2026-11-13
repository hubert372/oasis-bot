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
  - Live player counter channel, 👍/👎 on suggestions, "fixed" notice on bug reports,
    auto-close of inactive tickets, /giveaway, picture-only mutation-showcase

Run:  python oasis_bot.py   (settings in .env, see .env.example)
"""
import asyncio
import datetime
import io
import json
import os
import random
import re
import sqlite3
import unicodedata
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
    "welcome": "🌵・welcome",
    "verify":  "✅・verify",
    "roles":   "🎭・get-roles",
    "ticket":  "🎫・create-ticket",
    "general": "💬・general",
    "modlog":  "📋・mod-log",
    "updates": "📜・updates",
    "announcements": "📢・announcements",
    "codes":   "🎟️・codes",
    "tracker": "👀・player-tracker",
    "jtc":     "➕・Join to Create",
    "staff":   "🔒・staff-chat",
    "showcase": "✨・mutation-showcase",
    "suggestions": "💡・suggestions",
    "bugs":    "🐛・bug-reports",
    "giveaways": "🎉・giveaways",
    "control": "🤖・bot-control",
    "joins":   "👋・new-players",
}
CONTROL_CATEGORY = "🛠️ STAFF"

# In-game player tracker (owner-only channel). Roblox gives no names from outside the game,
# so players are recognised by their avatar picture.
TRACKER_IGNORE_USER_IDS = [507897662]     # Roblox user IDs that never trigger a ping (the owner)
TRACKER_POLL_SECONDS = 60
TRACKER_MISS_LIMIT = 2                    # checks in a row a player must be missing to count as "left"
TRACKER_MAX_PLAYERS = 30                  # above this the tracker stays quiet (it would be spam)
CONTROL_ROLE = "Owner"
BOT_ROLE = ("Bot", 0x5865F2)    # every bot on the server gets this rank, shown separately in the member list          # only this role (and the bot) can see the control channel
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

# Live player counter (channel at the top of the server, renamed every 10 min)
PLAYER_COUNT_PREFIX = "🎮 Playing now"
# Tickets: when staff replied and the player is silent this long, the bot pings them with a countdown;
# if they still don't answer, the ticket closes. Any reply from the player resets the timer.
TICKET_REMIND_AFTER_HOURS = 24
TICKET_CLOSE_AFTER_HOURS = 24       # counted from the reminder

# ---- Own AutoMod (staff are exempt) ----
# Words are checked after cleaning tricks like "f.u.c.k", "fuuuck", "sh1t", "b!tch".
BAD_CONTAINS = ("fuck", "cunt", "nigg", "fagg", "whore", "bitch", "motherf")   # anywhere in a word
BAD_PREFIX = ("shit", "slut", "retard", "porn", "bastard", "asshole", "dickhead", "wank", "twat",
              "penis", "vagina", "rapist", "dumbass", "jackass")                  # start of a word
BAD_EXACT = {"ass", "dick", "cock", "pussy", "fag", "sex", "nude", "nudes", "rape", "raped", "kys",
             "stfu", "cum", "boobs", "tits", "nigga", "damn", "wtf"}  # not "hoe" - a farming tool!  # whole word only
BAD_PHRASES = ("kill yourself", "kill urself", "kill your self", "go die")
SPAM_MESSAGES, SPAM_SECONDS = 5, 6        # 5 messages in 6 s = spam
SPAM_REPEATS = 3                          # the same message 3x in 20 s = spam
MENTION_LIMIT = 5                         # pinging 5+ people/roles in one message
INVITE_RE = re.compile(r"(discord\.gg|discord(app)?\.com/invite)/\S+", re.I)
DISABLE_DISCORD_AUTOMOD = True            # remove Discord's built-in AutoMod rules - the bot handles it
WARN_WINDOW_DAYS = 30                     # warnings older than this don't count toward punishments
WARN_PUNISHMENTS = {                      # active warnings -> timeout length
    2: datetime.timedelta(minutes=10),
    3: datetime.timedelta(hours=1),
    4: datetime.timedelta(days=1),
    5: datetime.timedelta(days=7),        # 5+ warnings: 7 days and a staff alert
}
GIVEAWAY_PING_ROLE = "Giveaway Ping"
SUGGESTION_REACTIONS = ["👍", "👎"]
FIXED_TAG = "Fixed"

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
db.execute("CREATE TABLE IF NOT EXISTS giveaways (message_id INTEGER PRIMARY KEY, channel_id INTEGER,"
           " prize TEXT, winners INTEGER, end_ts INTEGER, host_id INTEGER, ended INTEGER DEFAULT 0)")
db.execute("CREATE TABLE IF NOT EXISTS warns (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,"
           " mod_id INTEGER, reason TEXT, ts INTEGER)")
db.execute("CREATE TABLE IF NOT EXISTS codes (code TEXT PRIMARY KEY, reward TEXT, expires INTEGER,"
           " message_id INTEGER, active INTEGER DEFAULT 1)")
db.execute("CREATE TABLE IF NOT EXISTS gw_entries (message_id INTEGER, user_id INTEGER,"
           " PRIMARY KEY (message_id, user_id))")
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
        joins = await ensure_joins_channel(interaction.guild)
        if joins:
            await joins.send(embed=discord.Embed(
                description=f"🌴 Welcome to the oasis, {member.mention}! Grab your ping roles in "
                            f"{channel(interaction.guild, 'roles').mention if channel(interaction.guild, 'roles') else '#get-roles'} "
                            f"and [jump into the game]({GAME_LINK})!", colour=SAND))


async def ensure_joins_channel(guild):
    ch = channel(guild, "joins")
    if ch is None:
        verified = role(guild, "Verified Player")
        overwrites = {guild.default_role: PO(view_channel=False, send_messages=False),
                      guild.me: PO(view_channel=True, send_messages=True)}
        if verified:
            overwrites[verified] = PO(view_channel=True, send_messages=False, add_reactions=True)
        welcome = channel(guild, "welcome") if "welcome" in CH else None
        ch = await guild.create_text_channel(
            CH["joins"], overwrites=overwrites, topic="New players joining the oasis 🌴",
            category=welcome.category if welcome else None)
        if welcome:
            await ch.edit(position=welcome.position + 1)
        print(f"+ created {ch.name}")
    return ch


async def ensure_bot_role(guild):
    name, colour = BOT_ROLE
    r = role(guild, name)
    if r is None:
        r = await guild.create_role(name=name, colour=discord.Colour(colour), hoist=True,
                                    permissions=discord.Permissions.none(), reason="Rank for bots")
        mod = role(guild, "Moderator")
        try:
            await r.edit(position=max(1, mod.position - 1) if mod else 1)   # right below Moderator
        except discord.HTTPException:
            pass
        print(f"+ created role {name}")
    for m in guild.members:
        if m.bot and r not in m.roles and r < guild.me.top_role:
            await m.add_roles(r, reason="Bot rank")
    return r


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
                    "(for purchases: a screenshot of the transaction).\n\n"
                    f"⏰ If staff replies and you don't answer for {TICKET_REMIND_AFTER_HOURS} h, you'll get a "
                    f"reminder, and the ticket closes {TICKET_CLOSE_AFTER_HOURS} h after that.",
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
        if owner is None:
            return await interaction.response.send_message("This isn't a ticket.", ephemeral=True)
        if not (is_staff(interaction.user) or interaction.user.id == owner):
            return await interaction.response.send_message("You can't close this ticket.", ephemeral=True)
        await interaction.response.send_message("🔒 Closing this ticket in 5 seconds...")
        await close_ticket(ch, f"closed by {interaction.user.mention}")


async def close_ticket(ch, how):
    owner = ticket_owner_id(ch)
    lines = []
    async for m in ch.history(limit=None, oldest_first=True):
        extra = " ".join(a.url for a in m.attachments)
        text = m.content or " ".join(e.title or "" for e in m.embeds)
        lines.append(f"[{m.created_at:%Y-%m-%d %H:%M}] {m.author}: {text} {extra}".rstrip())
    transcript = discord.File(io.BytesIO("\n".join(lines).encode("utf-8")), filename=f"{ch.name}.txt")
    await log(ch.guild, discord.Embed(
        description=f"🔒 **{ch.name}** {how} "
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


CONTROL_HELP = [
    ("🔧 Komendy tylko dla ciebie (wpisuj tutaj)",
     "`/botupdate` - pobiera nowy kod bota z GitHuba i restartuje bota. Po restarcie kanał sam się czyści.\n"
     "`/panels` - wystawia od nowa panele weryfikacji, ról i ticketów (gdy ktoś je usunie).\n"
     "`/tracker on|off` - włącza/wyłącza śledzenie graczy w grze (#player-tracker: ping, gdy ktoś wejdzie "
     "do OASIS, i notatka z czasem gry, gdy wyjdzie)."),
    ("📢 Komendy staffu (działają na każdym kanale)",
     "`/announce ping` - okienko z ogłoszeniem (tytuł, treść, obrazek, przycisk Play); post idzie do "
     "#announcements z wybranym pingiem (@everyone, role Ping albo bez pingu).\n"
     "`/update` - okienko z patch notes; post idzie do #updates z pingiem Update Ping.\n"
     "`/code-add kod nagroda [wygasa] [ping]` - nowy kod w #codes, np. `/code-add SANDSTORM 500 coins 3d`\n"
     "`/code-remove kod [delete]` - kod dostaje dopisek EXPIRED (albo `delete: True` usuwa post)\n"
     "`/code-list` - wszystkie kody; kody z datą wygasają same\n"
     "`/giveaway nagroda czas [zwycięzcy]` - giveaway w #giveaways, czas np. `30m`, `2h`, `1d12h`.\n"
     "`/giveaway-end id_wiadomości` - kończy giveaway od razu, a po zakończeniu losuje nowego zwycięzcę.\n"
     "`/clear okres [gracz]` - czyści kanał, w którym to wpiszesz: `30m`, `2h`, `1d`, `7d` albo `all` "
     "(cały kanał, z potwierdzeniem). Przypięte wiadomości i panele zostają."),
    ("🛡️ Moderacja (Moderator i wyżej)",
     "`/warn gracz powód` - ostrzeżenie (gracz dostaje DM)\n"
     "`/warnings gracz` - lista ostrzeżeń, `/delwarn numer` - usuwa jedno, `/clearwarns gracz` - wszystkie (admin)\n"
     "`/mute gracz czas [powód]` - wyciszenie np. `10m`, `2h`, `1d`; `/unmute gracz`\n"
     "Kary za aktywne ostrzeżenia (z 30 dni): 2 = 10 min, 3 = 1 h, 4 = 1 dzień, 5+ = 7 dni i ping Moderatorów."),
    ("🌴 Komendy dla graczy",
     "`/rank` - poziom i XP, `/leaderboard` - top 10 najaktywniejszych."),
    ("⚙️ Działa samo",
     "• Licznik graczy na górze serwera (co 10 min)\n"
     "• Post w #updates, gdy opublikujesz nową wersję gry (max 1 na 6 h, `/update` go zastępuje)\n"
     "• Ticket: gdy staff odpisze, a gracz milczy 24 h - ping z licznikiem; po kolejnych 24 h ticket się zamyka\n"
     "• Własny AutoMod (zamiast AutoModa Discorda): przekleństwa po angielsku (też f.u.c.kk, f u c k, sh1t, fück), "
     "spam, zaproszenia na inne serwery, masowe pingi = usunięcie + ostrzeżenie\n"
     "• 👍/👎 pod każdą propozycją, autor buga dostaje info po tagu **Fixed**\n"
     "• #mutation-showcase przyjmuje tylko obrazki\n"
     "• XP i role za poziomy, Join to Create, ranga Bot dla każdego bota\n"
     "• Powitania nowych graczy w #new-players (nie na #general)\n"
     "• #mod-log: tylko usunięte/edytowane wiadomości, ostrzeżenia, wyciszenia, /clear i tickety"),
]


async def ensure_control_channel(guild):
    ch = channel(guild, "control")
    if ch is None:
        owner_role = role(guild, CONTROL_ROLE)
        overwrites = {guild.default_role: PO(view_channel=False),
                      guild.me: PO(view_channel=True, send_messages=True, manage_messages=True)}
        if owner_role:
            overwrites[owner_role] = PO(view_channel=True, send_messages=True)
        ch = await guild.create_text_channel(
            CH["control"], overwrites=overwrites, topic="Sterowanie botem OASIS - widzi to tylko Owner.",
            category=discord.utils.get(guild.categories, name=CONTROL_CATEGORY))
        print(f"+ created {ch.name}")
    embed = discord.Embed(title="🤖 OASIS Bot - panel sterowania", colour=SAND)
    for name, value in CONTROL_HELP:
        embed.add_field(name=name, value=value, inline=False)
    embed.set_footer(text=f"Wersja: {kv_get('version', 'pierwsze uruchomienie')}")
    msg_id = kv_get("panel:control")
    try:
        msg = await ch.fetch_message(int(msg_id)) if msg_id else None
    except discord.NotFound:
        msg = None
    if msg:
        await msg.edit(embed=embed)          # keeps the command list current after every update
    else:
        msg = await ch.send(embed=embed)
        await msg.pin()
        kv_set("panel:control", msg.id)
    return ch, msg.id


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
        for view in (VerifyView(), RolesView(), TicketPanel(), CloseTicketView(), GiveawayView()):
            self.add_view(view)
        guild = discord.Object(id=GUILD_ID)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)
        for loop in (check_game_update, player_counter, autoclose_tickets, giveaway_timer, expire_codes,
                     player_tracker):
            loop.start()


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
    control, help_id = await ensure_control_channel(guild)
    await ensure_joins_channel(guild)
    await ensure_codes_channel(guild)
    await ensure_tracker_channel(guild)
    await ensure_bot_role(guild)
    if DISABLE_DISCORD_AUTOMOD:
        for rule in await guild.fetch_automod_rules():
            await rule.delete(reason="OASIS bot runs its own AutoMod")
            print(f"- removed Discord AutoMod rule: {rule.name}")
    if kv_get("restart_notice"):
        kv_set("restart_notice", "")
        await control.purge(limit=200, check=lambda m: m.id != help_id)      # clean up after the update
        await control.send(kv_get("restart_text", "✅ Bot is back online."), delete_after=120)
    print(f"OASIS bot online as {bot.user} in {guild.name}")


# ---- Own AutoMod: bad words, spam, warnings, timeouts ----
LEET = str.maketrans({"0": "o", "1": "i", "!": "i", "|": "i", "3": "e", "4": "a", "@": "a",
                      "5": "s", "$": "s", "7": "t", "+": "t", "8": "b", "9": "g"})


LOOKALIKES = str.maketrans({"а": "a", "е": "e", "о": "o", "с": "c", "р": "p", "х": "x", "у": "y",
                            "і": "i", "к": "k", "м": "m", "т": "t", "н": "h", "в": "b", "ѕ": "s"})


def _variants(word):
    out = {word, re.sub(r"(.)\1+", r"\1", word)}           # "fuuuckk" -> "fuck"
    out |= {w.replace("ph", "f") for w in out}             # "phuck" -> "fuck"
    return out


def _is_bad(word, strict=True):
    for w in _variants(word):
        if w in BAD_EXACT or any(b in w for b in BAD_CONTAINS):
            return True
        if strict and w.startswith(BAD_PREFIX):
            return True
    return False


def bad_word_in(text):
    t = unicodedata.normalize("NFKD", text)
    t = "".join(c for c in t if not unicodedata.combining(c))  # "fück" -> "fuck"
    t = re.sub(r"[\u200b-\u200f\u2060\ufeff]", "", t)          # invisible characters
    t = t.lower().translate(LOOKALIKES).translate(LEET)
    tokens = re.findall(r"[a-z]+", t)
    if any(re.search(rf"\b{re.escape(p)}\b", " ".join(tokens)) for p in BAD_PHRASES):
        return True
    if any(_is_bad(tok) for tok in tokens):
        return True
    # pieces split by dots/spaces: "f.u.c.kk", "f u c k", "b i t c h"
    run = []
    for tok in tokens + [""]:
        if 0 < len(tok) <= 2:
            run.append(tok)
            continue
        if len(run) >= 2 and _is_bad("".join(run)):
            return True
        run = []
    # two neighbours where one is a short piece: "fuc k", "fu ck", "sh it"
    for a, b in zip(tokens, tokens[1:]):
        if (len(a) <= 2 or len(b) <= 2) and _is_bad(a + b, strict=False):
            return True
    return False


def active_warns(user_id):
    since = int(time.time()) - WARN_WINDOW_DAYS * 86400
    return db.execute("SELECT COUNT(*) FROM warns WHERE user_id=? AND ts>=?", (user_id, since)).fetchone()[0]


def fmt_delta(d):
    s = int(d.total_seconds())
    return f"{s // 86400} d" if s >= 86400 else f"{s // 3600} h" if s >= 3600 else f"{s // 60} min"


async def add_warn(member, reason, moderator=None):
    db.execute("INSERT INTO warns(user_id, mod_id, reason, ts) VALUES(?,?,?,?)",
               (member.id, moderator.id if moderator else 0, reason, int(time.time())))
    db.commit()
    count = active_warns(member.id)
    punish = WARN_PUNISHMENTS.get(min(count, max(WARN_PUNISHMENTS))) if count >= min(WARN_PUNISHMENTS) else None
    result = f"warning **{count}**"
    if punish:
        try:
            await member.timeout(punish, reason=f"{count} warnings: {reason}")
            result += f" → muted for **{fmt_delta(punish)}**"
        except discord.HTTPException:
            result += " (couldn't mute - their role is above the bot)"
    try:
        await member.send(f"⚠️ You got a warning on the OASIS server: **{reason}**\n"
                          f"Active warnings: {count}."
                          + (f" You are muted for {fmt_delta(punish)}." if punish else "")
                          + " Please follow the rules 🌴")
    except discord.HTTPException:
        pass                                     # DMs closed
    by = moderator.mention if moderator else "AutoMod"
    alert = " 🚨 <@&{}>".format(role(member.guild, "Moderator").id) \
        if count >= max(WARN_PUNISHMENTS) and role(member.guild, "Moderator") else ""
    ch = channel(member.guild, "modlog")
    if ch:
        await ch.send(f"⚠️ {member.mention} {result} by {by} - {reason}{alert}",
                      allowed_mentions=discord.AllowedMentions(roles=True, users=False))
    return count, punish


AUTOMOD_SAYS = {"Bad language": "watch your language", "Spam": "slow down",
                "Server invite links": "no server invites here", "Mass mentions": "don't mass-ping people"}
spam_tracker = {}       # user_id -> [(time, content), ...]
last_auto_warn = {}     # user_id -> time, so one burst of spam gives one warning
automod_deleted = set()  # ids removed by AutoMod (already logged as a warning)


async def automod_check(message):
    """Returns True if the message was removed."""
    member = message.author
    if is_staff(member):
        return False
    reason = None
    if message.content and bad_word_in(message.content):
        reason = "Bad language"
    else:
        now = time.time()
        history = [(t, c) for t, c in spam_tracker.get(member.id, []) if now - t < 20] + \
                  [(now, message.content.lower().strip())]
        spam_tracker[member.id] = history
        burst = sum(1 for t, _ in history if now - t < SPAM_SECONDS) >= SPAM_MESSAGES
        repeats = message.content and sum(1 for _, c in history if c == history[-1][1]) >= SPAM_REPEATS
        if burst or repeats:
            reason = "Spam"
        elif INVITE_RE.search(message.content):
            reason = "Server invite links"
        elif len(message.mentions) + len(message.role_mentions) >= MENTION_LIMIT or message.mention_everyone:
            reason = "Mass mentions"
    if reason is None:
        return False
    automod_deleted.add(message.id)
    try:
        await message.delete()
    except discord.HTTPException:
        pass
    now = time.time()
    if now - last_auto_warn.get(member.id, 0) > 30:
        last_auto_warn[member.id] = now
        count, punish = await add_warn(member, f"{reason} (auto)")
        await message.channel.send(
            f"⚠️ {member.mention} {AUTOMOD_SAYS.get(reason, 'please follow the rules')}! "
            f"Warning {count}" + (f" - muted for {fmt_delta(punish)}." if punish else "."), delete_after=10)
    return True


@bot.tree.command(name="warn", description="Give a member a warning")
@app_commands.default_permissions(moderate_members=True)
async def warn(interaction: discord.Interaction, member: discord.Member, reason: str):
    count, punish = await add_warn(member, reason, interaction.user)
    await interaction.response.send_message(
        f"⚠️ Warned {member.mention} ({count} active)" + (f", muted for {fmt_delta(punish)}." if punish else "."),
        ephemeral=True)


@bot.tree.command(name="warnings", description="Show a member's warnings")
@app_commands.default_permissions(moderate_members=True)
async def warnings(interaction: discord.Interaction, member: discord.Member):
    rows = db.execute("SELECT id, mod_id, reason, ts FROM warns WHERE user_id=? ORDER BY ts DESC LIMIT 15",
                      (member.id,)).fetchall()
    text = "\n".join(f"`#{i}` <t:{ts}:d> - {r} (by {'AutoMod' if not m else f'<@{m}>'})"
                     for i, m, r, ts in rows) or "No warnings 🌴"
    await interaction.response.send_message(
        embed=discord.Embed(title=f"Warnings: {member.display_name} ({active_warns(member.id)} active)",
                            description=text, colour=SAND), ephemeral=True)


@bot.tree.command(name="delwarn", description="Remove one warning by its number")
@app_commands.default_permissions(moderate_members=True)
async def delwarn(interaction: discord.Interaction, warning_id: int):
    gone = db.execute("DELETE FROM warns WHERE id=?", (warning_id,)).rowcount
    db.commit()
    await interaction.response.send_message("Removed." if gone else "No warning with that number.",
                                            ephemeral=True)


@bot.tree.command(name="clearwarns", description="Remove all warnings of a member")
@app_commands.default_permissions(administrator=True)
async def clearwarns(interaction: discord.Interaction, member: discord.Member):
    db.execute("DELETE FROM warns WHERE user_id=?", (member.id,))
    db.commit()
    await interaction.response.send_message(f"Cleared all warnings of {member.mention}.", ephemeral=True)


@bot.tree.command(name="mute", description="Mute (timeout) a member")
@app_commands.describe(duration="e.g. 10m, 2h, 1d (max 28d)")
@app_commands.default_permissions(moderate_members=True)
async def mute(interaction: discord.Interaction, member: discord.Member, duration: str, reason: str = "No reason"):
    seconds = min(parse_duration(duration), 28 * 86400)
    if seconds < 60:
        return await interaction.response.send_message("Duration e.g. 10m, 2h, 1d.", ephemeral=True)
    try:
        await member.timeout(datetime.timedelta(seconds=seconds), reason=reason)
    except discord.HTTPException:
        return await interaction.response.send_message("Can't mute this member (role above the bot?).",
                                                       ephemeral=True)
    await log(interaction.guild, discord.Embed(
        description=f"🔇 {member.mention} muted for {fmt_delta(datetime.timedelta(seconds=seconds))} "
                    f"by {interaction.user.mention} - {reason}", colour=RED))
    await interaction.response.send_message(f"🔇 Muted {member.mention}.", ephemeral=True)


@bot.tree.command(name="unmute", description="Remove a member's mute")
@app_commands.default_permissions(moderate_members=True)
async def unmute(interaction: discord.Interaction, member: discord.Member):
    await member.timeout(None)
    await log(interaction.guild, discord.Embed(
        description=f"🔊 {member.mention} unmuted by {interaction.user.mention}", colour=GREEN))
    await interaction.response.send_message(f"🔊 Unmuted {member.mention}.", ephemeral=True)


# ---- logs ----
@bot.event
async def on_member_join(member):
    if our_guild(member.guild) and member.bot:          # new bots get the Bot rank automatically
        await ensure_bot_role(member.guild)


# ---- /clear ----
FULL_CLEAR_WORDS = {"all", "full", "everything", "wszystko"}
clearing_channels = set()   # no delete-logs while /clear runs


def panel_message_ids():
    return {int(v) for k in ("verify", "roles", "ticket", "control")
            if (v := kv_get(f"panel:{k}")) and v.isdigit()}


class ConfirmFullClear(discord.ui.View):
    def __init__(self, target):
        super().__init__(timeout=60)
        self.target = target

    @discord.ui.button(label="Yes, wipe the whole channel", style=discord.ButtonStyle.danger, emoji="🧹")
    async def confirm(self, interaction: discord.Interaction, _):
        await interaction.response.edit_message(content="🧹 Wiping...", view=None)
        old = self.target
        new = await old.clone(reason=f"/clear all by {interaction.user}")   # same name, settings, permissions
        await new.edit(position=old.position)
        await old.delete(reason=f"/clear all by {interaction.user}")
        await post_panels(interaction.guild)          # re-post panels if this channel had one
        await ensure_control_channel(interaction.guild)
        await new.send("🧹 Channel cleared.", delete_after=10)
        await log(interaction.guild, discord.Embed(
            description=f"🧹 {interaction.user.mention} fully cleared #{new.name}", colour=SAND))

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _):
        await interaction.response.edit_message(content="Cancelled.", view=None)


@bot.tree.command(name="clear", description="Delete messages in this channel")
@app_commands.describe(period="How far back: e.g. 30m, 2h, 1d, 7d - or 'all' for the whole channel",
                       member="Only delete this member's messages (optional)")
@app_commands.default_permissions(manage_messages=True)
async def clear(interaction: discord.Interaction, period: str, member: discord.Member | None = None):
    ch = interaction.channel
    if period.lower().strip() in FULL_CLEAR_WORDS:
        if member is not None:
            return await interaction.response.send_message(
                "'all' wipes the whole channel - to remove one member's messages use a time, e.g. 30d.",
                ephemeral=True)
        return await interaction.response.send_message(
            f"⚠️ Delete **every message** in {ch.mention}? This can't be undone "
            f"(the channel is recreated with the same settings; panels are re-posted).",
            view=ConfirmFullClear(ch), ephemeral=True)
    seconds = parse_duration(period)
    if seconds < 60:
        return await interaction.response.send_message(
            "Give a time like `30m`, `2h`, `1d`, `7d` - or `all`.", ephemeral=True)
    await interaction.response.defer(ephemeral=True, thinking=True)
    after = discord.utils.utcnow() - datetime.timedelta(seconds=seconds)
    keep = panel_message_ids()
    clearing_channels.add(ch.id)
    try:
        deleted = await ch.purge(limit=None, after=after, check=lambda m: not m.pinned and m.id not in keep
                                 and (member is None or m.author.id == member.id))
    finally:
        clearing_channels.discard(ch.id)
    await interaction.followup.send(f"🧹 Deleted **{len(deleted)}** messages from the last {period}"
                                    + (f" by {member.mention}" if member else "") + ".")
    await log(interaction.guild, discord.Embed(
        description=f"🧹 {interaction.user.mention} cleared {len(deleted)} messages in {ch.mention} "
                    f"(last {period}{f', only {member.mention}' if member else ''})", colour=SAND))


@bot.event
async def on_message_delete(message):
    if not our_guild(message.guild) or message.author.bot or message.channel.name.startswith("ticket-"):
        return
    if message.channel.name == CH["showcase"] and not message.attachments:
        return    # removed by the picture-only filter, not worth logging
    if message.channel.id in clearing_channels:
        return
    if message.id in automod_deleted:
        automod_deleted.discard(message.id)
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
    if not is_staff(after.author) and bad_word_in(after.content):     # edited into a bad word
        automod_deleted.add(after.id)
        try:
            await after.delete()
        except discord.HTTPException:
            pass
        await add_warn(after.author, "Bad language (auto, edited message)")


# ---- XP ----
@bot.event
async def on_message(message):
    if not our_guild(message.guild) or message.author.bot or message.channel.name.startswith("ticket-"):
        return
    if await automod_check(message):
        return
    # mutation-showcase is a gallery: messages without a picture/video are removed
    if message.channel.name == CH["showcase"] and not message.attachments and not is_staff(message.author):
        try:
            await message.delete()
            await message.channel.send(f"{message.author.mention} this channel is for pictures only 📸 "
                                       f"- chat in the other channels!", delete_after=6)
        except discord.HTTPException:
            pass
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


# ---- Codes channel ----
async def ensure_codes_channel(guild):
    ch = channel(guild, "codes")
    if ch is None:
        verified = role(guild, "Verified Player")
        overwrites = {guild.default_role: PO(view_channel=False, send_messages=False),
                      guild.me: PO(view_channel=True, send_messages=True)}
        if verified:
            overwrites[verified] = PO(view_channel=True, send_messages=False, add_reactions=True)
        updates = channel(guild, "updates")
        ch = await guild.create_text_channel(
            CH["codes"], overwrites=overwrites, topic="Active OASIS codes - redeem them in the game! 🎟️",
            category=updates.category if updates else None)
        if updates:
            await ch.edit(position=updates.position + 1)
        print(f"+ created {ch.name}")
    return ch


def code_embed(code, reward, expires, active=True):
    if active:
        desc = f"🎁 **Reward:** {reward}\n" + (f"⏳ **Expires** <t:{expires}:R>" if expires else "⏳ No expiry date")
        embed = discord.Embed(title=f"🎟️ {code}", description=desc, colour=GREEN)
        embed.set_footer(text="Copy the code from the title and redeem it in OASIS!")
    else:
        embed = discord.Embed(title=f"~~{code}~~ - EXPIRED", description=f"~~{reward}~~", colour=0x95A5A6)
    return embed


async def expire_code(guild, code, delete=False):
    row = db.execute("SELECT reward, expires, message_id FROM codes WHERE code=?", (code,)).fetchone()
    if row is None:
        return False
    reward, expires, message_id = row
    if delete:
        db.execute("DELETE FROM codes WHERE code=?", (code,))
    else:
        db.execute("UPDATE codes SET active=0 WHERE code=?", (code,))
    db.commit()
    ch = channel(guild, "codes")
    if ch and message_id:
        try:
            msg = await ch.fetch_message(message_id)
            if delete:
                await msg.delete()
            else:
                await msg.edit(content=None, embed=code_embed(code, reward, expires, active=False))
        except discord.NotFound:
            pass
    return True


async def code_autocomplete(interaction: discord.Interaction, current: str):
    rows = db.execute("SELECT code FROM codes WHERE code LIKE ? ORDER BY active DESC, code LIMIT 25",
                      (f"%{current}%",)).fetchall()
    return [app_commands.Choice(name=c, value=c) for (c,) in rows]


@bot.tree.command(name="code-add", description="Post a new game code in the codes channel")
@app_commands.describe(code="The code exactly as players type it", reward="What the code gives",
                       expires="Optional: when it stops working, e.g. 3d, 12h", ping="Ping Update Ping?")
@app_commands.default_permissions(manage_guild=True)
async def code_add(interaction: discord.Interaction, code: str, reward: str, expires: str | None = None,
                   ping: bool = False):
    code = code.strip()
    if db.execute("SELECT 1 FROM codes WHERE code=? AND active=1", (code,)).fetchone():
        return await interaction.response.send_message(f"`{code}` is already active.", ephemeral=True)
    exp_ts = None
    if expires:
        seconds = parse_duration(expires)
        if seconds < 60:
            return await interaction.response.send_message("Expiry e.g. `12h`, `3d`, `1d12h`.", ephemeral=True)
        exp_ts = int(time.time()) + seconds
    ch = await ensure_codes_channel(interaction.guild)
    r = role(interaction.guild, UPDATE_PING_ROLE) if ping else None
    msg = await ch.send(content=f"{r.mention} New code! 🎉" if r else None,
                        embed=code_embed(code, reward, exp_ts),
                        allowed_mentions=discord.AllowedMentions(roles=[r] if r else []))
    db.execute("INSERT OR REPLACE INTO codes(code, reward, expires, message_id, active) VALUES(?,?,?,?,1)",
               (code, reward, exp_ts, msg.id))
    db.commit()
    await interaction.response.send_message(f"✅ Code `{code}` posted in {ch.mention}.", ephemeral=True)


@bot.tree.command(name="code-remove", description="Remove a code (mark it EXPIRED or delete its post)")
@app_commands.describe(code="Which code", delete="True = delete the post completely, False = show it as EXPIRED")
@app_commands.autocomplete(code=code_autocomplete)
@app_commands.default_permissions(manage_guild=True)
async def code_remove(interaction: discord.Interaction, code: str, delete: bool = False):
    if await expire_code(interaction.guild, code, delete):
        await interaction.response.send_message(
            f"🗑️ `{code}` {'deleted' if delete else 'marked as EXPIRED'}.", ephemeral=True)
    else:
        await interaction.response.send_message(f"No code `{code}`.", ephemeral=True)


@bot.tree.command(name="code-list", description="Show all codes")
@app_commands.default_permissions(manage_guild=True)
async def code_list(interaction: discord.Interaction):
    rows = db.execute("SELECT code, reward, expires, active FROM codes ORDER BY active DESC, code").fetchall()
    text = "\n".join(f"{'✅' if a else '❌'} `{c}` - {r}" + (f" (expires <t:{e}:R>)" if a and e else "")
                     for c, r, e, a in rows) or "No codes yet."
    await interaction.response.send_message(embed=discord.Embed(title="🎟️ Codes", description=text[:4000],
                                                                colour=SAND), ephemeral=True)


@tasks.loop(minutes=5)
async def expire_codes():
    guild = bot.get_guild(GUILD_ID)
    if guild is None:
        return
    due = db.execute("SELECT code FROM codes WHERE active=1 AND expires IS NOT NULL AND expires<=?",
                     (int(time.time()),)).fetchall()
    for (code,) in due:
        await expire_code(guild, code)


# ---- In-game player tracker (owner-only) ----
async def ensure_tracker_channel(guild):
    ch = channel(guild, "tracker")
    if ch is None:
        owner_role = role(guild, CONTROL_ROLE)
        overwrites = {guild.default_role: PO(view_channel=False),
                      guild.me: PO(view_channel=True, send_messages=True)}
        if owner_role:
            overwrites[owner_role] = PO(view_channel=True, send_messages=True)
        ch = await guild.create_text_channel(
            CH["tracker"], overwrites=overwrites, topic="Kto wchodzi do OASIS i ile gra - widzi to tylko Owner.",
            category=discord.utils.get(guild.categories, name=CONTROL_CATEGORY))
        print(f"+ created {ch.name}")
    return ch


def fmt_played(seconds):
    seconds = max(60, int(seconds))
    h, m = seconds // 3600, (seconds % 3600) // 60
    return f"{h} h {m} min" if h else f"{m} min"


async def fetch_ingame_avatars():
    """Avatar pictures of everyone in the game right now -> (set of image urls, all_resolved)."""
    thumb = {"type": "AvatarHeadshot", "size": "150x150", "format": "png", "isCircular": False}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=25)) as s:
        tokens, cursor = [], ""
        for _ in range(3):
            url = (f"https://games.roblox.com/v1/games/{PLACE_ID}/servers/Public?sortOrder=Asc&limit=100"
                   + (f"&cursor={cursor}" if cursor else ""))
            async with s.get(url) as r:
                if r.status != 200:
                    raise RuntimeError(f"server list answered {r.status}")
                data = await r.json()
            for server in data.get("data", []):
                tokens += server.get("playerTokens", [])
            cursor = data.get("nextPageCursor")
            if not cursor:
                break
        reqs = [{"requestId": f"t{i}", "token": t, **thumb} for i, t in enumerate(tokens)]
        reqs += [{"requestId": f"u{uid}", "targetId": uid, **thumb} for uid in TRACKER_IGNORE_USER_IDS]
        players, ignored, resolved = [], set(), True
        for i in range(0, len(reqs), 100):
            async with s.post("https://thumbnails.roblox.com/v1/batch", json=reqs[i:i + 100]) as r:
                if r.status != 200:
                    raise RuntimeError(f"thumbnails answered {r.status}")
                for item in (await r.json()).get("data", []):
                    url = item.get("imageUrl")
                    if item.get("requestId", "").startswith("u"):
                        if url:
                            ignored.add(url)
                    elif url and item.get("state") == "Completed":
                        players.append(url)
                    else:
                        resolved = False
    return set(players) - ignored, resolved


@tasks.loop(seconds=TRACKER_POLL_SECONDS)
async def player_tracker():
    if kv_get("tracker_enabled", "1") != "1":
        return
    guild = bot.get_guild(GUILD_ID)
    if guild is None:
        return
    try:
        current, resolved = await fetch_ingame_avatars()
    except Exception as e:
        print(f"! player tracker failed: {e}")
        return
    if len(current) > TRACKER_MAX_PLAYERS:
        return
    state = json.loads(kv_get("tracker_state") or "{}")
    ch = await ensure_tracker_channel(guild)
    now = int(time.time())
    for url in current:
        if url in state:
            state[url]["miss"] = 0
            continue
        embed = discord.Embed(description=f"🟢 **Gracz wszedł do OASIS** <t:{now}:t>\nW grze teraz: {len(current)}",
                              colour=GREEN)
        embed.set_thumbnail(url=url)
        msg = await ch.send(content=f"<@{guild.owner_id}>", embed=embed,
                            allowed_mentions=discord.AllowedMentions(users=True))
        state[url] = {"start": now, "msg": msg.id, "miss": 0}
    if resolved:
        for url in [u for u in state if u not in current]:
            state[url]["miss"] += 1
            if state[url]["miss"] < TRACKER_MISS_LIMIT:
                continue
            info = state.pop(url)
            played = now - info["start"] - (TRACKER_MISS_LIMIT - 1) * TRACKER_POLL_SECONDS
            embed = discord.Embed(description=f"🔴 **Gracz wyszedł** - był w grze ok. **{fmt_played(played)}**",
                                  colour=RED)
            embed.set_thumbnail(url=url)
            await ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none(),
                          reference=ch.get_partial_message(info["msg"]).to_reference(fail_if_not_exists=False))
    kv_set("tracker_state", json.dumps(state))


@bot.tree.command(name="tracker", description="Turn the in-game player tracker on or off")
@app_commands.choices(state=[app_commands.Choice(name="on", value="1"),
                             app_commands.Choice(name="off", value="0")])
@app_commands.default_permissions(administrator=True)
async def tracker(interaction: discord.Interaction, state: app_commands.Choice[str]):
    kv_set("tracker_enabled", state.value)
    kv_set("tracker_state", "{}")
    await interaction.response.send_message(
        "👀 Śledzenie graczy **włączone**." if state.value == "1" else "😴 Śledzenie graczy **wyłączone**.",
        ephemeral=True)


class AnnounceModal(discord.ui.Modal, title="New OASIS announcement"):
    headline = discord.ui.TextInput(label="Title", placeholder="OASIS is OUT NOW! 🌴", max_length=200)
    body = discord.ui.TextInput(label="Message", style=discord.TextStyle.paragraph, max_length=3800,
                                placeholder="The desert is open! Plant, grow and sell...")
    image = discord.ui.TextInput(label="Image link (optional)", required=False, placeholder="https://...png")
    play = discord.ui.TextInput(label="Add a Play button? (yes / no)", required=False, default="yes",
                                max_length=3)

    def __init__(self, ping: str):
        super().__init__()
        self.ping = ping

    async def on_submit(self, interaction: discord.Interaction):
        ch = channel(interaction.guild, "announcements")
        if ch is None:
            return await interaction.response.send_message("Announcements channel not found.", ephemeral=True)
        embed = discord.Embed(title=self.headline.value, description=self.body.value, colour=SAND,
                              timestamp=datetime.datetime.now(datetime.timezone.utc))
        if self.image.value.startswith("http"):
            embed.set_image(url=self.image.value)
        embed.set_footer(text="OASIS", icon_url=interaction.guild.icon.url if interaction.guild.icon else None)
        view = None
        if self.play.value.strip().lower() not in ("no", "n", "nie"):
            view = discord.ui.View()
            view.add_item(discord.ui.Button(label="Play OASIS", emoji="🎮", url=GAME_LINK))
        if self.ping == "everyone":
            content, mentions = "@everyone", discord.AllowedMentions(everyone=True)
        elif self.ping == "none":
            content, mentions = None, discord.AllowedMentions.none()
        else:
            r = role(interaction.guild, self.ping)
            content = r.mention if r else None
            mentions = discord.AllowedMentions(roles=[r] if r else [])
        msg = await ch.send(content=content, embed=embed, view=view, allowed_mentions=mentions)
        if ch.is_news():
            try:
                await msg.publish()
            except discord.HTTPException:
                pass
        await interaction.response.send_message(f"Announcement posted in {ch.mention}! 📢", ephemeral=True)


@bot.tree.command(name="announce", description="Post an announcement in the announcements channel")
@app_commands.describe(ping="Who gets pinged")
@app_commands.choices(ping=[
    app_commands.Choice(name="@everyone", value="everyone"),
    app_commands.Choice(name="Update Ping", value="Update Ping"),
    app_commands.Choice(name="Event Ping", value="Event Ping"),
    app_commands.Choice(name="Giveaway Ping", value="Giveaway Ping"),
    app_commands.Choice(name="Sneak Peek Ping", value="Sneak Peek Ping"),
    app_commands.Choice(name="No ping", value="none"),
])
@app_commands.default_permissions(manage_guild=True)
async def announce(interaction: discord.Interaction, ping: app_commands.Choice[str]):
    await interaction.response.send_modal(AnnounceModal(ping.value))


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
    if interaction.channel.name != CH["control"]:
        return await interaction.response.send_message(f"Use this in #{CH['control']}.", ephemeral=True)
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
    kv_set("restart_notice", "1")
    kv_set("version", f"{message} ({commit['sha'][:7]})")
    kv_set("restart_text", f"✅ Bot zaktualizowany i znowu online: **{message}**")
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


# ---- Suggestions & bug reports (forums) ----
@bot.event
async def on_thread_create(thread):
    if not our_guild(thread.guild) or thread.parent is None or thread.parent.name != CH["suggestions"]:
        return
    for _ in range(5):                      # the first post can arrive a moment after the thread
        try:
            starter = thread.starter_message or await thread.fetch_message(thread.id)
            for emoji in SUGGESTION_REACTIONS:
                await starter.add_reaction(emoji)
            return
        except discord.NotFound:
            await asyncio.sleep(1)


@bot.event
async def on_thread_update(before, after):
    if not our_guild(after.guild) or after.parent is None or after.parent.name != CH["bugs"]:
        return
    had = any(t.name == FIXED_TAG for t in before.applied_tags)
    has = any(t.name == FIXED_TAG for t in after.applied_tags)
    if has and not had:
        await after.send(f"✅ <@{after.owner_id}> this bug has been marked as **fixed** - "
                         f"thanks for reporting it! 🌴")


# ---- Tickets: remind silent players, then close ----
@tasks.loop(minutes=10)
async def autoclose_tickets():
    guild = bot.get_guild(GUILD_ID)
    if guild is None:
        return
    now = time.time()
    for ch in list(guild.text_channels):
        owner = ticket_owner_id(ch)
        if owner is None:
            continue
        last_human = None
        async for m in ch.history(limit=50):
            if not m.author.bot:
                last_human = m
                break
        if last_human is not None and last_human.author.id == owner:
            kv_set(f"ticket_rem:{ch.id}", "")          # player answered - waiting on staff, no timer
            continue
        # waiting on the player: since staff's last message, or since the ticket opened
        since = (last_human.created_at if last_human else ch.created_at).timestamp()
        state = json.loads(kv_get(f"ticket_rem:{ch.id}") or "{}")
        if state.get("since") != since:                # new staff message -> fresh countdown
            state = {"since": since}
        if "close" not in state:
            if now - since >= TICKET_REMIND_AFTER_HOURS * 3600:
                state["close"] = int(now + TICKET_CLOSE_AFTER_HOURS * 3600)
                await ch.send(f"⏰ <@{owner}> we're waiting for your reply! If you don't answer, this "
                              f"ticket closes automatically <t:{state['close']}:R>.",
                              allowed_mentions=discord.AllowedMentions(users=True))
        elif now >= state["close"]:
            kv_set(f"ticket_rem:{ch.id}", "")
            await ch.send("🔒 No reply - closing this ticket. Open a new one anytime!")
            await close_ticket(ch, "auto-closed (no reply from the player)")
            continue
        kv_set(f"ticket_rem:{ch.id}", json.dumps(state))


# ---- Giveaways ----
def parse_duration(text):
    total = 0
    for amount, unit in re.findall(r"(\d+)\s*([dhm])", text.lower()):
        total += int(amount) * {"d": 86400, "h": 3600, "m": 60}[unit]
    return total


def giveaway_embed(prize, end_ts, winners, entries, host_id, ended_text=None):
    if ended_text:
        desc = f"{ended_text}\n\nHosted by <@{host_id}>"
    else:
        desc = (f"Click **🎉 Enter** to join!\n\nEnds {discord.utils.format_dt(datetime.datetime.fromtimestamp(end_ts, datetime.timezone.utc), 'R')}"
                f"\nWinners: **{winners}**\nHosted by <@{host_id}>")
    embed = discord.Embed(title=f"🎁 {prize}", description=desc, colour=GREEN if ended_text else SAND)
    embed.set_footer(text=f"Entries: {entries}")
    return embed


class GiveawayView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Enter", emoji="🎉", style=discord.ButtonStyle.success,
                       custom_id="oasis:gw_enter")
    async def enter(self, interaction: discord.Interaction, _):
        mid, uid = interaction.message.id, interaction.user.id
        row = db.execute("SELECT prize, winners, end_ts, host_id, ended FROM giveaways WHERE message_id=?",
                         (mid,)).fetchone()
        if row is None or row[4]:
            return await interaction.response.send_message("This giveaway has ended.", ephemeral=True)
        if db.execute("SELECT 1 FROM gw_entries WHERE message_id=? AND user_id=?", (mid, uid)).fetchone():
            db.execute("DELETE FROM gw_entries WHERE message_id=? AND user_id=?", (mid, uid))
            msg = "You left the giveaway."
        else:
            db.execute("INSERT INTO gw_entries VALUES(?, ?)", (mid, uid))
            msg = "You're in! Good luck 🍀"
        db.commit()
        count = db.execute("SELECT COUNT(*) FROM gw_entries WHERE message_id=?", (mid,)).fetchone()[0]
        prize, winners, end_ts, host_id, _ = row
        await interaction.response.edit_message(embed=giveaway_embed(prize, end_ts, winners, count, host_id))
        await interaction.followup.send(msg, ephemeral=True)


async def finish_giveaway(message_id, reroll=False):
    row = db.execute("SELECT channel_id, prize, winners, end_ts, host_id FROM giveaways WHERE message_id=?",
                     (message_id,)).fetchone()
    if row is None:
        return False
    channel_id, prize, winners, end_ts, host_id = row
    db.execute("UPDATE giveaways SET ended=1 WHERE message_id=?", (message_id,))
    db.commit()
    ch = bot.get_channel(channel_id)
    if ch is None:
        return False
    entries = [u for (u,) in db.execute("SELECT user_id FROM gw_entries WHERE message_id=?", (message_id,))]
    picked = random.sample(entries, min(winners, len(entries)))
    mentions = ", ".join(f"<@{u}>" for u in picked)
    try:
        msg = await ch.fetch_message(message_id)
        result = f"🏆 Winner{'s' if len(picked) > 1 else ''}: {mentions}" if picked else "Nobody entered 😢"
        await msg.edit(embed=giveaway_embed(prize, end_ts, winners, len(entries), host_id, result), view=None)
    except discord.NotFound:
        msg = None
    if picked:
        await ch.send(f"🎉 {'New winner' if reroll else 'Congratulations'} {mentions}! You won **{prize}**! "
                      f"Open a ticket to claim it.",
                      reference=msg, allowed_mentions=discord.AllowedMentions(users=True))
    else:
        await ch.send(f"Nobody entered the **{prize}** giveaway.")
    return True


@tasks.loop(seconds=30)
async def giveaway_timer():
    due = db.execute("SELECT message_id FROM giveaways WHERE ended=0 AND end_ts<=?", (int(time.time()),))
    for (mid,) in due.fetchall():
        await finish_giveaway(mid)


@bot.tree.command(name="giveaway", description="Start a giveaway in the giveaways channel")
@app_commands.describe(prize="What can people win", duration="e.g. 30m, 2h, 1d, 1d12h",
                       winners="How many winners")
@app_commands.default_permissions(manage_guild=True)
async def giveaway(interaction: discord.Interaction, prize: str, duration: str,
                   winners: app_commands.Range[int, 1, 20] = 1):
    seconds = parse_duration(duration)
    if seconds < 60:
        return await interaction.response.send_message("Duration must be at least 1m (e.g. 30m, 2h, 1d).",
                                                       ephemeral=True)
    ch = channel(interaction.guild, "giveaways")
    if ch is None:
        return await interaction.response.send_message("Giveaways channel not found.", ephemeral=True)
    end_ts = int(time.time()) + seconds
    ping = role(interaction.guild, GIVEAWAY_PING_ROLE)
    msg = await ch.send(content=ping.mention if ping else None,
                        embed=giveaway_embed(prize, end_ts, winners, 0, interaction.user.id),
                        view=GiveawayView(),
                        allowed_mentions=discord.AllowedMentions(roles=[ping] if ping else []))
    db.execute("INSERT INTO giveaways(message_id, channel_id, prize, winners, end_ts, host_id) "
               "VALUES(?,?,?,?,?,?)", (msg.id, ch.id, prize, winners, end_ts, interaction.user.id))
    db.commit()
    await interaction.response.send_message(f"Giveaway started in {ch.mention}! 🎉", ephemeral=True)


@bot.tree.command(name="giveaway-end", description="End a giveaway now, or pick a new winner for an ended one")
@app_commands.describe(message_id="ID of the giveaway message (right-click it -> Copy Message ID)")
@app_commands.default_permissions(manage_guild=True)
async def giveaway_end(interaction: discord.Interaction, message_id: str):
    row = db.execute("SELECT ended FROM giveaways WHERE message_id=?", (int(message_id),)).fetchone() \
        if message_id.isdigit() else None
    if row is None:
        return await interaction.response.send_message("Giveaway not found.", ephemeral=True)
    await interaction.response.send_message("Drawing...", ephemeral=True)
    await finish_giveaway(int(message_id), reroll=bool(row[0]))


# ---- Roblox: game info, live player counter, update watcher ----
async def fetch_game():
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s:
        universe = kv_get("universe_id")
        if not universe:
            async with s.get(f"https://apis.roblox.com/universes/v1/places/{PLACE_ID}/universe") as r:
                universe = (await r.json())["universeId"]
            kv_set("universe_id", universe)
        async with s.get(f"https://games.roblox.com/v1/games?universeIds={universe}") as r:
            return (await r.json())["data"][0]


@tasks.loop(minutes=10)                     # Discord allows only 2 renames per 10 min
async def player_counter():
    guild = bot.get_guild(GUILD_ID)
    if guild is None:
        return
    try:
        playing = (await fetch_game()).get("playing", 0)
    except Exception as e:
        print(f"! player count failed: {e}")
        return
    name = f"{PLAYER_COUNT_PREFIX}: {playing}"
    ch = discord.utils.find(lambda c: c.name.startswith(PLAYER_COUNT_PREFIX), guild.voice_channels)
    if ch is None:
        await guild.create_voice_channel(name, position=0, overwrites={
            guild.default_role: PO(view_channel=True, connect=False)})
    elif ch.name != name:
        await ch.edit(name=name)


@tasks.loop(minutes=UPDATE_CHECK_MINUTES)
async def check_game_update():
    try:
        game = await fetch_game()
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
@player_counter.before_loop
@autoclose_tickets.before_loop
@giveaway_timer.before_loop
@expire_codes.before_loop
@player_tracker.before_loop
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
