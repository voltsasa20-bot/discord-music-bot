import discord
from discord import app_commands
from discord.ext import commands
import yt_dlp
import asyncio
import os
from collections import deque

# ─────────────────────────────────────────────
#  Настройки yt-dlp
# ─────────────────────────────────────────────
YTDL_OPTIONS = {
    "format": "bestaudio/best",
    "noplaylist": False,
    "quiet": True,
    "no_warnings": True,
    "default_search": "ytsearch",
    "source_address": "0.0.0.0",
}

FFMPEG_OPTIONS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn -b:a 128k",
}

ytdl = yt_dlp.YoutubeDL(YTDL_OPTIONS)


# ─────────────────────────────────────────────
#  Класс трека
# ─────────────────────────────────────────────
class Track:
    def __init__(self, url: str, title: str, webpage_url: str):
        self.url = url          # прямая ссылка на аудиопоток
        self.title = title      # название трека
        self.webpage_url = webpage_url  # ссылка на YouTube-страницу


async def fetch_tracks(query: str) -> list[Track]:
    """Извлекает один или несколько треков из запроса/ссылки."""
    loop = asyncio.get_event_loop()

    def _extract():
        with yt_dlp.YoutubeDL(YTDL_OPTIONS) as ydl:
            info = ydl.extract_info(query, download=False)
        return info

    info = await loop.run_in_executor(None, _extract)

    tracks = []
    if "entries" in info:
        # Это плейлист
        for entry in info["entries"]:
            if entry:
                url = entry.get("url") or entry.get("formats", [{}])[0].get("url", "")
                tracks.append(Track(
                    url=url,
                    title=entry.get("title", "Без названия"),
                    webpage_url=entry.get("webpage_url", query),
                ))
    else:
        url = info.get("url") or info.get("formats", [{}])[0].get("url", "")
        tracks.append(Track(
            url=url,
            title=info.get("title", "Без названия"),
            webpage_url=info.get("webpage_url", query),
        ))

    return tracks


# ─────────────────────────────────────────────
#  Состояние для каждого сервера (guild)
# ─────────────────────────────────────────────
class GuildState:
    def __init__(self):
        self.queue: deque[Track] = deque()
        self.current: Track | None = None
        self.text_channel: discord.TextChannel | None = None
        self.voice_client: discord.VoiceClient | None = None


guild_states: dict[int, GuildState] = {}


def get_state(guild_id: int) -> GuildState:
    if guild_id not in guild_states:
        guild_states[guild_id] = GuildState()
    return guild_states[guild_id]


# ─────────────────────────────────────────────
#  Инициализация бота
# ─────────────────────────────────────────────
intents = discord.Intents.default()

bot = commands.Bot(command_prefix="!", intents=intents)


# ─────────────────────────────────────────────
#  Воспроизведение треков
# ─────────────────────────────────────────────
def play_next(guild_id: int):
    state = get_state(guild_id)
    vc = state.voice_client

    if not vc or not vc.is_connected():
        return

    if not state.queue:
        state.current = None
        asyncio.run_coroutine_threadsafe(vc.disconnect(), bot.loop)
        return

    track = state.queue.popleft()
    state.current = track

    source = discord.FFmpegOpusAudio(track.url, **FFMPEG_OPTIONS)

    def after_playing(error):
        if error:
            print(f"Ошибка воспроизведения: {error}")
        play_next(guild_id)

    vc.play(source, after=after_playing)

    # Отправляем Embed в чат
    asyncio.run_coroutine_threadsafe(
        send_now_playing(state.text_channel, track),
        bot.loop,
    )


async def send_now_playing(channel: discord.TextChannel, track: Track):
    if channel is None:
        return
    embed = discord.Embed(
        title="🎵 Сейчас играет",
        description=f"**[{track.title}]({track.webpage_url})**",
        color=discord.Color.blurple(),
    )
    embed.set_footer(text="Управление: /skip • /queue • /stop")
    await channel.send(embed=embed)


# ─────────────────────────────────────────────
#  Событие готовности
# ─────────────────────────────────────────────
@bot.event
async def on_ready():
    print(f"✅ Бот запущен как {bot.user} (ID: {bot.user.id})")
    try:
        synced = await bot.tree.sync()
        print(f"✅ Синхронизировано {len(synced)} slash-команд")
    except Exception as e:
        print(f"❌ Ошибка синхронизации команд: {e}")


# ─────────────────────────────────────────────
#  /play
# ─────────────────────────────────────────────
@bot.tree.command(name="play", description="Воспроизвести трек или плейлист с YouTube")
@app_commands.describe(query="Ссылка на YouTube или название песни")
async def play(interaction: discord.Interaction, query: str):
    if not interaction.user.voice:
        await interaction.response.send_message(
            "❌ Сначала зайди в голосовой канал!", ephemeral=True
        )
        return

    await interaction.response.defer(thinking=True)

    state = get_state(interaction.guild_id)
    state.text_channel = interaction.channel

    # Подключаемся к голосовому каналу
    voice_channel = interaction.user.voice.channel
    if state.voice_client and state.voice_client.is_connected():
        if state.voice_client.channel != voice_channel:
            await state.voice_client.move_to(voice_channel)
    else:
        state.voice_client = await voice_channel.connect()

    # Получаем треки
    try:
        tracks = await fetch_tracks(query)
    except Exception as e:
        await interaction.followup.send(f"❌ Не удалось найти трек: `{e}`")
        return

    if not tracks:
        await interaction.followup.send("❌ Треки не найдены.")
        return

    # Добавляем в очередь
    for track in tracks:
        state.queue.append(track)

    if len(tracks) == 1:
        msg = f"✅ Добавлено в очередь: **{tracks[0].title}**"
    else:
        msg = f"✅ Добавлено в очередь: **{len(tracks)} треков** из плейлиста"

    await interaction.followup.send(msg)

    # Запускаем воспроизведение, если ничего не играет
    if not state.voice_client.is_playing() and not state.voice_client.is_paused():
        play_next(interaction.guild_id)


# ─────────────────────────────────────────────
#  /skip
# ─────────────────────────────────────────────
@bot.tree.command(name="skip", description="Пропустить текущую песню")
async def skip(interaction: discord.Interaction):
    state = get_state(interaction.guild_id)
    vc = state.voice_client

    if not vc or not vc.is_playing():
        await interaction.response.send_message(
            "❌ Сейчас ничего не играет.", ephemeral=True
        )
        return

    vc.stop()  # after_playing вызовет play_next автоматически
    await interaction.response.send_message("⏭️ Трек пропущен.")


# ─────────────────────────────────────────────
#  /queue
# ─────────────────────────────────────────────
@bot.tree.command(name="queue", description="Показать очередь треков")
async def queue(interaction: discord.Interaction):
    state = get_state(interaction.guild_id)

    embed = discord.Embed(title="📋 Очередь треков", color=discord.Color.blurple())

    if state.current:
        embed.add_field(
            name="🎵 Сейчас играет",
            value=f"[{state.current.title}]({state.current.webpage_url})",
            inline=False,
        )

    if state.queue:
        lines = []
        for i, track in enumerate(list(state.queue)[:15], start=1):
            lines.append(f"`{i}.` [{track.title}]({track.webpage_url})")
        if len(state.queue) > 15:
            lines.append(f"...и ещё {len(state.queue) - 15} треков")
        embed.add_field(name="📌 В очереди", value="\n".join(lines), inline=False)
    else:
        embed.add_field(name="📌 В очереди", value="Очередь пуста", inline=False)

    await interaction.response.send_message(embed=embed)


# ─────────────────────────────────────────────
#  /stop
# ─────────────────────────────────────────────
@bot.tree.command(name="stop", description="Остановить воспроизведение и очистить очередь")
async def stop(interaction: discord.Interaction):
    state = get_state(interaction.guild_id)
    vc = state.voice_client

    if not vc or not vc.is_connected():
        await interaction.response.send_message(
            "❌ Бот не подключён к голосовому каналу.", ephemeral=True
        )
        return

    state.queue.clear()
    state.current = None
    vc.stop()
    await vc.disconnect()
    state.voice_client = None

    await interaction.response.send_message("⏹️ Воспроизведение остановлено, очередь очищена.")


# ─────────────────────────────────────────────
#  Запуск бота
# ─────────────────────────────────────────────
TOKEN = os.getenv("DISCORD_TOKEN")

if not TOKEN:
    print("❌ Ошибка: переменная окружения DISCORD_TOKEN не задана.")
    print("   Установи её командой: set DISCORD_TOKEN=твой_токен  (Windows)")
    print("   Или: export DISCORD_TOKEN=твой_токен  (Mac/Linux)")
    exit(1)

bot.run(TOKEN)
