import os
import logging
import asyncio
from datetime import datetime
import json
import urllib.request
import urllib.error
from functools import partial
import discord
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv
import aiohttp

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


# ---------- BOT EVENTS ----------

@bot.event
async def on_ready():
    logging.info(f"Logged in as {bot.user} (id={bot.user.id})")

    # Sync slash commands
    try:
        if RUST_GUILD_ID:
            guild_obj = discord.Object(id=RUST_GUILD_ID)
            tree.copy_global_to(guild=guild_obj)
            synced = await tree.sync(guild=guild_obj)
            logging.info(f"Synced {len(synced)} commands to guild {RUST_GUILD_ID}")
        else:
            synced = await tree.sync()
            logging.info(f"Synced {len(synced)} global commands")
    except Exception as e:
        logging.exception("Failed to sync app commands: %s", e)


# ---------- SLASH COMMANDS ----------

@tree.command(description="Check if the bot is online.")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message(
        f"Pong! 🏓 Latency: {round(bot.latency * 1000)} ms",
        ephemeral=True,
    )


@tree.command(description="Show the F1 console connect command for this wipe.")
async def connect(interaction: discord.Interaction):
    if not F1_CONNECT:
        await interaction.response.send_message(
            "No F1 connect string configured yet. Ask staff to update `rust_config.json`.",
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        "Copy & paste this into your Rust F1 console:\n"
        f"```{F1_CONNECT}```",
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


@tree.command(description="Check TC upkeep and core resources.")
@app_commands.describe(
    tc_name="TC entity name from rust_config.json (e.g., tc_main, tcm_ext_n)"
)
async def tc_status(interaction: discord.Interaction, tc_name: str = "tc_main"):
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


# ---------- ENTRY POINT ----------

if __name__ == "__main__":
    logging.info("Starting Project Sisyphean bot...")
    bot.run(DISCORD_TOKEN)

