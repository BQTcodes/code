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

# --- CONFIGURATION CONSTANTS (UPDATE THESE) ---

# Replace with your actual Bot Token
TELEGRAM_BOT_TOKEN = "8557811828:AAGOHH0ATVUtZb7lwu2VOM9Jbuflb_O7hO0"
# Replace with your Telegram Admin User ID (994618750)
ADMIN_TELEGRAM_ID = 994618750

# RapidAPI Configuration
RAPIDAPI_KEY =
"8eeb93c824msh84e2f62ce8e3450p1b47c8jsnab7fa280287e"  # Replace with your key
RAPIDAPI_HOST = "realstonks.p.rapidapi.com"

# Gemini API Configuration (for advanced prediction logic)
GEMINI_API_KEY = "AIzaSyDd7miTURri6MU4rQCm8UMVtAyjasG5_Co" # Replace with your key
GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-preview-09-2025:generateContent"

# Database Configuration
DB_NAME = 'trading_bot_data.db'

# Define conversation states for user input (e.g., selecting a stock)
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
            # Check if user exists
            user = self.get_user(user_id)
            if user:
                conn.execute(
                    "UPDATE users SET username=?, is_admin=? WHERE user_id=?", 
                    (username, is_admin, user_id)
                )
            else:
                # New user insertion
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
            # Admins have unlimited access (far future date)
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
            
            # Start counting from now or the current expiration date, whichever is later
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
        # Check if subscription_expires is in the future
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

    # Voucher Management
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
        
# Initialize DB
db_manager = DBManager(DB_NAME)


# --- FINANCIAL ANALYSIS & PREDICTION ---

class FinancialPredictor:
    """Handles fetching data and generating predictions using the Gemini API."""
    
    @staticmethod
    def _fetch_stock_data(symbol):
        """Fetches a stock snapshot from the RapidAPI endpoint."""
        url = f"https://{RAPIDAPI_HOST}/stocks/{symbol}/advanced"
        headers = {
            "x-rapidapi-host": RAPIDAPI_HOST,
            "x-rapidapi-key": RAPIDAPI_KEY
        }
        
        try:
            response = requests.request("GET", url, headers=headers)
            response.raise_for_status() # Raise HTTPError for bad responses (4xx or 5xx)
            return response.json()
        except requests.exceptions.RequestException as e:
            logger.error(f"Error fetching data for {symbol}: {e}")
            return None

    @staticmethod
    async def get_prediction(symbol):
        """
        Uses Gemini API to interpret the fetched data and provide a prediction.
        This simulates the "master math" and "studying charts" requirement.
        """
        data = FinancialPredictor._fetch_stock_data(symbol)
        if not data:
            return "HOLD", "Could not fetch current market data. Service temporarily unavailable."

        # Construct the detailed prompt based on the snapshot data
        prompt_data = f"""
        Analyze the following real-time financial data snapshot for the asset {symbol} to provide a confident trading recommendation (BUY, SELL, or HOLD) for a MetaTrader bot.

        **Current Snapshot Data:**
        - Symbol: {data.get("symbol", "N/A")}
        - Company: {data.get("symbolName", "N/A")}
        - Last Price: ${data.get("lastPrice", "N/A")}
        - Price Change (Today): {data.get("priceChange", "N/A")} ({data.get("percentChange", "N/A") * 100 if isinstance(data.get("percentChange"), (int, float)) else 'N/A'}%)
        - Bid/Ask: {data.get("bidPrice", "N/A")} / {data.get("askPrice", "N/A")} (Size: {data.get("bidSize", "N/A")} / {data.get("askSize", "N/A")})
        - Day Range: Low {data.get("lowPrice", "N/A")} / High {data.get("highPrice", "N/A")}
        - Previous Close: ${data.get("previousPrice", "N/A")}
        - Trading Volume: {data.get("volume", "N/A")} (Avg: {data.get("averageVolume", "N/A")})
        - Stochastic K-14d Oscillator: {data.get("stochasticK14d", "N/A")} (Note: Below 20 is oversold, Above 80 is overbought)
        - Weighted Alpha: {data.get("weightedAlpha", "N/A")}
        - 1-Year Range: Low {data.get("lowPrice1y", "N/A")} / High {data.get("highPrice1y", "N/A")}

        **Analysis Instructions (Act as a Master Quant Analyst):**
        1. Evaluate the price movement relative to the previous close and the daily range.
        2. Interpret the Stochastic Oscillator value and its implication for momentum.
        3. Assess the current volume against the average volume to gauge market interest.
        4. Consider the 1-year range to determine the current position relative to long-term volatility.
        5. Provide your final recommendation in the format: "Recommendation: [BUY/SELL/HOLD]" followed by a detailed, concise justification.

        Provide only the Recommendation and Justification.
        """
        
        system_prompt = "You are a Master Quant Analyst for a high-frequency trading bot. Your goal is to provide extremely accurate, mathematically sound, and concise trading signals (BUY, SELL, or HOLD) based on the provided technical data snapshot. Your output must be based purely on the data analysis, simulating a powerful analytical engine."
        
        payload = {
            "contents": [{ "parts": [{ "text": prompt_data }] }],
            "systemInstruction": {
                "parts": [{ "text": system_prompt }]
            },
        }

        # Implementing a simple retry mechanism (Exponential Backoff not fully implemented due to single-file constraint)
        for attempt in range(3):
            try:
                # Assuming the environment can handle a direct fetch call to the Gemini API
                response = requests.post(
                    GEMINI_API_URL,
                    headers={'Content-Type': 'application/json'},
                    params={'key': GEMINI_API_KEY},
                    data=json.dumps(payload)
                )
                response.raise_for_status()
                
                result = response.json()
                text = result.get('candidates', [{}])[0].get('content', {}).get('parts', [{}])[0].get('text', '').strip()
                
                if text:
                    # Parse the result to extract recommendation and justification
                    lines = text.split('\n')
                    recommendation = "HOLD"
                    justification = text
                    
                    for line in lines:
                        if line.lower().startswith("recommendation:"):
                            recommendation = line.split(":")[1].strip().upper()
                            break
                    
                    return recommendation, justification
                
            except requests.exceptions.RequestException as e:
                logger.warning(f"Gemini API request failed (Attempt {attempt + 1}): {e}")
                time.sleep(2 ** attempt) # Wait before retrying

        return "HOLD", "Prediction engine failed to return an analysis after multiple attempts. Manual inspection required."


# --- BOT HANDLERS ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sends welcome message and checks for admin status."""
    user = update.effective_user
    db_manager.add_or_update_user(user.id, user.username)
    
    # Check subscription status
    user_data = db_manager.get_user(user.id)
    is_subscribed = user_data['is_subscribed'] if user_data else False
    
    welcome_message = (
        f"🌟 Welcome, *{user.first_name}*, to the Quantum Trader Bot! 🚀\n\n"
        "I am your highly advanced quantitative analysis engine, dedicated to finding "
        "the most profitable Buy/Sell signals based on real-time market data."
    )
    
    # Check if the user is the admin
    if user.id == ADMIN_TELEGRAM_ID:
        welcome_message += "\n\n**👑 ADMIN MODE ACTIVATED 👑** You have unlimited access."
    elif is_subscribed:
        expiry = user_data['subscription_expires']
        welcome_message += f"\n\n✅ **Subscription Active!** Your access expires on: `{expiry}`."
        welcome_message += "\nI will send you a powerful prediction every 30 minutes for your chosen asset."
    else:
        welcome_message += "\n\n❌ **Access Required.** Please choose an option below to start trading signals!"

    keyboard = [
        [InlineKeyboardButton("📈 Get Instant Prediction", callback_data='predict_now')],
        [InlineKeyboardButton("⚙️ Set Trading Asset", callback_data='set_asset')],
        [InlineKeyboardButton("📊 My Performance & Profit", callback_data='show_profit')],
        [InlineKeyboardButton("💳 Buy Access / Use Voucher", callback_data='buy_access')],
    ]
    if user.id == ADMIN_TELEGRAM_ID:
        keyboard.append([InlineKeyboardButton("🛠️ Admin Panel", callback_data='admin_panel')])
        
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        welcome_message, 
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )


# --- SUBSCRIPTION / VOUCHER FLOW ---

async def buy_access_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Shows the access and voucher options."""
    query = update.callback_query
    await query.answer()
    
    keyboard = [
        [InlineKeyboardButton("🔑 Use Voucher Code", callback_data='prompt_voucher')],
        [InlineKeyboardButton("💰 Contact to Buy Access (@zerodayx1)", url='https://t.me/zerodayx1')],
        [InlineKeyboardButton("⬅️ Back to Main Menu", callback_data='start_menu')],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        "**💳 Access & Vouchers**\n\n"
        "To activate your 30-minute signals, you need an active subscription.\n\n"
        "1. **Buy Access:** Contact the administrator via Telegram to purchase a subscription.\n"
        "2. **Use Voucher:** If you have a code, enter it to activate your trial or purchased time.",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

async def prompt_voucher(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Initiates the conversation to receive a voucher code."""
    query = update.callback_query
    await query.answer()
    
    await query.edit_message_text(
        "Please reply to this message with your 8-character voucher code (e.g., `ABCD-EFGH`).\n\n"
        "To cancel, type `/cancel_voucher`."
    )
    
    # Set the user state to expect a voucher code
    context.user_data['waiting_for_voucher'] = True

async def handle_voucher_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Processes the user's message as a voucher code."""
    if context.user_data.get('waiting_for_voucher'):
        user_id = update.effective_user.id
        voucher_code = update.message.text.strip().upper()
        
        if voucher_code.lower() == '/cancel_voucher':
            del context.user_data['waiting_for_voucher']
            await start(update, context) # Return to start menu
            return

        days = db_manager.use_voucher(voucher_code)
        
        if days:
            expiry_date_str = db_manager.update_user_subscription(user_id, days)
            await update.message.reply_text(
                f"🎉 **Voucher Success!** 🎉\n\n"
                f"You have been granted **{days} days** of premium access.\n"
                f"Your subscription now expires on: `{expiry_date_str}`.\n\n"
                "To begin receiving signals, use the **Set Trading Asset** button.",
                parse_mode='Markdown'
            )
            # Add job to the queue if the user is subscribed and has an asset selected
            user_data = db_manager.get_user(user_id)
            if user_data and user_data['selected_asset']:
                await start_periodic_updates(update, context)
        else:
            await update.message.reply_text(
                "❌ **Invalid or Used Voucher.** Please check your code and try again, or contact the admin."
            )
            
        del context.user_data['waiting_for_voucher']


# --- ASSET SELECTION FLOW ---

async def set_asset_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Prompts the user to select the asset to track."""
    query = update.callback_query
    await query.answer()

    # Supported asset list (can be expanded)
    assets = [
        InlineKeyboardButton("TSLA (Tesla Stock)", callback_data='select_asset_TSLA'),
        InlineKeyboardButton("BTC/USD (Crypto)", callback_data='select_asset_BTC-USD'),
        InlineKeyboardButton("EUR/USD (Forex)", callback_data='select_asset_EUR/USD'),
    ]
    
    keyboard = [assets, [InlineKeyboardButton("⬅️ Back to Main Menu", callback_data='start_menu')]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        "**⚙️ Select Your Trading Asset**\n\n"
        "Please choose the asset you want the Quantum Trader Bot to track. "
        "You will receive a prediction for this asset every 30 minutes (if subscribed).",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )
    return SELECTING_ASSET

async def select_asset_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles the selection of the trading asset."""
    query = update.callback_query
    await query.answer()
    
    user_id = update.effective_user.id
    # Extract asset symbol from callback data (e.g., 'select_asset_TSLA')
    asset_symbol = query.data.split('_')[-1]

    db_manager.set_user_asset(user_id, asset_symbol)
    
    await query.edit_message_text(
        f"✅ **Asset Set!** You are now tracking: *{asset_symbol}*.\n\n"
        "If your subscription is active, your periodic signals will start soon (or are already running). "
        "You can check your status in the main menu.",
        parse_mode='Markdown'
    )
    
    # Check if user is subscribed and start/restart the periodic job
    user_data = db_manager.get_user(user_id)
    is_admin = user_id == ADMIN_TELEGRAM_ID
    
    if is_admin or (user_data and user_data['is_subscribed']):
        await start_periodic_updates(update, context)
        
    await start_menu_from_callback(update, context)
    return SELECTING_ASSET


# --- ADMIN PANEL ---

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin-only commands menu."""
    query = update.callback_query
    await query.answer()
    
    if query.from_user.id != ADMIN_TELEGRAM_ID:
        await query.edit_message_text("Unauthorized access.")
        return

    keyboard = [
        [InlineKeyboardButton("➕ Generate 10 Day Voucher", callback_data='admin_gen_voucher_10')],
        [InlineKeyboardButton("➕ Generate 30 Day Voucher", callback_data='admin_gen_voucher_30')],
        [InlineKeyboardButton("Revoke Voucher (Manual)", callback_data='admin_revoke_prompt')],
        [InlineKeyboardButton("Force All Updates (Testing)", callback_data='admin_force_updates')],
        [InlineKeyboardButton("⬅️ Back to Main Menu", callback_data='start_menu')],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        "**👑 Admin Control Panel 👑**\n\nManage subscriptions and bot operations.",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

async def generate_voucher_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Generates a time-based voucher."""
    query = update.callback_query
    await query.answer()
    
    if query.from_user.id != ADMIN_TELEGRAM_ID: return

    days = int(query.data.split('_')[-1]) # e.g., 'admin_gen_voucher_10' -> 10
    code = db_manager.create_voucher(days)

    await query.edit_message_text(
        f"✅ **New {days}-Day Voucher Created!**\n\n"
        f"Code: `{code}`\n\n"
        f"Share this code with the user. They can use it in the 'Buy Access / Use Voucher' menu.",
        parse_mode='Markdown'
    )
    await start_menu_from_callback(update, context)


async def admin_force_updates(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Immediately runs the update job for all subscribed users (for testing)."""
    query = update.callback_query
    await query.answer("Forcing updates now...")
    
    if query.from_user.id != ADMIN_TELEGRAM_ID: return
    
    # Directly call the periodic job function
    await send_periodic_update(context)
    
    await query.edit_message_text(
        "✅ **Update Cycle Forced.** Predictions have been sent to all subscribed users.",
        parse_mode='Markdown'
    )
    await start_menu_from_callback(update, context)

async def admin_revoke_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Initiates the conversation to revoke a voucher."""
    query = update.callback_query
    await query.answer()
    
    if query.from_user.id != ADMIN_TELEGRAM_ID: return
    
    await query.edit_message_text(
        "Please reply to this message with the voucher code you wish to revoke.\n\n"
        "Revoking marks it as used, preventing any future use.\n"
        "To cancel, type `/cancel_revoke`."
    )
    context.user_data['waiting_for_revoke_code'] = True

async def handle_revoke_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Processes the admin's message as a voucher code to revoke."""
    if context.user_data.get('waiting_for_revoke_code'):
        if update.effective_user.id != ADMIN_TELEGRAM_ID: return

        voucher_code = update.message.text.strip().upper()
        
        if voucher_code.lower() == '/cancel_revoke':
            del context.user_data['waiting_for_revoke_code']
            await start(update, context) # Return to start menu
            return

        db_manager.revoke_voucher(voucher_code)
        
        await update.message.reply_text(
            f"✅ **Voucher Revoked!**\nCode `{voucher_code}` has been marked as used and can no longer be activated.",
            parse_mode='Markdown'
        )
        del context.user_data['waiting_for_revoke_code']
        # Note: The admin can return to the admin panel via callback/start

# --- PREDICTION & PROFIT HANDLERS ---

async def instant_prediction(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sends a one-time prediction for the user's selected asset."""
    query = update.callback_query
    await query.answer("Fetching powerful signal... 🧠")
    
    user_id = query.from_user.id
    user_data = db_manager.get_user(user_id)
    
    if not user_data or not user_data['selected_asset']:
        await query.edit_message_text(
            "❌ **Error:** You must first select a trading asset using the 'Set Trading Asset' button.",
            parse_mode='Markdown'
        )
        return
        
    asset = user_data['selected_asset']
    
    # Fetch prediction
    await query.edit_message_text(f"⏳ Analyzing *{asset}* data with Gemini Engine...")
    recommendation, justification = await FinancialPredictor.get_prediction(asset)
    
    # Format message
    signal_icon = "🟢 BUY" if recommendation == "BUY" else "🔴 SELL" if recommendation == "SELL" else "🟡 HOLD"
    
    message = (
        f"**📊 Quantum Signal Report for {asset}**\n\n"
        f"--- **MASTER QUANT ANALYST PREDICTION** ---\n"
        f"**Signal:** {signal_icon}\n\n"
        f"**Justification:**\n"
        f"_{justification}_\n\n"
        f"--- **NEXT UPDATE IN 30 MINUTES** ---"
    )
    
    # Send the final prediction message
    await context.bot.send_message(
        chat_id=user_id, 
        text=message, 
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Main Menu", callback_data='start_menu')]])
    )

async def show_profit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Displays the user's current profit/loss performance."""
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    user_data = db_manager.get_user(user_id)
    
    if not user_data:
        await query.edit_message_text("User data not found.")
        return

    balance = user_data['initial_balance'] + user_data['current_profit']
    profit = user_data['current_profit']
    
    # Simulate a very simplistic profit update for demonstration
    # In a real bot, profit would be updated based on executed trades
    if user_id != ADMIN_TELEGRAM_ID:
        # Simulate a small, random profit/loss for non-admin users
        import random
        change = random.uniform(-5.0, 15.0) # Random change between -$5 and +$15
        new_profit = round(profit + change, 2)
        db_manager.update_user_profit(user_id, new_profit)
        profit = new_profit
        balance = user_data['initial_balance'] + new_profit

    profit_color = "🟢" if profit >= 0 else "🔴"
    
    profit_message = (
        f"**📊 Your Performance Report (User ID: {user_id})**\n\n"
        f"**Current Trading Asset:** *{user_data.get('selected_asset', 'Not Set')}*\n\n"
        f"**Initial Balance:** `$1000.00`\n"
        f"**Current Profit/Loss:** {profit_color} `\$ {profit:.2f}`\n"
        f"**Total Balance:** `\$ {balance:.2f}`\n\n"
        f"*(Note: Profit is currently simulated. In a live system, this tracks real-time trade results.)*"
    )
    
    await query.edit_message_text(
        profit_message, 
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Main Menu", callback_data='start_menu')]]),
        parse_mode='Markdown'
    )


# --- SCHEDULER LOGIC ---

async def start_periodic_updates(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sets up or replaces the 30-minute recurring job for a single user."""
    user_id = update.effective_user.id
    job_name = f'signal_job_{user_id}'
    
    # Remove existing job if it exists
    current_jobs = context.application.job_queue.get_jobs_by_name(job_name)
    for job in current_jobs:
        job.schedule_removal()
    
    # Add the new repeating job: every 30 minutes (1800 seconds)
    context.application.job_queue.run_repeating(
        send_periodic_update,
        interval=1800, # 30 minutes
        first=5, # Start 5 seconds after call
        name=job_name,
        data={'user_id': user_id}
    )
    logger.info(f"Periodic job started/restarted for user {user_id}")

async def send_periodic_update(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Job function called by JobQueue every 30 minutes. 
    It iterates over all subscribed users and sends them an update.
    If called via start_periodic_updates, context.job will be available.
    If called via admin_force_updates, it iterates over all subscribed users.
    """
    
    subscribed_users = db_manager.get_all_subscribed_users()

    for user in subscribed_users:
        user_id = user['user_id']
        asset = user['selected_asset']
        expiry = user['subscription_expires']
        is_admin = user_id == ADMIN_TELEGRAM_ID
        
        # Check for expired subscription
        if not is_admin and datetime.strptime(expiry, '%Y-%m-%d %H:%M:%S') < datetime.now():
            db_manager.update_user_subscription(user_id, 0) # Mark as unsubscribed
            await context.bot.send_message(
                chat_id=user_id,
                text="❌ **Subscription Expired!**\n\nYour access to the 30-minute signals has ended. Please renew your subscription via the main menu.",
                parse_mode='Markdown'
            )
            # Remove the job queue for this specific user
            job_name = f'signal_job_{user_id}'
            current_jobs = context.application.job_queue.get_jobs_by_name(job_name)
            for job in current_jobs:
                job.schedule_removal()
            continue # Skip prediction for expired user

        # Skip if user has no asset selected
        if not asset:
            await context.bot.send_message(
                chat_id=user_id,
                text="⚠️ **Warning:** Your periodic update was skipped because you have not selected a trading asset. Please use the 'Set Trading Asset' button in the main menu.",
                parse_mode='Markdown'
            )
            continue
            
        try:
            # Fetch prediction
            recommendation, justification = await FinancialPredictor.get_prediction(asset)

            # Format message
            signal_icon = "🟢 BUY" if recommendation == "BUY" else "🔴 SELL" if recommendation == "SELL" else "🟡 HOLD"
            
            message = (
                f"**🔔 30-Minute Quantum Signal 🔔**\n\n"
                f"**Asset:** *{asset}*\n"
                f"--- **MASTER QUANT ANALYST PREDICTION** ---\n"
                f"**Signal:** {signal_icon}\n\n"
                f"**Justification:**\n"
                f"_{justification}_\n\n"
                f"--- **NEXT UPDATE IN 30 MINUTES** ---"
            )
            
            await context.bot.send_message(
                chat_id=user_id, 
                text=message, 
                parse_mode='Markdown'
            )
            logger.info(f"Sent periodic update to user {user_id} for {asset}")

        except Exception as e:
            logger.error(f"Error sending periodic update to user {user_id}: {e}")
            await context.bot.send_message(
                chat_id=user_id,
                text="❌ **Prediction Engine Error:** Could not generate a signal at this time. Please check the network connection.",
                parse_mode='Markdown'
            )

# --- UTILITY HANDLERS ---

async def start_menu_from_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A wrapper to return to the start menu from a callback query."""
    query = update.callback_query
    await query.answer()

    user = query.from_user
    user_data = db_manager.get_user(user.id)
    is_subscribed = user_data['is_subscribed'] if user_data else False
    
    message = f"🌟 Welcome back, *{user.first_name}*!"
    if user.id == ADMIN_TELEGRAM_ID:
        message += "\n\n**👑 ADMIN MODE**"
    elif is_subscribed:
        message += f"\n\n✅ **Active.** Expires: `{user_data['subscription_expires']}`. Asset: *{user_data['selected_asset']}*"
    else:
        message += "\n\n❌ **Access Required.**"

    keyboard = [
        [InlineKeyboardButton("📈 Get Instant Prediction", callback_data='predict_now')],
        [InlineKeyboardButton("⚙️ Set Trading Asset", callback_data='set_asset')],
        [InlineKeyboardButton("📊 My Performance & Profit", callback_data='show_profit')],
        [InlineKeyboardButton("💳 Buy Access / Use Voucher", callback_data='buy_access')],
    ]
    if user.id == ADMIN_TELEGRAM_ID:
        keyboard.append([InlineKeyboardButton("🛠️ Admin Panel", callback_data='admin_panel')])
        
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        message, 
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )


def main() -> None:
    """Start the bot."""
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    
    # --- HANDLERS ---
    
    # Main entry point and menu
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("cancel_voucher", start))
    application.add_handler(CommandHandler("cancel_revoke", start))

    # General message handler for receiving voucher code or revoke code
    # Must be placed before other simple message handlers
    from telegram.ext import MessageHandler, filters
    application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_voucher_input))
    application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_revoke_input))


    # Callback Query Handlers (Buttons)
    application.add_handler(CallbackQueryHandler(start_menu_from_callback, pattern='^start_menu$'))
    application.add_handler(CallbackQueryHandler(admin_panel, pattern='^admin_panel$'))
    application.add_handler(CallbackQueryHandler(buy_access_menu, pattern='^buy_access$'))
    application.add_handler(CallbackQueryHandler(prompt_voucher, pattern='^prompt_voucher$'))
    application.add_handler(CallbackQueryHandler(set_asset_menu, pattern='^set_asset$'))
    application.add_handler(CallbackQueryHandler(instant_prediction, pattern='^predict_now$'))
    application.add_handler(CallbackQueryHandler(show_profit, pattern='^show_profit$'))

    # Asset Selection Handlers
    application.add_handler(CallbackQueryHandler(select_asset_callback, pattern='^select_asset_'))

    # Admin Voucher Handlers
    application.add_handler(CallbackQueryHandler(generate_voucher_callback, pattern='^admin_gen_voucher_'))
    application.add_handler(CallbackQueryHandler(admin_force_updates, pattern='^admin_force_updates$'))
    application.add_handler(CallbackQueryHandler(admin_revoke_prompt, pattern='^admin_revoke_prompt$'))

    # --- INITIAL JOB SETUP (Start admin job and existing user jobs) ---
    
    # The admin (user ID 994618750) gets unlimited access immediately
    db_manager.add_or_update_user(ADMIN_TELEGRAM_ID, "AdminUser")
    db_manager.update_user_subscription(ADMIN_TELEGRAM_ID, 36500, is_admin=True)
    db_manager.set_user_asset(ADMIN_TELEGRAM_ID, "TSLA") # Set a default asset for admin

    # Re-schedule jobs for all active subscribed users on startup
    for user in db_manager.get_all_subscribed_users():
        user_id = user['user_id']
        job_name = f'signal_job_{user_id}'
        
        # Check if the subscription is still valid (future check in get_all_subscribed_users)
        if user['is_subscribed'] and user['selected_asset']:
            application.job_queue.run_repeating(
                send_periodic_update,
                interval=1800, # 30 minutes
                first=10, # Give it time to start up
                name=job_name,
                data={'user_id': user_id}
            )
            logger.info(f"Re-scheduled job for existing user {user_id}")

    # Start the Bot
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    # Initial setup for any required folders/files
    # SQLite file creation is handled by DBManager
    logger.info("Starting bot...")
    main()
    logger.info("Bot shutting down.")
