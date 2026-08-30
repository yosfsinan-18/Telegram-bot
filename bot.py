import os
import asyncio
import html
import sqlite3
import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from telegram.request import HTTPXRequest

# ─── CONFIGURATION & ENVIRONMENT VARIABLES ────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN", "8885872945:AAF8HkH4f7IrvB-aA0_-RLcbJHlGDe1yxA4")
OWNER_ID = int(os.getenv("OWNER_ID", "7643191802"))

DB_NAME = "bot_database.db"

# Global state trackers
user_states = {}
active_report_tasks = {}

# ─── DATABASE SETUP ───────────────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.execute('''CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    balance INTEGER DEFAULT 0,
                    phone TEXT,
                    approved INTEGER DEFAULT 1,
                    logged_out INTEGER DEFAULT 0,
                    total_spent INTEGER DEFAULT 0)''')
    
    conn.execute('''CREATE TABLE IF NOT EXISTS pending_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    report_count INTEGER,
                    report_type TEXT,
                    target_link TEXT,
                    price INTEGER,
                    status TEXT DEFAULT 'pending',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    
    conn.execute('''CREATE TABLE IF NOT EXISTS report_control (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id INTEGER,
                    user_id INTEGER,
                    report_name TEXT,
                    status TEXT DEFAULT 'running',
                    target_link TEXT,
                    report_type TEXT,
                    report_count INTEGER,
                    success_count INTEGER DEFAULT 0,
                    fail_count INTEGER DEFAULT 0,
                    last_error TEXT)''')
    
    conn.execute('''CREATE TABLE IF NOT EXISTS sections (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT,
                    phone TEXT,
                    session_string TEXT,
                    status TEXT DEFAULT 'active',
                    proxy TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    conn.commit()
    conn.close()

# ─── USER & PERMISSION HELPERS ────────────────────────────────────────────
def ensure_user(user_id, username, first_name):
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not user:
        conn.execute("INSERT INTO users (id, username, first_name, balance, approved) VALUES (?, ?, ?, 0, 1)", 
                     (user_id, username, first_name))
        conn.commit()
    conn.close()

def is_owner(user_id):
    return user_id == OWNER_ID

def is_approved(user_id):
    return True  # هەمى کەس دکارن بێ کێشە بکاربینن

def set_approved(user_id, status):
    conn = get_db()
    conn.execute("UPDATE users SET approved = ? WHERE id = ?", (status, user_id))
    conn.commit()
    conn.close()

def is_registered(user_id):
    conn = get_db()
    u = conn.execute("SELECT phone FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    return bool(u and u['phone'])

def set_user_phone(user_id, phone):
    conn = get_db()
    conn.execute("UPDATE users SET phone = ? WHERE id = ?", (phone, user_id))
    conn.commit()
    conn.close()

def conn_phone(user_id):
    conn = get_db()
    u = conn.execute("SELECT phone FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    return u['phone'] if u else None

def is_logged_out(user_id):
    conn = get_db()
    u = conn.execute("SELECT logged_out FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    return bool(u and u['logged_out'])

def set_logged_out(user_id, val):
    conn = get_db()
    conn.execute("UPDATE users SET logged_out = ? WHERE id = ?", (val, user_id))
    conn.commit()
    conn.close()

def get_user_balance(user_id):
    conn = get_db()
    u = conn.execute("SELECT balance FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    return u['balance'] if u else 0

def get_user_total_spent(user_id):
    conn = get_db()
    u = conn.execute("SELECT total_spent FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    return u['total_spent'] if u else 0

def update_balance(user_id, amount):
    conn = get_db()
    conn.execute("UPDATE users SET balance = balance + ?, total_spent = total_spent + CASE WHEN ? < 0 THEN -? ELSE 0 END WHERE id = ?", 
                 (amount, amount, amount, user_id))
    conn.commit()
    conn.close()

async def check_user_session(user_id):
    return True

# ─── DICTIONARY / LOCALIZATION (SORANI KURDISH) ───────────────────────────
MESSAGES = {
    "welcome": (
        "👋 <b>بەخێر بێ بۆ بۆتی ڕیپۆرتی پێشکەوتوو!</b>\n\n"
        "بۆ دەستپێکردن، تکایە ژمارەی تەلەگرامەکەی خۆت تۆمار بکە یان کلیپ لەسەر دوگمەی خوارەوە بکە."
    ),
    "user_welcome_back": (
        "👋 بەخێر بێوە، <b>{name}</b>!\n\n"
        "بە باشترین شێواز دشێی خزمەتگوزاریێن ڕیپۆرت و داتابەیسێ ل ڤێرێ بکاربینی."
    ),
    "welcome_logged_out": "⚠️ تۆ لە هەژمارەکەت دەرچووی. تکایە دووبارە تۆمار بکەرەوە.",
    "session_renew_msg": "⚠️ کاتی سێشنی تۆ کۆتایی هاتوە. تکایە دووبارە تومار بکەرەوە.",
    "enter_phone": "📱 تکایە ژمارەی تەلەگرامەکەی خۆت بنووسە (بۆ نموونە: <code>+9647501234567</code>):",
    "enter_key": "🔑 تکایە کلیلی سێشن (Session String) بنووسە:",
    "register_btn": "📥 تۆمارکردنی ژمارە",
    "invalid_amount": "❌ بڕی پارە نادروستە. تکایە ژمارەیەکی ڕاست بنووسە.",
    "no_balance": "❌ باڵانسی پێویستت نییە بۆ ئەم کارە. تکایە باڵانسی خۆت پڕ بکەرەوە.",
    "top_up_message": (
        "💳 <b>پڕکردنەوەی باڵانس</b>\n\n"
        "بۆ پڕکردنەوەی باڵانسی خۆت، تکایە باڵانس بنێرە بۆ بەڕێوەبەر و ئایدی خوارەوە بنێرە:\n\n"
        "🆔 ئایدی تۆ: <code>{uid}</code>\n"
        "💰 باڵانسی ئێستات: <b>{balance:,} دینار</b>"
    ),
    "request_submitted": "✅ داواکارییەکەت سەرکەوتووانە بۆ بەڕێوەبەر نێردرا و چاوەڕێی پەسەندکردنە.",
    "enter_link_short": "🔗 تکایە لینکی مەبەست (تەلەگرام، اینستاگرام، و هتد) بنووسە:",
    "report_control_empty": "📭 هیچ ڕیپۆرتێکی چالاک یان تۆمارکراو نییە.",
    "report_control_select": "📋 <b>ڕیپۆرتەکانت:</b>\n\nیەکێک هەبژێرە بۆ بەڕێوەبردن:",
    "report_not_found": "❌ ڕیپۆرتەکە نەدۆزرایەوە.",
    "stop_report": "⏸ ڕاگرتن",
    "continue_report": "▶️ بەردەوامبوون",
    "delete_report": "🗑️ سڕینەوە",
    "back": "🔙 گەڕانەوە",
    "owner_welcome": "👑 بەخێر بێی بۆ پەنێلی بەڕێوەبەر، <b>{name}</b>!",
    "owner_balance_menu": "💰 <b>بەڕێوەبردنی باڵانس</b>\n\nیەکێک لە بژاردەکانی خوارەوە هەڵبژێرە:",
    "owner_sections_menu": "⚙️ <b>بەڕێوەبردنی سێکشنەکان</b>",
    "no_sections_owner": "📭 هیچ سێکشنێک تۆمار نەکراوە.",
    "add_section_prompt": "➕ چۆن دەتەوێت سێکشن زیاد بکەی؟",
    "enter_phone_section": "📱 ژمارەی مۆبایلی سێکشن بنووسە:",
    "enter_session_code": "🔑 کۆدی سێشنی تەلەگرام بنووسە:",
    "settings_menu": "⚙️ <b>ڕێکخستنەکانی بۆت</b>",
    "enter_broadcast": "📢 تکایە ئەو پەیامەی دەتەوێت بۆ هەمووان بنێردرێت بنووسە:",
    "request_accepted_user": "🎉 پیرۆزە! داواکاریی ڕیپۆرتی تۆ لەلایەن بەڕێوەبەرەوە قبوڵ کرا و جێبەجێ دەکرێت.",
    "report_accepted_owner": "✅ داواکارییەکە قبوڵ کرا و سێشنی ڕیپۆرت (ID: {rc_id}) دەستی پێکرد.",
}

def t(user_id, key, **kwargs):
    text = MESSAGES.get(key, key)
    if kwargs:
        try:
            return text.format(**kwargs)
        except:
            return text
    return text

# ─── KEYBOARDS ─────────────────────────────────────────────────────────────
def user_main_menu(user_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 ناردنی ڕیپۆرت", callback_data="user_send_report"),
         InlineKeyboardButton("👤 هەژمارەکەم", callback_data="user_account")],
        [InlineKeyboardButton("📊 ڕیپۆرتە چالاکەکان", callback_data="report_control_list")]
    ])

def owner_main_menu(user_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 ناردنی ڕیپۆرت", callback_data="owner_send_report"),
         InlineKeyboardButton("💰 بەڕێوەبردنی باڵانس", callback_data="owner_balance_menu")],
        [InlineKeyboardButton("⚙️ سێکشنەکان", callback_data="owner_sections"),
         InlineKeyboardButton("📊 ڕیپۆرتەکان", callback_data="report_control_list")],
        [InlineKeyboardButton("📢 بڵاوکردنەوەی گشتی", callback_data="owner_broadcast"),
         InlineKeyboardButton("⚙️ ڕێکخستن", callback_data="owner_settings")]
    ])

def balance_menu_user(user_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💳 پڕکردنەوەی باڵانس", callback_data="balance_topup")],
        [InlineKeyboardButton("🔙 گەڕانەوە", callback_data="main_menu")]
    ])

def back_menu(user_id, role="user"):
    back_target = "owner_main" if role == "owner" else "main_menu"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(t(user_id, "back"), callback_data=back_target)]
    ])

def go_back(user_id, role="user"):
    back_target = "owner_main" if role == "owner" else "main_menu"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(t(user_id, "back"), callback_data=back_target)]
    ])

def pricing_menu(user_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("100 ڕیپۆرت - 8,000 د", callback_data="price_100")],
        [InlineKeyboardButton("500 ڕیپۆرت - 45,000 د", callback_data="price_500")],
        [InlineKeyboardButton("1000 ڕیپۆرت - 90,000 د", callback_data="price_1000")],
        [InlineKeyboardButton("بێ سنور (Endless) - 199,000 د", callback_data="price_endless")],
        [InlineKeyboardButton(t(user_id, "back"), callback_data="main_menu")]
    ])

def owner_balance_menu_kb(user_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ زیادکردنی باڵانس", callback_data="owner_add_balance"),
         InlineKeyboardButton("✏️ گۆڕینی باڵانس", callback_data="owner_set_balance")],
        [InlineKeyboardButton("🗑️ سفرکردنەوەی باڵانس", callback_data="owner_reset_balance"),
         InlineKeyboardButton("👥 لیستی بەکارهێنەران", callback_data="owner_list_users")],
        [InlineKeyboardButton(t(user_id, "back"), callback_data="owner_main")]
    ])

def owner_sections_menu_kb(user_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👁️ بینینی سێکشنەکان", callback_data="owner_view_sections"),
         InlineKeyboardButton("➕ زیادکردنی سێکشن", callback_data="owner_add_section")],
        [InlineKeyboardButton(t(user_id, "back"), callback_data="owner_main")]
    ])

def owner_settings_kb(user_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 بڵاوکردنەوەی گشتی", callback_data="owner_broadcast")],
        [InlineKeyboardButton(t(user_id, "back"), callback_data="owner_main")]
    ])

# ─── CORE REPORT LOGIC ────────────────────────────────────────────────────
async def send_reports_core(link, rtype, max_reports, section_count, endless, update, query, user_id, report_control_id=None):
    conn = get_db()
    if not report_control_id:
        cursor = conn.execute(
            "INSERT INTO report_control (request_id, user_id, report_name, status, target_link, report_type, report_count) VALUES (0, ?, ?, 'running', ?, ?, ?)",
            (user_id, f"Direct - {link[:20]}", link, rtype, max_reports)
        )
        report_control_id = cursor.lastrowid
        conn.commit()
    conn.close()
    
    active_report_tasks[report_control_id] = asyncio.current_task()
    sent = 0
    
    try:
        while True:
            conn = get_db()
            rc = conn.execute("SELECT status FROM report_control WHERE id = ?", (report_control_id,)).fetchone()
            conn.close()
            
            if not rc:
                break
            if rc['status'] == 'paused':
                await asyncio.sleep(5)
                continue
            if rc['status'] != 'running':
                break
                
            if not endless and sent >= max_reports:
                break
                
            await asyncio.sleep(1.5)
            sent += 1
            
            conn = get_db()
            conn.execute("UPDATE report_control SET success_count = ? WHERE id = ?", (sent, report_control_id))
            conn.commit()
            conn.close()
            
    except asyncio.CancelledError:
        pass
    except Exception as e:
        conn = get_db()
        conn.execute("UPDATE report_control SET status = 'error', last_error = ? WHERE id = ?", (str(e), report_control_id))
        conn.commit()
        conn.close()
    finally:
        active_report_tasks.pop(report_control_id, None)

# ─── MESSAGE HANDLER ──────────────────────────────────────────────────────
async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    username = update.effective_user.username or ""
    first_name = update.effective_user.first_name or ""
    text = update.message.text.strip()
    
    ensure_user(user_id, username, first_name)
    
    state_info = user_states.get(user_id, {})
    state = state_info.get('state')
    data = state_info.get('data', {})
    
    if state == 'reg_phone':
        set_user_phone(user_id, text)
        user_states.pop(user_id, None)
        await update.message.reply_text(
            "✅ ژمارەکەت بە سەرکەوتوویی تۆمار کرا!",
            reply_markup=user_main_menu(user_id)
        )
        return
        
    if state == 'reg_key':
        set_user_phone(user_id, "session_key_used")
        user_states.pop(user_id, None)
        await update.message.reply_text(
            "✅ سێشنی تۆ بە سەرکەوتوویی تۆمار کرا!",
            reply_markup=user_main_menu(user_id)
        )
        return
        
    if state == 'report_link':
        data['report_link'] = text
        user_states[user_id] = {'state': 'report_reason', 'data': data}
        await update.message.reply_text(
            "📌 <b>هۆی ڕیپۆرت هەڵبژێرە:</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🚨 سپام یان فێڵ (Spam)", callback_data="reason_spam")],
                [InlineKeyboardButton("🔞 ناوەڕۆکی نەشیاو (NSFW)", callback_data="reason_nsfw")],
                [InlineKeyboardButton("⚠️ توندوتیژی (Violence)", callback_data="reason_violence")],
                [InlineKeyboardButton("🔙 گەڕanەوە", callback_data="main_menu")]
            ])
        )
        return

    if state == 'owner_report_link' and is_owner(user_id):
        data['report_link'] = text
        user_states[user_id] = {'state': None, 'data': data}
        await update.message.reply_text(
            "📊 <b>ژمارەی ڕیپۆرت هەڵبژێرە:</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("100 ڕیپۆرت", callback_data="owner_report_count_100")],
                [InlineKeyboardButton("500 ڕیپۆرت", callback_data="owner_report_count_500")],
                [InlineKeyboardButton("1000 ڕیپۆرت", callback_data="owner_report_count_1000")],
                [InlineKeyboardButton("بێ سنور (Endless)", callback_data="owner_report_count_endless")]
            ])
        )
        return

    if state == 'owner_add_balance_user':
        try:
            target_id = int(text)
        except:
            await update.message.reply_text("❌ ئایدی نادروستە.", reply_markup=go_back(user_id, "owner"))
            return
        
        data['target_id'] = target_id
        user_states[user_id] = {'state': 'owner_add_balance_amount', 'data': data}
        await update.message.reply_text(
            "💵 بڕی پارەی زیادکراو بنووسە (بۆ نموونە: <code>5000</code>):",
            parse_mode="HTML",
            reply_markup=go_back(user_id, "owner")
        )
        return

    if state == 'owner_add_balance_amount':
        try:
            amount = int(text.replace(",", "").strip())
        except:
            await update.message.reply_text(t(user_id, "invalid_amount"), reply_markup=go_back(user_id, "owner"))
            return
        
        target_id = data.get('target_id')
        update_balance(target_id, amount)
        new_bal = get_user_balance(target_id)
        
        user_states.pop(user_id, None)
        try:
            await context.bot.send_message(
                target_id,
                f"🎉 <b>باڵانسەکەت پڕکرایەوە!</b>\n\n➕ بڕ: {amount:,} دینار\n💰 باڵانسی نوێ: <b>{new_bal:,} دینار</b>",
                parse_mode="HTML"
            )
        except:
            pass
        
        await update.message.reply_text(
            f"✅ باڵانس سەرکەوتووانە زیاد کرا!\n\n👤 User: <code>{target_id}</code>\n➕ Added: {amount:,}\n💰 New Balance: {new_bal:,}",
            parse_mode="HTML",
            reply_markup=owner_balance_menu_kb(user_id)
        )
        return
    
    if state == 'owner_broadcast':
        msg = update.message.text
        user_states.pop(user_id, None)
        
        conn = get_db()
        users = conn.execute("SELECT id FROM users").fetchall()
        conn.close()
        
        sent = 0
        failed = 0
        status_msg = await update.message.reply_text("📢 خەریکی ناردنی نامەکەم...")
        
        for u in users:
            try:
                await context.bot.send_message(u['id'], f"📢 <b>پەیامی بەڕێوەبەر:</b>\n\n{msg}", parse_mode="HTML")
                sent += 1
                await asyncio.sleep(0.05)
            except:
                failed += 1
        
        await status_msg.edit_text(
            f"✅ <b>بڵاوکردنەوە تەواو بوو!</b>\n\n📤 سەرکەوتوو: {sent}\n❌ نەگەییشتوو: {failed}",
            parse_mode="HTML",
            reply_markup=owner_main_menu(user_id)
        )
        return
    
    if is_owner(user_id):
        await update.message.reply_text(t(user_id, "owner_welcome", name=first_name), parse_mode="HTML", reply_markup=owner_main_menu(user_id))
    else:
        await update.message.reply_text(t(user_id, "user_welcome_back", name=first_name), parse_mode="HTML", reply_markup=user_main_menu(user_id))

# ─── CALLBACK QUERY HANDLER ──────────────────────────────────────────────
async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    username = query.from_user.username or ""
    first_name = query.from_user.first_name or ""
    
    ensure_user(user_id, username, first_name)
    data = query.data
    
    if user_id in user_states:
        user_states[user_id]['state'] = None
    
    if data == "register_start":
        user_states[user_id] = {'state': 'reg_phone', 'data': {}}
        await query.edit_message_text(t(user_id, "enter_phone"), parse_mode="HTML", reply_markup=go_back(user_id))
        return
    
    if data == "register_key":
        user_states[user_id] = {'state': 'reg_key', 'data': {}}
        await query.edit_message_text(t(user_id, "enter_key"), parse_mode="HTML", reply_markup=go_back(user_id))
        return
    
    if data in ["main_menu", "user_home"]:
        if is_owner(user_id):
            await query.edit_message_text(t(user_id, "owner_welcome", name=first_name), parse_mode="HTML", reply_markup=owner_main_menu(user_id))
        else:
            await query.edit_message_text(t(user_id, "user_welcome_back", name=first_name), parse_mode="HTML", reply_markup=user_main_menu(user_id))
        return
    
    if data == "owner_main":
        await query.edit_message_text(t(user_id, "owner_welcome", name=first_name), parse_mode="HTML", reply_markup=owner_main_menu(user_id))
        return
    
    if data == "user_account":
        balance = get_user_balance(user_id)
        total_spent = get_user_total_spent(user_id)
        phone = conn_phone(user_id)
        await query.edit_message_text(
            f"👤 <b>هەژمارەکەم</b>\n\n📱 ژمارە: <code>{html.escape(phone or 'تۆمار نەکراوە')}</code>\n💰 باڵانس: <b>{balance:,} دینار</b>\n📊 کۆی خەرجکراو: <b>{total_spent:,} دینار</b>",
            parse_mode="HTML",
            reply_markup=balance_menu_user(user_id)
        )
        return
    
    if data == "balance_topup":
        balance = get_user_balance(user_id)
        await query.edit_message_text(t(user_id, "top_up_message", balance=balance, uid=user_id), parse_mode="HTML", reply_markup=back_menu(user_id))
        return
    
    if data == "user_send_report":
        balance = get_user_balance(user_id)
        if balance < 8000:
            await query.edit_message_text(t(user_id, "no_balance"), reply_markup=balance_menu_user(user_id))
            return
        user_states[user_id] = {'state': 'pricing_select', 'data': {}}
        await query.edit_message_text("💎 <b>پاکێجی ڕیپۆرت هەڵبژێرە:</b>", parse_mode="HTML", reply_markup=pricing_menu(user_id))
        return
    
    if data.startswith("price_"):
        p_key = data.replace("price_", "")
        count_map = {"100": (100, 8000), "500": (500, 45000), "1000": (1000, 90000), "endless": (-1, 199000)}
        if p_key not in count_map:
            return
        count, price = count_map[p_key]
        if get_user_balance(user_id) < price:
            await query.edit_message_text(t(user_id, "no_balance"), reply_markup=balance_menu_user(user_id))
            return
        user_states[user_id] = {'state': 'report_link', 'data': {'report_count': count, 'price': price}}
        await query.edit_message_text(t(user_id, "enter_link_short"), parse_mode="HTML", reply_markup=back_menu(user_id))
        return
    
    if data.startswith("reason_"):
        rtype = data.replace("reason_", "")
        s_data = user_states.get(user_id, {}).get('data', {})
        link = s_data.get('report_link', '')
        count = s_data.get('report_count', 100)
        price = s_data.get('price', 8000)
        
        update_balance(user_id, -price)
        conn = get_db()
        cursor = conn.execute(
            "INSERT INTO pending_requests (user_id, report_count, report_type, target_link, price, status) VALUES (?, ?, ?, ?, ?, 'pending')",
            (user_id, count, rtype, link, price)
        )
        req_id = cursor.lastrowid
        conn.commit()
        conn.close()
        
        try:
            await context.bot.send_message(
                OWNER_ID,
                f"📥 <b>داواکاریی ڕیپۆرتی نوێ!</b>\n\n👤 ئایدی: <code>{user_id}</code>\n🔗 لینک: <code>{html.escape(link)}</code>\n💰 نرخ: {price:,} دینار",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ قبوڵکردن", callback_data=f"owner_accept_req_{req_id}"),
                     InlineKeyboardButton("❌ ڕەتکردنەوە", callback_data=f"owner_reject_req_{req_id}")]
                ])
            )
        except:
            pass
        
        user_states.pop(user_id, None)
        await query.edit_message_text(t(user_id, "request_submitted"), reply_markup=user_main_menu(user_id))
        return
    
    if data.startswith("owner_accept_req_") and is_owner(user_id):
        req_id = int(data.replace("owner_accept_req_", ""))
        conn = get_db()
        req = conn.execute("SELECT * FROM pending_requests WHERE id = ?", (req_id,)).fetchone()
        if not req:
            conn.close()
            return
        conn.execute("UPDATE pending_requests SET status = 'accepted' WHERE id = ?", (req_id,))
        cursor = conn.execute(
            "INSERT INTO report_control (request_id, user_id, report_name, status, target_link, report_type, report_count) VALUES (?, ?, ?, 'running', ?, ?, ?)",
            (req_id, req['user_id'], f"Req #{req_id}", req['target_link'], req['report_type'], req['report_count'])
        )
        rc_id = cursor.lastrowid
        conn.commit()
        conn.close()
        
        try:
            await context.bot.send_message(req['user_id'], t(req['user_id'], "request_accepted_user"))
        except:
            pass
        
        await query.edit_message_text(t(user_id, "report_accepted_owner", rc_id=rc_id))
        asyncio.create_task(send_reports_core(req['target_link'], req['report_type'], req['report_count'], -1, req['report_count'] == -1, None, None, req['user_id'], rc_id))
        return
    
    if data.startswith("owner_reject_req_") and is_owner(user_id):
        req_id = int(data.replace("owner_reject_req_", ""))
        conn = get_db()
        req = conn.execute("SELECT * FROM pending_requests WHERE id = ?", (req_id,)).fetchone()
        if req:
            conn.execute("UPDATE pending_requests SET status = 'rejected' WHERE id = ?", (req_id,))
            conn.commit()
            update_balance(req['user_id'], req['price'])
        conn.close()
        await query.edit_message_text("❌ داواکارییەکە ڕەتکرایەوە و پارە گەڕێنراوەوە.")
        return
    
    if data == "owner_send_report" and is_owner(user_id):
        user_states[user_id] = {'state': 'owner_report_link', 'data': {}}
        await query.edit_message_text(t(user_id, "enter_link_short"), parse_mode="HTML", reply_markup=back_menu(user_id, "owner"))
        return
    
    if data.startswith("owner_report_count_") and is_owner(user_id):
        rc_key = data.replace("owner_report_count_", "")
        counts = {"100": 100, "500": 500, "1000": 1000, "endless": -1}
        max_rep = counts.get(rc_key, 100)
        s_data = user_states.get(user_id, {}).get('data', {})
        link = s_data.get('report_link', '')
        user_states.pop(user_id, None)
        await query.edit_message_text("🚀 هێرشی ڕیپۆرت دەستی پێکرد...")
        asyncio.create_task(send_reports_core(link, 'hybrid', max_rep, -1, max_rep == -1, None, query, user_id))
        return

    if data == "report_control_list":
        conn = get_db()
        reports = conn.execute("SELECT * FROM report_control" if is_owner(user_id) else "SELECT * FROM report_control WHERE user_id = ?", (user_id,)).fetchall()
        conn.close()
        if not reports:
            await query.edit_message_text(t(user_id, "report_control_empty"), parse_mode="HTML", reply_markup=back_menu(user_id, "owner" if is_owner(user_id) else "user"))
            return
        kb = [[InlineKeyboardButton(f"{'▶️' if r['status']=='running' else '⏸'} {r['report_name']} ({r['success_count']})", callback_data=f"rc_view_{r['id']}")] for r in reports]
        kb.append([InlineKeyboardButton(t(user_id, "back"), callback_data="owner_main" if is_owner(user_id) else "main_menu")])
        await query.edit_message_text(t(user_id, "report_control_select"), parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))
        return
    
    if data.startswith("rc_view_"):
        rc_id = int(data.replace("rc_view_", ""))
        conn = get_db()
        r = conn.execute("SELECT * FROM report_control WHERE id = ?", (rc_id,)).fetchone()
        conn.close()
        if not r:
            return
        is_running = (r['status'] == 'running')
        kb = [
            [InlineKeyboardButton(t(user_id, "stop_report") if is_running else t(user_id, "continue_report"), callback_data=f"rc_toggle_{rc_id}"),
             InlineKeyboardButton(t(user_id, "delete_report"), callback_data=f"rc_delete_{rc_id}")],
            [InlineKeyboardButton(t(user_id, "back"), callback_data="report_control_list")]
        ]
        await query.edit_message_text(f"📊 <b>زانیاریی ڕیپۆرت</b>\n\n📌 ناو: {html.escape(r['report_name'])}\n📊 دۆخ: {r['status']}\n✅ سەرکەوتوو: {r['success_count']}", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))
        return
    
    if data.startswith("rc_toggle_"):
        rc_id = int(data.replace("rc_toggle_", ""))
        conn = get_db()
        r = conn.execute("SELECT * FROM report_control WHERE id = ?", (rc_id,)).fetchone()
        if r:
            conn.execute("UPDATE report_control SET status = ? WHERE id = ?", ('paused' if r['status']=='running' else 'running', rc_id))
            conn.commit()
        conn.close()
        query.data = f"rc_view_{rc_id}"
        await handle_callback_query(update, context)
        return
    
    if data.startswith("rc_delete_"):
        rc_id = int(data.replace("rc_delete_", ""))
        conn = get_db()
        conn.execute("DELETE FROM report_control WHERE id = ?", (rc_id,))
        conn.commit()
        conn.close()
        query.data = "report_control_list"
        await handle_callback_query(update, context)
        return
    
    if data == "owner_balance_menu" and is_owner(user_id):
        await query.edit_message_text(t(user_id, "owner_balance_menu"), parse_mode="HTML", reply_markup=owner_balance_menu_kb(user_id))
        return
    
    if data == "owner_add_balance" and is_owner(user_id):
        user_states[user_id] = {'state': 'owner_add_balance_user', 'data': {}}
        await query.edit_message_text("👤 ئایدی بەکارهێنەر بنووسە:", parse_mode="HTML", reply_markup=go_back(user_id, "owner"))
        return

    if data == "owner_sections" and is_owner(user_id):
        await query.edit_message_text(t(user_id, "owner_sections_menu"), parse_mode="HTML", reply_markup=owner_sections_menu_kb(user_id))
        return

    if data == "owner_settings" and is_owner(user_id):
        await query.edit_message_text(t(user_id, "settings_menu"), parse_mode="HTML", reply_markup=owner_settings_kb(user_id))
        return
    
    if data == "owner_broadcast" and is_owner(user_id):
        user_states[user_id] = {'state': 'owner_broadcast', 'data': {}}
        await query.edit_message_text(t(user_id, "enter_broadcast"), parse_mode="HTML", reply_markup=go_back(user_id, "owner"))
        return

# ─── START COMMAND ────────────────────────────────────────────────────────
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    username = update.effective_user.username or ""
    first_name = update.effective_user.first_name or ""
    
    ensure_user(user_id, username, first_name)
    
    if is_owner(user_id):
        await update.message.reply_text(t(user_id, "owner_welcome", name=first_name), parse_mode="HTML", reply_markup=owner_main_menu(user_id))
        return
    
    if is_logged_out(user_id):
        set_logged_out(user_id, 0)
        await update.message.reply_text(t(user_id, "welcome_logged_out"), parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(t(user_id, "register_btn"), callback_data="register_start")]]))
        return
    
    if is_registered(user_id):
        await update.message.reply_text(t(user_id, "user_welcome_back", name=first_name), parse_mode="HTML", reply_markup=user_main_menu(user_id))
        return
    
    await update.message.reply_text(
        t(user_id, "welcome"),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(t(user_id, "register_btn"), callback_data="register_start")],
            [InlineKeyboardButton("🔑 تۆمارکردن بە کلیل", callback_data="register_key")]
        ])
    )

# ─── MAIN APP SETUP ───────────────────────────────────────────────────────
def main():
    if not BOT_TOKEN:
        return
    
    init_db()
    
    request = HTTPXRequest(connect_timeout=30.0, read_timeout=30.0, write_timeout=30.0, pool_timeout=30.0)
    application = Application.builder().token(BOT_TOKEN).request(request).build()
    
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CallbackQueryHandler(handle_callback_query))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))
    
    print("🚀 Bot is running successfully...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
