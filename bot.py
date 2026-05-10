# -*- coding: utf-8 -*-
import discord
from discord.ext import commands
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import asyncio
import json
import os
import re
import threading
from flask import Flask
from threading import Thread
import traceback

from dotenv import load_dotenv
load_dotenv()

TOKEN            = os.getenv("DISCORD_TOKEN")
PORT             = int(os.getenv("PORT", 10000))
ALERT_ROLE_NAME  = os.getenv("ALERT_ROLE", "cazador de bosses")
ALERT_CHANNEL_ID = int(os.getenv("ALERT_CHANNEL_ID", 0))  # ID del canal de alertas

# Zona horaria Argentina (UTC-3)
AR_TZ = ZoneInfo("America/Argentina/Buenos_Aires")

def now_ar():
    return datetime.now(AR_TZ)

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

# =========================
# FLASK WEB SERVER
# =========================

app = Flask(__name__)

@app.route('/')
def home():
    return {"status": "Bot Discord activo", "timestamp": now_ar().isoformat()}, 200

@app.route('/health')
def health():
    return {"status": "ok"}, 200

def run_flask():
    app.run(host='0.0.0.0', port=PORT, debug=False)

# =========================
# DATOS
# =========================

bosses    = {}
SAVE_FILE = "bosses.json"
data_lock = threading.Lock()

alerted_warning = set()
spawn_announced = set()

# =========================
# GUARDAR / CARGAR
# =========================

def save_bosses():
    try:
        data = {}
        for name, boss in bosses.items():
            data[name] = {
                "respawn":       boss["respawn"],
                "last_kill":     boss["last_kill"].isoformat()  if boss["last_kill"]  else None,
                "next_spawn":    boss["next_spawn"].isoformat() if boss["next_spawn"] else None,
                "image_url":     boss.get("image_url"),
                "kills_count":   boss.get("kills_count", 0),
                "killer_id":     boss.get("killer_id"),
                "killer_tagged": boss.get("killer_tagged", False),
                "missed_spawns": boss.get("missed_spawns", 0),
                "drops":         boss.get("drops", ""),
                "location":      boss.get("location", ""),
                "extra_info":    boss.get("extra_info", ""),
            }
        with open(SAVE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
        print("Bosses guardados en " + SAVE_FILE)
    except Exception as e:
        print("Error al guardar bosses: " + str(e))
        traceback.print_exc()

def load_bosses():
    try:
        if not os.path.exists(SAVE_FILE):
            save_bosses()
            return

        with open(SAVE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        def parse_dt(val):
            if not val:
                return None
            dt = datetime.fromisoformat(val)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=AR_TZ)
            return dt

        for name, boss in data.items():
            bosses[name] = {
                "respawn":       boss["respawn"],
                "last_kill":     parse_dt(boss.get("last_kill")),
                "next_spawn":    parse_dt(boss.get("next_spawn")),
                "image_url":     boss.get("image_url"),
                "kills_count":   boss.get("kills_count", 0),
                "killer_id":     boss.get("killer_id"),
                "killer_tagged": boss.get("killer_tagged", False),
                "missed_spawns": boss.get("missed_spawns", 0),
                "drops":         boss.get("drops", ""),
                "location":      boss.get("location", ""),
                "extra_info":    boss.get("extra_info", ""),
            }
        print(str(len(bosses)) + " bosses cargados")
    except Exception as e:
        print("Error al cargar bosses: " + str(e))
        traceback.print_exc()

# =========================
# HELPERS
# =========================

def find_boss(name: str):
    for k, v in bosses.items():
        if k.lower() == name.lower():
            return k, v
    return None, None

def format_remaining(seconds: float) -> str:
    seconds = abs(int(seconds))
    h    = seconds // 3600
    mins = (seconds % 3600) // 60
    secs = seconds % 60
    if h > 0:
        return f"{h}h {mins}m"
    return f"{mins}m {secs}s"

def warning_threshold(respawn_minutes: int) -> int:
    """Minutos de anticipacion segun el respawn del boss."""
    if respawn_minutes < 10:
        return 1
    elif respawn_minutes < 30:
        return 3
    elif respawn_minutes < 60:
        return 5
    elif respawn_minutes < 240:
        return 10
    elif respawn_minutes < 720:
        return 20
    else:
        return 30

async def get_alert_channel():
    """Canal de alertas: usa ALERT_CHANNEL_ID si esta configurado, sino el primero disponible."""
    if ALERT_CHANNEL_ID:
        ch = bot.get_channel(ALERT_CHANNEL_ID)
        if ch:
            return ch
    for guild in bot.guilds:
        for ch in guild.text_channels:
            if ch.permissions_for(guild.me).send_messages:
                return ch
    return None

async def get_alert_role(guild: discord.Guild):
    return discord.utils.get(guild.roles, name=ALERT_ROLE_NAME)

# =========================
# BOT READY
# =========================

@bot.event
async def on_ready():
    try:
        load_bosses()
        print("Conectado como " + str(bot.user))
        bot.loop.create_task(boss_checker())
    except Exception as e:
        print("Error en on_ready: " + str(e))
        traceback.print_exc()

# =========================
# SETCHANNEL  !setchannel
# =========================

@bot.command(name="setchannel")
async def set_channel(ctx):
    """Usa este comando en el canal donde queres las alertas."""
    await ctx.send(
        f"Canal: **#{ctx.channel.name}**\n"
        f"Agrega esta variable en Render:\n"
        f"`ALERT_CHANNEL_ID` = `{ctx.channel.id}`\n"
        f"Luego reinicia el bot."
    )

# =========================
# ADD  !add
# =========================

@bot.command(name="add")
async def addboss(ctx, name: str, respawn: int, image_url: str = None):
    """Agregar un boss: !add NombreBoss 20 [URL_imagen]"""
    try:
        if find_boss(name)[0]:
            await ctx.send(f"**{name}** ya existe. Usa `!edit {name} <minutos>` para cambiar el respawn.")
            return

        with data_lock:
            bosses[name] = {
                "respawn":       respawn,
                "last_kill":     None,
                "next_spawn":    None,
                "image_url":     image_url,
                "kills_count":   0,
                "killer_id":     None,
                "killer_tagged": False,
                "missed_spawns": 0,
                "drops":         "",
                "location":      "",
                "extra_info":    "",
            }
            save_bosses()

        threshold = warning_threshold(respawn)
        msg = f"**{name}** agregado â€” respawn {respawn} min (aviso: {threshold} min antes)"
        if image_url:
            msg += "\nImagen configurada."
        await ctx.send(msg)
    except Exception as e:
        print("Error en !add: " + str(e))
        traceback.print_exc()
        await ctx.send("Error: " + str(e))

# =========================
# REMOVE  !remove
# =========================

@bot.command(name="remove", aliases=["delete", "del", "rm"])
async def remove_boss(ctx, name: str):
    """Eliminar un boss: !remove NombreBoss"""
    try:
        boss_name, _ = find_boss(name)
        if not boss_name:
            await ctx.send(f"**{name}** no existe.")
            return

        with data_lock:
            del bosses[boss_name]
            alerted_warning.discard(boss_name)
            spawn_announced.discard(boss_name)
            save_bosses()

        await ctx.send(f"**{boss_name}** eliminado.")
    except Exception as e:
        print("Error en !remove: " + str(e))
        traceback.print_exc()
        await ctx.send("Error: " + str(e))

# =========================
# EDIT  !edit
# =========================

@bot.command(name="edit")
async def edit_boss(ctx, name: str, respawn: int):
    """Cambiar el tiempo de respawn: !edit NombreBoss 25"""
    try:
        boss_name, data = find_boss(name)
        if not boss_name:
            await ctx.send(f"**{name}** no existe.")
            return

        old = data["respawn"]
        with data_lock:
            bosses[boss_name]["respawn"] = respawn
            if bosses[boss_name]["last_kill"]:
                bosses[boss_name]["next_spawn"] = (
                    bosses[boss_name]["last_kill"] + timedelta(minutes=respawn)
                )
                alerted_warning.discard(boss_name)
                spawn_announced.discard(boss_name)
            save_bosses()

        threshold = warning_threshold(respawn)
        await ctx.send(
            f"**{boss_name}**: respawn {old} min -> {respawn} min "
            f"(aviso: {threshold} min antes)"
        )
    except Exception as e:
        print("Error en !edit: " + str(e))
        traceback.print_exc()
        await ctx.send("Error: " + str(e))

# =========================
# SETINFO  !setinfo
# =========================

@bot.command(name="setinfo")
async def set_info(ctx, name: str, *, args: str):
    """
    Agregar info extra: !setinfo Boss drops:Espada,Escudo lugar:Cueva nota:Aparece de noche
    Claves validas: drops, lugar, nota
    """
    try:
        boss_name, _ = find_boss(name)
        if not boss_name:
            await ctx.send(f"**{name}** no existe.")
            return

        pattern = r'(drops|lugar|nota):(.+?)(?=\s+(?:drops|lugar|nota):|$)'
        matches = re.findall(pattern, args, re.IGNORECASE)

        if not matches:
            await ctx.send(
                "Formato incorrecto. Ejemplo:\n"
                "`!setinfo Boss drops:Espada,Escudo lugar:Cueva nota:Aparece de noche`"
            )
            return

        changes = []
        with data_lock:
            for key, value in matches:
                value = value.strip()
                key   = key.lower()
                if key == "drops":
                    bosses[boss_name]["drops"]      = value
                    changes.append(f"Drops: {value}")
                elif key == "lugar":
                    bosses[boss_name]["location"]   = value
                    changes.append(f"Lugar: {value}")
                elif key == "nota":
                    bosses[boss_name]["extra_info"] = value
                    changes.append(f"Nota: {value}")
            save_bosses()

        embed = discord.Embed(
            title=f"Info de {boss_name} actualizada",
            description="\n".join(changes),
            color=discord.Color.blurple()
        )
        await ctx.send(embed=embed)
    except Exception as e:
        print("Error en !setinfo: " + str(e))
        traceback.print_exc()
        await ctx.send("Error: " + str(e))

# =========================
# KILL  !k
# =========================

@bot.command(name="k", aliases=["kill", "mata"])
async def kill(ctx, name: str):
    """Registrar kill: !k NombreBoss"""
    try:
        boss_name, data = find_boss(name)
        if not boss_name:
            await ctx.send(f"**{name}** no existe.")
            return

        now        = now_ar()
        respawn    = data["respawn"]
        next_spawn = now + timedelta(minutes=respawn)

        with data_lock:
            bosses[boss_name]["last_kill"]     = now
            bosses[boss_name]["next_spawn"]    = next_spawn
            bosses[boss_name]["kills_count"]  += 1
            bosses[boss_name]["killer_id"]     = ctx.author.id
            bosses[boss_name]["killer_tagged"] = False  # resetear: proxima alerta lo tagea
            bosses[boss_name]["missed_spawns"] = 0
            alerted_warning.discard(boss_name)
            spawn_announced.discard(boss_name)
            save_bosses()

        hora      = next_spawn.strftime("%H:%M:%S")
        threshold = warning_threshold(respawn)

        embed = discord.Embed(
            title=f"{boss_name} eliminado",
            description=f"Proximo spawn: **{hora}** AR",
            color=discord.Color.red()
        )
        embed.add_field(name="Respawn",       value=f"{respawn} min",                   inline=True)
        embed.add_field(name="Kills totales", value=str(data["kills_count"] + 1),        inline=True)
        embed.add_field(name="Matado por",    value=ctx.author.mention,                  inline=True)
        embed.add_field(name="Aviso previo",  value=f"{threshold} min antes del spawn",  inline=True)

        if data.get("image_url"):
            embed.set_thumbnail(url=data["image_url"])

        await ctx.send(embed=embed)
    except Exception as e:
        print("Error en !k: " + str(e))
        traceback.print_exc()
        await ctx.send("Error: " + str(e))

# =========================
# LIST  !list
# =========================

@bot.command(name="list", aliases=["bosses", "timers"])
async def bosseslist(ctx):
    """Ver todos los bosses: !list"""
    try:
        if not bosses:
            await ctx.send("No hay bosses registrados.")
            return

        now        = now_ar()
        boss_items = list(bosses.items())
        chunks     = [boss_items[i:i+10] for i in range(0, len(boss_items), 10)]

        for idx, chunk in enumerate(chunks):
            title = "BOSS TIMERS"
            if len(chunks) > 1:
                title += f" ({idx+1}/{len(chunks)})"

            embed = discord.Embed(title=title, color=discord.Color.gold())

            for bname, data in chunk:
                if data["next_spawn"]:
                    remaining = (data["next_spawn"] - now).total_seconds()
                    if remaining < 0:
                        estado = f">> LIVE â€” hace {format_remaining(remaining)}"
                    else:
                        estado = f"En {format_remaining(remaining)} ({data['next_spawn'].strftime('%H:%M')} AR)"
                else:
                    estado = "Sin registrar"

                missed   = data.get("missed_spawns", 0)
                extra    = f" | +{missed} spawn(s) sin matar" if missed > 0 else ""
                img_line = f"\n[imagen]({data['image_url']})" if data.get("image_url") else ""

                embed.add_field(
                    name=bname,
                    value=f"{estado}\nKills: {data['kills_count']}{extra}{img_line}",
                    inline=False
                )

            for _, data in chunk:
                if data.get("image_url"):
                    embed.set_thumbnail(url=data["image_url"])
                    break

            await ctx.send(embed=embed)
    except Exception as e:
        print("Error en !list: " + str(e))
        traceback.print_exc()
        await ctx.send("Error: " + str(e))

# =========================
# INFO  !info
# =========================

@bot.command(name="info")
async def boss_info(ctx, name: str):
    """Ver info detallada: !info NombreBoss"""
    try:
        boss_name, data = find_boss(name)
        if not boss_name:
            await ctx.send(f"**{name}** no existe.")
            return

        now   = now_ar()
        embed = discord.Embed(title=boss_name, color=discord.Color.blue())

        if data["next_spawn"]:
            remaining = (data["next_spawn"] - now).total_seconds()
            spawn_str = data["next_spawn"].strftime("%H:%M:%S")
            if remaining < 0:
                timer_val = f"LIVE â€” hace {format_remaining(remaining)} ({spawn_str} AR)"
            else:
                timer_val = f"En {format_remaining(remaining)} ({spawn_str} AR)"
            embed.add_field(name="Proximo spawn", value=timer_val, inline=False)

        if data["last_kill"]:
            embed.add_field(
                name="Ultimo kill",
                value=data["last_kill"].strftime("%H:%M:%S") + " AR",
                inline=True
            )

        embed.add_field(name="Respawn base",  value=f"{data['respawn']} min",                      inline=True)
        embed.add_field(name="Kills totales", value=str(data["kills_count"]),                       inline=True)
        embed.add_field(name="Aviso previo",  value=f"{warning_threshold(data['respawn'])} min antes", inline=True)

        missed = data.get("missed_spawns", 0)
        if missed > 0:
            embed.add_field(name="Spawns sin matar", value=str(missed), inline=True)

        if data.get("killer_id"):
            embed.add_field(name="Ultimo matador", value=f"<@{data['killer_id']}>", inline=True)

        if data.get("drops"):
            embed.add_field(name="Drops",     value=data["drops"],      inline=False)
        if data.get("location"):
            embed.add_field(name="Ubicacion", value=data["location"],   inline=False)
        if data.get("extra_info"):
            embed.add_field(name="Notas",     value=data["extra_info"], inline=False)

        if data.get("image_url"):
            embed.set_image(url=data["image_url"])

        await ctx.send(embed=embed)
    except Exception as e:
        print("Error en !info: " + str(e))
        traceback.print_exc()
        await ctx.send("Error: " + str(e))

# =========================
# IMG  !img
# =========================

@bot.command(name="img", aliases=["image"])
async def set_image(ctx, name: str, image_url: str):
    """Actualizar imagen: !img NombreBoss URL"""
    try:
        boss_name, _ = find_boss(name)
        if not boss_name:
            await ctx.send(f"**{name}** no existe.")
            return

        with data_lock:
            bosses[boss_name]["image_url"] = image_url
            save_bosses()

        await ctx.send(f"Imagen de **{boss_name}** actualizada.")
    except Exception as e:
        print("Error en !img: " + str(e))
        traceback.print_exc()
        await ctx.send("Error: " + str(e))

# =========================
# RESET  !reset
# =========================

@bot.command(name="reset")
async def reset_boss(ctx, name: str):
    """Reiniciar timer sin contar kill: !reset NombreBoss"""
    try:
        boss_name, _ = find_boss(name)
        if not boss_name:
            await ctx.send(f"**{name}** no existe.")
            return

        with data_lock:
            bosses[boss_name]["last_kill"]     = None
            bosses[boss_name]["next_spawn"]    = None
            bosses[boss_name]["killer_id"]     = None
            bosses[boss_name]["killer_tagged"] = False
            bosses[boss_name]["missed_spawns"] = 0
            alerted_warning.discard(boss_name)
            spawn_announced.discard(boss_name)
            save_bosses()

        await ctx.send(f"Timer de **{boss_name}** reiniciado.")
    except Exception as e:
        print("Error en !reset: " + str(e))
        traceback.print_exc()
        await ctx.send("Error: " + str(e))

# =========================
# ALERTAS AUTOMATICAS
# =========================

async def boss_checker():
    await bot.wait_until_ready()

    channel = await get_alert_channel()
    if channel:
        print(f"Canal de alertas: #{channel.name}")
    else:
        print("No se encontro canal para alertas")

    while not bot.is_closed():
        now     = now_ar()
        channel = await get_alert_channel()

        if channel:
            guild        = channel.guild
            alert_role   = await get_alert_role(guild)
            role_mention = alert_role.mention if alert_role else f"@{ALERT_ROLE_NAME}"

            for name, data in list(bosses.items()):
                if not data["next_spawn"]:
                    continue

                remaining = (data["next_spawn"] - now).total_seconds()
                threshold = warning_threshold(data["respawn"]) * 60  # en segundos

                # â”€â”€ ALERTA PREVIA (proporcional al respawn) â”€â”€
                if 0 < remaining <= threshold and name not in alerted_warning:

                    # Tagear al killer solo si todavia no fue tageado en este ciclo
                    killer_mention = ""
                    if data.get("killer_id") and not data.get("killer_tagged"):
                        killer_mention = f"<@{data['killer_id']}>"

                    mentions  = " ".join(filter(None, [role_mention, killer_mention]))
                    mins_left = max(1, int(remaining // 60) + 1)

                    embed = discord.Embed(
                        title=f"{name} â€” en {mins_left} min",
                        description=(
                            f"Spawn a las **{data['next_spawn'].strftime('%H:%M:%S')}** AR\n"
                            f"Preparate para ir a la zona!"
                        ),
                        color=discord.Color.orange()
                    )
                    if data.get("image_url"):
                        embed.set_thumbnail(url=data["image_url"])
                    if data.get("location"):
                        embed.add_field(name="Ubicacion", value=data["location"], inline=False)

                    try:
                        await channel.send(content=mentions, embed=embed)
                        with data_lock:
                            bosses[name]["killer_tagged"] = True
                            save_bosses()
                    except Exception as e:
                        print(f"Error alerta previa {name}: {e}")

                    alerted_warning.add(name)

                # â”€â”€ ALERTA SPAWN / LIVE â”€â”€
                elif remaining <= 0 and name not in spawn_announced:

                    # Killer NO se tagea aqui (ya fue tageado en la alerta previa)
                    mentions = role_mention

                    missed = data.get("missed_spawns", 0)

                    if missed > 0:
                        delay_extra = missed + 1
                        estimated   = data["next_spawn"] + timedelta(minutes=delay_extra)
                        desc = (
                            f"Spawn perdido #{missed + 1}\n"
                            f"Proximo estimado: **{estimated.strftime('%H:%M:%S')}** AR "
                            f"(+{delay_extra} min de delay)"
                        )
                        color = discord.Color.dark_red()
                        title = f"BOSS PERDIDO â€” {name}"
                    else:
                        desc  = f"Hora de spawn: **{data['next_spawn'].strftime('%H:%M:%S')}** AR"
                        color = discord.Color.green()
                        title = f"** {name} â€” LIVE **"

                    embed = discord.Embed(title=title, description=desc, color=color)
                    if data.get("image_url"):
                        embed.set_image(url=data["image_url"])
                    if data.get("location"):
                        embed.add_field(name="Ubicacion", value=data["location"], inline=False)

                    try:
                        await channel.send(content=mentions, embed=embed)
                    except Exception as e:
                        print(f"Error alerta spawn {name}: {e}")

                    with data_lock:
                        bosses[name]["missed_spawns"] = missed + 1
                        delay_next = missed + 2
                        bosses[name]["next_spawn"] = (
                            data["next_spawn"] + timedelta(minutes=delay_next)
                        )
                        alerted_warning.discard(name)
                        save_bosses()

                    spawn_announced.add(name)

                # Limpiar cuando el timer ya avanzo al siguiente ciclo
                elif remaining > threshold and name in spawn_announced:
                    spawn_announced.discard(name)

        await asyncio.sleep(10)

# =========================
# UTILIDADES
# =========================

@bot.command()
async def ping(ctx):
    try:
        await ctx.send(f"Pong! {round(bot.latency * 1000)}ms")
    except Exception as e:
        await ctx.send("Error: " + str(e))

@bot.command(name="commands", aliases=["cmds", "c", "ayuda"])
async def commands_list(ctx):
    try:
        embed = discord.Embed(title="Comandos disponibles", color=discord.Color.blurple())
        cmds = [
            ("!add <Boss> <min> [URL]",               "Agregar un boss"),
            ("!remove <Boss>",                         "Eliminar un boss"),
            ("!edit <Boss> <min>",                     "Cambiar tiempo de respawn"),
            ("!setinfo <Boss> drops:X lugar:Y nota:Z", "Agregar info extra al boss"),
            ("!k / !kill / !mata <Boss>",              "Registrar kill"),
            ("!reset <Boss>",                          "Reiniciar timer sin contar kill"),
            ("!list / !bosses / !timers",              "Ver todos los timers"),
            ("!info <Boss>",                           "Info detallada del boss"),
            ("!img <Boss> <URL>",                      "Actualizar imagen del boss"),
            ("!setchannel",                            "Fijar canal de alertas (usarlo en el canal deseado)"),
            ("!ping",                                  "Ver latencia del bot"),
            ("!commands / !cmds / !ayuda",             "Ver este mensaje"),
        ]
        for cmd, desc in cmds:
            embed.add_field(name=f"`{cmd}`", value=desc, inline=False)

        embed.set_footer(text=(
            f"TZ: Argentina (ART) | Rol: @{ALERT_ROLE_NAME} | "
            "Avisos: <10min->1min | 10-30min->3min | 30-60min->5min | "
            "1-4h->10min | 4-12h->20min | >12h->30min"
        ))
        await ctx.send(embed=embed)
    except Exception as e:
        print("Error en !commands: " + str(e))
        await ctx.send("Error: " + str(e))

# =========================
# INICIAR
# =========================

if __name__ == "__main__":
    try:
        print("Iniciando bot...")
        flask_thread = Thread(target=run_flask, daemon=True)
        flask_thread.start()
        print("Servidor Flask iniciado")
        bot.run(TOKEN)
    except Exception as e:
        print("Error fatal: " + str(e))
        traceback.print_exc() 
