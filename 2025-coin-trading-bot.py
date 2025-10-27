import os
import time
import logging
import math
from logging.handlers import TimedRotatingFileHandler
from binance.client import Client
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
MAX_PRICE = 1000.00  # Max price for consideration
MIN_PRICE = 1.00   # Min price for consideration
CURRENT_PRICE_MAX = 1000.00  # Filter coins with current price < $1000.00
LOOP_INTERVAL = 60  # 1 minute for real-time trading
LOG_FILE = "crypto_trading_bot.log"
VOLUME_THRESHOLD = 15000  # 24h volume > $15,000
RSI_PERIOD = 14
BB_PERIOD = 20
BB_DEV = 2
STOCH_K_PERIOD = 14
STOCH_D_PERIOD = 3
ATR_PERIOD = 14
SMA_PERIOD = 50
EMA_PERIOD = 21
HISTORY_DAYS = 60  # Increased to ensure enough data for SMA/EMA
KLINE_INTERVAL = '1h'  # Hourly klines for history
PROFIT_TARGET = 0.008  # 0.8% profit
RISK_PER_TRADE = 0.10  # 10% of balance per trade
MIN_BALANCE = 2.0  # Minimum USDT to keep
ORDER_TIMEOUT = 300  # 5 minutes for limit orders

# Set Binance.US API keys
API_KEY = os.getenv('BINANCE_API_KEY')
API_SECRET = os.getenv('BINANCE_API_SECRET')

api_url = "https://api.binance.us"

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s:%(message)s',
    handlers=[
        TimedRotatingFileHandler(LOG_FILE, when="midnight", interval=1, backupCount=7),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Timezone setup
CST_TZ = pytz.timezone('America/Chicago')

def format_time_central(ts_ms):
    if not ts_ms:
        return "N/A"
    dt_utc = datetime.fromtimestamp(ts_ms / 1000, tz=pytz.UTC)
    dt_central = dt_utc.astimezone(CST_TZ)
    return dt_central.strftime("%Y-%m-%d %H:%M:%S CST")

# In-memory positions (symbol: {'qty': float, 'entry_price': float, 'entry_time': datetime, 'buy_fee': float})
positions = {}

# === Helper Functions ===
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
    """Fetch and parse exchange filters for a symbol."""
    try:
        info = client.get_exchange_info()
        symbol_info = next(s for s in info['symbols'] if s['symbol'] == symbol)
        filters = {}
        for f in symbol_info['filters']:
            ftype = f['filterType']
            params = {}
            if ftype == 'LOT_SIZE':
                params = {
                    'minQty': float(f.get('minQty')),
                    'maxQty': float(f.get('maxQty')),
                    'stepSize': float(f.get('stepSize'))
                }
            elif ftype == 'MIN_NOTIONAL':
                params = {'minNotional': float(f.get('minNotional'))}
            elif ftype == 'PRICE_FILTER':
                params = {
                    'minPrice': float(f.get('minPrice')),
                    'maxPrice': float(f.get('maxPrice')),
                    'tickSize': float(f.get('tickSize'))
                }
            filters[ftype] = params
        return filters
    except Exception as e:
        logger.error(f"Failed to get filters for {symbol}: {e}")
        return {}

def round_to_step_size(value, step_size):
    """Round value to the nearest multiple of step_size."""
    if value == 0 or step_size == 0:
        return 0
    return round(value / step_size) * step_size

def round_to_tick_size(value, tick_size):
    """Round value to the nearest multiple of tick_size."""
    if value == 0 or tick_size == 0:
        return 0
    return round(value / tick_size) * tick_size

def validate_and_adjust_order(client, symbol, side, order_type, quantity, price=None, current_price=None):
    """Validate and adjust order parameters based on exchange filters."""
    filters = get_symbol_filters(client, symbol)
    adjusted_qty = quantity
    adjusted_price = price

    # Adjust quantity for LOT_SIZE
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
            return None, f"Quantity below minQty after adjustment: {adjusted_qty}"

    # Adjust price for PRICE_FILTER if provided
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
                return None, f"Price below minPrice after adjustment: {adjusted_price}"

    # Check MIN_NOTIONAL
    min_notional_filter = filters.get('MIN_NOTIONAL', {})
    min_notional = min_notional_filter.get('minNotional', 0)
    if min_notional > 0:
        effective_price = adjusted_price or current_price
        if effective_price is None:
            return None, "No effective price available for MIN_NOTIONAL check"
        notional = adjusted_qty * effective_price
        if notional < min_notional:
            # Adjust qty upward
            needed_qty = min_notional / effective_price
            adjusted_qty = max(adjusted_qty, needed_qty)
            # Re-apply LOT_SIZE rounding
            if lot_filter:
                step_size = lot_filter.get('stepSize', 0)
                adjusted_qty = round_to_step_size(adjusted_qty, step_size)
                max_qty = lot_filter.get('maxQty', float('inf'))
                if adjusted_qty > max_qty:
                    return None, f"Adjusted qty exceeds maxQty for MIN_NOTIONAL: {adjusted_qty}"
            notional = adjusted_qty * effective_price
            if notional < min_notional:
                return None, f"Still below MIN_NOTIONAL after adjustment: {notional}"

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
        bullish_score = sum([
            np.any(talib.CDLHAMMER(opens, highs, lows, closes) > 0),
            np.any(talib.CDLENGULFING(opens, highs, lows, closes) > 0),
            np.any(talib.CDLMORNINGSTAR(opens, highs, lows, closes) > 0)
        ])
        bearish_score = sum([
            np.any(talib.CDLSHOOTINGSTAR(opens, highs, lows, closes) > 0),
            np.any(talib.CDLENGULFING(opens, highs, lows, closes) < 0),
            np.any(talib.CDLEVENINGSTAR(opens, highs, lows, closes) > 0)
        ])
        if bullish_score > bearish_score:
            return "bullish"
        elif bearish_score > bullish_score:
            return "bearish"
        return "sideways"
    except Exception as e:
        logger.error(f"Momentum status failed for {symbol}: {e}")
        return "sideways"

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=20))
def get_volume_24h(client, symbol):
    try:
        ticker = client.get_ticker(symbol=symbol)
        return float(ticker.get('quoteVolume', 0))
    except Exception as e:
        logger.error(f"Volume fetch failed for {symbol}: {e}")
        return 0

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=20))
def get_trade_fees(client, symbol):
    try:
        fee_info = client.get_trade_fee(symbol=symbol)
        maker_fee = float(fee_info[0]['makerCommission'])
        taker_fee = float(fee_info[0]['takerCommission'])
        return maker_fee, taker_fee
    except Exception as e:
        logger.error(f"Fee fetch failed for {symbol}: {e}")
        return 0.004, 0.006  # Fallback to Tier I defaults

def send_whatsapp_alert(message: str):
    try:
        if not CALLMEBOT_API_KEY or not CALLMEBOT_PHONE:
            logger.error("CallMeBot config not set.")
            return
        url = f"https://api.callmebot.com/whatsapp.php?phone={CALLMEBOT_PHONE}&text={requests.utils.quote(message)}&apikey={CALLMEBOT_API_KEY}"
        resp = requests.get(url, timeout=30)
        if resp.status_code == 200:
            logger.info(f"Alert sent: {message[:50]}...")
        else:
            logger.error(f"Failed to send alert: {resp.status_code} - {resp.text}")
    except Exception as e:
        logger.error(f"Error sending alert: {e}")

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=20))
def get_balance(client, asset='USDT'):
    try:
        account = client.get_account()
        for bal in account['balances']:
            if bal['asset'] == asset:
                return float(bal['free'])
        return 0.0
    except Exception as e:
        logger.error(f"Error getting balance: {e}")
        return 0.0

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=20))
def get_historical_metrics(client, symbol):
    try:
        end_time = datetime.now(CST_TZ)
        start_time = end_time - timedelta(days=HISTORY_DAYS)
        start_ms = int(start_time.timestamp() * 1000)
        klines = client.get_historical_klines(symbol, KLINE_INTERVAL, start_ms)
        if len(klines) < max(24, BB_PERIOD, STOCH_K_PERIOD, ATR_PERIOD, SMA_PERIOD):
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
        upper, middle, lower = talib.BBANDS(closes, timeperiod=BB_PERIOD, nbdevup=BB_DEV, nbdevdn=BB_DEV, matype=0)
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
            'fifteen_low': fifteen_low,
            'avg_volume': avg_volume,
            'rsi': rsi,
            'roc_5d': roc_5d,
            'max_dd': max_dd,
            'current_price': closes[-1],
            'macd': macd_val,
            'signal': signal_val,
            'hist': hist_val,
            'mfi': mfi,
            'bb_upper': bb_upper,
            'bb_middle': bb_middle,
            'bb_lower': bb_lower,
            'stoch_k': stoch_k,
            'stoch_d': stoch_d,
            'atr': atr,
            'sma': sma,
            'ema': ema,
            'opens': opens,
            'highs': highs,
            'lows': lows,
            'closes': closes
        }
    except Exception as e:
        logger.error(f"History fetch failed for {symbol}: {e}")
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
        logger.error(f"Current data fetch failed for {symbol}: {e}")
        return None

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=20))
def get_order_book_pressure(client, symbol):
    try:
        book = client.get_order_book(symbol=symbol, limit=10)
        bid_qty = sum(float(b[1]) for b in book['bids'])
        ask_qty = sum(float(a[1]) for a in book['asks'])
        return bid_qty > ask_qty  # True if more buying pressure
    except Exception as e:
        logger.error(f"Order book fetch failed for {symbol}: {e}")
        return False

def is_bullish_candlestick_pattern(metrics):
    if metrics is None:
        return False
    opens = metrics['opens'][-3:]
    highs = metrics['highs'][-3:]
    lows = metrics['lows'][-3:]
    closes = metrics['closes'][-3:]
    if len(opens) < 3:
        return False
    patterns = [
        talib.CDLHAMMER(opens, highs, lows, closes)[-1] > 0,
        talib.CDLINVERTEDHAMMER(opens, highs, lows, closes)[-1] > 0,
        talib.CDLENGULFING(opens, highs, lows, closes)[-1] > 0,
        talib.CDLMORNINGSTAR(opens, highs, lows, closes)[-1] > 0,
        talib.CDL3WHITESOLDIERS(opens, highs, lows, closes)[-1] > 0,
        talib.CDLPIERCING(opens, highs, lows, closes)[-1] > 0,
        talib.CDLMORNINGDOJISTAR(opens, highs, lows, closes)[-1] > 0,
        talib.CDL3LINESTRIKE(opens, highs, lows, closes)[-1] > 0,
        talib.CDLBULLISHABANDONEDBABY(opens, highs, lows, closes)[-1] > 0
    ]
    return any(patterns)

def is_bearish_candlestick_pattern(metrics):
    if metrics is None:
        return False
    opens = metrics['opens'][-3:]
    highs = metrics['highs'][-3:]
    lows = metrics['lows'][-3:]
    closes = metrics['closes'][-3:]
    if len(opens) < 3:
        return False
    patterns = [
        talib.CDLSHOOTINGSTAR(opens, highs, lows, closes)[-1] > 0,
        talib.CDLHANGINGMAN(opens, highs, lows, closes)[-1] > 0,
        talib.CDLENGULFING(opens, highs, lows, closes)[-1] < 0,
        talib.CDLEVENINGSTAR(opens, highs, lows, closes)[-1] > 0,
        talib.CDL3BLACKCROWS(opens, highs, lows, closes)[-1] > 0,
        talib.CDLGRAVESTONEDOJI(opens, highs, lows, closes)[-1] > 0,
        talib.CDLEVENINGDOJISTAR(opens, highs, lows, closes)[-1] > 0,
        talib.CDL3LINESTRIKE(opens, highs, lows, closes)[-1] < 0,
        talib.CDLBEARISHABANDONEDBABY(opens, highs, lows, closes)[-1] > 0
    ]
    return any(patterns)

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
    if metrics['max_dd'] < -10 or metrics['roc_5d'] <= 0 or metrics['rsi'] <= 50 or metrics['mfi'] > 70:
        return False
    if metrics['macd'] <= metrics['signal'] or metrics['hist'] <= 0:
        return False
    momentum = get_momentum_status(client, symbol)
    if momentum != 'bullish':
        return False
    if current['price'] > metrics['fifteen_low'] * 1.001:
        return False
    if not get_order_book_pressure(client, symbol):
        return False
    if not is_bullish_candlestick_pattern(metrics):
        return False
    # Bollinger Bands: price near or below lower band
    if current['price'] > metrics['bb_lower'] * 1.005:
        return False
    # Stochastic Oscillator: %K crosses above %D and is oversold (<30)
    if metrics['stoch_k'] <= metrics['stoch_d'] or metrics['stoch_k'] > 30:
        return False
    # Moving Averages: EMA above SMA (bullish trend)
    if metrics['ema'] <= metrics['sma']:
        return False
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
    # Adjust slippage buffer based on ATR
    buy_price = current_price * (1 - 0.001 - atr / current_price * 0.5)  # Dynamic slippage
    order = place_limit_buy(client, symbol, buy_price, qty, current_price)
    if not order:
        return
    start = time.time()
    while time.time() - start < ORDER_TIMEOUT:
        order_status = client.get_order(symbol=symbol, orderId=order['orderId'])
        if order_status['status'] == 'FILLED':
            entry_time = datetime.now(CST_TZ)
            entry_price = float(order_status['cummulativeQuoteQty']) / float(order_status['executedQty'])
            buy_fee = maker_fee  # Limit buy uses maker fee
            positions[symbol] = {'qty': float(order_status['executedQty']), 'entry_price': entry_price, 'entry_time': entry_time, 'buy_fee': buy_fee}
            send_whatsapp_alert(f"BUY filled {symbol} @ {entry_price} qty {order_status['executedQty']} fee {buy_fee*100:.2f}%")
            return
        time.sleep(30)
    cancel_order(client, symbol, order['orderId'])
    # Fallback to market if still signal
    if check_buy_signal(client, symbol):
        try:
            mkt_order = place_market_buy(client, symbol, qty, current_price)
            if mkt_order:
                entry_time = datetime.now(CST_TZ)
                entry_price = sum(float(f['price']) * float(f['qty']) for f in mkt_order['fills']) / sum(float(f['qty']) for f in mkt_order['fills'])
                buy_fee = taker_fee  # Market buy uses taker fee
                positions[symbol] = {'qty': sum(float(f['qty']) for f in mkt_order['fills']), 'entry_price': entry_price, 'entry_time': entry_time, 'buy_fee': buy_fee}
                send_whatsapp_alert(f"BUY market filled {symbol} @ {entry_price} qty {mkt_order['executedQty']} fee {buy_fee*100:.2f}%")
        except Exception as e:
            logger.error(f"Market buy fallback failed: {e}")

def check_sell_signal(client, symbol, position):
    current = fetch_current_data(client, symbol)
    if not current:
        return False, None, None
    current_price = current['price']
    maker_fee, taker_fee = get_trade_fees(client, symbol)
    # Use actual buy fee from position
    buy_fee = position['buy_fee']
    profit_pct = (current_price - position['entry_price']) / position['entry_price'] - (buy_fee + taker_fee)
    if profit_pct < PROFIT_TARGET:
        return False, None, None
    klines = client.get_klines(symbol=symbol, interval='1m', limit=2)
    prev_close = float(klines[0][4])
    delta = (current_price - prev_close) / prev_close
    metrics = get_historical_metrics(client, symbol)
    if not metrics or any(v is None for v in [metrics['stoch_k'], metrics['stoch_d'], metrics['atr'], metrics['sma'], metrics['ema']]):
        return False, None, None
    # Bearish candlestick pattern
    if is_bearish_candlestick_pattern(metrics):
        return True, 'market', taker_fee
    # Bollinger Bands: price near or above upper band
    if current_price >= metrics['bb_upper'] * 0.995:
        return True, 'market', taker_fee
    # Stochastic Oscillator: %K crosses below %D and is overbought (>70)
    if metrics['stoch_k'] >= metrics['stoch_d'] or metrics['stoch_k'] < 70:
        pass  # Not overbought yet, check other conditions
    else:
        return True, 'market', taker_fee
    # Moving Averages: EMA crosses below SMA or price significantly above EMA
    if metrics['ema'] < metrics['sma'] or current_price > metrics['ema'] * 1.02:
        return True, 'market', taker_fee
    # ATR-based exit: price movement exceeds 2x ATR
    if current_price > position['entry_price'] + 2 * metrics['atr']:
        return True, 'market', taker_fee
    if delta > 0.005:  # Fast rise
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
    atr = metrics['atr']
    should_sell, order_type, sell_fee = check_sell_signal(client, symbol, position)
    if not should_sell:
        return
    if order_type == 'market':
        order = place_market_sell(client, symbol, qty)
        if order:
            exit_time = datetime.now(CST_TZ)
            exit_price = sum(float(f['price']) * float(f['qty']) for f in order['fills']) / sum(float(f['qty']) for f in order['fills'])
            profit = (exit_price - position['entry_price']) * sum(float(f['qty']) for f in order['fills']) - (sell_fee * exit_price * sum(float(f['qty']) for f in order['fills']))
            del positions[symbol]
            send_whatsapp_alert(f"SOLD market {symbol} @ {exit_price} profit {profit:.2f} USDT fee {sell_fee*100:.2f}%")
    else:
        # Adjust sell price based on ATR
        sell_price = position['entry_price'] * (1 + PROFIT_TARGET + position['buy_fee'] + sell_fee) + atr * 0.5
        order = place_limit_sell(client, symbol, sell_price, qty)
        if order:
            # For limit sell, wait for fill similar to buy
            start = time.time()
            while time.time() - start < ORDER_TIMEOUT:
                order_status = client.get_order(symbol=symbol, orderId=order['orderId'])
                if order_status['status'] == 'FILLED':
                    exit_time = datetime.now(CST_TZ)
                    exit_price = float(order_status['cummulativeQuoteQty']) / float(order_status['executedQty'])
                    profit = (exit_price - position['entry_price']) * float(order_status['executedQty']) - (sell_fee * exit_price * float(order_status['executedQty']))
                    del positions[symbol]
                    send_whatsapp_alert(f"SOLD limit {symbol} @ {exit_price} profit {profit:.2f} USDT fee {sell_fee*100:.2f}%")
                    return
                time.sleep(30)
            cancel_order(client, symbol, order['orderId'])
            # Fallback to market if still signal
            should_sell_retry, _, _ = check_sell_signal(client, symbol, position)
            if should_sell_retry:
                execute_sell(client, symbol, position)  # Recursive fallback

def get_all_usdt_symbols(client):
    try:
        info = client.get_exchange_info()
        return [s['symbol'] for s in info['symbols'] if s['symbol'].endswith('USDT') and s['status'] == 'TRADING']
    except Exception as e:
        logger.error(f"Failed to get symbols: {e}")
        return []

def place_limit_buy(client, symbol, price, qty, current_price):
    adjusted, error = validate_and_adjust_order(client, symbol, 'BUY', 'LIMIT', qty, price, current_price)
    if error:
        logger.error(f"Order validation failed for {symbol}: {error}")
        send_whatsapp_alert(f"BUY validation failed {symbol}: {error}")
        return None
    try:
        order = client.order_limit_buy(symbol=symbol, quantity=adjusted['quantity'], price=adjusted['price'])
        logger.info(f"Placed limit buy for {symbol} at {adjusted['price']} qty {adjusted['quantity']}: {order}")
        send_whatsapp_alert(f"Placed BUY limit {symbol} @ {adjusted['price']} qty {adjusted['quantity']}")
        return order
    except BinanceOrderException as e:
        if 'Filter failure' in str(e) or '-201' in str(e.code):
            logger.error(f"Filter error in buy for {symbol}: {e}. Retrying once...")
            # Retry once with fresh validation
            adjusted_retry, error_retry = validate_and_adjust_order(client, symbol, 'BUY', 'LIMIT', qty, price, current_price)
            if not error_retry:
                try:
                    order = client.order_limit_buy(symbol=symbol, quantity=adjusted_retry['quantity'], price=adjusted_retry['price'])
                    return order
                except BinanceOrderException:
                    pass
        logger.error(f"Buy order failed for {symbol}: {e}")
        send_whatsapp_alert(f"BUY failed {symbol}: {e.message}")
        return None

def place_market_buy(client, symbol, qty, current_price):
    adjusted, error = validate_and_adjust_order(client, symbol, 'BUY', 'MARKET', qty, current_price=current_price)
    if error:
        logger.error(f"Order validation failed for {symbol}: {error}")
        send_whatsapp_alert(f"MARKET BUY validation failed {symbol}: {error}")
        return None
    try:
        order = client.order_market_buy(symbol=symbol, quantity=adjusted['quantity'])
        logger.info(f"Placed market buy for {symbol} qty {adjusted['quantity']}: {order}")
        send_whatsapp_alert(f"Placed BUY market {symbol} qty {adjusted['quantity']}")
        return order
    except BinanceOrderException as e:
        if 'Filter failure' in str(e) or '-201' in str(e.code):
            logger.error(f"Filter error in market buy for {symbol}: {e}. Retrying once...")
            # Retry once
            adjusted_retry, _ = validate_and_adjust_order(client, symbol, 'BUY', 'MARKET', qty, current_price=current_price)
            if adjusted_retry:
                try:
                    order = client.order_market_buy(symbol=symbol, quantity=adjusted_retry['quantity'])
                    return order
                except BinanceOrderException:
                    pass
        logger.error(f"Market buy order failed for {symbol}: {e}")
        send_whatsapp_alert(f"MARKET BUY failed {symbol}: {e.message}")
        return None

def place_market_sell(client, symbol, qty):
    adjusted, error = validate_and_adjust_order(client, symbol, 'SELL', 'MARKET', qty)
    if error:
        logger.error(f"Order validation failed for {symbol}: {error}")
        send_whatsapp_alert(f"MARKET SELL validation failed {symbol}: {error}")
        return None
    try:
        order = client.order_market_sell(symbol=symbol, quantity=adjusted['quantity'])
        logger.info(f"Placed market sell for {symbol} qty {adjusted['quantity']}: {order}")
        send_whatsapp_alert(f"Placed SELL market {symbol} qty {adjusted['quantity']}")
        return order
    except BinanceOrderException as e:
        if 'Filter failure' in str(e) or '-201' in str(e.code):
            logger.error(f"Filter error in sell for {symbol}: {e}. Retrying once...")
            # Retry once
            adjusted_retry, _ = validate_and_adjust_order(client, symbol, 'SELL', 'MARKET', qty)
            if adjusted_retry:
                try:
                    order = client.order_market_sell(symbol=symbol, quantity=adjusted_retry['quantity'])
                    return order
                except BinanceOrderException:
                    pass
        logger.error(f"Sell order failed for {symbol}: {e}")
        send_whatsapp_alert(f"SELL failed {symbol}: {e.message}")
        return None

def place_limit_sell(client, symbol, price, qty):
    adjusted, error = validate_and_adjust_order(client, symbol, 'SELL', 'LIMIT', qty, price)
    if error:
        logger.error(f"Order validation failed for {symbol}: {error}")
        send_whatsapp_alert(f"SELL validation failed {symbol}: {error}")
        return None
    try:
        order = client.order_limit_sell(symbol=symbol, quantity=adjusted['quantity'], price=adjusted['price'])
        logger.info(f"Placed limit sell for {symbol} at {adjusted['price']} qty {adjusted['quantity']}: {order}")
        send_whatsapp_alert(f"Placed SELL limit {symbol} @ {adjusted['price']} qty {adjusted['quantity']}")
        return order
    except BinanceOrderException as e:
        if 'Filter failure' in str(e) or '-201' in str(e.code):
            logger.error(f"Filter error in sell for {symbol}: {e}. Retrying once...")
            # Retry once with fresh validation
            adjusted_retry, error_retry = validate_and_adjust_order(client, symbol, 'SELL', 'LIMIT', qty, price)
            if not error_retry:
                try:
                    order = client.order_limit_sell(symbol=symbol, quantity=adjusted_retry['quantity'], price=adjusted_retry['price'])
                    return order
                except BinanceOrderException:
                    pass
        logger.error(f"Sell order failed for {symbol}: {e}")
        send_whatsapp_alert(f"SELL failed {symbol}: {e.message}")
        return None

def cancel_order(client, symbol, order_id):
    try:
        client.cancel_order(symbol=symbol, orderId=order_id)
        logger.info(f"Canceled order {order_id} for {symbol}")
        send_whatsapp_alert(f"Canceled order {symbol}")
    except Exception as e:
        logger.error(f"Cancel failed for {symbol}: {e}")

# === MAIN EXECUTION ===
if __name__ == "__main__":
    logger.info("Starting World's Most Profitable Crypto Trading Bot on Binance.US")
    if not API_KEY or not API_SECRET:
        logger.error("API keys not set.")
        exit(1)
    client = Client(API_KEY, API_SECRET, tld='us')
    symbols = get_all_usdt_symbols(client)
    logger.info(f"Monitoring {len(symbols)} USDT pairs")

    try:
        while True:
            balance = get_balance(client)
            if balance < MIN_BALANCE + 10:
                send_whatsapp_alert(f"Low USDT balance: {balance}")

            for symbol in symbols:
                if check_buy_signal(client, symbol):
                    execute_buy(client, symbol)
                if symbol in positions:
                    execute_sell(client, symbol, positions[symbol])

            time.sleep(LOOP_INTERVAL)
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt: Shutting down")
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
    finally:
        logger.info("Shutdown complete")
