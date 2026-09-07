import asyncio
import io
import os
import re
import time
import imaplib
import logging
import email as email_module
import qrcode
import requests
from datetime import datetime
from dotenv import load_dotenv

from pyrogram import Client, filters, idle
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton as _BaseIBtn, CallbackQuery, Message
from pyrogram.enums import ButtonStyle, ChatMemberStatus
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError, FreshResetAuthorisationForbiddenError
from telethon.tl.functions.account import GetAuthorizationsRequest, ResetAuthorizationRequest
from motor.motor_asyncio import AsyncIOMotorClient
from bson.objectid import ObjectId
import threading
from pymongo import MongoClient


def InlineKeyboardButton(text, style=None, **kwargs):
    """Colored-button wrapper (kurigram ButtonStyle). Auto-picks color from label text."""
    if style is None:
        danger = ("❌", "🚫", "🗑", "delete", "terminate", "logout", "reject", "ban",
                  "close", "remove", "cancel", "stop", "finish")
        success = ("✅", "🟢", "✨", "🛒", "buy", "deposit", "approve", "confirm",
                   "verify", "add", "claim", "unban", "check payment", "send money", "+")
        t = text.lower()
        style = ButtonStyle.DANGER if any(k in t for k in danger) else (
            ButtonStyle.SUCCESS if any(k in t for k in success) else ButtonStyle.PRIMARY)
    return _BaseIBtn(text, style=style, **kwargs)


# ==================== ENVIRONMENT CONFIGURATION ====================
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))

LOG_CHANNEL_ID = os.getenv("LOG_CHANNEL_ID", "")

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
UPI_ID_TEXT = os.getenv("UPI_ID_TEXT", "yourupi@bank")
BINANCE_ID = os.getenv("BINANCE_ID", "0xYourBinanceUSDTAddressHere")
PAYEE_NAME = os.getenv("PAYEE_NAME", "Account Store")
PAYMENT_API_KEY = os.getenv("PAYMENT_API_KEY", "")

# --- Email watcher (FamApp/FamX receipt auto-verification) ---
IMAP_SERVER = os.getenv("IMAP_SERVER", "imap.gmail.com")
IMAP_EMAIL = os.getenv("IMAP_EMAIL", "")
IMAP_PASSWORD = os.getenv("IMAP_PASSWORD", "")      # Gmail App Password
IMAP_POLL_INTERVAL = int(os.getenv("IMAP_POLL_INTERVAL", "20"))
# Sirf is sender ke emails scan honge (FamApp receipts)
FAMPAY_SENDER = os.getenv("FAMPAY_SENDER", "no-reply@famapp.in").strip().lower()

# --- Force-join gate (required group + channel) ---
REQ_GC_ID = os.getenv("REQ_GC_ID", "")               # proof group (username ya numeric id)
REQ_GC_LINK = os.getenv("REQ_GC_LINK", "")
REQ_GC_NAME = os.getenv("REQ_GC_NAME", "Proof Group")
REQ_CH_ID = os.getenv("REQ_CH_ID", "")               # store/log channel
REQ_CH_LINK = os.getenv("REQ_CH_LINK", "")
REQ_CH_NAME = os.getenv("REQ_CH_NAME", "Store Channel")

MIN_DEPOSIT = 50.0
MIN_WITHDRAW = 50.0

logging.basicConfig(level=logging.INFO)

# ==================== MONGO DB INITIALIZATION ====================
mongo_client = AsyncIOMotorClient(MONGO_URI)
db = mongo_client["swastik_shop_db"]


users_col = db["users"]
accounts_col = db["accounts"]
sudo_col = db["sudo_users"]
requests_col = db["requests"]
settings_col = db["settings"]
payments_col = db["payments"]
fampay_credits_col = db["fampay_credits"]   # email watcher yahan {utr, amount} daalta hai

SUDO_USERS = set()

# Flag Mapping Helper
FLAG_MAP = {
    "india": "🇮🇳", "in": "🇮🇳",
    "bangladesh": "🇧🇩", "bd": "🇧🇩",
    "indonesia": "🇮🇩", "id": "🇮🇩",
    "usa": "🇺🇸", "us": "🇺🇸", "united states": "🇺🇸",
    "uk": "🇬🇧", "united kingdom": "🇬🇧",
    "russia": "🇷🇺", "ru": "🇷🇺",
    "canada": "🇨🇦", "ca": "🇨🇦",
    "brazil": "🇧🇷", "br": "🇧🇷",
    "germany": "🇩🇪", "de": "🇩🇪",
    "vietnam": "🇻🇳", "vn": "🇻🇳",
    "philippines": "🇵🇭", "ph": "🇵🇭",
    "pakistan": "🇵🇰", "pk": "🇵🇰",
    "thailand": "🇹🇭", "th": "🇹🇭"
}

def get_flag(country_name: str) -> str:
    """Dynamically fetches flag emoji from country name or calculates ISO regional indicator."""
    c_clean = country_name.strip().lower()
    if c_clean in FLAG_MAP:
        return FLAG_MAP[c_clean]

    if len(c_clean) == 2 and c_clean.isalpha():
        return chr(ord(c_clean[0].upper()) + 127397) + chr(ord(c_clean[1].upper()) + 127397)

    return "🌐"

async def init_db():
    global SUDO_USERS
    await sudo_col.update_one({"user_id": OWNER_ID}, {"$set": {"user_id": OWNER_ID}}, upsert=True)
    sudo_docs = await sudo_col.find().to_list(length=1000)
    SUDO_USERS = {doc["user_id"] for doc in sudo_docs}
    SUDO_USERS.add(OWNER_ID)

    m_doc = await settings_col.find_one({"key": "maintenance"})
    if not m_doc:
        await settings_col.insert_one({
            "key": "maintenance",
            "is_active": False,
            "reason": "Routine system maintenance and performance upgrades in progress."
        })

# ==================== BOT CLIENT SETUP ====================
app = Client("ShopBotGUI", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

user_states = {}
temp_data = {}
admin_deposit_msg_map = {}

# ==================== HELPER DB FUNCTIONS ====================
async def get_maintenance_status() -> tuple[bool, str]:
    m_doc = await settings_col.find_one({"key": "maintenance"})
    if not m_doc:
        return False, "Routine system maintenance and performance upgrades in progress."
    return m_doc.get("is_active", False), m_doc.get("reason", "Routine system maintenance and performance upgrades in progress.")

async def set_maintenance_status(is_active: bool, reason: str = None):
    update_data = {"is_active": is_active}
    if reason is not None:
        update_data["reason"] = reason
    await settings_col.update_one({"key": "maintenance"}, {"$set": update_data}, upsert=True)

def mask_phone_number(phone: str) -> str:
    if len(phone) > 5:
        return phone[:5] + "X" * (len(phone) - 5)
    return phone

async def get_user_data(user_id: int):
    user = await users_col.find_one({"user_id": user_id})
    if not user:
        user = {
            "user_id": user_id,
            "balance": 0.0,
            "profile_cashback": 0.0,
            "withdraw_cashback": 0.0,
            "is_banned": False,
            "ban_reason": ""
        }
        await users_col.insert_one(user)
    return user

async def get_user_balance(user_id: int) -> float:
    user = await get_user_data(user_id)
    return user.get("balance", 0.0)

async def get_withdraw_cashback(user_id: int) -> float:
    user = await get_user_data(user_id)
    return user.get("withdraw_cashback", 0.0)

async def is_banned(user_id: int) -> tuple[bool, str]:
    user = await get_user_data(user_id)
    return user.get("is_banned", False), user.get("ban_reason", "")

async def set_user_balance(user_id: int, new_balance: float):
    await users_col.update_one({"user_id": user_id}, {"$set": {"balance": new_balance}}, upsert=True)

async def update_balance(user_id: int, amount: float):
    await users_col.update_one({"user_id": user_id}, {"$inc": {"balance": amount}}, upsert=True)

async def update_profile_cashback(user_id: int, amount: float):
    await users_col.update_one({"user_id": user_id}, {"$inc": {"profile_cashback": amount}}, upsert=True)

async def update_withdraw_cashback(user_id: int, amount: float):
    await users_col.update_one({"user_id": user_id}, {"$inc": {"withdraw_cashback": amount}}, upsert=True)

async def add_sudo_user(user_id: int):
    SUDO_USERS.add(user_id)
    await sudo_col.update_one({"user_id": user_id}, {"$set": {"user_id": user_id}}, upsert=True)

async def remove_sudo_user(user_id: int):
    if user_id == OWNER_ID:
        return
    SUDO_USERS.discard(user_id)
    await sudo_col.delete_one({"user_id": user_id})

async def log_to_channel(text: str, reply_markup=None):
    if LOG_CHANNEL_ID:
        try:
            target_chat = int(LOG_CHANNEL_ID) if LOG_CHANNEL_ID.lstrip('-').isdigit() else LOG_CHANNEL_ID
            await app.send_message(target_chat, text, reply_markup=reply_markup)
        except Exception as e:
            logging.error(f"Log Channel Error: {e}")

def get_buy_now_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🛒 Buy Now", url="https://t.me/swastiktgs_bot")]
    ])

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

def get_account_options_keyboard(acc_id: str):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Re-fetch OTP", callback_data=f"refetch_otp_{acc_id}")],
        [InlineKeyboardButton("📱 Manage Devices", callback_data=f"manage_devs_{acc_id}")],
        [InlineKeyboardButton("🚪 Finish & Logout Bot", callback_data=f"logout_bot_{acc_id}")],
        [InlineKeyboardButton("🔙 Main Menu", callback_data="user_main_menu")]
    ])

# ==================== FORCE-JOIN CHANNEL GATE ====================
async def _resolve_chat_id(chat_ref: str):
    ref = str(chat_ref).strip()
    if not ref:
        return None
    try:
        return int(ref)
    except ValueError:
        try:
            c = await app.get_chat(ref)
            return c.id
        except Exception as e:
            logging.error(f"resolve chat {ref}: {e}")
            return None

async def _user_in_chat(user_id: int, chat_id) -> bool:
    if not chat_id:
        return True
    try:
        m = await app.get_chat_member(chat_id, user_id)
        return m.status in (ChatMemberStatus.MEMBER,
                            ChatMemberStatus.ADMINISTRATOR,
                            ChatMemberStatus.OWNER)
    except Exception:
        return False

def get_required_channels():
    out = []
    if REQ_GC_ID and REQ_GC_LINK:
        out.append({"ref": REQ_GC_ID, "link": REQ_GC_LINK, "name": REQ_GC_NAME, "icon": "👥"})
    if REQ_CH_ID and REQ_CH_LINK:
        out.append({"ref": REQ_CH_ID, "link": REQ_CH_LINK, "name": REQ_CH_NAME, "icon": "📢"})
    return out

async def not_joined_channels(user_id: int):
    missing = []
    for ch in get_required_channels():
        cid = await _resolve_chat_id(ch["ref"])
        if not cid:
            continue
        ok = await _user_in_chat(user_id, cid)
        if not ok:
            missing.append(ch)
    return missing

async def _send_join_gate(chat_id, missing):
    btns = []
    for ch in missing:
        btns.append([InlineKeyboardButton(f"{ch['icon']} Join {ch['name']}", url=ch["link"])])
    btns.append([InlineKeyboardButton("✅ Check Membership", callback_data="check_join")])
    text = ("🔒 **You must join our channels to use this bot.**\n\n"
            "Join the channels below, then tap **Check Membership** to continue.")
    try:
        await app.send_message(chat_id, text, reply_markup=InlineKeyboardMarkup(btns))
    except Exception as e:
        logging.error(f"join gate error: {e}")

# ==================== EMAIL WATCHER (FAMAPP RECEIPTS → fampay_credits) ====================
def _get_email_body(msg) -> str:
    """Extract plain-text AND html body from an email (FamApp receipts are often HTML)."""
    body = ""
    for part in msg.walk():
        ctype = part.get_content_type()
        if ctype in ("text/plain", "text/html"):
            try:
                chunk = part.get_payload(decode=True).decode(
                    part.get_content_charset() or "utf-8", errors="ignore")
            except Exception:
                chunk = ""
            if chunk:
                body += chunk + "\n"
    return body

def _parse_famapp_receipt(body: str):
    """
    Parse a FamApp/FamX receipt.
    Returns (amount, utr) ONLY for INCOMING money ("successfully received"),
    else (None, None). "paid" emails are ignored.
    """
    received = re.search(r"successfully\s+received", body, re.IGNORECASE)
    if not received:
        return (None, None)          # outgoing 'paid' ya koi aur email -> ignore

    # Sirf received block ka amount (balance ka ₹ andar nahi aayega)
    seg_start = received.end()
    tx = re.search(r"Transaction\s*ID", body, re.IGNORECASE)
    seg = body[seg_start: tx.start() if tx else len(body)]

    m_amt = re.search(r"₹\s*([\d,]+(?:\.\d+)?)", seg)
    amount = float(m_amt.group(1).replace(",", "")) if m_amt else 0.0

    m_utr = re.search(r"Transaction\s*ID\s*:?\s*([A-Za-z0-9]{6,30})", body, re.IGNORECASE)
    utr = m_utr.group(1) if m_utr else ""

    if amount <= 0 or not utr:
        return (None, None)
    return (amount, utr)

# ==================== EMAIL WATCHER (FAMAPP RECEIPTS → fampay_credits) — THREAD BASED ====================
_sync_mongo = MongoClient(MONGO_URI)
_sync_db = _sync_mongo["swastik_shop_db"]   # FIXED: was "swasti_shop_db" (wrong DB name)
_sync_fampay = _sync_db["fampay_credits"]   # same collection, sync access

def _fetch_and_store_fampay_receipts():
    """Sync IMAP poll: sirf FAMPAY_SENDER ke INCOMING receipts store karta hai."""
    if not (IMAP_EMAIL and IMAP_PASSWORD):
        return
    try:
        conn = imaplib.IMAP4_SSL(IMAP_SERVER, 993)
        conn.login(IMAP_EMAIL, IMAP_PASSWORD)
        conn.select("INBOX")

        # Gmail side par hi sender filter -> spam/newsletter scan hi nahi hoga
        if FAMPAY_SENDER:
            typ, data = conn.search(None, f'(UNSEEN FROM "{FAMPAY_SENDER}")')
        else:
            typ, data = conn.search(None, 'UNSEEN')

        if typ == "OK":
            for num in (data[0].split() if data and data[0] else []):
                try:
                    typ, msg_data = conn.fetch(num, '(RFC822)')
                    if typ != "OK":
                        continue
                    msg = email_module.message_from_bytes(msg_data[0][1])

                    # double-safety sender check
                    if FAMPAY_SENDER and FAMPAY_SENDER not in (msg.get("From") or "").lower():
                        continue

                    body = _get_email_body(msg)
                    amount, utr = _parse_famapp_receipt(body)

                    if not amount or not utr:
                        logging.info(f"Skipped (no incoming credit) from {msg.get('From')}")
                        continue    # seen flag mat lagao

                    if not _sync_fampay.find_one({"utr": utr}):
                        _sync_fampay.insert_one({
                            "utr": utr,
                            "amount": amount,
                            "sender": msg.get("From", ""),
                            "received_at": time.time(),
                        })
                        logging.info(f"FamApp credit logged -> UTR={utr} amount=₹{amount}")
                        conn.store(num, '+FLAGS', '\\Seen')   # sirf valid mile tab seen
                except Exception as e:
                    logging.error(f"IMAP parse error on msg: {e}")
        conn.logout()
    except Exception as e:
        logging.error(f"IMAP connection error: {e}")

def _email_watcher_loop():
    while True:
        try:
            _fetch_and_store_fampay_receipts()
        except Exception as e:
            logging.error(f"email watcher error: {e}")
        time.sleep(IMAP_POLL_INTERVAL)

# ==================== AUTO PAYMENT VERIFICATION (EMAIL-BACKED) ====================
async def check_auto_payment_status(txn_id: str, amount: float) -> bool:
    """FamApp email-backed UTR verification. Only a UTR logged by the email watcher is accepted."""
    if not txn_id:
        return False
    rec = await fampay_credits_col.find_one({"utr": txn_id, "used": {"$ne": True}})
    if not rec:
        return False
    if abs(float(rec.get("amount", 0)) - amount) > 0.01:
        return False
    await fampay_credits_col.update_one(
        {"_id": rec["_id"]},
        {"$set": {"used": True, "used_at": time.time(), "credited_user": rec.get("_pending_user")}}
    )
    return True

async def manual_fallback_timeout(user_id: int, pay_id: str, amount: float):
    """If not auto-verified within 5 minutes, give the user a Manual(Admin) approval button."""
    await asyncio.sleep(300)
    pay = await payments_col.find_one({"_id": ObjectId(pay_id)})
    if not pay or pay.get("status") in ("SUCCESS", "MANUAL", "REJECTED"):
        return
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📩 Payment Not Verified? Manual Approval",
                              callback_data=f"auto_to_manual_{pay_id}")]
    ])
    try:
        await app.send_message(
            user_id,
            f"⏳ **AUTO-VERIFICATION TIMED OUT (5 MIN)**\n\n"
            f"If you have already sent ₹{amount:.2f} but it was not verified automatically, "
            f"press the button below and send the payment screenshot.",
            reply_markup=kb,
        )
    except Exception as e:
        logging.error(f"manual_fallback_timeout: {e}")

# ==================== OTP LISTENER ENGINE WITH REFUND ====================
async def fetch_latest_otp(user_id: int, acc_id: str, is_manual: bool = False):
    acc = await accounts_col.find_one({"_id": ObjectId(acc_id)})

    if not acc:
        await app.send_message(user_id, "❌ **Account session record not found!**")
        return

    phone_number = acc["phone_number"]
    session_string = acc["session_string"]
    two_fa = acc["two_fa"]
    category = acc.get("category", "General")
    country = acc.get("country", "Global")
    year = acc.get("year", "N/A")
    price = acc.get("price", 0.0)

    try:
        t_client = TelegramClient(StringSession(session_string), API_ID, API_HASH)
        await t_client.connect()

        if not await t_client.is_user_authorized():
            await update_balance(user_id, price)
            await accounts_col.update_one({"_id": ObjectId(acc_id)}, {"$set": {"status": "EXPIRED"}})
            await app.send_message(
                user_id,
                f"⚠️ **Session is expired! Money refunded in your profile (₹{price:.2f}).**"
            )
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
                f"⚠️ *Note: We are not responsible for any issues after receiving the OTP.*",
                reply_markup=get_account_options_keyboard(acc_id)
            )

            masked_phone = mask_phone_number(phone_number)
            flag = get_flag(country)
            log_text = (
                f"✅ **LOGIN OTP RECEIVED!**\n\n"
                f"👤 **Buyer ID:** `{user_id}`\n"
                f"📁 **Category:** {category}\n"
                f"{flag} **Country & Year:** {country} ({year})\n"
                f"📞 **Phone Number:** `{masked_phone}`\n\n"
                f"📌 **Status:** Login Code Delivered"
            )
            await log_to_channel(log_text, reply_markup=get_buy_now_keyboard())

        elif is_manual:
            await app.send_message(
                user_id,
                f"⌛ **No fresh OTP found yet for** `{phone_number}`. Re-send OTP in app and click again.",
                reply_markup=get_account_options_keyboard(acc_id)
            )

    except Exception as e:
        logging.error(f"OTP Fetch Error: {e}")

async def listen_for_otp(user_id: int, phone_number: str, session_string: str, two_fa: str, acc_id: str, price: float):
    try:
        t_client = TelegramClient(StringSession(session_string), API_ID, API_HASH)
        await t_client.connect()

        if not await t_client.is_user_authorized():
            await update_balance(user_id, price)
            await accounts_col.update_one({"_id": ObjectId(acc_id)}, {"$set": {"status": "EXPIRED"}})
            await app.send_message(user_id, f"⚠️ **Session is expired! Money refunded in your profile (₹{price:.2f}).**")
            return

        buy_time = time.time()
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
        [InlineKeyboardButton("🛒 Buy Telegram Account", callback_data="user_buy_menu")],
        [InlineKeyboardButton("💳 Deposit Money (UPI/Crypto)", callback_data="user_deposit_mode_choice"), InlineKeyboardButton("💸 Withdraw Cashback", callback_data="user_withdraw_menu")],
        [InlineKeyboardButton("👤 Profile", callback_data="user_profile"), InlineKeyboardButton("👨‍💻 Support", url="https://t.me/PROOF_PAYMENTS12")]
    ]
    if user_id in SUDO_USERS:
        buttons.append([InlineKeyboardButton("⚙️ Admin Dashboard", callback_data="admin_panel")])
    return InlineKeyboardMarkup(buttons)

def get_admin_panel_keyboard(user_id: int):
    row_1 = [
        InlineKeyboardButton("➕ Add Account Stock", callback_data="admin_add_acc"),
        InlineKeyboardButton("🗑️ Remove Stock", callback_data="admin_remove_stock")
    ]

    row_2 = []
    if user_id == OWNER_ID:
        row_2.append(InlineKeyboardButton("🏷️ Change Price / Cashback (Owner)", callback_data="admin_change_price"))

    buttons = [row_1]
    if row_2:
        buttons.append(row_2)

    if user_id == OWNER_ID:
        buttons.append([InlineKeyboardButton("✏️ Edit User Balance", callback_data="admin_edit_bal")])

    buttons.append([
        InlineKeyboardButton("📊 Stats & Revenue", callback_data="admin_stats"),
        InlineKeyboardButton("ℹ️ User History & Info", callback_data="admin_user_history")
    ])

    buttons.append([
        InlineKeyboardButton("🛠️ Maintenance Mode", callback_data="admin_maint_panel"),
        InlineKeyboardButton("📢 Broadcast DM", callback_data="admin_broadcast")
    ])

    buttons.append([InlineKeyboardButton("🚫 Ban User", callback_data="admin_ban_user"), InlineKeyboardButton("🟢 Unban User", callback_data="admin_unban_user")])

    if user_id == OWNER_ID:
        buttons.append([InlineKeyboardButton("👥 Manage Admins (Owner Only)", callback_data="admin_manage_sudo")])

    buttons.append([InlineKeyboardButton("🔙 Exit Admin Panel", callback_data="user_main_menu")])
    return InlineKeyboardMarkup(buttons)

async def get_maintenance_panel_keyboard():
    is_active, _ = await get_maintenance_status()
    toggle_text = "🔴 Turn OFF Maintenance" if is_active else "🟢 Turn ON Maintenance"

    buttons = [
        [InlineKeyboardButton(toggle_text, callback_data="adm_toggle_maint")],
        [InlineKeyboardButton("✏️ Change Maintenance Reason", callback_data="adm_change_maint_reason")],
        [InlineKeyboardButton("🔙 Back to Admin Panel", callback_data="admin_panel")]
    ]
    return InlineKeyboardMarkup(buttons)

async def get_manage_sudo_keyboard():
    buttons = [
        [InlineKeyboardButton("➕ Add Admin", callback_data="adm_add_sudo_btn")]
    ]
    sudo_docs = await sudo_col.find({"user_id": {"$ne": OWNER_ID}}).to_list(length=100)
    for doc in sudo_docs:
        s_id = doc["user_id"]
        buttons.append([InlineKeyboardButton(f"❌ Remove {s_id}", callback_data=f"adm_rem_sudo_{s_id}")])

    buttons.append([InlineKeyboardButton("🔙 Back to Admin Panel", callback_data="admin_panel")])
    return InlineKeyboardMarkup(buttons)

# ==================== COMMAND HANDLERS ====================
@app.on_message(filters.command("start") & filters.private)
async def start_handler(client: Client, message: Message):
    user_id = message.from_user.id
    user_states.pop(user_id, None)

    banned, reason = await is_banned(user_id)
    if banned:
        await message.reply_text(f"🚫 **You are banned from using this bot.**\n\n**Reason:** {reason}")
        return

    maint_active, maint_reason = await get_maintenance_status()
    if maint_active and user_id not in SUDO_USERS:
        await message.reply_text(f"🚧 **SYSTEM MAINTENANCE MODE ACTIVE** 🚧\n\n**Message:** {maint_reason}\n\n*All bot operations are temporarily paused. Please try again later.*")
        return

    # Force-join gate (owner/admins exempt)
    if user_id not in SUDO_USERS and user_id != OWNER_ID:
        missing = await not_joined_channels(user_id)
        if missing:
            await _send_join_gate(user_id, missing)
            return

    bal = await get_user_balance(user_id)
    text = f"👋 **Welcome to the Account Store Bot!**\n\n🆔 **User ID:** `{user_id}`\n💰 **Wallet Balance:** ₹{bal:.2f}"
    await message.reply_text(text, reply_markup=get_main_menu_keyboard(user_id))

@app.on_message(filters.command("admin") & filters.private)
async def admin_command_handler(client: Client, message: Message):
    user_id = message.from_user.id
    user_states.pop(user_id, None)
    if user_id not in SUDO_USERS:
        await message.reply_text("🚫 **Unauthorized.** This command is restricted to admins.")
        return
    await message.reply_text("⚙️ **Welcome to the Admin Dashboard**", reply_markup=get_admin_panel_keyboard(user_id))

# ==================== CALLBACK ROUTER ====================
@app.on_callback_query()
async def callback_router(client: Client, query: CallbackQuery):
    user_id = query.from_user.id
    data = query.data

    banned, reason = await is_banned(user_id)
    if banned:
        await query.answer(f"🚫 You are banned! Reason: {reason}", show_alert=True)
        return

    maint_active, maint_reason = await get_maintenance_status()
    if maint_active and user_id not in SUDO_USERS and not data.startswith("admin_"):
        await query.answer(f"🚧 Bot is under maintenance!\nReason: {maint_reason}", show_alert=True)
        return

    if data == "check_join":
        # Owner/admins hamesha bypass
        if user_id in SUDO_USERS or user_id == OWNER_ID:
            missing = []
        else:
            missing = await not_joined_channels(user_id)

        if missing:
            name_list = "\n".join([f"• {ch['icon']} {ch['name']}" for ch in missing])
            btns = []
            for ch in missing:
                btns.append([InlineKeyboardButton(f"{ch['icon']} Join {ch['name']}", url=ch["link"])])
            btns.append([InlineKeyboardButton("✅ Check Membership", callback_data="check_join")])
            try:
                await query.message.edit_text(
                    f"🔒 **You still need to join:**\n{name_list}\n\n"
                    f"Join all channels, then press **Check Membership**.",
                    reply_markup=InlineKeyboardMarkup(btns))
            except Exception:
                pass
            await query.answer("⚠️ Please join all channels first!", show_alert=True)
        else:
            user_states.pop(user_id, None)
            temp_data.pop(user_id, None)
            bal = await get_user_balance(user_id)
            try:
                await query.message.edit_text(
                    f"👋 **Main Menu**\n\n💰 **Balance:** ₹{bal:.2f}",
                    reply_markup=get_main_menu_keyboard(user_id))
            except Exception:
                pass
            await query.answer("✅ Membership Verified!", show_alert=True)

    elif data == "user_main_menu":
        user_states.pop(user_id, None)
        temp_data.pop(user_id, None)
        bal = await get_user_balance(user_id)
        await query.message.edit_text(f"👋 **Main Menu**\n\n💰 **Balance:** ₹{bal:.2f}", reply_markup=get_main_menu_keyboard(user_id))

    elif data == "user_profile":
        u_data = await get_user_data(user_id)
        await query.message.edit_text(
            f"👤 **Your Profile**\n\n"
            f"🆔 **ID:** `{user_id}`\n"
            f"💵 **Wallet Balance:** ₹{u_data.get('balance', 0.0):.2f}\n"
            f"🎁 **Profile Cashback (Locked):** ₹{u_data.get('profile_cashback', 0.0):.2f}\n"
            f"💸 **Withdrawable Cashback:** ₹{u_data.get('withdraw_cashback', 0.0):.2f}\n\n"
            f"ℹ️ _Profile Cashback is locked and can NOT be withdrawn or transferred.\n"
            f"Only Withdrawable Cashback can be withdrawn._",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Main Menu", callback_data="user_main_menu")]])
        )

    # ==================== DEPOSIT MODE CHOICE ====================
    elif data == "user_deposit_mode_choice":
        buttons = [
            [InlineKeyboardButton("⚡ Automatic Payment (UPI / INR)", callback_data="dep_mode_auto")],
            [InlineKeyboardButton("🟡 Crypto Deposit (USDT)", callback_data="dep_mode_crypto")],
            [InlineKeyboardButton("✍️ Manual Deposit (UPI / INR)", callback_data="dep_mode_manual")],
            [InlineKeyboardButton("🔙 Back to Main Menu", callback_data="user_main_menu")]
        ]
        await query.message.edit_text(
            "💳 **DEPOSIT MONEY MENU**\n\n"
            "Please select how you would like to deposit funds:\n\n"
            "• **Automatic Payment (UPI / INR):** Instantly verified after checking payment.\n"
            "• **Crypto Deposit (USDT):** Pay via Binance / USDT Payment Gateways.\n"
            "• **Manual Payment (UPI / INR):** Proof verified by Admin manually.",
            reply_markup=InlineKeyboardMarkup(buttons)
        )

    elif data == "dep_mode_crypto":
        buttons = [
            [InlineKeyboardButton("🔙 Back to Deposit Options", callback_data="user_deposit_mode_choice")]
        ]
        await query.message.edit_text(
            f"🟡 **USDT / CRYPTO DEPOSIT (BINANCE PAY)**\n\n"
            f"Send USDT using **Binance Pay** (internal transfer):\n\n"
            f"📌 **Binance Pay UID:** `{BINANCE_ID}`\n\n"
            f"▫️ Open Binance → **Pay → Send**\n"
            f"▫️ Enter UID **{BINANCE_ID}**\n"
            f"▫️ No network needed (app to app)\n\n"
            f"After sending, send your **TxHash + Amount** to Support for manual credit.",
            reply_markup=InlineKeyboardMarkup(buttons)
        )

    elif data == "dep_mode_manual":
        user_states[user_id] = "WAIT_DEPOSIT_AMOUNT_MANUAL"
        await query.message.edit_text(
            f"✍️ **MANUAL DEPOSIT MONEY (UPI / INR)**\n\n"
            f"⚠️ **Minimum Deposit Amount:** ₹{MIN_DEPOSIT:.2f}\n\n"
            f"🔢 **Enter the amount you wish to deposit (in ₹):**",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Main Menu", callback_data="user_main_menu")]])
        )

    elif data == "dep_mode_auto":
        user_states[user_id] = "WAIT_DEPOSIT_AMOUNT_AUTO"
        await query.message.edit_text(
            f"⚡ **AUTOMATIC DEPOSIT MONEY (UPI / INR)**\n\n"
            f"⚠️ **Minimum Deposit Amount:** ₹{MIN_DEPOSIT:.2f}\n\n"
            f"🔢 **Enter the amount you wish to deposit (in ₹):**",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Main Menu", callback_data="user_main_menu")]])
        )

    elif data.startswith("auto_check_pay_"):
        pay_id = data.split("_")[3]
        pay_doc = await payments_col.find_one({"_id": ObjectId(pay_id)})

        if not pay_doc:
            await query.answer("❌ Payment request expired!", show_alert=True)
            return

        if pay_doc["status"] == "SUCCESS":
            await query.answer("✅ Payment already verified and added!", show_alert=True)
            return

        user_states[user_id] = f"WAIT_AUTO_TXN_ID_{pay_id}"
        await query.message.reply_text(
            "🧾 **Please enter the Transaction ID / UTR Number to check payment:**"
        )

    elif data.startswith("auto_to_manual_"):
        pay_id = data.split("_")[3]
        pay = await payments_col.find_one({"_id": ObjectId(pay_id)})
        if not pay:
            await query.answer("❌ Payment request expired!", show_alert=True)
            return
        if pay.get("status") == "SUCCESS":
            await query.answer("✅ This deposit has already been credited!", show_alert=True)
            return

        await payments_col.update_one({"_id": ObjectId(pay_id)}, {"$set": {"status": "MANUAL"}})
        amount = float(pay.get("amount", 0))
        temp_data[user_id] = {"amount": amount}
        user_states[user_id] = "WAIT_DEPOSIT_PHOTO_MANUAL"

        qr_image = generate_upi_qr(UPI_ID_TEXT, PAYEE_NAME, amount)
        await app.send_photo(
            chat_id=user_id,
            photo=qr_image,
            caption=f"💳 **MANUAL VERIFY (ADMIN)**\n\n"
                    f"📌 UPI: `{UPI_ID_TEXT}`\n"
                    f"💵 Amount: ₹{amount:.2f}\n\n"
                    f"Please send the **payment screenshot** from your UPI app here — "
                    f"our admin will verify it manually and update your wallet.",
        )

    # ==================== COUNTRY & STOCK NAVIGATION FLOW ====================
    elif data == "user_buy_menu":
        pipeline = [
            {"$match": {"status": "AVAILABLE"}},
            {"$group": {"_id": "$country", "count": {"$sum": 1}}}
        ]
        countries = await accounts_col.aggregate(pipeline).to_list(length=100)

        if not countries:
            await query.answer("❌ Currently Out of Stock!", show_alert=True)
            return

        bal = await get_user_balance(user_id)

        buttons = []
        for c in countries:
            c_name = c["_id"]
            count = c["count"]
            flag = get_flag(c_name)

            btn_text = f"{flag} {c_name.capitalize()} ({count})"
            buttons.append([InlineKeyboardButton(btn_text, callback_data=f"sel_cntry_{c_name}")])

        buttons.append([InlineKeyboardButton("Back to Purchase Options", callback_data="user_main_menu")])

        header_text = (
            f"🏪 **Buy Telegram Account**\n\n"
            f"**Click country to view price and stock:**\n"
            f"_____________________________________\n\n"
            f"✅ **Total balance:** ₹{bal:.2f}\n"
            f"✅ **Server:** Server (2)\n"
            f"✅ **Page 1 of 1**"
        )
        await query.message.edit_text(header_text, reply_markup=InlineKeyboardMarkup(buttons))

    elif data.startswith("sel_cntry_"):
        c_name = data.split("_")[2]

        pipeline = [
            {"$match": {"country": c_name, "status": "AVAILABLE"}},
            {"$group": {
                "_id": {
                    "category": "$category",
                    "year": "$year",
                    "price": "$price"
                },
                "count": {"$sum": 1}
            }}
        ]
        items = await accounts_col.aggregate(pipeline).to_list(length=100)

        if not items:
            await query.answer("❌ Out of stock for this selection!", show_alert=True)
            return

        buttons = []
        for item in items:
            info = item["_id"]
            cat = info.get("category", "General")
            year = info["year"]
            price = info["price"]
            count = item["count"]

            btn_text = f"📁 {cat} | Year: {year} | ₹{price:.2f} | Stock: {count}"
            buttons.append([InlineKeyboardButton(btn_text, callback_data=f"sel_item_{c_name}_{cat}_{year}_{price}")])

        buttons.append([InlineKeyboardButton("🔙 Back to Countries", callback_data="user_buy_menu")])

        flag = get_flag(c_name)
        await query.message.edit_text(
            f"{flag} **{c_name.upper()} ACCOUNTS**\n\nSelect your package option below:",
            reply_markup=InlineKeyboardMarkup(buttons)
        )

    elif data.startswith("sel_item_"):
        parts = data.split("_")
        c_name, cat, year, price = parts[2], parts[3], parts[4], float(parts[5])

        flag = get_flag(c_name)
        bal = await get_user_balance(user_id)

        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Confirm Purchase", callback_data=f"cnf_buy_{c_name}_{cat}_{year}_{price}")],
            [InlineKeyboardButton("🔙 Cancel", callback_data="user_buy_menu")]
        ])

        await query.message.edit_text(
            f"{flag} **{c_name.upper()} - ACCOUNT CONFIRMATION**\n\n"
            f"📁 **Category:** {cat}\n"
            f"📅 **Creation Year:** {year}\n"
            f"💵 **Price:** ₹{price:.2f}\n"
            f"💰 **Your Balance:** ₹{bal:.2f}\n\n"
            f"Click Confirm below to complete purchase.",
            reply_markup=kb
        )

    elif data.startswith("cnf_buy_"):
        parts = data.split("_")
        c_name, cat, year, price = parts[2], parts[3], parts[4], float(parts[5])

        bal = await get_user_balance(user_id)
        if bal < price:
            await query.answer("❌ Not enough balance! Please deposit first.", show_alert=True)
            return

        flag = get_flag(c_name)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Yes, Purchase Now", callback_data=f"cb_buy_yes_{c_name}_{cat}_{year}_{price}")],
            [InlineKeyboardButton("❌ Cancel", callback_data="user_buy_menu")]
        ])
        await query.message.edit_text(
            f"{flag} **FINAL PURCHASE CONFIRMATION**\n\n"
            f"📁 **Category:** {cat}\n"
            f"{flag} **Country / Year:** {c_name} ({year})\n"
            f"💵 **Price:** ₹{price:.2f}\n"
            f"💰 **Your Balance:** ₹{bal:.2f}\n\n"
            f"⚠️ **₹{price:.2f} will be deducted from your wallet.**\n"
            f"Are you sure you want to purchase?",
            reply_markup=kb
        )

    elif data.startswith("cb_buy_yes_"):
        parts = data.split("_")
        c_name, cat, year, price = parts[3], parts[4], parts[5], float(parts[6])

        bal = await get_user_balance(user_id)
        if bal < price:
            await query.answer("❌ Not enough balance!", show_alert=True)
            return

        acc = await accounts_col.find_one_and_update(
            {"country": c_name, "category": cat, "year": year, "price": price, "status": "AVAILABLE"},
            {"$set": {"status": "SOLD", "sold_to": user_id}}
        )

        if not acc:
            await query.answer("❌ Item went out of stock!", show_alert=True)
            return

        await update_balance(user_id, -price)

        acc_id = str(acc["_id"])
        phone = acc["phone_number"]
        session_str = acc["session_string"]
        two_fa = acc["two_fa"]
        cashback = acc.get("cashback", 0.0)

        flag = get_flag(c_name)
        msg = f"⚡ **OTP Live Monitoring Started!**\n\n" \
              f"{flag} **Country:** {c_name.capitalize()} ({year})\n" \
              f"📞 **Phone:** `{phone}`\n" \
              f"🔑 **2FA Password:** `{two_fa}`\n" \
              f"💵 **Price Paid:** ₹{price:.2f}\n"

        if cashback > 0:
            msg += f"\n🎁 **This account has a cashback of:** ₹{cashback:.2f}\n" \
                   f"_You can claim it after pressing **Finish & Logout Bot**._\n"

        msg += "\n_Enter phone number in app. Monitoring OTP..._"

        await query.message.edit_text(msg, reply_markup=get_account_options_keyboard(acc_id))

        asyncio.create_task(listen_for_otp(user_id, phone, session_str, two_fa, acc_id, price))

    elif data.startswith("refetch_otp_"):
        acc_id = data.split("_")[2]
        await query.answer("🔄 Re-fetching latest OTP...", show_alert=False)
        await fetch_latest_otp(user_id, acc_id, is_manual=True)

    elif data.startswith("manage_devs_"):
        acc_id = data.split("_")[2]
        await query.answer("📱 Fetching Active Devices...", show_alert=False)
        acc = await accounts_col.find_one({"_id": ObjectId(acc_id)})

        if not acc:
            await query.message.reply_text("❌ **Account session record not found!**")
            return

        try:
            t_client = TelegramClient(StringSession(acc["session_string"]), API_ID, API_HASH)
            await t_client.connect()

            if not await t_client.is_user_authorized():
                await query.message.reply_text(f"⚠️ **Account Session Expired or Closed:** `{acc['phone_number']}`")
                return

            authorizations = await t_client(GetAuthorizationsRequest())
            await t_client.disconnect()

            dev_text = f"📱 **Active Devices List for** `{acc['phone_number']}`:\n\n"
            buttons = []

            for idx, auth in enumerate(authorizations.authorizations, 1):
                is_curr = " (Current Session)" if auth.current else ""
                dev_text += (
                    f"**{idx}. {auth.device_model}**{is_curr}\n"
                    f"▫️ **App:** {auth.app_name} ({auth.app_version})\n"
                    f"▫️ **System:** {auth.platform} ({auth.system_version})\n"
                    f"▫️ **IP:** `{auth.ip}` ({auth.country})\n\n"
                )
                btn_label = f"❌ Terminate {auth.device_model}" + (" (Current)" if auth.current else "")
                buttons.append([InlineKeyboardButton(btn_label, callback_data=f"term_hash_{acc_id}_{auth.hash}")])

            buttons.append([InlineKeyboardButton("🔙 Back to Options", callback_data=f"refetch_otp_{acc_id}")])
            await query.message.reply_text(dev_text, reply_markup=InlineKeyboardMarkup(buttons))

        except Exception as e:
            await query.message.reply_text(f"❌ Error fetching active devices: `{e}`")

    elif data.startswith("term_hash_"):
        parts = data.split("_")
        acc_id, hash_val = parts[2], int(parts[3])

        await query.answer("🛠️ Terminating selected session...", show_alert=False)
        acc = await accounts_col.find_one({"_id": ObjectId(acc_id)})

        if not acc:
            await query.message.reply_text("❌ **Account session record not found!**")
            return

        try:
            t_client = TelegramClient(StringSession(acc["session_string"]), API_ID, API_HASH)
            await t_client.connect()

            if not await t_client.is_user_authorized():
                await query.message.reply_text(f"⚠️ **Account Session Expired or Closed:** `{acc['phone_number']}`")
                return

            try:
                await t_client(ResetAuthorizationRequest(hash=hash_val))
                await query.message.reply_text("✅ **Device session terminated successfully!**")
            except FreshResetAuthorisationForbiddenError:
                await query.message.reply_text("⚠️ **Telegram Security Restriction:** New sessions cannot terminate other devices within 24 hours.")
            except Exception as err:
                await query.message.reply_text(f"❌ Failed to terminate device session: `{err}`")

            await t_client.disconnect()

        except Exception as e:
            await query.message.reply_text(f"❌ Error connecting to session: `{e}`")

    elif data.startswith("logout_bot_"):
        acc_id = data.split("_")[2]
        await query.answer("🚪 Logging out bot session...", show_alert=True)

        acc = await accounts_col.find_one({"_id": ObjectId(acc_id)})
        if acc:
            try:
                t_client = TelegramClient(StringSession(acc["session_string"]), API_ID, API_HASH)
                await t_client.connect()
                await t_client.log_out()
                await query.message.reply_text("🚪 **Finish & Logout Complete! Bot session deleted.**")
            except Exception as e:
                await query.message.reply_text(f"⚠️ Session notice: `{e}`")

        # ---- CASHBACK OFFER (after logout) ----
        if acc and float(acc.get("cashback", 0.0) or 0.0) > 0 and not acc.get("cashback_claimed", False):
            acc_cb = float(acc.get("cashback", 0.0) or 0.0)
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("👤 Add to My Profile", callback_data=f"cbtoprofile_{acc_id}", style=ButtonStyle.PRIMARY)],
                [InlineKeyboardButton("💸 Add to Withdraw Balance", callback_data=f"cbtowithdraw_{acc_id}", style=ButtonStyle.SUCCESS)]
            ])
            await app.send_message(
                user_id,
                f"🎁 **CASHBACK REWARD!**\n\n"
                f"The account you purchased has a cashback of **₹{acc_cb:.2f}**.\n\n"
                f"**Choose where you want to add it:**\n\n"
                f"👤 **Add to My Profile** — Saved to your Profile Cashback.\n"
                f"⚠️ _Once added to your Profile, it can **NOT** be transferred to your Withdraw Balance later._\n\n"
                f"💸 **Add to Withdraw Balance** — Added to your Withdrawable Cashback balance and can be withdrawn.\n\n"
                f"⚠️ **This is a one-time choice and cannot be changed!**",
                reply_markup=kb
            )

    # ==================== CASHBACK CLAIM HANDLERS ====================
    elif data.startswith("cbtoprofile_") or data.startswith("cbtowithdraw_"):
        acc_id = data.split("_", 1)[1]

        # Atomic claim — dobara click se double credit nahi hoga
        acc_doc = await accounts_col.find_one_and_update(
            {"_id": ObjectId(acc_id), "cashback_claimed": {"$ne": True}},
            {"$set": {"cashback_claimed": True}}
        )
        if not acc_doc:
            await query.answer("⚠️ Already claimed or invalid!", show_alert=True)
            return

        amount = float(acc_doc.get("cashback", 0.0) or 0.0)

        if data.startswith("cbtoprofile_"):
            # Profile cashback = locked, withdraw me use NAHI hoga
            await update_profile_cashback(user_id, amount)
            await query.message.edit_text(
                f"✅ **₹{amount:.2f} Cashback added to your Profile!**\n\n"
                f"⚠️ **Note:** Profile Cashback is locked and can NOT be transferred "
                f"to your Withdraw Balance or withdrawn.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Main Menu", callback_data="user_main_menu")]])
            )
            await log_to_channel(
                f"🎁 **CASHBACK CLAIMED (PROFILE)**\n\n"
                f"👤 **User ID:** `{user_id}`\n"
                f"💵 **Amount:** ₹{amount:.2f}\n"
                f"📌 **Type:** Profile Cashback (Locked)"
            )
        else:
            # Withdrawable cashback balance
            await update_withdraw_cashback(user_id, amount)
            await query.message.edit_text(
                f"✅ **₹{amount:.2f} Cashback added to your Withdraw Balance!**\n\n"
                f"💸 You can now withdraw it from **Withdraw Cashback** menu.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💸 Withdraw Cashback", callback_data="user_withdraw_menu")],
                                                    [InlineKeyboardButton("🔙 Back to Main Menu", callback_data="user_main_menu")]])
            )
            await log_to_channel(
                f"🎁 **CASHBACK CLAIMED (WITHDRAW)**\n\n"
                f"👤 **User ID:** `{user_id}`\n"
                f"💵 **Amount:** ₹{amount:.2f}\n"
                f"📌 **Type:** Withdrawable Cashback"
            )
        await query.answer("✅ Cashback claimed!")

    elif data == "user_withdraw_menu":
        cb = await get_withdraw_cashback(user_id)
        if cb < MIN_WITHDRAW:
            bal = await get_user_balance(user_id)
            await query.answer(
                f"❌ Withdrawals are only from CASHBACK balance.\n"
                f"🎁 Withdrawable Cashback: ₹{cb:.2f} (Min ₹{MIN_WITHDRAW:.2f})\n"
                f"💰 Wallet Balance (not withdrawable): ₹{bal:.2f}",
                show_alert=True)
            return

        user_states[user_id] = "WAIT_WITHDRAW_AMOUNT"
        await query.message.edit_text(
            f"💸 **CASHBACK WITHDRAWAL**\n\n"
            f"🎁 **Withdrawable Cashback:** ₹{cb:.2f}\n"
            f"⚠️ **Minimum Withdrawal:** ₹{MIN_WITHDRAW:.2f}\n\n"
            f"ℹ️ _Only your **Cashback** can be withdrawn — your deposit/wallet balance can NOT be withdrawn._\n\n"
            f"🔢 **Enter amount to withdraw:**",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Main Menu", callback_data="user_main_menu")]])
        )

    # ==================== ADMIN PANEL HANDLERS ====================
    elif data == "admin_panel":
        if user_id not in SUDO_USERS: return
        user_states.pop(user_id, None)
        await query.message.edit_text("⚙️ **Admin Dashboard**", reply_markup=get_admin_panel_keyboard(user_id))

    elif data == "admin_maint_panel":
        if user_id not in SUDO_USERS: return
        user_states.pop(user_id, None)
        is_active, reason = await get_maintenance_status()
        status_text = "🟢 **ONLINE (Active)**" if not is_active else "🔴 **MAINTENANCE MODE (Paused)**"
        kb = await get_maintenance_panel_keyboard()
        await query.message.edit_text(
            f"🛠️ **MAINTENANCE CONTROL PANEL**\n\n"
            f"📊 **Current Status:** {status_text}\n"
            f"📝 **Reason / Message:**\n`{reason}`\n\n"
            f"Toggle maintenance status or modify the maintenance message using options below:",
            reply_markup=kb
        )

    elif data == "adm_toggle_maint":
        if user_id not in SUDO_USERS: return
        is_active, reason = await get_maintenance_status()
        new_state = not is_active
        await set_maintenance_status(new_state)

        state_str = "ENABLED" if new_state else "DISABLED"
        await query.answer(f"✅ Maintenance Mode {state_str}!", show_alert=True)

        status_text = "🟢 **ONLINE (Active)**" if not new_state else "🔴 **MAINTENANCE MODE (Paused)**"
        kb = await get_maintenance_panel_keyboard()
        await query.message.edit_text(
            f"🛠️ **MAINTENANCE CONTROL PANEL**\n\n"
            f"📊 **Current Status:** {status_text}\n"
            f"📝 **Reason / Message:**\n`{reason}`\n\n"
            f"Toggle maintenance status or modify the maintenance message using options below:",
            reply_markup=kb
        )

    elif data == "adm_change_maint_reason":
        if user_id not in SUDO_USERS: return
        user_states[user_id] = "ADM_STEP_MAINT_REASON"
        await query.message.edit_text(
            "✏️ **SET MAINTENANCE REASON / TEXT**\n\n"
            "Send the text or reason to show users when maintenance mode is active:",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Maintenance Panel", callback_data="admin_maint_panel")]])
        )

    elif data == "admin_stats":
        if user_id not in SUDO_USERS: return
        await query.answer("📊 Calculating revenue & stats...", show_alert=False)

        total_users = await users_col.count_documents({})
        banned_users = await users_col.count_documents({"is_banned": True})

        available_stock = await accounts_col.count_documents({"status": "AVAILABLE"})
        sold_stock = await accounts_col.count_documents({"status": "SOLD"})

        revenue_pipeline = [
            {"$match": {"status": "SOLD"}},
            {"$group": {
                "_id": None,
                "total_rev": {"$sum": "$price"},
                "total_cb": {"$sum": "$cashback"}
            }}
        ]
        rev_res = await accounts_col.aggregate(revenue_pipeline).to_list(length=1)

        total_revenue = rev_res[0]["total_rev"] if rev_res else 0.0
        total_cashback_issued = rev_res[0]["total_cb"] if rev_res else 0.0

        user_cb_pipeline = [
            {"$group": {
                "_id": None,
                "total_claimed_cb": {"$sum": "$profile_cashback"},
                "total_withdraw_cb": {"$sum": "$withdraw_cashback"}
            }}
        ]
        user_cb_res = await users_col.aggregate(user_cb_pipeline).to_list(length=1)
        profile_cb_claimed = user_cb_res[0]["total_claimed_cb"] if user_cb_res else 0.0
        withdraw_cb_total = user_cb_res[0]["total_withdraw_cb"] if user_cb_res else 0.0

        stats_text = (
            f"📊 **BOT STATISTICS & REVENUE METRICS**\n\n"
            f"💰 **Total Revenue:** ₹{total_revenue:.2f}\n"
            f"🎁 **Total Cashback Issued:** ₹{total_cashback_issued:.2f}\n"
            f"👤 **Profile Cashback Claimed:** ₹{profile_cb_claimed:.2f}\n"
            f"💸 **Withdrawable Cashback Held:** ₹{withdraw_cb_total:.2f}\n\n"
            f"📦 **Available Stock:** {available_stock} accounts\n"
            f"🛍️ **Total Accounts Sold:** {sold_stock} accounts\n\n"
            f"👥 **Total Registered Users:** {total_users}\n"
            f"🚫 **Banned Users:** {banned_users}\n"
            f"👨‍💻 **Total Admins:** {len(SUDO_USERS)}"
        )
        await query.message.edit_text(
            stats_text,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Admin Panel", callback_data="admin_panel")]])
        )

    elif data == "admin_user_history":
        if user_id not in SUDO_USERS: return
        user_states[user_id] = "ADM_STEP_GET_USER_HISTORY"
        await query.message.edit_text(
            "ℹ️ **FETCH USER DETAILS & HISTORY**\n\n"
            "Send the **User ID** of the user whose complete information and history you want to check:",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Admin Panel", callback_data="admin_panel")]])
        )

    elif data == "admin_add_acc":
        if user_id not in SUDO_USERS: return
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("⚡ Temporary Spam", callback_data="adm_cat_Temporary Spam")],
            [InlineKeyboardButton("🚫 Permanent Spam", callback_data="adm_cat_Permanent Spam")],
            [InlineKeyboardButton("✨ Fresh Account", callback_data="adm_cat_Fresh Account")],
            [InlineKeyboardButton("📜 Old Account", callback_data="adm_cat_Old Account")],
            [InlineKeyboardButton("🔙 Back to Admin Panel", callback_data="admin_panel")]
        ])
        await query.message.edit_text("📂 **Select Account Category:**", reply_markup=kb)

    elif data == "admin_remove_stock":
        if user_id not in SUDO_USERS: return

        # Pipeline updated to pick a sample _id from the grouped stock
        pipeline = [
            {"$match": {"status": "AVAILABLE"}},
            {"$group": {
                "_id": {
                    "category": "$category",
                    "country": "$country",
                    "year": "$year",
                    "price": "$price"
                },
                "sample_id": {"$first": "$_id"},
                "count": {"$sum": 1}
            }}
        ]
        stocks = await accounts_col.aggregate(pipeline).to_list(length=100)

        if not stocks:
            await query.answer("❌ No active stock currently available!", show_alert=True)
            return

        buttons = []
        for s in stocks:
            info = s["_id"]
            sample_id = str(s["sample_id"])
            cat = info.get("category", "General")
            country = info["country"]
            year = info["year"]
            price = info["price"]
            count = s["count"]
            flag = get_flag(country)

            btn_label = f"🗑️ Delete [{cat}] {flag} {country} ({year}) | ₹{price} | Stock: {count}"
            # Cleaned callback_data using ObjectId to guarantee <64 bytes and fix split errors
            buttons.append([InlineKeyboardButton(btn_label, callback_data=f"adm_rmstk_{sample_id}")])

        buttons.append([InlineKeyboardButton("🔙 Back to Admin Panel", callback_data="admin_panel")])
        await query.message.edit_text("🗑️ **Select Stock Item to Remove/Delete:**", reply_markup=InlineKeyboardMarkup(buttons))

    elif data.startswith("adm_rmstk_"):
        if user_id not in SUDO_USERS: return
        sample_id = data.split("_")[2]

        # Fetch sample item details using sample_id to perform accurate bulk deletion
        sample_doc = await accounts_col.find_one({"_id": ObjectId(sample_id)})
        if not sample_doc:
            await query.answer("❌ Stock item not found or already deleted!", show_alert=True)
            return

        res = await accounts_col.delete_many({
            "category": sample_doc.get("category"),
            "country": sample_doc.get("country"),
            "year": sample_doc.get("year"),
            "price": sample_doc.get("price"),
            "status": "AVAILABLE"
        })

        await query.answer(f"✅ Removed {res.deleted_count} items from stock!", show_alert=True)
        await query.message.edit_text("✅ **Stock removed successfully!**", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Admin Panel", callback_data="admin_panel")]]))
    elif data == "admin_change_price":
        if user_id != OWNER_ID:
            await query.answer("🚫 Only Owner can change stock prices!", show_alert=True)
            return

        user_states.pop(user_id, None)
        pipeline = [
            {"$match": {"status": "AVAILABLE"}},
            {"$group": {
                "_id": {
                    "category": "$category",
                    "country": "$country",
                    "year": "$year",
                    "price": "$price"
                },
                "count": {"$sum": 1}
            }}
        ]
        stocks = await accounts_col.aggregate(pipeline).to_list(length=100)

        if not stocks:
            await query.answer("❌ No Stock Available to change price!", show_alert=True)
            return

        buttons = []
        for s in stocks:
            info = s["_id"]
            cat = info.get("category", "General")
            country = info["country"]
            year = info["year"]
            price = info["price"]
            count = s["count"]
            flag = get_flag(country)

            btn_label = f"📁 [{cat}] {flag} {country} ({year}) - Current: ₹{price} | Stock: {count}"
            buttons.append([InlineKeyboardButton(btn_label, callback_data=f"adm_chgprice_sel_{cat}_{country}_{year}")])

        buttons.append([InlineKeyboardButton("🔙 Back to Admin Panel", callback_data="admin_panel")])
        await query.message.edit_text("🏷️ **Select Stock Item to Edit Price / Cashback:**", reply_markup=InlineKeyboardMarkup(buttons))

    elif data.startswith("adm_chgprice_sel_"):
        if user_id != OWNER_ID:
            await query.answer("🚫 Only Owner can change stock!", show_alert=True)
            return

        parts = data.split("_")
        cat, country, year = parts[3], parts[4], parts[5]

        temp_data[user_id] = {"chg_cat": cat, "chg_country": country, "chg_year": year}
        cur = await accounts_col.find_one(
            {"category": cat, "country": country, "year": year, "status": "AVAILABLE"})
        cur_price = cur.get("price", 0.0) if cur else 0.0
        cur_cb = cur.get("cashback", 0.0) if cur else 0.0

        flag = get_flag(country)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🏷️ Change Price", callback_data="chg_price_btn")],
            [InlineKeyboardButton("🎁 Change Cashback", callback_data="chg_cash_btn")],
            [InlineKeyboardButton("🔙 Back to Stock List", callback_data="admin_change_price")]
        ])
        await query.message.edit_text(
            f"🛠️ **EDIT STOCK: {cat} ({flag} {country} {year})**\n\n"
            f"💰 **Current Price:** ₹{cur_price:.2f}\n"
            f"🎁 **Current Cashback:** ₹{cur_cb:.2f}\n\n"
            f"What do you want to change?",
            reply_markup=kb
        )

    elif data == "chg_price_btn":
        if user_id != OWNER_ID:
            await query.answer("🚫 Only Owner!", show_alert=True)
            return
        user_states[user_id] = "ADM_STEP_WAIT_NEW_PRICE"
        await query.message.edit_text(
            "🏷️ **Enter the new Price (₹):**",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel", callback_data="admin_change_price")]])
        )

    elif data == "chg_cash_btn":
        if user_id != OWNER_ID:
            await query.answer("🚫 Only Owner!", show_alert=True)
            return
        user_states[user_id] = "ADM_STEP_WAIT_NEW_CASHBACK"
        await query.message.edit_text(
            "🎁 **Enter the new Cashback amount (₹) — type `0` to remove cashback:**",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel", callback_data="admin_change_price")]])
        )

    elif data == "admin_edit_bal":
        if user_id != OWNER_ID:
            await query.answer("🚫 Only Owner can edit balance!", show_alert=True)
            return
        user_states[user_id] = "ADM_STEP_EDIT_BAL"
        await query.message.edit_text(
            "✏️ **ADD USER BALANCE**\n\n"
            "Send User ID and Balance to Add separated by space.\n"
            "Format: `UserID BalanceToAdd`\n"
            "Example: `123456789 5`",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Admin Panel", callback_data="admin_panel")]])
        )

    elif data == "admin_manage_sudo":
        if user_id != OWNER_ID:
            await query.answer("🚫 Owner Only Access!", show_alert=True)
            return
        user_states.pop(user_id, None)
        kb = await get_manage_sudo_keyboard()
        await query.message.edit_text("👥 **MANAGE ADMINS**", reply_markup=kb)

    elif data == "adm_add_sudo_btn":
        if user_id != OWNER_ID: return
        user_states[user_id] = "ADM_STEP_INPUT_ADD_SUDO"
        await query.message.edit_text(
            "➕ **ADD NEW ADMIN**\n\nSend the **Telegram User ID** of the person you want to make Admin:",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Manage Admins", callback_data="admin_manage_sudo")]])
        )

    elif data.startswith("adm_rem_sudo_"):
        if user_id != OWNER_ID: return
        target_id = int(data.split("_")[3])
        await remove_sudo_user(target_id)
        await query.answer(f"🗑️ Removed Admin {target_id}", show_alert=True)
        kb = await get_manage_sudo_keyboard()
        await query.message.edit_text("👥 **MANAGE ADMINS**", reply_markup=kb)

    elif data.startswith("adm_cat_"):
        if user_id not in SUDO_USERS: return
        cat = data.split("_")[2]
        temp_data[user_id] = {"category": cat}
        user_states[user_id] = "ADM_STEP_COUNTRY"
        await query.message.edit_text(
            f"Selected Category: **{cat}**\n\n📝 **Step 1:** Enter Country Name (e.g. `India`, `Bangladesh`, `Indonesia`, `USA`):",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Admin Panel", callback_data="admin_panel")]])
        )

    elif data == "admin_broadcast":
        if user_id not in SUDO_USERS: return
        user_states[user_id] = "ADM_STEP_BROADCAST"
        await query.message.edit_text(
            "📢 **Send the message you want to Broadcast in DM to all users:**",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Admin Panel", callback_data="admin_panel")]])
        )

    elif data == "admin_ban_user":
        if user_id not in SUDO_USERS: return
        user_states[user_id] = "ADM_STEP_BAN_ID"
        await query.message.edit_text(
            "🚫 **Enter User ID or @Username to Ban:**",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Admin Panel", callback_data="admin_panel")]])
        )

    elif data == "admin_unban_user":
        if user_id not in SUDO_USERS: return
        user_states[user_id] = "ADM_STEP_UNBAN_ID"
        await query.message.edit_text(
            "🟢 **Enter User ID or @Username to Unban:**",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Admin Panel", callback_data="admin_panel")]])
        )

    elif data.startswith("adm_app_dep_"):
        if user_id not in SUDO_USERS: return
        _, _, _, dep_user_id, amount, req_id = data.split("_")
        dep_user_id = int(dep_user_id)
        amount = float(amount)

        res = await requests_col.find_one_and_update(
            {"_id": ObjectId(req_id), "status": "PENDING"},
            {"$set": {"status": "APPROVED", "approved_by": user_id}}
        )

        if not res:
            await query.answer("⚠️ Already processed by another admin!", show_alert=True)
            return

        await update_balance(dep_user_id, amount)
        admin_mention = query.from_user.mention

        # Synchronize and update across all admin message entries
        msg_entries = admin_deposit_msg_map.pop(req_id, [])
        for adm_id, msg_id in msg_entries:
            try:
                await app.edit_message_caption(
                    chat_id=adm_id,
                    message_id=msg_id,
                    caption=query.message.caption + f"\n\n✅ **APPROVED (+₹{amount:.2f})** by {admin_mention}",
                    reply_markup=None
                )
            except Exception as e:
                logging.error(f"Error updating admin message for {adm_id}: {e}")

        await app.send_message(dep_user_id, f"🎉 **Deposit Approved!** ₹{amount:.2f} credited to your wallet.")

        # Log auto-approval info to log channel
        log_text = (
            f"💳 **AUTOMATIC / MANUAL DEPOSIT APPROVED**\n\n"
            f"👤 **User ID:** `{dep_user_id}`\n"
            f"💵 **Amount Credited:** ₹{amount:.2f}\n"
            f"👨‍💻 **Approved By:** {admin_mention}\n"
            f"📌 **Status:** Wallet Balance Updated"
        )
        await log_to_channel(log_text)

    elif data.startswith("adm_rej_dep_"):
        if user_id not in SUDO_USERS: return
        _, _, _, dep_user_id, req_id = data.split("_")
        dep_user_id = int(dep_user_id)

        res = await requests_col.find_one_and_update(
            {"_id": ObjectId(req_id), "status": "PENDING"},
            {"$set": {"status": "REJECTED", "rejected_by": user_id}}
        )

        if not res:
            await query.answer("⚠️ Already processed by another admin!", show_alert=True)
            return

        admin_mention = query.from_user.mention

        # Synchronize and remove buttons across all admin message entries
        msg_entries = admin_deposit_msg_map.pop(req_id, [])
        for adm_id, msg_id in msg_entries:
            try:
                await app.edit_message_caption(
                    chat_id=adm_id,
                    message_id=msg_id,
                    caption=query.message.caption + f"\n\n❌ **REJECTED** by {admin_mention}",
                    reply_markup=None
                )
            except Exception as e:
                logging.error(f"Error updating admin message for {adm_id}: {e}")

        await app.send_message(dep_user_id, "❌ Your deposit request was rejected by Admin.")

    elif data.startswith("own_app_wth_"):
        if user_id != OWNER_ID:
            await query.answer("🚫 Only Owner can approve withdrawals!", show_alert=True)
            return

        parts = data.split("_")
        wth_user_id, amount, req_id = int(parts[3]), float(parts[4]), parts[5]

        res = await requests_col.find_one_and_update(
            {"_id": ObjectId(req_id), "status": "PENDING"},
            {"$set": {"status": "APPROVED"}}
        )

        if not res:
            await query.answer("⚠️ Already processed!", show_alert=True)
            return

        owner_mention = query.from_user.mention
        await query.message.edit_caption(caption=query.message.caption + f"\n\n✅ **WITHDRAWAL SENT & APPROVED** by {owner_mention}", reply_markup=None)
        await app.send_message(wth_user_id, f"🎉 **Withdrawal Approved!** ₹{amount:.2f} has been sent to your QR account.")

# ==================== PHOTO RECEIVER ====================
@app.on_message(filters.photo & filters.private)
async def photo_receiver(client: Client, message: Message):
    user_id = message.from_user.id
    state = user_states.get(user_id)

    banned, reason = await is_banned(user_id)
    if banned:
        await message.reply_text(f"🚫 **You are banned from using this bot.**\n\n**Reason:** {reason}")
        return

    maint_active, maint_reason = await get_maintenance_status()
    if maint_active and user_id not in SUDO_USERS:
        await message.reply_text(f"🚧 **SYSTEM MAINTENANCE MODE ACTIVE** 🚧\n\n**Message:** {maint_reason}")
        return

    if state == "WAIT_DEPOSIT_PHOTO_MANUAL":
        temp_data[user_id]["photo_id"] = message.photo.file_id
        user_states[user_id] = "WAIT_DEPOSIT_TXN_ID_MANUAL"
        await message.reply_text("🧾 **Now enter the Transaction ID / UTR Number:**")

    elif state == "WAIT_WITHDRAW_QR":
        qr_photo_id = message.photo.file_id
        amount = temp_data[user_id]["withdraw_amount"]
        user_states.pop(user_id, None)

        # Withdraw sirf CASHBACK se hoga — deposit/wallet balance involved NAHI hai
        await update_withdraw_cashback(user_id, -amount)

        req_doc = {"type": "WITHDRAW", "status": "PENDING"}
        req_res = await requests_col.insert_one(req_doc)
        req_id = str(req_res.inserted_id)

        await message.reply_text("⏳ **Withdrawal request submitted! Sent to Owner for payment processing.**")

        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Send Money & Approve", callback_data=f"own_app_wth_{user_id}_{amount}_{req_id}")]
        ])

        caption = (
            f"💸 **NEW CASHBACK WITHDRAWAL REQUEST (OWNER ONLY)**\n\n"
            f"👤 **User:** {message.from_user.mention} (`{user_id}`)\n"
            f"💵 **Amount:** ₹{amount:.2f}\n"
            f"🎁 **Source:** Cashback Balance\n"
            f"📌 **Status:** Pending payment to QR below."
        )

        try:
            await app.send_photo(chat_id=OWNER_ID, photo=qr_photo_id, caption=caption, reply_markup=kb)
        except Exception as e:
            logging.error(f"Failed to send withdraw req to Owner: {e}")

# ==================== STEP-BY-STEP INPUT ROUTER ====================
@app.on_message(filters.text & filters.private & ~filters.command(["start", "help", "admin"]))
async def text_router(client: Client, message: Message):
    user_id = message.from_user.id
    state = user_states.get(user_id)

    banned, reason = await is_banned(user_id)
    if banned:
        await message.reply_text(f"🚫 **You are banned from using this bot.**\n\n**Reason:** {reason}")
        return

    maint_active, maint_reason = await get_maintenance_status()
    if maint_active and user_id not in SUDO_USERS and not (state and state.startswith("ADM_")):
        await message.reply_text(f"🚧 **SYSTEM MAINTENANCE MODE ACTIVE** 🚧\n\n**Message:** {maint_reason}")
        return

    if not state:
        return

    if state.startswith("WAIT_AUTO_TXN_ID_"):
        pay_id = state.replace("WAIT_AUTO_TXN_ID_", "")
        txn_id = message.text.strip()

        pay_doc = await payments_col.find_one({"_id": ObjectId(pay_id)})
        if not pay_doc:
            await message.reply_text("❌ Payment request session expired!")
            user_states.pop(user_id, None)
            return

        expected_amount = pay_doc["amount"]

        await message.reply_text("🔍 **Checking payment status automatically...**")
        is_valid = await check_auto_payment_status(txn_id, expected_amount)

        if is_valid:
            await payments_col.update_one({"_id": ObjectId(pay_id)}, {"$set": {"status": "SUCCESS", "txn_id": txn_id}})
            await update_balance(user_id, expected_amount)
            user_states.pop(user_id, None)

            await message.reply_text(
                f"🎉 **PAYMENT RECEIVED & VERIFIED!**\n\n"
                f"✅ Credited **₹{expected_amount:.2f}** to your wallet balance."
            )

            # Auto log message to log channel / group chat
            log_text = (
                f"⚡ **AUTO DEPOSIT SUCCESSFUL (UPI / INR)**\n\n"
                f"👤 **User ID:** `{user_id}`\n"
                f"💵 **Amount:** ₹{expected_amount:.2f}\n"
                f"🧾 **Txn ID:** `{txn_id}`\n\n"
                f"📌 **Status:** Automatically Credited & Approved"
            )
            await log_to_channel(log_text)
        else:
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("📩 Payment Not Verified? Manual Approval",
                                      callback_data=f"auto_to_manual_{pay_id}")]
            ])
            await message.reply_text(
                "❌ **Payment Not Verified!**\n\nIf you have paid, press the button below and "
                "send the screenshot — our admin will approve it.",
                reply_markup=kb,
            )

    elif state == "WAIT_DEPOSIT_AMOUNT_MANUAL":
        try:
            amount = float(message.text.strip())
            if amount < MIN_DEPOSIT:
                await message.reply_text(f"❌ **Minimum Deposit limit is ₹{MIN_DEPOSIT:.2f}.**")
                return

            temp_data[user_id] = {"amount": amount}
            user_states[user_id] = "WAIT_DEPOSIT_PHOTO_MANUAL"

            qr_image = generate_upi_qr(UPI_ID_TEXT, PAYEE_NAME, amount)
            await app.send_photo(
                chat_id=user_id,
                photo=qr_image,
                caption=f"💳 **Send ₹{amount:.2f} via UPI / INR to ID:** `{UPI_ID_TEXT}`\n\nSend payment screenshot here."
            )
        except ValueError:
            await message.reply_text("❌ Invalid input!")

    elif state == "WAIT_DEPOSIT_AMOUNT_AUTO":
        try:
            amount = float(message.text.strip())
            if amount < MIN_DEPOSIT:
                await message.reply_text(f"❌ **Minimum Deposit limit is ₹{MIN_DEPOSIT:.2f}.**")
                return

            pay_doc = {
                "user_id": user_id,
                "amount": amount,
                "status": "PENDING",
                "created_at": time.time()
            }
            pay_res = await payments_col.insert_one(pay_doc)
            pay_id = str(pay_res.inserted_id)

            user_states.pop(user_id, None)
            qr_image = generate_upi_qr(UPI_ID_TEXT, PAYEE_NAME, amount)

            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Check Payment (UPI / INR)", callback_data=f"auto_check_pay_{pay_id}")],
                [InlineKeyboardButton("🔙 Back to Main Menu", callback_data="user_main_menu")]
            ])

            caption = (
                f"💳 **Pay ₹{amount:.2f} using QR code above (UPI / INR)**\n\n"
                f"📌 **UPI ID:** `{UPI_ID_TEXT}`\n\n"
                f"After paying, press the **Check Payment** button below to complete verification."
            )

            await app.send_photo(
                chat_id=user_id,
                photo=qr_image,
                caption=caption,
                reply_markup=kb
            )
            asyncio.create_task(manual_fallback_timeout(user_id, pay_id, amount))
        except ValueError:
            await message.reply_text("❌ Invalid input!")

    elif state == "WAIT_DEPOSIT_TXN_ID_MANUAL":
        txn_id = message.text.strip()
        data = temp_data[user_id]
        photo_id = data["photo_id"]
        amount = data["amount"]
        user_states.pop(user_id, None)

        req_doc = {"type": "DEPOSIT", "status": "PENDING"}
        req_res = await requests_col.insert_one(req_doc)
        req_id = str(req_res.inserted_id)

        await message.reply_text("⏳ **Deposit proof submitted! Admins are verifying your payment.**")

        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Approve", callback_data=f"adm_app_dep_{user_id}_{amount}_{req_id}"),
                InlineKeyboardButton("❌ Reject", callback_data=f"adm_rej_dep_{user_id}_{req_id}")
            ]
        ])

        deposit_caption = (
            f"📥 **NEW DEPOSIT VERIFICATION REQUEST**\n\n"
            f"👤 **User:** {message.from_user.mention} (`{user_id}`)\n"
            f"💵 **Amount:** ₹{amount:.2f}\n"
            f"🧾 **Transaction ID / UTR:** `{txn_id}`"
        )

        admin_deposit_msg_map[req_id] = []
        for sudo_id in SUDO_USERS:
            try:
                sent_msg = await app.send_photo(chat_id=sudo_id, photo=photo_id, caption=deposit_caption, reply_markup=kb)
                admin_deposit_msg_map[req_id].append((sudo_id, sent_msg.id))
            except Exception as e:
                logging.error(f"Failed sending DM to Admin {sudo_id}: {e}")

    elif state == "ADM_STEP_GET_USER_HISTORY":
        if user_id not in SUDO_USERS: return
        try:
            target_id = int(message.text.strip())
            u_data = await users_col.find_one({"user_id": target_id})

            if not u_data:
                await message.reply_text(
                    f"❌ **User ID `{target_id}` not found in Database!**",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Admin Panel", callback_data="admin_panel")]])
                )
                user_states.pop(user_id, None)
                return

            purchased_accs = await accounts_col.find({"sold_to": target_id, "status": "SOLD"}).to_list(length=100)

            history_text = ""
            if purchased_accs:
                history_text = "\n\n📦 **Purchased Accounts History:**\n"
                for idx, acc in enumerate(purchased_accs, 1):
                    flag = get_flag(acc.get('country', ''))
                    history_text += f"{idx}. `{acc.get('phone_number')}` | {acc.get('category')} ({flag} {acc.get('country')} {acc.get('year')}) | ₹{acc.get('price', 0.0):.2f}\n"
            else:
                history_text = "\n\n📦 **Purchased Accounts History:** No accounts purchased yet."

            status_ban = "🚫 Banned" if u_data.get("is_banned", False) else "🟢 Active"
            ban_reason = f"\n⚠️ **Ban Reason:** {u_data.get('ban_reason')}" if u_data.get("is_banned", False) else ""

            info_msg = (
                f"👤 **USER DETAILED INFO & HISTORY**\n\n"
                f"🆔 **User ID:** `{target_id}`\n"
                f"📌 **Account Status:** {status_ban}{ban_reason}\n"
                f"💵 **Wallet Balance:** ₹{u_data.get('balance', 0.0):.2f}\n"
                f"🎁 **Profile Cashback (Locked):** ₹{u_data.get('profile_cashback', 0.0):.2f}\n"
                f"💸 **Withdrawable Cashback:** ₹{u_data.get('withdraw_cashback', 0.0):.2f}\n"
                f"🛍️ **Total Accounts Bought:** {len(purchased_accs)}"
                f"{history_text}"
            )

            user_states.pop(user_id, None)
            await message.reply_text(info_msg, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Admin Panel", callback_data="admin_panel")]]))

        except ValueError:
            await message.reply_text("❌ Invalid User ID! Please enter numeric User ID only:")

    elif state == "ADM_STEP_MAINT_REASON":
        if user_id not in SUDO_USERS: return
        new_reason = message.text.strip()
        is_active, _ = await get_maintenance_status()
        await set_maintenance_status(is_active, new_reason)
        user_states.pop(user_id, None)
        kb = await get_maintenance_panel_keyboard()
        await message.reply_text(f"✅ **Maintenance Reason Updated!**\n\n`{new_reason}`", reply_markup=kb)

    elif state == "WAIT_WITHDRAW_AMOUNT":
        try:
            amount = float(message.text.strip())
            cb = await get_withdraw_cashback(user_id)

            if amount < MIN_WITHDRAW:
                await message.reply_text(f"❌ Minimum withdrawal is ₹{MIN_WITHDRAW:.2f}:")
                return

            if amount > cb:
                bal = await get_user_balance(user_id)
                await message.reply_text(
                    f"❌ **Insufficient Cashback Balance!**\n\n"
                    f"🎁 Available Withdrawable Cashback: **₹{cb:.2f}**\n"
                    f"💰 Wallet Balance (NOT withdrawable): ₹{bal:.2f}\n\n"
                    f"ℹ️ _Withdrawals are only allowed from your **Cashback Balance** — "
                    f"your deposit/wallet balance can NOT be withdrawn._"
                )
                return

            temp_data[user_id] = {"withdraw_amount": amount}
            user_states[user_id] = "WAIT_WITHDRAW_QR"
            await message.reply_text("📸 **Now send your Payment QR Code Photo:**")

        except ValueError:
            await message.reply_text("❌ Enter numbers only:")

    elif state == "ADM_STEP_WAIT_NEW_PRICE":
        if user_id != OWNER_ID:
            await message.reply_text("🚫 Only Owner can change prices!")
            return

        try:
            new_price = float(message.text.strip())
            c_info = temp_data[user_id]
            cat, country, year = c_info["chg_cat"], c_info["chg_country"], c_info["chg_year"]

            res = await accounts_col.update_many(
                {"category": cat, "country": country, "year": year, "status": "AVAILABLE"},
                {"$set": {"price": new_price}}
            )

            user_states.pop(user_id, None)
            temp_data.pop(user_id, None)

            flag = get_flag(country)
            await message.reply_text(
                f"✅ **Price Updated!**\n\nUpdated price for `{cat}` ({flag} {country} {year}) to **₹{new_price:.2f}**.",
                reply_markup=get_admin_panel_keyboard(user_id)
            )
        except ValueError:
            await message.reply_text("❌ Price must be a valid number! Try again:")

    elif state == "ADM_STEP_WAIT_NEW_CASHBACK":
        if user_id != OWNER_ID:
            await message.reply_text("🚫 Only Owner can change cashback!")
            return

        try:
            new_cb = float(message.text.strip())
            c_info = temp_data[user_id]
            cat, country, year = c_info["chg_cat"], c_info["chg_country"], c_info["chg_year"]

            res = await accounts_col.update_many(
                {"category": cat, "country": country, "year": year, "status": "AVAILABLE"},
                {"$set": {"cashback": new_cb}}
            )

            user_states.pop(user_id, None)
            temp_data.pop(user_id, None)

            flag = get_flag(country)
            await message.reply_text(
                f"✅ **Cashback Updated!**\n\nSet cashback for `{cat}` ({flag} {country} {year}) to **₹{new_cb:.2f}**.",
                reply_markup=get_admin_panel_keyboard(user_id)
            )
        except ValueError:
            await message.reply_text("❌ Cashback must be a valid number! Try again:")

    elif state == "ADM_STEP_EDIT_BAL":
        if user_id != OWNER_ID:
            await message.reply_text("🚫 Only Owner can edit balance!")
            return

        try:
            parts = message.text.strip().split()
            if len(parts) != 2:
                await message.reply_text("❌ Invalid Format! Use: `UserID BalanceToAdd`")
                return

            t_user_id = int(parts[0])
            add_amount = float(parts[1])

            await update_balance(t_user_id, add_amount)
            new_total = await get_user_balance(t_user_id)

            user_states.pop(user_id, None)
            await message.reply_text(
                f"✅ **Balance Added Successfully!**\n\n"
                f"👤 User ID: `{t_user_id}`\n"
                f"➕ Added Amount: ₹{add_amount:.2f}\n"
                f"💰 New Balance: ₹{new_total:.2f}",
                reply_markup=get_admin_panel_keyboard(user_id)
            )
        except ValueError:
            await message.reply_text("❌ Check your input numbers!")

    elif state == "ADM_STEP_INPUT_ADD_SUDO":
        if user_id != OWNER_ID: return
        try:
            target_id = int(message.text.strip())
            await add_sudo_user(target_id)
            user_states.pop(user_id, None)
            kb = await get_manage_sudo_keyboard()
            await message.reply_text(f"✅ User `{target_id}` added to Admins!", reply_markup=kb)
        except ValueError:
            await message.reply_text("❌ Invalid User ID! Enter numbers only:")

    elif state == "ADM_STEP_BROADCAST":
        user_states.pop(user_id, None)
        broadcast_msg = message.text
        cursor = users_col.find({"is_banned": False})
        users = await cursor.to_list(length=10000)

        success = 0
        failed = 0
        await message.reply_text(f"⏳ **Starting broadcast to {len(users)} users...**")

        for u in users:
            try:
                await app.send_message(u["user_id"], broadcast_msg)
                success += 1
                await asyncio.sleep(0.05)
            except Exception:
                failed += 1

        await message.reply_text(f"✅ **Broadcast Completed!**\n\n🟢 Delivered: {success}\n🔴 Failed: {failed}", reply_markup=get_admin_panel_keyboard(user_id))

    elif state == "ADM_STEP_BAN_ID":
        try:
            target_user = (await client.get_users(message.text.strip())).id

            if target_user == OWNER_ID or target_user in SUDO_USERS:
                await message.reply_text("❌ You cannot ban yourself or another admin.")
                user_states.pop(user_id, None)
                return

            temp_data[user_id] = {"target_ban_user": target_user}
            user_states[user_id] = "ADM_STEP_BAN_REASON"
            await message.reply_text(f"👤 Target User ID: `{target_user}`\n\n📝 **Enter Reason for Ban:**")
        except Exception:
            await message.reply_text("❌ Invalid User ID or Username:")

    elif state == "ADM_STEP_BAN_REASON":
        reason = message.text.strip()
        target_user = temp_data[user_id]["target_ban_user"]
        user_states.pop(user_id, None)

        await users_col.update_one({"user_id": target_user}, {"$set": {"is_banned": True, "ban_reason": reason}}, upsert=True)
        await message.reply_text(f"🚫 User `{target_user}` banned.\n**Reason:** {reason}", reply_markup=get_admin_panel_keyboard(user_id))

    elif state == "ADM_STEP_UNBAN_ID":
        try:
            target_user = (await client.get_users(message.text.strip())).id
            user_states.pop(user_id, None)
            await users_col.update_one({"user_id": target_user}, {"$set": {"is_banned": False, "ban_reason": ""}})
            await message.reply_text(f"🟢 User `{target_user}` is now Unbanned.", reply_markup=get_admin_panel_keyboard(user_id))
        except Exception:
            await message.reply_text("❌ Invalid User ID or Username:")

    elif state == "ADM_STEP_COUNTRY":
        c_input = message.text.strip()
        temp_data[user_id]["country"] = c_input
        user_states[user_id] = "ADM_STEP_YEAR"
        flag = get_flag(c_input)
        await message.reply_text(f"{flag} **Step 2:** Enter Account Creation Year (e.g. `2022`, `2024`):")

    elif state == "ADM_STEP_YEAR":
        temp_data[user_id]["year"] = message.text.strip()
        user_states[user_id] = "ADM_STEP_PRICE"
        await message.reply_text("💵 **Step 3:** Enter Account Price (₹):")

    elif state == "ADM_STEP_PRICE":
        try:
            temp_data[user_id]["price"] = float(message.text.strip())
            user_states[user_id] = "ADM_STEP_CASHBACK_VAL"
            await message.reply_text("🎁 **Enter Cashback Amount (If none, type `0`):**")
        except ValueError:
            await message.reply_text("❌ Please enter numbers only:")

    elif state == "ADM_STEP_CASHBACK_VAL":
        try:
            temp_data[user_id]["cashback"] = float(message.text.strip())
            user_states[user_id] = "ADM_STEP_PHONE"
            await message.reply_text("📞 **Enter Account Phone Number (with Country Code e.g. `+1234567890`):**")
        except ValueError:
            await message.reply_text("❌ Please enter numbers only:")

    elif state == "ADM_STEP_PHONE":
        temp_data[user_id]["phone"] = message.text.strip()
        user_states[user_id] = "ADM_STEP_2FA"
        await message.reply_text("🔑 **Enter 2FA Password (If none, type `None`):**")

    elif state == "ADM_STEP_2FA":
        temp_data[user_id]["two_fa"] = message.text.strip()
        phone = temp_data[user_id]["phone"]

        await message.reply_text(f"⏳ Triggering Telegram OTP request to `{phone}`...")
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

            acc_doc = {
                "category": data["category"],
                "country": data["country"],
                "year": data["year"],
                "price": data["price"],
                "cashback": data["cashback"],
                "phone_number": data["phone"],
                "session_string": session_str,
                "two_fa": data["two_fa"],
                "status": "AVAILABLE",
                "sold_to": None
            }
            await accounts_col.insert_one(acc_doc)

            user_states.pop(user_id, None)
            flag = get_flag(data['country'])
            await message.reply_text(
                f"✅ **Account Added to MongoDB Stock!**\n\n"
                f"📂 **Category:** {data['category']}\n"
                f"{flag} **Location:** {data['country']} ({data['year']})\n"
                f"📞 **Phone:** `{data['phone']}`",
                reply_markup=get_admin_panel_keyboard(user_id)
            )

        except Exception as e:
            await message.reply_text(f"❌ Error during sign in: `{e}`")

# ==================== START SERVER ====================
if __name__ == "__main__":
    threading.Thread(target=_email_watcher_loop, daemon=True).start()   # email watcher alag thread mein
    loop = asyncio.get_event_loop()
    loop.run_until_complete(init_db())
    print("🚀 Mongo Engine Activated!")
    app.run()
