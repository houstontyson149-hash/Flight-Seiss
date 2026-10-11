"""Swiss Engineering - a role-restricted Discord music bot (discord.py 2.x)."""

import asyncio
import os
from collections import deque
from dataclasses import dataclass, field

import discord
import yt_dlp
from discord import app_commands
from discord.ext import commands

BOT_NAME = "Swiss Engineering"
ALLOWED_ROLE_IDS = {1532328027613036546}  # only these roles can use the bot
EMBED_COLOR = discord.Color.red()

YTDL_OPTS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "default_search": "ytsearch",
    "source_address": "0.0.0.0",
}
FFMPEG_OPTS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}


# --------------------------------------------------------------------------- #
# Role gate: every slash command passes through this single check
# --------------------------------------------------------------------------- #
class GatedTree(app_commands.CommandTree):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        user = interaction.user
        if isinstance(user, discord.Member) and any(
            r.id in ALLOWED_ROLE_IDS for r in user.roles
        ):
            return True
        raise app_commands.CheckFailure("missing_role")

    async def on_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.CheckFailure):
            msg = f"⛔ You don't have the required role to use **{BOT_NAME}**."
        else:
            msg = f"⚠️ Something went wrong: `{error}`"
            print(f"[error] {error!r}")
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)


# --------------------------------------------------------------------------- #
# Player state
# --------------------------------------------------------------------------- #
@dataclass
class Track:
    title: str
    stream_url: str
    page_url: str
    duration: int
    requester: str


@dataclass
class GuildState:
    queue: deque = field(default_factory=deque)
    current: Track | None = None
    volume: float = 0.5
    loop: bool = False


def fmt_duration(seconds: int) -> str:
    if not seconds:
        return "live"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def extract(query: str) -> dict | None:
    with yt_dlp.YoutubeDL(YTDL_OPTS) as ydl:
        info = ydl.extract_info(query, download=False)
    if info and "entries" in info:
        entries = [e for e in info["entries"] if e]
        info = entries[0] if entries else None
    return info


class SwissBot(commands.Bot):
    def __init__(self):
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=discord.Intents.default(),
            tree_cls=GatedTree,
            activity=discord.Activity(type=discord.ActivityType.listening, name="/play"),
        )
        self.states: dict[int, GuildState] = {}

    async def setup_hook(self):
        await self.tree.sync()

    def state(self, guild_id: int) -> GuildState:
        return self.states.setdefault(guild_id, GuildState())

    def play_next(self, guild: discord.Guild):
        st = self.state(guild.id)
        vc = guild.voice_client
        if vc is None:
            return
        if st.loop and st.current:
            st.queue.appendleft(st.current)
        if not st.queue:
            st.current = None
            return
        track = st.queue.popleft()
        st.current = track
        source = discord.PCMVolumeTransformer(
            discord.FFmpegPCMAudio(track.stream_url, **FFMPEG_OPTS), volume=st.volume
        )
        vc.play(
            source,
            after=lambda e: self.loop.call_soon_threadsafe(self.play_next, guild),
        )


bot = SwissBot()


def embed(title: str, description: str = "") -> discord.Embed:
    e = discord.Embed(title=title, description=description, color=EMBED_COLOR)
    e.set_footer(text=BOT_NAME)
    return e


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
@bot.event
async def on_ready():
    print(f"{BOT_NAME} online as {bot.user} (id: {bot.user.id})")


@bot.tree.command(name="play", description="Play a song from a search or URL")
@app_commands.describe(query="Song name or URL")
@app_commands.guild_only()
async def play(interaction: discord.Interaction, query: str):
    voice = interaction.user.voice
    if voice is None or voice.channel is None:
        return await interaction.response.send_message(
            "Join a voice channel first.", ephemeral=True
        )
    await interaction.response.defer()

    loop = asyncio.get_running_loop()
    try:
        info = await loop.run_in_executor(None, extract, query)
    except Exception as exc:
        return await interaction.followup.send(f"Couldn't load that: `{exc}`")
    if not info:
        return await interaction.followup.send("No results found.")

    track = Track(
        title=info.get("title", "Unknown"),
        stream_url=info["url"],
        page_url=info.get("webpage_url", ""),
        duration=info.get("duration") or 0,
        requester=interaction.user.display_name,
    )

    guild = interaction.guild
    vc = guild.voice_client
    if vc is None:
        vc = await voice.channel.connect()
    elif vc.channel != voice.channel:
        await vc.move_to(voice.channel)

    st = bot.state(guild.id)
    st.queue.append(track)

    if not vc.is_playing() and not vc.is_paused() and st.current is None:
        bot.play_next(guild)
        await interaction.followup.send(
            embed=embed("▶️ Now playing", f"[{track.title}]({track.page_url}) · {fmt_duration(track.duration)}")
        )
    else:
        await interaction.followup.send(
            embed=embed("➕ Added to queue", f"[{track.title}]({track.page_url}) · position {len(st.queue)}")
        )


@bot.tree.command(name="skip", description="Skip the current song")
@app_commands.guild_only()
async def skip(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    st = bot.state(interaction.guild.id)
    if not vc or not (vc.is_playing() or vc.is_paused()):
        return await interaction.response.send_message("Nothing is playing.", ephemeral=True)
    st.current = None  # prevents loop mode from re-queueing the skipped track
    vc.stop()
    await interaction.response.send_message("⏭️ Skipped.")


@bot.tree.command(name="pause", description="Pause playback")
@app_commands.guild_only()
async def pause(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_playing():
        vc.pause()
        return await interaction.response.send_message("⏸️ Paused.")
    await interaction.response.send_message("Nothing is playing.", ephemeral=True)


@bot.tree.command(name="resume", description="Resume playback")
@app_commands.guild_only()
async def resume(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_paused():
        vc.resume()
        return await interaction.response.send_message("▶️ Resumed.")
    await interaction.response.send_message("Nothing is paused.", ephemeral=True)


@bot.tree.command(name="stop", description="Stop playback and clear the queue")
@app_commands.guild_only()
async def stop(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    st = bot.state(interaction.guild.id)
    st.queue.clear()
    st.current = None
    st.loop = False
    if vc and (vc.is_playing() or vc.is_paused()):
        vc.stop()
    await interaction.response.send_message("⏹️ Stopped and cleared the queue.")


@bot.tree.command(name="queue", description="Show the queue")
@app_commands.guild_only()
async def queue(interaction: discord.Interaction):
    st = bot.state(interaction.guild.id)
    if not st.current and not st.queue:
        return await interaction.response.send_message("The queue is empty.", ephemeral=True)
    lines = []
    if st.current:
        lines.append(f"**Now:** {st.current.title} · {fmt_duration(st.current.duration)}")
    for i, t in enumerate(list(st.queue)[:10], 1):
        lines.append(f"`{i}.` {t.title} · {fmt_duration(t.duration)}")
    if len(st.queue) > 10:
        lines.append(f"…and {len(st.queue) - 10} more")
    await interaction.response.send_message(embed=embed("🎶 Queue", "\n".join(lines)))


@bot.tree.command(name="nowplaying", description="Show the current song")
@app_commands.guild_only()
async def nowplaying(interaction: discord.Interaction):
    t = bot.state(interaction.guild.id).current
    if not t:
        return await interaction.response.send_message("Nothing is playing.", ephemeral=True)
    await interaction.response.send_message(
        embed=embed(
            "🎵 Now playing",
            f"[{t.title}]({t.page_url})\n{fmt_duration(t.duration)} · requested by {t.requester}",
        )
    )


@bot.tree.command(name="volume", description="Set volume (1-100)")
@app_commands.describe(level="Volume from 1 to 100")
@app_commands.guild_only()
async def volume(interaction: discord.Interaction, level: app_commands.Range[int, 1, 100]):
    st = bot.state(interaction.guild.id)
    st.volume = level / 100
    vc = interaction.guild.voice_client
    if vc and vc.source and isinstance(vc.source, discord.PCMVolumeTransformer):
        vc.source.volume = st.volume
    await interaction.response.send_message(f"🔊 Volume set to {level}%.")


@bot.tree.command(name="loop", description="Toggle looping the current song")
@app_commands.guild_only()
async def loop_cmd(interaction: discord.Interaction):
    st = bot.state(interaction.guild.id)
    st.loop = not st.loop
    await interaction.response.send_message(f"🔁 Loop {'on' if st.loop else 'off'}.")


@bot.tree.command(name="leave", description="Disconnect the bot from voice")
@app_commands.guild_only()
async def leave(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if not vc:
        return await interaction.response.send_message("I'm not in a voice channel.", ephemeral=True)
    st = bot.state(interaction.guild.id)
    st.queue.clear()
    st.current = None
    st.loop = False
    await vc.disconnect()
    await interaction.response.send_message("👋 Disconnected.")


if __name__ == "__main__":
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise SystemExit("Set the DISCORD_TOKEN environment variable first.")
    bot.run(token)
