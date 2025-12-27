import os
import json
import discord

def _chan_id() -> int:
    raw = os.getenv("AUDIT_LOG_CHANNEL_ID", "")
    return int(raw) if raw.isdigit() else 0

async def post_audit_to_channel(bot: discord.Client, entry: dict):
    channel_id = _chan_id()
    if not channel_id:
        return

    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except Exception:
            return

    # Keep it readable + safe in Discord
    compact = {
        "ts": entry.get("timestamp"),
        "event": entry.get("event"),
        "user": entry.get("username"),
        "uid": entry.get("user_id"),
        "critical": entry.get("critical"),
        "details": entry.get("details"),
        "chain_hash": entry.get("chain_hash"),
        "prev_chain_hash": entry.get("prev_chain_hash"),
    }

    content = "```json\n" + json.dumps(compact, indent=2)[:1800] + "\n```"
    await channel.send(content)
