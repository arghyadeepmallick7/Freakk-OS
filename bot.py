"""
FREAKOS — production-ready single-file Discord bot.

Uses discord.py 2.x.  Everything (database, scheduler, views, commands,
persistent state) lives inside this file.  There are NO local imports of
`services`, `utils`, `cogs`, `modules`, `database`, `extensions`, or
`commands` folders.
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import random
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

# ============================================================
# CONFIGURATION
# ============================================================
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
DB_PATH = os.getenv("DB_PATH", "data/freakos.db").strip() or "data/freakos.db"

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("freakos")


# ============================================================
# FORMATTERS / HELPERS  (newline-safe — never collapse whitespace)
# ============================================================

def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(dt: Optional[datetime]) -> Optional[str]:
    if not dt:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def from_iso(s: Optional[str]) -> Optional[datetime]:
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


def fmt_dur(seconds: int) -> str:
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


_DUR_RE = re.compile(r"(\d+)\s*([smhdw])", re.I)

def parse_dur(s: Optional[str]) -> Optional[int]:
    if not s:
        return None
    total = 0
    matched = False
    for n, u in _DUR_RE.findall(s):
        matched = True
        total += int(n) * {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[u.lower()]
    return total if matched else None


_PH_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")

def render(template: Optional[str], **placeholders: Any) -> str:
    """
    Centralized placeholder renderer.

    - Never strips, splits, or joins — blank lines, indentation, and
      trailing whitespace are preserved exactly as the administrator
      entered them.
    - Unknown placeholders are left untouched (no crash).
    """
    if template is None:
        return ""
    def _sub(m: re.Match) -> str:
        key = m.group(1)
        if key in placeholders:
            v = placeholders[key]
            return "" if v is None else str(v)
        return m.group(0)
    return _PH_RE.sub(_sub, template)


def chunk_message(text: Optional[str], limit: int = 1990) -> list[str]:
    """
    Split a message into <= limit chunks WITHOUT altering its content.

    Prefers splitting on a newline boundary so the reconstruction of all
    returned chunks (in order) is byte-identical to the input.  Never
    strips, collapses, or joins-with-space.
    """
    if text is None:
        return [""]
    if text == "":
        return [""]
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    start = 0
    n = len(text)
    while start < n:
        end = start + limit
        if end >= n:
            chunks.append(text[start:])
            break
        cut = text.rfind("\n", start, end)
        if cut <= start:
            cut = end  # hard cut on a very long single line
        chunks.append(text[start:cut])
        start = cut  # keep the newline at the start of the next chunk
    return chunks or [""]


async def safe_send(target, content: Optional[str] = None, *,
                    embed: Optional[discord.Embed] = None,
                    view: Optional[discord.ui.View] = None,
                    ephemeral: bool = False):
    """
    Send `content` to a channel or interaction, splitting long messages
    on newline boundaries.  Handles both Channel and Interaction.
    """
    is_interaction = isinstance(target, discord.Interaction)
    if content is None and embed is None:
        return

    if content is None:
        if is_interaction:
            if target.response.is_done():
                await target.followup.send(embed=embed, view=view, ephemeral=ephemeral)
            else:
                await target.response.send_message(embed=embed, view=view, ephemeral=ephemeral)
        else:
            await target.send(embed=embed, view=view)
        return

    chunks = chunk_message(content)
    for i, c in enumerate(chunks):
        if is_interaction:
            if i == 0:
                if target.response.is_done():
                    await target.followup.send(c, embed=embed, view=view, ephemeral=ephemeral)
                else:
                    await target.response.send_message(c, embed=embed, view=view, ephemeral=ephemeral)
                embed = None
                view = None
            else:
                await target.followup.send(c, ephemeral=ephemeral)
        else:
            if i == 0:
                await target.send(c, embed=embed, view=view)
                embed = None
                view = None
            else:
                await target.send(c)


def make_embed(title: Optional[str] = None,
               description: Optional[str] = None,
               color: int = 0x5865F2) -> discord.Embed:
    e = discord.Embed(color=discord.Color(color))
    if title:
        e.title = title[:256]
    if description:
        e.description = description[:4096]
    e.timestamp = utcnow()
    return e


def is_admin_or_mod(member: discord.Member) -> bool:
    p = member.guild_permissions
    return p.administrator or p.manage_guild or p.moderate_members


def hierarchy_ok(guild: discord.Guild, target: discord.Member) -> tuple[bool, str]:
    """Can the bot act on `target`?"""
    me = guild.me
    if target.id == guild.owner_id:
        return False, "Cannot act on the server owner."
    if target.id == me.id:
        return False, "Cannot act on myself."
    if target.top_role >= me.top_role:
        return False, "Target's role is equal or higher than mine."
    return True, ""


def build_user_placeholders(user: discord.abc.User,
                            guild: Optional[discord.Guild] = None,
                            **extra: Any) -> dict:
    g = guild
    if g is None and isinstance(user, discord.Member):
        g = user.guild
    return {
        "user": user.name,
        "username": user.name,
        "display_name": getattr(user, "display_name", user.name),
        "mention": user.mention,
        "user_id": str(user.id),
        "server": g.name if g else "",
        "server_name": g.name if g else "",
        "server_id": str(g.id) if g else "",
        "member_count": str(g.member_count) if g and g.member_count else "",
        "timestamp": fmt_dt(utcnow()),
        **extra,
    }


# ============================================================
# DATABASE
# ============================================================

SCHEMA = """
CREATE TABLE IF NOT EXISTS guild_config (
    guild_id INTEGER NOT NULL,
    key      TEXT    NOT NULL,
    value    TEXT,
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
CREATE TABLE IF NOT EXISTS ticket_types (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    emoji TEXT,
    category_id INTEGER,
    support_role_id INTEGER,
    message TEXT,
    UNIQUE(guild_id, name)
);
CREATE TABLE IF NOT EXISTS tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    type TEXT,
    status TEXT DEFAULT 'open',
    claimed_by INTEGER,
    created_at TEXT NOT NULL
);
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
CREATE INDEX IF NOT EXISTS idx_tasks_due         ON scheduled_tasks(completed, run_at);
CREATE INDEX IF NOT EXISTS idx_warn_guild_user   ON warnings(guild_id, user_id);
CREATE INDEX IF NOT EXISTS idx_tickets_guild_ch  ON tickets(guild_id, channel_id);
CREATE INDEX IF NOT EXISTS idx_tickets_user_stat ON tickets(guild_id, user_id, status);
CREATE INDEX IF NOT EXISTS idx_vouches_guild_usr ON vouches(guild_id, user_id);
CREATE INDEX IF NOT EXISTS idx_orders_guild_usr  ON orders(guild_id, user_id);
CREATE INDEX IF NOT EXISTS idx_timeouts_ends     ON timeouts(dm_sent, ends_at);
"""


class Database:
    def __init__(self, path: str):
        self.path = path
        self.conn: Optional[aiosqlite.Connection] = None
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        d = os.path.dirname(os.path.abspath(self.path))
        if d:
            os.makedirs(d, exist_ok=True)
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute("PRAGMA journal_mode=WAL")
        await self.conn.execute("PRAGMA foreign_keys=ON")
        await self.conn.execute("PRAGMA busy_timeout=5000")
        await self.conn.executescript(SCHEMA)
        await self.conn.commit()
        log.info("Database ready: %s", os.path.abspath(self.path))

    async def close(self) -> None:
        if self.conn:
            try:
                await self.conn.close()
            except Exception:
                pass

    async def execute(self, sql: str, params: tuple = ()) -> int:
        async with self._lock:
            cur = await self.conn.execute(sql, params)
            await self.conn.commit()
            return cur.lastrowid or 0

    async def fetchone(self, sql: str, params: tuple = ()) -> Optional[aiosqlite.Row]:
        async with self._lock:
            cur = await self.conn.execute(sql, params)
            return await cur.fetchone()

    async def fetchall(self, sql: str, params: tuple = ()) -> list[aiosqlite.Row]:
        async with self._lock:
            cur = await self.conn.execute(sql, params)
            return await cur.fetchall()

    # ---- config helpers ----
    async def get_cfg(self, guild_id: int, key: str, default: Any = None) -> Any:
        row = await self.fetchone(
            "SELECT value FROM guild_config WHERE guild_id=? AND key=?",
            (guild_id, key),
        )
        return row["value"] if row else default

    async def set_cfg(self, guild_id: int, key: str, value: Any) -> None:
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

    async def get_json(self, guild_id: int, key: str, default: Any = None) -> Any:
        v = await self.get_cfg(guild_id, key)
        if v is None:
            return default
        try:
            return json.loads(v)
        except Exception:
            return default

    async def set_json(self, guild_id: int, key: str, value: Any) -> None:
        await self.set_cfg(guild_id, key, json.dumps(value))


# ============================================================
# SCHEDULER
# ============================================================

class Scheduler:
    """
    One-shot persistent task queue backed by SQLite.

    - Started exactly once via `start()`.
    - Cancels cleanly via `stop()`.
    - Pending tasks are re-loaded and executed on restart.
    """

    def __init__(self, bot: "Freakos"):
        self.bot = bot
        self._wake = asyncio.Event()
        self._task: Optional[asyncio.Task] = None

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._run(), name="freakos-scheduler")

    async def stop(self) -> None:
        if not self._task:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    def wake(self) -> None:
        self._wake.set()

    async def schedule(self, task_type: str, run_at: datetime,
                       payload: Optional[dict] = None,
                       guild_id: Optional[int] = None) -> int:
        tid = await self.bot.db.execute(
            "INSERT INTO scheduled_tasks (task_type, guild_id, payload, run_at) "
            "VALUES (?, ?, ?, ?)",
            (task_type, guild_id, json.dumps(payload or {}), to_iso(run_at)),
        )
        self.wake()
        return tid

    async def cancel_pending(self, task_type: str, payload_like: str) -> None:
        await self.bot.db.execute(
            "UPDATE scheduled_tasks SET completed=1 "
            "WHERE completed=0 AND task_type=? AND payload LIKE ?",
            (task_type, payload_like),
        )

    async def _run(self) -> None:
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
                        await asyncio.wait_for(self._wake.wait(), timeout=60)
                    except asyncio.TimeoutError:
                        pass
                    continue

                run_at = from_iso(row["run_at"]) or utcnow()
                delay = (run_at - utcnow()).total_seconds()
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
                    (row["id"],),
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Scheduler loop error")
                await asyncio.sleep(5)

    async def _execute(self, row: aiosqlite.Row) -> None:
        ttype = row["task_type"]
        try:
            payload = json.loads(row["payload"] or "{}")
        except Exception:
            payload = {}
        handler: Optional[Callable] = getattr(self.bot, f"task_{ttype}", None)
        if handler is None:
            log.warning("No handler for scheduled task %s", ttype)
            return
        try:
            await handler(payload)
        except Exception:
            log.exception("Scheduled task %s failed", ttype)


# ============================================================
# LOGGING HELPER
# ============================================================

async def log_guild(bot: "Freakos", guild: Optional[discord.Guild],
                    event_key: str, title: str, description: str,
                    color: int = 0x5865F2) -> None:
    if not guild:
        return
    if await bot.db.get_cfg(guild.id, "logging.enabled", "0") != "1":
        return
    ch_id = await bot.db.get_cfg(guild.id, "logging.channel")
    if not ch_id:
        return
    events = await bot.db.get_json(guild.id, "logging.events", default=[])
    if event_key not in events:
        return
    ch = guild.get_channel(int(ch_id))
    if not ch or not isinstance(ch, discord.TextChannel):
        return
    try:
        await ch.send(embed=make_embed(title=title, description=description,
                                       color=color))
    except Exception:
        log.exception("log_guild send failed")


# ============================================================
# PERSISTENT VIEWS
# ============================================================

# --- Ticket panel (opens the type picker) ---

class TicketCreateButton(discord.ui.View):
    """Persistent 'Open Ticket' button — one global instance."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Open Ticket", style=discord.ButtonStyle.primary,
                       custom_id="freakos:ticket:create", emoji="🎫")
    async def create(self, interaction: discord.Interaction, _):
        if not interaction.guild:
            return
        rows = await interaction.client.db.fetchall(
            "SELECT name, emoji FROM ticket_types WHERE guild_id=? ORDER BY id",
            (interaction.guild.id,),
        )
        if not rows:
            return await interaction.response.send_message(
                "No ticket types configured. Ask an admin to run `/ticket type`.",
                ephemeral=True,
            )
        opts = [
            discord.SelectOption(
                label=r["name"][:100],
                value=r["name"][:100],
                emoji=(r["emoji"] or None),
            )
            for r in rows[:25]
        ]
        select = discord.ui.Select(placeholder="Pick a ticket type…", options=opts)

        async def cb(sel: discord.Interaction):
            await open_ticket(sel, sel.data["values"][0])

        select.callback = cb
        v = discord.ui.View(timeout=60)
        v.add_item(select)
        await interaction.response.send_message(
            "Choose a ticket type to open:", view=v, ephemeral=True
        )


# --- Ticket management view (single global instance; resolves by channel) ---

class TicketManageView(discord.ui.View):
    """Persistent buttons shown inside every ticket channel."""

    def __init__(self):
        super().__init__(timeout=None)

    async def _lookup(self, interaction: discord.Interaction):
        return await interaction.client.db.fetchone(
            "SELECT * FROM tickets WHERE channel_id=?", (interaction.channel.id,)
        )

    @discord.ui.button(label="Claim", style=discord.ButtonStyle.success,
                       custom_id="freakos:ticket:claim")
    async def claim(self, interaction: discord.Interaction, _):
        row = await self._lookup(interaction)
        if not row:
            return await interaction.response.send_message("Not a ticket channel.", ephemeral=True)
        if not is_admin_or_mod(interaction.user):
            return await interaction.response.send_message("Staff only.", ephemeral=True)
        if row["claimed_by"] == interaction.user.id:
            return await interaction.response.send_message(
                "You already claimed this ticket.", ephemeral=True)
        await interaction.client.db.execute(
            "UPDATE tickets SET claimed_by=? WHERE id=?",
            (interaction.user.id, row["id"]),
        )
        await interaction.response.send_message(f"✅ Claimed by {interaction.user.mention}")

    @discord.ui.button(label="Close", style=discord.ButtonStyle.secondary,
                       custom_id="freakos:ticket:close")
    async def close(self, interaction: discord.Interaction, _):
        row = await self._lookup(interaction)
        if not row:
            return await interaction.response.send_message("Not a ticket channel.", ephemeral=True)
        if interaction.user.id != row["user_id"] and not is_admin_or_mod(interaction.user):
            return await interaction.response.send_message("Not allowed.", ephemeral=True)
        await interaction.client.db.execute(
            "UPDATE tickets SET status='closed' WHERE id=?", (row["id"],))
        try:
            ow = interaction.channel.overwrites_for(interaction.guild.default_role)
            ow.send_messages = False
            await interaction.channel.set_permissions(
                interaction.guild.default_role, overwrite=ow)
        except Exception:
            pass
        await interaction.response.send_message("🔒 Ticket closed.")


async def open_ticket(interaction: discord.Interaction, ttype: str) -> None:
    guild = interaction.guild
    if not guild:
        return
    db: Database = interaction.client.db
    bot: "Freakos" = interaction.client

    row = await db.fetchone(
        "SELECT * FROM ticket_types WHERE guild_id=? AND name=?",
        (guild.id, ttype),
    )
    if not row:
        return await interaction.response.send_message("Type not found.", ephemeral=True)

    # Serialize per-user to prevent double-create on rapid clicks
    lock_key = (guild.id, interaction.user.id)
    lock = bot._ticket_locks.setdefault(lock_key, asyncio.Lock())
    async with lock:
        limit = int(await db.get_cfg(guild.id, "ticket.limit", "1") or 1)
        open_count = await db.fetchone(
            "SELECT COUNT(*) c FROM tickets "
            "WHERE guild_id=? AND user_id=? AND status='open'",
            (guild.id, interaction.user.id),
        )
        if open_count and open_count["c"] >= limit:
            return await interaction.response.send_message(
                f"You already have {open_count['c']} open ticket(s).", ephemeral=True)

        cooldown = int(await db.get_cfg(guild.id, "ticket.cooldown", "0") or 0)
        if cooldown > 0:
            last = await db.fetchone(
                "SELECT created_at FROM tickets WHERE guild_id=? AND user_id=? "
                "ORDER BY id DESC LIMIT 1",
                (guild.id, interaction.user.id),
            )
            if last:
                last_dt = from_iso(last["created_at"])
                if last_dt and (utcnow() - last_dt).total_seconds() < cooldown:
                    rem = cooldown - int((utcnow() - last_dt).total_seconds())
                    return await interaction.response.send_message(
                        f"Cooldown: try again in {fmt_dur(rem)}.", ephemeral=True)

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

        try:
            channel = await guild.create_text_channel(
                name=f"ticket-{interaction.user.name}"[:90],
                category=category if isinstance(category, discord.CategoryChannel) else None,
                overwrites=overwrites,
                reason=f"Ticket by {interaction.user}",
            )
        except discord.Forbidden:
            return await interaction.response.send_message(
                "I lack permission to create the ticket channel.", ephemeral=True)

        tid = await db.execute(
            "INSERT INTO tickets (guild_id, channel_id, user_id, type, status, created_at) "
            "VALUES (?, ?, ?, ?, 'open', ?)",
            (guild.id, channel.id, interaction.user.id, ttype, to_iso(utcnow())),
        )

        body = row["message"] or (
            f"Hey {interaction.user.mention}, thanks for opening a **{ttype}** ticket!\n\n"
            "Support will be with you shortly. Please describe your issue."
        )
        embed = make_embed(title=f"Ticket #{tid} — {ttype}", description=body)
        try:
            await channel.send(content=interaction.user.mention, embed=embed,
                               view=TicketManageView())
        except Exception:
            log.exception("Failed to send ticket intro")

        await interaction.response.send_message(
            f"✅ Ticket created: {channel.mention}", ephemeral=True)
        await log_guild(bot, guild, "tickets", "Ticket Opened",
                        f"{interaction.user.mention} opened `{ttype}` → {channel.mention}")


# --- Reaction / button roles ---

class ReactionRoleView(discord.ui.View):
    """Persistent view — one instance per panel; custom_ids include the panel id."""

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
            await interaction.response.send_message("Missing permissions.", ephemeral=True)


async def _toggle_role(interaction: discord.Interaction, role_id: int) -> None:
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
            "I lack permission (check role hierarchy).", ephemeral=True)


# --- Giveaway entry view (single global instance; resolves by message) ---

class GiveawayView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Enter", style=discord.ButtonStyle.success, emoji="🎉",
                       custom_id="freakos:gw:enter")
    async def enter(self, interaction: discord.Interaction, _):
        gw = await interaction.client.db.fetchone(
            "SELECT * FROM giveaways WHERE message_id=?", (interaction.message.id,))
        if not gw or gw["ended"]:
            return await interaction.response.send_message("Giveaway ended.", ephemeral=True)
        await interaction.client.db.execute(
            "INSERT OR IGNORE INTO giveaway_entries (giveaway_id, user_id) VALUES (?, ?)",
            (gw["id"], interaction.user.id),
        )
        await interaction.response.send_message("✅ Entered!", ephemeral=True)


# --- Shop panel (single global instance) ---

class ShopPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Browse Products", style=discord.ButtonStyle.primary,
                       emoji="🛒", custom_id="freakos:shop:browse")
    async def browse(self, interaction: discord.Interaction, _):
        rows = await interaction.client.db.fetchall(
            "SELECT id, name, price, stock, active FROM shop_products "
            "WHERE guild_id=? ORDER BY id",
            (interaction.guild.id,),
        )
        if not rows:
            return await interaction.response.send_message("No products yet.", ephemeral=True)
        lines = []
        for r in rows:
            stock = "∞" if r["stock"] < 0 else r["stock"]
            flag = "✅" if r["active"] else "❌"
            lines.append(f"{flag} `#{r['id']}` **{r['name']}** — {r['price']} (stock {stock})")
        await safe_send(interaction, "\n".join(lines), ephemeral=True)

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
            (pid, interaction.guild.id))
        if not p or not p["active"]:
            return await interaction.response.send_message("Product not available.", ephemeral=True)
        if p["stock"] >= 0 and p["stock"] < qty:
            return await interaction.response.send_message("Not enough stock.", ephemeral=True)
        total = float(p["price"]) * qty
        oid = await db.execute(
            "INSERT INTO orders (guild_id, user_id, product_id, quantity, price, "
            "status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)",
            (interaction.guild.id, interaction.user.id, pid, qty, total,
             to_iso(utcnow()), to_iso(utcnow())),
        )
        if p["stock"] >= 0:
            await db.execute("UPDATE shop_products SET stock=stock-? WHERE id=?",
                             (qty, pid))
        await interaction.response.send_message(
            f"✅ Order `#{oid}` created for {qty}× **{p['name']}** — total {total}.\n"
            "Await payment instructions from staff.",
            ephemeral=True,
        )
        await log_guild(interaction.client, interaction.guild, "shop", "New Order",
                        f"{interaction.user.mention} ordered `#{oid}` "
                        f"({qty}× {p['name']}).")


# ============================================================
# CONFIGURED-MESSAGE RENDERERS  (shared by events + test commands)
# ============================================================

async def _render_configured(bot: "Freakos", guild: discord.Guild,
                             cfg_base: str, channel_id_key: str,
                             default_tpl: str, **extra_ph) -> Optional[tuple[discord.TextChannel, str, Optional[dict]]]:
    """Build the (channel, rendered_text, embed_cfg) for a system.  Returns None if not sendable."""
    ch_id = await bot.db.get_cfg(guild.id, channel_id_key)
    if not ch_id:
        return None
    ch = guild.get_channel(int(ch_id))
    if not isinstance(ch, discord.TextChannel):
        return None
    tpl = await bot.db.get_cfg(guild.id, cfg_base) or default_tpl
    rendered = render(tpl, **extra_ph)
    embed_cfg = await bot.db.get_json(guild.id, cfg_base + "_embed", default=None)
    return ch, rendered, embed_cfg


def _apply_embed_cfg(base: discord.Embed, cfg: Optional[dict]) -> discord.Embed:
    if not cfg:
        return base
    if cfg.get("title"):
        base.title = str(cfg["title"])[:256]
    if cfg.get("footer"):
        base.set_footer(text=str(cfg["footer"])[:2048])
    if cfg.get("image"):
        base.set_image(url=str(cfg["image"]))
    if cfg.get("thumbnail"):
        base.set_thumbnail(url=str(cfg["thumbnail"]))
    if cfg.get("color") is not None:
        try:
            base.color = discord.Color(int(cfg["color"]))
        except Exception:
            pass
    return base


async def send_welcome(bot: "Freakos", member: discord.Member) -> None:
    guild = member.guild
    if await bot.db.get_cfg(guild.id, "welcome.enabled", "0") != "1":
        return
    ph = build_user_placeholders(member, guild)
    res = await _render_configured(
        bot, guild, "welcome.message", "welcome.channel",
        "Welcome {mention} to **{server_name}**!", **ph,
    )
    if not res:
        return
    ch, text, embed_cfg = res
    try:
        if embed_cfg:
            e = _apply_embed_cfg(make_embed(), embed_cfg)
            if not e.description:
                e.description = text[:4096]
            await ch.send(embed=e)
        else:
            await safe_send(ch, text)
    except Exception:
        log.exception("welcome send failed")


async def send_departure(bot: "Freakos", member: discord.Member) -> None:
    guild = member.guild
    if await bot.db.get_cfg(guild.id, "departure.enabled", "0") != "1":
        return
    ph = build_user_placeholders(member, guild)
    res = await _render_configured(
        bot, guild, "departure.message", "departure.channel",
        "**{username}** left the server.", **ph,
    )
    if not res:
        return
    ch, text, embed_cfg = res
    try:
        if embed_cfg:
            e = _apply_embed_cfg(make_embed(), embed_cfg)
            if not e.description:
                e.description = text[:4096]
            await ch.send(embed=e)
        else:
            await safe_send(ch, text)
    except Exception:
        log.exception("departure send failed")


# ============================================================
# SCHEDULED TASK HANDLERS
# ============================================================

async def _task_timeout_end(bot: "Freakos", payload: dict) -> None:
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

    member: Optional[discord.Member] = guild.get_member(row["user_id"])
    if member is None:
        try:
            member = await guild.fetch_member(row["user_id"])
        except (discord.NotFound, discord.HTTPException):
            member = None

    if member is not None:
        # If the member is still timed out, reschedule instead of firing early.
        try:
            until = member.timed_out_until
            if until and until > utcnow() + timedelta(seconds=10):
                await bot.scheduler.schedule(
                    "timeout_end", until + timedelta(seconds=5),
                    {"timeout_id": tid}, guild.id,
                )
                return
        except Exception:
            pass

        tpl = await bot.db.get_cfg(guild.id, "actiondm.timeout_end_msg")
        if tpl:
            mod = guild.get_member(row["moderator_id"])
            ph = build_user_placeholders(
                member, guild,
                reason=row["reason"] or "No reason provided.",
                moderator=mod.mention if mod else f"<@{row['moderator_id']}>",
                case_id=str(tid),
                duration="—",
                timeout_end=fmt_dt(from_iso(row["ends_at"])),
                timestamp=fmt_dt(utcnow()),
            )
            try:
                await safe_send(member, render(tpl, **ph))
            except Exception:
                pass
    await bot.db.execute("UPDATE timeouts SET dm_sent=1 WHERE id=?", (tid,))


async def _task_giveaway_end(bot: "Freakos", payload: dict) -> None:
    gid = payload.get("giveaway_id")
    if not gid:
        return
    await end_giveaway(bot, gid)


async def _task_announcement(bot: "Freakos", payload: dict) -> None:
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
    ch = guild.get_channel(row["channel_id"])
    if not isinstance(ch, discord.TextChannel):
        await bot.db.execute("UPDATE announcements SET sent=1 WHERE id=?", (aid,))
        return
    embed = make_embed(
        title=row["title"] or None,
        description=row["body"] or None,
    )
    if row["image"]:
        try:
            embed.set_image(url=row["image"])
        except Exception:
            pass
    if row["footer"]:
        embed.set_footer(text=str(row["footer"])[:2048])
    try:
        await ch.send(embed=embed)
    except Exception:
        log.exception("announcement %s failed", aid)
    await bot.db.execute("UPDATE announcements SET sent=1 WHERE id=?", (aid,))


async def end_giveaway(bot: "Freakos", gid: int, reroll: bool = False) -> None:
    row = await bot.db.fetchone("SELECT * FROM giveaways WHERE id=?", (gid,))
    if not row:
        return
    if row["ended"] and not reroll:
        return  # already ended (prevents duplicate end DMs)
    entries = await bot.db.fetchall(
        "SELECT user_id FROM giveaway_entries WHERE giveaway_id=?", (gid,))
    guild = bot.get_guild(row["guild_id"])
    winners_text = "No valid entries 😢"
    if entries and guild:
        pool = [e["user_id"] for e in entries]
        random.shuffle(pool)
        n = min(int(row["winners"]), len(pool))
        winner_ids = pool[:n]
        winners_text = ", ".join(f"<@{w}>" for w in winner_ids)

    channel = guild.get_channel(row["channel_id"]) if guild else None
    if isinstance(channel, discord.TextChannel):
        if row["message_id"]:
            try:
                msg = await channel.fetch_message(row["message_id"])
                embed = msg.embeds[0] if msg.embeds else make_embed(title="Giveaway")
                embed.title = f"🎉 Giveaway Ended — {row['prize']}"[:256]
                embed.description = f"Winners: {winners_text}"[:4096]
                await msg.edit(embed=embed, view=None)
            except Exception:
                pass
        try:
            await channel.send(f"🎉 **{row['prize']}** ended! Winners: {winners_text}")
        except Exception:
            pass
    if not reroll:
        await bot.db.execute("UPDATE giveaways SET ended=1 WHERE id=?", (gid,))


# ============================================================
# BOT CLASS
# ============================================================

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
        self._ticket_locks: dict[tuple, asyncio.Lock] = {}

    # ---------- lifecycle ----------
    async def setup_hook(self) -> None:
        await self.db.connect()
        self.scheduler = Scheduler(self)
        self.scheduler.start()

        # Register persistent views (must be exactly one instance per custom_id).
        self.add_view(TicketCreateButton())
        self.add_view(TicketManageView())
        self.add_view(ShopPanelView())
        self.add_view(GiveawayView())

        panels = await self.db.fetchall("SELECT * FROM reaction_panels")
        for p in panels:
            roles = await self.db.fetchall(
                "SELECT role_id, label, emoji FROM reaction_roles WHERE panel_id=?",
                (p["id"],))
            self.add_view(ReactionRoleView(
                p["id"], [dict(r) for r in roles], p["mode"] or "button"))

        register_all_commands(self)

        try:
            synced = await self.tree.sync()
            log.info("Synced %d slash commands", len(synced))
        except Exception:
            log.exception("Slash-command sync failed")

    async def close(self) -> None:
        try:
            if self.scheduler:
                await self.scheduler.stop()
            await self.db.close()
        finally:
            await super().close()

    async def on_ready(self) -> None:
        log.info("Logged in as %s (%s) | guilds=%d",
                 self.user, getattr(self.user, "id", "?"), len(self.guilds))
        try:
            await self.change_presence(activity=discord.Activity(
                type=discord.ActivityType.watching, name="over the server"))
        except Exception:
            pass

    # ---------- scheduled-task entry points ----------
    async def task_timeout_end(self, payload: dict) -> None:
        await _task_timeout_end(self, payload)

    async def task_giveaway_end(self, payload: dict) -> None:
        await _task_giveaway_end(self, payload)

    async def task_announcement(self, payload: dict) -> None:
        await _task_announcement(self, payload)

    # ---------- member events ----------
    async def on_member_join(self, member: discord.Member) -> None:
        try:
            await send_welcome(self, member)
        except Exception:
            log.exception("welcome error")

        # Autorole
        try:
            if await self.db.get_cfg(member.guild.id, "autorole.enabled", "0") == "1":
                roles = await self.db.get_json(member.guild.id, "autorole.roles", default=[])
                good = []
                for rid in roles:
                    r = member.guild.get_role(rid)
                    if r and not r.managed and r < member.guild.me.top_role:
                        good.append(r)
                if good:
                    await member.add_roles(*good, reason="Autorole")
        except Exception:
            log.exception("autorole error")

        # Autonick
        try:
            if await self.db.get_cfg(member.guild.id, "autonick.enabled", "0") == "1":
                fmt = await self.db.get_cfg(member.guild.id, "autonick.format") or "{user}"
                nick = render(fmt, **build_user_placeholders(member, member.guild))
                nick = nick[:32] or None
                if (member.guild.me.guild_permissions.manage_nicknames
                        and member.top_role < member.guild.me.top_role
                        and member.id != member.guild.owner_id):
                    await member.edit(nick=nick, reason="Autonick")
        except Exception:
            pass

        await log_guild(self, member.guild, "joins", "Member Joined",
                        f"{member.mention} ({member})")

    async def on_member_remove(self, member: discord.Member) -> None:
        try:
            await send_departure(self, member)
        except Exception:
            log.exception("departure error")
        await log_guild(self, member.guild, "leaves", "Member Left",
                        f"{member.mention} ({member})")

    # ---------- message events ----------
    async def on_message_delete(self, message: discord.Message) -> None:
        if not message.guild or message.author.bot:
            return
        content = message.content or "*(empty)*"
        await log_guild(self, message.guild, "messages", "Message Deleted",
                        f"**Author:** {message.author.mention}\n"
                        f"**Channel:** {message.channel.mention}\n"
                        f"**Content:**\n{content[:1500]}")

    async def on_message_edit(self, before: discord.Message, after: discord.Message) -> None:
        if not after.guild or after.author.bot or before.content == after.content:
            return
        await log_guild(self, after.guild, "messages", "Message Edited",
                        f"**Author:** {after.author.mention}\n"
                        f"**Channel:** {after.channel.mention}\n"
                        f"**Before:**\n{(before.content or '')[:700]}\n"
                        f"**After:**\n{(after.content or '')[:700]}")

    # ---------- voice events ----------
    async def on_voice_state_update(self, member: discord.Member,
                                    before: discord.VoiceState,
                                    after: discord.VoiceState) -> None:
        guild = member.guild
        if member.bot:
            return
        old_id = before.channel.id if before.channel else None
        new_id = after.channel.id if after.channel else None
        if old_id == new_id:
            return  # mute/deafen/stream change — ignore

        if await self.db.get_cfg(guild.id, "vcnotify.enabled", "1") != "1":
            return

        watch = await self.db.get_json(guild.id, "vcnotify.channels", default=[])
        if not watch:
            return

        # LEAVE event for source channel
        if old_id and old_id in watch:
            cfg = await self.db.get_json(guild.id, f"vcnotify.cfg.{old_id}", default={})
            if cfg.get("enabled", True):
                target = guild.get_channel(cfg.get("target_channel_id") or 0)
                if isinstance(target, discord.TextChannel):
                    tpl = cfg.get("leave_msg") or "👋 {mention} left **{channel_name}**."
                    ph = build_user_placeholders(
                        member, guild, channel_name=before.channel.name)
                    try:
                        await safe_send(target, render(tpl, **ph))
                    except Exception:
                        pass

        # JOIN event for destination channel
        if new_id and new_id in watch:
            cfg = await self.db.get_json(guild.id, f"vcnotify.cfg.{new_id}", default={})
            if cfg.get("enabled", True):
                target = guild.get_channel(cfg.get("target_channel_id") or 0)
                if isinstance(target, discord.TextChannel):
                    tpl = cfg.get("join_msg") or "🎧 {mention} joined **{channel_name}**."
                    ph = build_user_placeholders(
                        member, guild, channel_name=after.channel.name)
                    try:
                        await safe_send(target, render(tpl, **ph))
                    except Exception:
                        pass

    # ---------- message processing ----------
    async def on_message(self, message: discord.Message) -> None:
        if not message.guild or message.author.bot:
            return
        try:
            await self._automod_check(message)
        except Exception:
            log.exception("automod error")

        # Text custom commands (prefix: "!")
        if message.content.startswith("!") and isinstance(message.author, discord.Member):
            name = message.content[1:].split(" ", 1)[0].lower()
            if name:
                row = await self.db.fetchone(
                    "SELECT * FROM custom_commands WHERE guild_id=? AND name=? AND enabled=1",
                    (message.guild.id, name),
                )
                if row:
                    ph = build_user_placeholders(message.author, message.guild)
                    text = render(row["response"] or "", **ph)
                    try:
                        if row["embed"]:
                            await message.channel.send(
                                embed=make_embed(title=name.title(), description=text[:4096]))
                        else:
                            await safe_send(message.channel, text)
                    except Exception:
                        pass

        await self.process_commands(message)

    # ---------- AutoMod ----------
    async def _automod_check(self, message: discord.Message) -> None:
        guild = message.guild
        author = message.author
        if not isinstance(author, discord.Member):
            return
        if await self.db.get_cfg(guild.id, "automod.enabled", "0") != "1":
            return
        if author.guild_permissions.administrator or author.guild_permissions.manage_messages:
            return
        if author.id == self.user.id:
            return
        # Never act against anyone above the bot
        if author.top_role >= guild.me.top_role:
            return

        whitelist = await self.db.get_json(guild.id, "automod.whitelist", default={})
        if message.channel.id in set(whitelist.get("channels", [])):
            return
        wl_roles = set(whitelist.get("roles", []))
        if any(r.id in wl_roles for r in author.roles):
            return

        settings = await self.db.get_json(guild.id, "automod.settings", default={})
        action = settings.get("action", "delete")
        violations: list[str] = []
        content = message.content or ""

        invite_re = re.compile(r"(discord\.gg|discord\.com/invite|discordapp\.com/invite)/\S+", re.I)
        link_re = re.compile(r"https?://\S+", re.I)

        if settings.get("invites", True) and invite_re.search(content):
            violations.append("invite link")
        elif settings.get("links", True) and link_re.search(content):
            violations.append("link")

        words = settings.get("words", [])
        low = content.lower()
        for w in words:
            if w and w.lower() in low:
                violations.append(f"banned word `{w}`")
                break

        if settings.get("mentions", True) and len(message.mentions) >= 5:
            violations.append("mention spam")

        if settings.get("antispam", True):
            key = (guild.id, author.id)
            now = utcnow().timestamp()
            arr = self._spam_tracker.setdefault(key, [])
            arr.append(now)
            self._spam_tracker[key] = [t for t in arr if now - t < 5]
            if len(self._spam_tracker[key]) >= 6:
                violations.append("spam")
                self._spam_tracker[key] = []

        if not violations:
            return

        # Delete the offending message first if any action involves removal
        try:
            await message.delete()
        except Exception:
            pass

        if action == "warn":
            await self.db.execute(
                "INSERT INTO warnings (guild_id, user_id, moderator_id, reason, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (guild.id, author.id, self.user.id,
                 f"AutoMod: {', '.join(violations)}", to_iso(utcnow())),
            )
            try:
                await message.channel.send(
                    f"{author.mention} warning: {', '.join(violations)}", delete_after=5)
            except Exception:
                pass
        elif action == "timeout":
            try:
                await author.timeout(timedelta(minutes=10),
                                     reason=f"AutoMod: {violations}")
            except Exception:
                pass
        elif action == "kick":
            try:
                await author.kick(reason=f"AutoMod: {violations}")
            except Exception:
                pass
        elif action == "ban":
            try:
                await author.ban(reason=f"AutoMod: {violations}", delete_message_days=1)
            except Exception:
                pass

        await log_guild(self, guild, "automod", "AutoMod Action",
                        f"{author.mention}: {', '.join(violations)} (action: {action})",
                        color=0xE67E22)

    # ---------- global error handler ----------
    async def on_app_command_error(self, interaction: discord.Interaction,
                                   error: app_commands.AppCommandError) -> None:
        original = getattr(error, "original", error)
        log.exception("App command error: %s", original)

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


# ============================================================
# MODAL HELPERS
# ============================================================

class TextModal(discord.ui.Modal):
    """Simple multiline input that preserves newlines exactly."""

    def __init__(self, title: str, default: str, on_submit: Callable):
        super().__init__(title=title[:45])
        self._cb = on_submit
        self.text = discord.ui.TextInput(
            label="Message",
            style=discord.TextStyle.paragraph,
            default=default[:4000] if default else "",
            max_length=4000,
            required=False,
        )
        self.add_item(self.text)

    async def on_submit(self, interaction: discord.Interaction):
        await self._cb(interaction, self.text.value)


async def _save_and_reply(interaction: discord.Interaction, key: str, value: str) -> None:
    # Preserve exact newlines — no strip / split / join.
    await interaction.client.db.set_cfg(interaction.guild.id, key, value)
    await interaction.response.send_message("✅ Saved.", ephemeral=True)


# ============================================================
# COMMAND REGISTRATION
# ============================================================

def register_all_commands(bot: Freakos) -> None:
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

    # ================= GENERAL =================

    @tree.command(name="ping", description="Show bot latency.")
    async def ping(interaction: discord.Interaction):
        await interaction.response.send_message(f"🏓 Pong! `{round(bot.latency*1000)}ms`")

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
            e.add_field(name="Top Role", value=m.top_role.mention, inline=True)
            roles = [r.mention for r in reversed(m.roles) if r.name != "@everyone"]
            if roles:
                e.add_field(name="Roles", value=", ".join(roles[:20])[:1024], inline=False)
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
            e.add_field(name=cat,
                        value=", ".join(f"`/{n}`" for n in names)[:1024],
                        inline=False)
        await interaction.response.send_message(embed=e, ephemeral=True)

    @tree.command(name="setup", description="Interactive setup wizard.")
    async def setup(interaction: discord.Interaction):
        if not await require_admin(interaction):
            return
        g = interaction.guild
        ticket_types = await db.fetchone(
            "SELECT COUNT(*) c FROM ticket_types WHERE guild_id=?", (g.id,))
        shop_products = await db.fetchone(
            "SELECT COUNT(*) c FROM shop_products WHERE guild_id=?", (g.id,))
        rr_panels = await db.fetchone(
            "SELECT COUNT(*) c FROM reaction_panels WHERE guild_id=?", (g.id,))
        gws = await db.fetchone(
            "SELECT COUNT(*) c FROM giveaways WHERE guild_id=? AND ended=0", (g.id,))
        ccs = await db.fetchone(
            "SELECT COUNT(*) c FROM custom_commands WHERE guild_id=?", (g.id,))
        anns = await db.fetchone(
            "SELECT COUNT(*) c FROM announcements WHERE guild_id=? AND sent=0", (g.id,))

        checks = [
            ("Welcome", await db.get_cfg(g.id, "welcome.enabled", "0") == "1"),
            ("Autorole", await db.get_cfg(g.id, "autorole.enabled", "0") == "1"),
            ("Autonick", await db.get_cfg(g.id, "autonick.enabled", "0") == "1"),
            ("Departure", await db.get_cfg(g.id, "departure.enabled", "0") == "1"),
            ("Action DMs", await db.get_cfg(g.id, "actiondm.enabled", "0") == "1"),
            ("VC Notifications",
             bool(await db.get_json(g.id, "vcnotify.channels", default=[]))),
            ("Tickets", ticket_types["c"] > 0),
            ("Vouches", bool(await db.get_cfg(g.id, "vouch.channel"))),
            ("Shop", shop_products["c"] > 0),
            ("Notifications", bool(await db.get_cfg(g.id, "notify.channel"))),
            ("AutoMod", await db.get_cfg(g.id, "automod.enabled", "0") == "1"),
            ("Logging", bool(await db.get_cfg(g.id, "logging.channel"))),
            ("Reaction Roles", rr_panels["c"] > 0),
            ("Giveaways (open)", gws["c"] > 0),
            ("Custom Commands", ccs["c"] > 0),
            ("Announcements (pending)", anns["c"] > 0),
        ]
        lines = [f"{'🟢' if on else '⚪'} **{name}**" for name, on in checks]
        e = make_embed(
            title="🛠 FREAKOS Setup",
            description=(
                "Configure each module with its own slash-command group:\n"
                "`/welcome` `/autorole` `/autonick` `/departure` `/actiondm` `/vcnotify` "
                "`/ticket` `/vouch` `/shop` `/order` `/notify` `/automod` `/logging` "
                "`/reactionrole` `/giveaway` `/customcommand` `/announce`\n\n"
                "**Current status:**\n" + "\n".join(lines)
            ),
        )
        await interaction.response.send_message(embed=e, ephemeral=True)

    # ================= WELCOME =================
    welcome = app_commands.Group(name="welcome", description="Welcome system")
    tree.add_command(welcome)

    @welcome.command(name="setup", description="Set welcome channel and enable.")
    async def w_setup(interaction: discord.Interaction, channel: discord.TextChannel):
        if not await require_admin(interaction): return
        await db.set_cfg(interaction.guild.id, "welcome.channel", channel.id)
        await db.set_cfg(interaction.guild.id, "welcome.enabled", "1")
        await interaction.response.send_message(
            f"✅ Welcome configured to {channel.mention}.", ephemeral=True)

    @welcome.command(name="enable", description="Enable welcome messages.")
    async def w_enable(interaction: discord.Interaction):
        if not await require_admin(interaction): return
        await db.set_cfg(interaction.guild.id, "welcome.enabled", "1")
        await interaction.response.send_message("✅ Enabled.", ephemeral=True)

    @welcome.command(name="disable", description="Disable welcome messages.")
    async def w_disable(interaction: discord.Interaction):
        if not await require_admin(interaction): return
        await db.set_cfg(interaction.guild.id, "welcome.enabled", "0")
        await interaction.response.send_message("✅ Disabled.", ephemeral=True)

    @welcome.command(name="channel", description="Set welcome channel.")
    async def w_channel(interaction: discord.Interaction, channel: discord.TextChannel):
        if not await require_admin(interaction): return
        await db.set_cfg(interaction.guild.id, "welcome.channel", channel.id)
        await interaction.response.send_message(
            f"✅ Channel set to {channel.mention}.", ephemeral=True)

    @welcome.command(name="message",
                     description="Set welcome message (multiline supported).")
    async def w_message(interaction: discord.Interaction):
        if not await require_admin(interaction): return
        cur = await db.get_cfg(interaction.guild.id, "welcome.message", "") or ""
        await interaction.response.send_modal(TextModal(
            title="Welcome Message", default=cur,
            on_submit=lambda i, v: _save_and_reply(i, "welcome.message", v)))

    @welcome.command(name="embed", description="Configure welcome embed.")
    @app_commands.describe(title="Embed title", color="Hex color (e.g. #5865F2)",
                           footer="Footer text", image="Image URL",
                           enabled="Use embed instead of plain text")
    async def w_embed(interaction: discord.Interaction,
                      enabled: bool,
                      title: Optional[str] = None,
                      color: Optional[str] = None,
                      footer: Optional[str] = None,
                      image: Optional[str] = None):
        if not await require_admin(interaction): return
        cfg: dict = {"enabled": enabled}
        if title: cfg["title"] = title
        if footer: cfg["footer"] = footer
        if image: cfg["image"] = image
        if color:
            try:
                cfg["color"] = int(color.replace("#", ""), 16)
            except Exception:
                pass
        if enabled:
            await db.set_json(interaction.guild.id, "welcome.message_embed", cfg)
        else:
            await db.set_cfg(interaction.guild.id, "welcome.message_embed", None)
        await interaction.response.send_message("✅ Saved.", ephemeral=True)

    @welcome.command(name="test", description="Send a test welcome message.")
    async def w_test(interaction: discord.Interaction):
        if not await require_admin(interaction): return
        if not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message("Guild only.", ephemeral=True)
        await send_welcome(bot, interaction.user)
        await interaction.response.send_message("✅ Test sent.", ephemeral=True)

    @welcome.command(name="reset", description="Reset welcome config.")
    async def w_reset(interaction: discord.Interaction):
        if not await require_admin(interaction): return
        for k in ("welcome.channel", "welcome.message",
                  "welcome.message_embed", "welcome.enabled"):
            await db.set_cfg(interaction.guild.id, k, None)
        await interaction.response.send_message("✅ Reset.", ephemeral=True)

    # ================= AUTOROLE =================
    autorole = app_commands.Group(name="autorole", description="Autorole system")
    tree.add_command(autorole)

    @autorole.command(name="setup", description="Enable autorole with a single role.")
    async def ar_setup(interaction: discord.Interaction, role: discord.Role):
        if not await require_admin(interaction): return
        if role >= interaction.guild.me.top_role or role.managed:
            return await interaction.response.send_message(
                "❌ Role is higher than mine or managed.", ephemeral=True)
        await db.set_json(interaction.guild.id, "autorole.roles", [role.id])
        await db.set_cfg(interaction.guild.id, "autorole.enabled", "1")
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
        await db.set_cfg(interaction.guild.id, "autorole.enabled", "1")
        await interaction.response.send_message("✅ Enabled.", ephemeral=True)

    @autorole.command(name="disable", description="Disable autorole.")
    async def ar_disable(interaction: discord.Interaction):
        if not await require_admin(interaction): return
        await db.set_cfg(interaction.guild.id, "autorole.enabled", "0")
        await interaction.response.send_message("✅ Disabled.", ephemeral=True)

    @autorole.command(name="reset", description="Reset autorole.")
    async def ar_reset(interaction: discord.Interaction):
        if not await require_admin(interaction): return
        for k in ("autorole.roles", "autorole.enabled"):
            await db.set_cfg(interaction.guild.id, k, None)
        await interaction.response.send_message("✅ Reset.", ephemeral=True)

    # ================= AUTONICK =================
    autonick = app_commands.Group(name="autonick", description="Autonick system")
    tree.add_command(autonick)

    @autonick.command(name="setup", description="Set format and enable autonick.")
    async def an_setup(interaction: discord.Interaction, format: str):
        if not await require_admin(interaction): return
        await db.set_cfg(interaction.guild.id, "autonick.format", format)
        await db.set_cfg(interaction.guild.id, "autonick.enabled", "1")
        await interaction.response.send_message("✅ Autonick enabled.", ephemeral=True)

    @autonick.command(name="format", description="Change autonick format.")
    async def an_format(interaction: discord.Interaction, format: str):
        if not await require_admin(interaction): return
        await db.set_cfg(interaction.guild.id, "autonick.format", format)
        await interaction.response.send_message("✅ Format updated.", ephemeral=True)

    @autonick.command(name="enable", description="Enable autonick.")
    async def an_enable(interaction: discord.Interaction):
        if not await require_admin(interaction): return
        await db.set_cfg(interaction.guild.id, "autonick.enabled", "1")
        await interaction.response.send_message("✅ Enabled.", ephemeral=True)

    @autonick.command(name="disable", description="Disable autonick.")
    async def an_disable(interaction: discord.Interaction):
        if not await require_admin(interaction): return
        await db.set_cfg(interaction.guild.id, "autonick.enabled", "0")
        await interaction.response.send_message("✅ Disabled.", ephemeral=True)

    @autonick.command(name="reset", description="Reset autonick.")
    async def an_reset(interaction: discord.Interaction):
        if not await require_admin(interaction): return
        for k in ("autonick.format", "autonick.enabled"):
            await db.set_cfg(interaction.guild.id, k, None)
        await interaction.response.send_message("✅ Reset.", ephemeral=True)

    # ================= DEPARTURE =================
    departure = app_commands.Group(name="departure", description="Departure messages")
    tree.add_command(departure)

    @departure.command(name="setup", description="Set channel and enable departure.")
    async def d_setup(interaction: discord.Interaction, channel: discord.TextChannel):
        if not await require_admin(interaction): return
        await db.set_cfg(interaction.guild.id, "departure.channel", channel.id)
        await db.set_cfg(interaction.guild.id, "departure.enabled", "1")
        await interaction.response.send_message(
            f"✅ Departure set to {channel.mention}.", ephemeral=True)

    @departure.command(name="enable", description="Enable departure.")
    async def d_enable(interaction: discord.Interaction):
        if not await require_admin(interaction): return
        await db.set_cfg(interaction.guild.id, "departure.enabled", "1")
        await interaction.response.send_message("✅ Enabled.", ephemeral=True)

    @departure.command(name="disable", description="Disable departure.")
    async def d_disable(interaction: discord.Interaction):
        if not await require_admin(interaction): return
        await db.set_cfg(interaction.guild.id, "departure.enabled", "0")
        await interaction.response.send_message("✅ Disabled.", ephemeral=True)

    @departure.command(name="channel", description="Set departure channel.")
    async def d_channel(interaction: discord.Interaction, channel: discord.TextChannel):
        if not await require_admin(interaction): return
        await db.set_cfg(interaction.guild.id, "departure.channel", channel.id)
        await interaction.response.send_message("✅ Set.", ephemeral=True)

    @departure.command(name="message",
                       description="Set departure message (multiline supported).")
    async def d_message(interaction: discord.Interaction):
        if not await require_admin(interaction): return
        cur = await db.get_cfg(interaction.guild.id, "departure.message", "") or ""
        await interaction.response.send_modal(TextModal(
            title="Departure Message", default=cur,
            on_submit=lambda i, v: _save_and_reply(i, "departure.message", v)))

    @departure.command(name="test", description="Send a test departure message.")
    async def d_test(interaction: discord.Interaction):
        if not await require_admin(interaction): return
        if not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message("Guild only.", ephemeral=True)
        await send_departure(bot, interaction.user)
        await interaction.response.send_message("✅ Test sent.", ephemeral=True)

    @departure.command(name="reset", description="Reset departure.")
    async def d_reset(interaction: discord.Interaction):
        if not await require_admin(interaction): return
        for k in ("departure.channel", "departure.message",
                  "departure.message_embed", "departure.enabled"):
            await db.set_cfg(interaction.guild.id, k, None)
        await interaction.response.send_message("✅ Reset.", ephemeral=True)

    # ================= ACTION DMS =================
    actiondm = app_commands.Group(name="actiondm", description="Action DMs")
    tree.add_command(actiondm)

    @actiondm.command(name="setup", description="Enable action DMs.")
    async def a_setup(interaction: discord.Interaction):
        if not await require_admin(interaction): return
        await db.set_cfg(interaction.guild.id, "actiondm.enabled", "1")
        await interaction.response.send_message("✅ Enabled.", ephemeral=True)

    @actiondm.command(name="enable", description="Enable action DMs.")
    async def a_enable(interaction: discord.Interaction):
        if not await require_admin(interaction): return
        await db.set_cfg(interaction.guild.id, "actiondm.enabled", "1")
        await interaction.response.send_message("✅ Enabled.", ephemeral=True)

    @actiondm.command(name="disable", description="Disable action DMs.")
    async def a_disable(interaction: discord.Interaction):
        if not await require_admin(interaction): return
        await db.set_cfg(interaction.guild.id, "actiondm.enabled", "0")
        await interaction.response.send_message("✅ Disabled.", ephemeral=True)

    async def _edit_actiondm(interaction: discord.Interaction, key: str, title: str):
        if not await require_admin(interaction): return
        cur = await db.get_cfg(interaction.guild.id, key, "") or ""
        await interaction.response.send_modal(TextModal(
            title=title, default=cur,
            on_submit=lambda i, v: _save_and_reply(i, key, v)))

    @actiondm.command(name="timeout", description="Edit the timeout DM.")
    async def a_timeout(interaction: discord.Interaction):
        await _edit_actiondm(interaction, "actiondm.timeout_msg", "Timeout DM")

    @actiondm.command(name="timeout-end", description="Edit the timeout-end DM.")
    async def a_timeout_end(interaction: discord.Interaction):
        await _edit_actiondm(interaction, "actiondm.timeout_end_msg", "Timeout-End DM")

    @actiondm.command(name="kick", description="Edit the kick DM.")
    async def a_kick(interaction: discord.Interaction):
        await _edit_actiondm(interaction, "actiondm.kick_msg", "Kick DM")

    @actiondm.command(name="ban", description="Edit the ban DM.")
    async def a_ban(interaction: discord.Interaction):
        await _edit_actiondm(interaction, "actiondm.ban_msg", "Ban DM")

    @actiondm.command(name="test", description="Send a test timeout DM to yourself.")
    async def a_test(interaction: discord.Interaction):
        if not await require_admin(interaction): return
        tpl = await db.get_cfg(interaction.guild.id, "actiondm.timeout_msg") or \
            "You were timed out in {server_name} for {duration}.\nReason: {reason}"
        ph = build_user_placeholders(
            interaction.user, interaction.guild,
            moderator=interaction.user.mention,
            reason="Test",
            duration="10m",
            timeout_end=fmt_dt(utcnow() + timedelta(minutes=10)),
            case_id="TEST",
        )
        try:
            await safe_send(interaction.user, render(tpl, **ph))
            await interaction.response.send_message("✅ Sent via DM.", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message("❌ Couldn't DM you.", ephemeral=True)

    @actiondm.command(name="reset", description="Reset all action DM templates.")
    async def a_reset(interaction: discord.Interaction):
        if not await require_admin(interaction): return
        for k in ("actiondm.enabled", "actiondm.timeout_msg",
                  "actiondm.timeout_end_msg", "actiondm.kick_msg", "actiondm.ban_msg"):
            await db.set_cfg(interaction.guild.id, k, None)
        await interaction.response.send_message("✅ Reset.", ephemeral=True)

    # ================= VC NOTIFICATIONS =================
    vcnotify = app_commands.Group(name="vcnotify", description="VC notifications")
    tree.add_command(vcnotify)

    @vcnotify.command(name="setup",
                      description="Route a voice channel to a text channel.")
    async def v_setup(interaction: discord.Interaction,
                      voice_channel: discord.VoiceChannel,
                      text_channel: discord.TextChannel):
        if not await require_admin(interaction): return
        cfg = await db.get_json(interaction.guild.id,
                                f"vcnotify.cfg.{voice_channel.id}", default={})
        cfg["target_channel_id"] = text_channel.id
        cfg["enabled"] = True
        await db.set_json(interaction.guild.id,
                          f"vcnotify.cfg.{voice_channel.id}", cfg)
        watch = await db.get_json(interaction.guild.id, "vcnotify.channels", default=[])
        if voice_channel.id not in watch:
            watch.append(voice_channel.id)
            await db.set_json(interaction.guild.id, "vcnotify.channels", watch)
        await interaction.response.send_message(
            f"✅ {voice_channel.mention} → {text_channel.mention}.", ephemeral=True)

    @vcnotify.command(name="enable", description="Enable VC notifications globally.")
    async def v_enable(interaction: discord.Interaction):
        if not await require_admin(interaction): return
        await db.set_cfg(interaction.guild.id, "vcnotify.enabled", "1")
        await interaction.response.send_message("✅ Enabled.", ephemeral=True)

    @vcnotify.command(name="disable", description="Disable VC notifications globally.")
    async def v_disable(interaction: discord.Interaction):
        if not await require_admin(interaction): return
        await db.set_cfg(interaction.guild.id, "vcnotify.enabled", "0")
        await interaction.response.send_message("✅ Disabled.", ephemeral=True)

    @vcnotify.command(name="add", description="Add a voice channel to watch.")
    async def v_add(interaction: discord.Interaction, voice_channel: discord.VoiceChannel):
        if not await require_admin(interaction): return
        watch = await db.get_json(interaction.guild.id, "vcnotify.channels", default=[])
        if voice_channel.id not in watch:
            watch.append(voice_channel.id)
            await db.set_json(interaction.guild.id, "vcnotify.channels", watch)
        await interaction.response.send_message("✅ Added.", ephemeral=True)

    @vcnotify.command(name="remove", description="Remove a voice channel.")
    async def v_remove(interaction: discord.Interaction, voice_channel: discord.VoiceChannel):
        if not await require_admin(interaction): return
        watch = await db.get_json(interaction.guild.id, "vcnotify.channels", default=[])
        watch = [c for c in watch if c != voice_channel.id]
        await db.set_json(interaction.guild.id, "vcnotify.channels", watch)
        await interaction.response.send_message("✅ Removed.", ephemeral=True)

    @vcnotify.command(name="list", description="List watched voice channels.")
    async def v_list(interaction: discord.Interaction):
        watch = await db.get_json(interaction.guild.id, "vcnotify.channels", default=[])
        if not watch:
            return await interaction.response.send_message("*(none)*", ephemeral=True)
        lines = []
        for cid in watch:
            cfg = await db.get_json(interaction.guild.id,
                                    f"vcnotify.cfg.{cid}", default={})
            tgt = cfg.get("target_channel_id")
            lines.append(f"• <#{cid}> → <#{tgt}>" if tgt else f"• <#{cid}> (no target)")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    async def _edit_vc_msg(interaction: discord.Interaction,
                           vc: discord.VoiceChannel, key: str, title: str):
        if not await require_admin(interaction): return
        cfg = await db.get_json(interaction.guild.id,
                                f"vcnotify.cfg.{vc.id}", default={})
        cur = cfg.get(key) or ""

        async def save(i: discord.Interaction, v: str):
            cfg2 = await db.get_json(i.guild.id, f"vcnotify.cfg.{vc.id}", default={})
            cfg2[key] = v
            await db.set_json(i.guild.id, f"vcnotify.cfg.{vc.id}", cfg2)
            await i.response.send_message("✅ Saved.", ephemeral=True)

        await interaction.response.send_modal(
            TextModal(title=title, default=cur, on_submit=save))

    @vcnotify.command(name="join-message", description="Set the join message for a VC.")
    async def v_join(interaction: discord.Interaction, voice_channel: discord.VoiceChannel):
        await _edit_vc_msg(interaction, voice_channel, "join_msg", "VC Join Message")

    @vcnotify.command(name="leave-message", description="Set the leave message for a VC.")
    async def v_leave(interaction: discord.Interaction, voice_channel: discord.VoiceChannel):
        await _edit_vc_msg(interaction, voice_channel, "leave_msg", "VC Leave Message")

    @vcnotify.command(name="test", description="Send a test VC message.")
    async def v_test(interaction: discord.Interaction, voice_channel: discord.VoiceChannel):
        if not await require_admin(interaction): return
        cfg = await db.get_json(interaction.guild.id,
                                f"vcnotify.cfg.{voice_channel.id}", default={})
        target = interaction.guild.get_channel(cfg.get("target_channel_id") or 0)
        if not isinstance(target, discord.TextChannel):
            return await interaction.response.send_message("No target set.", ephemeral=True)
        tpl = cfg.get("join_msg") or "🎧 {mention} joined **{channel_name}**."
        ph = build_user_placeholders(interaction.user, interaction.guild,
                                     channel_name=voice_channel.name)
        await safe_send(target, render(tpl, **ph))
        await interaction.response.send_message("✅ Sent.", ephemeral=True)

    @vcnotify.command(name="reset", description="Reset all VC notification config.")
    async def v_reset(interaction: discord.Interaction):
        if not await require_admin(interaction): return
        rows = await db.fetchall(
            "SELECT key FROM guild_config WHERE guild_id=? AND key LIKE 'vcnotify.cfg.%'",
            (interaction.guild.id,))
        for r in rows:
            await db.set_cfg(interaction.guild.id, r["key"], None)
        for k in ("vcnotify.channels", "vcnotify.enabled"):
            await db.set_cfg(interaction.guild.id, k, None)
        await interaction.response.send_message("✅ Reset.", ephemeral=True)

    # ================= TICKETS =================
    ticket = app_commands.Group(name="ticket", description="Ticket system")
    tree.add_command(ticket)

    @ticket.command(name="setup", description="Show ticket setup guidance.")
    async def t_setup(interaction: discord.Interaction):
        if not await require_admin(interaction): return
        await interaction.response.send_message(
            "Use `/ticket type` to add a ticket type, then `/ticket panel`.",
            ephemeral=True)

    @ticket.command(name="panel", description="Post the ticket panel.")
    async def t_panel(interaction: discord.Interaction,
                      channel: Optional[discord.TextChannel] = None,
                      title: Optional[str] = None,
                      description: Optional[str] = None):
        if not await require_admin(interaction): return
        ch = channel or interaction.channel
        embed = make_embed(
            title=title or "🎫 Support Tickets",
            description=description or "Click the button below to open a ticket.",
        )
        await ch.send(embed=embed, view=TicketCreateButton())
        await interaction.response.send_message(
            f"✅ Panel posted in {ch.mention}.", ephemeral=True)

    @ticket.command(name="type", description="Add or update a ticket type.")
    async def t_type(interaction: discord.Interaction,
                     name: str,
                     category: discord.CategoryChannel,
                     support_role: discord.Role,
                     emoji: Optional[str] = None,
                     message: Optional[str] = None):
        if not await require_admin(interaction): return
        await db.execute(
            "INSERT INTO ticket_types "
            "(guild_id, name, emoji, category_id, support_role_id, message) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(guild_id, name) DO UPDATE SET "
            "emoji=excluded.emoji, category_id=excluded.category_id, "
            "support_role_id=excluded.support_role_id, message=excluded.message",
            (interaction.guild.id, name, emoji, category.id, support_role.id, message))
        await interaction.response.send_message(
            f"✅ Ticket type `{name}` saved.", ephemeral=True)

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
        await db.set_cfg(interaction.guild.id, "ticket.limit", str(max(0, limit)))
        await interaction.response.send_message("✅ Set.", ephemeral=True)

    @ticket.command(name="cooldown", description="Cooldown between tickets (seconds).")
    async def t_cooldown(interaction: discord.Interaction, seconds: int):
        if not await require_admin(interaction): return
        await db.set_cfg(interaction.guild.id, "ticket.cooldown", str(max(0, seconds)))
        await interaction.response.send_message("✅ Set.", ephemeral=True)

    @ticket.command(name="claim", description="Claim the current ticket.")
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

    @ticket.command(name="close", description="Close the current ticket.")
    async def t_close(interaction: discord.Interaction):
        row = await db.fetchone("SELECT * FROM tickets WHERE channel_id=?",
                                (interaction.channel.id,))
        if not row:
            return await interaction.response.send_message("Not a ticket.", ephemeral=True)
        if interaction.user.id != row["user_id"] and not is_admin_or_mod(interaction.user):
            return await interaction.response.send_message("Not allowed.", ephemeral=True)
        await db.execute("UPDATE tickets SET status='closed' WHERE id=?", (row["id"],))
        try:
            ow = interaction.channel.overwrites_for(interaction.guild.default_role)
            ow.send_messages = False
            await interaction.channel.set_permissions(
                interaction.guild.default_role, overwrite=ow)
        except Exception:
            pass
        await interaction.response.send_message("🔒 Ticket closed.")

    @ticket.command(name="reopen", description="Reopen the current ticket.")
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

    @ticket.command(name="delete", description="Delete the current ticket channel.")
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

    @ticket.command(name="rename", description="Rename the current ticket channel.")
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
        try:
            await interaction.channel.set_permissions(
                member, view_channel=True, send_messages=True,
                read_message_history=True)
        except Exception as e:
            return await interaction.response.send_message(f"Failed: {e}", ephemeral=True)
        await interaction.response.send_message(f"✅ Added {member.mention}.")

    @ticket.command(name="remove", description="Remove a user from the ticket.")
    async def t_remove(interaction: discord.Interaction, member: discord.Member):
        if not is_admin_or_mod(interaction.user):
            return await interaction.response.send_message("Staff only.", ephemeral=True)
        try:
            await interaction.channel.set_permissions(member, overwrite=None)
        except Exception as e:
            return await interaction.response.send_message(f"Failed: {e}", ephemeral=True)
        await interaction.response.send_message(f"✅ Removed {member.mention}.")

    @ticket.command(name="transcript", description="Generate a transcript file.")
    async def t_transcript(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        lines = []
        async for m in interaction.channel.history(limit=1000, oldest_first=True):
            lines.append(f"[{fmt_dt(m.created_at)}] {m.author}: {m.content}")
        text = "\n".join(lines) or "(empty)"
        buf = io.BytesIO(text.encode("utf-8"))
        await interaction.followup.send(
            "📄 Transcript:",
            file=discord.File(buf, filename=f"transcript-{interaction.channel.id}.txt"),
            ephemeral=True)

    @ticket.command(name="reset", description="Reset all ticket config.")
    async def t_reset(interaction: discord.Interaction):
        if not await require_admin(interaction): return
        await db.execute("DELETE FROM ticket_types WHERE guild_id=?",
                         (interaction.guild.id,))
        for k in ("ticket.limit", "ticket.cooldown"):
            await db.set_cfg(interaction.guild.id, k, None)
        await interaction.response.send_message("✅ Reset.", ephemeral=True)

    # ================= VOUCHES =================
    vouch = app_commands.Group(name="vouch", description="Vouch system")
    tree.add_command(vouch)

    @vouch.command(name="setup", description="Set the vouch channel.")
    async def vo_setup(interaction: discord.Interaction, channel: discord.TextChannel):
        if not await require_admin(interaction): return
        await db.set_cfg(interaction.guild.id, "vouch.channel", channel.id)
        await interaction.response.send_message("✅ Set.", ephemeral=True)

    @vouch.command(name="panel", description="Show how to use vouches.")
    async def vo_panel(interaction: discord.Interaction):
        await interaction.response.send_message(
            "Users can vouch with `/vouch add user:@user comment:...`.",
            ephemeral=True)

    @vouch.command(name="add", description="Vouch for a user.")
    async def vo_add(interaction: discord.Interaction,
                     user: discord.Member, comment: str):
        await db.execute(
            "INSERT INTO vouches (guild_id, user_id, voucher_id, comment, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (interaction.guild.id, user.id, interaction.user.id, comment,
             to_iso(utcnow())))
        ch_id = await db.get_cfg(interaction.guild.id, "vouch.channel")
        if ch_id:
            ch = interaction.guild.get_channel(int(ch_id))
            if isinstance(ch, discord.TextChannel):
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
        lines = [f"• <@{r['user_id']}> — {r['comment'][:80]} (by <@{r['voucher_id']}>)"
                 for r in rows]
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @vouch.command(name="user", description="Show a user's vouches.")
    async def vo_user(interaction: discord.Interaction, user: discord.Member):
        rows = await db.fetchall(
            "SELECT * FROM vouches WHERE guild_id=? AND user_id=? "
            "ORDER BY id DESC LIMIT 20",
            (interaction.guild.id, user.id))
        if not rows:
            return await interaction.response.send_message("No vouches.", ephemeral=True)
        lines = [f"• {r['comment'][:80]} — <@{r['voucher_id']}> "
                 f"({fmt_dt(from_iso(r['created_at']))})" for r in rows]
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
        await db.execute("DELETE FROM vouches WHERE guild_id=?", (interaction.guild.id,))
        await db.set_cfg(interaction.guild.id, "vouch.channel", None)
        await interaction.response.send_message("✅ Reset.", ephemeral=True)

    # ================= SHOP =================
    shop = app_commands.Group(name="shop", description="Shop system")
    tree.add_command(shop)

    @shop.command(name="setup", description="Post the shop panel.")
    async def sh_setup(interaction: discord.Interaction, channel: discord.TextChannel):
        if not await require_admin(interaction): return
        await channel.send(
            embed=make_embed(title="🛒 Shop",
                             description="Browse products and buy using the buttons below."),
            view=ShopPanelView())
        await interaction.response.send_message("✅ Panel posted.", ephemeral=True)

    @shop.command(name="add", description="Add a product.")
    async def sh_add(interaction: discord.Interaction,
                     name: str, price: float, description: str = "",
                     stock: int = -1, image: Optional[str] = None):
        if not await require_admin(interaction): return
        pid = await db.execute(
            "INSERT INTO shop_products "
            "(guild_id, name, description, price, stock, active, image) "
            "VALUES (?, ?, ?, ?, ?, 1, ?)",
            (interaction.guild.id, name, description, price, stock, image))
        await interaction.response.send_message(
            f"✅ Added product `#{pid}`.", ephemeral=True)

    @shop.command(name="remove", description="Remove a product.")
    async def sh_remove(interaction: discord.Interaction, product_id: int):
        if not await require_admin(interaction): return
        await db.execute("DELETE FROM shop_products WHERE id=? AND guild_id=?",
                         (product_id, interaction.guild.id))
        await interaction.response.send_message("✅ Removed.", ephemeral=True)

    @shop.command(name="edit", description="Edit a product.")
    async def sh_edit(interaction: discord.Interaction, product_id: int,
                      name: Optional[str] = None, price: Optional[float] = None,
                      description: Optional[str] = None,
                      stock: Optional[int] = None,
                      active: Optional[bool] = None,
                      image: Optional[str] = None):
        if not await require_admin(interaction): return
        updates, params = [], []
        for col, val in (("name", name), ("price", price),
                         ("description", description), ("stock", stock),
                         ("image", image)):
            if val is not None:
                updates.append(f"{col}=?")
                params.append(val)
        if active is not None:
            updates.append("active=?")
            params.append(1 if active else 0)
        if not updates:
            return await interaction.response.send_message("Nothing to update.", ephemeral=True)
        params += [product_id, interaction.guild.id]
        await db.execute(f"UPDATE shop_products SET {', '.join(updates)} "
                         "WHERE id=? AND guild_id=?", tuple(params))
        await interaction.response.send_message("✅ Updated.", ephemeral=True)

    @shop.command(name="list", description="List products.")
    async def sh_list(interaction: discord.Interaction):
        rows = await db.fetchall(
            "SELECT * FROM shop_products WHERE guild_id=? ORDER BY id",
            (interaction.guild.id,))
        if not rows:
            return await interaction.response.send_message("No products.", ephemeral=True)
        lines = []
        for r in rows:
            stock = "∞" if r["stock"] < 0 else r["stock"]
            flag = "✅" if r["active"] else "❌"
            lines.append(f"{flag} `#{r['id']}` **{r['name']}** — "
                         f"{r['price']} (stock {stock})")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @shop.command(name="buy", description="Buy a product (creates an order).")
    async def sh_buy(interaction: discord.Interaction,
                     product_id: int, quantity: int = 1):
        p = await db.fetchone(
            "SELECT * FROM shop_products WHERE id=? AND guild_id=?",
            (product_id, interaction.guild.id))
        if not p or not p["active"]:
            return await interaction.response.send_message("Not available.", ephemeral=True)
        quantity = max(1, quantity)
        if p["stock"] >= 0 and p["stock"] < quantity:
            return await interaction.response.send_message("Not enough stock.", ephemeral=True)
        total = float(p["price"]) * quantity
        oid = await db.execute(
            "INSERT INTO orders (guild_id, user_id, product_id, quantity, price, "
            "status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)",
            (interaction.guild.id, interaction.user.id, product_id, quantity, total,
             to_iso(utcnow()), to_iso(utcnow())))
        if p["stock"] >= 0:
            await db.execute("UPDATE shop_products SET stock=stock-? WHERE id=?",
                             (quantity, product_id))
        await interaction.response.send_message(
            f"✅ Order `#{oid}` created. Total: {total}.", ephemeral=True)

    @shop.command(name="stock", description="Check a product's stock.")
    async def sh_stock(interaction: discord.Interaction, product_id: int):
        p = await db.fetchone(
            "SELECT * FROM shop_products WHERE id=? AND guild_id=?",
            (product_id, interaction.guild.id))
        if not p:
            return await interaction.response.send_message("Not found.", ephemeral=True)
        stock = "∞" if p["stock"] < 0 else p["stock"]
        await interaction.response.send_message(f"Stock: **{stock}**", ephemeral=True)

    @shop.command(name="panel", description="Post the shop panel in a channel.")
    async def sh_panel(interaction: discord.Interaction, channel: discord.TextChannel):
        if not await require_admin(interaction): return
        await channel.send(
            embed=make_embed(title="🛒 Shop", description="Browse products."),
            view=ShopPanelView())
        await interaction.response.send_message("✅ Panel posted.", ephemeral=True)

    @shop.command(name="reset", description="Reset all products.")
    async def sh_reset(interaction: discord.Interaction):
        if not await require_admin(interaction): return
        await db.execute("DELETE FROM shop_products WHERE guild_id=?",
                         (interaction.guild.id,))
        await interaction.response.send_message("✅ Reset.", ephemeral=True)

    # ================= ORDERS =================
    order = app_commands.Group(name="order", description="Order system")
    tree.add_command(order)

    @order.command(name="create", description="Create an order.")
    async def o_create(interaction: discord.Interaction,
                       product_id: int, quantity: int = 1):
        await sh_buy.callback(interaction, product_id, quantity)

    @order.command(name="list", description="List recent orders.")
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
        e.add_field(name="Created", value=fmt_dt(from_iso(r["created_at"])))
        await interaction.response.send_message(embed=e, ephemeral=True)

    @order.command(name="status", description="Set an order's status.")
    async def o_status(interaction: discord.Interaction, order_id: int, status: str):
        if not await require_admin(interaction): return
        await db.execute(
            "UPDATE orders SET status=?, updated_at=? WHERE id=? AND guild_id=?",
            (status, to_iso(utcnow()), order_id, interaction.guild.id))
        await interaction.response.send_message("✅ Updated.", ephemeral=True)

    @order.command(name="cancel", description="Cancel an order.")
    async def o_cancel(interaction: discord.Interaction, order_id: int):
        r = await db.fetchone("SELECT * FROM orders WHERE id=? AND guild_id=?",
                              (order_id, interaction.guild.id))
        if not r:
            return await interaction.response.send_message("Not found.", ephemeral=True)
        if r["user_id"] != interaction.user.id and not is_admin_or_mod(interaction.user):
            return await interaction.response.send_message("Not allowed.", ephemeral=True)
        await db.execute(
            "UPDATE orders SET status='cancelled', updated_at=? WHERE id=?",
            (to_iso(utcnow()), order_id))
        await interaction.response.send_message("✅ Cancelled.", ephemeral=True)

    @order.command(name="complete", description="Mark an order as complete.")
    async def o_complete(interaction: discord.Interaction, order_id: int):
        if not await require_admin(interaction): return
        await db.execute(
            "UPDATE orders SET status='completed', updated_at=? "
            "WHERE id=? AND guild_id=?",
            (to_iso(utcnow()), order_id, interaction.guild.id))
        await interaction.response.send_message("✅ Completed.", ephemeral=True)

    # ================= NOTIFICATIONS =================
    notify = app_commands.Group(name="notify", description="Notification system")
    tree.add_command(notify)

    @notify.command(name="setup", description="Set the notify channel.")
    async def n_setup(interaction: discord.Interaction, channel: discord.TextChannel):
        if not await require_admin(interaction): return
        await db.set_cfg(interaction.guild.id, "notify.channel", channel.id)
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
        ch_id = await db.get_cfg(interaction.guild.id, "notify.channel")
        ch = interaction.guild.get_channel(int(ch_id)) if ch_id else None
        if not isinstance(ch, discord.TextChannel):
            return await interaction.response.send_message("No channel.", ephemeral=True)
        msgs = await db.get_json(interaction.guild.id, "notify.messages", default={})
        tpl = msgs.get(name)
        if not tpl:
            return await interaction.response.send_message("No such preset.", ephemeral=True)
        ph = build_user_placeholders(interaction.user, interaction.guild)
        await safe_send(ch, render(tpl, **ph))
        await interaction.response.send_message("✅ Sent.", ephemeral=True)

    @notify.command(name="reset", description="Reset notify config.")
    async def n_reset(interaction: discord.Interaction):
        if not await require_admin(interaction): return
        for k in ("notify.channel", "notify.messages"):
            await db.set_cfg(interaction.guild.id, k, None)
        await interaction.response.send_message("✅ Reset.", ephemeral=True)

    # ================= AUTOMOD =================
    automod = app_commands.Group(name="automod", description="Auto moderation")
    tree.add_command(automod)

    @automod.command(name="setup", description="Enable automod.")
    async def am_setup(interaction: discord.Interaction):
        if not await require_admin(interaction): return
        await db.set_cfg(interaction.guild.id, "automod.enabled", "1")
        await interaction.response.send_message("✅ Enabled.", ephemeral=True)

    @automod.command(name="enable", description="Enable automod.")
    async def am_enable(interaction: discord.Interaction):
        if not await require_admin(interaction): return
        await db.set_cfg(interaction.guild.id, "automod.enabled", "1")
        await interaction.response.send_message("✅ Enabled.", ephemeral=True)

    @automod.command(name="disable", description="Disable automod.")
    async def am_disable(interaction: discord.Interaction):
        if not await require_admin(interaction): return
        await db.set_cfg(interaction.guild.id, "automod.enabled", "0")
        await interaction.response.send_message("✅ Disabled.", ephemeral=True)

    async def _am_setting(interaction: discord.Interaction, key: str, value: Any):
        if not await require_admin(interaction): return
        s = await db.get_json(interaction.guild.id, "automod.settings", default={})
        s[key] = value
        await db.set_json(interaction.guild.id, "automod.settings", s)
        await interaction.response.send_message("✅ Saved.", ephemeral=True)

    @automod.command(name="antispam", description="Toggle anti-spam.")
    async def am_spam(interaction: discord.Interaction, enabled: bool):
        await _am_setting(interaction, "antispam", enabled)

    @automod.command(name="links", description="Toggle link filtering.")
    async def am_links(interaction: discord.Interaction, enabled: bool):
        await _am_setting(interaction, "links", enabled)

    @automod.command(name="mentions", description="Toggle mention-spam filter.")
    async def am_mentions(interaction: discord.Interaction, enabled: bool):
        await _am_setting(interaction, "mentions", enabled)

    @automod.command(name="invites", description="Toggle invite filtering.")
    async def am_invites(interaction: discord.Interaction, enabled: bool):
        await _am_setting(interaction, "invites", enabled)

    @automod.command(name="words", description="Set banned words (comma-separated).")
    async def am_words(interaction: discord.Interaction, words: str):
        await _am_setting(interaction, "words",
                          [w.strip() for w in words.split(",") if w.strip()])

    @automod.command(name="actions",
                     description="Set action: delete | warn | timeout | kick | ban")
    async def am_actions(interaction: discord.Interaction, action: str):
        action = action.lower()
        if action not in ("delete", "warn", "timeout", "kick", "ban"):
            return await interaction.response.send_message("Invalid action.", ephemeral=True)
        await _am_setting(interaction, "action", action)

    @automod.command(name="whitelist", description="Whitelist a channel or role.")
    async def am_whitelist(interaction: discord.Interaction,
                           channel: Optional[discord.TextChannel] = None,
                           role: Optional[discord.Role] = None):
        if not await require_admin(interaction): return
        wl = await db.get_json(interaction.guild.id, "automod.whitelist", default={})
        wl.setdefault("channels", [])
        wl.setdefault("roles", [])
        if channel and channel.id not in wl["channels"]:
            wl["channels"].append(channel.id)
        if role and role.id not in wl["roles"]:
            wl["roles"].append(role.id)
        await db.set_json(interaction.guild.id, "automod.whitelist", wl)
        await interaction.response.send_message("✅ Whitelisted.", ephemeral=True)

    @automod.command(name="reset", description="Reset automod.")
    async def am_reset(interaction: discord.Interaction):
        if not await require_admin(interaction): return
        for k in ("automod.enabled", "automod.settings", "automod.whitelist"):
            await db.set_cfg(interaction.guild.id, k, None)
        await interaction.response.send_message("✅ Reset.", ephemeral=True)

    # ================= MODERATION =================
    async def _create_case(guild_id: int, user_id: int, mod_id: int,
                           action: str, reason: str) -> int:
        return await db.execute(
            "INSERT INTO cases "
            "(guild_id, user_id, moderator_id, action, reason, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (guild_id, user_id, mod_id, action, reason, to_iso(utcnow())))

    async def _try_action_dm(guild: discord.Guild, member: discord.Member,
                             template_key: str, **extra):
        if await db.get_cfg(guild.id, "actiondm.enabled", "0") != "1":
            return
        tpl = await db.get_cfg(guild.id, template_key)
        if not tpl:
            return
        ph = build_user_placeholders(member, guild, **extra)
        try:
            await safe_send(member, render(tpl, **ph))
        except Exception:
            pass  # DM failures never abort moderation

    @tree.command(name="warn", description="Warn a member.")
    @app_commands.default_permissions(moderate_members=True)
    async def warn(interaction: discord.Interaction,
                   member: discord.Member, reason: str = "No reason"):
        if not is_admin_or_mod(interaction.user):
            return await interaction.response.send_message("No permission.", ephemeral=True)
        ok, why = hierarchy_ok(interaction.guild, member)
        if not ok:
            return await interaction.response.send_message(f"❌ {why}", ephemeral=True)
        wid = await db.execute(
            "INSERT INTO warnings "
            "(guild_id, user_id, moderator_id, reason, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (interaction.guild.id, member.id, interaction.user.id, reason,
             to_iso(utcnow())))
        cid = await _create_case(interaction.guild.id, member.id,
                                 interaction.user.id, "warn", reason)
        await interaction.response.send_message(
            f"⚠️ Warned {member.mention} (warning #{wid}, case #{cid}): {reason}")
        await log_guild(bot, interaction.guild, "warnings", "Member Warned",
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
        lines = [f"`#{r['id']}` by <@{r['moderator_id']}> — {r['reason']}"
                 for r in rows[:25]]
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @tree.command(name="clearwarnings", description="Clear warnings for a member.")
    async def clearwarnings(interaction: discord.Interaction, member: discord.Member):
        if not is_admin_or_mod(interaction.user):
            return await interaction.response.send_message("No permission.", ephemeral=True)
        await db.execute("DELETE FROM warnings WHERE guild_id=? AND user_id=?",
                         (interaction.guild.id, member.id))
        await interaction.response.send_message("✅ Cleared.", ephemeral=True)

    @tree.command(name="timeout", description="Timeout a member.")
    @app_commands.default_permissions(moderate_members=True)
    async def timeout_cmd(interaction: discord.Interaction,
                          member: discord.Member,
                          duration: str, reason: str = "No reason"):
        if not is_admin_or_mod(interaction.user):
            return await interaction.response.send_message("No permission.", ephemeral=True)
        ok, why = hierarchy_ok(interaction.guild, member)
        if not ok:
            return await interaction.response.send_message(f"❌ {why}", ephemeral=True)
        secs = parse_dur(duration)
        if not secs or secs < 1 or secs > 28 * 86400:
            return await interaction.response.send_message(
                "Invalid duration (max 28d).", ephemeral=True)
        until = utcnow() + timedelta(seconds=secs)
        try:
            await member.timeout(timedelta(seconds=secs), reason=reason)
        except discord.Forbidden:
            return await interaction.response.send_message("❌ Forbidden.", ephemeral=True)
        cid = await _create_case(interaction.guild.id, member.id,
                                 interaction.user.id, "timeout", reason)
        tid = await db.execute(
            "INSERT INTO timeouts "
            "(guild_id, user_id, moderator_id, reason, ends_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (interaction.guild.id, member.id, interaction.user.id,
             reason, to_iso(until)))
        await _try_action_dm(
            interaction.guild, member, "actiondm.timeout_msg",
            moderator=interaction.user.mention, reason=reason,
            duration=duration, timeout_end=fmt_dt(until),
            case_id=str(cid),
        )
        await bot.scheduler.schedule(
            "timeout_end", until + timedelta(seconds=5),
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
        try:
            await member.timeout(None, reason=f"Timeout removed by {interaction.user}")
        except discord.Forbidden:
            return await interaction.response.send_message("❌ Forbidden.", ephemeral=True)
        await interaction.response.send_message("✅ Timeout removed.")

    @tree.command(name="kick", description="Kick a member.")
    @app_commands.default_permissions(kick_members=True)
    async def kick(interaction: discord.Interaction,
                   member: discord.Member, reason: str = "No reason"):
        if not is_admin_or_mod(interaction.user):
            return await interaction.response.send_message("No permission.", ephemeral=True)
        ok, why = hierarchy_ok(interaction.guild, member)
        if not ok:
            return await interaction.response.send_message(f"❌ {why}", ephemeral=True)
        cid = await _create_case(interaction.guild.id, member.id,
                                 interaction.user.id, "kick", reason)
        # DM first — failure never cancels the kick
        await _try_action_dm(
            interaction.guild, member, "actiondm.kick_msg",
            moderator=interaction.user.mention, reason=reason, case_id=str(cid))
        try:
            await member.kick(reason=reason)
        except discord.Forbidden:
            return await interaction.response.send_message("❌ Forbidden.", ephemeral=True)
        await interaction.response.send_message(f"👢 Kicked {member} (case #{cid}).")
        await log_guild(bot, interaction.guild, "kicks", "Member Kicked",
                        f"**Member:** {member}\n"
                        f"**By:** {interaction.user.mention}\n"
                        f"**Reason:** {reason}")

    @tree.command(name="ban", description="Ban a member.")
    @app_commands.default_permissions(ban_members=True)
    async def ban(interaction: discord.Interaction,
                  member: discord.Member, reason: str = "No reason"):
        if not is_admin_or_mod(interaction.user):
            return await interaction.response.send_message("No permission.", ephemeral=True)
        ok, why = hierarchy_ok(interaction.guild, member)
        if not ok:
            return await interaction.response.send_message(f"❌ {why}", ephemeral=True)
        cid = await _create_case(interaction.guild.id, member.id,
                                 interaction.user.id, "ban", reason)
        await _try_action_dm(
            interaction.guild, member, "actiondm.ban_msg",
            moderator=interaction.user.mention, reason=reason, case_id=str(cid))
        try:
            await member.ban(reason=reason)
        except discord.Forbidden:
            return await interaction.response.send_message("❌ Forbidden.", ephemeral=True)
        await interaction.response.send_message(f"🔨 Banned {member} (case #{cid}).")
        await log_guild(bot, interaction.guild, "bans", "Member Banned",
                        f"**Member:** {member}\n"
                        f"**By:** {interaction.user.mention}\n"
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
        try:
            await interaction.guild.unban(discord.Object(id=uid), reason=reason)
        except discord.NotFound:
            return await interaction.response.send_message("User not banned.", ephemeral=True)
        except discord.Forbidden:
            return await interaction.response.send_message("❌ Forbidden.", ephemeral=True)
        await interaction.response.send_message(f"✅ Unbanned <@{uid}>.")
        await log_guild(bot, interaction.guild, "unbans", "Member Unbanned",
                        f"**User:** <@{uid}>\n"
                        f"**By:** {interaction.user.mention}")

    @tree.command(name="purge", description="Bulk-delete messages.")
    @app_commands.default_permissions(manage_messages=True)
    async def purge(interaction: discord.Interaction, amount: int,
                    member: Optional[discord.Member] = None):
        if not is_admin_or_mod(interaction.user):
            return await interaction.response.send_message("No permission.", ephemeral=True)
        amount = max(1, min(amount, 500))
        await interaction.response.defer(ephemeral=True)

        def check(m: discord.Message) -> bool:
            return member is None or m.author.id == member.id

        try:
            deleted = await interaction.channel.purge(limit=amount, check=check)
        except discord.Forbidden:
            return await interaction.followup.send("❌ Forbidden.", ephemeral=True)
        await interaction.followup.send(
            f"🗑 Deleted {len(deleted)} messages.", ephemeral=True)

    @tree.command(name="slowmode", description="Set slowmode for a channel.")
    @app_commands.default_permissions(manage_channels=True)
    async def slowmode(interaction: discord.Interaction, seconds: int,
                       channel: Optional[discord.TextChannel] = None):
        if not is_admin_or_mod(interaction.user):
            return await interaction.response.send_message("No permission.", ephemeral=True)
        ch = channel or interaction.channel
        try:
            await ch.edit(slowmode_delay=max(0, min(seconds, 21600)))
        except Exception as e:
            return await interaction.response.send_message(f"Failed: {e}", ephemeral=True)
        await interaction.response.send_message(
            f"✅ Slowmode = {seconds}s in {ch.mention}.")

    @tree.command(name="lock", description="Lock a channel.")
    @app_commands.default_permissions(manage_channels=True)
    async def lock(interaction: discord.Interaction,
                   channel: Optional[discord.TextChannel] = None):
        if not is_admin_or_mod(interaction.user):
            return await interaction.response.send_message("No permission.", ephemeral=True)
        ch = channel or interaction.channel
        try:
            ow = ch.overwrites_for(interaction.guild.default_role)
            ow.send_messages = False
            await ch.set_permissions(interaction.guild.default_role, overwrite=ow)
        except Exception as e:
            return await interaction.response.send_message(f"Failed: {e}", ephemeral=True)
        await interaction.response.send_message(f"🔒 Locked {ch.mention}.")

    @tree.command(name="unlock", description="Unlock a channel.")
    @app_commands.default_permissions(manage_channels=True)
    async def unlock(interaction: discord.Interaction,
                     channel: Optional[discord.TextChannel] = None):
        if not is_admin_or_mod(interaction.user):
            return await interaction.response.send_message("No permission.", ephemeral=True)
        ch = channel or interaction.channel
        try:
            ow = ch.overwrites_for(interaction.guild.default_role)
            ow.send_messages = None
            await ch.set_permissions(interaction.guild.default_role, overwrite=ow)
        except Exception as e:
            return await interaction.response.send_message(f"Failed: {e}", ephemeral=True)
        await interaction.response.send_message(f"🔓 Unlocked {ch.mention}.")

    @tree.command(name="lockdown", description="Lock every text channel in the server.")
    @app_commands.default_permissions(administrator=True)
    async def lockdown(interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        n = 0
        for ch in interaction.guild.text_channels:
            try:
                ow = ch.overwrites_for(interaction.guild.default_role)
                ow.send_messages = False
                await ch.set_permissions(interaction.guild.default_role, overwrite=ow)
                n += 1
            except Exception:
                pass
        await interaction.followup.send(f"🔒 Lockdown: {n} channels.", ephemeral=True)

    @tree.command(name="unlockdown", description="Lift a server lockdown.")
    @app_commands.default_permissions(administrator=True)
    async def unlockdown(interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message("Admin only.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        n = 0
        for ch in interaction.guild.text_channels:
            try:
                ow = ch.overwrites_for(interaction.guild.default_role)
                ow.send_messages = None
                await ch.set_permissions(interaction.guild.default_role, overwrite=ow)
                n += 1
            except Exception:
                pass
        await interaction.followup.send(f"🔓 Unlockdown: {n} channels.", ephemeral=True)

    # ================= LOGGING =================
    logging_grp = app_commands.Group(name="logging", description="Logging")
    tree.add_command(logging_grp)

    @logging_grp.command(name="setup", description="Set log channel & enable default events.")
    async def lg_setup(interaction: discord.Interaction, channel: discord.TextChannel):
        if not await require_admin(interaction): return
        await db.set_cfg(interaction.guild.id, "logging.channel", channel.id)
        await db.set_cfg(interaction.guild.id, "logging.enabled", "1")
        await db.set_json(interaction.guild.id, "logging.events", [
            "joins", "leaves", "messages", "warnings", "kicks", "bans",
            "unbans", "tickets", "shop", "automod",
        ])
        await interaction.response.send_message("✅ Logging configured.", ephemeral=True)

    @logging_grp.command(name="enable", description="Enable logging.")
    async def lg_enable(interaction: discord.Interaction):
        if not await require_admin(interaction): return
        await db.set_cfg(interaction.guild.id, "logging.enabled", "1")
        await interaction.response.send_message("✅ Enabled.", ephemeral=True)

    @logging_grp.command(name="disable", description="Disable logging.")
    async def lg_disable(interaction: discord.Interaction):
        if not await require_admin(interaction): return
        await db.set_cfg(interaction.guild.id, "logging.enabled", "0")
        await interaction.response.send_message("✅ Disabled.", ephemeral=True)

    @logging_grp.command(name="channel", description="Set the log channel.")
    async def lg_channel(interaction: discord.Interaction, channel: discord.TextChannel):
        if not await require_admin(interaction): return
        await db.set_cfg(interaction.guild.id, "logging.channel", channel.id)
        await interaction.response.send_message("✅ Set.", ephemeral=True)

    @logging_grp.command(name="events", description="Comma-separated event list.")
    async def lg_events(interaction: discord.Interaction, events: str):
        if not await require_admin(interaction): return
        evs = [e.strip() for e in events.split(",") if e.strip()]
        await db.set_json(interaction.guild.id, "logging.events", evs)
        await interaction.response.send_message("✅ Saved.", ephemeral=True)

    @logging_grp.command(name="reset", description="Reset logging.")
    async def lg_reset(interaction: discord.Interaction):
        if not await require_admin(interaction): return
        for k in ("logging.channel", "logging.events", "logging.enabled"):
            await db.set_cfg(interaction.guild.id, k, None)
        await interaction.response.send_message("✅ Reset.", ephemeral=True)

    # ================= REACTION ROLES =================
    rr = app_commands.Group(name="reactionrole",
                           description="Reaction / button role panels")
    tree.add_command(rr)

    @rr.command(name="setup", description="Create a new reaction-role panel.")
    async def rr_setup(interaction: discord.Interaction,
                       title: str, description: str = "",
                       mode: str = "button"):
        if not await require_admin(interaction): return
        mode = mode.lower()
        if mode not in ("button", "select"):
            return await interaction.response.send_message(
                "mode must be `button` or `select`.", ephemeral=True)
        pid = await db.execute(
            "INSERT INTO reaction_panels "
            "(guild_id, channel_id, title, description, mode) "
            "VALUES (?, ?, ?, ?, ?)",
            (interaction.guild.id, interaction.channel.id,
             title, description, mode))
        await interaction.response.send_message(
            f"✅ Panel `#{pid}` created. "
            f"Add roles with `/reactionrole add panel_id:{pid} role:@role`.",
            ephemeral=True)

    @rr.command(name="create", description="Post an existing panel.")
    async def rr_create(interaction: discord.Interaction, panel_id: int,
                        channel: Optional[discord.TextChannel] = None):
        if not await require_admin(interaction): return
        p = await db.fetchone(
            "SELECT * FROM reaction_panels WHERE id=? AND guild_id=?",
            (panel_id, interaction.guild.id))
        if not p:
            return await interaction.response.send_message("Panel not found.", ephemeral=True)
        roles = await db.fetchall(
            "SELECT role_id, label, emoji FROM reaction_roles WHERE panel_id=?",
            (panel_id,))
        view = ReactionRoleView(panel_id, [dict(r) for r in roles],
                                p["mode"] or "button")
        ch = channel or interaction.channel
        msg = await ch.send(
            embed=make_embed(title=p["title"] or "Roles",
                             description=p["description"] or None),
            view=view)
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
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(panel_id, role_id) DO UPDATE SET "
            "label=excluded.label, emoji=excluded.emoji",
            (panel_id, role.id, label or role.name, emoji))
        await interaction.response.send_message(
            "✅ Added (re-post the panel to refresh).", ephemeral=True)

    @rr.command(name="remove", description="Remove a role from a panel.")
    async def rr_remove(interaction: discord.Interaction,
                        panel_id: int, role: discord.Role):
        if not await require_admin(interaction): return
        await db.execute(
            "DELETE FROM reaction_roles WHERE panel_id=? AND role_id=?",
            (panel_id, role.id))
        await interaction.response.send_message("✅ Removed.", ephemeral=True)

    @rr.command(name="list", description="List reaction-role panels.")
    async def rr_list(interaction: discord.Interaction):
        rows = await db.fetchall(
            "SELECT * FROM reaction_panels WHERE guild_id=?",
            (interaction.guild.id,))
        if not rows:
            return await interaction.response.send_message("*(none)*", ephemeral=True)
        lines = []
        for p in rows:
            cnt = await db.fetchone(
                "SELECT COUNT(*) c FROM reaction_roles WHERE panel_id=?",
                (p["id"],))
            lines.append(f"• `#{p['id']}` {p['title']} — {cnt['c']} role(s)")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @rr.command(name="reset", description="Reset reaction-role panels.")
    async def rr_reset(interaction: discord.Interaction):
        if not await require_admin(interaction): return
        ids = await db.fetchall(
            "SELECT id FROM reaction_panels WHERE guild_id=?",
            (interaction.guild.id,))
        for r in ids:
            await db.execute("DELETE FROM reaction_roles WHERE panel_id=?",
                             (r["id"],))
        await db.execute("DELETE FROM reaction_panels WHERE guild_id=?",
                         (interaction.guild.id,))
        await interaction.response.send_message("✅ Reset.", ephemeral=True)

    # ================= GIVEAWAYS =================
    giveaway = app_commands.Group(name="giveaway", description="Giveaways")
    tree.add_command(giveaway)

    @giveaway.command(name="create", description="Create a giveaway.")
    async def gw_create(interaction: discord.Interaction,
                        prize: str, duration: str, winners: int = 1,
                        channel: Optional[discord.TextChannel] = None):
        if not await require_admin(interaction): return
        secs = parse_dur(duration)
        if not secs:
            return await interaction.response.send_message("Invalid duration.", ephemeral=True)
        winners = max(1, winners)
        ch = channel or interaction.channel
        ends_at = utcnow() + timedelta(seconds=secs)
        embed = make_embed(
            title=f"🎉 {prize}"[:256],
            description=(f"React with the button to enter!\n"
                         f"**Winners:** {winners}\n**Ends:** {fmt_dt(ends_at)}"))
        gid = await db.execute(
            "INSERT INTO giveaways "
            "(guild_id, channel_id, prize, winners, host_id, ends_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (interaction.guild.id, ch.id, prize, winners,
             interaction.user.id, to_iso(ends_at)))
        view = GiveawayView()
        msg = await ch.send(embed=embed, view=view)
        await db.execute("UPDATE giveaways SET message_id=? WHERE id=?",
                         (msg.id, gid))
        bot.add_view(view, message_id=msg.id)
        await bot.scheduler.schedule(
            "giveaway_end", ends_at, {"giveaway_id": gid}, interaction.guild.id)
        await interaction.response.send_message(
            f"✅ Giveaway `#{gid}` started.", ephemeral=True)

    @giveaway.command(name="end", description="End a giveaway now.")
    async def gw_end(interaction: discord.Interaction, giveaway_id: int):
        if not await require_admin(interaction): return
        await end_giveaway(bot, giveaway_id)
        await interaction.response.send_message("✅ Ended.", ephemeral=True)

    @giveaway.command(name="reroll", description="Reroll the winners.")
    async def gw_reroll(interaction: discord.Interaction, giveaway_id: int):
        if not await require_admin(interaction): return
        await end_giveaway(bot, giveaway_id, reroll=True)
        await interaction.response.send_message("✅ Rerolled.", ephemeral=True)

    @giveaway.command(name="list", description="List giveaways.")
    async def gw_list(interaction: discord.Interaction):
        rows = await db.fetchall(
            "SELECT * FROM giveaways WHERE guild_id=? ORDER BY id DESC",
            (interaction.guild.id,))
        if not rows:
            return await interaction.response.send_message("*(none)*", ephemeral=True)
        lines = [
            f"• `#{r['id']}` **{r['prize']}** — "
            f"{'ended' if r['ended'] else fmt_dt(from_iso(r['ends_at']))}"
            for r in rows
        ]
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    # ================= CUSTOM COMMANDS =================
    cc = app_commands.Group(name="customcommand", description="Custom text commands")
    tree.add_command(cc)

    @cc.command(name="create", description="Create a custom command.")
    async def cc_create(interaction: discord.Interaction, name: str,
                        response: str, embed: bool = False):
        if not await require_admin(interaction): return
        await db.execute(
            "INSERT INTO custom_commands "
            "(guild_id, name, response, embed, enabled) "
            "VALUES (?, ?, ?, ?, 1) "
            "ON CONFLICT(guild_id, name) DO UPDATE SET "
            "response=excluded.response, embed=excluded.embed",
            (interaction.guild.id, name.lower(), response, 1 if embed else 0))
        await interaction.response.send_message(
            f"✅ Created `!{name}` (supports newlines / placeholders).",
            ephemeral=True)

    @cc.command(name="edit", description="Edit a custom command.")
    async def cc_edit(interaction: discord.Interaction, name: str,
                      response: Optional[str] = None,
                      embed: Optional[bool] = None,
                      enabled: Optional[bool] = None):
        if not await require_admin(interaction): return
        updates, params = [], []
        if response is not None:
            updates.append("response=?"); params.append(response)
        if embed is not None:
            updates.append("embed=?"); params.append(1 if embed else 0)
        if enabled is not None:
            updates.append("enabled=?"); params.append(1 if enabled else 0)
        if not updates:
            return await interaction.response.send_message("Nothing to update.", ephemeral=True)
        params += [interaction.guild.id, name.lower()]
        await db.execute(
            f"UPDATE custom_commands SET {', '.join(updates)} "
            "WHERE guild_id=? AND name=?", tuple(params))
        await interaction.response.send_message("✅ Updated.", ephemeral=True)

    @cc.command(name="delete", description="Delete a custom command.")
    async def cc_delete(interaction: discord.Interaction, name: str):
        if not await require_admin(interaction): return
        await db.execute(
            "DELETE FROM custom_commands WHERE guild_id=? AND name=?",
            (interaction.guild.id, name.lower()))
        await interaction.response.send_message("✅ Deleted.", ephemeral=True)

    @cc.command(name="list", description="List custom commands.")
    async def cc_list(interaction: discord.Interaction):
        rows = await db.fetchall(
            "SELECT name, enabled FROM custom_commands WHERE guild_id=?",
            (interaction.guild.id,))
        if not rows:
            return await interaction.response.send_message("*(none)*", ephemeral=True)
        lines = [f"• `!{r['name']}` {'🟢' if r['enabled'] else '⚪'}" for r in rows]
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @cc.command(name="reset", description="Reset custom commands.")
    async def cc_reset(interaction: discord.Interaction):
        if not await require_admin(interaction): return
        await db.execute("DELETE FROM custom_commands WHERE guild_id=?",
                         (interaction.guild.id,))
        await interaction.response.send_message("✅ Reset.", ephemeral=True)

    # ================= ANNOUNCEMENTS =================
    ann = app_commands.Group(name="announce", description="Announcements")
    tree.add_command(ann)

    @ann.command(name="create", description="Create an announcement draft.")
    async def a_create(interaction: discord.Interaction,
                       channel: discord.TextChannel, title: str, body: str,
                       image: Optional[str] = None, footer: Optional[str] = None):
        if not await require_admin(interaction): return
        aid = await db.execute(
            "INSERT INTO announcements "
            "(guild_id, channel_id, title, body, image, footer, sent) "
            "VALUES (?, ?, ?, ?, ?, ?, 0)",
            (interaction.guild.id, channel.id, title, body, image, footer))
        await interaction.response.send_message(
            f"✅ Announcement `#{aid}` created.", ephemeral=True)

    @ann.command(name="edit", description="Edit an announcement.")
    async def a_edit(interaction: discord.Interaction, announcement_id: int,
                     title: Optional[str] = None, body: Optional[str] = None,
                     image: Optional[str] = None, footer: Optional[str] = None):
        if not await require_admin(interaction): return
        updates, params = [], []
        for col, val in (("title", title), ("body", body),
                         ("image", image), ("footer", footer)):
            if val is not None:
                updates.append(f"{col}=?")
                params.append(val)
        if not updates:
            return await interaction.response.send_message("Nothing to update.", ephemeral=True)
        params += [announcement_id, interaction.guild.id]
        await db.execute(
            f"UPDATE announcements SET {', '.join(updates)} "
            "WHERE id=? AND guild_id=?", tuple(params))
        await interaction.response.send_message("✅ Updated.", ephemeral=True)

    @ann.command(name="send", description="Send an announcement now.")
    async def a_send(interaction: discord.Interaction, announcement_id: int):
        if not await require_admin(interaction): return
        await _task_announcement(bot, {"announcement_id": announcement_id})
        await interaction.response.send_message("✅ Sent.", ephemeral=True)

    @ann.command(name="schedule", description="Schedule an announcement.")
    async def a_schedule(interaction: discord.Interaction, announcement_id: int,
                         in_duration: str):
        if not await require_admin(interaction): return
        secs = parse_dur(in_duration)
        if not secs:
            return await interaction.response.send_message("Invalid duration.", ephemeral=True)
        run_at = utcnow() + timedelta(seconds=secs)
        await db.execute(
            "UPDATE announcements SET scheduled_at=? WHERE id=? AND guild_id=?",
            (to_iso(run_at), announcement_id, interaction.guild.id))
        # Cancel any prior pending schedule for the same announcement
        await bot.scheduler.cancel_pending(
            "announcement", f'%"announcement_id": {announcement_id}%')
        await bot.scheduler.schedule(
            "announcement", run_at, {"announcement_id": announcement_id},
            interaction.guild.id)
        await interaction.response.send_message(
            f"✅ Scheduled for {fmt_dt(run_at)}.", ephemeral=True)

    @ann.command(name="cancel", description="Cancel a scheduled announcement.")
    async def a_cancel(interaction: discord.Interaction, announcement_id: int):
        if not await require_admin(interaction): return
        await bot.scheduler.cancel_pending(
            "announcement", f'%"announcement_id": {announcement_id}%')
        await interaction.response.send_message("✅ Cancelled.", ephemeral=True)


# ============================================================
# STARTUP
# ============================================================

async def main() -> None:
    if not TOKEN:
        log.error("DISCORD_TOKEN is missing. Set it in your environment or .env file.")
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
