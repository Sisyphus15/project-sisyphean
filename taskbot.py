import os
import logging
import discord
from discord import app_commands
from dotenv import load_dotenv
from datetime import datetime, timezone

from task_store import TaskStore, Task

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
GUILD_ID = int(os.getenv("GUILD_ID", "0"))
TASK_CHANNEL_ID = int(os.getenv("TASK_CHANNEL_ID", "0"))
TASK_ADMIN_ROLE_IDS = [int(x.strip()) for x in os.getenv("TASK_ADMIN_ROLE_IDS", "").split(",") if x.strip().isdigit()]

store = TaskStore()
os.makedirs("logs", exist_ok=True)
task_logger = logging.getLogger("tasks")
if not task_logger.handlers:
    handler = logging.FileHandler(os.path.join("logs", "tasks.log"), encoding="utf-8")
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    handler.setFormatter(formatter)
    task_logger.addHandler(handler)
task_logger.setLevel(logging.INFO)

def ts_fmt(unix_ts: int, style: str = "R") -> str:
    # style: R=relative, F=full, f=short date/time, D=date
    return f"<t:{unix_ts}:{style}>"

def utc_to_dt(unix_ts: int) -> datetime:
    return datetime.fromtimestamp(unix_ts, tz=timezone.utc)

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

class SisypheanClient(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.guilds = True
        intents.members = True  # needed for richer member lookups in embeds
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        # For persistent buttons after restart, register views here once you adopt custom_id based routing.
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

client = SisypheanClient()

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

# ---------- Commands ----------

@client.tree.command(name="task_create", description="Create a task and assign it to a role.")
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

@client.tree.command(name="task_list", description="List recent tasks (optionally filter).")
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

@client.tree.command(name="task_complete", description="Mark a task as DONE.")
async def task_complete(interaction: discord.Interaction, task_id: int):
    await set_status(interaction, task_id, "DONE")

@client.tree.command(name="task_hold", description="Put a task on HOLD.")
async def task_hold(interaction: discord.Interaction, task_id: int):
    await set_status(interaction, task_id, "HOLD")

@client.tree.command(name="task_reopen", description="Reopen a task (set to PENDING).")
async def task_reopen(interaction: discord.Interaction, task_id: int):
    await set_status(interaction, task_id, "PENDING")

@client.tree.command(name="task_progress", description="Set a task to IN_PROGRESS.")
async def task_progress(interaction: discord.Interaction, task_id: int):
    await set_status(interaction, task_id, "IN_PROGRESS")

@client.tree.command(name="task_assign", description="Reassign a task to a different role.")
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

if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise SystemExit("DISCORD_TOKEN missing in .env")
    if not TASK_CHANNEL_ID:
        raise SystemExit("TASK_CHANNEL_ID missing in .env")
    client.run(DISCORD_TOKEN)
