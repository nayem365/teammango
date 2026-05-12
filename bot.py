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
ADMIN_AWAITING_NEW_ID = 50

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
            admin_notes TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id SERIAL PRIMARY KEY,
            user_id BIGINT,
            message TEXT,
            is_from_admin BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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

def update_step(user_id, step):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("UPDATE applications SET step=%s, updated_at=CURRENT_TIMESTAMP WHERE user_id=%s", (step, user_id))
    conn.commit()
    cur.close()
    conn.close()

def save_message(user_id, text, is_admin=False):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("INSERT INTO messages (user_id, message, is_from_admin) VALUES (%s,%s,%s)", (user_id, text, is_admin))
    conn.commit()
    cur.close()
    conn.close()

# ========== UTILITIES ==========
async def edit_or_reply(update: Update, context: ContextTypes.DEFAULT_TYPE, text, **kwargs):
    """Edit the last bot message if exists, else send new."""
    if context.user_data.get("last_bot_msg_id"):
        try:
            await context.bot.edit_message_text(
                text=text,
                chat_id=update.effective_chat.id,
                message_id=context.user_data["last_bot_msg_id"],
                **kwargs
            )
            return
        except Exception:
            pass
    msg = await update.effective_message.reply_text(text, **kwargs)
    context.user_data["last_bot_msg_id"] = msg.message_id

async def edit_or_reply_photo(update: Update, context: ContextTypes.DEFAULT_TYPE, photo, caption, **kwargs):
    """Edit with photo (can't edit text→photo, so send new and store ID)."""
    if context.user_data.get("last_bot_msg_id"):
        try:
            await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=context.user_data["last_bot_msg_id"])
        except:
            pass
    msg = await update.effective_message.reply_photo(photo=photo, caption=caption, **kwargs)
    context.user_data["last_bot_msg_id"] = msg.message_id

async def clear_last_message(context, chat_id):
    if context.user_data.get("last_bot_msg_id"):
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=context.user_data["last_bot_msg_id"])
        except:
            pass
        context.user_data["last_bot_msg_id"] = None

# ========== CONVERSATION HANDLERS ==========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_data = get_user(user_id)
    if user_data and user_data.get('status') == 'completed':
        await edit_or_reply(update, context, "✅ *You are a registered 7Starswin Agent!* Contact admin for support.", parse_mode='Markdown')
        return ConversationHandler.END

    # Clean welcome with photo or text
    welcome_text = (
        "🌟 *Welcome to 7StarSwin Agent Program* 🌟\n\n"
        "Complete the registration form to become an official agent.\n\n"
        "• Government ID (for name verification)\n"
        "• Active phone number\n"
        "• 7StarSwin Player ID (zero balance, no transactions)"
    )
    if WELCOME_PHOTO:
        await edit_or_reply_photo(update, context, WELCOME_PHOTO, welcome_text, parse_mode='Markdown')
    else:
        await edit_or_reply(update, context, welcome_text, parse_mode='Markdown')
    
    # Then ask for name (replace welcome)
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
    await edit_or_reply(update, context, text, parse_mode='Markdown')

async def get_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    if not re.match(r'^[A-Za-z\s]{2,100}$', name) or len(name.split()) < 2 or len(name.split()) > 4:
        await edit_or_reply(update, context, "❌ *Invalid name*\nUse 2-4 words, letters only.\nTry again:", parse_mode='Markdown')
        return NAME
    save_field(update.effective_user.id, 'full_name', name)
    await ask_phone(update, context)
    return PHONE

async def ask_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("📱 SHARE CONTACT", request_contact=True)]])
    await edit_or_reply(update, context, "📞 *Phone Number*\n\nShare using the button below:", parse_mode='Markdown', reply_markup=keyboard)

async def get_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.contact:
        await edit_or_reply(update, context, "❌ Please use the *Share Contact* button.", parse_mode='Markdown')
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
    await edit_or_reply(update, context, "🌍 *Country of residence*:", parse_mode='Markdown', reply_markup=keyboard)

async def country_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    country_map = {
        "country_US":"United States", "country_UK":"United Kingdom", "country_BD":"Bangladesh",
        "country_RU":"Russia", "country_IN":"India", "country_UAE":"UAE", "country_OTHER":"Other"
    }
    country = country_map.get(query.data, "Other")
    save_field(query.from_user.id, 'country', country)
    await ask_city(query, context)
    return CITY

async def ask_city(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await edit_or_reply(update, context, "🏙️ *City*\n\nEnter your city name:", parse_mode='Markdown')

async def get_city(update: Update, context: ContextTypes.DEFAULT_TYPE):
    city = update.message.text.strip()
    if len(city) < 2:
        await edit_or_reply(update, context, "❌ Please enter a valid city name:", parse_mode='Markdown')
        return CITY
    save_field(update.effective_user.id, 'city', city)
    await ask_street(update, context)
    return STREET

async def ask_street(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await edit_or_reply(update, context, "🏠 *Street name*\n\nEnter street only (e.g., Main Street):", parse_mode='Markdown')

async def get_street(update: Update, context: ContextTypes.DEFAULT_TYPE):
    street = update.message.text.strip()
    if len(street) < 2:
        await edit_or_reply(update, context, "❌ Please enter a valid street name:", parse_mode='Markdown')
        return STREET
    save_field(update.effective_user.id, 'street', street)
    await ask_funding(update, context)
    return FUNDING

async def ask_funding(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("💵 USDT (TRC20)", callback_data="funding_USDT")],
        [InlineKeyboardButton("₿ Other Cryptocurrency", callback_data="funding_CRYPTO")]
    ])
    await edit_or_reply(update, context, "💰 *STEP 2 OF 3: Funding Method*\n\nSelect your prepayment method:", parse_mode='Markdown', reply_markup=keyboard)

async def funding_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    funding = "USDT" if query.data == "funding_USDT" else "Other Crypto"
    save_field(query.from_user.id, 'funding_method', funding)
    await ask_player_id(query, context)
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
    await edit_or_reply(update, context, text, parse_mode='Markdown')

async def get_player_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pid = update.message.text.strip()
    if not re.match(r'^\d{9,10}$', pid):
        await edit_or_reply(update, context, "❌ *Invalid Player ID*\nMust be 9 or 10 digits.\nTry again:", parse_mode='Markdown')
        return PLAYER_ID
    save_field(update.effective_user.id, 'player_id', pid)
    await ask_referral(update, context)
    return REFERRAL

async def ask_referral(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await edit_or_reply(update, context, "🎁 *Referral Code (Optional)*\n\nEnter code or type `/skip`:", parse_mode='Markdown')

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
    await edit_or_reply(update, context, summary, parse_mode='Markdown', reply_markup=keyboard)

async def handle_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if query.data == "edit_app":
        await edit_or_reply(query, context, "✏️ Restart your application with /start", parse_mode='Markdown')
        return ConversationHandler.END

    # Confirm
    update_status(user_id, 'pending_admin_review')
    update_step(user_id, 2)
    data = get_user(user_id)

    # Notify user
    await edit_or_reply(query, context, 
        "✅ *APPLICATION SUBMITTED!*\n\nAdmin will review within 24h.\nUse /start to check status.",
        parse_mode='Markdown'
    )

    # Notify admins
    admin_msg = (
        f"🆕 *NEW AGENT APPLICATION*\n\n"
        f"👤 {data.get('full_name')}\n"
        f"🆔 `{user_id}`\n"
        f"📞 {data.get('phone')}\n"
        f"🌍 {data.get('country')}\n"
        f"🎮 Player ID: `{data.get('player_id')}`\n"
        f"💰 Funding: {data.get('funding_method')}\n"
        f"📅 {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    )
    admin_keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ APPROVE ID", callback_data=f"approve_id_{user_id}")],
        [InlineKeyboardButton("❌ REJECT ID", callback_data=f"reject_id_{user_id}")],
        [InlineKeyboardButton("📹 SEND VIDEO", callback_data=f"send_video_{user_id}")]
    ])
    for aid in ADMIN_IDS:
        await context.bot.send_message(aid, admin_msg, parse_mode='Markdown', reply_markup=admin_keyboard)
    save_message(user_id, "Application submitted")
    return ConversationHandler.END

# ========== ADMIN HANDLERS (same as before, but using edit pattern where needed) ==========
async def admin_approve_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = int(query.data.split('_')[2])
    update_status(user_id, 'approved_id')
    update_step(user_id, 3)

    payment_text = (
        "✅ *PLAYER ID VERIFIED!*\n\n"
        "📌 *STEP 2: PREPAYMENT $50 USDT*\n"
        "• Network: TRC20\n"
        "• Amount: 50 USDT\n\n"
        "After payment, click:"
    )
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("💰 I HAVE PAID", callback_data="payment_sent")]])
    if PREPAYMENT_PHOTO:
        await context.bot.send_photo(user_id, PREPAYMENT_PHOTO, caption=payment_text, parse_mode='Markdown', reply_markup=keyboard)
    else:
        await context.bot.send_message(user_id, payment_text, parse_mode='Markdown', reply_markup=keyboard)

    await query.edit_message_text(f"✅ Approved Player ID for user {user_id}")
    save_message(user_id, "Admin approved Player ID", is_admin=True)

async def admin_reject_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = int(query.data.split('_')[2])
    msg = "❌ *Player ID rejected.* Create a new account (zero balance, no promo code) and send new ID using /start."
    if PLAYER_ID_HELP_VIDEO:
        await context.bot.send_video(user_id, PLAYER_ID_HELP_VIDEO, caption=msg, parse_mode='Markdown')
    else:
        await context.bot.send_message(user_id, msg, parse_mode='Markdown')
    await query.edit_message_text(f"❌ Rejected Player ID for user {user_id}")
    save_message(user_id, "Admin rejected Player ID", is_admin=True)

async def admin_send_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = int(query.data.split('_')[3])
    if PLAYER_ID_HELP_VIDEO:
        await context.bot.send_video(user_id, PLAYER_ID_HELP_VIDEO, caption="📹 Video guide: How to get valid Player ID")
    else:
        await context.bot.send_message(user_id, "No video configured. Contact support.")
    await query.edit_message_text(f"📹 Video sent to user {user_id}")

async def user_payment_sent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    update_status(user_id, 'prepayment_received')
    await query.edit_message_text("💰 *Payment confirmation received!* Admin will verify soon.", parse_mode='Markdown')
    for aid in ADMIN_IDS:
        await context.bot.send_message(aid, f"💰 Prepayment from user {user_id} (Name: {get_user(user_id).get('full_name')}) - verify & /create_account")
    save_message(user_id, "User confirmed prepayment", is_admin=False)

async def admin_create_account(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    parts = update.message.text.split()
    if len(parts) != 2:
        await update.message.reply_text("Usage: /create_account <user_id>")
        return
    user_id = int(parts[1])
    update_status(user_id, 'completed')
    congrats = "🎉 *CONGRATULATIONS!* You are now an official 7Starswin Agent!\nLogin credentials within 24h."
    if FINAL_PHOTO:
        await context.bot.send_photo(user_id, FINAL_PHOTO, caption=congrats, parse_mode='Markdown')
    else:
        await context.bot.send_message(user_id, congrats, parse_mode='Markdown')
    await update.message.reply_text(f"✅ Agent account created for user {user_id}")
    save_message(user_id, "Agent account created", is_admin=True)

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

# ========== MAIN ==========
def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

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
    app.add_handler(CallbackQueryHandler(admin_approve_id, pattern='^approve_id_'))
    app.add_handler(CallbackQueryHandler(admin_reject_id, pattern='^reject_id_'))
    app.add_handler(CallbackQueryHandler(admin_send_video, pattern='^send_video_'))
    app.add_handler(CallbackQueryHandler(user_payment_sent, pattern='^payment_sent$'))
    app.add_handler(CommandHandler('check_status', check_status))
    app.add_handler(CommandHandler('create_account', admin_create_account))

    logger.info("Bot started with clean stepwise UI")
    app.run_polling()

if __name__ == '__main__':
    main()
