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
    """Parse strings like 10s, 5m, 2h, 1d, or combinations 1h30m into seconds."""
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
    """Replace {key} placeholders. Preserves newlines and whitespace exactly."""
    if not text:
        return ""
    out = text
    for k, v in kw.items():
        if v is None:
            v = ""
        out = out.replace("{" + k + "}", str(v))
    return out


def chunk_message(text: str, limit: int = 1990) -> list[str]:
    """Split a message into <=limit chunks without destroying newlines."""
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
    """Can the bot act on `target`?"""
    me = guild.me
    if target.id == guild.owner_id:
        return False, "Cannot act on the server owner."
    if target.id == me.id:
        return False, "Cannot act on myself."
    if target.id == guild.owner_id:
        return False, "Cannot act on the owner."
    if target.top_role >= me.top_role:
        return False, "Target has a role equal or higher than mine."
    return True, ""


async def _safe_reply(interaction: discord.Interaction, content: str = None, *, embed=None,
                      view=None, ephemeral: bool = True, file=None):
    """Send a message on an interaction regardless of whether it's been acknowledged."""
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
        else:
            return await interaction.response.send_message(**kwargs)
    except Exception:
        log.exception("Failed to reply to interaction")
        return None


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
CREATE TABLE IF NOT EXISTS tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    type TEXT,
    status TEXT DEFAULT 'open',
    claimed_by INTEGER,
    created_at TEXT NOT NULL,
    close_reason TEXT,
    ticket_number INTEGER
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
        # Lightweight migrations (safe / idempotent).
        for stmt in (
            "ALTER TABLE tickets ADD COLUMN close_reason TEXT",
            "ALTER TABLE tickets ADD COLUMN ticket_number INTEGER",
        ):
            try:
                await self.conn.execute(stmt)
                await self.conn.commit()
            except Exception:
                pass  # column already exists
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
# Persistent Views
# =====================================================================

class TicketCreateButton(discord.ui.View):
    """Persistent 'Open Ticket' button that shows a type select on click."""
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
            (guild.id,),
        )
        if not rows:
            await interaction.response.send_message(
                "No ticket types configured. Ask an admin to run `/ticket type`.",
                ephemeral=True,
            )
            return
        opts = []
        for r in rows[:25]:
            opts.append(discord.SelectOption(
                label=r["name"][:100],
                emoji=(r["emoji"] or None),
                value=r["name"][:100],
            ))
        select = discord.ui.Select(placeholder="Pick a ticket type…", options=opts)

        async def cb(sel_interaction: discord.Interaction):
            await _open_ticket(sel_interaction, sel_interaction.data["values"][0])

        select.callback = cb
        view = discord.ui.View(timeout=60)
        view.add_item(select)
        await interaction.response.send_message(
            "Choose a ticket type to open:", view=view, ephemeral=True
        )


class TicketPanelModal(discord.ui.Modal, title="Ticket Panel"):
    """Collects the panel title/description as real multi-line text."""
    panel_title = discord.ui.TextInput(
        label="Title",
        style=discord.TextStyle.short,
        max_length=256,
        required=False,
    )
    panel_description = discord.ui.TextInput(
        label="Description",
        style=discord.TextStyle.paragraph,
        max_length=4000,
        required=False,
    )

    def __init__(self, channel: discord.TextChannel):
        super().__init__()
        self.channel = channel

    async def on_submit(self, interaction: discord.Interaction):
        embed = make_embed(
            title=self.panel_title.value or None,
            description=self.panel_description.value or None,
        )
        await self.channel.send(embed=embed, view=TicketCreateButton())
        await interaction.response.send_message(
            f"✅ Panel posted in {self.channel.mention}.", ephemeral=True
        )


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
        return await interaction.response.send_message("Type not found.", ephemeral=True)

    limit = int(await db.get_config(guild.id, "ticket.limit", "1") or 1)
    open_count = await db.fetchone(
        "SELECT COUNT(*) c FROM tickets WHERE guild_id=? AND user_id=? AND status='open'",
        (guild.id, interaction.user.id),
    )
    if open_count and open_count["c"] >= limit:
        return await interaction.response.send_message(
            f"You already have {open_count['c']} open ticket(s).", ephemeral=True
        )

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
                    f"Cooldown: try again in {fmt_duration(rem)}.", ephemeral=True
                )

    category = guild.get_channel(row["category_id"]) if row["category_id"] else None
    support_role = guild.get_role(row["support_role_id"]) if row["support_role_id"] else None

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        interaction.user: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, attach_files=True, read_message_history=True
        ),
        guild.me: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, manage_channels=True, manage_permissions=True
        ),
    }
    if support_role:
        overwrites[support_role] = discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True
        )

    # Per-guild sequential ticket number.
    counter = int(await db.get_config(guild.id, "ticket.counter", "0") or 0) + 1
    await db.set_config(guild.id, "ticket.counter", str(counter))
    ticket_num_str = f"#{counter:03d}"

    try:
        channel = await guild.create_text_channel(
            name=f"ticket-{interaction.user.name}"[:90],
            category=category,
            overwrites=overwrites,
            reason=f"Ticket by {interaction.user}",
        )
    except discord.Forbidden:
        return await interaction.response.send_message(
            "I lack permission to create the ticket channel.", ephemeral=True
        )

    created_at = now_utc()
    tid = await db.execute(
        "INSERT INTO tickets (guild_id, channel_id, user_id, type, status, created_at, ticket_number) "
        "VALUES (?, ?, ?, ?, 'open', ?, ?)",
        (guild.id, channel.id, interaction.user.id, ttype, iso(created_at), counter),
    )

    body = row["message"] or (
        f"Hey {interaction.user.mention}, thanks for opening a **{ttype}** ticket!\n\n"
        "Support will be with you shortly. Please describe your issue."
    )

    info_embed = discord.Embed(
        title="× 〻 Ticket Information 〻 ×",
        description=body,
        color=0x5865F2,
        timestamp=created_at,
    )
    info_embed.add_field(name="𑣲 Ticket Number", value=ticket_num_str, inline=False)
    info_embed.add_field(name="𑣲 Category", value=ttype, inline=False)
    info_embed.add_field(name="𑣲 Created By", value=interaction.user.mention, inline=False)
    info_embed.add_field(name="𑣲 Created At", value=fmt_dt_human(created_at), inline=False)

    cfg = await _ticket_room_cfg(db, guild.id)
    view = TicketRoomView(tid, cfg)
    try:
        await channel.send(content=interaction.user.mention, embed=info_embed, view=view)
    except Exception:
        log.exception("Failed to send ticket info embed")

    await interaction.response.send_message(f"✅ Ticket created: {channel.mention}", ephemeral=True)

    await _log_guild(interaction.client, guild, "tickets", "Ticket Opened",
                     f"{interaction.user.mention} opened `{ttype}` → {channel.mention}")


class ReactionRoleView(discord.ui.View):
    """One persistent view per panel with buttons/select loaded from DB."""
    def __init__(self, panel_id: int, roles: list[dict], mode: str = "button"):
        super().__init__(timeout=None)
        self.panel_id = panel_id
        if mode == "select":
            opts = [
                discord.SelectOption(label=r["label"][:100] or f"Role {r['role_id']}",
                                     value=str(r["role_id"]),
                                     emoji=r.get("emoji") or None)
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


async def _ticket_delete_delay(db: "Database", guild_id: int) -> int:
    """Seconds to wait before auto-deleting a closed ticket. Defaults to 120s (2 min)."""
    raw = await db.get_config(guild_id, "ticket.close_delete_delay", "120")
    try:
        val = int(raw)
    except (TypeError, ValueError):
        val = 120
    return max(0, val)


async def _generate_transcript(channel) -> discord.File:
    """Builds a plain-text transcript file of the channel's recent history."""
    import io
    messages = []
    async for m in channel.history(limit=500, oldest_first=True):
        attach = ""
        if m.attachments:
            attach = " " + " ".join(f"[attachment: {a.url}]" for a in m.attachments)
        messages.append(f"[{fmt_dt(m.created_at)}] {m.author}: {m.content}{attach}")
    buf = io.BytesIO(("\n".join(messages) or "(empty)").encode("utf-8"))
    return discord.File(buf, filename=f"transcript-{channel.id}.txt")


async def _post_ticket_close_log(bot: "Freakos", guild: discord.Guild, channel, row, closed_by, reason: str):
    """Posts the 'Ticket Closed Logs' summary embed + transcript to the configured ticket-logs channel."""
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
        color=0x57F287,
        timestamp=now_utc(),
    )
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
        await log_ch.send(embed=e, file=await _generate_transcript(channel))
    except Exception:
        log.exception("Failed to post ticket close log for ticket %s", row["id"])


# =====================================================================
# Inner ticket-room button system (buttons shown inside each ticket channel)
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
    "primary": discord.ButtonStyle.primary, "secondary": discord.ButtonStyle.secondary,
    "success": discord.ButtonStyle.success, "danger": discord.ButtonStyle.danger,
}
NOTIFY_COOLDOWN_SECONDS = 60


async def _ticket_room_cfg(db: "Database", guild_id: int) -> dict:
    """Per-guild customization for the inner ticket-room buttons (enabled/label/emoji/style)."""
    stored = await db.get_json(guild_id, "ticket.room_buttons", default={}) or {}
    cfg = {k: dict(v) for k, v in TICKET_ROOM_BUTTONS.items()}
    for key, overrides in stored.items():
        if key in cfg and isinstance(overrides, dict):
            cfg[key].update(overrides)
    return cfg


async def _member_can_close(db: "Database", guild_id: int) -> bool:
    return (await db.get_config(guild_id, "ticket.member_can_close", "1")) != "0"


async def _is_ticket_staff(db: "Database", guild: discord.Guild, member: discord.Member, ticket_row=None) -> bool:
    """Staff = server admins/mods, OR holders of the configured ticket support role(s)."""
    if is_admin_or_mod(member):
        return True
    role_ids = set()
    global_role = await db.get_config(guild.id, "ticket.support_role_id")
    if global_role:
        role_ids.add(int(global_role))
    if ticket_row is not None:
        t = await db.fetchone("SELECT support_role_id FROM ticket_types WHERE guild_id=? AND name=?",
                              (guild.id, ticket_row["type"]))
        if t and t["support_role_id"]:
            role_ids.add(t["support_role_id"])
    return any(r.id in role_ids for r in member.roles) if role_ids else False


def _resolve_member_ref(guild: discord.Guild, raw: str) -> Optional[int]:
    """Extracts a user ID from a raw mention or plain numeric ID string."""
    raw = (raw or "").strip()
    m = re.match(r"^<@!?(\d+)>$", raw) or re.match(r"^(\d+)$", raw)
    return int(m.group(1)) if m else None


async def _close_ticket_room(interaction: discord.Interaction, ticket_id: int,
                             reason: str = "No reason specified"):
    """Shared close routine used by both the Close and Close-With-Reason buttons."""
    db: Database = interaction.client.db
    row = await db.fetchone("SELECT * FROM tickets WHERE id=?", (ticket_id,))
    if not row:
        return await _safe_reply(interaction, "Ticket not found.")
    if row["status"] == "closed":
        return await _safe_reply(interaction, "This ticket is already closed.")

    await db.execute("UPDATE tickets SET status='closed', close_reason=? WHERE id=?",
                     (reason, ticket_id))

    # Remove the ticket creator's access, keep staff access.
    try:
        creator = interaction.guild.get_member(row["user_id"])
        if creator:
            await interaction.channel.set_permissions(creator, overwrite=None)
        ow = interaction.channel.overwrites_for(interaction.guild.default_role)
        ow.send_messages = False
        await interaction.channel.set_permissions(interaction.guild.default_role, overwrite=ow)
    except Exception:
        log.exception("Failed to update permissions on ticket close")

    closing_text = (
        "× 〻 Ticket Closed 〻 ×\n\n"
        f"𑣲 Closed By:\n{interaction.user.mention}\n\n"
        f"𑣲 Reason:\n{reason}"
    )
    try:
        await _safe_reply(interaction, closing_text, ephemeral=False)
    except Exception:
        log.exception("Failed to send close message")

    # Post log (embed + transcript) to the configured logs channel.
    await _post_ticket_close_log(interaction.client, interaction.guild, interaction.channel,
                                 row, interaction.user, reason)

    delay = await _ticket_delete_delay(db, interaction.guild.id)
    if delay > 0:
        try:
            await interaction.channel.send(
                f"🗑 This channel will be deleted automatically in {fmt_duration(delay)}."
            )
        except Exception:
            pass
        await interaction.client.scheduler.schedule(
            "ticket_delete", now_utc() + timedelta(seconds=delay),
            {"ticket_id": ticket_id}, interaction.guild.id)


class TicketCloseReasonModal(discord.ui.Modal, title="Close Ticket"):
    reason = discord.ui.TextInput(label="Reason", style=discord.TextStyle.paragraph,
                                  max_length=500, required=True,
                                  placeholder="Why is this ticket being closed?")

    def __init__(self, ticket_id: int):
        super().__init__()
        self.ticket_id = ticket_id

    async def on_submit(self, interaction: discord.Interaction):
        await _close_ticket_room(interaction, self.ticket_id, reason=self.reason.value)


class TicketAddUserModal(discord.ui.Modal, title="Add User"):
    user_input = discord.ui.TextInput(label="User ID or Mention",
                                      placeholder="@User or 123456789012345678")

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
                member, view_channel=True, send_messages=True, read_message_history=True
            )
        except Exception:
            return await _safe_reply(interaction, "❌ Failed to update permissions.")
        await _safe_reply(interaction, f"✅ User added to ticket. ({member.mention})")


class TicketRemoveUserModal(discord.ui.Modal, title="Remove User"):
    user_input = discord.ui.TextInput(label="User ID or Mention",
                                      placeholder="@User or 123456789012345678")

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
    """Ephemeral yes/no confirmation shown before a plain Close goes through."""
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
    """The full ticket-management button panel shown inside each ticket channel."""
    def __init__(self, ticket_id: int, cfg: dict, claimed_by: Optional[int] = None):
        super().__init__(timeout=None)
        self.ticket_id = ticket_id
        self.claimed_by = claimed_by

        def add(key: str, label: str, emoji: str, style_key: str, custom_id: str, callback):
            b = cfg.get(key, {})
            if not b.get("enabled", True):
                return
            btn = discord.ui.Button(
                label=b.get("label", label), emoji=b.get("emoji", emoji) or None,
                style=_BUTTON_STYLES.get(b.get("style", style_key), discord.ButtonStyle.secondary),
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
                btn = discord.ui.Button(label="Unclaim", emoji="↩",
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
        db: Database = interaction.client.db
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
        db: Database = interaction.client.db
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
        db: Database = interaction.client.db
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
        db: Database = interaction.client.db
        row = await db.fetchone("SELECT * FROM tickets WHERE id=?", (self.ticket_id,))
        if not row:
            return await _safe_reply(interaction, "Ticket not found.")
        if not await _is_ticket_staff(db, interaction.guild, interaction.user, row):
            return await _safe_reply(interaction, "Staff only.")
        if row["claimed_by"] and row["claimed_by"] != interaction.user.id \
                and not is_admin_or_mod(interaction.user):
            return await _safe_reply(
                interaction, f"Only <@{row['claimed_by']}> (or an admin) can unclaim this.")
        await db.execute("UPDATE tickets SET claimed_by=NULL WHERE id=?", (self.ticket_id,))
        cfg = await _ticket_room_cfg(db, interaction.guild.id)
        new_view = TicketRoomView(self.ticket_id, cfg, None)
        await interaction.response.edit_message(view=new_view)
        interaction.client.add_view(new_view)
        await interaction.followup.send(f"↩ Ticket unclaimed by {interaction.user.mention}.")

    async def _cb_add_user(self, interaction: discord.Interaction):
        db: Database = interaction.client.db
        row = await db.fetchone("SELECT * FROM tickets WHERE id=?", (self.ticket_id,))
        if not row or not await _is_ticket_staff(db, interaction.guild, interaction.user, row):
            return await _safe_reply(interaction, "Staff only.")
        await interaction.response.send_modal(TicketAddUserModal(self.ticket_id))

    async def _cb_remove_user(self, interaction: discord.Interaction):
        db: Database = interaction.client.db
        row = await db.fetchone("SELECT * FROM tickets WHERE id=?", (self.ticket_id,))
        if not row or not await _is_ticket_staff(db, interaction.guild, interaction.user, row):
            return await _safe_reply(interaction, "Staff only.")
        await interaction.response.send_modal(TicketRemoveUserModal(self.ticket_id))

    async def _cb_lock(self, interaction: discord.Interaction):
        db: Database = interaction.client.db
        row = await db.fetchone("SELECT * FROM tickets WHERE id=?", (self.ticket_id,))
        if not row or not await _is_ticket_staff(db, interaction.guild, interaction.user, row):
            return await _safe_reply(interaction, "Staff only.")
        creator = interaction.guild.get_member(row["user_id"])
        try:
            if creator:
                await interaction.channel.set_permissions(
                    creator, view_channel=True, send_messages=False, read_message_history=True)
        except Exception:
            pass
        await interaction.response.send_message(f"🔒 Ticket locked by {interaction.user.mention}.")

    async def _cb_unlock(self, interaction: discord.Interaction):
        db: Database = interaction.client.db
        row = await db.fetchone("SELECT * FROM tickets WHERE id=?", (self.ticket_id,))
        if not row or not await _is_ticket_staff(db, interaction.guild, interaction.user, row):
            return await _safe_reply(interaction, "Staff only.")
        creator = interaction.guild.get_member(row["user_id"])
        try:
            if creator:
                await interaction.channel.set_permissions(
                    creator, view_channel=True, send_messages=True, read_message_history=True)
        except Exception:
            pass
        await interaction.response.send_message(f"🔓 Ticket unlocked by {interaction.user.mention}.")

    async def _cb_transcript(self, interaction: discord.Interaction):
        db: Database = interaction.client.db
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
                    content=f"📄 Transcript for ticket #{row['id']} ({interaction.channel.mention})",
                    file=await _generate_transcript(interaction.channel))
                await interaction.followup.send(
                    f"✅ Transcript sent to {target.mention}.", ephemeral=True)
            except Exception:
                log.exception("Failed to send transcript for ticket %s", row["id"])
                await interaction.followup.send("❌ Failed to send transcript.", ephemeral=True)
        else:
            await interaction.followup.send(
                file=await _generate_transcript(interaction.channel), ephemeral=True)

    async def _cb_notify(self, interaction: discord.Interaction):
        db: Database = interaction.client.db
        row = await db.fetchone("SELECT * FROM tickets WHERE id=?", (self.ticket_id,))
        if not row:
            return await _safe_reply(interaction, "Ticket not found.")
        bot = interaction.client
        now = now_utc().timestamp()
        last = bot._notify_cooldown.get(self.ticket_id, 0)
        if now - last < NOTIFY_COOLDOWN_SECONDS:
            rem = int(NOTIFY_COOLDOWN_SECONDS - (now - last))
            return await _safe_reply(interaction, f"⏳ Please wait {rem}s before notifying again.")
        bot._notify_cooldown[self.ticket_id] = now
        role_id = await db.get_config(interaction.guild.id, "ticket.support_role_id")
        if not role_id:
            t = await db.fetchone(
                "SELECT support_role_id FROM ticket_types WHERE guild_id=? AND name=?",
                (interaction.guild.id, row["type"]))
            role_id = t["support_role_id"] if t else None
        mention = f"<@&{role_id}>" if role_id else "@here"
        await interaction.response.send_message(
            f"🔔 **Staff Notification**\n\n{mention}\n\nA user needs help in:\n{interaction.channel.mention}")


# =====================================================================
# Tasks (invoked by scheduler)
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


async def _run_ticket_delete(bot: "Freakos", payload: dict):
    """Delete a ticket channel a fixed delay after it was closed, unless reopened."""
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
        self._vc_cache: dict[int, int] = {}
        self._spam_tracker: dict[tuple, list[float]] = {}
        self._notify_cooldown: dict[int, float] = {}

    async def setup_hook(self):
        await self.db.connect()
        self.scheduler = Scheduler(self)
        self.scheduler.start()
        register_all_commands(self)
        await self._restore_persistent_views()
        await self._restore_scheduled_tasks()

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

    async def _restore_scheduled_tasks(self):
        rows = await self.db.fetchall(
            "SELECT * FROM scheduled_tasks WHERE completed=0")
        log.info("Restored %d pending scheduled task(s)", len(rows))

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

    async def on_member_join(self, member: discord.Member):
        guild = member.guild
        enabled = await self.db.get_config(guild.id, "welcome.enabled", "0")
        if enabled == "1":
            ch_id = await self.db.get_config(guild.id, "welcome.channel")
            ch = guild.get_channel(int(ch_id)) if ch_id else None
            if ch:
                msg_tpl = await self.db.get_config(guild.id, "welcome.message") or (
                    "× 〻 Welcome Message 〻 ×\n\n"
                    "𑣲 Welcome:\n{mention}\n\n"
                    "𑣲 Member Count:\n{member_count}\n\n"
                    "𑣲 Joined At:\n{joined_at}"
                )
                ph = dict(
                    user=member.name,
                    mention=member.mention,
                    username=member.name,
                    display_name=member.display_name,
                    server=guild.name,
                    member_count=str(guild.member_count),
                    joined_at=fmt_dt_human(member.joined_at or now_utc()),
                )
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
                            timestamp=now_utc(),
                        )
                        if embed_cfg.get("title"):
                            e.title = apply_placeholders(
                                str(embed_cfg["title"]), **ph)[:256]
                        if embed_cfg.get("footer"):
                            e.set_footer(text=apply_placeholders(
                                str(embed_cfg["footer"]), **ph)[:2048])

                        # Thumbnail: member PFP (toggle), else custom thumbnail.
                        if show_avatar and member.display_avatar:
                            e.set_thumbnail(url=member.display_avatar.url)
                        elif embed_cfg.get("thumbnail"):
                            e.set_thumbnail(url=str(embed_cfg["thumbnail"]))

                        # Image: server icon (toggle), else custom image.
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
                display_name=member.display_name, server=guild.name)[:32]
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
                tpl = await self.db.get_config(guild.id, "departure.message") or (
                    "× 〻 Member Left 〻 ×\n\n"
                    "𑣲 User:\n{user}\n\n"
                    "𑣲 Left At:\n{left_at}"
                )
                ph = dict(
                    user=member.name,
                    username=member.name,
                    display_name=member.display_name,
                    mention=member.mention,
                    server=guild.name,
                    member_count=str(guild.member_count),
                    left_at=fmt_dt_human(now_utc()),
                )
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
                            timestamp=now_utc(),
                        )
                        if embed_cfg.get("title"):
                            e.title = apply_placeholders(
                                str(embed_cfg["title"]), **ph)[:256]
                        if embed_cfg.get("footer"):
                            e.set_footer(text=apply_placeholders(
                                str(embed_cfg["footer"]), **ph)[:2048])

                        # Thumbnail: member PFP (toggle), else custom thumbnail.
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

    async def on_voice_state_update(self, member: discord.Member,
                                    before: discord.VoiceState,
                                    after: discord.VoiceState):
        guild = member.guild
        if member.bot:
            return
        old_ch = before.channel
        new_ch = after.channel
        old_id = old_ch.id if old_ch else None
        new_id = new_ch.id if new_ch else None
        if old_id == new_id:
            return
        self._vc_cache[member.id] = new_id or 0

        if await self.db.get_config(guild.id, "vcnotify.enabled", "1") == "0":
            return

        watch = await self.db.get_json(guild.id, "vcnotify.channels", default=[])
        if not watch:
            return
        try:
            watch_ids = {int(c) for c in watch}
        except (TypeError, ValueError):
            watch_ids = set()
        if not watch_ids:
            return

        if old_id and old_id in watch_ids and old_ch is not None:
            cfg = await self.db.get_json(guild.id, f"vcnotify.cfg.{old_id}", default={})
            if cfg.get("enabled", True):
                target = guild.get_channel(cfg.get("target_channel_id") or 0)
                if target:
                    tpl = cfg.get("leave_msg") or "👋 {mention} left **{channel_name}**."
                    rendered = apply_placeholders(
                        tpl, mention=member.mention, user=member.name,
                        username=member.name, display_name=member.display_name,
                        channel_name=old_ch.name, channel_mention=old_ch.mention,
                        server=guild.name)
                    try:
                        for c in chunk_message(rendered):
                            await target.send(c)
                    except Exception:
                        log.exception("VC leave notify failed for channel %s", old_id)

        if new_id and new_id in watch_ids and new_ch is not None:
            cfg = await self.db.get_json(guild.id, f"vcnotify.cfg.{new_id}", default={})
            if cfg.get("enabled", True):
                target = guild.get_channel(cfg.get("target_channel_id") or 0)
                if target:
                    tpl = cfg.get("join_msg") or "🎧 {mention} joined **{channel_name}**."
                    rendered = apply_placeholders(
                        tpl, mention=member.mention, user=member.name,
                        username=member.name, display_name=member.display_name,
                        channel_name=new_ch.name, channel_mention=new_ch.mention,
                        server=guild.name)
                    try:
                        for c in chunk_message(rendered):
                            await target.send(c)
                    except Exception:
                        log.exception("VC join notify failed for channel %s", new_id)

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
                     f"AutoMod: {', '.join(violations)}", iso(now_utc())))
                try:
                    await message.channel.send(
                        f"{message.author.mention} warning: {', '.join(violations)}",
                        delete_after=5)
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
            f"🏓 Pong! `{round(bot.latency*1000)}ms`")

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
                "**Current status:**\n" + "\n".join(lines)),
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
                     description="Show the joining member's avatar as a thumbnail on the welcome embed.")
    @app_commands.describe(enabled="True to show avatar, False to hide it.")
    async def w_avatar(interaction: discord.Interaction, enabled: bool):
        if not await require_admin(interaction): return
        await db.set_config(interaction.guild.id, "welcome.show_avatar", "1" if enabled else "0")
        await interaction.response.send_message(
            f"✅ Member avatar on welcome embed: **{'enabled' if enabled else 'disabled'}**.",
            ephemeral=True)

    @welcome.command(name="server-logo",
                     description="Show the server icon as a banner image on the welcome embed.")
    @app_commands.describe(enabled="True to show the server logo, False to hide it.")
    async def w_server_logo(interaction: discord.Interaction, enabled: bool):
        if not await require_admin(interaction): return
        await db.set_config(interaction.guild.id, "welcome.show_server_logo",
                            "1" if enabled else "0")
        await interaction.response.send_message(
            f"✅ Server logo on welcome embed: **{'enabled' if enabled else 'disabled'}**.",
            ephemeral=True)

    @welcome.command(name="message",
                     description="Set the embed description / plain message (supports newlines).")
    async def w_message(interaction: discord.Interaction):
        if not await require_admin(interaction): return
        current = await db.get_config(interaction.guild.id, "welcome.message", "") or ""
        await interaction.response.send_modal(TextModal(
            title="Welcome Message",
            default=current,
            on_submit=lambda i, v: _save_and_reply(i, "welcome.message", v)))

    @welcome.command(name="embed",
                     description="Configure the welcome embed (title, color, footer, image, thumbnail).")
    @app_commands.describe(
        enabled="Send as an embed? (True/False). Default True.",
        title="Embed title (supports placeholders).",
        color="Hex color, e.g. #5865F2.",
        footer="Embed footer text (supports placeholders).",
        image="Large banner image URL (overridden by server-logo toggle when on).",
        thumbnail="Small thumbnail image URL (overridden by avatar toggle when on).")
    async def w_embed(interaction: discord.Interaction,
                      enabled: Optional[bool] = True,
                      title: Optional[str] = None,
                      color: Optional[str] = None,
                      footer: Optional[str] = None,
                      image: Optional[str] = None,
                      thumbnail: Optional[str] = None):
        if not await require_admin(interaction): return
        cfg = await db.get_json(interaction.guild.id, "welcome.embed", default={}) or {}
        cfg["enabled"] = bool(enabled)
        if title is not None: cfg["title"] = title or ""
        if footer is not None: cfg["footer"] = footer or ""
        if image is not None: cfg["image"] = image or ""
        if thumbnail is not None: cfg["thumbnail"] = thumbnail or ""
        if color is not None:
            parsed = None
            try:
                parsed = int(color.replace("#", "").replace("0x", ""), 16)
            except Exception:
                parsed = None
            cfg["color"] = parsed if parsed is not None else 0x5865F2
        else:
            cfg.setdefault("color", 0x5865F2)
        await db.set_json(interaction.guild.id, "welcome.embed", cfg)
        await interaction.response.send_message(
            "✅ Welcome embed config saved. Preview it with `/welcome test`.",
            ephemeral=True)

    @welcome.command(name="test", description="Send a test welcome message/embed.")
    async def w_test(interaction: discord.Interaction):
        if not await require_admin(interaction): return
        ch_id = await db.get_config(interaction.guild.id, "welcome.channel")
        ch = interaction.guild.get_channel(int(ch_id)) if ch_id else None
        if not ch:
            return await interaction.response.send_message(
                "No welcome channel set.", ephemeral=True)
        tpl = await db.get_config(interaction.guild.id, "welcome.message") or "Welcome {mention}!"
        ph = dict(
            user=interaction.user.name, mention=interaction.user.mention,
            username=interaction.user.name,
            display_name=interaction.user.display_name,
            server=interaction.guild.name,
            member_count=str(interaction.guild.member_count),
            joined_at=fmt_dt_human(interaction.user.joined_at or now_utc()))
        rendered = apply_placeholders(tpl, **ph)
        embed_cfg = await db.get_json(interaction.guild.id, "welcome.embed", default={}) or {}
        use_embed = embed_cfg.get("enabled", True)
        show_avatar = (await db.get_config(interaction.guild.id,
                                           "welcome.show_avatar", "1")) == "1"
        show_logo = (await db.get_config(interaction.guild.id,
                                         "welcome.show_server_logo", "1")) == "1"
        if use_embed:
            e = discord.Embed(
                description=(rendered or "")[:4096],
                color=int(embed_cfg.get("color") or 0x5865F2),
                timestamp=now_utc())
            if embed_cfg.get("title"):
                e.title = apply_placeholders(str(embed_cfg["title"]), **ph)[:256]
            if embed_cfg.get("footer"):
                e.set_footer(text=apply_placeholders(str(embed_cfg["footer"]), **ph)[:2048])
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

    @welcome.command(name="reset", description="Reset welcome config.")
    async def w_reset(interaction: discord.Interaction):
        if not await require_admin(interaction): return
        for k in ("welcome.channel", "welcome.message", "welcome.embed",
                  "welcome.enabled", "welcome.show_avatar", "welcome.show_server_logo"):
            await db.set_config(interaction.guild.id, k, None)
        await interaction.response.send_message("✅ Welcome reset.", ephemeral=True)

    # ================= AUTOROLE =================
    autorole = app_commands.Group(name="autorole", description="Autorole system")
    tree.add_command(autorole)

    @autorole.command(name="setup", description="Quick enable autorole.")
    @app_commands.describe(role="Role to give on join")
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

    # ================= AUTONICK =================
    autonick = app_commands.Group(name="autonick", description="Autonick system")
    tree.add_command(autonick)

    @autonick.command(name="setup", description="Set format and enable autonick.")
    @app_commands.describe(format="Format, e.g. {user} | {server}")
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

    # ================= DEPARTURE =================
    departure = app_commands.Group(name="departure", description="Departure messages")
    tree.add_command(departure)

    @departure.command(name="setup",
                       description="Configure departure channel and enable.")
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

    @departure.command(name="avatar",
                       description="Show the leaving member's avatar as a thumbnail on the departure embed.")
    @app_commands.describe(enabled="True to show avatar, False to hide it.")
    async def d_avatar(interaction: discord.Interaction, enabled: bool):
        if not await require_admin(interaction): return
        await db.set_config(interaction.guild.id, "departure.show_avatar",
                            "1" if enabled else "0")
        await interaction.response.send_message(
            f"✅ Member avatar on departure embed: "
            f"**{'enabled' if enabled else 'disabled'}**.", ephemeral=True)

    @departure.command(name="message",
                       description="Set the embed description / plain message (supports newlines).")
    async def d_message(interaction: discord.Interaction):
        if not await require_admin(interaction): return
        current = await db.get_config(interaction.guild.id, "departure.message", "") or ""
        await interaction.response.send_modal(TextModal(
            title="Departure Message", default=current,
            on_submit=lambda i, v: _save_and_reply(i, "departure.message", v)))

    @departure.command(name="embed",
                       description="Configure the departure embed (title, color, footer, image, thumbnail).")
    @app_commands.describe(
        enabled="Send as an embed? (True/False). Default True.",
        title="Embed title (supports placeholders).",
        color="Hex color, e.g. #ED4245.",
        footer="Embed footer text (supports placeholders).",
        image="Large banner image URL.",
        thumbnail="Small thumbnail image URL (overridden by avatar toggle when on).")
    async def d_embed(interaction: discord.Interaction,
                      enabled: Optional[bool] = True,
                      title: Optional[str] = None,
                      color: Optional[str] = None,
                      footer: Optional[str] = None,
                      image: Optional[str] = None,
                      thumbnail: Optional[str] = None):
        if not await require_admin(interaction): return
        cfg = await db.get_json(interaction.guild.id, "departure.embed", default={}) or {}
        cfg["enabled"] = bool(enabled)
        if title is not None: cfg["title"] = title or ""
        if footer is not None: cfg["footer"] = footer or ""
        if image is not None: cfg["image"] = image or ""
        if thumbnail is not None: cfg["thumbnail"] = thumbnail or ""
        if color is not None:
            parsed = None
            try:
                parsed = int(color.replace("#", "").replace("0x", ""), 16)
            except Exception:
                parsed = None
            cfg["color"] = parsed if parsed is not None else 0xED4245
        else:
            cfg.setdefault("color", 0xED4245)
        await db.set_json(interaction.guild.id, "departure.embed", cfg)
        await interaction.response.send_message(
            "✅ Departure embed config saved. Preview it with `/departure test`.",
            ephemeral=True)

    @departure.command(name="test",
                       description="Send a test departure message/embed.")
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
        embed_cfg = await db.get_json(interaction.guild.id, "departure.embed", default={}) or {}
        use_embed = embed_cfg.get("enabled", True)
        show_avatar = (await db.get_config(interaction.guild.id,
                                           "departure.show_avatar", "1")) == "1"
        if use_embed:
            e = discord.Embed(
                description=(rendered or "")[:4096],
                color=int(embed_cfg.get("color") or 0xED4245),
                timestamp=now_utc())
            if embed_cfg.get("title"):
                e.title = apply_placeholders(str(embed_cfg["title"]), **ph)[:256]
            if embed_cfg.get("footer"):
                e.set_footer(text=apply_placeholders(str(embed_cfg["footer"]), **ph)[:2048])
            if show_avatar and interaction.user.display_avatar:
                e.set_thumbnail(url=interaction.user.display_avatar.url)
            elif embed_cfg.get("thumbnail"):
                e.set_thumbnail(url=str(embed_cfg["thumbnail"]))
            if embed_cfg.get("image"):
                e.set_image(url=str(embed_cfg["image"]))
            await ch.send(embed=e)
        else:
            for c in chunk_message(rendered):
                await ch.send(c)
        await interaction.response.send_message("✅ Test sent.", ephemeral=True)

    @departure.command(name="reset", description="Reset departure.")
    async def d_reset(interaction: discord.Interaction):
        if not await require_admin(interaction): return
        for k in ("departure.channel", "departure.message",
                  "departure.embed", "departure.enabled",
                  "departure.show_avatar"):
            await db.set_config(interaction.guild.id, k, None)
        await interaction.response.send_message("✅ Reset.", ephemeral=True)

    # ================= ACTION DMS =================
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
           
