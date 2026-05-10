import discord
from discord.ext import commands
from datetime import datetime, timedelta
import asyncio
import json
import os
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
            )
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
                datetime.fromisoformat(
                    boss["last_kill"]
                )
                if boss["last_kill"]
                else None
            ),

            "next_spawn": (
                datetime.fromisoformat(
                    boss["next_spawn"]
                )
                if boss["next_spawn"]
                else None
            )
        }

# =========================
# BOT READY
# =========================

@bot.event
async def on_ready():

    load_bosses()

    print(f"Conectado como {bot.user}")

    bot.loop.create_task(
        boss_checker()
    )

# =========================
# AGREGAR BOSS
# =========================

@bot.command()
async def addboss(
    ctx,
    name,
    respawn: int
):

    bosses[name] = {

        "respawn": respawn,
        "last_kill": None,
        "next_spawn": None
    }

    save_bosses()

    await ctx.send(
        f"✅ Boss {name} agregado "
        f"({respawn} min)"
    )

# =========================
# REGISTRAR KILL
# =========================

@bot.command()
async def kill(
    ctx,
    name
):

    if name not in bosses:

        await ctx.send(
            "❌ Ese boss no existe"
        )
        return

    now = datetime.now()

    respawn = bosses[name]["respawn"]

    next_spawn = (
        now + timedelta(
            minutes=respawn
        )
    )

    bosses[name]["last_kill"] = now
    bosses[name]["next_spawn"] = next_spawn

    # Reiniciar alertas
    alerted_one_minute.discard(name)
    spawn_announced.discard(name)

    save_bosses()

    hora = next_spawn.strftime(
        "%H:%M:%S"
    )

    await ctx.send(

        f"☠️ {name} derrotado\n"
        f"🔥 Próximo spawn: {hora}"
    )

# =========================
# LISTA DE BOSSES
# =========================

@bot.command()
async def bosseslist(ctx):

    if not bosses:

        await ctx.send(
            "❌ No hay bosses"
        )
        return

    msg = "📋 Boss Timers\n\n"

    now = datetime.now()

    for name, data in bosses.items():

        if data["next_spawn"]:

            remaining = (
                data["next_spawn"] - now
            ).total_seconds()

            if remaining < 0:

                msg += (
                    f"🔥 {name} "
                    f"→ SPAWN NOW\n"
                )

            else:

                mins = int(
                    remaining // 60
                )

                secs = int(
                    remaining % 60
                )

                msg += (
                    f"🦊 {name} "
                    f"→ {mins}m {secs}s\n"
                )

        else:

            msg += (
                f"🦊 {name} "
                f"→ sin registrar\n"
            )

    await ctx.send(msg)

# =========================
# ALERTAS AUTOMATICAS
# =========================

async def boss_checker():

    await bot.wait_until_ready()

    while not bot.is_closed():

        now = datetime.now()

        for guild in bot.guilds:

            for channel in guild.text_channels:

                for name, data in bosses.items():

                    if not data["next_spawn"]:
                        continue

                    remaining = (
                        data["next_spawn"] - now
                    ).total_seconds()

                    # ALERTA 1 MINUTO
                    if (
                        remaining <= 60
                        and remaining > 0
                        and name not in alerted_one_minute
                    ):

                        await channel.send(
                            f"⚠️ {name} "
                            f"aparecerá en 1 minuto"
                        )

                        alerted_one_minute.add(name)

                    # SPAWN
                    if (
                        remaining <= 0
                        and name not in spawn_announced
                    ):

                        await channel.send(
                            f"🔥 {name} "
                            f"debería haber aparecido"
                        )

                        spawn_announced.add(name)

        await asyncio.sleep(10)

# =========================
# PING
# =========================

@bot.command()
async def ping(ctx):

    await ctx.send("pong")

# =========================
# INICIAR BOT + SERVIDOR WEB
# =========================

if __name__ == "__main__":
    # Iniciar servidor Flask en un thread separado
    flask_thread = Thread(target=run_flask, daemon=True)
    flask_thread.start()
    
    # Iniciar bot Discord en el thread principal
    bot.run(TOKEN)
