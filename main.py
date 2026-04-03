import os
import certifi
import discord
import aiohttp
import asyncio

# ==========================================
# 🚑 PARCHE SSL
# ==========================================
os.environ["SSL_CERT_FILE"] = certifi.where()

# ===========================
# CONFIGURACIÓN (Variables)
# ===========================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
API_FOOTBALL_KEY = os.getenv("API_FOOTBALL_KEY")
try:
    CHANNEL_ID = int(os.getenv("CHANNEL_ID"))
    HISTORICAL_CHANNEL_ID = int(os.getenv("HISTORICAL_CHANNEL_ID", 0)) 
except (TypeError, ValueError):
    CHANNEL_ID = 0
    HISTORICAL_CHANNEL_ID = 0

# ===========================
# LÓGICA DEL SNIPER Y MEMORIAS
# ===========================
API_FOOTBALL_URL = "https://v3.football.api-sports.io/fixtures"
API_STATS_URL = "https://v3.football.api-sports.io/fixtures/statistics"
API_TEAM_STATS_URL = "https://v3.football.api-sports.io/teams/statistics"

POLL_INTERVAL = 120 
XG_JUMP_THRESHOLD = 0.15 

# UMBRAL DE DESIGUALDAD: ¿Cuántos goles de diferencia neta debe haber entre los equipos?
MISMATCH_THRESHOLD = 1.5 

xg_memory = {}
team_stats_cache = {} 

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

# ===========================
# 📊 LÓGICAS ASÍNCRONAS DE API-FOOTBALL
# ===========================
async def get_live_fixtures(session):
    headers = {"x-apisports-key": API_FOOTBALL_KEY, "Accept": "application/json"}
    try:
        async with session.get(API_FOOTBALL_URL, headers=headers, params={"live": "all"}, timeout=10) as resp:
            resp.raise_for_status()
            data = await resp.json()
            return data.get("response", [])
    except Exception:
        return []

async def get_fixture_xg(session, fixture_id):
    headers = {"x-apisports-key": API_FOOTBALL_KEY, "Accept": "application/json"}
    try:
        async with session.get(API_STATS_URL, headers=headers, params={"fixture": fixture_id}, timeout=10) as resp:
            resp.raise_for_status()
            data = await resp.json()
            if not data.get("response"): return None, None
            
            response_data = data["response"]
            xg_home, xg_away = 0.0, 0.0
            home_id = response_data[0]["team"]["id"]

            for team_data in response_data:
                for stat in team_data.get("statistics", []):
                    if stat["type"] == "expected_goals" and stat["value"] is not None:
                        valor = float(stat["value"])
                        if team_data["team"]["id"] == home_id:
                            xg_home = valor
                        else:
                            xg_away = valor
            return xg_home, xg_away
    except Exception:
        return None, None

async def get_team_season_stats(session, league_id, season, team_id):
    """Extrae los goles a favor y en contra promedio de toda la temporada."""
    cache_key = f"{team_id}-{league_id}-{season}"
    if cache_key in team_stats_cache:
        return team_stats_cache[cache_key]

    headers = {"x-apisports-key": API_FOOTBALL_KEY, "Accept": "application/json"}
    params = {"league": league_id, "season": season, "team": team_id}
    
    try:
        async with session.get(API_TEAM_STATS_URL, headers=headers, params=params, timeout=10) as resp:
            data = await resp.json()
            response_data = data.get("response")
            
            if response_data and "goals" in response_data:
                avg_for_str = response_data["goals"]["for"]["average"]["total"]
                avg_against_str = response_data["goals"]["against"]["average"]["total"]
                
                gf = float(avg_for_str) if avg_for_str is not None else 0.0
                ga = float(avg_against_str) if avg_against_str is not None else 0.0
                
                # Guardamos el Poder Neto: Goles a favor menos goles en contra
                stats = {"gf": gf, "ga": ga, "net": round(gf - ga, 2)}
                team_stats_cache[cache_key] = stats
                return stats
    except Exception as e:
        print(f"Error obteniendo historial equipo {team_id}: {e}")
        
    return {"gf": 0.0, "ga": 0.0, "net": 0.0}

# ===========================
# 🧠 CEREBRO MAESTRO
# ===========================
async def process_momentum(session, fixtures, channel, historical_channel):
    global xg_memory
    
    for item in fixtures:
        league_name = item['league']['name']
        country = item['league']['country']
        league_id = item['league']['id']
        season = item['league']['season']
        minute = item['fixture']['status']['elapsed']
        
        if minute is None or minute < 5: continue

        fixture_id = item['fixture']['id']
        home_name = item['teams']['home']['name']
        away_name = item['teams']['away']['name']
        home_id = item['teams']['home']['id']
        away_id = item['teams']['away']['id']
        gh = item['goals']['home']
        ga = item['goals']['away']

        xg_home, xg_away = await get_fixture_xg(session, fixture_id)
        if xg_home is None or xg_away is None: continue
            
        if fixture_id not in xg_memory:
            xg_memory[fixture_id] = {
                'min': minute, 'xg_home': xg_home, 'xg_away': xg_away,
                'home': home_name, 'away': away_name, 'gh': gh, 'ga': ga,
                'alerta_mismatch_enviada': False
            }
        else:
            old_data = xg_memory[fixture_id]
            diff_home = xg_home - old_data['xg_home']
            diff_away = xg_away - old_data['xg_away']
            min_diff = minute - old_data['min']

            # ==========================================
            # 🔥 1. ALERTA MOMENTUM (Va al Canal Principal)
            # ==========================================
            alerta_momentum = False
            equipo_fuego, salto_xg = "", 0.0

            if diff_home >= XG_JUMP_THRESHOLD:
                alerta_momentum, equipo_fuego, salto_xg = True, home_name, diff_home
            elif diff_away >= XG_JUMP_THRESHOLD:
                alerta_momentum, equipo_fuego, salto_xg = True, away_name, diff_away

            if alerta_momentum and min_diff > 0:
                msg = (f"📈 **ALERTA DE MOMENTUM** 📈\n"
                       f"⚠️ **{equipo_fuego}** está atacando con todo.\n"
                       f"🏆 {league_name} ({country})\n"
                       f"🏟️ {home_name} `{gh}` - `{ga}` {away_name} (Min {minute}')\n"
                       f"📊 **Salto xG:** `+{round(salto_xg, 2)}` en {min_diff} mins.")
                if channel: await channel.send(msg)

            # ==========================================
            # ⚖️ 2. CAZA-DESIGUALDADES (Va al Canal Histórico)
            # ==========================================
            canal_destino = historical_channel if historical_channel else channel

            if not old_data['alerta_mismatch_enviada']:
                stats_home = await get_team_season_stats(session, league_id, season, home_id)
                stats_away = await get_team_season_stats(session, league_id, season, away_id)
                
                # Verificamos que ambos equipos tengan historial cargado
                if (stats_home['gf'] > 0 or stats_home['ga'] > 0) and (stats_away['gf'] > 0 or stats_away['ga'] > 0):
                    brecha = abs(stats_home['net'] - stats_away['net'])
                    
                    if brecha >= MISMATCH_THRESHOLD:
                        # Identificamos quién es el fuerte y quién es el débil
                        favorito = home_name if stats_home['net'] > stats_away['net'] else away_name
                        debil = away_name if favorito == home_name else home_name
                        stats_fav = stats_home if favorito == home_name else stats_away
                        stats_deb = stats_away if favorito == home_name else stats_home
                        
                        msg_hist = (
                            f"⚖️ **ALERTA: GRAN DESIGUALDAD HISTÓRICA** ⚖️\n"
                            f"⚠️ Diferencia masiva en el rendimiento de temporada.\n"
                            f"🏆 {league_name} ({country}) - Min `{minute}'`\n"
                            f"🏟️ {home_name} `{gh}` - `{ga}` {away_name}\n\n"
                            f"🟢 **{favorito}:** Anota `{stats_fav['gf']}` | Recibe `{stats_fav['ga']}` (Neto: `{stats_fav['net']}`)\n"
                            f"🔴 **{debil}:** Anota `{stats_deb['gf']}` | Recibe `{stats_deb['ga']}` (Neto: `{stats_deb['net']}`)\n"
                            f"📊 **Brecha de Poder:** `{round(brecha, 2)}` goles de ventaja estadística.\n"
                            f"💡 *Oportunidad para apostar a goles del favorito o Hándicap.*"
                        )
                        if canal_destino: await canal_destino.send(msg_hist)
                        old_data['alerta_mismatch_enviada'] = True

            # Actualizamos memoria
            xg_memory[fixture_id] = {
                'min': minute, 'xg_home': xg_home, 'xg_away': xg_away,
                'home': home_name, 'away': away_name, 'gh': gh, 'ga': ga,
                'alerta_mismatch_enviada': old_data['alerta_mismatch_enviada']
            }
            
        await asyncio.sleep(1) 

    # 🧹 RECOLECTOR DE BASURA
    live_ids = [item['fixture']['id'] for item in fixtures]
    ids_to_delete = [mem_id for mem_id in xg_memory.keys() if mem_id not in live_ids]
    for old_id in ids_to_delete:
        del xg_memory[old_id]

async def background_task():
    await client.wait_until_ready()
    channel = client.get_channel(CHANNEL_ID)
    historical_channel = client.get_channel(HISTORICAL_CHANNEL_ID)
    
    if channel:
        await channel.send(f"🌍 **Radar Definitivo Iniciado.**\n"
                           f"Escaneando métricas en vivo e historial cada `{int(POLL_INTERVAL/60)}` minutos.")
    
    if historical_channel:
        await historical_channel.send(f"📚 **Canal de Desigualdades Conectado.**\n"
                                      f"Te notificaré partidos donde un equipo supere al otro por al menos `{MISMATCH_THRESHOLD}` goles de poder neto en la temporada.")
    
    async with aiohttp.ClientSession() as session:
        while not client.is_closed():
            try:
                fixtures = await get_live_fixtures(session)
                if fixtures:
                    await process_momentum(session, fixtures, channel, historical_channel)
            except Exception as e:
                print(f"Error: {e}")
            await asyncio.sleep(POLL_INTERVAL)

# ==========================================
# 🤖 COMANDOS INTERACTIVOS
# ==========================================
@client.event
async def on_message(message):
    if message.author == client.user: return
    comando = message.content.lower()
    global XG_JUMP_THRESHOLD 
    global MISMATCH_THRESHOLD

    if comando == "!comandos" or comando == "!help":
        msg = (
            f"🛠️ **MENÚ DE COMANDOS - RADAR DEFINITIVO** 🛠️\n"
            f"🔸 `!estado` - Muestra panel de control y caché.\n"
            f"🔸 `!partidos` - Lista los partidos con xG en vivo.\n"
            f"🔸 `!topxg` - Muestra el Top 5 de partidos peligrosos.\n"
            f"🔸 `!setumbral [número]` - Cambia el gatillo del Momentum xG.\n"
            f"🔸 `!setbrecha [número]` - Cambia la diferencia de goles histórica."
        )
        await message.channel.send(msg)

    elif comando == "!estado":
        msg = (f"🤖 **PANEL DE CONTROL**\n"
               f"📡 Partidos en radar vivo: `{len(xg_memory)}`\n"
               f"🧠 Historiales en Caché: `{len(team_stats_cache)}` equipos\n"
               f"🎯 Umbral Momentum: `+{XG_JUMP_THRESHOLD}` xG\n"
               f"⚖️ Brecha Desigualdad: `{MISMATCH_THRESHOLD}` Goles Netos")
        await message.channel.send(msg)

    elif comando.startswith("!setumbral "):
        try:
            XG_JUMP_THRESHOLD = float(comando.replace("!setumbral ", "").strip())
            await message.channel.send(f"✅ Umbral de Momentum actualizado a `+{XG_JUMP_THRESHOLD}`.")
        except ValueError:
            pass

    elif comando.startswith("!setbrecha "):
        try:
            MISMATCH_THRESHOLD = float(comando.replace("!setbrecha ", "").strip())
            await message.channel.send(f"✅ Brecha de desigualdad histórica actualizada a `{MISMATCH_THRESHOLD}`.")
        except ValueError:
            await message.channel.send("❌ Formato inválido. Usa: `!setbrecha 2.0`")

    elif comando == "!partidos":
        if not xg_memory:
            await message.channel.send("💤 Radar vacío.")
            return
        lines = [f"🌍 **RADAR ACTIVO: {len(xg_memory)} PARTIDOS**"]
        for data in xg_memory.values():
            lines.append(f"⏱️ `{data['min']}'` | {data['home']} **{data['gh']}-{data['ga']}** {data['away']} | xG: `{data['xg_home']}-{data['xg_away']}`")
        texto = "\n".join(lines)[:1900]
        await message.channel.send(texto)

@client.event
async def on_ready():
    print(f"✅ Conectado como {client.user}")
    client.loop.create_task(background_task())

if __name__ == "__main__":
    if DISCORD_TOKEN: client.run(DISCORD_TOKEN)
