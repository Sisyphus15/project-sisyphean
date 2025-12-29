from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from dateutil import parser as dtparser
from zoneinfo import ZoneInfo

DB_PATH = "sisyphus.db"
DEFAULT_TZ = "America/New_York"

ALIASES = {
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

RANGE_RE = re.compile(
    r"^\s*(?P<start>.+?)\s*-\s*(?P<end>.+?)(?:\s+(?P<tz>[A-Za-z/_]+))?\s*$"
)


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_timezones (
            user_id INTEGER PRIMARY KEY,
            tz TEXT NOT NULL
        )
        """
    )
    return conn


def normalize_tz(token: str) -> str:
    t = token.strip().lower()
    return ALIASES.get(t, token.strip())


def set_user_tz(user_id: int, tz: str) -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO user_timezones (user_id, tz) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET tz=excluded.tz",
            (user_id, tz),
        )


def get_user_tz(user_id: int) -> Optional[str]:
    with db() as conn:
        row = conn.execute("SELECT tz FROM user_timezones WHERE user_id=?", (user_id,)).fetchone()
    return row[0] if row else None


def parse_duration(text: str) -> Optional[timedelta]:
    s = text.strip().lower()
    s = s.removeprefix("in ").strip()
    m = re.fullmatch(r"(?:(\d+)\s*h)?\s*(?:(\d+)\s*m)?", s)
    if not m:
        return None
    h = int(m.group(1) or 0)
    mins = int(m.group(2) or 0)
    if h == 0 and mins == 0:
        return None
    return timedelta(hours=h, minutes=mins)


@dataclass
class ParsedTime:
    start_utc: datetime
    end_utc: Optional[datetime]
    source_tz: str


def stamp(dt_utc: datetime, style: str) -> str:
    return f"<t:{int(dt_utc.timestamp())}:{style}>"


def parse_when(when: str, user_tz: str) -> ParsedTime:
    raw = when.strip()

    dur = parse_duration(raw)
    if dur:
        now = datetime.now(timezone.utc)
        return ParsedTime(now + dur, None, "UTC")

    m = RANGE_RE.match(raw)
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

        return ParsedTime(start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc), src_tz)

    # single time with optional tz suffix: "8pm uk"
    parts = raw.split()
    src_tz = user_tz
    if len(parts) >= 2 and ("/" in parts[-1] or parts[-1].lower() in ALIASES):
        src_tz = normalize_tz(parts[-1])
        raw = " ".join(parts[:-1]).strip()

    z = ZoneInfo(src_tz)
    now_local = datetime.now(z).replace(second=0, microsecond=0)
    dt_local = dtparser.parse(raw, default=now_local)

    # If they only gave a time and it's already passed today, assume tomorrow
    if dt_local <= now_local and re.search(r"\d", raw) and not re.search(
        r"\b(yesterday|today|tomorrow|next)\b", raw.lower()
    ):
        dt_local += timedelta(days=1)

    return ParsedTime(dt_local.astimezone(timezone.utc), None, src_tz)


class TimePingCog(commands.Cog):
    tz_group = app_commands.Group(name="tz", description="Set or view your timezone")

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @tz_group.command(name="set", description="Set your timezone (e.g., America/New_York, Europe/London, pst, uk)")
    async def tz_set(self, interaction: discord.Interaction, timezone_str: str):
        tz = normalize_tz(timezone_str)
        try:
            ZoneInfo(tz)
        except Exception:
            await interaction.response.send_message(
                f"❌ Unknown timezone: `{timezone_str}`. Try `America/New_York` or `Europe/London`.",
                ephemeral=True,
            )
            return

        set_user_tz(interaction.user.id, tz)
        await interaction.response.send_message(f"✅ Timezone set to `{tz}`", ephemeral=True)

    @tz_group.command(name="me", description="Show your saved timezone")
    async def tz_me(self, interaction: discord.Interaction):
        tz = get_user_tz(interaction.user.id) or DEFAULT_TZ
        now_local = datetime.now(ZoneInfo(tz)).strftime("%Y-%m-%d %I:%M %p")
        await interaction.response.send_message(
            f"🧭 Your timezone: `{tz}` • Local time: **{now_local}**",
            ephemeral=True,
        )

    @app_commands.command(name="time", description="Make a Time Ping. Examples: 8pm | tomorrow 7pm | 10-12 gmt | in 90m")
    async def time_ping(self, interaction: discord.Interaction, when: str):
        user_tz = get_user_tz(interaction.user.id) or DEFAULT_TZ
        try:
            parsed = parse_when(when, user_tz)
        except Exception:
            await interaction.response.send_message(
                "❌ Couldn't parse that. Try: `8pm`, `tomorrow 7pm`, `10-12 gmt`, `20:00 uk`, `in 90m`",
                ephemeral=True,
            )
            return

        if parsed.end_utc:
            msg = f"🕒 **Window:** {stamp(parsed.start_utc,'t')}–{stamp(parsed.end_utc,'t')} ({stamp(parsed.start_utc,'R')})"
        else:
            msg = f"🕒 **Time:** {stamp(parsed.start_utc,'t')} ({stamp(parsed.start_utc,'R')})"

        await interaction.response.send_message(msg)


async def setup(bot: commands.Bot):
    cog = TimePingCog(bot)
    await bot.add_cog(cog)
    bot.tree.add_command(cog.tz_group)
