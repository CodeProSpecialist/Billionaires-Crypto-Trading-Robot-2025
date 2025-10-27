import os
import time
import logging
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

def place_limit_buy(client, symbol, price, qty):
    try:
        order = client.order_limit_buy(symbol=symbol, quantity=qty, price=price)
        logger.info(f"Placed limit buy for {symbol} at {price} qty {qty}: {order}")
        send_whatsapp_alert(f"Placed BUY limit {symbol} @ {price} qty {qty}")
        return order
    except BinanceOrderException as e:
        logger.error(f"Buy order failed for {symbol}: {e}")
        send_whatsapp_alert(f"BUY failed {symbol}: {e.message}")
        return None

def place_market_sell(client, symbol, qty):
    try:
        order = client.order_market_sell(symbol=symbol, quantity=qty)
        logger.info(f"Placed market sell for {symbol} qty {qty}: {order}")
        send_whatsapp_alert(f"Placed SELL market {symbol} qty {qty}")
        return order
    except BinanceOrderException as e:
        logger.error(f"Sell order failed for {symbol}: {e}")
        send_whatsapp_alert(f"SELL failed {symbol}: {e.message}")
        return None

def place_limit_sell(client, symbol, price, qty):
    try:
        order = client.order_limit_sell(symbol=symbol, quantity=qty, price=price)
        logger.info(f"Placed limit sell for {symbol} at {price} qty {qty}: {order}")
        send_whatsapp_alert(f"Placed SELL limit {symbol} @ {price} qty {qty}")
        return order
    except BinanceOrderException as e:
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
    current_price = fetch_current_data(client, symbol)['price']
    maker_fee, taker_fee = get_trade_fees(client, symbol)
    alloc = min((balance - MIN_BALANCE) * RISK_PER_TRADE, balance - MIN_BALANCE)
    qty = alloc / current_price
    buy_price = current_price * 0.999  # Slippage buffer
    order = place_limit_buy(client, symbol, buy_price, qty)
    if not order:
        return
    start = time.time()
    while time.time() - start < ORDER_TIMEOUT:
        order_status = client.get_order(symbol=symbol, orderId=order['orderId'])
        if order_status['status'] == 'FILLED':
            entry_time = datetime.now(CST_TZ)
            entry_price = float(order_status['cummulativeQuoteQty']) / float(order_status['executedQty'])
            buy_fee = maker_fee  # Limit buy uses maker fee
            pos = Position(symbol=symbol, qty=qty, entry_price=entry_price, entry_time=entry_time, buy_fee=buy_fee)
            session.add(pos)
            session.commit()
            send_whatsapp_alert(f"BUY filled {symbol} @ {entry_price} qty {qty} fee {buy_fee*100:.2f}%")
            return
        time.sleep(30)
    cancel_order(client, symbol, order['orderId'])
    # Fallback to market if still signal
    if check_buy_signal(client, session, symbol, get_open_positions(session)):
        try:
            mkt_order = client.order_market_buy(symbol=symbol, quantity=qty)
            entry_time = datetime.now(CST_TZ)
            entry_price = sum(float(f['price']) * float(f['qty']) for f in mkt_order['fills']) / sum(float(f['qty']) for f in mkt_order['fills'])
            buy_fee = taker_fee  # Market buy uses taker fee
            pos = Position(symbol=symbol, qty=qty, entry_price=entry_price, entry_time=entry_time, buy_fee=buy_fee)
            session.add(pos)
            session.commit()
            send_whatsapp_alert(f"BUY market filled {symbol} @ {entry_price} qty {qty} fee {buy_fee*100:.2f}%")
        except Exception as e:
            logger.error(f"Market buy fallback failed: {e}")

def check_sell_signal(client, symbol, position):
    current_price = fetch_current_data(client, symbol)['price']
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
    current_price = fetch_current_data(client, symbol)['price']
    should_sell, order_type, sell_fee = check_sell_signal(client, symbol, position)
    if not should_sell:
        return
    if order_type == 'market':
        order = place_market_sell(client, symbol, qty)
    else:
        sell_price = position['entry_price'] * (1 + PROFIT_TARGET + position['buy_fee'] + sell_fee)
        order = place_limit_sell(client, symbol, sell_price, qty)
    if order:
        exit_time = datetime.now(CST_TZ)
        exit_price = float(order['cummulativeQuoteQty']) / float(order['executedQty']) if 'cummulativeQuoteQty' in order else current_price
        profit = (exit_price - position['entry_price']) * qty
        trade = Trade(
            symbol=symbol, entry_time=position['entry_time'], entry_price=position['entry_price'],
            qty=qty, exit_time=exit_time, exit_price=exit_price, profit=profit
        )
        session.add(trade)
        session.query(Position).filter(Position.symbol == symbol).delete()
        session.commit()
        send_whatsapp_alert(f"SOLD {symbol} @ {exit_price} profit {profit:.2f} USDT fee {sell_fee*100:.2f}%")

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
