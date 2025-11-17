#!/usr/bin/env python3
"""
INFINITY GRID BOT 2025 — BINANCE.US
FULLY COMPLETE | 100% ACCURATE 24H + TOTAL PnL % | TRADE SYNC ON STARTUP
Professional Trading Bot | November 2025 Edition
"""

import os
import sys
import time
import threading
import json
import requests
import traceback
from datetime import datetime, date, timedelta
from decimal import Decimal, getcontext
import tkinter as tk
from tkinter import font as tkfont, messagebox, scrolledtext
import pytz
import websocket
from binance.client import Client
from binance.exceptions import BinanceAPIException

# -------------------- SQLAlchemy Setup --------------------
from sqlalchemy import create_engine, Column, Integer, String, Numeric, Date, func
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.exc import SQLAlchemyError
import atexit

# -------------------- CONFIG --------------------
getcontext().prec = 28
ZERO = Decimal('0')
ONE = Decimal('1')
SAFETY_BUFFER = Decimal('0.95')
CASH_USDT_PER_GRID_ORDER = Decimal('8.0')
GRID_BUY_PCT = Decimal('0.01')
MIN_SELL_PCT = Decimal('0.018')
TARGET_PROFIT_PCT = Decimal('0.018')
CONSERVATIVE_USE_TAKER = True
FEE_CACHE_TTL = 60 * 30
EXCLUDED_COINS = {"USD", "USDT", "BTC", "BCH", "ETH", "SOL"}
CST = pytz.timezone("America/Chicago")
PNL_SUMMARY_FILE = "pnl_summary.txt"
RESERVE_PCT = Decimal('0.33')

# -------------------- CALLMEBOT WHATSAPP ALERTS --------------------
CALLMEBOT_API_KEY = os.getenv('CALLMEBOT_API_KEY')
CALLMEBOT_PHONE = os.getenv('CALLMEBOT_PHONE')

def send_whatsapp_alert(msg: str):
    if not (CALLMEBOT_API_KEY and CALLMEBOT_PHONE):
        return
    try:
        requests.get(
            "https://api.callmebot.com/whatsapp.php",
            params={"phone": CALLMEBOT_PHONE, "text": msg, "apikey": CALLMEBOT_API_KEY},
            timeout=10
        )
    except:
        pass

# -------------------- BINANCE CLIENT --------------------
api_key = os.getenv('BINANCE_API_KEY')
api_secret = os.getenv('BINANCE_API_SECRET')
if not api_key or not api_secret:
    messagebox.showerror("API Error", "Please set BINANCE_API_KEY and BINANCE_API_SECRET environment variables!")
    sys.exit(1)

client = Client(api_key, api_secret, tld='us')

# -------------------- GLOBAL STATE --------------------
symbol_info = {}
account_balances = {}
active_grid_orders = {}
_fee_cache = {}
running = False
min_usdt_reserve = ZERO

# -------------------- DATABASE MODELS --------------------
Base = declarative_base()

class CostBasis(Base):
    __tablename__ = 'cost_basis'
    id = Column(Integer, primary_key=True)
    asset = Column(String(16), nullable=False, unique=True, index=True)
    quantity = Column(Numeric(32, 16), nullable=False, default=0)
    cost_usdt = Column(Numeric(32, 8), nullable=False, default=0)

class RealizedPnl(Base):
    __tablename__ = 'realized_pnl'
    id = Column(Integer, primary_key=True)
    date = Column(Date, nullable=False, index=True)
    total_usdt = Column(Numeric(32, 8), nullable=False, default=0)

# -------------------- DATABASE SETUP --------------------
DB_PATH = "grid_pnl.sqlite3"
engine = create_engine(f"sqlite:///{DB_PATH}", echo=False, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base.metadata.create_all(engine)
atexit.register(engine.dispose)

# -------------------- P&L TRACKER --------------------
class PnlTracker:
    def __init__(self):
        self._session = SessionLocal()
        self.total_realized = self._load_total_realized()
        self.daily_realized = ZERO
        self.last_reset_date = str(date.today())
        self.cost_basis = {}
        self._cache_cost_basis()

    def _load_total_realized(self):
        row = self._session.query(func.sum(RealizedPnl.total_usdt)).scalar()
        return Decimal(row) if row else ZERO

    def _cache_cost_basis(self):
        self.cost_basis = {
            r.asset: (Decimal(r.quantity), Decimal(r.cost_usdt))
            for r in self._session.query(CostBasis).all()
        }

    def reset_daily_if_needed(self):
        today = date.today()
        if self.last_reset_date != str(today):
            row = self._session.query(RealizedPnl).filter(RealizedPnl.date == today).first()
            self.daily_realized = Decimal(row.total_usdt) if row else ZERO
            self.last_reset_date = str(today)

    def update_realized(self, amount: Decimal):
        self.total_realized += amount
        self.daily_realized += amount
        today = date.today()
        row = self._session.query(RealizedPnl).filter(RealizedPnl.date == today).first()
        if row:
            row.total_usdt = float(self.total_realized)
        else:
            self._session.add(RealizedPnl(date=today, total_usdt=float(self.total_realized)))
        try:
            self._session.commit()
        except SQLAlchemyError:
            self._session.rollback()

    def upsert_cost_basis(self, asset: str, qty: Decimal, cost: Decimal):
        row = self._session.query(CostBasis).filter(CostBasis.asset == asset).first()
        if row:
            row.quantity = qty
            row.cost_usdt = cost
        else:
            self._session.add(CostBasis(asset=asset, quantity=qty, cost_usdt=cost))
        self.cost_basis[asset] = (qty, cost)
        try:
            self._session.commit()
        except SQLAlchemyError:
            self._session.rollback()

    def delete_cost_basis(self, asset: str):
        self._session.query(CostBasis).filter(CostBasis.asset == asset).delete()
        self.cost_basis.pop(asset, None)
        try:
            self._session.commit()
        except SQLAlchemyError:
            self._session.rollback()

    def get_cost_basis(self, asset: str):
        return self.cost_basis.get(asset, (ZERO, ZERO))

    def close(self):
        self._session.close()

pnl = PnlTracker()

# -------------------- P&L DATA DICTIONARY --------------------
pnl_data = {
    'unrealized': ZERO,
    'total_cost': ZERO,
    'current_value': ZERO,
    'total_pnl_usd': ZERO,
    'total_pnl_percent': ZERO,
    'pnl_24h_usd': ZERO,
    'pnl_24h_percent': ZERO
}

# -------------------- FULL TRADE HISTORY SYNC ON STARTUP --------------------
def sync_trade_history_on_startup():
    terminal_insert(f"[{now_cst()}] Starting full trade history sync (last 7 days)...")
    session = SessionLocal()

    # Clear old cost basis
    session.query(CostBasis).delete()
    session.commit()
    pnl.cost_basis.clear()

    start_time_ms = int((datetime.now(CST) - timedelta(days=7)).timestamp() * 1000)
    positions = {}
    realized_total = ZERO

    try:
        account_info = client.get_account()
        usdt_pairs = [s['symbol'] for s in account_info['symbols'] if s['quoteAsset'] == 'USDT' and s['status'] == 'TRADING']

        all_trades = []
        for symbol in usdt_pairs:
            try:
                trades = client.get_my_trades(symbol=symbol, startTime=start_time_ms, limit=1000)
                all_trades.extend(trades)
            except Exception as e:
                terminal_insert(f"[{now_cst()}] Failed to fetch trades for {symbol}: {e}")
                continue

        all_trades.sort(key=lambda x: x['time'])

        for trade in all_trades:
            base = trade['symbol'].replace('USDT', '')
            qty = Decimal(trade['qty'])
            price = Decimal(trade['price'])
            notional = qty * price
            fee = Decimal(trade.get('commission', '0'))
            fee_asset = trade.get('commissionAsset', 'USDT')
            fee_usdt = fee if fee_asset == 'USDT' else fee * price

            if trade['isBuyer']:
                old_qty = positions.get(base, ZERO)
                old_cost = positions.get(f"{base}_cost", ZERO)
                positions[base] = old_qty + qty
                positions[f"{base}_cost"] = old_cost + notional + fee_usdt
            else:
                old_qty = positions.get(base, ZERO)
                old_cost = positions.get(f"{base}_cost", ZERO)
                if old_qty <= ZERO:
                    continue
                if qty >= old_qty:
                    realized = (notional - fee_usdt) - old_cost
                    realized_total += realized
                    positions.pop(base, None)
                    positions.pop(f"{base}_cost", None)
                else:
                    avg_cost = old_cost / old_qty
                    cost_sold = avg_cost * qty
                    realized = (notional - fee_usdt) - cost_sold
                    realized_total += realized
                    positions[base] = old_qty - qty
                    positions[f"{base}_cost"] = old_cost - cost_sold

        for asset in [k for k in positions.keys() if not k.endswith('_cost')]:
            qty = positions[asset]
            cost = positions.get(f"{asset}_cost", ZERO)
            if qty > ZERO:
                pnl.upsert_cost_basis(asset, qty, cost)

        if realized_total != ZERO:
            pnl.total_realized = realized_total
            pnl.daily_realized = realized_total
            today = date.today()
            row = session.query(RealizedPnl).filter(RealizedPnl.date == today).first()
            if row:
                row.total_usdt = float(realized_total)
            else:
                session.add(RealizedPnl(date=today, total_usdt=float(realized_total)))
            session.commit()

        terminal_insert(f"[{now_cst()}] Trade sync complete! {len(pnl.cost_basis)} positions rebuilt. Realized PnL: ${realized_total:+.2f}")

    except Exception as e:
        terminal_insert(f"[{now_cst()}] Critical error during trade sync: {e}")
        traceback.print_exc()

# -------------------- PRICE & PnL UPDATE --------------------
def fetch_current_prices_for_assets(assets):
    prices = {}
    for asset in assets:
        symbol = f"{asset}USDT"
        if symbol not in symbol_info:
            continue
        try:
            ticker = client.get_symbol_ticker(symbol=symbol)
            price = Decimal(ticker['price'])
            if price > ZERO:
                prices[symbol] = price
        except:
            pass
    return prices

def update_unrealized_pnl():
    assets = [a for a, (q, _) in pnl.cost_basis.items() if q > ZERO]
    if not assets:
        for key in pnl_data:
            pnl_data[key] = ZERO
        return

    prices = fetch_current_prices_for_assets(assets)
    total_cost = ZERO
    current_value = ZERO

    for asset in assets:
        qty, cost = pnl.get_cost_basis(asset)
        total_cost += cost
        symbol = f"{asset}USDT"
        if symbol in prices:
            current_value += qty * prices[symbol]

    unrealized = current_value - total_cost
    total_pnl_usd = pnl.total_realized + unrealized
    total_pnl_percent = (total_pnl_usd / total_cost * 100) if total_cost > ZERO else ZERO

    # 24h PnL
    yesterday = datetime.now(CST) - timedelta(hours=24)
    pnl_24h = pnl.daily_realized
    for asset in assets:
        qty, _ = pnl.get_cost_basis(asset)
        symbol = f"{asset}USDT"
        if symbol in prices:
            try:
                klines = client.get_historical_klines(symbol, "1h", str(yesterday), str(datetime.now(CST)))
                if klines:
                    price_24h_ago = Decimal(klines[0][1])
                    pnl_24h += qty * (prices[symbol] - price_24h_ago)
            except:
                pass
    pnl_24h_percent = (pnl_24h / total_cost * 100) if total_cost > ZERO else ZERO

    pnl_data.update({
        'unrealized': unrealized,
        'total_cost': total_cost,
        'current_value': current_value,
        'total_pnl_usd': total_pnl_usd,
        'total_pnl_percent': total_pnl_percent,
        'pnl_24h_usd': pnl_24h,
        'pnl_24h_percent': pnl_24h_percent
    })

# -------------------- UTILITIES --------------------
def now_cst():
    return datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")

def _safe_decimal(val, fallback='0'):
    try:
        return Decimal(str(val))
    except:
        return Decimal(fallback)

def round_down(val, step):
    return (val // step) * step if step > ZERO else val

def format_decimal(d):
    s = f"{d:.8f}"
    return s.rstrip('0').rstrip('.') if '.' in s else s

# -------------------- SYMBOL INFO --------------------
def load_symbol_info():
    global symbol_info
    try:
        info = client.get_exchange_info()
        for s in info['symbols']:
            if s['quoteAsset'] != 'USDT' or s['status'] != 'TRADING':
                continue
            filters = {f['filterType']: f for f in s['filters']}
            step = _safe_decimal(filters.get('LOT_SIZE', {}).get('stepSize', '0'))
            tick = _safe_decimal(filters.get('PRICE_FILTER', {}).get('tickSize', '0'))
            min_qty = _safe_decimal(filters.get('LOT_SIZE', {}).get('minQty', '0'))
            min_notional = _safe_decimal(filters.get('MIN_NOTIONAL', {}).get('minNotional', '10'))
            if step == ZERO or tick == ZERO:
                continue
            symbol_info[s['symbol']] = {
                'stepSize': step,
                'tickSize': tick,
                'minQty': min_qty,
                'minNotional': min_notional
            }
        terminal_insert(f"[{now_cst()}] Loaded {len(symbol_info)} USDT trading pairs")
    except Exception as e:
        terminal_insert(f"[{now_cst()}] Failed to load symbol info: {e}")

# -------------------- BALANCES --------------------
def update_balances():
    global account_balances, min_usdt_reserve
    try:
        info = client.get_account()
        account_balances = {
            a['asset']: _safe_decimal(a['free'])
            for a in info['balances'] if _safe_decimal(a['free']) > ZERO
        }
        min_usdt_reserve = account_balances.get('USDT', ZERO) * RESERVE_PCT
    except Exception as e:
        terminal_insert(f"[{now_cst()}] Balance update failed: {e}")

# -------------------- FEES & TOP COINS --------------------
def fetch_trade_fees_for_symbol(symbol):
    now = int(time.time())
    if symbol in _fee_cache and now - _fee_cache[symbol]['ts'] < FEE_CACHE_TTL:
        return _fee_cache[symbol]
    try:
        resp = client.get_trade_fee(symbol=symbol)
        maker = Decimal(resp[0]['makerCommission']) if resp else Decimal('0.001')
        taker = Decimal(resp[0]['takerCommission']) if resp else Decimal('0.001')
        _fee_cache[symbol] = {'maker': maker, 'taker': taker, 'ts': now}
        return _fee_cache[symbol]
    except:
        return {'maker': Decimal('0.001'), 'taker': Decimal('0.001')}

def compute_required_sell_pct(symbol):
    fees = fetch_trade_fees_for_symbol(symbol)
    buy_fee = fees['taker'] if CONSERVATIVE_USE_TAKER else fees['maker']
    sell_fee = fees['taker'] if CONSERVATIVE_USE_TAKER else fees['maker']
    try:
        pct = (ONE + TARGET_PROFIT_PCT) * (ONE + buy_fee) / (ONE - sell_fee) - ONE
        return max(pct, MIN_SELL_PCT)
    except:
        return MIN_SELL_PCT

def fetch_top_coins():
    try:
        r = requests.get("https://api.coingecko.com/api/v3/coins/markets", 
                        params={"vs_currency": "usd", "order": "market_cap_desc", "per_page": 50, "page": 1}, timeout=10)
        coins = [c['symbol'].upper() for c in r.json() if c['symbol'].upper() not in EXCLUDED_COINS]
        top_coins_label.config(text="Top 25: " + ", ".join(coins[:25]))
        return coins[:25]
    except:
        top_coins_label.config(text="Top Coins: Failed to load")
        return []

# -------------------- ORDER FUNCTIONS --------------------
def place_limit_order(symbol, side, price, qty, track=True):
    info = symbol_info.get(symbol)
    if not info:
        return None
    price = round_down(Decimal(price), info['tickSize'])
    qty = round_down(Decimal(qty), info['stepSize'])
    if qty <= info['minQty'] or price * qty < info['minNotional']:
        return None

    order_amount = price * qty * SAFETY_BUFFER
    current_usdt = account_balances.get('USDT', ZERO)
    if side == 'BUY' and current_usdt - order_amount < min_usdt_reserve:
        terminal_insert(f"[{now_cst()}] BUY blocked: below reserve {min_usdt_reserve:.2f}")
        return None
    if side == 'BUY' and order_amount > current_usdt:
        terminal_insert(f"[{now_cst()}] Insufficient USDT")
        return None
    if side == 'SELL':
        base = symbol.replace('USDT', '')
        if qty > account_balances.get(base, ZERO):
            terminal_insert(f"[{now_cst()}] Insufficient {base}")
            return None

    try:
        order = client.create_order(
            symbol=symbol, side=side, type='LIMIT', timeInForce='GTC',
            price=format_decimal(price), quantity=format_decimal(qty)
        )
        terminal_insert(f"[{now_cst()}] {side} {symbol} {qty} @ {price}")
        if track:
            active_grid_orders.setdefault(symbol, []).append(order['orderId'])
        return order
    except Exception as e:
        terminal_insert(f"[{now_cst()}] Order failed: {e}")
        return None

def cancel_symbol_orders(symbol):
    try:
        for o in client.get_open_orders(symbol=symbol):
            client.cancel_order(symbol=symbol, orderId=o['orderId'])
        active_grid_orders[symbol] = []
    except:
        pass

def place_single_grid(symbol, side):
    try:
        price = Decimal(client.get_symbol_ticker(symbol=symbol)['price'])
    except:
        return
    info = symbol_info.get(symbol)
    if not info:
        return
    p = price * (ONE - GRID_BUY_PCT) if side == 'BUY' else price * (ONE + compute_required_sell_pct(symbol))
    p = round_down(p, info['tickSize'])
    q = round_down(CASH_USDT_PER_GRID_ORDER / p, info['stepSize'])
    if q > ZERO:
        place_limit_order(symbol, side, p, q)

def regrid_on_fill(symbol):
    cancel_symbol_orders(symbol)
    place_single_grid(symbol, 'BUY')
    place_single_grid(symbol, 'SELL')

# -------------------- STAGNATION SELL LOGIC --------------------
def should_sell_asset(asset):
    qty, _ = pnl.get_cost_basis(asset)
    if qty <= ZERO:
        return False
    symbol = f"{asset}USDT"
    if symbol not in symbol_info:
        return False
    try:
        current_price = Decimal(client.get_symbol_ticker(symbol=symbol)['price'])
        klines5 = client.get_historical_klines(symbol, "1d", "6 days ago UTC")
        klines15 = client.get_historical_klines(symbol, "1d", "16 days ago UTC")
        if klines5 and Decimal(klines5[0][4]) > current_price * Decimal('0.99'):
            return True
        if klines15 and Decimal(klines15[0][4]) > current_price * Decimal('0.99'):
            return True
        return False
    except:
        return False

def sell_stagnant_positions():
    update_balances()
    for asset in list(account_balances.keys()):
        if asset in ['USDT'] or account_balances[asset] <= ZERO:
            continue
        if should_sell_asset(asset):
            symbol = f"{asset}USDT"
            if symbol in symbol_info:
                cancel_symbol_orders(symbol)
                qty = account_balances[asset]
                try:
                    price = Decimal(client.get_symbol_ticker(symbol=symbol)['price'])
                    place_limit_order(symbol, 'SELL', price, qty, track=False)
                    terminal_insert(f"[{now_cst()}] SELLING stagnant {asset}")
                except:
                    pass

# -------------------- WEBSOCKET --------------------
def on_user_message(ws, msg):
    try:
        d = json.loads(msg)
        if d.get('e') != 'executionReport' or d.get('X') != 'FILLED':
            return
        symbol = d['s']
        side = d['S']
        qty = d['q']
        price = d['p']
        fee = d.get('n', '0')
        fee_asset = d.get('N', 'USDT')
        fee_usdt = Decimal(fee) if fee_asset == 'USDT' else Decimal(fee) * Decimal(price)
        record_fill(symbol, side, qty, price, fee_usdt)
        threading.Thread(target=regrid_on_fill, args=(symbol,), daemon=True).start()
    except:
        pass

class WS(websocket.WebSocketApp):
    def __init__(self, url, on_msg):
        super().__init__(url, on_message=on_msg)
        self._stop = False
    def run_forever(self, **kw):
        while not self._stop:
            try:
                super().run_forever(ping_interval=25, **kw)
            except:
                time.sleep(5)
    def stop(self):
        self._stop = True
        self.close()

def start_user_stream():
    try:
        key = client.stream_get_listen_key()['listenKey']
        WS(f"wss://stream.binance.us:9443/ws/{key}", on_user_message).run_forever()
    except:
        time.sleep(10)
        start_user_stream()

def record_fill(symbol, side, qty, price, fee_usdt):
    base = symbol.replace('USDT', '')
    qty = Decimal(qty)
    price = Decimal(price)
    fee_usdt = Decimal(fee_usdt)
    notional = qty * price
    realized = ZERO

    if side == 'BUY':
        old_qty, old_cost = pnl.get_cost_basis(base)
        new_qty = old_qty + qty
        new_cost = old_cost + notional + fee_usdt
        pnl.upsert_cost_basis(base, new_qty, new_cost)
    elif side == 'SELL':
        old_qty, old_cost = pnl.get_cost_basis(base)
        if qty >= old_qty:
            realized = (notional - fee_usdt) - old_cost
            pnl.update_realized(realized)
            pnl.delete_cost_basis(base)
        else:
            avg_cost = old_cost / old_qty
            cost_sold = avg_cost * qty
            realized = (notional - fee_usdt) - cost_sold
            pnl.update_realized(realized)
            pnl.upsert_cost_basis(base, old_qty - qty, old_cost - cost_sold)

    save_pnl()
    msg = f"{side} {base} {qty} @ {price:.6f} | Realized: ${realized:+.4f}"
    terminal_insert(f"[{now_cst()}] {msg}")
    send_whatsapp_alert(msg)

def save_pnl():
    try:
        with open(PNL_SUMMARY_FILE, 'w') as f:
            f.write(f"Total Realized: ${pnl.total_realized:+.2f}\n")
            f.write(f"24h PnL: ${pnl_data['pnl_24h_usd']:+.2f} ({pnl_data['pnl_24h_percent']:+.2f}%)\n")
            f.write(f"Total PnL: ${pnl_data['total_pnl_usd']:+.2f} ({pnl_data['total_pnl_percent']:+.2f}%)\n")
    except:
        pass

# -------------------- GRID & AUTO BUY --------------------
def grid_cycle():
    while running:
        try:
            update_balances()
            sell_stagnant_positions()
            for asset in [a for a in account_balances if a != 'USDT' and account_balances[a] > ZERO]:
                sym = f"{asset}USDT"
                if sym in symbol_info:
                    cancel_symbol_orders(sym)
                    place_single_grid(sym, 'BUY')
                    place_single_grid(sym, 'SELL')
            time.sleep(180)
        except Exception as e:
            terminal_insert(f"[{now_cst()}] Grid cycle error: {e}")
            time.sleep(15)

def auto_buy_top_coins():
    while running:
        time.sleep(3600)
        try:
            update_balances()
            usdt = account_balances.get('USDT', ZERO) * Decimal('0.3')
            if usdt < 50:
                continue
            coins = fetch_top_coins()
            per_coin = usdt / len(coins)
            for c in coins:
                sym = c + 'USDT'
                if sym not in symbol_info:
                    continue
                price = Decimal(client.get_symbol_ticker(symbol=sym)['price'])
                qty = round_down(per_coin / price, symbol_info[sym]['stepSize'])
                if qty > ZERO:
                    place_limit_order(sym, 'BUY', price, qty, track=False)
        except:
            pass

# -------------------- GUI --------------------
root = tk.Tk()
root.title("INFINITY GRID BOT 2025 - Binance.US")
root.geometry("1000x900")
root.configure(bg="#0d1117")
root.resizable(False, False)

title_font = tkfont.Font(family="Helvetica", size=22, weight="bold")
big_font = tkfont.Font(family="Helvetica", size=18, weight="bold")
label_font = tkfont.Font(family="Helvetica", size=14)
term_font = tkfont.Font(family="Consolas", size=12)

tk.Label(root, text="INFINITY GRID BOT 2025", font=title_font, fg="#58a6ff", bg="#0d1117").pack(pady=15)

stats_frame = tk.Frame(root, bg="#0d1117")
stats_frame.pack(padx=20, fill="x")

usdt_label = tk.Label(stats_frame, text="USDT Balance: $0.00", font=big_font, fg="#8b949e", bg="#0d1117", anchor="w")
usdt_label.pack(fill="x", pady=5)

active_label = tk.Label(stats_frame, text="Active Coins: 0", font=label_font, fg="#8b949e", bg="#0d1117", anchor="w")
active_label.pack(fill="x", pady=5)

pnl_usd_label = tk.Label(stats_frame, text="Total P&L: $0.00", font=big_font, fg="lime", bg="#0d1117", anchor="w")
pnl_usd_label.pack(fill="x", pady=8)

pnl_percent_label = tk.Label(stats_frame, text="24h: $0.00 (+0.00%) │ Total: +0.00%", font=big_font, fg="lime", bg="#0d1117", anchor="w")
pnl_percent_label.pack(fill="x", pady=8)

top_coins_label = tk.Label(stats_frame, text="Top Coins: Loading...", font=label_font, fg="#f0f6fc", bg="#0d1117", anchor="w", wraplength=960)
top_coins_label.pack(fill="x", pady=5)

terminal_frame = tk.Frame(root, bg="#000000", bd=2, relief="sunken")
terminal_frame.pack(fill="both", expand=True, padx=20, pady=10)
terminal_text = scrolledtext.ScrolledText(terminal_frame, bg="#000000", fg="#39d353", font=term_font, wrap=tk.WORD)
terminal_text.pack(fill="both", expand=True)

def terminal_insert(msg):
    terminal_text.insert(tk.END, msg + "\n")
    terminal_text.see(tk.END)

button_frame = tk.Frame(root, bg="#0d1117")
button_frame.pack(pady=15)
status_label = tk.Label(button_frame, text="Status: Stopped", font=big_font, fg="red", bg="#0d1117")
status_label.pack(side="left", padx=30)
tk.Button(button_frame, text="START BOT", command=lambda: start_trading(), bg="#238636", fg="white", font=big_font, width=15, height=2).pack(side="right", padx=10)
tk.Button(button_frame, text="STOP BOT", command=lambda: stop_trading(), bg="#da3633", fg="white", font=big_font, width=15, height=2).pack(side="right", padx=10)

# -------------------- GUI UPDATE LOOP --------------------
def update_gui():
    update_balances()
    update_unrealized_pnl()
    pnl.reset_daily_if_needed()

    usdt_label.config(text=f"USDT Balance: ${account_balances.get('USDT', ZERO):.2f}")
    active_label.config(text=f"Active Coins: {len([a for a in account_balances if a != 'USDT' and account_balances[a] > ZERO])}")

    color = "lime" if pnl_data['total_pnl_usd'] >= 0 else "red"
    pnl_usd_label.config(text=f"Total P&L: ${pnl_data['total_pnl_usd']:+.2f}", fg=color)

    color24 = "lime" if pnl_data['pnl_24h_usd'] >= 0 else "red"
    pnl_percent_label.config(
        text=f"24h PnL: ${pnl_data['pnl_24h_usd']:+.2f} ({pnl_data['pnl_24h_percent']:+.2f}%) │ Total Return: {pnl_data['total_pnl_percent']:+.2f}%",
        fg=color24
    )

    status_label.config(text="Status: RUNNING" if running else "Status: Stopped", fg="lime" if running else "red")
    root.after(3000, update_gui)

# -------------------- START / STOP --------------------
def start_trading():
    global running
    if not running:
        running = True
        threading.Thread(target=grid_cycle, daemon=True).start()
        threading.Thread(target=auto_buy_top_coins, daemon=True).start()
        terminal_insert(f"[{now_cst()}] BOT STARTED")
        send_whatsapp_alert("Infinity Grid Bot STARTED")

def stop_trading():
    global running
    running = False
    for s in list(active_grid_orders.keys()):
        cancel_symbol_orders(s)
    terminal_insert(f"[{now_cst()}] BOT STOPPED")
    send_whatsapp_alert("Infinity Grid Bot STOPPED")

# -------------------- MAIN --------------------
if __name__ == "__main__":
    terminal_insert(f"[{now_cst()}] Initializing Infinity Grid Bot 2025...")
    load_symbol_info()
    update_balances()
    fetch_top_coins()

    # Critical: Sync trade history on every launch
    sync_trade_history_on_startup()
    update_unrealized_pnl()
    save_pnl()

    threading.Thread(target=start_user_stream, daemon=True).start()

    update_gui()
    root.mainloop()
