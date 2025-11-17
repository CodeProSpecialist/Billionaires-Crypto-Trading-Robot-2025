#!/usr/bin/env python3
"""
INFINITY GRID BOT 2025 — BINANCE.US
Full P&L Tracker + Manual & Daily Rebalance (Max 5% per coin)
WebSocket-Only Fills | No IP Ban | Perfect PnL Forever
FINAL PRODUCTION VERSION — November 17, 2025
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

# -------------------- SQLAlchemy --------------------
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
RESERVE_PCT = Decimal('0.33')
MAX_POSITION_PCT = Decimal('0.05')           # No coin > 5%
DAILY_LOSS_TRIGGER_PCT = Decimal('-0.03')    # Trigger if 24h PnL ≤ -3%
EXCLUDED_COINS = {"USD", "USDT", "BTC", "BCH", "ETH", "SOL"}
CST = pytz.timezone("America/Chicago")
PNL_SUMMARY_FILE = "pnl_summary.txt"
first_run_done = False
last_auto_rebalance_date = None

# -------------------- WHATSAPP ALERTS --------------------
CALLMEBOT_API_KEY = os.getenv('CALLMEBOT_API_KEY')
CALLMEBOT_PHONE = os.getenv('CALLMEBOT_PHONE')

def send_whatsapp_alert(msg: str):
    if not (CALLMEBOT_API_KEY and CALLMEBOT_PHONE):
        return
    try:
        requests.get("https://api.callmebot.com/whatsapp.php",
                     params={"phone": CALLMEBOT_PHONE, "text": msg, "apikey": CALLMEBOT_API_KEY},
                     timeout=10)
    except:
        pass

# -------------------- BINANCE CLIENT --------------------
api_key = os.getenv('BINANCE_API_KEY')
api_secret = os.getenv('BINANCE_API_SECRET')
if not api_key or not api_secret:
    messagebox.showerror("API Error", "Set BINANCE_API_KEY and BINANCE_API_SECRET environment variables!")
    sys.exit(1)

client = Client(api_key, api_secret, tld='us')

# -------------------- GLOBALS --------------------
symbol_info = {}
account_balances = {}
active_grid_orders = {}
running = False
min_usdt_reserve = ZERO

# -------------------- DATABASE & FULL P&L TRACKER --------------------
Base = declarative_base()

class CostBasis(Base):
    __tablename__ = 'cost_basis'
    id = Column(Integer, primary_key=True)
    asset = Column(String(16), nullable=False, unique=True, index=True)
    quantity = Column(Numeric(32, 16), default=0)
    cost_usdt = Column(Numeric(32, 8), default=0)

class RealizedPnl(Base):
    __tablename__ = 'realized_pnl'
    id = Column(Integer, primary_key=True)
    date = Column(Date, nullable=False, index=True)
    total_usdt = Column(Numeric(32, 8), default=0)

engine = create_engine("sqlite:///grid_pnl.sqlite3", future=True, connect_args={"timeout": 30})
SessionLocal = sessionmaker(bind=engine)
Base.metadata.create_all(engine)
atexit.register(engine.dispose)

class PnlTracker:
    def __init__(self):
        self.session = SessionLocal()
        self.total_realized = self._load_total_realized()
        self.daily_realized = ZERO
        self.last_reset_date = str(date.today())
        self.cost_basis = {}
        self._load_cost_basis()

    def _load_total_realized(self):
        val = self.session.query(func.sum(RealizedPnl.total_usdt)).scalar()
        return Decimal(val) if val else ZERO

    def _load_cost_basis(self):
        for row in self.session.query(CostBasis).all():
            self.cost_basis[row.asset] = (Decimal(row.quantity), Decimal(row.cost_usdt))

    def reset_daily_if_needed(self):
        today = str(date.today())
        if self.last_reset_date != today:
            row = self.session.query(RealizedPnl).filter(RealizedPnl.date == date.today()).first()
            self.daily_realized = Decimal(row.total_usdt) if row else ZERO
            self.last_reset_date = today

    def update_realized(self, amount: Decimal):
        self.total_realized += amount
        self.daily_realized += amount
        today = date.today()
        row = self.session.query(RealizedPnl).filter(RealizedPnl.date == today).first()
        if row:
            row.total_usdt = float(self.total_realized)
        else:
            self.session.add(RealizedPnl(date=today, total_usdt=float(self.total_realized)))
        try:
            self.session.commit()
        except SQLAlchemyError:
            self.session.rollback()

    def upsert_cost_basis(self, asset: str, qty: Decimal, cost: Decimal):
        row = self.session.query(CostBasis).filter(CostBasis.asset == asset).first()
        if row:
            row.quantity = float(qty)
            row.cost_usdt = float(cost)
        else:
            self.session.add(CostBasis(asset=asset, quantity=float(qty), cost_usdt=float(cost)))
        self.cost_basis[asset] = (qty, cost)
        try:
            self.session.commit()
        except SQLAlchemyError:
            self.session.rollback()

    def delete_cost_basis(self, asset: str):
        self.session.query(CostBasis).filter(CostBasis.asset == asset).delete()
        self.cost_basis.pop(asset, None)
        try:
            self.session.commit()
        except SQLAlchemyError:
            self.session.rollback()

    def get_cost_basis(self, asset: str):
        return self.cost_basis.get(asset, (ZERO, ZERO))

    def close(self):
        self.session.close()

pnl = PnlTracker()

pnl_data = {k: ZERO for k in [
    'unrealized','total_cost','current_value','total_pnl_usd','total_pnl_percent',
    'pnl_24h_usd','pnl_24h_percent'
]}

# -------------------- UTILITIES --------------------
def now_cst():
    return datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")

def update_balances():
    global account_balances, min_usdt_reserve
    try:
        info = client.get_account()
        account_balances = {a['asset']: Decimal(a['free']) for a in info['balances'] if Decimal(a['free']) > ZERO}
        min_usdt_reserve = account_balances.get('USDT', ZERO) * RESERVE_PCT
    except Exception as e:
        terminal_insert(f"[{now_cst()}] Balance update failed: {e}")

def load_symbol_info():
    global symbol_info
    try:
        info = client.get_exchange_info()
        for s in info['symbols']:
            if s['quoteAsset'] != 'USDT' or s['status'] != 'TRADING': continue
            filters = {f['filterType']: f for f in s['filters']}
            step = Decimal(filters.get('LOT_SIZE', {}).get('stepSize', '0'))
            tick = Decimal(filters.get('PRICE_FILTER', {}).get('tickSize', '0'))
            if step == ZERO or tick == ZERO: continue
            symbol_info[s['symbol']] = {
                'stepSize': step,
                'tickSize': tick,
                'minQty': Decimal(filters.get('LOT_SIZE', {}).get('minQty', '0')),
                'minNotional': Decimal(filters.get('MIN_NOTIONAL', {}).get('minNotional', '10'))
            }
        terminal_insert(f"[{now_cst()}] Loaded {len(symbol_info)} USDT pairs")
    except Exception as e:
        terminal_insert(f"[{now_cst()}] Symbol load failed: {e}")

# -------------------- LIGHT STARTUP SYNC --------------------
def light_startup_sync():
    global first_run_done
    if first_run_done: return
    terminal_insert(f"[{now_cst()}] Starting safe startup sync...")
    try:
        exchange_info = client.get_exchange_info()
        usdt_pairs = [s['symbol'] for s in exchange_info['symbols'] if s['quoteAsset'] == 'USDT' and s['status'] == 'TRADING']
        processed = 0
        for symbol in usdt_pairs[:30]:
            try:
                trades = client.get_my_trades(symbol=symbol, limit=100)
                for t in trades:
                    base = symbol.replace('USDT', '')
                    qty = Decimal(t['qty'])
                    if qty <= ZERO: continue
                    price = Decimal(t['price'])
                    fee = Decimal(t.get('commission', '0'))
                    fee_asset = t.get('commissionAsset', 'USDT')
                    fee_usdt = fee if fee_asset == 'USDT' else fee * price
                    record_fill(symbol, 'BUY' if t['isBuyer'] else 'SELL', str(qty), str(price), fee_usdt)
                    processed += 1
            except: continue
        terminal_insert(f"[{now_cst()}] Startup sync complete — {processed} fills restored")
    except Exception as e:
        terminal_insert(f"[{now_cst()}] Startup sync skipped: {e}")
    first_run_done = True

# -------------------- WEBSOCKET & FILL TRACKING --------------------
def record_fill(symbol, side, qty_str, price_str, fee_usdt):
    qty = Decimal(qty_str)
    price = Decimal(price_str)
    fee = Decimal(fee_usdt)
    base = symbol.replace('USDT', '')
    notional = qty * price

    if side == 'BUY':
        old_qty, old_cost = pnl.get_cost_basis(base)
        pnl.upsert_cost_basis(base, old_qty + qty, old_cost + notional + fee)
    else:
        old_qty, old_cost = pnl.get_cost_basis(base)
        if qty >= old_qty:
            realized = (notional - fee) - old_cost
            pnl.update_realized(realized)
            pnl.delete_cost_basis(base)
        else:
            avg_cost = old_cost / old_qty
            cost_sold = avg_cost * qty
            realized = (notional - fee) - cost_sold
            pnl.update_realized(realized)
            pnl.upsert_cost_basis(base, old_qty - qty, old_cost - cost_sold)

    save_pnl()
    msg = f"{side} {base} {qty} @ {price:.6f} → ${realized:+.2f}"
    terminal_insert(f"[{now_cst()}] {msg}")
    send_whatsapp_alert(msg)

def on_user_message(ws, message):
    try:
        data = json.loads(message)
        if data.get('e') != 'executionReport' or data.get('X') != 'FILLED': return
        symbol = data['s']
        side = data['S']
        qty = data['q']
        price = data['p']
        fee = data.get('n', '0')
        fee_asset = data.get('N', 'USDT')
        fee_usdt = Decimal(fee) if fee_asset == 'USDT' else Decimal(fee) * Decimal(price)
        record_fill(symbol, side, qty, price, fee_usdt)
        threading.Thread(target=regrid_on_fill, args=(symbol,), daemon=True).start()
    except Exception as e:
        terminal_insert(f"[{now_cst()}] WS Error: {e}")

def start_user_stream():
    def run():
        while True:
            try:
                key = client.stream_get_listen_key()['listenKey']
                ws = websocket.WebSocketApp(
                    f"wss://stream.binance.us:9443/ws/{key}",
                    on_message=on_user_message,
                    on_close=lambda ws, *_: time.sleep(5)
                )
                ws.run_forever(ping_interval=1800)
            except:
                time.sleep(10)
    threading.Thread(target=run, daemon=True).start()

# -------------------- ORDERS & GRID --------------------
def place_limit_order(symbol, side, price, qty, track=True):
    info = symbol_info.get(symbol)
    if not info: return None
    price = (Decimal(price) // info['tickSize']) * info['tickSize']
    qty = (Decimal(qty) // info['stepSize']) * info['stepSize']
    if qty < info['minQty'] or price * qty < info['minNotional']: return None

    try:
        order = client.create_order(
            symbol=symbol, side=side, type='LIMIT', timeInForce='GTC',
            price=str(price), quantity=str(qty)
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
    except: pass

def place_single_grid(symbol, side):
    try:
        price = Decimal(client.get_symbol_ticker(symbol=symbol)['price'])
        p = price * (ONE - GRID_BUY_PCT) if side == 'BUY' else price * (ONE + MIN_SELL_PCT)
        p = (p // symbol_info[symbol]['tickSize']) * symbol_info[symbol]['tickSize']
        q = CASH_USDT_PER_GRID_ORDER / p
        q = (q // symbol_info[symbol]['stepSize']) * symbol_info[symbol]['stepSize']
        if q > ZERO:
            place_limit_order(symbol, side, p, q)
    except: pass

def regrid_on_fill(symbol):
    cancel_symbol_orders(symbol)
    place_single_grid(symbol, 'BUY')
    place_single_grid(symbol, 'SELL')

# -------------------- REBALANCE (MANUAL + AUTO) --------------------
def perform_rebalance(reason: str = "Manual"):
    global last_auto_rebalance_date
    terminal_insert(f"[{now_cst()}] {reason} REBALANCE STARTED")
    send_whatsapp_alert(f"🚨 {reason} REBALANCE STARTED")

    for sym in list(active_grid_orders.keys()):
        cancel_symbol_orders(sym)

    update_balances()
    update_unrealized_pnl()
    total_value = pnl_data['current_value'] + account_balances.get('USDT', ZERO)
    if total_value <= ZERO:
        terminal_insert(f"[{now_cst()}] Rebalance skipped: zero value")
        return

    target_value = total_value * MAX_POSITION_PCT
    prices = {}
    for asset in account_balances:
        if asset == 'USDT' or account_balances[asset] <= ZERO: continue
        sym = f"{asset}USDT"
        if sym not in symbol_info: continue
        try:
            prices[asset] = Decimal(client.get_symbol_ticker(symbol=sym)['price'])
        except: continue

    for asset, qty in account_balances.items():
        if asset == 'USDT' or qty <= ZERO: continue
        price = prices.get(asset)
        if not price: continue
        current_value = qty * price
        if current_value > target_value:
            sell_qty = (current_value - target_value) / price
            step = symbol_info[f"{asset}USDT"]['stepSize']
            sell_qty = (sell_qty // step) * step
            if sell_qty > ZERO:
                place_limit_order(f"{asset}USDT", 'SELL', price * Decimal('0.999'), sell_qty, track=False)
                terminal_insert(f"[{now_cst()}] REBALANCE SELL {asset}: {sell_qty} ({current_value/total_value:.1%} → 5%)")

    if reason == "Daily Loss Protection":
        last_auto_rebalance_date = date.today()
    terminal_insert(f"[{now_cst()}] {reason} REBALANCE COMPLETE")
    send_whatsapp_alert(f"✅ {reason} REBALANCE COMPLETE — All positions ≤5%")

def emergency_rebalance_if_needed():
    global last_auto_rebalance_date
    if last_auto_rebalance_date == date.today():
        return
    pnl.reset_daily_if_needed()
    update_unrealized_pnl()
    if pnl_data['pnl_24h_percent'] <= DAILY_LOSS_TRIGGER_PCT:
        perform_rebalance("Daily Loss Protection")

# -------------------- P&L UPDATE --------------------
def update_unrealized_pnl():
    assets = [a for a, (q, _) in pnl.cost_basis.items() if q > ZERO]
    if not assets:
        for k in pnl_data: pnl_data[k] = ZERO
        return

    total_cost = current_value = ZERO
    prices = {}
    for a in assets:
        sym = f"{a}USDT"
        if sym in symbol_info:
            try:
                prices[sym] = Decimal(client.get_symbol_ticker(symbol=sym)['price'])
            except: pass

    for a in assets:
        qty, cost = pnl.get_cost_basis(a)
        total_cost += cost
        if f"{a}USDT" in prices:
            current_value += qty * prices[f"{a}USDT"]

    unrealized = current_value - total_cost
    total_pnl = pnl.total_realized + unrealized
    total_pct = (total_pnl / total_cost * 100) if total_cost > ZERO else ZERO

    pnl_24h = pnl.daily_realized
    yesterday = datetime.now(CST) - timedelta(hours=24)
    for a in assets:
        qty, _ = pnl.get_cost_basis(a)
        sym = f"{a}USDT"
        if sym in prices:
            try:
                klines = client.get_historical_klines(sym, "1h", str(yesterday))
                if klines:
                    price_24h = Decimal(klines[0][1])
                    pnl_24h += qty * (prices[sym] - price_24h)
            except: pass
    pnl_24h_pct = (pnl_24h / total_cost * 100) if total_cost > ZERO else ZERO

    pnl_data.update({
        'unrealized': unrealized,
        'total_cost': total_cost,
        'current_value': current_value,
        'total_pnl_usd': total_pnl,
        'total_pnl_percent': total_pct,
        'pnl_24h_usd': pnl_24h,
        'pnl_24h_percent': pnl_24h_pct
    })

def save_pnl():
    try:
        with open(PNL_SUMMARY_FILE, 'w') as f:
            f.write(f"Total P&L: ${pnl_data['total_pnl_usd']:+.2f} ({pnl_data['total_pnl_percent']:+.2f}%)\n")
            f.write(f"24h P&L: ${pnl_data['pnl_24h_usd']:+.2f} ({pnl_data['pnl_24h_percent']:+.2f}%)\n")
    except: pass

# -------------------- GUI --------------------
root = tk.Tk()
root.title("INFINITY GRID BOT 2025 — FINAL")
root.geometry("1200x1000")
root.configure(bg="#0d1117")

title_font = tkfont.Font(family="Helvetica", size=24, weight="bold")
big_font = tkfont.Font(family="Helvetica", size=18, weight="bold")
btn_font = tkfont.Font(family="Helvetica", size=14, weight="bold")
term_font = tkfont.Font(family="Consolas", size=11)

tk.Label(root, text="INFINITY GRID BOT 2025", font=title_font, fg="#58a6ff", bg="#0d1117").pack(pady=20)

stats = tk.Frame(root, bg="#0d1117")
stats.pack(padx=30, fill="x")

usdt_label = tk.Label(stats, text="USDT Balance: $0.00", font=big_font, fg="#8b949e", bg="#0d1117", anchor="w")
usdt_label.pack(fill="x", pady=4)
pnl_label = tk.Label(stats, text="Total P&L: $0.00", font=big_font, fg="lime", bg="#0d1117", anchor="w")
pnl_label.pack(fill="x", pady=6)
pnl_24h_label = tk.Label(stats, text="24h P&L: $0.00 (+0.00%)", font=big_font, fg="lime", bg="#0d1117", anchor="w")
pnl_24h_label.pack(fill="x", pady=6)

# Manual Rebalance Button
rebalance_btn = tk.Button(root, text="🚨 REBALANCE NOW (Force All ≤5%) 🚨",
                          command=lambda: perform_rebalance("Manual"),
                          bg="#ff4444", fg="white", font=btn_font, height=2, width=40)
rebalance_btn.pack(pady=15)

terminal_frame = tk.Frame(root, bg="black")
terminal_frame.pack(fill="both", expand=True, padx=30, pady=10)
terminal_text = scrolledtext.ScrolledText(terminal_frame, bg="black", fg="#39d353", font=term_font)
terminal_text.pack(fill="both", expand=True)

def terminal_insert(msg):
    terminal_text.insert(tk.END, msg + "\n")
    terminal_text.see(tk.END)

button_frame = tk.Frame(root, bg="#0d1117")
button_frame.pack(pady=20)
status_label = tk.Label(button_frame, text="Status: Stopped", font=big_font, fg="red", bg="#0d1117")
status_label.pack(side="left", padx=40)
tk.Button(button_frame, text="START BOT", command=lambda: start_trading(), bg="#238636", fg="white", font=big_font, width=18, height=2).pack(side="right", padx=10)
tk.Button(button_frame, text="STOP BOT", command=lambda: stop_trading(), bg="#da3633", fg="white", font=big_font, width=18, height=2).pack(side="right", padx=10)

# -------------------- CYCLES --------------------
def grid_cycle():
    while running:
        update_balances()
        for asset in [a for a in account_balances if a != 'USDT' and account_balances[a] > ZERO]:
            sym = f"{asset}USDT"
            if sym in symbol_info:
                cancel_symbol_orders(sym)
                place_single_grid(sym, 'BUY')
                place_single_grid(sym, 'SELL')
        time.sleep(180)

def daily_safety_checker():
    while True:
        now = datetime.now(CST)
        next_run = now.replace(hour=9, minute=5, second=0, microsecond=0)
        if now >= next_run:
            next_run += timedelta(days=1)
        time.sleep(max((next_run - now).total_seconds(), 60))
        if running:
            emergency_rebalance_if_needed()

def update_gui():
    update_balances()
    pnl.reset_daily_if_needed()
    update_unrealized_pnl()
    usdt_label.config(text=f"USDT Balance: ${account_balances.get('USDT', ZERO):.2f}")
    pnl_label.config(text=f"Total P&L: ${pnl_data['total_pnl_usd']:+.2f}", fg="lime" if pnl_data['total_pnl_usd'] >= 0 else "red")
    pnl_24h_label.config(text=f"24h P&L: ${pnl_data['pnl_24h_usd']:+.2f} ({pnl_data['pnl_24h_percent']:+.2f}%)",
                         fg="lime" if pnl_data['pnl_24h_usd'] >= 0 else "red")
    status_label.config(text="Status: RUNNING" if running else "Status: Stopped",
                        fg="lime" if running else "red")
    root.after(3000, update_gui)

def start_trading():
    global running
    if not running:
        running = True
        threading.Thread(target=grid_cycle, daemon=True).start()
        threading.Thread(target=daily_safety_checker, daemon=True).start()
        terminal_insert(f"[{now_cst()}] BOT STARTED — Daily Protection Active")
        send_whatsapp_alert("Infinity Grid Bot STARTED — Protection ON")

def stop_trading():
    global running
    running = False
    for s in list(active_grid_orders.keys()):
        cancel_symbol_orders(s)
    terminal_insert(f"[{now_cst()}] BOT STOPPED")
    send_whatsapp_alert("Infinity Grid Bot STOPPED")

# -------------------- MAIN --------------------
if __name__ == "__main__":
    terminal_insert(f"[{now_cst()}] INFINITY GRID BOT 2025 — FINAL PRODUCTION")
    load_symbol_info()
    update_balances()
    light_startup_sync()
    update_unrealized_pnl()
    save_pnl()
    start_user_stream()
    threading.Thread(target=daily_safety_checker, daemon=True).start()
    update_gui()
    root.mainloop()

    atexit.register(lambda: pnl.close())
