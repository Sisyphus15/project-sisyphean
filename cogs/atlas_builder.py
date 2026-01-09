import asyncio
import io
import json
import logging
import os

import discord
from discord import app_commands
from discord.ext import commands
from PIL import Image

from atlas_db import (
    get_or_create_session,
    get_session_for_channel,
    get_waiting_slot,
    mark_slot_ready,
    set_waiting_slot,
)
from atlas_runner import run_atlas_build
from permissions import has_permission

SLOT_LABELS = {
    "nodes": "Nodes",
    "boars": "Boars",
    "horses": "Horses",
    "berries": "Berries",
    "hemp": "Hemp",
    "bears": "Bears",
    "nobuild": "No Build",
}


def _load_panel_size(atlas_dir: str) -> int:
    cfg_path = os.path.join(atlas_dir, "config.json")
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        return int(cfg.get("panel_size", 256) or 256)
    except Exception:
        return 256


def _center_crop_square(img: Image.Image) -> Image.Image:
    w, h = img.size
    side = min(w, h)
    left = int((w - side) / 2)
    top = int((h - side) / 2)
    return img.crop((left, top, left + side, top + side))


async def handle_atlas_attachment(message: discord.Message, db_path: str, atlas_dir: str) -> bool:
    """
    Returns True if Atlas consumed the message, else False.
    """
    if not message.attachments:
        return False
    attachment = message.attachments[0]
    if not attachment.content_type or not attachment.content_type.startswith("image/"):
        return False
    if not message.guild or not isinstance(message.author, discord.Member):
        return False

    session_id = get_session_for_channel(db_path, message.guild.id, message.channel.id, message.author.id)
    if not session_id:
        return False

    slot_key = get_waiting_slot(db_path, session_id)
    if not slot_key:
        return False

    try:
        data = await attachment.read()
    except Exception:
        logging.exception("Atlas attachment download failed.")
        return False

    panel_size = _load_panel_size(atlas_dir)
    input_dir = os.path.join(atlas_dir, "input")
    os.makedirs(input_dir, exist_ok=True)
    save_path = os.path.join(input_dir, f"{slot_key}.png")

    try:
        img = Image.open(io.BytesIO(data))
        img = img.convert("RGBA")
        img = _center_crop_square(img)
        img = img.resize((panel_size, panel_size), Image.LANCZOS)
        img.save(save_path, format="PNG")
    except Exception:
        logging.exception("Atlas image processing failed.")
        await message.channel.send(f"❌ Failed to process {SLOT_LABELS.get(slot_key, slot_key)}.")
        return True

    mark_slot_ready(db_path, session_id, slot_key, save_path, attachment.url)
    set_waiting_slot(db_path, session_id, None)
    await message.channel.send(f"Saved {SLOT_LABELS.get(slot_key, slot_key).upper()} ✅")
    return True


class AtlasBuildView(discord.ui.View):
    def __init__(self, db_path: str, atlas_dir: str, session_id: str, owner_id: int):
        super().__init__(timeout=15 * 60)
        self.db_path = db_path
        self.atlas_dir = atlas_dir
        self.session_id = session_id
        self.owner_id = owner_id

    async def _ensure_owner(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("This Atlas builder session belongs to someone else.", ephemeral=True)
            return False
        return True

    async def _set_slot(self, interaction: discord.Interaction, slot_key: str) -> None:
        if not await self._ensure_owner(interaction):
            return
        set_waiting_slot(self.db_path, self.session_id, slot_key)
        label = SLOT_LABELS.get(slot_key, slot_key).lower()
        await interaction.response.send_message(f"Upload the screenshot for {label} now.", ephemeral=True)

    async def _run_build(self, interaction: discord.Interaction) -> None:
        if not await self._ensure_owner(interaction):
            return

        await interaction.response.send_message("Building dashboard...", ephemeral=True)
        set_waiting_slot(self.db_path, self.session_id, None)

        returncode, stdout, stderr, output_png_path = await asyncio.to_thread(
            run_atlas_build, self.atlas_dir
        )

        if returncode == 0 and os.path.exists(output_png_path):
            await interaction.channel.send(file=discord.File(output_png_path))
            return

        snippet = (stderr or stdout or "Unknown error").strip()
        if len(snippet) > 1000:
            snippet = snippet[-1000:]
        await interaction.channel.send(f"❌ Atlas build failed:\n```{snippet}```")

    @discord.ui.button(label="Nodes", style=discord.ButtonStyle.primary)
    async def nodes_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._set_slot(interaction, "nodes")

    @discord.ui.button(label="Boars", style=discord.ButtonStyle.primary)
    async def boars_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._set_slot(interaction, "boars")

    @discord.ui.button(label="Horses", style=discord.ButtonStyle.primary)
    async def horses_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._set_slot(interaction, "horses")

    @discord.ui.button(label="Berries", style=discord.ButtonStyle.primary)
    async def berries_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._set_slot(interaction, "berries")

    @discord.ui.button(label="Hemp", style=discord.ButtonStyle.primary)
    async def hemp_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._set_slot(interaction, "hemp")

    @discord.ui.button(label="Bears", style=discord.ButtonStyle.primary)
    async def bears_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._set_slot(interaction, "bears")

    @discord.ui.button(label="No Build", style=discord.ButtonStyle.secondary)
    async def nobuild_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._set_slot(interaction, "nobuild")

    @discord.ui.button(label="Build", style=discord.ButtonStyle.success)
    async def build_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._run_build(interaction)


class AtlasBuilder(commands.Cog):
    def __init__(self, bot: commands.Bot, db_path: str, atlas_dir: str):
        self.bot = bot
        self.db_path = db_path
        self.atlas_dir = atlas_dir

    @app_commands.command(name="atlas_build_dashboard", description="Build the Atlas dashboard from map screenshots.")
    async def atlas_build_dashboard(self, interaction: discord.Interaction):
        if not isinstance(interaction.user, discord.Member) or not has_permission(interaction, "staff_manage"):
            await interaction.response.send_message("❌ You don’t have permission to use this.", ephemeral=True)
            return
        if not interaction.guild or not interaction.channel:
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return

        session_id = get_or_create_session(
            self.db_path,
            interaction.guild.id,
            interaction.channel.id,
            interaction.user.id,
        )

        view = AtlasBuildView(self.db_path, self.atlas_dir, session_id, interaction.user.id)
        await interaction.response.send_message("Atlas dashboard builder ready.", ephemeral=True, view=view)


async def setup(bot: commands.Bot):
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    db_path = getattr(bot, "atlas_db_path", os.path.join(base_dir, "sisyphus.db"))
    atlas_dir = getattr(
        bot,
        "atlas_dir",
        os.path.join(base_dir, "atlas_grid", "rust-heatmap-dashboard"),
    )
    await bot.add_cog(AtlasBuilder(bot, db_path, atlas_dir))
