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
from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, func
from sqlalchemy.orm import declarative_base, sessionmaker
import pandas as pd

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
HISTORY_DAYS = 30  # Track 30 days of history
KLINE_INTERVAL = '1h'  # Hourly klines for history
PROFIT_TARGET = 0.008  # 0.8% profit
RISK_PER_TRADE = 0.10  # 10% of balance per trade
MIN_BALANCE = 2.0  # Minimum USDT to keep
ORDER_TIMEOUT = 300  # 5 minutes for limit orders
DATABASE_URL = "sqlite:///crypto_bot.db"  # SQLite database

# Set Binance.US API keys
API_KEY = os.getenv('BINANCE_API_KEY')
API_SECRET = os.getenv('BINANCE_API_SECRET')

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

# === SQLAlchemy Setup ===
Base = declarative_base()

class Candle(Base):
    __tablename__ = 'candles'
    id = Column(Integer, primary_key=True)
    symbol = Column(String, nullable=False)
    timestamp = Column(DateTime, nullable=False)
    open = Column(Float, nullable=False)
    high = Column(Float, nullable=False)
    low = Column(Float, nullable=False)
    close = Column(Float, nullable=False)
    volume = Column(Float, nullable=False)

class Trade(Base):
    __tablename__ = 'trades'
    id = Column(Integer, primary_key=True)
    symbol = Column(String, nullable=False)
    entry_time = Column(DateTime, nullable=False)
    entry_price = Column(Float, nullable=False)
    qty = Column(Float, nullable=False)
    exit_time = Column(DateTime)
    exit_price = Column(Float)
    profit = Column(Float)

class Position(Base):
    __tablename__ = 'positions'
    id = Column(Integer, primary_key=True)
    symbol = Column(String, nullable=False)
    qty = Column(Float, nullable=False)
    entry_price = Column(Float, nullable=False)
    entry_time = Column(DateTime, nullable=False)
    buy_fee = Column(Float, nullable=False)  # Store actual buy fee

engine = create_engine(DATABASE_URL, echo=False)
Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine)

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
        info = client.get_exchange_info(symbol=symbol)
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

    # Optionally test the order (uncomment if using test orders)
    # try:
    #     test_params = {'symbol': symbol, 'side': side, 'type': order_type, 'quantity': adjusted_qty}
    #     if adjusted_price:
    #         test_params['price'] = adjusted_price
    #     client.create_test_order(**test_params)
    # except BinanceOrderException as e:
    #     return None, f"Test order failed: {e.message}"

    return {'quantity': adjusted_qty, 'price': adjusted_price}, None

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=20))
def get_momentum_status(client, symbol):
    try:
        klines = client.get_klines(symbol=symbol, interval='1d', limit=10)
        if len(klines) < 3:
            return "sideways"
        opens = [float(k[1]) for k in klines]
        highs = [float(k[2]) for k in klines]
        lows = [float(k[3]) for k in klines]
        closes = [float(k[4]) for k in klines]
        bullish_score = sum([
            np.any(talib.CDLHAMMER(np.array(opens), np.array(highs), np.array(lows), np.array(closes)) > 0),
            np.any(talib.CDLENGULFING(np.array(opens), np.array(highs), np.array(lows), np.array(closes)) > 0),
            np.any(talib.CDLMORNINGSTAR(np.array(opens), np.array(highs), np.array(lows), np.array(closes)) > 0)
        ])
        bearish_score = sum([
            np.any(talib.CDLSHOOTINGSTAR(np.array(opens), np.array(highs), np.array(lows), np.array(closes)) > 0),
            np.any(talib.CDLENGULFING(np.array(opens), np.array(highs), np.array(lows), np.array(closes)) < 0),
            np.any(talib.CDLEVENINGSTAR(np.array(opens), np.array(highs), np.array(lows), np.array(closes)) > 0)
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

def get_open_positions(session):
    return {p.symbol: {'qty': p.qty, 'entry_price': p.entry_price, 'entry_time': p.entry_time, 'buy_fee': p.buy_fee} for p in session.query(Position).all()}

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

def store_historical_data(client, session, symbol):
    try:
        end_time = datetime.now(CST_TZ)
        start_time = end_time - timedelta(days=HISTORY_DAYS)
        start_ms = int(start_time.timestamp() * 1000)
        klines = client.get_historical_klines(symbol, KLINE_INTERVAL, start_ms)
        for kline in klines:
            ts = datetime.fromtimestamp(kline[0]/1000, tz=CST_TZ)
            candle = Candle(
                symbol=symbol,
                timestamp=ts,
                open=float(kline[1]),
                high=float(kline[2]),
                low=float(kline[3]),
                close=float(kline[4]),
                volume=float(kline[5])
            )
            session.merge(candle)
        session.commit()
        logger.info(f"Stored {len(klines)} klines for {symbol}")
    except Exception as e:
        logger.error(f"History fetch failed for {symbol}: {e}")
        session.rollback()

def prune_old_data(session):
    cutoff = datetime.now(CST_TZ) - timedelta(days=HISTORY_DAYS)
    session.query(Candle).filter(Candle.timestamp < cutoff).delete()
    session.commit()

def get_historical_metrics(session, symbol):
    cutoff = datetime.now(CST_TZ) - timedelta(days=HISTORY_DAYS)
    q = session.query(Candle).filter(
        Candle.symbol == symbol,
        Candle.timestamp >= cutoff
    ).order_by(Candle.timestamp).all()
    if len(q) < 24:  # Min 24 hours
        return None
    df = pd.DataFrame([{
        'timestamp': c.timestamp, 'close': c.close, 'low': c.low, 'volume': c.volume
    } for c in q])
    df.set_index('timestamp', inplace=True)
    fifteen_low = df['low'].min()
    avg_volume = df['volume'].mean()
    rsi = calculate_rsi(df['close'].values)
    roc_5d = (df['close'].iloc[-1] - df['close'].iloc[-120]) / df['close'].iloc[-120] * 100 if len(df) > 120 else 0
    max_dd = ((df['close'] - df['close'].expanding().max()) / df['close'].expanding().max() * 100).min()
    return {
        'fifteen_low': fifteen_low,
        'avg_volume': avg_volume,
        'rsi': rsi,
        'roc_5d': roc_5d,
        'max_dd': max_dd,
        'current_price': df['close'].iloc[-1]
    }

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

def check_buy_signal(client, session, symbol, positions):
    if symbol in positions:
        return False
    metrics = get_historical_metrics(session, symbol)
    if not metrics or metrics['rsi'] is None:
        return False
    current = fetch_current_data(client, symbol)
    if not current:
        return False
    if not (MIN_PRICE <= current['price'] <= MAX_PRICE) or current['volume_24h'] < VOLUME_THRESHOLD:
        return False
    if metrics['max_dd'] < -10 or metrics['roc_5d'] <= 0 or metrics['rsi'] <= 50:
        return False
    momentum = get_momentum_status(client, symbol)
    if momentum != 'bullish':
        return False
    if current['price'] > metrics['fifteen_low'] * 1.001:
        return False
    return True

def execute_buy(client, session, symbol):
    balance = get_balance(client)
    if balance <= MIN_BALANCE:
        send_whatsapp_alert("Low balance, skipping buy")
        return
    current = fetch_current_data(client, symbol)
    if not current:
        return
    current_price = current['price']
    maker_fee, taker_fee = get_trade_fees(client, symbol)
    alloc = min((balance - MIN_BALANCE) * RISK_PER_TRADE, balance - MIN_BALANCE)
    qty = alloc / current_price
    buy_price = current_price * 0.999  # Slippage buffer
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
            pos = Position(symbol=symbol, qty=float(order_status['executedQty']), entry_price=entry_price, entry_time=entry_time, buy_fee=buy_fee)
            session.add(pos)
            session.commit()
            send_whatsapp_alert(f"BUY filled {symbol} @ {entry_price} qty {order_status['executedQty']} fee {buy_fee*100:.2f}%")
            return
        time.sleep(30)
    cancel_order(client, symbol, order['orderId'])
    # Fallback to market if still signal
    if check_buy_signal(client, session, symbol, get_open_positions(session)):
        try:
            mkt_order = place_market_buy(client, symbol, qty, current_price)
            if mkt_order:
                entry_time = datetime.now(CST_TZ)
                entry_price = sum(float(f['price']) * float(f['qty']) for f in mkt_order['fills']) / sum(float(f['qty']) for f in mkt_order['fills'])
                buy_fee = taker_fee  # Market buy uses taker fee
                pos = Position(symbol=symbol, qty=sum(float(f['qty']) for f in mkt_order['fills']), entry_price=entry_price, entry_time=entry_time, buy_fee=buy_fee)
                session.add(pos)
                session.commit()
                send_whatsapp_alert(f"BUY market filled {symbol} @ {entry_price} qty {mkt_order['executedQty']} fee {buy_fee*100:.2f}%")
        except Exception as e:
            logger.error(f"Market buy fallback failed: {e}")

def check_sell_signal(client, symbol, position):
    current = fetch_current_data(client, symbol)
    if not current:
        return False, None
    current_price = current['price']
    maker_fee, taker_fee = get_trade_fees(client, symbol)
    # Use actual buy fee from position, estimate sell fee (worst-case taker for market, maker for limit)
    buy_fee = position['buy_fee']
    profit_pct = (current_price - position['entry_price']) / position['entry_price'] - (buy_fee + taker_fee)
    if profit_pct < PROFIT_TARGET:
        return False, None
    klines = client.get_klines(symbol=symbol, interval='1m', limit=2)
    prev_close = float(klines[0][4])
    delta = (current_price - prev_close) / prev_close
    if delta > 0.005:  # Fast rise
        return True, 'market', taker_fee
    return True, 'limit', maker_fee

def execute_sell(client, session, symbol, position):
    qty = position['qty']
    current = fetch_current_data(client, symbol)
    if not current:
        return
    current_price = current['price']
    should_sell, order_type, sell_fee = check_sell_signal(client, symbol, position)
    if not should_sell:
        return
    if order_type == 'market':
        order = place_market_sell(client, symbol, qty)
        if order:
            exit_time = datetime.now(CST_TZ)
            exit_price = sum(float(f['price']) * float(f['qty']) for f in order['fills']) / sum(float(f['qty']) for f in order['fills'])
            profit = (exit_price - position['entry_price']) * sum(float(f['qty']) for f in order['fills']) - (sell_fee * exit_price * sum(float(f['qty']) for f in order['fills']))
            trade = Trade(
                symbol=symbol, entry_time=position['entry_time'], entry_price=position['entry_price'],
                qty=sum(float(f['qty']) for f in order['fills']), exit_time=exit_time, exit_price=exit_price, profit=profit
            )
            session.add(trade)
            session.query(Position).filter(Position.symbol == symbol).delete()
            session.commit()
            send_whatsapp_alert(f"SOLD market {symbol} @ {exit_price} profit {profit:.2f} USDT fee {sell_fee*100:.2f}%")
    else:
        sell_price = position['entry_price'] * (1 + PROFIT_TARGET + position['buy_fee'] + sell_fee)
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
                    trade = Trade(
                        symbol=symbol, entry_time=position['entry_time'], entry_price=position['entry_price'],
                        qty=float(order_status['executedQty']), exit_time=exit_time, exit_price=exit_price, profit=profit
                    )
                    session.add(trade)
                    session.query(Position).filter(Position.symbol == symbol).delete()
                    session.commit()
                    send_whatsapp_alert(f"SOLD limit {symbol} @ {exit_price} profit {profit:.2f} USDT fee {sell_fee*100:.2f}%")
                    return
                time.sleep(30)
            cancel_order(client, symbol, order['orderId'])
            # Fallback to market if still signal
            should_sell_retry, _, _ = check_sell_signal(client, symbol, position)
            if should_sell_retry:
                execute_sell(client, session, symbol, position)  # Recursive fallback

def get_all_usdt_symbols(client):
    try:
        info = client.get_exchange_info()
        return [s['symbol'] for s in info['symbols'] if s['symbol'].endswith('USDT') and s['status'] == 'TRADING']
    except Exception as e:
        logger.error(f"Failed to get symbols: {e}")
        return []

def graceful_shutdown(session):
    try:
        session.commit()
        logger.info("Committed pending changes")
    except Exception as e:
        logger.error(f"Commit failed during shutdown: {e}")
        session.rollback()
    finally:
        session.close()
        logger.info("Database session closed")
        send_whatsapp_alert("Bot Shutdown: Gracefully stopped. All data saved.")

# === MAIN EXECUTION ===
if __name__ == "__main__":
    logger.info("Starting World's Most Profitable Crypto Trading Bot on Binance.US")
    if not API_KEY or not API_SECRET:
        logger.error("API keys not set.")
        exit(1)
    client = Client(API_KEY, API_SECRET, tld='us')
    session = Session()
    symbols = get_all_usdt_symbols(client)
    logger.info(f"Monitoring {len(symbols)} USDT pairs")

    try:
        while True:
            prune_old_data(session)
            balance = get_balance(client)
            if balance < MIN_BALANCE + 10:
                send_whatsapp_alert(f"Low USDT balance: {balance}")
            positions = get_open_positions(session)

            for symbol in symbols:
                store_historical_data(client, session, symbol)
                if check_buy_signal(client, session, symbol, positions):
                    execute_buy(client, session, symbol)
                if symbol in positions:
                    execute_sell(client, session, symbol, positions[symbol])

            time.sleep(LOOP_INTERVAL)
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt: Initiating graceful shutdown")
        graceful_shutdown(session)
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        graceful_shutdown(session)
    finally:
        logger.info("Shutdown complete")
