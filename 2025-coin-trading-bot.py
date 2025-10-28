import os
import time
import logging
import math
from logging.handlers import TimedRotatingFileHandler
from binance.client import Client
from binance.enums import (
    SIDE_BUY,
    SIDE_SELL,
    ORDER_TYPE_MARKET,
    ORDER_TYPE_LIMIT,
    ORDER_TYPE_STOP_LOSS,
    ORDER_TYPE_STOP_LOSS_LIMIT,
    ORDER_TYPE_TAKE_PROFIT,
    ORDER_TYPE_TAKE_PROFIT_LIMIT,
    ORDER_TYPE_LIMIT_MAKER
)
from binance.exceptions import BinanceAPIException, BinanceOrderException
from tenacity import retry, stop_after_attempt, wait_exponential
import talib
import numpy as np
from datetime import datetime, timedelta
import pytz
import requests

# === CONFIGURATION ===
CALLMEBOT_API_KEY = os.getenv('CALLMEBOT_API_KEY')
CALLMEBOT_PHONE = os.getenv('CALLMEBOT_PHONE')
MAX_PRICE = 1000.00
MIN_PRICE = 1.00
CURRENT_PRICE_MAX = 1000.00
LOOP_INTERVAL = 60
LOG_FILE = "crypto_trading_bot.log"
VOLUME_THRESHOLD = 15000
RSI_PERIOD = 14
BB_PERIOD = 20
BB_DEV = 2
STOCH_K_PERIOD = 14
STOCH_D_PERIOD = 3
ATR_PERIOD = 14
SMA_PERIOD = 50
EMA_PERIOD = 21
HISTORY_DAYS = 60
KLINE_INTERVAL = '1h'
PROFIT_TARGET = 0.008  # 0.8%
RISK_PER_TRADE = 0.10
MIN_BALANCE = 2.0
ORDER_TIMEOUT = 300

# API Keys
API_KEY = os.getenv('BINANCE_API_KEY')
API_SECRET = os.getenv('BINANCE_API_SECRET')

# Logging setup
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s %(levelname)s:%(message)s',
    handlers=[
        TimedRotatingFileHandler(LOG_FILE, when="midnight", interval=1, backupCount=7),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Timezone
CST_TZ = pytz.timezone('America/Chicago')

# In-memory positions
positions = {}

# === Helper Functions ===
def now_cst():
    return datetime.now(CST_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")

def format_time_central(ts_ms):
    if not ts_ms:
        return "N/A"
    dt_utc = datetime.utcfromtimestamp(ts_ms / 1000).replace(tzinfo=pytz.UTC)
    dt_central = dt_utc.astimezone(CST_TZ)
    return dt_central.strftime("%Y-%m-%d %H:%M:%S %Z")

# === PROFESSIONAL DASHBOARD ===
def print_status_dashboard(client):
    try:
        balance = get_balance(client, 'USDT')
        total_value = balance
        total_profit_usdt = 0.0

        print("\n" + "="*100)
        print(f" PROFESSIONAL TRADING DASHBOARD - {now_cst()} ")
        print("="*100)
        print(f"Account Balance (USDT):        ${balance:,.6f}")
        print(f"Available Cash (USDT):         ${balance:,.6f}")
        print(f"Active Positions:              {len(positions)}")
        print("-" * 100)

        if not positions:
            print(" No open positions.")
            print("="*100 + "\n")
            return

        print(f"{'SYMBOL':<10} {'QTY':>10} {'ENTRY':>12} {'CURRENT':>12} {'RSI':>6} {'P&L %':>8} {'PROFIT $':>10} {'SELL PRICE':>12} {'AGE':>12}")
        print("-" * 100)

        for symbol, pos in positions.items():
            current_data = fetch_current_data(client, symbol)
            if not current_data:
                logger.warning(f"Skipping {symbol}: no price data")
                continue

            current_price = current_data['price']
            qty = pos['qty']
            entry_price = pos['entry_price']
            entry_time = pos['entry_time']
            buy_fee = pos['buy_fee']

            # Fetch RSI and fees
            metrics = get_historical_metrics(client, symbol)
            rsi = metrics['rsi'] if metrics and metrics['rsi'] is not None else -1
            maker_fee, taker_fee = get_trade_fees(client, symbol)
            total_fees = buy_fee + taker_fee

            # Profit calculations
            gross_profit = (current_price - entry_price) * qty
            fee_cost = total_fees * current_price * qty
            net_profit_usdt = gross_profit - fee_cost
            profit_pct = ((current_price - entry_price) / entry_price - total_fees) * 100

            # Target sell price to achieve PROFIT_TARGET
            target_sell_price = entry_price * (1 + PROFIT_TARGET + buy_fee + taker_fee)

            # Position age
            age = datetime.now(CST_TZ) - entry_time
            age_str = str(age).split('.')[0]

            total_value += current_price * qty
            total_profit_usdt += net_profit_usdt

            rsi_display = f"{rsi:5.1f}" if rsi >= 0 else " N/A "

            print(f"{symbol:<10} {qty:>10.6f} {entry_price:>12.6f} {current_price:>12.6f} "
                  f"{rsi_display} {profit_pct:>7.2f}% {net_profit_usdt:>10.2f} {target_sell_price:>12.6f} {age_str:>12}")

        print("-" * 100)
        print(f"Total Portfolio Value:         ${total_value:,.6f}")
        print(f"Total Unrealized P&L:          ${total_profit_usdt:,.2f}")
        print(f"Bot Running... Next update in {LOOP_INTERVAL} seconds.\n")
        print("="*100 + "\n")

    except Exception as e:
        logger.error(f"Dashboard error: {e}")

# === Indicator & Data Functions ===
def calculate_rsi(prices, period=RSI_PERIOD):
    if len(prices) < period + 1:
        return None
    delta = np.diff(prices)
    gain = np.where(delta > 0, delta, 0)
    loss = np.where(delta < 0, -delta, 0)
    avg_gain = np.mean(gain[:period])
    avg_loss = np.mean(loss[:period])
    for i in range(period, len(delta)):
        avg_gain = (avg_gain * (period - 1) + gain[i]) / period
        avg_loss = (avg_loss * (period - 1) + loss[i]) / period
    if avg_loss == 0:
        return 100
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def get_symbol_filters(client, symbol):
    try:
        info = client.get_exchange_info()
        symbol_info = next(s for s in info['symbols'] if s['symbol'] == symbol)
        filters = {}
        for f in symbol_info['filters']:
            ftype = f['filterType']
            params = {}
            if ftype == 'LOT_SIZE':
                params = {k: float(f.get(k)) for k in ['minQty', 'maxQty', 'stepSize']}
            elif ftype == 'MIN_NOTIONAL':
                params = {'minNotional': float(f.get('minNotional'))}
            elif ftype == 'PRICE_FILTER':
                params = {k: float(f.get(k)) for k in ['minPrice', 'maxPrice', 'tickSize']}
            filters[ftype] = params
        return filters
    except Exception as e:
        logger.error(f"Failed to get filters for {symbol}: {e}")
        return {}

def round_to_step_size(value, step_size):
    if value == 0 or step_size == 0:
        return 0
    return round(value / step_size) * step_size

def round_to_tick_size(value, tick_size):
    if value == 0 or tick_size == 0:
        return 0
    return round(value / tick_size) * tick_size

def validate_and_adjust_order(client, symbol, side, order_type, quantity, price=None, current_price=None):
    filters = get_symbol_filters(client, symbol)
    adjusted_qty = quantity
    adjusted_price = price

    lot_filter = filters.get('LOT_SIZE', {})
    if lot_filter:
        step_size = lot_filter.get('stepSize', 0)
        adjusted_qty = round_to_step_size(adjusted_qty, step_size)
        min_qty = lot_filter.get('minQty', 0)
        max_qty = lot_filter.get('maxQty', float('inf'))
        if adjusted_qty < min_qty:
            adjusted_qty = min_qty
        if adjusted_qty > max_qty:
            adjusted_qty = max_qty
        if adjusted_qty < min_qty:
            return None, f"Qty below minQty: {adjusted_qty}"

    if adjusted_price is not None:
        price_filter = filters.get('PRICE_FILTER', {})
        if price_filter:
            tick_size = price_filter.get('tickSize', 0)
            adjusted_price = round_to_tick_size(adjusted_price, tick_size)
            min_price = price_filter.get('minPrice', 0)
            max_price = price_filter.get('maxPrice', float('inf'))
            if adjusted_price < min_price:
                adjusted_price = min_price
            if adjusted_price > max_price:
                adjusted_price = max_price
            if adjusted_price < min_price:
                return None, f"Price below minPrice: {adjusted_price}"

    min_notional = filters.get('MIN_NOTIONAL', {}).get('minNotional', 0)
    if min_notional > 0:
        effective_price = adjusted_price or current_price
        if effective_price:
            notional = adjusted_qty * effective_price
            if notional < min_notional:
                needed_qty = min_notional / effective_price
                adjusted_qty = max(adjusted_qty, needed_qty)
                if lot_filter:
                    adjusted_qty = round_to_step_size(adjusted_qty, lot_filter.get('stepSize', 0))
                if adjusted_qty * effective_price < min_notional:
                    return None, f"Cannot meet MIN_NOTIONAL: {adjusted_qty * effective_price}"

    return {'quantity': adjusted_qty, 'price': adjusted_price}, None

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=20))
def get_momentum_status(client, symbol):
    try:
        klines = client.get_klines(symbol=symbol, interval='1d', limit=10)
        if len(klines) < 3:
            return "sideways"
        opens = np.array([float(k[1]) for k in klines])
        highs = np.array([float(k[2]) for k in klines])
        lows = np.array([float(k[3]) for k in klines])
        closes = np.array([float(k[4]) for k in klines])
        bullish_score = sum([np.any(talib.CDLHAMMER(opens, highs, lows, closes) > 0),
                             np.any(talib.CDLENGULFING(opens, highs, lows, closes) > 0),
                             np.any(talib.CDLMORNINGSTAR(opens, highs, lows, closes) > 0)])
        bearish_score = sum([np.any(talib.CDLSHOOTINGSTAR(opens, highs, lows, closes) > 0),
                             np.any(talib.CDLENGULFING(opens, highs, lows, closes) < 0),
                             np.any(talib.CDLEVENINGSTAR(opens, highs, lows, closes) > 0)])
        return "bullish" if bullish_score > bearish_score else "bearish" if bearish_score > bullish_score else "sideways"
    except Exception as e:
        logger.error(f"Momentum failed {symbol}: {e}")
        return "sideways"

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=20))
def get_volume_24h(client, symbol):
    try:
        ticker = client.get_ticker(symbol=symbol)
        return float(ticker.get('quoteVolume', 0))
    except Exception as e:
        logger.error(f"Volume failed {symbol}: {e}")
        return 0

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=20))
def get_trade_fees(client, symbol):
    try:
        fee_info = client.get_trade_fee(symbol=symbol)
        return float(fee_info[0]['makerCommission']), float(fee_info[0]['takerCommission'])
    except Exception as e:
        logger.debug(f"Using default fees for {symbol}")
        return 0.004, 0.006

def send_whatsapp_alert(message: str):
    try:
        if not CALLMEBOT_API_KEY or not CALLMEBOT_PHONE:
            return
        url = f"https://api.callmebot.com/whatsapp.php?phone={CALLMEBOT_PHONE}&text={requests.utils.quote(message)}&apikey={CALLMEBOT_API_KEY}"
        resp = requests.get(url, timeout=30)
        if resp.status_code == 200:
            logger.info(f"WhatsApp alert: {message[:50]}...")
    except Exception as e:
        logger.error(f"Alert failed: {e}")

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=20))
def get_balance(client, asset='USDT'):
    try:
        account = client.get_account()
        for bal in account['balances']:
            if bal['asset'] == asset:
                return float(bal['free'])
        return 0.0
    except Exception as e:
        logger.error(f"Balance fetch error: {e}")
        return 0.0

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=20))
def get_historical_metrics(client, symbol):
    try:
        end_time = datetime.now(CST_TZ)
        start_time = end_time - timedelta(days=HISTORY_DAYS)
        start_ms = int(start_time.timestamp() * 1000)
        klines = client.get_historical_klines(symbol, KLINE_INTERVAL, start_ms)
        if len(klines) < max(24, BB_PERIOD, ATR_PERIOD, SMA_PERIOD):
            return None
        opens = np.array([float(k[1]) for k in klines])
        highs = np.array([float(k[2]) for k in klines])
        lows = np.array([float(k[3]) for k in klines])
        closes = np.array([float(k[4]) for k in klines])
        volumes = np.array([float(k[5]) for k in klines])
        fifteen_low = np.min(lows)
        avg_volume = np.mean(volumes)
        rsi = calculate_rsi(closes)
        roc_5d = (closes[-1] - closes[-120]) / closes[-120] * 100 if len(closes) > 120 else 0
        max_dd = ((closes - np.maximum.accumulate(closes)) / np.maximum.accumulate(closes) * 100).min()
        macd, signal, hist = talib.MACD(closes, fastperiod=12, slowperiod=26, signalperiod=9)
        macd_val = macd[-1] if len(macd) > 0 else 0
        signal_val = signal[-1] if len(signal) > 0 else 0
        hist_val = hist[-1] if len(hist) > 0 else 0
        mfi = talib.MFI(highs, lows, closes, volumes, timeperiod=14)[-1] if len(highs) >= 14 else None
        upper, middle, lower = talib.BBANDS(closes, timeperiod=BB_PERIOD, nbdevup=BB_DEV, nbdevdn=BB_DEV)
        bb_upper = upper[-1] if len(upper) > 0 else None
        bb_middle = middle[-1] if len(middle) > 0 else None
        bb_lower = lower[-1] if len(lower) > 0 else None
        slowk, slowd = talib.STOCH(highs, lows, closes, fastk_period=5, slowk_period=3, slowd_period=3)
        stoch_k = slowk[-1] if len(slowk) > 0 else None
        stoch_d = slowd[-1] if len(slowd) > 0 else None
        atr = talib.ATR(highs, lows, closes, timeperiod=ATR_PERIOD)[-1] if len(highs) >= ATR_PERIOD else None
        sma = talib.SMA(closes, timeperiod=SMA_PERIOD)[-1] if len(closes) >= SMA_PERIOD else None
        ema = talib.EMA(closes, timeperiod=EMA_PERIOD)[-1] if len(closes) >= EMA_PERIOD else None
        return {
            'fifteen_low': fifteen_low, 'avg_volume': avg_volume, 'rsi': rsi, 'roc_5d': roc_5d,
            'max_dd': max_dd, 'current_price': closes[-1], 'macd': macd_val, 'signal': signal_val,
            'hist': hist_val, 'mfi': mfi, 'bb_upper': bb_upper, 'bb_middle': bb_middle,
            'bb_lower': bb_lower, 'stoch_k': stoch_k, 'stoch_d': stoch_d, 'atr': atr,
            'sma': sma, 'ema': ema, 'opens': opens, 'highs': highs, 'lows': lows, 'closes': closes
        }
    except Exception as e:
        logger.error(f"History failed {symbol}: {e}")
        return None

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=20))
def fetch_current_data(client, symbol):
    try:
        ticker = client.get_ticker(symbol=symbol)
        return {
            'price': float(ticker['lastPrice']),
            'volume_24h': float(ticker['quoteVolume']),
            'price_change_pct': float(ticker['priceChangePercent'])
        }
    except Exception as e:
        logger.error(f"Current data failed {symbol}: {e}")
        return None

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=20))
def get_order_book_pressure(client, symbol):
    try:
        book = client.get_order_book(symbol=symbol, limit=10)
        bid_qty = sum(float(b[1]) for b in book['bids'])
        ask_qty = sum(float(a[1]) for a in book['asks'])
        return bid_qty > ask_qty
    except Exception as e:
        logger.error(f"Order book failed {symbol}: {e}")
        return False

def is_bullish_candlestick_pattern(metrics):
    if not metrics or len(metrics['opens']) < 3:
        return False
    opens = metrics['opens'][-3:]
    highs = metrics['highs'][-3:]
    lows = metrics['lows'][-3:]
    closes = metrics['closes'][-3:]
    patterns = [
        talib.CDLHAMMER(opens, highs, lows, closes)[-1] > 0,
        talib.CDLINVERTEDHAMMER(opens, highs, lows, closes)[-1] > 0,
        talib.CDLENGULFING(opens, highs, lows, closes)[-1] > 0,
        talib.CDLMORNINGSTAR(opens, highs, lows, closes)[-1] > 0,
        talib.CDL3WHITESOLDIERS(opens, highs, lows, closes)[-1] > 0,
    ]
    return any(patterns)

def is_bearish_candlestick_pattern(metrics):
    if not metrics or len(metrics['opens']) < 3:
        return False
    opens = metrics['opens'][-3:]
    highs = metrics['highs'][-3:]
    lows = metrics['lows'][-3:]
    closes = metrics['closes'][-3:]
    patterns = [
        talib.CDLSHOOTINGSTAR(opens, highs, lows, closes)[-1] > 0,
        talib.CDLHANGINGMAN(opens, highs, lows, closes)[-1] > 0,
        talib.CDLENGULFING(opens, highs, lows, closes)[-1] < 0,
    ]
    return any(patterns)

# === Trading Logic ===
def check_buy_signal(client, symbol):
    if symbol in positions:
        return False
    metrics = get_historical_metrics(client, symbol)
    if not metrics or any(v is None for v in [metrics['rsi'], metrics['mfi'], metrics['bb_lower'], metrics['stoch_k'], metrics['stoch_d'], metrics['atr'], metrics['sma'], metrics['ema']]):
        return False
    current = fetch_current_data(client, symbol)
    if not current:
        return False
    if not (MIN_PRICE <= current['price'] <= MAX_PRICE) or current['volume_24h'] < VOLUME_THRESHOLD:
        return False
    if metrics['rsi'] <= 50 or metrics['mfi'] > 70 or metrics['macd'] <= metrics['signal'] or metrics['hist'] <= 0:
        return False
    if get_momentum_status(client, symbol) != 'bullish':
        return False
    if current['price'] > metrics['bb_lower'] * 1.005 or metrics['stoch_k'] > 30 or metrics['ema'] <= metrics['sma']:
        return False
    if not is_bullish_candlestick_pattern(metrics):
        return False
    logger.debug(f"BUY SIGNAL: {symbol}")
    return True

def execute_buy(client, symbol):
    balance = get_balance(client)
    if balance <= MIN_BALANCE:
        send_whatsapp_alert("Low balance, skipping buy")
        return
    current = fetch_current_data(client, symbol)
    if not current:
        return
    current_price = current['price']
    metrics = get_historical_metrics(client, symbol)
    if not metrics or metrics['atr'] is None:
        return
    atr = metrics['atr']
    maker_fee, taker_fee = get_trade_fees(client, symbol)
    alloc = min((balance - MIN_BALANCE) * RISK_PER_TRADE, balance - MIN_BALANCE)
    qty = alloc / current_price
    buy_price = current_price * (1 - 0.001 - atr / current_price * 0.5)
    order = place_limit_buy(client, symbol, buy_price, qty, current_price)
    if not order:
        return
    start = time.time()
    while time.time() - start < ORDER_TIMEOUT:
        order_status = client.get_order(symbol=symbol, orderId=order['orderId'])
        if order_status['status'] == 'FILLED':
            entry_price = float(order_status['cummulativeQuoteQty']) / float(order_status['executedQty'])
            positions[symbol] = {
                'qty': float(order_status['executedQty']),
                'entry_price': entry_price,
                'entry_time': datetime.now(CST_TZ),
                'buy_fee': maker_fee
            }
            send_whatsapp_alert(f"BUY {symbol} @ {entry_price:.6f}")
            logger.info(f"BUY FILLED: {symbol} @ {entry_price}")
            return
        time.sleep(30)
    cancel_order(client, symbol, order['orderId'])

def check_sell_signal(client, symbol, position):
    current = fetch_current_data(client, symbol)
    if not current:
        return False, None, None
    current_price = current['price']
    maker_fee, taker_fee = get_trade_fees(client, symbol)
    buy_fee = position['buy_fee']
    profit_pct = (current_price - position['entry_price']) / position['entry_price'] - (buy_fee + taker_fee)
    if profit_pct < PROFIT_TARGET:
        return False, None, None
    metrics = get_historical_metrics(client, symbol)
    if not metrics:
        return False, None, None
    if is_bearish_candlestick_pattern(metrics) or current_price >= metrics['bb_upper'] * 0.995:
        return True, 'market', taker_fee
    return True, 'limit', maker_fee

def execute_sell(client, symbol, position):
    qty = position['qty']
    current = fetch_current_data(client, symbol)
    if not current:
        return
    current_price = current['price']
    metrics = get_historical_metrics(client, symbol)
    if not metrics or metrics['atr'] is None:
        return
    should_sell, order_type, sell_fee = check_sell_signal(client, symbol, position)
    if not should_sell:
        return
    if order_type == 'market':
        order = place_market_sell(client, symbol, qty)
        if order:
            exit_price = sum(float(f['price']) * float(f['qty']) for f in order['fills']) / sum(float(f['qty']) for f in order['fills'])
            profit = (exit_price - position['entry_price']) * qty - (sell_fee * exit_price * qty)
            del positions[symbol]
            send_whatsapp_alert(f"SOLD {symbol} @ {exit_price:.6f} Profit: ${profit:.2f}")
            logger.info(f"SOLD {symbol} @ {exit_price} Profit: ${profit:.2f}")
    else:
        sell_price = position['entry_price'] * (1 + PROFIT_TARGET + position['buy_fee'] + sell_fee) + metrics['atr'] * 0.5
        order = place_limit_sell(client, symbol, sell_price, qty)
        if order:
            start = time.time()
            while time.time() - start < ORDER_TIMEOUT:
                order_status = client.get_order(symbol=symbol, orderId=order['orderId'])
                if order_status['status'] == 'FILLED':
                    exit_price = float(order_status['cummulativeQuoteQty']) / float(order_status['executedQty'])
                    profit = (exit_price - position['entry_price']) * float(order_status['executedQty']) - (sell_fee * exit_price * float(order_status['executedQty']))
                    del positions[symbol]
                    send_whatsapp_alert(f"SOLD LIMIT {symbol} @ {exit_price:.6f} Profit: ${profit:.2f}")
                    return
                time.sleep(30)
            cancel_order(client, symbol, order['orderId'])

def get_all_usdt_symbols(client):
    try:
        info = client.get_exchange_info()
        return [s['symbol'] for s in info['symbols'] if s['symbol'].endswith('USDT') and s['status'] == 'TRADING']
    except Exception as e:
        logger.error(f"Symbols fetch failed: {e}")
        return []

def place_limit_buy(client, symbol, price, qty, current_price):
    adjusted, error = validate_and_adjust_order(client, symbol, 'BUY', ORDER_TYPE_LIMIT, qty, price, current_price)
    if error:
        logger.error(f"BUY validation failed {symbol}: {error}")
        return None
    try:
        order = client.order_limit_buy(symbol=symbol, quantity=adjusted['quantity'], price=adjusted['price'])
        logger.info(f"Placed BUY LIMIT {symbol} @ {adjusted['price']} qty {adjusted['quantity']}")
        return order
    except BinanceOrderException as e:
        logger.error(f"BUY failed {symbol}: {e}")
        return None

def place_market_buy(client, symbol, qty, current_price):
    adjusted, error = validate_and_adjust_order(client, symbol, 'BUY', ORDER_TYPE_MARKET, qty, current_price=current_price)
    if error:
        logger.error(f"MARKET BUY failed {symbol}: {error}")
        return None
    try:
        order = client.order_market_buy(symbol=symbol, quantity=adjusted['quantity'])
        logger.info(f"Placed MARKET BUY {symbol} qty {adjusted['quantity']}")
        return order
    except BinanceOrderException as e:
        logger.error(f"MARKET BUY failed {symbol}: {e}")
        return None

def place_market_sell(client, symbol, qty):
    adjusted, error = validate_and_adjust_order(client, symbol, 'SELL', ORDER_TYPE_MARKET, qty)
    if error:
        logger.error(f"MARKET SELL failed {symbol}: {error}")
        return None
    try:
        order = client.order_market_sell(symbol=symbol, quantity=adjusted['quantity'])
        logger.info(f"Placed MARKET SELL {symbol} qty {adjusted['quantity']}")
        return order
    except BinanceOrderException as e:
        logger.error(f"MARKET SELL failed {symbol}: {e}")
        return None

def place_limit_sell(client, symbol, price, qty):
    adjusted, error = validate_and_adjust_order(client, symbol, 'SELL', ORDER_TYPE_LIMIT, qty, price)
    if error:
        logger.error(f"SELL validation failed {symbol}: {error}")
        return None
    try:
        order = client.order_limit_sell(symbol=symbol, quantity=adjusted['quantity'], price=adjusted['price'])
        logger.info(f"Placed SELL LIMIT {symbol} @ {adjusted['price']} qty {adjusted['quantity']}")
        return order
    except BinanceOrderException as e:
        logger.error(f"SELL failed {symbol}: {e}")
        return None

def cancel_order(client, symbol, order_id):
    try:
        client.cancel_order(symbol=symbol, orderId=order_id)
        logger.info(f"Canceled order {order_id} for {symbol}")
    except Exception as e:
        logger.error(f"Cancel failed {symbol}: {e}")

# === MAIN LOOP ===
if __name__ == "__main__":
    logger.info("Starting Crypto Trading Bot on Binance.US")
    if not API_KEY or not API_SECRET:
        logger.error("API keys not set.")
        exit(1)

    client = Client(API_KEY, API_SECRET, tld='us')

    try:
        if client.ping() == {}:
            print("API connection successful. Binance.US is reachable.")
            time.sleep(1)
        else:
            logger.error("Ping failed.")
            exit(1)
    except Exception as e:
        logger.error(f"API ping failed: {e}")
        exit(1)

    symbols = get_all_usdt_symbols(client)
    logger.info(f"Monitoring {len(symbols)} USDT pairs")

    try:
        while True:
            print_status_dashboard(client)

            balance = get_balance(client)
            if balance < MIN_BALANCE + 10:
                send_whatsapp_alert(f"Low USDT: ${balance:.2f}")

            for symbol in symbols:
                if check_buy_signal(client, symbol):
                    execute_buy(client, symbol)
                if symbol in positions:
                    execute_sell(client, symbol, positions[symbol])

            time.sleep(LOOP_INTERVAL)
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
    finally:
        logger.info("Bot stopped.")
