import discord
from discord.ext import commands
from discord import app_commands
import asyncio
import datetime
import json
import sqlite3
import time
import re
import unicodedata
import requests
import difflib
import logging
import os

# --- CONFIGURACIÓN ---
CONFIG_FILE = "config.jsonc"

def cargar_config():
    defaults = {
        "token": "",
        "prefix": "!",
        "log_level": "INFO",
        "bot": {
            "nombre": "Sentinel",
            "status_type": "watching",
            "status_text": "el chat",
            "presence": "online"
        },
        "antispam": {
            "ventana_segundos": 4,
            "limite_mensajes": 4,
            "timeout_minutos": 1
        },
        "badwords": {
            "umbral_alto": 90,
            "umbral_medio": 70,
            "umbral_fuzzy": 90,
            "min_fuzzy": 8,
            "min_substring": 5
        },
        "mensajes": {
            "delete_after_alerta": 5,
            "delete_after_aviso": 6
        },
        "ai": {
            "api_key": "",
            "modelos": [
                {"nombre": "Free", "model": "openrouter/free"},
            ],
            "max_historial": 10,
            "max_tokens": 1024,
            "reasoning": True
        }
    }
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                contenido = f.read()
            # Eliminar comentarios // de líneas completas
            lineas = [l for l in contenido.splitlines() if not l.strip().startswith("//")]
            cfg = json.loads("\n".join(lineas))
            for key, val in defaults.items():
                if key not in cfg:
                    cfg[key] = val
                elif isinstance(val, dict):
                    for k, v in val.items():
                        if k not in cfg[key]:
                            cfg[key][k] = v
            return cfg
        except Exception as e:
            print(f"Error cargando config: {e} — usando valores por defecto")
    else:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(defaults, f, indent=4, ensure_ascii=False)
    return defaults

cfg = cargar_config()

# --- LOGGING ---
os.makedirs("logs", exist_ok=True)

logging.basicConfig(
    level=getattr(logging, cfg["log_level"].upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler("logs/sentinel.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger("sentinel")

# Permisos obligatorios
intents = discord.Intents.default()
intents.message_content = True
intents.members = True 

bot = commands.Bot(command_prefix=cfg["prefix"], intents=intents)

DB_FILE = "sentinel.db"
user_message_history = {}

# --- GESTIÓN DE BASE DE DATOS ---
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS autoprofile (
        guild_id TEXT PRIMARY KEY,
        channel_id INTEGER NOT NULL
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS badwords (
        guild_id TEXT NOT NULL,
        word TEXT NOT NULL,
        PRIMARY KEY (guild_id, word)
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS allowed_roles (
        guild_id TEXT NOT NULL,
        role_id INTEGER NOT NULL,
        PRIMARY KEY (guild_id, role_id)
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS spam_history (
        guild_id TEXT NOT NULL,
        user_id INTEGER NOT NULL,
        message_id INTEGER NOT NULL,
        channel_id INTEGER NOT NULL,
        timestamp REAL NOT NULL,
        PRIMARY KEY (guild_id, user_id, message_id)
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS ai_history (
        user_id INTEGER NOT NULL,
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        reasoning_details TEXT,
        created_at REAL NOT NULL
    )''')
    conn.commit()

    # Migración: agregar columna reasoning_details si no existe
    try:
        c.execute("ALTER TABLE ai_history ADD COLUMN reasoning_details TEXT")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # Columna ya existe

    conn.close()
    log.info("Base de datos inicializada: %s", DB_FILE)

def db_get_autoprofile(guild_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT channel_id FROM autoprofile WHERE guild_id = ?", (guild_id,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None

def db_set_autoprofile(guild_id, channel_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO autoprofile (guild_id, channel_id) VALUES (?, ?)", (guild_id, channel_id))
    conn.commit()
    conn.close()

def db_get_badwords(guild_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT word FROM badwords WHERE guild_id = ?", (guild_id,))
    rows = [row[0] for row in c.fetchall()]
    conn.close()
    return rows

def db_add_badword(guild_id, word):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO badwords (guild_id, word) VALUES (?, ?)", (guild_id, word))
    changed = c.rowcount > 0
    conn.commit()
    conn.close()
    return changed

def db_remove_badword(guild_id, word):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("DELETE FROM badwords WHERE guild_id = ? AND word = ?", (guild_id, word))
    changed = c.rowcount > 0
    conn.commit()
    conn.close()
    return changed

def db_get_allowed_roles(guild_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT role_id FROM allowed_roles WHERE guild_id = ?", (guild_id,))
    rows = [row[0] for row in c.fetchall()]
    conn.close()
    return rows

def db_add_allowed_role(guild_id, role_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO allowed_roles (guild_id, role_id) VALUES (?, ?)", (guild_id, role_id))
    changed = c.rowcount > 0
    conn.commit()
    conn.close()
    return changed

def db_remove_allowed_role(guild_id, role_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("DELETE FROM allowed_roles WHERE guild_id = ? AND role_id = ?", (guild_id, role_id))
    changed = c.rowcount > 0
    conn.commit()
    conn.close()
    return changed

def db_add_spam_entry(guild_id, user_id, message_id, channel_id, timestamp):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO spam_history (guild_id, user_id, message_id, channel_id, timestamp) VALUES (?, ?, ?, ?, ?)",
              (guild_id, user_id, message_id, channel_id, timestamp))
    conn.commit()
    conn.close()

def db_get_spam_entries(guild_id, user_id, within_seconds=4):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    cutoff = time.time() - within_seconds
    c.execute("SELECT message_id, channel_id, timestamp FROM spam_history WHERE guild_id = ? AND user_id = ? AND timestamp > ? ORDER BY timestamp",
              (guild_id, user_id, cutoff))
    rows = c.fetchall()
    conn.close()
    return rows

def db_clear_spam_entries(guild_id, user_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("DELETE FROM spam_history WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
    conn.commit()
    conn.close()

def db_cleanup_spam():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    cutoff = time.time() - 30
    c.execute("DELETE FROM spam_history WHERE timestamp < ?", (cutoff,))
    conn.commit()
    conn.close()

async def spam_cleanup_loop():
    await bot.wait_until_ready()
    while not bot.is_closed():
        db_cleanup_spam()
        await asyncio.sleep(300)


def db_get_ai_history(user_id, limit=10):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT role, content, reasoning_details FROM ai_history WHERE user_id = ? ORDER BY created_at ASC", (user_id,))
    rows = c.fetchall()
    conn.close()
    messages = []
    for r, ct, rd in rows:
        msg = {"role": r, "content": ct}
        if rd and r == "assistant":
            msg["reasoning_details"] = json.loads(rd)
        messages.append(msg)
    if len(messages) > limit:
        messages = messages[-limit:]
    return messages


def db_add_ai_history(user_id, role, content, reasoning_details=None):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    rd_json = json.dumps(reasoning_details) if reasoning_details else None
    c.execute("INSERT INTO ai_history (user_id, role, content, reasoning_details, created_at) VALUES (?, ?, ?, ?, ?)",
              (user_id, role, content, rd_json, time.time()))
    conn.commit()
    conn.close()


def db_clear_ai_history(user_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("DELETE FROM ai_history WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()


init_db()


# --- TRADUCTOR FONÉTICO MULTILINGÜE ---

# Homoglifos comunes: caracteres que parecen latinos pero no lo son
HOMOGLIFOS = {
    'а': 'a', 'А': 'a',  # Cyrillic а
    'е': 'e', 'Е': 'e',  # Cyrillic е
    'о': 'o', 'О': 'o',  # Cyrillic о
    'р': 'p', 'Р': 'p',  # Cyrillic р
    'с': 'c', 'С': 'c',  # Cyrillic с
    'х': 'x', 'Х': 'x',  # Cyrillic х
    'і': 'i', 'І': 'i',  # Ukrainian і
    'ѕ': 's', 'Ѕ': 's',  # Cyrillic ѕ
    'т': 't', 'Т': 't',  # Cyrillic т
    'н': 'h', 'Н': 'h',  # Cyrillic н
    'м': 'm', 'М': 'm',  # Cyrillic м
    'к': 'k', 'К': 'k',  # Cyrillic к
    'в': 'b', 'В': 'b',  # Cyrillic в
    'ᴀ': 'a', 'ʙ': 'b', 'ᴄ': 'c', 'ᴅ': 'd',
    'ᴇ': 'e', 'ꜰ': 'f', 'ɢ': 'g', 'ʜ': 'h',
    'ɪ': 'i', 'ᴊ': 'j', 'ᴋ': 'k', 'ʟ': 'l',
    'ᴍ': 'm', 'ɴ': 'n', 'ᴏ': 'o', 'ᴘ': 'p',
    'ǫ': 'q', 'ʀ': 'r', 'ꜱ': 's', 'ᴛ': 't',
    'ᴜ': 'u', 'ᴠ': 'v', 'ᴡ': 'w', 'ʏ': 'y',
    'ᴢ': 'z',
    'α': 'a',  # Greek alpha
    'ε': 'e',  # Greek epsilon
    'ι': 'i',  # Greek iota
    'ο': 'o',  # Greek omicron
    'ρ': 'p',  # Greek rho
    'υ': 'u',  # Greek upsilon
}

def reemplazar_homoglifos(texto):
    return ''.join(HOMOGLIFOS.get(c, c) for c in texto)

def normalizar_fonetico(texto):
    """
    Convierte el texto a su representación de sonido real (Español / Slang Inglés).
    Equivalencias justificadas fonéticamente: b/v (indistinguibles en español),
    k/q -> c, z -> s, leet speak, y slang inglés.
    """
    texto = texto.lower().strip()
    
    # Reemplazar homoglifos (Cyrillic, Greek, etc.)
    texto = reemplazar_homoglifos(texto)
    
    # Remover tildes y diacríticos
    texto = ''.join(c for c in unicodedata.normalize('NFD', texto) if unicodedata.category(c) != 'Mn')
    
    # Equivalencias de pronunciación (Inglés Slang -> Español)
    texto = re.sub(r'oo+', 'u', texto)       # grooming -> grumin
    texto = re.sub(r'ee+', 'i', texto)       # feet -> fit
    texto = re.sub(r'ing\b', 'in', texto)    # grooming -> groomin
    texto = re.sub(r'ck', 'c', texto)
    texto = re.sub(r'ph', 'f', texto)
    
    # Mapeo de caracteres individuales y Leet Speak
    # NOTA: NO se unifica m/n (sonidos distintos en español)
    mapeo_leet = {
        '4': 'a', '@': 'a',
        '3': 'e',
        '1': 'i', '!': 'i', '|': 'i',
        '0': 'o',
        'u': 'u', 'w': 'u',
        '5': 's', '$': 's',
        '7': 't',
        'b': 'v', 'v': 'v',       # b/v son fonéticamente idénticos en español
        'k': 'c', 'q': 'c', 'c': 'c',
        'z': 's', 's': 's',
        'n': 'n', 'ñ': 'n',
    }
    
    res = [mapeo_leet.get(char, char) for char in texto]
    texto = "".join(res)
    
    # Filtrar solo letras
    texto_limpio = re.sub(r'[^a-z]', '', texto)
    
    # Colapsar repeticiones continuas (ej: grrrruuusmin -> grusmin)
    resultado = re.sub(r'(.)\1+', r'\1', texto_limpio)
    
    # Normalizar m final -> n (captura errores comunes / evasión tipo "violaciom")
    if resultado.endswith('m'):
        resultado = resultado[:-1] + 'n'
    
    return resultado

def eliminar_separadores(texto):
    """Elimina todos los separadores (puntos, guiones, espacios, etc.) para detectar
    camuflajes como 'p.u.t.o', 'p-u-t-o', 'g rooming'."""
    texto = texto.lower().strip()
    texto = reemplazar_homoglifos(texto)
    texto = ''.join(c for c in unicodedata.normalize('NFD', texto) if unicodedata.category(c) != 'Mn')
    mapeo_leet = {
        '4': 'a', '@': 'a', '3': 'e', '1': 'i', '!': 'i', '|': 'i',
        '0': 'o', 'u': 'u', 'w': 'u', '5': 's', '$': 's', '7': 't',
        'b': 'v', 'v': 'v', 'k': 'c', 'q': 'c', 'c': 'c', 'z': 's', 's': 's',
        'n': 'n', 'ñ': 'n',
    }
    texto = ''.join(mapeo_leet.get(c, c) for c in texto)
    # Quitar TODO lo que no sea letra
    return re.sub(r'[^a-z]', '', texto)

def calcular_similitud(cadena1, cadena2):
    return difflib.SequenceMatcher(None, cadena1, cadena2).ratio()

def es_palabra_prohibida(mensaje_texto, palabras_prohibidas):
    palabras_raw = re.findall(r'[a-zA-Z0-9@$!|]+', mensaje_texto)
    mejor匹配 = None
    mejor_confianza = 0

    # Versión sin separadores para detectar "p.u.t.o", "g rooming", etc.
    mensaje_solo_letras = eliminar_separadores(mensaje_texto)
    # Colapsar repeticiones en la versión limpia también
    mensaje_solo_letras = re.sub(r'(.)\1+', r'\1', mensaje_solo_letras)

    for prohibida in palabras_prohibidas:
        bad_fonetico = normalizar_fonetico(prohibida)
        if not bad_fonetico:
            continue

        # 1. Coincidencia palabra por palabra
        for p in palabras_raw:
            p_fonetico = normalizar_fonetico(p)
            if not p_fonetico:
                continue

            # Coincidencia fonética exacta = 100%
            if p_fonetico == bad_fonetico:
                return prohibida, 100

            # Similitud fuzzy — palabras prohibidas >=min_fuzzy chars
            if len(bad_fonetico) >= cfg["badwords"]["min_fuzzy"] and len(p_fonetico) >= 3:
                similitud = calcular_similitud(p_fonetico, bad_fonetico)
                confianza = round(similitud * 100)
                if confianza >= cfg["badwords"]["umbral_fuzzy"] and confianza > mejor_confianza:
                    mejor匹配 = prohibida
                    mejor_confianza = confianza

        # 2. Escáner de texto corrido — substring exacto
        mensaje_unificado = normalizar_fonetico(mensaje_texto)
        
        if len(bad_fonetico) >= cfg["badwords"]["min_substring"] and bad_fonetico in mensaje_unificado:
            ratio_longitud = len(bad_fonetico) / len(mensaje_unificado) if mensaje_unificado else 0
            confianza = min(100, round(85 + ratio_longitud * 30))
            if confianza > mejor_confianza:
                mejor匹配 = prohibida
                mejor_confianza = confianza

        # 3. Escáner sin separadores — palabra por palabra
        palabras_limpias = re.findall(r'[a-z]+', mensaje_solo_letras)
        for p_clean in palabras_limpias:
            p_fonetico = normalizar_fonetico(p_clean)
            if not p_fonetico:
                continue
            if p_fonetico == bad_fonetico:
                return prohibida, 100

    if mejor匹配 and mejor_confianza >= cfg["badwords"]["umbral_medio"]:
        return mejor匹配, mejor_confianza

    return None, 0


# --- GRUPO DE COMANDOS /INFO ---
info_group = app_commands.Group(name="info", description="Comandos de información y consulta del servidor")

@info_group.command(name="badwords", description="Muestra la lista de palabras prohibidas configuradas en este servidor.")
async def info_badwords(interaction: discord.Interaction):
    guild_id = str(interaction.guild.id)
    palabras = db_get_badwords(guild_id)

    if not palabras:
        await interaction.response.send_message("ℹ️ Este servidor no tiene palabras prohibidas registradas.", ephemeral=True)
        return

    lista_formateada = "\n".join([f"• {p}" for p in palabras])

    embed = discord.Embed(
        title="🛡️ Lista Negra de Palabras (Badwords)",
        description=f"Se están filtrando las siguientes palabras y todas sus variantes/camuflajes:\n\n{lista_formateada}",
        color=0xe74c3c
    )
    embed.set_footer(text="Haz clic sobre los cuadros negros para revelar las palabras.")

    await interaction.response.send_message(embed=embed, ephemeral=True)


# --- COMANDOS DE CONFIGURACIÓN ---

@bot.tree.command(name="autoprofile", description="Asigna un canal para analizar perfiles y detectar multicuentas.")
@app_commands.describe(canal="El canal de texto donde se enviarán las alertas de seguridad")
async def autoprofile(interaction: discord.Interaction, canal: discord.TextChannel):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Solo los administradores pueden configurar esto.", ephemeral=True)
        return
        
    db_set_autoprofile(str(interaction.guild.id), canal.id)
    log.info("[AUTOPROFILE] %s configuró canal de alertas en %s (%s)",
             interaction.user, interaction.guild.name, interaction.guild.id)
    await interaction.response.send_message(f"✅ AutoProfile activado en {canal.mention}.", ephemeral=True)


@bot.tree.command(name="blockword", description="Bloquea una palabra y todas sus variantes/camuflajes/errores.")
@app_commands.describe(word="La palabra que deseas prohibir en el servidor")
async def blockword(interaction: discord.Interaction, word: str):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Solo administradores.", ephemeral=True)
        return

    guild_id = str(interaction.guild.id)
    word_clean = word.lower().strip()

    if not db_add_badword(guild_id, word_clean):
        await interaction.response.send_message(f"⚠️ La palabra `{word_clean}` ya estaba en la lista negra.", ephemeral=True)
        return

    log.info("[BLOCKWORD] %s bloqueó \"%s\" en %s", interaction.user, word_clean, interaction.guild.name)
    await interaction.response.send_message(
        f"🚫 Palabra **`{word_clean}`** añadida a la lista negra.",
        ephemeral=True
    )


## En caso de ser muy lazy para agregarlas manualmente

PALABRAS_DEFAULT = [
    # Español — insultos y vulgares
    "puto", "puta", "put0", "pendejo", "pendeja",
    "mierda", "mrd", "m1erda",
    "cabron", "cabrona", "cbrn",
    "coño", "coñ0", "conio", "c0ño",
    "chinga", "chingar", "chingado", "chingada",
    "pinche", "perra",
    "estupido", "estupida", "imbecil",
    "idiota", "idiotas",
    "maldito", "maldita",
    "culero", "culera",
    "pendejez", "estupidez",
    "subnormal", "retrasado", "retrasada",
    "pedo", "pedofilo", "pedofila",
    "grooming",
    # Inglés — insultos y vulgares
    "fuck", "fck", "fuk", "shit", "sh1t", "sht",
    "bitch", "b1tch", "b!tch", "asshole", "assh0le", "ahole",
    "dick", "d1ck", "cock", "c0ck",
    "bastard", "b4stard",
    "slut", "whore", "wh0re",
    "retard", "r3tard", "r3tarded",
    "cunt", "c0nt", "cnt",
    "rape", "rapist", "raped",
    # Raciales / étnicos
    "nigger", "n1gger", "nigga", "n1gga", "nigg", "niqqa", "niqga",
    "n1gg3r", "n1gg4", "nigg3r", "nigg4",
    "whigger", "whigga", "wigga", "w1gga",
    "spic", "sp1c", "spick",
    "wetback", "wetb4ck",
    "beaner", "be4ner",
    "chink", "ch1nk",
    "gook", "g00k",
    "kike", "k1ke",
    "towelhead", "towelh3ad",
    "sandnigger", "sandn1gger",
    # Homofóbicos
    "fag", "faggot", "f4ggot", "fagot", "f4g",
    "tranny", "tr4nny",
    "dyke", "d1ke",
]

@bot.tree.command(name="blockdefault", description="Añade una lista predeterminada de palabras vulgares comunes en español.")
async def blockdefault(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Solo administradores.", ephemeral=True)
        return

    guild_id = str(interaction.guild.id)
    agregadas = []
    ya_existian = []

    for palabra in PALABRAS_DEFAULT:
        if db_add_badword(guild_id, palabra):
            agregadas.append(palabra)
        else:
            ya_existian.append(palabra)

    total = len(agregadas)
    if total == 0:
        await interaction.response.send_message(
            f"⚠️ Todas las {len(PALABRAS_DEFAULT)} palabras ya estaban en la lista negra.",
            ephemeral=True
        )
        return

    log.info("[BLOCKDEFAULT] %s añadió %d palabras por defecto en %s",
             interaction.user, total, interaction.guild.name)

    embed = discord.Embed(
        title="🚫 Palabras por defecto añadidas",
        description=f"Se agregaron **{total}** palabras a la lista negra.\n"
                    f"Ya existían: **{len(ya_existian)}**",
        color=0xe74c3c
    )
    if agregadas:
        lista = ", ".join(f"`{p}`" for p in agregadas[:20])
        if total > 20:
            lista += f"\n... y **{total - 20}** más."
        embed.add_field(name="Añadidas", value=lista, inline=False)

    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="unblockword", description="Elimina una palabra de la lista negra del servidor.")
@app_commands.describe(word="La palabra que deseas desbloquear")
async def unblockword(interaction: discord.Interaction, word: str):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Solo administradores.", ephemeral=True)
        return

    guild_id = str(interaction.guild.id)
    word_clean = word.lower().strip()

    if not db_remove_badword(guild_id, word_clean):
        await interaction.response.send_message(f"⚠️ La palabra `{word_clean}` no se encuentra en la lista negra.", ephemeral=True)
        return

    log.info("[UNBLOCKWORD] %s desbloqueó \"%s\" en %s", interaction.user, word_clean, interaction.guild.name)
    await interaction.response.send_message(
        f"✅ Palabra **`{word_clean}`** eliminada de la lista negra.",
        ephemeral=True
    )


@bot.tree.command(name="speak", description="Hace que Sentinel diga un mensaje en el chat de forma anónima.")
@app_commands.describe(mensaje="El mensaje que quieres que Sentinel diga en el chat")
async def speak(interaction: discord.Interaction, mensaje: str):
    if not interaction.guild or interaction.user.id != interaction.guild.owner_id:
        await interaction.response.send_message("❌ Este comando es exclusivo y reservado únicamente para el **Owner** del servidor.", ephemeral=True)
        return

    log.info("[SPEAK] %s envió mensaje anónimo en %s: \"%s\"",
             interaction.user, interaction.guild.name, mensaje[:100])
    await interaction.response.send_message("✅ Mensaje enviado de forma anónima.", ephemeral=True)
    await interaction.channel.send(mensaje)


@bot.tree.command(name="ping", description="Muestra la latencia del bot en ms.")
async def ping(interaction: discord.Interaction):
    latencia = round(bot.latency * 1000)
    log.info("[PING] %s — %dms", interaction.user, latencia)
    await interaction.response.send_message(f"🏓 Pong! Latencia: **{latencia}ms**")


@bot.command(name="ping", help="Muestra la latencia del bot en ms.")
async def ping_prefix(ctx):
    latencia = round(bot.latency * 1000)
    log.info("[PING] %s — %dms", ctx.author, latencia)
    await ctx.send(f"🏓 Pong! Latencia: **{latencia}ms**")


@bot.tree.command(name="allowrole", description="Añade un rol que puede omitir el filtro de palabras prohibidas.")
@app_commands.describe(rol="El rol que quieres excluir del filtro")
async def allowrole(interaction: discord.Interaction, rol: discord.Role):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Solo administradores.", ephemeral=True)
        return

    guild_id = str(interaction.guild.id)

    if rol >= interaction.user.top_role:
        await interaction.response.send_message("❌ No puedes añadir un rol igual o superior al tuyo.", ephemeral=True)
        return

    if not db_add_allowed_role(guild_id, rol.id):
        await interaction.response.send_message(f"⚠️ El rol {rol.mention} ya estaba exento del filtro.", ephemeral=True)
        return

    log.info("[ALLOWROLE] %s añadió rol %s (%s) en %s", interaction.user, rol.name, rol.id, interaction.guild.name)
    await interaction.response.send_message(
        f"✅ El rol {rol.mention} ahora está exento del filtro de palabras.",
        ephemeral=True
    )


@bot.tree.command(name="disallowrole", description="Elimina un rol de la lista de exentos del filtro.")
@app_commands.describe(rol="El rol que quieres volver a aplicar el filtro")
async def disallowrole(interaction: discord.Interaction, rol: discord.Role):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Solo administradores.", ephemeral=True)
        return

    guild_id = str(interaction.guild.id)

    if not db_remove_allowed_role(guild_id, rol.id):
        await interaction.response.send_message(f"⚠️ El rol {rol.mention} no estaba en la lista de exentos.", ephemeral=True)
        return

    log.info("[DISALLOWROLE] %s eliminó rol %s (%s) en %s", interaction.user, rol.name, rol.id, interaction.guild.name)
    await interaction.response.send_message(
        f"✅ El rol {rol.mention} ahora sí será revisado por el filtro de palabras.",
        ephemeral=True
    )


@info_group.command(name="roles", description="Muestra los roles exentos del filtro de palabras prohibidas.")
async def info_roles(interaction: discord.Interaction):
    guild_id = str(interaction.guild.id)
    role_ids = db_get_allowed_roles(guild_id)

    if not role_ids:
        await interaction.response.send_message("ℹ️ No hay roles exentos del filtro. Todos los usuarios son revisados.", ephemeral=True)
        return

    roles_texto = []
    for rid in role_ids:
        role = interaction.guild.get_role(rid)
        if role:
            roles_texto.append(f"• {role.mention}")
        else:
            roles_texto.append(f"• `<@&{rid}>` (rol eliminado)")

    embed = discord.Embed(
        title="🛡️ Roles Exentos del Filtro",
        description="\n".join(roles_texto),
        color=0x2ecc71
    )
    embed.set_footer(text="Estos roles no son revisados por el filtro de palabras prohibidas.")

    await interaction.response.send_message(embed=embed, ephemeral=True)


def get_ai_modelos():
    return cfg.get("ai", {}).get("modelos", [{"nombre": "Free", "model": "openrouter/free"}])

def get_ai_default_model():
    for m in get_ai_modelos():
        if m.get("default"):
            return m
    return get_ai_modelos()[0]

def find_ai_model(nombre):
    for m in get_ai_modelos():
        if m["nombre"].lower() == nombre.lower():
            return m
    return get_ai_default_model()

async def autocomplete_modelo(interaction: discord.Interaction, current: str):
    return [
        app_commands.Choice(name=m["nombre"], value=m["nombre"])
        for m in get_ai_modelos()
        if current.lower() in m["nombre"].lower()
    ]


@bot.tree.command(name="ai", description="Chatea con IA (OpenRouter). Soporta conversación con contexto.")
@app_commands.describe(
    mensaje="Tu mensaje para la IA",
    modelo="Nombre del modelo a usar (opcional)"
)
@app_commands.autocomplete(modelo=autocomplete_modelo)
async def ai_command(interaction: discord.Interaction, mensaje: str, modelo: str = None):
    api_key = cfg.get("ai", {}).get("api_key", "")
    if not api_key:
        await interaction.response.send_message("❌ La API key de OpenRouter no está configurada.", ephemeral=True)
        return

    await interaction.response.defer()

    user_id = interaction.user.id
    max_h = cfg["ai"].get("max_historial", 10)

    db_add_ai_history(user_id, "user", mensaje)

    historial = db_get_ai_history(user_id, limit=max_h)

    model_info = find_ai_model(modelo) if modelo else get_ai_default_model()

    log.info("[AI] %s (%s) → modelo: %s | mensaje: \"%s\"", interaction.user, user_id, model_info["nombre"], mensaje[:80])
    model_id = model_info["model"]
    model_reasoning = model_info.get("reasoning", cfg["ai"].get("reasoning", True))

    # Limpiar reasoning_details de los mensajes antes de enviar a la API
    historial_limpio = []
    for msg_h in historial:
        clean = {"role": msg_h["role"], "content": msg_h["content"]}
        historial_limpio.append(clean)

    payload = {
        "model": model_id,
        "messages": [
            {"role": "system", "content": "Eres un asistente útil. Responde en español a menos que te pidan otro idioma. Sé conciso."}
        ] + historial_limpio,
        "max_tokens": cfg["ai"].get("max_tokens", 1024),
        "stream": True,
    }
    if model_reasoning:
        payload["reasoning"] = {"enabled": True}

    msg = await interaction.followup.send(embed=discord.Embed(
        title="🤖 IA",
        description="🧠 *Pensando...*",
        color=0xf1c40f
    ))

    try:
        resp = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            stream=True,
            timeout=120,
        )
        resp.raise_for_status()
        resp.encoding = "utf-8"

        respuesta = ""
        reasoning_text = ""
        last_edit = 0
        last_len = 0
        edit_interval = 1.5

        def build_embed(resp_text, r_text, phase):
            if phase == "reasoning":
                r_display = r_text[-1500:] if len(r_text) > 1500 else r_text
                embed = discord.Embed(
                    title="🤖 IA",
                    description="🧠 *Razonando...*",
                    color=0xf1c40f
                )
                if r_display:
                    embed.add_field(name="🧠 Razonamiento", value=f"```{r_display}```", inline=False)
                return embed
            else:
                embed = discord.Embed(
                    title="🤖 IA",
                    description=resp_text[:4000] if resp_text else "*Generando...*",
                    color=0x3498db
                )
                if r_text:
                    r_display = r_text[-1000:] + "..." if len(r_text) > 1000 else r_text
                    embed.add_field(name="🧠 Razonamiento", value=f"```{r_display}```", inline=False)
                return embed

        phase = "reasoning"

        for line in resp.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data: "):
                continue
            data = line[6:]
            if data.strip() == "[DONE]":
                break
            try:
                chunk = json.loads(data)
                choices = chunk.get("choices", [])
                if not choices:
                    continue
                delta = choices[0].get("delta", {})
                if "content" in delta and delta["content"]:
                    if phase == "reasoning" and respuesta == "":
                        phase = "response"
                    respuesta += delta["content"]
                if "reasoning_content" in delta and delta["reasoning_content"]:
                    reasoning_text += delta["reasoning_content"]
            except json.JSONDecodeError:
                continue

            now = time.time()
            if now - last_edit >= edit_interval:
                current_len = len(respuesta) if phase == "response" else len(reasoning_text)
                if current_len - last_len >= 30 or now - last_edit >= 3:
                    try:
                        await msg.edit(embed=build_embed(respuesta, reasoning_text, phase))
                        last_edit = now
                        last_len = current_len
                    except discord.errors.HTTPException:
                        pass

        if not respuesta:
            log.warning("[AI] %s (%s) — respuesta vacía de %s", interaction.user, user_id, model_id)
            await msg.edit(embed=discord.Embed(
                title="❌ Error",
                description="No se recibió respuesta de la IA.",
                color=0xe74c3c
            ))
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            c.execute("DELETE FROM ai_history WHERE user_id = ? AND role = 'user' ORDER BY created_at DESC LIMIT 1", (user_id,))
            conn.commit()
            conn.close()
            return

        db_add_ai_history(user_id, "assistant", respuesta,
                          reasoning_details=[{"type": "reasoning_text", "text": reasoning_text}] if reasoning_text else None)

        historial_final = db_get_ai_history(user_id, limit=max_h)

        embed = discord.Embed(
            title="🤖 IA",
            description=respuesta[:4000],
            color=0x3498db
        )
        embed.set_footer(text=f"Modelo: {model_info['nombre']} ({model_id}) • Contexto: {len(historial_final)} mensajes")

        if reasoning_text:
            r_display = reasoning_text[:1000] + "..." if len(reasoning_text) > 1000 else reasoning_text
            embed.add_field(name="🧠 Razonamiento", value=f"```{r_display}```", inline=False)

        await msg.edit(embed=embed)

        log.info("[AI] %s (%s) ← %s | %d chars respuesta, %d chars razonamiento",
                 interaction.user, user_id, model_id, len(respuesta), len(reasoning_text))

    except requests.exceptions.Timeout:
        log.warning("[AI] %s (%s) — timeout con %s", interaction.user, user_id, model_id)
        await msg.edit(embed=discord.Embed(
            title="⏳ Timeout",
            description="La IA tardó demasiado. Intenta de nuevo.",
            color=0xe74c3c
        ))
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("DELETE FROM ai_history WHERE user_id = ? AND role = 'user' ORDER BY created_at DESC LIMIT 1", (user_id,))
        conn.commit()
        conn.close()
    except Exception as e:
        log.error("[AI] Error: %s", e)
        await msg.edit(embed=discord.Embed(
            title="❌ Error",
            description=f"Error al contactar la IA: `{e}`",
            color=0xe74c3c
        ))
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("DELETE FROM ai_history WHERE user_id = ? AND role = 'user' ORDER BY created_at DESC LIMIT 1", (user_id,))
        conn.commit()
        conn.close()


@bot.tree.command(name="ai-reset", description="Borra el historial de conversación con la IA.")
async def ai_reset(interaction: discord.Interaction):
    db_clear_ai_history(interaction.user.id)
    log.info("[AI-RESET] %s (%s) borró historial de IA", interaction.user, interaction.user.id)
    await interaction.response.send_message("🧹 Historial de conversación con IA borrado.", ephemeral=True)


@bot.tree.command(name="ai-model", description="Muestra los modelos de IA disponibles o cambia el predeterminado.")
@app_commands.describe(modelo="Nombre del modelo para establecer como predeterminado (opcional)")
@app_commands.autocomplete(modelo=autocomplete_modelo)
async def ai_model(interaction: discord.Interaction, modelo: str = None):
    modelos = get_ai_modelos()

    if modelo:
        found = find_ai_model(modelo)
        if found:
            log.info("[AI-MODEL] %s cambió modelo a %s (%s)", interaction.user, found["nombre"], found["model"])
            await interaction.response.send_message(
                f"✅ Modelo predeterminado cambiado a **{found['nombre']}** (`{found['model']}`)",
                ephemeral=True
            )
        else:
            await interaction.response.send_message(f"❌ Modelo '{modelo}' no encontrado.", ephemeral=True)
        return

    embed = discord.Embed(
        title="🤖 Modelos de IA Disponibles",
        color=0x3498db
    )
    for i, m in enumerate(modelos):
        r_tag = " 🧠" if m.get("reasoning") else ""
        default = " *(predeterminado)*" if m.get("default") else ""
        embed.add_field(
            name=f"{m['nombre']}{default}{r_tag}",
            value=f"`{m['model']}`",
            inline=False
        )
    embed.set_footer(text="Usa /ai modelo:<nombre> para elegir uno. 🧠 = soporta reasoning.")
    await interaction.response.send_message(embed=embed, ephemeral=True)


# --- EVENTO ON_READY ---
@bot.event
async def on_ready():
    bot.tree.add_command(info_group)

    # Cargar historial anti-spam reciente de la DB
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    cutoff = time.time() - 4
    c.execute("SELECT guild_id, user_id, message_id, channel_id, timestamp FROM spam_history WHERE timestamp > ?", (cutoff,))
    for gid, uid, mid, cid, ts in c.fetchall():
        key = (gid, uid)
        if key not in user_message_history:
            user_message_history[key] = []
    conn.close()
    db_cleanup_spam()
    log.info("Historial anti-spam cargado desde la base de datos.")

    asyncio.create_task(spam_cleanup_loop())
    log.info("Tarea de limpieza anti-spam iniciada (cada 5 min).")

    # Establecer estado del bot
    status_type = cfg["bot"]["status_type"].lower()
    status_text = cfg["bot"]["status_text"]
    activity_map = {
        "playing": discord.Game(name=status_text),
        "watching": discord.Activity(type=discord.ActivityType.watching, name=status_text),
        "listening": discord.Activity(type=discord.ActivityType.listening, name=status_text),
        "competing": discord.Activity(type=discord.ActivityType.competing, name=status_text),
    }
    activity = activity_map.get(status_type, discord.Activity(type=discord.ActivityType.watching, name=status_text))

    presence_map = {
        "online": discord.Status.online,
        "dnd": discord.Status.dnd,
        "idle": discord.Status.idle,
        "invisible": discord.Status.invisible,
        "offline": discord.Status.offline,
    }
    status_presence = presence_map.get(cfg["bot"].get("presence", "online").lower(), discord.Status.online)

    await bot.change_presence(status=status_presence, activity=activity)
    log.info("Estado: %s | Presencia: %s %s", status_type, cfg["bot"].get("presence", "online"), status_text)

    try:
        synced_global = await bot.tree.sync()
        print(f"✅ Sync global: {len(synced_global)} comandos.")
        for cmd in synced_global:
            if isinstance(cmd, app_commands.Group):
                for sub in cmd.commands:
                    print(f"   /{cmd.name} {sub.name}")
            else:
                print(f"   /{cmd.name}")
    except Exception as e:
        print(f"Error sincronizando comandos globales: {e}")

    for guild in bot.guilds:
        try:
            await bot.tree.sync(guild=guild)
            print(f"✅ Sync en servidor: {guild.name} ({guild.id})")
        except Exception as e:
            print(f"Error sincronizando en {guild.name}: {e}")
            
    print(f'🛡️ Sentinel en línea como {bot.user}.')
    print('✅ Motor Fonético Multilingüe Activo.')
    log.info("Bot conectado como %s (ID: %s)", bot.user, bot.user.id)
    log.info("Servidores conectados: %d", len(bot.guilds))
    for g in bot.guilds:
        log.info("   - %s (%d miembros)", g.name, g.member_count)


# --- EVENTO ON_MESSAGE (FILTRO BADWORDS + ANTI-SPAM) ---
@bot.event
async def on_message(message):
    if message.author.bot or not message.guild:
        return

    await bot.process_commands(message)

    guild_id = message.guild.id
    guild_id_str = str(guild_id)

    # 0. VERIFICAR ROLES EXENTOS
    usuario_roles = [r.id for r in message.author.roles]
    roles_exentos = db_get_allowed_roles(guild_id_str)
    tiene_exencion = any(r in roles_exentos for r in usuario_roles)

    # 1. FILTRO DE PALABRAS PROHIBIDAS (solo si no tiene rol exento)
    if not tiene_exencion:
        palabras_lista = db_get_badwords(guild_id_str)
        if palabras_lista:
            palabra_detectada, confianza = es_palabra_prohibida(message.content, palabras_lista)

            if palabra_detectada:
                log.info("[BADWORDS] %s (%s) en #%s: \"%s\" (detectado: %s, confianza: %d%%)",
                         message.author, message.author.id, message.channel.name,
                         message.content[:100], palabra_detectada, confianza)

                try:
                    if confianza >= 90:
                        # Alta confianza: eliminar mensaje
                        await message.delete()
                        await message.channel.send(
                            f"🛡️ {message.author.mention}, tu mensaje fue eliminado "
                            f"(**{confianza}%** de probabilidad de vocabulario no permitido).",
                            delete_after=5
                        )
                    elif confianza >= 70:
                        # Confianza media: solo advertencia
                        await message.channel.send(
                            f"⚠️ {message.author.mention}, tu mensaje ha sido marcado "
                            f"(**{confianza}%** de probabilidad). Evita ese vocabulario.",
                            delete_after=6
                        )
                    return
                except Exception as e:
                    log.error("Error procesando palabra prohibida: %s", e)

    if message.author.guild_permissions.administrator:
        return

    # 2. MÓDULO ANTI-SPAM (con persistencia en SQLite)
    user_id = message.author.id
    key = (guild_id, user_id)
    ahora = time.time()

    # Guardar en DB para persistencia entre reinicios
    db_add_spam_entry(guild_id_str, user_id, message.id, message.channel.id, ahora)

    if key not in user_message_history:
        user_message_history[key] = []

    user_message_history[key] = [msg for msg in user_message_history[key] if ahora - msg.created_at.timestamp() <= 4]
    user_message_history[key].append(message)

    if len(user_message_history[key]) >= 4:
        mensajes_a_borrar = list(user_message_history[key])
        user_message_history[key] = []

        # Limpiar DB
        db_clear_spam_entries(guild_id_str, user_id)

        try:
            await message.channel.delete_messages(mensajes_a_borrar)
        except Exception:
            for msg in mensajes_a_borrar:
                try: await msg.delete()
                except Exception: pass

        try:
            member = message.guild.get_member(user_id) or await message.guild.fetch_member(user_id)
            duracion = datetime.timedelta(minutes=1)
            await member.timeout(duracion, reason="Sentinel Anti-Spam")
            log.info("[ANTI-SPAM] %s (%s) silenciado por 1 min en %s (%s) — %d mensajes en 4s",
                     member, member.id, message.guild.name, message.guild.id, len(mensajes_a_borrar))
            await message.channel.send(f"🚫 {member.mention} silenciado por 1 minuto (Anti-Spam).", delete_after=5)
        except Exception as e:
            log.error("Error al aplicar timeout: %s", e)


# --- EVENTO ON_MESSAGE_EDIT (FILTRO BADWORDS EN EDICIONES) ---
@bot.event
async def on_message_edit(before, after):
    if before.author.bot or not before.guild:
        return

    # Ignorar si el contenido no cambió
    if before.content == after.content:
        return

    guild_id_str = str(before.guild.id)

    # Verificar roles exentos
    usuario_roles = [r.id for r in before.author.roles]
    roles_exentos = db_get_allowed_roles(guild_id_str)
    if any(r in roles_exentos for r in usuario_roles):
        return

    palabras_lista = db_get_badwords(guild_id_str)
    if not palabras_lista:
        return

    palabra_detectada, confianza = es_palabra_prohibida(after.content, palabras_lista)

    if palabra_detectada:
        log.info("[BADWORDS-EDIT] %s (%s) editó en #%s: \"%s\" (detectado: %s, confianza: %d%%)",
                 before.author, before.author.id, before.channel.name,
                 after.content[:100], palabra_detectada, confianza)

        try:
            if confianza >= 90:
                await before.delete()
                await before.channel.send(
                    f"🛡️ {before.author.mention}, tu mensaje fue eliminado tras editarlo "
                    f"(**{confianza}%** de probabilidad de vocabulario no permitido).",
                    delete_after=5
                )
            elif confianza >= 70:
                await before.channel.send(
                    f"⚠️ {before.author.mention}, tu mensaje editado ha sido marcado "
                    f"(**{confianza}%** de probabilidad). Evita ese vocabulario.",
                    delete_after=6
                )
        except Exception as e:
            log.error("Error procesando palabra prohibida en edición: %s", e)


# --- EVENTO ON_MEMBER_JOIN (AUTOPROFILE) ---
@bot.event
async def on_member_join(member):
    guild_id_str = str(member.guild.id)
    
    canal_id = db_get_autoprofile(guild_id_str)
    if not canal_id:
        return 
        
    canal = bot.get_channel(canal_id)
    
    if not canal:
        return

    nivel_riesgo = 1
    motivos = []
    estado = "✅ Normal"

    if not member.avatar:
        nivel_riesgo += 3
        motivos.append("• **Alerta:** No tiene foto de perfil propia.")

    edad_cuenta = discord.utils.utcnow() - member.created_at
    dias_creada = edad_cuenta.days

    if dias_creada < 1:
        nivel_riesgo += 5
        motivos.append("• **Crítico:** Cuenta creada hace menos de 24 horas.")
    elif dias_creada < 7:
        nivel_riesgo += 3
        motivos.append("• **Advertencia:** Cuenta muy nueva (Menos de 7 días).")

    nombre_nuevo = member.name.lower()
    coincidencias = []
    
    for otro_miembro in member.guild.members:
        if otro_miembro.id == member.id: continue
        otro_nombre = otro_miembro.name.lower()
        if len(nombre_nuevo) >= 4 and (nombre_nuevo in otro_nombre or otro_nombre in nombre_nuevo):
            coincidencias.append(otro_miembro.name)

    if coincidencias:
        nivel_riesgo += 4
        estado = "👥 Multicuenta Sospechosa"
        motivos.append(f"• **Posible Alt:** Coincide en nombre con: `{', '.join(coincidencias[:2])}`")

    if member.bot:
        nivel_riesgo += 2
        motivos.append("• **Nota:** Cuenta identificada como Bot.")

    if nivel_riesgo > 10: nivel_riesgo = 10
    color = 0x00ff00 if "Normal" in estado else (0xffff00 if "Observación" in estado else 0xff0000)

    creacion_str = member.created_at.strftime("%d/%m/%Y a las %H:%M")
    ingreso_str = member.joined_at.strftime("%d/%m/%Y a las %H:%M") if member.joined_at else "Desconocido"

    embed = discord.Embed(title="🛡️ AutoProfile: Análisis de Seguridad", color=color)
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="👤 Usuario Evaluado", value=f"{member.mention} (`{member.name}`)", inline=False)
    embed.add_field(name="📌 Estado Asignado", value=f"**{estado}**", inline=True)
    embed.add_field(name="📊 Nivel de Riesgo", value=f"**{nivel_riesgo} / 10**", inline=True)
    embed.add_field(name="🆔 ID de Cuenta", value=str(member.id), inline=False)
    embed.add_field(name="📅 Creación de la cuenta", value=creacion_str, inline=True)
    embed.add_field(name="📥 Ingreso al servidor", value=ingreso_str, inline=True)
    
    texto_motivos = "\n".join(motivos) if motivos else "✅ Ninguna anomalía o coincidencia sospechosa detectada."
    embed.add_field(name="🔍 Factores y Coincidencias Detectadas", value=texto_motivos, inline=False)
    embed.set_footer(text="Sistema de Seguridad Sentinel • Creador: alensitopromax")
    
    await canal.send(embed=embed)
    log.info("[AUTOPROFILE] %s (%s) se unió a %s — Riesgo: %d/10 — %s",
             member, member.id, member.guild.name, nivel_riesgo, estado)

# Token del bot
if not cfg["token"]:
    log.error("No se encontró el token en config.json. Agrégalo y reinicia.")
    exit(1)

bot.run(cfg["token"])
