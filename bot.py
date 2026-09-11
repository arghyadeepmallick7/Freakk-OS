"""
FREAKOS — single-file build with ticket system.

Contains: message formatter, database, scheduler, embeds, errors, and the
core cogs (General, Welcome, Autorole, Autonick, Departure, ActionDM,
VCNotify, Moderation, Logging, Tickets, CustomCommands, Announcements,
Giveaways).

Run:  python bot.py
"""
from __future__ import annotations

# ============================================================================
# BOOT FIX — runs BEFORE any other import.  Diagnostics print to console so
# we can see exactly which discord module Python loads.
# ============================================================================
import os as _os
import sys as _sys

_HERE = _os.path.dirname(_os.path.abspath(__file__))
_PYVER = f"python{_sys.version_info.major}.{_sys.version_info.minor}"

_CANDIDATES = [
    _os.path.join(_HERE, ".local", "lib", _PYVER, "site-packages"),
    _os.path.join(_HERE, ".local", "lib", "python3.14", "site-packages"),
    _os.path.join(_HERE, ".local", "lib", "python3.13", "site-packages"),
    _os.path.join(_HERE, ".local", "lib", "python3.12", "site-packages"),
    _os.path.join(_HERE, ".local", "lib", "python3.11", "site-packages"),
    _os.path.join(_HERE, ".local", "lib", "python3.10", "site-packages"),
]
_FOUND = [p for p in _CANDIDATES if _os.path.isdir(p)]

# Real .local paths go to the front of sys.path so they beat the system stub.
_sys.path = _FOUND + [p for p in _sys.path if p not in _FOUND]

# Drop any cached discord modules so the next import respects the new order.
for _k in [k for k in list(_sys.modules.keys()) if k == "discord" or k.startswith("discord.")]:
    del _sys.modules[_k]

import discord as _d

print("=" * 72, flush=True)
print("[FREAKOS] BOOT DIAGNOSTIC", flush=True)
print(f"[FREAKOS] Python: {_sys.version.split()[0]}", flush=True)
print(f"[FREAKOS] .local paths found: {_FOUND}", flush=True)
print(f"[FREAKOS] sys.path[0:3]: {_sys.path[0:3]}", flush=True)
print(f"[FREAKOS] discord loaded from: {getattr(_d, '__file__', '?')}", flush=True)
print(f"[FREAKOS] discord has Bot: {hasattr(_d, 'Bot')}", flush=True)
print("=" * 72, flush=True)

# If Python still landed on the stub, force-load discord from disk.
if not hasattr(_d, "Bot"):
    import importlib.util as _ilu
    for _base in _FOUND:
        _target = _os.path.join(_base, "discord", "__init__.py")
        if _os.path.isfile(_target):
            print(f"[FREAKOS] Force-loading discord from {_target}", flush=True)
            try:
                _spec = _ilu.spec_from_file_location(
                    "discord", _target,
                    submodule_search_locations=[_os.path.dirname(_target)],
                )
                _mod = _ilu.module_from_spec(_spec)
                _sys.modules["discord"] = _mod
                _spec.loader.exec_module(_mod)
                _d = _mod
                print(f"[FREAKOS] Force-load OK — has Bot: {hasattr(_d, 'Bot')}", flush=True)
                break
            except Exception as _e:
                print(f"[FREAKOS] Force-load failed: {_e}", flush=True)

if not hasattr(_d, "Bot"):
    print(
        "\n"
        "[FREAKOS] FATAL: could not obtain the real discord.py library.\n"
        "          Loaded module: {}\n"
        "          Fix: delete bot.py and re-upload this file. If it\n"
        "          still fails, the container's .local is missing the\n"
        "          real discord package — delete the whole .local folder\n"
        "          and restart so pip reinstalls it cleanly.\n".format(
            getattr(_d, "__file__", "?")
        ),
        flush=True,
    )
    _sys.exit(1)

del _d, _os, _sys
for _n in ("_HERE", "_PYVER", "_CANDIDATES", "_FOUND", "_k"):
    try:
        del globals()[_n]
    except KeyError:
        pass
# ============================================================================
# END BOOT FIX
# ============================================================================

import asyncio
import datetime
import json
import logging
import random
import re
import time
from typing import Any, Iterable, Mapping, Optional

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

# ============================================================================
# ENV + LOGGING
# ============================================================================

load_dotenv()

LOG_LEVEL = getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO)
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("freakos")
logging.getLogger("discord").setLevel(logging.WARNING)
logging.getLogger("discord.http").setLevel(logging.WARNING)


# ============================================================================
# MESSAGE FORMATTER
# ============================================================================

MSG_LIMIT = 2000
EMBED_TITLE_LIMIT = 256
EMBED_DESC_LIMIT = 4096
EMBED_FIELD_NAME_LIMIT = 256
EMBED_FIELD_VALUE_LIMIT = 1024
EMBED_FOOTER_LIMIT = 2048

_PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


def render(template, ctx=None, *, convert_literal_newlines=True, limit=None):
    if template is None:
        return ""
    text = str(template)
    if convert_literal_newlines:
        text = text.replace("\\n", "\n").replace("\\r\\n", "\r\n")
    if ctx:
        def repl(m):
            k = m.group(1)
            if k in ctx and ctx[k] is not None:
                return str(ctx[k])
            return m.group(0)
        text = _PLACEHOLDER_RE.sub(repl, text)
    if limit is not None and len(text) > limit:
        text = _truncate(text, limit)
    return text


def _truncate(text, limit, ellipsis="…"):
    if len(text) <= limit:
        return text
    if limit <= len(ellipsis):
        return text[:limit]
    budget = limit - len(ellipsis)
    head = text[:budget]
    nl = head.rfind("\n")
    if nl >= budget // 2:
        return head[:nl] + ellipsis
    return head + ellipsis


def split_for_discord(text, limit=MSG_LIMIT):
    if len(text) <= limit:
        return [text]
    out = []
    rem = text
    while len(rem) > limit:
        head = rem[:limit]
        cut = head.rfind("\n")
        if cut < limit // 2:
            cut = head.rfind(" ")
        if cut <= 0:
            cut = limit
        out.append(rem[:cut])
        rem = rem[cut + 1:] if rem[cut:cut + 1] in ("\n", " ") else rem[cut:]
    if rem:
        out.append(rem)
    return out


# ============================================================================
# DATABASE
# ============================================================================

SCHEMA = """
CREATE TABLE IF NOT EXISTS guild_config (
    guild_id INTEGER NOT NULL,
    key      TEXT    NOT NULL,
    value    TEXT    NOT NULL,
    PRIMARY KEY (guild_id, key)
);
CREATE TABLE IF NOT EXISTS autoroles (
    guild_id INTEGER NOT NULL,
    role_id  INTEGER NOT NULL,
    PRIMARY KEY (guild_id, role_id)
);
CREATE TABLE IF NOT EXISTS vc_notify (
    guild_id         INTEGER NOT NULL,
    voice_channel_id INTEGER NOT NULL,
    text_channel_id  INTEGER,
    join_message     TEXT,
    leave_message    TEXT,
    PRIMARY KEY (guild_id, voice_channel_id)
);
CREATE TABLE IF NOT EXISTS cases (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id     INTEGER NOT NULL,
    user_id      INTEGER NOT NULL,
    moderator_id INTEGER NOT NULL,
    action       TEXT    NOT NULL,
    reason       TEXT,
    duration     INTEGER,
    created_at   INTEGER NOT NULL,
    expires_at   INTEGER
);
CREATE INDEX IF NOT EXISTS cases_guild_user ON cases (guild_id, user_id);
CREATE TABLE IF NOT EXISTS timeout_dms (
    case_id    INTEGER PRIMARY KEY,
    guild_id   INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    sent       INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS custom_commands (
    guild_id INTEGER NOT NULL,
    name     TEXT    NOT NULL,
    response TEXT    NOT NULL,
    embed    INTEGER NOT NULL DEFAULT 0,
    enabled  INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (guild_id, name)
);
CREATE TABLE IF NOT EXISTS announcements (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id   INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    payload    TEXT    NOT NULL,
    send_at    INTEGER NOT NULL,
    sent       INTEGER NOT NULL DEFAULT 0,
    cancelled  INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS giveaways (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id   INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    message_id INTEGER,
    prize      TEXT    NOT NULL,
    winners    INTEGER NOT NULL DEFAULT 1,
    host_id    INTEGER NOT NULL,
    ends_at    INTEGER NOT NULL,
    ended      INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS giveaway_entries (
    giveaway_id INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    PRIMARY KEY (giveaway_id, user_id)
);
CREATE TABLE IF NOT EXISTS scheduled_tasks (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id   INTEGER,
    task_type  TEXT    NOT NULL,
    payload    TEXT    NOT NULL,
    run_at     INTEGER NOT NULL,
    status     TEXT    NOT NULL DEFAULT 'pending',
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS scheduled_due ON scheduled_tasks (status, run_at);

CREATE TABLE IF NOT EXISTS tickets (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    channel_id  INTEGER NOT NULL UNIQUE,
    user_id     INTEGER NOT NULL,
    status      TEXT NOT NULL DEFAULT 'open',
    created_at  INTEGER NOT NULL,
    closed_at   INTEGER,
    delete_at   INTEGER
);
CREATE INDEX IF NOT EXISTS tickets_channel ON tickets (channel_id);
CREATE INDEX IF NOT EXISTS tickets_open    ON tickets (guild_id, user_id, status);
"""


class Database:
    def __init__(self, path):
        self.path = path
        self._conn = None
        self._lock = asyncio.Lock()

    async def connect(self):
        d = os.path.dirname(self.path)
        if d:
            os.makedirs(d, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.executescript(SCHEMA)
        await self._conn.commit()

    async def close(self):
        if self._conn:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self):
        assert self._conn is not None
        return self._conn

    async def execute(self, sql, params=()):
        async with self._lock:
            cur = await self.conn.execute(sql, tuple(params))
            await self.conn.commit()
            return cur

    async def fetchone(self, sql, params=()):
        cur = await self.conn.execute(sql, tuple(params))
        row = await cur.fetchone()
        await cur.close()
        return row

    async def fetchall(self, sql, params=()):
        cur = await self.conn.execute(sql, tuple(params))
        rows = await cur.fetchall()
        await cur.close()
        return list(rows)

    async def get_config(self, guild_id, key, default=None):
        row = await self.fetchone(
            "SELECT value FROM guild_config WHERE guild_id=? AND key=?", (guild_id, key)
        )
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except (TypeError, json.JSONDecodeError):
            return row["value"]

    async def set_config(self, guild_id, key, value):
        await self.execute(
            "INSERT INTO guild_config (guild_id, key, value) VALUES (?, ?, ?) "
            "ON CONFLICT(guild_id, key) DO UPDATE SET value=excluded.value",
            (guild_id, key, json.dumps(value, ensure_ascii=False)),
        )

    async def del_config(self, guild_id, key):
        await self.execute(
            "DELETE FROM guild_config WHERE guild_id=? AND key=?", (guild_id, key)
        )

    async def all_config(self, guild_id):
        rows = await self.fetchall(
            "SELECT key, value FROM guild_config WHERE guild_id=?", (guild_id,)
        )
        out = {}
        for r in rows:
            try:
                out[r["key"]] = json.loads(r["value"])
            except (TypeError, json.JSONDecodeError):
                out[r["key"]] = r["value"]
        return out


async def create_case(db, guild_id, user_id, moderator_id, action, reason, duration=None):
    now = int(time.time())
    expires = now + duration if duration else None
    cur = await db.execute(
        "INSERT INTO cases (guild_id, user_id, moderator_id, action, reason, duration, created_at, expires_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (guild_id, user_id, moderator_id, action, reason, duration, now, expires),
    )
    return cur.lastrowid


async def schedule_task(db, task_type, payload, run_at, guild_id=None):
    cur = await db.execute(
        "INSERT INTO scheduled_tasks (guild_id, task_type, payload, run_at, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (guild_id, task_type, json.dumps(payload, ensure_ascii=False), run_at, int(time.time())),
    )
    return cur.lastrowid


# ============================================================================
# SCHEDULER
# ============================================================================

class Scheduler:
    def __init__(self, bot, db, tick=20):
        self.bot = bot
        self.db = db
        self.tick = tick
        self._handlers = {}
        self._task = None
        self._running = False

    def register(self, task_type, handler):
        self._handlers[task_type] = handler

    async def start(self):
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="scheduler")
        log.info("Scheduler started")

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    async def _loop(self):
        while self._running:
            try:
                await self._run_due()
            except Exception:
                log.exception("Scheduler tick failed")
            await asyncio.sleep(self.tick)

    async def _run_due(self):
        now = int(time.time())
        rows = await self.db.fetchall(
            "SELECT id, guild_id, task_type, payload FROM scheduled_tasks "
            "WHERE status='pending' AND run_at<=? ORDER BY run_at LIMIT 25",
            (now,),
        )
        for row in rows:
            claimed = await self.db.execute(
                "UPDATE scheduled_tasks SET status='done' WHERE id=? AND status='pending'",
                (row["id"],),
            )
            if claimed.rowcount == 0:
                continue
            handler = self._handlers.get(row["task_type"])
            if not handler:
                log.warning("No handler for %s", row["task_type"])
                continue
            try:
                payload = json.loads(row["payload"])
                await handler(self.bot, payload)
            except Exception:
                log.exception("Handler %s failed", row["task_type"])


# ============================================================================
# EMBEDS / ERRORS
# ============================================================================

BRAND = 0x5865F2


def embed_base(*, title=None, description=None, color=BRAND, timestamp=True):
    e = discord.Embed(color=color)
    if title is not None:
        e.title = render(title, limit=EMBED_TITLE_LIMIT)
    if description is not None:
        e.description = render(description, limit=EMBED_DESC_LIMIT)
    if timestamp:
        e.timestamp = datetime.datetime.now(datetime.timezone.utc)
    return e


def embed_success(desc):
    return embed_base(description=desc, color=0x57F287)


def embed_error(desc):
    return embed_base(description=desc, color=0xED4245)


def embed_info(desc):
    return embed_base(description=desc)


def embed_field(e, name, value, *, inline=False):
    e.add_field(
        name=render(name, limit=EMBED_FIELD_NAME_LIMIT) or "\u200b",
        value=render(value, limit=EMBED_FIELD_VALUE_LIMIT) or "\u200b",
        inline=inline,
    )
    return e


async def _err_reply(interaction, msg):
    try:
        e = embed_error(msg)
        if interaction.response.is_done():
            await interaction.followup.send(embed=e, ephemeral=True)
        else:
            await interaction.response.send_message(embed=e, ephemeral=True)
    except discord.HTTPException:
        pass


async def on_app_command_error(interaction, error):
    if isinstance(error, app_commands.MissingPermissions):
        missing = ", ".join(f"`{p}`" for p in error.missing_permissions)
        await _err_reply(interaction, f"You need: {missing}.")
        return
    if isinstance(error, app_commands.BotMissingPermissions):
        missing = ", ".join(f"`{p}`" for p in error.missing_permissions)
        await _err_reply(interaction, f"I'm missing: {missing}.")
        return
    if isinstance(error, app_commands.CommandOnCooldown):
        await _err_reply(interaction, f"Slow down — try again in `{error.retry_after:.1f}s`.")
        return
    if isinstance(error, app_commands.CheckFailure):
        await _err_reply(interaction, "You are not allowed to use this command.")
        return
    if isinstance(error, discord.Forbidden):
        await _err_reply(interaction, "I do not have permission to do that.")
        return
    if isinstance(error, discord.NotFound):
        await _err_reply(interaction, "That resource no longer exists.")
        return
    log.exception("Unhandled app command error", exc_info=error)
    await _err_reply(interaction, "Something went wrong. The error has been logged.")


# ============================================================================
# MODULE STATUS (for /setup wizard)
# ============================================================================

MODULE_STATUS_KEYS = [
    ("Welcome", "welcome.enabled"),
    ("Autorole", "autorole.enabled"),
    ("Autonick", "autonick.enabled"),
    ("Departure", "departure.enabled"),
    ("Action DMs", "actiondm.enabled"),
    ("VC Notifications", "vcnotify.enabled"),
    ("Logging", "logging.enabled"),
    ("Tickets", "ticket.enabled"),
    ("Giveaways", "giveaways.enabled"),
    ("Announcements", "announcements.enabled"),
    ("Custom Commands", "custom_commands.enabled"),
]


# ---------------------------------------------------------------- General
class General(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="ping", description="Show latency.")
    async def ping(self, interaction):
        ws = round(self.bot.latency * 1000)
        await interaction.response.send_message(embed=embed_info(f"WebSocket: `{ws} ms`"), ephemeral=True)

    @app_commands.command(name="serverinfo", description="Server information.")
    async def serverinfo(self, interaction):
        g = interaction.guild
        if not g:
            await interaction.response.send_message("Server-only.", ephemeral=True)
            return
        e = embed_base(title=g.name)
        if g.icon:
            e.set_thumbnail(url=g.icon.url)
        embed_field(e, "Owner", f"<@{g.owner_id}>", inline=True)
        embed_field(e, "Members", str(g.member_count), inline=True)
        embed_field(e, "Roles", str(len(g.roles)), inline=True)
        embed_field(e, "Channels", str(len(g.channels)), inline=True)
        embed_field(e, "Created", f"<t:{int(g.created_at.timestamp())}:R>", inline=True)
        embed_field(e, "ID", f"`{g.id}`", inline=True)
        await interaction.response.send_message(embed=e)

    @app_commands.command(name="userinfo", description="User information.")
    async def userinfo(self, interaction, user=None):
        target = user or interaction.user
        if not isinstance(target, discord.Member):
            await interaction.response.send_message("Not found.", ephemeral=True)
            return
        e = embed_base(title=str(target))
        e.set_thumbnail(url=target.display_avatar.url)
        embed_field(e, "ID", f"`{target.id}`", inline=True)
        embed_field(e, "Created", f"<t:{int(target.created_at.timestamp())}:R>", inline=True)
        if target.joined_at:
            embed_field(e, "Joined", f"<t:{int(target.joined_at.timestamp())}:R>", inline=True)
        roles = [r.mention for r in target.roles if r.name != "@everyone"]
        if roles:
            joined = " ".join(roles)
            embed_field(e, "Roles", joined[:1000])
        await interaction.response.send_message(embed=e)

    @app_commands.command(name="avatar", description="Show avatar.")
    async def avatar(self, interaction, user=None):
        t = user or interaction.user
        e = embed_base(title=f"{t.display_name}'s avatar")
        e.set_image(url=t.display_avatar.url)
        await interaction.response.send_message(embed=e)

    @app_commands.command(name="setup", description="FREAKOS setup wizard.")
    @app_commands.default_permissions(administrator=True)
    async def setup(self, interaction):
        if not interaction.guild:
            await interaction.response.send_message("Server-only.", ephemeral=True)
            return
        view = SetupWizard(self.bot, interaction.guild.id)
        await interaction.response.send_message(
            embed=await view.build_embed(), view=view, ephemeral=True
        )

    @app_commands.command(name="help", description="List FREAKOS commands.")
    async def help(self, interaction):
        lines = [
            "**General** — `/setup` `/help` `/ping` `/serverinfo` `/userinfo` `/avatar`",
            "**Welcome** — `/welcome setup|enable|disable|channel|message|embed|test|reset`",
            "**Autorole** — `/autorole setup|add|remove|list|enable|disable|reset`",
            "**Autonick** — `/autonick setup|enable|disable|format|reset`",
            "**Departure** — `/departure setup|enable|disable|channel|message|test|reset`",
            "**Action DMs** — `/actiondm setup|enable|disable|timeout|timeout-end|kick|ban|test|reset`",
            "**VC Notify** — `/vcnotify setup|enable|disable|add|remove|list|join-message|leave-message|test|reset`",
            "**Moderation** — `/warn /warnings /clearwarnings /timeout /untimeout /kick /ban /unban /purge /slowmode /lock /unlock /lockdown /unlockdown`",
            "**Logging** — `/logging setup|enable|disable|channel|events|reset`",
            "**Tickets** — `/ticket setup|panel|message|close|reopen|delete|add|remove|reset`",
            "**Custom Commands** — `/customcommand create|edit|delete|list|reset`",
            "**Announcements** — `/announce send|schedule|cancel`",
            "**Giveaways** — `/giveaway create|end|reroll|list`",
        ]
        e = embed_base(title="FREAKOS — Commands")
        e.description = "\n".join(lines)[:4096]
        await interaction.response.send_message(embed=e, ephemeral=True)


class SetupWizard(discord.ui.View):
    def __init__(self, bot, guild_id):
        super().__init__(timeout=300)
        self.bot = bot
        self.guild_id = guild_id
        options = [discord.SelectOption(label=n, value=n) for n, _ in MODULE_STATUS_KEYS]
        sel = discord.ui.Select(placeholder="Choose a module…", options=options)
        sel.callback = self._on_select
        self.sel = sel
        self.add_item(sel)

    async def build_embed(self):
        e = embed_base(title="FREAKOS Setup Wizard")
        lines = ["Use the dropdown to inspect a module.\n"]
        for name, key in MODULE_STATUS_KEYS:
            val = await self.bot.db.get_config(self.guild_id, key, False)
            lines.append(f"**{name}** — {'🟢 enabled' if val else '⚪ disabled'}")
        e.description = "\n".join(lines)[:4096]
        return e

    async def _on_select(self, interaction):
        name = self.sel.values[0]
        tips = {
            "Welcome": "`/welcome channel` then `/welcome message` (multiline).",
            "Autorole": "`/autorole add @role` then `/autorole enable`.",
            "Autonick": "`/autonick format {display_name}` then `/autonick enable`.",
            "Departure": "`/departure channel` then `/departure message` (multiline).",
            "Action DMs": "`/actiondm timeout|timeout-end|kick|ban` (multiline editors).",
            "VC Notifications": "`/vcnotify add #voice #text` then edit messages.",
            "Logging": "`/logging channel #channel` then `/logging enable`.",
            "Tickets": "`/ticket setup` then `/ticket panel`. Closed tickets auto-delete in 3 min.",
            "Giveaways": "`/giveaway create`.",
            "Announcements": "`/announce send` or `/announce schedule`.",
            "Custom Commands": "`/customcommand create`.",
        }
        await interaction.response.edit_message(
            embed=embed_info(tips.get(name, "")), view=self
        )


# ---------------------------------------------------------------- Welcome
DEFAULT_WELCOME = (
    "Hey {mention}!\n"
    "\n"
    "Welcome to {server}.\n"
    "\n"
    "We hope you enjoy your stay!"
)


def _member_ctx(m):
    return {
        "mention": m.mention, "user": str(m), "username": m.name,
        "display_name": m.display_name, "user_id": m.id,
        "server": m.guild.name, "server_name": m.guild.name,
        "server_id": m.guild.id, "member_count": m.guild.member_count,
    }


class Welcome(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    grp = app_commands.Group(name="welcome", description="Welcome system.",
                             default_permissions=discord.Permissions(administrator=True))

    @grp.command(name="setup")
    async def setup(self, interaction):
        cfg = await self.bot.db.all_config(interaction.guild.id)
        e = embed_base(title="Welcome — Configuration")
        embed_field(e, "Enabled", "🟢" if cfg.get("welcome.enabled") else "⚪", inline=True)
        ch = cfg.get("welcome.channel")
        embed_field(e, "Channel", f"<#{ch}>" if ch else "—", inline=True)
        embed_field(e, "Mode", "embed" if cfg.get("welcome.embed") else "text", inline=True)
        msg = cfg.get("welcome.message") or DEFAULT_WELCOME
        embed_field(e, "Message", f"```\n{msg[:900]}\n```")
        await interaction.response.send_message(embed=e, ephemeral=True)

    @grp.command(name="enable")
    async def enable(self, interaction):
        await self.bot.db.set_config(interaction.guild.id, "welcome.enabled", True)
        await interaction.response.send_message(embed=embed_success("Welcome enabled."), ephemeral=True)

    @grp.command(name="disable")
    async def disable(self, interaction):
        await self.bot.db.set_config(interaction.guild.id, "welcome.enabled", False)
        await interaction.response.send_message(embed=embed_success("Welcome disabled."), ephemeral=True)

    @grp.command(name="channel")
    async def channel(self, interaction, channel: discord.TextChannel):
        await self.bot.db.set_config(interaction.guild.id, "welcome.channel", channel.id)
        await interaction.response.send_message(embed=embed_success(f"Channel → {channel.mention}."), ephemeral=True)

    @grp.command(name="message")
    async def message(self, interaction):
        cur = await self.bot.db.get_config(interaction.guild.id, "welcome.message", DEFAULT_WELCOME)
        await interaction.response.send_modal(_TextModal("welcome.message", "Welcome message", cur))

    @grp.command(name="embed")
    async def embed_toggle(self, interaction):
        cur = await self.bot.db.get_config(interaction.guild.id, "welcome.embed", False)
        await self.bot.db.set_config(interaction.guild.id, "welcome.embed", not cur)
        await interaction.response.send_message(
            embed=embed_success(f"Embed mode {'disabled' if cur else 'enabled'}."), ephemeral=True
        )

    @grp.command(name="test")
    async def test(self, interaction):
        assert isinstance(interaction.user, discord.Member)
        await self._deliver(interaction.guild, interaction.user, interaction.channel)
        await interaction.response.send_message(embed=embed_success("Test sent."), ephemeral=True)

    @grp.command(name="reset")
    async def reset(self, interaction):
        for k in ("welcome.enabled", "welcome.channel", "welcome.message", "welcome.embed"):
            await self.bot.db.del_config(interaction.guild.id, k)
        await interaction.response.send_message(embed=embed_success("Welcome reset."), ephemeral=True)

    async def _deliver(self, guild, member, override_channel=None):
        cfg = await self.bot.db.all_config(guild.id)
        if not cfg.get("welcome.enabled"):
            return
        ch_id = cfg.get("welcome.channel")
        ch = override_channel or (guild.get_channel(int(ch_id)) if ch_id else None)
        if not isinstance(ch, discord.TextChannel):
            return
        rendered = render(cfg.get("welcome.message") or DEFAULT_WELCOME, _member_ctx(member))
        if cfg.get("welcome.embed"):
            e = embed_base(description=rendered)
            e.set_thumbnail(url=member.display_avatar.url)
            await ch.send(content=member.mention, embed=e)
        else:
            await ch.send(content=rendered[:2000])

    @commands.Cog.listener()
    async def on_member_join(self, member):
        await self._deliver(member.guild, member)


class _TextModal(discord.ui.Modal):
    def __init__(self, config_key, title, current):
        super().__init__(title=title)
        self.config_key = config_key
        self.input = discord.ui.TextInput(
            label="Message (multiline supported)",
            style=discord.TextStyle.paragraph,
            default=current[:4000], max_length=4000, required=True,
        )
        self.add_item(self.input)

    async def on_submit(self, interaction):
        await interaction.client.db.set_config(
            interaction.guild.id, self.config_key, str(self.input.value)
        )
        await interaction.response.send_message(
            embed=embed_success("Saved. Formatting preserved."), ephemeral=True
        )


# ---------------------------------------------------------------- Autorole
class Autorole(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    grp = app_commands.Group(name="autorole", description="Auto-role on join.",
                             default_permissions=discord.Permissions(administrator=True))

    @grp.command(name="setup")
    async def setup(self, interaction):
        enabled = await self.bot.db.get_config(interaction.guild.id, "autorole.enabled", False)
        rows = await self.bot.db.fetchall("SELECT role_id FROM autoroles WHERE guild_id=?", (interaction.guild.id,))
        e = embed_base(title="Autorole")
        embed_field(e, "Enabled", "🟢" if enabled else "⚪", inline=True)
        embed_field(e, "Roles", "\n".join(f"<@&{r['role_id']}>" for r in rows) or "— none")
        await interaction.response.send_message(embed=e, ephemeral=True)

    @grp.command(name="add")
    async def add(self, interaction, role: discord.Role):
        if role.is_default() or role.managed or role >= interaction.guild.me.top_role:
            await interaction.response.send_message(embed=embed_error("Cannot assign that role."), ephemeral=True)
            return
        await self.bot.db.execute("INSERT OR IGNORE INTO autoroles (guild_id, role_id) VALUES (?, ?)",
                                  (interaction.guild.id, role.id))
        await interaction.response.send_message(embed=embed_success(f"Added {role.mention}."), ephemeral=True)

    @grp.command(name="remove")
    async def remove(self, interaction, role: discord.Role):
        await self.bot.db.execute("DELETE FROM autoroles WHERE guild_id=? AND role_id=?",
                                  (interaction.guild.id, role.id))
        await interaction.response.send_message(embed=embed_success("Removed."), ephemeral=True)

    @grp.command(name="list")
    async def list_(self, interaction):
        rows = await self.bot.db.fetchall("SELECT role_id FROM autoroles WHERE guild_id=?", (interaction.guild.id,))
        await interaction.response.send_message(
            "\n".join(f"<@&{r['role_id']}>" for r in rows) or "No autoroles.", ephemeral=True
        )

    @grp.command(name="enable")
    async def enable(self, interaction):
        await self.bot.db.set_config(interaction.guild.id, "autorole.enabled", True)
        await interaction.response.send_message(embed=embed_success("Enabled."), ephemeral=True)

    @grp.command(name="disable")
    async def disable(self, interaction):
        await self.bot.db.set_config(interaction.guild.id, "autorole.enabled", False)
        await interaction.response.send_message(embed=embed_success("Disabled."), ephemeral=True)

    @grp.command(name="reset")
    async def reset(self, interaction):
        await self.bot.db.execute("DELETE FROM autoroles WHERE guild_id=?", (interaction.guild.id,))
        await self.bot.db.del_config(interaction.guild.id, "autorole.enabled")
        await interaction.response.send_message(embed=embed_success("Reset."), ephemeral=True)

    @commands.Cog.listener()
    async def on_member_join(self, member):
        g = member.guild
        if not await self.bot.db.get_config(g.id, "autorole.enabled", False):
            return
        rows = await self.bot.db.fetchall("SELECT role_id FROM autoroles WHERE guild_id=?", (g.id,))
        roles = []
        for r in rows:
            role = g.get_role(r["role_id"])
            if role and not role.managed and role < g.me.top_role:
                roles.append(role)
        if roles:
            try:
                await member.add_roles(*roles, reason="Autorole")
            except discord.Forbidden:
                pass


# ---------------------------------------------------------------- Autonick
class Autonick(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    grp = app_commands.Group(name="autonick", description="Auto-nickname on join.",
                             default_permissions=discord.Permissions(administrator=True))

    @grp.command(name="setup")
    async def setup(self, interaction):
        enabled = await self.bot.db.get_config(interaction.guild.id, "autonick.enabled", False)
        fmt = await self.bot.db.get_config(interaction.guild.id, "autonick.format", "{display_name}")
        e = embed_base(title="Autonick")
        embed_field(e, "Enabled", "🟢" if enabled else "⚪", inline=True)
        embed_field(e, "Format", f"`{fmt}`")
        await interaction.response.send_message(embed=e, ephemeral=True)

    @grp.command(name="enable")
    async def enable(self, interaction):
        await self.bot.db.set_config(interaction.guild.id, "autonick.enabled", True)
        await interaction.response.send_message(embed=embed_success("Enabled."), ephemeral=True)

    @grp.command(name="disable")
    async def disable(self, interaction):
        await self.bot.db.set_config(interaction.guild.id, "autonick.enabled", False)
        await interaction.response.send_message(embed=embed_success("Disabled."), ephemeral=True)

    @grp.command(name="format")
    async def format_(self, interaction, fmt: str):
        if len(fmt) > 32:
            await interaction.response.send_message(embed=embed_error("Max 32 chars."), ephemeral=True)
            return
        await self.bot.db.set_config(interaction.guild.id, "autonick.format", fmt)
        await interaction.response.send_message(embed=embed_success("Saved."), ephemeral=True)

    @grp.command(name="reset")
    async def reset(self, interaction):
        await self.bot.db.del_config(interaction.guild.id, "autonick.enabled")
        await self.bot.db.del_config(interaction.guild.id, "autonick.format")
        await interaction.response.send_message(embed=embed_success("Reset."), ephemeral=True)

    @commands.Cog.listener()
    async def on_member_join(self, member):
        g = member.guild
        if not await self.bot.db.get_config(g.id, "autonick.enabled", False):
            return
        if not g.me.guild_permissions.manage_nicknames:
            return
        if member.top_role >= g.me.top_role:
            return
        fmt = await self.bot.db.get_config(g.id, "autonick.format", "{display_name}")
        ctx = {
            "user": member.name, "username": member.name,
            "display_name": member.display_name,
            "server": g.name, "server_name": g.name,
        }
        nick = render(fmt, ctx, convert_literal_newlines=False)[:32]
        try:
            await member.edit(nick=nick, reason="Autonick")
        except discord.Forbidden:
            pass


# ---------------------------------------------------------------- Departure
DEFAULT_DEPARTURE = (
    "Goodbye {user}!\n"
    "\n"
    "We are sorry to see you leave {server}.\n"
    "\n"
    "You will always be welcome back."
)


class Departure(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    grp = app_commands.Group(name="departure", description="Departure system.",
                             default_permissions=discord.Permissions(administrator=True))

    @grp.command(name="setup")
    async def setup(self, interaction):
        cfg = await self.bot.db.all_config(interaction.guild.id)
        e = embed_base(title="Departure")
        embed_field(e, "Enabled", "🟢" if cfg.get("departure.enabled") else "⚪", inline=True)
        embed_field(e, "Channel", f"<#{cfg.get('departure.channel')}>" if cfg.get("departure.channel") else "—", inline=True)
        embed_field(e, "Message", f"```\n{(cfg.get('departure.message') or DEFAULT_DEPARTURE)[:900]}\n```")
        await interaction.response.send_message(embed=e, ephemeral=True)

    @grp.command(name="enable")
    async def enable(self, interaction):
        await self.bot.db.set_config(interaction.guild.id, "departure.enabled", True)
        await interaction.response.send_message(embed=embed_success("Enabled."), ephemeral=True)

    @grp.command(name="disable")
    async def disable(self, interaction):
        await self.bot.db.set_config(interaction.guild.id, "departure.enabled", False)
        await interaction.response.send_message(embed=embed_success("Disabled."), ephemeral=True)

    @grp.command(name="channel")
    async def channel(self, interaction, channel: discord.TextChannel):
        await self.bot.db.set_config(interaction.guild.id, "departure.channel", channel.id)
        await interaction.response.send_message(embed=embed_success(f"Channel → {channel.mention}."), ephemeral=True)

    @grp.command(name="message")
    async def message(self, interaction):
        cur = await self.bot.db.get_config(interaction.guild.id, "departure.message", DEFAULT_DEPARTURE)
        await interaction.response.send_modal(_TextModal("departure.message", "Departure message", cur))

    @grp.command(name="test")
    async def test(self, interaction):
        assert isinstance(interaction.user, discord.Member)
        await self._deliver(interaction.guild, interaction.user, interaction.channel)
        await interaction.response.send_message(embed=embed_success("Test sent."), ephemeral=True)

    @grp.command(name="reset")
    async def reset(self, interaction):
        for k in ("departure.enabled", "departure.channel", "departure.message", "departure.embed"):
            await self.bot.db.del_config(interaction.guild.id, k)
        await interaction.response.send_message(embed=embed_success("Reset."), ephemeral=True)

    async def _deliver(self, guild, member, override_channel=None):
        cfg = await self.bot.db.all_config(guild.id)
        if not cfg.get("departure.enabled"):
            return
        ch = override_channel or (guild.get_channel(int(cfg["departure.channel"])) if cfg.get("departure.channel") else None)
        if not isinstance(ch, discord.TextChannel):
            return
        rendered = render(cfg.get("departure.message") or DEFAULT_DEPARTURE, _member_ctx(member))
        if cfg.get("departure.embed"):
            e = embed_base(description=rendered)
            e.set_thumbnail(url=member.display_avatar.url)
            await ch.send(embed=e)
        else:
            await ch.send(content=rendered[:2000])

    @commands.Cog.listener()
    async def on_member_remove(self, member):
        await self._deliver(member.guild, member)


# ---------------------------------------------------------------- ActionDM
ACTIONDM_DEFAULTS = {
    "timeout": (
        "You have been timed out in {server}.\n"
        "\n"
        "Reason:\n{reason}\n"
        "\n"
        "Duration:\n{duration}\n"
        "\n"
        "Moderator:\n{moderator}"
    ),
    "timeout_end": "Your timeout in {server} has ended.\n\nPlease follow the rules from now on.",
    "kick": "You were kicked from {server}.\n\nReason:\n{reason}",
    "ban": "You were banned from {server}.\n\nReason:\n{reason}",
}


class ActionDM(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    grp = app_commands.Group(name="actiondm", description="Moderation action DMs.",
                             default_permissions=discord.Permissions(administrator=True))

    @grp.command(name="setup")
    async def setup(self, interaction):
        cfg = await self.bot.db.all_config(interaction.guild.id)
        e = embed_base(title="Action DMs")
        embed_field(e, "Enabled", "🟢" if cfg.get("actiondm.enabled") else "⚪", inline=True)
        for k, default in ACTIONDM_DEFAULTS.items():
            body = cfg.get(f"actiondm.{k}") or default
            embed_field(e, k.replace("_", " ").title(), f"```\n{body[:400]}\n```")
        await interaction.response.send_message(embed=e, ephemeral=True)

    @grp.command(name="enable")
    async def enable(self, interaction):
        await self.bot.db.set_config(interaction.guild.id, "actiondm.enabled", True)
        await interaction.response.send_message(embed=embed_success("Enabled."), ephemeral=True)

    @grp.command(name="disable")
    async def disable(self, interaction):
        await self.bot.db.set_config(interaction.guild.id, "actiondm.enabled", False)
        await interaction.response.send_message(embed=embed_success("Disabled."), ephemeral=True)

    async def _edit(self, interaction, key, title):
        cur = await self.bot.db.get_config(interaction.guild.id, f"actiondm.{key}", ACTIONDM_DEFAULTS[key])
        await interaction.response.send_modal(_TextModal(f"actiondm.{key}", title, cur))

    @grp.command(name="timeout")
    async def timeout(self, interaction):
        await self._edit(interaction, "timeout", "Timeout DM")

    @grp.command(name="timeout-end")
    async def timeout_end(self, interaction):
        await self._edit(interaction, "timeout_end", "Timeout-end DM")

    @grp.command(name="kick")
    async def kick(self, interaction):
        await self._edit(interaction, "kick", "Kick DM")

    @grp.command(name="ban")
    async def ban(self, interaction):
        await self._edit(interaction, "ban", "Ban DM")

    @grp.command(name="test")
    async def test(self, interaction):
        cfg = await self.bot.db.all_config(interaction.guild.id)
        ctx = self._ctx(interaction.guild, interaction.user, None, "Example reason.", None, None)
        chunks = []
        for k, d in ACTIONDM_DEFAULTS.items():
            body = cfg.get(f"actiondm.{k}") or d
            chunks.append(f"### {k}\n{render(body, ctx)}")
        try:
            await interaction.user.send("\n\n".join(chunks)[:2000])
        except discord.Forbidden:
            await interaction.response.send_message("DMs closed.", ephemeral=True)
            return
        await interaction.response.send_message(embed=embed_success("Test sent."), ephemeral=True)

    @grp.command(name="reset")
    async def reset(self, interaction):
        for k in list(ACTIONDM_DEFAULTS) + ["enabled"]:
            await self.bot.db.del_config(interaction.guild.id, f"actiondm.{k}")
        await interaction.response.send_message(embed=embed_success("Reset."), ephemeral=True)

    @staticmethod
    def _ctx(guild, user, moderator, reason, duration, case_id):
        return {
            "user": str(user) if user else "",
            "username": getattr(user, "name", ""),
            "display_name": getattr(user, "display_name", ""),
            "mention": getattr(user, "mention", ""),
            "user_id": getattr(user, "id", ""),
            "server": guild.name, "server_name": guild.name, "server_id": guild.id,
            "moderator": str(moderator) if moderator else "",
            "reason": reason or "No reason provided",
            "duration": duration or "", "case_id": case_id or "", "timeout_end": "",
        }

    async def send_action_dm(self, guild, user, action, *, moderator=None, reason=None,
                             duration=None, case_id=None, timeout_end=None):
        if not await self.bot.db.get_config(guild.id, "actiondm.enabled", False):
            return False
        template = await self.bot.db.get_config(guild.id, f"actiondm.{action}") or ACTIONDM_DEFAULTS.get(action)
        if not template:
            return False
        ctx = self._ctx(guild, user, moderator, reason, duration, case_id)
        if timeout_end:
            ctx["timeout_end"] = timeout_end
        body = render(template, ctx)
        try:
            await user.send(body[:2000])
            return True
        except (discord.Forbidden, discord.HTTPException):
            return False

    async def register_timeout_end(self, guild_id, user_id, case_id, expires_at):
        await self.bot.db.execute(
            "INSERT OR REPLACE INTO timeout_dms (case_id, guild_id, user_id, expires_at, sent) VALUES (?, ?, ?, ?, 0)",
            (case_id, guild_id, user_id, expires_at),
        )
        await schedule_task(self.bot.db, "timeout_end_dm",
                            {"case_id": case_id, "guild_id": guild_id, "user_id": user_id},
                            run_at=expires_at, guild_id=guild_id)


# ---------------------------------------------------------------- VCNotify
class VCNotify(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    grp = app_commands.Group(name="vcnotify", description="Voice channel notifications.",
                             default_permissions=discord.Permissions(administrator=True))

    @grp.command(name="setup")
    async def setup(self, interaction):
        enabled = await self.bot.db.get_config(interaction.guild.id, "vcnotify.enabled", False)
        rows = await self.bot.db.fetchall("SELECT * FROM vc_notify WHERE guild_id=?", (interaction.guild.id,))
        e = embed_base(title="VC Notifications")
        embed_field(e, "Enabled", "🟢" if enabled else "⚪", inline=True)
        if rows:
            e.add_field(
                name="Channels",
                value="\n".join(
                    f"<#{r['voice_channel_id']}> → " + (f"<#{r['text_channel_id']}>" if r["text_channel_id"] else "voice chat")
                    for r in rows
                )[:1024],
            )
        else:
            e.add_field(name="Channels", value="— none")
        await interaction.response.send_message(embed=e, ephemeral=True)

    @grp.command(name="enable")
    async def enable(self, interaction):
        await self.bot.db.set_config(interaction.guild.id, "vcnotify.enabled", True)
        await interaction.response.send_message(embed=embed_success("Enabled."), ephemeral=True)

    @grp.command(name="disable")
    async def disable(self, interaction):
        await self.bot.db.set_config(interaction.guild.id, "vcnotify.enabled", False)
        await interaction.response.send_message(embed=embed_success("Disabled."), ephemeral=True)

    @grp.command(name="add")
    async def add(self, interaction, voice: discord.VoiceChannel, text: discord.TextChannel = None):
        await self.bot.db.execute(
            "INSERT OR REPLACE INTO vc_notify (guild_id, voice_channel_id, text_channel_id, join_message, leave_message) "
            "VALUES (?, ?, ?, COALESCE((SELECT join_message FROM vc_notify WHERE guild_id=? AND voice_channel_id=?), ?), "
            "COALESCE((SELECT leave_message FROM vc_notify WHERE guild_id=? AND voice_channel_id=?), ?))",
            (interaction.guild.id, voice.id, text.id if text else None,
             interaction.guild.id, voice.id, "{mention} joined {channel_name}.\n\nWelcome!",
             interaction.guild.id, voice.id, "{mention} left {channel_name}.\n\nSee you later!"),
        )
        await interaction.response.send_message(embed=embed_success(f"Monitoring {voice.mention}."), ephemeral=True)

    @grp.command(name="remove")
    async def remove(self, interaction, voice: discord.VoiceChannel):
        await self.bot.db.execute("DELETE FROM vc_notify WHERE guild_id=? AND voice_channel_id=?",
                                  (interaction.guild.id, voice.id))
        await interaction.response.send_message(embed=embed_success("Removed."), ephemeral=True)

    @grp.command(name="list")
    async def list_(self, interaction):
        rows = await self.bot.db.fetchall("SELECT * FROM vc_notify WHERE guild_id=?", (interaction.guild.id,))
        if not rows:
            await interaction.response.send_message("None monitored.", ephemeral=True)
            return
        e = embed_base(title="Monitored VCs")
        e.description = "\n".join(
            f"<#{r['voice_channel_id']}> → " + (f"<#{r['text_channel_id']}>" if r["text_channel_id"] else "voice chat")
            for r in rows
        )[:4096]
        await interaction.response.send_message(embed=e, ephemeral=True)

    async def _edit_msg(self, interaction, voice, column, title):
        row = await self.bot.db.fetchone(
            "SELECT * FROM vc_notify WHERE guild_id=? AND voice_channel_id=?",
            (interaction.guild.id, voice.id),
        )
        if not row:
            await interaction.response.send_message(embed=embed_error("Not monitored."), ephemeral=True)
            return
        await interaction.response.send_modal(_VCModal(column, voice.id, title, row[column] or ""))

    @grp.command(name="join-message")
    async def join_message(self, interaction, voice: discord.VoiceChannel):
        await self._edit_msg(interaction, voice, "join_message", "Join message")

    @grp.command(name="leave-message")
    async def leave_message(self, interaction, voice: discord.VoiceChannel):
        await self._edit_msg(interaction, voice, "leave_message", "Leave message")

    @grp.command(name="test")
    async def test(self, interaction, voice: discord.VoiceChannel):
        row = await self.bot.db.fetchone(
            "SELECT * FROM vc_notify WHERE guild_id=? AND voice_channel_id=?",
            (interaction.guild.id, voice.id),
        )
        if not row:
            await interaction.response.send_message(embed=embed_error("Not monitored."), ephemeral=True)
            return
        ctx = {"mention": interaction.user.mention, "user": str(interaction.user), "channel_name": voice.name}
        await interaction.channel.send(render(row["join_message"] or "", ctx))
        await interaction.response.send_message(embed=embed_success("Test sent."), ephemeral=True)

    @grp.command(name="reset")
    async def reset(self, interaction):
        await self.bot.db.execute("DELETE FROM vc_notify WHERE guild_id=?", (interaction.guild.id,))
        await self.bot.db.del_config(interaction.guild.id, "vcnotify.enabled")
        await interaction.response.send_message(embed=embed_success("Reset."), ephemeral=True)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if member.bot:
            return
        g = member.guild
        if not await self.bot.db.get_config(g.id, "vcnotify.enabled", False):
            return
        if before.channel and before.channel != after.channel:
            await self._notify(g, before.channel, member, "leave_message")
        if after.channel and after.channel != before.channel:
            await self._notify(g, after.channel, member, "join_message")

    async def _notify(self, guild, vc, member, key):
        row = await self.bot.db.fetchone(
            "SELECT * FROM vc_notify WHERE guild_id=? AND voice_channel_id=?",
            (guild.id, vc.id),
        )
        if not row:
            return
        template = row[key] or ""
        if not template:
            return
        target = guild.get_channel(row["text_channel_id"]) if row["text_channel_id"] else vc
        if not isinstance(target, discord.TextChannel):
            return
        ctx = {
            "mention": member.mention, "user": str(member),
            "username": member.name, "display_name": member.display_name,
            "channel_name": vc.name, "channel": vc.mention,
            "server": guild.name, "server_name": guild.name,
        }
        try:
            await target.send(render(template, ctx)[:2000])
        except discord.Forbidden:
            pass


class _VCModal(discord.ui.Modal):
    def __init__(self, column, vc_id, title, current):
        super().__init__(title=title)
        self.column = column
        self.vc_id = vc_id
        self.input = discord.ui.TextInput(
            label="Message (multiline supported)",
            style=discord.TextStyle.paragraph,
            default=current[:4000], max_length=4000, required=True,
        )
        self.add_item(self.input)

    async def on_submit(self, interaction):
        assert self.column in ("join_message", "leave_message")
        await interaction.client.db.execute(
            f"UPDATE vc_notify SET {self.column}=? WHERE guild_id=? AND voice_channel_id=?",
            (str(self.input.value), interaction.guild.id, self.vc_id),
        )
        await interaction.response.send_message(embed=embed_success("Saved."), ephemeral=True)


# ---------------------------------------------------------------- Moderation
def _check_hierarchy(guild, mod, target):
    if mod.id == target.id:
        return "You cannot moderate yourself."
    if target.id == guild.owner_id:
        return "You cannot moderate the owner."
    if mod.id != guild.owner_id and mod.top_role <= target.top_role:
        return "Target has an equal or higher role."
    if guild.me and guild.me.id != guild.owner_id and guild.me.top_role <= target.top_role:
        return "My role is too low to moderate that member."
    return None


def _fmt_dur(s):
    out = []
    for u, d in (("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        if s >= d:
            out.append(f"{s // d}{u}")
            s %= d
    return " ".join(out) or "0s"


class Moderation(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="warn", description="Warn a member.")
    @app_commands.default_permissions(moderate_members=True)
    async def warn(self, interaction, member: discord.Member, reason: str):
        err = _check_hierarchy(interaction.guild, interaction.user, member)
        if err:
            await interaction.response.send_message(embed=embed_error(err), ephemeral=True)
            return
        cid = await create_case(self.bot.db, interaction.guild.id, member.id, interaction.user.id, "warn", reason)
        try:
            await member.send(f"Warned in **{interaction.guild.name}**.\n\nReason:\n{reason}")
        except discord.HTTPException:
            pass
        e = embed_base(title=f"⚠️ Warning #{cid}", color=0xFEE75C)
        embed_field(e, "User", member.mention, inline=True)
        embed_field(e, "Moderator", interaction.user.mention, inline=True)
        embed_field(e, "Reason", reason)
        await interaction.response.send_message(embed=e)

    @app_commands.command(name="warnings", description="List warnings for a member.")
    @app_commands.default_permissions(moderate_members=True)
    async def warnings(self, interaction, member: discord.Member):
        rows = await self.bot.db.fetchall(
            "SELECT id, moderator_id, reason, created_at FROM cases "
            "WHERE guild_id=? AND user_id=? AND action='warn' ORDER BY id DESC LIMIT 25",
            (interaction.guild.id, member.id),
        )
        if not rows:
            await interaction.response.send_message(f"{member.mention} has no warnings.", ephemeral=True)
            return
        e = embed_base(title=f"Warnings for {member}")
        for r in rows:
            embed_field(e, f"Case #{r['id']} — <t:{r['created_at']}:R>",
                        f"Mod: <@{r['moderator_id']}>\nReason: {r['reason'] or '—'}")
        await interaction.response.send_message(embed=e, ephemeral=True)

    @app_commands.command(name="clearwarnings", description="Clear warnings.")
    @app_commands.default_permissions(administrator=True)
    async def clearwarnings(self, interaction, member: discord.Member):
        cur = await self.bot.db.execute(
            "DELETE FROM cases WHERE guild_id=? AND user_id=? AND action='warn'",
            (interaction.guild.id, member.id),
        )
        await interaction.response.send_message(embed=embed_success(f"Cleared {cur.rowcount}."), ephemeral=True)

    @app_commands.command(name="timeout", description="Timeout a member.")
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.choices(unit=[
        app_commands.Choice(name="seconds", value=1),
        app_commands.Choice(name="minutes", value=60),
        app_commands.Choice(name="hours", value=3600),
        app_commands.Choice(name="days", value=86400),
    ])
    async def timeout(self, interaction, member: discord.Member, duration: int,
                      unit: app_commands.Choice[int], reason: str = "No reason"):
        err = _check_hierarchy(interaction.guild, interaction.user, member)
        if err:
            await interaction.response.send_message(embed=embed_error(err), ephemeral=True)
            return
        sec = duration * unit.value
        if sec <= 0 or sec > 28 * 86400:
            await interaction.response.send_message(embed=embed_error("1s to 28d."), ephemeral=True)
            return
        ends = discord.utils.utcnow() + datetime.timedelta(seconds=sec)
        adm = self.bot.get_cog("ActionDM")
        if adm:
            await adm.send_action_dm(interaction.guild, member, "timeout",
                                     moderator=interaction.user, reason=reason,
                                     duration=_fmt_dur(sec),
                                     timeout_end=f"<t:{int(ends.timestamp())}:R>")
        try:
            await member.timeout(datetime.timedelta(seconds=sec), reason=reason)
        except discord.Forbidden:
            await interaction.response.send_message(embed=embed_error("Can't timeout."), ephemeral=True)
            return
        cid = await create_case(self.bot.db, interaction.guild.id, member.id,
                                interaction.user.id, "timeout", reason, duration=sec)
        if adm:
            await adm.register_timeout_end(interaction.guild.id, member.id, cid, int(ends.timestamp()))
        e = embed_base(title=f"⏱ Timeout — Case #{cid}")
        embed_field(e, "User", member.mention, inline=True)
        embed_field(e, "Duration", _fmt_dur(sec), inline=True)
        embed_field(e, "Reason", reason)
        await interaction.response.send_message(embed=e)

    @app_commands.command(name="untimeout", description="Remove timeout.")
    @app_commands.default_permissions(moderate_members=True)
    async def untimeout(self, interaction, member: discord.Member, reason: str = "No reason"):
        err = _check_hierarchy(interaction.guild, interaction.user, member)
        if err:
            await interaction.response.send_message(embed=embed_error(err), ephemeral=True)
            return
        try:
            await member.timeout(None, reason=reason)
        except discord.Forbidden:
            await interaction.response.send_message(embed=embed_error("Failed."), ephemeral=True)
            return
        await interaction.response.send_message(embed=embed_success("Timeout removed."))

    @app_commands.command(name="kick", description="Kick a member.")
    @app_commands.default_permissions(kick_members=True)
    async def kick(self, interaction, member: discord.Member, reason: str = "No reason"):
        err = _check_hierarchy(interaction.guild, interaction.user, member)
        if err:
            await interaction.response.send_message(embed=embed_error(err), ephemeral=True)
            return
        adm = self.bot.get_cog("ActionDM")
        if adm:
            await adm.send_action_dm(interaction.guild, member, "kick", moderator=interaction.user, reason=reason)
        try:
            await member.kick(reason=reason)
        except discord.Forbidden:
            await interaction.response.send_message(embed=embed_error("Can't kick."), ephemeral=True)
            return
        cid = await create_case(self.bot.db, interaction.guild.id, member.id, interaction.user.id, "kick", reason)
        e = embed_base(title=f"👢 Kick — Case #{cid}")
        embed_field(e, "User", str(member), inline=True)
        embed_field(e, "Reason", reason)
        await interaction.response.send_message(embed=e)

    @app_commands.command(name="ban", description="Ban a member.")
    @app_commands.default_permissions(ban_members=True)
    async def ban(self, interaction, member: discord.Member, reason: str = "No reason"):
        err = _check_hierarchy(interaction.guild, interaction.user, member)
        if err:
            await interaction.response.send_message(embed=embed_error(err), ephemeral=True)
            return
        adm = self.bot.get_cog("ActionDM")
        if adm:
            await adm.send_action_dm(interaction.guild, member, "ban", moderator=interaction.user, reason=reason)
        try:
            await member.ban(reason=reason)
        except discord.Forbidden:
            await interaction.response.send_message(embed=embed_error("Can't ban."), ephemeral=True)
            return
        cid = await create_case(self.bot.db, interaction.guild.id, member.id, interaction.user.id, "ban", reason)
        e = embed_base(title=f"🔨 Ban — Case #{cid}")
        embed_field(e, "User", str(member), inline=True)
        embed_field(e, "Reason", reason)
        await interaction.response.send_message(embed=e)

    @app_commands.command(name="unban", description="Unban by user ID.")
    @app_commands.default_permissions(ban_members=True)
    async def unban(self, interaction, user_id: str, reason: str = "No reason"):
        try:
            uid = int(user_id)
            user = await self.bot.fetch_user(uid)
            await interaction.guild.unban(user, reason=reason)
        except (ValueError, discord.NotFound, discord.HTTPException):
            await interaction.response.send_message(embed=embed_error("Failed."), ephemeral=True)
            return
        await interaction.response.send_message(embed=embed_success(f"Unbanned {user}."))

    @app_commands.command(name="purge", description="Bulk delete messages.")
    @app_commands.default_permissions(manage_messages=True)
    async def purge(self, interaction, amount: int, member: discord.Member = None):
        amount = max(1, min(amount, 500))
        await interaction.response.defer(ephemeral=True)
        check = (lambda m: m.author.id == member.id) if member else None
        deleted = await interaction.channel.purge(limit=amount, check=check)
        await interaction.followup.send(embed=embed_success(f"Deleted {len(deleted)}."), ephemeral=True)

    @app_commands.command(name="slowmode", description="Set slowmode.")
    @app_commands.default_permissions(manage_channels=True)
    async def slowmode(self, interaction, seconds: int):
        seconds = max(0, min(seconds, 21600))
        await interaction.channel.edit(slowmode_delay=seconds)
        await interaction.response.send_message(embed=embed_success(f"Slowmode: {seconds}s."))

    @app_commands.command(name="lock", description="Lock the channel.")
    @app_commands.default_permissions(manage_channels=True)
    async def lock(self, interaction, channel: discord.TextChannel = None):
        ch = channel or interaction.channel
        await ch.set_permissions(interaction.guild.default_role, send_messages=False)
        await interaction.response.send_message(embed=embed_success(f"🔒 {ch.mention}"))

    @app_commands.command(name="unlock", description="Unlock the channel.")
    @app_commands.default_permissions(manage_channels=True)
    async def unlock(self, interaction, channel: discord.TextChannel = None):
        ch = channel or interaction.channel
        await ch.set_permissions(interaction.guild.default_role, send_messages=None)
        await interaction.response.send_message(embed=embed_success(f"🔓 {ch.mention}"))

    @app_commands.command(name="lockdown", description="Lock all text channels.")
    @app_commands.default_permissions(administrator=True)
    async def lockdown(self, interaction):
        await interaction.response.defer(ephemeral=True)
        n = 0
        for ch in interaction.guild.text_channels:
            try:
                await ch.set_permissions(interaction.guild.default_role, send_messages=False)
                n += 1
            except discord.Forbidden:
                pass
        await interaction.followup.send(embed=embed_success(f"Locked {n}."), ephemeral=True)

    @app_commands.command(name="unlockdown", description="Unlock all text channels.")
    @app_commands.default_permissions(administrator=True)
    async def unlockdown(self, interaction):
        await interaction.response.defer(ephemeral=True)
        n = 0
        for ch in interaction.guild.text_channels:
            try:
                await ch.set_permissions(interaction.guild.default_role, send_messages=None)
                n += 1
            except discord.Forbidden:
                pass
        await interaction.followup.send(embed=embed_success(f"Unlocked {n}."), ephemeral=True)


# ---------------------------------------------------------------- Logging
LOG_EVENTS = ["member_join", "member_leave", "message_delete", "message_edit",
              "warnings", "timeouts", "kicks", "bans", "role_changes"]


class GuildLogging(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    grp = app_commands.Group(name="logging", description="Event logging.",
                             default_permissions=discord.Permissions(administrator=True))

    async def _send(self, guild, event, embed):
        cfg = await self.bot.db.all_config(guild.id)
        if not cfg.get("logging.enabled"):
            return
        events = cfg.get("logging.events") or ["all"]
        if "all" not in events and event not in events:
            return
        ch_id = cfg.get("logging.channel")
        if not ch_id:
            return
        ch = guild.get_channel(int(ch_id))
        if not isinstance(ch, discord.TextChannel):
            return
        try:
            await ch.send(embed=embed)
        except discord.HTTPException:
            pass

    @grp.command(name="setup")
    async def setup(self, interaction):
        cfg = await self.bot.db.all_config(interaction.guild.id)
        e = embed_base(title="Logging")
        embed_field(e, "Enabled", "🟢" if cfg.get("logging.enabled") else "⚪", inline=True)
        ch = cfg.get("logging.channel")
        embed_field(e, "Channel", f"<#{ch}>" if ch else "—", inline=True)
        events = cfg.get("logging.events") or ["all"]
        embed_field(e, "Events", ", ".join(f"`{x}`" for x in events))
        await interaction.response.send_message(embed=e, ephemeral=True)

    @grp.command(name="enable")
    async def enable(self, interaction):
        await self.bot.db.set_config(interaction.guild.id, "logging.enabled", True)
        await interaction.response.send_message(embed=embed_success("Enabled."), ephemeral=True)

    @grp.command(name="disable")
    async def disable(self, interaction):
        await self.bot.db.set_config(interaction.guild.id, "logging.enabled", False)
        await interaction.response.send_message(embed=embed_success("Disabled."), ephemeral=True)

    @grp.command(name="channel")
    async def channel(self, interaction, channel: discord.TextChannel):
        await self.bot.db.set_config(interaction.guild.id, "logging.channel", channel.id)
        await interaction.response.send_message(embed=embed_success(f"→ {channel.mention}."), ephemeral=True)

    @grp.command(name="events")
    async def events(self, interaction, events: str):
        if events.strip().lower() == "all":
            val = ["all"]
        else:
            val = [x for x in events.split() if x in LOG_EVENTS]
            if not val:
                await interaction.response.send_message(embed=embed_error("No valid events."), ephemeral=True)
                return
        await self.bot.db.set_config(interaction.guild.id, "logging.events", val)
        await interaction.response.send_message(embed=embed_success(f"Set: {', '.join(val)}"), ephemeral=True)

    @grp.command(name="reset")
    async def reset(self, interaction):
        for k in ("logging.enabled", "logging.channel", "logging.events"):
            await self.bot.db.del_config(interaction.guild.id, k)
        await interaction.response.send_message(embed=embed_success("Reset."), ephemeral=True)

    @commands.Cog.listener()
    async def on_member_join(self, member):
        e = embed_base(title="Member joined", description=f"{member.mention} (`{member.id}`)")
        e.set_thumbnail(url=member.display_avatar.url)
        await self._send(member.guild, "member_join", e)

    @commands.Cog.listener()
    async def on_member_remove(self, member):
        await self._send(member.guild, "member_leave",
                         embed_base(title="Member left", description=f"{member} (`{member.id}`)"))

    @commands.Cog.listener()
    async def on_message_delete(self, message):
        if message.author.bot or not message.guild:
            return
        e = embed_base(title="Message deleted",
                       description=f"Author: {message.author.mention}\nChannel: {message.channel.mention}")
        if message.content:
            e.add_field(name="Content", value=message.content[:1024])
        await self._send(message.guild, "message_delete", e)

    @commands.Cog.listener()
    async def on_message_edit(self, before, after):
        if before.author.bot or not before.guild or before.content == after.content:
            return
        e = embed_base(title="Message edited",
                       description=f"Author: {before.author.mention}\nChannel: {before.channel.mention}")
        e.add_field(name="Before", value=(before.content or "—")[:1024])
        e.add_field(name="After", value=(after.content or "—")[:1024])
        await self._send(before.guild, "message_edit", e)

    @commands.Cog.listener()
    async def on_member_update(self, before, after):
        if before.roles == after.roles:
            return
        added = [r.mention for r in after.roles if r not in before.roles]
        removed = [r.mention for r in before.roles if r not in after.roles]
        e = embed_base(title="Roles updated", description=after.mention)
        if added:
            e.add_field(name="Added", value=" ".join(added)[:1024])
        if removed:
            e.add_field(name="Removed", value=" ".join(removed)[:1024])
        await self._send(after.guild, "role_changes", e)


# ---------------------------------------------------------------- Tickets
TICKET_OPEN_ID = "freakos:ticket_open"
TICKET_CLOSE_ID = "freakos:ticket_close"
TICKET_DELETE_ID = "freakos:ticket_delete"

DEFAULT_TICKET_OPEN_MESSAGE = (
    "Hey {mention}, thanks for opening a ticket!\n"
    "\n"
    "Support will be with you shortly.\n"
    "\n"
    "Describe your issue below and someone will reply."
)
DEFAULT_AUTO_DELETE_SECONDS = 180


class OpenTicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Open a Ticket", style=discord.ButtonStyle.primary,
                       emoji="🎫", custom_id=TICKET_OPEN_ID)
    async def open_ticket(self, interaction, button):
        cog = interaction.client.get_cog("Tickets")
        if cog is None:
            await interaction.response.send_message("Ticket system unavailable.", ephemeral=True)
            return
        await cog.handle_open(interaction)


class CloseTicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Close", style=discord.ButtonStyle.danger,
                       emoji="🔒", custom_id=TICKET_CLOSE_ID)
    async def close(self, interaction, button):
        cog = interaction.client.get_cog("Tickets")
        if cog is None:
            return
        await cog.handle_close_button(interaction)

    @discord.ui.button(label="Delete now", style=discord.ButtonStyle.secondary,
                       emoji="🗑️", custom_id=TICKET_DELETE_ID)
    async def delete(self, interaction, button):
        cog = interaction.client.get_cog("Tickets")
        if cog is None:
            return
        await cog.handle_delete_button(interaction)


class Tickets(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    grp = app_commands.Group(name="ticket", description="Ticket system.",
                             default_permissions=discord.Permissions(manage_guild=True))

    async def cog_load(self):
        now = int(time.time())
        rows = await self.bot.db.fetchall(
            "SELECT channel_id, delete_at FROM tickets WHERE status='closed' AND delete_at IS NOT NULL"
        )
        for r in rows:
            await schedule_task(self.bot.db, "ticket_delete",
                                {"channel_id": r["channel_id"]},
                                run_at=max(r["delete_at"], now))
        if rows:
            log.info("Recovered %d pending ticket auto-deletes.", len(rows))

    @grp.command(name="setup", description="Configure the ticket system.")
    async def setup(self, interaction, category: discord.CategoryChannel,
                    support_role: discord.Role,
                    auto_delete_seconds: int = DEFAULT_AUTO_DELETE_SECONDS):
        auto_delete_seconds = max(30, min(auto_delete_seconds, 3600))
        await self.bot.db.set_config(interaction.guild.id, "ticket.category_id", category.id)
        await self.bot.db.set_config(interaction.guild.id, "ticket.support_role_id", support_role.id)
        await self.bot.db.set_config(interaction.guild.id, "ticket.auto_delete_seconds", auto_delete_seconds)
        await self.bot.db.set_config(interaction.guild.id, "ticket.enabled", True)
        await interaction.response.send_message(
            embed=embed_success(
                f"Ticket system configured.\n"
                f"Category: {category.mention}\n"
                f"Support role: {support_role.mention}\n"
                f"Closed tickets auto-delete after **{auto_delete_seconds}s**."
            ),
            ephemeral=True,
        )

    @grp.command(name="panel", description="Send the ticket panel to a channel.")
    async def panel(self, interaction, channel: discord.TextChannel,
                    title: str = "Support Tickets",
                    description: str = "Click the button below to open a ticket."):
        embed = embed_base(title=title, description=description)
        msg = await channel.send(embed=embed, view=OpenTicketView())
        await self.bot.db.set_config(interaction.guild.id, "ticket.panel_channel_id", channel.id)
        await self.bot.db.set_config(interaction.guild.id, "ticket.panel_message_id", msg.id)
        await interaction.response.send_message(
            embed=embed_success(f"Panel posted in {channel.mention}."), ephemeral=True
        )

    @grp.command(name="message", description="Edit the message shown inside a new ticket.")
    async def message(self, interaction):
        cur = await self.bot.db.get_config(
            interaction.guild.id, "ticket.open_message", DEFAULT_TICKET_OPEN_MESSAGE
        )
        await interaction.response.send_modal(
            _TextModal("ticket.open_message", "Ticket welcome message", cur)
        )

    @grp.command(name="reset", description="Reset ticket configuration.")
    async def reset(self, interaction):
        for k in ("ticket.category_id", "ticket.support_role_id",
                  "ticket.panel_channel_id", "ticket.panel_message_id",
                  "ticket.open_message", "ticket.auto_delete_seconds", "ticket.enabled"):
            await self.bot.db.del_config(interaction.guild.id, k)
        await interaction.response.send_message(embed=embed_success("Ticket config reset."), ephemeral=True)

    @grp.command(name="close", description="Close the current ticket.")
    async def close(self, interaction):
        await self._close(interaction, source="command")

    @grp.command(name="reopen", description="Reopen a closed ticket.")
    async def reopen(self, interaction):
        row = await self.bot.db.fetchone(
            "SELECT * FROM tickets WHERE channel_id=?", (interaction.channel.id,)
        )
        if not row:
            await interaction.response.send_message("This channel is not a ticket.", ephemeral=True)
            return
        if row["status"] == "open":
            await interaction.response.send_message("Ticket is already open.", ephemeral=True)
            return
        await self.bot.db.execute(
            "UPDATE tickets SET status='open', closed_at=NULL, delete_at=NULL WHERE channel_id=?",
            (interaction.channel.id,),
        )
        try:
            await interaction.channel.edit(
                name=interaction.channel.name.replace("closed-", ""),
                reason=f"Reopened by {interaction.user}",
            )
            await interaction.channel.set_permissions(
                interaction.guild.default_role, send_messages=None
            )
        except discord.HTTPException:
            pass
        await interaction.response.send_message(embed=embed_success("Ticket reopened. Auto-delete cancelled."))

    @grp.command(name="delete", description="Delete the current ticket immediately.")
    async def delete(self, interaction):
        row = await self.bot.db.fetchone(
            "SELECT id FROM tickets WHERE channel_id=?", (interaction.channel.id,)
        )
        if not row:
            await interaction.response.send_message("This channel is not a ticket.", ephemeral=True)
            return
        await interaction.response.send_message("Deleting ticket…", ephemeral=True)
        await self._delete_channel(interaction.channel.id)

    @grp.command(name="add", description="Add a user to the current ticket.")
    async def add(self, interaction, user: discord.Member):
        if not await self._is_ticket(interaction.channel.id):
            await interaction.response.send_message("Not a ticket channel.", ephemeral=True)
            return
        try:
            await interaction.channel.set_permissions(
                user, view_channel=True, send_messages=True, read_message_history=True
            )
        except discord.Forbidden:
            await interaction.response.send_message("Missing permissions.", ephemeral=True)
            return
        await interaction.response.send_message(embed=embed_success(f"Added {user.mention}."))

    @grp.command(name="remove", description="Remove a user from the current ticket.")
    async def remove(self, interaction, user: discord.Member):
        if not await self._is_ticket(interaction.channel.id):
            await interaction.response.send_message("Not a ticket channel.", ephemeral=True)
            return
        try:
            await interaction.channel.set_permissions(user, view_channel=False)
        except discord.Forbidden:
            await interaction.response.send_message("Missing permissions.", ephemeral=True)
            return
        await interaction.response.send_message(embed=embed_success(f"Removed {user.mention}."))

    async def handle_open(self, interaction):
        g = interaction.guild
        if g is None:
            await interaction.response.send_message("Server-only.", ephemeral=True)
            return
        existing = await self.bot.db.fetchone(
            "SELECT channel_id FROM tickets WHERE guild_id=? AND user_id=? AND status='open'",
            (g.id, interaction.user.id),
        )
        if existing:
            ch = g.get_channel(existing["channel_id"])
            if ch:
                await interaction.response.send_message(
                    f"You already have an open ticket: {ch.mention}", ephemeral=True
                )
                return
            await self.bot.db.execute("UPDATE tickets SET status='closed' WHERE channel_id=?",
                                      (existing["channel_id"],))
        cfg = await self.bot.db.all_config(g.id)
        category_id = cfg.get("ticket.category_id")
        support_role_id = cfg.get("ticket.support_role_id")
        category = g.get_channel(category_id) if category_id else None
        if not isinstance(category, discord.CategoryChannel):
            category = None
        overwrites = {
            g.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(
                view_channel=True, send_messages=True,
                read_message_history=True, attach_files=True, embed_links=True,
            ),
            g.me: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True,
                manage_channels=True, manage_messages=True,
            ),
        }
        if support_role_id:
            role = g.get_role(support_role_id)
            if role:
                overwrites[role] = discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, read_message_history=True,
                    attach_files=True, embed_links=True, manage_messages=True,
                )
        try:
            channel = await g.create_text_channel(
                name=f"ticket-{interaction.user.name}"[:95],
                category=category, overwrites=overwrites,
                reason=f"Ticket opened by {interaction.user}",
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                "I don't have permission to create ticket channels.", ephemeral=True
            )
            return
        except discord.HTTPException as e:
            await interaction.response.send_message(f"Failed: {e}", ephemeral=True)
            return
        await self.bot.db.execute(
            "INSERT INTO tickets (guild_id, channel_id, user_id, status, created_at) "
            "VALUES (?, ?, ?, 'open', ?)",
            (g.id, channel.id, interaction.user.id, int(time.time())),
        )
        template = cfg.get("ticket.open_message") or DEFAULT_TICKET_OPEN_MESSAGE
        rendered = render(template, {
            "mention": interaction.user.mention,
            "user": str(interaction.user),
            "username": interaction.user.name,
            "display_name": interaction.user.display_name,
            "server": g.name, "channel": channel.mention,
        })
        try:
            await channel.send(content=interaction.user.mention,
                               embed=embed_base(description=rendered),
                               view=CloseTicketView())
        except discord.HTTPException:
            pass
        await interaction.response.send_message(
            embed=embed_success(f"Ticket opened: {channel.mention}"), ephemeral=True
        )

    async def handle_close_button(self, interaction):
        await self._close(interaction, source="button")

    async def handle_delete_button(self, interaction):
        if not await self._is_ticket(interaction.channel.id):
            await interaction.response.send_message("Not a ticket.", ephemeral=True)
            return
        await interaction.response.send_message("Deleting…", ephemeral=True)
        await self._delete_channel(interaction.channel.id)

    async def _close(self, interaction, *, source):
        if not await self._is_ticket(interaction.channel.id):
            await interaction.response.send_message("This channel is not a ticket.", ephemeral=True)
            return
        delay = await self.bot.db.get_config(
            interaction.guild.id, "ticket.auto_delete_seconds", DEFAULT_AUTO_DELETE_SECONDS
        )
        delete_at = int(time.time()) + int(delay)
        await self.bot.db.execute(
            "UPDATE tickets SET status='closed', closed_at=?, delete_at=? WHERE channel_id=?",
            (int(time.time()), delete_at, interaction.channel.id),
        )
        await schedule_task(self.bot.db, "ticket_delete",
                            {"channel_id": interaction.channel.id},
                            run_at=delete_at, guild_id=interaction.guild.id)
        try:
            await interaction.channel.set_permissions(interaction.guild.default_role, send_messages=False)
            await interaction.channel.edit(
                name=f"closed-{interaction.channel.name}"[:95],
                reason=f"Closed by {interaction.user}",
            )
        except discord.HTTPException:
            pass
        await interaction.response.send_message(
            embed=embed_success(
                f"Ticket closed. It will be deleted <t:{delete_at}:R> ({delay}s). "
                f"Use `/ticket reopen` to cancel."
            )
        )

    async def _is_ticket(self, channel_id):
        row = await self.bot.db.fetchone("SELECT 1 FROM tickets WHERE channel_id=?", (channel_id,))
        return row is not None

    async def _delete_channel(self, channel_id):
        row = await self.bot.db.fetchone("SELECT guild_id FROM tickets WHERE channel_id=?", (channel_id,))
        if row is None:
            return
        guild = self.bot.get_guild(row["guild_id"])
        if guild:
            ch = guild.get_channel(channel_id)
            if ch:
                try:
                    await ch.delete(reason="Ticket auto-delete")
                except discord.HTTPException:
                    pass
        await self.bot.db.execute("UPDATE tickets SET status='deleted', delete_at=NULL WHERE channel_id=?",
                                  (channel_id,))

    async def _delete_task(self, bot, payload):
        channel_id = payload["channel_id"]
        row = await self.bot.db.fetchone(
            "SELECT status, delete_at FROM tickets WHERE channel_id=?", (channel_id,)
        )
        if row is None:
            return
        if row["status"] != "closed":
            return
        if row["delete_at"] is None or int(row["delete_at"]) > int(time.time()) + 2:
            return
        await self._delete_channel(channel_id)


async def _ticket_delete_handler(bot, payload):
    cog = bot.get_cog("Tickets")
    if cog:
        await cog._delete_task(bot, payload)


# ---------------------------------------------------------------- CustomCommands
class CustomCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    grp = app_commands.Group(name="customcommand", description="Per-server custom commands.",
                             default_permissions=discord.Permissions(administrator=True))

    @grp.command(name="create")
    async def create(self, interaction, name: str, embed: bool = False):
        name = name.strip().lower()
        if not name.isidentifier() or len(name) > 32:
            await interaction.response.send_message(embed=embed_error("Bad name."), ephemeral=True)
            return
        existing = await self.bot.db.fetchone(
            "SELECT 1 FROM custom_commands WHERE guild_id=? AND name=?",
            (interaction.guild.id, name),
        )
        if existing:
            await interaction.response.send_message(embed=embed_error("Exists."), ephemeral=True)
            return
        await interaction.response.send_modal(_CustomModal(name, embed))

    @grp.command(name="edit")
    async def edit(self, interaction, name: str):
        row = await self.bot.db.fetchone(
            "SELECT response, embed FROM custom_commands WHERE guild_id=? AND name=?",
            (interaction.guild.id, name.lower()),
        )
        if not row:
            await interaction.response.send_message(embed=embed_error("Not found."), ephemeral=True)
            return
        await interaction.response.send_modal(_CustomModal(name.lower(), bool(row["embed"]), row["response"]))

    @grp.command(name="delete")
    async def delete(self, interaction, name: str):
        cur = await self.bot.db.execute(
            "DELETE FROM custom_commands WHERE guild_id=? AND name=?",
            (interaction.guild.id, name.lower()),
        )
        if cur.rowcount == 0:
            await interaction.response.send_message(embed=embed_error("Not found."), ephemeral=True)
            return
        await interaction.response.send_message(embed=embed_success("Deleted."), ephemeral=True)

    @grp.command(name="list")
    async def list_(self, interaction):
        rows = await self.bot.db.fetchall(
            "SELECT name, enabled FROM custom_commands WHERE guild_id=? ORDER BY name",
            (interaction.guild.id,),
        )
        if not rows:
            await interaction.response.send_message("None configured.", ephemeral=True)
            return
        e = embed_base(title="Custom Commands")
        e.description = "\n".join(f"`{r['name']}` {'🟢' if r['enabled'] else '⚪'}" for r in rows)
        await interaction.response.send_message(embed=e, ephemeral=True)

    @grp.command(name="reset")
    async def reset(self, interaction):
        await self.bot.db.execute("DELETE FROM custom_commands WHERE guild_id=?", (interaction.guild.id,))
        await interaction.response.send_message(embed=embed_success("Reset."), ephemeral=True)

    @commands.Cog.listener()
    async def on_message(self, message):
        if not message.guild or message.author.bot:
            return
        content = message.content.strip()
        if not content:
            return
        prefix = await self.bot.db.get_config(message.guild.id, "custom_commands.prefix", "!")
        if not content.startswith(prefix):
            return
        name = content[len(prefix):].split()[0].lower()
        if not name:
            return
        row = await self.bot.db.fetchone(
            "SELECT response, embed, enabled FROM custom_commands WHERE guild_id=? AND name=?",
            (message.guild.id, name),
        )
        if not row or not row["enabled"]:
            return
        ctx = {
            "user": str(message.author), "username": message.author.name,
            "display_name": message.author.display_name, "mention": message.author.mention,
            "user_id": message.author.id,
            "server": message.guild.name, "server_name": message.guild.name,
            "server_id": message.guild.id,
            "channel": message.channel.mention, "channel_name": message.channel.name,
        }
        rendered = render(row["response"], ctx)
        try:
            if row["embed"]:
                await message.channel.send(embed=embed_base(description=rendered))
            else:
                await message.channel.send(rendered[:2000])
        except discord.HTTPException:
            pass


class _CustomModal(discord.ui.Modal):
    def __init__(self, name, embed, current=""):
        super().__init__(title=f"Custom command: {name}")
        self.name = name
        self.use_embed = embed
        self.input = discord.ui.TextInput(
            label="Response (multiline supported)",
            style=discord.TextStyle.paragraph,
            default=current[:4000], max_length=4000, required=True,
        )
        self.add_item(self.input)

    async def on_submit(self, interaction):
        await interaction.client.db.execute(
            "INSERT INTO custom_commands (guild_id, name, response, embed, enabled) VALUES (?, ?, ?, ?, 1) "
            "ON CONFLICT(guild_id, name) DO UPDATE SET response=excluded.response, embed=excluded.embed",
            (interaction.guild.id, self.name, str(self.input.value), 1 if self.use_embed else 0),
        )
        await interaction.response.send_message(embed=embed_success(f"Saved `{self.name}`."), ephemeral=True)


# ---------------------------------------------------------------- Announcements
class Announcements(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    grp = app_commands.Group(name="announce", description="Announcements.",
                             default_permissions=discord.Permissions(administrator=True))

    @grp.command(name="send", description="Send an announcement now.")
    async def send(self, interaction, channel: discord.TextChannel, title: str, body: str):
        try:
            await channel.send(embed=embed_base(title=title, description=body))
        except discord.HTTPException:
            await interaction.response.send_message(embed=embed_error("Failed."), ephemeral=True)
            return
        await interaction.response.send_message(embed=embed_success(f"Sent to {channel.mention}."), ephemeral=True)

    @grp.command(name="schedule", description="Schedule an announcement.")
    async def schedule(self, interaction, channel: discord.TextChannel,
                       title: str, body: str, minutes_from_now: int):
        minutes_from_now = max(1, minutes_from_now)
        payload = {"channel_id": channel.id, "title": title, "body": body}
        run_at = int(time.time()) + minutes_from_now * 60
        cur = await self.bot.db.execute(
            "INSERT INTO announcements (guild_id, channel_id, payload, send_at) VALUES (?, ?, ?, ?)",
            (interaction.guild.id, channel.id, json.dumps(payload, ensure_ascii=False), run_at),
        )
        await schedule_task(self.bot.db, "announcement_send", {"announcement_id": cur.lastrowid},
                            run_at=run_at, guild_id=interaction.guild.id)
        await interaction.response.send_message(
            embed=embed_success(f"Scheduled #{cur.lastrowid} for <t:{run_at}:R>."), ephemeral=True
        )

    @grp.command(name="cancel", description="Cancel a scheduled announcement.")
    async def cancel(self, interaction, announcement_id: int):
        cur = await self.bot.db.execute(
            "UPDATE announcements SET cancelled=1 WHERE id=? AND guild_id=? AND sent=0",
            (announcement_id, interaction.guild.id),
        )
        if cur.rowcount == 0:
            await interaction.response.send_message(embed=embed_error("Nothing to cancel."), ephemeral=True)
            return
        await interaction.response.send_message(embed=embed_success(f"Cancelled #{announcement_id}."), ephemeral=True)


# ---------------------------------------------------------------- Giveaways
class Giveaways(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    grp = app_commands.Group(name="giveaway", description="Giveaways.",
                             default_permissions=discord.Permissions(manage_guild=True))

    @grp.command(name="create")
    async def create(self, interaction, channel: discord.TextChannel,
                     prize: str, minutes: int, winners: int = 1):
        minutes = max(1, minutes)
        winners = max(1, min(winners, 20))
        ends_at = int(time.time()) + minutes * 60
        e = embed_base(
            title=f"🎉 Giveaway — {prize}",
            description=(
                f"React with 🎉 to enter!\n\n"
                f"**Ends:** <t:{ends_at}:R>\n"
                f"**Winners:** {winners}\n"
                f"**Hosted by:** {interaction.user.mention}"
            ),
        )
        msg = await channel.send(embed=e)
        await msg.add_reaction("🎉")
        cur = await self.bot.db.execute(
            "INSERT INTO giveaways (guild_id, channel_id, message_id, prize, winners, host_id, ends_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (interaction.guild.id, channel.id, msg.id, prize, winners, interaction.user.id, ends_at),
        )
        await schedule_task(self.bot.db, "giveaway_end", {"giveaway_id": cur.lastrowid},
                            run_at=ends_at, guild_id=interaction.guild.id)
        await interaction.response.send_message(
            embed=embed_success(f"Started in {channel.mention}."), ephemeral=True
        )

    @grp.command(name="end")
    async def end(self, interaction, giveaway_id: int):
        await self._finish(giveaway_id)
        await interaction.response.send_message(embed=embed_success("Ended."), ephemeral=True)

    @grp.command(name="reroll")
    async def reroll(self, interaction, giveaway_id: int):
        row = await self.bot.db.fetchone("SELECT * FROM giveaways WHERE id=? AND guild_id=?",
                                         (giveaway_id, interaction.guild.id))
        if not row:
            await interaction.response.send_message(embed=embed_error("Not found."), ephemeral=True)
            return
        entries = await self._entries(giveaway_id)
        if not entries:
            await interaction.response.send_message("No entries.", ephemeral=True)
            return
        await interaction.channel.send(f"🎉 Rerolled winner for **{row['prize']}**: <@{random.choice(entries)}>!")

    @grp.command(name="list")
    async def list_(self, interaction):
        rows = await self.bot.db.fetchall(
            "SELECT id, prize, ends_at FROM giveaways WHERE guild_id=? AND ended=0 ORDER BY ends_at",
            (interaction.guild.id,),
        )
        if not rows:
            await interaction.response.send_message("None active.", ephemeral=True)
            return
        e = embed_base(title="Active Giveaways")
        for r in rows:
            embed_field(e, f"#{r['id']} — {r['prize']}", f"Ends <t:{r['ends_at']}:R>")
        await interaction.response.send_message(embed=e, ephemeral=True)

    async def _entries(self, gid):
        rows = await self.bot.db.fetchall("SELECT user_id FROM giveaway_entries WHERE giveaway_id=?", (gid,))
        return [r["user_id"] for r in rows]

    async def _finish(self, gid):
        row = await self.bot.db.fetchone("SELECT * FROM giveaways WHERE id=? AND ended=0", (gid,))
        if not row:
            return
        await self.bot.db.execute("UPDATE giveaways SET ended=1 WHERE id=?", (gid,))
        entries = await self._entries(gid)
        guild = self.bot.get_guild(row["guild_id"])
        ch = guild.get_channel(row["channel_id"]) if guild else None
        if not isinstance(ch, discord.TextChannel):
            return
        if not entries:
            await ch.send(f"🎉 Giveaway for **{row['prize']}** ended with no entries.")
            return
        winners = random.sample(entries, min(row["winners"], len(entries)))
        mentions = ", ".join(f"<@{u}>" for u in winners)
        await ch.send(f"🎉 Congratulations {mentions}! You won **{row['prize']}**!")

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload):
        if str(payload.emoji) != "🎉":
            return
        row = await self.bot.db.fetchone("SELECT id, ended FROM giveaways WHERE message_id=?",
                                         (payload.message_id,))
        if not row or row["ended"]:
            return
        if self.bot.user and payload.user_id == self.bot.user.id:
            return
        await self.bot.db.execute(
            "INSERT OR IGNORE INTO giveaway_entries (giveaway_id, user_id) VALUES (?, ?)",
            (row["id"], payload.user_id),
        )

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload):
        if str(payload.emoji) != "🎉":
            return
        row = await self.bot.db.fetchone("SELECT id FROM giveaways WHERE message_id=?",
                                         (payload.message_id,))
        if not row:
            return
        await self.bot.db.execute(
            "DELETE FROM giveaway_entries WHERE giveaway_id=? AND user_id=?",
            (row["id"], payload.user_id),
        )


# ============================================================================
# BOT
# ============================================================================

class Freakos(discord.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True
        intents.message_content = True
        intents.voice_states = True

        owner_ids = {int(x) for x in (os.getenv("OWNER_IDS") or "").split(",") if x.strip().isdigit()}
        super().__init__(intents=intents, owner_ids=owner_ids)

        self.db = Database(os.getenv("DATABASE_PATH", "data/freakos.db"))
        self.scheduler = Scheduler(self, self.db)

    async def setup_hook(self):
        await self.db.connect()
        log.info("Database ready at %s", self.db.path)

        for cog in (General, Welcome, Autorole, Autonick, Departure, ActionDM,
                    VCNotify, Moderation, GuildLogging, Tickets,
                    CustomCommands, Announcements, Giveaways):
            try:
                await self.add_cog(cog(self))
                log.info("Loaded %s", cog.__name__)
            except Exception:
                log.exception("Failed to load %s", cog.__name__)

        self.tree.on_error = on_app_command_error

        self.scheduler.register("timeout_end_dm", self._handle_timeout_end)
        self.scheduler.register("announcement_send", self._handle_announcement)
        self.scheduler.register("giveaway_end", self._handle_giveaway_end)
        self.scheduler.register("ticket_delete", _ticket_delete_handler)
        await self.scheduler.start()

        self.add_view(OpenTicketView())
        self.add_view(CloseTicketView())

        dev = (os.getenv("DEV_GUILD_ID") or "").strip()
        try:
            if dev.isdigit():
                g = discord.Object(id=int(dev))
                self.tree.copy_global_to(guild=g)
                synced = await self.tree.sync(guild=g)
                log.info("Synced %d commands to dev guild %s", len(synced), dev)
            else:
                synced = await self.tree.sync()
                log.info("Synced %d global commands", len(synced))
        except discord.HTTPException:
            log.exception("Sync failed")

    async def _handle_timeout_end(self, bot, payload):
        row = await self.db.fetchone("SELECT sent FROM timeout_dms WHERE case_id=?", (payload["case_id"],))
        if row is None or row["sent"]:
            return
        guild = bot.get_guild(payload["guild_id"])
        if guild is None:
            return
        member = guild.get_member(payload["user_id"])
        if member is None:
            try:
                member = await guild.fetch_member(payload["user_id"])
            except discord.HTTPException:
                return
        adm = bot.get_cog("ActionDM")
        if adm:
            await adm.send_action_dm(guild, member, "timeout_end")
        await self.db.execute("UPDATE timeout_dms SET sent=1 WHERE case_id=?", (payload["case_id"],))

    async def _handle_announcement(self, bot, payload):
        row = await self.db.fetchone(
            "SELECT guild_id, channel_id, payload, sent, cancelled FROM announcements WHERE id=?",
            (payload["announcement_id"],),
        )
        if not row or row["sent"] or row["cancelled"]:
            return
        guild = bot.get_guild(row["guild_id"])
        if guild is None:
            return
        ch = guild.get_channel(row["channel_id"])
        if not isinstance(ch, discord.TextChannel):
            return
        data = json.loads(row["payload"])
        try:
            await ch.send(embed=embed_base(title=data.get("title", ""), description=data.get("body", "")))
            await self.db.execute("UPDATE announcements SET sent=1 WHERE id=?", (payload["announcement_id"],))
        except discord.HTTPException:
            pass

    async def _handle_giveaway_end(self, bot, payload):
        cog = bot.get_cog("Giveaways")
        if cog:
            await cog._finish(payload["giveaway_id"])

    async def on_ready(self):
        log.info("Logged in as %s — %d guild(s)", self.user, len(self.guilds))
        try:
            await self.change_presence(
                activity=discord.Activity(type=discord.ActivityType.watching, name="/help")
            )
        except discord.HTTPException:
            pass

    async def close(self):
        await self.scheduler.stop()
        await self.db.close()
        await super().close()


# ============================================================================
# MAIN
# ============================================================================

def main():
    token = (os.getenv("DISCORD_TOKEN") or "").strip()
    if not token:
        print("DISCORD_TOKEN is not set. Create .env from .env.example.", file=sys.stderr)
        sys.exit(1)
    bot = Freakos()
    try:
        bot.run(token, log_handler=None)
    except KeyboardInterrupt:
        pass
    except discord.LoginFailure:
        log.error("Invalid token.")
        sys.exit(1)
    except discord.PrivilegedIntentsRequired:
        log.error("Enable SERVER MEMBERS INTENT and MESSAGE CONTENT INTENT in the Developer Portal.")
        sys.exit(1)


if __name__ == "__main__":
    main()
