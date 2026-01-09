import os
import time
import uuid
import asyncio
import hashlib
import subprocess
from pathlib import Path

import discord
from discord import app_commands
import aiosqlite

SLOTS = ["nodes", "boars", "horses", "berries", "hemp", "bears", "nobuild"]
SLOT_LABELS = {
    "nodes": "NODES",
    "boars": "BOARS",
    "horses": "HORSES",
    "berries": "BERRIES",
    "hemp": "HEMP",
    "bears": "BEARS",
    "nobuild": "NO BUILD",
}


class AtlasDB:
    def __init__(self, db_path: str):
        self.db_path = db_path

    async def init(self):
        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript(
                """
            PRAGMA journal_mode=WAL;

            CREATE TABLE IF NOT EXISTS atlas_sessions (
              id TEXT PRIMARY KEY,
              guild_id TEXT NOT NULL,
              channel_id TEXT NOT NULL,
              user_id TEXT NOT NULL,
              status TEXT NOT NULL,
              created_at INTEGER NOT NULL,
              updated_at INTEGER NOT NULL,
              map_id TEXT,
              config_json TEXT,
              base_dir TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS atlas_slots (
              session_id TEXT NOT NULL,
              slot_key TEXT NOT NULL,
              status TEXT NOT NULL,
              file_path TEXT,
              updated_at INTEGER NOT NULL,
              PRIMARY KEY (session_id, slot_key)
            );

            CREATE TABLE IF NOT EXISTS atlas_uploads (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              session_id TEXT NOT NULL,
              slot_key TEXT NOT NULL,
              discord_attachment_url TEXT NOT NULL,
              saved_path TEXT NOT NULL,
              sha256 TEXT NOT NULL,
              created_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS atlas_builds (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              session_id TEXT NOT NULL,
              status TEXT NOT NULL,
              output_path TEXT,
              log TEXT,
              created_at INTEGER NOT NULL
            );
            """
            )
            await db.commit()

    async def create_session(self, guild_id: int, channel_id: int, user_id: int, base_dir: str) -> str:
        sid = str(uuid.uuid4())
        now = int(time.time())
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO atlas_sessions VALUES (?,?,?,?,?,?,?,?,?,?)",
                (sid, str(guild_id), str(channel_id), str(user_id), "active", now, now, None, None, base_dir),
            )
            for s in SLOTS:
                await db.execute(
                    "INSERT INTO atlas_slots VALUES (?,?,?,?,?)",
                    (sid, s, "empty", None, now),
                )
            await db.commit()
        return sid

    async def get_active_session(self, guild_id: int, channel_id: int, user_id: int) -> str | None:
        async with aiosqlite.connect(self.db_path) as db:
            row = await db.execute_fetchone(
                "SELECT id FROM atlas_sessions WHERE guild_id=? AND channel_id=? AND user_id=? AND status='active' "
                "ORDER BY created_at DESC LIMIT 1",
                (str(guild_id), str(channel_id), str(user_id)),
            )
            return row[0] if row else None

    async def set_awaiting(self, session_id: str, slot_key: str):
        now = int(time.time())
        async with aiosqlite.connect(self.db_path) as db:
            # clear any other awaiting slot (single pending slot per session)
            await db.execute(
                "UPDATE atlas_slots SET status='ready' WHERE session_id=? AND status='awaiting_upload'",
                (session_id,),
            )
            await db.execute(
                "UPDATE atlas_slots SET status='awaiting_upload', updated_at=? WHERE session_id=? AND slot_key=?",
                (now, session_id, slot_key),
            )
            await db.execute(
                "UPDATE atlas_sessions SET updated_at=? WHERE id=?",
                (now, session_id),
            )
            await db.commit()

    async def get_awaiting_slot(self, session_id: str) -> str | None:
        async with aiosqlite.connect(self.db_path) as db:
            row = await db.execute_fetchone(
                "SELECT slot_key FROM atlas_slots WHERE session_id=? AND status='awaiting_upload' LIMIT 1",
                (session_id,),
            )
            return row[0] if row else None

    async def set_slot_file(self, session_id: str, slot_key: str, file_path: str, attachment_url: str, sha256: str):
        now = int(time.time())
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE atlas_slots SET status='ready', file_path=?, updated_at=? WHERE session_id=? AND slot_key=?",
                (file_path, now, session_id, slot_key),
            )
            await db.execute(
                "INSERT INTO atlas_uploads(session_id,slot_key,discord_attachment_url,saved_path,sha256,created_at) "
                "VALUES (?,?,?,?,?,?)",
                (session_id, slot_key, attachment_url, file_path, sha256, now),
            )
            await db.execute(
                "UPDATE atlas_sessions SET updated_at=? WHERE id=?",
                (now, session_id),
            )
            await db.commit()

    async def slot_statuses(self, session_id: str) -> dict[str, str]:
        async with aiosqlite.connect(self.db_path) as db:
            rows = await db.execute_fetchall(
                "SELECT slot_key, status FROM atlas_slots WHERE session_id=?",
                (session_id,),
            )
            return {k: s for (k, s) in rows}

    async def get_base_dir(self, session_id: str) -> str:
        async with aiosqlite.connect(self.db_path) as db:
            row = await db.execute_fetchone("SELECT base_dir FROM atlas_sessions WHERE id=?", (session_id,))
            return row[0]

    async def add_build(self, session_id: str, status: str, output_path: str | None, log: str):
        now = int(time.time())
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO atlas_builds(session_id,status,output_path,log,created_at) VALUES (?,?,?,?,?)",
                (session_id, status, output_path, log, now),
            )
            await db.commit()


def sha256_bytes(b: bytes) -> str:
    h = hashlib.sha256()
    h.update(b)
    return h.hexdigest()


class AtlasBuilderView(discord.ui.View):
    def __init__(self, db: AtlasDB, session_id: str, owner_id: int):
        super().__init__(timeout=60 * 30)  # 30 min
        self.db = db
        self.session_id = session_id
        self.owner_id = owner_id

        for slot in SLOTS:
            self.add_item(AtlasSlotButton(slot))

        self.add_item(AtlasBuildButton())
        self.add_item(AtlasResetButton())

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.owner_id


class AtlasSlotButton(discord.ui.Button):
    def __init__(self, slot_key: str):
        super().__init__(label=SLOT_LABELS[slot_key], style=discord.ButtonStyle.primary, row=0)
        self.slot_key = slot_key

    async def callback(self, interaction: discord.Interaction):
        view: AtlasBuilderView = self.view  # type: ignore
        await view.db.set_awaiting(view.session_id, self.slot_key)
        await interaction.response.send_message(
            f"📥 Upload the screenshot for **{SLOT_LABELS[self.slot_key]}** now.",
            ephemeral=True,
        )


class AtlasBuildButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="BUILD", style=discord.ButtonStyle.success, row=1)

    async def callback(self, interaction: discord.Interaction):
        view: AtlasBuilderView = self.view  # type: ignore
        base_dir = await view.db.get_base_dir(view.session_id)

        # run pipeline
        dashboard_dir = Path(base_dir) / "rust-heatmap-dashboard"
        cmd = ["python3", "ingest_maps.py"]
        # On Windows dev, use "python" instead of python3
        if os.name == "nt":
            cmd = ["python", "ingest_maps.py"]

        p = subprocess.run(cmd, cwd=str(dashboard_dir), capture_output=True, text=True)
        log = (p.stdout or "") + "\n" + (p.stderr or "")
        out_path = dashboard_dir / "output" / "dashboard.png"

        if p.returncode == 0 and out_path.exists():
            await view.db.add_build(view.session_id, "ok", str(out_path), log)
            await interaction.response.send_message("✅ Built dashboard. Posting…", ephemeral=True)
            await interaction.channel.send(file=discord.File(str(out_path), filename="atlas_grid_dashboard.png"))
        else:
            await view.db.add_build(view.session_id, "failed", None, log[-3500:])
            await interaction.response.send_message(
                "❌ Build failed. Check logs.\n"
                f"```{log[-1500:]}```",
                ephemeral=True,
            )


class AtlasResetButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="RESET", style=discord.ButtonStyle.danger, row=1)

    async def callback(self, interaction: discord.Interaction):
        # Minimal reset: tell user to start a new session for now
        await interaction.response.send_message(
            "Reset is not wired yet (v1). Use /atlas_build_dashboard again to start fresh.",
            ephemeral=True,
        )


class AtlasBuilderCog(discord.ext.commands.Cog):
    def __init__(self, bot: discord.Client, db: AtlasDB, base_dir: str):
        self.bot = bot
        self.db = db
        self.base_dir = base_dir

    @app_commands.command(name="atlas_build_dashboard", description="Open Atlas Grid dashboard builder UI")
    async def atlas_build_dashboard(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        sid = await self.db.get_active_session(interaction.guild_id, interaction.channel_id, interaction.user.id)
        if not sid:
            sid = await self.db.create_session(interaction.guild_id, interaction.channel_id, interaction.user.id, self.base_dir)

        statuses = await self.db.slot_statuses(sid)
        status_line = " ".join(
            f"{SLOT_LABELS[k]} {'✅' if statuses.get(k)=='ready' else '❌'}" for k in SLOTS
        )

        view = AtlasBuilderView(self.db, sid, interaction.user.id)

        await interaction.followup.send(
            f"**Atlas Grid Dashboard Builder**\n{status_line}\n\nClick a tile, then upload the screenshot.",
            view=view,
            ephemeral=True,
        )


async def handle_atlas_attachment(db: AtlasDB, message: discord.Message, base_dir: str):
    """
    Call this from your global on_message handler.
    Saves the first image attachment to the currently awaiting slot.
    """
    if not message.guild or not message.attachments:
        return

    # only care about images
    att = next((a for a in message.attachments if (a.content_type or "").startswith("image/")), None)
    if not att:
        return

    # Find active session for this user/channel
    sid = await db.get_active_session(message.guild.id, message.channel.id, message.author.id)
    if not sid:
        return

    awaiting = await db.get_awaiting_slot(sid)
    if not awaiting:
        return

    data = await att.read()
    h = sha256_bytes(data)

    # Save into session dashboard input folder
    dashboard_dir = Path(base_dir) / "rust-heatmap-dashboard"
    input_dir = dashboard_dir / "input"
    input_dir.mkdir(parents=True, exist_ok=True)

    out_path = input_dir / f"{awaiting}.png"
    out_path.write_bytes(data)

    await db.set_slot_file(sid, awaiting, str(out_path), att.url, h)
    await message.reply(f"✅ Saved **{SLOT_LABELS[awaiting]}** → `{out_path.name}`")
