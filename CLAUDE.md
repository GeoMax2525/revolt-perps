# Revolt Perps — Directional Martingale Grid Bot

## Strategy
Directional Martingale Grid with aggressive profit de-risking.
- Grid levels double in size (Martingale)
- +100% profit → close 50%, move SL to -40% on rest ("free trade")
- Enhanced partial TPs at +50% and +150%
- 8 safety layers prevent blow-up

## Safety Layers
1. Max 6 grid levels (hard cap)
2. Max 12% account risk per grid
3. Leverage capped at 3x (max 5x)
4. ATR-based dynamic grid spacing
5. Trend filter (EMA-200 on 4H)
6. Enhanced partial TPs + trailing SL
7. Auto-pause on >10% daily drawdown + emergency SL at -15%
8. Funding rate filter

## Files
- main.py — entry point, main loop
- engine.py — core Martingale Grid logic
- price_feed.py — real-time BTC price + indicators
- config.py — all parameters

## Running
```
pip install -r requirements.txt
python main.py
```

## Paper Trading
Uses real BTC prices from CoinGecko + Binance public APIs.
All trades simulated locally — no exchange account needed.
