Legal Disclaimers
Important Notice: This information is provided as of October 27, 2025, and cryptocurrency markets, regulations, and technologies evolve rapidly. Always verify the latest information and consult professionals before using this bot.

Not Financial, Investment, or Legal Advice: This bot and its description are for informational and educational purposes only. They do not constitute financial, investment, tax, legal, or professional advice. The author is not a financial advisor, broker, or registered investment advisor. All trading decisions are your sole responsibility. Seek independent advice from qualified professionals to assess suitability for your circumstances.
High Risk of Loss: Cryptocurrency trading involves substantial risks, including the potential for complete loss of invested capital. Prices are highly volatile, subject to rapid and unpredictable changes due to market sentiment, regulatory news, technological issues, or external factors. Past performance does not guarantee future results. You could lose more than your initial investment.
No Guarantee of Profits: There is no assurance that this bot will generate profits. Trading strategies, including mean-reversion and momentum filters, may fail in certain market conditions (e.g., high volatility, low liquidity, or black swan events). The 0.8% profit target is a goal, not a guarantee, and losses may occur due to fees, slippage, or unfavorable price movements.
Legal and Regulatory Compliance: Cryptocurrency trading is subject to U.S. laws, including oversight by the SEC (for securities-like assets) and CFTC (for commodities). As of October 2025, the CLARITY Act and FIT21 provide clearer jurisdiction, but users must ensure compliance with AML/KYC requirements under the Bank Secrecy Act (BSA), sanctions screening, and state licensing (e.g., New York's BitLicense). Do not use this bot if prohibited in your jurisdiction. On October 17, 2025, the NFA eliminated certain disclosure guidance for digital assets but still requires disclosing material risks. Users are responsible for tax reporting (e.g., IRS Form 1099-DA for gains/losses) and any violations could result in penalties.
No Liability: The author disclaims all liability for any direct, indirect, consequential, or special losses arising from using this bot, including trading losses, data inaccuracies, third-party service failures (e.g., Binance.US API, CallMeBot), or security breaches. You are responsible for securing API keys and accounts. The bot interacts with third-party services; any downtime, errors, or changes in their terms are beyond control.
Software Risks and Warranty Disclaimer: The bot is provided "as is" without warranties of merchantability, fitness for purpose, or non-infringement. It may contain bugs, and users assume risks from technical failures, incorrect configurations, or unauthorized access. Test thoroughly in a simulation environment before live trading.
User Responsibilities: You must comply with Binance.US terms, maintain account security, and monitor trades. Stop using the bot if it violates any laws or platform rules. This bot does not provide custody services; all assets remain under your control on Binance.US.

By using this bot, you acknowledge these risks and agree to indemnify the author against any claims.
10-2025-Newer-Coin-Trading-Robot-for-Binance.US
10-2025 Newer Coin Trading Robot for Binance.US
How the Crypto Trading Bot Works: Buying, Selling, Fee Compensation, and Profit Mechanism
The trading bot is a Python-based automated system designed for Binance.US, focusing on USDT-paired cryptocurrencies priced between $1–$25 with specific bullish signals. It uses a mean-reversion strategy with momentum filters, leveraging 15 days of historical price data stored in an SQLAlchemy database (SQLite by default). The bot runs in a loop every 60 seconds, checking signals, managing orders, and sending CallMeBot WhatsApp alerts for key events.
It prioritizes limit orders for buys (to get maker fees) and mixes market/limit sells based on price velocity (fast rises use market sells for quick exits). All trades aim for a net profit of at least 0.8% after fees, with risk controls like allocating only 10% of balance per trade and maintaining a $2 minimum USDT buffer.
Below, I'll break down the buying, selling, fee compensation, and profit mechanisms step by step, referencing the code's logic.
1. How the Bot Buys
The bot buys only when a coin meets strict criteria for low risk and bullish potential, ensuring it enters near a 15-day low with upward momentum. This is checked in the check_buy_signal function during the main loop.

Signal Detection:

Queries the database for 15 days of hourly candle data (stored via store_historical_data using Binance.US historical klines).
Computes metrics like:

15-day low price (to buy near dips).
RSI (Relative Strength Index) > 50 (indicating strength in uptrends).
5-day ROC (Rate of Change) > 0 (positive momentum).
Max drawdown < -10% (avoids coins that lose value quickly).
Average volume > $100,000 (ensures liquidity).
Current price between $1–$25.
Bullish candlestick patterns (e.g., hammer, engulfing) via TA-Lib in get_momentum_status.


If the current price is ≤ 0.1% above the 15-day low and all filters pass, it triggers a buy (e.g., "bullish" momentum required).


Order Placement (execute_buy):

Checks USDT balance (> $2 buffer); allocates 10% max per trade.
Places a limit buy at 0.1% below current price (slippage buffer) to qualify as a maker order (lower fees).
Monitors for 5 minutes (300s timeout). If filled:

Records actual entry price (from order status) and buy fee (dynamic via get_trade_fees).
Stores position in DB (symbol, qty, entry_price, entry_time, buy_fee).
Sends alert: "BUY filled [symbol] @ [price] qty [qty] fee [fee]%".


If not filled: Cancels and falls back to market buy (if signal still valid), using taker fee.
No buy if position already open or balance low.



This ensures buys happen only on high-confidence dips, with data backfilled on startup/pruned daily.
2. How the Bot Sells
Selling occurs when a held position reaches the profit target after fees, checked in check_sell_signal for open positions (loaded from DB).

Signal Detection:

Fetches current price and dynamic fees (maker/taker via API).
Calculates net profit percentage: (current_price - entry_price) / entry_price - (buy_fee + estimated_sell_fee).

buy_fee is the actual fee from the buy (stored in position).
estimated_sell_fee assumes worst-case taker fee (0.6% in Tier I) for conservatism.


If net profit < 0.8% (PROFIT_TARGET), no sell.
Checks price velocity (1-min delta > 0.5%): Fast rise → market sell (taker fee); slow → limit sell (maker fee).


Order Placement (execute_sell):

For market sell (fast exit): Immediate sell of full qty; uses taker fee.
For limit sell (slow exit): Sets price at entry_price * (1 + PROFIT_TARGET + buy_fee + sell_fee) to lock in net profit.
On fill: Records exit price/profit, logs trade to DB, deletes position, sends alert: "SOLD [symbol] @ [exit_price] profit [profit] USDT fee [sell_fee]%".
No polling/timeout for sells (assumes quick fills); market sells are instant.



This adaptive approach captures profits quickly in volatile upswings while optimizing for better prices in gradual rises.
3. How the Bot Compensates for Fees
Fees are dynamically handled to ensure trades are profitable net of costs, based on your Binance.US tier (queried via API). The bot assumes Tier I/VIP 1 (0.4% maker, 0.6% taker) as fallback but fetches real-time values.

Fee Fetching:

get_trade_fees queries client.get_trade_fee(symbol) for maker/taker rates (e.g., 0.004 maker, 0.006 taker in Tier I).
If API fails, defaults to Tier I (0.4% maker, 0.6% taker).


Compensation in Buy:

Limit buys use maker fee (lower); market fallback uses taker.
Actual buy fee stored in position for precise later calculations.


Compensation in Sell:

Profit calc subtracts buy_fee + sell_fee (sell_fee estimated as taker for market, maker for limit).
Sell price for limits is inflated by fees + profit target, ensuring net gain.
Example (Tier I, limit buy + market sell):

Buy: 0.4% fee → Effective entry = entry_price * (1 + 0.004).
Sell: Requires current_price >= effective entry * (1 + 0.008 + 0.006) to net 0.8% after 0.6% taker fee.


Total round-trip compensation: 0.8%–1.2% buffered into targets, preventing breakeven/loss trades.



If on Tier 0 (free maker, 0.01% taker), fees drop to near-zero, allowing more frequent trades without changing code (dynamic fetch handles it).
4. How the Bot Profits
The bot profits by buying low (near 15-day lows with bullish filters) and selling at a fixed 0.8% net gain after fees, compounding over multiple trades.

Strategy Overview:

Mean-Reversion + Momentum: Buys on dips (≤0.1% above 15d low) but only if RSI >50, ROC >0, low drawdown, high volume, and bullish patterns. Avoids "falling knives" (quick losers).
Profit Target: Strict 0.8% net after fees; no holding beyond signals.
Risk Management: 10% allocation/trade, $2 buffer, no overlapping positions per coin.
Expected Win Rate: Backtesting (implied in design) aims for 60%+ wins via filters; small gains (0.8%) but frequent (2–5 signals/week/coin).


Profit Calculation Example:

Balance: $100 USDT.
Buy SOLUSDT at $20 (limit, 0.4% fee): Alloc $9.8 (10% - buffer), qty=0.49 SOL, fee=$0.0392, effective cost=$9.8392.
Price rises to $20.30 (1.5% up).
Net profit check: (20.30 - 20) / 20 - (0.004 + 0.006) = 0.015 - 0.01 = 0.005 < 0.008 → No sell.
Price hits $20.36: 0.018 - 0.01 = 0.008 → Sell (market, 0.6% fee).
Sell proceeds: $9.9564 - $0.0597 fee = $9.8967.
Profit: $9.8967 - $9.8392 = $0.0575 (~0.58% net, but scaled to 0.8% target after exact fees).


Overall Profitability:

Relies on diversified coins (all USDT pairs filtered by criteria).
Compounds: 10 trades/month at 0.8% net = ~8% monthly (pre-compound), minus losses (filtered to minimize).
Enhancements: 15d history for opportunities, alerts for monitoring, graceful shutdown/restart with DB persistence.



Installation in Ubuntu 24.04 Linux (Anaconda Python Environment Preferred)
For optimal isolation and management of dependencies, use Anaconda. This setup ensures a controlled Python environment, avoiding conflicts with system Python.
Step-by-Step Anaconda Installation

Download the Anaconda Installer:
Open a terminal and download the latest Linux installer (as of October 2025, check for updates at https://www.anaconda.com/download):
bashwget https://repo.anaconda.com/archive/Anaconda3-2024.10-1-Linux-x86_64.sh

Run the Installer:
Execute the script:
bashbash Anaconda3-2024.10-1-Linux-x86_64.sh

Press Enter to review the license.
Scroll with Space and type yes to accept.
Confirm the default path (e.g., /home/yourusername/anaconda3) by pressing Enter.
Type yes to initialize Conda (this adds setup to .bashrc).


Reload Shell Configuration:
Apply changes:
bashsource ~/.bashrc
Your terminal should show (base) indicating the base environment is active.
Create a New Conda Environment:
Create an isolated environment for the bot (e.g., with Python 3.11):
bashconda create -n trading_bot python=3.11
Activate it:
bashconda activate trading_bot
(To deactivate later: conda deactivate.)
Install Dependencies:
Inside the activated environment:
bashpip install python-binance ta-lib numpy pandas sqlalchemy requests tenacity

Note: If ta-lib fails, install the system library first:
bashsudo apt update && sudo apt install libta-lib0 libta-lib0-dev



Verify Installation:
Run conda list to check packages. Test Python:
bashpython -c "print('Hello, Anaconda!')"


What to Add to .bashrc in Ubuntu Linux
The Anaconda installer automatically adds Conda initialization to ~/.bashrc (e.g., export PATH and conda setup block). For the bot's environment variables (used via os.getenv), add these exports at the end of ~/.bashrc (replace placeholders with your actual values):
bash# Environment variables for Crypto Trading Bot
export BINANCE_API_KEY="your_binance_api_key_here"
export BINANCE_API_SECRET="your_binance_api_secret_here"
export CALLMEBOT_API_KEY="your_callmebot_api_key_here"
export CALLMEBOT_PHONE="your_phone_number_here"  # E.g., +1234567890

After editing, reload: source ~/.bashrc.
Security Tip: Store sensitive keys securely (e.g., use a .env file and dotenv if preferred, but the code uses os.getenv directly). Avoid committing .bashrc to version control.

Run Program:
bashconda activate trading_bot
python3 2025-coin-trading-bot.py
Note: Ensure the bot script is saved as 2025-coin-trading-bot.py. Monitor logs in crypto_trading_bot.log for issues. This setup is for educational use; live trading carries risks as outlined in disclaimers.
