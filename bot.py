import discord
from discord.ext import commands
from datetime import datetime, timedelta
import asyncio
import json
import os
import random
from dotenv import load_dotenv
from flask import Flask
from threading import Thread

# Cargar variables de entorno
load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
PORT = int(os.getenv("PORT", 10000))

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(
    command_prefix="!",
    intents=intents
)

# =========================
# FLASK WEB SERVER
# =========================

app = Flask(__name__)

@app.route('/')
def home():
    return {"status": "Bot Discord activo", "timestamp": datetime.now().isoformat()}, 200

@app.route('/health')
def health():
    return {"status": "ok"}, 200

def run_flask():
    app.run(host='0.0.0.0', port=PORT, debug=False)

# =========================
# DATOS
# =========================

bosses = {}
SAVE_FILE = "bosses.json"

alerted_one_minute = set()
spawn_announced = set()

# =========================
# GUARDAR / CARGAR
# =========================

def save_bosses():
    data = {}
    for name, boss in bosses.items():
        data[name] = {
            "respawn": boss["respawn"],
            "last_kill": (
                boss["last_kill"].isoformat()
                if boss["last_kill"]
                else None
            ),
            "next_spawn": (
                boss["next_spawn"].isoformat()
                if boss["next_spawn"]
                else None
            ),
            "image_url": boss.get("image_url", None),
            "kills_count": boss.get("kills_count", 0)
        }
    with open(SAVE_FILE, "w") as f:
        json.dump(data, f, indent=4)

def load_bosses():
    if not os.path.exists(SAVE_FILE):
        return
    with open(SAVE_FILE, "r") as f:
        data = json.load(f)
    for name, boss in data.items():
        bosses[name] = {
            "respawn": boss["respawn"],
            "last_kill": (
                datetime.fromisoformat(boss["last_kill"])
                if boss["last_kill"]
                else None
            ),
            "next_spawn": (
                datetime.fromisoformat(boss["next_spawn"])
                if boss["next_spawn"]
                else None
            ),
            "image_url": boss.get("image_url", None),
            "kills_count": boss.get("kills_count", 0)
        }

# =========================
# BOT READY
# =========================

@bot.event
async def on_ready():
    load_bosses()
    print(f"✅ Conectado como {bot.user}")
    bot.loop.create_task(boss_checker())

# =========================
# AGREGAR BOSS
# =========================

@bot.command(name="add")
async def addboss(ctx, name: str, respawn: int, image_url: str = None):
    """Agregar un boss: !add NombreBoss 20 [URL_imagen]"""
    if name.lower() in [b.lower() for b in bosses]:
        await ctx.send(f"❌ {name} ya existe")
        return
    
    bosses[name] = {
        "respawn": respawn,
        "last_kill": None,
        "next_spawn": None,
        "image_url": image_url,
        "kills_count": 0
    }
    save_bosses()
    
    msg = f"✅ **{name}** agregado ({respawn}min)"
    if image_url:
        msg += f"\n🖼️ Imagen configurada"
    
    await ctx.send(msg)

# =========================
# REGISTRAR KILL / AUTO-SPAWN SIMULADO
# =========================

@bot.command(name="k", aliases=["kill", "mata"])
async def kill(ctx, name: str):
    """Matar un boss: !k NombreBoss o !kill NombreBoss"""
    # Buscar el boss (case-insensitive)
    boss_name = None
    for b in bosses:
        if b.lower() == name.lower():
            boss_name = b
            break
    
    if not boss_name:
        await ctx.send(f"❌ **{name}** no existe")
        return
    
    now = datetime.now()
    respawn = bosses[boss_name]["respawn"]
    
    # Simular random kill: ±1 min
    random_delay = random.randint(-60, 60)
    next_spawn = now + timedelta(minutes=respawn) + timedelta(seconds=random_delay)
    
    bosses[boss_name]["last_kill"] = now
    bosses[boss_name]["next_spawn"] = next_spawn
    bosses[boss_name]["kills_count"] += 1
    
    # Reiniciar alertas
    alerted_one_minute.discard(boss_name)
    spawn_announced.discard(boss_name)
    
    save_bosses()
    
    hora = next_spawn.strftime("%H:%M:%S")
    variacion = f"({random_delay:+d}s)" if random_delay != 0 else ""
    
    embed = discord.Embed(
        title=f"☠️ {boss_name} DERROTADO",
        description=f"Próximo spawn: **{hora}** {variacion}",
        color=discord.Color.red()
    )
    embed.add_field(name="Respawn", value=f"{respawn} min", inline=True)
    embed.add_field(name="Total kills", value=f"{bosses[boss_name]['kills_count']}", inline=True)
    
    if bosses[boss_name]["image_url"]:
        embed.set_thumbnail(url=bosses[boss_name]["image_url"])
    
    await ctx.send(embed=embed)

# =========================
# LISTA DE BOSSES
# =========================

@bot.command(name="list", aliases=["bosses", "timers"])
async def bosseslist(ctx):
    """Ver todos los bosses: !list o !bosses o !timers"""
    if not bosses:
        await ctx.send("❌ No hay bosses registrados")
        return
    
    now = datetime.now()
    
    embed = discord.Embed(
        title="📋 BOSS TIMERS",
        color=discord.Color.gold()
    )
    
    for name, data in bosses.items():
        if data["next_spawn"]:
            remaining = (data["next_spawn"] - now).total_seconds()
            
            if remaining < 0:
                remaining = abs(remaining)
                mins = int(remaining // 60)
                secs = int(remaining % 60)
                estado = f"🔥 **SPAWN NOW** (hace {mins}m {secs}s)"
            else:
                mins = int(remaining // 60)
                secs = int(remaining % 60)
                estado = f"⏱️ {mins}m {secs}s"
        else:
            estado = "❓ Sin registrar"
        
        embed.add_field(
            name=name,
            value=f"{estado}\n💀 Kills: {data['kills_count']}",
            inline=False
        )
    
    await ctx.send(embed=embed)

# =========================
# ACTUALIZAR IMAGEN
# =========================

@bot.command(name="img", aliases=["image"])
async def set_image(ctx, name: str, image_url: str):
    """Agregar imagen a un boss: !img NombreBoss URL"""
    boss_name = None
    for b in bosses:
        if b.lower() == name.lower():
            boss_name = b
            break
    
    if not boss_name:
        await ctx.send(f"❌ **{name}** no existe")
        return
    
    bosses[boss_name]["image_url"] = image_url
    save_bosses()
    
    await ctx.send(f"✅ Imagen de **{boss_name}** actualizada")

# =========================
# INFORMACIÓN DE BOSS
# =========================

@bot.command(name="info")
async def boss_info(ctx, name: str):
    """Ver info detallada: !info NombreBoss"""
    boss_name = None
    for b in bosses:
        if b.lower() == name.lower():
            boss_name = b
            break
    
    if not boss_name:
        await ctx.send(f"❌ **{name}** no existe")
        return
    
    data = bosses[boss_name]
    now = datetime.now()
    
    embed = discord.Embed(
        title=f"📊 {boss_name}",
        color=discord.Color.blue()
    )
    
    if data["last_kill"]:
        embed.add_field(
            name="Último kill",
            value=data["last_kill"].strftime("%H:%M:%S"),
            inline=True
        )
    
    if data["next_spawn"]:
        remaining = (data["next_spawn"] - now).total_seconds()
        mins = int(abs(remaining) // 60)
        secs = int(abs(remaining) % 60)
        embed.add_field(
            name="Próximo spawn",
            value=data["next_spawn"].strftime("%H:%M:%S"),
            inline=True
        )
    
    embed.add_field(name="Respawn", value=f"{data['respawn']} min", inline=True)
    embed.add_field(name="Total kills", value=str(data['kills_count']), inline=True)
    
    if data["image_url"]:
        embed.set_image(url=data["image_url"])
    
    await ctx.send(embed=embed)

# =========================
# ALERTAS AUTOMATICAS
# =========================

async def boss_checker():
    await bot.wait_until_ready()
    
    # Usar solo el primer canal de texto del primer guild
    channel = None
    for guild in bot.guilds:
        for ch in guild.text_channels:
            if ch.permissions_for(guild.me).send_messages:
                channel = ch
                break
        if channel:
            break
    
    while not bot.is_closed():
        now = datetime.now()
        
        if channel:
            for name, data in bosses.items():
                if not data["next_spawn"]:
                    continue
                
                remaining = (data["next_spawn"] - now).total_seconds()
                
                # ALERTA 1 MINUTO
                if (remaining <= 60 and remaining > 0 
                    and name not in alerted_one_minute):
                    
                    embed = discord.Embed(
                        title=f"⚠️ {name} APARECERÁ EN 1 MINUTO",
                        color=discord.Color.orange()
                    )
                    if data["image_url"]:
                        embed.set_thumbnail(url=data["image_url"])
                    
                    await channel.send(embed=embed)
                    alerted_one_minute.add(name)
                
                # SPAWN
                if (remaining <= 0 and name not in spawn_announced):
                    
                    embed = discord.Embed(
                        title=f"🔥 {name} DEBERÍA HABER APARECIDO YA",
                        color=discord.Color.red()
                    )
                    if data["image_url"]:
                        embed.set_image(url=data["image_url"])
                    
                    await channel.send(embed=embed)
                    spawn_announced.add(name)
        
        await asyncio.sleep(10)

# =========================
# COMANDOS ÚTILES
# =========================

@bot.command()
async def ping(ctx):
    """Ping del bot"""
    await ctx.send(f"🏓 Pong! {round(bot.latency * 1000)}ms")

@bot.command(name="help", aliases=["h", "?"])
async def help_command(ctx):
    """Ver todos los comandos"""
    embed = discord.Embed(
        title="📖 COMANDOS DISPONIBLES",
        color=discord.Color.blurple()
    )
    
    commands_info = [
        ("!add NombreBoss MINUTOS [URL]", "Agregar un boss"),
        ("!k / !kill / !mata NombreBoss", "Registrar kill (auto-spawn ±1min)"),
        ("!list / !bosses / !timers", "Ver todos los timers"),
        ("!info NombreBoss", "Info detallada del boss"),
        ("!img NombreBoss URL", "Agregar/actualizar imagen"),
        ("!ping", "Ver latencia del bot"),
        ("!help", "Ver este mensaje"),
    ]
    
    for cmd, desc in commands_info:
        embed.add_field(name=cmd, value=desc, inline=False)
    
    await ctx.send(embed=embed)

# =========================
# INICIAR BOT + SERVIDOR WEB
# =========================

if __name__ == "__main__":
    flask_thread = Thread(target=run_flask, daemon=True)
    flask_thread.start()
    bot.run(TOKEN)
