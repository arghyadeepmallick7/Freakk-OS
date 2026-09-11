"""
FREAKOS — Production-ready single-file Discord bot.
Uses discord.py 2.x (NOT py-cord). Everything is inside this file.
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import random
import re
import traceback
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


def fmt_short(dt: Optional[datetime]) -> str:
    if not dt:
        return "—"
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M")


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
    """Parse 10s, 5m, 2h, 1d, 1h30m into seconds."""
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


async def send_long(interaction_or_channel, content: str, **kw):
    chunks = chunk_message(content)
    target = interaction_or_channel
    sent = []
    for i, c in enumerate(chunks):
        if i == 0 and hasattr(target, "response"):
            if not target.response.is_done():
                await target.response.send_message(c, **kw)
                sent.append(None)
                continue
            else:
                sent.append(await target.followup.send(c, **kw))
        else:
            if hasattr(target, "followup"):
                sent.append(await target.followup.send(c, **kw))
            else:
                sent.append(await target.send(c, **kw))
    return sent


def make_embed(title: str = None, description: str = None, color: int = 0x5865F2) -> discord.Embed:
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


def parse_hex_color(s: Optional[str]) -> Optional[int]:
    if not s:
        return None
    try:
        return int(s.replace("#", "").replace("0x", ""), 16)
    except Exception:
        return None


def sanitize_channel_name(name: str, fallback: str = "ticket") -> str:
    name = (name or "").lower()
    name = re.sub(r"[^a-z0-9\-_]+", "-", name)
    name = re.sub(r"-+", "-", name).strip("-")
    return (name or fallback)[:90]


# =====================================================================
# Database
# =====================================================================

SCHEMA = """
CREATE TABLE IF NOT EXISTS guild_config (
    guild_id INTEGER NOT NULL,
    key TEXT NOT NULL,
    value TEXT,
    PRIMARY KEY (guild_id, key)
);
CREATE TABLE IF NOT EXISTS warnings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    moderator_id INTEGER NOT NULL,
    reason TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    moderator_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    reason TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS timeouts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    moderator_id INTEGER NOT NULL,
    reason TEXT,
    ends_at TEXT NOT NULL,
    dm_sent INTEGER DEFAULT 0
);
-- ===================== TICKETS =====================
CREATE TABLE IF NOT EXISTS tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    type TEXT,
    status TEXT DEFAULT 'open',
    claimed_by INTEGER,
    priority TEXT DEFAULT 'normal',
    created_at TEXT NOT NULL,
    closed_at TEXT,
    closed_by INTEGER,
    close_reason TEXT,
    last_activity TEXT
);
CREATE TABLE IF NOT EXISTS ticket_types (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    emoji TEXT,
    category_id INTEGER,
    support_role_id INTEGER,
    message TEXT,
    banner TEXT,
    thumbnail TEXT,
    button_label TEXT,
    button_emoji TEXT,
    ping_support INTEGER DEFAULT 1,
    max_tickets INTEGER DEFAULT 0,
    sort_order INTEGER DEFAULT 0,
    UNIQUE(guild_id, name)
);
CREATE TABLE IF NOT EXISTS ticket_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id INTEGER NOT NULL,
    author_id INTEGER NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ticket_blacklist (
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    reason TEXT,
    added_by INTEGER,
    created_at TEXT NOT NULL,
    PRIMARY KEY (guild_id, user_id)
);
CREATE TABLE IF NOT EXISTS ticket_feedback (
    ticket_id INTEGER PRIMARY KEY,
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    rating INTEGER NOT NULL,
    comment TEXT,
    created_at TEXT NOT NULL
);
-- ===================== OTHER =====================
CREATE TABLE IF NOT EXISTS vouches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    voucher_id INTEGER NOT NULL,
    comment TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS shop_products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    description TEXT,
    price REAL NOT NULL,
    stock INTEGER DEFAULT -1,
    active INTEGER DEFAULT 1,
    image TEXT
);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    product_id INTEGER NOT NULL,
    quantity INTEGER NOT NULL,
    price REAL NOT NULL,
    status TEXT DEFAULT 'pending',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reaction_panels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    message_id INTEGER,
    title TEXT,
    description TEXT,
    mode TEXT DEFAULT 'button'
);
CREATE TABLE IF NOT EXISTS reaction_roles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    panel_id INTEGER NOT NULL,
    role_id INTEGER NOT NULL,
    label TEXT,
    emoji TEXT,
    UNIQUE(panel_id, role_id)
);
CREATE TABLE IF NOT EXISTS giveaways (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    message_id INTEGER,
    prize TEXT NOT NULL,
    winners INTEGER DEFAULT 1,
    host_id INTEGER,
    ends_at TEXT NOT NULL,
    ended INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS giveaway_entries (
    giveaway_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    PRIMARY KEY (giveaway_id, user_id)
);
CREATE TABLE IF NOT EXISTS custom_commands (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    response TEXT,
    embed INTEGER DEFAULT 0,
    enabled INTEGER DEFAULT 1,
    UNIQUE(guild_id, name)
);
CREATE TABLE IF NOT EXISTS announcements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    title TEXT,
    body TEXT,
    image TEXT,
    footer TEXT,
    scheduled_at TEXT,
    sent INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS scheduled_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_type TEXT NOT NULL,
    guild_id INTEGER,
    payload TEXT,
    run_at TEXT NOT NULL,
    completed INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_tasks_due ON scheduled_tasks(completed, run_at);
CREATE INDEX IF NOT EXISTS idx_tickets_guild_status ON tickets(guild_id, status);
CREATE INDEX IF NOT EXISTS idx_tickets_channel ON tickets(channel_id);
CREATE INDEX IF NOT EXISTS idx_ticket_types_guild ON ticket_types(guild_id);
"""

# Columns that might be missing on existing DBs — applied idempotently.
MIGRATIONS = [
    # tickets
    ("tickets", "priority", "TEXT DEFAULT 'normal'"),
    ("tickets", "closed_at", "TEXT"),
    ("tickets", "closed_by", "INTEGER"),
    ("tickets", "close_reason", "TEXT"),
    ("tickets", "last_activity", "TEXT"),
    # ticket_types
    ("ticket_types", "banner", "TEXT"),
    ("ticket_types", "thumbnail", "TEXT"),
    ("ticket_types", "button_label", "TEXT"),
    ("ticket_types", "button_emoji", "TEXT"),
    ("ticket_types", "ping_support", "INTEGER DEFAULT 1"),
    ("ticket_types", "max_tickets", "INTEGER DEFAULT 0"),
    ("ticket_types", "sort_order", "INTEGER DEFAULT 0"),
]


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
        # Idempotent migrations for older DBs
        for table, col, coltype in MIGRATIONS:
            try:
                await self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}")
            except Exception:
                pass
        await self.conn.commit()
        log.info("Database ready at %s", self.path)

    async def close(self):
        if self.conn:
            await self.conn.close()

    async def execute(self, sql: str, params: tuple = ()) -> int:
        async with self._lock:
            cur = await self.conn.execute(sql, params)
            await self.conn.commit()
            return cur.lastrowid

    async def executemany(self, sql: str, seq):
        async with self._lock:
            cur = await self.conn.executemany(sql, seq)
            await self.conn.commit()
            return cur.rowcount

    async def fetchone(self, sql: str, params: tuple = ()) -> Optional[aiosqlite.Row]:
        async with self._lock:
            cur = await self.conn.execute(sql, params)
            return await cur.fetchone()

    async def fetchall(self, sql: str, params: tuple = ()) -> list[aiosqlite.Row]:
        async with self._lock:
            cur = await self.conn.execute(sql, params)
            return await cur.fetchall()

    # ---- config helpers ----
    async def get_config(self, guild_id: int, key: str, default=None):
        row = await self.fetchone(
            "SELECT value FROM guild_config WHERE guild_id=? AND key=?",
            (guild_id, key),
        )
        return row["value"] if row else default

    async def set_config(self, guild_id: int, key: str, value):
        if value is None:
            await self.execute(
                "DELETE FROM guild_config WHERE guild_id=? AND key=?",
                (guild_id, key),
            )
        else:
            await self.execute(
                "INSERT INTO guild_config (guild_id, key, value) VALUES (?, ?, ?) "
                "ON CONFLICT(guild_id, key) DO UPDATE SET value=excluded.value",
                (guild_id, key, str(value)),
            )

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

    async def schedule(self, task_type: str, run_at: datetime, payload: dict = None,
                       guild_id: int = None) -> int:
        tid = await self.bot.db.execute(
            "INSERT INTO scheduled_tasks (task_type, guild_id, payload, run_at) "
            "VALUES (?, ?, ?, ?)",
            (task_type, guild_id, json.dumps(payload or {}), iso(run_at)),
        )
        self._wake.set()
        return tid

    def wake(self):
        self._wake.set()

    async def _run(self):
        await self.bot.wait_until_ready()
        while True:
            try:
                row = await self.bot.db.fetchone(
                    "SELECT * FROM scheduled_tasks WHERE completed=0 "
                    "ORDER BY run_at ASC LIMIT 1"
                )
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
                        await asyncio.wait_for(self._wake.wait(), timeout=min(delay, 60))
                    except asyncio.TimeoutError:
                        pass
                    continue
                await self._execute(row)
                await self.bot.db.execute(
                    "UPDATE scheduled_tasks SET completed=1 WHERE id=?",
                    (row["id"],),
                )
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
# Embeds helper
# =====================================================================

def build_embed_from_config(cfg: dict, defaults: discord.Embed) -> discord.Embed:
    e = defaults
    if cfg.get("title"):
        e.title = str(cfg["title"])[:256]
    if cfg.get("color") is not None:
        try:
            e.color = discord.Color(int(cfg["color"]))
        except Exception:
            pass
    if cfg.get("footer"):
        e.set_footer(text=str(cfg["footer"])[:2048])
    if cfg.get("image"):
        e.set_image(url=str(cfg["image"]))
    if cfg.get("thumbnail"):
        e.set_thumbnail(url=str(cfg["thumbnail"]))
    return e


# =====================================================================
# TICKET SYSTEM — Constants
# =====================================================================

TICKET_PRIORITY_INFO = {
    "low":    ("🟢", "Low",    0x95A5A6),
    "normal": ("🔵", "Normal", 0x5865F2),
    "high":   ("🟠", "High",   0xE67E22),
    "urgent": ("🔴", "Urgent", 0xED4245),
}

TICKET_STATUS_COLOR = {
    "open":    0x57F287,
    "claimed": 0xFEE75C,
    "closed":  0xED4245,
}


def priority_choices() -> list[app_commands.Choice]:
    return [app_commands.Choice(name=f"{v[0]} {v[1]}", value=k)
            for k, v in TICKET_PRIORITY_INFO.items()]


def ticket_type_choices_factory(bot: "Freakos"):
    async def _choices(interaction: discord.Interaction,
                       current: str) -> list[app_commands.Choice[str]]:
        if not interaction.guild:
            return []
        rows = await bot.db.fetchall(
            "SELECT name FROM ticket_types WHERE guild_id=? ORDER BY sort_order, id",
            (interaction.guild.id,),
        )
        return [app_commands.Choice(name=r["name"], value=r["name"])
                for r in rows if current.lower() in r["name"].lower()][:25]
    return _choices


# =====================================================================
# TICKET SYSTEM — Persistent views
# =====================================================================

class TicketCreateButton(discord.ui.View):
    """Persistent 'Open Ticket' panel button. Shows a type-picker on click (or
    opens directly if the guild has a single type)."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Open Ticket", style=discord.ButtonStyle.primary,
                       custom_id="freakos:ticket:create", emoji="🎫")
    async def create(self, interaction: discord.Interaction, _):
        guild = interaction.guild
        if not guild:
            return
        rows = await interaction.client.db.fetchall(
            "SELECT name, emoji, button_emoji FROM ticket_types "
            "WHERE guild_id=? ORDER BY sort_order, id",
            (guild.id,),
        )
        if not rows:
            return await interaction.response.send_message(
                "⚠️ No ticket types configured yet. Ask an admin to run `/ticket type`.",
                ephemeral=True,
            )

        # Direct open if only one type
        if len(rows) == 1:
            return await _open_ticket(interaction, rows[0]["name"])

        opts = []
        for r in rows[:25]:
            emoji = r["button_emoji"] or r["emoji"] or None
            opts.append(discord.SelectOption(
                label=r["name"][:100],
                emoji=emoji,
                value=r["name"][:100],
            ))

        select = discord.ui.Select(placeholder="Select a ticket type…", options=opts)

        async def cb(sel_interaction: discord.Interaction):
            await _open_ticket(sel_interaction, sel_interaction.data["values"][0])

        select.callback = cb
        view = discord.ui.View(timeout=120)
        view.add_item(select)
        await interaction.response.send_message(
            "Please select a ticket type:", view=view, ephemeral=True
        )


class TicketPanelModal(discord.ui.Modal, title="Ticket Panel"):
    """Multi-line panel builder for the /ticket panel command."""

    panel_title = discord.ui.TextInput(
        label="Title", style=discord.TextStyle.short,
        max_length=256, required=False,
        placeholder="Support Tickets",
    )
    panel_description = discord.ui.TextInput(
        label="Description", style=discord.TextStyle.paragraph,
        max_length=4000, required=False,
        placeholder="Click the button below to open a ticket.\nChoose a category that fits your issue best.",
    )
    panel_banner = discord.ui.TextInput(
        label="Banner image URL", style=discord.TextStyle.short,
        max_length=400, required=False,
        placeholder="https://… (leave blank to skip)",
    )
    panel_thumbnail = discord.ui.TextInput(
        label="Thumbnail image URL", style=discord.TextStyle.short,
        max_length=400, required=False,
    )
    panel_color = discord.ui.TextInput(
        label="Embed color (hex)", style=discord.TextStyle.short,
        max_length=10, required=False, placeholder="#5865F2",
    )

    def __init__(self, channel: discord.TextChannel):
        super().__init__()
        self.channel = channel

    async def on_submit(self, interaction: discord.Interaction):
        color = parse_hex_color(self.panel_color.value) or 0x5865F2
        embed = make_embed(
            title=self.panel_title.value or "🎫 Support Tickets",
            description=self.panel_description.value or
                "Click the button below to open a ticket.\n"
                "Please describe your issue clearly once the ticket is created.",
            color=color,
        )
        if self.panel_banner.value:
            embed.set_image(url=self.panel_banner.value.strip())
        if self.panel_thumbnail.value:
            embed.set_thumbnail(url=self.panel_thumbnail.value.strip())
        embed.set_footer(text=interaction.guild.name if interaction.guild else "")

        try:
            msg = await self.channel.send(embed=embed, view=TicketCreateButton())
        except discord.Forbidden:
            return await interaction.response.send_message(
                "❌ I can't post in that channel.", ephemeral=True
            )

        # Persist metadata for re-posting without re-entering details
        if interaction.guild:
            await interaction.client.db.set_json(interaction.guild.id, "ticket.panel", {
                "title": self.panel_title.value or "",
                "description": self.panel_description.value or "",
                "banner": self.panel_banner.value or "",
                "thumbnail": self.panel_thumbnail.value or "",
                "color": color,
                "channel_id": self.channel.id,
                "message_id": msg.id,
            })
        await interaction.response.send_message(
            f"✅ Ticket panel posted in {self.channel.mention}.", ephemeral=True
        )


class TicketManageView(discord.ui.View):
    """Buttons inside a ticket channel. Uses per-ticket custom_ids so multiple
    open tickets resolve to their own view instances correctly."""

    def __init__(self, ticket_id: int):
        super().__init__(timeout=None)
        self.ticket_id = ticket_id
        tid = ticket_id

        claim = discord.ui.Button(
            label="Claim", style=discord.ButtonStyle.success,
            custom_id=f"freakos:ticket:claim:{tid}", emoji="✋",
        )
        claim.callback = self._claim
        self.add_item(claim)

        close = discord.ui.Button(
            label="Close", style=discord.ButtonStyle.danger,
            custom_id=f"freakos:ticket:close:{tid}", emoji="🔒",
        )
        close.callback = self._close
        self.add_item(close)

        transcript = discord.ui.Button(
            label="Transcript", style=discord.ButtonStyle.secondary,
            custom_id=f"freakos:ticket:transcript:{tid}", emoji="📄",
        )
        transcript.callback = self._transcript
        self.add_item(transcript)

        adduser = discord.ui.Button(
            label="Add User", style=discord.ButtonStyle.secondary,
            custom_id=f"freakos:ticket:add:{tid}", emoji="➕",
        )
        adduser.callback = self._add_user
        self.add_item(adduser)

    async def _fetch(self, interaction: discord.Interaction):
        return await interaction.client.db.fetchone(
            "SELECT * FROM tickets WHERE id=?", (self.ticket_id,)
        )

    async def _claim(self, interaction: discord.Interaction):
        row = await self._fetch(interaction)
        if not row:
            return await interaction.response.send_message("Ticket not found.", ephemeral=True)
        if not is_admin_or_mod(interaction.user):
            return await interaction.response.send_message("Staff only.", ephemeral=True)
        if row["claimed_by"] == interaction.user.id:
            return await interaction.response.send_message("Already claimed by you.", ephemeral=True)
        await interaction.client.db.execute(
            "UPDATE tickets SET claimed_by=?, status='claimed' WHERE id=?",
            (interaction.user.id, self.ticket_id),
        )
        await interaction.response.send_message(
            f"✅ Ticket claimed by {interaction.user.mention}"
        )
        await _log_ticket(interaction.client, interaction.guild, row,
                          "Ticket Claimed", f"Claimed by {interaction.user.mention}")

    async def _close(self, interaction: discord.Interaction):
        row = await self._fetch(interaction)
        if not row:
            return await interaction.response.send_message("Ticket not found.", ephemeral=True)
        if interaction.user.id != row["user_id"] and not is_admin_or_mod(interaction.user):
            return await interaction.response.send_message("Not allowed.", ephemeral=True)
        allow_user = await interaction.client.db.get_config(
            interaction.guild.id, "ticket.allow_user_close", "1")
        if interaction.user.id == row["user_id"] and allow_user != "1":
            return await interaction.response.send_message(
                "Users can't close their own tickets here.", ephemeral=True)
        await interaction.response.defer()
        await _close_ticket(interaction.client, self.ticket_id, interaction.user,
                            "Closed via button")

    async def _transcript(self, interaction: discord.Interaction):
        if not is_admin_or_mod(interaction.user):
            return await interaction.response.send_message("Staff only.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        text = await _build_transcript(interaction.channel)
        buf = io.BytesIO(text.encode("utf-8"))
        await interaction.followup.send(
            "📄 Transcript:",
            file=discord.File(buf, filename=f"transcript-{interaction.channel.id}.txt"),
            ephemeral=True,
        )

    async def _add_user(self, interaction: discord.Interaction):
        if not is_admin_or_mod(interaction.user):
            return await interaction.response.send_message("Staff only.", ephemeral=True)
        await interaction.response.send_modal(TicketAddUserModal(self.ticket_id))


class TicketAddUserModal(discord.ui.Modal, title="Add User to Ticket"):
    user_id = discord.ui.TextInput(label="User ID or mention", max_length=40)

    def __init__(self, ticket_id: int):
        super().__init__()
        self.ticket_id = ticket_id

    async def on_submit(self, interaction: discord.Interaction):
        raw = self.user_id.value.strip()
        m = re.search(r"(\d{15,25})", raw)
        if not m:
            return await interaction.response.send_message("Invalid user ID.", ephemeral=True)
        uid = int(m.group(1))
        member = interaction.guild.get_member(uid)
        if not member:
            return await interaction.response.send_message("Member not found.", ephemeral=True)
        await interaction.channel.set_permissions(
            member, view_channel=True, send_messages=True,
            read_message_history=True, attach_files=True)
        await interaction.response.send_message(f"✅ Added {member.mention}.")


# =====================================================================
# TICKET SYSTEM — Core operations
# =====================================================================

async def _get_ticket_by_channel(db: Database, channel_id: int):
    return await db.fetchone("SELECT * FROM tickets WHERE channel_id=?", (channel_id,))


async def _ticket_support_role(guild: discord.Guild, row) -> Optional[discord.Role]:
    if not row or not row["support_role_id"]:
        return None
    return guild.get_role(row["support_role_id"])


async def _open_ticket(interaction: discord.Interaction, ttype: str):
    guild = interaction.guild
    if not guild:
        return
    db: Database = interaction.client.db

    row = await db.fetchone(
        "SELECT * FROM ticket_types WHERE guild_id=? AND name=?",
        (guild.id, ttype),
    )
    if not row:
        return await interaction.response.send_message("Ticket type not found.", ephemeral=True)

    # Blacklist
    bl = await db.fetchone(
        "SELECT * FROM ticket_blacklist WHERE guild_id=? AND user_id=?",
        (guild.id, interaction.user.id),
    )
    if bl:
        reason = bl["reason"] or "No reason provided."
        return await interaction.response.send_message(
            f"🚫 You are blacklisted from opening tickets.\n**Reason:** {reason}",
            ephemeral=True,
        )

    # Global + per-type limit
    global_limit = int(await db.get_config(guild.id, "ticket.limit", "1") or 1)
    type_limit = row["max_tickets"] if row["max_tickets"] and row["max_tickets"] > 0 else global_limit

    total_open = await db.fetchone(
        "SELECT COUNT(*) c FROM tickets WHERE guild_id=? AND user_id=? AND status != 'closed'",
        (guild.id, interaction.user.id),
    )
    if total_open and total_open["c"] >= global_limit:
        return await interaction.response.send_message(
            f"You already have {total_open['c']} open ticket(s). Maximum is {global_limit}.",
            ephemeral=True,
        )

    type_open = await db.fetchone(
        "SELECT COUNT(*) c FROM tickets WHERE guild_id=? AND user_id=? AND type=? "
        "AND status != 'closed'",
        (guild.id, interaction.user.id, ttype),
    )
    if type_open and type_open["c"] >= type_limit:
        return await interaction.response.send_message(
            f"You already have {type_open['c']} open `{ttype}` ticket(s).", ephemeral=True,
        )

    # Cooldown
    cooldown = int(await db.get_config(guild.id, "ticket.cooldown", "0") or 0)
    if cooldown > 0:
        last = await db.fetchone(
            "SELECT created_at FROM tickets WHERE guild_id=? AND user_id=? "
            "ORDER BY id DESC LIMIT 1",
            (guild.id, interaction.user.id),
        )
        if last:
            last_dt = parse_iso(last["created_at"])
            if last_dt and (now_utc() - last_dt).total_seconds() < cooldown:
                rem = cooldown - int((now_utc() - last_dt).total_seconds())
                return await interaction.response.send_message(
                    f"⏳ Cooldown: try again in {fmt_duration(rem)}.", ephemeral=True,
                )

    # Category + support role
    category = guild.get_channel(row["category_id"]) if row["category_id"] else None
    if not category and await db.get_config(guild.id, "ticket.category"):
        category = guild.get_channel(int(await db.get_config(guild.id, "ticket.category")))
    support_role = guild.get_role(row["support_role_id"]) if row["support_role_id"] else None
    if not support_role and await db.get_config(guild.id, "ticket.support_role"):
        support_role = guild.get_role(int(await db.get_config(guild.id, "ticket.support_role")))

    # Channel name
    naming = await db.get_config(guild.id, "ticket.naming") or "ticket-{user}"
    placeholder_name = apply_placeholders(
        naming,
        user=interaction.user.name,
        username=interaction.user.name,
        display_name=interaction.user.display_name,
        type=ttype,
        id="0",
    )
    temp_name = sanitize_channel_name(placeholder_name, fallback="ticket")

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        interaction.user: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, attach_files=True,
            read_message_history=True, embed_links=True,
        ),
        guild.me: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, manage_channels=True,
            manage_permissions=True, attach_files=True, embed_links=True,
            read_message_history=True,
        ),
    }
    if support_role:
        overwrites[support_role] = discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True,
            attach_files=True, embed_links=True, manage_messages=True,
        )

    try:
        channel = await guild.create_text_channel(
            name=temp_name,
            category=category,
            overwrites=overwrites,
            topic=f"Ticket for {interaction.user} • Type: {ttype}",
            reason=f"Ticket opened by {interaction.user} ({ttype})",
        )
    except discord.Forbidden:
        return await interaction.response.send_message(
            "❌ I lack permission to create the ticket channel. Check my role position and `Manage Channels`.",
            ephemeral=True,
        )

    tid = await db.execute(
        "INSERT INTO tickets (guild_id, channel_id, user_id, type, status, "
        "created_at, last_activity) VALUES (?, ?, ?, ?, 'open', ?, ?)",
        (guild.id, channel.id, interaction.user.id, ttype, iso(now_utc()), iso(now_utc())),
    )

    # Rename to final name (with ticket id)
    try:
        final_name = sanitize_channel_name(
            apply_placeholders(
                naming,
                user=interaction.user.name,
                username=interaction.user.name,
                display_name=interaction.user.display_name,
                type=ttype,
                id=str(tid),
            ),
            fallback=f"ticket-{tid}",
        )
        if final_name != temp_name:
            await channel.edit(name=final_name, reason="Ticket name finalised")
    except Exception:
        pass

    # Compose intro message
    intro = row["message"] or (
        f"Hey {interaction.user.mention}, thanks for opening a **{ttype}** ticket!\n\n"
        "Support will be with you shortly. Please describe your issue in detail."
    )
    rendered = apply_placeholders(
        intro,
        user=interaction.user.name,
        username=interaction.user.name,
        display_name=interaction.user.display_name,
        mention=interaction.user.mention,
        server=guild.name,
        type=ttype,
        id=str(tid),
        ticket_id=str(tid),
        timestamp=fmt_dt(now_utc()),
    )

    embed = discord.Embed(
        title=f"Ticket #{tid} — {ttype}",
        description=rendered[:4000],
        color=TICKET_STATUS_COLOR["open"],
        timestamp=now_utc(),
    )
    if row["banner"]:
        embed.set_image(url=row["banner"])
    elif row["thumbnail"]:
        embed.set_thumbnail(url=row["thumbnail"])
    embed.set_footer(text=f"Opened by {interaction.user} • Type: {ttype}")

    ping_roles = []
    if support_role and row["ping_support"]:
        ping_roles.append(support_role.mention)
    ping_content = " ".join(ping_roles) + (" | " + interaction.user.mention if ping_roles else interaction.user.mention)

    view = TicketManageView(tid)
    try:
        await channel.send(content=ping_content[:1900], embed=embed, view=view)
    except Exception:
        log.exception("Failed to send ticket intro")

    # DM on open
    if await db.get_config(guild.id, "ticket.dm_on_open", "0") == "1":
        try:
            dm_tpl = await db.get_config(guild.id, "ticket.dm_open_msg") or (
                "Your ticket **#{id}** ({type}) has been created in **{server}**.\n"
                "Channel: {channel_name}\n\nA member of our team will be with you shortly."
            )
            dm_text = apply_placeholders(
                dm_tpl, user=interaction.user.name, mention=interaction.user.mention,
                server=guild.name, type=ttype, id=str(tid),
                channel_name=channel.name, timestamp=fmt_dt(now_utc()),
            )
            for c in chunk_message(dm_text):
                await interaction.user.send(c)
        except Exception:
            pass

    # Initial activity timestamp
    await db.execute("UPDATE tickets SET last_activity=? WHERE id=?",
                     (iso(now_utc()), tid))

    await interaction.response.send_message(
        f"✅ Ticket created: {channel.mention}", ephemeral=True,
    )
    await _log_ticket(interaction.client, guild, {
        "id": tid, "user_id": interaction.user.id, "type": ttype,
        "channel_id": channel.id,
    }, "Ticket Opened",
        f"**User:** {interaction.user.mention}\n**Type:** `{ttype}`\n**Channel:** {channel.mention}")


async def _close_ticket(bot: "Freakos", ticket_id: int, closer: discord.abc.User,
                        reason: str = "No reason provided.", silent: bool = False):
    db: Database = bot.db
    row = await db.fetchone("SELECT * FROM tickets WHERE id=?", (ticket_id,))
    if not row:
        return
    guild = bot.get_guild(row["guild_id"])
    if not guild:
        return

    await db.execute(
        "UPDATE tickets SET status='closed', closed_at=?, closed_by=?, close_reason=? "
        "WHERE id=?",
        (iso(now_utc()), closer.id, reason, ticket_id),
    )

    channel = guild.get_channel(row["channel_id"])
    closing_msg = await db.get_config(guild.id, "ticket.close_message")
    if not closing_msg:
        closing_msg = (
            "🔒 **Ticket closed**\n"
            "**By:** {closer}\n**Reason:** {reason}\n\n"
            "This channel will be locked. Staff can reopen it with `/ticket reopen`."
        )
    rendered = apply_placeholders(
        closing_msg,
        closer=closer.mention if hasattr(closer, "mention") else str(closer),
        user=closer.name, reason=reason, id=str(ticket_id),
        timestamp=fmt_dt(now_utc()),
    )

    if channel:
        # Lock permissions for ticket opener
        opener = guild.get_member(row["user_id"])
        if opener:
            try:
                ow = channel.overwrites_for(opener)
                ow.send_messages = False
                await channel.set_permissions(opener, overwrite=ow)
            except Exception:
                pass
        try:
            await channel.send(rendered[:1900])
        except Exception:
            pass

    # Transcript
    transcript_text = None
    transcript_chan_id = await db.get_config(guild.id, "ticket.transcript_channel")
    if transcript_chan_id and channel:
        try:
            tchan = guild.get_channel(int(transcript_chan_id))
            if tchan:
                transcript_text = await _build_transcript(channel)
                buf = io.BytesIO(transcript_text.encode("utf-8"))
                e = make_embed(
                    title=f"📄 Transcript — Ticket #{ticket_id}",
                    description=(
                        f"**User:** <@{row['user_id']}>\n"
                        f"**Type:** `{row['type']}`\n"
                        f"**Closed by:** {closer.mention if hasattr(closer, 'mention') else closer}\n"
                        f"**Reason:** {reason}"
                    ),
                )
                await tchan.send(
                    embed=e,
                    file=discord.File(buf, filename=f"ticket-{ticket_id}.txt"),
                )
        except Exception:
            log.exception("Transcript failed")

    # DM on close
    if await db.get_config(guild.id, "ticket.dm_on_close", "0") == "1":
        try:
            user = bot.get_user(row["user_id"]) or await bot.fetch_user(row["user_id"])
            if user:
                dm_tpl = await db.get_config(guild.id, "ticket.dm_close_msg") or (
                    "Your ticket **#{id}** ({type}) in **{server}** has been closed.\n"
                    "**Reason:** {reason}"
                )
                dm_text = apply_placeholders(
                    dm_tpl, user=user.name, mention=user.mention, server=guild.name,
                    type=row["type"], id=str(ticket_id), reason=reason,
                    timestamp=fmt_dt(now_utc()),
                )
                for c in chunk_message(dm_text):
                    await user.send(c)
                if transcript_text:
                    buf = io.BytesIO(transcript_text.encode("utf-8"))
                    await user.send(file=discord.File(buf, filename=f"ticket-{ticket_id}.txt"))
        except Exception:
            pass

    await _log_ticket(bot, guild, row, "Ticket Closed",
                      f"**Ticket:** #{ticket_id}\n**By:** {closer.mention if hasattr(closer, 'mention') else closer}\n**Reason:** {reason}")


async def _build_transcript(channel: discord.TextChannel) -> str:
    lines = [
        "=" * 70,
        f"Ticket transcript — #{channel.name}",
        f"Guild: {channel.guild.name}",
        f"Generated: {fmt_dt(now_utc())}",
        "=" * 70,
        "",
    ]
    try:
        async for m in channel.history(limit=2000, oldest_first=True):
            ts = fmt_dt(m.created_at)
            author = f"{m.author} ({m.author.id})"
            content = m.content or ""
            if m.embeds:
                for e in m.embeds:
                    if e.title:
                        content += f"\n[embed] {e.title}"
                    if e.description:
                        content += f"\n[embed] {e.description[:200]}"
            if m.attachments:
                for a in m.attachments:
                    content += f"\n[attachment] {a.url}"
            lines.append(f"[{ts}] {author}:")
            for l in (content or "(no content)").split("\n"):
                lines.append(f"    {l}")
            lines.append("")
    except Exception:
        lines.append("(failed to fetch history)")
    return "\n".join(lines)


async def _log_ticket(bot: "Freakos", guild: discord.Guild, row, title: str, description: str):
    if not guild:
        return
    chan_id = await bot.db.get_config(guild.id, "ticket.log_channel")
    if not chan_id:
        return
    ch = guild.get_channel(int(chan_id))
    if not ch:
        return
    try:
        e = make_embed(title=f"🎫 {title}", description=description, color=0x5865F2)
        await ch.send(embed=e)
    except Exception:
        pass


# =====================================================================
# Reaction-role / Shop / Giveaway persistent views (unchanged)
# =====================================================================

class ReactionRoleView(discord.ui.View):
    def __init__(self, panel_id: int, roles: list[dict], mode: str = "button"):
        super().__init__(timeout=None)
        self.panel_id = panel_id
        if mode == "select":
            opts = [
                discord.SelectOption(
                    label=(r["label"] or f"Role {r['role_id']}")[:100],
                    value=str(r["role_id"]),
                    emoji=r.get("emoji") or None,
                )
                for r in roles[:25]
            ]
            if opts:
                sel = discord.ui.Select(
                    placeholder="Toggle a role…", options=opts,
                    custom_id=f"freakos:rr:select:{panel_id}",
                    min_values=0, max_values=len(opts),
                )
                sel.callback = self._select_cb
                self.add_item(sel)
        else:
            for r in roles[:24]:
                btn = discord.ui.Button(
                    label=(r["label"] or f"Role {r['role_id']}")[:80],
                    emoji=r.get("emoji") or None,
                    style=discord.ButtonStyle.secondary,
                    custom_id=f"freakos:rr:btn:{panel_id}:{r['role_id']}",
                )
                btn.callback = self._make_toggle(r["role_id"])
                self.add_item(btn)

    def _make_toggle(self, role_id: int):
        async def cb(interaction: discord.Interaction):
            await _toggle_role(interaction, role_id)
        return cb

    async def _select_cb(self, interaction: discord.Interaction):
        selected = {int(v) for v in interaction.data.get("values", [])}
        rows = await interaction.client.db.fetchall(
            "SELECT role_id FROM reaction_roles WHERE panel_id=?", (self.panel_id,)
        )
        all_ids = {r["role_id"] for r in rows}
        to_add = selected
        to_remove = all_ids - selected
        try:
            add_roles = [interaction.guild.get_role(i) for i in to_add if interaction.guild.get_role(i)]
            rem_roles = [interaction.guild.get_role(i) for i in to_remove if interaction.guild.get_role(i)]
            if add_roles:
                await interaction.user.add_roles(*add_roles, reason="Reaction role")
            if rem_roles:
                await interaction.user.remove_roles(*rem_roles, reason="Reaction role")
            await interaction.response.send_message("Roles updated.", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message("Missing permissions.", ephemeral=True)


async def _toggle_role(interaction: discord.Interaction, role_id: int):
    role = interaction.guild.get_role(role_id)
    if not role:
        return await interaction.response.send_message("Role no longer exists.", ephemeral=True)
    try:
        if role in interaction.user.roles:
            await interaction.user.remove_roles(role, reason="Reaction role")
            await interaction.response.send_message(f"Removed {role.mention}.", ephemeral=True)
        else:
            await interaction.user.add_roles(role, reason="Reaction role")
            await interaction.response.send_message(f"Added {role.mention}.", ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message(
            "I lack permission (check role hierarchy).", ephemeral=True
        )


class GiveawayView(discord.ui.View):
    def __init__(self, giveaway_id: int):
        super().__init__(timeout=None)
        self.giveaway_id = giveaway_id

    @discord.ui.button(label="Enter", style=discord.ButtonStyle.success, emoji="🎉",
                       custom_id="freakos:gw:enter")
    async def enter(self, interaction: discord.Interaction, _):
        gw = await interaction.client.db.fetchone(
            "SELECT * FROM giveaways WHERE id=?", (self.giveaway_id,)
        )
        if not gw or gw["ended"]:
            return await interaction.response.send_message("Giveaway ended.", ephemeral=True)
        await interaction.client.db.execute(
            "INSERT OR IGNORE INTO giveaway_entries (giveaway_id, user_id) VALUES (?, ?)",
            (self.giveaway_id, interaction.user.id),
        )
        await interaction.response.send_message("✅ Entered!", ephemeral=True)


class ShopPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Browse Products", style=discord.ButtonStyle.primary,
                       emoji="🛒", custom_id="freakos:shop:browse")
    async def browse(self, interaction: discord.Interaction, _):
        rows = await interaction.client.db.fetchall(
            "SELECT id, name, price, stock, active FROM shop_products WHERE guild_id=? ORDER BY id",
            (interaction.guild.id,),
        )
        if not rows:
            return await interaction.response.send_message("No products yet.", ephemeral=True)
        lines = []
        for r in rows:
            stock = "∞" if r["stock"] < 0 else r["stock"]
            flag = "✅" if r["active"] else "❌"
            lines.append(f"{flag} `#{r['id']}` **{r['name']}** — {r['price']} (stock {stock})")
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
            return await interaction.response.send_message("Invalid numbers.", ephemeral=True)
        db: Database = interaction.client.db
        p = await db.fetchone(
            "SELECT * FROM shop_products WHERE id=? AND guild_id=?",
            (pid, interaction.guild.id),
        )
        if not p or not p["active"]:
            return await interaction.response.send_message("Product not available.", ephemeral=True)
        if p["stock"] >= 0 and p["stock"] < qty:
            return await interaction.response.send_message("Not enough stock.", ephemeral=True)
        total = p["price"] * qty
        oid = await db.execute(
            "INSERT INTO orders (guild_id, user_id, product_id, quantity, price, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)",
            (interaction.guild.id, interaction.user.id, pid, qty, total,
             iso(now_utc()), iso(now_utc())),
        )
        if p["stock"] >= 0:
            await db.execute(
                "UPDATE shop_products SET stock=stock-? WHERE id=?", (qty, pid)
            )
        await interaction.response.send_message(
            f"✅ Order `#{oid}` created for {qty}× **{p['name']}** — total {total}.\n"
            "Await payment instructions from staff.",
            ephemeral=True,
        )
        await _log_guild(interaction.client, interaction.guild, "shop",
                         "New Order",
                         f"{interaction.user.mention} ordered `#{oid}` ({qty}× {p['name']}).")


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
# Scheduled task handlers
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
                    {"timeout_id": tid}, guild.id,
                )
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
                moderator=f"<@{row['moderator_id']}>",
            )
            try:
                for chunk in chunk_message(rendered):
                    await member.send(chunk)
            except Exception:
                pass
    await bot.db.execute("UPDATE timeouts SET dm_sent=1 WHERE id=?", (tid,))


async def _run_giveaway_end(bot: "Freakos", payload: dict):
    gid = payload.get("giveaway_id")
    if not gid:
        return
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


async def _end_giveaway(bot: "Freakos", gid: int, reroll: bool = False):
    row = await bot.db.fetchone("SELECT * FROM giveaways WHERE id=?", (gid,))
    if not row:
        return
    entries = await bot.db.fetchall(
        "SELECT user_id FROM giveaway_entries WHERE giveaway_id=?", (gid,)
    )
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
        self._vc_cache: dict[int, int] = {}
        self._spam_tracker: dict[tuple, list[float]] = {}
        self._autoclose_task: Optional[asyncio.Task] = None

    async def setup_hook(self):
        await self.db.connect()
        self.scheduler = Scheduler(self)
        self.scheduler.start()
        register_all_commands(self)
        await self._restore_persistent_views()
        await self._restore_scheduled_tasks()
        self._autoclose_task = asyncio.create_task(self._ticket_autoclose_loop(),
                                                    name="freakos-ticket-autoclose")

    async def _restore_persistent_views(self):
        # Global panels
        self.add_view(TicketCreateButton())
        self.add_view(ShopPanelView())
        # Per-ticket manage views
        rows = await self.db.fetchall("SELECT id FROM tickets WHERE status != 'closed'")
        for r in rows:
            self.add_view(TicketManageView(r["id"]))
        # Giveaways
        gws = await self.db.fetchall("SELECT id FROM giveaways WHERE ended=0")
        for g in gws:
            self.add_view(GiveawayView(g["id"]))
        # Reaction role panels
        panels = await self.db.fetchall("SELECT * FROM reaction_panels")
        for p in panels:
            roles = await self.db.fetchall(
                "SELECT role_id, label, emoji FROM reaction_roles WHERE panel_id=?",
                (p["id"],),
            )
            self.add_view(ReactionRoleView(
                p["id"], [dict(r) for r in roles], p["mode"] or "button",
            ))

    async def _restore_scheduled_tasks(self):
        rows = await self.db.fetchall("SELECT * FROM scheduled_tasks WHERE completed=0")
        log.info("Restored %d pending scheduled task(s)", len(rows))

    async def close(self):
        try:
            if self._autoclose_task:
                self._autoclose_task.cancel()
                try:
                    await self._autoclose_task
                except asyncio.CancelledError:
                    pass
            if self.scheduler:
                await self.scheduler.stop()
            await self.db.close()
        finally:
            await super().close()

    async def on_ready(self):
        log.info("Logged in as %s (%s) | guilds=%d", self.user, self.user.id, len(self.guilds))
        try:
            await self.change_presence(activity=discord.Activity(
                type=discord.ActivityType.watching, name="over the server"))
        except Exception:
            pass

    # ===== Ticket auto-close loop =====
    async def _ticket_autoclose_loop(self):
        await self.wait_until_ready()
        while True:
            try:
                await asyncio.sleep(300)  # every 5 minutes
                await self._check_autoclose()
            except asyncio.CancelledError:
                return
            except Exception:
                log.exception("Ticket autoclose loop error")

    async def _check_autoclose(self):
        for guild in self.guilds:
            raw = await self.db.get_config(guild.id, "ticket.auto_close")
            if not raw:
                continue
            try:
                hours = float(raw)
            except ValueError:
                continue
            if hours <= 0:
                continue
            cutoff = now_utc() - timedelta(hours=hours)
            rows = await self.db.fetchall(
                "SELECT * FROM tickets WHERE guild_id=? AND status != 'closed'",
                (guild.id,),
            )
            for row in rows:
                last = parse_iso(row["last_activity"] or row["created_at"])
                if last and last < cutoff:
                    channel = guild.get_channel(row["channel_id"])
                    if channel:
                        try:
                            await channel.send(
                                f"⏰ Auto-closing due to {hours:g}h of inactivity."
                            )
                        except Exception:
                            pass
                    await _close_ticket(self, row["id"], self.user,
                                        f"Auto-closed after {hours:g}h of inactivity")

    # ===== Task dispatch handlers =====
    async def task_timeout_end(self, payload: dict):
        await _run_timeout_end(self, payload)

    async def task_giveaway_end(self, payload: dict):
        await _run_giveaway_end(self, payload)

    async def task_announcement(self, payload: dict):
        await _run_announcement(self, payload)

    # ===== Events =====
    async def on_member_join(self, member: discord.Member):
        guild = member.guild
        enabled = await self.db.get_config(guild.id, "welcome.enabled", "0")
        if enabled == "1":
            ch_id = await self.db.get_config(guild.id, "welcome.channel")
            ch = guild.get_channel(int(ch_id)) if ch_id else None
            if ch:
                msg_tpl = await self.db.get_config(guild.id, "welcome.message") or \
                    "Welcome {mention} to **{server}**!"
                rendered = apply_placeholders(
                    msg_tpl, user=member.name, mention=member.mention,
                    username=member.name, display_name=member.display_name,
                    server=guild.name, member_count=str(guild.member_count),
                )
                try:
                    for c in chunk_message(rendered):
                        await ch.send(c)
                except Exception:
                    pass
        if await self.db.get_config(guild.id, "autorole.enabled", "0") == "1":
            roles = await self.db.get_json(guild.id, "autorole.roles", default=[])
            good = []
            for rid in roles:
                r = guild.get_role(rid)
                if r and r < guild.me.top_role and not r.managed:
                    good.append(r)
            if good:
                try:
                    await member.add_roles(*good, reason="Autorole")
                except Exception:
                    log.exception("Autorole failed")
        if await self.db.get_config(guild.id, "autonick.enabled", "0") == "1":
            fmt = await self.db.get_config(guild.id, "autonick.format") or "{user}"
            nick = apply_placeholders(
                fmt, user=member.name, username=member.name,
                display_name=member.display_name, server=guild.name,
            )[:32]
            try:
                if guild.me.guild_permissions.manage_nicknames and \
                        member.top_role < guild.me.top_role:
                    await member.edit(nick=nick, reason="Autonick")
            except Exception:
                pass
        await _log_guild(self, guild, "joins", "Member Joined",
                         f"{member.mention} ({member})")

    async def on_member_remove(self, member: discord.Member):
        guild = member.guild
        if await self.db.get_config(guild.id, "departure.enabled", "0") == "1":
            ch_id = await self.db.get_config(guild.id, "departure.channel")
            ch = guild.get_channel(int(ch_id)) if ch_id else None
            if ch:
                tpl = await self.db.get_config(guild.id, "departure.message") or \
                    "**{user}** left the server."
                rendered = apply_placeholders(
                    tpl, user=member.name, username=member.name,
                    display_name=member.display_name, mention=member.mention,
                    server=guild.name, member_count=str(guild.member_count),
                )
                try:
                    for c in chunk_message(rendered):
                        await ch.send(c)
                except Exception:
                    pass
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

    async def on_voice_state_update(self, member: discord.Member,
                                    before: discord.VoiceState,
                                    after: discord.VoiceState):
        guild = member.guild
        if member.bot:
            return
        old_id = before.channel.id if before.channel else None
        new_id = after.channel.id if after.channel else None
        if old_id == new_id:
            return
        self._vc_cache[member.id] = new_id or 0
        watch = await self.db.get_json(guild.id, "vcnotify.channels", default=[])
        if not watch:
            return
        if old_id and old_id in watch:
            cfg = await self.db.get_json(guild.id, f"vcnotify.cfg.{old_id}", default={})
            if cfg.get("enabled", True):
                target = guild.get_channel(cfg.get("target_channel_id") or 0)
                if target:
                    tpl = cfg.get("leave_msg") or "👋 {mention} left **{channel_name}**."
                    rendered = apply_placeholders(
                        tpl, mention=member.mention, user=member.name,
                        username=member.name, display_name=member.display_name,
                        channel_name=before.channel.name, server=guild.name,
                    )
                    try:
                        for c in chunk_message(rendered):
                            await target.send(c)
                    except Exception:
                        pass
        if new_id and new_id in watch:
            cfg = await self.db.get_json(guild.id, f"vcnotify.cfg.{new_id}", default={})
            if cfg.get("enabled", True):
                target = guild.get_channel(cfg.get("target_channel_id") or 0)
                if target:
                    tpl = cfg.get("join_msg") or "🎧 {mention} joined **{channel_name}**."
                    rendered = apply_placeholders(
                        tpl, mention=member.mention, user=member.name,
                        username=member.name, display_name=member.display_name,
                        channel_name=after.channel.name, server=guild.name,
                    )
                    try:
                        for c in chunk_message(rendered):
                            await target.send(c)
                    except Exception:
                        pass

    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return
        # Ticket activity tracking
        try:
            row = await self.db.fetchone(
                "SELECT id, status FROM tickets WHERE channel_id=?",
                (message.channel.id,),
            )
            if row and row["status"] != "closed":
                await self.db.execute(
                    "UPDATE tickets SET last_activity=? WHERE id=?",
                    (iso(now_utc()), row["id"]),
                )
        except Exception:
            pass

        # AutoMod
        try:
            await self._automod_check(message)
        except Exception:
            log.exception("automod error")

        # Custom commands (text prefix: !name)
        if message.content.startswith("!"):
            name = message.content[1:].split(" ", 1)[0].lower()
            row = await self.db.fetchone(
                "SELECT * FROM custom_commands WHERE guild_id=? AND name=? AND enabled=1",
                (message.guild.id, name),
            )
            if row:
                rendered = apply_placeholders(
                    row["response"] or "",
                    user=message.author.name, mention=message.author.mention,
                    username=message.author.name, display_name=message.author.display_name,
                    server=message.guild.name,
                )
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

    # ===== AutoMod =====
    async def _automod_check(self, message: discord.Message):
        guild = message.guild
        if await self.db.get_config(guild.id, "automod.enabled", "0") != "1":
            return
        if message.author.guild_permissions.administrator:
            return
        if message.author.id == self.user.id:
            return
        whitelist = await self.db.get_json(guild.id, "automod.whitelist", default={})
        wl_channels = set(whitelist.get("channels", []))
        wl_roles = set(whitelist.get("roles", []))
        if message.channel.id in wl_channels:
            return
        if any(r.id in wl_roles for r in message.author.roles):
            return

        settings = await self.db.get_json(guild.id, "automod.settings", default={})
        action = settings.get("action", "delete")
        violations = []

        if settings.get("invites", True) and re.search(
                r"(discord\.gg|discord\.com/invite|discordapp\.com/invite)/\S+",
                message.content, re.I):
            violations.append("invite link")

        if settings.get("links", True) and re.search(
                r"https?://\S+", message.content, re.I):
            if not re.search(r"(discord\.gg|discord\.com/invite|discordapp\.com/invite)/\S+",
                             message.content, re.I):
                violations.append("link")

        words = settings.get("words", [])
        low = message.content.lower()
        for w in words:
            if w and w.lower() in low:
                violations.append(f"banned word `{w}`")
                break

        if settings.get("mentions", True) and len(message.mentions) >= 5:
            violations.append("mention spam")

        if settings.get("antispam", True):
            key = (guild.id, message.author.id)
            now = now_utc().timestamp()
            arr = self._spam_tracker.setdefault(key, [])
            arr.append(now)
            self._spam_tracker[key] = [t for t in arr if now - t < 5]
            if len(self._spam_tracker[key]) >= 6:
                violations.append("spam")
                self._spam_tracker[key] = []

        if not violations:
            return

        try:
            if action in ("delete", "warn", "timeout", "kick", "ban"):
                try:
                    await message.delete()
                except Exception:
                    pass
            if action == "warn":
                await self.db.execute(
                    "INSERT INTO warnings (guild_id, user_id, moderator_id, reason, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (guild.id, message.author.id, self.user.id,
                     f"AutoMod: {', '.join(violations)}", iso(now_utc())),
                )
                try:
                    await message.channel.send(
                        f"{message.author.mention} warning: {', '.join(violations)}",
                        delete_after=5,
                    )
                except Exception:
                    pass
            elif action == "timeout":
                try:
                    await message.author.timeout(timedelta(minutes=10),
                                                 reason=f"AutoMod: {violations}")
                except Exception:
                    pass
            elif action == "kick":
                try:
                    await message.author.kick(reason=f"AutoMod: {violations}")
                except Exception:
                    pass
            elif action == "ban":
                try:
                    await message.author.ban(reason=f"AutoMod: {violations}",
                                             delete_message_days=1)
                except Exception:
                    pass
            await _log_guild(self, guild, "automod", "AutoMod Action",
                             f"{message.author.mention}: {', '.join(violations)} "
                             f"(action: {action})", color=0xE67E22)
        except Exception:
            log.exception("automod action failed")

    # ===== Error handler =====
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
        elif isinstance(original, (commands.MemberNotFound,)):
            msg = "❌ Member not found."
        elif isinstance(original, (commands.RoleNotFound,)):
            msg = "❌ Role not found."
        elif isinstance(original, (commands.ChannelNotFound,)):
            msg = "❌ Channel not found."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except Exception:
            pass


# =====================================================================
# Command registration
# =====================================================================

def register_all_commands(bot: Freakos):
    tree = bot.tree
    db = bot.db
    ttype_autocomplete = ticket_type_choices_factory(bot)

    # ---------- helpers ----------
    async def require_admin(interaction: discord.Interaction) -> bool:
        if not interaction.guild:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return False
        if not is_admin_or_mod(interaction.user):
            await interaction.response.send_message(
                "You need Manage Server / Administrator.", ephemeral=True)
            return False
        return True

    # ================= GENERAL =================
    @tree.command(name="ping", description="Show bot latency.")
    async def ping(interaction: discord.Interaction):
        await interaction.response.send_message(
            f"🏓 Pong! `{round(bot.latency*1000)}ms`"
        )

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
            "General": ["ping", "help", "serverinfo", "userinfo", "avatar", "setup"],
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
            "Moderation": ["warn", "warnings", "clearwarnings", "timeout", "untimeout",
                           "kick", "ban", "unban", "purge", "slowmode", "lock", "unlock",
                           "lockdown", "unlockdown"],
            "Logging": ["logging"],
            "Reaction Roles": ["reactionrole"],
            "Giveaways": ["giveaway"],
            "Custom Commands": ["customcommand"],
            "Announcements": ["announce"],
        }
        e = make_embed(title="FREAKOS — Commands",
                       description="Use `/` for slash commands, `!name` for custom commands.")
        for cat, names in cats.items():
            e.add_field(name=cat, value=", ".join(f"`/{n}`" for n in names), inline=False)
        await interaction.response.send_message(embed=e, ephemeral=True)

    # ================= SETUP WIZARD =================
    @tree.command(name="setup", description="Interactive setup wizard.")
    async def setup(interaction: discord.Interaction):
        if not await require_admin(interaction):
            return
        g = interaction.guild
        lines = []
        checks = [
            ("Welcome", await db.get_config(g.id, "welcome.enabled", "0") == "1"),
            ("Autorole", await db.get_config(g.id, "autorole.enabled", "0") == "1"),
            ("Autonick", await db.get_config(g.id, "autonick.enabled", "0") == "1"),
            ("Departure", await db.get_config(g.id, "departure.enabled", "0") == "1"),
            ("Action DMs", await db.get_config(g.id, "actiondm.enabled", "0") == "1"),
            ("VC Notifications", bool(await db.get_json(g.id, "vcnotify.channels", default=[]))),
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
            description=(
                "Use the subcommands of each group to configure systems:\n"
                "`/welcome`, `/autorole`, `/autonick`, `/departure`, `/actiondm`, "
                "`/vcnotify`, `/ticket`, `/vouch`, `/shop`, `/order`, `/notify`, "
                "`/automod`, `/logging`, `/reactionrole`, `/giveaway`, "
                "`/customcommand`, `/announce`.\n\n"
                "**Current status:**\n" + "\n".join(lines)
            ),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ================= WELCOME =================
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
    async def w_enable(interaction: discord.Interaction
