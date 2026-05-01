"""
bot.py — Telegram interface for Revolt Perps Bot

Commands:
    /perps     — Dashboard with current status
    /start     — Start a new grid cycle
    /stop      — Close active cycle
    /pause     — Pause the bot
    /resume    — Resume the bot
    /stats     — Detailed performance stats
    /config    — Show current configuration

Trade notifications post to the Perps topic in HQ.
"""

import asyncio
import logging

from aiogram import Bot, Dispatcher, Router
from aiogram.filters import Command
from aiogram.types import Message
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from config import BOT_TOKEN, GROUP_ID, TOPIC_THREAD_ID, LEVERAGE, MAX_GRID_LEVELS
from engine import MartingaleGridEngine
from price_feed import get_btc_price, get_atr, get_ema

logger = logging.getLogger(__name__)
router = Router()

# Global engine reference — set in main.py
engine: MartingaleGridEngine | None = None
bot_instance: Bot | None = None


def set_engine(e: MartingaleGridEngine):
    global engine
    engine = e


def set_bot(b: Bot):
    global bot_instance
    bot_instance = b


def _format_usd(value: float) -> str:
    if abs(value) >= 1_000_000:
        return f"${value / 1_000_000:.2f}M"
    if abs(value) >= 1_000:
        return f"${value / 1_000:.1f}K"
    return f"${value:.2f}"


# ── /perps — Main Dashboard ─────────────────────────────────────────────────

@router.message(Command("perps"))
async def cmd_perps(message: Message):
    if not engine:
        await message.reply("Bot not initialized yet.")
        return

    price = await get_btc_price()
    status = engine.get_status()

    pnl = status["pnl"]
    pnl_sign = "+" if pnl >= 0 else ""
    pnl_emoji = "🟢" if pnl >= 0 else "🔴"

    lines = [
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        "🔮 REVOLT PERPS",
        f"Strategy: Martingale Grid | {LEVERAGE}x",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        "",
        f"📊 BTC: {_format_usd(price)}",
        f"💰 Balance: {_format_usd(status['balance'])} / {_format_usd(status['starting_balance'])}",
        f"{pnl_emoji} PnL: {pnl_sign}{_format_usd(pnl)} ({pnl_sign}{status['pnl_pct']}%)",
        f"📈 Peak: {_format_usd(status['peak_balance'])}",
        "",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        "📋 PERFORMANCE",
        f"Cycles: {status['total_cycles']} | W/L: {status['wins']}/{status['losses']}",
        f"Win Rate: {status['win_rate']}%",
        f"Total PnL: {_format_usd(status['total_pnl'])}",
        f"Grid Spacing: {_format_usd(status['grid_spacing'])}",
        "",
    ]

    # Active cycle info
    cycle = status.get("active_cycle")
    if cycle:
        lines += [
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
            f"⚡ ACTIVE CYCLE #{cycle['id']}",
            f"Direction: {cycle['direction'].upper()}",
            f"Levels: {cycle['levels']}/{cycle['max_levels']}",
            f"Avg Entry: {_format_usd(cycle['avg_entry'])}",
            f"Size: {_format_usd(cycle['total_size'])}",
            f"Remaining: {cycle['remaining_pct']}%",
            f"Realized: {_format_usd(cycle['realized_pnl'])}",
            f"Age: {cycle['age_minutes']:.0f} min",
        ]
        if cycle["sl_price"]:
            lines.append(f"SL: {_format_usd(cycle['sl_price'])}")
        if cycle["main_tp_done"]:
            lines.append("✅ Main TP hit — free trade mode")
    else:
        lines += [
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
            "💤 No active cycle",
        ]

    if status["paused"]:
        lines += ["", f"⚠️ PAUSED: {status['pause_reason']}"]

    lines += ["", "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"]

    await message.reply("\n".join(lines))


# ── /start — Start new cycle ─────────────────────────────────────────────────

@router.message(Command("start"))
async def cmd_start(message: Message):
    if not engine:
        await message.reply("Bot not initialized yet.")
        return

    if engine.active_cycle:
        await message.reply("Already have an active cycle. Use /stop to close it first.")
        return

    if engine.paused:
        engine.paused = False
        engine.pause_reason = ""

    await engine.open_new_cycle()

    if engine.active_cycle:
        c = engine.active_cycle
        await message.reply(
            f"⚡ Cycle #{c.cycle_id} opened\n"
            f"Direction: {c.direction.upper()}\n"
            f"Entry: {_format_usd(c.avg_entry_price)}\n"
            f"Base size: {_format_usd(c.total_size_usd)}\n"
            f"Grid spacing: {_format_usd(engine.grid_spacing)}"
        )
    else:
        await message.reply("Could not open cycle — check safety filters.")


# ── /stop — Close active cycle ───────────────────────────────────────────────

@router.message(Command("stop"))
async def cmd_stop(message: Message):
    if not engine or not engine.active_cycle:
        await message.reply("No active cycle to close.")
        return

    price = await get_btc_price()
    cycle = engine.active_cycle

    if cycle.direction == "long":
        pnl_pct = (price - cycle.avg_entry_price) / cycle.avg_entry_price * 100 * LEVERAGE
    else:
        pnl_pct = (cycle.avg_entry_price - price) / cycle.avg_entry_price * 100 * LEVERAGE

    await engine._close_cycle("manual", price, pnl_pct)

    await message.reply(
        f"🛑 Cycle closed manually\n"
        f"PnL: {_format_usd(engine.completed_cycles[-1].close_pnl)}"
    )


# ── /pause & /resume ─────────────────────────────────────────────────────────

@router.message(Command("pause"))
async def cmd_pause(message: Message):
    if not engine:
        return
    engine.paused = True
    engine.pause_reason = "Manual pause"
    await message.reply("⏸ Bot paused. Use /resume to continue.")


@router.message(Command("resume"))
async def cmd_resume(message: Message):
    if not engine:
        return
    engine.paused = False
    engine.pause_reason = ""
    await message.reply("▶️ Bot resumed.")


# ── /stats — Detailed stats ──────────────────────────────────────────────────

@router.message(Command("stats"))
async def cmd_stats(message: Message):
    if not engine:
        await message.reply("Bot not initialized yet.")
        return

    status = engine.get_status()
    cycles = engine.completed_cycles

    if not cycles:
        await message.reply("No completed cycles yet.")
        return

    # Calculate stats
    profits = [c.close_pnl for c in cycles if c.close_pnl >= 0]
    losses = [c.close_pnl for c in cycles if c.close_pnl < 0]
    avg_profit = sum(profits) / len(profits) if profits else 0
    avg_loss = sum(losses) / len(losses) if losses else 0
    best = max(c.close_pnl for c in cycles)
    worst = min(c.close_pnl for c in cycles)
    avg_levels = sum(len(c.levels) for c in cycles) / len(cycles)

    lines = [
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        "📊 REVOLT PERPS — STATS",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        "",
        f"Total Cycles: {len(cycles)}",
        f"Wins: {status['wins']} | Losses: {status['losses']}",
        f"Win Rate: {status['win_rate']}%",
        "",
        f"Avg Win: {_format_usd(avg_profit)}",
        f"Avg Loss: {_format_usd(avg_loss)}",
        f"Best Cycle: {_format_usd(best)}",
        f"Worst Cycle: {_format_usd(worst)}",
        f"Avg Grid Depth: {avg_levels:.1f} levels",
        "",
        f"Total PnL: {_format_usd(status['total_pnl'])}",
        f"Balance: {_format_usd(status['balance'])}",
        f"Peak: {_format_usd(status['peak_balance'])}",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
    ]

    await message.reply("\n".join(lines))


# ── /config — Show config ────────────────────────────────────────────────────

@router.message(Command("config"))
async def cmd_config(message: Message):
    from config import (
        LEVERAGE, MAX_GRID_LEVELS, BASE_ORDER_PCT,
        GRID_SPACING_MODE, ATR_MULTIPLIER, GRID_SPACING_FIXED,
        TP_TRIGGER_PCT, TP_CLOSE_PCT, TP_REMAINING_SL_PCT,
        MAX_ACCOUNT_RISK_PCT, MAX_DAILY_DRAWDOWN_PCT, EMERGENCY_SL_PCT,
        TREND_FILTER_ENABLED, FUNDING_FILTER_ENABLED,
    )

    lines = [
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        "⚙️ CONFIGURATION",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        "",
        f"Leverage: {LEVERAGE}x",
        f"Max Grid Levels: {MAX_GRID_LEVELS}",
        f"Base Order: {BASE_ORDER_PCT}% of account",
        f"Grid Spacing: {GRID_SPACING_MODE} ({ATR_MULTIPLIER}x ATR)" if GRID_SPACING_MODE == "atr" else f"Grid Spacing: fixed ${GRID_SPACING_FIXED}",
        "",
        f"TP Trigger: +{TP_TRIGGER_PCT}% → close {TP_CLOSE_PCT}%",
        f"Remaining SL: -{TP_REMAINING_SL_PCT}%",
        "",
        f"Max Account Risk: {MAX_ACCOUNT_RISK_PCT}%",
        f"Daily Drawdown Pause: {MAX_DAILY_DRAWDOWN_PCT}%",
        f"Emergency SL: {EMERGENCY_SL_PCT}%",
        f"Trend Filter: {'ON' if TREND_FILTER_ENABLED else 'OFF'}",
        f"Funding Filter: {'ON' if FUNDING_FILTER_ENABLED else 'OFF'}",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
    ]

    await message.reply("\n".join(lines))


# ── Notification helper ──────────────────────────────────────────────────────

async def notify(text: str):
    """Send a notification to the Perps topic in HQ."""
    if bot_instance:
        try:
            await bot_instance.send_message(
                GROUP_ID, text,
                message_thread_id=TOPIC_THREAD_ID,
            )
        except Exception as exc:
            logger.warning("Notify failed: %s", exc)
