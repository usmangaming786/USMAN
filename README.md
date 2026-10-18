# 📱 Telegram Bulk SMS Dispatcher Bot

Bulk SMS distribution system built with Python, Telegram Bot API, and Firebase Realtime Database.

---

## ⚡ How It Works

```
[ Telegram User ]
       │
       ▼ (Sends SMS text + Numbers list)
[ Telegram Bot (bot.py) ]
       │
       ▼ (Distributes targets evenly across online devices)
[ Firebase RTDB: PUT /clients/{deviceId}/sendSms.json ]
       │
       ▼ (Realtime event listener in Android App)
[ Android Phones (SmsManager) ] ──> Sends SMS to Targets!
```

---

## 🚀 Quick Setup

1. **Get a Telegram Bot Token**:
   - Open Telegram, search for `@BotFather`.
   - Send `/newbot` and follow instructions.
   - Copy the HTTP API token.

2. **Configure [config.py](file:///C:/Users/MANSI/.gemini/antigravity-ide/scratch/sms-bot/config.py)**:
   - Paste your token into `BOT_TOKEN`:
     ```python
     BOT_TOKEN = "1234567890:ABCdefGHIjklMNOpqrSTUvwxYZ"
     ```
   - Your Firebase Realtime DB URL is already pre-configured:
     ```python
     DEFAULT_FIREBASE_URL = "https://berlinxmirrorfinalboss-default-rtdb.firebaseio.com"
     ```

3. **Start the Bot**:
   - Double-click [`start.bat`](file:///C:/Users/MANSI/.gemini/antigravity-ide/scratch/sms-bot/start.bat) or run:
     ```bash
     python bot.py
     ```

---

## 🕹️ Bot Commands & Features

| Command | Action |
|---|---|
| `/start` | Open main dashboard with interactive buttons |
| `/send` | Launch step-by-step Bulk SMS Campaign wizard |
| `/devices` | View all online Android devices, battery, and SIM carrier details |
| `/seturl` | Change Firebase Realtime Database URL dynamically |
| `/cancel` | Cancel current wizard / action |

---

## 📋 Campaign Wizard Flow

1. **Online Worker Verification**:
   - Bot automatically checks Firebase RTDB.
   - Detects all devices with `status: true` (e.g. TECNO, Vivo, Moto, Oppo).
2. **SMS Template**:
   - Send your exact SMS text.
   - Shows character counter and estimated SMS parts (160 chars/part).
3. **Numbers List**:
   - Send numbers directly in chat or upload a `.txt` file.
   - Automatically cleans whitespace, dashes, duplicates, and normalizes Indian format (+91).
4. **Distribution & Settings**:
   - Select SIM Slot 0 (SIM 1) or Slot 1 (SIM 2).
   - Select throttle delay (1s, 2s, 5s) to avoid carrier blocks.
   - Evenly balances numbers across all available online phones.
5. **Live Blast Progress**:
   - Realtime visual progress bar: `[████████░░░░] 60%`
   - Dispatched count, failed count, remaining targets.
   - `🛑 STOP CAMPAIGN` button to pause/abort anytime.
