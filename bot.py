import asyncio
import logging
import aiosqlite
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, Message
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError

# ==================== CONFIGURATION ====================
BOT_TOKEN = "YOUR_BOT_TOKEN_HERE"
API_ID = 12345678  # Replace with your API ID from my.telegram.org
API_HASH = "YOUR_API_HASH_HERE"

# Default Sudo/Admin Users List (Will also be loaded from DB dynamically)
DEFAULT_SUDO_USERS = [123456789]

# Log Channel ID (Proof & Purchase Logs)
LOG_CHANNEL_ID = -1001234567890

# UPI QR & Payment Info
UPI_ID_TEXT = "yourupi@bank"

DB_NAME = "shop_bot.db"
logging.basicConfig(level=logging.INFO)

# Global in-memory cache for Sudo Users
SUDO_USERS = set(DEFAULT_SUDO_USERS)

# ==================== DATABASE INITIALIZATION ====================
async def init_db():
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
        
        # Populate initial sudo users into DB
        for sudo_id in DEFAULT_SUDO_USERS:
            await db.execute("INSERT OR IGNORE INTO sudo_users (user_id) VALUES (?)", (sudo_id,))
        await db.commit()

        # Load all sudo users from DB into set
        async with db.execute("SELECT user_id FROM sudo_users") as cursor:
            rows = await cursor.fetchall()
            for r in rows:
                SUDO_USERS.add(r[0])

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

async def log_to_channel(text: str):
    if LOG_CHANNEL_ID:
        try:
            await app.send_message(LOG_CHANNEL_ID, text)
        except Exception as e:
            logging.error(f"Log Channel Error: {e}")

# ==================== OTP LISTENER ENGINE ====================
async def listen_for_otp(user_id: int, phone_number: str, session_string: str, two_fa: str):
    try:
        client = TelegramClient(StringSession(session_string), API_ID, API_HASH)
        await client.connect()

        if not await client.is_user_authorized():
            await app.send_message(user_id, f"⚠️ **Account Session Expired:** `{phone_number}`")
            return

        await app.send_message(
            user_id,
            f"⚡ **OTP Live Monitoring Started!**\n\n"
            f"📞 **Phone:** `{phone_number}`\n"
            f"🔑 **2FA Password:** `{two_fa}`\n\n"
            f"_Please enter this phone number in your Telegram App. The OTP will be sent here as soon as it arrives._"
        )

        for _ in range(60):  # 5 min monitoring window
            await asyncio.sleep(5)
            async for message in client.iter_messages(777000, limit=1):
                if message and message.text:
                    await app.send_message(
                        user_id,
                        f"📲 **NEW LOGIN OTP RECEIVED!**\n\n"
                        f"**Phone:** `{phone_number}`\n"
                        f"**OTP Details:**\n`{message.text}`\n\n"
                        f"**2FA Password:** `{two_fa}`"
                    )
                    await client.disconnect()
                    return

        await client.disconnect()
        await app.send_message(user_id, f"⌛ **OTP Session Expired** for `{phone_number}`.")
    except Exception as e:
        logging.error(f"OTP Listener Error: {e}")

# ==================== MAIN MENUS (USER & ADMIN) ====================

def get_main_menu_keyboard(user_id: int):
    buttons = [
        [InlineKeyboardButton("🛒 Buy Accounts", callback_data="user_buy_menu"), InlineKeyboardButton("💳 Deposit Money", callback_data="user_deposit_menu")],
        [InlineKeyboardButton("👤 Profile", callback_data="user_profile"), InlineKeyboardButton("👨‍💻 Support", url="https://t.me/your_admin_username")]
    ]
    if user_id in SUDO_USERS:
        buttons.append([InlineKeyboardButton("⚙️ Admin Dashboard", callback_data="admin_panel")])
    return InlineKeyboardMarkup(buttons)

def get_admin_panel_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add Account Stock", callback_data="admin_add_acc"), InlineKeyboardButton("🏷️ Change Stock Price", callback_data="admin_change_price")],
        [InlineKeyboardButton("✏️ Edit User Balance", callback_data="admin_edit_bal"), InlineKeyboardButton("👥 Manage Admins", callback_data="admin_manage_sudo")],
        [InlineKeyboardButton("🚫 Ban User", callback_data="admin_ban_user"), InlineKeyboardButton("🟢 Unban User", callback_data="admin_unban_user")],
        [InlineKeyboardButton("🔙 Exit Admin Panel", callback_data="user_main_menu")]
    ])

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
        "1. **Buying Accounts:** Click on **🛒 Buy Accounts**, select a package, and click purchase. "
        "The bot will automatically display the phone number, 2FA key, and stream the OTP directly in this chat.\n\n"
        "2. **Depositing Funds:** Click on **💳 Deposit Money**, send the payment via UPI/QR code, "
        "upload the payment screenshot, and input the payment amount for verification.\n\n"
        "3. **Support:** Use the **👨‍💻 Support** button in the main menu to reach out to an admin."
    )
    await message.reply_text(text)

@app.on_message(filters.command("admin") & filters.private)
async def admin_command_handler(client: Client, message: Message):
    user_id = message.from_user.id
    if user_id not in SUDO_USERS:
        await message.reply_text("🚫 **Unauthorized.** This command is restricted to admins.")
        return
    await message.reply_text("⚙️ **Welcome to the Admin Panel**", reply_markup=get_admin_panel_keyboard())

# ==================== CALLBACK ROUTER ====================
@app.on_callback_query()
async def callback_router(client: Client, query: CallbackQuery):
    user_id = query.from_user.id
    data = query.data

    if await is_banned(user_id):
        await query.answer("🚫 You are banned!", show_alert=True)
        return

    # MAIN MENU & PROFILE
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

    # BUY ACCOUNTS - DYNAMIC GROUPED STOCK BUTTONS
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

    # ATOMIC PURCHASE LOGIC
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

            # Deduct Balance & Mark Account SOLD
            await db.execute("UPDATE users SET balance = balance - ? WHERE user_id = ?", (price, user_id))
            await db.execute("UPDATE accounts SET status = 'SOLD', sold_to = ? WHERE id = ?", (user_id, acc_id))
            
            # Apply Cashback only if DISCOUNT IS ENABLED
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
            msg += f"🎁 **Cashback Added:** +₹{cashback_credited:.2f} credited to your wallet!\n"

        await query.message.edit_text(msg)

        # Log Channel Entry
        await log_to_channel(
            f"🛍️ **NEW PURCHASE PROOF**\n\n"
            f"👤 **User ID:** `{user_id}`\n"
            f"🌍 **Account:** {country} ({year})\n"
            f"💵 **Price:** ₹{price}\n"
            f"🎁 **Cashback Given:** ₹{cashback_credited}"
        )

        # Start Live OTP Listener Background Process
        asyncio.create_task(listen_for_otp(user_id, phone, session_str, two_fa))

    # MANUAL DEPOSIT SYSTEM
    elif data == "user_deposit_menu":
        user_states[user_id] = "WAIT_DEPOSIT_PHOTO"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="user_main_menu")]])
        
        await query.message.edit_text(
            f"💳 **Manual Deposit Menu**\n\n"
            f"1. Pay via UPI: `{UPI_ID_TEXT}`\n"
            f"2. Pay using QR Code.\n\n"
            f"📸 **Please send a screenshot of your payment to this chat:**",
            reply_markup=kb
        )

    # ==================== ADMIN PANEL CALLBACKS ====================
    elif data == "admin_panel":
        if user_id not in SUDO_USERS: return
        await query.message.edit_text("⚙️ **Admin Dashboard**", reply_markup=get_admin_panel_keyboard())

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
        await query.message.edit_text("👤 **Enter Target User ID:**")

    elif data == "admin_manage_sudo":
        if user_id not in SUDO_USERS: return
        admin_list_text = "\n".join([f"• `{sudo}`" for sudo in SUDO_USERS])
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Add New Admin", callback_data="admin_add_sudo_step")],
            [InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]
        ])
        await query.message.edit_text(
            f"👥 **Current Sudo Admin List:**\n\n{admin_list_text}\n\nClick below to add a new admin.",
            reply_markup=kb
        )

    elif data == "admin_add_sudo_step":
        if user_id not in SUDO_USERS: return
        user_states[user_id] = "ADM_STEP_ADD_SUDO"
        await query.message.edit_text("👤 **Enter the User ID to grant Admin privileges:**")

    elif data == "admin_ban_user":
        if user_id not in SUDO_USERS: return
        user_states[user_id] = "ADM_STEP_BAN_USER"
        await query.message.edit_text("🚫 **Enter User ID to Ban:**")

    elif data == "admin_unban_user":
        if user_id not in SUDO_USERS: return
        user_states[user_id] = "ADM_STEP_UNBAN_USER"
        await query.message.edit_text("🟢 **Enter User ID to Unban:**")

    # DISCOUNT YES / NO BUTTON SELECTION FOR ADMIN
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

    # ADMIN APPROVE / REJECT DEPOSIT
    elif data.startswith("adm_app_dep_"):
        parts = data.split("_")
        dep_user_id = int(parts[3])
        amount = float(parts[4])

        await update_balance(dep_user_id, amount)
        await query.message.edit_caption(caption=query.message.caption + f"\n\n✅ **APPROVED (+₹{amount})**")
        await app.send_message(dep_user_id, f"🎉 **Deposit Approved!** ₹{amount:.2f} credited to your wallet.")
        
        await log_to_channel(f"💳 **NEW DEPOSIT APPROVED**\n\n👤 **User ID:** `{dep_user_id}`\n💰 **Amount Credited:** ₹{amount:.2f}")

    elif data.startswith("adm_rej_dep_"):
        dep_user_id = int(data.split("_")[3])
        await query.message.edit_caption(caption=query.message.caption + "\n\n❌ **REJECTED**")
        await app.send_message(dep_user_id, "❌ Your deposit request was rejected by Admin.")

# ==================== PHOTO RECEIVER (DEPOSIT) ====================
@app.on_message(filters.photo & filters.private)
async def photo_receiver(client: Client, message: Message):
    user_id = message.from_user.id
    if user_states.get(user_id) == "WAIT_DEPOSIT_PHOTO":
        user_states[user_id] = "WAIT_DEPOSIT_AMOUNT"
        temp_data[user_id] = {"photo_id": message.photo.file_id}
        await message.reply_text("🔢 **Now enter the exact payment amount (₹):**")

# ==================== STEP-BY-STEP INPUT ROUTER ====================
@app.on_message(filters.text & filters.private & ~filters.command(["start", "help", "admin"]))
async def text_router(client: Client, message: Message):
    user_id = message.from_user.id
    state = user_states.get(user_id)

    if not state:
        return

    # USER DEPOSIT SUBMISSION
    if state == "WAIT_DEPOSIT_AMOUNT":
        try:
            amount = float(message.text.strip())
            photo_id = temp_data[user_id]["photo_id"]
            user_states.pop(user_id, None)

            await message.reply_text("⏳ **Deposit proof submitted to Admin for verification!**")
            
            kb = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ Approve", callback_data=f"adm_app_dep_{user_id}_{amount}"),
                    InlineKeyboardButton("❌ Reject", callback_data=f"adm_rej_dep_{user_id}")
                ]
            ])
            for sudo_id in SUDO_USERS:
                await app.send_photo(sudo_id, photo=photo_id, caption=f"📥 **NEW DEPOSIT REQUEST**\n\n👤 User ID: `{user_id}`\n💵 Amount: ₹{amount}", reply_markup=kb)
        except ValueError:
            await message.reply_text("❌ Please enter a valid numerical amount (e.g. 100):")

    # ADMIN: UPDATE STOCK PRICE
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
                reply_markup=get_admin_panel_keyboard()
            )
        except ValueError:
            await message.reply_text("❌ Please enter numbers only:")

    # ADMIN WIZARD: ADD ACCOUNT
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
            await t_client.sign_in(data["phone"], otp)
            session_str = t_client.session.save()
            await t_client.disconnect()

            async with aiosqlite.connect(DB_NAME) as db:
                await db.execute("""
                    INSERT INTO accounts (country, year, price, has_discount, cashback, phone_number, session_string, two_fa)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (data["country"], data["year"], data["price"], data["has_discount"], data["cashback"], data["phone"], session_str, data["two_fa"]))
                await db.commit()

            user_states.pop(user_id, None)
            await message.reply_text(f"✅ **Account Added to Stock!**\n\n🌍 {data['country']} ({data['year']})\n📞 `{data['phone']}`", reply_markup=get_admin_panel_keyboard())
        except SessionPasswordNeededError:
            await t_client.sign_in(password=data["two_fa"])
            session_str = t_client.session.save()
            await t_client.disconnect()

            async with aiosqlite.connect(DB_NAME) as db:
                await db.execute("""
                    INSERT INTO accounts (country, year, price, has_discount, cashback, phone_number, session_string, two_fa)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (data["country"], data["year"], data["price"], data["has_discount"], data["cashback"], data["phone"], session_str, data["two_fa"]))
                await db.commit()

            user_states.pop(user_id, None)
            await message.reply_text(f"✅ **Account Added with 2FA Session!**", reply_markup=get_admin_panel_keyboard())
        except Exception as e:
            await message.reply_text(f"❌ Error during sign in: {e}")

    # ADMIN: EDIT USER BALANCE DIRECTLY
    elif state == "ADM_STEP_BAL_USER":
        try:
            temp_data[user_id] = {"target_user": int(message.text.strip())}
            user_states[user_id] = "ADM_STEP_BAL_NEW_VAL"
            await message.reply_text("💵 Enter the new **Exact Balance (₹)**:")
        except ValueError:
            await message.reply_text("❌ Please enter a valid User ID (numbers only):")

    elif state == "ADM_STEP_BAL_NEW_VAL":
        try:
            new_bal = float(message.text.strip())
            target_user = temp_data[user_id]["target_user"]
            await set_user_balance(target_user, new_bal)
            user_states.pop(user_id, None)
            await message.reply_text(f"✅ User `{target_user}` balance updated to **₹{new_bal:.2f}**.", reply_markup=get_admin_panel_keyboard())
            await app.send_message(target_user, f"🔔 **Your Wallet Balance has been updated to: ₹{new_bal:.2f}**")
        except ValueError:
            await message.reply_text("❌ Please enter numbers only:")

    # ADMIN: MANAGE SUDO USERS
    elif state == "ADM_STEP_ADD_SUDO":
        try:
            new_sudo_id = int(message.text.strip())
            await add_sudo_user(new_sudo_id)
            user_states.pop(user_id, None)
            await message.reply_text(f"✅ User `{new_sudo_id}` has been granted Admin privileges.", reply_markup=get_admin_panel_keyboard())
            await app.send_message(new_sudo_id, "🎉 **You have been granted Admin privileges in this bot!** Use /admin to open panel.")
        except ValueError:
            await message.reply_text("❌ Please enter a valid User ID (numbers only):")

    # ADMIN: BAN & UNBAN WIZARD
    elif state == "ADM_STEP_BAN_USER":
        try:
            target_user = int(message.text.strip())
            async with aiosqlite.connect(DB_NAME) as db:
                await db.execute("UPDATE users SET is_banned = 1 WHERE user_id = ?", (target_user,))
                await db.commit()
            user_states.pop(user_id, None)
            await message.reply_text(f"🚫 User `{target_user}` is now Banned.", reply_markup=get_admin_panel_keyboard())
        except ValueError:
            await message.reply_text("❌ Please enter a valid User ID:")

    elif state == "ADM_STEP_UNBAN_USER":
        try:
            target_user = int(message.text.strip())
            async with aiosqlite.connect(DB_NAME) as db:
                await db.execute("UPDATE users SET is_banned = 0 WHERE user_id = ?", (target_user,))
                await db.commit()
            user_states.pop(user_id, None)
            await message.reply_text(f"🟢 User `{target_user}` is now Unbanned.", reply_markup=get_admin_panel_keyboard())
        except ValueError:
            await message.reply_text("❌ Please enter a valid User ID:")

# ==================== START SERVER ====================
if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(init_db())
    print("🚀 All-in-One Button Shop Bot Activated!")
    app.run()
