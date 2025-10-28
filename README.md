# 🚀 Billionaires Crypto Trading Robot 2025: Binance.US Edition 🚀
**Skyrocketing Wealth Creation with Precision Automation**  

*****
Update to the newest version 
for the best working 
trading robot python code. 
Newest Python code updates were 
on October 28, 2025. 
*****

( Note: delete the file named 
binance_trades.db when restarting the program if there might be any configuration changes. 
This also fixes any program startup errors. )

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

### How the Crypto Trading Bot Works: Buying, Selling, Fee Compensation, and Profit Mechanism

The Billionaires Crypto Trading Robot is a Python-based automated system designed for Binance.US, focusing on USDT-paired cryptocurrencies priced between $1–$1000 with specific bullish signals. It uses a mean-reversion strategy enhanced with comprehensive indicators including RSI, MACD, MFI, Bollinger Bands, Stochastic Oscillator, ATR, SMA, EMA, and candlestick patterns, leveraging 60 days of historical price data (1-hour klines) stored in an SQLAlchemy database (SQLite by default). The bot runs in a loop every 60 seconds, checking signals, managing orders, processing filled orders, and sending CallMeBot WhatsApp alerts for key events. It also prints a professional dashboard to the console for real-time status monitoring.

On startup, it imports all owned nonzero-balance assets (any pair convertible to USDT) as tracked positions, using current market prices as entry points for profit calculations. It prioritizes **limit orders** for buys (to get maker fees) with a 300-second timeout and adaptive sells (market for urgent exits based on bearish signals, limit otherwise). All trades aim for a **net profit of at least 0.8% after fees**, with risk controls like allocating only 10% of balance per trade and maintaining a $2 minimum USDT buffer. **Profits are not guaranteed, and you risk losing all or part of your investment.**

Below, I'll break down the buying, selling, fee compensation, and profit mechanisms step by step, referencing the code's logic.

#### 1. **How the Bot Buys**
The bot buys only when a coin meets strict criteria for low risk and bullish potential, ensuring it enters near the lower Bollinger Band with oversold conditions and upward trend signals. This is checked in the `check_buy_signal` function during the main loop for symbols without existing positions.

- **Signal Detection**:
  - Queries the database and fetches 60 days of 1-hour kline data via Binance.US API.
  - Computes metrics like:
    - RSI (14-period) > 50 (indicating strength).
    - MFI (Money Flow Index, 14-period) ≤ 70 (avoids overbought).
    - MACD (12,26,9) > signal line and histogram > 0 (bullish momentum).
    - Stochastic Oscillator (5,3,3) K% ≤ 30 (oversold).
    - EMA (21-period) > SMA (50-period) (uptrend confirmation).
    - Current price ≤ lower Bollinger Band (20-period, 2-dev) * 1.005 (near oversold band).
    - Bullish candlestick patterns (e.g., hammer, inverted hammer, engulfing, morning star, three white soldiers) via TA-Lib in `is_bullish_candlestick_pattern`.
    - Bullish momentum status via TA-Lib patterns in `get_momentum_status`.
    - 24-hour quote volume > $15,000 (ensures liquidity).
    - Current price between $1–$1000.
  - If all filters pass, it triggers a buy.

- **Order Placement (`execute_buy` and `place_limit_buy_with_tracking`)**:
  - Checks USDT balance (> $2 buffer); allocates 10% max per trade.
  - Calculates buy price as current price * (1 - 0.001 - 0.5 * ATR / current price), where ATR is 14-period for volatility buffer.
  - Places a **limit buy** to qualify as a maker order (lower fees), adjusted for symbol filters (lot size, price tick, min notional).
  - Tracks the order in the database as a PendingOrder and monitors for fills in `check_and_process_filled_orders`.
  - On fill (polled in main loop): Records actual entry price, quantity, and buy fee rate (dynamic via `get_trade_fees`).
  - Stores or updates position in DB (symbol, qty, avg_entry_price, buy_fee_rate).
  - Sends alert: e.g., "BUY filled [symbol] @ [price] qty [qty]".
  - No buy if position already open, balance low, or signal invalid.

This ensures buys happen only on high-confidence oversold entries in uptrends, with data fetched on demand and persistence for restarts.

#### 2. **How the Bot Sells**
Selling occurs when a held position (including imported assets) reaches the profit target after fees, checked in `check_sell_signal` for all open positions (loaded from DB).

- **Signal Detection**:
  - Fetches current price and dynamic fees (maker/taker via API).
  - Calculates net profit percentage: `(current_price - avg_entry_price) / avg_entry_price - (buy_fee_rate + sell_fee)`.
    - `buy_fee_rate` is the stored rate from the buy (or default 0.1%).
    - `sell_fee` assumes taker for market sells (urgent) or maker for limit.
  - If net profit < 0.8% (`PROFIT_TARGET`), no sell.
  - Determines sell type: Market if bearish candlestick patterns (e.g., shooting star, hanging man, engulfing) or current price ≥ upper Bollinger Band * 0.995; otherwise limit.

- **Order Placement (`execute_sell`, `place_market_sell`, `place_limit_sell_with_tracking`)**:
  - For **market sell** (urgent exit): Immediate sell of full qty; uses taker fee.
  - For **limit sell** (controlled exit): Sets price at `avg_entry_price * (1 + PROFIT_TARGET + buy_fee_rate + sell_fee)` + 0.5 * ATR to lock in net profit with volatility buffer.
  - Adjusts for symbol filters (lot size, price tick, min notional).
  - Tracks limit sells as PendingOrder; processes fills in main loop.
  - On fill: Records exit price/profit, logs trade to DB, deletes position, sends alert: "SOLD [symbol] @ [exit_price] Profit $[profit]".
  - Market sells are instant; limit sells are polled via order status.

This adaptive approach captures profits while responding to bearish signals for quick exits in volatile conditions.

#### 3. **How the Bot Compensates for Fees**
Fees are dynamically handled to ensure trades are profitable **net of costs**, based on your Binance.US tier (queried via API). The bot fetches real-time maker/taker rates (defaults to 0.1% if API fails).

- **Fee Fetching**:
  - `get_trade_fees` queries `client.get_trade_fee(symbol)` for maker/taker rates (e.g., 0.001 maker, 0.001 taker in lower tiers).

- **Compensation in Buy**:
  - Limit buys use maker fee (lower); stored as `buy_fee_rate` in position for precise calculations.

- **Compensation in Sell**:
  - Profit calc subtracts **buy_fee_rate + sell_fee** (sell_fee as taker for market, maker for limit).
  - Sell price for limits is inflated by fees + profit target + ATR buffer, ensuring net gain.
  - Example (assuming 0.1% maker/taker):
    - Buy: 0.1% fee → Effective entry = avg_entry_price * (1 + 0.001).
    - Sell: Requires current_price >= effective entry * (1 + 0.008 + 0.001) to net 0.8% after sell fee.
  - Total round-trip compensation: Buffered into targets, preventing breakeven/loss trades.

Lower-tier fees (e.g., Tier 0 with near-zero rates) allow more frequent trades without code changes (dynamic fetch handles it).

#### 4. **How the Bot Profits**
The bot profits by buying oversold (near lower BB with oscillators confirming) in uptrends and selling at a fixed 0.8% net gain after fees, compounding over multiple trades. **Profits are not guaranteed, and you risk losing all or part of your investment.**

- **Strategy Overview**:
  - **Mean-Reversion + Comprehensive Filters**: Buys near lower BB (≤1.005x) but only if RSI >50, MFI ≤70, MACD bullish, Stochastic oversold (K% ≤30), EMA > SMA, bullish patterns/momentum. Avoids weak trends.
  - **Profit Target**: Strict 0.8% net after fees; holds until target or bearish signals trigger market sell.
  - **Risk Management**: 10% allocation/trade, $2 buffer, no overlapping positions per coin, ATR buffers for slippage/volatility.
  - **Expected Win Rate**: Design aims for high-confidence entries; small gains (0.8%) but selective (filters minimize losses). Losses can exceed gains in adverse markets, potentially depleting your balance.

- **Profit Calculation Example**:
  - Balance: $100 USDT.
  - Buy SOLUSDT at $20 (limit, 0.1% fee): Alloc $9.8 (10% - buffer), qty=0.49 SOL, fee=$0.0098, effective cost=$9.8098.
  - Price rises to $20.30 (1.5% up).
  - Net profit check: (20.30 - 20) / 20 - (0.001 + 0.001) = 0.015 - 0.002 = 0.013 > 0.008 → Sell if signals allow.
  - Sell (limit, 0.1% fee): Target price ~$20.36 (adjusted for fees/ATR), proceeds: $9.9564 - $0.00996 fee = $9.94644.
  - Profit: $9.94644 - $9.8098 = **$0.13664** (~1.39% gross, 0.8% net after fees).
  - **Risk Note**: If SOLUSDT drops, no sell unless profit target hit; no built-in stop-loss, risking loss.

- **Overall Profitability**:
  - Relies on all USDT pairs filtered by criteria, including imported assets.
  - Compounds: Selective trades at 0.8% net; minus losses (filters aim to minimize). 
  - Enhancements: 60d history for robust indicators, alerts/dashboard for monitoring, DB persistence for graceful shutdown/restart, pending order tracking for reliable fills.

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

#### What to Add to `.bashrc` in Ubuntu Linux
The Anaconda installer automatically adds Conda initialization to `~/.bashrc` (e.g., export PATH and conda setup block). For the bot's environment variables (used via `os.getenv`), add these exports at the end of `~/.bashrc` (replace placeholders with your actual values):

```bash
# Environment variables for Billionaires Crypto Trading Robot
export BINANCE_API_KEY="your_binance_api_key_here"
export BINANCE_API_SECRET="your_binance_api_secret_here"
export CALLMEBOT_API_KEY="your_callmebot_api_key_here"
export CALLMEBOT_PHONE="your_phone_number_here"  # E.g., +1234567890
```

- After editing, reload: `source ~/.bashrc`.
- **Security Tip**: Store sensitive keys securely (e.g., use a .env file and `python-dotenv` if preferred, but the code uses `os.getenv` directly). Avoid committing `.bashrc` to version control. Alternatively, place keys in a secure file and source it in `.bashrc`:  
  ```bash
  source /path/to/secure_env.sh
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
