# 🤖 FREAKOS Discord Bot

> **A Complete, Production-Ready, Public, Multi-Server Discord Bot**  
> Built with **Python 3.11+**, **discord.py 2.x**, **aiosqlite**, and **asyncio**.

---

## 📖 Overview

**FREAKOS** is a modular, high-performance, public Discord bot designed to support thousands of servers simultaneously with complete guild isolation. Every configuration and data record is keyed to `guild_id`, ensuring changes in one server never affect another.

### Key Highlights
- **Universal Multiline Engine**: Global multiline text and blank-line preservation across all modals, embeds, and messages.
- **Persistent UI Components**: Buttons, select menus, and modals that survive bot crashes and hosting restarts.
- **Background Async Scheduler**: Recovers active giveaways, scheduled announcements, reminders, and timeout-end notifications from the persistent SQLite database.
- **Robust Multi-Server Architecture**: True guild isolation with zero hardcoded IDs.
- **Safe Security & Hierarchy Enforcement**: Validates role hierarchy and Discord permissions before executing any moderation or role actions.

---

## 🛠️ Technology Stack

- **Language:** Python 3.11+ (Compatible with Python 3.11, 3.12, 3.13, 3.14+)
- **Discord Framework:** `discord.py >= 2.3.2`
- **Database:** `aiosqlite >= 0.19.0` (Async SQLite, clean architecture ready for PostgreSQL)
- **Environment Management:** `python-dotenv >= 1.0.0`
- **Timezone Support:** `pytz >= 2023.3`

---

## 🚀 Quick Start & Local Setup

### 1. Clone or Extract the Project
```bash
cd FREAKOS
```

### 2. Create and Activate a Virtual Environment
**On Windows (PowerShell):**
```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
```

**On Linux / macOS:**
```bash
python3 -m venv venv
source venv/bin/activate
```

### 3. Install Required Dependencies
```bash
pip install -r requirements.txt
```

### 4. Configure Environment Variables
Copy `.env.example` to `.env`:
```bash
cp .env.example .env
```
Edit `.env` and provide your Discord Bot Token:
```env
DISCORD_TOKEN=your_actual_bot_token_here
DATABASE_PATH=data/freakos.db
DEV_GUILD_ID=
OWNER_IDS=
```

---

## 🔑 Discord Developer Portal Setup

1. Navigate to the [Discord Developer Portal](https://discord.com/developers/applications).
2. Click **New Application** and name it **FREAKOS**.
3. Go to the **Bot** tab on the left sidebar:
   - Click **Reset Token** and copy the token into your `.env` file (`DISCORD_TOKEN`).
   - Under **Privileged Gateway Intents**, enable:
     - ✅ **Server Members Intent** (Required for Welcome, Departure, Autorole, Autonick)
     - ✅ **Message Content Intent** (Required for AutoMod, Custom Commands, Transcripts)
     - ✅ **Presence Intent** (Optional)
4. Go to **OAuth2 ➔ URL Generator**:
   - Under **Scopes**, select: `bot` and `applications.commands`.
   - Under **Bot Permissions**, select: `Administrator` (or select specific permissions: Manage Roles, Manage Channels, Kick Members, Ban Members, Moderate Members, Send Messages, Embed Links, Attach Files, Manage Messages).
   - Copy the generated URL and use it to invite the bot to your Discord server.

---

## 🖥️ Running FREAKOS

### Start the Bot
```bash
python bot.py
```

### Slash Command Syncing
- **Global Sync (Default):** Leave `DEV_GUILD_ID` blank in `.env`. Slash commands will sync globally to all servers (Discord may take up to an hour to propagate global commands).
- **Instant Dev Sync:** Set `DEV_GUILD_ID=YOUR_SERVER_ID` in `.env` for instant slash command registration in your development server.

---

## ☁️ Hosting Deployment (Endercloud / Intercloud / Linux VPS)

### Option 1: Running with Systemd (Recommended for Linux VPS)
Create a systemd service file at `/etc/systemd/system/freakos.service`:
```ini
[Unit]
Description=FREAKOS Discord Bot Service
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/path/to/FREAKOS
ExecStart=/path/to/FREAKOS/venv/bin/python bot.py
Restart=always
RestartSec=10
EnvironmentFile=/path/to/FREAKOS/.env

[Install]
WantedBy=multi-user.target
```
Enable and start the service:
```bash
sudo systemctl daemon-reload
sudo systemctl enable freakos
sudo systemctl start freakos
sudo systemctl status freakos
```

### Option 2: Running with PM2
```bash
pm2 start bot.py --name "freakos" --interpreter ./venv/bin/python
pm2 save
pm2 startup
```

---

## 📂 Project Architecture

```
FREAKOS/
├── bot.py                     # Master bot client, initialization & setup_hook
├── requirements.txt           # Dependency requirements
├── .env.example               # Environment variables template
├── .gitignore                 # Standard git ignores
├── README.md                  # Comprehensive documentation
├── services/
│   ├── __init__.py
│   ├── database.py            # Async SQLite manager with 30+ tables & parameterized queries
│   ├── message_formatter.py   # Centralized multiline formatting & placeholder engine
│   ├── permissions.py         # Role hierarchy and bot permission validation
│   ├── logging_service.py     # Server event audit log dispatcher
│   └── scheduler.py           # Background async loop for giveaway & timeout recovery
├── utils/
│   ├── __init__.py
│   ├── embeds.py              # Consistent branded embeds with multiline support
│   ├── errors.py              # Global application command error handler
│   └── views.py               # Reusable confirmation dialogs & paginators
├── cogs/
│   ├── general.py             # /setup interactive dashboard, /help, /ping, /serverinfo, /userinfo
│   ├── welcome.py             # /welcome suite + on_member_join
│   ├── autorole.py            # /autorole suite + on_member_join
│   ├── autonick.py            # /autonick suite + on_member_join
│   ├── departure.py           # /departure suite + on_member_remove
│   ├── actiondm.py            # /actiondm suite (Timeout, Timeout-End, Kick, Ban)
│   ├── vcnotify.py            # /vcnotify suite + on_voice_state_update
│   ├── tickets.py             # /ticket suite + persistent buttons & transcripts
│   ├── vouches.py             # /vouch suite + persistent vouch buttons
│   ├── shop.py                # /shop suite + persistent catalog panel
│   ├── orders.py              # /order suite (pending/processing/completed/cancelled)
│   ├── notifications.py       # /notify suite (recurring & scheduled notifications)
│   ├── automod.py             # /automod suite + on_message filter
│   ├── moderation.py          # /warn, /timeout, /kick, /ban, /purge, /lock, /lockdown
│   ├── logging.py             # /logging suite + audit event listeners
│   ├── reaction_roles.py      # /reactionrole suite + persistent button views
│   ├── giveaways.py           # /giveaway suite + persistent entry buttons
│   ├── custom_commands.py     # /customcommand suite + dynamic executor
│   └── announcements.py       # /announce suite + scheduling
└── tests/
    ├── __init__.py
    └── test_core.py           # Comprehensive automated test suite
```

---

## 🗃️ Complete Command Directory (19 Modules)

| Module | Available Slash Commands | Description |
| :--- | :--- | :--- |
| **General** | `/setup`, `/help`, `/ping`, `/serverinfo`, `/userinfo`, `/avatar` | Interactive setup dashboard, dynamic command listing, diagnostics. |
| **Welcome** | `/welcome setup`, `enable`, `disable`, `message`, `embed`, `channel`, `test`, `reset` | Multiline join messages, custom embeds, placeholder interpolation. |
| **Autorole** | `/autorole setup`, `add`, `remove`, `list`, `enable`, `disable`, `reset` | Automatic role assignment on member join with hierarchy safety. |
| **Autonick** | `/autonick setup`, `enable`, `disable`, `format`, `reset` | Dynamic nickname formatting upon joining. |
| **Departure** | `/departure setup`, `enable`, `disable`, `channel`, `message`, `test`, `reset` | Multiline leave notices sent to designated departure channels. |
| **Action DMs**| `/actiondm setup`, `enable`, `disable`, `timeout`, `timeout-end`, `kick`, `ban`, `test`, `reset` | Dedicated multiline DM editors for moderation punishments. |
| **VC Alerts** | `/vcnotify setup`, `enable`, `disable`, `add`, `remove`, `list`, `join-message`, `leave-message`, `test`, `reset` | Voice channel join/leave notifications with VC movement isolation. |
| **Tickets**   | `/ticket setup`, `panel`, `type`, `category`, `support-role`, `limit`, `cooldown`, `claim`, `close`, `reopen`, `delete`, `rename`, `add`, `remove`, `transcript`, `reset` | Enterprise ticket system with persistent buttons and transcripts. |
| **Vouches**   | `/vouch setup`, `panel`, `add`, `list`, `user`, `stats`, `reset` | Ratings (1-5 stars), reviews, stats, and persistent submission panels. |
| **Shop**      | `/shop setup`, `add`, `remove`, `edit`, `list`, `buy`, `stock`, `panel`, `reset` | Server product store with multiline descriptions and catalog views. |
| **Orders**    | `/order create`, `list`, `view`, `status`, `cancel`, `complete` | Order lifecycle tracking (pending, processing, completed, cancelled). |
| **Notify**    | `/notify setup`, `add`, `remove`, `list`, `test`, `reset` | Scheduled and recurring multiline server broadcasts. |
| **AutoMod**   | `/automod setup`, `enable`, `disable`, `antispam`, `links`, `mentions`, `words`, `invites`, `actions`, `whitelist`, `reset` | Automated anti-spam, invite blocking, and word filters. |
| **Moderation**| `/warn`, `/warnings`, `/clearwarnings`, `/timeout`, `/untimeout`, `/kick`, `/ban`, `/unban`, `/purge`, `/slowmode`, `/lock`, `/unlock`, `/lockdown`, `/unlockdown` | Complete staff moderation suite with hierarchy validation. |
| **Logging**   | `/logging setup`, `enable`, `disable`, `channel`, `events`, `reset` | Server audit log dispatcher for joins, leaves, edits, deletes, and mod actions. |
| **Reaction Roles**| `/reactionrole setup`, `create`, `add`, `remove`, `list`, `reset` | Interactive button and dropdown role assignment panels. |
| **Giveaways** | `/giveaway create`, `end`, `reroll`, `list` | Restart-safe giveaways with persistent entry buttons and timers. |
| **Custom Cmds**| `/customcommand create`, `edit`, `delete`, `list`, `reset` | Custom server commands with safe multiline placeholder output. |
| **Announcements**| `/announce create`, `edit`, `send`, `schedule`, `cancel` | Rich embed broadcasts with scheduling support. |

---

## 📝 Multiline Formatting & Placeholders

### Multiline Preservation Guarantee
When an administrator inputs:
```text
Hey {mention}!

Welcome to {server}.

Enjoy your stay!
```
FREAKOS guarantees that the output will strictly preserve line 1, the blank line, line 2, the blank line, and line 3 across all embeds, DMs, text messages, and notifications.

### Available Placeholders
- `{user}` / `{mention}`: Mentions the target member (`@User`).
- `{username}`: User's raw username (`username`).
- `{display_name}`: User's server display name.
- `{user_id}`: Discord user ID (`123456789012345678`).
- `{server}` / `{server_name}`: Server name.
- `{server_id}`: Guild ID.
- `{member_count}`: Total members in server.
- `{channel}` / `{channel_name}`: Target channel.
- `{moderator}`: Moderator who performed the action.
- `{reason}`: Stated reason for the action.
- `{duration}`: Duration of timeout or temporary action.
- `{case_id}`: Moderation audit case number.
- `{ticket_id}`: Support ticket ID.
- `{order_id}`: Store order reference ID.
- `{prize}` / `{winner_count}`: Giveaway prize details.

---

## 🧪 Running Automated Tests

FREAKOS includes an automated test suite verifying multiline preservation, database CRUD, multi-guild isolation, and scheduler recovery:

```bash
python -m unittest tests/test_core.py
```

---

## 🔒 Security Best Practices

1. **Never share or commit `.env` or `DISCORD_TOKEN`**: `.env` is pre-configured in `.gitignore`.
2. **Role Hierarchy**: Ensure the **FREAKOS** role in your server is positioned **above** any roles it is expected to assign or moderate.
3. **No Arbitrary Execution**: Custom commands use a deterministic string interpolator without `eval()` or `exec()`.
4. **Database Safety**: All queries use parameterized SQL prepared statements to prevent injection.

---

## 📄 License
This project is licensed under the MIT License.
