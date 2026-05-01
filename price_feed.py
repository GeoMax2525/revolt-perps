"""
price_feed.py — Real-time BTC price + indicators from public APIs.

No API key needed. Uses CoinGecko for current price and
Binance public API for historical candles (ATR, EMA).
"""

import asyncio
import logging
import time
from collections import deque

import aiohttp

logger = logging.getLogger(__name__)

# ── Price sources ────────────────────────────────────────────────────────────
COINGECKO_URL = "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd"
BINANCE_KLINE_URL = "https://api.binance.com/api/v3/klines"

# Cache
_last_price: float = 0.0
_last_fetch: float = 0.0
_price_history: deque = deque(maxlen=500)


async def get_btc_price() -> float:
    """Fetch current BTC/USD price from CoinGecko."""
    global _last_price, _last_fetch

    # Rate limit: don't fetch more than once per 3 seconds
    if time.time() - _last_fetch < 3 and _last_price > 0:
        return _last_price

    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=10)
        ) as session:
            async with session.get(COINGECKO_URL) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    price = float(data.get("bitcoin", {}).get("usd", 0))
                    if price > 0:
                        _last_price = price
                        _last_fetch = time.time()
                        _price_history.append({"time": time.time(), "price": price})
                        return price
    except Exception as exc:
        logger.warning("Price fetch failed: %s", exc)

    # Fallback to Binance
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=10)
        ) as session:
            params = {"symbol": "BTCUSDT", "interval": "1m", "limit": 1}
            async with session.get(BINANCE_KLINE_URL, params=params) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    if data:
                        price = float(data[0][4])  # close price
                        _last_price = price
                        _last_fetch = time.time()
                        _price_history.append({"time": time.time(), "price": price})
                        return price
    except Exception as exc:
        logger.warning("Binance fallback failed: %s", exc)

    return _last_price


async def get_candles(interval: str = "4h", limit: int = 250) -> list[dict]:
    """Fetch historical candles from Binance public API.
    Returns list of {time, open, high, low, close, volume}."""
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15)
        ) as session:
            params = {"symbol": "BTCUSDT", "interval": interval, "limit": limit}
            async with session.get(BINANCE_KLINE_URL, params=params) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json(content_type=None)
                candles = []
                for c in data:
                    candles.append({
                        "time": int(c[0]),
                        "open": float(c[1]),
                        "high": float(c[2]),
                        "low": float(c[3]),
                        "close": float(c[4]),
                        "volume": float(c[5]),
                    })
                return candles
    except Exception as exc:
        logger.warning("Candle fetch failed: %s", exc)
        return []


async def get_atr(period: int = 14, interval: str = "4h") -> float:
    """Calculate ATR(14) from historical candles."""
    candles = await get_candles(interval=interval, limit=period + 10)
    if len(candles) < period + 1:
        return 100.0  # fallback fixed spacing

    trs = []
    for i in range(1, len(candles)):
        high = candles[i]["high"]
        low = candles[i]["low"]
        prev_close = candles[i - 1]["close"]
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)

    atr = sum(trs[-period:]) / period
    return round(atr, 2)


async def get_ema(period: int = 200, interval: str = "4h") -> float:
    """Calculate EMA from historical candles."""
    candles = await get_candles(interval=interval, limit=period + 50)
    if len(candles) < period:
        return 0.0

    closes = [c["close"] for c in candles]
    multiplier = 2 / (period + 1)
    ema = sum(closes[:period]) / period
    for price in closes[period:]:
        ema = price * multiplier + ema * (1 - multiplier)
    return round(ema, 2)


async def get_funding_rate() -> float:
    """Get current BTC/USDT funding rate from Binance."""
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=10)
        ) as session:
            url = "https://fapi.binance.com/fapi/v1/fundingRate"
            params = {"symbol": "BTCUSDT", "limit": 1}
            async with session.get(url, params=params) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    if data:
                        return float(data[0].get("fundingRate", 0))
    except Exception as exc:
        logger.debug("Funding rate fetch failed: %s", exc)
    return 0.0


def get_price_history() -> list[dict]:
    """Return recent price history."""
    return list(_price_history)
