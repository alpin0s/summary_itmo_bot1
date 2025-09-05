import asyncio
import datetime
import random 
import re
import sqlite3

import requests
from aiogram import Bot, Dispatcher, F, types
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

BOT_TOKEN = 'Ваш токен' 
GEMINI_API_KEY = 'Гемини токен' 
DB_FILE = "chats.db"

API_URL = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash-latest:generateContent?key={GEMINI_API_KEY}"

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
daily_message_cache = {}


def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("CREATE TABLE IF NOT EXISTS enabled_chats (chat_id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()

def load_enabled_chats():
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("SELECT chat_id FROM enabled_chats")
        chats = {row[0] for row in cursor.fetchall()}
        conn.close()
        return chats
    except sqlite3.Error as e:
        print(f"Ошибка при загрузке чатов из БД: {e}")
        return set()

def add_chat(chat_id):
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("INSERT INTO enabled_chats (chat_id) VALUES (?)", (chat_id,))
        conn.commit()
        conn.close()
        print(f"Чат {chat_id} добавлен в базу данных.")
        return True
    except sqlite3.IntegrityError:
        conn.close()
        return False
    except sqlite3.Error as e:
        print(f"Ошибка при добавлении чата в БД: {e}")
        return False

def remove_chat(chat_id):
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM enabled_chats WHERE chat_id = ?", (chat_id,))
        rows_affected = cursor.rowcount
        conn.commit()
        conn.close()
        if rows_affected > 0:
            print(f"Чат {chat_id} удален из базы данных.")
            return True
        return False
    except sqlite3.Error as e:
        print(f"Ошибка при удалении чата из БД: {e}")
        return False


def call_gemini_api(messages_text):
    """Вызывает API для создания структурированной сводки."""
    headers = {'Content-Type': 'application/json'}
    prompt_text = (
        "Проанализируй переписку в чате. Сгруппируй сообщения по темам. "
        "Для каждой темы укажи: название, краткое описание в скобках, количество сообщений и ID ПЕРВОГО сообщения. "
        "Твой ответ ДОЛЖЕН БЫТЬ ТОЛЬКО списком тем. Не добавляй заголовки, вступления или любой другой текст. "
        "Формат каждой строки должен быть строго таким: 'Название темы (краткое описание) (N сообщений) - ИД M'.\n"
        f"Сообщения для анализа:\n{messages_text}"
    )
    json_data = {'contents': [{'parts': [{'text': prompt_text}]}],'generationConfig': {'temperature': 0.4, 'maxOutputTokens': 2048,}}
    try:
        response = requests.post(API_URL, headers=headers, json=json_data, timeout=120)
        response.raise_for_status()
        data = response.json()
        return data['candidates'][0]['content']['parts'][0]['text']
    except Exception as e:
        print(f"Ошибка при вызове Gemini API для сводки: {e}")
    return None

def call_gemini_for_question(messages_text: str, user_question: str):
    """Вызывает API для ответа на вопрос пользователя."""
    headers = {'Content-Type': 'application/json'}
    prompt_text = (
        "Ты — умный AI-ассистент. Твоя задача — ответить на вопрос пользователя, основываясь ИСКЛЮЧИТЕЛЬНО на предоставленной истории сообщений из Telegram-чата. "
        "Проанализируй сообщения, найди самую важную и релевантную информацию по вопросу и дай краткий, но исчерпывающий ответ. "
        "Не придумывай ничего от себя. Если в тексте нет ответа, так и напиши: 'К сожалению, я не нашел ответа на ваш вопрос в недавней истории чата.'\n\n"
        f"--- ИСТОРИЯ СООБЩЕНИЙ ---\n{messages_text}\n\n"
        f"--- ВОПРОС ПОЛЬЗОВАТЕЛЯ ---\n{user_question}"
    )
    json_data = {'contents': [{'parts': [{'text': prompt_text}]}],'generationConfig': {'temperature': 0.7, 'maxOutputTokens': 1024,}}
    try:
        response = requests.post(API_URL, headers=headers, json=json_data, timeout=120)
        response.raise_for_status()
        data = response.json()
        return data['candidates'][0]['content']['parts'][0]['text']
    except Exception as e:
        print(f"Ошибка при вызове Gemini API для вопроса: {e}")
    return None


async def create_and_send_summary(chat_id: int, summary_title: str):
    """Создает и отправляет отчет на основе ВСЕХ сообщений в дневном кэше."""
    messages_to_process = daily_message_cache.get(chat_id, [])
    
    if not messages_to_process:
        print(f"Нет сообщений для создания сводки для чата {chat_id}.")
        if "вручную" in summary_title:
             await bot.send_message(chat_id, "Сообщений для отчета еще нет.")
        return

    print(f"Создаю '{summary_title}' для чата {chat_id} на основе {len(messages_to_process)} сообщений.")
    messages_for_api = "\n".join([f"[{msg['id']}] {msg['text']}" for msg in messages_to_process])
    api_response = call_gemini_api(messages_for_api)

    if not api_response:
        await bot.send_message(chat_id, "Не удалось получить ответ от AI для создания сводки.")
        return

    topic_pattern = re.compile(r"(.+?)\s+\((.*?)\)\s+\((\d+)(?: сообщени[й|я|е])?\)\s+-\s+(?:ИД\s)?(\d+)", re.MULTILINE)
    topics = topic_pattern.findall(api_response)

    if not topics:
        print(f"Не удалось разобрать ответ от AI для чата {chat_id}:\n{api_response}")
        return

    summary_message = f"**{summary_title}**\n\n"
    for title, desc, count, first_message_id in topics:
        link_chat_id = str(chat_id).replace('-100', '')
        link = f"https://t.me/c/{link_chat_id}/{first_message_id}"
        summary_message += f"💬 *{title.strip()}* ({desc}) - [{count} сообщений]({link})\n"

    try:
        await bot.send_message(chat_id, summary_message, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)
        print(f"Отчёт для чата {chat_id} успешно отправлен.")
    except TelegramBadRequest as e:
        print(f"Ошибка при отправке сообщения в чат {chat_id}: {e}")


async def send_summary_with_delay(chat_id: int, delay: float):
    """Отправляет отчет для одного чата после заданной задержки."""
    print(f"Отчет для чата {chat_id} будет отправлен через {delay:.1f} секунд.")
    await asyncio.sleep(delay)
    await create_and_send_summary(chat_id, "📆 Что обсуждалось в чате за сегодня:")

async def scheduled_summary_loop():
    """Асинхронный цикл, который запускает рассылку в 'окне' после 20:00."""
    while True:
        now = datetime.datetime.now()
        run_time = now.replace(hour=20, minute=0, second=0, microsecond=0)
        if now > run_time:
            run_time += datetime.timedelta(days=1)
        
        sleep_seconds = (run_time - now).total_seconds()
        print(f"Следующая плановая рассылка через {sleep_seconds/3600:.2f} часов (в 20:00).")
        await asyncio.sleep(sleep_seconds)
        
        print("=== НАЧАЛО ПЕРИОДА РАССЫЛКИ (20:00) ===")
        enabled_chats = load_enabled_chats()

        for chat_id in enabled_chats:
            delay = random.uniform(0, 300) 
            asyncio.create_task(send_summary_with_delay(chat_id, delay))

async def midnight_cleanup_loop():
    while True:
        now = datetime.datetime.now()
        run_time = (now + datetime.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        sleep_seconds = (run_time - now).total_seconds()
        print(f"Следующая очистка кэша через {sleep_seconds/3600:.2f} часов (в 00:00).")
        await asyncio.sleep(sleep_seconds)

        print("=== ПОЛНОЧЬ! ОЧИСТКА ДНЕВНОГО КЭША ===")
        daily_message_cache.clear()

@dp.message(Command("enable"), F.chat.type.in_({'group', 'supergroup'}))
async def enable_summary_command(message: types.Message):
    if add_chat(message.chat.id): await message.reply("✅ Суммаризация включена. Отчеты в 20:00, вопросы до 00:00.")
    else: await message.reply("ℹ️ Суммаризация уже была включена.")

@dp.message(Command("disable"), F.chat.type.in_({'group', 'supergroup'}))
async def disable_summary_command(message: types.Message):
    if remove_chat(message.chat.id): await message.reply("❌ Суммаризация отключена.")
    else: await message.reply("ℹ️ Суммаризация и так была выключена.")

@dp.message(Command("summarize_now"), F.chat.type.in_({'group', 'supergroup'}))
async def summarize_now_command(message: types.Message):
    """Создает отчет по текущему состоянию дневного кэша. Не очищает его."""
    await message.reply("⏱️ Создаю отчет по всем сообщениям за сегодня...")
    await create_and_send_summary(message.chat.id, "📊 Сводка по сообщениям (запрошена вручную):")

@dp.message(Command("question"), F.chat.type.in_({'group', 'supergroup'}))
async def question_command(message: types.Message, command: CommandObject):
    """Отвечает на вопрос, используя все сообщения из дневного кэша."""
    if not command.args:
        await message.reply("Пожалуйста, задайте ваш вопрос после команды.")
        return

    await message.reply("🔍 Ищу ответ во всех сообщениях за сегодня...")
    all_messages_for_today = daily_message_cache.get(message.chat.id, [])

    if not all_messages_for_today:
        await message.reply("Пока нет сообщений за сегодня для анализа.")
        return

    messages_for_api = "\n".join([msg['text'] for msg in all_messages_for_today])
    answer = call_gemini_for_question(messages_for_api, command.args)

    if answer: await message.reply(answer)
    else: await message.reply("Не удалось получить ответ от AI.")

@dp.message(F.chat.type.in_({'group', 'supergroup'}))
async def handle_group_messages(message: Message):
    """Сохраняет сообщения в единый дневной кэш."""
    chat_id = message.chat.id
    if chat_id not in load_enabled_chats(): return

    if chat_id not in daily_message_cache: daily_message_cache[chat_id] = []
    
    if message.text:
        daily_message_cache[chat_id].append({"text": message.text, "id": message.message_id})


async def main():
    init_db()
    asyncio.create_task(scheduled_summary_loop())
    asyncio.create_task(midnight_cleanup_loop())
    
    print("--- Бот запущен с надежной логикой рассылки и очистки ---")
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("--- Бот остановлен вручную ---")
    except Exception as e:

        print(f"!!! КРИТИЧЕСКАЯ ОШИБКА ПРИ ЗАПУСКЕ: {e}")
