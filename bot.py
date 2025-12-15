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
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv
import aiohttp
import json


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

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree  # nicer alias


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
    def __init__(self, profiles: list[dict], timeout: float | None = 1800.0):
        super().__init__(timeout=timeout)
        self.message: discord.Message | None = None
        if profiles:
            self.add_item(ConnectSelect(profiles))

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except Exception:
                logging.exception("Failed to disable /connect view on timeout")

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


# ---------- BOT EVENTS ----------

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")

    try:
        synced = await bot.tree.sync()  # sync GLOBAL commands
        print(f"✅ Synced {len(synced)} global application commands.")
    except Exception as e:
        print("Error syncing global commands:", e)


# -------------------------
# CONNECT MENU CONFIG
# -------------------------

CONNECT_CONFIG_PATH = os.getenv("CONNECT_CONFIG_PATH") or os.path.join(BASE_DIR, "connect_servers.json")
ROLES_CONFIG_PATH = os.getenv("ROLES_CONFIG_PATH") or os.path.join(BASE_DIR, "roles_config.json")
DUTY_STATUS_STATE_PATH = os.getenv("DUTY_STATUS_STATE_PATH") or os.path.join(BASE_DIR, "duty_status.json")


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


# ---------- SLASH COMMANDS ----------

@tree.command(description="Check if the bot is online.")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message(
        f"Pong! 🏓 Latency: {round(bot.latency * 1000)} ms",
        ephemeral=True,
    )


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


# ---------- SAM & HQ SWITCH COMMANDS ----------

@tree.command(description="Turn MAIN SAM site ON (via smart switch).")
async def sam_on(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    ok, msg = await call_entity_action("sam_main", "on")
    if ok:
        await interaction.followup.send("🟢 MAIN SAM turned **ON** ✅", ephemeral=True)
    else:
        await interaction.followup.send(f"❌ Failed to turn ON MAIN SAM: {msg}", ephemeral=True)


@tree.command(description="Turn MAIN SAM site OFF (via smart switch).")
async def sam_off(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    ok, msg = await call_entity_action("sam_main", "off")
    if ok:
        await interaction.followup.send("⚫ MAIN SAM turned **OFF** ✅", ephemeral=True)
    else:
        await interaction.followup.send(f"❌ Failed to turn OFF MAIN SAM: {msg}", ephemeral=True)


@tree.command(description="Check MAIN SAM smart switch status.")
async def sam_status(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    ok, msg = await call_entity_action("sam_main", "status")
    if ok:
        await interaction.followup.send(f"ℹ️ MAIN SAM status:\n`{msg}`", ephemeral=True)
    else:
        await interaction.followup.send(f"❌ Failed to get MAIN SAM status: {msg}", ephemeral=True)


@tree.command(description="Turn MAIN SAM site ON via Rust+.")
async def sam_main_on(interaction: discord.Interaction):
    await handle_entity_action(interaction, "sam_main", "on")


@tree.command(description="Turn MAIN SAM site OFF via Rust+.")
async def sam_main_off(interaction: discord.Interaction):
    await handle_entity_action(interaction, "sam_main", "off")


@tree.command(description="Check MAIN SAM site status via Rust+.")
async def sam_main_status(interaction: discord.Interaction):
    await handle_entity_status(interaction, "sam_main")


@tree.command(description="Turn HQ main switch ON.")
async def hq_on(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    ok, msg = await call_entity_action("switch_hq", "on")
    if ok:
        await interaction.followup.send("🟢 HQ main switch turned **ON** ✅", ephemeral=True)
    else:
        await interaction.followup.send(f"❌ Failed to turn ON HQ switch: {msg}", ephemeral=True)


@tree.command(description="Turn HQ main switch OFF.")
async def hq_off(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    ok, msg = await call_entity_action("switch_hq", "off")
    if ok:
        await interaction.followup.send("⚫ HQ main switch turned **OFF** ✅", ephemeral=True)
    else:
        await interaction.followup.send(f"❌ Failed to turn OFF HQ switch: {msg}", ephemeral=True)


@tree.command(description="Check HQ main switch status.")
async def hq_status(interaction: discord.Interaction):
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
        # Uses your existing Rust+ helper
        await handle_entity_action(interaction, "sam_main", "on")

    @discord.ui.button(label="MAIN SAM OFF", style=discord.ButtonStyle.secondary, emoji="🛰")
    async def sam_off_btn(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        await handle_entity_action(interaction, "sam_main", "off")

    @discord.ui.button(label="SAM Status", style=discord.ButtonStyle.secondary, emoji="ℹ️")
    async def sam_status_btn(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        await handle_entity_status(interaction, "sam_main")

    # ---- ROW 3: HQ POWER + TC STATUS ----

    @discord.ui.button(label="HQ ON", style=discord.ButtonStyle.success, emoji="🔌")
    async def hq_on_btn(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        await handle_entity_action(interaction, "switch_hq", "on")

    @discord.ui.button(label="HQ OFF", style=discord.ButtonStyle.secondary, emoji="🔌")
    async def hq_off_btn(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        await handle_entity_action(interaction, "switch_hq", "off")

    @discord.ui.button(label="HQ Status", style=discord.ButtonStyle.secondary, emoji="ℹ️")
    async def hq_status_btn(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
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


# ---------- ENTRY POINT ----------

if __name__ == "__main__":
    logging.info("Starting Project Sisyphean bot...")
    bot.run(DISCORD_TOKEN)
