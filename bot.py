import time
import os
import json
import ccxt
import pandas as pd
import pandas_ta as ta
from twilio.rest import Client

# --- Secure Configuration Layer (Reads directly from Render Environment Variables) ---
TWILIO_SID    = os.getenv('TWILIO_SID')
TWILIO_TOKEN  = os.getenv('TWILIO_TOKEN')
FROM_WHATSAPP = 'whatsapp:+14155238886'               # Default Twilio Sandbox Number
TO_WHATSAPP   = os.getenv('MY_WHATSAPP_NUMBER')      # e.g., whatsapp:+91XXXXXXXXXX

# --- Strategy Constants & Inputs (Optimized Timeframes) ---
SYMBOL = 'BTC/USDT:USDT'
LTF_TIMEFRAME = '15m'  # Micro Execution Chart (15 Minutes)
HTF_TIMEFRAME = '1h'   # Macro Trend Filter (1 Hour)

RSI_LENGTH = 14
HTF_SMOOTH_LEN = 9

LONG_FLOOR = 30
LONG_TRIGGER = 35
SHORT_CEILING = 70
SHORT_TRIGGER = 65

STOP_PERCENT = 2.5
BREAK_EVEN_TRIGGER = 1.5
ATR_VOLUME_PERIOD = 20

STATE_FILE = "bot_state.json"

# --- Delta Exchange India Configuration ---
exchange = ccxt.delta({
    'apiKey': os.getenv('DELTA_API_KEY'),
    'secret': os.getenv('DELTA_SECRET_KEY'),
    'enableRateLimit': True,
    'urls': {
        'api': {
            'public': 'https://delta.exchange',
            'private': 'https://delta.exchange',
        }
    }
})

# --- WhatsApp Alert Core Engine ---
def send_whatsapp_alert(msg):
    try:
        # Check if Twilio configuration is present before attempting to send
        if not TWILIO_SID or not TWILIO_TOKEN or not TO_WHATSAPP:
            print(f"[WHATSAPP SKIP] Environment variables missing. Alert log: {msg}")
            return
            
        client = Client(TWILIO_SID, TWILIO_TOKEN)
        client.messages.create(
            body=f"🤖 *Delta Bot Alert*:\n{msg}",
            from_=FROM_WHATSAPP,
            to=TO_WHATSAPP
        )
        print(f"[WHATSAPP] Alert sent successfully: {msg}")
    except Exception as e:
        print(f"[WHATSAPP ERROR] Failed to send notification: {e}")

# --- Local State Persistence Engine ---
def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r') as f:
                return json.load(f)
        except Exception:
            pass
    return {"position_size": 0.0, "entry_price": None, "break_even_protected": False}

def save_state(state):
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f)

def get_market_data(symbol, timeframe, limit=200):
    bars = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    df = pd.DataFrame(bars, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
    return df

def execute_trade(side, quantity, order_type='market'):
    try:
        print(f"[ORDER] Submitting {side.upper()} order for {quantity} {SYMBOL}...")
        order = exchange.create_order(
            symbol=SYMBOL,
            type=order_type,
            side=side,
            amount=quantity
        )
        print(f"[SUCCESS] Order Executed ID: {order['id']}")
        return order
    except Exception as e:
        print(f"[ERROR] Trade execution failed: {e}")
        return None

def check_bars_since(series_condition, limit=10):
    for i in range(1, limit + 1):
        if series_condition.iloc[-i]:
            return i - 1
    return 999

def run_strategy():
    state = load_state()
    
    # 1. Fetch Market Matrix
    try:
        df_ltf = get_market_data(SYMBOL, LTF_TIMEFRAME, limit=100)
        df_htf = get_market_data(SYMBOL, HTF_TIMEFRAME, limit=100)
    except Exception as e:
        print(f"[ERROR] Connection / Data fetch error: {e}")
        return

    # 2. LTF Technical Metrics (15m)
    df_ltf['rsi'] = ta.rsi(df_ltf['close'], length=RSI_LENGTH)
    df_ltf['atr'] = ta.atr(df_ltf['high'], df_ltf['low'], df_ltf['close'], length=14)
    df_ltf['atr_avg'] = ta.sma(df_ltf['atr'], length=ATR_VOLUME_PERIOD)
    
    # 3. HTF Macro Metrics (1h)
    df_htf['rsi'] = ta.rsi(df_htf['close'], length=RSI_LENGTH)
    df_htf['rsi_smoothed'] = ta.ema(df_htf['rsi'], length=HTF_SMOOTH_LEN)
    
    latest_htf_smoothed = df_htf['rsi_smoothed'].iloc[-1]
    
    # 4. Extract Last Closed Values (Pine index [-2] equivalents)
    ltf_rsi = df_ltf['rsi'].iloc[-2]
    ltf_rsi_prior = df_ltf['rsi'].iloc[-3]
    atr_val = df_ltf['atr'].iloc[-2]
    atr_avg = df_ltf['atr_avg'].iloc[-2]
    
    # 5. Calculate Crossovers & Floor Ranges
    crossover_long = (ltf_rsi_prior <= LONG_TRIGGER) and (ltf_rsi > LONG_TRIGGER)
    crossunder_short = (ltf_rsi_prior >= SHORT_TRIGGER) and (ltf_rsi < SHORT_TRIGGER)
    
    condition_floor_long = df_ltf['rsi'] < LONG_FLOOR
    condition_ceiling_short = df_ltf['rsi'] > SHORT_CEILING
    
    bars_since_floor = check_bars_since(condition_floor_long, limit=12)
    bars_since_ceiling = check_bars_since(condition_ceiling_short, limit=12)
    
    # 6. Strategy Evaluation Blocks
    market_is_volatile = atr_val > (atr_avg * 0.85)
    bullish_macro = latest_htf_smoothed > 50
    bearish_macro = latest_htf_smoothed < 50
    
    long_snapback = crossover_long and (bars_since_floor < 10)
    short_snapback = crossunder_short and (bars_since_ceiling < 10)
    
    long_entry_condition = long_snapback and bullish_macro and market_is_volatile and (state["position_size"] <= 0)
    short_entry_condition = short_snapback and bearish_macro and market_is_volatile and (state["position_size"] >= 0)
    
    current_pos = state["position_size"]
    
    # 7. Risk Engine Execution Logic & Target Tracking (Live Pricing)
    live_price = df_ltf['close'].iloc[-1]
    
    if current_pos != 0:
        entry_p = state["entry_price"]
        
        # Long Position Tracking Rules
        if current_pos > 0:
            if not state["break_even_protected"] and (live_price >= entry_p * (1 + (BREAK_EVEN_TRIGGER / 100))):
                state["break_even_protected"] = True
                send_whatsapp_alert(f"🛡️ *Break-Even Protected*\n15m Long entry at {entry_p} is now protected at par.")
                save_state(state)
                
            current_stop = entry_p if state["break_even_protected"] else (entry_p * (1 - (STOP_PERCENT / 100)))
            
            if live_price < current_stop:
                execute_trade('sell', abs(current_pos))
                send_whatsapp_alert(f"🚨 *STOP TRIGGERED (LONG)*\nClosed position at {live_price}.\nReason: 15m Structural Stop/BE hit.")
                state = {"position_size": 0.0, "entry_price": None, "break_even_protected": False}
                save_state(state)
                return
                
        # Short Position Tracking Rules
        elif current_pos < 0:
            if not state["break_even_protected"] and (live_price <= entry_p * (1 - (BREAK_EVEN_TRIGGER / 100))):
                state["break_even_protected"] = True
                send_whatsapp_alert(f"🛡️ *Break-Even Protected*\n15m Short entry at {entry_p} is now protected at par.")
                save_state(state)
                
            current_stop = entry_p if state["break_even_protected"] else (entry_p * (1 + (STOP_PERCENT / 100)))
            
            if live_price > current_stop:
                execute_trade('buy', abs(current_pos))
                send_whatsapp_alert(f"🚨 *STOP TRIGGERED (SHORT)*\nClosed position at {live_price}.\nReason: 15m Structural Stop/BE hit.")
                state = {"position_size": 0.0, "entry_price": None, "break_even_protected": False}
                save_state(state)
                return

    # 8. Entry Order Allocations
    if long_entry_condition:
        if current_pos < 0:
            execute_trade('buy', abs(current_pos))
            execute_trade('buy', 0.2)
            state = {"position_size": 0.2, "entry_price": live_price, "break_even_protected": False}
            send_whatsapp_alert(f"🚀 *LONG REVERSAL (15m/1h)*\nFlipped Short to Long. Opened 0.2 BTC at {live_price}.")
        else:
            execute_trade('buy', 0.1)
            state = {"position_size": 0.1, "entry_price": live_price, "break_even_protected": False}
            send_whatsapp_alert(f"🟢 *LONG ENTRY (15m/1h)*\nBought 0.1 BTC at {live_price}.")
        save_state(state)
        
    elif short_entry_condition:
        if current_pos > 0:
            execute_trade('sell', abs(current_pos))
            execute_trade('sell', 0.2)
            state = {"position_size": -0.2, "entry_price": live_price, "break_even_protected": False}
            send_whatsapp_alert(f"💥 *SHORT REVERSAL (15m/1h)*\nFlipped Long to Short. Opened 0.2 BTC at {live_price}.")
        else:
            execute_trade('sell', 0.1)
            state = {"position_size": -0.1, "entry_price": live_price, "break_even_protected": False}
            send_whatsapp_alert(f"🔴 *SHORT ENTRY (15m/1h)*\nSold 0.1 BTC at {live_price}.")
        save_state(state)

# --- Active Strategy Execution Loop ---
print("[START] Delta India 15m/1h Crypto Automation Running...")
send_whatsapp_alert("✅ Cloud trading engine successfully initialized on Render (15m/1h Configuration). Monitoring live ticks...")

while True:
    try:
        run_strategy()
    except Exception as e:
        print(f"[CRITICAL ERROR] Main loop cycle fault: {e}")
    time.sleep(30)  # Evaluate parameters every 30 seconds
