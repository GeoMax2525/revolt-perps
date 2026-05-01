"""
main.py — Revolt Perps Bot

Directional Martingale Grid with 8 safety layers.
Paper trading with real BTC price data.

Usage:
    python main.py
"""

import asyncio
import logging
import json
from datetime import datetime

from config import POLL_INTERVAL, LOG_LEVEL, LOG_FILE
from price_feed import get_btc_price, get_atr, get_ema
from engine import MartingaleGridEngine

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)


async def main():
    logger.info("=" * 60)
    logger.info("REVOLT PERPS BOT — Starting")
    logger.info("=" * 60)

    engine = MartingaleGridEngine()
    await engine.initialize()

    # Show initial state
    price = await get_btc_price()
    ema = await get_ema()
    atr = await get_atr()
    logger.info(
        "BTC: $%.2f | EMA200: $%.2f | ATR: $%.2f | Grid spacing: $%.2f",
        price, ema, atr, engine.grid_spacing,
    )
    logger.info(
        "Balance: $%.2f | Leverage: %dx | Max levels: %d | Direction: %s",
        engine.balance, engine.balance, engine.active_cycle,
        "auto" if not engine.active_cycle else engine.active_cycle.direction,
    )

    tick_count = 0
    last_status_print = 0

    while True:
        try:
            price = await get_btc_price()
            if price <= 0:
                await asyncio.sleep(POLL_INTERVAL)
                continue

            tick_count += 1

            # Open new cycle if none active
            if not engine.active_cycle and not engine.paused:
                await engine.open_new_cycle()

            # Process price update
            if engine.active_cycle:
                await engine.on_price_update(price)

            # Print status every 60 seconds
            if tick_count % (60 // POLL_INTERVAL) == 0:
                status = engine.get_status()
                cycle_info = ""
                if status.get("active_cycle"):
                    c = status["active_cycle"]
                    cycle_info = (
                        f" | Cycle #{c['id']} {c['direction'].upper()} "
                        f"levels={c['levels']}/{c['max_levels']} "
                        f"avg=${c['avg_entry']:.0f} "
                        f"remaining={c['remaining_pct']:.0f}%"
                    )

                logger.info(
                    "STATUS: BTC=$%.0f | Bal=$%.2f (%+.2f) | "
                    "W/L=%d/%d (%.0f%% WR)%s",
                    price, status["balance"], status["pnl"],
                    status["wins"], status["losses"], status["win_rate"],
                    cycle_info,
                )

            # Save state periodically (every 5 min)
            if tick_count % (300 // POLL_INTERVAL) == 0:
                _save_state(engine)

        except KeyboardInterrupt:
            logger.info("Shutting down...")
            _save_state(engine)
            break
        except Exception as exc:
            logger.error("Main loop error: %s", exc)

        await asyncio.sleep(POLL_INTERVAL)


def _save_state(engine: MartingaleGridEngine):
    """Save engine state to JSON for persistence."""
    status = engine.get_status()
    status["saved_at"] = datetime.utcnow().isoformat()
    status["completed_cycles"] = len(engine.completed_cycles)

    try:
        with open("state.json", "w") as f:
            json.dump(status, f, indent=2)
        logger.info("State saved to state.json")
    except Exception as exc:
        logger.warning("Failed to save state: %s", exc)


if __name__ == "__main__":
    asyncio.run(main())
