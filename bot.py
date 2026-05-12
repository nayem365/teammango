import os
import re
import logging
from datetime import datetime

import psycopg2
from psycopg2.extras import DictCursor
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, filters, ContextTypes
)
from telegram.request import HTTPXRequest

# ========== CONFIG ==========
BOT_TOKEN = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://user:pass@localhost:5432/dbname")
ADMIN_IDS = [int(x.strip()) for x in os.environ.get("ADMIN_IDS", "123456789").split(",")]

# Optional media IDs (set in Heroku env)
WELCOME_PHOTO = os.environ.get("WELCOME_PHOTO", "")
PLAYER_ID_HELP_VIDEO = os.environ.get("PLAYER_ID_HELP_VIDEO", "")
PREPAYMENT_PHOTO = os.environ.get("PREPAYMENT_PHOTO", "")
FINAL_PHOTO = os.environ.get("FINAL_PHOTO", "")

# Conversation states
(NAME, PHONE, COUNTRY, CITY, STREET, FUNDING, PLAYER_ID, REFERRAL, CONFIRM) = range(9)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ========== DATABASE ==========
def get_db_connection():
    return psycopg2.connect(DATABASE_URL, sslmode='require')

def init_db():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS applications (
            user_id BIGINT PRIMARY KEY,
            step INTEGER DEFAULT 1,
            full_name TEXT,
            phone TEXT,
            country TEXT,
            city TEXT,
            street TEXT,
            funding_method TEXT,
            player_id TEXT,
            referral_code TEXT,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    cur.close()
    conn.close()
    logger.info("Database ready")

def save_field(user_id, field, value):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM applications WHERE user_id = %s", (user_id,))
    if cur.fetchone():
        cur.execute(f"UPDATE applications SET {field}=%s, updated_at=CURRENT_TIMESTAMP WHERE user_id=%s", (value, user_id))
    else:
        cur.execute(f"INSERT INTO applications (user_id, {field}) VALUES (%s, %s)", (user_id, value))
    conn.commit()
    cur.close()
    conn.close()

def get_user(user_id):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=DictCursor)
    cur.execute("SELECT * FROM applications WHERE user_id=%s", (user_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return dict(row) if row else None

def update_status(user_id, status):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("UPDATE applications SET status=%s, updated_at=CURRENT_TIMESTAMP WHERE user_id=%s", (status, user_id))
    conn.commit()
    cur.close()
    conn.close()

# ========== HELPER: ALWAYS EDIT OR SEND NEW ==========
async def safe_edit(update: Update, context: ContextTypes.DEFAULT_TYPE, text, **kwargs):
    """Edit the bot's last message if possible, otherwise send a new message."""
    chat_id = update.effective_chat.id
    last_msg_id = context.user_data.get("last_bot_msg_id")

    try:
        if last_msg_id:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=last_msg_id,
                text=text,
                **kwargs
            )
            return
    except Exception as e:
        logger.warning(f"Edit failed: {e}. Sending new message instead.")

    # Send new message and store its ID
    msg = await update.effective_message.reply_text(text, **kwargs)
    context.user_data["last_bot_msg_id"] = msg.message_id

async def safe_edit_photo(update: Update, context: ContextTypes.DEFAULT_TYPE, photo, caption, **kwargs):
    """Send a photo, deleting previous bot message to keep chat clean."""
    chat_id = update.effective_chat.id
    last_msg_id = context.user_data.get("last_bot_msg_id")
    if last_msg_id:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=last_msg_id)
        except:
            pass
    msg = await update.effective_message.reply_photo(photo=photo, caption=caption, **kwargs)
    context.user_data["last_bot_msg_id"] = msg.message_id

# ========== CONVERSATION HANDLERS ==========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_data = get_user(user_id)
    if user_data and user_data.get('status') == 'completed':
        await safe_edit(update, context, "✅ *You are a registered agent!*", parse_mode='Markdown')
        return ConversationHandler.END

    welcome_text = (
        "🌟 *Welcome to 7StarSwin Agent Program* 🌟\n\n"
        "Complete the registration form to become an official agent.\n\n"
        "• Government ID (for name verification)\n"
        "• Active phone number\n"
        "• 7StarSwin Player ID (zero balance, no transactions)"
    )
    if WELCOME_PHOTO:
        await safe_edit_photo(update, context, WELCOME_PHOTO, welcome_text, parse_mode='Markdown')
    else:
        await safe_edit(update, context, welcome_text, parse_mode='Markdown')

    # Now ask for name (replaces welcome)
    await ask_name(update, context)
    return NAME

async def ask_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📝 *STEP 1 OF 3: Personal Information*\n\n"
        "Please enter your *FULL NAME* exactly as on your government ID.\n\n"
        "✅ 2-4 words (First and Last name)\n"
        "✅ Letters only (A-Z)\n"
        "Example: *John Smith*"
    )
    await safe_edit(update, context, text, parse_mode='Markdown')

async def get_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    if not re.match(r'^[A-Za-z\s]{2,100}$', name) or len(name.split()) < 2 or len(name.split()) > 4:
        await safe_edit(update, context, "❌ *Invalid name*\nUse 2-4 words, letters only.\nTry again:", parse_mode='Markdown')
        return NAME
    save_field(update.effective_user.id, 'full_name', name)
    await ask_phone(update, context)
    return PHONE

async def ask_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("📱 SHARE CONTACT", request_contact=True)]])
    await safe_edit(update, context, "📞 *Phone Number*\n\nShare using the button below:", parse_mode='Markdown', reply_markup=keyboard)

async def get_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.contact:
        await safe_edit(update, context, "❌ Please use the *Share Contact* button.", parse_mode='Markdown')
        return PHONE
    save_field(update.effective_user.id, 'phone', update.message.contact.phone_number)
    await ask_country(update, context)
    return COUNTRY

async def ask_country(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🇺🇸 US", callback_data="country_US"), InlineKeyboardButton("🇬🇧 UK", callback_data="country_UK")],
        [InlineKeyboardButton("🇧🇩 BD", callback_data="country_BD"), InlineKeyboardButton("🇮🇳 India", callback_data="country_IN")],
        [InlineKeyboardButton("🇷🇺 Russia", callback_data="country_RU"), InlineKeyboardButton("🇦🇪 UAE", callback_data="country_UAE")],
        [InlineKeyboardButton("🌍 Other", callback_data="country_OTHER")]
    ])
    await safe_edit(update, context, "🌍 *Country of residence*:", parse_mode='Markdown', reply_markup=keyboard)

async def country_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    country_map = {
        "country_US":"United States", "country_UK":"United Kingdom", "country_BD":"Bangladesh",
        "country_RU":"Russia", "country_IN":"India", "country_UAE":"UAE", "country_OTHER":"Other"
    }
    country = country_map.get(query.data, "Other")
    save_field(query.from_user.id, 'country', country)
    # Create a fake update for safe_edit
    class FakeUpdate:
        effective_chat = query.message.chat
        effective_message = query.message
    await ask_city(FakeUpdate(), context)
    return CITY

async def ask_city(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await safe_edit(update, context, "🏙️ *City*\n\nEnter your city name:", parse_mode='Markdown')

async def get_city(update: Update, context: ContextTypes.DEFAULT_TYPE):
    city = update.message.text.strip()
    if len(city) < 2:
        await safe_edit(update, context, "❌ Please enter a valid city name:", parse_mode='Markdown')
        return CITY
    save_field(update.effective_user.id, 'city', city)
    await ask_street(update, context)
    return STREET

async def ask_street(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await safe_edit(update, context, "🏠 *Street name*\n\nEnter street only (e.g., Main Street):", parse_mode='Markdown')

async def get_street(update: Update, context: ContextTypes.DEFAULT_TYPE):
    street = update.message.text.strip()
    if len(street) < 2:
        await safe_edit(update, context, "❌ Please enter a valid street name:", parse_mode='Markdown')
        return STREET
    save_field(update.effective_user.id, 'street', street)
    await ask_funding(update, context)
    return FUNDING

async def ask_funding(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("💵 USDT (TRC20)", callback_data="funding_USDT")],
        [InlineKeyboardButton("₿ Other Cryptocurrency", callback_data="funding_CRYPTO")]
    ])
    await safe_edit(update, context, "💰 *STEP 2 OF 3: Funding Method*\n\nSelect your prepayment method:", parse_mode='Markdown', reply_markup=keyboard)

async def funding_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    funding = "USDT" if query.data == "funding_USDT" else "Other Crypto"
    save_field(query.from_user.id, 'funding_method', funding)
    class FakeUpdate:
        effective_chat = query.message.chat
        effective_message = query.message
    await ask_player_id(FakeUpdate(), context)
    return PLAYER_ID

async def ask_player_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🎮 *STEP 3 OF 3: Player ID*\n\n"
        "Send your *7Starswin Player ID*\n\n"
        "• 9-10 digit number\n"
        "• Zero balance / no transactions\n"
        "• No promo code used\n\n"
        "Send your Player ID now:"
    )
    await safe_edit(update, context, text, parse_mode='Markdown')

async def get_player_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pid = update.message.text.strip()
    if not re.match(r'^\d{9,10}$', pid):
        await safe_edit(update, context, "❌ *Invalid Player ID*\nMust be 9 or 10 digits.\nTry again:", parse_mode='Markdown')
        return PLAYER_ID
    save_field(update.effective_user.id, 'player_id', pid)
    await ask_referral(update, context)
    return REFERRAL

async def ask_referral(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await safe_edit(update, context, "🎁 *Referral Code (Optional)*\n\nEnter code or type `/skip`:", parse_mode='Markdown')

async def get_referral(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ref = update.message.text.strip()
    save_field(update.effective_user.id, 'referral_code', ref)
    await show_summary(update, context)
    return CONFIRM

async def skip_referral(update: Update, context: ContextTypes.DEFAULT_TYPE):
    save_field(update.effective_user.id, 'referral_code', 'None')
    await show_summary(update, context)
    return CONFIRM

async def show_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    data = get_user(user_id)
    summary = f"""
📋 *APPLICATION SUMMARY*

👤 Name: {data.get('full_name')}
📞 Phone: {data.get('phone')}
🌍 Country: {data.get('country')}
🏙️ City: {data.get('city')}
🏠 Street: {data.get('street')}
💰 Funding: {data.get('funding_method')}
🎮 Player ID: {data.get('player_id')}
🎁 Referral: {data.get('referral_code')}

✅ Verify all info then confirm.
"""
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ CONFIRM", callback_data="confirm_app")],
        [InlineKeyboardButton("✏️ EDIT", callback_data="edit_app")]
    ])
    await safe_edit(update, context, summary, parse_mode='Markdown', reply_markup=keyboard)

async def handle_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if query.data == "edit_app":
        await safe_edit(query, context, "✏️ Restart your application with /start", parse_mode='Markdown')
        return ConversationHandler.END

    # Confirm
    update_status(user_id, 'pending_admin_review')
    data = get_user(user_id)

    await safe_edit(query, context, "✅ *APPLICATION SUBMITTED!*\n\nAdmin will review within 24h.\nUse /start to check status.", parse_mode='Markdown')

    # Notify admins
    admin_msg = f"🆕 New agent: {data.get('full_name')} (ID: {user_id})"
    for aid in ADMIN_IDS:
        await context.bot.send_message(aid, admin_msg)
    return ConversationHandler.END

async def check_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    data = get_user(user_id)
    if not data:
        await update.message.reply_text("No application. Send /start")
        return
    status_map = {
        'pending': "⏳ Application in progress.",
        'pending_admin_review': "⏳ Under review (24h).",
        'approved_id': "✅ Player ID approved. Please make prepayment.",
        'prepayment_received': "💰 Prepayment received, admin verifying.",
        'completed': "🎉 You are an official agent!"
    }
    await update.message.reply_text(status_map.get(data.get('status'), "Unknown status"), parse_mode='Markdown')

# ========== MAIN WITH HIGHER TIMEOUTS ==========
def main():
    init_db()

    # Create a custom request with longer timeouts (fixes Heroku timeouts)
    request = HTTPXRequest(
        connect_timeout=30.0,
        read_timeout=30.0,
        write_timeout=30.0,
        pool_timeout=30.0
    )

    app = Application.builder().token(BOT_TOKEN).request(request).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler('start', start)],
        states={
            NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_name)],
            PHONE: [MessageHandler(filters.CONTACT, get_phone)],
            COUNTRY: [CallbackQueryHandler(country_selection, pattern='^country_')],
            CITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_city)],
            STREET: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_street)],
            FUNDING: [CallbackQueryHandler(funding_selection, pattern='^funding_')],
            PLAYER_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_player_id)],
            REFERRAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_referral), CommandHandler('skip', skip_referral)],
            CONFIRM: [CallbackQueryHandler(handle_confirmation, pattern='^(confirm_app|edit_app)$')],
        },
        fallbacks=[CommandHandler('start', start)]
    )
    app.add_handler(conv)
    app.add_handler(CommandHandler('check_status', check_status))

    logger.info("Bot started with extended timeouts – should stay alive on Heroku")

    # Run polling with a custom read timeout for getUpdates
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,   # avoids old updates causing timeouts
        read_timeout=30
    )

if __name__ == '__main__':
    main()
