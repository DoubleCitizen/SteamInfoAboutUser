import datetime
import logging
import os
import time
from typing import Optional, Any, Dict, List

import requests
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# === Настройка ===
load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OLLAMA_URL = "http://ollama:11434/api/chat"
STEAM_API_KEY = os.getenv("STEAM_API_KEY")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# === Вспомогательные функции ===

def resolve_steam_id(user_input: str) -> Optional[str]:
    """Преобразует SteamID64, vanity URL или кастомный ник в SteamID64."""
    user_input = user_input.strip()
    if user_input.isdigit() and len(user_input) >= 15:
        return user_input

    # Попытка разрешить как vanity URL
    url = "https://api.steampowered.com/ISteamUser/ResolveVanityURL/v0001/"
    try:
        resp = requests.get(url, params={"key": STEAM_API_KEY, "vanityurl": user_input}, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if data["response"]["success"] == 1:
            return data["response"]["steamid"]
    except Exception as e:
        logger.error(f"Ошибка при разрешении vanity URL '{user_input}': {e}")
    return None


def fetch_steam_profile(steam_id: str) -> Optional[Dict[str, Any]]:
    """Получает полные данные профиля, друзей и игр."""
    base_url = "https://api.steampowered.com"
    headers = {"Content-Type": "application/json"}

    # --- Профиль ---
    profile_resp = requests.get(
        f"{base_url}/ISteamUser/GetPlayerSummaries/v0002/",
        params={"key": STEAM_API_KEY, "steamids": steam_id},
        headers=headers,
        timeout=10,
    )
    if profile_resp.status_code != 200:
        return None
    players = profile_resp.json().get("response", {}).get("players", [])
    if not players:
        return None
    user_data = players[0]

    # --- Друзья ---
    friends_list = []
    try:
        friends_resp = requests.get(
            f"{base_url}/ISteamUser/GetFriendList/v0001/",
            params={"key": STEAM_API_KEY, "steamid": steam_id, "relationship": "friend"},
            headers=headers,
            timeout=15,
        )
        if friends_resp.status_code == 200:
            friends = friends_resp.json().get("friendslist", {}).get("friends", [])
            friend_ids = [f["steamid"] for f in friends]
            if friend_ids:
                for i in range(0, len(friend_ids), 100):
                    batch = ",".join(friend_ids[i:i + 100])
                    profiles_resp = requests.get(
                        f"{base_url}/ISteamUser/GetPlayerSummaries/v0002/",
                        params={"key": STEAM_API_KEY, "steamids": batch},
                        headers=headers,
                        timeout=15,
                    )
                    if profiles_resp.status_code == 200:
                        batch_profiles = profiles_resp.json().get("response", {}).get("players", [])
                        friends_list.extend(batch_profiles)
    except Exception as e:
        logger.warning(f"Ошибка загрузки друзей: {e}")

    # --- Игры ---
    owned_games = []
    try:
        games_resp = requests.get(
            f"{base_url}/IPlayerService/GetOwnedGames/v0001/",
            params={
                "key": STEAM_API_KEY,
                "steamid": steam_id,
                "include_appinfo": 1,
                "include_played_free_games": 1,
            },
            headers=headers,
            timeout=15,
        )
        if games_resp.status_code == 200:
            all_games = games_resp.json().get("response", {}).get("games", [])
            sorted_games = sorted(all_games, key=lambda g: g.get("playtime_forever", 0), reverse=True)
            owned_games = sorted_games[:10]
    except Exception as e:
        logger.warning(f"Ошибка загрузки игр: {e}")

    return {
        "profile": user_data,
        "friends": friends_list,
        "owned_games_sample": owned_games,
    }


def simplify_steam_profile(data: Dict[str, Any]) -> str:
    profile = data["profile"]
    friends = data["friends"]
    games = data["owned_games_sample"]

    name = profile.get("realname") or "Not specified"
    persona = profile.get("personaname") or "No nickname"
    country = profile.get("loccountrycode") or "Unknown"
    created = profile.get("timecreated")
    created_str = datetime.datetime.utcfromtimestamp(created).strftime("%m/%d/%Y") if created else "Unknown"

    friend_countries = {}
    for f in friends:
        c = f.get("loccountrycode", "??")
        friend_countries[c] = friend_countries.get(c, 0) + 1
    top_countries = ", ".join(f"{cnt} from {c}" for c, cnt in sorted(friend_countries.items(), key=lambda x: -x[1])[:5])

    total_playtime = sum(g.get("playtime_forever", 0) for g in games) / 60
    game_titles = ", ".join(g["name"] for g in games[:10])

    return f"""Steam User:
- Display name: {persona}
- Real name: {name}
- Country: {country}
- Account created: {created_str}

Friends:
- Total friends: {len(friends)}
- Top friend countries: {top_countries}

Gaming activity:
- Sample of owned games: {len(games)}
- Total playtime (in sample): ~{total_playtime:.1f} hours
- Example games: {game_titles}"""


async def llm_message(message: str) -> str:
    prompt = f"""You're a cheeky, sarcastic gamer from a chaotic Telegram group—think meme lord with a heart of gold-plated snark. Playfully roast this Steam user like you're teasing your weird-but-lovable roommate.

    Rules:
    - EXACTLY 2 sentences.
    - (≤50 words): highlight their absurdly niche gaming habits or bizarre playtime choices—bonus if it screams “I haven’t seen sunlight since 2019” 🌙🎮.
    - (≤50 words): gently jab at their life arc—maybe they’re globe-hopping while grinding CS:GO, or collecting hats in TF2 like it’s a retirement plan 🇺🇿✈️📉.
    - Use emojis for flavor, not cruelty (e.g., 🎮 = passion, 🕰️ = time well… spent?, 🧳 = eternal traveler, 🇷🇺/🇺🇿 = plot twist).
    - NO insults. NO assumptions about mental health, loneliness, or failure. Keep it light, witty, and based ONLY on visible Steam activity.
    - If you sound mean or generic, you lose XP.

    Steam profile summary:
    Напиши на русском языке
    {message}
    """
    try:
        payload = {
            "model": "phi3:mini",
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
        }
        resp = requests.post(OLLAMA_URL, json=payload, timeout=30)
        resp.raise_for_status()
        answer = resp.json()["message"]["content"]
    except Exception as e:
        logger.error(f"Ошибка ИИ: {e}")
        answer = "Sorry, I'm having trouble thinking right now. 😕"

    return f"{answer}\n\n{message}"


# === Обработчики Telegram ===

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Отправь мне свой SteamID (цифровой или кастомный URL), и я покажу информацию о твоём профиле.\n"
        "Примеры:\n"
        "- 76561198000000000\n"
        "- https://steamcommunity.com/id/ваш_ник/\n"
        "- ваш_ник"
    )


async def handle_steam_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    # Извлечение ID из ссылки или текста
    steam_id = None
    if text.startswith("http"):
        if "/id/" in text:
            vanity = text.split("/id/")[-1].split("/")[0]
            steam_id = resolve_steam_id(vanity)
        elif "/profiles/" in text:
            steam_id = text.split("/profiles/")[-1].split("/")[0]
        else:
            await update.message.reply_text("❌ Неверный формат ссылки.")
            return
    else:
        steam_id = resolve_steam_id(text)

    if not steam_id:
        await update.message.reply_text(
            "❌ Не удалось найти профиль. Убедитесь, что ник верный и профиль публичный."
        )
        return

    profile_data = fetch_steam_profile(steam_id)
    if not profile_data:
        await update.message.reply_text("❌ Не удалось получить данные. Профиль приватный или не существует.")
        return

    # Краткий вывод в Telegram
    p = profile_data["profile"]
    status_map = {0: "Оффлайн", 1: "Онлайн", 2: "Занят", 3: "Отошёл", 4: "Спит", 5: "Хочет поиграть", 6: "Не видно"}
    status = status_map.get(p.get("personastate"), "Неизвестно")
    visibility = "Публичный" if p.get("communityvisibilitystate") == 3 else "Приватный"

    caption = (
        f"👤 <b>Имя:</b> {p.get('personaname', '—')}\n"
        f"🌐 <b>Статус:</b> {status}\n"
        f"👁️ <b>Видимость:</b> {visibility}\n"
        f"🔗 <a href='{p.get('profileurl', '')}'>Открыть профиль</a>"
    )

    avatar = p.get("avatarfull")
    if avatar:
        await update.message.reply_photo(photo=avatar, caption=caption, parse_mode="HTML")
    else:
        await update.message.reply_text(caption, parse_mode="HTML")

    # Отправка анализа + LLM-роаст
    simplified = simplify_steam_profile(profile_data)
    roast = await llm_message(simplified)
    await update.message.reply_text(roast)


# === Инициализация ===

def wait_for_ollama(url: str, timeout: int = 60):
    start = time.time()
    while time.time() - start < timeout:
        try:
            resp = requests.get(f"{url}/api/tags", timeout=5)
            if resp.status_code == 200:
                logger.info("✅ Ollama is ready!")
                return True
        except Exception:
            logger.info("⏳ Waiting for Ollama...")
            time.sleep(3)
    raise TimeoutError("Ollama did not start in time")


def load_model_if_needed(model_name: str = "phi3:mini"):
    try:
        resp = requests.get("http://ollama:11434/api/tags", timeout=10)
        if resp.status_code == 200:
            models = resp.json().get("models", [])
            if any(model_name == m["name"] for m in models):
                logger.info(f"✅ Модель {model_name} уже загружена.")
                return
    except Exception as e:
        logger.warning(f"Не удалось проверить модели: {e}")

    logger.info(f"📥 Загружаем модель: {model_name}...")
    try:
        resp = requests.post("http://ollama:11434/api/pull", json={"name": model_name}, stream=True, timeout=600)
        resp.raise_for_status()
        for _ in resp.iter_lines():
            pass
        logger.info(f"✅ Модель {model_name} успешно загружена!")
    except Exception as e:
        logger.error(f"❌ Ошибка загрузки модели {model_name}: {e}")
        raise


def main():
    wait_for_ollama("http://ollama:11434", timeout=120)
    load_model_if_needed("phi3:mini")

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_steam_id))

    logger.info("Бот запущен...")
    app.run_polling()


if __name__ == "__main__":
    main()