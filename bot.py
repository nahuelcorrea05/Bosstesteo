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

TOKEN = os.getenv("DISCORD_TOKEN")
PORT = int(os.getenv("PORT", 10000))
ALERT_ROLE_NAME = os.getenv("ALERT_ROLE", "cazador de bosses")  # nombre del rol a mencionar

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

bosses = {}
SAVE_FILE = "bosses.json"
data_lock = threading.Lock()

alerted_one_minute = set()   # boss_name -> set
spawn_announced = set()

# =========================
# GUARDAR / CARGAR
# =========================

def save_bosses():
    try:
        data = {}
        for name, boss in bosses.items():
            data[name] = {
                "respawn": boss["respawn"],
                "last_kill": (
                    boss["last_kill"].isoformat() if boss["last_kill"] else None
                ),
                "next_spawn": (
                    boss["next_spawn"].isoformat() if boss["next_spawn"] else None
                ),
                "image_url": boss.get("image_url"),
                "kills_count": boss.get("kills_count", 0),
                "killer_id": boss.get("killer_id"),        # ID del usuario que usÃ³ !k
                "missed_spawns": boss.get("missed_spawns", 0),  # cuÃ¡ntos spawns sin !k
                "drops": boss.get("drops", ""),
                "location": boss.get("location", ""),
                "extra_info": boss.get("extra_info", ""),
            }
        with open(SAVE_FILE, "w") as f:
            json.dump(data, f, indent=4)
        print(f"âœ… Bosses guardados en {SAVE_FILE}")
    except Exception as e:
        print(f"âŒ Error al guardar bosses: {e}")
        traceback.print_exc()

def load_bosses():
    try:
        if not os.path.exists(SAVE_FILE):
            print(f"âš ï¸ {SAVE_FILE} no existe, creando archivo vacÃ­o")
            save_bosses()
            return

        with open(SAVE_FILE, "r") as f:
            data = json.load(f)

        for name, boss in data.items():
            def parse_dt(val):
                if not val:
                    return None
                dt = datetime.fromisoformat(val)
                # Si no tiene tzinfo, asumimos que fue guardado en AR_TZ (migraciÃ³n)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=AR_TZ)
                return dt

            bosses[name] = {
                "respawn": boss["respawn"],
                "last_kill": parse_dt(boss.get("last_kill")),
                "next_spawn": parse_dt(boss.get("next_spawn")),
                "image_url": boss.get("image_url"),
                "kills_count": boss.get("kills_count", 0),
                "killer_id": boss.get("killer_id"),
                "missed_spawns": boss.get("missed_spawns", 0),
                "drops": boss.get("drops", ""),
                "location": boss.get("location", ""),
                "extra_info": boss.get("extra_info", ""),
            }
        print(f"âœ… {len(bosses)} bosses cargados")
    except Exception as e:
        print(f"âŒ Error al cargar bosses: {e}")
        traceback.print_exc()

# =========================
# HELPERS
# =========================

def find_boss(name: str):
    """Busca un boss por nombre (case-insensitive). Retorna (key, data) o (None, None)."""
    for k, v in bosses.items():
        if k.lower() == name.lower():
            return k, v
    return None, None

def format_remaining(seconds: float) -> str:
    """Formatea segundos en Xm Ys."""
    seconds = abs(int(seconds))
    mins = seconds // 60
    secs = seconds % 60
    return f"{mins}m {secs}s"

async def get_alert_role(guild: discord.Guild) -> discord.Role | None:
    return discord.utils.get(guild.roles, name=ALERT_ROLE_NAME)

# =========================
# BOT READY
# =========================

@bot.event
async def on_ready():
    try:
        load_bosses()
        print(f"âœ… Conectado como {bot.user}")
        bot.loop.create_task(boss_checker())
    except Exception as e:
        print(f"âŒ Error en on_ready: {e}")
        traceback.print_exc()

# =========================
# AGREGAR BOSS  !add
# =========================

@bot.command(name="add")
async def addboss(ctx, name: str, respawn: int, image_url: str = None):
    """Agregar un boss: !add NombreBoss 20 [URL_imagen]"""
    try:
        if find_boss(name)[0]:
            await ctx.send(f"âŒ **{name}** ya existe. UsÃ¡ `!edit {name} <minutos>` para cambiar el respawn.")
            return

        with data_lock:
            bosses[name] = {
                "respawn": respawn,
                "last_kill": None,
                "next_spawn": None,
                "image_url": image_url,
                "kills_count": 0,
                "killer_id": None,
                "missed_spawns": 0,
                "drops": "",
                "location": "",
                "extra_info": "",
            }
            save_bosses()

        msg = f"âœ… **{name}** agregado con respawn de **{respawn} min**"
        if image_url:
            msg += "\nðŸ–¼ï¸ Imagen configurada"
        await ctx.send(msg)
    except Exception as e:
        print(f"âŒ Error en !add: {e}")
        traceback.print_exc()
        await ctx.send(f"âŒ Error: {str(e)}")

# =========================
# ELIMINAR BOSS  !remove
# =========================

@bot.command(name="remove", aliases=["delete", "del", "rm"])
async def remove_boss(ctx, name: str):
    """Eliminar un boss: !remove NombreBoss"""
    try:
        boss_name, _ = find_boss(name)
        if not boss_name:
            await ctx.send(f"âŒ **{name}** no existe")
            return

        with data_lock:
            del bosses[boss_name]
            alerted_one_minute.discard(boss_name)
            spawn_announced.discard(boss_name)
            save_bosses()

        await ctx.send(f"ðŸ—‘ï¸ **{boss_name}** eliminado correctamente")
    except Exception as e:
        print(f"âŒ Error en !remove: {e}")
        traceback.print_exc()
        await ctx.send(f"âŒ Error: {str(e)}")

# =========================
# EDITAR RESPAWN  !edit
# =========================

@bot.command(name="edit")
async def edit_boss(ctx, name: str, respawn: int):
    """Cambiar el tiempo de respawn: !edit NombreBoss 25"""
    try:
        boss_name, data = find_boss(name)
        if not boss_name:
            await ctx.send(f"âŒ **{name}** no existe")
            return

        old = data["respawn"]
        with data_lock:
            bosses[boss_name]["respawn"] = respawn
            # Recalcular next_spawn si hay un kill registrado
            if bosses[boss_name]["last_kill"]:
                bosses[boss_name]["next_spawn"] = (
                    bosses[boss_name]["last_kill"] + timedelta(minutes=respawn)
                )
                alerted_one_minute.discard(boss_name)
                spawn_announced.discard(boss_name)
            save_bosses()

        await ctx.send(
            f"âœï¸ **{boss_name}**: respawn actualizado de **{old} min** â†’ **{respawn} min**"
        )
    except Exception as e:
        print(f"âŒ Error en !edit: {e}")
        traceback.print_exc()
        await ctx.send(f"âŒ Error: {str(e)}")

# =========================
# INFO EXTRA  !setinfo
# =========================

@bot.command(name="setinfo")
async def set_info(ctx, name: str, *, args: str):
    """
    Agregar info extra al boss.
    Uso: !setinfo Boss drops:Espada,Escudo lugar:Cueva nota:Aparece de noche
    Claves vÃ¡lidas: drops, lugar, nota
    """
    try:
        boss_name, _ = find_boss(name)
        if not boss_name:
            await ctx.send(f"âŒ **{name}** no existe")
            return

        # Parsear clave:valor (permite espacios en el valor si no hay siguiente clave)
        pattern = r'(drops|lugar|nota):(.+?)(?=\s+(?:drops|lugar|nota):|$)'
        matches = re.findall(pattern, args, re.IGNORECASE)

        if not matches:
            await ctx.send(
                "âŒ Formato incorrecto. Ejemplo:\n"
                "`!setinfo Boss drops:Espada,Escudo lugar:Cueva nota:Aparece de noche`"
            )
            return

        changes = []
        with data_lock:
            for key, value in matches:
                value = value.strip()
                key = key.lower()
                if key == "drops":
                    bosses[boss_name]["drops"] = value
                    changes.append(f"**Drops:** {value}")
                elif key == "lugar":
                    bosses[boss_name]["location"] = value
                    changes.append(f"**Lugar:** {value}")
                elif key == "nota":
                    bosses[boss_name]["extra_info"] = value
                    changes.append(f"**Nota:** {value}")
            save_bosses()

        embed = discord.Embed(
            title=f"ðŸ“ Info de {boss_name} actualizada",
            description="\n".join(changes),
            color=discord.Color.blurple()
        )
        await ctx.send(embed=embed)
    except Exception as e:
        print(f"âŒ Error en !setinfo: {e}")
        traceback.print_exc()
        await ctx.send(f"âŒ Error: {str(e)}")

# =========================
# KILL  !k
# =========================

@bot.command(name="k", aliases=["kill", "mata"])
async def kill(ctx, name: str):
    """Matar un boss: !k NombreBoss"""
    try:
        boss_name, data = find_boss(name)
        if not boss_name:
            await ctx.send(f"âŒ **{name}** no existe")
            return

        now = now_ar()
        respawn = data["respawn"]
        next_spawn = now + timedelta(minutes=respawn)

        with data_lock:
            bosses[boss_name]["last_kill"] = now
            bosses[boss_name]["next_spawn"] = next_spawn
            bosses[boss_name]["kills_count"] += 1
            bosses[boss_name]["killer_id"] = ctx.author.id
            bosses[boss_name]["missed_spawns"] = 0  # reset al matar
            alerted_one_minute.discard(boss_name)
            spawn_announced.discard(boss_name)
            save_bosses()

        hora = next_spawn.strftime("%H:%M:%S")

        embed = discord.Embed(
            title=f"â˜ ï¸ {boss_name} DERROTADO",
            description=f"PrÃ³ximo spawn: **{hora}** (AR)",
            color=discord.Color.red()
        )
        embed.add_field(name="Respawn", value=f"{respawn} min", inline=True)
        embed.add_field(name="Kills totales", value=str(data["kills_count"] + 1), inline=True)
        embed.add_field(name="Matado por", value=ctx.author.mention, inline=True)

        if data.get("image_url"):
            embed.set_thumbnail(url=data["image_url"])

        await ctx.send(embed=embed)
    except Exception as e:
        print(f"âŒ Error en !k: {e}")
        traceback.print_exc()
        await ctx.send(f"âŒ Error: {str(e)}")

# =========================
# LISTA  !list
# =========================

@bot.command(name="list", aliases=["bosses", "timers"])
async def bosseslist(ctx):
    """Ver todos los bosses: !list"""
    try:
        if not bosses:
            await ctx.send("âŒ No hay bosses registrados")
            return

        now = now_ar()

        # Discord permite mÃ¡x 25 fields por embed; si hay mÃ¡s de 12 bosses
        # dividimos en varios embeds
        boss_items = list(bosses.items())
        chunks = [boss_items[i:i+12] for i in range(0, len(boss_items), 12)]

        for idx, chunk in enumerate(chunks):
            embed = discord.Embed(
                title="ðŸ“‹ BOSS TIMERS" + (f" (pÃ¡g {idx+1}/{len(chunks)})" if len(chunks) > 1 else ""),
                color=discord.Color.gold()
            )

            for name, data in chunk:
                if data["next_spawn"]:
                    remaining = (data["next_spawn"] - now).total_seconds()
                    if remaining < 0:
                        mins_late = format_remaining(remaining)
                        estado = f"ðŸ”¥ **SPAWN NOW** (hace {mins_late})"
                    else:
                        estado = f"â±ï¸ {format_remaining(remaining)}"
                else:
                    estado = "â“ Sin registrar"

                missed = data.get("missed_spawns", 0)
                missed_txt = f" âš ï¸ +{missed} spawn(s) sin matar" if missed > 0 else ""

                # thumbnail solo funciona en el primer field del embed,
                # asÃ­ que mostramos la URL como texto compacto si hay imagen
                img_line = ""
                if data.get("image_url"):
                    img_line = f"\nðŸ–¼ï¸ [ver imagen]({data['image_url']})"

                embed.add_field(
                    name=name,
                    value=f"{estado}\nðŸ’€ Kills: {data['kills_count']}{missed_txt}{img_line}",
                    inline=False
                )

            # Thumbnail del primer boss del chunk que tenga imagen
            for _, data in chunk:
                if data.get("image_url"):
                    embed.set_thumbnail(url=data["image_url"])
                    break

            await ctx.send(embed=embed)
    except Exception as e:
        print(f"âŒ Error en !list: {e}")
        traceback.print_exc()
        await ctx.send(f"âŒ Error: {str(e)}")

# =========================
# INFO DETALLADA  !info
# =========================

@bot.command(name="info")
async def boss_info(ctx, name: str):
    """Ver info detallada: !info NombreBoss"""
    try:
        boss_name, data = find_boss(name)
        if not boss_name:
            await ctx.send(f"âŒ **{name}** no existe")
            return

        now = now_ar()

        embed = discord.Embed(
            title=f"ðŸ“Š {boss_name}",
            color=discord.Color.blue()
        )

        # Timer
        if data["next_spawn"]:
            remaining = (data["next_spawn"] - now).total_seconds()
            spawn_str = data["next_spawn"].strftime("%H:%M:%S")
            if remaining < 0:
                timer_val = f"ðŸ”¥ DeberÃ­a haber spawneado hace {format_remaining(remaining)}\n({spawn_str} AR)"
            else:
                timer_val = f"â±ï¸ En {format_remaining(remaining)}\n({spawn_str} AR)"
            embed.add_field(name="PrÃ³ximo spawn", value=timer_val, inline=False)

        if data["last_kill"]:
            embed.add_field(
                name="Ãšltimo kill",
                value=data["last_kill"].strftime("%H:%M:%S") + " AR",
                inline=True
            )

        embed.add_field(name="Respawn base", value=f"{data['respawn']} min", inline=True)
        embed.add_field(name="Kills totales", value=str(data["kills_count"]), inline=True)

        missed = data.get("missed_spawns", 0)
        if missed > 0:
            embed.add_field(name="Spawns sin matar", value=str(missed), inline=True)

        if data.get("killer_id"):
            embed.add_field(name="Ãšltimo matador", value=f"<@{data['killer_id']}>", inline=True)

        # Info extra
        if data.get("drops"):
            embed.add_field(name="ðŸŽ Drops", value=data["drops"], inline=False)

        if data.get("location"):
            embed.add_field(name="ðŸ“ UbicaciÃ³n / Spawn", value=data["location"], inline=False)

        if data.get("extra_info"):
            embed.add_field(name="ðŸ“Œ Notas", value=data["extra_info"], inline=False)

        if data.get("image_url"):
            embed.set_image(url=data["image_url"])

        await ctx.send(embed=embed)
    except Exception as e:
        print(f"âŒ Error en !info: {e}")
        traceback.print_exc()
        await ctx.send(f"âŒ Error: {str(e)}")

# =========================
# IMAGEN  !img
# =========================

@bot.command(name="img", aliases=["image"])
async def set_image(ctx, name: str, image_url: str):
    """Actualizar imagen: !img NombreBoss URL"""
    try:
        boss_name, _ = find_boss(name)
        if not boss_name:
            await ctx.send(f"âŒ **{name}** no existe")
            return

        with data_lock:
            bosses[boss_name]["image_url"] = image_url
            save_bosses()

        await ctx.send(f"âœ… Imagen de **{boss_name}** actualizada")
    except Exception as e:
        print(f"âŒ Error en !img: {e}")
        traceback.print_exc()
        await ctx.send(f"âŒ Error: {str(e)}")

# =========================
# RESET TIMER  !reset
# =========================

@bot.command(name="reset")
async def reset_boss(ctx, name: str):
    """Reiniciar el timer de un boss sin contar kill: !reset NombreBoss"""
    try:
        boss_name, _ = find_boss(name)
        if not boss_name:
            await ctx.send(f"âŒ **{name}** no existe")
            return

        with data_lock:
            bosses[boss_name]["last_kill"] = None
            bosses[boss_name]["next_spawn"] = None
            bosses[boss_name]["killer_id"] = None
            bosses[boss_name]["missed_spawns"] = 0
            alerted_one_minute.discard(boss_name)
            spawn_announced.discard(boss_name)
            save_bosses()

        await ctx.send(f"ðŸ”„ Timer de **{boss_name}** reiniciado")
    except Exception as e:
        print(f"âŒ Error en !reset: {e}")
        traceback.print_exc()
        await ctx.send(f"âŒ Error: {str(e)}")

# =========================
# ALERTAS AUTOMÃTICAS
# =========================

async def boss_checker():
    await bot.wait_until_ready()

    channel = None
    for guild in bot.guilds:
        for ch in guild.text_channels:
            if ch.permissions_for(guild.me).send_messages:
                channel = ch
                print(f"âœ… Canal de alertas: #{ch.name} en {guild.name}")
                break
        if channel:
            break

    if not channel:
        print("âš ï¸ No se encontrÃ³ canal para alertas")

    while not bot.is_closed():
        now = now_ar()

        if channel:
            guild = channel.guild
            alert_role = await get_alert_role(guild)
            role_mention = alert_role.mention if alert_role else f"@{ALERT_ROLE_NAME}"

            for name, data in list(bosses.items()):
                if not data["next_spawn"]:
                    continue

                remaining = (data["next_spawn"] - now).total_seconds()

                # â”€â”€ ALERTA 1 MINUTO â”€â”€
                if 0 < remaining <= 60 and name not in alerted_one_minute:
                    killer_mention = (
                        f"<@{data['killer_id']}>" if data.get("killer_id") else ""
                    )
                    mentions = " ".join(filter(None, [role_mention, killer_mention]))

                    embed = discord.Embed(
                        title=f"âš ï¸ {name} aparece en 1 minuto",
                        description=f"Spawn a las **{data['next_spawn'].strftime('%H:%M:%S')}** AR",
                        color=discord.Color.orange()
                    )
                    if data.get("image_url"):
                        embed.set_thumbnail(url=data["image_url"])
                    if data.get("location"):
                        embed.add_field(name="ðŸ“ UbicaciÃ³n", value=data["location"], inline=False)

                    try:
                        await channel.send(content=mentions, embed=embed)
                    except Exception as e:
                        print(f"âŒ Error alerta 1min {name}: {e}")

                    alerted_one_minute.add(name)

                # â”€â”€ ALERTA SPAWN â”€â”€
                elif remaining <= 0 and name not in spawn_announced:
                    killer_mention = (
                        f"<@{data['killer_id']}>" if data.get("killer_id") else ""
                    )
                    mentions = " ".join(filter(None, [role_mention, killer_mention]))

                    missed = data.get("missed_spawns", 0)
                    if missed > 0:
                        # Spawn perdido: el boss ya spawneÃ³ antes sin que nadie lo matara
                        # Calculamos el prÃ³ximo respawn aproximado (+1 min por spawn perdido)
                        delay_extra = missed + 1  # +1 min por cada spawn perdido
                        estimated = data["next_spawn"] + timedelta(minutes=delay_extra)
                        desc = (
                            f"ðŸ”´ Spawn perdido #{missed + 1}\n"
                            f"PrÃ³x. estimado: **{estimated.strftime('%H:%M:%S')}** AR "
                            f"(+{delay_extra} min de delay)"
                        )
                        color = discord.Color.dark_red()
                        title = f"â° {name} â€” spawn perdido"
                    else:
                        desc = f"Hora: **{data['next_spawn'].strftime('%H:%M:%S')}** AR"
                        color = discord.Color.red()
                        title = f"ðŸ”¥ {name} deberÃ­a haber spawneado"

                    embed = discord.Embed(title=title, description=desc, color=color)
                    if data.get("image_url"):
                        embed.set_image(url=data["image_url"])
                    if data.get("location"):
                        embed.add_field(name="ðŸ“ UbicaciÃ³n", value=data["location"], inline=False)

                    try:
                        await channel.send(content=mentions, embed=embed)
                    except Exception as e:
                        print(f"âŒ Error alerta spawn {name}: {e}")

                    # Incrementar missed_spawns y programar siguiente ciclo (+1 min de delay)
                    with data_lock:
                        bosses[name]["missed_spawns"] = missed + 1
                        delay_next = missed + 2  # cada spawn perdido adicional +1 min mÃ¡s
                        bosses[name]["next_spawn"] = (
                            data["next_spawn"] + timedelta(minutes=delay_next)
                        )
                        alerted_one_minute.discard(name)
                        # NO descartamos spawn_announced aquÃ­; lo hacemos despuÃ©s de sleep
                        save_bosses()

                    spawn_announced.add(name)

                # Si ya se anunciÃ³ el spawn y el timer avanzÃ³ (siguiente ciclo), limpiar
                elif remaining > 60 and name in spawn_announced:
                    spawn_announced.discard(name)

        await asyncio.sleep(10)

# =========================
# UTILIDADES
# =========================

@bot.command()
async def ping(ctx):
    try:
        await ctx.send(f"ðŸ“ Pong! {round(bot.latency * 1000)}ms")
    except Exception as e:
        await ctx.send(f"âŒ Error: {str(e)}")

@bot.command(name="commands", aliases=["cmds", "c", "ayuda"])
async def commands_list(ctx):
    """Ver todos los comandos"""
    try:
        embed = discord.Embed(
            title="ðŸ“– Comandos disponibles",
            color=discord.Color.blurple()
        )

        cmds = [
            ("!add <Boss> <min> [URL]",         "Agregar un boss"),
            ("!remove <Boss>",                   "Eliminar un boss"),
            ("!edit <Boss> <min>",               "Cambiar tiempo de respawn"),
            ("!setinfo <Boss> drops:X lugar:Y nota:Z", "Agregar info extra al boss"),
            ("!k / !kill / !mata <Boss>",        "Registrar kill"),
            ("!reset <Boss>",                    "Reiniciar timer sin contar kill"),
            ("!list / !bosses / !timers",        "Ver todos los timers"),
            ("!info <Boss>",                     "Info detallada del boss"),
            ("!img <Boss> <URL>",                "Actualizar imagen del boss"),
            ("!ping",                            "Ver latencia del bot"),
            ("!commands / !cmds / !ayuda",       "Ver este mensaje"),
        ]

        for cmd, desc in cmds:
            embed.add_field(name=f"`{cmd}`", value=desc, inline=False)

        embed.set_footer(text=f"Zona horaria: Argentina (ART) | Rol de alerta: @{ALERT_ROLE_NAME}")
        await ctx.send(embed=embed)
    except Exception as e:
        print(f"âŒ Error en !commands: {e}")
        await ctx.send(f"âŒ Error: {str(e)}")

# =========================
# INICIAR
# =========================

if __name__ == "__main__":
    try:
        print("ðŸš€ Iniciando bot...")
        flask_thread = Thread(target=run_flask, daemon=True)
        flask_thread.start()
        print("âœ… Servidor Flask iniciado")
        bot.run(TOKEN)
    except Exception as e:
        print(f"âŒ Error fatal: {e}")
        traceback.print_exc()
