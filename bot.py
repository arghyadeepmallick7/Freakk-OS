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
    created_at TEXT NOT NULL
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
class TicketManageView(discord.ui.View):
    """Buttons inside a ticket channel."""
    def __init__(self, ticket_id: int):
        super().__init__(timeout=None)
        self.ticket_id = ticket_id
    @discord.ui.button(label="Claim", style=discord.ButtonStyle.success,
                       custom_id="freakos:ticket:claim")
    async def claim(self, interaction: discord.Interaction, _):
        row = await interaction.client.db.fetchone(
            "SELECT * FROM tickets WHERE id=?", (self.ticket_id,)
        )
        if not row:
            return await interaction.response.send_message("Ticket not found.", ephemeral=True)
        if not is_admin_or_mod(interaction.user):
            return await interaction.response.send_message("Staff only.", ephemeral=True)
        await interaction.client.db.execute(
            "UPDATE tickets SET claimed_by=? WHERE id=?",
            (interaction.user.id, self.ticket_id),
        )
        await interaction.response.send_message(
            f"✅ Claimed by {interaction.user.mention}"
        )
    @discord.ui.button(label="Close", style=discord.ButtonStyle.secondary,
                       custom_id="freakos:ticket:close")
    async def close(self, interaction: discord.Interaction, _):
        row = await interaction.client.db.fetchone(
            "SELECT * FROM tickets WHERE id=?", (self.ticket_id,)
        )
        if not row:
            return await interaction.response.send_message("Ticket not found.", ephemeral=True)
        if interaction.user.id != row["user_id"] and not is_admin_or_mod(interaction.user):
            return await interaction.response.send_message("Not allowed.", ephemeral=True)
        await interaction.client.db.execute(
            "UPDATE tickets SET status='closed' WHERE id=?", (self.ticket_id,)
        )
        try:
            ow = interaction.channel.overwrites_for(interaction.guild.default_role)
            ow.send_messages = False
            await interaction.channel.set_permissions(interaction.guild.default_role, overwrite=ow)
        except Exception:
            pass
        await interaction.response.send_message("🔒 Ticket closed.")
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
    tid = await db.execute(
        "INSERT INTO tickets (guild_id, channel_id, user_id, type, status, created_at) "
        "VALUES (?, ?, ?, ?, 'open', ?)",
        (guild.id, channel.id, interaction.user.id, ttype, iso(now_utc())),
    )
    body = (row["message"] or (
        f"Hey {interaction.user.mention}, thanks for opening a **{ttype}** ticket!\\n\\n"
        "Support will be with you shortly. Please describe your issue."
    )).replace("\\n", "\n")
    embed = make_embed(title=f"Ticket #{tid} — {ttype}", description=body)
    view = TicketManageView(tid)
    try:
        await channel.send(content=interaction.user.mention, embed=embed, view=view)
    except Exception:
        pass
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
                    "timeout_end",
                    until + timedelta(seconds=5),
                    {"timeout_id": tid},
                    guild.id,
                )
                return
        except Exception:
            pass
        msg = await bot.db.get_config(guild.id, "actiondm.timeout_end_msg")
        if msg:
            rendered = apply_placeholders(
                msg,
                user=member.name,
                mention=member.mention,
                username=member.name,
                display_name=member.display_name,
                server=guild.name,
                reason=row["reason"] or "No reason provided.",
                timestamp=fmt_dt(now_utc()),
                case_id=str(tid),
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
            await channel.send(
                f"🎉 **{row['prize']}** ended! Winners: {winners_text}"
            )
        except Exception:
            pass
    if not reroll:
        await bot.db.execute("UPDATE giveaways SET ended=1 WHERE id=?", (gid,))
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
        rows = await self.db.fetchall("SELECT id FROM tickets WHERE status='open'")
        for r in rows:
            self.add_view(TicketManageView(r["id"]))
        gws = await self.db.fetchall("SELECT id FROM giveaways WHERE ended=0")
        for g in gws:
            self.add_view(GiveawayView(g["id"]))
        panels = await self.db.fetchall("SELECT * FROM reaction_panels")
        for p in panels:
            roles = await self.db.fetchall(
                "SELECT role_id, label, emoji FROM reaction_roles WHERE panel_id=?",
                (p["id"],),
            )
            self.add_view(ReactionRoleView(
                p["id"],
                [dict(r) for r in roles],
                p["mode"] or "button",
            ))
    async def _restore_scheduled_tasks(self):
        rows = await self.db.fetchall(
            "SELECT * FROM scheduled_tasks WHERE completed=0"
        )
        log.info("Restored %d pending scheduled task(s)", len(rows))
    async def close(self):
        try:
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
    async def task_timeout_end(self, payload: dict):
        await _run_timeout_end(self, payload)
    async def task_giveaway_end(self, payload: dict):
        await _run_giveaway_end(self, payload)
    async def task_announcement(self, payload: dict):
        await _run_announcement(self, payload)
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
                    msg_tpl,
                    user=member.name,
                    mention=member.mention,
                    username=member.name,
                    display_name=member.display_name,
                    server=guild.name,
                    member_count=str(guild.member_count),
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
        try:
            await self._automod_check(message)
        except Exception:
            log.exception("automod error")
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
                             f"(action: {action})",
                             color=0xE67E22)
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
