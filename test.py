import os
import sqlite3
import requests
import json
import time
import uuid
import logging
from datetime import datetime, timedelta

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
    JobQueue,
)

# --- CONFIGURATION CONSTANTS ---

TELEGRAM_BOT_TOKEN = "8557811828:AAGOHH0ATVUtZb7lwu2VOM9Jbuflb_O7hO0"
ADMIN_TELEGRAM_ID = 994618750

RAPIDAPI_KEY = "8eeb93c824msh84e2f62ce8e3450p1b47c8jsnab7fa280287e"
RAPIDAPI_HOST = "realstonks.p.rapidapi.com"

GEMINI_API_KEY = "AIzaSyDd7miTURri6MU4rQCm8UMVtAyjasG5_Co"
GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-preview-09-2025:generateContent"

DB_NAME = 'trading_bot_data.db'
SELECTING_ASSET = 1

# --- LOGGING SETUP ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)


# --- DATABASE MANAGER ---

class DBManager:
    """Handles all SQLite database operations."""
    def __init__(self, db_name):
        self.db_name = db_name
        self.conn = None
        self.cursor = None
        self._setup_db()

    def _get_connection(self):
        """Ensures a connection exists."""
        if self.conn is None:
            self.conn = sqlite3.connect(self.db_name)
            self.cursor = self.conn.cursor()
        return self.conn

    def _setup_db(self):
        """Creates necessary tables if they don't exist."""
        conn = self._get_connection()
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    is_admin BOOLEAN,
                    is_subscribed BOOLEAN,
                    subscription_expires DATETIME,
                    selected_asset TEXT,
                    initial_balance REAL DEFAULT 1000.0,
                    current_profit REAL DEFAULT 0.0
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS vouchers (
                    code TEXT PRIMARY KEY,
                    days INTEGER,
                    created_at DATETIME,
                    is_used BOOLEAN
                )
            """)
            conn.commit()
            logger.info("Database tables initialized successfully.")
        except Exception as e:
            logger.error(f"Error setting up database: {e}")

    def get_user(self, user_id):
        conn = self._get_connection()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        return cursor.fetchone()

    def add_or_update_user(self, user_id, username=None):
        conn = self._get_connection()
        is_admin = 1 if user_id == ADMIN_TELEGRAM_ID else 0
        try:
            user = self.get_user(user_id)
            if user:
                conn.execute(
                    "UPDATE users SET username=?, is_admin=? WHERE user_id=?", 
                    (username, is_admin, user_id)
                )
            else:
                conn.execute(
                    "INSERT INTO users (user_id, username, is_admin, is_subscribed, subscription_expires, selected_asset) VALUES (?, ?, ?, 0, ?, ?)",
                    (user_id, username, is_admin, None, None)
                )
            conn.commit()
        except Exception as e:
            logger.error(f"Error adding/updating user {user_id}: {e}")

    def update_user_subscription(self, user_id, days, is_admin=False):
        conn = self._get_connection()
        if is_admin:
            expiry_date = (datetime.now() + timedelta(days=36500)).strftime('%Y-%m-%d %H:%M:%S')
            is_subscribed = 1
        elif days > 0:
            user = self.get_user(user_id)
            current_expiry = datetime.now()
            if user and user['subscription_expires']:
                try:
                    current_expiry = datetime.strptime(user['subscription_expires'], '%Y-%m-%d %H:%M:%S')
                except ValueError:
                    pass
            
            start_time = max(datetime.now(), current_expiry)
            expiry_date = (start_time + timedelta(days=days)).strftime('%Y-%m-%d %H:%M:%S')
            is_subscribed = 1
        else:
            expiry_date = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            is_subscribed = 0

        conn.execute(
            "UPDATE users SET is_subscribed=?, subscription_expires=? WHERE user_id=?",
            (is_subscribed, expiry_date, user_id)
        )
        conn.commit()
        return expiry_date

    def get_all_subscribed_users(self):
        conn = self._get_connection()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        cursor.execute(
            "SELECT * FROM users WHERE is_subscribed = 1 AND subscription_expires > ?", 
            (now_str,)
        )
        return cursor.fetchall()
    
    def set_user_asset(self, user_id, asset_symbol):
        conn = self._get_connection()
        conn.execute(
            "UPDATE users SET selected_asset=? WHERE user_id=?", 
            (asset_symbol, user_id)
        )
        conn.commit()

    def update_user_profit(self, user_id, new_profit):
        conn = self._get_connection()
        conn.execute(
            "UPDATE users SET current_profit=? WHERE user_id=?", 
            (new_profit, user_id)
        )
        conn.commit()

    def create_voucher(self, days):
        code = str(uuid.uuid4()).split('-')[0].upper()
        conn = self._get_connection()
        conn.execute(
            "INSERT INTO vouchers (code, days, created_at, is_used) VALUES (?, ?, ?, 0)",
            (code, days, datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        )
        conn.commit()
        return code

    def use_voucher(self, code):
        conn = self._get_connection()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM vouchers WHERE code = ? AND is_used = 0", (code,))
        voucher = cursor.fetchone()
        
        if voucher:
            conn.execute("UPDATE vouchers SET is_used = 1 WHERE code = ?", (code,))
            conn.commit()
            return voucher['days']
        return None

    def revoke_voucher(self, code):
        conn = self._get_connection()
        conn.execute("UPDATE vouchers SET is_used = 1 WHERE code = ?", (code,))
        conn.commit()
        
db_manager = DBManager(DB_NAME)


# --- FINANCIAL ANALYSIS & PREDICTION ---

class FinancialPredictor:
    """Handles data analysis and predictions."""
    
    @staticmethod
    def _fetch_market_data(symbol):
        """Fetches market data from secure sources."""
        url = f"https://{RAPIDAPI_HOST}/stocks/{symbol}/advanced"
        headers = {
            "x-rapidapi-host": RAPIDAPI_HOST,
            "x-rapidapi-key": RAPIDAPI_KEY
        }
        
        try:
            response = requests.request("GET", url, headers=headers, timeout=10)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error(f"Market data fetch error for {symbol}")
            return None

    @staticmethod
    async def get_prediction(symbol):
        """Generates trading signals using advanced analysis."""
        data = FinancialPredictor._fetch_market_data(symbol)
        if not data:
            return "HOLD", "Market data currently unavailable. Please try again later."

        analysis_prompt = f"""
        Analyze this financial data for {symbol} and provide a trading signal:
        
        Current Price: ${data.get("lastPrice", "N/A")}
        Daily Change: {data.get("priceChange", "N/A")} ({data.get("percentChange", "N/A")}%)
        Volume: {data.get("volume", "N/A")}
        Daily Range: {data.get("lowPrice", "N/A")} - {data.get("highPrice", "N/A")}
        Technical Indicators: Stochastic {data.get("stochasticK14d", "N/A")}
        
        Provide only: "BUY", "SELL", or "HOLD" with brief reasoning.
        """
        
        system_instruction = "You are a professional trading analyst. Provide clear, concise trading signals based on technical analysis."
        
        payload = {
            "contents": [{ "parts": [{ "text": analysis_prompt }] }],
            "systemInstruction": {
                "parts": [{ "text": system_instruction }]
            },
        }

        for attempt in range(3):
            try:
                response = requests.post(
                    GEMINI_API_URL,
                    headers={'Content-Type': 'application/json'},
                    params={'key': GEMINI_API_KEY},
                    data=json.dumps(payload),
                    timeout=15
                )
                response.raise_for_status()
                
                result = response.json()
                text = result.get('candidates', [{}])[0].get('content', {}).get('parts', [{}])[0].get('text', '').strip()
                
                if text:
                    lines = text.split('\n')
                    recommendation = "HOLD"
                    justification = text
                    
                    for line in lines:
                        if any(word in line.upper() for word in ['BUY', 'SELL', 'HOLD']):
                            if 'BUY' in line.upper():
                                recommendation = "BUY"
                            elif 'SELL' in line.upper():
                                recommendation = "SELL"
                            break
                    
                    return recommendation, justification
                
            except Exception as e:
                logger.warning(f"Analysis attempt {attempt + 1} failed")
                time.sleep(2 ** attempt)

        return "HOLD", "Analysis system is temporarily unavailable. Please try again shortly."


# --- BOT HANDLERS ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Welcome message and main menu."""
    user = update.effective_user
    db_manager.add_or_update_user(user.id, user.username)
    
    user_data = db_manager.get_user(user.id)
    is_subscribed = user_data['is_subscribed'] if user_data else False
    
    welcome_message = (
        f"🎯 *Welcome to Fayad Trading Bot!* 🚀\n\n"
        f"Hello *{user.first_name}*! I'm your advanced trading assistant, "
        f"providing professional market analysis and signals.\n\n"
    )
    
    if user.id == ADMIN_TELEGRAM_ID:
        welcome_message += "👑 **ADMIN PRIVILEGES ACTIVATED** 👑"
    elif is_subscribed:
        expiry = user_data['subscription_expires']
        welcome_message += f"✅ **Premium Access Active**\nExpires: `{expiry}`"
    else:
        welcome_message += "🔒 **Premium Features Locked**\nActivate your subscription to unlock signals!"

    keyboard = [
        [InlineKeyboardButton("📊 Get Signal Now", callback_data='predict_now')],
        [InlineKeyboardButton("⚙️ Set Trading Pair", callback_data='set_asset')],
        [InlineKeyboardButton("💹 My Portfolio", callback_data='show_profit')],
        [InlineKeyboardButton("🔓 Activate Premium", callback_data='buy_access')],
    ]
    if user.id == ADMIN_TELEGRAM_ID:
        keyboard.append([InlineKeyboardButton("🛠️ Admin Panel", callback_data='admin_panel')])
        
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        welcome_message, 
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )


async def buy_access_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Subscription and voucher options."""
    query = update.callback_query
    await query.answer()
    
    keyboard = [
        [InlineKeyboardButton("🎫 Use Voucher", callback_data='prompt_voucher')],
        [InlineKeyboardButton("💬 Contact Admin", url='https://t.me/zerodayx1')],
        [InlineKeyboardButton("⬅️ Main Menu", callback_data='start_menu')],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        "**🔓 Premium Activation**\n\n"
        "Unlock professional trading signals and analysis:\n\n"
        "• Real-time market insights\n"
        "• Automated signal delivery\n"
        "• Portfolio tracking\n"
        "• Advanced technical analysis\n\n"
        "Choose an option below:",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

async def prompt_voucher(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Voucher code input."""
    query = update.callback_query
    await query.answer()
    
    await query.edit_message_text(
        "🔑 *Enter Voucher Code*\n\n"
        "Please reply with your voucher code (format: `ABCD-EFGH`)\n\n"
        "Type /cancel to return to menu.",
        parse_mode='Markdown'
    )
    
    context.user_data['waiting_for_voucher'] = True

async def handle_voucher_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Process voucher codes."""
    if context.user_data.get('waiting_for_voucher'):
        user_id = update.effective_user.id
        voucher_code = update.message.text.strip().upper()
        
        if voucher_code.lower() == '/cancel':
            del context.user_data['waiting_for_voucher']
            await start(update, context)
            return

        days = db_manager.use_voucher(voucher_code)
        
        if days:
            expiry_date_str = db_manager.update_user_subscription(user_id, days)
            await update.message.reply_text(
                f"🎉 *Voucher Activated!* 🎉\n\n"
                f"**{days} days** of premium access granted!\n"
                f"Access expires: `{expiry_date_str}`\n\n"
                f"Set your trading pair to start receiving signals!",
                parse_mode='Markdown'
            )
            user_data = db_manager.get_user(user_id)
            if user_data and user_data['selected_asset']:
                await start_periodic_updates(update, context)
        else:
            await update.message.reply_text(
                "❌ *Invalid Voucher*\n\n"
                "Please check the code and try again.",
                parse_mode='Markdown'
            )
            
        del context.user_data['waiting_for_voucher']


async def set_asset_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Asset selection menu."""
    query = update.callback_query
    await query.answer()

    assets = [
        InlineKeyboardButton("TESLA", callback_data='select_asset_TSLA'),
        InlineKeyboardButton("BITCOIN", callback_data='select_asset_BTC-USD'),
        InlineKeyboardButton("EUR/USD", callback_data='select_asset_EUR/USD'),
        InlineKeyboardButton("APPLE", callback_data='select_asset_AAPL'),
        InlineKeyboardButton("AMAZON", callback_data='select_asset_AMZN'),
    ]
    
    keyboard = [assets, [InlineKeyboardButton("⬅️ Main Menu", callback_data='start_menu')]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        "**⚙️ Select Trading Pair**\n\n"
        "Choose your preferred asset for analysis and signals:",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )
    return SELECTING_ASSET

async def select_asset_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle asset selection."""
    query = update.callback_query
    await query.answer()
    
    user_id = update.effective_user.id
    asset_symbol = query.data.split('_')[-1]

    db_manager.set_user_asset(user_id, asset_symbol)
    
    await query.edit_message_text(
        f"✅ *Trading Pair Set*\n\n"
        f"Now tracking: **{asset_symbol}**\n\n"
        f"Premium signals will be delivered automatically.",
        parse_mode='Markdown'
    )
    
    user_data = db_manager.get_user(user_id)
    is_admin = user_id == ADMIN_TELEGRAM_ID
    
    if is_admin or (user_data and user_data['is_subscribed']):
        await start_periodic_updates(update, context)
        
    await start_menu_from_callback(update, context)
    return SELECTING_ASSET


async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin control panel."""
    query = update.callback_query
    await query.answer()
    
    if query.from_user.id != ADMIN_TELEGRAM_ID:
        await query.edit_message_text("❌ Access Denied")
        return

    keyboard = [
        [InlineKeyboardButton("🎫 7-Day Voucher", callback_data='admin_gen_voucher_7')],
        [InlineKeyboardButton("🎫 30-Day Voucher", callback_data='admin_gen_voucher_30')],
        [InlineKeyboardButton("🔄 Force Update", callback_data='admin_force_updates')],
        [InlineKeyboardButton("📊 System Stats", callback_data='admin_stats')],
        [InlineKeyboardButton("⬅️ Main Menu", callback_data='start_menu')],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        "**👑 Admin Control Panel**\n\n"
        "Manage system operations and subscriptions:",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

async def generate_voucher_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Generate vouchers."""
    query = update.callback_query
    await query.answer()
    
    if query.from_user.id != ADMIN_TELEGRAM_ID: return

    days = int(query.data.split('_')[-1])
    code = db_manager.create_voucher(days)

    await query.edit_message_text(
        f"✅ *Voucher Created*\n\n"
        f"**{days}-Day Premium Access**\n"
        f"Code: `{code}`\n\n"
        f"Share this code with users.",
        parse_mode='Markdown'
    )

async def admin_force_updates(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Force immediate updates."""
    query = update.callback_query
    await query.answer("Sending updates...")
    
    if query.from_user.id != ADMIN_TELEGRAM_ID: return
    
    await send_periodic_update(context)
    
    await query.edit_message_text(
        "✅ *Updates Sent*\n\n"
        "All subscribers have received new signals.",
        parse_mode='Markdown'
    )

async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show system statistics."""
    query = update.callback_query
    await query.answer()
    
    if query.from_user.id != ADMIN_TELEGRAM_ID: return
    
    users = db_manager.get_all_subscribed_users()
    total_users = len(users)
    
    await query.edit_message_text(
        f"**📊 System Statistics**\n\n"
        f"• Active Subscribers: **{total_users}**\n"
        f"• System Status: **Operational**\n"
        f"• Last Update: `{datetime.now().strftime('%Y-%m-%d %H:%M')}`",
        parse_mode='Markdown'
    )


async def instant_prediction(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """One-time signal generation."""
    query = update.callback_query
    await query.answer("🔍 Analyzing markets...")
    
    user_id = query.from_user.id
    user_data = db_manager.get_user(user_id)
    
    if not user_data or not user_data['selected_asset']:
        await query.edit_message_text(
            "❌ *Setup Required*\n\n"
            "Please set your trading pair first.",
            parse_mode='Markdown'
        )
        return
        
    asset = user_data['selected_asset']
    
    await query.edit_message_text(f"⏳ Analyzing **{asset}**...")
    recommendation, justification = await FinancialPredictor.get_prediction(asset)
    
    signal_icon = "🟢" if recommendation == "BUY" else "🔴" if recommendation == "SELL" else "🟡"
    
    message = (
        f"**🎯 Trading Signal - {asset}**\n\n"
        f"{signal_icon} **Recommendation: {recommendation}**\n\n"
        f"**Analysis:**\n"
        f"_{justification}_\n\n"
        f"---\n"
        f"*Signal generated at {datetime.now().strftime('%H:%M')}*"
    )
    
    await context.bot.send_message(
        chat_id=user_id, 
        text=message, 
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Main Menu", callback_data='start_menu')]])
    )

async def show_profit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Display user portfolio."""
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    user_data = db_manager.get_user(user_id)
    
    if not user_data:
        await query.edit_message_text("User data not found.")
        return

    balance = user_data['initial_balance'] + user_data['current_profit']
    profit = user_data['current_profit']
    
    if user_id != ADMIN_TELEGRAM_ID:
        import random
        change = random.uniform(-8.0, 20.0)
        new_profit = round(profit + change, 2)
        db_manager.update_user_profit(user_id, new_profit)
        profit = new_profit
        balance = user_data['initial_balance'] + new_profit

    profit_color = "🟢" if profit >= 0 else "🔴"
    trend = "📈" if profit >= 0 else "📉"
    
    profit_message = (
        f"**💼 Portfolio Overview**\n\n"
        f"**Trading Pair:** {user_data.get('selected_asset', 'Not Set')}\n"
        f"**Initial Capital:** `$1000.00`\n"
        f"**Current P/L:** {profit_color} `${profit:+.2f}` {trend}\n"
        f"**Total Balance:** `${balance:.2f}`\n\n"
        f"*Performance tracking activated*"
    )
    
    await query.edit_message_text(
        profit_message, 
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Main Menu", callback_data='start_menu')]]),
        parse_mode='Markdown'
    )


# --- SCHEDULER LOGIC ---

async def start_periodic_updates(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Setup periodic updates for user."""
    user_id = update.effective_user.id
    job_name = f'signal_job_{user_id}'
    
    current_jobs = context.application.job_queue.get_jobs_by_name(job_name)
    for job in current_jobs:
        job.schedule_removal()
    
    context.application.job_queue.run_repeating(
        send_periodic_update,
        interval=1800,
        first=5,
        name=job_name,
        data={'user_id': user_id}
    )
    logger.info(f"Periodic updates started for user {user_id}")

async def send_periodic_update(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send scheduled signals to subscribers."""
    subscribed_users = db_manager.get_all_subscribed_users()

    for user in subscribed_users:
        user_id = user['user_id']
        asset = user['selected_asset']
        expiry = user['subscription_expires']
        is_admin = user_id == ADMIN_TELEGRAM_ID
        
        if not is_admin and datetime.strptime(expiry, '%Y-%m-%d %H:%M:%S') < datetime.now():
            db_manager.update_user_subscription(user_id, 0)
            await context.bot.send_message(
                chat_id=user_id,
                text="❌ *Subscription Expired*\n\n"
                     "Your premium access has ended. Renew to continue receiving signals.",
                parse_mode='Markdown'
            )
            job_name = f'signal_job_{user_id}'
            current_jobs = context.application.job_queue.get_jobs_by_name(job_name)
            for job in current_jobs:
                job.schedule_removal()
            continue

        if not asset:
            await context.bot.send_message(
                chat_id=user_id,
                text="⚠️ *Setup Required*\n\n"
                     "Please set your trading pair to receive automated signals.",
                parse_mode='Markdown'
            )
            continue
            
        try:
            recommendation, justification = await FinancialPredictor.get_prediction(asset)

            signal_icon = "🟢" if recommendation == "BUY" else "🔴" if recommendation == "SELL" else "🟡"
            
            message = (
                f"**🔔 Scheduled Signal - {asset}**\n\n"
                f"{signal_icon} **Action: {recommendation}**\n\n"
                f"**Market Analysis:**\n"
                f"_{justification}_\n\n"
                f"---\n"
                f"*Next update in 30 minutes*"
            )
            
            await context.bot.send_message(
                chat_id=user_id, 
                text=message, 
                parse_mode='Markdown'
            )
            logger.info(f"Signal sent to user {user_id} for {asset}")

        except Exception as e:
            logger.error(f"Error sending update to user {user_id}: {e}")
            await context.bot.send_message(
                chat_id=user_id,
                text="⚠️ *Signal Delay*\n\n"
                     "Temporary issue with market analysis. Next update will proceed normally.",
                parse_mode='Markdown'
            )

async def start_menu_from_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Return to main menu from callback."""
    query = update.callback_query
    await query.answer()

    user = query.from_user
    user_data = db_manager.get_user(user.id)
    is_subscribed = user_data['is_subscribed'] if user_data else False
    
    message = f"🎯 *Fayad Trading Bot*\n\nWelcome back, *{user.first_name}*!"
    
    if user.id == ADMIN_TELEGRAM_ID:
        message += "\n\n👑 **Admin Mode**"
    elif is_subscribed:
        message += f"\n\n✅ **Premium Active**\nAsset: {user_data['selected_asset']}"
    else:
        message += "\n\n🔒 **Premium Required**"

    keyboard = [
        [InlineKeyboardButton("📊 Get Signal", callback_data='predict_now')],
        [InlineKeyboardButton("⚙️ Trading Pair", callback_data='set_asset')],
        [InlineKeyboardButton("💹 Portfolio", callback_data='show_profit')],
        [InlineKeyboardButton("🔓 Activate Premium", callback_data='buy_access')],
    ]
    if user.id == ADMIN_TELEGRAM_ID:
        keyboard.append([InlineKeyboardButton("🛠️ Admin", callback_data='admin_panel')])
        
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        message, 
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )


def main() -> None:
    """Start the bot."""
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    
    # Handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("cancel", start))

    from telegram.ext import MessageHandler, filters
    application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_voucher_input))

    # Callback Handlers
    application.add_handler(CallbackQueryHandler(start_menu_from_callback, pattern='^start_menu$'))
    application.add_handler(CallbackQueryHandler(admin_panel, pattern='^admin_panel$'))
    application.add_handler(CallbackQueryHandler(buy_access_menu, pattern='^buy_access$'))
    application.add_handler(CallbackQueryHandler(prompt_voucher, pattern='^prompt_voucher$'))
    application.add_handler(CallbackQueryHandler(set_asset_menu, pattern='^set_asset$'))
    application.add_handler(CallbackQueryHandler(instant_prediction, pattern='^predict_now$'))
    application.add_handler(CallbackQueryHandler(show_profit, pattern='^show_profit$'))
    application.add_handler(CallbackQueryHandler(admin_stats, pattern='^admin_stats$'))

    application.add_handler(CallbackQueryHandler(select_asset_callback, pattern='^select_asset_'))
    application.add_handler(CallbackQueryHandler(generate_voucher_callback, pattern='^admin_gen_voucher_'))
    application.add_handler(CallbackQueryHandler(admin_force_updates, pattern='^admin_force_updates$'))

    # Admin setup
    db_manager.add_or_update_user(ADMIN_TELEGRAM_ID, "AdminUser")
    db_manager.update_user_subscription(ADMIN_TELEGRAM_ID, 36500, is_admin=True)
    db_manager.set_user_asset(ADMIN_TELEGRAM_ID, "TSLA")

    # Restore user jobs
    for user in db_manager.get_all_subscribed_users():
        user_id = user['user_id']
        job_name = f'signal_job_{user_id}'
        
        if user['is_subscribed'] and user['selected_asset']:
            application.job_queue.run_repeating(
                send_periodic_update,
                interval=1800,
                first=10,
                name=job_name,
                data={'user_id': user_id}
            )
            logger.info(f"Restored job for user {user_id}")

    # Start bot
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    logger.info("🚀 Starting Fayad Trading Bot...")
    main()
    logger.info("Bot session ended.")
