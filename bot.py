import os
import re
import time
import random
import sqlite3
import asyncio
import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv
import yt_dlp as ytdl
import requests
import lyricsgenius
from collections import defaultdict, deque
from datetime import datetime, timedelta

# Load Opus library for macOS
if not discord.opus.is_loaded():
    try:
        opus_paths = [
            '/opt/homebrew/lib/libopus.dylib',
            '/usr/local/lib/libopus.dylib'
        ]
        for path in opus_paths:
            if os.path.exists(path):
                discord.opus.load_opus(path)
                break
    except Exception as e:
        print(f"Opus loading error: {e}")

load_dotenv()

# --- GENIUS SETUP ---
GENIUS_TOKEN = os.getenv("GENIUS_TOKEN")
genius = None
if GENIUS_TOKEN:
    genius = lyricsgenius.Genius(GENIUS_TOKEN, remove_section_headers=False, skip_non_songs=True)
    genius.timeout = 15
    genius.retries = 3

# --- DATABASE SETUP ---
conn = sqlite3.connect("music_stats.db", check_same_thread=False)
cursor = conn.cursor()

cursor.execute('''
    CREATE TABLE IF NOT EXISTS listening_stats (
        user_id INTEGER,
        guild_id INTEGER,
        seconds_listened INTEGER DEFAULT 0,
        songs_played INTEGER DEFAULT 0,
        last_played TIMESTAMP,
        PRIMARY KEY (user_id, guild_id)
    )
''')

cursor.execute('''
    CREATE TABLE IF NOT EXISTS music_friends (
        user1_id INTEGER,
        user2_id INTEGER,
        guild_id INTEGER,
        seconds_together INTEGER DEFAULT 0,
        PRIMARY KEY (user1_id, user2_id, guild_id)
    )
''')

cursor.execute('''
    CREATE TABLE IF NOT EXISTS current_songs (
        guild_id INTEGER PRIMARY KEY,
        title TEXT,
        artist TEXT,
        url TEXT,
        thumbnail TEXT,
        started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
''')

cursor.execute('''
    CREATE TABLE IF NOT EXISTS song_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id INTEGER,
        user_id INTEGER,
        song_title TEXT,
        artist TEXT,
        played_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
''')
conn.commit()

# --- DESIGN SYSTEM ---
class DesignSystem:
    COLORS = {
        'primary': 0x3498DB,
        'success': 0x2ECC71,
        'warning': 0xF1C40F,
        'error': 0xE74C3C,
        'info': 0x9B59B6,
        'music': 0x1DB954,
        'gold': 0xFFD700,
        'pink': 0xE91E63,
        'purple': 0x7289DA,
        'cyan': 0x00BCD4,
        'orange': 0xFF9800
    }
    
    @staticmethod
    def create_progress_bar(current, total, length=15, style='blocks'):
        if total <= 0:
            percent = 0
        else:
            percent = min(1.0, max(0.0, current / total))
        
        filled = int(length * percent)
        
        if style == 'blocks':
            bar = '█' * filled + '░' * (length - filled)
        elif style == 'circles':
            bar = '●' * filled + '○' * (length - filled)
        elif style == 'hearts':
            bar = '❤️' * filled + '🤍' * (length - filled)
        else:
            bar = '▓' * filled + '░' * (length - filled)
        
        return f"`{bar}` **{int(percent * 100)}%**"
    
    @staticmethod
    def create_embed(title, description=None, color='primary', thumbnail=None):
        embed = discord.Embed(
            title=title,
            description=description,
            color=DesignSystem.COLORS.get(color, DesignSystem.COLORS['primary'])
        )
        if thumbnail:
            embed.set_thumbnail(url=thumbnail)
        return embed

def get_user_badge(hours):
    if hours >= 500: return "Divine 🌟"
    elif hours >= 200: return "Mythic 🔱"
    elif hours >= 100: return "Cosmic 🌌"
    elif hours >= 50: return "Legend 👑"
    elif hours >= 20: return "Addict 🎯"
    elif hours >= 10: return "Enthusiast 🔥"
    elif hours >= 5: return "Vibing 🎵"
    elif hours >= 1: return "Listener 🎧"
    return "Newbie 🌱"

def format_time(seconds):
    if not seconds:
        return "0sec"
    
    seconds = int(seconds)
    days = seconds // 86400
    hours = (seconds % 86400) // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    
    parts = []
    if days > 0:
        parts.append(f"{days}d")
    if hours > 0:
        parts.append(f"{hours}h")
    if minutes > 0:
        parts.append(f"{minutes}min")
    if secs > 0 and not parts:
        parts.append(f"{secs}sec")
    
    return " ".join(parts) if parts else "0sec"

def format_time_short(seconds):
    if not seconds:
        return "0m"
    
    seconds = int(seconds)
    days = seconds // 86400
    hours = (seconds % 86400) // 3600
    minutes = (seconds % 3600) // 60
    
    if days > 0:
        return f"{days}d {hours}h"
    elif hours > 0:
        return f"{hours}h {minutes}min"
    elif minutes > 0:
        return f"{minutes}min"
    else:
        return f"{seconds}sec"

def clean_song_title(title):
    if not title:
        return ""
    title = re.sub(r'\(.*?\)|\[.*?\]', '', title)
    title = re.sub(r'(?i)\b(official\s*(video|music\s*video|audio|lyric\s*video|hd|4k|8k|mv|clip))\b', '', title)
    title = re.sub(r'(?i)\b(feat\.?|ft\.?|featuring)\b.*', '', title)
    title = re.sub(r'(?i)\b(prod\.?|produced\s*by)\b.*', '', title)
    title = re.sub(r'[\(\)\[\]{}]', '', title)
    title = re.sub(r'\s+', ' ', title)
    return title.strip()

def extract_artist_from_title(title):
    if not title:
        return None, None
    
    patterns = [
        r'^(.*?)\s*[-–—]\s*(.*)$',
        r'^(.*?)\s*["\'](.*?)["\']\s*$',
    ]
    
    for pattern in patterns:
        match = re.match(pattern, title)
        if match:
            artist = match.group(1).strip()
            song = match.group(2).strip()
            if artist and song:
                return artist, song
    
    return None, title

# --- FFMPEG & YT-DLP CONFIGS ---
ytdl_format_options = {
    'format': 'bestaudio/best',
    'noplaylist': False,
    'quiet': True,
    'default_search': 'ytsearch',
    'nocheckcertificate': True,
    'ignoreerrors': True,
    'no_warnings': True,
    'source_address': '0.0.0.0',
    'extractor_args': {'youtube': {'player_client': ['android', 'mweb'], 'skip': ['dash', 'hls']}}
}

ytdl_client = ytdl.YoutubeDL(ytdl_format_options)

def fetch_track_info(query: str, search_results=1):
    if "open.spotify.com" in query and "track/" in query:
        try:
            track_id = re.search(r'track/([a-zA-Z0-9]+)', query)
            if track_id:
                clean_spotify_url = f"https://open.spotify.com/track/{track_id.group(1)}"
                res = requests.get(f"https://open.spotify.com/oembed?url={clean_spotify_url}", timeout=5).json()
                title = res.get("title")
                if title:
                    query = title
        except Exception as e:
            print(f"Spotify extract error: {e}")

    elif "deezer.com" in query:
        try:
            match = re.search(r'track/(\d+)', query)
            if match:
                res = requests.get(f"https://api.deezer.com/track/{match.group(1)}", timeout=5).json()
                query = f"{res.get('artist', {}).get('name', '')} - {res.get('title', '')}"
        except Exception:
            pass

    if not (query.startswith('http://') or query.startswith('https://')):
        query = f"ytsearch{search_results}:{query}" if search_results > 1 else f"ytsearch:{query}"

    try:
        data = ytdl_client.extract_info(query, download=False)
        if not data:
            return None
        if 'entries' in data:
            entries = [e for e in data['entries'] if e]
            if not entries:
                return None
            return entries if len(entries) > 1 else entries[0]
        return data
    except Exception as e:
        print(f"Fetch error: {e}")
        return None

class YTDLSource(discord.PCMVolumeTransformer):
    def __init__(self, source, *, data, volume=0.5, speed=1.0):
        super().__init__(source, volume)
        self.data = data
        self.title = data.get('title', 'Unknown Track')
        self.duration = data.get('duration', 0)
        self.thumbnail = data.get('thumbnail', None)
        self.url = data.get('webpage_url', '')
        self.uploader = data.get('uploader', 'Unknown Artist')
        self.start_time = time.time()
        self.speed = speed
        self.current_position = 0
        self.paused_at = None
        
        self.artist, self.song_title = extract_artist_from_title(self.title)
        if not self.artist:
            self.artist = self.uploader
        if not self.song_title:
            self.song_title = self.title

    @classmethod
    async def create_source(cls, search_query: str, volume=0.5, speed=1.0, start_time=0):
        data = await asyncio.to_thread(fetch_track_info, search_query)
        if not data or not data.get('url'):
            return None
        
        ffmpeg_opts = {
            'options': f'-vn -af atempo={speed}'
        }
        
        if start_time > 0:
            ffmpeg_opts['before_options'] = f'-ss {start_time} -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5'
        else:
            ffmpeg_opts['before_options'] = '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -probesize 10000000 -analyzeduration 0'
        
        source = discord.FFmpegPCMAudio(data.get('url'), **ffmpeg_opts)
        return cls(source, data=data, volume=volume, speed=speed)


# --- BOT BUILDER FACTORY ---
def setup_bot_instance(prefix: str):
    intents = discord.Intents.default()
    intents.message_content = True
    intents.voice_states = True
    intents.members = True

    bot = commands.Bot(command_prefix=prefix, intents=intents, help_command=None)

    song_queues = defaultdict(deque)
    current_playing = {}
    disconnect_timers = {}
    loop_mode = {}
    volumes = {}
    speeds = {}
    positions = {}
    user_cooldowns = defaultdict(lambda: 0)

    async def start_auto_leave_timer(guild):
        guild_id = guild.id

        if guild_id in disconnect_timers and not disconnect_timers[guild_id].done():
            disconnect_timers[guild_id].cancel()

        async def auto_leave_task():
            await asyncio.sleep(60)
            vc = guild.voice_client
            if vc and vc.is_connected():
                is_alone = len([m for m in vc.channel.members if not m.bot]) == 0
                is_idle = not vc.is_playing() and not vc.is_paused()
                
                if is_alone or is_idle:
                    if guild_id in song_queues:
                        song_queues[guild_id].clear()
                    if guild_id in current_playing:
                        del current_playing[guild_id]
                    await vc.disconnect()

        disconnect_timers[guild_id] = asyncio.create_task(auto_leave_task())

    def cancel_auto_leave_timer(guild_id):
        if guild_id in disconnect_timers and not disconnect_timers[guild_id].done():
            disconnect_timers[guild_id].cancel()

    def play_next(ctx):
        guild_id = ctx.guild.id
        
        if loop_mode.get(guild_id) == 'single' and guild_id in current_playing:
            current_source = current_playing[guild_id]
            if current_source:
                ctx.voice_client.play(current_source, after=lambda e: play_next(ctx))
                return
        
        if guild_id in song_queues and len(song_queues[guild_id]) > 0:
            cancel_auto_leave_timer(guild_id)
            next_player = song_queues[guild_id].popleft()
            current_playing[guild_id] = next_player
            positions[guild_id] = 0
            
            cursor.execute('''
                INSERT OR REPLACE INTO current_songs (guild_id, title, artist, url, thumbnail)
                VALUES (?, ?, ?, ?, ?)
            ''', (guild_id, next_player.title, next_player.artist, next_player.url, next_player.thumbnail))
            
            cursor.execute('''
                INSERT INTO song_history (guild_id, user_id, song_title, artist)
                VALUES (?, ?, ?, ?)
            ''', (guild_id, ctx.author.id if hasattr(ctx, 'author') else 0, next_player.title, next_player.artist))
            conn.commit()
            
            if loop_mode.get(guild_id) == 'queue':
                song_queues[guild_id].append(next_player)
            
            ctx.voice_client.play(next_player, after=lambda e: play_next(ctx))
            
            embed = DesignSystem.create_embed(
                title=f"🎵 Now Playing",
                description=f"**{next_player.title}**",
                color='music'
            )
            embed.add_field(name="✨ Artist", value=next_player.artist, inline=True)
            embed.add_field(name="⏰ Duration", value=format_time(next_player.duration), inline=True)
            embed.add_field(name="⚡ Speed", value=f"x{next_player.speed}", inline=True)
            if next_player.thumbnail:
                embed.set_thumbnail(url=next_player.thumbnail)
            
            asyncio.run_coroutine_threadsafe(ctx.send(embed=embed), bot.loop)
        else:
            if guild_id in current_playing:
                del current_playing[guild_id]
            cursor.execute('DELETE FROM current_songs WHERE guild_id = ?', (guild_id,))
            conn.commit()
            asyncio.run_coroutine_threadsafe(start_auto_leave_timer(ctx.guild), bot.loop)

    async def check_voice(ctx):
        if not ctx.author.voice or not ctx.author.voice.channel:
            embed = DesignSystem.create_embed(
                title="⚠️ Voice Channel Required",
                description="You need to be in a voice channel to use music commands!",
                color='warning'
            )
            embed.add_field(name="💡 How to fix", value="Join a voice channel first, then use the command.", inline=False)
            await ctx.send(embed=embed)
            return False
        
        if ctx.voice_client and ctx.voice_client.is_connected():
            if ctx.author.voice.channel.id != ctx.voice_client.channel.id:
                embed = DesignSystem.create_embed(
                    title="⚠️ Wrong Voice Channel",
                    description=f"You are in **{ctx.author.voice.channel.mention}**",
                    color='warning'
                )
                embed.add_field(
                    name="🎵 Bot is in", 
                    value=f"{ctx.voice_client.channel.mention}",
                    inline=False
                )
                embed.add_field(
                    name="💡 How to fix", 
                    value=f"Join {ctx.voice_client.channel.mention} to use music commands.",
                    inline=False
                )
                await ctx.send(embed=embed)
                return False
                
        return True

    def check_cooldown(user_id, cooldown=1):
        current_time = time.time()
        if current_time - user_cooldowns[user_id] < cooldown:
            return False
        user_cooldowns[user_id] = current_time
        return True

    @bot.event
    async def on_voice_state_update(member, before, after):
        for guild in bot.guilds:
            vc = guild.voice_client
            if vc and vc.is_connected():
                human_members = [m for m in vc.channel.members if not m.bot]
                if len(human_members) == 0:
                    await start_auto_leave_timer(guild)

    @bot.event
    async def on_member_join(member):
        embed = DesignSystem.create_embed(
            title=f"👋 Welcome to {member.guild.name}!",
            description=f"Hey {member.mention}! Welcome aboard 🎉\n\n"
                       f"🎵 **Music Commands:** `{prefix}h` or `{prefix}help`\n"
                       f"💡 **Tip:** Use `{prefix}me` to see your stats!",
            color='success'
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        
        target_channel = member.guild.system_channel
        if not target_channel:
            target_channel = next((c for c in member.guild.text_channels if c.permissions_for(member.guild.me).send_messages), None)

        if target_channel:
            await target_channel.send(embed=embed)

    # --- USER INFO COMMAND ---
    @bot.command(name="A", aliases=["a", "avatar", "pfp"])
    async def user_info(ctx, target: discord.Member = None):
        member = target or ctx.author
        
        embed = discord.Embed(
            title=f"👤 {member.display_name}",
            color=member.color if member.color.value != 0 else 0x3498DB
        )
        embed.set_image(url=member.display_avatar.url)
        embed.set_footer(text=f"Requested by {ctx.author.display_name}")
        
        await ctx.send(embed=embed)

    # --- MUSIC COMMANDS ---
    @bot.command(name="play", aliases=["p"])
    async def play(ctx, *, search: str):
        if not await check_voice(ctx):
            return
        
        if not check_cooldown(ctx.author.id, 2):
            await ctx.send("⏳ Please wait 2 seconds before using this command again!")
            return

        channel = ctx.author.voice.channel
        
        if ctx.voice_client is None:
            await channel.connect()
        
        cancel_auto_leave_timer(ctx.guild.id)
        guild_id = ctx.guild.id
        volume = volumes.get(guild_id, 0.5)
        speed = speeds.get(guild_id, 1.0)

        async with ctx.typing():
            if "playlist" in search or "album" in search or "list=" in search:
                players = await YTDLSource.create_source(search, volume, speed)
                if players:
                    if isinstance(players, list):
                        for player in players[:10]:
                            if guild_id not in song_queues:
                                song_queues[guild_id] = deque()
                            song_queues[guild_id].append(player)
                        await ctx.send(f"📑 **Added {len(players[:10])} tracks from playlist!**")
                    else:
                        if guild_id not in song_queues:
                            song_queues[guild_id] = deque()
                        song_queues[guild_id].append(players)
                        await ctx.send(f"➕ **Added to queue:** {players.title}")
            else:
                player = await YTDLSource.create_source(search, volume, speed)
                if player is None:
                    return await ctx.send("❌ Couldn't load track!")

                cursor.execute('''
                    INSERT INTO listening_stats (user_id, guild_id, seconds_listened, songs_played, last_played)
                    VALUES (?, ?, 0, 1, CURRENT_TIMESTAMP)
                    ON CONFLICT(user_id, guild_id) DO UPDATE SET 
                        songs_played = songs_played + 1,
                        last_played = CURRENT_TIMESTAMP
                ''', (ctx.author.id, ctx.guild.id))
                conn.commit()

                if guild_id not in song_queues:
                    song_queues[guild_id] = deque()

                if ctx.voice_client.is_playing() or ctx.voice_client.is_paused():
                    song_queues[guild_id].append(player)
                    await ctx.send(f"➕ **Added to queue:** {player.title}")
                else:
                    current_playing[guild_id] = player
                    positions[guild_id] = 0
                    
                    cursor.execute('''
                        INSERT OR REPLACE INTO current_songs (guild_id, title, artist, url, thumbnail)
                        VALUES (?, ?, ?, ?, ?)
                    ''', (guild_id, player.title, player.artist, player.url, player.thumbnail))
                    
                    cursor.execute('''
                        INSERT INTO song_history (guild_id, user_id, song_title, artist)
                        VALUES (?, ?, ?, ?)
                    ''', (guild_id, ctx.author.id, player.title, player.artist))
                    conn.commit()
                    
                    ctx.voice_client.play(player, after=lambda e: play_next(ctx))
                    
                    embed = DesignSystem.create_embed(
                        title=f"🎵 Now Playing",
                        description=f"**{player.title}**",
                        color='music'
                    )
                    embed.add_field(name="✨ Artist", value=player.artist, inline=True)
                    embed.add_field(name="⏰ Duration", value=format_time(player.duration), inline=True)
                    embed.add_field(name="⚡ Speed", value=f"x{speed}", inline=True)
                    if player.thumbnail:
                        embed.set_thumbnail(url=player.thumbnail)
                    embed.set_footer(text=f"Requested by {ctx.author.display_name}")
                    await ctx.send(embed=embed)

    # --- SPEED COMMAND ---
    @bot.command(name="speed", aliases=["vitesse", "rate"])
    async def speed(ctx, vitesse: float = None):
        if not await check_voice(ctx):
            return
        
        guild_id = ctx.guild.id
        
        allowed_speeds = [0.5, 0.75, 1.0, 1.25, 1.5, 2.0]
        
        if vitesse is None:
            current_speed = speeds.get(guild_id, 1.0)
            
            embed = DesignSystem.create_embed(
                title="⚡ Playback Speed",
                description=f"Current speed: **x{current_speed}**",
                color='info'
            )
            embed.add_field(
                name="Available Speeds",
                value="`0.5` - Slow\n`0.75` - Slightly slow\n`1.0` - Normal\n`1.25` - Slightly fast\n`1.5` - Fast\n`2.0` - Very fast",
                inline=False
            )
            embed.set_footer(text=f"Usage: {prefix}speed <value>")
            await ctx.send(embed=embed)
            return
        
        if vitesse not in allowed_speeds:
            await ctx.send(f"❌ Invalid speed! Use: {', '.join([str(s) for s in allowed_speeds])}")
            return
        
        speeds[guild_id] = vitesse
        
        if guild_id in current_playing and ctx.voice_client and (ctx.voice_client.is_playing() or ctx.voice_client.is_paused()):
            current_player = current_playing[guild_id]
            elapsed = time.time() - current_player.start_time if ctx.voice_client.is_playing() else 0
            new_position = positions.get(guild_id, 0) + elapsed
            
            ctx.voice_client.stop()
            
            volume = volumes.get(guild_id, 0.5)
            new_player = await YTDLSource.create_source(current_player.url, volume, vitesse, new_position)
            
            if new_player:
                current_playing[guild_id] = new_player
                positions[guild_id] = new_position
                new_player.start_time = time.time()
                ctx.voice_client.play(new_player, after=lambda e: play_next(ctx))
                
                embed = DesignSystem.create_embed(
                    title="⚡ Speed Changed",
                    description=f"Speed set to **x{vitesse}**",
                    color='success'
                )
                embed.add_field(name="🎵 Track", value=current_player.title, inline=True)
                embed.add_field(name="⏰ Position", value=format_time(new_position), inline=True)
                await ctx.send(embed=embed)
        else:
            embed = DesignSystem.create_embed(
                title="⚡ Speed Set",
                description=f"Speed will be **x{vitesse}** for next songs",
                color='success'
            )
            await ctx.send(embed=embed)

    # --- FORWARD 10 SECONDS ---
    @bot.command(name="+", aliases=["fw", "forward", ">>", "+10"])
    async def forward_10s(ctx):
        if not await check_voice(ctx):
            return
        
        if not check_cooldown(ctx.author.id):
            await ctx.send("⏳ Please wait 1 second!")
            return
        
        guild_id = ctx.guild.id
        
        if guild_id not in current_playing:
            return await ctx.send("❌ No music playing!")
        
        if not ctx.voice_client or not ctx.voice_client.is_playing():
            return await ctx.send("❌ No music playing!")
        
        current_player = current_playing[guild_id]
        elapsed = time.time() - current_player.start_time
        current_position = positions.get(guild_id, 0) + elapsed
        new_position = min(current_position + 10, current_player.duration)
        
        if new_position >= current_player.duration:
            return await ctx.send("⏭️ End of track reached! Use `!skip` for next song.")
        
        ctx.voice_client.stop()
        
        volume = volumes.get(guild_id, 0.5)
        speed = speeds.get(guild_id, 1.0)
        new_player = await YTDLSource.create_source(current_player.url, volume, speed, new_position)
        
        if new_player:
            current_playing[guild_id] = new_player
            positions[guild_id] = new_position
            new_player.start_time = time.time()
            ctx.voice_client.play(new_player, after=lambda e: play_next(ctx))
            
            embed = DesignSystem.create_embed(
                title="⏩ +10s",
                description=f"**{current_player.title}**",
                color='info'
            )
            embed.add_field(name="⏰ Position", value=f"{format_time(new_position)} / {format_time(current_player.duration)}", inline=True)
            embed.add_field(name="⚡ Speed", value=f"x{speed}", inline=True)
            await ctx.send(embed=embed)

    # --- REWIND 10 SECONDS ---
    @bot.command(name="-", aliases=["rw", "rewind", "<<", "-10", "back"])
    async def rewind_10s(ctx):
        if not await check_voice(ctx):
            return
        
        if not check_cooldown(ctx.author.id):
            await ctx.send("⏳ Please wait 1 second!")
            return
        
        guild_id = ctx.guild.id
        
        if guild_id not in current_playing:
            return await ctx.send("❌ No music playing!")
        
        if not ctx.voice_client or not ctx.voice_client.is_playing():
            return await ctx.send("❌ No music playing!")
        
        current_player = current_playing[guild_id]
        elapsed = time.time() - current_player.start_time
        current_position = positions.get(guild_id, 0) + elapsed
        new_position = max(current_position - 10, 0)
        
        ctx.voice_client.stop()
        
        volume = volumes.get(guild_id, 0.5)
        speed = speeds.get(guild_id, 1.0)
        new_player = await YTDLSource.create_source(current_player.url, volume, speed, new_position)
        
        if new_player:
            current_playing[guild_id] = new_player
            positions[guild_id] = new_position
            new_player.start_time = time.time()
            ctx.voice_client.play(new_player, after=lambda e: play_next(ctx))
            
            embed = DesignSystem.create_embed(
                title="⏪ -10s",
                description=f"**{current_player.title}**",
                color='info'
            )
            embed.add_field(name="⏰ Position", value=f"{format_time(new_position)} / {format_time(current_player.duration)}", inline=True)
            embed.add_field(name="⚡ Speed", value=f"x{speed}", inline=True)
            await ctx.send(embed=embed)

    @bot.command(name="search", aliases=["find", "sr"])
    async def search_song(ctx, *, query: str):
        if not await check_voice(ctx):
            return
        
        async with ctx.typing():
            results = await asyncio.to_thread(fetch_track_info, query, 5)
            if not results or not isinstance(results, list):
                return await ctx.send("❌ No results found!")
            
            embed = discord.Embed(
                title=f"🔍 Search Results for: {query}",
                color=0x9B59B6
            )
            
            for i, result in enumerate(results[:5], 1):
                embed.add_field(
                    name=f"{i}. {result.get('title', 'Unknown')}",
                    value=f"Duration: {format_time(result.get('duration', 0))}",
                    inline=False
                )
            
            embed.set_footer(text="Type the number (1-5) to play, or 'cancel' to abort")
            await ctx.send(embed=embed)
            
            def check(m):
                return m.author == ctx.author and m.channel == ctx.channel and m.content.isdigit() and 1 <= int(m.content) <= 5
            
            try:
                response = await bot.wait_for('message', timeout=30.0, check=check)
                selected = results[int(response.content) - 1]
                
                channel = ctx.author.voice.channel
                if ctx.voice_client is None:
                    await channel.connect()
                
                player = await YTDLSource.create_source(selected.get('webpage_url'))
                if player:
                    guild_id = ctx.guild.id
                    if guild_id not in song_queues:
                        song_queues[guild_id] = deque()
                    
                    if ctx.voice_client.is_playing() or ctx.voice_client.is_paused():
                        song_queues[guild_id].append(player)
                        await ctx.send(f"➕ **Added to queue:** {player.title}")
                    else:
                        current_playing[guild_id] = player
                        positions[guild_id] = 0
                        cursor.execute('''
                            INSERT OR REPLACE INTO current_songs (guild_id, title, artist, url, thumbnail)
                            VALUES (?, ?, ?, ?, ?)
                        ''', (guild_id, player.title, player.artist, player.url, player.thumbnail))
                        conn.commit()
                        ctx.voice_client.play(player, after=lambda e: play_next(ctx))
                        await ctx.send(f"🎶 **Now Playing:** {player.title}")
                        
            except asyncio.TimeoutError:
                await ctx.send("⏰ Search timed out!")

    @bot.command(name="next", aliases=["n", "skip"])
    async def next_song(ctx):
        if not await check_voice(ctx):
            return

        if ctx.voice_client and (ctx.voice_client.is_playing() or ctx.voice_client.is_paused()):
            ctx.voice_client.stop()
            await ctx.send("⏭️ Skipped!")
        else:
            await ctx.send("❌ No music playing.")

    @bot.command(name="stop", aliases=["st"])
    async def stop(ctx):
        if not await check_voice(ctx):
            return

        guild_id = ctx.guild.id
        if guild_id in song_queues:
            song_queues[guild_id].clear()
        if guild_id in current_playing:
            del current_playing[guild_id]
        
        cursor.execute('DELETE FROM current_songs WHERE guild_id = ?', (guild_id,))
        conn.commit()
        
        if ctx.voice_client and (ctx.voice_client.is_playing() or ctx.voice_client.is_paused()):
            ctx.voice_client.stop()
            await ctx.send("⏹️ Stopped music.")
            await start_auto_leave_timer(ctx.guild)

    @bot.command(name="leave", aliases=["l", "dc", "bye", "Bye", "BYE", "disconnect", "quit", "exit"])
    async def leave(ctx):
        if not await check_voice(ctx):
            return

        guild_id = ctx.guild.id
        cancel_auto_leave_timer(guild_id)
        if guild_id in song_queues:
            song_queues[guild_id].clear()
        if guild_id in current_playing:
            del current_playing[guild_id]
        
        cursor.execute('DELETE FROM current_songs WHERE guild_id = ?', (guild_id,))
        conn.commit()
        
        if ctx.voice_client:
            await ctx.voice_client.disconnect()
            
            embed = DesignSystem.create_embed(
                title="👋 Disconnected",
                description="Bot has left the voice channel.",
                color='error'
            )
            embed.add_field(name="🎵 Queue", value="Cleared", inline=True)
            embed.add_field(name="💡 Tip", value=f"Use `{prefix}p <song>` to play again!", inline=True)
            await ctx.send(embed=embed)

    @bot.command(name="pause", aliases=["ps"])
    async def pause(ctx):
        if not await check_voice(ctx):
            return
        
        if ctx.voice_client and ctx.voice_client.is_playing():
            ctx.voice_client.pause()
            await ctx.send("⏸️ Paused!")
        else:
            await ctx.send("❌ Nothing is playing!")

    @bot.command(name="resume", aliases=["rs", "unpause"])
    async def resume(ctx):
        if not await check_voice(ctx):
            return
        
        if ctx.voice_client and ctx.voice_client.is_paused():
            ctx.voice_client.resume()
            await ctx.send("▶️ Resumed!")
        else:
            await ctx.send("❌ Nothing is paused!")

    @bot.command(name="volume", aliases=["vol", "v"])
    async def volume(ctx, volume: int = None):
        if not await check_voice(ctx):
            return
        
        guild_id = ctx.guild.id
        
        if volume is None:
            current_vol = int(volumes.get(guild_id, 0.5) * 100)
            await ctx.send(f"🔊 Current volume: **{current_vol}%**")
            return
        
        if not 0 <= volume <= 200:
            await ctx.send("❌ Volume must be between 0 and 200!")
            return
        
        volumes[guild_id] = volume / 100
        if ctx.voice_client and ctx.voice_client.source:
            ctx.voice_client.source.volume = volume / 100
            await ctx.send(f"🔊 Volume set to **{volume}%**")
        else:
            await ctx.send(f"🔊 Volume will be set to **{volume}%** for next songs")

    @bot.command(name="queue", aliases=["q", "list"])
    async def queue(ctx, page: int = 1):
        guild_id = ctx.guild.id
        
        if guild_id not in song_queues or len(song_queues[guild_id]) == 0:
            return await ctx.send("📋 Queue is empty!")
        
        items_per_page = 10
        total_pages = max(1, (len(song_queues[guild_id]) + items_per_page - 1) // items_per_page)
        page = min(max(1, page), total_pages)
        
        embed = discord.Embed(
            title=f"📋 Music Queue",
            description=f"**{len(song_queues[guild_id])} songs** in queue",
            color=0x3498DB
        )
        
        start_idx = (page - 1) * items_per_page
        end_idx = min(start_idx + items_per_page, len(song_queues[guild_id]))
        
        for i, song in enumerate(list(song_queues[guild_id])[start_idx:end_idx], start_idx + 1):
            embed.add_field(
                name=f"{i}. {song.title}",
                value=f"Duration: {format_time(song.duration)}",
                inline=False
            )
        
        embed.set_footer(text=f"Page {page}/{total_pages} | Use {prefix}queue <page> to navigate")
        await ctx.send(embed=embed)

    @bot.command(name="remove", aliases=["rm"])
    async def remove_from_queue(ctx, position: int):
        if not await check_voice(ctx):
            return
            
        guild_id = ctx.guild.id
        
        if guild_id not in song_queues or len(song_queues[guild_id]) == 0:
            return await ctx.send("❌ Queue is empty!")
        
        if position < 1 or position > len(song_queues[guild_id]):
            return await ctx.send(f"❌ Invalid position! Queue has {len(song_queues[guild_id])} songs.")
        
        queue_list = list(song_queues[guild_id])
        removed_song = queue_list.pop(position - 1)
        song_queues[guild_id] = deque(queue_list)
        
        await ctx.send(f"🗑️ Removed **{removed_song.title}** from queue!")

    @bot.command(name="shuffle", aliases=["mix"])
    async def shuffle(ctx):
        if not await check_voice(ctx):
            return
            
        guild_id = ctx.guild.id
        
        if guild_id not in song_queues or len(song_queues[guild_id]) == 0:
            return await ctx.send("❌ Queue is empty!")
        
        queue_list = list(song_queues[guild_id])
        random.shuffle(queue_list)
        song_queues[guild_id] = deque(queue_list)
        
        await ctx.send("🔀 Queue shuffled!")

    @bot.command(name="loop", aliases=["repeat"])
    async def loop(ctx, mode: str = None):
        if not await check_voice(ctx):
            return
            
        guild_id = ctx.guild.id
        
        modes = ['off', 'single', 'queue']
        
        if mode is None:
            current_mode = loop_mode.get(guild_id, 'off')
            await ctx.send(f"🔄 Current loop mode: **{current_mode}**")
            return
        
        mode = mode.lower()
        if mode not in modes:
            await ctx.send(f"❌ Invalid mode! Use: `{'`, `'.join(modes)}`")
            return
        
        loop_mode[guild_id] = mode
        await ctx.send(f"🔄 Loop mode set to: **{mode}**")

    @bot.command(name="now", aliases=["np", "current"])
    async def now_playing(ctx):
        guild_id = ctx.guild.id
        
        if guild_id not in current_playing:
            return await ctx.send("❌ Nothing is playing right now!")
        
        player = current_playing[guild_id]
        
        if ctx.voice_client and ctx.voice_client.is_playing():
            elapsed = time.time() - player.start_time
        else:
            elapsed = 0
        
        total_position = positions.get(guild_id, 0) + elapsed
        progress = DesignSystem.create_progress_bar(total_position, player.duration)
        speed = speeds.get(guild_id, 1.0)
        
        embed = DesignSystem.create_embed(
            title=f"🎵 Now Playing",
            description=f"**{player.title}**",
            color='music'
        )
        embed.add_field(name="✨ Artist", value=player.artist, inline=True)
        embed.add_field(name="⚡ Speed", value=f"x{speed}", inline=True)
        embed.add_field(name="⏰ Progress", value=f"{progress}\n`{format_time(total_position)} / {format_time(player.duration)}`", inline=False)
        if player.thumbnail:
            embed.set_thumbnail(url=player.thumbnail)
        
        await ctx.send(embed=embed)

    # --- UNIQUE COMMANDS ---
    @bot.command(name="mood", aliases=["vibe"])
    async def mood_play(ctx, mood: str = None):
        if not await check_voice(ctx):
            return
        
        moods = {
            'happy': ['happy vibes playlist', 'upbeat music mix', 'feel good songs'],
            'sad': ['sad songs playlist', 'emotional music', 'heartbreak songs'],
            'chill': ['chill music playlist', 'lofi beats', 'relaxing music'],
            'party': ['party music mix', 'dance songs', 'club hits'],
            'focus': ['study music', 'concentration music', 'instrumental'],
            'workout': ['workout music', 'gym motivation', 'exercise songs'],
            'sleep': ['sleep music', 'calm piano', 'meditation music'],
            'romantic': ['romantic songs', 'love songs playlist', 'date night music'],
            'nostalgia': ['90s hits', '2000s throwback', 'old school classics'],
            'gaming': ['gaming music', 'epic music', 'video game soundtrack']
        }
        
        if not mood:
            embed = DesignSystem.create_embed(
                title="🎩 Mood Playlist",
                description="Choose your mood and I'll play the perfect music!",
                color='purple'
            )
            moods_list = "\n".join([f"• **{m}**" for m in moods.keys()])
            embed.add_field(name="Available Moods", value=moods_list, inline=False)
            embed.set_footer(text=f"Usage: {prefix}mood <mood_name>")
            await ctx.send(embed=embed)
            return
        
        mood = mood.lower()
        if mood not in moods:
            await ctx.send(f"❌ Invalid mood! Available: {', '.join(moods.keys())}")
            return
        
        search_term = random.choice(moods[mood])
        await play(ctx, search=search_term)

    @bot.command(name="history", aliases=["recent"])
    async def song_history(ctx, limit: int = 5):
        cursor.execute('''
            SELECT song_title, artist, played_at 
            FROM song_history 
            WHERE guild_id = ? 
            ORDER BY played_at DESC 
            LIMIT ?
        ''', (ctx.guild.id, min(limit, 20)))
        history = cursor.fetchall()
        
        if not history:
            return await ctx.send("📊 No history yet!")
        
        embed = DesignSystem.create_embed(
            title="⏰ Recent Songs",
            description=f"Last {len(history)} songs played:",
            color='cyan'
        )
        
        for i, (title, artist, played_at) in enumerate(history, 1):
            embed.add_field(
                name=f"{i}. {title}",
                value=f"Artist: {artist}",
                inline=False
            )
        
        await ctx.send(embed=embed)

    @bot.command(name="random", aliases=["surprise"])
    async def random_song(ctx):
        if not await check_voice(ctx):
            return
            
        random_terms = [
            'top hits 2024', 'popular music', 'trending songs',
            'viral music', 'best songs', 'chart toppers'
        ]
        
        search_term = random.choice(random_terms)
        await play(ctx, search=search_term)

    @bot.command(name="stats", aliases=["statistics", "analytics"])
    async def detailed_stats(ctx):
        cursor.execute('''
            SELECT 
                COUNT(DISTINCT user_id) as total_users,
                SUM(songs_played) as total_songs,
                SUM(seconds_listened) as total_seconds
            FROM listening_stats 
            WHERE guild_id = ?
        ''', (ctx.guild.id,))
        stats = cursor.fetchone()
        
        total_users = stats[0] or 0
        total_songs = stats[1] or 0
        total_seconds = stats[2] or 0
        
        cursor.execute('''
            SELECT song_title, COUNT(*) as play_count
            FROM song_history
            WHERE guild_id = ?
            GROUP BY song_title
            ORDER BY play_count DESC
            LIMIT 1
        ''', (ctx.guild.id,))
        top_song = cursor.fetchone()
        
        embed = DesignSystem.create_embed(
            title="📊 Server Statistics",
            description="Detailed music statistics:",
            color='gold'
        )
        
        embed.add_field(name="✨ Total Users", value=str(total_users), inline=True)
        embed.add_field(name="🎵 Total Songs", value=str(total_songs), inline=True)
        embed.add_field(name="⏰ Total Time", value=format_time(total_seconds), inline=True)
        
        if top_song:
            embed.add_field(name="🏆 Most Played", value=top_song[0], inline=False)
        
        await ctx.send(embed=embed)

    # --- GENIUS LYRICS COMMAND ---
    @bot.command(name="lyric", aliases=["lyrics", "ly"])
    async def get_lyrics(ctx, *, song_name: str = None):
        if not genius:
            return await ctx.send("❌ Genius Token is missing in `.env` file!")

        guild_id = ctx.guild.id
        search_query = None
        
        if not song_name:
            if guild_id in current_playing and current_playing[guild_id]:
                player = current_playing[guild_id]
                if player.artist and player.song_title:
                    search_query = f"{player.artist} {player.song_title}"
                else:
                    search_query = player.title
            else:
                cursor.execute('SELECT title, artist FROM current_songs WHERE guild_id = ?', (guild_id,))
                db_song = cursor.fetchone()
                if db_song and db_song[0]:
                    if db_song[1] and db_song[1] != 'Unknown Artist':
                        search_query = f"{db_song[1]} {db_song[0]}"
                    else:
                        search_query = db_song[0]
                else:
                    return await ctx.send("❌ No song is currently playing! Provide a song name: `!lyric Artist - Song`")
        else:
            search_query = song_name
        
        search_query = clean_song_title(search_query)
        
        async with ctx.typing():
            try:
                song = await asyncio.to_thread(genius.search_song, search_query)
                
                if not song:
                    artist, title = extract_artist_from_title(search_query)
                    if artist and title:
                        song = await asyncio.to_thread(genius.search_song, f"{artist} {title}")
                
                if not song:
                    return await ctx.send(f"❌ Couldn't find lyrics for: **{search_query}**")
                
                lyrics_text = song.lyrics
                lyrics_text = re.sub(r'^\d*Embed', '', lyrics_text)
                if "Lyrics" in lyrics_text:
                    lyrics_text = lyrics_text.split("Lyrics", 1)[-1].strip()
                
                lyrics_chunks = [lyrics_text[i:i+1900] for i in range(0, len(lyrics_text), 1900)]
                
                for i, chunk in enumerate(lyrics_chunks, 1):
                    if i == 1:
                        embed = DesignSystem.create_embed(
                            title=f"📜 {song.title}",
                            description=chunk if chunk else "No printable lyrics found.",
                            color='gold'
                        )
                        embed.set_author(name=f"Artist: {song.artist}")
                        if song.song_art_image_url:
                            embed.set_thumbnail(url=song.song_art_image_url)
                        embed.set_footer(text=f"Page {i}/{len(lyrics_chunks)}")
                    else:
                        embed = discord.Embed(
                            description=f"*(Continued)*\n\n{chunk}",
                            color=0xFFD700
                        )
                        embed.set_footer(text=f"Page {i}/{len(lyrics_chunks)}")
                    
                    await ctx.send(embed=embed)

            except Exception as e:
                print(f"Genius Lyrics Error: {e}")
                await ctx.send("❌ An error occurred while searching Genius.")

    # --- HELP COMMAND ---
    @bot.command(name="help", aliases=["h", "H", "Help", "HELP", "menu", "Menu"])
    async def custom_help(ctx):
        embed = DesignSystem.create_embed(
            title="🎵 MUSIC BOT COMMANDS",
            description="Here are the available commands:",
            color='primary'
        )
        embed.add_field(name="▶️ **Music**", 
                       value=f"`{prefix}p <song/link>` - Play music\n"
                             f"`{prefix}search <query>` - Search songs\n"
                             f"`{prefix}skip` - Skip song\n"
                             f"`{prefix}stop` - Clear & stop\n"
                             f"`{prefix}pause` / `{prefix}resume` - Pause/Resume\n"
                             f"`{prefix}volume <0-200>` - Set volume\n"
                             f"`{prefix}loop <off/single/queue>` - Loop modes\n"
                             f"`{prefix}leave` / `{prefix}bye` - Disconnect bot\n"
                             f"`{prefix}lyric [song]` - Get lyrics", inline=False)
        
        embed.add_field(name="⚡ **Playback Control**",
                       value=f"`{prefix}speed <0.5-2.0>` - Change speed\n"
                             f"`{prefix}+` - Forward 10 seconds\n"
                             f"`{prefix}-` - Rewind 10 seconds\n"
                             f"`{prefix}now` - Now playing", inline=False)
        
        embed.add_field(name="📋 **Queue Management**",
                       value=f"`{prefix}queue` - Show queue\n"
                             f"`{prefix}remove <position>` - Remove from queue\n"
                             f"`{prefix}shuffle` - Shuffle queue", inline=False)
        
        embed.add_field(name="🎮 **Unique Features**",
                       value=f"`{prefix}mood <mood>` - Play by mood\n"
                             f"`{prefix}random` - Random song\n"
                             f"`{prefix}history` - Song history\n"
                             f"`{prefix}stats` - Server stats", inline=False)
        
        embed.add_field(name="🛠️ **Tools & Profile**",
                       value=f"`{prefix}A` / `{prefix}a [@user]` - Show profile image\n"
                             f"`{prefix}me` - Your music profile & BFFs\n"
                             f"`{prefix}top` - Leaderboard\n"
                             f"`{prefix}bff` - Music soulmate", inline=False)
        
        embed.set_footer(text=f"Prefix: {prefix} | Bot made with ❤️")
        await ctx.send(embed=embed)

    # --- ENHANCED PROFILE COMMAND ---
    @bot.command(name="me", aliases=["profile", "prof", "myprofile", "mystats"])
    async def profile_stats(ctx, target: discord.Member = None):
        member = target or ctx.author
        
        cursor.execute('SELECT seconds_listened, songs_played, last_played FROM listening_stats WHERE user_id = ? AND guild_id = ?', (member.id, ctx.guild.id))
        data = cursor.fetchone()
        
        seconds = data[0] if data else 0
        songs = data[1] if data else 0
        last_played = data[2] if data and len(data) > 2 else None
        
        time_display = format_time(seconds)
        
        cursor.execute('SELECT user_id FROM listening_stats WHERE guild_id = ? ORDER BY seconds_listened DESC', (ctx.guild.id,))
        all_users = [row[0] for row in cursor.fetchall()]
        rank = f"#{all_users.index(member.id) + 1}" if member.id in all_users else "N/A"
        
        current_level = int(seconds // 10800) + 1
        seconds_in_level = seconds % 10800
        level_progress = DesignSystem.create_progress_bar(seconds_in_level, 10800, style='hearts')
        badge = get_user_badge(seconds / 3600)
        
        embed = DesignSystem.create_embed(
            title=f"✨ {member.display_name}'s Music Profile",
            description=f"*{badge}*",
            color='purple'
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        
        embed.add_field(name="⏰ Listening Time", value=f"**{time_display}**", inline=True)
        embed.add_field(name="🎵 Songs Played", value=f"**{songs}**", inline=True)
        embed.add_field(name="🏆 Server Rank", value=f"**{rank}**", inline=True)
        
        embed.add_field(
            name=f"⚡ Level {current_level}",
            value=f"{level_progress} `{format_time(seconds_in_level)} / 3h`",
            inline=False
        )
        
        cursor.execute('''
            SELECT 
                CASE 
                    WHEN user1_id = ? THEN user2_id 
                    ELSE user1_id 
                END as friend_id,
                seconds_together
            FROM music_friends 
            WHERE (user1_id = ? OR user2_id = ?) AND guild_id = ?
            ORDER BY seconds_together DESC 
            LIMIT 3
        ''', (member.id, member.id, member.id, ctx.guild.id))
        bffs = cursor.fetchall()
        
        if bffs:
            bff_text = ""
            medals = ["🥇", "🥈", "🥉"]
            for i, (friend_id, secs) in enumerate(bffs):
                friend = ctx.guild.get_member(friend_id)
                friend_name = friend.display_name if friend else f"User_{friend_id}"
                time_together = format_time_short(secs)
                bff_text += f"{medals[i]} **{friend_name}** - `{time_together}`\n"
            
            embed.add_field(
                name="💖 Top 3 Music BFFs",
                value=bff_text or "No BFFs yet!",
                inline=False
            )
        
        cursor.execute('''
            SELECT song_title, artist, COUNT(*) as play_count
            FROM song_history
            WHERE guild_id = ? AND user_id = ?
            GROUP BY song_title, artist
            ORDER BY play_count DESC
            LIMIT 3
        ''', (ctx.guild.id, member.id))
        top_tracks = cursor.fetchall()
        
        if top_tracks:
            tracks_text = ""
            medals = ["🥇", "🥈", "🥉"]
            for i, (title, artist, count) in enumerate(top_tracks):
                tracks_text += f"{medals[i]} **{title}** - `{count}x`\n"
            
            embed.add_field(
                name="🎵 Top 3 Tracks",
                value=tracks_text or "No tracks yet!",
                inline=False
            )
        
        cursor.execute('''
            SELECT guild_id, SUM(seconds_listened) as total_seconds
            FROM listening_stats
            WHERE user_id = ?
            GROUP BY guild_id
            ORDER BY total_seconds DESC
            LIMIT 3
        ''', (member.id,))
        top_servers = cursor.fetchall()
        
        if top_servers:
            servers_text = ""
            medals = ["🥇", "🥈", "🥉"]
            for i, (guild_id, secs) in enumerate(top_servers):
                guild = bot.get_guild(guild_id)
                server_name = guild.name if guild else f"Server_{guild_id}"
                server_time = format_time_short(secs)
                servers_text += f"{medals[i]} **{server_name}** - `{server_time}`\n"
            
            embed.add_field(
                name="🌍 Top 3 Servers",
                value=servers_text or "No server data!",
                inline=False
            )
        
        footer_text = f"Requested by {ctx.author.display_name}"
        if last_played:
            footer_text += f" | Last played: {last_played}"
        embed.set_footer(text=footer_text)
        
        await ctx.send(embed=embed)

    @bot.command(name="top", aliases=["lb", "leaderboard"])
    async def leaderboard(ctx):
        cursor.execute('SELECT user_id, seconds_listened, songs_played FROM listening_stats WHERE guild_id = ? AND seconds_listened > 0 ORDER BY seconds_listened DESC LIMIT 10', (ctx.guild.id,))
        top_users = cursor.fetchall()
        if not top_users:
            return await ctx.send("📊 No stats recorded yet!")

        embed = DesignSystem.create_embed(
            title="🏆 TOP LISTENERS",
            color='gold'
        )
        ranks = ["🥇", "🥈", "🥉"] + [f"{i}️⃣" for i in range(4, 11)]
        
        for idx, (user_id, secs, songs) in enumerate(top_users):
            user = ctx.guild.get_member(user_id)
            name = user.display_name if user else f"User_{user_id}"
            time_display = format_time_short(secs)
            embed.add_field(
                name=f"{ranks[idx]} {name}", 
                value=f"⏱️ **{time_display}** | 🎵 **{songs} tracks**", 
                inline=False
            )
        
        await ctx.send(embed=embed)

    @bot.command(name="bff", aliases=["friend", "soulmate"])
    async def music_bff(ctx, target: discord.Member = None):
        member = target or ctx.author
        cursor.execute('''
            SELECT 
                CASE 
                    WHEN user1_id = ? THEN user2_id 
                    ELSE user1_id 
                END as friend_id,
                seconds_together
            FROM music_friends 
            WHERE (user1_id = ? OR user2_id = ?) AND guild_id = ?
            ORDER BY seconds_together DESC 
            LIMIT 1
        ''', (member.id, member.id, member.id, ctx.guild.id))
        data = cursor.fetchone()
        
        if not data:
            return await ctx.send(f"👥 **{member.display_name}** hasn't listened with anyone yet!")

        friend_id, secs = data
        friend = ctx.guild.get_member(friend_id)
        friend_name = friend.display_name if friend else f"User_{friend_id}"
        time_together = format_time(secs)

        embed = DesignSystem.create_embed(
            title="💖 MUSIC SOULMATE",
            color='pink'
        )
        embed.add_field(name="Listener", value=member.mention, inline=True)
        embed.add_field(name="Soulmate", value=friend.mention if friend else friend_name, inline=True)
        embed.add_field(name="⏰ Time Together", value=f"**{time_together}**", inline=False)
        
        if friend:
            embed.set_thumbnail(url=friend.display_avatar.url)
        
        await ctx.send(embed=embed)

    @bot.event
    async def on_command_error(ctx, error):
        if isinstance(error, commands.CommandNotFound):
            await ctx.send(f"❌ Unknown command! Use `{prefix}help` or `{prefix}h` for help.")
        elif isinstance(error, commands.MissingPermissions):
            await ctx.send("❌ You don't have permission to use this command!")
        else:
            print(f"Error: {error}")

    @bot.event
    async def on_ready():
        print(f'✅ Bot online: {bot.user.name} (Prefix: {prefix})')
        
        await bot.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.listening,
                name=f"{prefix}help | Music Bot"
            )
        )

    return bot


# --- BACKGROUND STATS UPDATER ---
bot1 = setup_bot_instance(prefix="!")

@tasks.loop(seconds=10)
async def global_stats_loop():
    for guild in bot1.guilds:
        for vc in guild.voice_clients:
            if vc and vc.is_playing():
                members = [m for m in vc.channel.members if not m.bot and not m.voice.deaf]
                for member in members:
                    cursor.execute('''
                        INSERT INTO listening_stats (user_id, guild_id, seconds_listened, songs_played)
                        VALUES (?, ?, 10, 0)
                        ON CONFLICT(user_id, guild_id) DO UPDATE SET seconds_listened = seconds_listened + 10
                    ''', (member.id, guild.id))

                for i in range(len(members)):
                    for j in range(i + 1, len(members)):
                        u1, u2 = sorted([members[i].id, members[j].id])
                        cursor.execute('''
                            INSERT INTO music_friends (user1_id, user2_id, guild_id, seconds_together)
                            VALUES (?, ?, ?, 10)
                            ON CONFLICT(user1_id, user2_id, guild_id) DO UPDATE SET seconds_together = seconds_together + 10
                        ''', (u1, u2, guild.id))
                conn.commit()

@bot1.event
async def on_ready():
    global_stats_loop.start()
    print(f'✅ Bot 1 Online: {bot1.user.name} [Prefix: !]')


# --- MAIN RUNNER ---
async def main():
    token1 = os.getenv("DISCORD_TOKEN_1") or os.getenv("DISCORD_TOKEN")
    token2 = os.getenv("DISCORD_TOKEN_2")

    tasks_to_run = []

    if token1:
        tasks_to_run.append(bot1.start(token1))

    if token2:
        bot2 = setup_bot_instance(prefix="?")
        print("🤖 Setting up Bot 2 with prefix [?]")
        tasks_to_run.append(bot2.start(token2))

    if tasks_to_run:
        await asyncio.gather(*tasks_to_run)
    else:
        print("❌ No Discord Tokens found in .env file!")

if __name__ == "__main__":
    asyncio.run(main())