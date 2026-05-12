import os
import re
import json
import logging
from datetime import datetime
from threading import Thread

import psycopg2
from psycopg2.extras import DictCursor
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, filters, ContextTypes
)

# ============== CONFIGURATION ==============
BOT_TOKEN = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://user:pass@localhost:5432/dbname")

# Admin IDs - Add your Telegram user IDs here (comma separated in environment variable)
ADMIN_IDS = [int(x.strip()) for x in os.environ.get("ADMIN_IDS", "123456789").split(",")]

# Video file ID (upload video to your bot first, then copy file_id)
PLAYER_ID_HELP_VIDEO = os.environ.get("PLAYER_ID_HELP_VIDEO", "")

# Photo file IDs (upload photos to your bot first)
WELCOME_PHOTO = os.environ.get("WELCOME_PHOTO", "")
PLAYER_ID_EXAMPLE_PHOTO = os.environ.get("PLAYER_ID_EXAMPLE_PHOTO", "")
PREPAYMENT_PHOTO = os.environ.get("PREPAYMENT_PHOTO", "")
FINAL_PHOTO = os.environ.get("FINAL_PHOTO", "")

# Button text constants
BTN_CONFIRM = "✅ CONFIRM APPLICATION"
BTN_EDIT = "✏️ EDIT INFORMATION"
BTN_PAYMENT_SENT = "💰 I HAVE MADE PREPAYMENT"
BTN_RESEND_ID = "🔄 SEND NEW PLAYER ID"
BTN_CHECK_STATUS = "📊 CHECK STATUS"

# Conversation states
(NAME, PHONE, COUNTRY, CITY, STREET, FUNDING, PLAYER_ID, REFERRAL, CONFIRM) = range(9)
ADMIN_AWAITING_NEW_ID = 50

# Setup logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# ============== DATABASE FUNCTIONS ==============

def get_db_connection():
    return psycopg2.connect(DATABASE_URL, sslmode='require')

def init_database():
    """Create tables if they don't exist"""
    conn = get_db_connection()
    cur = conn.cursor()
    
    # Applications table
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
    
    # Messages table for admin-user communication
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
    logger.info("Database initialized successfully")

def save_user_data(user_id, field, value):
    """Save or update a single field for user"""
    conn = get_db_connection()
    cur = conn.cursor()
    
    # Check if user exists
    cur.execute("SELECT user_id FROM applications WHERE user_id = %s", (user_id,))
    exists = cur.fetchone()
    
    if exists:
        cur.execute(f"UPDATE applications SET {field} = %s, updated_at = CURRENT_TIMESTAMP WHERE user_id = %s", (value, user_id))
    else:
        cur.execute(f"INSERT INTO applications (user_id, {field}) VALUES (%s, %s)", (user_id, value))
    
    conn.commit()
    cur.close()
    conn.close()

def get_user_data(user_id):
    """Get all data for a user"""
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=DictCursor)
    cur.execute("SELECT * FROM applications WHERE user_id = %s", (user_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return dict(row) if row else None

def update_step(user_id, step):
    """Update user's current step"""
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("UPDATE applications SET step = %s, updated_at = CURRENT_TIMESTAMP WHERE user_id = %s", (step, user_id))
    conn.commit()
    cur.close()
    conn.close()

def update_status(user_id, status):
    """Update user's application status"""
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("UPDATE applications SET status = %s, updated_at = CURRENT_TIMESTAMP WHERE user_id = %s", (status, user_id))
    conn.commit()
    cur.close()
    conn.close()

def get_all_pending_applications():
    """Get all applications waiting for admin review"""
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=DictCursor)
    cur.execute("SELECT * FROM applications WHERE status = 'pending_admin_review' ORDER BY created_at ASC")
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [dict(row) for row in rows]

def get_all_incomplete_applications():
    """Get users who started but didn't complete step 1"""
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=DictCursor)
    cur.execute("SELECT user_id, full_name FROM applications WHERE step = 1 AND status = 'pending'")
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [dict(row) for row in rows]

def save_message(user_id, message, is_from_admin=False):
    """Save a message for record keeping"""
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("INSERT INTO messages (user_id, message, is_from_admin) VALUES (%s, %s, %s)", (user_id, message, is_from_admin))
    conn.commit()
    cur.close()
    conn.close()

# ============== USER HANDLERS ==============

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    # Check if already completed agent
    user_data = get_user_data(user_id)
    if user_data and user_data.get('status') == 'completed':
        await update.message.reply_text(
            "✅ *You are already a registered 7Starswin Agent!*\n\n"
            "Contact admin for support or dashboard access.",
            parse_mode='Markdown'
        )
        return ConversationHandler.END
    
    # Send welcome message with photo if available
    if WELCOME_PHOTO:
        await update.message.reply_photo(
            photo=WELCOME_PHOTO,
            caption="🌟 *WELCOME TO 7STARSWIN AGENT PROGRAM* 🌟\n\n"
                    "Complete the registration form to become an official agent.\n\n"
                    "📌 *You will need:*\n"
                    "• Government ID (for name verification)\n"
                    "• Active phone number\n"
                    "• 7Starswin Player ID (zero balance, no transactions)",
            parse_mode='Markdown'
        )
    else:
        await update.message.reply_text(
            "🌟 *WELCOME TO 7STARSWIN AGENT PROGRAM* 🌟\n\n"
            "Complete the registration form to become an official agent.\n\n"
            "📌 *You will need:*\n"
            "• Government ID (for name verification)\n"
            "• Active phone number\n"
            "• 7Starswin Player ID (zero balance, no transactions)",
            parse_mode='Markdown'
        )
    
    await update.message.reply_text(
        "📝 *STEP 1 OF 3: Personal Information*\n\n"
        "Please enter your *FULL NAME* exactly as shown on your government ID.\n\n"
        "✅ 2-4 words (First and Last name)\n"
        "✅ Letters only (A-Z)\n"
        "✅ Example: John Smith",
        parse_mode='Markdown'
    )
    return NAME

async def get_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    
    # Validation: letters and spaces only, 2-4 words
    if not re.match(r'^[A-Za-z\s]{2,100}$', name) or len(name.split()) < 2 or len(name.split()) > 4:
        await update.message.reply_text(
            "❌ *Invalid name format*\n\n"
            "Please use:\n"
            "• Only letters A-Z\n"
            "• First and Last name (2-4 words)\n"
            "• No numbers or special characters\n\n"
            "Try again:",
            parse_mode='Markdown'
        )
        return NAME
    
    save_user_data(update.effective_user.id, 'full_name', name)
    
    await update.message.reply_text(
        "📞 *Phone Number*\n\n"
        "Please share your phone number using the button below.\n\n"
        "⚠️ This number will be used for agent communications.",
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📱 SHARE CONTACT", request_contact=True)]
        ])
    )
    return PHONE

async def get_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.contact:
        await update.message.reply_text(
            "❌ Please use the *Share Contact* button.",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📱 SHARE CONTACT", request_contact=True)]
            ])
        )
        return PHONE
    
    phone = update.message.contact.phone_number
    save_user_data(update.effective_user.id, 'phone', phone)
    
    await update.message.reply_text(
        "🌍 *Country of Residence*\n\nSelect your country:",
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🇺🇸 United States", callback_data="country_US")],
            [InlineKeyboardButton("🇬🇧 United Kingdom", callback_data="country_UK")],
            [InlineKeyboardButton("🇧🇩 Bangladesh", callback_data="country_BD")],
            [InlineKeyboardButton("🇷🇺 Russia", callback_data="country_RU")],
            [InlineKeyboardButton("🇮🇳 India", callback_data="country_IN")],
            [InlineKeyboardButton("🇦🇪 UAE", callback_data="country_UAE")],
            [InlineKeyboardButton("🇨🇦 Canada", callback_data="country_CA")],
            [InlineKeyboardButton("🇦🇺 Australia", callback_data="country_AU")],
            [InlineKeyboardButton("🌍 Other", callback_data="country_OTHER")]
        ])
    )
    return COUNTRY

async def country_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    country_map = {
        "country_US": "United States", "country_UK": "United Kingdom",
        "country_BD": "Bangladesh", "country_RU": "Russia",
        "country_IN": "India", "country_UAE": "UAE",
        "country_CA": "Canada", "country_AU": "Australia",
        "country_OTHER": "Other"
    }
    country = country_map.get(query.data, "Other")
    save_user_data(query.from_user.id, 'country', country)
    
    await query.edit_message_text(
        f"✅ Country: *{country}*\n\n🏙️ *City*\n\nEnter your city name:",
        parse_mode='Markdown'
    )
    return CITY

async def get_city(update: Update, context: ContextTypes.DEFAULT_TYPE):
    city = update.message.text.strip()
    if len(city) < 2:
        await update.message.reply_text("❌ Please enter a valid city name:")
        return CITY
    
    save_user_data(update.effective_user.id, 'city', city)
    
    await update.message.reply_text(
        "🏠 *Street Name*\n\n"
        "Enter your street name only (not full address).\n\n"
        "✅ Example: Main Street, Park Avenue\n"
        "⚠️ This will be visible to players for cashier withdrawal",
        parse_mode='Markdown'
    )
    return STREET

async def get_street(update: Update, context: ContextTypes.DEFAULT_TYPE):
    street = update.message.text.strip()
    if len(street) < 2:
        await update.message.reply_text("❌ Please enter a valid street name:")
        return STREET
    
    save_user_data(update.effective_user.id, 'street', street)
    
    await update.message.reply_text(
        "💰 *STEP 2 OF 3: Funding Method*\n\n"
        "How would you like to top up your account for agent registration?\n\n"
        "Select one option:",
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("💵 USDT (TRC20 Network)", callback_data="funding_USDT")],
            [InlineKeyboardButton("₿ Other Cryptocurrency", callback_data="funding_CRYPTO")]
        ])
    )
    return FUNDING

async def funding_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    funding_map = {"funding_USDT": "USDT", "funding_CRYPTO": "Other Crypto"}
    funding = funding_map.get(query.data, "USDT")
    save_user_data(query.from_user.id, 'funding_method', funding)
    
    # Send Player ID instruction with photo
    if PLAYER_ID_EXAMPLE_PHOTO:
        await query.message.reply_photo(
            photo=PLAYER_ID_EXAMPLE_PHOTO,
            caption="🎮 *STEP 3 OF 3: Player ID*\n\n"
                    "Send your *7Starswin Player ID*\n\n"
                    "📌 *REQUIREMENTS:*\n"
                    "• 9-10 digit number\n"
                    "• Zero balance\n"
                    "• No transactions\n"
                    "• No promo code used\n\n"
                    "📍 *Where to find it:* Open 7Starswin app → Profile → Your ID number\n\n"
                    "Send your Player ID now:",
            parse_mode='Markdown'
        )
    else:
        await query.message.edit_text(
            "🎮 *STEP 3 OF 3: Player ID*\n\n"
            "Send your *7Starswin Player ID*\n\n"
            "📌 *REQUIREMENTS:*\n"
            "• 9-10 digit number\n"
            "• Zero balance\n"
            "• No transactions\n"
            "• No promo code used\n\n"
            "📍 Where to find it: Open 7Starswin app → Profile → Your ID number\n\n"
            "Send your Player ID now:",
            parse_mode='Markdown'
        )
    return PLAYER_ID

async def get_player_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    player_id = update.message.text.strip()
    
    # Validate 9-10 digits
    if not re.match(r'^\d{9,10}$', player_id):
        await update.message.reply_text(
            "❌ *Invalid Player ID*\n\n"
            "Must be 9 or 10 digits only.\n"
            "Example: 1234567890\n\n"
            "Try again:",
            parse_mode='Markdown'
        )
        return PLAYER_ID
    
    save_user_data(update.effective_user.id, 'player_id', player_id)
    
    await update.message.reply_text(
        "🎁 *Referral Code (Optional)*\n\n"
        "If you have a referral code, enter it now.\n\n"
        "Type `/skip` if you don't have one.",
        parse_mode='Markdown'
    )
    return REFERRAL

async def get_referral(update: Update, context: ContextTypes.DEFAULT_TYPE):
    referral = update.message.text.strip()
    save_user_data(update.effective_user.id, 'referral_code', referral)
    await show_summary(update, context)
    return CONFIRM

async def skip_referral(update: Update, context: ContextTypes.DEFAULT_TYPE):
    save_user_data(update.effective_user.id, 'referral_code', 'None')
    await show_summary(update, context)
    return CONFIRM

async def show_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    data = get_user_data(user_id)
    
    summary = f"""
📋 *APPLICATION SUMMARY - PLEASE REVIEW*

━━━━━━━━━━━━━━━━━━━━━━━━━
👤 *Full Name:* {data.get('full_name', 'Not provided')}
📞 *Phone:* {data.get('phone', 'Not provided')}
🌍 *Country:* {data.get('country', 'Not provided')}
🏙️ *City:* {data.get('city', 'Not provided')}
🏠 *Street:* {data.get('street', 'Not provided')}
💰 *Funding Method:* {data.get('funding_method', 'Not provided')}
🎮 *Player ID:* {data.get('player_id', 'Not provided')}
🎁 *Referral Code:* {data.get('referral_code', 'None')}
━━━━━━━━━━━━━━━━━━━━━━━━━

⚠️ *Please verify all information is correct before submitting.*
"""
    
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(BTN_CONFIRM, callback_data="confirm_app")],
        [InlineKeyboardButton(BTN_EDIT, callback_data="edit_app")]
    ])
    
    # Try to edit message if it exists, otherwise send new
    try:
        await update.message.reply_text(summary, parse_mode='Markdown', reply_markup=keyboard)
    except:
        await update.callback_query.edit_message_text(summary, parse_mode='Markdown', reply_markup=keyboard)

async def handle_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    
    if query.data == "edit_app":
        await query.edit_message_text(
            "✏️ *Restarting Application*\n\n"
            "Send /start to begin again.",
            parse_mode='Markdown'
        )
        return ConversationHandler.END
    
    elif query.data == "confirm_app":
        # Save final application
        update_step(user_id, 2)
        update_status(user_id, 'pending_admin_review')
        
        data = get_user_data(user_id)
        
        # Send confirmation to user
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(BTN_CHECK_STATUS, callback_data="check_status")]
        ])
        
        await query.edit_message_text(
            "✅ *APPLICATION SUBMITTED SUCCESSFULLY!*\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "📌 Your application is now under review.\n"
            "⏳ Please wait up to 24 hours.\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "You will be notified once an admin reviews your Player ID.\n\n"
            "Use the button below to check your status anytime.",
            parse_mode='Markdown',
            reply_markup=keyboard
        )
        
        # Build summary for admin
        admin_summary = f"""
🆕 *NEW AGENT APPLICATION*

━━━━━━━━━━━━━━━━━━━━━━━━━
👤 *Name:* {data.get('full_name', 'N/A')}
📞 *Phone:* {data.get('phone', 'N/A')}
🆔 *Telegram ID:* `{user_id}`
🌍 *Country:* {data.get('country', 'N/A')}
🏙️ *City:* {data.get('city', 'N/A')}
🏠 *Street:* {data.get('street', 'N/A')}
💰 *Funding:* {data.get('funding_method', 'N/A')}
🎮 *Player ID:* `{data.get('player_id', 'N/A')}`
🎁 *Referral:* {data.get('referral_code', 'None')}
📅 *Submitted:* {datetime.now().strftime('%Y-%m-%d %H:%M')}
━━━━━━━━━━━━━━━━━━━━━━━━━

⚠️ *Action Required: Verify Player ID*
"""
        
        admin_keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ APPROVE PLAYER ID", callback_data=f"approve_id_{user_id}")],
            [InlineKeyboardButton("❌ REJECT PLAYER ID", callback_data=f"reject_id_{user_id}")],
            [InlineKeyboardButton("📹 SEND VIDEO GUIDE", callback_data=f"send_video_{user_id}")]
        ])
        
        # Send to all admins
        for admin_id in ADMIN_IDS:
            try:
                await context.bot.send_message(
                    admin_id,
                    admin_summary,
                    parse_mode='Markdown',
                    reply_markup=admin_keyboard
                )
            except Exception as e:
                logger.error(f"Failed to send to admin {admin_id}: {e}")
        
        save_message(user_id, "Application submitted", is_from_admin=False)
        return ConversationHandler.END

async def check_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    data = get_user_data(user_id)
    
    if not data:
        await query.edit_message_text("No application found. Send /start to begin.")
        return
    
    status = data.get('status', 'unknown')
    
    status_messages = {
        'pending': "⏳ *Application in progress*\n\nPlease complete all steps.",
        'pending_admin_review': "⏳ *Under Review*\n\nYour Player ID is being verified. Please wait up to 24 hours.",
        'approved_id': "✅ *Player ID Approved!*\n\nProceed to make your prepayment. Check your messages for instructions.",
        'prepayment_received': "💰 *Prepayment Received*\n\nAdmin is verifying your payment. You'll be notified soon.",
        'account_created': "🎉 *Account Created!*\n\nYou will receive your agent login details within 24 hours.",
        'completed': "✅ *OFFICIAL AGENT*\n\nYou are a registered 7Starswin Agent! Contact admin for dashboard access."
    }
    
    msg = status_messages.get(status, "🔄 Application status unknown. Contact admin.")
    
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Refresh Status", callback_data="check_status")]
    ])
    
    await query.edit_message_text(msg, parse_mode='Markdown', reply_markup=keyboard)

# ============== ADMIN HANDLERS ==============

async def admin_approve_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user_id = int(query.data.split('_')[2])
    
    update_status(user_id, 'approved_id')
    update_step(user_id, 3)
    
    # Send prepayment instruction with photo
    if PREPAYMENT_PHOTO:
        await context.bot.send_photo(
            user_id,
            photo=PREPAYMENT_PHOTO,
            caption="✅ *PLAYER ID VERIFIED!*\n\n"
                    "━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    "📌 *STEP 2 OF 3: PREPAYMENT REQUIRED*\n"
                    "━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                    "To activate your agent account, please make a prepayment of **$50 USDT** to your 7Starswin player account.\n\n"
                    "💰 *Payment Details:*\n"
                    "• Network: TRC20\n"
                    "• Amount: $50 USDT\n\n"
                    "⚠️ After making the payment, click the button below:",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(BTN_PAYMENT_SENT, callback_data="payment_sent")]
            ])
        )
    else:
        await context.bot.send_message(
            user_id,
            "✅ *PLAYER ID VERIFIED!*\n\n"
            "📌 *STEP 2 OF 3: PREPAYMENT REQUIRED*\n\n"
            "To activate your agent account, please make a prepayment of **$50 USDT** to your 7Starswin player account.\n\n"
            "After making the payment, click the button below:",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(BTN_PAYMENT_SENT, callback_data="payment_sent")]
            ])
        )
    
    await query.edit_message_reply_markup(reply_markup=None)
    await query.edit_message_text(f"✅ Approved Player ID for user {user_id}. Prepayment stage initiated.")
    save_message(user_id, "Admin approved Player ID", is_from_admin=True)

async def admin_reject_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user_id = int(query.data.split('_')[2])
    
    instruction_text = (
        "❌ *PLAYER ID REJECTED*\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "📌 *REQUIREMENTS FOR NEW ACCOUNT:*\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "1️⃣ Register a NEW account using this link\n"
        "2️⃣ DO NOT use any promo code\n"
        "3️⃣ Pick your preferred currency\n"
        "4️⃣ Keep ZERO balance - NO transactions\n\n"
        "After creating the new account, send your new Player ID here:"
    )
    
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(BTN_RESEND_ID, callback_data="resend_id")]
    ])
    
    if PLAYER_ID_HELP_VIDEO:
        await context.bot.send_video(
            user_id,
            video=PLAYER_ID_HELP_VIDEO,
            caption=instruction_text,
            parse_mode='Markdown',
            reply_markup=keyboard
        )
    else:
        await context.bot.send_message(
            user_id,
            instruction_text,
            parse_mode='Markdown',
            reply_markup=keyboard
        )
    
    await query.edit_message_text(f"❌ Rejected Player ID for user {user_id}. Video guide sent.")
    save_message(user_id, "Admin rejected Player ID - video guide sent", is_from_admin=True)

async def admin_send_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user_id = int(query.data.split('_')[3])
    
    if PLAYER_ID_HELP_VIDEO:
        await context.bot.send_video(
            user_id,
            video=PLAYER_ID_HELP_VIDEO,
            caption="📹 *How to Register & Verify Your Player ID*\n\nFollow this guide carefully to create a valid account.",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(BTN_RESEND_ID, callback_data="resend_id")]
            ])
        )
    else:
        await context.bot.send_message(
            user_id,
            "📹 *Video Guide*\n\nNo video configured. Please contact support.",
            parse_mode='Markdown'
        )
    
    await query.edit_message_text(f"📹 Video guide sent to user {user_id}.")

async def user_resend_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    await query.edit_message_text(
        "🆔 *Send Your NEW Player ID*\n\n"
        "Please send your new 7Starswin Player ID (9-10 digits only):",
        parse_mode='Markdown'
    )
    return ADMIN_AWAITING_NEW_ID

async def receive_new_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    new_player_id = update.message.text.strip()
    
    if not re.match(r'^\d{9,10}$', new_player_id):
        await update.message.reply_text("❌ Invalid format. Send 9-10 digits only:")
        return ADMIN_AWAITING_NEW_ID
    
    save_user_data(user_id, 'player_id', new_player_id)
    update_status(user_id, 'pending_admin_review')
    
    data = get_user_data(user_id)
    
    admin_summary = f"""
🔄 *RESUBMITTED PLAYER ID*

👤 *User:* {user_id}
👤 *Name:* {data.get('full_name', 'N/A')}
🎮 *New Player ID:* `{new_player_id}`

Please verify this new ID.
"""
    
    for admin_id in ADMIN_IDS:
        await context.bot.send_message(
            admin_id,
            admin_summary,
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ APPROVE NEW ID", callback_data=f"approve_id_{user_id}")],
                [InlineKeyboardButton("❌ REJECT", callback_data=f"reject_id_{user_id}")]
            ])
        )
    
    await update.message.reply_text(
        "✅ *New Player ID Submitted*\n\n"
        "Admin will review your new ID within 24 hours.\n\n"
        "Use /start to check status.",
        parse_mode='Markdown'
    )
    return ConversationHandler.END

async def user_payment_sent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    
    update_status(user_id, 'prepayment_received')
    
    await query.edit_message_text(
        "💰 *PREPAYMENT CONFIRMATION RECEIVED!*\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "✅ Thank you for your payment.\n"
        "⏳ Admin is verifying your transaction.\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "You will be notified once verification is complete.\n\n"
        "Use /start to check your status.",
        parse_mode='Markdown'
    )
    
    data = get_user_data(user_id)
    
    for admin_id in ADMIN_IDS:
        await context.bot.send_message(
            admin_id,
            f"💰 *PREPAYMENT RECEIVED*\n\n"
            f"👤 *User:* {user_id}\n"
            f"👤 *Name:* {data.get('full_name', 'N/A')}\n"
            f"💵 *Amount:* $50 USDT\n\n"
            f"Please verify the payment.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ VERIFY & CREATE AGENT ACCOUNT", callback_data=f"create_account_{user_id}")]
            ])
        )
    
    save_message(user_id, "User confirmed prepayment sent", is_from_admin=False)

async def admin_create_account(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user_id = int(query.data.split('_')[2])
    
    update_status(user_id, 'completed')
    update_step(user_id, 5)
    
    if FINAL_PHOTO:
        await context.bot.send_photo(
            user_id,
            photo=FINAL_PHOTO,
            caption="🎉 *CONGRATULATIONS!* 🎉\n\n"
                    "━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    "You are now an official **7STARSWIN AGENT**!\n"
                    "━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                    "✅ Agent account created successfully\n"
                    "✅ Login credentials will be sent within 24 hours\n"
                    "✅ Commission rate: Up to 40%\n"
                    "✅ Start onboarding players today\n\n"
                    "Contact admin for your agent dashboard access.\n\n"
                    "*Welcome to the team!* 🚀",
            parse_mode='Markdown'
        )
    else:
        await context.bot.send_message(
            user_id,
            "🎉 *CONGRATULATIONS!* 🎉\n\n"
            "You are now an official **7STARSWIN AGENT**!\n\n"
            "✅ Agent account created successfully\n"
            "✅ Login credentials will be sent within 24 hours\n"
            "✅ Commission rate: Up to 40%\n\n"
            "*Welcome to the team!* 🚀",
            parse_mode='Markdown'
        )
    
    await query.edit_message_text(f"✅ Agent account created for user {user_id}. Status: OFFICIAL AGENT")
    save_message(user_id, "Admin created agent account - OFFICIAL AGENT", is_from_admin=True)

# ============== ADMIN COMMANDS ==============

async def admin_list_pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to list all pending applications"""
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ Unauthorized.")
        return
    
    pending = get_all_pending_applications()
    
    if not pending:
        await update.message.reply_text("📭 No pending applications.")
        return
    
    for app in pending[:10]:  # Show first 10
        await update.message.reply_text(
            f"🆔 User: `{app['user_id']}`\n"
            f"👤 Name: {app.get('full_name', 'N/A')}\n"
            f"🎮 Player ID: `{app.get('player_id', 'N/A')}`\n"
            f"📅 Submitted: {app['created_at']}",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Approve", callback_data=f"approve_id_{app['user_id']}"),
                 InlineKeyboardButton("❌ Reject", callback_data=f"reject_id_{app['user_id']}")]
            ])
        )
    
    await update.message.reply_text(f"📊 Total pending: {len(pending)}")

async def admin_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to broadcast message to all users"""
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ Unauthorized.")
        return
    
    message = " ".join(context.args)
    if not message:
        await update.message.reply_text("Usage: /broadcast Your message here")
        return
    
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT user_id FROM applications")
    users = cur.fetchall()
    cur.close()
    conn.close()
    
    success = 0
    fail = 0
    
    for (user_id,) in users:
        try:
            await context.bot.send_message(user_id, f"📢 *ANNOUNCEMENT*\n\n{message}", parse_mode='Markdown')
            success += 1
        except:
            fail += 1
    
    await update.message.reply_text(f"✅ Broadcast sent to {success} users. Failed: {fail}")

# ============== DAILY REMINDER ==============

async def send_daily_reminders(app: Application):
    """Send reminders to users who haven't completed application"""
    incomplete = get_all_incomplete_applications()
    
    for user in incomplete:
        try:
            await app.bot.send_message(
                user['user_id'],
                "⏰ *REMINDER*\n\n"
                "You haven't completed your 7Starswin agent application.\n\n"
                "Send /start to continue where you left off.",
                parse_mode='Markdown'
            )
            logger.info(f"Reminder sent to {user['user_id']}")
        except Exception as e:
            logger.error(f"Failed to send reminder to {user['user_id']}: {e}")

def start_scheduler(app: Application):
    """Start the daily reminder scheduler"""
    import asyncio
    
    async def reminder_job():
        while True:
            await asyncio.sleep(86400)  # 24 hours
            await send_daily_reminders(app)
    
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.create_task(reminder_job())
    loop.run_forever()

# ============== MAIN ==============

def main():
    # Initialize database
    init_database()
    
    # Create application
    app = Application.builder().token(BOT_TOKEN).build()
    
    # User conversation handler
    conv_handler = ConversationHandler(
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
    
    # Admin commands
    app.add_handler(CommandHandler('admin_pending', admin_list_pending))
    app.add_handler(CommandHandler('broadcast', admin_broadcast))
    
    # Admin callback handlers
    app.add_handler(CallbackQueryHandler(admin_approve_id, pattern='^approve_id_'))
    app.add_handler(CallbackQueryHandler(admin_reject_id, pattern='^reject_id_'))
    app.add_handler(CallbackQueryHandler(admin_send_video, pattern='^send_video_'))
    app.add_handler(CallbackQueryHandler(admin_create_account, pattern='^create_account_'))
    app.add_handler(CallbackQueryHandler(user_payment_sent, pattern='^payment_sent$'))
    app.add_handler(CallbackQueryHandler(user_resend_id, pattern='^resend_id$'))
    app.add_handler(CallbackQueryHandler(check_status, pattern='^check_status$'))
    
    # New ID handler
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex(r'^\d{9,10}$'), receive_new_id))
    app.add_handler(conv_handler)
    
    # Start the bot
    logger.info("Bot started!")
    
    # Start daily reminders in background thread
    import threading
    reminder_thread = threading.Thread(target=start_scheduler, args=(app,), daemon=True)
    reminder_thread.start()
    
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
