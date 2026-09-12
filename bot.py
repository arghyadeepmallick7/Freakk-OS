"""
FREAKOS — Production-ready single-file Discord bot.
Uses discord.py 2.x (NOT py-cord). Everything is inside this file.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
DB_PATH = os.getenv("DB_PATH", "data/freakos.db")

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("freakos")


# =====================================================================
# Helpers
# =====================================================================

def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def fmt_dt(dt: Optional[datetime]) -> str:
    if not dt:
        return "—"
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def fmt_dt_human(dt: Optional[datetime]) -> str:
    if not dt:
        return "—"
    return dt.astimezone(timezone.utc).strftime("%d %B %Y %H:%M")


def fmt_duration(seconds: int) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    m, s = divmod(seconds, 60)
    if m < 60:
        return f"{m}m {s}s" if s else f"{m}m"
    h, m = divmod(m, 60)
    if h < 24:
        return f"{h}h {m}m" if m else f"{h}h"
    d, h = divmod(h, 24)
    return f"{d}d {h}h" if h else f"{d}d"


def parse_duration(s: str) -> Optional[int]:
    if not s:
        return None
    total = 0
    matched = False
    for amount, unit in re.findall(r"(\d+)\s*([smhdw])", s.lower()):
        matched = True
        n = int(amount)
        total += n * {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[unit]
    return total if matched else None


def apply_placeholders(text: str, **kw: Any) -> str:
    if not text:
        return ""
    out = text
    for k, v in kw.items():
        if v is None:
            v = ""
        out = out.replace("{" + k + "}", str(v))
    return out


def chunk_message(text: str, limit: int = 1990) -> list[str]:
    if text is None:
        return [""]
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    cur = ""
    for line in text.split("\n"):
        while len(line) > limit:
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if not cur:
            cur = line
        elif len(cur) + 1 + len(line) > limit:
            chunks.append(cur)
            cur = line
        else:
            cur = cur + "\n" + line
    if cur or not chunks:
        chunks.append(cur)
    return chunks


def make_embed(title: str = None, description: str = None,
               color: int = 0x5865F2) -> discord.Embed:
    e = discord.Embed(color=color)
    if title:
        e.title = title[:256]
    if description:
        e.description = description[:4096]
    e.timestamp = now_utc()
    return e


def is_admin_or_mod(member: discord.Member) -> bool:
    perms = member.guild_permissions
    return perms.administrator or perms.manage_guild or perms.moderate_members


def hierarchy_ok(guild: discord.Guild, target: discord.Member) -> tuple[bool, str]:
    me = guild.me
    if target.id == guild.owner_id:
        return False, "Cannot act on the server owner."
    if target.id == me.id:
        return False, "Cannot act on myself."
    if target.top_role >= me.top_role:
        return False, "Target has a role equal or higher than mine."
    return True, ""


async def _safe_reply(interaction: discord.Interaction, content: str = None, *,
                      embed=None, view=None, ephemeral: bool = True, file=None):
    kwargs = {"ephemeral": ephemeral}
    if content is not None:
        kwargs["content"] = content
    if embed is not None:
        kwargs["embed"] = embed
    if view is not None:
        kwargs["view"] = view
    if file is not None:
        kwargs["file"] = file
    try:
        if interaction.response.is_done():
            return await interaction.followup.send(**kwargs)
        return await interaction.response.send_message(**kwargs)
    except Exception:
        log.exception("Failed to reply to interaction")
        return None


# =====================================================================
# Database
# =====================================================================

SCHEMA = """
CREATE TABLE IF NOT EXISTS guild_config (
    guild_id INTEGER NOT NULL, key TEXT NOT NULL, value TEXT,
    PRIMARY KEY (guild_id, key)
);
CREATE TABLE IF NOT EXISTS warnings (
    id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL, moderator_id INTEGER NOT NULL,
    reason TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cases (
    id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL, moderator_id INTEGER NOT NULL,
    action TEXT NOT NULL, reason TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS timeouts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL, moderator_id INTEGER NOT NULL,
    reason TEXT, ends_at TEXT NOT NULL, dm_sent INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
    type TEXT, status TEXT DEFAULT 'open', claimed_by INTEGER,
    created_at TEXT NOT NULL, close_reason TEXT, ticket_number INTEGER
);
CREATE TABLE IF NOT EXISTS ticket_types (
    id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL,
    name TEXT NOT NULL, emoji TEXT, category_id INTEGER,
    support_role_id INTEGER, message TEXT, UNIQUE(guild_id, name)
);
CREATE TABLE IF NOT EXISTS vouches (
    id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL, voucher_id INTEGER NOT NULL,
    comment TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS shop_products (
    id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL,
    name TEXT NOT NULL, description TEXT, price REAL NOT NULL,
    stock INTEGER DEFAULT -1, active INTEGER DEFAULT 1, image TEXT
);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL, product_id INTEGER NOT NULL,
    quantity INTEGER NOT NULL, price REAL NOT NULL,
    status TEXT DEFAULT 'pending', created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reaction_panels (
    id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL, message_id INTEGER, title TEXT,
    description TEXT, mode TEXT DEFAULT 'button'
);
CREATE TABLE IF NOT EXISTS reaction_roles (
    id INTEGER PRIMARY KEY AUTOINCREMENT, panel_id INTEGER NOT NULL,
    role_id INTEGER NOT NULL, label TEXT, emoji TEXT,
    UNIQUE(panel_id, role_id)
);
CREATE TABLE IF NOT EXISTS giveaways (
    id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL, message_id INTEGER, prize TEXT NOT NULL,
    winners INTEGER DEFAULT 1, host_id INTEGER, ends_at TEXT NOT NULL,
    ended INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS giveaway_entries (
    giveaway_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
    PRIMARY KEY (giveaway_id, user_id)
);
CREATE TABLE IF NOT EXISTS custom_commands (
    id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL,
    name TEXT NOT NULL, response TEXT, embed INTEGER DEFAULT 0,
    enabled INTEGER DEFAULT 1, UNIQUE(guild_id, name)
);
CREATE TABLE IF NOT EXISTS announcements (
    id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL, title TEXT, body TEXT, image TEXT,
    footer TEXT, scheduled_at TEXT, sent INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS scheduled_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT, task_type TEXT NOT NULL,
    guild_id INTEGER, payload TEXT, run_at TEXT NOT NULL,
    completed INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_tasks_due ON scheduled_tasks(completed, run_at);
CREATE TABLE IF NOT EXISTS automod_violations (
    id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL, rule TEXT NOT NULL, action TEXT,
    channel_id INTEGER, reason TEXT, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_automod_guild ON automod_violations(guild_id);
CREATE INDEX IF NOT EXISTS idx_automod_user ON automod_violations(guild_id, user_id);
"""


class Database:
    def __init__(self, path: str):
        self.path = path
        self.conn: Optional[aiosqlite.Connection] = None
        self._lock = asyncio.Lock()

    async def connect(self):
        d = os.path.dirname(os.path.abspath(self.path))
        if d:
            os.makedirs(d, exist_ok=True)
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute("PRAGMA journal_mode=WAL")
        await self.conn.execute("PRAGMA foreign_keys=ON")
        await self.conn.executescript(SCHEMA)
        await self.conn.commit()
        for stmt in (
            "ALTER TABLE tickets ADD COLUMN close_reason TEXT",
            "ALTER TABLE tickets ADD COLUMN ticket_number INTEGER",
        ):
            try:
                await self.conn.execute(stmt)
                await self.conn.commit()
            except Exception:
                pass
        log.info("Database ready at %s", self.path)

    async def close(self):
        if self.conn:
            await self.conn.close()

    async def execute(self, sql: str, params: tuple = ()) -> int:
        async with self._lock:
            cur = await self.conn.execute(sql, params)
            await self.conn.commit()
            return cur.lastrowid

    async def fetchone(self, sql: str, params: tuple = ()) -> Optional[aiosqlite.Row]:
        async with self._lock:
            cur = await self.conn.execute(sql, params)
            return await cur.fetchone()

    async def fetchall(self, sql: str, params: tuple = ()) -> list[aiosqlite.Row]:
        async with self._lock:
            cur = await self.conn.execute(sql, params)
            return await cur.fetchall()

    async def get_config(self, guild_id: int, key: str, default=None):
        row = await self.fetchone(
            "SELECT value FROM guild_config WHERE guild_id=? AND key=?",
            (guild_id, key))
        return row["value"] if row else default

    async def set_config(self, guild_id: int, key: str, value):
        if value is None:
            await self.execute("DELETE FROM guild_config WHERE guild_id=? AND key=?",
                               (guild_id, key))
        else:
            await self.execute(
                "INSERT INTO guild_config (guild_id, key, value) VALUES (?, ?, ?) "
                "ON CONFLICT(guild_id, key) DO UPDATE SET value=excluded.value",
                (guild_id, key, str(value)))

    async def get_json(self, guild_id: int, key: str, default=None):
        v = await self.get_config(guild_id, key)
        if v is None:
            return default
        try:
            return json.loads(v)
        except Exception:
            return default

    async def set_json(self, guild_id: int, key: str, value):
        await self.set_config(guild_id, key, json.dumps(value))


# =====================================================================
# Scheduler
# =====================================================================

class Scheduler:
    def __init__(self, bot: "Freakos"):
        self.bot = bot
        self._wake = asyncio.Event()
        self._task: Optional[asyncio.Task] = None

    def start(self):
        self._task = asyncio.create_task(self._run(), name="freakos-scheduler")

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def schedule(self, task_type: str, run_at: datetime,
                       payload: dict = None, guild_id: int = None) -> int:
        tid = await self.bot.db.execute(
            "INSERT INTO scheduled_tasks (task_type, guild_id, payload, run_at) "
            "VALUES (?, ?, ?, ?)",
            (task_type, guild_id, json.dumps(payload or {}), iso(run_at)))
        self._wake.set()
        return tid

    async def _run(self):
        await self.bot.wait_until_ready()
        while True:
            try:
                row = await self.bot.db.fetchone(
                    "SELECT * FROM scheduled_tasks WHERE completed=0 "
                    "ORDER BY run_at ASC LIMIT 1")
                if row is None:
                    self._wake.clear()
                    try:
                        await asyncio.wait_for(self._wake.wait(), timeout=300)
                    except asyncio.TimeoutError:
                        pass
                    continue
                run_at = parse_iso(row["run_at"]) or now_utc()
                delay = (run_at - now_utc()).total_seconds()
                if delay > 0:
                    self._wake.clear()
                    try:
                        await asyncio.wait_for(self._wake.wait(),
                                               timeout=min(delay, 60))
                    except asyncio.TimeoutError:
                        pass
                    continue
                await self._execute(row)
                await self.bot.db.execute(
                    "UPDATE scheduled_tasks SET completed=1 WHERE id=?",
                    (row["id"],))
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Scheduler loop error")
                await asyncio.sleep(5)

    async def _execute(self, row):
        ttype = row["task_type"]
        try:
            payload = json.loads(row["payload"] or "{}")
        except Exception:
            payload = {}
        handler = getattr(self.bot, f"task_{ttype}", None)
        if not handler:
            log.warning("No handler for scheduled task %s", ttype)
            return
        try:
            await handler(payload)
        except Exception:
            log.exception("Scheduled task %s failed", ttype)


# =====================================================================
# Ticket
# =====================================================================

class TicketCreateButton(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Open Ticket", style=discord.ButtonStyle.primary,
                       custom_id="freakos:ticket:create", emoji="🎫")
    async def create(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        if not guild:
            return
        rows = await interaction.client.db.fetchall(
            "SELECT name, emoji FROM ticket_types WHERE guild_id=? ORDER BY id",
            (guild.id,))
        if not rows:
            await interaction.response.send_message(
                "No ticket types configured. Ask an admin to run `/ticket type`.",
                ephemeral=True)
            return
        opts = [discord.SelectOption(
            label=r["name"][:100], emoji=(r["emoji"] or None),
            value=r["name"][:100]) for r in rows[:25]]
        select = discord.ui.Select(placeholder="Pick a ticket type…", options=opts)

        async def cb(sel_interaction: discord.Interaction):
            await _open_ticket(sel_interaction, sel_interaction.data["values"][0])

        select.callback = cb
        view = discord.ui.View(timeout=60)
        view.add_item(select)
        await interaction.response.send_message(
            "Choose a ticket type to open:", view=view, ephemeral=True)


class TicketPanelModal(discord.ui.Modal, title="Ticket Panel"):
    panel_title = discord.ui.TextInput(
        label="Title", style=discord.TextStyle.short,
        max_length=256, required=False)
    panel_description = discord.ui.TextInput(
        label="Description", style=discord.TextStyle.paragraph,
        max_length=4000, required=False)

    def __init__(self, channel: discord.TextChannel):
        super().__init__()
        self.channel = channel

    async def on_submit(self, interaction: discord.Interaction):
        embed = make_embed(
            title=self.panel_title.value or None,
            description=self.panel_description.value or None)
        try:
            if interaction.guild and interaction.guild.icon:
                embed.set_image(url=interaction.guild.icon.url)
        except Exception:
            pass
        await self.channel.send(embed=embed, view=TicketCreateButton())
        await interaction.response.send_message(
            f"✅ Panel posted in {self.channel.mention}.", ephemeral=True)


async def _open_ticket(interaction: discord.Interaction, ttype: str):
    guild = interaction.guild
    if not guild:
        return
    db: Database = interaction.client.db
    row = await db.fetchone(
        "SELECT * FROM ticket_types WHERE guild_id=? AND name=?",
        (guild.id, ttype))
    if not row:
        return await interaction.response.send_message("Type not found.", ephemeral=True)

    limit = int(await db.get_config(guild.id, "ticket.limit", "1") or 1)
    open_count = await db.fetchone(
        "SELECT COUNT(*) c FROM tickets WHERE guild_id=? AND user_id=? AND status='open'",
        (guild.id, interaction.user.id))
    if open_count and open_count["c"] >= limit:
        return await interaction.response.send_message(
            f"You already have {open_count['c']} open ticket(s).", ephemeral=True)

    cooldown = int(await db.get_config(guild.id, "ticket.cooldown", "0") or 0)
    if cooldown > 0:
        last = await db.fetchone(
            "SELECT created_at FROM tickets WHERE guild_id=? AND user_id=? "
            "ORDER BY id DESC LIMIT 1",
            (guild.id, interaction.user.id))
        if last:
            last_dt = parse_iso(last["created_at"])
            if last_dt and (now_utc() - last_dt).total_seconds() < cooldown:
                rem = cooldown - int((now_utc() - last_dt).total_seconds())
                return await interaction.response.send_message(
                    f"Cooldown: try again in {fmt_duration(rem)}.", ephemeral=True)

    category = guild.get_channel(row["category_id"]) if row["category_id"] else None
    support_role = guild.get_role(row["support_role_id"]) if row["support_role_id"] else None

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        interaction.user: discord.PermissionOverwrite(
            view_channel=True, send_messages=True,
            attach_files=True, read_message_history=True),
        guild.me: discord.PermissionOverwrite(
            view_channel=True, send_messages=True,
            manage_channels=True, manage_permissions=True),
    }
    if support_role:
        overwrites[support_role] = discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True)

    counter = int(await db.get_config(guild.id, "ticket.counter", "0") or 0) + 1
    await db.set_config(guild.id, "ticket.counter", str(counter))
    ticket_num_str = f"#{counter:03d}"

    try:
        channel = await guild.create_text_channel(
            name=f"ticket-{interaction.user.name}"[:90],
            category=category, overwrites=overwrites,
            reason=f"Ticket by {interaction.user}")
    except discord.Forbidden:
        return await interaction.response.send_message(
            "I lack permission to create the ticket channel.", ephemeral=True)

    created_at = now_utc()
    tid = await db.execute(
        "INSERT INTO tickets (guild_id, channel_id, user_id, type, status, "
        "created_at, ticket_number) VALUES (?, ?, ?, ?, 'open', ?, ?)",
        (guild.id, channel.id, interaction.user.id, ttype, iso(created_at), counter))

    body = row["message"] or (
        f"Hey {interaction.user.mention}, thanks for opening a **{ttype}** ticket!\n\n"
        "Support will be with you shortly. Please describe your issue.")

    info_embed = discord.Embed(
        title="× 〻 Ticket Information 〻 ×",
        description=body, color=0x5865F2, timestamp=created_at)
    info_embed.add_field(name="𑣲 Ticket Number", value=ticket_num_str, inline=False)
    info_embed.add_field(name="𑣲 Category", value=ttype, inline=False)
    info_embed.add_field(name="𑣲 Created By", value=interaction.user.mention, inline=False)
    info_embed.add_field(name="𑣲 Created At", value=fmt_dt_human(created_at), inline=False)

    try:
        if interaction.user.display_avatar:
            info_embed.set_thumbnail(url=interaction.user.display_avatar.url)
    except Exception:
        pass

    cfg = await _ticket_room_cfg(db, guild.id)
    view = TicketRoomView(tid, cfg)
    try:
        await channel.send(content=interaction.user.mention, embed=info_embed, view=view)
    except Exception:
        log.exception("Failed to send ticket info embed")

    await interaction.response.send_message(
        f"✅ Ticket created: {channel.mention}", ephemeral=True)

    await _log_guild(interaction.client, guild, "tickets", "Ticket Opened",
                     f"{interaction.user.mention} opened `{ttype}` → {channel.mention}")


# =====================================================================
# Ticket close log + transcript
# =====================================================================

async def _generate_transcript(channel) -> discord.File:
    import io
    messages = []
    async for m in channel.history(limit=500, oldest_first=True):
        attach = ""
        if m.attachments:
            attach = " " + " ".join(f"[attachment: {a.url}]" for a in m.attachments)
        messages.append(f"[{fmt_dt(m.created_at)}] {m.author}: {m.content}{attach}")
    buf = io.BytesIO(("\n".join(messages) or "(empty)").encode("utf-8"))
    return discord.File(buf, filename=f"transcript-{channel.id}.txt")


async def _post_ticket_close_log(bot: "Freakos", guild: discord.Guild, channel,
                                 row, closed_by, reason: str):
    ch_id = await bot.db.get_config(guild.id, "ticket.logs_channel")
    if not ch_id:
        return
    log_ch = guild.get_channel(int(ch_id))
    if not log_ch:
        return

    t_num = row["ticket_number"]
    ticket_num_str = f"#{t_num:03d}" if t_num else f"#{row['id']}"

    e = discord.Embed(
        title="× 〻 Ticket Closed Logs 〻 ×",
        color=0x57F287, timestamp=now_utc())
    e.add_field(name="𑣲 Ticket Number", value=ticket_num_str, inline=False)
    e.add_field(name="𑣲 Category", value=row["type"] or "—", inline=False)
    e.add_field(name="𑣲 Created By", value=f"<@{row['user_id']}>", inline=False)
    e.add_field(name="𑣲 Ticket Created Time",
                value=fmt_dt_human(parse_iso(row["created_at"])), inline=False)
    e.add_field(name="𑣲 Closed By", value=closed_by.mention, inline=False)
    e.add_field(name="𑣲 Closed Time", value=fmt_dt_human(now_utc()), inline=False)
    e.add_field(name="𑣲 Close Reason", value=reason or "No reason specified", inline=False)
    e.add_field(name="𑣲 Transcript", value="Attached below ⬇", inline=False)

    try:
        if getattr(closed_by, "display_avatar", None):
            e.set_thumbnail(url=closed_by.display_avatar.url)
    except Exception:
        pass

    try:
        await log_ch.send(embed=e, file=await _generate_transcript(channel))
    except Exception:
        log.exception("Failed to post ticket close log for ticket %s", row["id"])


async def _ticket_delete_delay(db: "Database", guild_id: int) -> int:
    raw = await db.get_config(guild_id, "ticket.close_delete_delay", "120")
    try:
        val = int(raw)
    except (TypeError, ValueError):
        val = 120
    return max(0, val)


# =====================================================================
# Inner ticket-room button system
# =====================================================================

TICKET_ROOM_BUTTONS = {
    "close":        {"label": "Close Ticket",        "emoji": "🔒", "style": "danger",    "enabled": True},
    "close_reason": {"label": "Close With Reason",   "emoji": "🔒", "style": "danger",    "enabled": True},
    "claim":        {"label": "Claim",               "emoji": "🙋", "style": "success",   "enabled": True},
    "add_user":     {"label": "Add User",            "emoji": "➕", "style": "secondary", "enabled": True},
    "remove_user":  {"label": "Remove User",         "emoji": "➖", "style": "secondary", "enabled": True},
    "lock":         {"label": "Lock",                "emoji": "🔒", "style": "secondary", "enabled": True},
    "unlock":       {"label": "Unlock",              "emoji": "🔓", "style": "secondary", "enabled": True},
    "transcript":   {"label": "Generate Transcript", "emoji": "📄", "style": "secondary", "enabled": True},
    "notify":       {"label": "Notify",              "emoji": "🔔", "style": "primary",   "enabled": True},
}
_BUTTON_STYLES = {
    "primary": discord.ButtonStyle.primary,
    "secondary": discord.ButtonStyle.secondary,
    "success": discord.ButtonStyle.success,
    "danger": discord.ButtonStyle.danger,
}
NOTIFY_COOLDOWN_SECONDS = 60


async def _ticket_room_cfg(db: "Database", guild_id: int) -> dict:
    stored = await db.get_json(guild_id, "ticket.room_buttons", default={}) or {}
    cfg = {k: dict(v) for k, v in TICKET_ROOM_BUTTONS.items()}
    for key, overrides in stored.items():
        if key in cfg and isinstance(overrides, dict):
            cfg[key].update(overrides)
    return cfg


async def _member_can_close(db: "Database", guild_id: int) -> bool:
    return (await db.get_config(guild_id, "ticket.member_can_close", "1")) != "0"


async def _is_ticket_staff(db: "Database", guild: discord.Guild,
                           member: discord.Member, ticket_row=None) -> bool:
    if is_admin_or_mod(member):
        return True
    role_ids = set()
    global_role = await db.get_config(guild.id, "ticket.support_role_id")
    if global_role:
        role_ids.add(int(global_role))
    if ticket_row is not None:
        t = await db.fetchone(
            "SELECT support_role_id FROM ticket_types WHERE guild_id=? AND name=?",
            (guild.id, ticket_row["type"]))
        if t and t["support_role_id"]:
            role_ids.add(t["support_role_id"])
    return any(r.id in role_ids for r in member.roles) if role_ids else False


def _resolve_member_ref(guild: discord.Guild, raw: str) -> Optional[int]:
    raw = (raw or "").strip()
    m = re.match(r"^<@!?(\d+)>$", raw) or re.match(r"^(\d+)$", raw)
    return int(m.group(1)) if m else None


async def _close_ticket_room(interaction: discord.Interaction, ticket_id: int,
                             reason: str = "No reason specified"):
    db: Database = interaction.client.db
    row = await db.fetchone("SELECT * FROM tickets WHERE id=?", (ticket_id,))
    if not row:
        return await _safe_reply(interaction, "Ticket not found.")
    if row["status"] == "closed":
        return await _safe_reply(interaction, "This ticket is already closed.")

    await db.execute("UPDATE tickets SET status='closed', close_reason=? WHERE id=?",
                     (reason, ticket_id))

    try:
        creator = interaction.guild.get_member(row["user_id"])
        if creator:
            await interaction.channel.set_permissions(creator, overwrite=None)
        ow = interaction.channel.overwrites_for(interaction.guild.default_role)
        ow.send_messages = False
        await interaction.channel.set_permissions(interaction.guild.default_role,
                                                  overwrite=ow)
    except Exception:
        log.exception("Failed to update permissions on ticket close")

    closing_text = (
        "× 〻 Ticket Closed 〻 ×\n\n"
        f"𑣲 Closed By:\n{interaction.user.mention}\n\n"
        f"𑣲 Reason:\n{reason}")
    try:
        await _safe_reply(interaction, closing_text, ephemeral=False)
    except Exception:
        log.exception("Failed to send close message")

    await _post_ticket_close_log(interaction.client, interaction.guild,
                                 interaction.channel, row, interaction.user, reason)

    delay = await _ticket_delete_delay(db, interaction.guild.id)
    if delay > 0:
        try:
            await interaction.channel.send(
                f"🗑 This channel will be deleted automatically in {fmt_duration(delay)}.")
        except Exception:
            pass
        await interaction.client.scheduler.schedule(
            "ticket_delete", now_utc() + timedelta(seconds=delay),
            {"ticket_id": ticket_id}, interaction.guild.id)


class TicketCloseReasonModal(discord.ui.Modal, title="Close Ticket"):
    reason = discord.ui.TextInput(
        label="Reason", style=discord.TextStyle.paragraph,
        max_length=500, required=True,
        placeholder="Why is this ticket being closed?")

    def __init__(self, ticket_id: int):
        super().__init__()
        self.ticket_id = ticket_id

    async def on_submit(self, interaction: discord.Interaction):
        await _close_ticket_room(interaction, self.ticket_id, reason=self.reason.value)


class TicketAddUserModal(discord.ui.Modal, title="Add User"):
    user_input = discord.ui.TextInput(
        label="User ID or Mention", placeholder="@User or 123456789012345678")

    def __init__(self, ticket_id: int):
        super().__init__()
        self.ticket_id = ticket_id

    async def on_submit(self, interaction: discord.Interaction):
        uid = _resolve_member_ref(interaction.guild, self.user_input.value)
        member = (interaction.guild.get_member(uid) if uid else None)
        if not member and uid:
            try:
                member = await interaction.guild.fetch_member(uid)
            except Exception:
                member = None
        if not member:
            return await _safe_reply(interaction, "❌ Couldn't find that user.")
        try:
            await interaction.channel.set_permissions(
                member, view_channel=True, send_messages=True,
                read_message_history=True)
        except Exception:
            return await _safe_reply(interaction, "❌ Failed to update permissions.")
        await _safe_reply(interaction, f"✅ User added to ticket. ({member.mention})")


class TicketRemoveUserModal(discord.ui.Modal, title="Remove User"):
    user_input = discord.ui.TextInput(
        label="User ID or Mention", placeholder="@User or 123456789012345678")

    def __init__(self, ticket_id: int):
        super().__init__()
        self.ticket_id = ticket_id

    async def on_submit(self, interaction: discord.Interaction):
        uid = _resolve_member_ref(interaction.guild, self.user_input.value)
        member = (interaction.guild.get_member(uid) if uid else None)
        if not member and uid:
            try:
                member = await interaction.guild.fetch_member(uid)
            except Exception:
                member = None
        if not member:
            return await _safe_reply(interaction, "❌ Couldn't find that user.")
        try:
            await interaction.channel.set_permissions(member, overwrite=None)
        except Exception:
            return await _safe_reply(interaction, "❌ Failed to update permissions.")
        await _safe_reply(interaction, f"✅ User removed from ticket. ({member.mention})")


class TicketCloseConfirmView(discord.ui.View):
    def __init__(self, ticket_id: int):
        super().__init__(timeout=60)
        self.ticket_id = ticket_id

    @discord.ui.button(label="Confirm Close", style=discord.ButtonStyle.danger, emoji="🔒")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        for c in self.children:
            c.disabled = True
        await interaction.response.edit_message(content="Closing ticket…", view=self)
        await _close_ticket_room(interaction, self.ticket_id)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="❌ Close cancelled.", view=None)


class TicketRoomView(discord.ui.View):
    def __init__(self, ticket_id: int, cfg: dict, claimed_by: Optional[int] = None):
        super().__init__(timeout=None)
        self.ticket_id = ticket_id
        self.claimed_by = claimed_by

        def add(key, label, emoji, style_key, custom_id, callback):
            b = cfg.get(key, {})
            if not b.get("enabled", True):
                return
            btn = discord.ui.Button(
                label=b.get("label", label), emoji=b.get("emoji", emoji) or None,
                style=_BUTTON_STYLES.get(b.get("style", style_key),
                                         discord.ButtonStyle.secondary),
                custom_id=custom_id)
            btn.callback = callback
            self.add_item(btn)

        add("close", "Close Ticket", "🔒", "danger",
            f"freakos:tr:close:{ticket_id}", self._cb_close)
        add("close_reason", "Close With Reason", "🔒", "danger",
            f"freakos:tr:closereason:{ticket_id}", self._cb_close_reason)

        claim_cfg = cfg.get("claim", {})
        if claim_cfg.get("enabled", True):
            if claimed_by:
                btn = discord.ui.Button(
                    label="Unclaim", emoji="↩",
                    style=discord.ButtonStyle.secondary,
                    custom_id=f"freakos:tr:unclaim:{ticket_id}")
                btn.callback = self._cb_unclaim
            else:
                btn = discord.ui.Button(
                    label=claim_cfg.get("label", "Claim"),
                    emoji=claim_cfg.get("emoji", "🙋") or None,
                    style=_BUTTON_STYLES.get(claim_cfg.get("style", "success"),
                                             discord.ButtonStyle.success),
                    custom_id=f"freakos:tr:claim:{ticket_id}")
                btn.callback = self._cb_claim
            self.add_item(btn)

        add("add_user", "Add User", "➕", "secondary",
            f"freakos:tr:adduser:{ticket_id}", self._cb_add_user)
        add("remove_user", "Remove User", "➖", "secondary",
            f"freakos:tr:removeuser:{ticket_id}", self._cb_remove_user)
        add("lock", "Lock", "🔒", "secondary",
            f"freakos:tr:lock:{ticket_id}", self._cb_lock)
        add("unlock", "Unlock", "🔓", "secondary",
            f"freakos:tr:unlock:{ticket_id}", self._cb_unlock)
        add("transcript", "Generate Transcript", "📄", "secondary",
            f"freakos:tr:transcript:{ticket_id}", self._cb_transcript)
        add("notify", "Notify", "🔔", "primary",
            f"freakos:tr:notify:{ticket_id}", self._cb_notify)

    async def _cb_close(self, interaction: discord.Interaction):
        db = interaction.client.db
        row = await db.fetchone("SELECT * FROM tickets WHERE id=?", (self.ticket_id,))
        if not row:
            return await _safe_reply(interaction, "Ticket not found.")
        if row["status"] == "closed":
            return await _safe_reply(interaction, "Already closed.")
        is_staff = await _is_ticket_staff(db, interaction.guild, interaction.user, row)
        if interaction.user.id == row["user_id"] and not is_staff:
            if not await _member_can_close(db, interaction.guild.id):
                return await _safe_reply(interaction, "Closing is disabled for members.")
        elif not is_staff and interaction.user.id != row["user_id"]:
            return await _safe_reply(interaction, "Not allowed.")
        await interaction.response.send_message(
            "Are you sure you want to close this ticket?",
            view=TicketCloseConfirmView(self.ticket_id), ephemeral=True)

    async def _cb_close_reason(self, interaction: discord.Interaction):
        db = interaction.client.db
        row = await db.fetchone("SELECT * FROM tickets WHERE id=?", (self.ticket_id,))
        if not row:
            return await _safe_reply(interaction, "Ticket not found.")
        if row["status"] == "closed":
            return await _safe_reply(interaction, "Already closed.")
        is_staff = await _is_ticket_staff(db, interaction.guild, interaction.user, row)
        if interaction.user.id == row["user_id"] and not is_staff:
            if not await _member_can_close(db, interaction.guild.id):
                return await _safe_reply(interaction, "Closing is disabled for members.")
        elif not is_staff and interaction.user.id != row["user_id"]:
            return await _safe_reply(interaction, "Not allowed.")
        await interaction.response.send_modal(TicketCloseReasonModal(self.ticket_id))

    async def _cb_claim(self, interaction: discord.Interaction):
        db = interaction.client.db
        row = await db.fetchone("SELECT * FROM tickets WHERE id=?", (self.ticket_id,))
        if not row:
            return await _safe_reply(interaction, "Ticket not found.")
        if not await _is_ticket_staff(db, interaction.guild, interaction.user, row):
            return await _safe_reply(interaction, "Staff only.")
        if row["claimed_by"]:
            return await _safe_reply(interaction, f"Already claimed by <@{row['claimed_by']}>.")
        await db.execute("UPDATE tickets SET claimed_by=? WHERE id=?",
                         (interaction.user.id, self.ticket_id))
        cfg = await _ticket_room_cfg(db, interaction.guild.id)
        new_view = TicketRoomView(self.ticket_id, cfg, interaction.user.id)
        await interaction.response.edit_message(view=new_view)
        interaction.client.add_view(new_view)
        await interaction.followup.send(f"🙋 **Claimed By:**\n{interaction.user.mention}")

    async def _cb_unclaim(self, interaction: discord.Interaction):
        db = interaction.client.db
        row = await db.fetchone("SELECT * FROM tickets WHERE id=?", (self.ticket_id,))
        if not row:
            return await _safe_reply(interaction, "Ticket not found.")
        if not await _is_ticket_staff(db, interaction.guild, interaction.user, row):
            return await _safe_reply(interaction, "Staff only.")
        if row["claimed_by"] and row["claimed_by"] != interaction.user.id \
                and not is_admin_or_mod(interaction.user):
            return await _safe_reply(
                interaction,
                f"Only <@{row['claimed_by']}> (or an admin) can unclaim this.")
        await db.execute("UPDATE tickets SET claimed_by=NULL WHERE id=?", (self.ticket_id,))
        cfg = await _ticket_room_cfg(db, interaction.guild.id)
        new_view = TicketRoomView(self.ticket_id, cfg, None)
        await interaction.response.edit_message(view=new_view)
        interaction.client.add_view(new_view)
        await interaction.followup.send(f"↩ Ticket unclaimed by {interaction.user.mention}.")

    async def _cb_add_user(self, interaction: discord.Interaction):
        db = interaction.client.db
        row = await db.fetchone("SELECT * FROM tickets WHERE id=?", (self.ticket_id,))
        if not row or not await _is_ticket_staff(db, interaction.guild, interaction.user, row):
            return await _safe_reply(interaction, "Staff only.")
        await interaction.response.send_modal(TicketAddUserModal(self.ticket_id))

    async def _cb_remove_user(self, interaction: discord.Interaction):
        db = interaction.client.db
        row = await db.fetchone("SELECT * FROM tickets WHERE id=?", (self.ticket_id,))
        if not row or not await _is_ticket_staff(db, interaction.guild, interaction.user, row):
            return await _safe_reply(interaction, "Staff only.")
        await interaction.response.send_modal(TicketRemoveUserModal(self.ticket_id))

    async def _cb_lock(self, interaction: discord.Interaction):
        db = interaction.client.db
        row = await db.fetchone("SELECT * FROM tickets WHERE id=?", (self.ticket_id,))
        if not row or not await _is_ticket_staff(db, interaction.guild, interaction.user, row):
            return await _safe_reply(interaction, "Staff only.")
        creator = interaction.guild.get_member(row["user_id"])
        try:
            if creator:
                await interaction.channel.set_permissions(
                    creator, view_channel=True, send_messages=False,
                    read_message_history=True)
        except Exception:
            pass
        await interaction.response.send_message(
            f"🔒 Ticket locked by {interaction.user.mention}.")

    async def _cb_unlock(self, interaction: discord.Interaction):
        db = interaction.client.db
        row = await db.fetchone("SELECT * FROM tickets WHERE id=?", (self.ticket_id,))
        if not row or not await _is_ticket_staff(db, interaction.guild, interaction.user, row):
            return await _safe_reply(interaction, "Staff only.")
        creator = interaction.guild.get_member(row["user_id"])
        try:
            if creator:
                await interaction.channel.set_permissions(
                    creator, view_channel=True, send_messages=True,
                    read_message_history=True)
        except Exception:
            pass
        await interaction.response.send_message(
            f"🔓 Ticket unlocked by {interaction.user.mention}.")

    async def _cb_transcript(self, interaction: discord.Interaction):
        db = interaction.client.db
        row = await db.fetchone("SELECT * FROM tickets WHERE id=?", (self.ticket_id,))
        if not row or not await _is_ticket_staff(db, interaction.guild, interaction.user, row):
            return await _safe_reply(interaction, "Staff only.")
        await interaction.response.defer(ephemeral=True)
        ch_id = await db.get_config(interaction.guild.id, "ticket.transcript_channel") or \
            await db.get_config(interaction.guild.id, "ticket.logs_channel")
        target = interaction.guild.get_channel(int(ch_id)) if ch_id else None
        if target:
            try:
                await target.send(
                    content=f"📄 Transcript for ticket #{row['id']} "
                            f"({interaction.channel.mention})",
                    file=await _generate_transcript(interaction.channel))
                await interaction.followup.send(
                    f"✅ Transcript sent to {target.mention}.", ephemeral=True)
            except Exception:
                log.exception("Failed to send transcript for ticket %s", row["id"])
                await interaction.followup.send(
                    "❌ Failed to send transcript.", ephemeral=True)
        else:
            await interaction.followup.send(
                file=await _generate_transcript(interaction.channel), ephemeral=True)

    async def _cb_notify(self, interaction: discord.Interaction):
        db = interaction.client.db
        row = await db.fetchone("SELECT * FROM tickets WHERE id=?", (self.ticket_id,))
        if not row:
            return await _safe_reply(interaction, "Ticket not found.")
        bot = interaction.client
        now = now_utc().timestamp()
        last = bot._notify_cooldown.get(self.ticket_id, 0)
        if now - last < NOTIFY_COOLDOWN_SECONDS:
            rem = int(NOTIFY_COOLDOWN_SECONDS - (now - last))
            return await _safe_reply(interaction,
                                     f"⏳ Please wait {rem}s before notifying again.")
        bot._notify_cooldown[self.ticket_id] = now
        role_id = await db.get_config(interaction.guild.id, "ticket.support_role_id")
        if not role_id:
            t = await db.fetchone(
                "SELECT support_role_id FROM ticket_types WHERE guild_id=? AND name=?",
                (interaction.guild.id, row["type"]))
            role_id = t["support_role_id"] if t else None
        mention = f"<@&{role_id}>" if role_id else "@here"
        await interaction.response.send_message(
            f"🔔 **Staff Notification**\n\n{mention}\n\n"
            f"A user needs help in:\n{interaction.channel.mention}")


# =====================================================================
# Other persistent views
# =====================================================================

class ReactionRoleView(discord.ui.View):
    def __init__(self, panel_id: int, roles: list[dict], mode: str = "button"):
        super().__init__(timeout=None)
        self.panel_id = panel_id
        if mode == "select":
            opts = [discord.SelectOption(
                label=r["label"][:100] or f"Role {r['role_id']}",
                value=str(r["role_id"]),
                emoji=r.get("emoji") or None) for r in roles[:25]]
            if opts:
                sel = discord.ui.Select(
                    placeholder="Toggle a role…", options=opts,
                    custom_id=f"freakos:rr:select:{panel_id}",
                    min_values=0, max_values=len(opts))
                sel.callback = self._select_cb
                self.add_item(sel)
        else:
            for r in roles[:24]:
                btn = discord.ui.Button(
                    label=(r["label"] or f"Role {r['role_id']}")[:80],
                    emoji=r.get("emoji") or None,
                    style=discord.ButtonStyle.secondary,
                    custom_id=f"freakos:rr:btn:{panel_id}:{r['role_id']}")
                btn.callback = self._make_toggle(r["role_id"])
                self.add_item(btn)

    def _make_toggle(self, role_id: int):
        async def cb(interaction: discord.Interaction):
            await _toggle_role(interaction, role_id)
        return cb

    async def _select_cb(self, interaction: discord.Interaction):
        selected = {int(v) for v in interaction.data.get("values", [])}
        rows = await interaction.client.db.fetchall(
            "SELECT role_id FROM reaction_roles WHERE panel_id=?", (self.panel_id,))
        all_ids = {r["role_id"] for r in rows}
        to_add = selected
        to_remove = all_ids - selected
        try:
            add_roles = [interaction.guild.get_role(i) for i in to_add
                         if interaction.guild.get_role(i)]
            rem_roles = [interaction.guild.get_role(i) for i in to_remove
                         if interaction.guild.get_role(i)]
            if add_roles:
                await interaction.user.add_roles(*add_roles, reason="Reaction role")
            if rem_roles:
                await interaction.user.remove_roles(*rem_roles, reason="Reaction role")
            await interaction.response.send_message("Roles updated.", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message(
                "Missing permissions.", ephemeral=True)


async def _toggle_role(interaction: discord.Interaction, role_id: int):
    role = interaction.guild.get_role(role_id)
    if not role:
        return await interaction.response.send_message(
            "Role no longer exists.", ephemeral=True)
    try:
        if role in interaction.user.roles:
            await interaction.user.remove_roles(role, reason="Reaction role")
            await interaction.response.send_message(
                f"Removed {role.mention}.", ephemeral=True)
        else:
            await interaction.user.add_roles(role, reason="Reaction role")
            await interaction.response.send_message(
                f"Added {role.mention}.", ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message(
            "I lack permission (check role hierarchy).", ephemeral=True)


class GiveawayView(discord.ui.View):
    def __init__(self, giveaway_id: int):
        super().__init__(timeout=None)
        self.giveaway_id = giveaway_id

    @discord.ui.button(label="Enter", style=discord.ButtonStyle.success, emoji="🎉",
                       custom_id="freakos:gw:enter")
    async def enter(self, interaction: discord.Interaction, _):
        gw = await interaction.client.db.fetchone(
            "SELECT * FROM giveaways WHERE id=?", (self.giveaway_id,))
        if not gw or gw["ended"]:
            return await interaction.response.send_message(
                "Giveaway ended.", ephemeral=True)
        await interaction.client.db.execute(
            "INSERT OR IGNORE INTO giveaway_entries (giveaway_id, user_id) "
            "VALUES (?, ?)", (self.giveaway_id, interaction.user.id))
        await interaction.response.send_message("✅ Entered!", ephemeral=True)


class ShopPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Browse Products", style=discord.ButtonStyle.primary,
                       emoji="🛒", custom_id="freakos:shop:browse")
    async def browse(self, interaction: discord.Interaction, _):
        rows = await interaction.client.db.fetchall(
            "SELECT id, name, price, stock, active FROM shop_products "
            "WHERE guild_id=? ORDER BY id", (interaction.guild.id,))
        if not rows:
            return await interaction.response.send_message(
                "No products yet.", ephemeral=True)
        lines = []
        for r in rows:
            stock = "∞" if r["stock"] < 0 else r["stock"]
            flag = "✅" if r["active"] else "❌"
            lines.append(f"{flag} `#{r['id']}` **{r['name']}** — "
                         f"{r['price']} (stock {stock})")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @discord.ui.button(label="Buy", style=discord.ButtonStyle.success,
                       emoji="💳", custom_id="freakos:shop:buy")
    async def buy(self, interaction: discord.Interaction, _):
        await interaction.response.send_modal(BuyModal())


class BuyModal(discord.ui.Modal, title="Buy Product"):
    product_id = discord.ui.TextInput(label="Product ID", placeholder="1")
    quantity = discord.ui.TextInput(label="Quantity", default="1")

    async def on_submit(self, interaction: discord.Interaction):
        try:
            pid = int(self.product_id.value)
            qty = max(1, int(self.quantity.value))
        except ValueError:
            return await interaction.response.send_message(
                "Invalid numbers.", ephemeral=True)
        db: Database = interaction.client.db
        p = await db.fetchone(
            "SELECT * FROM shop_products WHERE id=? AND guild_id=?",
            (pid, interaction.guild.id))
        if not p or not p["active"]:
            return await interaction.response.send_message(
                "Product not available.", ephemeral=True)
        if p["stock"] >= 0 and p["stock"] < qty:
            return await interaction.response.send_message(
                "Not enough stock.", ephemeral=True)
        total = p["price"] * qty
        oid = await db.execute(
            "INSERT INTO orders (guild_id, user_id, product_id, quantity, price, "
            "status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)",
            (interaction.guild.id, interaction.user.id, pid, qty, total,
             iso(now_utc()), iso(now_utc())))
        if p["stock"] >= 0:
            await db.execute("UPDATE shop_products SET stock=stock-? WHERE id=?",
                             (qty, pid))
        await interaction.response.send_message(
            f"✅ Order `#{oid}` created for {qty}× **{p['name']}** — total {total}.\n"
            "Await payment instructions from staff.", ephemeral=True)
        await _log_guild(interaction.client, interaction.guild, "shop", "New Order",
                         f"{interaction.user.mention} ordered `#{oid}` "
                         f"({qty}× {p['name']}).")


# =====================================================================
# Logging helper
# =====================================================================

async def _log_guild(bot: "Freakos", guild: discord.Guild, event_key: str,
                     title: str, description: str, color: int = 0x5865F2):
    if not guild:
        return
    channel_id = await bot.db.get_config(guild.id, "logging.channel")
    if not channel_id:
        return
    enabled = await bot.db.get_json(guild.id, "logging.events", default=[])
    if event_key not in enabled:
        return
    ch = guild.get_channel(int(channel_id))
    if not ch:
        return
    try:
        await ch.send(embed=make_embed(title=title, description=description, color=color))
    except Exception:
        pass


# =====================================================================
# AutoMod engine
# =====================================================================

AM_RULES = ["spam", "links", "invites", "words", "mentions", "caps",
            "emojis", "characters", "everyone", "raid", "accounts", "antinuke"]
AM_ACTIONS = ("delete", "warn", "timeout", "kick", "ban")
AM_ACTION_SEVERITY = {"delete": 0, "warn": 1, "timeout": 2, "kick": 3, "ban": 4}

AM_DEFAULT_RULES = {
    "spam": {"enabled": True, "action": "delete", "max_messages": 5, "window": 5,
             "duplicate_enabled": True, "duplicate_count": 3, "duplicate_window": 12},
    "links": {"enabled": False, "action": "delete", "mode": "blacklist",
              "blacklist": [], "allowlist": []},
    "invites": {"enabled": True, "action": "delete"},
    "words": {"enabled": True, "action": "delete", "words": [], "match": "partial"},
    "mentions": {"enabled": True, "action": "delete", "max_mentions": 6,
                 "max_role_mentions": 4},
    "caps": {"enabled": False, "action": "delete", "percent": 70, "min_length": 12},
    "emojis": {"enabled": False, "action": "delete", "max_emojis": 6},
    "characters": {"enabled": False, "action": "delete", "max_repeat": 8},
    "everyone": {"enabled": True, "action": "delete"},
    "raid": {"enabled": False, "action": "kick", "max_joins": 6, "window": 12},
    "accounts": {"enabled": False, "action": "kick", "min_age_days": 7},
    "antinuke": {"enabled": False, "action": "ban", "max_deletes": 3, "window": 30},
}

AM_DEFAULT_MESSAGE = ("{mention} your message was removed.\n"
                      "**Rule:** `{rule}` · **Action:** `{action}`")

AM_INVITE_RE = re.compile(
    r"(?:discord\.gg|discord(?:app)?\.com/invite)/[A-Za-z0-9\-]+", re.I)
AM_URL_RE = re.compile(r"(?:https?://|www\.)[^\s<]+", re.I)
AM_CUSTOM_EMOJI_RE = re.compile(r"<a?:[A-Za-z0-9_]{2,32}:[0-9]{15,25}>")
AM_UNICODE_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U00002190-\U000021FF"
    "\U00002B00-\U00002BFF\U0000FE00-\U0000FE0F]")


def _am_default_config() -> dict:
    rules = {}
    for k, v in AM_DEFAULT_RULES.items():
        rules[k] = {kk: (list(vv) if isinstance(vv, list) else vv)
                    for kk, vv in v.items()}
    return {
        "rules": rules,
        "whitelist": {"users": [], "roles": [], "channels": []},
        "rule_whitelist": {},
        "messages": {"violation": AM_DEFAULT_MESSAGE, "delete_after": 8, "dm": False},
        "timeout_duration": 600,
        "logs": {"enabled": True, "channel": None},
    }


def _am_int_list(v) -> list:
    out = []
    if isinstance(v, list):
        for x in v:
            try:
                out.append(int(x))
            except (TypeError, ValueError):
                continue
    return out


def _am_merge_defaults(stored: dict) -> dict:
    cfg = _am_default_config()
    if not isinstance(stored, dict):
        return cfg
    sr = stored.get("rules") if isinstance(stored.get("rules"), dict) else {}
    legacy_action = stored.get("_legacy_action")
    for name in AM_RULES:
        got = sr.get(name)
        if isinstance(got, dict):
            cfg["rules"][name].update(got)
    if legacy_action in AM_ACTIONS:
        for name in AM_RULES:
            got = sr.get(name)
            if not isinstance(got, dict) or "action" not in got:
                cfg["rules"][name]["action"] = legacy_action
    if isinstance(stored.get("whitelist"), dict):
        for k in ("users", "roles", "channels"):
            cfg["whitelist"][k] = _am_int_list(stored["whitelist"].get(k))
    if isinstance(stored.get("rule_whitelist"), dict):
        for rule, wl in stored["rule_whitelist"].items():
            if isinstance(wl, dict):
                cfg["rule_whitelist"][rule] = {
                    k: _am_int_list(wl.get(k)) for k in ("users", "roles", "channels")}
    if isinstance(stored.get("messages"), dict):
        cfg["messages"].update(stored["messages"])
    try:
        if stored.get("timeout_duration") is not None:
            cfg["timeout_duration"] = int(stored["timeout_duration"])
    except (TypeError, ValueError):
        pass
    if isinstance(stored.get("logs"), dict):
        cfg["logs"].update(stored["logs"])
    return cfg


async def am_load_config(db: "Database", guild_id: int) -> dict:
    stored = await db.get_json(guild_id, "automod.config", default=None)
    need_save = False
    if not isinstance(stored, dict):
        stored = {}
        old = await db.get_json(guild_id, "automod.settings", default=None)
        oldwl = await db.get_json(guild_id, "automod.whitelist", default=None)
        if isinstance(old, dict) and old:
            need_save = True
            rules: dict = {}
            if "antispam" in old:
                rules["spam"] = {"enabled": bool(old["antispam"])}
            if "links" in old:
                rules["links"] = {"enabled": bool(old["links"])}
            if "invites" in old:
                rules["invites"] = {"enabled": bool(old["invites"])}
            if "mentions" in old:
                rules["mentions"] = {"enabled": bool(old["mentions"])}
            if isinstance(old.get("words"), list) and old["words"]:
                rules["words"] = {"enabled": True,
                                  "words": [str(w) for w in old["words"]]}
            stored["rules"] = rules
            if old.get("action") in AM_ACTIONS:
                stored["_legacy_action"] = old["action"]
        if isinstance(oldwl, dict) and oldwl:
            need_save = True
            stored["whitelist"] = oldwl
    cfg = _am_merge_defaults(stored)
    cfg["_master"] = (await db.get_config(guild_id, "automod.enabled", "0") == "1")
    if need_save:
        await am_save_config(db, guild_id, cfg)
    return cfg


async def am_save_config(db: "Database", guild_id: int, cfg: dict):
    clean = {k: v for k, v in cfg.items() if not str(k).startswith("_")}
    await db.set_json(guild_id, "automod.config", clean)


def am_rule(cfg: dict, name: str) -> dict:
    return cfg.setdefault("rules", {}).setdefault(name, dict(AM_DEFAULT_RULES.get(name, {})))


def _am_wl_hit(wl: dict, member, channel) -> bool:
    if not isinstance(wl, dict):
        return False
    try:
        if member is not None and member.id in _am_int_list(wl.get("users")):
            return True
        if member is not None:
            rids = set(_am_int_list(wl.get("roles")))
            for r in getattr(member, "roles", []) or []:
                if getattr(r, "id", None) in rids:
                    return True
        if channel is not None:
            cid = getattr(channel, "id", None)
            if cid is not None and cid in _am_int_list(wl.get("channels")):
                return True
    except Exception:
        return False
    return False


def am_global_whitelisted(cfg: dict, member, channel) -> bool:
    return _am_wl_hit(cfg.get("whitelist", {}), member, channel)


def am_rule_whitelisted(cfg: dict, rule: str, member, channel) -> bool:
    return _am_wl_hit((cfg.get("rule_whitelist", {}) or {}).get(rule, {}),
                      member, channel)


def am_domains(content: str) -> list:
    out = []
    for url in AM_URL_RE.findall(content or ""):
        u = url.lower().rstrip(").,>]}\"'")
        if u.startswith("www."):
            u = u[4:]
        elif "://" in u:
            u = u.split("://", 1)[1]
        host = u.split("/", 1)[0].split("?", 1)[0]
        if host:
            out.append(host)
    return out


def am_count_emojis(content: str) -> int:
    if not content:
        return 0
    return len(AM_CUSTOM_EMOJI_RE.findall(content)) + \
        len(AM_UNICODE_EMOJI_RE.findall(content))


def am_caps_ratio(content: str) -> float:
    letters = [c for c in (content or "") if c.isalpha()]
    if not letters:
        return 0.0
    upper = sum(1 for c in letters if c.isupper())
    return (upper / len(letters)) * 100.0


def am_repeat_hit(content: str, max_repeat: int) -> bool:
    if not content:
        return False
    try:
        n = int(max_repeat)
    except (TypeError, ValueError):
        n = 8
    if n < 2:
        return False
    return bool(re.search(r"(.)\1{" + str(n - 1) + r",}", content))


def am_timeout_seconds(cfg: dict, rule_name: str) -> int:
    r = (cfg.get("rules", {}) or {}).get(rule_name, {}) or {}
    for src in (r.get("timeout_duration"), cfg.get("timeout_duration"), 600):
        try:
            v = int(src)
            if v > 0:
                return v
        except (TypeError, ValueError):
            continue
    return 600


async def am_record_violation(db: "Database", guild_id: int, user_id: int,
                              rule: str, action: str, channel_id, reason: str):
    try:
        await db.execute(
            "INSERT INTO automod_violations (guild_id, user_id, rule, action, "
            "channel_id, reason, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (guild_id, user_id, rule, action, channel_id,
             (reason or "")[:500], iso(now_utc())))
    except Exception:
        log.exception("automod: failed to record violation")


async def am_log(bot: "Freakos", guild: discord.Guild, title: str,
                 description: str, color: int = 0xE67E22, cfg: dict = None):
    if not guild:
        return
    try:
        if cfg is None:
            cfg = await am_load_config(bot.db, guild.id)
        logs = cfg.get("logs", {}) or {}
        if not logs.get("enabled", True):
            return
        ch_id = logs.get("channel")
        ch = guild.get_channel(int(ch_id)) if ch_id else None
        if ch:
            try:
                await ch.send(embed=make_embed(
                    title=title, description=(description or "")[:4000], color=color))
                return
            except Exception:
                pass
    except Exception:
        pass
    # Fall back to the existing logging system.
    await _log_guild(bot, guild, "automod", title, description, color)


def am_render_violation(cfg: dict, member, guild, rule: str,
                        action: str, reason: str) -> str:
    tpl = (cfg.get("messages", {}) or {}).get("violation") or AM_DEFAULT_MESSAGE
    return apply_placeholders(
        tpl, user=getattr(member, "name", str(member)),
        mention=getattr(member, "mention", str(member)),
        username=getattr(member, "name", str(member)),
        display_name=getattr(member, "display_name", getattr(member, "name", "")),
        server=getattr(guild, "name", ""), rule=rule, action=action, reason=reason)


async def _am_action_dm(bot: "Freakos", guild: discord.Guild, member,
                        template_key: str, **extra):
    try:
        if await bot.db.get_config(guild.id, "actiondm.enabled", "0") != "1":
            return
        msg = await bot.db.get_config(guild.id, template_key)
        if not msg:
            return
        rendered = apply_placeholders(
            msg, user=member.name, mention=member.mention,
            username=member.name, display_name=member.display_name,
            server=guild.name, timestamp=fmt_dt(now_utc()), **extra)
        for c in chunk_message(rendered):
            await member.send(c)
    except Exception:
        pass


async def am_punish(bot: "Freakos", guild: discord.Guild, member, action: str,
                    reason: str, rule_name: str, cfg: dict,
                    message: discord.Message = None) -> bool:
    """Apply a single AutoMod punishment safely. Returns True if action applied."""
    # --- Safety gates: never punish bots, self, owner, or administrators. ---
    if guild is None or member is None:
        return False
    if getattr(member, "bot", False):
        return False
    if bot.user is not None and member.id == bot.user.id:
        return False
    if member.id == guild.owner_id:
        return False
    perms = getattr(member, "guild_permissions", None)
    if perms is not None and getattr(perms, "administrator", False):
        return False

    # Delete the offending message first (applies to every action).
    if message is not None:
        try:
            await message.delete()
        except Exception:
            pass

    action = (action or "delete").lower()
    if action not in AM_ACTIONS:
        action = "delete"

    ch_id = getattr(getattr(message, "channel", None), "id", None)
    full_reason = f"AutoMod [{rule_name}]: {reason}"[:500]
    await am_record_violation(bot.db, guild.id, member.id, rule_name,
                              action, ch_id, reason)

    if action == "delete":
        return True

    if not isinstance(member, discord.Member):
        return False
    ok, why = hierarchy_ok(guild, member)
    if not ok:
        await am_log(bot, guild, "AutoMod",
                     f"⚠️ Could not punish {member.mention} (`{rule_name}`): {why}",
                     0xE67E22, cfg=cfg)
        return False

    me = guild.me
    bot_id = bot.user.id if bot.user else 0
    try:
        if action == "warn":
            await bot.db.execute(
                "INSERT INTO warnings (guild_id, user_id, moderator_id, reason, "
                "created_at) VALUES (?, ?, ?, ?, ?)",
                (guild.id, member.id, bot_id, full_reason, iso(now_utc())))
            await bot.db.execute(
                "INSERT INTO cases (guild_id, user_id, moderator_id, action, "
                "reason, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (guild.id, member.id, bot_id, "warn", full_reason, iso(now_utc())))
            return True

        if action == "timeout":
            if me is None or not me.guild_permissions.moderate_members:
                await am_log(bot, guild, "AutoMod",
                             f"⚠️ Missing **Moderate Members** to timeout {member.mention}.",
                             0xE67E22, cfg=cfg)
                return False
            secs = max(1, min(am_timeout_seconds(cfg, rule_name), 28 * 86400))
            until = now_utc() + timedelta(seconds=secs)
            await member.timeout(timedelta(seconds=secs), reason=full_reason)
            await bot.db.execute(
                "INSERT INTO cases (guild_id, user_id, moderator_id, action, "
                "reason, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (guild.id, member.id, bot_id, "timeout", full_reason, iso(now_utc())))
            tid = await bot.db.execute(
                "INSERT INTO timeouts (guild_id, user_id, moderator_id, reason, "
                "ends_at) VALUES (?, ?, ?, ?, ?)",
                (guild.id, member.id, bot_id, full_reason, iso(until)))
            try:
                await bot.scheduler.schedule(
                    "timeout_end", until + timedelta(seconds=5),
                    {"timeout_id": tid}, guild.id)
            except Exception:
                pass
            await _am_action_dm(bot, guild, member, "actiondm.timeout_msg",
                                moderator=f"<@{bot_id}>", reason=full_reason,
                                duration=fmt_duration(secs),
                                timeout_end=fmt_dt(until))
            return True

        if action == "kick":
            if me is None or not me.guild_permissions.kick_members:
                await am_log(bot, guild, "AutoMod",
                             f"⚠️ Missing **Kick Members** to kick {member.mention}.",
                             0xE67E22, cfg=cfg)
                return False
            await bot.db.execute(
                "INSERT INTO cases (guild_id, user_id, moderator_id, action, "
                "reason, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (guild.id, member.id, bot_id, "kick", full_reason, iso(now_utc())))
            await _am_action_dm(bot, guild, member, "actiondm.kick_msg",
                                moderator=f"<@{bot_id}>", reason=full_reason)
            await member.kick(reason=full_reason)
            return True

        if action == "ban":
            if me is None or not me.guild_permissions.ban_members:
                await am_log(bot, guild, "AutoMod",
                             f"⚠️ Missing **Ban Members** to ban {member.mention}.",
                             0xE67E22, cfg=cfg)
                return False
            await bot.db.execute(
                "INSERT INTO cases (guild_id, user_id, moderator_id, action, "
                "reason, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (guild.id, member.id, bot_id, "ban", full_reason, iso(now_utc())))
            await _am_action_dm(bot, guild, member, "actiondm.ban_msg",
                                moderator=f"<@{bot_id}>", reason=full_reason)
            try:
                await member.ban(reason=full_reason, delete_message_seconds=86400)
            except TypeError:
                await member.ban(reason=full_reason, delete_message_days=1)
            return True
    except discord.Forbidden:
        await am_log(bot, guild, "AutoMod",
                     f"⚠️ Forbidden while applying `{action}` to {member.mention} "
                     f"(`{rule_name}`). Check permissions/hierarchy.",
                     0xE67E22, cfg=cfg)
    except Exception:
        log.exception("automod punishment failed (action=%s rule=%s)", action, rule_name)
    return False


# =====================================================================
# Scheduler-driven tasks
# =====================================================================

async def _run_timeout_end(bot: "Freakos", payload: dict):
    tid = payload.get("timeout_id")
    if not tid:
        return
    row = await bot.db.fetchone("SELECT * FROM timeouts WHERE id=?", (tid,))
    if not row or row["dm_sent"]:
        return
    guild = bot.get_guild(row["guild_id"])
    if not guild:
        await bot.db.execute("UPDATE timeouts SET dm_sent=1 WHERE id=?", (tid,))
        return
    member = guild.get_member(row["user_id"])
    if member:
        try:
            until = member.timed_out_until
            if until and until > now_utc():
                await bot.scheduler.schedule(
                    "timeout_end", until + timedelta(seconds=5),
                    {"timeout_id": tid}, guild.id)
                return
        except Exception:
            pass
        msg = await bot.db.get_config(guild.id, "actiondm.timeout_end_msg")
        if msg:
            rendered = apply_placeholders(
                msg, user=member.name, mention=member.mention,
                username=member.name, display_name=member.display_name,
                server=guild.name, reason=row["reason"] or "No reason provided.",
                timestamp=fmt_dt(now_utc()), case_id=str(tid),
                moderator=f"<@{row['moderator_id']}>")
            try:
                for chunk in chunk_message(rendered):
                    await member.send(chunk)
            except Exception:
                pass
    await bot.db.execute("UPDATE timeouts SET dm_sent=1 WHERE id=?", (tid,))


async def _run_giveaway_end(bot: "Freakos", payload: dict):
    gid = payload.get("giveaway_id")
    if gid:
        await _end_giveaway(bot, gid)


async def _run_announcement(bot: "Freakos", payload: dict):
    aid = payload.get("announcement_id")
    if not aid:
        return
    row = await bot.db.fetchone("SELECT * FROM announcements WHERE id=?", (aid,))
    if not row or row["sent"]:
        return
    guild = bot.get_guild(row["guild_id"])
    if not guild:
        await bot.db.execute("UPDATE announcements SET sent=1 WHERE id=?", (aid,))
        return
    channel = guild.get_channel(row["channel_id"])
    if not channel:
        await bot.db.execute("UPDATE announcements SET sent=1 WHERE id=?", (aid,))
        return
    embed = make_embed(title=row["title"] or None, description=row["body"] or None)
    if row["image"]:
        embed.set_image(url=row["image"])
    if row["footer"]:
        embed.set_footer(text=row["footer"])
    try:
        await channel.send(embed=embed)
    except Exception:
        log.exception("Failed to send announcement %s", aid)
    await bot.db.execute("UPDATE announcements SET sent=1 WHERE id=?", (aid,))


async def _run_ticket_delete(bot: "Freakos", payload: dict):
    tid = payload.get("ticket_id")
    if not tid:
        return
    row = await bot.db.fetchone("SELECT * FROM tickets WHERE id=?", (tid,))
    if not row or row["status"] != "closed":
        return
    guild = bot.get_guild(row["guild_id"])
    channel = guild.get_channel(row["channel_id"]) if guild else None
    if channel:
        try:
            await channel.delete(reason="Ticket auto-deleted after close")
        except Exception:
            log.exception("Failed to auto-delete ticket channel %s", row["channel_id"])
    await bot.db.execute("DELETE FROM tickets WHERE id=?", (tid,))


async def _end_giveaway(bot: "Freakos", gid: int, reroll: bool = False):
    row = await bot.db.fetchone("SELECT * FROM giveaways WHERE id=?", (gid,))
    if not row:
        return
    entries = await bot.db.fetchall(
        "SELECT user_id FROM giveaway_entries WHERE giveaway_id=?", (gid,))
    guild = bot.get_guild(row["guild_id"])
    winners_text = "No valid entries 😢"
    winner_ids: list[int] = []
    if entries and guild:
        pool = [e["user_id"] for e in entries]
        random.shuffle(pool)
        n = min(row["winners"], len(pool))
        winner_ids = pool[:n]
        winners_text = ", ".join(f"<@{w}>" for w in winner_ids)
    channel = guild.get_channel(row["channel_id"]) if guild else None
    if channel:
        try:
            if row["message_id"]:
                msg = await channel.fetch_message(row["message_id"])
                embed = msg.embeds[0] if msg.embeds else make_embed(title="Giveaway")
                embed.title = f"🎉 Giveaway Ended — {row['prize']}"
                embed.description = f"Winners: {winners_text}"
                await msg.edit(embed=embed, view=None)
        except Exception:
            pass
        try:
            await channel.send(f"🎉 **{row['prize']}** ended! Winners: {winners_text}")
        except Exception:
            pass
    if not reroll:
        await bot.db.execute("UPDATE giveaways SET ended=1 WHERE id=?", (gid,))


# =====================================================================
# Bot
# =====================================================================

class Freakos(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.guilds = True
        intents.members = True
        intents.messages = True
        intents.message_content = True
        intents.voice_states = True

        super().__init__(command_prefix="!", intents=intents, help_command=None)
        self.db = Database(DB_PATH)
        self.scheduler: Optional[Scheduler] = None
        self._spam_tracker: dict[tuple, list[float]] = {}
        self._notify_cooldown: dict[int, float] = {}
        # AutoMod in-memory trackers (rate-based only; config/stats persist in DB)
        self._am_rate: dict[tuple, list[float]] = {}
        self._am_dup: dict[tuple, list[tuple]] = {}
        self._am_joins: dict[int, list[float]] = {}
        self._am_nuke: dict[tuple, list[float]] = {}

    async def setup_hook(self):
        await self.db.connect()
        self.scheduler = Scheduler(self)
        self.scheduler.start()
        register_all_commands(self)
        await self._restore_persistent_views()

        # Force-sync commands to Discord (fixes CommandSignatureMismatch and
        # ensures edited commands like /vcnotify setup & add are pushed live).
        try:
            synced = await self.tree.sync()
            log.info("Synced %d slash commands (global)", len(synced))
        except Exception:
            log.exception("Global command sync failed")
            for g in list(self.guilds):
                try:
                    self.tree.copy_global_to(guild=g)
                    await self.tree.sync(guild=g)
                    log.info("Synced commands for guild %s", g.id)
                except Exception:
                    log.exception("Guild sync failed for %s", g.id)

    async def _restore_persistent_views(self):
        self.add_view(TicketCreateButton())
        self.add_view(ShopPanelView())
        rows = await self.db.fetchall(
            "SELECT id, guild_id, claimed_by FROM tickets WHERE status='open'")
        for r in rows:
            cfg = await _ticket_room_cfg(self.db, r["guild_id"])
            self.add_view(TicketRoomView(r["id"], cfg, r["claimed_by"]))
        gws = await self.db.fetchall("SELECT id FROM giveaways WHERE ended=0")
        for g in gws:
            self.add_view(GiveawayView(g["id"]))
        panels = await self.db.fetchall("SELECT * FROM reaction_panels")
        for p in panels:
            roles = await self.db.fetchall(
                "SELECT role_id, label, emoji FROM reaction_roles WHERE panel_id=?",
                (p["id"],))
            self.add_view(ReactionRoleView(
                p["id"], [dict(r) for r in roles], p["mode"] or "button"))

    async def close(self):
        try:
            if self.scheduler:
                await self.scheduler.stop()
            await self.db.close()
        finally:
            await super().close()

    async def on_ready(self):
        log.info("Logged in as %s (%s) | guilds=%d",
                 self.user, self.user.id, len(self.guilds))
        try:
            await self.change_presence(activity=discord.Activity(
                type=discord.ActivityType.watching, name="over the server"))
        except Exception:
            pass

    async def task_timeout_end(self, payload: dict):
        await _run_timeout_end(self, payload)

    async def task_giveaway_end(self, payload: dict):
        await _run_giveaway_end(self, payload)

    async def task_announcement(self, payload: dict):
        await _run_announcement(self, payload)

    async def task_ticket_delete(self, payload: dict):
        await _run_ticket_delete(self, payload)

    # ---------- MEMBER EVENTS ----------

    async def on_member_join(self, member: discord.Member):
        guild = member.guild

        if await self.db.get_config(guild.id, "welcome.enabled", "0") == "1":
            ch_id = await self.db.get_config(guild.id, "welcome.channel")
            ch = guild.get_channel(int(ch_id)) if ch_id else None
            if ch:
                msg_tpl = await self.db.get_config(guild.id, "welcome.message") or (
                    "× 〻 Welcome Message 〻 ×\n\n"
                    "𑣲 Welcome:\n{mention}\n\n"
                    "𑣲 Member Count:\n{member_count}\n\n"
                    "𑣲 Joined At:\n{joined_at}")
                ph = dict(
                    user=member.name, mention=member.mention,
                    username=member.name, display_name=member.display_name,
                    server=guild.name, member_count=str(guild.member_count),
                    joined_at=fmt_dt_human(member.joined_at or now_utc()))
                rendered = apply_placeholders(msg_tpl, **ph)
                embed_cfg = await self.db.get_json(
                    guild.id, "welcome.embed", default={}) or {}
                use_embed = embed_cfg.get("enabled", True)
                show_avatar = (await self.db.get_config(
                    guild.id, "welcome.show_avatar", "1")) == "1"
                show_logo = (await self.db.get_config(
                    guild.id, "welcome.show_server_logo", "1")) == "1"
                try:
                    if use_embed:
                        e = discord.Embed(
                            description=(rendered or "")[:4096],
                            color=int(embed_cfg.get("color") or 0x5865F2),
                            timestamp=now_utc())
                        if embed_cfg.get("title"):
                            e.title = apply_placeholders(
                                str(embed_cfg["title"]), **ph)[:256]
                        if embed_cfg.get("footer"):
                            e.set_footer(text=apply_placeholders(
                                str(embed_cfg["footer"]), **ph)[:2048])
                        if show_avatar and member.display_avatar:
                            e.set_thumbnail(url=member.display_avatar.url)
                        elif embed_cfg.get("thumbnail"):
                            e.set_thumbnail(url=str(embed_cfg["thumbnail"]))
                        if show_logo and guild.icon:
                            e.set_image(url=guild.icon.url)
                        elif embed_cfg.get("image"):
                            e.set_image(url=str(embed_cfg["image"]))
                        await ch.send(embed=e)
                    else:
                        for c in chunk_message(rendered):
                            await ch.send(c)
                except Exception:
                    log.exception("Welcome send failed")

        if await self.db.get_config(guild.id, "welcome.dm.enabled", "0") == "1":
            dm_title = await self.db.get_config(guild.id, "welcome.dm.title", "") or ""
            dm_content = await self.db.get_config(guild.id, "welcome.dm.content", "") or ""
            if dm_content.strip():
                dm_ph = dict(
                    user=member.name, mention=member.mention,
                    username=member.name, display_name=member.display_name,
                    server=guild.name, member_count=str(guild.member_count))
                try:
                    dm_embed = discord.Embed(
                        color=0x5865F2, timestamp=now_utc(),
                        description=apply_placeholders(dm_content, **dm_ph)[:4096])
                    if dm_title.strip():
                        dm_embed.title = apply_placeholders(dm_title, **dm_ph)[:256]
                    if guild.icon:
                        dm_embed.set_thumbnail(url=guild.icon.url)
                    await member.send(embed=dm_embed)
                except discord.Forbidden:
                    pass
                except Exception:
                    log.exception("Welcome DM failed for %s", member.id)

        if await self.db.get_config(guild.id, "autorole.enabled", "0") == "1":
            roles = await self.db.get_json(guild.id, "autorole.roles", default=[])
            good = [guild.get_role(rid) for rid in roles]
            good = [r for r in good if r and r < guild.me.top_role and not r.managed]
            if good:
                try:
                    await member.add_roles(*good, reason="Autorole")
                except Exception:
                    log.exception("Autorole failed")

        if await self.db.get_config(guild.id, "autonick.enabled", "0") == "1":
            fmt = await self.db.get_config(guild.id, "autonick.format") or "{user}"
            nick = apply_placeholders(
                fmt, user=member.name, username=member.name,
                display_name=member.display_name, server=guild.name)[:32]
            try:
                if guild.me.guild_permissions.manage_nicknames and \
                        member.top_role < guild.me.top_role:
                    await member.edit(nick=nick, reason="Autonick")
            except Exception:
                pass

        await _log_guild(self, guild, "joins", "Member Joined",
                         f"{member.mention} ({member})")

        try:
            await self._automod_join_check(member)
        except Exception:
            log.exception("automod join check failed")

    async def on_member_remove(self, member: discord.Member):
        guild = member.guild
        try:
            await self._automod_leave_check(member)
        except Exception:
            log.exception("automod leave check failed")
        if await self.db.get_config(guild.id, "departure.enabled", "0") == "1":
            ch_id = await self.db.get_config(guild.id, "departure.channel")
            ch = guild.get_channel(int(ch_id)) if ch_id else None
            if ch:
                tpl = await self.db.get_config(guild.id, "departure.message") or (
                    "× 〻 Member Left 〻 ×\n\n"
                    "𑣲 User:\n{user}\n\n"
                    "𑣲 Left At:\n{left_at}")
                ph = dict(
                    user=member.name, username=member.name,
                    display_name=member.display_name, mention=member.mention,
                    server=guild.name, member_count=str(guild.member_count),
                    left_at=fmt_dt_human(now_utc()))
                rendered = apply_placeholders(tpl, **ph)
                embed_cfg = await self.db.get_json(
                    guild.id, "departure.embed", default={}) or {}
                use_embed = embed_cfg.get("enabled", True)
                show_avatar = (await self.db.get_config(
                    guild.id, "departure.show_avatar", "1")) == "1"
                try:
                    if use_embed:
                        e = discord.Embed(
                            description=(rendered or "")[:4096],
                            color=int(embed_cfg.get("color") or 0xED4245),
                            timestamp=now_utc())
                        if embed_cfg.get("title"):
                            e.title = apply_placeholders(
                                str(embed_cfg["title"]), **ph)[:256]
                        if embed_cfg.get("footer"):
                            e.set_footer(text=apply_placeholders(
                                str(embed_cfg["footer"]), **ph)[:2048])
                        if show_avatar and member.display_avatar:
                            e.set_thumbnail(url=member.display_avatar.url)
                        elif embed_cfg.get("thumbnail"):
                            e.set_thumbnail(url=str(embed_cfg["thumbnail"]))
                        if embed_cfg.get("image"):
                            e.set_image(url=str(embed_cfg["image"]))
                        await ch.send(embed=e)
                    else:
                        for c in chunk_message(rendered):
                            await ch.send(c)
                except Exception:
                    log.exception("Departure send failed")
        await _log_guild(self, guild, "leaves", "Member Left",
                         f"{member.mention} ({member})")

    async def on_message_delete(self, message: discord.Message):
        if not message.guild or message.author.bot:
            return
        await _log_guild(self, message.guild, "messages", "Message Deleted",
                         f"**Author:** {message.author.mention}\n"
                         f"**Channel:** {message.channel.mention}\n"
                         f"**Content:**\n{message.content[:1500] or '*(empty)*'}")

    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if not after.guild or after.author.bot or before.content == after.content:
            return
        await _log_guild(self, after.guild, "messages", "Message Edited",
                         f"**Author:** {after.author.mention}\n"
                         f"**Channel:** {after.channel.mention}\n"
                         f"**Before:**\n{before.content[:700]}\n"
                         f"**After:**\n{after.content[:700]}")

    # ---------- VC NOTIFY ----------

    async def on_voice_state_update(self, member: discord.Member,
                                    before: discord.VoiceState,
                                    after: discord.VoiceState):
        try:
            guild = member.guild
            if member.bot or guild is None:
                return

            old_ch = before.channel
            new_ch = after.channel
            old_id = old_ch.id if old_ch else None
            new_id = new_ch.id if new_ch else None
            if old_id == new_id:
                return

            if await self.db.get_config(guild.id, "vcnotify.enabled", "1") == "0":
                return

            me = guild.me
            if me is None:
                return

            async def _fire(cfg_key: str, ch, tpl_default: str, kind: str):
                # Only voice channels have a built-in text chat (skip stages).
                if not isinstance(ch, discord.VoiceChannel):
                    return
                cfg = await self.db.get_json(guild.id, cfg_key, default={}) or {}
                if not cfg.get("enabled", True):
                    return
                # Make sure we're allowed to post in this VC's own text chat.
                try:
                    perms = ch.permissions_for(me)
                except Exception:
                    perms = None
                if perms is None or not (perms.view_channel and perms.send_messages):
                    log.warning(
                        "VC notify: missing permission to post in #%s text chat",
                        getattr(ch, "name", getattr(ch, "id", "?")))
                    return
                tpl = cfg.get(f"{kind}_msg") or tpl_default
                rendered = apply_placeholders(
                    tpl, mention=member.mention, user=member.name,
                    username=member.name, display_name=member.display_name,
                    channel_name=ch.name, channel_mention=ch.mention,
                    server=guild.name)
                for c in chunk_message(rendered):
                    try:
                        # Posts into the voice channel's OWN built-in text chat.
                        await ch.send(c)
                    except discord.Forbidden:
                        log.warning(
                            "VC notify: forbidden to post in #%s text chat",
                            getattr(ch, "name", getattr(ch, "id", "?")))
                        return
                    except Exception:
                        log.exception(
                            "VC %s notify failed (channel=%s)", kind, ch.id)
                        return

            # Every voice channel works independently — no separate target needed.
            if old_id and old_ch is not None:
                await _fire(f"vcnotify.cfg.{old_id}", old_ch,
                            "👋 {mention} left **{channel_name}**.", "leave")
            if new_id and new_ch is not None:
                await _fire(f"vcnotify.cfg.{new_id}", new_ch,
                            "🎧 {mention} joined **{channel_name}**.", "join")
        except Exception:
            log.exception("VC notify handler crashed")

    # ---------- MESSAGE EVENTS ----------

    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return
        try:
            await self._automod_check(message)
        except Exception:
            log.exception("automod error")

        if message.content.startswith("!"):
            name = message.content[1:].split(" ", 1)[0].lower()
            row = await self.db.fetchone(
                "SELECT * FROM custom_commands WHERE guild_id=? AND name=? AND enabled=1",
                (message.guild.id, name))
            if row:
                rendered = apply_placeholders(
                    row["response"] or "",
                    user=message.author.name, mention=message.author.mention,
                    username=message.author.name,
                    display_name=message.author.display_name,
                    server=message.guild.name)
                try:
                    if row["embed"]:
                        await message.channel.send(embed=make_embed(
                            title=name.title(), description=rendered))
                    else:
                        for c in chunk_message(rendered):
                            await message.channel.send(c)
                except Exception:
                    pass

        await self.process_commands(message)

    async def _automod_check(self, message: discord.Message):
        guild = message.guild
        if guild is None:
            return
        author = message.author
        if author.bot or (self.user and author.id == self.user.id):
            return
        if not isinstance(author, discord.Member):
            return
        # Never auto-moderate the owner or administrators.
        if author.id == guild.owner_id or author.guild_permissions.administrator:
            return

        cfg = await am_load_config(self.db, guild.id)
        if not cfg.get("_master"):
            return
        channel = message.channel
        if am_global_whitelisted(cfg, author, channel):
            return

        content = message.content or ""
        rules = cfg.get("rules", {})
        matches: list = []

        def _add(name: str, reason: str):
            r = rules.get(name, {}) or {}
            matches.append((name, r.get("action", "delete"), reason))

        def _blocked(name: str) -> bool:
            return am_rule_whitelisted(cfg, name, author, channel)

        def _int(d: dict, k: str, default: int) -> int:
            try:
                return int(d.get(k, default))
            except (TypeError, ValueError):
                return default

        # --- @everyone / @here protection ---
        ev = rules.get("everyone", {}) or {}
        if ev.get("enabled") and not _blocked("everyone") and message.mention_everyone:
            if not author.guild_permissions.mention_everyone:
                _add("everyone", "used @everyone/@here without permission")

        # --- Discord invites ---
        inv = rules.get("invites", {}) or {}
        if inv.get("enabled") and not _blocked("invites") and AM_INVITE_RE.search(content):
            _add("invites", "posted a Discord invite link")

        # --- Links (all / blacklist / whitelist) ---
        lk = rules.get("links", {}) or {}
        if lk.get("enabled") and not _blocked("links"):
            doms = am_domains(content)
            if doms:
                mode = (lk.get("mode") or "blacklist").lower()
                bl = [str(d).lower() for d in (lk.get("blacklist") or [])]
                alw = [str(d).lower() for d in (lk.get("allowlist") or [])]
                hit = False
                if mode == "all":
                    hit = True
                elif mode == "blacklist":
                    hit = any(any(d == b or d.endswith("." + b) for b in bl) for d in doms)
                elif mode == "whitelist":
                    hit = any(not any(d == a or d.endswith("." + a) for a in alw)
                              for d in doms)
                if hit:
                    _add("links", f"posted a disallowed link ({', '.join(doms[:3])})")

        # --- Banned words (exact / partial) ---
        wd = rules.get("words", {}) or {}
        if wd.get("enabled") and not _blocked("words"):
            low = content.lower()
            exact = (wd.get("match") or "partial").lower() == "exact"
            for w in (wd.get("words") or []):
                w = str(w).strip()
                if not w:
                    continue
                wl = w.lower()
                if exact:
                    if re.search(r"(?<!\w)" + re.escape(wl) + r"(?!\w)", low):
                        _add("words", f"used banned word `{w}`")
                        break
                elif wl in low:
                    _add("words", f"used banned word `{w}`")
                    break

        # --- Mention spam / role-mention limits ---
        mn = rules.get("mentions", {}) or {}
        if mn.get("enabled") and not _blocked("mentions"):
            max_m = _int(mn, "max_mentions", 6)
            max_rm = _int(mn, "max_role_mentions", 4)
            if len(message.mentions) >= max_m:
                _add("mentions", f"mentioned {len(message.mentions)} users")
            elif len(message.role_mentions) >= max_rm:
                _add("mentions", f"mentioned {len(message.role_mentions)} roles")

        # --- Excessive caps ---
        cp = rules.get("caps", {}) or {}
        if cp.get("enabled") and not _blocked("caps"):
            min_len = _int(cp, "min_length", 12)
            try:
                pct = float(cp.get("percent", 70))
            except (TypeError, ValueError):
                pct = 70.0
            if len(content) >= min_len and am_caps_ratio(content) >= pct:
                _add("caps", "excessive capital letters")

        # --- Emoji spam (unicode + custom) ---
        em = rules.get("emojis", {}) or {}
        if em.get("enabled") and not _blocked("emojis"):
            n = am_count_emojis(content)
            if n >= _int(em, "max_emojis", 6):
                _add("emojis", f"used {n} emojis")

        # --- Repeated-character spam ---
        chr_ = rules.get("characters", {}) or {}
        if chr_.get("enabled") and not _blocked("characters"):
            if am_repeat_hit(content, _int(chr_, "max_repeat", 8)):
                _add("characters", "repeated characters")

        # --- Spam (rate) + duplicate/repeated spam ---
        sp = rules.get("spam", {}) or {}
        if sp.get("enabled") and not _blocked("spam"):
            now = now_utc().timestamp()
            key = (guild.id, author.id)
            win = max(1, _int(sp, "window", 5))
            max_msgs = max(2, _int(sp, "max_messages", 5))
            arr = self._am_rate.setdefault(key, [])
            arr.append(now)
            self._am_rate[key] = [t for t in arr if now - t <= win]
            if len(self._am_rate[key]) >= max_msgs:
                _add("spam", f"sent {len(self._am_rate[key])} messages in {win}s")
                self._am_rate[key] = []
            if sp.get("duplicate_enabled", True):
                dwin = max(1, _int(sp, "duplicate_window", 12))
                dcount = max(2, _int(sp, "duplicate_count", 3))
                sig = content.strip().lower()
                if sig:
                    darr = self._am_dup.setdefault(key, [])
                    darr.append((now, sig))
                    self._am_dup[key] = [(t, c) for (t, c) in darr if now - t <= dwin]
                    same = sum(1 for (_t, c) in self._am_dup[key] if c == sig)
                    if same >= dcount:
                        _add("spam", f"repeated the same message {same}×")
                        self._am_dup[key] = []

        if not matches:
            return

        # Apply ONLY the strongest configured violation (no stacked punishments).
        matches.sort(key=lambda m: AM_ACTION_SEVERITY.get(m[1], 0), reverse=True)
        rule_name, action, reason = matches[0]

        await am_punish(self, guild, author, action, reason,
                        rule_name, cfg, message=message)

        # Customizable public violation message.
        try:
            tpl = (cfg.get("messages", {}) or {}).get("violation")
            if tpl:
                rendered = am_render_violation(cfg, author, guild,
                                               rule_name, action, reason)
                if rendered.strip():
                    da = _int(cfg.get("messages", {}) or {}, "delete_after", 8)
                    await channel.send(rendered, delete_after=(da if da > 0 else None))
        except Exception:
            pass

        await am_log(
            self, guild, "AutoMod Action",
            f"**User:** {author.mention} ({author})\n"
            f"**Channel:** {getattr(channel, 'mention', channel)}\n"
            f"**Rule:** `{rule_name}`\n**Action:** `{action}`\n"
            f"**Reason:** {reason}", 0xE67E22, cfg=cfg)

    async def _automod_join_check(self, member: discord.Member):
        """Raid protection (joins per window) + new-account protection."""
        guild = member.guild
        if getattr(member, "bot", False):
            return
        cfg = await am_load_config(self.db, guild.id)
        if not cfg.get("_master"):
            return
        rules = cfg.get("rules", {})
        now = now_utc()
        ts = now.timestamp()

        def _int(d: dict, k: str, default: int) -> int:
            try:
                return int(d.get(k, default))
            except (TypeError, ValueError):
                return default

        # --- Raid protection ---
        rd = rules.get("raid", {}) or {}
        if rd.get("enabled") and not am_rule_whitelisted(cfg, "raid", member, None):
            win = max(1, _int(rd, "window", 12))
            max_j = max(2, _int(rd, "max_joins", 6))
            arr = self._am_joins.setdefault(guild.id, [])
            arr.append(ts)
            self._am_joins[guild.id] = [t for t in arr if ts - t <= win]
            if len(self._am_joins[guild.id]) >= max_j:
                reason = f"raid: {len(self._am_joins[guild.id])} joins in {win}s"
                self._am_joins[guild.id] = []
                await am_punish(self, guild, member, rd.get("action", "kick"),
                                reason, "raid", cfg)
                await am_log(self, guild, "AutoMod · Raid",
                             f"{member.mention} ({member}) — {reason}",
                             0xE74C3C, cfg=cfg)
                return

        # --- New-account / account-age protection ---
        ac = rules.get("accounts", {}) or {}
        if ac.get("enabled") and not am_rule_whitelisted(cfg, "accounts", member, None):
            min_age = max(0, _int(ac, "min_age_days", 7))
            created = getattr(member, "created_at", None)
            if created is not None:
                age_days = (now - created).total_seconds() / 86400.0
                if age_days < min_age:
                    reason = f"account younger than {min_age}d (age {age_days:.1f}d)"
                    await am_punish(self, guild, member, ac.get("action", "kick"),
                                    reason, "accounts", cfg)
                    await am_log(self, guild, "AutoMod · New Account",
                                 f"{member.mention} ({member}) — {reason}",
                                 0xE74C3C, cfg=cfg)

    async def _automod_leave_check(self, member: discord.Member):
        """Detect kicks via audit log for anti-nuke."""
        await self._am_nuke_check(
            member.guild, discord.AuditLogAction.member_kick, "Kick",
            f"kicked {member.mention} ({member})")

    async def _am_nuke_check(self, guild: discord.Guild, audit_action,
                             event_label: str, target_desc: str):
        """Identify a destructive actor from the audit log and act conservatively.

        Never punishes the bot itself, the owner, administrators, or bots.
        """
        cfg = await am_load_config(self.db, guild.id)
        if not cfg.get("_master"):
            return
        nk = (cfg.get("rules", {}) or {}).get("antinuke", {}) or {}
        if not nk.get("enabled"):
            return
        me = guild.me
        if me is None or not me.guild_permissions.view_audit_log:
            return

        def _int(d: dict, k: str, default: int) -> int:
            try:
                return int(d.get(k, default))
            except (TypeError, ValueError):
                return default

        actor = None
        try:
            async for entry in guild.audit_log(action=audit_action, limit=1):
                if entry.user is not None and \
                        (now_utc() - entry.created_at).total_seconds() <= 15:
                    actor = entry.user
                break
        except Exception:
            return
        if actor is None:
            return
        if self.user is not None and actor.id == self.user.id:
            return
        if getattr(actor, "bot", False):
            return
        if isinstance(actor, discord.Member):
            if actor.id == guild.owner_id or actor.guild_permissions.administrator:
                return
        if actor.id == guild.owner_id:
            return

        win = max(1, _int(nk, "window", 30))
        threshold = max(1, _int(nk, "max_deletes", 3))
        now_ts = now_utc().timestamp()
        key = (guild.id, actor.id)
        arr = self._am_nuke.setdefault(key, [])
        arr.append(now_ts)
        self._am_nuke[key] = [t for t in arr if now_ts - t <= win]
        count = len(self._am_nuke[key])

        if count < threshold:
            await am_log(self, guild, f"AutoMod · Anti-Nuke ({event_label})",
                         f"{actor.mention} — {target_desc}", 0xE74C3C, cfg=cfg)
            return

        self._am_nuke[key] = []
        action = nk.get("action", "ban")
        reason = f"anti-nuke: {count} destructive actions ({event_label}) in {win}s"
        target = actor if isinstance(actor, discord.Member) \
            else guild.get_member(actor.id)
        if isinstance(target, discord.Member):
            await am_punish(self, guild, target, action, reason, "antinuke", cfg)
        else:
            await am_record_violation(self.db, guild.id, actor.id, "antinuke",
                                      action, None, reason)
            try:
                if action == "ban" and me.guild_permissions.ban_members:
                    await guild.ban(discord.Object(id=actor.id), reason=reason)
            except Exception:
                pass
        await am_log(self, guild, f"AutoMod · Anti-Nuke ({event_label})",
                     f"**Actor:** {actor.mention}\n**Action:** `{action}`\n"
                     f"{target_desc}", 0xE74C3C, cfg=cfg)

    async def on_guild_channel_delete(self, channel):
        guild = getattr(channel, "guild", None)
        if guild is None:
            return
        try:
            await self._am_nuke_check(
                guild, discord.AuditLogAction.channel_delete, "Channel Delete",
                f"deleted channel `#{getattr(channel, 'name', channel)}`")
        except Exception:
            log.exception("automod anti-nuke (channel delete) failed")

    async def on_guild_role_delete(self, role):
        try:
            await self._am_nuke_check(
                role.guild, discord.AuditLogAction.role_delete, "Role Delete",
                f"deleted role `{role.name}`")
        except Exception:
            log.exception("automod anti-nuke (role delete) failed")

    async def on_member_ban(self, guild: discord.Guild, user):
        try:
            await self._am_nuke_check(
                guild, discord.AuditLogAction.ban, "Ban",
                f"banned {user.mention} ({user})")
        except Exception:
            log.exception("automod anti-nuke (ban) failed")

    async def on_app_command_error(self, interaction: discord.Interaction,
                                   error: app_commands.AppCommandError):
        original = getattr(error, "original", error)
        log.exception("Command error: %s", original)
        msg = "❌ An unexpected error occurred."
        if isinstance(original, app_commands.MissingPermissions):
            msg = "❌ You lack permissions for that."
        elif isinstance(original, app_commands.BotMissingPermissions):
            msg = "❌ I lack permissions for that."
        elif isinstance(original, app_commands.CommandOnCooldown):
            msg = f"⏳ Cooldown — try again in {original.retry_after:.1f}s."
        elif isinstance(original, app_commands.CheckFailure):
            msg = "❌ You can't use that."
        elif isinstance(original, discord.Forbidden):
            msg = "❌ Discord denied that action (permissions/hierarchy)."
        elif isinstance(original, discord.NotFound):
            msg = "❌ Target no longer exists."
        elif isinstance(original, discord.HTTPException):
            msg = f"❌ Discord API error: {original.status}"
        elif isinstance(original, commands.MemberNotFound):
            msg = "❌ Member not found."
        elif isinstance(original, commands.RoleNotFound):
            msg = "❌ Role not found."
        elif isinstance(original, commands.ChannelNotFound):
            msg = "❌ Channel not found."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except Exception:
            pass


# =====================================================================
# Modals
# =====================================================================

class TextModal(discord.ui.Modal):
    def __init__(self, title: str, default: str, on_submit):
        super().__init__(title=title[:45])
        self._on_submit_cb = on_submit
        self.text = discord.ui.TextInput(
            label="Message", style=discord.TextStyle.paragraph,
            default=default[:4000] if default else "",
            max_length=4000, required=False)
        self.add_item(self.text)

    async def on_submit(self, interaction: discord.Interaction):
        await self._on_submit_cb(interaction, self.text.value)


class WelcomeDMModal(discord.ui.Modal, title="Welcome DM Configuration"):
    dm_title = discord.ui.TextInput(
        label="DM Title", style=discord.TextStyle.short,
        max_length=200, required=False,
        placeholder="Optional title for the embed")
    dm_content = discord.ui.TextInput(
        label="DM Content", style=discord.TextStyle.paragraph,
        max_length=4000, required=True,
        placeholder="Message body. Supports {mention}, {user}, {server}, "
                    "{member_count}.")

    def __init__(self, default_title: str = "", default_content: str = ""):
        super().__init__()
        self.dm_title.default = (default_title or "")[:200]
        self.dm_content.default = (default_content or "")[:4000]

    async def on_submit(self, interaction: discord.Interaction):
        db: Database = interaction.client.db
        await db.set_config(interaction.guild.id, "welcome.dm.title",
                            self.dm_title.value or "")
        await db.set_config(interaction.guild.id, "welcome.dm.content",
                            self.dm_content.value or "")
        await db.set_config(interaction.guild.id, "welcome.dm.enabled", "1")
        await interaction.response.send_message(
            "✅ Welcome DM saved and enabled. Test it with `/welcome dm-test`.",
            ephemeral=True)


async def _save_and_reply(interaction: discord.Interaction, key: str, value: str):
    await interaction.client.db.set_config(interaction.guild.id, key, value)
    await interaction.response.send_message("✅ Saved.", ephemeral=True)


# =====================================================================
# Command registration
# =====================================================================

def register_all_commands(bot: Freakos):
    tree = bot.tree
    db = bot.db

    async def require_admin(interaction: discord.Interaction) -> bool:
        if not interaction.guild:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return False
        if not is_admin_or_mod(interaction.user):
            await interaction.response.send_message(
                "You need Manage Server / Administrator.", ephemeral=True)
            return False
        return True

    @tree.error
    async def _on_command_error(interaction: discord.Interaction,
                                error: app_commands.AppCommandError):
        # discord.py 2.x does NOT dispatch on_app_command_error automatically —
        # the tree error hook is the only place app-command errors surface, so
        # route it to the rich client handler (which also answers the
        # interaction to avoid "The application did not respond").
        await bot.on_app_command_error(interaction, error)

    # ---------------- GENERAL ----------------
    @tree.command(name="ping", description="Show bot latency.")
    async def ping(interaction: discord.Interaction):
        await interaction.response.send_message(
            f"🏓 Pong! `{round(bot.latency*1000)}ms`")

    @tree.command(name="sync", description="Force re-sync of slash commands.")
    @app_commands.default_permissions(administrator=True)
    async def sync_cmd(interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message(
                "Admin only.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        try:
            bot.tree.clear_commands(guild=None)
            await bot.tree.sync()
            synced = await bot.tree.sync()
            await interaction.followup.send(
                f"✅ Synced {len(synced)} commands globally.\n"
                "Restart Discord (Ctrl+R) if they don't appear.",
                ephemeral=True)
        except Exception as e:
            log.exception("Manual sync failed")
            await interaction.followup.send(f"❌ Sync failed: {e}", ephemeral=True)

    @tree.command(name="serverinfo", description="Show server info.")
    async def serverinfo(interaction: discord.Interaction):
        g = interaction.guild
        e = make_embed(title=g.name, description=g.description or None)
        if g.icon:
            e.set_thumbnail(url=g.icon.url)
        e.add_field(name="Owner", value=f"<@{g.owner_id}>", inline=True)
        e.add_field(name="Members", value=str(g.member_count), inline=True)
        e.add_field(name="Created", value=fmt_dt(g.created_at), inline=True)
        e.add_field(name="Roles", value=str(len(g.roles)), inline=True)
        e.add_field(name="Channels", value=str(len(g.channels)), inline=True)
        e.add_field(name="Boost Tier", value=str(g.premium_tier), inline=True)
        await interaction.response.send_message(embed=e)

    @tree.command(name="userinfo", description="Show info about a user.")
    async def userinfo(interaction: discord.Interaction,
                       member: Optional[discord.Member] = None):
        m = member or interaction.user
        e = make_embed(title=str(m))
        e.set_thumbnail(url=m.display_avatar.url)
        e.add_field(name="ID", value=str(m.id), inline=True)
        e.add_field(name="Bot?", value="Yes" if m.bot else "No", inline=True)
        e.add_field(name="Created", value=fmt_dt(m.created_at), inline=True)
        if isinstance(m, discord.Member):
            e.add_field(name="Joined", value=fmt_dt(m.joined_at), inline=True)
            roles = [r.mention for r in reversed(m.roles) if r.name != "@everyone"]
            e.add_field(name="Top Role", value=m.top_role.mention, inline=True)
            if roles:
                e.add_field(name="Roles", value=", ".join(roles[:20]), inline=False)
        await interaction.response.send_message(embed=e)

    @tree.command(name="avatar", description="Show a user's avatar.")
    async def avatar(interaction: discord.Interaction,
                     member: Optional[discord.Member] = None):
        m = member or interaction.user
        e = make_embed(title=f"{m.name}'s avatar")
        e.set_image(url=m.display_avatar.url)
        await interaction.response.send_message(embed=e)

    @tree.command(name="help", description="List all bot commands.")
    async def help_cmd(interaction: discord.Interaction):
        cats = {
            "General": ["ping", "help", "serverinfo", "userinfo", "avatar", "setup", "sync"],
            "Welcome": ["welcome"],
            "Autorole": ["autorole"],
            "Autonick": ["autonick"],
            "Departure": ["departure"],
            "Action DMs": ["actiondm"],
            "VC Notifications": ["vcnotify"],
            "Tickets": ["ticket"],
            "Vouches": ["vouch"],
            "Shop": ["shop"],
            "Orders": ["order"],
            "Notifications": ["notify"],
            "AutoMod": ["automod"],
            "Moderation": ["warn", "warnings", "clearwarnings", "timeout",
                           "untimeout", "kick", "ban", "unban", "purge",
                           "lock", "unlock"],
            "Logging": ["logging"],
            "Reaction Roles": ["reactionrole"],
            "Giveaways": ["giveaway"],
            "Custom Commands": ["customcommand"],
            "Announcements": ["announce"],
        }
        e = make_embed(title="FREAKOS — Commands",
                       description="Use `/` for slash commands, "
                                   "`!name` for custom commands.")
        for cat, names in cats.items():
            e.add_field(name=cat, value=", ".join(f"`/{n}`" for n in names),
                        inline=False)
        await interaction.response.send_message(embed=e, ephemeral=True)

    @tree.command(name="setup", description="Interactive setup wizard.")
    async def setup(interaction: discord.Interaction):
        if not await require_admin(interaction):
            return
        g = interaction.guild
        lines = []
        checks = [
            ("Welcome", await db.get_config(g.id, "welcome.enabled", "0") == "1"),
            ("Welcome DM", await db.get_config(g.id, "welcome.dm.enabled", "0") == "1"),
            ("Autorole", await db.get_config(g.id, "autorole.enabled", "0") == "1"),
            ("Autonick", await db.get_config(g.id, "autonick.enabled", "0") == "1"),
            ("Departure", await db.get_config(g.id, "departure.enabled", "0") == "1"),
            ("Action DMs", await db.get_config(g.id, "actiondm.enabled", "0") == "1"),
            ("VC Notifications", await db.get_config(g.id, "vcnotify.enabled", "1") != "0"),
            ("Tickets", bool(await db.fetchall(
                "SELECT 1 FROM ticket_types WHERE guild_id=?", (g.id,)))),
            ("Vouches", bool(await db.get_config(g.id, "vouch.channel"))),
            ("Shop", bool(await db.fetchall(
                "SELECT 1 FROM shop_products WHERE guild_id=?", (g.id,)))),
            ("Notifications", bool(await db.get_config(g.id, "notify.channel"))),
            ("AutoMod", await db.get_config(g.id, "automod.enabled", "0") == "1"),
            ("Logging", bool(await db.get_config(g.id, "logging.channel"))),
            ("Reaction Roles", bool(await db.fetchall(
                "SELECT 1 FROM reaction_panels WHERE guild_id=?", (g.id,)))),
            ("Giveaways", bool(await db.fetchall(
                "SELECT 1 FROM giveaways WHERE guild_id=? AND ended=0", (g.id,)))),
            ("Custom Commands", bool(await db.fetchall(
                "SELECT 1 FROM custom_commands WHERE guild_id=?", (g.id,)))),
            ("Announcements", bool(await db.fetchall(
                "SELECT 1 FROM announcements WHERE guild_id=?", (g.id,)))),
        ]
        for name, on in checks:
            lines.append(f"{'🟢' if on else '⚪'} **{name}**")
        embed = make_embed(
            title="🛠 FREAKOS Setup",
            description=("Use the subcommands of each group to configure systems:\n"
                         "`/welcome`, `/autorole`, `/autonick`, `/departure`, "
                         "`/actiondm`, `/vcnotify`, `/ticket`, `/vouch`, `/shop`, "
                         "`/order`, `/notify`, `/automod`, `/logging`, "
                         "`/reactionrole`, `/giveaway`, `/customcommand`, `/announce`.\n\n"
                         "**Current status:**\n" + "\n".join(lines)))
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ---------------- WELCOME ----------------
    welcome = app_commands.Group(name="welcome", description="Welcome system")
    tree.add_command(welcome)

    @welcome.command(name="setup", description="Quick setup of welcome channel.")
    @app_commands.describe(channel="Channel for welcome messages")
    async def w_setup(interaction: discord.Interaction, channel: discord.TextChannel):
        if not await require_admin(interaction):
            return
        await db.set_config(interaction.guild.id, "welcome.channel", channel.id)
        await db.set_config(interaction.guild.id, "welcome.enabled", "1")
        await interaction.response.send_message(
            f"✅ Welcome configured to {channel.mention}.", ephemeral=True)

    @welcome.command(name="enable", description="Enable welcome messages.")
    async def w_enable(interaction: discord.Interaction):
        if not await require_admin(interaction): return
        await db.set_config(interaction.guild.id, "welcome.enabled", "1")
        await interaction.response.send_message("✅ Enabled.", ephemeral=True)

    @welcome.command(name="disable", description="Disable welcome messages.")
    async def w_disable(interaction: discord.Interaction):
        if not await require_admin(interaction): return
        await db.set_config(interaction.guild.id, "welcome.enabled", "0")
        await interaction.response.send_message("✅ Disabled.", ephemeral=True)

    @welcome.command(name="channel", description="Set welcome channel.")
    async def w_channel(interaction: discord.Interaction, channel: discord.TextChannel):
        if not await require_admin(interaction): return
        await db.set_config(interaction.guild.id, "welcome.channel", channel.id)
        await interaction.response.send_message(
            f"✅ Channel set to {channel.mention}.", ephemeral=True)

    @welcome.command(name="avatar",
                     description="Show joining member's avatar as thumbnail.")
    async def w_avatar(interaction: discord.Interaction, enabled: bool):
        if not await require_admin(interaction): return
        await db.set_config(interaction.guild.id, "welcome.show_avatar",
                            "1" if enabled else "0")
        await interaction.response.send_message(
            f"✅ Avatar: **{'on' if enabled else 'off'}**.", ephemeral=True)

    @welcome.command(name="server-logo",
                     description="Show server icon as banner on welcome embed.")
    async def w_server_logo(interaction: discord.Interaction, enabled: bool):
        if not await require_admin(interaction): return
        await db.set_config(interaction.guild.id, "welcome.show_server_logo",
                            "1" if enabled else "0")
        await interaction.response.send_message(
            f"✅ Server logo: **{'on' if enabled else 'off'}**.", ephemeral=True)

    @welcome.command(name="message",
                     description="Set the welcome embed description (supports newlines).")
    async def w_message(interaction: discord.Interaction):
        if not await require_admin(interaction): return
        current = await db.get_config(interaction.guild.id, "welcome.message", "") or ""
        await interaction.response.send_modal(TextModal(
            title="Welcome Message", default=current,
            on_submit=lambda i, v: _save_and_reply(i, "welcome.message", v)))

    @welcome.command(name="test", description="Send a test welcome message/embed.")
    async def w_test(interaction: discord.Interaction):
        if not await require_admin(interaction): return
        ch_id = await db.get_config(interaction.guild.id, "welcome.channel")
        ch = interaction.guild.get_channel(int(ch_id)) if ch_id else None
        if not ch:
            return await interaction.response.send_message(
                "No welcome channel set.", ephemeral=True)
        tpl = await db.get_config(interaction.guild.id, "welcome.message") or \
            "Welcome {mention}!"
        ph = dict(
            user=interaction.user.name, mention=interaction.user.mention,
            username=interaction.user.name,
            display_name=interaction.user.display_name,
            server=interaction.guild.name,
            member_count=str(interaction.guild.member_count),
            joined_at=fmt_dt_human(interaction.user.joined_at or now_utc()))
        rendered = apply_placeholders(tpl, **ph)
        embed_cfg = await db.get_json(
            interaction.guild.id, "welcome.embed", default={}) or {}
        use_embed = embed_cfg.get("enabled", True)
        show_avatar = (await db.get_config(
            interaction.guild.id, "welcome.show_avatar", "1")) == "1"
        show_logo = (await db.get_config(
            interaction.guild.id, "welcome.show_server_logo", "1")) == "1"
        if use_embed:
            e = discord.Embed(
                description=(rendered or "")[:4096],
                color=int(embed_cfg.get("color") or 0x5865F2),
                timestamp=now_utc())
            if embed_cfg.get("title"):
                e.title = apply_placeholders(str(embed_cfg["title"]), **ph)[:256]
            if embed_cfg.get("footer"):
                e.set_footer(text=apply_placeholders(
                    str(embed_cfg["footer"]), **ph)[:2048])
            if show_avatar and interaction.user.display_avatar:
                e.set_thumbnail(url=interaction.user.display_avatar.url)
            elif embed_cfg.get("thumbnail"):
                e.set_thumbnail(url=str(embed_cfg["thumbnail"]))
            if show_logo and interaction.guild.icon:
                e.set_image(url=interaction.guild.icon.url)
            elif embed_cfg.get("image"):
                e.set_image(url=str(embed_cfg["image"]))
            await ch.send(embed=e)
        else:
            for c in chunk_message(rendered):
                await ch.send(c)
        await interaction.response.send_message("✅ Test sent.", ephemeral=True)

    @welcome.command(name="dm-setup",
                     description="Open a popup to configure the DM sent to new members.")
    async def w_dm_setup(interaction: discord.Interaction):
        if not await require_admin(interaction): return
        cur_title = await db.get_config(interaction.guild.id, "welcome.dm.title", "") or ""
        cur_content = await db.get_config(interaction.guild.id, "welcome.dm.content", "") or ""
        await interaction.response.send_modal(
            WelcomeDMModal(cur_title, cur_content))

    @welcome.command(name="dm-enable", description="Enable the welcome DM.")
    async def w_dm_enable(interaction: discord.Interaction):
        if not await require_admin(interaction): return
        await db.set_config(interaction.guild.id, "welcome.dm.enabled", "1")
        await interaction.response.send_message("✅ Welcome DM enabled.", ephemeral=True)

    @welcome.command(name="dm-disable", description="Disable the welcome DM.")
    async def w_dm_disable(interaction: discord.Interaction):
        if not await require_admin(interaction): return
        await db.set_config(interaction.guild.id, "welcome.dm.enabled", "0")
        await interaction.response.send_message("✅ Welcome DM disabled.", ephemeral=True)

    @welcome.command(name="dm-test",
                     description="Send a test welcome DM to yourself.")
    async def w_dm_test(interaction: discord.Interaction):
        if not await require_admin(interaction): return
        title = await db.get_config(interaction.guild.id, "welcome.dm.title", "") or ""
        content = await db.get_config(interaction.guild.id, "welcome.dm.content", "") or ""
        if not content.strip():
            return await interaction.response.send_message(
                "No DM content set. Use `/welcome dm-setup` first.", ephemeral=True)
        ph = dict(
            user=interaction.user.name, mention=interaction.user.mention,
            username=interaction.user.name,
            display_name=interaction.user.display_name,
            server=interaction.guild.name,
            member_count=str(interaction.guild.member_count))
        try:
            embed = discord.Embed(
                color=0x5865F2, timestamp=now_utc(),
                description=apply_placeholders(content, **ph)[:4096])
            if title.strip():
                embed.title = apply_placeholders(title, **ph)[:256]
            if interaction.guild.icon:
                embed.set_thumbnail(url=interaction.guild.icon.url)
            await interaction.user.send(embed=embed)
            await interaction.response.send_message("✅ Sent (check your DMs).", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message("❌ Couldn't DM you.", ephemeral=True)

    @welcome.command(name="reset", description="Reset welcome config.")
    async def w_reset(interaction: discord.Interaction):
        if not await require_admin(interaction): return
        for k in ("welcome.channel", "welcome.message", "welcome.embed",
                  "welcome.enabled", "welcome.show_avatar",
                  "welcome.show_server_logo", "welcome.dm.title",
                  "welcome.dm.content", "welcome.dm.enabled"):
            await db.set_config(interaction.guild.id, k, None)
        await interaction.response.send_message("✅ Welcome reset.", ephemeral=True)

    # ---------------- AUTOROLE ----------------
    autorole = app_commands.Group(name="autorole", description="Autorole system")
    tree.add_command(autorole)

    @autorole.command(name="setup", description="Quick enable autorole.")
    async def ar_setup(interaction: discord.Interaction, role: discord.Role):
        if not await require_admin(interaction): return
        await db.set_json(interaction.guild.id, "autorole.roles", [role.id])
        await db.set_config(interaction.guild.id, "autorole.enabled", "1")
        await interaction.response.send_message(
            f"✅ Autorole set to {role.mention}.", ephemeral=True)

    @autorole.command(name="add", description="Add a role to autorole.")
    async def ar_add(interaction: discord.Interaction, role: discord.Role):
        if not await require_admin(interaction): return
        if role >= interaction.guild.me.top_role or role.managed:
            return await interaction.response.send_message(
                "❌ Role is higher than mine or managed.", ephemeral=True)
        roles = await db.get_json(interaction.guild.id, "autorole.roles", default=[])
        if role.id not in roles:
            roles.append(role.id)
            await db.set_json(interaction.guild.id, "autorole.roles", roles)
        await interaction.response.send_message(f"✅ Added {role.mention}.", ephemeral=True)

    @autorole.command(name="remove", description="Remove a role from autorole.")
    async def ar_remove(interaction: discord.Interaction, role: discord.Role):
        if not await require_admin(interaction): return
        roles = await db.get_json(interaction.guild.id, "autorole.roles", default=[])
        roles = [r for r in roles if r != role.id]
        await db.set_json(interaction.guild.id, "autorole.roles", roles)
        await interaction.response.send_message(f"✅ Removed {role.mention}.", ephemeral=True)

    @autorole.command(name="list", description="List autorole roles.")
    async def ar_list(interaction: discord.Interaction):
        roles = await db.get_json(interaction.guild.id, "autorole.roles", default=[])
        text = "\n".join(f"• <@&{r}>" for r in roles) or "*(none)*"
        await interaction.response.send_message(f"**Autoroles:**\n{text}", ephemeral=True)

    @autorole.command(name="enable", description="Enable autorole.")
    async def ar_enable(interaction: discord.Interaction):
        if not await require_admin(interaction): return
        await db.set_config(interaction.guild.id, "autorole.enabled", "1")
        await interaction.response.send_message("✅ Enabled.", ephemeral=True)

    @autorole.command(name="disable", description="Disable autorole.")
    async def ar_disable(interaction: discord.Interaction):
        if not await require_admin(interaction): return
        await db.set_config(interaction.guild.id, "autorole.enabled", "0")
        await interaction.response.send_message("✅ Disabled.", ephemeral=True)

    @autorole.command(name="reset", description="Reset autorole.")
    async def ar_reset(interaction: discord.Interaction):
        if not await require_admin(interaction): return
        for k in ("autorole.roles", "autorole.enabled"):
            await db.set_config(interaction.guild.id, k, None)
        await interaction.response.send_message("✅ Reset.", ephemeral=True)

    # ---------------- AUTONICK ----------------
    autonick = app_commands.Group(name="autonick", description="Autonick system")
    tree.add_command(autonick)

    @autonick.command(name="setup", description="Set format and enable autonick.")
    async def an_setup(interaction: discord.Interaction, format: str):
        if not await require_admin(interaction): return
        await db.set_config(interaction.guild.id, "autonick.format", format)
        await db.set_config(interaction.guild.id, "autonick.enabled", "1")
        await interaction.response.send_message("✅ Autonick enabled.", ephemeral=True)

    @autonick.command(name="format", description="Change autonick format.")
    async def an_format(interaction: discord.Interaction, format: str):
        if not await require_admin(interaction): return
        await db.set_config(interaction.guild.id, "autonick.format", format)
        await interaction.response.send_message("✅ Format updated.", ephemeral=True)

    @autonick.command(name="enable", description="Enable autonick.")
    async def an_enable(interaction: discord.Interaction):
        if not await require_admin(interaction): return
        await db.set_config(interaction.guild.id, "autonick.enabled", "1")
        await interaction.response.send_message("✅ Enabled.", ephemeral=True)

    @autonick.command(name="disable", description="Disable autonick.")
    async def an_disable(interaction: discord.Interaction):
        if not await require_admin(interaction): return
        await db.set_config(interaction.guild.id, "autonick.enabled", "0")
        await interaction.response.send_message("✅ Disabled.", ephemeral=True)

    @autonick.command(name="reset", description="Reset autonick.")
    async def an_reset(interaction: discord.Interaction):
        if not await require_admin(interaction): return
        for k in ("autonick.format", "autonick.enabled"):
            await db.set_config(interaction.guild.id, k, None)
        await interaction.response.send_message("✅ Reset.", ephemeral=True)

    # ---------------- DEPARTURE ----------------
    departure = app_commands.Group(name="departure", description="Departure messages")
    tree.add_command(departure)

    @departure.command(name="setup", description="Configure departure channel and enable.")
    async def d_setup(interaction: discord.Interaction, channel: discord.TextChannel):
        if not await require_admin(interaction): return
        await db.set_config(interaction.guild.id, "departure.channel", channel.id)
        await db.set_config(interaction.guild.id, "departure.enabled", "1")
        await interaction.response.send_message(
            f"✅ Departure set to {channel.mention}.", ephemeral=True)

    @departure.command(name="enable", description="Enable departure.")
    async def d_enable(interaction: discord.Interaction):
        if not await require_admin(interaction): return
        await db.set_config(interaction.guild.id, "departure.enabled", "1")
        await interaction.response.send_message("✅ Enabled.", ephemeral=True)

    @departure.command(name="disable", description="Disable departure.")
    async def d_disable(interaction: discord.Interaction):
        if not await require_admin(interaction): return
        await db.set_config(interaction.guild.id, "departure.enabled", "0")
        await interaction.response.send_message("✅ Disabled.", ephemeral=True)

    @departure.command(name="channel", description="Set departure channel.")
    async def d_channel(interaction: discord.Interaction, channel: discord.TextChannel):
        if not await require_admin(interaction): return
        await db.set_config(interaction.guild.id, "departure.channel", channel.id)
        await interaction.response.send_message("✅ Set.", ephemeral=True)

    @departure.command(name="avatar", description="Show leaving member's avatar.")
    async def d_avatar(interaction: discord.Interaction, enabled: bool):
        if not await require_admin(interaction): return
        await db.set_config(interaction.guild.id, "departure.show_avatar",
                            "1" if enabled else "0")
        await interaction.response.send_message(
            f"✅ Avatar: **{'on' if enabled else 'off'}**.", ephemeral=True)

    @departure.command(name="message", description="Set departure message (supports newlines).")
    async def d_message(interaction: discord.Interaction):
        if not await require_admin(interaction): return
        current = await db.get_config(interaction.guild.id, "departure.message", "") or ""
        await interaction.response.send_modal(TextModal(
            title="Departure Message", default=current,
            on_submit=lambda i, v: _save_and_reply(i, "departure.message", v)))

    @departure.command(name="test", description="Send a test departure message.")
    async def d_test(interaction: discord.Interaction):
        if not await require_admin(interaction): return
        ch_id = await db.get_config(interaction.guild.id, "departure.channel")
        ch = interaction.guild.get_channel(int(ch_id)) if ch_id else None
        if not ch:
            return await interaction.response.send_message(
                "No departure channel set.", ephemeral=True)
        tpl = await db.get_config(interaction.guild.id, "departure.message") or \
            "**{user}** left."
        ph = dict(
            user=interaction.user.name, mention=interaction.user.mention,
            username=interaction.user.name,
            display_name=interaction.user.display_name,
            server=interaction.guild.name,
            member_count=str(interaction.guild.member_count),
            left_at=fmt_dt_human(now_utc()))
        rendered = apply_placeholders(tpl, **ph)
        show_avatar = (await db.get_config(
            interaction.guild.id, "departure.show_avatar", "1")) == "1"
        e = discord.Embed(description=(rendered or "")[:4096],
                          color=0xED4245, timestamp=now_utc())
        if show_avatar and interaction.user.display_avatar:
            e.set_thumbnail(url=interaction.user.display_avatar.url)
        await ch.send(embed=e)
        await interaction.response.send_message("✅ Test sent.", ephemeral=True)

    @departure.command(name="reset", description="Reset departure.")
    async def d_reset(interaction: discord.Interaction):
        if not await require_admin(interaction): return
        for k in ("departure.channel", "departure.message",
                  "departure.embed", "departure.enabled",
                  "departure.show_avatar"):
            await db.set_config(interaction.guild.id, k, None)
        await interaction.response.send_message("✅ Reset.", ephemeral=True)

    # ---------------- ACTION DMS ----------------
    actiondm = app_commands.Group(name="actiondm", description="Action DM messages")
    tree.add_command(actiondm)

    @actiondm.command(name="setup", description="Enable action DMs.")
    async def a_setup(interaction: discord.Interaction):
        if not await require_admin(interaction): return
        await db.set_config(interaction.guild.id, "actiondm.enabled", "1")
        await interaction.response.send_message("✅ Action DMs enabled.", ephemeral=True)

    @actiondm.command(name="enable", description="Enable action DMs.")
    async def a_enable(interaction: discord.Interaction):
        if not await require_admin(interaction): return
        await db.set_config(interaction.guild.id, "actiondm.enabled", "1")
        await interaction.response.send_message("✅ Enabled.", ephemeral=True)

    @actiondm.command(name="disable", description="Disable action DMs.")
    async def a_disable(interaction: discord.Interaction):
        if not await require_admin(interaction): return
        await db.set_config(interaction.guild.id, "actiondm.enabled", "0")
        await interaction.response.send_message("✅ Disabled.", ephemeral=True)

    async def _edit_actiondm(interaction, key, title):
        if not await require_admin(interaction): return
        current = await db.get_config(interaction.guild.id, key, "") or ""
        await interaction.response.send_modal(TextModal(
            title=title, default=current,
            on_submit=lambda i, v: _save_and_reply(i, key, v)))

    @actiondm.command(name="timeout", description="Edit timeout DM.")
    async def a_timeout(interaction: discord.Interaction):
        await _edit_actiondm(interaction, "actiondm.timeout_msg", "Timeout DM")

    @actiondm.command(name="timeout-end", description="Edit timeout-end DM.")
    async def a_timeout_end(interaction: discord.Interaction):
        await _edit_actiondm(interaction, "actiondm.timeout_end_msg", "Timeout-End DM")

    @actiondm.command(name="kick", description="Edit kick DM.")
    async def a_kick(interaction: discord.Interaction):
        await _edit_actiondm(interaction, "actiondm.kick_msg", "Kick DM")

    @actiondm.command(name="ban", description="Edit ban DM.")
    async def a_ban(interaction: discord.Interaction):
        await _edit_actiondm(interaction, "actiondm.ban_msg", "Ban DM")

    @actiondm.command(name="test", description="Send a test DM to yourself.")
    async def a_test(interaction: discord.Interaction):
        if not await require_admin(interaction): return
        msg = await db.get_config(interaction.guild.id, "actiondm.timeout_msg") or \
            "You were timed out in {server} for {duration}. Reason: {reason}"
        rendered = apply_placeholders(
            msg, user=interaction.user.name, mention=interaction.user.mention,
            username=interaction.user.name,
            display_name=interaction.user.display_name,
            moderator=interaction.user.mention, reason="Test", duration="10m",
            timeout_end=fmt_dt(now_utc() + timedelta(minutes=10)),
            case_id="TEST", server=interaction.guild.name,
            timestamp=fmt_dt(now_utc()))
        try:
            for c in chunk_message(rendered):
                await interaction.user.send(c)
            await interaction.response.send_message("✅ Sent via DM.", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message("❌ Couldn't DM you.", ephemeral=True)

    @actiondm.command(name="reset", description="Reset action DMs.")
    async def a_reset(interaction: discord.Interaction):
        if not await require_admin(interaction): return
        for k in ("actiondm.enabled", "actiondm.timeout_msg",
                  "actiondm.timeout_end_msg", "actiondm.kick_msg",
                  "actiondm.ban_msg"):
            await db.set_config(interaction.guild.id, k, None)
        await interaction.response.send_message("✅ Reset.", ephemeral=True)

    # ---------------- VC NOTIFICATIONS ----------------
    vcnotify = app_commands.Group(
        name="vcnotify", description="Voice-channel join/leave notifications")
    tree.add_command(vcnotify)

    async def _vc_get_cfg(guild_id: int, vc_id: int) -> dict:
        return await db.get_json(guild_id, f"vcnotify.cfg.{vc_id}", default={}) or {}

    async def _vc_set_cfg(guild_id: int, vc_id: int, cfg: dict):
        await db.set_json(guild_id, f"vcnotify.cfg.{vc_id}", cfg)

    async def _vc_watched(guild_id: int) -> list:
        return await db.get_json(guild_id, "vcnotify.channels", default=[]) or []

    async def _vc_set_watched(guild_id: int, watched: list):
        await db.set_json(guild_id, "vcnotify.channels", watched)

    async def _vc_reply_error(interaction: discord.Interaction, msg: str):
        """Send an error reply whether or not the interaction was already deferred."""
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except Exception:
            log.exception("Failed to send vcnotify error reply")

    @vcnotify.command(
        name="setup",
        description="Enable VC notifications (posted in each voice channel's own text chat).")
    async def v_setup(interaction: discord.Interaction):
        # Defer FIRST so Discord always gets an acknowledgement within 3s.
        await interaction.response.defer(ephemeral=True)
        try:
            if not interaction.guild:
                return await interaction.followup.send("Guild only.", ephemeral=True)
            if not is_admin_or_mod(interaction.user):
                return await interaction.followup.send(
                    "You need Manage Server / Administrator.", ephemeral=True)

            await db.set_config(interaction.guild.id, "vcnotify.enabled", "1")
            await interaction.followup.send(
                "✅ VC notifications enabled.\n"
                "Join/leave messages are posted **inside each voice channel's own "
                "text chat** — no separate text channel needed.\n"
                "This applies to **every** voice channel automatically "
                "(General 1, General 2, Gaming, Music, Staff VC, …).\n"
                "Customise a specific VC with `/vcnotify join-message` and "
                "`/vcnotify leave-message`.",
                ephemeral=True)
        except Exception as e:
            log.exception("vcnotify setup failed")
            await _vc_reply_error(interaction, f"❌ Setup failed: {e}")

    @vcnotify.command(
        name="add",
        description="Enable notifications for one voice channel (posts in its own text chat).")
    @app_commands.describe(voice_channel="Voice channel to enable")
    async def v_add(interaction: discord.Interaction,
                    voice_channel: discord.VoiceChannel):
        # Defer FIRST so Discord always gets an acknowledgement within 3s.
        await interaction.response.defer(ephemeral=True)
        try:
            if not interaction.guild:
                return await interaction.followup.send("Guild only.", ephemeral=True)
            if not is_admin_or_mod(interaction.user):
                return await interaction.followup.send(
                    "You need Manage Server / Administrator.", ephemeral=True)

            cfg = await _vc_get_cfg(interaction.guild.id, voice_channel.id)
            cfg["enabled"] = True
            await _vc_set_cfg(interaction.guild.id, voice_channel.id, cfg)
            await db.set_config(interaction.guild.id, "vcnotify.enabled", "1")
            await interaction.followup.send(
                f"✅ Notifications enabled for {voice_channel.mention}. "
                "Messages will post in its own text chat.",
                ephemeral=True)
        except Exception as e:
            log.exception("vcnotify add failed")
            await _vc_reply_error(interaction, f"❌ Failed: {e}")

    @vcnotify.command(
        name="remove",
        description="Disable notifications for one voice channel.")
    @app_commands.describe(voice_channel="Voice channel to silence")
    async def v_remove(interaction: discord.Interaction, voice_channel: discord.VoiceChannel):
        await interaction.response.defer(ephemeral=True)
        try:
            if not interaction.guild:
                return await interaction.followup.send("Guild only.", ephemeral=True)
            if not is_admin_or_mod(interaction.user):
                return await interaction.followup.send(
                    "You need Manage Server / Administrator.", ephemeral=True)

            cfg = await _vc_get_cfg(interaction.guild.id, voice_channel.id)
            cfg["enabled"] = False
            await _vc_set_cfg(interaction.guild.id, voice_channel.id, cfg)
            await interaction.followup.send(
                f"✅ Disabled notifications for {voice_channel.mention}.",
                ephemeral=True)
        except Exception as e:
            log.exception("vcnotify remove failed")
            await _vc_reply_error(interaction, f"❌ Failed: {e}")

    @vcnotify.command(name="enable", description="Enable VC notifications.")
    async def v_enable(interaction: discord.Interaction):
        if not await require_admin(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        await db.set_config(interaction.guild.id, "vcnotify.enabled", "1")
        await interaction.followup.send("✅ Enabled.", ephemeral=True)

    @vcnotify.command(name="disable", description="Disable VC notifications.")
    async def v_disable(interaction: discord.Interaction):
        if not await require_admin(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        await db.set_config(interaction.guild.id, "vcnotify.enabled", "0")
        await interaction.followup.send("✅ Disabled.", ephemeral=True)

    @vcnotify.command(name="list", description="Show VC notification settings.")
    async def v_list(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            if not interaction.guild:
                return await interaction.followup.send("Guild only.", ephemeral=True)
            global_on = await db.get_config(
                interaction.guild.id, "vcnotify.enabled", "1") != "0"
            lines = [
                f"**Global state:** {'🟢 enabled' if global_on else '🔴 disabled'}",
                "Notifications post inside **each voice channel's own text chat** "
                "(all voice channels, automatically).",
            ]
            rows = await db.fetchall(
                "SELECT key, value FROM guild_config WHERE guild_id=? "
                "AND key LIKE 'vcnotify.cfg.%'",
                (interaction.guild.id,))
            overrides = []
            for r in rows:
                cid = str(r["key"]).rsplit(".", 1)[-1]
                try:
                    cfg = json.loads(r["value"]) if r["value"] else {}
                except Exception:
                    cfg = {}
                if not isinstance(cfg, dict):
                    cfg = {}
                state = "on" if cfg.get("enabled", True) else "off"
                jm = "✏️ custom" if cfg.get("join_msg") else "default"
                lm = "✏️ custom" if cfg.get("leave_msg") else "default"
                overrides.append(
                    f"• <#{cid}> | per-channel: {state} | join: {jm} | leave: {lm}")
            if overrides:
                lines.append("**Per-channel overrides:**")
                lines.extend(overrides)
            else:
                lines.append("*(no per-channel overrides — defaults used everywhere)*")
            await interaction.followup.send("\n".join(lines), ephemeral=True)
        except Exception as e:
            log.exception("vcnotify list failed")
            await _vc_reply_error(interaction, f"❌ Failed: {e}")

    async def _edit_vc_msg(interaction: discord.Interaction,
                           vc: discord.VoiceChannel, key: str, title: str):
        if not await require_admin(interaction):
            return
        cfg = await _vc_get_cfg(interaction.guild.id, vc.id)
        current = cfg.get(key) or ""

        async def save(i: discord.Interaction, v: str):
            cfg2 = await _vc_get_cfg(i.guild.id, vc.id)
            cfg2[key] = v
            await _vc_set_cfg(i.guild.id, vc.id, cfg2)
            await i.response.send_message("✅ Saved.", ephemeral=True)

        await interaction.response.send_modal(
            TextModal(title=title, default=current, on_submit=save))

    @vcnotify.command(name="join-message",
                      description="Set the template posted when someone joins.")
    @app_commands.describe(voice_channel="Voice channel whose template to edit")
    async def v_join(interaction: discord.Interaction, voice_channel: discord.VoiceChannel):
        await _edit_vc_msg(interaction, voice_channel, "join_msg", "VC Join Message")

    @vcnotify.command(name="leave-message",
                      description="Set the template posted when someone leaves.")
    @app_commands.describe(voice_channel="Voice channel whose template to edit")
    async def v_leave(interaction: discord.Interaction, voice_channel: discord.VoiceChannel):
        await _edit_vc_msg(interaction, voice_channel, "leave_msg", "VC Leave Message")

    @vcnotify.command(
        name="test",
        description="Post a test join + leave message in a VC's own text chat.")
    @app_commands.describe(voice_channel="Voice channel to test")
    async def v_test(interaction: discord.Interaction, voice_channel: discord.VoiceChannel):
        await interaction.response.defer(ephemeral=True)
        try:
            if not interaction.guild:
                return await interaction.followup.send("Guild only.", ephemeral=True)
            if not is_admin_or_mod(interaction.user):
                return await interaction.followup.send(
                    "You need Manage Server / Administrator.", ephemeral=True)

            cfg = await _vc_get_cfg(interaction.guild.id, voice_channel.id)
            join_tpl = cfg.get("join_msg") or "🎧 {mention} joined **{channel_name}**."
            leave_tpl = cfg.get("leave_msg") or "👋 {mention} left **{channel_name}**."
            common = dict(
                mention=interaction.user.mention, user=interaction.user.name,
                username=interaction.user.name,
                display_name=interaction.user.display_name,
                channel_name=voice_channel.name,
                channel_mention=voice_channel.mention,
                server=interaction.guild.name)

            me = interaction.guild.me
            perms = voice_channel.permissions_for(me) if me else None
            if perms is None or not (perms.view_channel and perms.send_messages):
                return await interaction.followup.send(
                    "❌ I can't post in that voice channel's text chat. "
                    "Give me **View Channel** and **Send Messages** for it.",
                    ephemeral=True)

            for c in chunk_message("[TEST] " + apply_placeholders(join_tpl, **common)):
                await voice_channel.send(c)
            for c in chunk_message("[TEST] " + apply_placeholders(leave_tpl, **common)):
                await voice_channel.send(c)
            await interaction.followup.send(
                f"✅ Test sent in {voice_channel.mention}'s text chat.", ephemeral=True)
        except discord.Forbidden:
            await _vc_reply_error(
                interaction,
                "❌ I can't post in that voice channel's text chat. Check my permissions.")
        except Exception as e:
            log.exception("vcnotify test failed")
            await _vc_reply_error(interaction, f"❌ Failed: {e}")

    @vcnotify.command(name="reset", description="Wipe VC-notification config.")
    async def v_reset(interaction: discord.Interaction):
        if not await require_admin(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        try:
            await db.set_config(interaction.guild.id, "vcnotify.channels", None)
            await db.set_config(interaction.guild.id, "vcnotify.enabled", None)
            rows = await db.fetchall(
                "SELECT key FROM guild_config WHERE guild_id=? "
                "AND key LIKE 'vcnotify.cfg.%'",
                (interaction.guild.id,))
            for r in rows:
                await db.set_config(interaction.guild.id, r["key"], None)
            await interaction.followup.send(
                "✅ VC-notification config reset.", ephemeral=True)
        except Exception as e:
            log.exception("vcnotify reset failed")
            await interaction.followup.send(f"❌ Failed: {e}", ephemeral=True)

    # ---------------- TICKETS ----------------
    ticket = app_commands.Group(name="ticket", description="Ticket system")
    tree.add_command(ticket)

    @ticket.command(name="setup", description="Initial ticket setup.")
    async def t_setup(interaction: discord.Interaction):
        if not await require_admin(interaction): return
        await interaction.response.send_message(
            "Use `/ticket type` to add a ticket type, then `/ticket panel` to place the panel.",
            ephemeral=True)

    @ticket.command(name="panel", description="Post the ticket panel.")
    async def t_panel(interaction: discord.Interaction,
                      channel: Optional[discord.TextChannel] = None):
        if not await require_admin(interaction): return
        ch = channel or interaction.channel
        await interaction.response.send_modal(TicketPanelModal(ch))

    @ticket.command(name="type", description="Add or update a ticket type.")
    async def t_type(interaction: discord.Interaction, name: str,
                     category: discord.CategoryChannel,
                     support_role: discord.Role,
                     emoji: Optional[str] = None):
        if not await require_admin(interaction): return
        await db.execute(
            "INSERT INTO ticket_types (guild_id, name, emoji, category_id, support_role_id) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(guild_id, name) DO UPDATE SET "
            "emoji=excluded.emoji, category_id=excluded.category_id, "
            "support_role_id=excluded.support_role_id",
            (interaction.guild.id, name, emoji, category.id, support_role.id))
        await interaction.response.send_message(
            f"✅ Ticket type `{name}` saved.", ephemeral=True)

    @ticket.command(name="message", description="Set a ticket type's intro message.")
    async def t_message(interaction: discord.Interaction, name: str):
        if not await require_admin(interaction): return
        row = await db.fetchone(
            "SELECT * FROM ticket_types WHERE guild_id=? AND name=?",
            (interaction.guild.id, name))
        if not row:
            return await interaction.response.send_message(
                "Ticket type not found.", ephemeral=True)
        current = row["message"] or ""

        async def save(i: discord.Interaction, v: str):
            await db.execute(
                "UPDATE ticket_types SET message=? WHERE guild_id=? AND name=?",
                (v, i.guild.id, name))
            await i.response.send_message("✅ Saved.", ephemeral=True)

        await interaction.response.send_modal(TextModal(
            title=f"{name} — Intro Message"[:45], default=current, on_submit=save))

    @ticket.command(name="category", description="Update a type's category.")
    async def t_category(interaction: discord.Interaction, name: str,
                         category: discord.CategoryChannel):
        if not await require_admin(interaction): return
        await db.execute(
            "UPDATE ticket_types SET category_id=? WHERE guild_id=? AND name=?",
            (category.id, interaction.guild.id, name))
        await interaction.response.send_message("✅ Updated.", ephemeral=True)

    @ticket.command(name="support-role", description="Update a type's support role.")
    async def t_support(interaction: discord.Interaction, name: str, role: discord.Role):
        if not await require_admin(interaction): return
        await db.execute(
            "UPDATE ticket_types SET support_role_id=? WHERE guild_id=? AND name=?",
            (role.id, interaction.guild.id, name))
        await interaction.response.send_message("✅ Updated.", ephemeral=True)

    @ticket.command(name="limit", description="Max open tickets per user.")
    async def t_limit(interaction: discord.Interaction, limit: int):
        if not await require_admin(interaction): return
        await db.set_config(interaction.guild.id, "ticket.limit", str(limit))
        await interaction.response.send_message("✅ Set.", ephemeral=True)

    @ticket.command(name="cooldown", description="Cooldown between tickets (seconds).")
    async def t_cooldown(interaction: discord.Interaction, seconds: int):
        if not await require_admin(interaction): return
        await db.set_config(interaction.guild.id, "ticket.cooldown", str(seconds))
        await interaction.response.send_message("✅ Set.", ephemeral=True)

    @ticket.command(name="delete-delay",
                    description="Set auto-delete delay after close (seconds). 0 disables.")
    async def t_delete_delay(interaction: discord.Interaction, seconds: int):
        if not await require_admin(interaction): return
        seconds = max(0, seconds)
        await db.set_config(interaction.guild.id, "ticket.close_delete_delay",
                            str(seconds))
        await interaction.response.send_message(
            f"✅ Auto-delete set to {fmt_duration(seconds)}." if seconds
            else "✅ Auto-delete disabled.", ephemeral=True)

    @ticket.command(name="logs", description="Set closed-ticket logs channel.")
    async def t_logs(interaction: discord.Interaction, channel: discord.TextChannel):
        if not await require_admin(interaction): return
        await db.set_config(interaction.guild.id, "ticket.logs_channel", channel.id)
        await interaction.response.send_message(
            f"✅ Ticket logs → {channel.mention}.", ephemeral=True)

    @ticket.command(name="transcript-channel",
                    description="Set the channel for on-demand transcripts.")
    async def t_transcript_channel(interaction: discord.Interaction,
                                   channel: discord.TextChannel):
        if not await require_admin(interaction): return
        await db.set_config(interaction.guild.id, "ticket.transcript_channel",
                            channel.id)
        await interaction.response.send_message(
            f"✅ Transcripts → {channel.mention}.", ephemeral=True)

    @ticket.command(name="supportrole", description="Set global ticket support role.")
    async def t_support_role_global(interaction: discord.Interaction, role: discord.Role):
        if not await require_admin(interaction): return
        await db.set_config(interaction.guild.id, "ticket.support_role_id", role.id)
        await interaction.response.send_message(
            f"✅ Support role set to {role.mention}.", ephemeral=True)

    @ticket.command(name="memberclose", description="Allow members to close their tickets.")
    async def t_member_close(interaction: discord.Interaction, enabled: bool):
        if not await require_admin(interaction): return
        await db.set_config(interaction.guild.id, "ticket.member_can_close",
                            "1" if enabled else "0")
        await interaction.response.send_message("✅ Saved.", ephemeral=True)

    @ticket.command(name="claim", description="Claim current ticket.")
    async def t_claim(interaction: discord.Interaction):
        row = await db.fetchone("SELECT * FROM tickets WHERE channel_id=?",
                                (interaction.channel.id,))
        if not row:
            return await interaction.response.send_message("Not a ticket.", ephemeral=True)
        if not is_admin_or_mod(interaction.user):
            return await interaction.response.send_message("Staff only.", ephemeral=True)
        await db.execute("UPDATE tickets SET claimed_by=? WHERE id=?",
                         (interaction.user.id, row["id"]))
        await interaction.response.send_message(
            f"✅ Claimed by {interaction.user.mention}")

    @ticket.command(name="close", description="Close current ticket.")
    async def t_close(interaction: discord.Interaction, reason: str = "No reason specified"):
        row = await db.fetchone("SELECT * FROM tickets WHERE channel_id=?",
                                (interaction.channel.id,))
        if not row:
            return await interaction.response.send_message("Not a ticket.", ephemeral=True)
        if interaction.user.id != row["user_id"] and not is_admin_or_mod(interaction.user):
            return await interaction.response.send_message("Not allowed.", ephemeral=True)
        await db.execute("UPDATE tickets SET status='closed', close_reason=? WHERE id=?",
                         (reason, row["id"]))
        try:
            ow = interaction.channel.overwrites_for(interaction.guild.default_role)
            ow.send_messages = False
            await interaction.channel.set_permissions(
                interaction.guild.default_role, overwrite=ow)
        except Exception:
            pass
        await _post_ticket_close_log(bot, interaction.guild, interaction.channel,
                                     row, interaction.user, reason)
        delay = await _ticket_delete_delay(db, interaction.guild.id)
        if delay > 0:
            await interaction.response.send_message(
                f"🔒 Ticket closed. Deleting in {fmt_duration(delay)}.")
            await bot.scheduler.schedule(
                "ticket_delete", now_utc() + timedelta(seconds=delay),
                {"ticket_id": row["id"]}, interaction.guild.id)
        else:
            await interaction.response.send_message("🔒 Ticket closed.")

    @ticket.command(name="reopen", description="Reopen current ticket.")
    async def t_reopen(interaction: discord.Interaction):
        if not is_admin_or_mod(interaction.user):
            return await interaction.response.send_message("Staff only.", ephemeral=True)
        row = await db.fetchone("SELECT * FROM tickets WHERE channel_id=?",
                                (interaction.channel.id,))
        if not row:
            return await interaction.response.send_message("Not a ticket.", ephemeral=True)
        await db.execute("UPDATE tickets SET status='open' WHERE id=?", (row["id"],))
        try:
            ow = interaction.channel.overwrites_for(interaction.guild.default_role)
            ow.send_messages = None
            await interaction.channel.set_permissions(
                interaction.guild.default_role, overwrite=ow)
        except Exception:
            pass
        await interaction.response.send_message("🔓 Reopened.")

    @ticket.command(name="delete", description="Delete current ticket channel.")
    async def t_delete(interaction: discord.Interaction):
        if not is_admin_or_mod(interaction.user):
            return await interaction.response.send_message("Staff only.", ephemeral=True)
        row = await db.fetchone("SELECT * FROM tickets WHERE channel_id=?",
                                (interaction.channel.id,))
        if not row:
            return await interaction.response.send_message("Not a ticket.", ephemeral=True)
        await db.execute("DELETE FROM tickets WHERE id=?", (row["id"],))
        await interaction.response.send_message("🗑 Deleting in 3s…")
        await asyncio.sleep(3)
        try:
            await interaction.channel.delete(reason="Ticket deleted")
        except Exception:
            pass

    @ticket.command(name="rename", description="Rename current ticket.")
    async def t_rename(interaction: discord.Interaction, name: str):
        if not is_admin_or_mod(interaction.user):
            return await interaction.response.send_message("Staff only.", ephemeral=True)
        try:
            await interaction.channel.edit(name=name[:90])
        except Exception as e:
            return await interaction.response.send_message(f"Failed: {e}", ephemeral=True)
        await interaction.response.send_message("✅ Renamed.")

    @ticket.command(name="add", description="Add a user to the ticket.")
    async def t_add(interaction: discord.Interaction, member: discord.Member):
        if not is_admin_or_mod(interaction.user):
            return await interaction.response.send_message("Staff only.", ephemeral=True)
        await interaction.channel.set_permissions(
            member, view_channel=True, send_messages=True, read_message_history=True)
        await interaction.response.send_message(f"✅ Added {member.mention}.")

    @ticket.command(name="remove", description="Remove a user from the ticket.")
    async def t_remove(interaction: discord.Interaction, member: discord.Member):
        if not is_admin_or_mod(interaction.user):
            return await interaction.response.send_message("Staff only.", ephemeral=True)
        await interaction.channel.set_permissions(member, overwrite=None)
        await interaction.response.send_message(f"✅ Removed {member.mention}.")

    @ticket.command(name="transcript", description="Generate a transcript.")
    async def t_transcript(interaction: discord.Interaction):
        await interaction.response.send_message(
            "📄 Transcript:", file=await _generate_transcript(interaction.channel))

    @ticket.command(name="reset", description="Reset all ticket config.")
    async def t_reset(interaction: discord.Interaction):
        if not await require_admin(interaction): return
        await db.execute("DELETE FROM ticket_types WHERE guild_id=?",
                         (interaction.guild.id,))
        for k in ("ticket.limit", "ticket.cooldown", "ticket.close_delete_delay",
                  "ticket.logs_channel", "ticket.transcript_channel",
                  "ticket.support_role_id", "ticket.member_can_close",
                  "ticket.room_buttons", "ticket.counter"):
            await db.set_config(interaction.guild.id, k, None)
        await interaction.response.send_message("✅ Reset.", ephemeral=True)

    # ---------------- VOUCHES ----------------
    vouch = app_commands.Group(name="vouch", description="Vouch system")
    tree.add_command(vouch)

    @vouch.command(name="setup", description="Set the vouch channel.")
    async def vo_setup(interaction: discord.Interaction, channel: discord.TextChannel):
        if not await require_admin(interaction): return
        await db.set_config(interaction.guild.id, "vouch.channel", channel.id)
        await interaction.response.send_message("✅ Set.", ephemeral=True)

    @vouch.command(name="add", description="Vouch for a user.")
    async def vo_add(interaction: discord.Interaction,
                     user: discord.Member, comment: str):
        await db.execute(
            "INSERT INTO vouches (guild_id, user_id, voucher_id, comment, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (interaction.guild.id, user.id, interaction.user.id,
             comment, iso(now_utc())))
        ch_id = await db.get_config(interaction.guild.id, "vouch.channel")
        if ch_id:
            ch = interaction.guild.get_channel(int(ch_id))
            if ch:
                e = make_embed(title="⭐ New Vouch", description=comment)
                e.add_field(name="User", value=user.mention, inline=True)
                e.add_field(name="From", value=interaction.user.mention, inline=True)
                try:
                    await ch.send(embed=e)
                except Exception:
                    pass
        await interaction.response.send_message("✅ Vouch recorded.", ephemeral=True)

    @vouch.command(name="list", description="List recent vouches.")
    async def vo_list(interaction: discord.Interaction):
        rows = await db.fetchall(
            "SELECT * FROM vouches WHERE guild_id=? ORDER BY id DESC LIMIT 20",
            (interaction.guild.id,))
        if not rows:
            return await interaction.response.send_message("No vouches yet.", ephemeral=True)
        lines = [f"• <@{r['user_id']}> — {r['comment'][:80]} "
                 f"(by <@{r['voucher_id']}>)" for r in rows]
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @vouch.command(name="user", description="Show a user's vouches.")
    async def vo_user(interaction: discord.Interaction, user: discord.Member):
        rows = await db.fetchall(
            "SELECT * FROM vouches WHERE guild_id=? AND user_id=? "
            "ORDER BY id DESC LIMIT 20", (interaction.guild.id, user.id))
        if not rows:
            return await interaction.response.send_message("No vouches.", ephemeral=True)
        lines = [f"• {r['comment'][:80]} — <@{r['voucher_id']}> "
                 f"({fmt_dt(parse_iso(r['created_at']))})" for r in rows]
        e = make_embed(title=f"Vouches for {user}", description="\n".join(lines))
        await interaction.response.send_message(embed=e, ephemeral=True)

    @vouch.command(name="stats", description="Vouch leaderboard.")
    async def vo_stats(interaction: discord.Interaction):
        rows = await db.fetchall(
            "SELECT user_id, COUNT(*) c FROM vouches WHERE guild_id=? "
            "GROUP BY user_id ORDER BY c DESC LIMIT 10",
            (interaction.guild.id,))
        if not rows:
            return await interaction.response.send_message("No vouches yet.", ephemeral=True)
        lines = [f"`#{i+1}` <@{r['user_id']}> — **{r['c']}**"
                 for i, r in enumerate(rows)]
        e = make_embed(title="⭐ Vouch Leaderboard", description="\n".join(lines))
        await interaction.response.send_message(embed=e, ephemeral=True)

    @vouch.command(name="reset", description="Reset vouches.")
    async def vo_reset(interaction: discord.Interaction):
        if not await require_admin(interaction): return
        await db.execute("DELETE FROM vouches WHERE guild_id=?",
                         (interaction.guild.id,))
        await db.set_config(interaction.guild.id, "vouch.channel", None)
        await interaction.response.send_message("✅ Reset.", ephemeral=True)

    # ---------------- SHOP ----------------
    shop = app_commands.Group(name="shop", description="Shop system")
    tree.add_command(shop)

    @shop.command(name="setup", description="Post the shop panel.")
    async def sh_setup(interaction: discord.Interaction, channel: discord.TextChannel):
        if not await require_admin(interaction): return
        await channel.send(
            embed=make_embed(title="🛒 Shop",
                             description="Browse products and place orders below."),
            view=ShopPanelView())
        await interaction.response.send_message("✅ Panel posted.", ephemeral=True)

    @shop.command(name="add", description="Add a product.")
    async def sh_add(interaction: discord.Interaction,
                     name: str, price: float, description: str = "",
                     stock: int = -1, image: Optional[str] = None):
        if not await require_admin(interaction): return
        pid = await db.execute(
            "INSERT INTO shop_products (guild_id, name, description, price, "
            "stock, active, image) VALUES (?, ?, ?, ?, ?, 1, ?)",
            (interaction.guild.id, name, description, price, stock, image))
        await interaction.response.send_message(
            f"✅ Added product `#{pid}`.", ephemeral=True)

    @shop.command(name="remove", description="Remove a product.")
    async def sh_remove(interaction: discord.Interaction, product_id: int):
        if not await require_admin(interaction): return
        await db.execute("DELETE FROM shop_products WHERE id=? AND guild_id=?",
                         (product_id, interaction.guild.id))
        await interaction.response.send_message("✅ Removed.", ephemeral=True)

    @shop.command(name="list", description="List products.")
    async def sh_list(interaction: discord.Interaction):
        rows = await db.fetchall(
            "SELECT * FROM shop_products WHERE guild_id=? ORDER BY id",
            (interaction.guild.id,))
        if not rows:
            return await interaction.response.send_message(
                "No products.", ephemeral=True)
        lines = []
        for r in rows:
            stock = "∞" if r["stock"] < 0 else r["stock"]
            flag = "✅" if r["active"] else "❌"
            lines.append(f"{flag} `#{r['id']}` **{r['name']}** — "
                         f"{r['price']} (stock {stock})")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @shop.command(name="buy", description="Buy a product (creates order).")
    async def sh_buy(interaction: discord.Interaction, product_id: int, quantity: int = 1):
        p = await db.fetchone(
            "SELECT * FROM shop_products WHERE id=? AND guild_id=?",
            (product_id, interaction.guild.id))
        if not p or not p["active"]:
            return await interaction.response.send_message(
                "Not available.", ephemeral=True)
        if p["stock"] >= 0 and p["stock"] < quantity:
            return await interaction.response.send_message(
                "Not enough stock.", ephemeral=True)
        total = p["price"] * quantity
        oid = await db.execute(
            "INSERT INTO orders (guild_id, user_id, product_id, quantity, price, "
            "status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)",
            (interaction.guild.id, interaction.user.id, product_id, quantity,
             total, iso(now_utc()), iso(now_utc())))
        if p["stock"] >= 0:
            await db.execute("UPDATE shop_products SET stock=stock-? WHERE id=?",
                             (quantity, product_id))
        await interaction.response.send_message(
            f"✅ Order `#{oid}` created. Total: {total}.", ephemeral=True)

    @shop.command(name="reset", description="Reset shop.")
    async def sh_reset(interaction: discord.Interaction):
        if not await require_admin(interaction): return
        await db.execute("DELETE FROM shop_products WHERE guild_id=?",
                         (interaction.guild.id,))
        await interaction.response.send_message("✅ Reset.", ephemeral=True)

    # ---------------- ORDERS ----------------
    order = app_commands.Group(name="order", description="Order system")
    tree.add_command(order)

    @order.command(name="list", description="List orders.")
    async def o_list(interaction: discord.Interaction):
        rows = await db.fetchall(
            "SELECT * FROM orders WHERE guild_id=? ORDER BY id DESC LIMIT 25",
            (interaction.guild.id,))
        if not rows:
            return await interaction.response.send_message("No orders.", ephemeral=True)
        lines = [f"`#{r['id']}` <@{r['user_id']}> → product {r['product_id']} "
                 f"× {r['quantity']} — {r['status']}" for r in rows]
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @order.command(name="view", description="View an order.")
    async def o_view(interaction: discord.Interaction, order_id: int):
        r = await db.fetchone("SELECT * FROM orders WHERE id=? AND guild_id=?",
                              (order_id, interaction.guild.id))
        if not r:
            return await interaction.response.send_message("Not found.", ephemeral=True)
        e = make_embed(title=f"Order #{r['id']}")
        e.add_field(name="Customer", value=f"<@{r['user_id']}>")
        e.add_field(name="Product", value=f"#{r['product_id']}")
        e.add_field(name="Quantity", value=str(r["quantity"]))
        e.add_field(name="Total", value=str(r["price"]))
        e.add_field(name="Status", value=r["status"])
        e.add_field(name="Created", value=fmt_dt(parse_iso(r["created_at"])))
        await interaction.response.send_message(embed=e, ephemeral=True)

    @order.command(name="status", description="Set order status.")
    async def o_status(interaction: discord.Interaction, order_id: int, status: str):
        if not await require_admin(interaction): return
        await db.execute(
            "UPDATE orders SET status=?, updated_at=? WHERE id=? AND guild_id=?",
            (status, iso(now_utc()), order_id, interaction.guild.id))
        await interaction.response.send_message("✅ Updated.", ephemeral=True)

    @order.command(name="complete", description="Mark order complete.")
    async def o_complete(interaction: discord.Interaction, order_id: int):
        if not await require_admin(interaction): return
        await db.execute(
            "UPDATE orders SET status='completed', updated_at=? WHERE id=? AND guild_id=?",
            (iso(now_utc()), order_id, interaction.guild.id))
        await interaction.response.send_message("✅ Completed.", ephemeral=True)

    # ---------------- NOTIFICATIONS ----------------
    notify = app_commands.Group(name="notify", description="Notification channel")
    tree.add_command(notify)

    @notify.command(name="setup", description="Set notify channel.")
    async def n_setup(interaction: discord.Interaction, channel: discord.TextChannel):
        if not await require_admin(interaction): return
        await db.set_config(interaction.guild.id, "notify.channel", channel.id)
        await interaction.response.send_message("✅ Set.", ephemeral=True)

    @notify.command(name="add", description="Add a notify preset.")
    async def n_add(interaction: discord.Interaction, name: str, message: str):
        if not await require_admin(interaction): return
        msgs = await db.get_json(interaction.guild.id, "notify.messages", default={})
        msgs[name] = message
        await db.set_json(interaction.guild.id, "notify.messages", msgs)
        await interaction.response.send_message("✅ Added.", ephemeral=True)

    @notify.command(name="remove", description="Remove a notify preset.")
    async def n_remove(interaction: discord.Interaction, name: str):
        if not await require_admin(interaction): return
        msgs = await db.get_json(interaction.guild.id, "notify.messages", default={})
        msgs.pop(name, None)
        await db.set_json(interaction.guild.id, "notify.messages", msgs)
        await interaction.response.send_message("✅ Removed.", ephemeral=True)

    @notify.command(name="list", description="List notify presets.")
    async def n_list(interaction: discord.Interaction):
        msgs = await db.get_json(interaction.guild.id, "notify.messages", default={})
        await interaction.response.send_message(
            "\n".join(f"• `{k}`" for k in msgs) or "*(none)*", ephemeral=True)

    @notify.command(name="test", description="Send a test notification.")
    async def n_test(interaction: discord.Interaction, name: str):
        if not await require_admin(interaction): return
        ch_id = await db.get_config(interaction.guild.id, "notify.channel")
        ch = interaction.guild.get_channel(int(ch_id)) if ch_id else None
        if not ch:
            return await interaction.response.send_message(
                "No channel.", ephemeral=True)
        msgs = await db.get_json(interaction.guild.id, "notify.messages", default={})
        tpl = msgs.get(name)
        if not tpl:
            return await interaction.response.send_message(
                "No such preset.", ephemeral=True)
        rendered = apply_placeholders(
            tpl, user=interaction.user.name, mention=interaction.user.mention,
            server=interaction.guild.name, timestamp=fmt_dt(now_utc()))
        for c in chunk_message(rendered):
            await ch.send(c)
        await interaction.response.send_message("✅ Sent.", ephemeral=True)

    # ---------------- AUTOMOD ----------------
    automod = app_commands.Group(name="automod", description="Auto moderation")
    tree.add_command(automod)

    _am_rule_choices = [app_commands.Choice(name=r, value=r) for r in AM_RULES]
    _am_action_choices = [app_commands.Choice(name=a, value=a) for a in AM_ACTIONS]

    async def _am_load(interaction) -> dict:
        return await am_load_config(db, interaction.guild.id)

    async def _am_save(interaction, cfg: dict):
        await am_save_config(db, interaction.guild.id, cfg)

    def _am_status_embed(cfg: dict) -> discord.Embed:
        master = cfg.get("_master")
        e = make_embed(
            title="🛡️ AutoMod Status",
            description="**Master switch:** " + ("✅ ON" if master else "❌ OFF"))
        rules = cfg.get("rules", {}) or {}
        lines = []
        for name in AM_RULES:
            r = rules.get(name, {}) or {}
            state = "🟢" if r.get("enabled") else "⚪"
            lines.append(f"{state} `{name}` → {r.get('action', 'delete')}")
        e.add_field(name="Rules → action", value="\n".join(lines), inline=False)
        wl = cfg.get("whitelist", {}) or {}
        e.add_field(
            name="Global whitelist",
            value=(f"users {len(_am_int_list(wl.get('users')))} · "
                   f"roles {len(_am_int_list(wl.get('roles')))} · "
                   f"channels {len(_am_int_list(wl.get('channels')))}"),
            inline=True)
        logs = cfg.get("logs", {}) or {}
        log_txt = ("on" if logs.get("enabled", True) else "off")
        if logs.get("channel"):
            log_txt += f" → <#{logs['channel']}>"
        else:
            log_txt += " (default)"
        e.add_field(name="Logging", value=log_txt, inline=True)
        return e

    @automod.command(name="setup", description="Enable AutoMod and show an overview.")
    async def am_setup(interaction: discord.Interaction):
        if not await require_admin(interaction): return
        await db.set_config(interaction.guild.id, "automod.enabled", "1")
        cfg = await _am_load(interaction)
        await interaction.response.send_message(embed=_am_status_embed(cfg),
                                                ephemeral=True)

    @automod.command(name="enable", description="Enable AutoMod (master switch).")
    async def am_enable(interaction: discord.Interaction):
        if not await require_admin(interaction): return
        await db.set_config(interaction.guild.id, "automod.enabled", "1")
        await interaction.response.send_message("✅ AutoMod enabled.", ephemeral=True)

    @automod.command(name="disable", description="Disable AutoMod (master switch).")
    async def am_disable(interaction: discord.Interaction):
        if not await require_admin(interaction): return
        await db.set_config(interaction.guild.id, "automod.enabled", "0")
        await interaction.response.send_message("✅ AutoMod disabled.", ephemeral=True)

    @automod.command(name="status", description="Show the full AutoMod configuration.")
    async def am_status(interaction: discord.Interaction):
        if not await require_admin(interaction): return
        cfg = await _am_load(interaction)
        await interaction.response.send_message(embed=_am_status_embed(cfg),
                                                ephemeral=True)

    @automod.command(name="reset", description="Reset ALL AutoMod settings to defaults.")
    async def am_reset(interaction: discord.Interaction):
        if not await require_admin(interaction): return
        await _am_save(interaction, _am_default_config())
        await db.set_config(interaction.guild.id, "automod.enabled", "0")
        await interaction.response.send_message(
            "✅ AutoMod reset to defaults (master switch off).", ephemeral=True)

    # --- Per-rule subgroups (enable/disable on every rule) ---
    _am_groups: dict = {}
    for _r in AM_RULES:
        _g = app_commands.Group(name=_r, description=f"{_r.capitalize()} protection")
        automod.add_command(_g)
        _am_groups[_r] = _g

    def _am_add_toggles(group, rule: str):
        @group.command(name="enable", description=f"Enable the {rule} rule.")
        async def _rule_enable(interaction: discord.Interaction):
            if not await require_admin(interaction): return
            cfg = await _am_load(interaction)
            am_rule(cfg, rule)["enabled"] = True
            await _am_save(interaction, cfg)
            await interaction.response.send_message(
                f"✅ `{rule}` enabled.", ephemeral=True)

        @group.command(name="disable", description=f"Disable the {rule} rule.")
        async def _rule_disable(interaction: discord.Interaction):
            if not await require_admin(interaction): return
            cfg = await _am_load(interaction)
            am_rule(cfg, rule)["enabled"] = False
            await _am_save(interaction, cfg)
            await interaction.response.send_message(
                f"✅ `{rule}` disabled.", ephemeral=True)

    for _r in AM_RULES:
        _am_add_toggles(_am_groups[_r], _r)

    # --- spam settings ---
    @_am_groups["spam"].command(name="settings", description="Configure spam detection.")
    async def am_spam_settings(interaction: discord.Interaction,
                               max_messages: Optional[int] = None,
                               window: Optional[int] = None,
                               duplicate_count: Optional[int] = None,
                               duplicate_window: Optional[int] = None):
        if not await require_admin(interaction): return
        cfg = await _am_load(interaction)
        r = am_rule(cfg, "spam")
        if max_messages is not None: r["max_messages"] = max(2, int(max_messages))
        if window is not None: r["window"] = max(1, int(window))
        if duplicate_count is not None: r["duplicate_count"] = max(2, int(duplicate_count))
        if duplicate_window is not None: r["duplicate_window"] = max(1, int(duplicate_window))
        await _am_save(interaction, cfg)
        await interaction.response.send_message(
            f"✅ Spam: {r['max_messages']} msgs/{r['window']}s · "
            f"duplicate {r['duplicate_count']}/{r['duplicate_window']}s",
            ephemeral=True)

    # --- links settings ---
    @_am_groups["links"].command(name="settings", description="Configure link filtering.")
    @app_commands.choices(mode=[app_commands.Choice(name=m, value=m)
                                for m in ("all", "blacklist", "whitelist")])
    async def am_links_settings(interaction: discord.Interaction,
                                mode: Optional[app_commands.Choice[str]] = None,
                                blacklist: Optional[str] = None,
                                allowlist: Optional[str] = None):
        if not await require_admin(interaction): return
        cfg = await _am_load(interaction)
        r = am_rule(cfg, "links")
        if mode is not None: r["mode"] = mode.value
        if blacklist is not None:
            r["blacklist"] = [d.strip().lower() for d in blacklist.split(",") if d.strip()]
        if allowlist is not None:
            r["allowlist"] = [d.strip().lower() for d in allowlist.split(",") if d.strip()]
        await _am_save(interaction, cfg)
        await interaction.response.send_message(
            f"✅ Links mode: **{r.get('mode', 'blacklist')}** · "
            f"blacklist {len(r.get('blacklist', []))} · allowlist {len(r.get('allowlist', []))}",
            ephemeral=True)

    # --- words settings + management ---
    @_am_groups["words"].command(name="settings", description="Configure banned-word matching.")
    @app_commands.choices(match=[app_commands.Choice(name=m, value=m)
                                 for m in ("exact", "partial")])
    async def am_words_settings(interaction: discord.Interaction,
                                match: Optional[app_commands.Choice[str]] = None):
        if not await require_admin(interaction): return
        cfg = await _am_load(interaction)
        r = am_rule(cfg, "words")
        if match is not None: r["match"] = match.value
        await _am_save(interaction, cfg)
        await interaction.response.send_message(
            f"✅ Word match mode: **{r.get('match', 'partial')}**", ephemeral=True)

    @_am_groups["words"].command(name="add", description="Add banned word(s), comma-separated.")
    async def am_words_add(interaction: discord.Interaction, words: str):
        if not await require_admin(interaction): return
        cfg = await _am_load(interaction)
        r = am_rule(cfg, "words")
        cur = [str(w) for w in (r.get("words") or [])]
        low = {w.lower() for w in cur}
        added = []
        for w in words.split(","):
            w = w.strip()
            if w and w.lower() not in low:
                cur.append(w)
                low.add(w.lower())
                added.append(w)
        r["words"] = cur
        await _am_save(interaction, cfg)
        await interaction.response.send_message(
            ("✅ Added: " + ", ".join(f"`{w}`" for w in added)) if added
            else "ℹ️ No new words added.", ephemeral=True)

    @_am_groups["words"].command(name="remove", description="Remove banned word(s), comma-separated.")
    async def am_words_remove(interaction: discord.Interaction, words: str):
        if not await require_admin(interaction): return
        cfg = await _am_load(interaction)
        r = am_rule(cfg, "words")
        cur = [str(w) for w in (r.get("words") or [])]
        drop = {w.strip().lower() for w in words.split(",") if w.strip()}
        kept = [w for w in cur if w.lower() not in drop]
        removed = len(cur) - len(kept)
        r["words"] = kept
        await _am_save(interaction, cfg)
        await interaction.response.send_message(
            f"✅ Removed {removed} word(s). {len(kept)} remain.", ephemeral=True)

    @_am_groups["words"].command(name="list", description="List banned words.")
    async def am_words_list(interaction: discord.Interaction):
        if not await require_admin(interaction): return
        cfg = await _am_load(interaction)
        words = [str(w) for w in (am_rule(cfg, "words").get("words") or [])]
        if not words:
            return await interaction.response.send_message(
                "No banned words configured.", ephemeral=True)
        shown = ", ".join(f"`{w}`" for w in words[:100])
        more = f"\n…and {len(words) - 100} more" if len(words) > 100 else ""
        await interaction.response.send_message(
            f"**{len(words)} banned word(s):**\n{shown}{more}", ephemeral=True)

    @_am_groups["words"].command(name="clear", description="Clear all banned words.")
    async def am_words_clear(interaction: discord.Interaction):
        if not await require_admin(interaction): return
        cfg = await _am_load(interaction)
        am_rule(cfg, "words")["words"] = []
        await _am_save(interaction, cfg)
        await interaction.response.send_message("✅ Cleared all banned words.",
                                                ephemeral=True)

    # --- mentions settings ---
    @_am_groups["mentions"].command(name="settings", description="Configure mention limits.")
    async def am_mentions_settings(interaction: discord.Interaction,
                                   max_mentions: Optional[int] = None,
                                   max_role_mentions: Optional[int] = None):
        if not await require_admin(interaction): return
        cfg = await _am_load(interaction)
        r = am_rule(cfg, "mentions")
        if max_mentions is not None: r["max_mentions"] = max(1, int(max_mentions))
        if max_role_mentions is not None: r["max_role_mentions"] = max(1, int(max_role_mentions))
        await _am_save(interaction, cfg)
        await interaction.response.send_message(
            f"✅ Mentions: max {r['max_mentions']} users / {r['max_role_mentions']} roles",
            ephemeral=True)

    # --- caps settings ---
    @_am_groups["caps"].command(name="settings", description="Configure caps filter.")
    async def am_caps_settings(interaction: discord.Interaction,
                               percent: Optional[int] = None,
                               min_length: Optional[int] = None):
        if not await require_admin(interaction): return
        cfg = await _am_load(interaction)
        r = am_rule(cfg, "caps")
        if percent is not None: r["percent"] = max(1, min(100, int(percent)))
        if min_length is not None: r["min_length"] = max(1, int(min_length))
        await _am_save(interaction, cfg)
        await interaction.response.send_message(
            f"✅ Caps: {r['percent']}% over {r['min_length']} chars", ephemeral=True)

    # --- emojis settings ---
    @_am_groups["emojis"].command(name="settings", description="Configure emoji-spam limit.")
    async def am_emojis_settings(interaction: discord.Interaction,
                                 max_emojis: Optional[int] = None):
        if not await require_admin(interaction): return
        cfg = await _am_load(interaction)
        r = am_rule(cfg, "emojis")
        if max_emojis is not None: r["max_emojis"] = max(1, int(max_emojis))
        await _am_save(interaction, cfg)
        await interaction.response.send_message(
            f"✅ Emojis: max {r['max_emojis']}", ephemeral=True)

    # --- characters settings ---
    @_am_groups["characters"].command(name="settings", description="Configure repeated-character limit.")
    async def am_characters_settings(interaction: discord.Interaction,
                                     max_repeat: Optional[int] = None):
        if not await require_admin(interaction): return
        cfg = await _am_load(interaction)
        r = am_rule(cfg, "characters")
        if max_repeat is not None: r["max_repeat"] = max(2, int(max_repeat))
        await _am_save(interaction, cfg)
        await interaction.response.send_message(
            f"✅ Characters: max {r['max_repeat']} repeats", ephemeral=True)

    # --- raid settings ---
    @_am_groups["raid"].command(name="settings", description="Configure raid protection.")
    async def am_raid_settings(interaction: discord.Interaction,
                               max_joins: Optional[int] = None,
                               window: Optional[int] = None):
        if not await require_admin(interaction): return
        cfg = await _am_load(interaction)
        r = am_rule(cfg, "raid")
        if max_joins is not None: r["max_joins"] = max(2, int(max_joins))
        if window is not None: r["window"] = max(1, int(window))
        await _am_save(interaction, cfg)
        await interaction.response.send_message(
            f"✅ Raid: {r['max_joins']} joins/{r['window']}s", ephemeral=True)

    # --- accounts settings ---
    @_am_groups["accounts"].command(name="settings", description="Configure account-age protection.")
    async def am_accounts_settings(interaction: discord.Interaction,
                                   min_age_days: Optional[int] = None):
        if not await require_admin(interaction): return
        cfg = await _am_load(interaction)
        r = am_rule(cfg, "accounts")
        if min_age_days is not None: r["min_age_days"] = max(0, int(min_age_days))
        await _am_save(interaction, cfg)
        await interaction.response.send_message(
            f"✅ Accounts: min age {r['min_age_days']} day(s)", ephemeral=True)

    # --- antinuke settings ---
    @_am_groups["antinuke"].command(name="settings", description="Configure anti-nuke thresholds.")
    async def am_antinuke_settings(interaction: discord.Interaction,
                                   max_deletes: Optional[int] = None,
                                   window: Optional[int] = None):
        if not await require_admin(interaction): return
        cfg = await _am_load(interaction)
        r = am_rule(cfg, "antinuke")
        if max_deletes is not None: r["max_deletes"] = max(1, int(max_deletes))
        if window is not None: r["window"] = max(1, int(window))
        await _am_save(interaction, cfg)
        await interaction.response.send_message(
            f"✅ Anti-nuke: {r['max_deletes']} actions/{r['window']}s", ephemeral=True)

    # --- action set (per-rule punishment) ---
    action_grp = app_commands.Group(name="action", description="Per-rule punishments")
    automod.add_command(action_grp)

    @action_grp.command(name="set", description="Set the punishment for a rule.")
    @app_commands.choices(rule=_am_rule_choices, action=_am_action_choices)
    async def am_action_set(interaction: discord.Interaction,
                            rule: app_commands.Choice[str],
                            action: app_commands.Choice[str],
                            timeout_duration: Optional[int] = None):
        if not await require_admin(interaction): return
        cfg = await _am_load(interaction)
        r = am_rule(cfg, rule.value)
        r["action"] = action.value
        extra = ""
        if timeout_duration is not None:
            secs = max(1, min(int(timeout_duration), 28 * 86400))
            r["timeout_duration"] = secs
            extra = f" · timeout {fmt_duration(secs)}"
        await _am_save(interaction, cfg)
        await interaction.response.send_message(
            f"✅ `{rule.value}` punishment → **{action.value}**{extra}", ephemeral=True)

    # --- whitelist ---
    wl_grp = app_commands.Group(name="whitelist", description="Bypass AutoMod")
    automod.add_command(wl_grp)

    async def _am_wl_edit(interaction, kind: str, ident: int, remove: bool):
        cfg = await _am_load(interaction)
        wl = cfg.setdefault("whitelist", {})
        lst = _am_int_list(wl.get(kind))
        if remove:
            if ident in lst:
                lst.remove(ident)
            msg = "✅ Removed from global whitelist."
        else:
            if ident not in lst:
                lst.append(ident)
            msg = "✅ Added to global whitelist."
        wl[kind] = lst
        await _am_save(interaction, cfg)
        await interaction.response.send_message(msg, ephemeral=True)

    @wl_grp.command(name="channel_add", description="Whitelist a channel (all rules).")
    async def am_wl_channel_add(interaction: discord.Interaction,
                                channel: discord.TextChannel):
        if not await require_admin(interaction): return
        await _am_wl_edit(interaction, "channels", channel.id, False)

    @wl_grp.command(name="channel_remove", description="Un-whitelist a channel.")
    async def am_wl_channel_remove(interaction: discord.Interaction,
                                   channel: discord.TextChannel):
        if not await require_admin(interaction): return
        await _am_wl_edit(interaction, "channels", channel.id, True)

    @wl_grp.command(name="role_add", description="Whitelist a role (all rules).")
    async def am_wl_role_add(interaction: discord.Interaction, role: discord.Role):
        if not await require_admin(interaction): return
        await _am_wl_edit(interaction, "roles", role.id, False)

    @wl_grp.command(name="role_remove", description="Un-whitelist a role.")
    async def am_wl_role_remove(interaction: discord.Interaction, role: discord.Role):
        if not await require_admin(interaction): return
        await _am_wl_edit(interaction, "roles", role.id, True)

    @wl_grp.command(name="user_add", description="Whitelist a user (all rules).")
    async def am_wl_user_add(interaction: discord.Interaction, user: discord.Member):
        if not await require_admin(interaction): return
        await _am_wl_edit(interaction, "users", user.id, False)

    @wl_grp.command(name="user_remove", description="Un-whitelist a user.")
    async def am_wl_user_remove(interaction: discord.Interaction, user: discord.Member):
        if not await require_admin(interaction): return
        await _am_wl_edit(interaction, "users", user.id, True)

    @wl_grp.command(name="rule", description="Whitelist a channel/role/user for ONE rule.")
    @app_commands.choices(rule=_am_rule_choices)
    async def am_wl_rule(interaction: discord.Interaction,
                         rule: app_commands.Choice[str],
                         channel: Optional[discord.TextChannel] = None,
                         role: Optional[discord.Role] = None,
                         user: Optional[discord.Member] = None,
                         remove: bool = False):
        if not await require_admin(interaction): return
        if not (channel or role or user):
            return await interaction.response.send_message(
                "Provide a channel, role, or user.", ephemeral=True)
        cfg = await _am_load(interaction)
        rwl = cfg.setdefault("rule_whitelist", {}).setdefault(
            rule.value, {"users": [], "roles": [], "channels": []})
        changed = 0
        for kind, obj in (("channels", channel), ("roles", role), ("users", user)):
            if obj is None:
                continue
            lst = _am_int_list(rwl.get(kind))
            if remove:
                if obj.id in lst:
                    lst.remove(obj.id)
                    changed += 1
            elif obj.id not in lst:
                lst.append(obj.id)
                changed += 1
            rwl[kind] = lst
        await _am_save(interaction, cfg)
        verb = "Removed" if remove else "Added"
        await interaction.response.send_message(
            f"✅ {verb} {changed} entr(ies) for rule `{rule.value}`.", ephemeral=True)

    # --- messages ---
    msg_grp = app_commands.Group(name="messages", description="Violation messages")
    automod.add_command(msg_grp)

    @msg_grp.command(name="set", description="Set the public violation message template.")
    async def am_msg_set(interaction: discord.Interaction, template: str,
                         delete_after: Optional[int] = None):
        if not await require_admin(interaction): return
        cfg = await _am_load(interaction)
        msgs = cfg.setdefault("messages", {})
        msgs["violation"] = template[:1900]
        if delete_after is not None:
            msgs["delete_after"] = max(0, int(delete_after))
        await _am_save(interaction, cfg)
        await interaction.response.send_message(
            "✅ Violation message saved.\n"
            "Placeholders: `{user}` `{mention}` `{username}` `{display_name}` "
            "`{server}` `{rule}` `{action}` `{reason}`", ephemeral=True)

    @msg_grp.command(name="reset", description="Reset the violation message to default.")
    async def am_msg_reset(interaction: discord.Interaction):
        if not await require_admin(interaction): return
        cfg = await _am_load(interaction)
        msgs = cfg.setdefault("messages", {})
        msgs["violation"] = AM_DEFAULT_MESSAGE
        msgs["delete_after"] = 8
        await _am_save(interaction, cfg)
        await interaction.response.send_message("✅ Violation message reset.",
                                                ephemeral=True)

    @msg_grp.command(name="view", description="View the current violation message.")
    async def am_msg_view(interaction: discord.Interaction):
        if not await require_admin(interaction): return
        cfg = await _am_load(interaction)
        msgs = cfg.get("messages", {}) or {}
        await interaction.response.send_message(
            f"**Template:**\n```\n{msgs.get('violation', AM_DEFAULT_MESSAGE)}\n```\n"
            f"**Delete after:** {msgs.get('delete_after', 8)}s", ephemeral=True)

    # --- logs ---
    logs_grp = app_commands.Group(name="logs", description="AutoMod logging")
    automod.add_command(logs_grp)

    @logs_grp.command(name="enable", description="Enable AutoMod logging.")
    async def am_logs_enable(interaction: discord.Interaction):
        if not await require_admin(interaction): return
        cfg = await _am_load(interaction)
        cfg.setdefault("logs", {})["enabled"] = True
        await _am_save(interaction, cfg)
        await interaction.response.send_message("✅ AutoMod logging enabled.",
                                                ephemeral=True)

    @logs_grp.command(name="disable", description="Disable AutoMod logging.")
    async def am_logs_disable(interaction: discord.Interaction):
        if not await require_admin(interaction): return
        cfg = await _am_load(interaction)
        cfg.setdefault("logs", {})["enabled"] = False
        await _am_save(interaction, cfg)
        await interaction.response.send_message("✅ AutoMod logging disabled.",
                                                ephemeral=True)

    @logs_grp.command(name="channel", description="Set a dedicated AutoMod log channel.")
    async def am_logs_channel(interaction: discord.Interaction,
                              channel: Optional[discord.TextChannel] = None):
        if not await require_admin(interaction): return
        cfg = await _am_load(interaction)
        cfg.setdefault("logs", {})["channel"] = channel.id if channel else None
        await _am_save(interaction, cfg)
        await interaction.response.send_message(
            ("✅ AutoMod logs → " + channel.mention) if channel
            else "✅ AutoMod logs fall back to the default logging channel.",
            ephemeral=True)

    # --- stats ---
    stats_grp = app_commands.Group(name="stats", description="Violation statistics")
    automod.add_command(stats_grp)

    @stats_grp.command(name="stats", description="Overall AutoMod statistics.")
    async def am_stats_stats(interaction: discord.Interaction):
        if not await require_admin(interaction): return
        gid = interaction.guild.id
        total = await db.fetchone(
            "SELECT COUNT(*) AS c FROM automod_violations WHERE guild_id=?", (gid,))
        per_rule = await db.fetchall(
            "SELECT rule, COUNT(*) AS c FROM automod_violations "
            "WHERE guild_id=? GROUP BY rule ORDER BY c DESC", (gid,))
        e = make_embed(title="📊 AutoMod Statistics",
                       description=f"**Total violations:** {total['c'] if total else 0}")
        if per_rule:
            e.add_field(name="By rule",
                        value="\n".join(f"`{r['rule']}` — {r['c']}" for r in per_rule),
                        inline=False)
        await interaction.response.send_message(embed=e, ephemeral=True)

    @stats_grp.command(name="violations", description="Recent violations (optionally for a user).")
    async def am_stats_violations(interaction: discord.Interaction,
                                  user: Optional[discord.Member] = None):
        if not await require_admin(interaction): return
        gid = interaction.guild.id
        if user is not None:
            rows = await db.fetchall(
                "SELECT rule, action, reason, created_at FROM automod_violations "
                "WHERE guild_id=? AND user_id=? ORDER BY id DESC LIMIT 10",
                (gid, user.id))
            title = f"📄 Violations — {user}"
        else:
            rows = await db.fetchall(
                "SELECT user_id, rule, action, reason, created_at FROM automod_violations "
                "WHERE guild_id=? ORDER BY id DESC LIMIT 10", (gid,))
            title = "📄 Recent Violations"
        if not rows:
            return await interaction.response.send_message(
                "No violations recorded.", ephemeral=True)
        lines = []
        for r in rows:
            who = "" if user is not None else f"<@{r['user_id']}> "
            lines.append(f"{who}`{r['rule']}` → {r['action']} — {r['created_at']}")
        await interaction.response.send_message(
            embed=make_embed(title=title, description="\n".join(lines)[:4000]),
            ephemeral=True)

    @stats_grp.command(name="top", description="Top rule violators.")
    async def am_stats_top(interaction: discord.Interaction):
        if not await require_admin(interaction): return
        gid = interaction.guild.id
        rows = await db.fetchall(
            "SELECT user_id, COUNT(*) AS c FROM automod_violations "
            "WHERE guild_id=? GROUP BY user_id ORDER BY c DESC LIMIT 10", (gid,))
        if not rows:
            return await interaction.response.send_message(
                "No violations recorded.", ephemeral=True)
        await interaction.response.send_message(
            embed=make_embed(
                title="🏆 Top Violators",
                description="\n".join(f"<@{r['user_id']}> — {r['c']}" for r in rows)),
            ephemeral=True)


    # ---------------- MODERATION ----------------
    async def _create_case(guild_id: int, user_id: int, mod_id: int,
                           action: str, reason: str) -> int:
        return await db.execute(
            "INSERT INTO cases (guild_id, user_id, moderator_id, action, "
            "reason, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (guild_id, user_id, mod_id, action, reason, iso(now_utc())))

    async def _try_action_dm(guild: discord.Guild, member: discord.Member,
                             template_key: str, **extra):
        if await db.get_config(guild.id, "actiondm.enabled", "0") != "1":
            return
        msg = await db.get_config(guild.id, template_key)
        if not msg:
            return
        rendered = apply_placeholders(
            msg, user=member.name, mention=member.mention,
            username=member.name, display_name=member.display_name,
            server=guild.name, timestamp=fmt_dt(now_utc()), **extra)
        try:
            for c in chunk_message(rendered):
                await member.send(c)
        except Exception:
            pass

    @tree.command(name="warn", description="Warn a member.")
    @app_commands.default_permissions(moderate_members=True)
    async def warn(interaction: discord.Interaction, member: discord.Member,
                   reason: str = "No reason"):
        if not is_admin_or_mod(interaction.user):
            return await interaction.response.send_message("No permission.", ephemeral=True)
        ok, why = hierarchy_ok(interaction.guild, member)
        if not ok:
            return await interaction.response.send_message(f"❌ {why}", ephemeral=True)
        wid = await db.execute(
            "INSERT INTO warnings (guild_id, user_id, moderator_id, reason, "
            "created_at) VALUES (?, ?, ?, ?, ?)",
            (interaction.guild.id, member.id, interaction.user.id,
             reason, iso(now_utc())))
        await _create_case(interaction.guild.id, member.id, interaction.user.id,
                           "warn", reason)
        await interaction.response.send_message(
            f"⚠️ Warned {member.mention} (warning #{wid}): {reason}")
        await _log_guild(bot, interaction.guild, "warnings", "Member Warned",
                         f"**Member:** {member.mention}\n"
                         f"**By:** {interaction.user.mention}\n"
                         f"**Reason:** {reason}")

    @tree.command(name="warnings", description="List warnings for a member.")
    async def warnings(interaction: discord.Interaction, member: discord.Member):
        rows = await db.fetchall(
            "SELECT * FROM warnings WHERE guild_id=? AND user_id=? ORDER BY id DESC",
            (interaction.guild.id, member.id))
        if not rows:
            return await interaction.response.send_message("No warnings.", ephemeral=True)
        lines = [f"`#{r['id']}` by <@{r['moderator_id']}> — {r['reason']}" for r in rows]
        await interaction.response.send_message("\n".join(lines[:25]), ephemeral=True)

    @tree.command(name="clearwarnings", description="Clear warnings for a member.")
    async def clearwarnings(interaction: discord.Interaction, member: discord.Member):
        if not is_admin_or_mod(interaction.user):
            return await interaction.response.send_message("No permission.", ephemeral=True)
        await db.execute("DELETE FROM warnings WHERE guild_id=? AND user_id=?",
                         (interaction.guild.id, member.id))
        await interaction.response.send_message("✅ Cleared.", ephemeral=True)

    @tree.command(name="timeout", description="Timeout a member.")
    @app_commands.default_permissions(moderate_members=True)
    async def timeout_cmd(interaction: discord.Interaction, member: discord.Member,
                          duration: str, reason: str = "No reason"):
        if not is_admin_or_mod(interaction.user):
            return await interaction.response.send_message("No permission.", ephemeral=True)
        ok, why = hierarchy_ok(interaction.guild, member)
        if not ok:
            return await interaction.response.send_message(f"❌ {why}", ephemeral=True)
        secs = parse_duration(duration)
        if not secs or secs < 1 or secs > 28 * 86400:
            return await interaction.response.send_message(
                "Invalid duration (max 28d).", ephemeral=True)
        until = now_utc() + timedelta(seconds=secs)
        await member.timeout(timedelta(seconds=secs), reason=reason)
        cid = await _create_case(interaction.guild.id, member.id,
                                 interaction.user.id, "timeout", reason)
        tid = await db.execute(
            "INSERT INTO timeouts (guild_id, user_id, moderator_id, reason, "
            "ends_at) VALUES (?, ?, ?, ?, ?)",
            (interaction.guild.id, member.id, interaction.user.id,
             reason, iso(until)))
        await _try_action_dm(interaction.guild, member, "actiondm.timeout_msg",
                             moderator=interaction.user.mention, reason=reason,
                             duration=duration, timeout_end=fmt_dt(until),
                             case_id=str(cid))
        await bot.scheduler.schedule("timeout_end", until + timedelta(seconds=5),
                                     {"timeout_id": tid}, interaction.guild.id)
        await interaction.response.send_message(
            f"⏳ Timed out {member.mention} for {duration} (case #{cid}).")

    @tree.command(name="untimeout", description="Remove a member's timeout.")
    @app_commands.default_permissions(moderate_members=True)
    async def untimeout(interaction: discord.Interaction, member: discord.Member):
        if not is_admin_or_mod(interaction.user):
            return await interaction.response.send_message("No permission.", ephemeral=True)
        ok, why = hierarchy_ok(interaction.guild, member)
        if not ok:
            return await interaction.response.send_message(f"❌ {why}", ephemeral=True)
        await member.timeout(None, reason=f"Timeout removed by {interaction.user}")
        await interaction.response.send_message("✅ Timeout removed.")

    @tree.command(name="kick", description="Kick a member.")
    @app_commands.default_permissions(kick_members=True)
    async def kick(interaction: discord.Interaction, member: discord.Member,
                   reason: str = "No reason"):
        if not is_admin_or_mod(interaction.user):
            return await interaction.response.send_message("No permission.", ephemeral=True)
        ok, why = hierarchy_ok(interaction.guild, member)
        if not ok:
            return await interaction.response.send_message(f"❌ {why}", ephemeral=True)
        cid = await _create_case(interaction.guild.id, member.id,
                                 interaction.user.id, "kick", reason)
        await _try_action_dm(interaction.guild, member, "actiondm.kick_msg",
                             moderator=interaction.user.mention, reason=reason,
                             case_id=str(cid))
        try:
            await member.kick(reason=reason)
        except discord.Forbidden:
            return await interaction.response.send_message("❌ Forbidden.", ephemeral=True)
        await interaction.response.send_message(f"👢 Kicked {member} (case #{cid}).")
        await _log_guild(bot, interaction.guild, "kicks", "Member Kicked",
                         f"**Member:** {member}\n**By:** {interaction.user.mention}\n"
                         f"**Reason:** {reason}")

    @tree.command(name="ban", description="Ban a member.")
    @app_commands.default_permissions(ban_members=True)
    async def ban(interaction: discord.Interaction, member: discord.Member,
                  reason: str = "No reason"):
        if not is_admin_or_mod(interaction.user):
            return await interaction.response.send_message("No permission.", ephemeral=True)
        ok, why = hierarchy_ok(interaction.guild, member)
        if not ok:
            return await interaction.response.send_message(f"❌ {why}", ephemeral=True)
        cid = await _create_case(interaction.guild.id, member.id,
                                 interaction.user.id, "ban", reason)
        await _try_action_dm(interaction.guild, member, "actiondm.ban_msg",
                             moderator=interaction.user.mention, reason=reason,
                             case_id=str(cid))
        try:
            await member.ban(reason=reason)
        except discord.Forbidden:
            return await interaction.response.send_message("❌ Forbidden.", ephemeral=True)
        await interaction.response.send_message(f"🔨 Banned {member} (case #{cid}).")
        await _log_guild(bot, interaction.guild, "bans", "Member Banned",
                         f"**Member:** {member}\n**By:** {interaction.user.mention}\n"
                         f"**Reason:** {reason}")

    @tree.command(name="unban", description="Unban by user ID.")
    @app_commands.default_permissions(ban_members=True)
    async def unban(interaction: discord.Interaction, user_id: str,
                    reason: str = "No reason"):
        if not is_admin_or_mod(interaction.user):
            return await interaction.response.send_message("No permission.", ephemeral=True)
        try:
            uid = int(user_id)
        except ValueError:
            return await interaction.response.send_message("Invalid ID.", ephemeral=True)
        user = discord.Object(id=uid)
        try:
            await interaction.guild.unban(user, reason=reason)
        except discord.NotFound:
            return await interaction.response.send_message("User not banned.", ephemeral=True)
        await interaction.response.send_message(f"✅ Unbanned <@{uid}>.")
        await _log_guild(bot, interaction.guild, "unbans", "Member Unbanned",
                         f"**User:** <@{uid}>\n**By:** {interaction.user.mention}")

    @tree.command(name="purge", description="Bulk delete messages.")
    @app_commands.default_permissions(manage_messages=True)
    async def purge(interaction: discord.Interaction, amount: int,
                    member: Optional[discord.Member] = None):
        if not is_admin_or_mod(interaction.user):
            return await interaction.response.send_message("No permission.", ephemeral=True)
        amount = max(1, min(amount, 500))
        await interaction.response.defer(ephemeral=True)
        deleted = await interaction.channel.purge(
            limit=amount,
            check=lambda m: member is None or m.author.id == member.id)
        await interaction.followup.send(
            f"🗑 Deleted {len(deleted)} messages.", ephemeral=True)

    @tree.command(name="lock", description="Lock a channel.")
    @app_commands.default_permissions(manage_channels=True)
    async def lock(interaction: discord.Interaction,
                   channel: Optional[discord.TextChannel] = None):
        if not is_admin_or_mod(interaction.user):
            return await interaction.response.send_message("No permission.", ephemeral=True)
        ch = channel or interaction.channel
        ow = ch.overwrites_for(interaction.guild.default_role)
        ow.send_messages = False
        await ch.set_permissions(interaction.guild.default_role, overwrite=ow)
        await interaction.response.send_message(f"🔒 Locked {ch.mention}.")

    @tree.command(name="unlock", description="Unlock a channel.")
    @app_commands.default_permissions(manage_channels=True)
    async def unlock(interaction: discord.Interaction,
                     channel: Optional[discord.TextChannel] = None):
        if not is_admin_or_mod(interaction.user):
            return await interaction.response.send_message("No permission.", ephemeral=True)
        ch = channel or interaction.channel
        ow = ch.overwrites_for(interaction.guild.default_role)
        ow.send_messages = None
        await ch.set_permissions(interaction.guild.default_role, overwrite=ow)
        await interaction.response.send_message(f"🔓 Unlocked {ch.mention}.")

    # ---------------- LOGGING ----------------
    logging_grp = app_commands.Group(name="logging", description="Logging")
    tree.add_command(logging_grp)

    @logging_grp.command(name="setup", description="Set log channel and enable common events.")
    async def lg_setup(interaction: discord.Interaction, channel: discord.TextChannel):
        if not await require_admin(interaction): return
        await db.set_config(interaction.guild.id, "logging.channel", channel.id)
        await db.set_json(interaction.guild.id, "logging.events", [
            "joins", "leaves", "messages", "warnings", "kicks", "bans",
            "unbans", "tickets", "shop", "automod"])
        await interaction.response.send_message("✅ Logging set.", ephemeral=True)

    @logging_grp.command(name="channel", description="Set log channel.")
    async def lg_channel(interaction: discord.Interaction, channel: discord.TextChannel):
        if not await require_admin(interaction): return
        await db.set_config(interaction.guild.id, "logging.channel", channel.id)
        await interaction.response.send_message("✅ Set.", ephemeral=True)

    @logging_grp.command(name="events", description="Set events (comma-separated).")
    async def lg_events(interaction: discord.Interaction, events: str):
        if not await require_admin(interaction): return
        evs = [e.strip() for e in events.split(",") if e.strip()]
        await db.set_json(interaction.guild.id, "logging.events", evs)
        await interaction.response.send_message("✅ Saved.", ephemeral=True)

    # ---------------- REACTION ROLES ----------------
    rr = app_commands.Group(name="reactionrole", description="Reaction/Button roles")
    tree.add_command(rr)

    @rr.command(name="setup", description="Create a new reaction-role panel.")
    async def rr_setup(interaction: discord.Interaction, title: str,
                       description: str = "", mode: str = "button"):
        if not await require_admin(interaction): return
        mode = mode.lower()
        if mode not in ("button", "select"):
            return await interaction.response.send_message(
                "mode must be `button` or `select`.", ephemeral=True)
        pid = await db.execute(
            "INSERT INTO reaction_panels (guild_id, channel_id, title, "
            "description, mode) VALUES (?, ?, ?, ?, ?)",
            (interaction.guild.id, interaction.channel.id, title, description, mode))
        await interaction.response.send_message(
            f"✅ Panel `#{pid}` created. Use `/reactionrole add panel_id:{pid} role:@role`.",
            ephemeral=True)

    @rr.command(name="create", description="Post an existing panel.")
    async def rr_create(interaction: discord.Interaction, panel_id: int,
                        channel: Optional[discord.TextChannel] = None):
        if not await require_admin(interaction): return
        p = await db.fetchone(
            "SELECT * FROM reaction_panels WHERE id=? AND guild_id=?",
            (panel_id, interaction.guild.id))
        if not p:
            return await interaction.response.send_message(
                "Panel not found.", ephemeral=True)
        roles = await db.fetchall(
            "SELECT role_id, label, emoji FROM reaction_roles WHERE panel_id=?",
            (panel_id,))
        view = ReactionRoleView(panel_id, [dict(r) for r in roles], p["mode"] or "button")
        ch = channel or interaction.channel
        msg = await ch.send(
            embed=make_embed(title=p["title"] or "Roles",
                             description=p["description"] or None), view=view)
        await db.execute(
            "UPDATE reaction_panels SET message_id=?, channel_id=? WHERE id=?",
            (msg.id, ch.id, panel_id))
        bot.add_view(view, message_id=msg.id)
        await interaction.response.send_message("✅ Panel posted.", ephemeral=True)

    @rr.command(name="add", description="Add a role to a panel.")
    async def rr_add(interaction: discord.Interaction, panel_id: int,
                     role: discord.Role, label: Optional[str] = None,
                     emoji: Optional[str] = None):
        if not await require_admin(interaction): return
        await db.execute(
            "INSERT INTO reaction_roles (panel_id, role_id, label, emoji) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(panel_id, role_id) DO UPDATE SET "
            "label=excluded.label, emoji=excluded.emoji",
            (panel_id, role.id, label or role.name, emoji))
        await interaction.response.send_message(
            "✅ Added (re-post panel to update).", ephemeral=True)

    @rr.command(name="remove", description="Remove a role from a panel.")
    async def rr_remove(interaction: discord.Interaction, panel_id: int, role: discord.Role):
        if not await require_admin(interaction): return
        await db.execute("DELETE FROM reaction_roles WHERE panel_id=? AND role_id=?",
                         (panel_id, role.id))
        await interaction.response.send_message("✅ Removed.", ephemeral=True)

    @rr.command(name="list", description="List reaction-role panels.")
    async def rr_list(interaction: discord.Interaction):
        rows = await db.fetchall(
            "SELECT * FROM reaction_panels WHERE guild_id=?",
            (interaction.guild.id,))
        lines = []
        for p in rows:
            cnt = await db.fetchone(
                "SELECT COUNT(*) c FROM reaction_roles WHERE panel_id=?",
                (p["id"],))
            lines.append(f"• `#{p['id']}` {p['title']} — {cnt['c']} role(s)")
        await interaction.response.send_message(
            "\n".join(lines) or "*(none)*", ephemeral=True)

    # ---------------- GIVEAWAYS ----------------
    giveaway = app_commands.Group(name="giveaway", description="Giveaways")
    tree.add_command(giveaway)

    @giveaway.command(name="create", description="Create a giveaway.")
    async def gw_create(interaction: discord.Interaction, prize: str,
                        duration: str, winners: int = 1,
                        channel: Optional[discord.TextChannel] = None):
        if not await require_admin(interaction): return
        secs = parse_duration(duration)
        if not secs:
            return await interaction.response.send_message(
                "Invalid duration.", ephemeral=True)
        ch = channel or interaction.channel
        ends_at = now_utc() + timedelta(seconds=secs)
        embed = make_embed(
            title=f"🎉 {prize}",
            description=f"React with the button to enter!\n"
                        f"**Winners:** {winners}\n**Ends:** {fmt_dt(ends_at)}")
        gid = await db.execute(
            "INSERT INTO giveaways (guild_id, channel_id, prize, winners, "
            "host_id, ends_at) VALUES (?, ?, ?, ?, ?, ?)",
            (interaction.guild.id, ch.id, prize, winners,
             interaction.user.id, iso(ends_at)))
        view = GiveawayView(gid)
        msg = await ch.send(embed=embed, view=view)
        await db.execute("UPDATE giveaways SET message_id=? WHERE id=?", (msg.id, gid))
        bot.add_view(view, message_id=msg.id)
        await bot.scheduler.schedule("giveaway_end", ends_at,
                                     {"giveaway_id": gid}, interaction.guild.id)
        await interaction.response.send_message(
            f"✅ Giveaway `#{gid}` started.", ephemeral=True)

    @giveaway.command(name="end", description="End a giveaway immediately.")
    async def gw_end(interaction: discord.Interaction, giveaway_id: int):
        if not await require_admin(interaction): return
        await _end_giveaway(bot, giveaway_id)
        await interaction.response.send_message("✅ Ended.", ephemeral=True)

    @giveaway.command(name="reroll", description="Reroll winners.")
    async def gw_reroll(interaction: discord.Interaction, giveaway_id: int):
        if not await require_admin(interaction): return
        await _end_giveaway(bot, giveaway_id, reroll=True)
        await interaction.response.send_message("✅ Rerolled.", ephemeral=True)

    @giveaway.command(name="list", description="List giveaways.")
    async def gw_list(interaction: discord.Interaction):
        rows = await db.fetchall(
            "SELECT * FROM giveaways WHERE guild_id=? ORDER BY id DESC",
            (interaction.guild.id,))
        lines = [f"• `#{r['id']}` **{r['prize']}** — "
                 f"{'ended' if r['ended'] else fmt_dt(parse_iso(r['ends_at']))}"
                 for r in rows]
        await interaction.response.send_message(
            "\n".join(lines) or "*(none)*", ephemeral=True)

    # ---------------- CUSTOM COMMANDS ----------------
    cc = app_commands.Group(name="customcommand", description="Custom text commands")
    tree.add_command(cc)

    @cc.command(name="create", description="Create a custom command.")
    async def cc_create(interaction: discord.Interaction, name: str,
                        response: str, embed: bool = False):
        if not await require_admin(interaction): return
        await db.execute(
            "INSERT INTO custom_commands (guild_id, name, response, embed, enabled) "
            "VALUES (?, ?, ?, ?, 1) ON CONFLICT(guild_id, name) DO UPDATE SET "
            "response=excluded.response, embed=excluded.embed",
            (interaction.guild.id, name.lower(), response, 1 if embed else 0))
        await interaction.response.send_message(
            f"✅ Created `!{name}`.", ephemeral=True)

    @cc.command(name="delete", description="Delete a custom command.")
    async def cc_delete(interaction: discord.Interaction, name: str):
        if not await require_admin(interaction): return
        await db.execute("DELETE FROM custom_commands WHERE guild_id=? AND name=?",
                         (interaction.guild.id, name.lower()))
        await interaction.response.send_message("✅ Deleted.", ephemeral=True)

    @cc.command(name="list", description="List custom commands.")
    async def cc_list(interaction: discord.Interaction):
        rows = await db.fetchall(
            "SELECT name, enabled FROM custom_commands WHERE guild_id=?",
            (interaction.guild.id,))
        lines = [f"• `!{r['name']}` {'🟢' if r['enabled'] else '⚪'}" for r in rows]
        await interaction.response.send_message(
            "\n".join(lines) or "*(none)*", ephemeral=True)

    # ---------------- ANNOUNCEMENTS ----------------
    ann = app_commands.Group(name="announce", description="Announcements")
    tree.add_command(ann)

    @ann.command(name="create", description="Create an announcement draft.")
    async def a_create(interaction: discord.Interaction, channel: discord.TextChannel,
                       title: str, body: str,
                       image: Optional[str] = None, footer: Optional[str] = None):
        if not await require_admin(interaction): return
        aid = await db.execute(
            "INSERT INTO announcements (guild_id, channel_id, title, body, "
            "image, footer, sent) VALUES (?, ?, ?, ?, ?, ?, 0)",
            (interaction.guild.id, channel.id, title, body, image, footer))
        await interaction.response.send_message(
            f"✅ Announcement `#{aid}` created.", ephemeral=True)

    @ann.command(name="send", description="Send an announcement now.")
    async def a_send(interaction: discord.Interaction, announcement_id: int):
        if not await require_admin(interaction): return
        await _run_announcement(bot, {"announcement_id": announcement_id})
        await interaction.response.send_message("✅ Sent.", ephemeral=True)

    @ann.command(name="schedule", description="Schedule an announcement.")
    async def a_schedule(interaction: discord.Interaction, announcement_id: int,
                         in_duration: str):
        if not await require_admin(interaction): return
        secs = parse_duration(in_duration)
        if not secs:
            return await interaction.response.send_message(
                "Invalid duration.", ephemeral=True)
        run_at = now_utc() + timedelta(seconds=secs)
        await db.execute(
            "UPDATE announcements SET scheduled_at=? WHERE id=? AND guild_id=?",
            (iso(run_at), announcement_id, interaction.guild.id))
        await bot.scheduler.schedule("announcement", run_at,
                                     {"announcement_id": announcement_id},
                                     interaction.guild.id)
        await interaction.response.send_message(
            f"✅ Scheduled for {fmt_dt(run_at)}.", ephemeral=True)

    @ann.command(name="cancel", description="Cancel a scheduled announcement.")
    async def a_cancel(interaction: discord.Interaction, announcement_id: int):
        if not await require_admin(interaction): return
        await db.execute(
            "UPDATE scheduled_tasks SET completed=1 WHERE task_type='announcement' "
            "AND completed=0 AND payload LIKE ?",
            (f'%"announcement_id": {announcement_id}%',))
        await interaction.response.send_message(
            "✅ Cancelled.", ephemeral=True)


# =====================================================================
# Entry point
# =====================================================================

async def main():
    if not TOKEN:
        log.error("DISCORD_TOKEN missing. Set it in your environment / .env file.")
        return
    bot = Freakos()
    try:
        await bot.start(TOKEN)
    except KeyboardInterrupt:
        pass
    finally:
        await bot.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
