#!/usr/bin/env python3
"""
PLATINUM CRYPTO TRADER DASHBOARD 2025 — BINANCE.US EDITION
ULTRA INTERACTIVE Streamlit UI + Console Fallback
Real-time controls, live charts, order book, position table, logs, and more.
"""
import streamlit as st
import os
import time
import threading
import json
import requests
import websocket
from decimal import Decimal, getcontext, ROUND_DOWN, InvalidOperation
from datetime import datetime
import pytz
from binance.client import Client
from binance.exceptions import BinanceAPIException
import logging
from logging.handlers import TimedRotatingFileHandler
import sys
import pandas as pd
import plotly.graph_objects as go
import plotly.express as px

# ========================= CONFIG =========================
getcontext().prec = 28
ZERO = Decimal('0')
ONE = Decimal('1')
SAFETY_BUFFER = Decimal('0.95')
MAX_POSITION_PCT = Decimal('0.05')
MIN_TRADE_VALUE = Decimal('5.0')
ENTRY_PCT_BELOW_ASK = Decimal('0.001')
TOP_N_VOLUME = 25
MAX_BUY_COINS = 2
DEPTH_LEVELS = 5
HEARTBEAT_INTERVAL = 25
MAX_STREAMS_PER_CONNECTION = 100
RECONNECT_BASE_DELAY = 5
MAX_RECONNECT_DELAY = 300
DASHBOARD_REFRESH = 30

USE_PAPER_TRADING = False

CST = pytz.timezone('America/Chicago')
TRADING_START_HOUR = 4
TRADING_END_HOUR = 16
LOG_FILE = os.path.expanduser("~/platinum_crypto_dashboard.log")

# ========================= LOGGING =========================
def setup_logging():
    logger = logging.getLogger("platinum_bot")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        fh = TimedRotatingFileHandler(LOG_FILE, when='midnight', backupCount=14)
        fh.setFormatter(logging.Formatter('%(asctime)s %(levelname)s - %(message)s'))
        ch = logging.StreamHandler(sys.stdout)
        ch.setFormatter(logging.Formatter('%(asctime)s %(levelname)s - %(message)s'))
        logger.addHandler(fh)
        logger.addHandler(ch)
    return logger

logger = setup_logging()

# ========================= GLOBALS =========================
client = None
symbol_info = {}
account_balances = {}
live_prices = {}
live_bids = {}
live_asks = {}
top_volume_symbols = []
active_positions = {}
price_lock = threading.Lock()
book_lock = threading.Lock()
ws_instances = []
ws_threads = []
user_ws = None
listen_key = None
listen_key_lock = threading.Lock()
last_rebalance_str = "Never"
bot_running = False
init_complete = False

# Session state defaults
DEFAULTS = {
    'logs': [], 'shutdown': False, 'auto_rebalance': True,
    'rebalance_interval': 7200, 'min_trade_value': 5.0,
    'max_position_pct': 5.0, 'entry_pct_below_ask': 0.1,
    'bot_running': False, 'init_status': '', 'init_thread_running': False,
    'last_auto': 0.0, 'selected_symbol': None, 'order_history': []
}

# ========================= UTILS =========================
def now_cst():
    return datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")

def log(msg):
    line = f"{now_cst()} - {msg}"
    print(line)
    logger.info(msg)
    try:
        if 'logs' in st.session_state:
            st.session_state.logs.append(line)
            if len(st.session_state.logs) > 500:
                st.session_state.logs = st.session_state.logs[-500:]
    except:
        pass

def send_whatsapp(msg):
    phone = os.getenv('CALLMEBOT_PHONE')
    key = os.getenv('CALLMEBOT_API_KEY')
    if phone and key:
        try:
            m = requests.utils.quote(msg)
            requests.get(
                f"https://api.callmebot.com/whatsapp.php?phone={phone}&text={m}&apikey={key}",
                timeout=5
            )
        except Exception:
            pass

def is_trading_hours():
    h = datetime.now(CST).hour
    return TRADING_START_HOUR <= h < TRADING_END_HOUR

# ========================= BALANCE & INFO =========================
def update_balances():
    global account_balances
    try:
        info = client.get_account()['balances']
        account_balances = {
            a['asset']: Decimal(a['free']) for a in info if Decimal(a['free']) > ZERO
        }
        log(f"USDT free: {account_balances.get('USDT', ZERO):.2f}")
    except Exception as e:
        log(f"Balance update error: {e}")

def get_total_portfolio_value():
    total = account_balances.get('USDT', ZERO)
    for asset, qty in account_balances.items():
        if asset == 'USDT':
            continue
        sym = f"{asset}USDT"
        price = live_prices.get(sym)
        if price:
            total += qty * price
    return total

def _safe_decimal(v, fallback='0'):
    try:
        return Decimal(str(v))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(fallback)

def load_symbol_info():
    global symbol_info
    try:
        info = client.get_exchange_info().get('symbols', [])
        for s in info:
            if s.get('quoteAsset') != 'USDT' or s.get('status') != 'TRADING':
                continue
            filters = {f['filterType']: f for f in s.get('filters', [])}
            stepSize = _safe_decimal(filters.get('LOT_SIZE', {}).get('stepSize', '0'))
            tickSize = _safe_decimal(filters.get('PRICE_FILTER', {}).get('tickSize', '0'))
            minQty = _safe_decimal(filters.get('LOT_SIZE', {}).get('minQty', '0'))
            minNotional = _safe_decimal(filters.get('MIN_NOTIONAL', {}).get('minNotional', '10'))
            if stepSize == ZERO or tickSize == ZERO:
                continue
            symbol_info[s['symbol']] = {
                'stepSize': stepSize,
                'tickSize': tickSize,
                'minQty': minQty,
                'minNotional': minNotional
            }
        log(f"Loaded {len(symbol_info)} symbols")
    except Exception as e:
        log(f"Symbol info error: {e}")

# ========================= SAFE ORDER =========================
def round_down_to_step(value: Decimal, step: Decimal) -> Decimal:
    try:
        if step <= 0:
            return value
        return value.quantize(step, rounding=ROUND_DOWN)
    except Exception:
        try:
            multiples = (value // step)
            return multiples * step
        except Exception:
            return value

def format_decimal_for_order(d: Decimal) -> str:
    s = format(d.normalize(), 'f')
    if '.' in s:
        s = s.rstrip('0').rstrip('.')
    return s

def _mock_place_limit_order(symbol, side, price, quantity):
    order = {
        'symbol': symbol, 'side': side,
        'price': format_decimal_for_order(price),
        'quantity': format_decimal_for_order(quantity),
        'status': 'TEST'
    }
    log(f"[PAPER] PLACED {side} {symbol} {quantity} @ {price}")
    return order

def place_limit_order(symbol, side, price, quantity):
    if USE_PAPER_TRADING:
        return _mock_place_limit_order(symbol, side, price, quantity)

    info = symbol_info.get(symbol)
    if not info:
        log(f"No info for {symbol}")
        return None

    price = round_down_to_step(Decimal(price), info['tickSize'])
    quantity = round_down_to_step(Decimal(quantity), info['stepSize'])

    if quantity <= info['minQty']:
        log(f"Qty too small {symbol} qty({quantity}) <= minQty({info['minQty']})")
        return None
    notional = price * quantity
    if notional < info['minNotional']:
        log(f"Notional too low for {symbol}: {notional} < {info['minNotional']}")
        return None

    if side == 'BUY':
        needed = notional * SAFETY_BUFFER
        if needed > account_balances.get('USDT', ZERO):
            log(f"Not enough USDT for BUY {symbol} needed:{needed} have:{account_balances.get('USDT', ZERO)}")
            return None
    else:
        base = symbol.replace('USDT', '')
        if quantity > account_balances.get(base, ZERO) * SAFETY_BUFFER:
            log(f"Not enough {base} for SELL")
            return None

    try:
        order = client.create_order(
            symbol=symbol,
            side=side,
            type='LIMIT',
            timeInForce='GTC',
            quantity=format_decimal_for_order(quantity),
            price=format_decimal_for_order(price)
        )
        log(f"PLACED {side} {symbol} {quantity} @ {price}")
        send_whatsapp(f"{side} {symbol} {quantity}@{price}")
        # Save to history
        st.session_state.order_history.append({
            'time': now_cst(),
            'symbol': symbol,
            'side': side,
            'price': float(price),
            'quantity': float(quantity),
            'status': 'PLACED'
        })
        return order
    except BinanceAPIException as e:
        msg = getattr(e, 'message', str(e))
        log(f"ORDER FAILED {symbol} {side}: {msg}")
        return None
    except Exception as e:
        log(f"ORDER ERROR {symbol}: {e}")
        return None

# ========================= HEARTBEAT WS =========================
class HeartbeatWebSocket(websocket.WebSocketApp):
    def __init__(self, url, **kwargs):
        super().__init__(url, **kwargs)
        self.last_pong = time.time()
        self.hb_thread = None
        self.reconnect = 0
        self._stop = False

    def on_open(self, ws):
        try:
            log(f"WS OPEN {ws.url.split('?')[0]}")
            self.last_pong = time.time()
            self.reconnect = 0
            if not self.hb_thread or not self.hb_thread.is_alive():
                self.hb_thread = threading.Thread(target=self._heartbeat, daemon=True)
                self.hb_thread.start()
        except Exception as e:
            log(f"on_open error: {e}")

    def on_pong(self, *args):
        self.last_pong = time.time()

    def _heartbeat(self):
        try:
            while not self._stop and getattr(self, 'sock', None) and getattr(self.sock, 'connected', False):
                if time.time() - self.last_pong > HEARTBEAT_INTERVAL + 5:
                    log("No pong to closing WS")
                    try:
                        self.close()
                    except Exception:
                        pass
                    break
                try:
                    self.send("ping", opcode=websocket.ABNF.OPCODE_PING)
                except Exception:
                    pass
                time.sleep(HEARTBEAT_INTERVAL)
        except Exception as e:
            log(f"Heartbeat thread error: {e}")

    def run_forever(self, **kwargs):
        self._stop = False
        while not self._stop:
            try:
                super().run_forever(ping_interval=None, ping_timeout=None, **kwargs)
            except Exception as e:
                log(f"WS crash: {e}")
            self.reconnect += 1
            delay = min(MAX_RECONNECT_DELAY, RECONNECT_BASE_DELAY * (2 ** (self.reconnect - 1)))
            log(f"Reconnect in {delay}s")
            time.sleep(delay)

    def stop(self):
        self._stop = True
        try:
            self.close()
        except Exception:
            pass

# ========================= WS HANDLERS =========================
def on_market_message(ws, message):
    try:
        data = json.loads(message)
        stream = data.get('stream', '')
        payload = data.get('data', {})
        if not payload:
            return
        sym = stream.split('@')[0].upper()

        if stream.endswith('@ticker'):
            price = _safe_decimal(payload.get('c', '0'))
            if price > ZERO:
                with price_lock:
                    live_prices[sym] = price
        elif stream.endswith(f'@depth{DEPTH_LEVELS}'):
            bids = [(_safe_decimal(p), _safe_decimal(q)) for p, q in payload.get('bids', [])[:DEPTH_LEVELS]]
            asks = [(_safe_decimal(p), _safe_decimal(q)) for p, q in payload.get('asks', [])[:DEPTH_LEVELS]]
            with book_lock:
                live_bids[sym] = bids
                live_asks[sym] = asks
    except Exception as e:
        log(f"Market WS parse error: {e}")

def on_user_message(ws, message):
    try:
        data = json.loads(message)
        if data.get('e') != 'executionReport':
            return
        ev = data
        sym = ev.get('s')
        side = ev.get('S')
        status = ev.get('X')
        price = _safe_decimal(ev.get('p', '0'))
        qty = _safe_decimal(ev.get('q', '0'))
        if status in ('FILLED', 'PARTIALLY_FILLED'):
            log(f"FILL {side} {sym} @ {price} | {qty}")
            send_whatsapp(f"{side} {sym} {status} @ {price}")
            # Update order history
            for order in st.session_state.order_history:
                if order['symbol'] == sym and order['status'] == 'PLACED':
                    order['status'] = status
                    break
    except Exception as e:
        log(f"User WS error: {e}")

def on_ws_error(ws, err):
    log(f"WS error ({getattr(ws, 'url', 'unknown')}): {err}")

def on_ws_close(ws, code, msg):
    log(f"WS closed ({getattr(ws, 'url', 'unknown')}) to {code}: {msg}")

# ========================= WS STARTERS =========================
def start_market_websockets(symbols):
    global ws_instances, ws_threads
    ticker = [f"{s.lower()}@ticker" for s in symbols]
    depth = [f"{s.lower()}@depth{DEPTH_LEVELS}" for s in symbols]
    streams = ticker + depth
    chunks = [streams[i:i + MAX_STREAMS_PER_CONNECTION] for i in range(0, len(streams), MAX_STREAMS_PER_CONNECTION)]

    for chunk in chunks:
        url = f"wss://stream.binance.us:9443/stream?streams={'/'.join(chunk)}"
        ws = HeartbeatWebSocket(
            url,
            on_message=on_market_message,
            on_error=on_ws_error,
            on_close=on_ws_close,
            on_open=lambda ws: log("Market WS open")
        )
        ws_instances.append(ws)
        t = threading.Thread(target=ws.run_forever, daemon=True)
        t.start()
        ws_threads.append(t)
        time.sleep(0.4)

def start_user_stream():
    global user_ws, listen_key
    try:
        with listen_key_lock:
            listen_key = client.stream_get_listen_key()
        url = f"wss://stream.binance.us:9443/ws/{listen_key}"
        user_ws = HeartbeatWebSocket(
            url,
            on_message=on_user_message,
            on_error=on_ws_error,
            on_close=on_ws_close,
            on_open=lambda ws: log("User WS open")
        )
        t = threading.Thread(target=user_ws.run_forever, daemon=True)
        t.start()
        ws_threads.append(t)
        log("User stream started")
    except Exception as e:
        log(f"User stream failed: {e}")

def keepalive_user_stream():
    while bot_running:
        time.sleep(1800)
        try:
            with listen_key_lock:
                if listen_key:
                    client.stream_keepalive(listen_key)
        except Exception:
            pass

# ========================= TRADING LOGIC =========================
def get_top_volume_symbols():
    global top_volume_symbols
    candidates = []
    with book_lock:
        for sym, bids in live_bids.items():
            if len(bids) < DEPTH_LEVELS:
                continue
            vol = sum((p * q) for p, q in bids[:DEPTH_LEVELS])
            try:
                volf = float(vol)
            except Exception:
                volf = 0.0
            if volf > 0:
                candidates.append((sym, volf))
    candidates.sort(key=lambda x: x[1], reverse=True)
    top_volume_symbols = [s for s, _ in candidates[:TOP_N_VOLUME]]
    log(f"Top {len(top_volume_symbols)} volume symbols refreshed")

def rebalance_portfolio():
    global last_rebalance_str
    try:
        if not is_trading_hours():
            log("Outside 4AM-4PM CST to no buys")
            return
        if not top_volume_symbols:
            log("No volume data yet")
            return

        update_balances()
        total = get_total_portfolio_value()
        pct = Decimal(str(st.session_state.max_position_pct)) / Decimal('100')
        target = total * pct
        usdt_free = account_balances.get('USDT', ZERO)
        investable = usdt_free * SAFETY_BUFFER

        active_positions.clear()
        for asset, qty in account_balances.items():
            if asset == 'USDT':
                continue
            sym = f"{asset}USDT"
            if sym in live_prices:
                active_positions[sym] = qty * live_prices[sym]

        # Sell excess
        for sym, val in list(active_positions.items()):
            if val > target:
                excess = val - target
                price = live_prices.get(sym, ZERO)
                if price <= ZERO:
                    continue
                step = symbol_info.get(sym, {}).get('stepSize')
                if not step:
                    continue
                qty = (excess / price).quantize(step, rounding=ROUND_DOWN)
                if qty > ZERO:
                    with book_lock:
                        bid = live_bids.get(sym, [(ZERO, ZERO)])[0][0]
                    if bid > ZERO:
                        place_limit_order(sym, 'SELL', bid, qty)

        # Buy top targets
        buy_targets = [s for s in top_volume_symbols[:MAX_BUY_COINS] if s not in active_positions]
        buys = 0
        min_trade = Decimal(str(st.session_state.min_trade_value))
        for sym in buy_targets:
            if buys >= MAX_BUY_COINS:
                break
            needed = min(target, investable)
            if needed < min_trade:
                continue
            with book_lock:
                ask = live_asks.get(sym, [(ZERO, ZERO)])[0][0]
            if ask <= ZERO:
                continue
            buy_price = (ask * (ONE - ENTRY_PCT_BELOW_ASK))
            tick = symbol_info.get(sym, {}).get('tickSize')
            step = symbol_info.get(sym, {}).get('stepSize')
            if not tick or not step:
                continue
            buy_price = round_down_to_step(buy_price, tick)
            qty = ((needed / buy_price)).quantize(step, rounding=ROUND_DOWN)
            order_val = buy_price * qty
            if order_val < min_trade or order_val > investable:
                continue
            order = place_limit_order(sym, 'BUY', buy_price, qty)
            if order:
                buys += 1
                investable -= order_val

        last_rebalance_str = now_cst()
        log(f"REBALANCE | Buys:{buys} | Targets:{buy_targets[:MAX_BUY_COINS]}")
    except Exception as e:
        log(f"Rebalance error: {e}")

# ========================= INITIALIZER =========================
def initialise_bot():
    global bot_running, init_complete
    try:
        log("=== INITIALISING PLATINUM TRADER ===")
        load_symbol_info()
        update_balances()

        all_syms = list(symbol_info.keys())
        if not all_syms:
            log("ERROR: No symbols loaded")
            return

        log(f"Starting {len(all_syms)} WebSocket streams...")
        start_list = all_syms[:1000] if len(all_syms) > 1000 else all_syms
        start_market_websockets(start_list)

        log("Starting user stream...")
        start_user_stream()
        threading.Thread(target=keepalive_user_stream, daemon=True).start()

        log("Waiting for market data...")
        deadline = time.time() + 15
        while time.time() < deadline:
            if live_prices or live_bids:
                break
            time.sleep(0.5)

        get_top_volume_symbols()
        bot_running = True
        init_complete = True
        st.session_state.bot_running = True
        st.session_state.init_status = "READY"
        log("PLATINUM TRADER STARTED")
        send_whatsapp("PLATINUM TRADER STARTED")
    except Exception as e:
        log(f"Initialise error: {e}")
        st.session_state.init_status = f"ERROR: {e}"
        st.session_state.bot_running = False

# ========================= INTERACTIVE UI =========================
def run_streamlit():
    st.set_page_config(page_title="Platinum Crypto Trader", layout="wide", initial_sidebar_state="expanded")
    
    # Title
    st.markdown("""
    # PLATINUM CRYPTO TRADER 2025
    **Binance.US | 4AM–4PM CST | Volume-Based Rebalancing**
    """)

    # Initialize session state
    for k, v in DEFAULTS.items():
        if k not in st.session_state:
            st.session_state[k] = v

    # Sidebar Controls
    with st.sidebar:
        st.header("Bot Control")
        if not st.session_state.bot_running:
            if st.button("START TRADER (REAL)", type="primary", use_container_width=True):
                if not st.session_state.init_thread_running:
                    st.session_state.init_thread_running = True
                    st.session_state.init_status = "Initialising..."
                    t = threading.Thread(target=initialise_bot, daemon=True)
                    t.start()
            st.info(st.session_state.get('init_status', 'Press START'))
        else:
            if st.button("STOP TRADER", type="secondary", use_container_width=True):
                st.session_state.bot_running = False
                for ws in ws_instances:
                    ws.stop()
                st.success("Bot stopped.")
        
        st.markdown("---")
        st.subheader("Strategy Settings")
        st.session_state.auto_rebalance = st.checkbox("Auto Rebalance", value=True)
        st.session_state.rebalance_interval = st.slider("Interval (min)", 10, 360, 120, 10) * 60
        st.session_state.max_position_pct = st.slider("Max % per Coin", 1.0, 20.0, 5.0, 0.5)
        st.session_state.min_trade_value = st.number_input("Min Trade ($)", 1.0, 100.0, 5.0, 1.0)

        if st.session_state.bot_running and is_trading_hours():
            if st.button("REBALANCE NOW", type="primary", use_container_width=True):
                threading.Thread(target=rebalance_portfolio, daemon=True).start()

    # API Check
    if not os.getenv('BINANCE_API_KEY') or not os.getenv('BINANCE_API_SECRET'):
        st.error("Set `BINANCE_API_KEY` and `BINANCE_API_SECRET` env vars")
        st.stop()

    global client
    try:
        client = Client(os.getenv('BINANCE_API_KEY'), os.getenv('BINANCE_API_SECRET'), tld='us')
    except Exception as e:
        st.error(f"Failed to create Binance client: {e}")
        st.stop()

    # Main Dashboard
    if not st.session_state.bot_running:
        st.warning("Bot not running. Start from sidebar.")
        st.stop()

    # Live Metrics
    update_balances()
    total = get_total_port from get_total_portfolio_value()
    usdt = account_balances.get('USDT', ZERO)
    in_hours = is_trading_hours()

    col1, col2, col3, col4, col5 = st.columns(5)
    col1.metric("USDT Free", f"${usdt:,.2f}")
    col2.metric("Portfolio", f"${total:,.2f}")
    col3.metric("Positions", len(active_positions))
    col4.metric("Window", "OPEN" if in_hours else "CLOSED")
    col5.metric("Last Rebalance", last_rebalance_str)

    # Tabs
    tab1, tab2, tab3, tab4, tab5 = st.tabs(["Volume Leaders", "Positions", "Order Book", "Manual Trade", "Logs"])

    with tab1:
        st.subheader("Top 25 Volume Coins")
        if top_volume_symbols:
            df_vol = []
            for sym in top_volume_symbols[:25]:
                with book_lock:
                    vol = sum(p * q for p, q in live_bids.get(sym, []))
                price = live_prices.get(sym, 0)
                df_vol.append({"Symbol": sym, "Bid Vol $": float(vol), "Price": float(price)})
            df_vol = pd.DataFrame(df_vol)
            st.dataframe(df_vol.style.format({"Bid Vol $": "${:,.0f}", "Price": "${:.6f}"}))

            fig = px.bar(df_vol.head(10), x='Symbol', y='Bid Vol $', title="Top 10 Bid Volume")
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.info("Waiting for volume data...")

    with tab2:
        st.subheader("Active Positions")
        if active_positions:
            df_pos = []
            target_val = total * (Decimal(str(st.session_state.max_position_pct)) / 100)
            for sym, val in active_positions.items():
                qty = account_balances.get(sym.replace('USDT', ''), ZERO)
                price = live_prices.get(sym, ZERO)
                pct = (val / total) * 100 if total > 0 else 0
                df_pos.append({
                    "Symbol": sym,
                    "Qty": float(qty),
                    "Value": float(val),
                    "%": pct,
                    "Target": float(target_val)
                })
            df_pos = pd.DataFrame(df_pos)
            st.dataframe(df_pos.style.format({"Value": "${:,.2f}", "Target": "${:,.2f}", "%": "{:.2f}%"}))
        else:
            st.info("No positions")

    with tab3:
        st.subheader("Live Order Book")
        sym = st.selectbox("Select Symbol", options=top_volume_symbols[:10] if top_volume_symbols else [])
        if sym:
            with book_lock:
                bids = live_bids.get(sym, [])
                asks = live_asks.get(sym, [])
            bid_df = pd.DataFrame(bids, columns=["Price", "Qty"]) if bids else pd.DataFrame()
            ask_df = pd.DataFrame(asks, columns=["Price", "Qty"]) if asks else pd.DataFrame()
            colb1, colb2 = st.columns(2)
            with colb1:
                st.write("**Bids**")
                st.dataframe(bid_df.style.format({"Price": "${:.6f}"}))
            with colb2:
                st.write("**Asks**")
                st.dataframe(ask_df.style.format({"Price": "${:.6f}"}))
            if bids and asks:
                fig = go.Figure()
                fig.add_trace(go.Bar(name='Bids', x=[-q for p,q in bids], y=[p for p,q in bids], orientation='h'))
                fig.add_trace(go.Bar(name='Asks', x=[q for p,q in asks], y=[p for p,q in asks], orientation='h'))
                fig.update_layout(title=f"{sym} Depth", barmode='relative')
                st.plotly_chart(fig, use_container_width=True)

    with tab4:
        st.subheader("Manual Trade")
        msym = st.selectbox("Symbol", options=sorted(symbol_info.keys()), key="manual_sym")
        side = st.radio("Side", ["BUY", "SELL"], horizontal=True)
        mprice = st.number_input("Price", min_value=0.0, value=float(live_prices.get(msym, 0)) or 0.1)
        mqty = st.number_input("Quantity", min_value=0.0, value=0.1)
        if st.button("PLACE ORDER"):
            order = place_limit_order(msym, side, Decimal(mprice), Decimal(mqty))
            if order:
                st.success(f"{side} {mqty} {msym} @ {mprice}")
            else:
                st.error("Order failed")

    with tab5:
        st.subheader("Live Log")
        log_container = st.empty()
        with log_container.container():
            for line in st.session_state.logs[-50:]:
                st.code(line)

    # Auto-rebalance
    if st.session_state.auto_rebalance and in_hours:
        if time.time() - st.session_state.last_auto > st.session_state.rebalance_interval:
            threading.Thread(target=rebalance_portfolio, daemon=True).start()
            st.session_state.last_auto = time.time()
        get_top_volume_symbols()

    st.success("PLATINUM-GRADE TRADER RUNNING")

# ========================= MAIN =========================
def main():
    try:
        run_streamlit()
    except Exception as e:
        print(f"Streamlit failed: {e}")
        print("Run with --console for text mode")
        sys.exit(1)

if __name__ == "__main__":
    main()
