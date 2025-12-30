import os
import logging
import asyncio
from datetime import datetime
import json
import urllib.request
import urllib.error
from functools import partial
from typing import Any
import discord
from discord.ext import commands, tasks
from discord import app_commands
from dotenv import load_dotenv
import aiohttp
import json
import sqlite3
import re
from datetime import timezone, timedelta
from dateutil import parser as dtparser
from zoneinfo import ZoneInfo
from audit_logger import audit_log
from permissions import has_permission
from audit_discord import post_audit_to_channel
from logging.handlers import RotatingFileHandler
import traceback
from task_store import TaskStore, Task

logging.basicConfig(level=logging.INFO)

# ---------- ENV + CONFIG + LOGGING ----------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "rust_config.json")

load_dotenv(os.path.join(BASE_DIR, ".env"))

# Single source of truth for the Windows Rust+ HTTP service:
# e.g. "http://192.168.1.184:3000"
RUSTPLUS_API_BASE = os.getenv("RUSTPLUS_API_BASE", "http://localhost:3000").rstrip("/")


def load_rust_config() -> dict:
    """Load rust_config.json if present, otherwise return an empty dict."""
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
            logging.info("Loaded rust_config.json")
            return cfg
    except FileNotFoundError:
        logging.warning("rust_config.json not found; using empty config.")
    except Exception as e:
        logging.exception("Failed to load rust_config.json: %s", e)
    return {}


def user_has_any_role(member: discord.Member, role_ids: list[int]) -> bool:
    """Return True if member has any of the roles in role_ids."""
    if not isinstance(member, discord.Member):
        return False
    wanted = {rid for rid in role_ids if rid}  # ignore zeros
    if not wanted:
        return False
    return any((role.id in wanted) for role in member.roles)


RUST_CFG = load_rust_config()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

# Optional but nice:
DEFAULT_CHANNEL_ID = int(os.getenv("DEFAULT_CHANNEL_ID", "0") or 0)
RUST_GUILD_ID = int(os.getenv("RUST_GUILD_ID", "0") or 0)
RUST_ROLE_ID = int(os.getenv("RUST_ROLE_ID", "0") or 0)

# F1 connect string:
# 1) Prefer rust_config.json["f1_connect"]
# 2) Fallback to F1_CONNECT from .env if present
F1_CONNECT = (RUST_CFG.get("f1_connect") or os.getenv("F1_CONNECT", "")).strip()

# ---------- DISCORD SETUP ----------

intents = discord.Intents.default()
intents.guilds = True  # we need this for slash commands
intents.messages = True
intents.message_content = True  # required for prefix commands
intents.voice_states = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree  # nicer alias


@bot.event
async def setup_hook():
    try:
        guild_id = int(os.getenv("DISCORD_GUILD_ID", "0") or 0) or int(os.getenv("RUST_GUILD_ID", "0") or 0)

        if not guild_id:
            logging.warning("⚠️ No DISCORD_GUILD_ID/RUST_GUILD_ID set. Syncing GLOBAL (may take a while to appear).")
            synced = await bot.tree.sync()
            logging.info("✅ Synced %d GLOBAL commands", len(synced))
            return

        guild = discord.Object(id=guild_id)
        bot.tree.copy_global_to(guild=guild)

        synced = await bot.tree.sync(guild=guild)
        logging.info("✅ Synced %d GUILD commands to %s", len(synced), guild_id)
        logging.info("📌 Commands: %s", ", ".join([c.name for c in synced]))
    except Exception:
        logging.exception("❌ Command sync failed in setup_hook")


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    task_logger.exception(
        "Slash command failed: %s | user=%s guild=%s channel=%s",
        repr(error),
        getattr(interaction.user, "id", None),
        getattr(interaction.guild, "id", None),
        getattr(interaction.channel, "id", None),
    )
    try:
        msg = "❌ Command failed. Logged."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except Exception:
        # swallow; already logged
        pass


# ---------- HELPERS ----------

async def fetch_tc_summary(tc_name: str) -> dict:
    url = f"{RUSTPLUS_API_BASE}/api/tc/{tc_name}"

    def _do_request():
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                data = resp.read()
                return json.loads(data.decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                body = e.read().decode("utf-8")
            except Exception:
                body = str(e)
            return {"ok": False, "error": f"HTTP {e.code}: {body}"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # Run the blocking I/O in a thread
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _do_request)


def get_default_channel(guild: discord.Guild) -> discord.TextChannel:
    """Return the configured channel or fall back to the guild's system channel."""
    if DEFAULT_CHANNEL_ID:
        ch = guild.get_channel(DEFAULT_CHANNEL_ID)
        if isinstance(ch, discord.TextChannel):
            return ch

    # Fallbacks
    if guild.system_channel:
        return guild.system_channel

    # Last resort: first text channel we can send to
    for ch in guild.text_channels:
        if ch.permissions_for(guild.me).send_messages:
            return ch

    raise RuntimeError("No suitable channel found for sending messages.")


def rust_role_mention(guild: discord.Guild) -> str:
    if RUST_ROLE_ID:
        role = guild.get_role(RUST_ROLE_ID)
        if role:
            return role.mention
    return ""


def make_embed(
    title: str,
    description: str,
    color: discord.Color,
    base_name: str | None = None,
    status_emoji: str | None = None,
) -> discord.Embed:
    """Standardizes how our alert embeds look."""
    embed = discord.Embed(
        title=title,
        description=description,
        color=color,
        timestamp=datetime.utcnow(),
    )
    embed.set_author(name="Project Sisyphean")
    embed.set_footer(text="Stay alert, stay alive.")

    if base_name:
        embed.add_field(name="Base", value=base_name, inline=True)

    if status_emoji:
        embed.add_field(name="Status", value=status_emoji, inline=True)

    return embed


def add_f1_to_description(desc: str) -> str:
    """Append F1 console connect instructions if configured."""
    if F1_CONNECT:
        desc += (
            "\n\nCopy & paste in F1 console:\n"
            f"```{F1_CONNECT}```"
        )
    return desc


# ---------- TASK HELPERS ----------

def ts_fmt(unix_ts: int, style: str = "R") -> str:
    # style: R=relative, F=full, f=short date/time, D=date
    return f"<t:{unix_ts}:{style}>"


def is_task_admin(member: discord.Member) -> bool:
    if not TASK_ADMIN_ROLE_IDS:
        # if not configured, allow anyone with Manage Messages as a sensible default
        return member.guild_permissions.manage_messages
    member_role_ids = {r.id for r in member.roles}
    return any(rid in member_role_ids for rid in TASK_ADMIN_ROLE_IDS)


def status_emoji(status: str) -> str:
    return {
        "PENDING": "⏳",
        "IN_PROGRESS": "🛠️",
        "HOLD": "🧊",
        "DONE": "✅",
    }.get(status, "📌")


def build_task_embed(guild: discord.Guild, task: Task) -> discord.Embed:
    role = guild.get_role(task.assigned_role_id)
    assigned = role.mention if role else f"`role:{task.assigned_role_id}`"
    target = f"<@{task.target_user_id}>" if task.target_user_id else "—"
    due = ts_fmt(task.due_at, "R") + " • " + ts_fmt(task.due_at, "f") if task.due_at else "—"
    creator = guild.get_member(task.created_by)
    creator_name = creator.display_name if creator else "Unknown"

    e = discord.Embed(
        title=f"{status_emoji(task.status)} Task #{task.id}: {task.title}",
        description="Project Sisyphean Tasking",
    )
    e.add_field(name="Status", value=f"`{task.status}`", inline=True)
    e.add_field(name="Assigned To", value=assigned, inline=True)
    e.add_field(name="Target", value=target, inline=True)
    e.add_field(name="Due", value=due, inline=False)

    e.set_footer(text=f"Created by {creator_name} • Updated {ts_fmt(task.updated_at, 'R')}")

    if task.status == "DONE" and getattr(task, "completed_by", None):
        completer = guild.get_member(task.completed_by)
        completer_name = completer.display_name if completer else "Unknown"
        when = ts_fmt(task.completed_at, "R") if task.completed_at else "—"
        e.add_field(name="Completed", value=f"{completer_name} • {when}", inline=False)

    return e


async def call_rustplus_api(path: str, method: str = "GET", json_body: dict | None = None) -> dict:
    """Call the Rust+ HTTP service on your PC and return parsed JSON."""
    if not RUSTPLUS_API_BASE:
        raise RuntimeError("RUSTPLUS_API_BASE is not configured in .env")

    url = f"{RUSTPLUS_API_BASE}{path}"

    async with aiohttp.ClientSession() as session:
        if method.upper() == "GET":
            async with session.get(url) as resp:
                return await resp.json()
        else:
            async with session.post(url, json=json_body) as resp:
                return await resp.json()


async def handle_entity_action(interaction: discord.Interaction, entity_name: str, action: str):
    """Helper: turn an entity on/off via Rust+ HTTP."""
    try:
        await interaction.response.defer(ephemeral=True)
    except discord.InteractionResponded:
        # already responded; we'll just use followup
        pass

    if not RUSTPLUS_API_BASE:
        await interaction.followup.send(
            "Rust+ control service is not configured. Ask an admin to set `RUSTPLUS_API_BASE` in `.env`.",
            ephemeral=True,
        )
        return

    try:
        data = await call_rustplus_api(f"/api/entity/{entity_name}/{action}", method="POST")
    except Exception as e:
        await interaction.followup.send(
            f"Error contacting Rust+ service: `{e}`",
            ephemeral=True,
        )
        return

    if not data.get("ok"):
        await interaction.followup.send(
            f"Rust+ service error: `{data.get('error', 'unknown error')}`",
            ephemeral=True,
        )
    else:
        msg = data.get("message", f"{entity_name} {action.upper()} complete.")
        await interaction.followup.send(f"{msg} ✅", ephemeral=True)


async def handle_entity_status(interaction: discord.Interaction, entity_name: str):
    """Helper: get entity status via Rust+ HTTP."""
    try:
        await interaction.response.defer(ephemeral=True)
    except discord.InteractionResponded:
        pass

    if not RUSTPLUS_API_BASE:
        await interaction.followup.send(
            "Rust+ control service is not configured. Ask an admin to set `RUSTPLUS_API_BASE` in `.env`.",
            ephemeral=True,
        )
        return

    try:
        data = await call_rustplus_api(f"/api/entity/{entity_name}/status", method="GET")
    except Exception as e:
        await interaction.followup.send(
            f"Error contacting Rust+ service: `{e}`",
            ephemeral=True,
        )
        return

    if not data.get("ok"):
        await interaction.followup.send(
            f"Rust+ service error: `{data.get('error', 'unknown error')}`",
            ephemeral=True,
        )
        return

    info = data.get("info") or data
    # For now, just show the JSON so we can see what Rust sends.
    pretty = json.dumps(info, indent=2, default=str)
    await interaction.followup.send(
        f"Status for **{entity_name}**:\n```json\n{pretty}\n```",
        ephemeral=True,
    )


async def call_entity_action(name: str, action: str) -> tuple[bool, str]:
    if not RUSTPLUS_API_BASE:
        return False, "RUSTPLUS_API_BASE is not set on the bot."

    method = "GET" if action == "status" else "POST"
    url = f"{RUSTPLUS_API_BASE}/api/entity/{name}/{action}"

    try:
        async with aiohttp.ClientSession() as session:
            http_method = getattr(session, method.lower())
            async with http_method(url) as resp:
                data = await resp.json()
                if resp.status == 200 and data.get("ok"):
                    return True, data.get("message", "OK")
                else:
                    return False, data.get("error", f"HTTP {resp.status}")
    except Exception as e:
        logging.exception("Error calling Rust service: %s", e)
        return False, f"Error talking to Rust service: {e}"


async def send_alert(
    guild: discord.Guild,
    embed: discord.Embed,
    ping_team: bool = True,
):
    """Send an alert embed to the default channel."""
    channel = get_default_channel(guild)
    mention = rust_role_mention(guild) if ping_team else ""
    content = mention if mention else None
    await channel.send(content=content, embed=embed)


class ConnectSelect(discord.ui.Select):
    def __init__(self, profiles: list[dict]):
        # Build options from profiles
        options: list[discord.SelectOption] = []
        for p in profiles:
            label = p.get("label", "Unnamed server")
            key = p.get("key", "")
            category = p.get("category", "")
            emoji = p.get("emoji") or None

            # description shows category + optional notes, truncated by Discord automatically
            notes = p.get("notes") or ""
            desc_parts = []
            if category:
                desc_parts.append(category)
            if notes:
                desc_parts.append(notes)
            description = " • ".join(desc_parts) if desc_parts else None

            option = discord.SelectOption(
                label=label[:100],      # Discord limit
                description=description[:100] if description else None,
                value=key,
                emoji=emoji,
            )
            options.append(option)

        placeholder = "Choose a server to get its F1 connect command..."
        super().__init__(
            placeholder=placeholder,
            min_values=1,
            max_values=1,
            options=options[:25],  # Discord max options = 25
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        key = self.values[0]
        profile = CONNECT_PROFILE_INDEX.get(key)
        if not profile:
            await interaction.followup.send(
                "❌ That server profile could not be found. Ask an admin to refresh the config.",
                ephemeral=True,
            )
            return

        f1 = profile.get("f1", "")
        label = profile.get("label", key)

        if not f1:
            await interaction.followup.send(
                f"❌ No F1 connect string defined for **{label}**.",
                ephemeral=True,
            )
            return

        msg = (
            f"**{label}**\n\n"
            "Copy & paste this into your Rust F1 console:\n"
            f"```{f1}```"
        )
        await interaction.followup.send(msg, ephemeral=True)


class ConnectMenuView(discord.ui.View):
    def __init__(self, profiles: list[dict], timeout: float | None = 300.0):
        super().__init__(timeout=timeout)
        self.message: discord.Message | None = None
        if profiles:
            self.add_item(ConnectSelect(profiles))

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        try:
            if self.message:
                await self.message.edit(view=self)
        except Exception:
            logging.exception("Failed to disable ConnectMenuView on timeout")


async def _send_permission_message(interaction: discord.Interaction, message: str):
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


async def ensure_rust_permission(interaction: discord.Interaction) -> bool:
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await _send_permission_message(
            interaction,
            "This control can only be used inside a server.",
        )
        return False

    if not has_permission(interaction, "rust_control"):
        await _send_permission_message(
            interaction,
            "⛔ You don't have permission to control base systems.",
        )
        return False

    return True


class TaskActionView(discord.ui.View):
    def __init__(self, task_id: int):
        super().__init__(timeout=None)
        self.task_id = task_id

    async def _apply(self, interaction: discord.Interaction, new_status: str):
        if not isinstance(interaction.user, discord.Member) or not is_task_admin(interaction.user):
            await interaction.response.send_message("❌ You don’t have permission to modify tasks.", ephemeral=True)
            return

        task = store.get(self.task_id)
        if not task:
            await interaction.response.send_message("❌ Task not found.", ephemeral=True)
            return

        if new_status == "DONE":
            store.complete_task(self.task_id, interaction.user.id)
        else:
            store.update_status_by(self.task_id, new_status, interaction.user.id)

        task = store.get(self.task_id)
        if interaction.guild and task:
            await update_task_message(interaction.guild, task)

        await interaction.response.send_message(f"✅ Task #{self.task_id} → `{new_status}`", ephemeral=True)

    @discord.ui.button(label="Complete", style=discord.ButtonStyle.success, emoji="✅")
    async def complete_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._apply(interaction, "DONE")

    @discord.ui.button(label="In Progress", style=discord.ButtonStyle.primary, emoji="🛠️")
    async def progress_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._apply(interaction, "IN_PROGRESS")

    @discord.ui.button(label="Hold", style=discord.ButtonStyle.secondary, emoji="🧊")
    async def hold_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._apply(interaction, "HOLD")

    @discord.ui.button(label="Reopen", style=discord.ButtonStyle.danger, emoji="🔄")
    async def reopen_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._apply(interaction, "PENDING")

def is_leadership():
    """App command check: allow leadership role or server admins."""
    async def predicate(interaction: discord.Interaction) -> bool:
        # No guild = no permission
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return False

        # If a leadership role is configured, require it
        if RUST_ROLE_LEADERSHIP_ID:
            role = interaction.guild.get_role(RUST_ROLE_LEADERSHIP_ID)
            if role and role in interaction.user.roles:
                return True

        # Fallback: allow admins
        return interaction.user.guild_permissions.administrator

    return app_commands.check(predicate)


# ---------- TASK COMMANDS ----------

async def update_task_message(guild: discord.Guild, task: Task):
    if not task.message_id:
        return
    channel = guild.get_channel(TASK_CHANNEL_ID)
    if channel is None or not isinstance(channel, (discord.TextChannel, discord.Thread)):
        return
    try:
        msg = await channel.fetch_message(task.message_id)
        await msg.edit(embed=build_task_embed(guild, task), view=TaskActionView(task.id))
    except Exception:
        # If message was deleted or permissions changed, we silently ignore for now.
        pass


async def set_status(interaction: discord.Interaction, task_id: int, new_status: str):
    if not isinstance(interaction.user, discord.Member) or not is_task_admin(interaction.user):
        await interaction.response.send_message("❌ You don’t have permission to modify tasks.", ephemeral=True)
        return

    task = store.get(task_id)
    if not task:
        await interaction.response.send_message("❌ Task not found.", ephemeral=True)
        return

    if new_status == "DONE":
        store.complete_task(task_id, interaction.user.id)
    else:
        store.update_status_by(task_id, new_status, interaction.user.id)
    task = store.get(task_id)  # refresh
    assert task is not None

    if interaction.guild:
        await update_task_message(interaction.guild, task)

    await interaction.response.send_message(f"✅ Task #{task_id} set to `{new_status}`", ephemeral=True)


@tree.command(name="task_create", description="Create a task and assign it to a role.")
@app_commands.describe(
    title="Task title",
    assigned_role="Role responsible for this task",
    target_user="Optional: who this task is about",
    due_in_hours="Optional: due in N hours from now"
)
async def task_create(
    interaction: discord.Interaction,
    title: str,
    assigned_role: discord.Role,
    target_user: discord.User | None = None,
    due_in_hours: int | None = None
):
    if not isinstance(interaction.user, discord.Member) or not is_task_admin(interaction.user):
        await interaction.response.send_message("❌ You don’t have permission to create tasks.", ephemeral=True)
        return

    due_at = None
    if due_in_hours is not None:
        if due_in_hours < 1 or due_in_hours > 24 * 14:
            await interaction.response.send_message("❌ due_in_hours must be between 1 and 336 (14 days).", ephemeral=True)
            return
        now = int(datetime.now(timezone.utc).timestamp())
        due_at = now + due_in_hours * 3600

    task = store.create_task(
        title=title.strip(),
        assigned_role_id=assigned_role.id,
        created_by=interaction.user.id,
        target_user_id=target_user.id if target_user else None,
        due_at=due_at
    )

    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message("❌ This command must be used in a server.", ephemeral=True)
        return

    channel = guild.get_channel(TASK_CHANNEL_ID)
    if channel is None or not isinstance(channel, (discord.TextChannel, discord.Thread)):
        await interaction.response.send_message("❌ TASK_CHANNEL_ID is not a valid text channel.", ephemeral=True)
        return

    embed = build_task_embed(guild, task)
    view = TaskActionView(task.id)
    msg = await channel.send(content=assigned_role.mention, embed=embed, view=view)
    store.set_message_id(task.id, msg.id)
    store.add_log(task.id, "CREATED", interaction.user.id, "Created via /task_create")

    await interaction.response.send_message(f"✅ Created Task #{task.id} in {channel.mention}", ephemeral=True)


@tree.command(name="task_list", description="List recent tasks (optionally filter).")
@app_commands.describe(status="Filter by status", assigned_role="Filter by assigned role", limit="How many to show (max 25)")
async def task_list(
    interaction: discord.Interaction,
    status: str | None = None,
    assigned_role: discord.Role | None = None,
    limit: int = 10
):
    if limit < 1:
        limit = 1
    if limit > 25:
        limit = 25

    status_u = status.upper() if status else None
    if status_u and status_u not in {"PENDING", "IN_PROGRESS", "HOLD", "DONE"}:
        await interaction.response.send_message("❌ Status must be one of: PENDING, IN_PROGRESS, HOLD, DONE", ephemeral=True)
        return

    tasks = store.list_tasks(status=status_u, assigned_role_id=assigned_role.id if assigned_role else None, limit=limit)

    if not tasks:
        await interaction.response.send_message("No tasks found for that filter.", ephemeral=True)
        return

    lines = []
    for t in tasks:
        role_mention = f"<@&{t.assigned_role_id}>"
        due = f" • due {ts_fmt(t.due_at,'R')}" if t.due_at else ""
        lines.append(f"{status_emoji(t.status)} **#{t.id}** `{t.status}` — {t.title} → {role_mention}{due}")

    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@tree.command(name="task_complete", description="Mark a task as DONE.")
async def task_complete(interaction: discord.Interaction, task_id: int):
    await set_status(interaction, task_id, "DONE")


@tree.command(name="task_hold", description="Put a task on HOLD.")
async def task_hold(interaction: discord.Interaction, task_id: int):
    await set_status(interaction, task_id, "HOLD")


@tree.command(name="task_reopen", description="Reopen a task (set to PENDING).")
async def task_reopen(interaction: discord.Interaction, task_id: int):
    await set_status(interaction, task_id, "PENDING")


@tree.command(name="task_progress", description="Set a task to IN_PROGRESS.")
async def task_progress(interaction: discord.Interaction, task_id: int):
    await set_status(interaction, task_id, "IN_PROGRESS")


@tree.command(name="task_assign", description="Reassign a task to a different role.")
async def task_assign(interaction: discord.Interaction, task_id: int, assigned_role: discord.Role):
    if not isinstance(interaction.user, discord.Member) or not is_task_admin(interaction.user):
        await interaction.response.send_message("❌ You don’t have permission to modify tasks.", ephemeral=True)
        return

    task = store.get(task_id)
    if not task:
        await interaction.response.send_message("❌ Task not found.", ephemeral=True)
        return

    store.assign_role_by(task_id, assigned_role.id, interaction.user.id)
    task = store.get(task_id)
    assert task is not None

    if interaction.guild:
        await update_task_message(interaction.guild, task)

    await interaction.response.send_message(f"✅ Task #{task_id} reassigned to {assigned_role.mention}", ephemeral=True)


# Load .env
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

# -------------------------
# DISCORD – CORE AUTH
# -------------------------
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
DISCORD_GUILD_ID = int(os.getenv("DISCORD_GUILD_ID", "0") or 0)

# -------------------------
# DISCORD – CHANNELS
# -------------------------
DISCORD_GENERAL_CHAT = int(os.getenv("DISCORD_GENERAL_CHAT", "0") or 0)
DISCORD_TEST_CHANNEL = int(os.getenv("DISCORD_TEST_CHANNEL", "0") or 0)
DISCORD_RAID_ALERTS_CHANNEL = int(os.getenv("DISCORD_RAID_ALERTS_CHANNEL", "0") or 0)
DISCORD_TC_STATUS_CHANNEL = int(os.getenv("DISCORD_TC_STATUS_CHANNEL", "0") or 0)
DISCORD_TRAINING_ANNOUNCE_CHANNEL = int(os.getenv("DISCORD_TRAINING_ANNOUNCE_CHANNEL", "0") or 0)
DISCORD_RECRUITING_CHANNEL = int(os.getenv("DISCORD_RECRUITING_CHANNEL", "0") or 0)
DISCORD_COMMAND_LOG_CHANNEL = int(os.getenv("DISCORD_COMMAND_LOG_CHANNEL", "0") or 0)
DISCORD_ERROR_LOG_CHANNEL = int(os.getenv("DISCORD_ERROR_LOG_CHANNEL", "0") or 0)
DUTY_STATUS_LOG_CHANNEL = int(os.getenv("DUTY_STATUS_LOG_CHANNEL", "0") or 0)

# -------------------------
# DISCORD – ROLES
# -------------------------
RUST_ROLE_RUSTTEAM_ID = int(os.getenv("RUST_ROLE_RUSTTEAM_ID", "0") or 0)
RUST_ROLE_PVP_ID = int(os.getenv("RUST_ROLE_PVP_ID", "0") or 0)
RUST_ROLE_BUILDER_ID = int(os.getenv("RUST_ROLE_BUILDER_ID", "0") or 0)
RUST_ROLE_FARMER_ID = int(os.getenv("RUST_ROLE_FARMER_ID", "0") or 0)
RUST_ROLE_RECRUITER_ID = int(os.getenv("RUST_ROLE_RECRUITER_ID", "0") or 0)
RUST_ROLE_EVENT_COORD_ID = int(os.getenv("RUST_ROLE_EVENT_COORD_ID", "0") or 0)
RUST_ROLE_LEADERSHIP_ID = int(os.getenv("RUST_ROLE_LEADERSHIP_ID", "0") or 0)

# Duty status roles
RUST_ROLE_ACTIVE_DUTY_ID = int(os.getenv("RUST_ROLE_ACTIVE_DUTY_ID", "0") or 0)
RUST_ROLE_RESERVES_ID = int(os.getenv("RUST_ROLE_RESERVES_ID", "0") or 0)
RUST_ROLE_INACTIVE_RESERVES_ID = int(os.getenv("RUST_ROLE_INACTIVE_RESERVES_ID", "0") or 0)
RUST_ROLE_VISITOR_ID = int(os.getenv("RUST_ROLE_VISITOR_ID", "0") or 0)

# -------------------------
# RUST+ API
# -------------------------
RUSTPLUS_API_BASE = os.getenv("RUSTPLUS_API_BASE", "").rstrip("/")

# -------------------------
# MISC
# -------------------------
ENVIRONMENT = os.getenv("ENVIRONMENT", "production")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
BOT_PREFIX = os.getenv("BOT_PREFIX", "!")
TASK_CHANNEL_ID = int(os.getenv("TASK_CHANNEL_ID", "0") or 0)
TASK_ADMIN_ROLE_IDS = [int(x.strip()) for x in os.getenv("TASK_ADMIN_ROLE_IDS", "").split(",") if x.strip().isdigit()]


# -------------------------
# CONNECT MENU CONFIG
# -------------------------

CONNECT_CONFIG_PATH = os.getenv("CONNECT_CONFIG_PATH") or os.path.join(BASE_DIR, "connect_servers.json")
ROLES_CONFIG_PATH = os.getenv("ROLES_CONFIG_PATH") or os.path.join(BASE_DIR, "roles_config.json")
DUTY_STATUS_STATE_PATH = os.getenv("DUTY_STATUS_STATE_PATH") or os.path.join(BASE_DIR, "duty_status.json")
DUTY_AUTOMATION_PATH = os.path.join(BASE_DIR, "duty_automation.json")
TASK_DB_PATH = os.getenv("TASK_DB_PATH")  # TaskStore will fallback to sisyphus.db if unset

# ---------- TASK STORE / LOGGING ----------

def setup_tasks_logger() -> logging.Logger:
    os.makedirs("logs", exist_ok=True)

    logger = logging.getLogger("tasks")
    logger.setLevel(logging.INFO)
    logger.propagate = False  # prevents duplicate logs via root logger

    # avoid duplicate handlers if code reloads
    if any(
        isinstance(h, RotatingFileHandler) and getattr(h, "baseFilename", "").endswith("tasks.log")
        for h in logger.handlers
    ):
        return logger

    fh = RotatingFileHandler("logs/tasks.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(fh)

    # mirror to stdout so journald captures task logs/errors
    sh = logging.StreamHandler()
    sh.setFormatter(logging.Formatter("TASKS %(levelname)s %(message)s"))
    logger.addHandler(sh)

    return logger

task_logger = setup_tasks_logger()
task_logger.info("Tasks logger online. PID=%s", os.getpid())
store = TaskStore(db_path=TASK_DB_PATH)


# ---------- ROLE CONFIG ----------

def norm_key(s: str) -> str:
    """Normalize a role key for consistent lookups."""
    return str(s or "").strip().lower().replace(" ", "_").replace("-", "_")


def _read_roles_config_raw() -> dict[str, Any]:
    """Read roles_config.json, flatten nested categories, return {name: id/raw}."""

    passthrough_keys = {"_ROLE_RENAMES", "_B_BILLETS"}

    def store(cleaned: dict[str, Any], path: tuple[str, ...], raw_value: object) -> None:
        label = ".".join(path)
        try:
            role_id = int(raw_value)
        except (TypeError, ValueError):
            logging.warning("Invalid role id for %s in roles_config.json: %r", label, raw_value)
            return

        # Allow lookups by the leaf key as well as dotted/underscored paths.
        keys = {
            norm_key(path[-1]),
            norm_key(label),
            norm_key("_".join(path)),
        }
        for key in keys:
            cleaned[key] = role_id

    def flatten(
        obj: object,
        prefix: tuple[str, ...] | None = None,
        cleaned: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        prefix = prefix or tuple()
        cleaned = cleaned or {}
        if not isinstance(obj, dict):
            logging.warning(
                "roles_config.json must be a JSON object; ignoring invalid entry at %s",
                ".".join(prefix) or "root",
            )
            return cleaned
        for raw_key, value in obj.items():
            key = str(raw_key).strip()
            if not key:
                continue
            passthrough_key = key.upper()
            if passthrough_key in passthrough_keys:
                cleaned[passthrough_key] = value
                cleaned[key] = value
                continue
            path = prefix + (key,)
            if isinstance(value, dict):
                flatten(value, path, cleaned)
            else:
                store(cleaned, path, value)
        return cleaned

    try:
        with open(ROLES_CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                return flatten(data)
            logging.warning("roles_config.json root is not an object; ignoring.")
            return {}
    except FileNotFoundError:
        logging.warning("roles_config.json not found; role config will use .env fallbacks.")
        return {}
    except Exception as e:
        logging.exception("Failed to read roles_config.json: %s", e)
        return {}


ROLE_CONFIG: dict[str, Any] = _read_roles_config_raw()
logging.info("Loaded %d roles from roles_config.json", len(ROLE_CONFIG))

_RAW_ROLE_RENAMES = ROLE_CONFIG.get("_ROLE_RENAMES")
if isinstance(_RAW_ROLE_RENAMES, dict):
    ROLE_RENAMES: dict[str, list[str]] = {
        norm_key(k): [norm_key(x) for x in v if isinstance(x, str)]
        for k, v in _RAW_ROLE_RENAMES.items()
        if isinstance(v, list)
    }
else:
    ROLE_RENAMES = {}

_RAW_B_BILLETS = ROLE_CONFIG.get("_B_BILLETS")
if isinstance(_RAW_B_BILLETS, list):
    B_BILLET_KEYS: list[str] = [norm_key(x) for x in _RAW_B_BILLETS if isinstance(x, str)]
else:
    B_BILLET_KEYS = []


def get_role_id(name: str) -> int:
    """
    Lookup a role id by logical name.
    """
    key = norm_key(name)

    rid = ROLE_CONFIG.get(key)
    if isinstance(rid, int) and rid > 0:
        return rid

    for alias in ROLE_RENAMES.get(key, []):
        rid2 = ROLE_CONFIG.get(alias)
        if isinstance(rid2, int) and rid2 > 0:
            return rid2

    return 0


# ---------- DUTY STATUS HELPERS ----------

DUTY_STATUS_KEYS = ["active_duty", "reservist", "inactive_reservist"]


def get_duty_status_role_ids() -> dict[str, int]:
    """Return mapping of duty status key -> role id (0 if missing)."""
    return {key: get_role_id(key) for key in DUTY_STATUS_KEYS}


def get_b_billet_role_ids() -> list[int]:
    """Return configured B Billet role ids."""
    ids: list[int] = []
    for key in B_BILLET_KEYS:
        rid = get_role_id(key)
        if rid:
            ids.append(rid)
    return ids


async def apply_duty_status(
    guild: discord.Guild,
    member: discord.Member,
    status_key: str,
    *,
    actor: discord.abc.User | None = None,
    source: str = "manual",
) -> str:
    """
    Core logic to change a member's duty status.
    - status_key must be one of: 'active_duty', 'reservist', 'inactive_reservist'
    - Removes all three duty status roles
    - Adds the selected one
    - Persists the status in DUTY_STATUS_STATE
    - Logs the change to DUTY_STATUS_LOG_CHANNEL (if configured)
    - Returns a pretty label for display
    """
    status_key = status_key.strip().lower()
    if status_key not in DUTY_STATUS_KEYS:
        raise ValueError("Invalid duty status key")

    status_roles = get_duty_status_role_ids()
    target_role_id = status_roles.get(status_key, 0)

    if not guild:
        raise ValueError("Guild is required to change duty status.")

    if not target_role_id:
        raise ValueError(f"No role configured for status '{status_key}'. Check roles_config.json.")

    roleid_to_key = {rid: key for key, rid in status_roles.items() if rid}

    old_keys = {
        roleid_to_key[role.id]
        for role in member.roles
        if role.id in roleid_to_key
    }
    old_label = (
        ", ".join(sorted(pretty_status_label(k) for k in old_keys))
        if old_keys
        else "None"
    )

    for key, rid in status_roles.items():
        if not rid:
            continue
        role = guild.get_role(rid)
        if role and role in member.roles:
            await member.remove_roles(role, reason="Changing duty status")

    new_role = guild.get_role(target_role_id)
    if not new_role:
        raise ValueError(f"Role for status '{status_key}' not found in guild.")

    await member.add_roles(new_role, reason="Duty status update")

    global DUTY_STATUS_STATE
    DUTY_STATUS_STATE[str(member.id)] = status_key
    _save_duty_status_state(DUTY_STATUS_STATE)

    new_label = pretty_status_label(status_key)

    if DUTY_STATUS_LOG_CHANNEL:
        log_channel = guild.get_channel(DUTY_STATUS_LOG_CHANNEL)
        if log_channel:
            desc_lines = [
                f"**Member:** {member.mention}",
                f"**Old:** {old_label}",
                f"**New:** {new_label}",
                f"**Source:** {source}",
            ]
            if actor:
                desc_lines.append(f"**By:** {actor.mention}")

            embed = discord.Embed(
                title="Duty Status Change",
                description="\n".join(desc_lines),
                color=discord.Color.blue(),
                timestamp=datetime.utcnow(),
            )
            try:
                await log_channel.send(embed=embed)
            except Exception as e:
                logging.exception("Failed to send duty status change log: %s", e)

    return new_label


def pretty_status_label(status_key: str | None) -> str:
    if not status_key:
        return "None"
    key = status_key.strip().lower()
    if key == "active_duty":
        return "Active Duty"
    if key == "reservist":
        return "Reservist"
    if key == "inactive_reservist":
        return "Inactive Reservist"
    return key.replace("_", " ").title()


# ---------- DUTY STATUS PERSISTENCE ----------

def _load_duty_status_state() -> dict[str, str]:
    """
    Load last-known duty status per user from duty_status.json.
    Keys are str(user_id), values are status keys like 'active_duty'.
    """
    try:
        with open(DUTY_STATUS_STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                return {str(k): str(v) for k, v in data.items()}
            logging.warning("duty_status.json is not an object; ignoring.")
            return {}
    except FileNotFoundError:
        logging.info("duty_status.json not found; starting with empty duty state.")
        return {}
    except Exception as e:
        logging.exception("Failed to read duty_status.json: %s", e)
        return {}


def _save_duty_status_state(state: dict[str, str]) -> bool:
    """Write duty status state to disk. Returns True on success."""
    try:
        with open(DUTY_STATUS_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
        return True
    except Exception as e:
        logging.exception("Failed to write duty_status.json: %s", e)
        return False


DUTY_STATUS_STATE: dict[str, str] = _load_duty_status_state()
logging.info("Loaded %d duty status entries from duty_status.json", len(DUTY_STATUS_STATE))


def _read_connect_config_raw() -> list[dict]:
    """Read the raw JSON list from CONNECT_CONFIG_PATH. Returns [] on error."""
    try:
        with open(CONNECT_CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, list):
                return data
            logging.warning("connect_servers.json is not a list; using empty list.")
            return []
    except FileNotFoundError:
        logging.warning("connect_servers.json not found; starting with empty list.")
        return []
    except Exception as e:
        logging.exception("Failed to read connect_servers.json: %s", e)
        return []


def _write_connect_config_raw(entries: list[dict]) -> bool:
    """Write the given list back to CONNECT_CONFIG_PATH. Returns True on success."""
    try:
        with open(CONNECT_CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(entries, f, indent=2, ensure_ascii=False)
        return True
    except Exception as e:
        logging.exception("Failed to write connect_servers.json: %s", e)
        return False



def load_connect_profiles():
    """
    Load server connect profiles from JSON.
    Each profile needs: key, label, f1, optional category/emoji/notes
    """
    raw = _read_connect_config_raw()
    profiles: list[dict] = []
    index: dict[str, dict] = {}

    for entry in raw:
        key = entry.get("key")
        label = entry.get("label")
        f1 = entry.get("f1")

        if not key or not label or not f1:
            logging.warning("Skipping invalid connect profile (missing key/label/f1): %r", entry)
            continue

        profiles.append(entry)
        index[key] = entry

    logging.info("Loaded %d connect profiles from %s", len(profiles), CONNECT_CONFIG_PATH)
    return profiles, index


CONNECT_PROFILES, CONNECT_PROFILE_INDEX = load_connect_profiles()


# ---------- STAFF CONFIG HELPERS ----------

STAFF_CONFIG_PATH = os.path.join(BASE_DIR, "staff_config.json")


def load_staff_config() -> dict:
    try:
        with open(STAFF_CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        logging.exception("Failed to load staff_config.json: %s", e)
        return {}


def staff_roles_cfg() -> dict:
    return load_staff_config().get("roles", {})


def flatten_unit_roles(roles_cfg: dict) -> list[int]:
    units = roles_cfg.get("units", {})
    role_ids: list[int] = []
    for category, mapping in units.items():
        for name, rid in (mapping or {}).items():
            if isinstance(rid, int) and rid:
                role_ids.append(rid)
    return role_ids


def leadership_role_ids(roles_cfg: dict) -> list[int]:
    lead = roles_cfg.get("leadership", {})
    return [rid for rid in lead.values() if isinstance(rid, int) and rid]


def status_role_id(roles_cfg: dict, key: str) -> int:
    return int(roles_cfg.get("status", {}).get(key, 0) or 0)


def find_role(guild: discord.Guild, role_id: int) -> discord.Role | None:
    if not role_id:
        return None
    return guild.get_role(role_id)


# ---------- DUTY AUTOMATION CONFIG ----------

def load_duty_automation_cfg() -> dict:
    try:
        with open(DUTY_AUTOMATION_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        logging.warning("duty_automation.json not found; duty automation disabled.")
        return {"enabled": False}
    except Exception as e:
        logging.exception("Failed to read duty_automation.json: %s", e)
        return {"enabled": False}


def save_duty_automation_cfg(cfg: dict) -> bool:
    try:
        with open(DUTY_AUTOMATION_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
        return True
    except Exception as e:
        logging.exception("Failed to write duty_automation.json: %s", e)
        return False


DUTY_AUTOMATION_CFG = load_duty_automation_cfg()


# ---------- TIME PING (GLOBAL TEAM COORDINATION) ----------

TIME_DB_PATH = os.path.join(BASE_DIR, "sisyphus.db")
DEFAULT_TZ = "America/New_York"

TZ_ALIASES = {
    "est": "America/New_York",
    "edt": "America/New_York",
    "cst": "America/Chicago",
    "cdt": "America/Chicago",
    "mst": "America/Denver",
    "mdt": "America/Denver",
    "pst": "America/Los_Angeles",
    "pdt": "America/Los_Angeles",
    "pt": "America/Los_Angeles",
    "et": "America/New_York",
    "ct": "America/Chicago",
    "mt": "America/Denver",
    "uk": "Europe/London",
    "gmt": "Etc/GMT",
    "utc": "UTC",
}

TIME_RANGE_RE = re.compile(r"^\\s*(?P<start>.+?)\\s*-\\s*(?P<end>.+?)(?:\\s+(?P<tz>[A-Za-z/_]+))?\\s*$")
TIME_INLINE_RE = re.compile(r"(?:^|\\s)!(?:t|time)\\s+(.+)$", re.IGNORECASE)


def _time_db() -> sqlite3.Connection:
    conn = sqlite3.connect(TIME_DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_timezones (
            user_id INTEGER PRIMARY KEY,
            tz TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS voice_sessions (
            user_id INTEGER NOT NULL,
            guild_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            started_at INTEGER NOT NULL,
            PRIMARY KEY (user_id, guild_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS voice_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            guild_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            started_at INTEGER NOT NULL,
            ended_at INTEGER NOT NULL,
            seconds INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_voice_history_lookup ON voice_history (guild_id, user_id, started_at)"
    )
    return conn


def normalize_tz(tz: str) -> str:
    t = tz.strip().lower()
    return TZ_ALIASES.get(t, tz.strip())


def set_user_timezone(user_id: int, tz: str) -> None:
    with _time_db() as conn:
        conn.execute(
            "INSERT INTO user_timezones (user_id, tz) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET tz=excluded.tz",
            (user_id, tz),
        )


def get_user_timezone(user_id: int) -> str:
    with _time_db() as conn:
        row = conn.execute("SELECT tz FROM user_timezones WHERE user_id=?", (user_id,)).fetchone()
    return row[0] if row else DEFAULT_TZ


def parse_duration(text: str) -> timedelta | None:
    s = text.strip().lower()
    s = s.removeprefix("in ").strip()
    m = re.fullmatch(r"(?:(\\d+)\\s*h)?\\s*(?:(\\d+)\\s*m)?", s)
    if not m:
        return None
    h = int(m.group(1) or 0)
    mins = int(m.group(2) or 0)
    if h == 0 and mins == 0:
        return None
    return timedelta(hours=h, minutes=mins)


def stamp(dt_utc: datetime, style: str = "t") -> str:
    if dt_utc.tzinfo is None:
        dt_utc = dt_utc.replace(tzinfo=timezone.utc)
    dt_utc = dt_utc.astimezone(timezone.utc)

    ts = int(dt_utc.timestamp())

    # Clamp clearly-bad timestamps (before 2000) to now to avoid ancient dates.
    if ts < 946684800:
        ts = int(datetime.now(timezone.utc).timestamp())

    return f"<t:{ts}:{style}>"


def parse_when_to_utc(when: str, user_tz: str) -> tuple[datetime, datetime | None]:
    raw = when.strip()

    dur = parse_duration(raw)
    if dur:
        start = datetime.now(timezone.utc) + dur
        return start, None

    m = TIME_RANGE_RE.match(raw)
    if m:
        start_txt = m.group("start").strip()
        end_txt = m.group("end").strip()
        tz_txt = m.group("tz")
        src_tz = normalize_tz(tz_txt) if tz_txt else user_tz

        z = ZoneInfo(src_tz)
        now_local = datetime.now(z).replace(second=0, microsecond=0)

        start_local = dtparser.parse(start_txt, default=now_local)
        end_local = dtparser.parse(end_txt, default=now_local)

        if end_local <= start_local:
            end_local += timedelta(days=1)

        return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)

    # single time with optional tz suffix ("8pm uk")
    parts = raw.split()
    src_tz = user_tz
    if len(parts) >= 2 and ("/" in parts[-1] or parts[-1].lower() in TZ_ALIASES):
        src_tz = normalize_tz(parts[-1])
        raw = " ".join(parts[:-1]).strip()

    z = ZoneInfo(src_tz)
    now_local = datetime.now(z).replace(second=0, microsecond=0)
    dt_local = dtparser.parse(raw, default=now_local)

    # If time-only and already passed today, assume tomorrow
    if dt_local <= now_local and re.search(r"\\d", raw) and not re.search(r"\\b(yesterday|today|tomorrow|next)\\b", raw.lower()):
        dt_local += timedelta(days=1)

    return dt_local.astimezone(timezone.utc), None


# ---------- DUTY AUTOMATION / VOICE TRACKING ----------


def is_excluded_voice_channel(channel: discord.abc.GuildChannel | None) -> bool:
    if not channel:
        return True  # treat None as excluded for safety

    cfg = DUTY_AUTOMATION_CFG or {}
    name_excludes = {str(x).strip().lower() for x in cfg.get("exclude_voice_channel_names", []) if x}
    id_excludes = {int(x) for x in cfg.get("exclude_voice_channel_ids", []) if str(x).isdigit()}

    if channel.id in id_excludes:
        return True

    if channel.name and channel.name.strip().lower() in name_excludes:
        return True

    return False


def _loa_set(guild_id: int, user_id: int, start_ts: int, end_ts: int, reason: str | None, created_by: int | None) -> None:
    with _time_db() as conn:
        conn.execute(
            """
            INSERT INTO loa (guild_id, user_id, start_ts, end_ts, reason, created_by, created_ts)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, user_id)
            DO UPDATE SET start_ts=excluded.start_ts, end_ts=excluded.end_ts, reason=excluded.reason,
                          created_by=excluded.created_by, created_ts=excluded.created_ts
            """,
            (guild_id, user_id, start_ts, end_ts, reason, created_by, int(datetime.now(timezone.utc).timestamp())),
        )


def _loa_clear(guild_id: int, user_id: int) -> bool:
    with _time_db() as conn:
        cur = conn.execute("DELETE FROM loa WHERE guild_id=? AND user_id=?", (guild_id, user_id))
        return cur.rowcount > 0


def _loa_get_active(guild_id: int, user_id: int, now_ts: int) -> tuple[int, int, str | None] | None:
    with _time_db() as conn:
        row = conn.execute(
            "SELECT start_ts, end_ts, reason FROM loa WHERE guild_id=? AND user_id=? AND end_ts>?",
            (guild_id, user_id, now_ts),
        ).fetchone()
    if not row:
        return None
    return int(row[0]), int(row[1]), (row[2] if row[2] else None)


def _is_on_loa(guild_id: int, user_id: int, now_ts: int) -> bool:
    return _loa_get_active(guild_id, user_id, now_ts) is not None


def _start_session(guild_id: int, user_id: int, channel_id: int, start_ts: int) -> None:
    with _time_db() as conn:
        conn.execute(
            """
            INSERT INTO voice_sessions (user_id, guild_id, channel_id, started_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id, guild_id) DO UPDATE SET channel_id=excluded.channel_id, started_at=excluded.started_at
            """,
            (user_id, guild_id, channel_id, start_ts),
        )


def _end_session_and_add(guild_id: int, user_id: int, end_ts: int) -> None:
    with _time_db() as conn:
        row = conn.execute(
            "SELECT channel_id, started_at FROM voice_sessions WHERE user_id=? AND guild_id=?",
            (user_id, guild_id),
        ).fetchone()
        if not row:
            return

        channel_id, started_at = row
        seconds = max(0, end_ts - int(started_at))

        cfg = DUTY_AUTOMATION_CFG or {}
        min_sec = int(cfg.get("min_session_seconds", 0) or 0)

        conn.execute("DELETE FROM voice_sessions WHERE user_id=? AND guild_id=?", (user_id, guild_id))

        if seconds < min_sec:
            return

        conn.execute(
            """
            INSERT INTO voice_history (user_id, guild_id, channel_id, started_at, ended_at, seconds)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (user_id, guild_id, channel_id, started_at, end_ts, seconds),
        )


def _period_start_ts(period_start: str) -> int:
    try:
        dt = datetime.fromisoformat(period_start)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return int(dt.timestamp())
    except Exception:
        return 0


def _get_week_seconds(guild_id: int, user_id: int, period_start: str) -> int:
    start_ts = _period_start_ts(period_start)
    with _time_db() as conn:
        row = conn.execute(
            "SELECT SUM(seconds) FROM voice_history WHERE guild_id=? AND user_id=? AND started_at>=?",
            (guild_id, user_id, start_ts),
        ).fetchone()
    return int(row[0] or 0)


def period_start_utc(dt: datetime, period: str) -> str:
    d = dt.astimezone(timezone.utc).date()
    period = (period or "weekly").lower()

    if period == "weekly":
        start = d - timedelta(days=d.weekday())  # Monday
        return start.isoformat()

    if period == "biweekly":
        epoch = datetime(2020, 1, 6, tzinfo=timezone.utc).date()  # Monday
        delta_days = (d - epoch).days
        block = (delta_days // 14) * 14
        return (epoch + timedelta(days=block)).isoformat()

    if period == "monthly":
        return d.replace(day=1).isoformat()

    start = d - timedelta(days=d.weekday())
    return start.isoformat()


def classify_duty_from_hours(hours: float, thresholds: dict) -> str:
    items = []
    for k, v in (thresholds or {}).items():
        try:
            items.append((k, float(v)))
        except Exception:
            continue
    items.sort(key=lambda t: t[1], reverse=True)

    for key, th in items:
        if hours >= th:
            return key
    return "inactive_reservist"


@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    logging.info(
        "VOICE evt | %s | before=%s | after=%s",
        member.display_name,
        getattr(before.channel, "name", None),
        getattr(after.channel, "name", None),
    )
    if member.bot or not member.guild:
        return

    if after.channel and after.channel.name != "Lost In The Woods":
        ch = member.guild.system_channel
        if ch:
            await ch.send(f"🎙 {member.display_name} joined {after.channel.name}", delete_after=10)

    guild_id = member.guild.id
    user_id = member.id
    now_ts = int(datetime.now(timezone.utc).timestamp())

    if _is_on_loa(guild_id, user_id, now_ts):
        _end_session_and_add(guild_id, user_id, now_ts)
        return

    before_ch = before.channel
    after_ch = after.channel

    before_ok = (before_ch is not None) and (not is_excluded_voice_channel(before_ch))
    after_ok = (after_ch is not None) and (not is_excluded_voice_channel(after_ch))
    logging.info("VOICE ok? before_ok=%s after_ok=%s", before_ok, after_ok)

    if before_ch is None and after_ok:
        logging.info("VOICE START: %s -> %s", member.display_name, after_ch.name)
        _start_session(guild_id, user_id, after_ch.id, now_ts)
        return

    if before_ok and after_ch is None:
        logging.info("VOICE END: %s left %s", member.display_name, before_ch.name)
        _end_session_and_add(guild_id, user_id, now_ts)
        return

    if before_ok and after_ok and before_ch.id != after_ch.id:
        _end_session_and_add(guild_id, user_id, now_ts)
        _start_session(guild_id, user_id, after_ch.id, now_ts)
        return

    if before_ok and (after_ch is not None) and (not after_ok):
        _end_session_and_add(guild_id, user_id, now_ts)
        return

    if (before_ch is not None) and (not before_ok) and after_ok:
        _start_session(guild_id, user_id, after_ch.id, now_ts)
        return

    # excluded -> excluded or no meaningful change: ignore


@tasks.loop(minutes=30)
async def duty_enforce_periodic():
    await bot.wait_until_ready()

    global DUTY_AUTOMATION_CFG
    DUTY_AUTOMATION_CFG = load_duty_automation_cfg()
    cfg = DUTY_AUTOMATION_CFG or {}

    if not cfg.get("enabled", False):
        return

    period = str(cfg.get("period", "weekly")).lower()
    run_hour = int(cfg.get("run_hour_utc", 12) or 12)

    now = datetime.now(timezone.utc)

    if now.hour != run_hour:
        return

    if period == "monthly":
        first_this = now.date().replace(day=1)
        prev_end = datetime.combine(first_this, datetime.min.time(), tzinfo=timezone.utc) - timedelta(seconds=1)
        prev_start = period_start_utc(prev_end, "monthly")
    else:
        prev_start = period_start_utc(now - timedelta(days=7 if period == "weekly" else 14), period)

    thresholds = cfg.get("thresholds_hours", {}) or {}
    grace_days = int(cfg.get("new_member_grace_days", 0) or 0)

    for guild in bot.guilds:
        for member in guild.members:
            if member.bot:
                continue
            now_ts = int(now.timestamp())
            if _is_on_loa(guild.id, member.id, now_ts):
                continue
            if grace_days and member.joined_at:
                joined = member.joined_at
                if joined.tzinfo is None:
                    joined = joined.replace(tzinfo=timezone.utc)
                if (now - joined).days < grace_days:
                    continue

            sec = _get_week_seconds(guild.id, member.id, prev_start)
            hours = sec / 3600.0
            target = classify_duty_from_hours(hours, thresholds)

            status_roles = get_duty_status_role_ids()
            current = None
            for k, rid in status_roles.items():
                if rid and any(r.id == rid for r in member.roles):
                    current = k
                    break
            if current == target:
                continue

            try:
                pretty = await apply_duty_status(
                    guild,
                    member,
                    target,
                    actor=None,
                    source=f"voice_{period}({hours:.2f}h)",
                )
                entry = audit_log(
                    "duty_auto_enforce",
                    bot.user,
                    {"user_id": member.id, "hours": round(hours, 2), "period_start": prev_start, "target": target, "pretty": pretty},
                    critical=False,
                )
                await post_audit_to_channel(bot, entry)
            except Exception as e:
                logging.exception("Auto duty enforce failed for %s: %s", member.id, e)


# ---------- SLASH COMMANDS ----------

@tree.command(description="Check if the bot is online.")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message(
        f"Pong! 🏓 Latency: {round(bot.latency * 1000)} ms",
        ephemeral=True,
    )


@tree.command(description="Set your timezone for /time (e.g., America/New_York, Europe/London, pst, uk).")
@app_commands.describe(timezone_str="IANA timezone (America/New_York) or shortcut (pst/uk/gmt)")
async def tz_set(interaction: discord.Interaction, timezone_str: str):
    tz = normalize_tz(timezone_str)
    try:
        ZoneInfo(tz)
    except Exception:
        await interaction.response.send_message(
            f"❌ Unknown timezone: `{timezone_str}`. Try `America/New_York` or `Europe/London`.",
            ephemeral=True,
        )
        return

    set_user_timezone(interaction.user.id, tz)
    await interaction.response.send_message(f"✅ Timezone set to `{tz}`", ephemeral=True)


@tree.command(description="Show your saved timezone for /time.")
async def tz_me(interaction: discord.Interaction):
    tz = get_user_timezone(interaction.user.id)
    now_local = datetime.now(ZoneInfo(tz)).strftime("%Y-%m-%d %I:%M %p")
    await interaction.response.send_message(
        f"🧭 Your timezone: `{tz}` • Local time: **{now_local}**",
        ephemeral=True,
    )


@tree.command(description="Post a time ping in everyone's local time.")
@app_commands.describe(when="Examples: 8pm | tomorrow 7pm | 10-12 gmt | 20:00 uk | in 90m")
async def time(interaction: discord.Interaction, when: str):
    user_tz = get_user_timezone(interaction.user.id)

    try:
        start_utc, end_utc = parse_when_to_utc(when, user_tz)
    except Exception:
        await interaction.response.send_message(
            "❌ Couldn't parse that. Try: `8pm`, `tomorrow 7pm`, `10-12 gmt`, `20:00 uk`, `in 90m`",
            ephemeral=True,
        )
        return

    if end_utc:
        msg = f"🕒 **Window:** {stamp(start_utc,'t')}–{stamp(end_utc,'t')} ({stamp(start_utc,'R')})"
    else:
        msg = f"🕒 **Time:** {stamp(start_utc,'t')} ({stamp(start_utc,'R')})"

    await interaction.response.send_message(msg)


@bot.command(name="t", aliases=["time"])
async def time_prefix(ctx: commands.Context, *, when: str):
    """
    Casual time ping: !t 5:00PM
    Also supports your parser: !t 10-12 gmt | !t tomorrow 7pm | !t in 90m
    """
    try:
        user_tz = get_user_timezone(ctx.author.id)
        start_utc, end_utc = parse_when_to_utc(when, user_tz)
    except Exception:
        await ctx.send("❌ Couldn't parse that. Try: `!t 5pm`, `!t tomorrow 7pm`, `!t 10-12 gmt`, `!t in 90m`")
        return

    if end_utc:
        msg = f"🕒 **Window:** {stamp(start_utc,'t')}–{stamp(end_utc,'t')} ({stamp(start_utc,'R')})"
    else:
        msg = f"🕒 **Time:** {stamp(start_utc,'t')} ({stamp(start_utc,'R')})"

    await ctx.send(msg)


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    # allow inline trigger like: "meet me at !t 5pm"
    m = TIME_INLINE_RE.search(message.content)
    if m:
        when = m.group(1).strip()
        try:
            user_tz = get_user_timezone(message.author.id)
            start_utc, end_utc = parse_when_to_utc(when, user_tz)
            if end_utc:
                msg = f"🕒 **Window:** {stamp(start_utc,'t')}–{stamp(end_utc,'t')} ({stamp(start_utc,'R')})"
            else:
                msg = f"🕒 **Time:** {stamp(start_utc,'t')} ({stamp(start_utc,'R')})"
            await message.channel.send(msg)
        except Exception:
            await message.channel.send("❌ Couldn't parse that. Try: `!t 5pm`, `!t 10-12 gmt`, `!t in 90m`")

    # keep normal prefix commands working too
    await bot.process_commands(message)


@tree.command(description="Send a test raid alert to the raid channel.")
@app_commands.describe(base_name="Name of the base to include in the message.")
async def raid_test(interaction: discord.Interaction, base_name: str = "Main Base"):
    if not interaction.guild:
        await interaction.response.send_message(
            "This command can only be used in a server.", ephemeral=True
        )
        return

    desc = "Get online and defend! 🎲🔫"
    desc = add_f1_to_description(desc)

    embed = make_embed(
        title="🚨 RAID ALERT!",
        description=desc,
        color=discord.Color.dark_red(),
        base_name=base_name,
        status_emoji="⚔️ Under Attack",
    )
    await send_alert(interaction.guild, embed, ping_team=True)
    await interaction.response.send_message(
        f"Raid test sent for **{base_name}** ✅", ephemeral=True
    )


@tree.command(description="Mark a base as ONLINE and ready.")
@app_commands.describe(base_name="Name of the base that is now online.")
async def base_online(interaction: discord.Interaction, base_name: str = "Main"):
    if not interaction.guild:
        await interaction.response.send_message(
            "This command can only be used in a server.", ephemeral=True
        )
        return

    embed = make_embed(
        title="🟢 ONLINE STATUS",
        description="Team is now ONLINE and ready. ✅",
        color=discord.Color.dark_green(),
        base_name=base_name,
        status_emoji="🟢 ONLINE",
    )
    # 🔕 no ping on status
    await send_alert(interaction.guild, embed, ping_team=False)
    await interaction.response.send_message(
        f"Online status alert sent for **{base_name}** ✅", ephemeral=True
    )


@tree.command(description="Mark a base as OFFLINE / sleeping.")
@app_commands.describe(base_name="Name of the base that is now offline.")
async def base_offline(interaction: discord.Interaction, base_name: str = "Main"):
    if not interaction.guild:
        await interaction.response.send_message(
            "This command can only be used in a server.", ephemeral=True
        )
        return

    embed = make_embed(
        title="⚫ OFFLINE STATUS",
        description="Team is now OFFLINE. 😴💤",
        color=discord.Color.dark_grey(),
        base_name=base_name,
        status_emoji="⚫ OFFLINE",
    )
    # 🔕 no ping on status
    await send_alert(interaction.guild, embed, ping_team=False)
    await interaction.response.send_message(
        f"Offline status alert sent for **{base_name}** ✅", ephemeral=True
    )


@tree.command(description="Open a menu of Rust servers to connect to (F1 console commands).")
async def connect(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    try:
        if not CONNECT_PROFILES:
            await interaction.followup.send(
                "No connect profiles are configured yet. Ask staff to update `connect_servers.json`.",
                ephemeral=True,
            )
            return

        view = ConnectMenuView(CONNECT_PROFILES)
        message = await interaction.followup.send(
            "Select a server to get its F1 connect command:",
            view=view,
            ephemeral=True,
            wait=True,
        )
        view.message = message
    except Exception:
        logging.exception("Failed to handle /connect interaction")
        if interaction.response.is_done():
            await interaction.followup.send(
                "⚠️ Connect failed. Check logs.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                "⚠️ Connect failed. Check logs.",
                ephemeral=True,
            )


@tree.command(description="Reload /connect server profiles from the config file.")
@is_leadership()
async def connect_reload(interaction: discord.Interaction):
    """Reload connect_servers.json without restarting the bot."""
    global CONNECT_PROFILES, CONNECT_PROFILE_INDEX

    new_profiles, new_index = load_connect_profiles()
    CONNECT_PROFILES = new_profiles
    CONNECT_PROFILE_INDEX = new_index

    if not CONNECT_PROFILES:
        msg = (
            f"Reloaded connect profiles from `{os.path.basename(CONNECT_CONFIG_PATH)}`, "
            "but no valid profiles were found. Check the JSON format."
        )
    else:
        msg = (
            f"Reloaded **{len(CONNECT_PROFILES)}** connect profiles from "
            f"`{os.path.basename(CONNECT_CONFIG_PATH)}` ✅"
        )

    await interaction.response.send_message(msg, ephemeral=True)


@tree.command(description="Reload role configuration from roles_config.json.")
async def roles_reload(interaction: discord.Interaction):
    """Leadership-only: reload ROLE_CONFIG from disk."""
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message(
            "This command can only be used in a server.",
            ephemeral=True,
        )
        return

    # Only leadership (and optionally recruiter / event_coord) can reload roles
    allowed_role_ids = [
        get_role_id("leadership"),
        get_role_id("recruiter"),
        get_role_id("event_coord"),
    ]
    if not user_has_any_role(interaction.user, allowed_role_ids):
        await interaction.response.send_message(
            "❌ You don't have permission to reload role configuration.",
            ephemeral=True,
        )
        return

    global ROLE_CONFIG
    ROLE_CONFIG = _read_roles_config_raw()
    await interaction.response.send_message(
        f"✅ Reloaded **{len(ROLE_CONFIG)}** role mappings from `roles_config.json`.",
        ephemeral=True,
    )


@tree.command(description="Run duty enforcement NOW (leadership only).")
async def duty_run_now(interaction: discord.Interaction):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("Server-only.", ephemeral=True)
        return

    allowed_role_ids = [get_role_id("leadership")]
    if not user_has_any_role(interaction.user, allowed_role_ids):
        await interaction.response.send_message("⛔ Leadership only.", ephemeral=True)
        return

    await interaction.response.send_message("✅ Running duty enforcement now...", ephemeral=True)

    # Temporarily bypass the run_hour gate by calling the core logic inline:
    global DUTY_AUTOMATION_CFG
    DUTY_AUTOMATION_CFG = load_duty_automation_cfg()
    cfg = DUTY_AUTOMATION_CFG or {}

    if not cfg.get("enabled", False):
        await interaction.followup.send("⚠️ duty_automation.json has enabled=false", ephemeral=True)
        return

    period = str(cfg.get("period", "weekly")).lower()
    now = datetime.now(timezone.utc)

    if period == "monthly":
        first_this = now.date().replace(day=1)
        prev_end = datetime.combine(first_this, datetime.min.time(), tzinfo=timezone.utc) - timedelta(seconds=1)
        prev_start = period_start_utc(prev_end, "monthly")
    else:
        prev_start = period_start_utc(now - timedelta(days=7 if period == "weekly" else 14), period)

    thresholds = cfg.get("thresholds_hours", {}) or {}
    changed = 0
    grace_days = int(cfg.get("new_member_grace_days", 0) or 0)

    for member in interaction.guild.members:
        if member.bot:
            continue
        if grace_days and member.joined_at:
            joined = member.joined_at
            if joined.tzinfo is None:
                joined = joined.replace(tzinfo=timezone.utc)
            if (now - joined).days < grace_days:
                continue

        sec = _get_week_seconds(interaction.guild.id, member.id, prev_start)
        hours = sec / 3600.0
        target = classify_duty_from_hours(hours, thresholds)

        status_roles = get_duty_status_role_ids()
        current = None
        for k, rid in status_roles.items():
            if rid and any(r.id == rid for r in member.roles):
                current = k
                break

        if current == target:
            continue

        try:
            await apply_duty_status(
                interaction.guild,
                member,
                target,
                actor=interaction.user,
                source=f"voice_{period}({hours:.2f}h)_manual",
            )
            changed += 1
        except Exception:
            logging.exception("Failed duty update for %s", member.id)

    await interaction.followup.send(f"✅ Enforcement complete. Changed **{changed}** member(s).", ephemeral=True)


@tree.command(description="Show VC hours this period (debug).")
async def duty_debug_me(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("Server-only.", ephemeral=True)
        return

    cfg = load_duty_automation_cfg() or {}
    period = str(cfg.get("period", "weekly")).lower()
    now = datetime.now(timezone.utc)

    if period == "monthly":
        first_this = now.date().replace(day=1)
        prev_end = datetime.combine(first_this, datetime.min.time(), tzinfo=timezone.utc) - timedelta(seconds=1)
        start = period_start_utc(prev_end, "monthly")
    else:
        start = period_start_utc(now - timedelta(days=7 if period == "weekly" else 14), period)

    sec = _get_week_seconds(interaction.guild.id, interaction.user.id, start)
    hours = sec / 3600.0
    target = classify_duty_from_hours(hours, (cfg.get("thresholds_hours") or {}))

    await interaction.response.send_message(
        f"🧾 **Duty Debug**\n"
        f"Period: `{period}`\n"
        f"Start: `{start}`\n"
        f"Seconds: `{sec}` (~{hours:.2f}h)\n"
        f"Would classify as: **{pretty_status_label(target)}**",
        ephemeral=True,
    )


duty_config_group = app_commands.Group(name="duty_config", description="Configure duty automation (leadership only).")


def _require_leadership(interaction: discord.Interaction) -> bool:
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return False
    return user_has_any_role(interaction.user, [get_role_id("leadership")]) or interaction.user.guild_permissions.administrator


async def _deny(interaction: discord.Interaction):
    await interaction.response.send_message("⛔ Leadership only.", ephemeral=True)


@duty_config_group.command(name="show", description="Show current duty automation config.")
async def duty_config_show(interaction: discord.Interaction):
    if not _require_leadership(interaction):
        return await _deny(interaction)

    global DUTY_AUTOMATION_CFG
    DUTY_AUTOMATION_CFG = load_duty_automation_cfg()
    pretty = json.dumps(DUTY_AUTOMATION_CFG, indent=2, ensure_ascii=False)
    await interaction.response.send_message(f"```json\n{pretty}\n```", ephemeral=True)


@duty_config_group.command(name="enable", description="Enable/disable duty automation.")
@app_commands.describe(enabled="true/false")
async def duty_config_enable(interaction: discord.Interaction, enabled: bool):
    if not _require_leadership(interaction):
        return await _deny(interaction)

    global DUTY_AUTOMATION_CFG
    DUTY_AUTOMATION_CFG = load_duty_automation_cfg()
    DUTY_AUTOMATION_CFG["enabled"] = bool(enabled)
    save_duty_automation_cfg(DUTY_AUTOMATION_CFG)
    await interaction.response.send_message(f"✅ duty automation enabled = `{enabled}`", ephemeral=True)


@duty_config_group.command(name="period", description="Set enforcement period.")
@app_commands.choices(period=[
    app_commands.Choice(name="weekly", value="weekly"),
    app_commands.Choice(name="biweekly", value="biweekly"),
    app_commands.Choice(name="monthly", value="monthly"),
])
async def duty_config_period(interaction: discord.Interaction, period: app_commands.Choice[str]):
    if not _require_leadership(interaction):
        return await _deny(interaction)

    global DUTY_AUTOMATION_CFG
    DUTY_AUTOMATION_CFG = load_duty_automation_cfg()
    DUTY_AUTOMATION_CFG["period"] = period.value
    save_duty_automation_cfg(DUTY_AUTOMATION_CFG)
    await interaction.response.send_message(f"✅ period = `{period.value}`", ephemeral=True)


@duty_config_group.command(name="run_hour_utc", description="Set the UTC hour enforcement runs (0-23).")
async def duty_config_run_hour(interaction: discord.Interaction, hour: app_commands.Range[int, 0, 23]):
    if not _require_leadership(interaction):
        return await _deny(interaction)

    global DUTY_AUTOMATION_CFG
    DUTY_AUTOMATION_CFG = load_duty_automation_cfg()
    DUTY_AUTOMATION_CFG["run_hour_utc"] = int(hour)
    save_duty_automation_cfg(DUTY_AUTOMATION_CFG)
    await interaction.response.send_message(f"✅ run_hour_utc = `{hour}`", ephemeral=True)


@duty_config_group.command(name="min_session_seconds", description="Minimum VC session length to count (seconds).")
async def duty_config_min_session(interaction: discord.Interaction, seconds: app_commands.Range[int, 0, 86400]):
    if not _require_leadership(interaction):
        return await _deny(interaction)

    global DUTY_AUTOMATION_CFG
    DUTY_AUTOMATION_CFG = load_duty_automation_cfg()
    DUTY_AUTOMATION_CFG["min_session_seconds"] = int(seconds)
    save_duty_automation_cfg(DUTY_AUTOMATION_CFG)
    await interaction.response.send_message(f"✅ min_session_seconds = `{seconds}`", ephemeral=True)


@duty_config_group.command(name="threshold_set", description="Set hours threshold for a duty status.")
@app_commands.choices(status=[
    app_commands.Choice(name="active_duty", value="active_duty"),
    app_commands.Choice(name="reservist", value="reservist"),
    app_commands.Choice(name="inactive_reservist", value="inactive_reservist"),
])
async def duty_config_threshold_set(interaction: discord.Interaction, status: app_commands.Choice[str], hours: float):
    if not _require_leadership(interaction):
        return await _deny(interaction)

    global DUTY_AUTOMATION_CFG
    DUTY_AUTOMATION_CFG = load_duty_automation_cfg()
    DUTY_AUTOMATION_CFG.setdefault("thresholds_hours", {})
    DUTY_AUTOMATION_CFG["thresholds_hours"][status.value] = float(hours)
    save_duty_automation_cfg(DUTY_AUTOMATION_CFG)
    await interaction.response.send_message(f"✅ threshold `{status.value}` = `{hours}` hours", ephemeral=True)


@duty_config_group.command(name="exclude_name_add", description="Exclude a voice channel by name (case-insensitive exact match).")
async def duty_config_exclude_name_add(interaction: discord.Interaction, name: str):
    if not _require_leadership(interaction):
        return await _deny(interaction)

    global DUTY_AUTOMATION_CFG
    DUTY_AUTOMATION_CFG = load_duty_automation_cfg()
    DUTY_AUTOMATION_CFG.setdefault("exclude_voice_channel_names", [])
    names = {str(x).strip() for x in DUTY_AUTOMATION_CFG["exclude_voice_channel_names"] if x}
    names.add(name.strip())
    DUTY_AUTOMATION_CFG["exclude_voice_channel_names"] = sorted(names)
    save_duty_automation_cfg(DUTY_AUTOMATION_CFG)
    await interaction.response.send_message(f"✅ excluded channel name added: `{name.strip()}`", ephemeral=True)


@duty_config_group.command(name="exclude_name_remove", description="Remove excluded voice channel name.")
async def duty_config_exclude_name_remove(interaction: discord.Interaction, name: str):
    if not _require_leadership(interaction):
        return await _deny(interaction)

    global DUTY_AUTOMATION_CFG
    DUTY_AUTOMATION_CFG = load_duty_automation_cfg()
    cur = [str(x).strip() for x in DUTY_AUTOMATION_CFG.get("exclude_voice_channel_names", []) if x]
    new = [x for x in cur if x.lower() != name.strip().lower()]
    DUTY_AUTOMATION_CFG["exclude_voice_channel_names"] = new
    save_duty_automation_cfg(DUTY_AUTOMATION_CFG)
    await interaction.response.send_message(f"✅ excluded channel name removed: `{name.strip()}`", ephemeral=True)


@duty_config_group.command(name="exclude_id_add", description="Exclude a voice channel by ID.")
async def duty_config_exclude_id_add(interaction: discord.Interaction, channel_id: str):
    if not _require_leadership(interaction):
        return await _deny(interaction)

    if not channel_id.isdigit():
        return await interaction.response.send_message("❌ channel_id must be numeric.", ephemeral=True)

    cid = int(channel_id)
    global DUTY_AUTOMATION_CFG
    DUTY_AUTOMATION_CFG = load_duty_automation_cfg()
    DUTY_AUTOMATION_CFG.setdefault("exclude_voice_channel_ids", [])
    ids = {int(x) for x in DUTY_AUTOMATION_CFG["exclude_voice_channel_ids"] if str(x).isdigit()}
    ids.add(cid)
    DUTY_AUTOMATION_CFG["exclude_voice_channel_ids"] = sorted(ids)
    save_duty_automation_cfg(DUTY_AUTOMATION_CFG)
    await interaction.response.send_message(f"✅ excluded channel id added: `{cid}`", ephemeral=True)


@duty_config_group.command(name="exclude_id_remove", description="Remove excluded voice channel ID.")
async def duty_config_exclude_id_remove(interaction: discord.Interaction, channel_id: str):
    if not _require_leadership(interaction):
        return await _deny(interaction)

    if not channel_id.isdigit():
        return await interaction.response.send_message("❌ channel_id must be numeric.", ephemeral=True)

    cid = int(channel_id)
    global DUTY_AUTOMATION_CFG
    DUTY_AUTOMATION_CFG = load_duty_automation_cfg()
    ids = [int(x) for x in DUTY_AUTOMATION_CFG.get("exclude_voice_channel_ids", []) if str(x).isdigit()]
    DUTY_AUTOMATION_CFG["exclude_voice_channel_ids"] = [x for x in ids if x != cid]
    save_duty_automation_cfg(DUTY_AUTOMATION_CFG)
    await interaction.response.send_message(f"✅ excluded channel id removed: `{cid}`", ephemeral=True)


tree.add_command(duty_config_group)


@tree.command(description="Put a member on Leave of Absence for X days (skips duty automation).")
@app_commands.describe(member="Member to place on LOA", days="How many days", reason="Optional reason")
async def loa_set(interaction: discord.Interaction, member: discord.Member, days: int, reason: str | None = None):
    if not interaction.guild:
        await interaction.response.send_message("Server-only command.", ephemeral=True)
        return
    if not isinstance(interaction.user, discord.Member) or not has_permission(interaction, "staff_manage"):
        await interaction.response.send_message("⛔ Staff only.", ephemeral=True)
        return
    if days <= 0 or days > 365:
        await interaction.response.send_message("❌ days must be between 1 and 365.", ephemeral=True)
        return

    now_ts = int(datetime.now(timezone.utc).timestamp())
    end_ts = now_ts + int(days * 86400)

    _loa_set(interaction.guild.id, member.id, now_ts, end_ts, reason, interaction.user.id)

    # optional: end any running voice session so they don't get partial history weirdness
    _end_session_and_add(interaction.guild.id, member.id, now_ts)

    until_dt = datetime.fromtimestamp(end_ts, tz=timezone.utc)
    msg = f"✅ {member.mention} is on **LOA** until {stamp(until_dt, 'f')} ({stamp(until_dt, 'R')})."
    if reason:
        msg += f"\n**Reason:** {reason}"

    await interaction.response.send_message(msg, ephemeral=True)


@tree.command(description="End a member's Leave of Absence early.")
@app_commands.describe(member="Member to remove from LOA")
async def loa_end(interaction: discord.Interaction, member: discord.Member):
    if not interaction.guild:
        await interaction.response.send_message("Server-only command.", ephemeral=True)
        return
    if not isinstance(interaction.user, discord.Member) or not has_permission(interaction, "staff_manage"):
        await interaction.response.send_message("⛔ Staff only.", ephemeral=True)
        return

    removed = _loa_clear(interaction.guild.id, member.id)
    if removed:
        await interaction.response.send_message(f"✅ LOA cleared for {member.mention}.", ephemeral=True)
    else:
        await interaction.response.send_message(f"ℹ️ {member.mention} is not currently on LOA.", ephemeral=True)


@tree.command(description="Check your current Leave of Absence status.")
async def loa_me(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("Server-only command.", ephemeral=True)
        return

    now_ts = int(datetime.now(timezone.utc).timestamp())
    row = _loa_get_active(interaction.guild.id, interaction.user.id, now_ts)
    if not row:
        await interaction.response.send_message("✅ You are **not** on LOA.", ephemeral=True)
        return

    _, end_ts, reason = row
    until_dt = datetime.fromtimestamp(end_ts, tz=timezone.utc)
    msg = f"🟦 You are on **LOA** until {stamp(until_dt, 'f')} ({stamp(until_dt, 'R')})."
    if reason:
        msg += f"\n**Reason:** {reason}"
    await interaction.response.send_message(msg, ephemeral=True)


@bot.tree.command(description="Add a new server profile to the /connect menu.")
async def connect_add(
    interaction: discord.Interaction,
    key: str,
    label: str,
    f1: str,
    category: str | None = None,
    emoji: str | None = None,
    notes: str | None = None,
):
    """Leadership-only: add a new connect profile."""
    # Must be in guild & be a Member
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message(
            "This command can only be used in a server.",
            ephemeral=True,
        )
        return

    # Permissions
    allowed_role_ids = [
        get_role_id("leadership"),
        get_role_id("recruiter"),
        get_role_id("event_coord"),
    ]
    if not user_has_any_role(interaction.user, allowed_role_ids):
        await interaction.response.send_message(
            "❌ You don't have permission to modify /connect profiles.",
            ephemeral=True,
        )
        return

    key = key.strip()
    label = label.strip()
    f1 = f1.strip()

    if not key or not label or not f1:
        await interaction.response.send_message(
            "❌ key, label, and f1 are required.",
            ephemeral=True,
        )
        return

    # Load current entries
    entries = _read_connect_config_raw()

    # Ensure key is unique
    for entry in entries:
        if entry.get("key") == key:
            await interaction.response.send_message(
                f"❌ A profile with key `{key}` already exists.",
                ephemeral=True,
            )
            return

    new_entry = {
        "key": key,
        "label": label,
        "f1": f1,
    }
    if category:
        new_entry["category"] = category
    if emoji:
        new_entry["emoji"] = emoji
    if notes:
        new_entry["notes"] = notes

    entries.append(new_entry)

    if not _write_connect_config_raw(entries):
        await interaction.response.send_message(
            "❌ Failed to write connect_servers.json. Check logs.",
            ephemeral=True,
        )
        return

    # Reload in-memory profiles
    global CONNECT_PROFILES, CONNECT_PROFILE_INDEX
    CONNECT_PROFILES, CONNECT_PROFILE_INDEX = load_connect_profiles()

    await interaction.response.send_message(
        f"✅ Added new connect profile **{label}** (`{key}`).",
        ephemeral=True,
    )


@bot.tree.command(description="Remove a server profile from the /connect menu.")
async def connect_remove(
    interaction: discord.Interaction,
    key: str,
):
    """Leadership-only: remove an existing connect profile by key."""
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message(
            "This command can only be used in a server.",
            ephemeral=True,
        )
        return

    allowed_role_ids = [
        get_role_id("leadership"),
        get_role_id("recruiter"),
        get_role_id("event_coord"),
    ]
    if not user_has_any_role(interaction.user, allowed_role_ids):
        await interaction.response.send_message(
            "❌ You don't have permission to modify /connect profiles.",
            ephemeral=True,
        )
        return

    key = key.strip()
    if not key:
        await interaction.response.send_message(
            "❌ Please provide a profile key to remove.",
            ephemeral=True,
        )
        return

    entries = _read_connect_config_raw()
    before = len(entries)
    entries = [e for e in entries if e.get("key") != key]
    removed = before - len(entries)

    if removed == 0:
        await interaction.response.send_message(
            f"❌ No profile found with key `{key}`.",
            ephemeral=True,
        )
        return

    if not _write_connect_config_raw(entries):
        await interaction.response.send_message(
            "❌ Failed to write connect_servers.json. Check logs.",
            ephemeral=True,
        )
        return

    global CONNECT_PROFILES, CONNECT_PROFILE_INDEX
    CONNECT_PROFILES, CONNECT_PROFILE_INDEX = load_connect_profiles()

    await interaction.response.send_message(
        f"✅ Removed **{removed}** profile(s) with key `{key}`.",
        ephemeral=True,
    )


@bot.tree.command(description="Update the F1 connect string (and optionally label/category) for a server profile.")
async def connect_set_f1(
    interaction: discord.Interaction,
    key: str,
    f1: str,
    label: str | None = None,
    category: str | None = None,
):
    """Leadership-only: update an existing profile's F1 string."""
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message(
            "This command can only be used in a server.",
            ephemeral=True,
        )
        return

    allowed_role_ids = [
        get_role_id("leadership"),
        get_role_id("recruiter"),
        get_role_id("event_coord"),
    ]
    if not user_has_any_role(interaction.user, allowed_role_ids):
        await interaction.response.send_message(
            "❌ You don't have permission to modify /connect profiles.",
            ephemeral=True,
        )
        return

    key = key.strip()
    f1 = f1.strip()

    if not key or not f1:
        await interaction.response.send_message(
            "❌ key and f1 are required.",
            ephemeral=True,
        )
        return

    entries = _read_connect_config_raw()
    found = False

    for entry in entries:
        if entry.get("key") == key:
            entry["f1"] = f1
            if label:
                entry["label"] = label
            if category:
                entry["category"] = category
            found = True
            break

    if not found:
        await interaction.response.send_message(
            f"❌ No profile found with key `{key}`.",
            ephemeral=True,
        )
        return

    if not _write_connect_config_raw(entries):
        await interaction.response.send_message(
            "❌ Failed to write connect_servers.json. Check logs.",
            ephemeral=True,
        )
        return

    global CONNECT_PROFILES, CONNECT_PROFILE_INDEX
    CONNECT_PROFILES, CONNECT_PROFILE_INDEX = load_connect_profiles()

    await interaction.response.send_message(
        f"✅ Updated F1 connect string for profile `{key}`.",
        ephemeral=True,
    )


staff_group = app_commands.Group(name="staff", description="Staff management commands")


async def unit_autocomplete(interaction: discord.Interaction, current: str):
    cfg = staff_roles_cfg()
    units = cfg.get("units", {})
    choices: list[app_commands.Choice[str]] = []

    for category, mapping in units.items():
        for unit_name, rid in (mapping or {}).items():
            if not rid:
                continue
            label = f"{category}: {unit_name}"
            if current.lower() in label.lower():
                choices.append(app_commands.Choice(name=label, value=str(rid)))

    return choices[:25]


async def leader_autocomplete(interaction: discord.Interaction, current: str):
    cfg = staff_roles_cfg()
    lead = cfg.get("leadership", {})
    choices: list[app_commands.Choice[str]] = []
    for key, rid in (lead or {}).items():
        if not rid:
            continue
        label = key.replace("_", " ").title()
        if current.lower() in label.lower():
            choices.append(app_commands.Choice(name=label, value=str(rid)))
    return choices[:25]


@staff_group.command(name="roster", description="Show the current formation roster (HQ + Guns).")
async def staff_roster(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("Server-only command.", ephemeral=True)
        return
    if not has_permission(interaction, "staff_manage"):
        await interaction.response.send_message("⛔ Staff only.", ephemeral=True)
        return

    cfg = staff_roles_cfg()
    units = cfg.get("units", {})
    lead_ids = leadership_role_ids(cfg)

    lines = ["**📋 Staff Roster (Formation View)**"]
    for category, mapping in units.items():
        lines.append(f"\n__**{category}**__")
        for unit_name, rid in (mapping or {}).items():
            role = find_role(interaction.guild, int(rid or 0))
            if not role:
                continue

            members = sorted(role.members, key=lambda m: m.display_name.lower())
            if not members:
                continue

            rendered = []
            for m in members:
                is_lead = any(r.id in lead_ids for r in m.roles)
                rendered.append(f"{'🧭' if is_lead else '•'} {m.mention}")

            lines.append(f"**{unit_name}** ({len(members)}): " + ", ".join(rendered))

    await interaction.response.send_message("\n".join(lines)[:1900], ephemeral=True)


@staff_group.command(name="assign", description="Assign a user to a unit (strips other unit roles first).")
@app_commands.describe(member="Member to assign", unit="Unit to assign them to")
@app_commands.autocomplete(unit=unit_autocomplete)
async def staff_assign(interaction: discord.Interaction, member: discord.Member, unit: str):
    if not interaction.guild:
        await interaction.response.send_message("Server-only command.", ephemeral=True)
        return
    if not has_permission(interaction, "staff_manage"):
        await interaction.response.send_message("⛔ Staff only.", ephemeral=True)
        return

    roles_cfg = staff_roles_cfg()
    unit_role_id = int(unit)

    to_remove = []
    for rid in flatten_unit_roles(roles_cfg):
        r = find_role(interaction.guild, rid)
        if r and r in member.roles:
            to_remove.append(r)

    target_role = find_role(interaction.guild, unit_role_id)
    if not target_role:
        await interaction.response.send_message("⚠️ That unit role is not configured or not found.", ephemeral=True)
        return

    try:
        if to_remove:
            await member.remove_roles(*to_remove, reason=f"Staff assign by {interaction.user}")
        await member.add_roles(target_role, reason=f"Staff assign by {interaction.user}")

        member_role_id = status_role_id(roles_cfg, "member")
        member_role = find_role(interaction.guild, member_role_id)
        if member_role and member_role not in member.roles:
            await member.add_roles(member_role, reason="Ensure member status")

        entry = audit_log(
            "staff_assign",
            interaction.user,
            {"target_id": member.id, "target": str(member), "unit_role_id": unit_role_id, "unit_role": target_role.name},
            critical=True,
        )
        await post_audit_to_channel(interaction.client, entry)

        await interaction.response.send_message(f"✅ Assigned {member.mention} to **{target_role.name}**", ephemeral=True)

    except discord.Forbidden:
        await interaction.response.send_message("❌ I don’t have permission to edit that user’s roles.", ephemeral=True)
    except Exception as e:
        entry = audit_log(
            "staff_assign_error",
            interaction.user,
            {"target_id": member.id, "error": str(e)},
            critical=True,
        )
        await post_audit_to_channel(interaction.client, entry)
        await interaction.response.send_message("❌ Failed to assign (check bot role hierarchy).", ephemeral=True)


@staff_group.command(name="remove", description="Remove a user from all unit + leadership roles (optionally move to Visitor).")
@app_commands.describe(member="Member to clear", to_visitor="Also add Visitor role")
async def staff_remove(interaction: discord.Interaction, member: discord.Member, to_visitor: bool = True):
    if not interaction.guild:
        await interaction.response.send_message("Server-only command.", ephemeral=True)
        return
    if not has_permission(interaction, "staff_manage"):
        await interaction.response.send_message("⛔ Staff only.", ephemeral=True)
        return

    roles_cfg = staff_roles_cfg()

    remove_ids = set(flatten_unit_roles(roles_cfg) + leadership_role_ids(roles_cfg))
    to_remove = []
    for rid in remove_ids:
        r = find_role(interaction.guild, rid)
        if r and r in member.roles:
            to_remove.append(r)

    try:
        if to_remove:
            await member.remove_roles(*to_remove, reason=f"Staff remove by {interaction.user}")

        if to_visitor:
            visitor_id = status_role_id(roles_cfg, "visitor")
            visitor_role = find_role(interaction.guild, visitor_id)
            if visitor_role and visitor_role not in member.roles:
                await member.add_roles(visitor_role, reason=f"Moved to Visitor by {interaction.user}")

        entry = audit_log(
            "staff_remove",
            interaction.user,
            {"target_id": member.id, "target": str(member), "to_visitor": to_visitor},
            critical=True,
        )
        await post_audit_to_channel(interaction.client, entry)

        await interaction.response.send_message(f"✅ Cleared unit/lead roles for {member.mention}", ephemeral=True)

    except discord.Forbidden:
        await interaction.response.send_message("❌ I don’t have permission to edit that user’s roles.", ephemeral=True)
    except Exception as e:
        entry = audit_log("staff_remove_error", interaction.user, {"target_id": member.id, "error": str(e)}, critical=True)
        await post_audit_to_channel(interaction.client, entry)
        await interaction.response.send_message("❌ Failed to remove roles.", ephemeral=True)


@staff_group.command(name="leader_add", description="Add a leadership role to a member (HQ Lead / Platoon Lead / Squad Lead).")
@app_commands.describe(member="Member to update", leader_role="Leadership role to add")
@app_commands.autocomplete(leader_role=leader_autocomplete)
async def staff_leader_add(interaction: discord.Interaction, member: discord.Member, leader_role: str):
    if not interaction.guild:
        await interaction.response.send_message("Server-only command.", ephemeral=True)
        return
    if not has_permission(interaction, "promote_demote"):
        await interaction.response.send_message("⛔ Command only.", ephemeral=True)
        return

    rid = int(leader_role)
    role = find_role(interaction.guild, rid)
    if not role:
        await interaction.response.send_message("⚠️ Leadership role not found/configured.", ephemeral=True)
        return

    try:
        await member.add_roles(role, reason=f"Leader add by {interaction.user}")

        entry = audit_log(
            "leader_add",
            interaction.user,
            {"target_id": member.id, "target": str(member), "leader_role_id": rid, "leader_role": role.name},
            critical=True,
        )
        await post_audit_to_channel(interaction.client, entry)

        await interaction.response.send_message(f"✅ Added **{role.name}** to {member.mention}", ephemeral=True)

    except discord.Forbidden:
        await interaction.response.send_message("❌ I don’t have permission to edit that user’s roles.", ephemeral=True)
    except Exception as e:
        entry = audit_log("leader_add_error", interaction.user, {"target_id": member.id, "error": str(e)}, critical=True)
        await post_audit_to_channel(interaction.client, entry)
        await interaction.response.send_message("❌ Failed to add leader role.", ephemeral=True)


@staff_group.command(name="leader_remove", description="Remove a leadership role from a member.")
@app_commands.describe(member="Member to update", leader_role="Leadership role to remove")
@app_commands.autocomplete(leader_role=leader_autocomplete)
async def staff_leader_remove(interaction: discord.Interaction, member: discord.Member, leader_role: str):
    if not interaction.guild:
        await interaction.response.send_message("Server-only command.", ephemeral=True)
        return
    if not has_permission(interaction, "promote_demote"):
        await interaction.response.send_message("⛔ Command only.", ephemeral=True)
        return

    rid = int(leader_role)
    role = find_role(interaction.guild, rid)
    if not role:
        await interaction.response.send_message("⚠️ Leadership role not found/configured.", ephemeral=True)
        return

    try:
        if role in member.roles:
            await member.remove_roles(role, reason=f"Leader remove by {interaction.user}")

        entry = audit_log(
            "leader_remove",
            interaction.user,
            {"target_id": member.id, "target": str(member), "leader_role_id": rid, "leader_role": role.name},
            critical=True,
        )
        await post_audit_to_channel(interaction.client, entry)

        await interaction.response.send_message(f"✅ Removed **{role.name}** from {member.mention}", ephemeral=True)

    except discord.Forbidden:
        await interaction.response.send_message("❌ I don’t have permission to edit that user’s roles.", ephemeral=True)
    except Exception as e:
        entry = audit_log("leader_remove_error", interaction.user, {"target_id": member.id, "error": str(e)}, critical=True)
        await post_audit_to_channel(interaction.client, entry)
        await interaction.response.send_message("❌ Failed to remove leader role.", ephemeral=True)


tree.add_command(staff_group)


# ---------- SAM & HQ SWITCH COMMANDS ----------

@tree.command(description="Turn MAIN SAM site ON (via smart switch).")
async def sam_on(interaction: discord.Interaction):
    if not await ensure_rust_permission(interaction):
        return

    audit_log(
        "rust_control",
        interaction.user,
        {"command": "sam_on"},
    )

    await interaction.response.defer(ephemeral=True)
    ok, msg = await call_entity_action("sam_main", "on")
    if ok:
        await interaction.followup.send("🟢 MAIN SAM turned **ON** ✅", ephemeral=True)
    else:
        await interaction.followup.send(f"❌ Failed to turn ON MAIN SAM: {msg}", ephemeral=True)


@tree.command(description="Turn MAIN SAM site OFF (via smart switch).")
async def sam_off(interaction: discord.Interaction):
    if not await ensure_rust_permission(interaction):
        return

    audit_log(
        "rust_control",
        interaction.user,
        {"command": "sam_off"},
    )

    await interaction.response.defer(ephemeral=True)
    ok, msg = await call_entity_action("sam_main", "off")
    if ok:
        await interaction.followup.send("⚫ MAIN SAM turned **OFF** ✅", ephemeral=True)
    else:
        await interaction.followup.send(f"❌ Failed to turn OFF MAIN SAM: {msg}", ephemeral=True)


@tree.command(description="Check MAIN SAM smart switch status.")
async def sam_status(interaction: discord.Interaction):
    if not await ensure_rust_permission(interaction):
        return

    audit_log(
        "rust_control",
        interaction.user,
        {"command": "sam_status"},
    )

    await interaction.response.defer(ephemeral=True)
    ok, msg = await call_entity_action("sam_main", "status")
    if ok:
        await interaction.followup.send(f"ℹ️ MAIN SAM status:\n`{msg}`", ephemeral=True)
    else:
        await interaction.followup.send(f"❌ Failed to get MAIN SAM status: {msg}", ephemeral=True)


@tree.command(description="Turn MAIN SAM site ON via Rust+.")
async def sam_main_on(interaction: discord.Interaction):
    if not await ensure_rust_permission(interaction):
        return

    audit_log(
        "rust_control",
        interaction.user,
        {"command": "sam_main_on"},
    )
    await handle_entity_action(interaction, "sam_main", "on")


@tree.command(description="Turn MAIN SAM site OFF via Rust+.")
async def sam_main_off(interaction: discord.Interaction):
    if not await ensure_rust_permission(interaction):
        return

    audit_log(
        "rust_control",
        interaction.user,
        {"command": "sam_main_off"},
    )
    await handle_entity_action(interaction, "sam_main", "off")


@tree.command(description="Check MAIN SAM site status via Rust+.")
async def sam_main_status(interaction: discord.Interaction):
    if not await ensure_rust_permission(interaction):
        return

    audit_log(
        "rust_control",
        interaction.user,
        {"command": "sam_main_status"},
    )
    await handle_entity_status(interaction, "sam_main")


@tree.command(description="Turn HQ main switch ON.")
async def hq_on(interaction: discord.Interaction):
    if not await ensure_rust_permission(interaction):
        return

    audit_log(
        "rust_control",
        interaction.user,
        {"command": "hq_on"},
    )

    await interaction.response.defer(ephemeral=True)
    ok, msg = await call_entity_action("switch_hq", "on")
    if ok:
        await interaction.followup.send("🟢 HQ main switch turned **ON** ✅", ephemeral=True)
    else:
        await interaction.followup.send(f"❌ Failed to turn ON HQ switch: {msg}", ephemeral=True)


@tree.command(description="Turn HQ main switch OFF.")
async def hq_off(interaction: discord.Interaction):
    if not await ensure_rust_permission(interaction):
        return

    audit_log(
        "rust_control",
        interaction.user,
        {"command": "hq_off"},
    )

    await interaction.response.defer(ephemeral=True)
    ok, msg = await call_entity_action("switch_hq", "off")
    if ok:
        await interaction.followup.send("⚫ HQ main switch turned **OFF** ✅", ephemeral=True)
    else:
        await interaction.followup.send(f"❌ Failed to turn OFF HQ switch: {msg}", ephemeral=True)


@tree.command(description="Check HQ main switch status.")
async def hq_status(interaction: discord.Interaction):
    if not await ensure_rust_permission(interaction):
        return

    audit_log(
        "rust_control",
        interaction.user,
        {"command": "hq_status"},
    )

    await interaction.response.defer(ephemeral=True)
    ok, msg = await call_entity_action("switch_hq", "status")
    if ok:
        await interaction.followup.send(f"ℹ️ HQ switch status:\n`{msg}`", ephemeral=True)
    else:
        await interaction.followup.send(f"❌ Failed to get HQ switch status: {msg}", ephemeral=True)


async def run_tc_status(interaction: discord.Interaction, tc_name: str = "tc_main"):
    """Shared logic for responding with TC upkeep and resource status."""
    # Defer since we have to call over HTTP
    await interaction.response.defer(ephemeral=True)

    data = await fetch_tc_summary(tc_name)

    if not data.get("ok"):
        err = data.get("error", "Unknown error")
        await interaction.followup.send(
            f"❌ Failed to fetch TC status for `{tc_name}`\n```{err}```",
            ephemeral=True,
        )
        return

    resources = data.get("resources", {})
    upkeep = data.get("upkeep", {})

    wood = resources.get("wood", 0)
    stone = resources.get("stone", 0)
    frags = resources.get("metal_fragments", 0)
    hqm = resources.get("hqm", 0)

    has_prot = upkeep.get("hasProtection", False)
    hours = upkeep.get("hours_remaining", None)

    # Build a nice description
    lines = []
    lines.append(f"**TC Name:** `{tc_name}`")
    lines.append("")
    lines.append("**Resources**")
    lines.append(f"🪵 Wood: **{wood:,}**")
    lines.append(f"🪨 Stone: **{stone:,}**")
    lines.append(f"🔩 Metal Frags: **{frags:,}**")
    lines.append(f"💎 HQM: **{hqm:,}**")
    lines.append("")

    if has_prot:
        if hours is not None:
            lines.append(f"🛡 Upkeep: **{hours:.2f} hours** remaining")
        else:
            lines.append("🛡 Upkeep: **Protected** (time unknown)")
    else:
        lines.append("⚠️ Upkeep: **No protection active**")

    desc = "\n".join(lines)

    embed = discord.Embed(
        title="🏛 TC Status",
        description=desc,
        color=discord.Color.gold(),
        timestamp=datetime.utcnow(),
    )
    embed.set_author(name="Project Sisyphean")
    embed.set_footer(text="Stay alert, stay alive.")

    await interaction.followup.send(embed=embed, ephemeral=True)


@tree.command(description="Check TC upkeep and core resources.")
@app_commands.describe(
    tc_name="TC entity name from rust_config.json (e.g., tc_main, tcm_ext_n)"
)
async def tc_status(interaction: discord.Interaction, tc_name: str = "tc_main"):
    """Slash command wrapper that calls the shared TC status handler."""
    await run_tc_status(interaction, tc_name)


@tree.command(description="Open the HQ command console for leadership.")
@app_commands.describe(
    base_name="Base name used in alerts and status (default: Main)."
)
async def hq(interaction: discord.Interaction, base_name: str = "Main"):
    """Leadership-only HQ control panel."""
    # Must be in a guild
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message(
            "This command can only be used inside a Discord server.",
            ephemeral=True,
        )
        return

    # Permissions – reuse same roles as connect management
    allowed_role_ids = [
        get_role_id("leadership"),
        get_role_id("recruiter"),
        get_role_id("event_coord"),
    ]

    try:
        has_perm = user_has_any_role(interaction.user, allowed_role_ids)
    except NameError:
        # If helper is missing for some reason, fall back to leadership only
        allowed_role_ids = [get_role_id("leadership")]
        has_perm = any(
            (role.id in allowed_role_ids and role.id != 0)
            for role in interaction.user.roles
        )

    if not has_perm:
        await interaction.response.send_message(
            "❌ You don't have permission to open the HQ console.",
            ephemeral=True,
        )
        return

    # Build a nice summary embed for context
    desc_lines = [
        f"**Base:** {base_name}",
        "",
        "🧭 **Sections:**",
        "• 🚨 Alerts – open the raid/status alert panel",
        "• 🌐 Connect – server connect menu (/connect)",
        "• 🛰 SAM – control Main SAM via smart switch / Rust+",
        "• 🔌 HQ – toggle HQ power",
        "• 🏛 TC – check main TC upkeep / resources",
    ]
    embed = discord.Embed(
        title="🛡 HQ Command Console",
        description="\n".join(desc_lines),
        color=discord.Color.dark_teal(),
    )
    embed.set_footer(text="Project Sisyphean — Stay alert, stay alive.")

    view = HQView(base_name=base_name)
    await interaction.response.send_message(
        embed=embed,
        view=view,
        ephemeral=True,
    )


@tree.command(description="Set a member's duty status (Active Duty / Reservist / Inactive Reservist).")
@app_commands.describe(
    member="Member to modify",
    status="Select a duty status",
)
@app_commands.choices(
    status=[
        app_commands.Choice(name="Active Duty", value="active_duty"),
        app_commands.Choice(name="Reservist", value="reservist"),
        app_commands.Choice(name="Inactive Reservist", value="inactive_reservist"),
    ]
)
async def status_set(
    interaction: discord.Interaction,
    member: discord.Member,
    status: app_commands.Choice[str],
):
    """Leadership-only: assign a member's duty status."""

    allowed_role_ids = [get_role_id("leadership")]
    if not user_has_any_role(interaction.user, allowed_role_ids):
        await interaction.response.send_message(
            "❌ You don't have permission to change duty statuses.",
            ephemeral=True,
        )
        return

    if not interaction.guild:
        await interaction.response.send_message(
            "This command can only be used inside a Discord server.",
            ephemeral=True,
        )
        return

    status_key = status.value  # "active_duty" / "reservist" / "inactive_reservist"

    try:
        pretty = await apply_duty_status(
            interaction.guild,
            member,
            status_key,
            actor=interaction.user,
            source="status_set",
        )
    except ValueError as e:
        await interaction.response.send_message(
            f"❌ {e}",
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        f"✅ **{member.display_name}** is now set to **{pretty}**.",
        ephemeral=True,
    )


@tree.command(description="Show a member's current duty status.")
async def status_info(interaction: discord.Interaction, member: discord.Member):
    """Check what duty status a user currently has."""
    if not interaction.guild:
        await interaction.response.send_message(
            "This command can only be used inside a Discord server.",
            ephemeral=True,
        )
        return

    status_roles = get_duty_status_role_ids()
    label_map = {
        "active_duty": "Active Duty",
        "reservist": "Reservist",
        "inactive_reservist": "Inactive Reservist",
    }
    user_status = "None Assigned"

    for key, rid in status_roles.items():
        if not rid:
            continue
        role = interaction.guild.get_role(rid)
        if role and role in member.roles:
            user_status = label_map.get(key, key.replace("_", " ").title())
            break

    embed = discord.Embed(
        title=f"Duty Status — {member.display_name}",
        description=f"**{user_status}**",
        color=discord.Color.blue(),
    )

    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(description="Audit and enforce duty statuses for all members.")
async def status_audit(interaction: discord.Interaction):
    """
    Leadership-only: enforce duty_status.json as the source of truth.

    Rules:
    - If a recorded status exists: member is forced to that status.
    - If no recorded status and member has NO duty roles: leave unchanged.
    - If no recorded status and member HAS any duty roles: reset to default (inactive_reservist).
    Also produces a per-member change report.
    """
    if not interaction.guild:
        await interaction.response.send_message(
            "This command can only be used in a server.",
            ephemeral=True,
        )
        return

    allowed_role_ids = [get_role_id("leadership")]
    if not user_has_any_role(interaction.user, allowed_role_ids):
        await interaction.response.send_message(
            "❌ You don't have permission to audit duty statuses.",
            ephemeral=True,
        )
        return

    guild = interaction.guild
    status_roles = get_duty_status_role_ids()
    roleid_to_key = {rid: key for key, rid in status_roles.items() if rid}

    reset_to_recorded = 0
    reset_to_default = 0
    untouched = 0

    enforced_entries: list[str] = []
    default_entries: list[str] = []

    global DUTY_STATUS_STATE

    for member in guild.members:
        if member.bot:
            continue

        user_id_str = str(member.id)
        recorded = DUTY_STATUS_STATE.get(user_id_str)

        actual_keys = []
        for role in member.roles:
            key = roleid_to_key.get(role.id)
            if key:
                actual_keys.append(key)

        actual_set = set(actual_keys)

        if actual_set:
            actual_label = ", ".join(sorted(pretty_status_label(k) for k in actual_set))
        else:
            actual_label = "None"

        if recorded in DUTY_STATUS_KEYS:
            if actual_set == {recorded}:
                untouched += 1
                continue

            try:
                old_label = actual_label
                new_label = pretty_status_label(recorded)
                await apply_duty_status(
                    guild,
                    member,
                    recorded,
                    actor=interaction.user,
                    source="status_audit",
                )
                reset_to_recorded += 1
                enforced_entries.append(
                    f"- {member.mention} — **{old_label}** → **{new_label}**"
                )
            except Exception as e:
                logging.exception(
                    "Failed to enforce recorded duty status for %s: %s",
                    member.id,
                    e,
                )
            continue

        if not actual_set:
            untouched += 1
            continue

        default_status = "inactive_reservist"
        try:
            old_label = actual_label
            new_label = pretty_status_label(default_status)
            await apply_duty_status(
                guild,
                member,
                default_status,
                actor=interaction.user,
                source="status_audit_default",
            )
            reset_to_default += 1
            default_entries.append(
                f"- {member.mention} — **{old_label}** → **{new_label}** (no record)"
            )
        except Exception as e:
            logging.exception(
                "Failed to reset unrecorded member %s to default duty status: %s",
                member.id,
                e,
            )

    desc_lines = [
        "Duty Status Audit Complete:",
        f"• ✅ Enforced recorded status: **{reset_to_recorded}** member(s)",
        f"• ✅ Reset illegal manual duty roles to default (Inactive Reservist): **{reset_to_default}** member(s)",
        f"• ➖ Left unchanged: **{untouched}** member(s)",
    ]

    embed = discord.Embed(
        title="Duty Status Audit",
        description="\n".join(desc_lines),
        color=discord.Color.orange(),
    )

    detail_lines: list[str] = []
    if enforced_entries:
        detail_lines.append("**Enforced recorded status:**")
        detail_lines.extend(enforced_entries)
        detail_lines.append("")
    if default_entries:
        detail_lines.append("**Reset to default (no record):**")
        detail_lines.extend(default_entries)

    detail_text = "\n".join(detail_lines) if detail_lines else "No members were changed."

    max_ephemeral_chars = 1800
    short_detail = (
        detail_text
        if len(detail_text) <= max_ephemeral_chars
        else "\n".join(detail_text.splitlines()[:25]) + "\n… (see log channel for full list)"
    )

    embed.add_field(name="Changed Members", value=short_detail, inline=False)

    await interaction.response.send_message(embed=embed, ephemeral=True)

    if DUTY_STATUS_LOG_CHANNEL and (enforced_entries or default_entries):
        log_channel = guild.get_channel(DUTY_STATUS_LOG_CHANNEL)
        if log_channel:
            log_embed = discord.Embed(
                title="Duty Status Audit Report",
                description="\n".join(desc_lines),
                color=discord.Color.orange(),
            )
            log_embed.add_field(
                name="Changed Members",
                value=detail_text,
                inline=False,
            )
            log_embed.set_footer(text=f"Triggered by {interaction.user.display_name}")
            try:
                await log_channel.send(embed=log_embed)
            except Exception as e:
                logging.exception("Failed to send duty status audit log: %s", e)


# ---------- INTERACTIVE MENU ----------

class AlertMenuView(discord.ui.View):
    def __init__(self, base_name: str | None = None, timeout: float | None = 60.0):
        super().__init__(timeout=timeout)
        self.base_name = base_name or "Main"

    async def interaction_checks(self, interaction: discord.Interaction) -> bool:
        # Optional: restrict usage (e.g., only Rust Team role). For now, allow all.
        return True

    @discord.ui.button(label="Raid Alert", style=discord.ButtonStyle.danger, emoji="🚨")
    async def raid_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        if not interaction.guild:
            await interaction.response.send_message(
                "This can only be used in a server.", ephemeral=True
            )
            return

        desc = "Get online and defend! 🎲🔫"
        desc = add_f1_to_description(desc)

        embed = make_embed(
            title="🚨 RAID ALERT!",
            description=desc,
            color=discord.Color.dark_red(),
            base_name=self.base_name,
            status_emoji="⚔️ Under Attack",
        )
        await send_alert(interaction.guild, embed, ping_team=True)
        await interaction.response.send_message(
            f"Raid alert sent for **{self.base_name}** ✅",
            ephemeral=True,
        )

    @discord.ui.button(label="Base Online", style=discord.ButtonStyle.success, emoji="🟢")
    async def online_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        if not interaction.guild:
            await interaction.response.send_message(
                "This can only be used in a server.", ephemeral=True
            )
            return
        embed = make_embed(
            title="🟢 ONLINE STATUS",
            description="Team is now ONLINE and ready. ✅",
            color=discord.Color.dark_green(),
            base_name=self.base_name,
            status_emoji="🟢 ONLINE",
        )
        # 🔕 no ping on status
        await send_alert(interaction.guild, embed, ping_team=False)
        await interaction.response.send_message(
            f"Online alert sent for **{self.base_name}** ✅",
            ephemeral=True,
        )

    @discord.ui.button(label="Base Offline", style=discord.ButtonStyle.secondary, emoji="⚫")
    async def offline_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        if not interaction.guild:
            await interaction.response.send_message(
                "This can only be used in a server.", ephemeral=True
            )
            return
        embed = make_embed(
            title="⚫ OFFLINE STATUS",
            description="Team is now OFFLINE. 😴💤",
            color=discord.Color.dark_grey(),
            base_name=self.base_name,
            status_emoji="⚫ OFFLINE",
        )
        # 🔕 no ping on status
        await send_alert(interaction.guild, embed, ping_team=False)
        await interaction.response.send_message(
            f"Offline alert sent for **{self.base_name}** ✅",
            ephemeral=True,
        )


@tree.command(description="Open a control panel to send raid / status alerts.")
@app_commands.describe(
    base_name="Base name to use in the alert messages (default: Main)."
)
async def menu(interaction: discord.Interaction, base_name: str = "Main"):
    view = AlertMenuView(base_name=base_name)
    await interaction.response.send_message(
        f"Control panel for **{base_name}**. "
        "Buttons will send alerts to the configured raid channel.",
        view=view,
        ephemeral=True,
    )


# ---------- HQ COMMAND CONSOLE ----------

class HQView(discord.ui.View):
    def __init__(self, base_name: str | None = None, timeout: float | None = 180.0):
        super().__init__(timeout=timeout)
        self.base_name = base_name or "Main"

    # ---- ROW 1: PANELS ----

    @discord.ui.button(label="Alert Panel", style=discord.ButtonStyle.danger, emoji="🚨")
    async def open_alert_panel(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        """Open the existing AlertMenuView for this base."""
        if not interaction.guild:
            await interaction.response.send_message(
                "This only works inside a server.", ephemeral=True
            )
            return

        view = AlertMenuView(base_name=self.base_name)
        await interaction.response.send_message(
            f"Alert panel for **{self.base_name}**.",
            view=view,
            ephemeral=True,
        )

    @discord.ui.button(label="Connect Menu", style=discord.ButtonStyle.primary, emoji="🌐")
    async def open_connect_menu(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        """Open the /connect dropdown menu as a view."""
        # If connect system isn't configured for some reason, fail gracefully.
        try:
            # Needs CONNECT_PROFILES & ConnectMenuView from your existing connect system
            if not CONNECT_PROFILES:
                await interaction.response.send_message(
                    "No connect profiles configured yet. Use `/connect_add` or `/connect_reload`.",
                    ephemeral=True,
                )
                return

            view = ConnectMenuView(CONNECT_PROFILES)
        except NameError:
            await interaction.response.send_message(
                "Connect menu is not configured in this bot build.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            "Select a server to get its F1 connect command:",
            view=view,
            ephemeral=True,
        )

    # ---- ROW 2: SAM CONTROLS ----

    @discord.ui.button(label="MAIN SAM ON", style=discord.ButtonStyle.success, emoji="🛰")
    async def sam_on_btn(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        if not await ensure_rust_permission(interaction):
            return

        audit_log(
            "rust_control",
            interaction.user,
            {"command": "sam_on_button", "base": self.base_name},
        )

        await handle_entity_action(interaction, "sam_main", "on")

    @discord.ui.button(label="MAIN SAM OFF", style=discord.ButtonStyle.secondary, emoji="🛰")
    async def sam_off_btn(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        if not await ensure_rust_permission(interaction):
            return

        audit_log(
            "rust_control",
            interaction.user,
            {"command": "sam_off_button", "base": self.base_name},
        )
        await handle_entity_action(interaction, "sam_main", "off")

    @discord.ui.button(label="SAM Status", style=discord.ButtonStyle.secondary, emoji="ℹ️")
    async def sam_status_btn(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        if not await ensure_rust_permission(interaction):
            return

        audit_log(
            "rust_control",
            interaction.user,
            {"command": "sam_status_button", "base": self.base_name},
        )
        await handle_entity_status(interaction, "sam_main")

    # ---- ROW 3: HQ POWER + TC STATUS ----

    @discord.ui.button(label="HQ ON", style=discord.ButtonStyle.success, emoji="🔌")
    async def hq_on_btn(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        if not await ensure_rust_permission(interaction):
            return

        audit_log(
            "rust_control",
            interaction.user,
            {"command": "hq_on_button", "base": self.base_name},
        )
        await handle_entity_action(interaction, "switch_hq", "on")

    @discord.ui.button(label="HQ OFF", style=discord.ButtonStyle.secondary, emoji="🔌")
    async def hq_off_btn(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        if not await ensure_rust_permission(interaction):
            return

        audit_log(
            "rust_control",
            interaction.user,
            {"command": "hq_off_button", "base": self.base_name},
        )
        await handle_entity_action(interaction, "switch_hq", "off")

    @discord.ui.button(label="HQ Status", style=discord.ButtonStyle.secondary, emoji="ℹ️")
    async def hq_status_btn(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        if not await ensure_rust_permission(interaction):
            return

        audit_log(
            "rust_control",
            interaction.user,
            {"command": "hq_status_button", "base": self.base_name},
        )
        # helper signature is handle_entity_status(interaction, entity_key)
        await handle_entity_status(interaction, "switch_hq")

    @discord.ui.button(label="TC Status", style=discord.ButtonStyle.primary, emoji="🏛")
    async def tc_status_btn(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        await run_tc_status(interaction, "tc_main")

    # ---- ROW 4: DUTY STATUS (SELF) ----

    @discord.ui.button(label="Set Active Duty", style=discord.ButtonStyle.success, emoji="🟢")
    async def set_active_duty_btn(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "This button can only be used inside a server.",
                ephemeral=True,
            )
            return

        try:
            pretty = await apply_duty_status(
                interaction.guild,
                interaction.user,
                "active_duty",
                actor=interaction.user,
                source="hq_button",
            )
        except ValueError as e:
            await interaction.response.send_message(
                f"❌ {e}",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            f"✅ Your duty status is now **{pretty}**.",
            ephemeral=True,
        )

    @discord.ui.button(label="Set Reservist", style=discord.ButtonStyle.secondary, emoji="🟡")
    async def set_reservist_btn(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "This button can only be used inside a server.",
                ephemeral=True,
            )
            return

        try:
            pretty = await apply_duty_status(
                interaction.guild,
                interaction.user,
                "reservist",
                actor=interaction.user,
                source="hq_button",
            )
        except ValueError as e:
            await interaction.response.send_message(
                f"❌ {e}",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            f"✅ Your duty status is now **{pretty}**.",
            ephemeral=True,
        )

    @discord.ui.button(label="Set Inactive Reservist", style=discord.ButtonStyle.danger, emoji="🔴")
    async def set_inactive_reservist_btn(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "This button can only be used inside a server.",
                ephemeral=True,
            )
            return

        try:
            pretty = await apply_duty_status(
                interaction.guild,
                interaction.user,
                "inactive_reservist",
                actor=interaction.user,
                source="hq_button",
            )
        except ValueError as e:
            await interaction.response.send_message(
                f"❌ {e}",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            f"✅ Your duty status is now **{pretty}**.",
            ephemeral=True,
        )


@bot.event
async def on_ready():
    for g in bot.guilds:
        try:
            await g.chunk()
        except Exception:
            logging.exception("Failed to chunk guild %s", g.id)

    if not duty_enforce_periodic.is_running():
        duty_enforce_periodic.start()


# ---------- ENTRY POINT ----------

if __name__ == "__main__":
    logging.info("Starting Project Sisyphean bot...")
    bot.run(DISCORD_TOKEN)
