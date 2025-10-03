import os
import time
import threading
import logging
from flask import Flask, jsonify
import pyotp
import requests

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

def get_market_data_angel(smartApi):
    """Get live index data using Angel One Market Data API"""
    try:
        # NIFTY 50 and BANKNIFTY tokens
        result = {}
        
        # Method 1: Try getMarketData if available in SDK
        if hasattr(smartApi, 'getMarketData'):
            try:
                # NIFTY 50 - token 99926000
                nifty_data = smartApi.getMarketData('LTP', {'NSE': ['99926000']})
                logger.info(f"NIFTY raw response: {nifty_data}")
                
                if nifty_data and nifty_data.get('status'):
                    fetched = nifty_data.get('data', {}).get('fetched', [])
                    if fetched and len(fetched) > 0:
                        result['NIFTY 50'] = float(fetched[0].get('ltp', 0))
                
                # BANKNIFTY - token 99926009
                bank_data = smartApi.getMarketData('LTP', {'NSE': ['99926009']})
                logger.info(f"BANKNIFTY raw response: {bank_data}")
                
                if bank_data and bank_data.get('status'):
                    fetched = bank_data.get('data', {}).get('fetched', [])
                    if fetched and len(fetched) > 0:
                        result['NIFTY BANK'] = float(fetched[0].get('ltp', 0))
                
                if result:
                    return result
            except Exception as e:
                logger.warning(f"getMarketData method failed: {e}")
        
        # Method 2: Direct API call
        headers = {
            'Authorization': f'Bearer {smartApi.access_token}',
            'Content-Type': 'application/json',
            'Accept': 'application/json',
            'X-UserType': 'USER',
            'X-SourceID': 'WEB',
            'X-ClientLocalIP': '127.0.0.1',
            'X-ClientPublicIP': '127.0.0.1',
            'X-MACAddress': '00:00:00:00:00:00',
            'X-PrivateKey': API_KEY
        }
        
        # Get NIFTY 50
        payload_nifty = {
            "mode": "LTP",
            "exchangeTokens": {
                "NSE": ["99926000"]
            }
        }
        
        response = requests.post(
            'https://apiconnect.angelbroking.com/rest/secure/angelbroking/market/v1/quote/',
            json=payload_nifty,
            headers=headers,
            timeout=10
        )
        
        logger.info(f"Direct API NIFTY response: {response.status_code} - {response.text}")
        
        if response.status_code == 200:
            data = response.json()
            if data.get('status'):
                fetched = data.get('data', {}).get('fetched', [])
                if fetched:
                    result['NIFTY 50'] = float(fetched[0].get('ltp', 0))
        
        # Get BANKNIFTY
        payload_bank = {
            "mode": "LTP",
            "exchangeTokens": {
                "NSE": ["99926009"]
            }
        }
        
        response = requests.post(
            'https://apiconnect.angelbroking.com/rest/secure/angelbroking/market/v1/quote/',
            json=payload_bank,
            headers=headers,
            timeout=10
        )
        
        logger.info(f"Direct API BANKNIFTY response: {response.status_code} - {response.text}")
        
        if response.status_code == 200:
            data = response.json()
            if data.get('status'):
                fetched = data.get('data', {}).get('fetched', [])
                if fetched:
                    result['NIFTY BANK'] = float(fetched[0].get('ltp', 0))
        
        return result if result else None
        
    except Exception as e:
        logger.exception(f"Failed to fetch Angel market data: {e}")
        return None

def bot_loop():
    if not all(REQUIRED):
        logger.error('Missing required environment variables. Bot will not start.')
        return

    try:
        smartApi, authToken, refreshToken, feedToken = login_and_setup(API_KEY, CLIENT_ID, PASSWORD, TOTP_SECRET)
        logger.info("✅ Login successful!")
    except Exception as e:
        logger.exception('Login/setup failed: %s', e)
        tele_send_http(TELE_CHAT_ID, f'❌ Login failed: {e}')
        return

    tele_send_http(TELE_CHAT_ID, f"✅ Bot started! Polling every {POLL_INTERVAL}s\n📊 Using Angel One Market Data API")

    while True:
        try:
            # Get live data from Angel One
            prices = get_market_data_angel(smartApi)
            
            if prices and any(prices.values()):
                messages = []
                ts = time.strftime('%Y-%m-%d %H:%M:%S')
                
                for name in ['NIFTY 50', 'NIFTY BANK']:
                    ltp = prices.get(name, 0)
                    if ltp and ltp > 0:
                        messages.append(f"📈 <b>{name}</b>: ₹{ltp:,.2f}")
                    else:
                        messages.append(f"📈 <b>{name}</b>: Data unavailable")
                
                messages.append(f"\n🕐 {ts}")
                messages.append(f"📡 Source: Angel One API")
                
                text = "\n".join(messages)
                logger.info('Sending update')
                tele_send_http(TELE_CHAT_ID, text)
            else:
                logger.error("No data received from Angel API")
                tele_send_http(TELE_CHAT_ID, "⚠️ Unable to fetch data from Angel One")
            
        except Exception as e:
            logger.exception(f"Error in bot loop: {e}")
            tele_send_http(TELE_CHAT_ID, f"⚠️ Error: {e}")
        
        time.sleep(POLL_INTERVAL)

# Start bot in a background thread
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
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 8080)))
