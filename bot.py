#!/usr/bin/env python3
"""
Telegram Mod Bot - Единый файл (без cachetools)
Просто скопируйте этот код в bot.py и запустите!
"""

import os
import re
import sqlite3
import json
import logging
import asyncio
import time
from datetime import datetime, timedelta
from collections import defaultdict

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ChatPermissions
from telegram.ext import (
    Application, CommandHandler, MessageHandler, 
    CallbackQueryHandler, filters, ContextTypes
)
from telegram.constants import ParseMode

# ==================== НАСТРОЙКИ ====================
BOT_TOKEN = "8032712809:AAFcmS1G4xKURy2MZ9izAK8Ne8HXg8EIr8I"  # ВСТАВЬТЕ СВОЙ ТОКЕН СЮДА

# Настройки по умолчанию
DEFAULT_WARN_LIMIT = 3
DEFAULT_ANTIFLOOD_COUNT = 5
DEFAULT_ANTIFLOOD_SECONDS = 10
DEFAULT_WELCOME_MESSAGE = "👋 Добро пожаловать, {name}!\nПожалуйста, ознакомься с правилами: /rules"
DEFAULT_RULES = """📋 Правила чата:
1. Уважайте друг друга
2. Не спамить
3. Запрещены оскорбления
4. Не рекламировать
5. Администратор всегда прав 😉"""

# ==================== НАСТРОЙКА ЛОГИРОВАНИЯ ====================
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ==================== ПРОСТОЙ КЭШ ДЛЯ АНТИФЛУДА (вместо cachetools) ====================
class SimpleCache:
    def __init__(self, maxsize=10000, ttl=60):
        self.maxsize = maxsize
        self.ttl = ttl
        self.cache = {}
        self.timestamps = {}
    
    def __contains__(self, key):
        self._cleanup()
        return key in self.cache
    
    def __getitem__(self, key):
        self._cleanup()
        return self.cache.get(key, [])
    
    def __setitem__(self, key, value):
        self._cleanup()
        self.cache[key] = value
        self.timestamps[key] = time.time()
    
    def _cleanup(self):
        now = time.time()
        expired = [k for k, ts in self.timestamps.items() if now - ts > self.ttl]
        for k in expired:
            del self.cache[k]
            del self.timestamps[k]

# ==================== БАЗА ДАННЫХ (SQLite) ====================
class Database:
    def __init__(self):
        self.conn = sqlite3.connect('bot_database.db', check_same_thread=False)
        self.cursor = self.conn.cursor()
        self.create_tables()
    
    def create_tables(self):
        # Настройки чатов
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS chat_settings (
                chat_id INTEGER PRIMARY KEY,
                welcome_message TEXT,
                rules TEXT,
                warn_limit INTEGER DEFAULT 3,
                antiflood_enabled BOOLEAN DEFAULT 1,
                antiflood_count INTEGER DEFAULT 5,
                antiflood_seconds INTEGER DEFAULT 10,
                bad_words TEXT DEFAULT '[]'
            )
        ''')
        
        # Предупреждения
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS warnings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER,
                user_id INTEGER,
                warned_by INTEGER,
                reason TEXT,
                created_at TIMESTAMP
            )
        ''')
        
        # Заглушенные пользователи
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS muted_users (
                chat_id INTEGER,
                user_id INTEGER,
                mute_until TIMESTAMP,
                PRIMARY KEY (chat_id, user_id)
            )
        ''')
        
        # Статистика пользователей
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_stats (
                chat_id INTEGER,
                user_id INTEGER,
                messages_count INTEGER DEFAULT 0,
                first_seen TIMESTAMP,
                last_seen TIMESTAMP,
                PRIMARY KEY (chat_id, user_id)
            )
        ''')
        
        self.conn.commit()
    
    # Настройки чата
    def get_chat_settings(self, chat_id):
        self.cursor.execute("SELECT * FROM chat_settings WHERE chat_id = ?", (chat_id,))
        settings = self.cursor.fetchone()
        
        if not settings:
            self.cursor.execute('''
                INSERT INTO chat_settings (chat_id, welcome_message, rules)
                VALUES (?, ?, ?)
            ''', (chat_id, DEFAULT_WELCOME_MESSAGE, DEFAULT_RULES))
            self.conn.commit()
            
            self.cursor.execute("SELECT * FROM chat_settings WHERE chat_id = ?", (chat_id,))
            settings = self.cursor.fetchone()
        
        columns = [description[0] for description in self.cursor.description]
        return dict(zip(columns, settings))
    
    def update_welcome(self, chat_id, message):
        self.cursor.execute("UPDATE chat_settings SET welcome_message = ? WHERE chat_id = ?", (message, chat_id))
        self.conn.commit()
    
    def update_rules(self, chat_id, rules):
        self.cursor.execute("UPDATE chat_settings SET rules = ? WHERE chat_id = ?", (rules, chat_id))
        self.conn.commit()
    
    def get_bad_words(self, chat_id):
        self.cursor.execute("SELECT bad_words FROM chat_settings WHERE chat_id = ?", (chat_id,))
        result = self.cursor.fetchone()
        if result and result[0]:
            return json.loads(result[0])
        return []
    
    def update_bad_words(self, chat_id, words_list):
        self.cursor.execute("UPDATE chat_settings SET bad_words = ? WHERE chat_id = ?", (json.dumps(words_list), chat_id))
        self.conn.commit()
    
    # Предупреждения
    def add_warning(self, chat_id, user_id, warned_by, reason=None):
        self.cursor.execute('''
            INSERT INTO warnings (chat_id, user_id, warned_by, reason, created_at)
            VALUES (?, ?, ?, ?, ?)
        ''', (chat_id, user_id, warned_by, reason, datetime.now()))
        self.conn.commit()
        return self.get_warnings_count(chat_id, user_id)
    
    def get_warnings_count(self, chat_id, user_id):
        self.cursor.execute('''
            SELECT COUNT(*) FROM warnings
            WHERE chat_id = ? AND user_id = ?
        ''', (chat_id, user_id))
        return self.cursor.fetchone()[0]
    
    def remove_warning(self, chat_id, user_id):
        self.cursor.execute('''
            DELETE FROM warnings
            WHERE id = (
                SELECT id FROM warnings
                WHERE chat_id = ? AND user_id = ?
                ORDER BY created_at DESC LIMIT 1
            )
        ''', (chat_id, user_id))
        self.conn.commit()
        return self.get_warnings_count(chat_id, user_id)
    
    def clear_warnings(self, chat_id, user_id):
        self.cursor.execute("DELETE FROM warnings WHERE chat_id = ? AND user_id = ?", (chat_id, user_id))
        self.conn.commit()
    
    # Муты
    def add_mute(self, chat_id, user_id, duration_seconds):
        mute_until = datetime.now() + timedelta(seconds=duration_seconds)
        self.cursor.execute('''
            INSERT OR REPLACE INTO muted_users (chat_id, user_id, mute_until)
            VALUES (?, ?, ?)
        ''', (chat_id, user_id, mute_until))
        self.conn.commit()
        return mute_until
    
    def remove_mute(self, chat_id, user_id):
        self.cursor.execute("DELETE FROM muted_users WHERE chat_id = ? AND user_id = ?", (chat_id, user_id))
        self.conn.commit()
    
    def is_muted(self, chat_id, user_id):
        self.cursor.execute('''
            SELECT mute_until FROM muted_users
            WHERE chat_id = ? AND user_id = ?
        ''', (chat_id, user_id))
        
        result = self.cursor.fetchone()
        if not result:
            return False
        
        mute_until = datetime.fromisoformat(result[0])
        if mute_until > datetime.now():
            return True
        else:
            self.remove_mute(chat_id, user_id)
            return False
    
    # Статистика
    def update_user_stats(self, chat_id, user_id, username, first_name):
        now = datetime.now()
        self.cursor.execute('''
            INSERT OR REPLACE INTO user_stats 
            (chat_id, user_id, messages_count, first_seen, last_seen)
            VALUES (?, ?, 
                COALESCE(
                    (SELECT messages_count + 1 FROM user_stats 
                     WHERE chat_id = ? AND user_id = ?),
                    1
                ),
                COALESCE(
                    (SELECT first_seen FROM user_stats 
                     WHERE chat_id = ? AND user_id = ?),
                    ?
                ),
                ?)
        ''', (chat_id, user_id, chat_id, user_id, chat_id, user_id, now, now))
        self.conn.commit()
    
    def get_user_stats(self, chat_id, user_id):
        self.cursor.execute('''
            SELECT * FROM user_stats
            WHERE chat_id = ? AND user_id = ?
        ''', (chat_id, user_id))
        
        result = self.cursor.fetchone()
        if result:
            columns = [description[0] for description in self.cursor.description]
            return dict(zip(columns, result))
        return None

# ==================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ====================
def parse_time(time_str):
    """Парсит время из строки (5m, 1h, 2d)"""
    if not time_str:
        return None
    
    units = {
        's': 1,
        'm': 60,
        'h': 3600,
        'd': 86400
    }
    
    match = re.match(r'(\d+)([smhd])', time_str.lower())
    if match:
        value, unit = match.groups()
        return int(value) * units.get(unit, 60)
    
    return None

def format_time(seconds):
    """Форматирует секунды в человекочитаемый вид"""
    if seconds < 60:
        return f"{seconds} сек"
    elif seconds < 3600:
        return f"{seconds // 60} мин"
    elif seconds < 86400:
        return f"{seconds // 3600} ч"
    else:
        return f"{seconds // 86400} дн"

def create_mute_permissions():
    """Создает права для заглушенного пользователя"""
    return ChatPermissions(
        can_send_messages=False,
        can_send_media_messages=False,
        can_send_polls=False,
        can_send_other_messages=False,
        can_add_web_page_previews=False
    )

async def is_admin(update, context, user_id=None):
    """Проверяет, является ли пользователь администратором"""
    if user_id is None:
        user_id = update.effective_user.id
    
    chat = update.effective_chat
    
    try:
        member = await chat.get_member(user_id)
        return member.status in ['administrator', 'creator']
    except:
        return False

# ==================== ИНИЦИАЛИЗАЦИЯ БД И КЭША ====================
db = Database()
flood_cache = SimpleCache(maxsize=10000, ttl=60)

# ==================== КОМАНДЫ МОДЕРАЦИИ ====================
async def ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        await update.message.reply_text("❌ Эта команда только для администраторов!")
        return
    
    if not update.message.reply_to_message:
        await update.message.reply_text("❌ Ответьте на сообщение пользователя, которого хотите забанить!")
        return
    
    user_to_ban = update.message.reply_to_message.from_user
    
    try:
        await update.effective_chat.ban_member(user_to_ban.id)
        await update.message.reply_text(f"✅ Пользователь {user_to_ban.full_name} забанен.")
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {str(e)}")

async def unban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        await update.message.reply_text("❌ Эта команда только для администраторов!")
        return
    
    if not context.args:
        await update.message.reply_text("❌ Укажите ID пользователя!\nПример: /unban 123456789")
        return
    
    try:
        user_id = int(context.args[0])
        await update.effective_chat.unban_member(user_id)
        await update.message.reply_text(f"✅ Пользователь {user_id} разбанен.")
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {str(e)}")

async def mute_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        await update.message.reply_text("❌ Эта команда только для администраторов!")
        return
    
    if not update.message.reply_to_message:
        await update.message.reply_text("❌ Ответьте на сообщение пользователя, которого хотите заглушить!")
        return
    
    user_to_mute = update.message.reply_to_message.from_user
    
    duration = None
    if context.args:
        duration = parse_time(context.args[0])
    
    if not duration:
        duration = 3600  # 1 час по умолчанию
    
    mute_until = datetime.now() + timedelta(seconds=duration)
    
    try:
        await update.effective_chat.restrict_member(
            user_to_mute.id,
            permissions=create_mute_permissions(),
            until_date=mute_until
        )
        db.add_mute(update.effective_chat.id, user_to_mute.id, duration)
        await update.message.reply_text(
            f"🔇 Пользователь {user_to_mute.full_name} заглушен на {format_time(duration)}."
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {str(e)}")

async def unmute_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        await update.message.reply_text("❌ Эта команда только для администраторов!")
        return
    
    if not update.message.reply_to_message:
        await update.message.reply_text("❌ Ответьте на сообщение пользователя!")
        return
    
    user_to_unmute = update.message.reply_to_message.from_user
    
    try:
        await update.effective_chat.restrict_member(
            user_to_unmute.id,
            permissions=ChatPermissions(can_send_messages=True)
        )
        db.remove_mute(update.effective_chat.id, user_to_unmute.id)
        await update.message.reply_text(f"🔊 Пользователь {user_to_unmute.full_name} разблокирован.")
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {str(e)}")

async def warn_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        await update.message.reply_text("❌ Эта команда только для администраторов!")
        return
    
    if not update.message.reply_to_message:
        await update.message.reply_text("❌ Ответьте на сообщение пользователя!")
        return
    
    user_to_warn = update.message.reply_to_message.from_user
    reason = ' '.join(context.args) if context.args else "Без причины"
    
    warn_count = db.add_warning(update.effective_chat.id, user_to_warn.id, update.effective_user.id, reason)
    settings = db.get_chat_settings(update.effective_chat.id)
    warn_limit = settings.get('warn_limit', DEFAULT_WARN_LIMIT)
    
    if warn_count >= warn_limit:
        try:
            await update.effective_chat.ban_member(user_to_warn.id)
            db.clear_warnings(update.effective_chat.id, user_to_warn.id)
            await update.message.reply_text(
                f"🚫 {user_to_warn.full_name} получил {warn_count}/{warn_limit} предупреждений и был забанен.\n"
                f"Причина последнего: {reason}"
            )
        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка при бане: {str(e)}")
    else:
        await update.message.reply_text(
            f"⚠️ {user_to_warn.full_name} получил предупреждение ({warn_count}/{warn_limit})\n"
            f"Причина: {reason}"
        )

async def unwarn_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        await update.message.reply_text("❌ Эта команда только для администраторов!")
        return
    
    if not update.message.reply_to_message:
        await update.message.reply_text("❌ Ответьте на сообщение пользователя!")
        return
    
    user_to_unwarn = update.message.reply_to_message.from_user
    warn_count = db.remove_warning(update.effective_chat.id, user_to_unwarn.id)
    
    await update.message.reply_text(
        f"✅ С пользователя {user_to_unwarn.full_name} снято предупреждение.\n"
        f"Текущее количество: {warn_count}"
    )

async def clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        await update.message.reply_text("❌ Эта команда только для администраторов!")
        return
    
    count = 10
    if context.args:
        try:
            count = int(context.args[0])
            if count > 100:
                count = 100
        except ValueError:
            await update.message.reply_text("❌ Укажите число!")
            return
    
    if not update.message.reply_to_message:
        await update.message.reply_text("❌ Ответьте на сообщение, с которого начать удаление!")
        return
    
    deleted = 0
    try:
        message_id = update.message.reply_to_message.message_id
        for i in range(count):
            try:
                await update.effective_chat.delete_message(message_id + i)
                deleted += 1
                await asyncio.sleep(0.5)
            except:
                pass
        
        result_msg = await update.message.reply_text(f"✅ Удалено {deleted} сообщений.")
        await asyncio.sleep(3)
        await update.message.delete()
        await result_msg.delete()
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {str(e)}")

async def pin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        await update.message.reply_text("❌ Эта команда только для администраторов!")
        return
    
    if not update.message.reply_to_message:
        await update.message.reply_text("❌ Ответьте на сообщение для закрепления!")
        return
    
    try:
        await update.message.reply_to_message.pin(disable_notification=True)
        await update.message.reply_text("📌 Сообщение закреплено.")
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {str(e)}")

async def slowmode_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        await update.message.reply_text("❌ Эта команда только для администраторов!")
        return
    
    seconds = 5
    if context.args:
        try:
            seconds = int(context.args[0])
            if seconds < 0:
                seconds = 0
            if seconds > 300:
                seconds = 300
        except ValueError:
            await update.message.reply_text("❌ Укажите число секунд!")
            return
    
    try:
        await update.effective_chat.set_slow_mode_delay(seconds)
        if seconds > 0:
            await update.message.reply_text(f"🐢 Медленный режим включен: {seconds} сек между сообщениями.")
        else:
            await update.message.reply_text("🐢 Медленный режим отключен.")
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {str(e)}")

# ==================== ПОЛЬЗОВАТЕЛЬСКИЕ КОМАНДЫ ====================
async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.reply_to_message:
        await update.message.reply_text("❌ Ответьте на сообщение, на которое хотите пожаловаться!")
        return
    
    reported_msg = update.message.reply_to_message
    reporter = update.effective_user
    reported_user = reported_msg.from_user
    
    admins = await update.effective_chat.get_administrators()
    
    report_text = (
        f"🚨 ЖАЛОБА в чате {update.effective_chat.title}\n\n"
        f"От: {reporter.full_name} (@{reporter.username})\n"
        f"На: {reported_user.full_name} (@{reported_user.username})\n"
        f"Сообщение: {reported_msg.text or reported_msg.caption or '[медиа]'}\n"
        f"[Перейти к сообщению]({reported_msg.link})"
    )
    
    sent_count = 0
    for admin in admins:
        if not admin.user.is_bot:
            try:
                await context.bot.send_message(
                    admin.user.id,
                    report_text,
                    parse_mode=ParseMode.MARKDOWN
                )
                sent_count += 1
            except:
                pass
    
    await update.message.reply_text(f"✅ Жалоба отправлена {sent_count} администраторам.")

async def info_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.reply_to_message:
        target_user = update.message.reply_to_message.from_user
    else:
        target_user = update.effective_user
    
    stats = db.get_user_stats(update.effective_chat.id, target_user.id)
    warns = db.get_warnings_count(update.effective_chat.id, target_user.id)
    is_muted_user = db.is_muted(update.effective_chat.id, target_user.id)
    
    info_text = (
        f"👤 **Информация о пользователе**\n\n"
        f"**Имя:** {target_user.full_name}\n"
        f"**Username:** @{target_user.username if target_user.username else 'нет'}\n"
        f"**ID:** `{target_user.id}`\n"
        f"**Предупреждения:** {warns}\n"
        f"**Статус мута:** {'🔇 Да' if is_muted_user else '🔊 Нет'}\n"
    )
    
    if stats:
        info_text += (
            f"\n**Статистика:**\n"
            f"**Сообщений:** {stats['messages_count']}\n"
        )
    
    await update.message.reply_text(info_text, parse_mode=ParseMode.MARKDOWN)

async def rules_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings = db.get_chat_settings(update.effective_chat.id)
    keyboard = [[InlineKeyboardButton("✅ Принимаю правила", callback_data="accept_rules")]]
    await update.message.reply_text(
        settings.get('rules', "Правила не установлены."),
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = """
🤖 **Доступные команды:**

**👑 Для админов:**
• /ban - забанить пользователя
• /unban [ID] - разбанить по ID
• /mute [время] - заглушить (1h, 1d, 30m)
• /unmute - снять заглушение
• /warn [причина] - выдать предупреждение
• /unwarn - снять предупреждение
• /clear [N] - удалить N сообщений
• /pin - закрепить сообщение
• /slowmode [сек] - медленный режим

**👥 Для всех:**
• /report - пожаловаться на сообщение
• /info - информация о пользователе
• /rules - правила чата
• /menu - меню с кнопками
• /help - это сообщение
"""
    await update.message.reply_text(help_text, parse_mode=ParseMode.MARKDOWN)

async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("📜 Правила", callback_data="menu_rules")],
        [InlineKeyboardButton("ℹ️ Моя информация", callback_data="menu_info")],
        [InlineKeyboardButton("🆘 Помощь", callback_data="menu_help")],
    ]
    await update.message.reply_text(
        "📋 **Главное меню**\nВыберите действие:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN
    )

# ==================== НАСТРОЙКИ ====================
async def set_welcome_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        await update.message.reply_text("❌ Только для админов!")
        return
    
    if not context.args:
        await update.message.reply_text("❌ Укажите текст приветствия!")
        return
    
    welcome_text = ' '.join(context.args)
    db.update_welcome(update.effective_chat.id, welcome_text)
    await update.message.reply_text("✅ Приветствие обновлено!")

async def set_rules_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        await update.message.reply_text("❌ Только для админов!")
        return
    
    if not context.args:
        await update.message.reply_text("❌ Укажите текст правил!")
        return
    
    rules_text = ' '.join(context.args)
    db.update_rules(update.effective_chat.id, rules_text)
    await update.message.reply_text("✅ Правила обновлены!")

async def add_badword_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        await update.message.reply_text("❌ Только для админов!")
        return
    
    if not context.args:
        await update.message.reply_text("❌ Укажите слово!")
        return
    
    word = context.args[0].lower()
    chat_id = update.effective_chat.id
    
    bad_words = db.get_bad_words(chat_id)
    if word not in bad_words:
        bad_words.append(word)
        db.update_bad_words(chat_id, bad_words)
        await update.message.reply_text(f"✅ Слово '{word}' добавлено в черный список!")
    else:
        await update.message.reply_text(f"⚠️ Слово '{word}' уже в списке!")

async def remove_badword_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        await update.message.reply_text("❌ Только для админов!")
        return
    
    if not context.args:
        await update.message.reply_text("❌ Укажите слово!")
        return
    
    word = context.args[0].lower()
    chat_id = update.effective_chat.id
    
    bad_words = db.get_bad_words(chat_id)
    if word in bad_words:
        bad_words.remove(word)
        db.update_bad_words(chat_id, bad_words)
        await update.message.reply_text(f"✅ Слово '{word}' удалено из черного списка!")
    else:
        await update.message.reply_text(f"⚠️ Слово '{word}' не найдено в списке!")

# ==================== ОБРАБОТЧИКИ СОБЫТИЙ ====================
async def handle_new_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings = db.get_chat_settings(update.effective_chat.id)
    
    for new_member in update.message.new_chat_members:
        if new_member.is_bot:
            continue
        
        welcome_text = settings.get('welcome_message', DEFAULT_WELCOME_MESSAGE)
        welcome_text = welcome_text.format(name=new_member.full_name)
        await update.message.reply_text(welcome_text)

async def handle_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    
    chat = update.effective_chat
    user = update.effective_user
    message = update.message
    
    if db.is_muted(chat.id, user.id):
        try:
            await message.delete()
        except:
        return
    
    db.update_user_stats(chat.id, user.id, user.username, user.first_name)
    settings = db.get_chat_settings(chat.id)
    
    # Антифлуд
    if settings.get('antiflood_enabled', True):
        cache_key = f"{chat.id}_{user.id}"
        
        if cache_key not in flood_cache:
            flood_cache[cache_key] = []
        
        current_time = time.time()
        flood_cache[cache_key].append(current_time)
        
        # Оставляем только сообщения за последние N секунд
        flood_cache[cache_key] = [
            t for t in flood_cache[cache_key] 
            if current_time - t <= settings.get('antiflood_seconds', 10)
        ]
        
        if len(flood_cache[cache_key]) > settings.get('antiflood_count', 5):
            try:
                await message.delete()
                
                mute_until = datetime.now() + timedelta(minutes=5)
                await chat.restrict_member(
                    user.id,
                    permissions=ChatPermissions(can_send_messages=False),
                    until_date=mute_until
                )
                
                db.add_mute(chat.id, user.id, 300)
                
                await context.bot.send_message(
                    chat.id,
                    f"🚫 {user.full_name} заглушен на 5 минут за флуд."
                )
            except:
                pass
            return
    
    # Анти-мат
    bad_words = db.get_bad_words(chat.id)
    if bad_words:
        text_lower = message.text.lower()
        for word in bad_words:
            if word.lower() in text_lower:
                try:
                    await message.delete()
                    warn_count = db.add_warning(chat.id, user.id, context.bot.id, f"Мат: {word}")
                    await context.bot.send_message(
                        chat.id,
                        f"⚠️ {user.full_name}, использование запрещенных слов запрещено!\n"
                        f"Предупреждение {warn_count}/{settings.get('warn_limit', 3)}"
                    )
                    
                    if warn_count >= settings.get('warn_limit', 3):
                        await chat.ban_member(user.id)
                        await context.bot.send_message(
                            chat.id,
                            f"🚫 {user.full_name} забанен за превышение лимита предупреждений."
                        )
                except:
                    pass
                return

# ==================== ОБРАБОТЧИК КНОПОК ====================
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    data = query.data
    user = query.from_user
    chat = query.message.chat
    
    if data == "accept_rules":
        await query.edit_message_text("✅ Спасибо! Правила приняты.")
        if db.is_muted(chat.id, user.id):
            db.remove_mute(chat.id, user.id)
            await chat.restrict_member(
                user.id,
                permissions=ChatPermissions(can_send_messages=True)
            )
    
    elif data == "menu_rules":
        settings = db.get_chat_settings(chat.id)
        keyboard = [[InlineKeyboardButton("✅ Принять", callback_data="accept_rules")]]
        await query.edit_message_text(
            settings.get('rules', "Правила не установлены."),
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    
    elif data == "menu_info":
        warns = db.get_warnings_count(chat.id, user.id)
        text = f"**Ваша информация:**\n\nID: `{user.id}`\nПредупреждений: {warns}"
        await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN)
    
    elif data == "menu_help":
        await query.edit_message_text(
            "Используйте /help для списка команд.\nИли просто пишите в чат!"
        )

# ==================== ЗАПУСК БОТА ====================
def main():
    """Запуск бота"""
    if BOT_TOKEN == "8032712809:AAFcmS1G4xKURy2MZ9izAK8Ne8HXg8EIr8I":
        print("⚠️  ВНИМАНИЕ: Вы используете токен по умолчанию!")
        print("⚠️  Замените его на свой токен в строке BOT_TOKEN")
        print("⚠️  Получите токен у @BotFather в Telegram\n")
    
    application = Application.builder().token(BOT_TOKEN).build()
    
    # Команды модерации
    application.add_handler(CommandHandler("ban", ban_command))
    application.add_handler(CommandHandler("unban", unban_command))
    application.add_handler(CommandHandler("mute", mute_command))
    application.add_handler(CommandHandler("unmute", unmute_command))
    application.add_handler(CommandHandler("warn", warn_command))
    application.add_handler(CommandHandler("unwarn", unwarn_command))
    application.add_handler(CommandHandler("clear", clear_command))
    application.add_handler(CommandHandler("pin", pin_command))
    application.add_handler(CommandHandler("slowmode", slowmode_command))
    
    # Пользовательские команды
    application.add_handler(CommandHandler("report", report_command))
    application.add_handler(CommandHandler("info", info_command))
    application.add_handler(CommandHandler("rules", rules_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("menu", menu_command))
    application.add_handler(CommandHandler("start", menu_command))
    
    # Команды настройки
    application.add_handler(CommandHandler("set_welcome", set_welcome_command))
    application.add_handler(CommandHandler("set_rules", set_rules_command))
    application.add_handler(CommandHandler("add_badword", add_badword_command))
    application.add_handler(CommandHandler("remove_badword", remove_badword_command))
    
    # Обработчики событий
    application.add_handler(MessageHandler(
        filters.StatusUpdate.NEW_CHAT_MEMBERS, 
        handle_new_members
    ))
    application.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND,
        handle_messages
    ))
    
    # Обработчик кнопок
    application.add_handler(CallbackQueryHandler(button_callback))
    
    print("🤖 Бот запущен! Нажмите Ctrl+C для остановки.")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
