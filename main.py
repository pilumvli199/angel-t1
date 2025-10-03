import os
import time
import threading
import logging
from flask import Flask, jsonify
import pyotp
import requests

# ---- SmartAPI import (FIXED) ----
SmartConnect = None
SmartWebSocketV2 = None
try:
    from SmartApi import SmartConnect as _SC
    from SmartApi.smartWebSocketV2 import SmartWebSocketV2 as _WS
    SmartConnect = _SC
    SmartWebSocketV2 = _WS
    logging.info("SmartConnect and WebSocket imported successfully!")
except Exception as e:
    logging.error(f"Failed to import SmartConnect: {e}")
    SmartConnect = None
    SmartWebSocketV2 = None

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

# Global storage for latest prices
latest_prices = {}
ws_connected = False

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

def setup_websocket(auth_token, api_key, client_id, feed_token):
    """Setup WebSocket for real-time index data"""
    global ws_connected, latest_prices
    
    if SmartWebSocketV2 is None:
        logger.error("WebSocket not available")
        return None
    
    # Index tokens for subscription
    token_list = [
        {
            "exchangeType": 2,  # NSE indices
            "tokens": ["99926000", "99926009"]  # NIFTY 50, BANKNIFTY
        }
    ]
    
    sws = SmartWebSocketV2(auth_token, api_key, client_id, feed_token)
    
    def on_data(wsapp, message):
        global latest_prices
        try:
            # Log raw message to debug
            logger.info(f"WebSocket raw message: {message}")
            
            # Parse websocket message - handle binary data
            if isinstance(message, bytes):
                # Binary message - need to decode
                import struct
                # Angel WebSocket sends binary packed data
                # Format varies, log first to understand structure
                logger.info(f"Binary message length: {len(message)}, hex: {message[:50].hex()}")
                return
            
            if isinstance(message, dict):
                token = str(message.get('token', ''))
                # Try different field names
                ltp = (message.get('last_traded_price') or 
                       message.get('ltp') or 
                       message.get('last_price') or
                       message.get('c'))
                
                logger.info(f"Parsed: token={token}, ltp={ltp}, full={message}")
                
                if token == "99926000" and ltp:  # NIFTY 50
                    latest_prices['NIFTY 50'] = float(ltp) / 100
                elif token == "99926009" and ltp:  # BANKNIFTY
                    latest_prices['NIFTY BANK'] = float(ltp) / 100
                
        except Exception as e:
            logger.exception(f"Error parsing websocket data: {e}")
    
    def on_open(wsapp):
        global ws_connected
        logger.info("WebSocket connected!")
        ws_connected = True
        sws.subscribe("correlation_id", 1, token_list)
    
    def on_error(wsapp, error):
        global ws_connected
        logger.error(f"WebSocket error: {error}")
        ws_connected = False
    
    def on_close(wsapp):
        global ws_connected
        logger.info("WebSocket closed")
        ws_connected = False
    
    sws.on_open = on_open
    sws.on_data = on_data
    sws.on_error = on_error
    sws.on_close = on_close
    
    return sws

def bot_loop():
    global ws_connected, latest_prices
    
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

    # Setup WebSocket
    logger.info("Setting up WebSocket for real-time data...")
    sws = setup_websocket(authToken, API_KEY, CLIENT_ID, feedToken)
    
    if sws:
        try:
            # Start WebSocket in separate thread
            ws_thread = threading.Thread(target=sws.connect, daemon=True)
            ws_thread.start()
            time.sleep(3)  # Wait for connection
        except Exception as e:
            logger.error(f"Failed to start WebSocket: {e}")
            tele_send_http(TELE_CHAT_ID, f'WebSocket failed: {e}')
            return
    
    if not ws_connected:
        logger.error("WebSocket connection failed")
        tele_send_http(TELE_CHAT_ID, 'WebSocket connection failed. Bot stopped.')
        return

    tele_send_http(TELE_CHAT_ID, f"Bot started with WebSocket! Polling every {POLL_INTERVAL}s for: NIFTY 50, NIFTY BANK")

    while True:
        messages = []
        ts = time.strftime('%Y-%m-%d %H:%M:%S')
        
        for name in ['NIFTY 50', 'NIFTY BANK']:
            ltp = latest_prices.get(name)
            if ltp is None:
                messages.append(f"{ts} | {name}: Waiting for data...")
            else:
                messages.append(f"{ts} | {name}: {ltp:.2f}")
        
        # Add WebSocket status
        ws_status = "✅ Connected" if ws_connected else "❌ Disconnected"
        messages.append(f"WebSocket: {ws_status}")
        
        text = "\n".join(messages)
        logger.info('Sending message:\n%s', text)
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
        'smartapi_sdk_available': SmartConnect is not None,
        'websocket_available': SmartWebSocketV2 is not None,
        'websocket_connected': ws_connected,
        'latest_prices': latest_prices
    }
    return jsonify(status)

if __name__ == '__main__':
    # run locally for debugging
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 8080)))
