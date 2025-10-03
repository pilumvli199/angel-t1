import os
import time
import threading
import logging
from flask import Flask, jsonify
import pyotp
import requests
from bs4 import BeautifulSoup

# ---- SmartAPI import (FIXED) ----
SmartConnect = None
try:
    from SmartApi import SmartConnect as _SC
    SmartConnect = _SC
    logging.info("SmartConnect imported successfully!")
except Exception as e:
    logging.error(f"Failed to import SmartConnect: {e}")
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

def get_index_data_nseindia():
    """Scrape NIFTY and BANKNIFTY from NSE India website"""
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'application/json'
        }
        
        # NSE API endpoint for indices
        url = "https://www.nseindia.com/api/allIndices"
        
        session = requests.Session()
        # First request to get cookies
        session.get("https://www.nseindia.com", headers=headers, timeout=10)
        
        # Get indices data
        response = session.get(url, headers=headers, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            result = {}
            
            for item in data.get('data', []):
                index_name = item.get('index', '')
                if 'NIFTY 50' in index_name or index_name == 'NIFTY 50':
                    result['NIFTY 50'] = float(item.get('last', 0))
                elif 'NIFTY BANK' in index_name or index_name == 'NIFTY BANK':
                    result['NIFTY BANK'] = float(item.get('last', 0))
            
            return result
        else:
            logger.warning(f"NSE API returned status {response.status_code}")
            return None
            
    except Exception as e:
        logger.exception(f"Failed to fetch NSE data: {e}")
        return None

def get_index_data_yahoo():
    """Fallback: Get data from Yahoo Finance"""
    try:
        result = {}
        
        # NIFTY 50
        url_nifty = "https://query1.finance.yahoo.com/v8/finance/chart/%5ENSEI"
        resp = requests.get(url_nifty, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            price = data['chart']['result'][0]['meta']['regularMarketPrice']
            result['NIFTY 50'] = float(price)
        
        # NIFTY BANK
        url_bank = "https://query1.finance.yahoo.com/v8/finance/chart/%5ENSEBANK"
        resp = requests.get(url_bank, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            price = data['chart']['result'][0]['meta']['regularMarketPrice']
            result['NIFTY BANK'] = float(price)
        
        return result if result else None
        
    except Exception as e:
        logger.exception(f"Failed to fetch Yahoo data: {e}")
        return None

def bot_loop():
    if not all(REQUIRED):
        logger.error('Missing required environment variables. Bot will not start.')
        logger.error('Ensure SMARTAPI_API_KEY, SMARTAPI_CLIENT_ID, SMARTAPI_PASSWORD, SMARTAPI_TOTP_SECRET, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID are set.')
        return

    # Login to keep session active (even if not using for data)
    try:
        smartApi, authToken, refreshToken, feedToken = login_and_setup(API_KEY, CLIENT_ID, PASSWORD, TOTP_SECRET)
        logger.info("Login successful - using public data sources for indices")
    except Exception as e:
        logger.exception('Login/setup failed: %s', e)
        tele_send_http(TELE_CHAT_ID, f'Login failed: {e}. Will try public data sources.')

    tele_send_http(TELE_CHAT_ID, f"✅ Bot started! Polling every {POLL_INTERVAL}s for: NIFTY 50, NIFTY BANK\n\n📊 Using NSE India & Yahoo Finance for real-time data")

    while True:
        try:
            # Try NSE India first
            prices = get_index_data_nseindia()
            source = "NSE India"
            
            # Fallback to Yahoo Finance
            if not prices:
                logger.warning("NSE India failed, trying Yahoo Finance...")
                prices = get_index_data_yahoo()
                source = "Yahoo Finance"
            
            if prices:
                messages = []
                ts = time.strftime('%Y-%m-%d %H:%M:%S')
                
                for name in ['NIFTY 50', 'NIFTY BANK']:
                    ltp = prices.get(name)
                    if ltp:
                        messages.append(f"📈 <b>{name}</b>: ₹{ltp:,.2f}")
                    else:
                        messages.append(f"📈 <b>{name}</b>: Data unavailable")
                
                messages.append(f"\n🕐 {ts}")
                messages.append(f"📡 Source: {source}")
                
                text = "\n".join(messages)
                logger.info('Sending update: %s', text)
                tele_send_http(TELE_CHAT_ID, text)
            else:
                logger.error("All data sources failed!")
                tele_send_http(TELE_CHAT_ID, "⚠️ Unable to fetch index data from any source")
            
        except Exception as e:
            logger.exception(f"Error in bot loop: {e}")
            tele_send_http(TELE_CHAT_ID, f"⚠️ Error: {e}")
        
        time.sleep(POLL_INTERVAL)

# Start bot in a background thread at import time so Gunicorn/Procfile runs it.
thread = threading.Thread(target=bot_loop, daemon=True)
thread.start()

@app.route('/')
def index():
    status = {
        'bot_thread_alive': thread.is_alive(),
        'poll_interval': POLL_INTERVAL,
        'smartapi_sdk_available': SmartConnect is not None,
        'data_source': 'NSE India / Yahoo Finance'
    }
    return jsonify(status)

if __name__ == '__main__':
    # run locally for debugging
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 8080)))
