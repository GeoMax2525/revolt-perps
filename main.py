"""
main.py — Revolt Perps Bot

Runs the Telegram bot + Martingale Grid engine concurrently.

Usage:
    python main.py
"""

import asyncio
import json
import logging
from datetime import datetime

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from config import (
    BOT_TOKEN, POLL_INTERVAL, LOG_LEVEL, LOG_FILE, LEVERAGE,
    MAX_GRID_LEVELS, GROUP_ID, TOPIC_THREAD_ID,
)
from price_feed import get_btc_price, get_atr, get_ema
from engine import MartingaleGridEngine
from bot import router, set_engine, set_bot, notify

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


async def trading_loop(engine: MartingaleGridEngine):
    """Background loop: fetch prices and run the grid engine."""
    await asyncio.sleep(5)  # let Telegram bot start first
    logger.info("Trading loop started — polling every %ds", POLL_INTERVAL)

    tick_count = 0
    last_level_count = 0

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
                if engine.active_cycle:
                    c = engine.active_cycle
                    await notify(
                        f"⚡ CYCLE #{c.cycle_id} OPENED\n"
                        f"Direction: {c.direction.upper()}\n"
                        f"Entry: ${c.avg_entry_price:,.0f}\n"
                        f"Size: ${c.total_size_usd:.2f}\n"
                        f"Grid: ${engine.grid_spacing:,.0f} spacing\n"
                        f"Leverage: {LEVERAGE}x"
                    )
                    last_level_count = 1

            # Process price update
            if engine.active_cycle:
                prev_remaining = engine.active_cycle.remaining_pct
                prev_levels = len(engine.active_cycle.levels)
                prev_tp = engine.active_cycle.main_tp_done

                await engine.on_price_update(price)

                # Notify on grid level add
                if engine.active_cycle and len(engine.active_cycle.levels) > prev_levels:
                    c = engine.active_cycle
                    await notify(
                        f"📊 GRID LEVEL {len(c.levels)}/{MAX_GRID_LEVELS}\n"
                        f"{c.direction.upper()} | Avg: ${c.avg_entry_price:,.0f}\n"
                        f"Size: ${c.total_size_usd:.2f}"
                    )

                # Notify on partial TP
                if engine.active_cycle and engine.active_cycle.remaining_pct < prev_remaining:
                    c = engine.active_cycle
                    closed_pct = prev_remaining - c.remaining_pct
                    await notify(
                        f"💰 PARTIAL TP — closed {closed_pct:.0f}%\n"
                        f"Realized: ${c.realized_pnl:.2f}\n"
                        f"Remaining: {c.remaining_pct:.0f}%"
                    )

                # Notify on main TP (free trade mode)
                if engine.active_cycle and engine.active_cycle.main_tp_done and not prev_tp:
                    await notify(
                        f"🎯 MAIN TP HIT — FREE TRADE MODE\n"
                        f"50% closed at +100% profit\n"
                        f"SL moved to ${engine.active_cycle.sl_price:,.0f}\n"
                        f"Remaining rides risk-free!"
                    )

                # Notify on cycle close
                if not engine.active_cycle and engine.completed_cycles:
                    last = engine.completed_cycles[-1]
                    emoji = "✅" if last.close_pnl >= 0 else "❌"
                    await notify(
                        f"{emoji} CYCLE #{last.cycle_id} CLOSED\n"
                        f"Reason: {last.close_reason}\n"
                        f"PnL: ${last.close_pnl:+.2f}\n"
                        f"Levels used: {len(last.levels)}/{MAX_GRID_LEVELS}\n"
                        f"Balance: ${engine.balance:,.2f}"
                    )

            # Status log every 60 seconds
            if tick_count % (60 // POLL_INTERVAL) == 0:
                status = engine.get_status()
                cycle_info = ""
                if status.get("active_cycle"):
                    c = status["active_cycle"]
                    cycle_info = (
                        f" | Cycle #{c['id']} {c['direction'].upper()} "
                        f"L={c['levels']}/{c['max_levels']} "
                        f"avg=${c['avg_entry']:.0f} "
                        f"rem={c['remaining_pct']:.0f}%"
                    )

                logger.info(
                    "BTC=$%.0f | Bal=$%.2f (%+.2f) | W/L=%d/%d (%.0f%%)%s",
                    price, status["balance"], status["pnl"],
                    status["wins"], status["losses"], status["win_rate"],
                    cycle_info,
                )

            # Save state every 5 min
            if tick_count % (300 // POLL_INTERVAL) == 0:
                _save_state(engine)

        except Exception as exc:
            logger.error("Trading loop error: %s", exc)

        await asyncio.sleep(POLL_INTERVAL)


def _save_state(engine: MartingaleGridEngine):
    """Save engine state to JSON."""
    status = engine.get_status()
    status["saved_at"] = datetime.utcnow().isoformat()
    try:
        with open("state.json", "w") as f:
            json.dump(status, f, indent=2)
    except Exception as exc:
        logger.warning("Save state failed: %s", exc)


async def main():
    logger.info("=" * 50)
    logger.info("REVOLT PERPS BOT — Starting")
    logger.info("=" * 50)

    # Initialize engine
    engine = MartingaleGridEngine()
    await engine.initialize()
    set_engine(engine)

    # Show initial state
    price = await get_btc_price()
    ema = await get_ema()
    atr = await get_atr()
    logger.info("BTC: $%.0f | EMA200: $%.0f | ATR: $%.0f | Spacing: $%.0f",
                price, ema, atr, engine.grid_spacing)
    logger.info("Balance: $%.2f | Leverage: %dx | Max levels: %d",
                engine.balance, LEVERAGE, MAX_GRID_LEVELS)

    # Setup Telegram bot
    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    set_bot(bot)

    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    # Start trading loop in background
    asyncio.create_task(trading_loop(engine))

    # Notify startup
    await notify(
        f"🔮 REVOLT PERPS BOT ONLINE\n"
        f"BTC: ${price:,.0f}\n"
        f"Balance: ${engine.balance:,.2f}\n"
        f"Leverage: {LEVERAGE}x | Grid: ${engine.grid_spacing:,.0f}\n"
        f"Max levels: {MAX_GRID_LEVELS}"
    )

    # Start polling
    logger.info("Telegram bot starting...")
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    finally:
        await bot.session.close()
        _save_state(engine)
        logger.info("Bot stopped.")


if __name__ == "__main__":
    asyncio.run(main())
