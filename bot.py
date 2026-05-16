import os
import sqlite3
import tempfile
from openai import OpenAI
from telegram import Update
from telegram.ext import (Application, MessageHandler,
                          CommandHandler, filters, ContextTypes)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "ВСТАВЬ_ТОКЕН")
DEEPSEEK_KEY = os.getenv("DEEPSEEK_KEY", "ВСТАВЬ_DEEPSEEK_КЛЮЧ")
GROQ_KEY = os.getenv("GROQ_KEY", "ВСТАВЬ_GROQ_КЛЮЧ")
TAVILY_KEY = os.getenv("TAVILY_KEY", "ВСТАВЬ_TAVILY_КЛЮЧ")

deepseek = OpenAI(api_key=DEEPSEEK_KEY, base_url="https://api.deepseek.com")
groq_client = OpenAI(api_key=GROQ_KEY, base_url="https://api.groq.com/openai/v1")
import httpx as http

conn = sqlite3.connect("brain.db")
conn.execute("""CREATE TABLE IF NOT EXISTS messages
               (user_id INTEGER, role TEXT, content TEXT,
                ts DATETIME DEFAULT CURRENT_TIMESTAMP)""")
conn.execute("""CREATE TABLE IF NOT EXISTS notes
               (user_id INTEGER, content TEXT,
                ts DATETIME DEFAULT CURRENT_TIMESTAMP)""")
conn.commit()

def get_history(user_id, limit=20):
    rows = conn.execute(
        "SELECT role, content FROM messages "
        "WHERE user_id=? ORDER BY ts DESC LIMIT ?",
        (user_id, limit)
    ).fetchall()
    return [{"role": r, "content": c} for r, c in reversed(rows)]

def save_message(user_id, role, content):
    conn.execute(
        "INSERT INTO messages VALUES (?,?,?,CURRENT_TIMESTAMP)",
        (user_id, role, content)
    )
    conn.commit()

async def search_web(query: str) -> str:
    async with http.AsyncClient() as client:
        resp = await client.post(
            "https://api.tavily.com/search",
            json={
                "api_key": TAVILY_KEY,
                "query": query,
                "max_results": 3,
                "include_answer": True
            }
        )
        data = resp.json()
        if data.get("answer"):
            return f"🌐 {data['answer']}"
        results = data.get("results", [])
        if not results:
            return "Ничего не нашёл."
        text = "🌐 Результаты поиска:\n\n"
        for r in results[:3]:
            text += f"• {r['title']}\n{r['content'][:200]}...\n\n"
        return text

async def ask_jarvis(user_id, text):
    save_message(user_id, "user", text)
    
    search_keywords = ["найди", "поищи", "что такое", "кто такой", 
                       "погода", "новости", "курс", "цена", "когда", 
                       "где", "search", "find", "what is"]
    
    needs_search = any(kw in text.lower() for kw in search_keywords)
    
    search_result = ""
    if needs_search:
        search_result = await search_web(text)
    
    messages = [
        {"role": "system", "content": 
         "Ты умный личный помощник Джарвис. "
         "Отвечай кратко и по делу. "
         "Говори на языке пользователя. "
         + (f"Данные из интернета: {search_result}" if search_result else "")
        },
    ] + get_history(user_id)
    
    response = deepseek.chat.completions.create(
        model="deepseek-chat",
        messages=messages
    )
    reply = response.choices[0].message.content
    save_message(user_id, "assistant", reply)
    
    if search_result:
        return search_result + "\n\n💬 " + reply
    return reply

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Я твой Джарвис 🤖\n\n"
        "Пиши или отправляй голосовые!\n\n"
        "/save текст — сохранить заметку\n"
        "/notes — мои заметки\n"
        "/clear — очистить историю"
    )

async def save_note(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = " ".join(context.args)
    if not text:
        await update.message.reply_text("Напиши: /save твой текст")
        return
    conn.execute(
        "INSERT INTO notes VALUES (?,?,CURRENT_TIMESTAMP)",
        (update.effective_user.id, text)
    )
    conn.commit()
    await update.message.reply_text("✅ Сохранено!")

async def show_notes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = conn.execute(
        "SELECT content FROM notes WHERE user_id=? ORDER BY ts DESC LIMIT 10",
        (update.effective_user.id,)
    ).fetchall()
    if not rows:
        await update.message.reply_text("Заметок нет. Добавь: /save текст")
        return
    text = "📝 Твои заметки:\n\n"
    for (content,) in rows:
        text += f"• {content}\n"
    await update.message.reply_text(text)

async def clear_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn.execute(
        "DELETE FROM messages WHERE user_id=?",
        (update.effective_user.id,)
    )
    conn.commit()
    await update.message.reply_text("🗑 История очищена")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id, action="typing"
    )
    reply = await ask_jarvis(user_id, update.message.text)
    await update.message.reply_text(reply)

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id, action="typing"
    )
    voice = update.message.voice
    file = await context.bot.get_file(voice.file_id)
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        await file.download_to_drive(tmp.name)
        with open(tmp.name, "rb") as audio:
            transcript = groq_client.audio.transcriptions.create(
                model="whisper-large-v3-turbo",
                file=audio
            )
    text = transcript.text
    await update.message.reply_text(f"🎤 Распознал: {text}")
    reply = await ask_jarvis(user_id, text)
    await update.message.reply_text(reply)

app = Application.builder().token(TELEGRAM_TOKEN).build()
app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("save", save_note))
app.add_handler(CommandHandler("notes", show_notes))
app.add_handler(CommandHandler("clear", clear_history))
app.add_handler(MessageHandler(filters.VOICE, handle_voice))
app.add_handler(MessageHandler(
    filters.TEXT & ~filters.COMMAND, handle_message
))

print("✅ Джарвис с голосом запущен!")
app.run_polling()