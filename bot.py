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
    if hours >= 500: return "Divine"
    elif hours >= 200: return "Mythic"
    elif hours >= 100: return "Cosmic"
    elif hours >= 50: return "Legend"
    elif hours >= 20: return "Addict"
    elif hours >= 10: return "Enthusiast"
    elif hours >= 5: return "Vibing"
    elif hours >= 1: return "Listener"
    return "Newbie"

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
                title=f"Now Playing",
                description=f"**{next_player.title}**",
                color='music'
            )
            embed.add_field(name="Artist", value=next_player.artist, inline=True)
            embed.add_field(name="Duration", value=format_time(next_player.duration), inline=True)
            embed.add_field(name="Speed", value=f"x{next_player.speed}", inline=True)
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
                title="Voice Channel Required",
                description="You need to be in a voice channel to use music commands!",
                color='warning'
            )
            embed.add_field(name="How to fix", value="Join a voice channel first, then use the command.", inline=False)
            await ctx.send(embed=embed)
            return False
        
        if ctx.voice_client and ctx.voice_client.is_connected():
            if ctx.author.voice.channel.id != ctx.voice_client.channel.id:
                embed = DesignSystem.create_embed(
                    title="Wrong Voice Channel",
                    description=f"You are in **{ctx.author.voice.channel.mention}**",
                    color='warning'
                )
                embed.add_field(
                    name="Bot is in", 
                    value=f"{ctx.voice_client.channel.mention}",
                    inline=False
                )
                embed.add_field(
                    name="How to fix", 
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
            title=f"Welcome to {member.guild.name}!",
            description=f"Hey {member.mention}! Welcome aboard!\n\n"
                       f"Music Commands: `{prefix}h` or `{prefix}help`\n"
                       f"Tip: Use `{prefix}me` to see your stats!",
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
            title=f"{member.display_name}",
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
            await ctx.send("Please wait 2 seconds before using this command again!")
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
                        await ctx.send(f"Added {len(players[:10])} tracks from playlist!")
                    else:
                        if guild_id not in song_queues:
                            song_queues[guild_id] = deque()
                        song_queues[guild_id].append(players)
                        await ctx.send(f"Added to queue: {players.title}")
            else:
                player = await YTDLSource.create_source(search, volume, speed)
                if player is None:
                    return await ctx.send("Couldn't load track!")

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
                    await ctx.send(f"Added to queue: {player.title}")
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
                        title=f"Now Playing",
                        description=f"**{player.title}**",
                        color='music'
                    )
                    embed.add_field(name="Artist", value=player.artist, inline=True)
                    embed.add_field(name="Duration", value=format_time(player.duration), inline=True)
                    embed.add_field(name="Speed", value=f"x{speed}", inline=True)
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
                title="Playback Speed",
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
            await ctx.send(f"Invalid speed! Use: {', '.join([str(s) for s in allowed_speeds])}")
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
                    title="Speed Changed",
                    description=f"Speed set to **x{vitesse}**",
                    color='success'
                )
                embed.add_field(name="Track", value=current_player.title, inline=True)
                embed.add_field(name="Position", value=format_time(new_position), inline=True)
                await ctx.send(embed=embed)
        else:
            embed = DesignSystem.create_embed(
                title="Speed Set",
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
            await ctx.send("Please wait 1 second!")
            return
        
        guild_id = ctx.guild.id
        
        if guild_id not in current_playing:
            return await ctx.send("No music playing!")
        
        if not ctx.voice_client or not ctx.voice_client.is_playing():
            return await ctx.send("No music playing!")
        
        current_player = current_playing[guild_id]
        elapsed = time.time() - current_player.start_time
        current_position = positions.get(guild_id, 0) + elapsed
        new_position = min(current_position + 10, current_player.duration)
        
        if new_position >= current_player.duration:
            return await ctx.send("End of track reached! Use skip for next song.")
        
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
                title="+10s",
                description=f"**{current_player.title}**",
                color='info'
            )
            embed.add_field(name="Position", value=f"{format_time(new_position)} / {format_time(current_player.duration)}", inline=True)
            embed.add_field(name="Speed", value=f"x{speed}", inline=True)
            await ctx.send(embed=embed)

    # --- REWIND 10 SECONDS ---
    @bot.command(name="-", aliases=["rw", "rewind", "<<", "-10", "back"])
    async def rewind_10s(ctx):
        if not await check_voice(ctx):
            return
        
        if not check_cooldown(ctx.author.id):
            await ctx.send("Please wait 1 second!")
            return
        
        guild_id = ctx.guild.id
        
        if guild_id not in current_playing:
            return await ctx.send("No music playing!")
        
        if not ctx.voice_client or not ctx.voice_client.is_playing():
            return await ctx.send("No music playing!")
        
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
                title="-10s",
                description=f"**{current_player.title}**",
                color='info'
            )
            embed.add_field(name="Position", value=f"{format_time(new_position)} / {format_time(current_player.duration)}", inline=True)
            embed.add_field(name="Speed", value=f"x{speed}", inline=True)
            await ctx.send(embed=embed)

    @bot.command(name="search", aliases=["find", "sr"])
    async def search_song(ctx, *, query: str):
        if not await check_voice(ctx):
            return
        
        async with ctx.typing():
            results = await asyncio.to_thread(fetch_track_info, query, 5)
            if not results or not isinstance(results, list):
                return await ctx.send("No results found!")
            
            embed = discord.Embed(
                title=f"Search Results for: {query}",
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
                        await ctx.send(f"Added to queue: {player.title}")
                    else:
                        current_playing[guild_id] = player
                        positions[guild_id] = 0
                        cursor.execute('''
                            INSERT OR REPLACE INTO current_songs (guild_id, title, artist, url, thumbnail)
                            VALUES (?, ?, ?, ?, ?)
                        ''', (guild_id, player.title, player.artist, player.url, player.thumbnail))
                        conn.commit()
                        ctx.voice_client.play(player, after=lambda e: play_next(ctx))
                        await ctx.send(f"Now Playing: {player.title}")
                        
            except asyncio.TimeoutError:
                await ctx.send("Search timed out!")

    @bot.command(name="next", aliases=["n", "skip"])
    async def next_song(ctx):
        if not await check_voice(ctx):
            return

        if ctx.voice_client and (ctx.voice_client.is_playing() or ctx.voice_client.is_paused()):
            ctx.voice_client.stop()
            await ctx.send("Skipped!")
        else:
            await ctx.send("No music playing.")

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
            await ctx.send("Stopped music.")
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
                title="Disconnected",
                description="Bot has left the voice channel.",
                color='error'
            )
            embed.add_field(name="Queue", value="Cleared", inline=True)
            embed.add_field(name="Tip", value=f"Use `{prefix}p <song>` to play again!", inline=True)
            await ctx.send(embed=embed)

    @bot.command(name="pause", aliases=["ps"])
    async def pause(ctx):
        if not await check_voice(ctx):
            return
        
        if ctx.voice_client and ctx.voice_client.is_playing():
            ctx.voice_client.pause()
            await ctx.send("Paused!")
        else:
            await ctx.send("Nothing is playing!")

    @bot.command(name="resume", aliases=["rs", "unpause"])
    async def resume(ctx):
        if not await check_voice(ctx):
            return
        
        if ctx.voice_client and ctx.voice_client.is_paused():
            ctx.voice_client.resume()
            await ctx.send("Resumed!")
        else:
            await ctx.send("Nothing is paused!")

    @bot.command(name="volume", aliases=["vol", "v"])
    async def volume(ctx, volume: int = None):
        if not await check_voice(ctx):
            return
        
        guild_id = ctx.guild.id
        
        if volume is None:
            current_vol = int(volumes.get(guild_id, 0.5) * 100)
            await ctx.send(f"Current volume: **{current_vol}%**")
            return
        
        if not 0 <= volume <= 200:
            await ctx.send("Volume must be between 0 and 200!")
            return
        
        volumes[guild_id] = volume / 100
        if ctx.voice_client and ctx.voice_client.source:
            ctx.voice_client.source.volume = volume / 100
            await ctx.send(f"Volume set to **{volume}%**")
        else:
            await ctx.send(f"Volume will be set to **{volume}%** for next songs")

    @bot.command(name="queue", aliases=["q", "list"])
    async def queue(ctx, page: int = 1):
        guild_id = ctx.guild.id
        
        if guild_id not in song_queues or len(song_queues[guild_id]) == 0:
            return await ctx.send("Queue is empty!")
        
        items_per_page = 10
        total_pages = max(1, (len(song_queues[guild_id]) + items_per_page - 1) // items_per_page)
        page = min(max(1, page), total_pages)
        
        embed = discord.Embed(
            title=f"Music Queue",
            description=f"**{len(song_queues[guild_id])} songs** in queue",
            color=0x3498DB
        )
        
        start_idx = (page - 1) * items
