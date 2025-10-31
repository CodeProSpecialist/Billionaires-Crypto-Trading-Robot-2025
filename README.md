# 🚀 Billionaires Crypto Trading Robot 2025: Binance.US Edition 🚀
**Skyrocketing Wealth Creation with Precision Automation**  

*****
Update to the newest version 
for the best working 
trading robot python code. 
Newest Python code updates were 
completed on October 31, 2025. 
*****

( Note: delete the file named 
binance_trades.db when restarting the program if there might be any configuration changes. 
This also fixes any program startup errors. )

### Overview of the Binance.US Dynamic Trailing Bot

This bot is a fully automated trading system designed for Binance.US, focusing on detecting price dips for buys and stalls/profits for sells in USDT-paired cryptocurrencies. It's built with robustness in mind, including custom rate limiting, error handling, a dashboard for monitoring, and database persistence. Below, I'll describe each major feature in detail, including how it works, its purpose, and key implementation notes. The features are categorized for clarity.

#### 1. **Dynamic Trailing Buy Mechanism**
   - **Description**: This feature monitors symbols for "flash dips" (rapid price drops) or strong sell pressure signals to trigger buys. It uses a dedicated thread per symbol (`DynamicBuyThread`) that polls the order book every 1 second. Key signals include:
     - RSI ≤ 35 (oversold), bullish trend from BBANDS and MACD.
     - Sell pressure peak ≥ 60% dropping by 10% in recent history (deque of 5 samples).
     - Price near 24h low (≤ 1.01x low).
     - Rapid drop ≥ 1% in 5 seconds → immediate market buy.
     - Trailing: Buys if price rises 0.3% from lowest seen.
   - **How it Works**: The thread caches order book and price data. On trigger, it allocates 10% of available USDT (minus min balance) for a limit or market buy. Orders are tracked in DB, with cooldown (15 min per symbol to avoid spam buys).
   - **Purpose**: Captures opportunities in volatile markets by buying low during dips, reducing risk of buying at peaks.
   - **Implementation Notes**: Uses TALIB for indicators, Decimal for precision. Errors are logged but don't stop the thread. Buy cooldown uses a dict with timestamps.

#### 2. **Dynamic Trailing Sell Mechanism**
   - **Description**: For held positions, this feature (`DynamicSellThread`) trails the price to sell on profit or stall. Signals include:
     - Net return ≥ 0.8% after fees.
     - RSI ≥ 65 (overbought).
     - Buy pressure spike ≥ 65% dropping to ≤ 55% (deque of 5 samples).
     - Price falls 0.5% from peak → sell.
     - 15-min stall (no new peak) → market sell.
   - **How it Works**: Thread per position polls order book. On trigger, places limit or market sell for full quantity. Stall detection uses a list of peak timestamps/prices, pruned after 15 min.
   - **Purpose**: Locks in profits during rallies or prevents losses on stagnant prices, automating "take profit" and "cut stagnation".
   - **Implementation Notes**: Fees fetched dynamically. Uses position data from DB. Errors logged, thread-safe with locks.

#### 3. **15-Min Price Stall Detection for Sells**
   - **Description**: Specific sub-feature in sell thread: If no new price peak in 15 minutes, it triggers a market sell to exit the position.
   - **How it Works**: Tracks peaks with timestamps in a list, prunes old ones. If last peak is ≥15 min old, sells.
   - **Purpose**: Avoids holding "dead" assets that aren't moving, freeing capital for better opportunities.
   - **Implementation Notes**: Integrated in `detect_price_stall`. Sends WhatsApp alert on trigger.

#### 4. **Full Professional Dashboard**
   - **Description**: A terminal-based UI (`print_professional_dashboard`) updating every 30 seconds, showing:
     - Time, USDT balance, portfolio value.
     - Active threads, trailing buys/sells.
     - Rate limit status (OFF during grace), circuit breaker state.
     - Positions table: Symbol, qty, entry/current price, RSI, P&L %, profit, status.
     - Total unrealized P&L.
     - Market universe: Valid symbols, avg volume, price range.
     - Buy watchlist: Oversold symbols with sell pressure.
     - Sell watchlist: Profitable positions with overbought RSI.
   - **How it Works**: Clears terminal, uses ANSI colors for formatting. Queries DB for positions, API for real-time data. Locks for thread-safety.
   - **Purpose**: Provides real-time monitoring without external tools, helping users track performance and signals.
   - **Implementation Notes**: Uses locks to avoid race conditions. Grace period shown as countdown.

#### 5. **WhatsApp Alerts**
   - **Description**: Sends notifications via CallMeBot API for key events: Trailing active, flash dip buy, stall sell, executed trades.
   - **How it Works**: `send_whatsapp_alert` uses requests to hit the API with message. Env vars for key/phone.
   - **Purpose**: Keeps users informed in real-time without watching the dashboard.
   - **Implementation Notes**: Timeout 5s, errors logged but non-blocking.

#### 6. **SQLite Database for Trades and Positions**
   - **Description**: Persists trades, pending orders, positions in `binance_trades.db`.
     - Tables: Trades (side, price, qty, timestamp), PendingOrders (order ID, side, price, qty), Positions (symbol, qty, entry price, fee).
   - **How it Works**: DBManager context for sessions. Imports existing Binance assets at startup. Records fills from pending orders.
   - **Purpose**: Tracks history, average entry prices, quantities for P&L calculations. Survives restarts.
   - **Implementation Notes**: Uses SQLAlchemy. Positions updated on buys/sells, deleted on full exit.

#### 7. **Custom Rate Limiting with Exponential Backoff**
   - **Description**: Limits API calls: 8s base per thread, +3s per additional thread. Backoff on errors (8s → 16s → ... max 120s). Resets on success.
   - **How it Works**: `CustomRateLimiter` tracks per-thread timestamps/backoffs. `acquire` sleeps if needed. Bypassed during 8-min grace.
   - **Purpose**: Prevents Binance bans from over-calling (e.g., 1200/min weight limit). Backoff reduces spam during outages.
   - **Implementation Notes**: Thread-safe with locks. No external libs.

#### 8. **Global Circuit Breaker**
   - **Description**: After 5 consecutive failures (rate limits, timeouts), blocks all API calls for 60s (OPEN state). Then HALF-OPEN: Allows one probe thread. 3 successes → CLOSED; failure → OPEN again.
   - **How it Works**: `acquire` checks state, blocks in OPEN. `record_failure/success` updates count/state. Bypassed during grace.
   - **Purpose**: Protects against cascading failures. If Binance is down (e.g., maintenance, high load), it pauses the bot to avoid bans or infinite loops. Auto-recovers without manual intervention, reducing downtime. In volatile markets, it prevents "throttling" escalation where repeated failures lead to longer bans. By halting on repeated errors, it saves API weight for when the service recovers, improving reliability and compliance.
   - **Why the Circuit Breaker Helps in Detail**:
     - **Prevents API Abuse**: Binance.US has strict rate limits (e.g., 1200 requests/min). Without a breaker, repeated errors (429/418 codes) could lead to IP bans or account suspension. The breaker "breaks the circuit" to stop calls, giving time for recovery.
     - **Handles Outages Gracefully**: During Binance downtime or network issues, the bot would otherwise loop and log errors endlessly, consuming resources. The 60s timeout lets the system cool down, then probes safely.
     - **Auto-Recovery**: HALF-OPEN allows gradual resumption (one thread tests). 3 successes confirm stability, resuming full operation. This minimizes manual restarts.
     - **Reduces Load on Bot**: Stops unnecessary threads during problems, freeing CPU/memory. In a multi-thread setup (one per symbol), this prevents amplification of errors.
     - **Compliance and Cost Savings**: Avoids overage fees or restrictions. For high-volume bots, it's essential for long-term operation without intervention.
     - **Edge Cases**: E.g., if API returns 503 (service unavailable), breaker activates. During grace period, it's disabled to allow startup without stalls.
     - **Implementation Notes**: Global (shared across threads). State shown in dashboard.

#### 9. **Zero Rate Limits for First 8 Minutes (Grace Period)**
   - **Description**: For 480s after startup, all rate limits, backoff, circuit breaker are bypassed.
   - **How it Works**: `STARTUP_GRACE_END` timestamp checked in limiter/breaker/client. Set after DB import + first dashboard.
   - **Purpose**: Allows fast startup, symbol fetch, position import without stalls (e.g., DB creation lag from API calls).
   - **Implementation Notes**: Thread-safe lock. Dashboard shows countdown.

#### 10. **Custom Retry for API Calls**
   - **Description**: `@retry_custom` decorator retries functions 5 times on error, exponential delay (2s → 4s → ..... 32s).
   - **How it Works**: Wraps API-heavy functions like fetch symbols, get klines, order book.
   - **Purpose**: Handles transient errors (network blips, API hiccups) without failing the bot.
   - **Implementation Notes**: No external libs, pure Python. Logs retries.

#### 11. **Thread-Safe Operation and Clean Shutdown**
   - **Description**: Uses locks for shared data (threads, DB, dashboard). SIGINT handler stops all threads gracefully.
   - **How it Works**: `thread_lock` for thread dicts. Signal handler sets `running = False`.
   - **Purpose**: Prevents race conditions in multi-threaded environment (one thread per symbol + buy/sell).
   - **Implementation Notes**: Background process threads, limiter release on shutdown.

#### 12. **Rapid Drop Detection for Buys**
   - **Description**: In buy thread, checks for ≥1% drop in 5s → market buy.
   - **How it Works**: Caches last price/timestamp per symbol.
   - **Purpose**: Captures "flash crashes" for quick entry.
   - **Implementation Notes**: Global cache dict.

#### 13. **Buy Cooldown (15 Minutes)**
   - **Description**: Prevents repeat buys on same symbol for 15 min after a buy.
   - **How it Works**: `buy_cooldown` dict with timestamps.
   - **Purpose**: Avoids over-buying on volatile symbols.
   - **Implementation Notes**: Checked before starting buy thread.

#### 14. **Position Import from Binance at Startup**
   - **Description**: Scans Binance account balances, imports non-USDT assets as positions with current price as entry.
   - **How it Works**: `import_owned_assets_to_db` fetches balances, prices, fees; adds to DB if not exist.
   - **Purpose**: Syncs bot with existing holdings on restart.
   - **Implementation Notes**: Skips zero qty or stablecoins.

#### 15. **Technical Indicators (RSI, BBANDS, MACD)**
   - **Description**: Uses TALIB to calculate RSI (oversold/overbought), BBANDS (price relative to middle band), MACD (trend direction).
   - **How it Works**: Fetches 1m klines (last 100), computes in `get_rsi_and_trend`.
   - **Purpose**: Filters signals for buys (oversold + bullish) and sells (overbought + profit).
   - **Implementation Notes**: Handles finite checks, defaults to unknown.

This bot is designed for reliability in volatile crypto markets, with custom controls for easy tweaking (e.g., change `base_seconds` in limiter). All features are thread-safe and logged for debugging.

## How to Run

```bash
python3 2025-coin-trading-bot.py
```

> Press **Ctrl+C** to shut down gracefully.

---


### **Setup Requirements**

```bash
# Environment Variables
export BINANCE_API_KEY="..."
export BINANCE_API_SECRET="..."
export CALLMEBOT_API_KEY="..."   # optional
export CALLMEBOT_PHONE="..."     # optional
```

```bash
pip install python-binance sqlalchemy talib tenacity requests numpy
```

---

### **Summary: How It Works (Step-by-Step)**

1. **Start** → Load API keys, init DB, fetch valid USDT pairs  
2. **Spawn 1 thread per coin** → each runs forever  
3. **Every 1 second**:  
   - Pull klines → compute RSI, MACD, BB, trend  
   - Pull order book → compute bid/ask pressure  
   - Check 24h low & volume  
4. **If BUY signal** → place limit buy @ best bid (adjusted)  
5. **If SELL signal** → place limit sell @ next tick above ask  
6. **Main loop (every 15s)**:  
   - Check filled orders → update DB  
   - Print **live dashboard** with P&L  
7. **Repeat 24/7** → fully automated

---

**Professional. Fast. Precise. Fee-Aware. Visual.**  
*Built for serious traders who want automation without compromise.*

Unleash the power of automated crypto trading with the Billionaires Crypto Trading Robot 2025, a cutting-edge bot crafted for Binance.US. Target 0.8% net profits per trade on USDT pairs using advanced mean-reversion with comprehensive momentum, oscillator, and trend filters. **Not affiliated with Binance.US or CallMeBot.** **Profits are not guaranteed; you risk losing all or part of your investment.** Always test in simulation mode (e.g., Binance.US testnet) before live trading and proceed at your own risk!

### Legal Disclaimers

**Important Notice: This information is provided as of October 28, 2025, and cryptocurrency markets, regulations, and technologies evolve rapidly. Always verify the latest information and consult professionals before using this bot.**

- **No Affiliation or Endorsement**: This trading bot is not affiliated with, endorsed by, or sponsored by Binance.US or CallMeBot. All interactions with these platforms are at the user's sole discretion and risk, subject to their respective terms of service.
- **Not Financial, Investment, or Legal Advice**: This bot and its description are for informational and educational purposes only. They do not constitute financial, investment, tax, legal, or professional advice. The author is not a financial advisor, broker, or registered investment advisor. All trading decisions are your sole responsibility. Seek independent advice from qualified professionals to assess suitability for your circumstances.
- **High Risk of Loss**: Cryptocurrency trading involves substantial risks, including the potential for **complete or partial loss of your investment**. Prices are highly volatile, subject to rapid and unpredictable changes due to market sentiment, regulatory news, technological issues, or external factors. Past performance does not guarantee future results. You could lose more than your initial investment.
- **No Guarantee of Profits**: **Profits are not guaranteed**. There is no assurance that this bot will generate profits or avoid losses. Trading strategies, including mean-reversion with momentum, oscillator, and trend filters, may fail in certain market conditions (e.g., high volatility, low liquidity, or black swan events). The 0.8% profit target is a goal, not a guarantee, and losses may occur due to fees, slippage, or unfavorable price movements.
- **Legal and Regulatory Compliance**: Cryptocurrency trading is subject to U.S. laws, including oversight by the SEC (for securities-like assets) and CFTC (for commodities). As of October 2025, the CLARITY Act and FIT21 provide clearer jurisdiction, but users must ensure compliance with AML/KYC requirements under the Bank Secrecy Act (BSA), sanctions screening, and state licensing (e.g., New York's BitLicense). Do not use this bot if prohibited in your jurisdiction. On October 17, 2025, the NFA eliminated certain disclosure guidance for digital assets but still requires disclosing material risks. Users are responsible for tax reporting (e.g., IRS Form 1099-DA for gains/losses) and any violations could result in penalties.
- **No Liability**: The author disclaims all liability for any direct, indirect, consequential, or special losses arising from using this bot, including trading losses, data inaccuracies, third-party service failures (e.g., Binance.US API, CallMeBot), or security breaches. You are responsible for securing API keys and accounts. The bot interacts with third-party services; any downtime, errors, or changes in their terms are beyond control.
- **Software Risks and Warranty Disclaimer**: The bot is provided "as is" without warranties of merchantability, fitness for purpose, or non-infringement. It may contain bugs, and users assume risks from technical failures, incorrect configurations, or unauthorized access. Test thoroughly in a simulation environment before live trading.
- **User Responsibilities**: You must comply with Binance.US and CallMeBot terms, maintain account security, and monitor trades. Stop using the bot if it violates any laws or platform rules. This bot does not provide custody services; all assets remain under your control on Binance.US.

By using this bot, you acknowledge these risks and agree to indemnify the author against any claims.


### Installation in Ubuntu 24.04 Linux (Anaconda Python Environment Preferred)

For optimal isolation and management of dependencies, use Anaconda. This setup ensures a controlled Python environment, avoiding conflicts with system Python.

#### Step-by-Step Anaconda Installation
1. **Download the Anaconda Installer**:  
   Open a terminal and download the latest Linux installer (as of October 2025, check for updates at https://www.anaconda.com/download):  
   ```bash
   wget https://repo.anaconda.com/archive/Anaconda3-2024.10-1-Linux-x86_64.sh
   ```

2. **Run the Installer**:  
   Execute the script:  
   ```bash
   bash Anaconda3-2024.10-1-Linux-x86_64.sh
   ```  
   - Press `Enter` to review the license.  
   - Scroll with `Space` and type `yes` to accept.  
   - Confirm the default path (e.g., `/home/yourusername/anaconda3`) by pressing `Enter`.  
   - Type `yes` to initialize Conda (this adds setup to `.bashrc`).

3. **Reload Shell Configuration**:  
   Apply changes:  
   ```bash
   source ~/.bashrc
   ```  
   Your terminal should show `(base)` indicating the base environment is active.

4. **Create a New Conda Environment**:  
   Create an isolated environment for the bot (e.g., with Python 3.11):  
   ```bash
   conda create -n trading_bot python=3.11
   ```  
   Activate it:  
   ```bash
   conda activate trading_bot
   ```  
   (To deactivate later: `conda deactivate`.)

5. **Install Dependencies**:  
   Inside the activated environment:  
   ```bash
   pip install python-binance ta-lib numpy pandas sqlalchemy requests tenacity
   ```  
   - Note: If `ta-lib` fails, install the system library first:  
     ### Simplified TA-Lib Installation (Version 0.6.4) on Ubuntu 24.04

To install the latest TA-Lib (v0.6.4) on Ubuntu 24.04:

1. **Install Build Tools**:
   ```bash
   sudo apt update
   sudo apt install -y build-essential wget autoconf automake libtool pkg-config python3-dev python3-pip
   ```

2. **Download and Build TA-Lib**:
   ```bash
   wget https://github.com/TA-Lib/ta-lib/releases/download/v0.6.4/ta-lib-0.6.4-src.tar.gz
   tar -xzf ta-lib-0.6.4-src.tar.gz
   cd ta-lib-0.6.4
   ./autogen.sh
   ./configure --prefix=/usr
   make -j$(nproc)
   sudo make install
   sudo ldconfig
   ```

3. **Install Python TA-Lib (v0.6.8)**:
   ```bash
   pip3 install TA-Lib==0.6.8
   ```

4. **Verify Installation**:
   ```bash
   pkg-config --modversion ta-lib  # Should show "0.6.4"
   python3 -c "import talib; print(talib.__version__)"  # Should show "0.6.8"
   ```

### Notes
- If `pip3 install` fails, try:
  ```bash
  TA_INCLUDE_PATH=/usr/include TA_LIBRARY_PATH=/usr/lib pip3 install TA-Lib==0.6.8
  ```
- If errors occur during `./configure` or `make`, ensure all dependencies are installed or run `autoreconf -fiv` before `./configure`.

Let me know if you hit any issues!

6. **Verify Installation**:  
   Run `conda list` to check packages. Test Python:  
   ```bash
   python -c "print('Hello, Anaconda!')"
   ```
********
Step-by-Step Guide to Create API Keys

Log In to Your Binance.US Account:

Go to www.binance.us and sign in with your credentials.


Access API Management:

Hover over your profile icon or email address in the upper right corner of the dashboard.
From the dropdown menu, select API Management (or Settings > API Management).


Create a New API Key:

In the API Management section, enter a descriptive name for your key (e.g., "Trading Bot Key").
Click Create API (or Create).
Complete 2FA verification (e.g., Google Authenticator or SMS code).
Confirm via email verification from Binance.US.


Copy Your Keys:

Your API Key and Secret Key will be displayed once—copy and save them securely (e.g., in a password manager).
The Secret Key will be hidden forever after you leave the page. If lost, delete the key and create a new one.


Configure Permissions (API Restrictions):

Defaults to Read-Only—ideal for data access (e.g., portfolio tracking).
For trading bots: Enable Spot Trading if needed (allows buying/selling), but NEVER enable Withdrawals unless absolutely required, as it risks fund theft.
IP Restrictions: Add trusted IP addresses (e.g., your server's IP) to limit access. Leave unrestricted only if necessary.
Click Save or Confirm to apply.
********

#### What to Add to `.bashrc` in Ubuntu Linux
The Anaconda installer automatically adds Conda initialization to `~/.bashrc` (e.g., export PATH and conda setup block). For the bot's environment variables (used via `os.getenv`), add these exports at the end of `~/.bashrc` (replace placeholders with your actual values):

```bash
# Environment variables for Billionaires Crypto Trading Robot
export BINANCE_API_KEY="your_binance_api_key_here"
export BINANCE_API_SECRET="your_binance_api_secret_here"
export CALLMEBOT_API_KEY="your_callmebot_api_key_here"
export CALLMEBOT_PHONE="your_phone_number_here"  # E.g., +1234567890
```

Run Program: 

```bash
conda activate 
python3 2025-coin-trading-bot.py
```

**Note**: Ensure the bot script is saved as `2025-coin-trading-bot.py`. Monitor logs in `crypto_trading_bot.log` for issues and the console dashboard for status. This setup is for educational use; live trading carries significant risks, including the potential to **lose all or part of your investment**, as outlined in the disclaimers. 

To set up CallMeBot for WhatsApp, visit https://www.callmebot.com/ and follow the instructions to get your API key and configure the service. Then, use `curl` or Python to send messages via the API.

This setup is for educational use; live trading carries significant risks, including the potential to **lose all or part of your investment**, as outlined in the disclaimers. 

The bot is not affiliated with or endorsed by Binance.US or CallMeBot. Always test in a simulation environment (e.g., Binance.US testnet) before deploying with real funds.
