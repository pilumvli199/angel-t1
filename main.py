import os
import time
import threading
import logging
from flask import Flask, jsonify
import pyotp

# ---- SmartAPI import fallback ----
SmartConnect = None
try:
    from SmartApi.smartConnect import SmartConnect as _SC
    SmartConnect = _SC
except Exception:
    try:
        from smartapi import SmartConnect as _SC2
        SmartConnect = _SC2
    except Exception:
        SmartConnect = None

from telegram import Bot

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger('angel-railway-bot')

# Load config from env
API_KEY = os.getenv('SMARTAPI_API_KEY')
CLIENT_ID = os.getenv('SMARTAPI_CLIENT_ID')
PASSWORD = os.getenv('SMARTAPI_PASSWORD')
TOTP_SECRET = os.getenv('SMARTAPI_TOTP_SECRET')
TELE_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELE_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
POLL_INTERVAL = int(os.getenv('POLL_INTERVAL') or 60)

REQUIRED = [API_KEY, CLIENT_ID, PASSWORD, TOTP_SECRET, TELE_TOKEN, TELE_CHAT_ID]

app = Flask(__name__)

def tele_send(bot: Bot, chat_id: str, text: str):
    """Send Telegram message synchronously (works with python-telegram-bot v13.x)."""
    try:
        bot.send_message(chat_id=chat_id, text=text)
    except Exception as e:
        logger.exception('Telegram send failed: %s', e)

def login_and_setup(api_key, client_id, password, totp_secret):
    if SmartConnect is None:
        raise RuntimeError('SmartAPI SDK not available. Check requirements.txt installation.')
    smartApi = SmartConnect(api_key=api_key)
    totp = pyotp.TOTP(totp_secret).now()
    logger.info('Logging in to SmartAPI...')
    data = smartApi.generateSession(client_id, password, totp)
    if not data or data.get('status') is False:
        raise RuntimeError(f"Login failed: {data}")
    authToken = data['data']['jwtToken']
    refreshToken = data['data']['refreshToken']
    try:
        feedToken = smartApi.getfeedToken()
    except Exception:
        feedToken = None
    try:
        smartApi.generateToken(refreshToken)
    except Exception:
        pass
    return smartApi, authToken, refreshToken, feedToken

def find_symboltoken_for_query(smartApi, query):
    try:
        res = smartApi.searchScrip(query)
    except TypeError:
        try:
            res = smartApi.searchScrip('NSE', query)
        except Exception:
            return None
    except Exception:
        return None
    try:
        candidates = res.get('data') if isinstance(res, dict) else res
        if not candidates: 
            return None
        first = candidates[0]
        token = first.get('symboltoken') or first.get('token')
        tsym = first.get('tradingsymbol') or first.get('symbol')
        return {'symboltoken': str(token), 'tradingsymbol': tsym}
    except Exception:
        return None

def get_ltp(smartApi, exchange, tradingsymbol, symboltoken):
    try:
        data = smartApi.ltpData(exchange, tradingsymbol, symboltoken)
        if isinstance(data, dict) and data.get('status') is not False:
            d = data.get('data') if isinstance(data.get('data'), dict) else data
            ltp = None
            if isinstance(d, dict):
                ltp = d.get('ltp') or d.get('last_price')
            return float(ltp) if ltp else None
    except Exception:
        pass
    return None

def bot_loop():
    if not all(REQUIRED):
        logger.error('Missing env vars, bot will not start.')
        return
    bot = Bot(token=TELE_TOKEN)
    try:
        smartApi, *_ = login_and_setup(API_KEY, CLIENT_ID, PASSWORD, TOTP_SECRET)
    except Exception as e:
        tele_send(bot, TELE_CHAT_ID, f'Login failed: {e}')
        return

    targets = ['NIFTY 50', 'SENSEX']
    found = {}
    for t in targets:
        info = find_symboltoken_for_query(smartApi, t)
        if info:
            found[t] = info
        else:
            tele_send(bot, TELE_CHAT_ID, f'Could not find token for {t}')

    if not found:
        tele_send(bot, TELE_CHAT_ID, 'No symbols found; bot stopped.')
        return

    tele_send(bot, TELE_CHAT_ID, f"Bot started. Polling every {POLL_INTERVAL}s for: {', '.join(found.keys())}")

    while True:
        ts = time.strftime('%Y-%m-%d %H:%M:%S')
        msgs = []
        for name, info in found.items():
            ltp = get_ltp(smartApi, 'NSE', info['tradingsymbol'], info['symboltoken'])
            msgs.append(f"{ts} | {name}: {ltp if ltp else 'N/A'}")
        tele_send(bot, TELE_CHAT_ID, "\n".join(msgs))
        time.sleep(POLL_INTERVAL)

# Run bot loop in background
thread = threading.Thread(target=bot_loop, daemon=True)
thread.start()

@app.route('/')
def index():
    return jsonify({
        'bot_thread_alive': thread.is_alive(),
        'poll_interval': POLL_INTERVAL,
        'smartapi_sdk_available': SmartConnect is not None
    })

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 8080)))
