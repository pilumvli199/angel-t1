import os
import time
import threading
import logging
from flask import Flask, jsonify
import pyotp
import requests

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

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger('angel-railway-bot-http')

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

def tele_send_http(chat_id: str, text: str):
    """Send message using Telegram Bot HTTP API via requests (synchronous)."""
    try:
        token = TELE_TOKEN
        if not token:
            logger.error('TELEGRAM_BOT_TOKEN not set, cannot send Telegram message.')
            return False
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML"
        }
        r = requests.post(url, json=payload, timeout=10)
        if r.status_code != 200:
            logger.warning('Telegram API returned %s: %s', r.status_code, r.text)
            return False
        return True
    except Exception as e:
        logger.exception('Failed to send Telegram message: %s', e)
        return False

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
        except Exception as e:
            logger.exception('searchScrip fallback failed: %s', e)
            return None
    except Exception as e:
        logger.exception('searchScrip failed: %s', e)
        return None
    try:
        candidates = res.get('data') if isinstance(res, dict) and 'data' in res else res
        if not candidates:
            return None
        first = candidates[0]
        token = first.get('symboltoken') or first.get('token')
        tsym = first.get('tradingsymbol') or first.get('symbol') or first.get('symbolName')
        return {'symboltoken': str(token), 'tradingsymbol': tsym}
    except Exception:
        logger.exception('Parsing searchScrip response failed')
        return None

def get_ltp(smartApi, exchange, tradingsymbol, symboltoken):
    try:
        data = smartApi.ltpData(exchange, tradingsymbol, symboltoken)
        if isinstance(data, dict) and data.get('status') is not False:
            d = data.get('data') if isinstance(data.get('data'), dict) else data
            ltp = None
            if isinstance(d, dict):
                ltp = d.get('ltp') or d.get('last_price') or d.get('ltpValue')
            if ltp is None and isinstance(d, list) and len(d) > 0:
                entry = d[0]
                ltp = entry.get('ltp') or entry.get('last_price')
            return float(ltp) if ltp is not None else None
        else:
            logger.warning('ltpData returned unexpected: %s', data)
            return None
    except Exception:
        logger.exception('ltpData call failed')
        return None

def bot_loop():
    if not all(REQUIRED):
        logger.error('Missing required environment variables. Bot will not start.')
        logger.error('Ensure SMARTAPI_API_KEY, SMARTAPI_CLIENT_ID, SMARTAPI_PASSWORD, SMARTAPI_TOTP_SECRET, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID are set.')
        return

    try:
        smartApi, authToken, refreshToken, feedToken = login_and_setup(API_KEY, CLIENT_ID, PASSWORD, TOTP_SECRET)
    except Exception as e:
        logger.exception('Login/setup failed: %s', e)
        tele_send_http(TELE_CHAT_ID, f'Login failed: {e}')
        return

    targets = ['NIFTY 50', 'SENSEX']
    found = {}
    for t in targets:
        info = find_symboltoken_for_query(smartApi, t)
        if not info:
            logger.warning('Could not find symbol for %s', t)
            tele_send_http(TELE_CHAT_ID, f'Could not find symbol token for {t}.')
        else:
            found[t] = info
            logger.info('Found %s -> %s', t, info)

    if not found:
        logger.error('No symbols found. Exiting bot loop.')
        tele_send_http(TELE_CHAT_ID, 'No symbols found; bot stopped.')
        return

    tele_send_http(TELE_CHAT_ID, f"Bot started. Polling every {POLL_INTERVAL}s for: {', '.join(found.keys())}")

    while True:
        messages = []
        ts = time.strftime('%Y-%m-%d %H:%M:%S')
        for name, info in found.items():
            ltp = get_ltp(smartApi, 'NSE', info.get('tradingsymbol') or '', info.get('symboltoken') or '')
            if ltp is None:
                messages.append(f"{ts} | {name}: LTP not available")
            else:
                messages.append(f"{ts} | {name}: {ltp}")
        text = "\n".join(messages)
        logger.info('Sending message:\\n%s', text)
        tele_send_http(TELE_CHAT_ID, text)
        time.sleep(POLL_INTERVAL)

# Start bot in a background thread at import time so Gunicorn/Procfile runs it.
thread = threading.Thread(target=bot_loop, daemon=True)
thread.start()

@app.route('/')
def index():
    status = {
        'bot_thread_alive': thread.is_alive(),
        'poll_interval': POLL_INTERVAL,
        'smartapi_sdk_available': SmartConnect is not None
    }
    return jsonify(status)

if __name__ == '__main__':
    # run locally for debugging
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 8080)))
