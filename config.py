"""
config.py — Revolt Perps Bot Configuration

Directional Martingale Grid with safety layers.
All parameters tunable without code changes.
"""

# ── Exchange / Data ──────────────────────────────────────────────────────────
PRICE_SOURCE = "coingecko"  # "coingecko" or "binance"
SYMBOL = "BTC"
POLL_INTERVAL = 5  # seconds between price checks

# ── Account ──────────────────────────────────────────────────────────────────
STARTING_BALANCE = 10_000  # USDT (paper)
LEVERAGE = 10              # higher leverage, tighter range

# ── Grid Strategy ────────────────────────────────────────────────────────────
DIRECTION = "auto"         # "long", "short", or "auto" (uses trend filter)
BASE_ORDER_PCT = 1.0       # % of account for first order
GRID_SPACING_MODE = "atr"  # "fixed" or "atr"
GRID_SPACING_FIXED = 100   # USD spacing if mode="fixed"
ATR_MULTIPLIER = 1.0       # tighter spacing for higher leverage
DOUBLING_FACTOR = 2.0      # each level doubles (classic Martingale)
MAX_GRID_LEVELS = 11       # SL at 11th level (Authenticity's spec)

# ── Take Profit / Stop Loss ─────────────────────────────────────────────────
# Authenticity's mechanic: +100% → take 90% off, recast with remaining 10%
TP_TRIGGER_PCT = 100       # +100% profit on total position → trigger
TP_CLOSE_PCT = 90          # take 90% off the table
RECAST_ENABLED = True      # after TP, use remaining 10% as new base order
TP_REMAINING_SL_PCT = 40   # SL on remainder if not recasting

# Enhanced partial TPs (disabled — Authenticity's version takes 90% at once)
PARTIAL_TP_1_PCT = 0
PARTIAL_TP_1_TRIGGER = 50
PARTIAL_TP_2_PCT = 0
PARTIAL_TP_2_TRIGGER = 150

# ── Safety Layers ────────────────────────────────────────────────────────────
MAX_ACCOUNT_RISK_PCT = 12  # max % of account in one grid (safety layer 2)
MAX_DAILY_DRAWDOWN_PCT = 10  # auto-pause if down this % in 24h (safety layer 7)
EMERGENCY_SL_PCT = 15      # full close if account drops this % (safety layer 7b)

# ── Trend Filter (safety layer 5) ────────────────────────────────────────────
TREND_FILTER_ENABLED = True
TREND_EMA_PERIOD = 200     # EMA period on 4H candles
TREND_TIMEFRAME = "4h"     # "1h", "4h", "1d"

# ── Funding Rate Filter (safety layer 8) ─────────────────────────────────────
FUNDING_FILTER_ENABLED = True
FUNDING_RATE_MAX = 0.05    # skip if funding rate against position > this %

# ── Telegram ─────────────────────────────────────────────────────────────────
import os
BOT_TOKEN = os.getenv("PERPS_BOT_TOKEN", "8696824600:AAGATYta5OYnF0pftjDrFFkrR3IR3FZ9yw4")
GROUP_ID = int(os.getenv("PERPS_GROUP_ID", "-1003852140576"))
TOPIC_THREAD_ID = int(os.getenv("PERPS_TOPIC_ID", "3713"))

# ── Logging ──────────────────────────────────────────────────────────────────
LOG_LEVEL = "INFO"
LOG_FILE = "revolt_perps.log"
