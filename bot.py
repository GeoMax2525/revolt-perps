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
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
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


def _perps_keyboard(has_cycle: bool, paused: bool):
    """Build inline keyboard for the dashboard."""
    builder = InlineKeyboardBuilder()

    if has_cycle:
        builder.row(
            InlineKeyboardButton(text="🛑 Close Cycle", callback_data="perps:stop"),
        )
    else:
        builder.row(
            InlineKeyboardButton(text="⚡ Start Cycle", callback_data="perps:start"),
        )

    if paused:
        builder.row(
            InlineKeyboardButton(text="▶️ Resume", callback_data="perps:resume"),
        )
    else:
        builder.row(
            InlineKeyboardButton(text="⏸ Pause", callback_data="perps:pause"),
        )

    builder.row(
        InlineKeyboardButton(text="🔄 Refresh", callback_data="perps:refresh"),
        InlineKeyboardButton(text="📊 Stats", callback_data="perps:stats"),
        InlineKeyboardButton(text="⚙️ Config", callback_data="perps:config"),
    )

    return builder.as_markup()


async def _build_dashboard() -> str:
    """Build the dashboard text."""
    price = await get_btc_price()
    status = engine.get_status()

    pnl = status["pnl"]
    pnl_sign = "+" if pnl >= 0 else ""
    pnl_emoji = "🟢" if pnl >= 0 else "🔴"

    lines = [
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        "🔮 <b>REVOLT PERPS</b>",
        f"Martingale Grid | {LEVERAGE}x Leverage",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        "",
        f"📊 BTC: <b>{_format_usd(price)}</b>",
        f"💰 Balance: <b>{_format_usd(status['balance'])}</b> / {_format_usd(status['starting_balance'])}",
        f"{pnl_emoji} PnL: <b>{pnl_sign}{_format_usd(pnl)}</b> ({pnl_sign}{status['pnl_pct']}%)",
        f"📈 Peak: {_format_usd(status['peak_balance'])}",
        "",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        "<b>PERFORMANCE</b>",
        f"Cycles: {status['total_cycles']}  |  W: {status['wins']}  L: {status['losses']}  |  {status['win_rate']}% WR",
        f"Total PnL: {_format_usd(status['total_pnl'])}  |  Grid: {_format_usd(status['grid_spacing'])}",
    ]

    cycle = status.get("active_cycle")
    if cycle:
        # Calculate current unrealized PnL
        entry = cycle["avg_entry"]
        if entry > 0:
            if cycle.get("direction") == "long":
                curr_pnl_pct = (price - entry) / entry * 100 * LEVERAGE
            else:
                curr_pnl_pct = (entry - price) / entry * 100 * LEVERAGE
        else:
            curr_pnl_pct = 0

        pnl_bar = "🟢" if curr_pnl_pct >= 0 else "🔴"
        curr_pnl_usd = cycle["total_size"] * (cycle["remaining_pct"] / 100) * curr_pnl_pct / 100

        lines += [
            "",
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
            f"⚡ <b>ACTIVE CYCLE #{cycle['id']}</b>  |  {cycle['direction'].upper()}",
            f"",
            f"Entry: {_format_usd(cycle['avg_entry'])}  →  Now: {_format_usd(price)}",
            f"{pnl_bar} Unrealized: <b>{curr_pnl_pct:+.1f}%</b>  |  <b>{_format_usd(curr_pnl_usd)}</b>",
            f"Levels: {cycle['levels']}/{cycle['max_levels']}  |  Size: {_format_usd(cycle['total_size'])}",
            f"Remaining: {cycle['remaining_pct']:.0f}%  |  Realized: {_format_usd(cycle['realized_pnl'])}",
            f"Age: {cycle['age_minutes']:.0f} min",
        ]
        if cycle.get("sl_price"):
            lines.append(f"SL: {_format_usd(cycle['sl_price'])}")
        if cycle.get("main_tp_done"):
            lines.append("✅ <b>FREE TRADE MODE</b> — main TP hit, riding risk-free")
    else:
        lines += [
            "",
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
            "💤 No active cycle — tap <b>Start Cycle</b> to begin",
        ]

    if status["paused"]:
        lines += ["", f"⚠️ <b>PAUSED:</b> {status['pause_reason']}"]

    lines += ["", "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"]

    return "\n".join(lines)


# ── /perps — Main Dashboard ─────────────────────────────────────────────────

@router.message(Command("perps"))
async def cmd_perps(message: Message):
    if not engine:
        await message.reply("Bot not initialized yet.")
        return

    text = await _build_dashboard()
    status = engine.get_status()
    keyboard = _perps_keyboard(
        has_cycle=status.get("active_cycle") is not None,
        paused=status["paused"],
    )

    await message.reply(text, reply_markup=keyboard, parse_mode="HTML")


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

# ── Callback handlers for buttons ────────────────────────────────────────────

@router.callback_query(lambda c: c.data and c.data.startswith("perps:"))
async def cb_perps(callback: CallbackQuery):
    if not engine:
        await callback.answer("Bot not initialized.")
        return

    action = callback.data.split(":", 1)[1]

    if action == "refresh":
        await callback.answer("Refreshing...")
        text = await _build_dashboard()
        status = engine.get_status()
        keyboard = _perps_keyboard(
            has_cycle=status.get("active_cycle") is not None,
            paused=status["paused"],
        )
        try:
            await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
        except Exception:
            pass

    elif action == "start":
        if engine.active_cycle:
            await callback.answer("Already have an active cycle.", show_alert=True)
            return
        if engine.paused:
            engine.paused = False
            engine.pause_reason = ""
        await engine.open_new_cycle()
        await callback.answer("Cycle started!" if engine.active_cycle else "Could not start — check filters.")
        text = await _build_dashboard()
        status = engine.get_status()
        keyboard = _perps_keyboard(
            has_cycle=status.get("active_cycle") is not None,
            paused=status["paused"],
        )
        try:
            await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
        except Exception:
            pass

    elif action == "stop":
        if not engine.active_cycle:
            await callback.answer("No active cycle.", show_alert=True)
            return
        price = await get_btc_price()
        cycle = engine.active_cycle
        if cycle.direction == "long":
            pnl_pct = (price - cycle.avg_entry_price) / cycle.avg_entry_price * 100 * LEVERAGE
        else:
            pnl_pct = (cycle.avg_entry_price - price) / cycle.avg_entry_price * 100 * LEVERAGE
        await engine._close_cycle("manual", price, pnl_pct)
        await callback.answer("Cycle closed.")
        text = await _build_dashboard()
        status = engine.get_status()
        keyboard = _perps_keyboard(has_cycle=False, paused=status["paused"])
        try:
            await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
        except Exception:
            pass

    elif action == "pause":
        engine.paused = True
        engine.pause_reason = "Manual pause"
        await callback.answer("Bot paused.")
        text = await _build_dashboard()
        status = engine.get_status()
        keyboard = _perps_keyboard(
            has_cycle=status.get("active_cycle") is not None,
            paused=True,
        )
        try:
            await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
        except Exception:
            pass

    elif action == "resume":
        engine.paused = False
        engine.pause_reason = ""
        await callback.answer("Bot resumed.")
        text = await _build_dashboard()
        status = engine.get_status()
        keyboard = _perps_keyboard(
            has_cycle=status.get("active_cycle") is not None,
            paused=False,
        )
        try:
            await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
        except Exception:
            pass

    elif action == "stats":
        await callback.answer()
        if not engine.completed_cycles:
            await callback.message.reply("No completed cycles yet.")
            return
        cycles = engine.completed_cycles
        profits = [c.close_pnl for c in cycles if c.close_pnl >= 0]
        losses = [c.close_pnl for c in cycles if c.close_pnl < 0]
        avg_profit = sum(profits) / len(profits) if profits else 0
        avg_loss = sum(losses) / len(losses) if losses else 0
        best = max(c.close_pnl for c in cycles)
        worst = min(c.close_pnl for c in cycles)
        avg_levels = sum(len(c.levels) for c in cycles) / len(cycles)
        status = engine.get_status()

        text = "\n".join([
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
            "📊 <b>DETAILED STATS</b>",
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
            "",
            f"Total Cycles: {len(cycles)}",
            f"Wins: {status['wins']}  |  Losses: {status['losses']}  |  WR: {status['win_rate']}%",
            "",
            f"Avg Win: {_format_usd(avg_profit)}",
            f"Avg Loss: {_format_usd(avg_loss)}",
            f"Best: {_format_usd(best)}",
            f"Worst: {_format_usd(worst)}",
            f"Avg Grid Depth: {avg_levels:.1f} levels",
            "",
            f"Total PnL: <b>{_format_usd(status['total_pnl'])}</b>",
            f"Balance: <b>{_format_usd(status['balance'])}</b>",
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        ])
        await callback.message.reply(text, parse_mode="HTML")

    elif action == "config":
        await callback.answer()
        from config import (
            GRID_SPACING_MODE, ATR_MULTIPLIER, GRID_SPACING_FIXED,
            TP_TRIGGER_PCT, TP_CLOSE_PCT, TP_REMAINING_SL_PCT,
            MAX_ACCOUNT_RISK_PCT, MAX_DAILY_DRAWDOWN_PCT, EMERGENCY_SL_PCT,
            TREND_FILTER_ENABLED, FUNDING_FILTER_ENABLED, BASE_ORDER_PCT,
        )
        text = "\n".join([
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
            "⚙️ <b>CONFIGURATION</b>",
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
            "",
            f"Leverage: {LEVERAGE}x",
            f"Max Grid Levels: {MAX_GRID_LEVELS}",
            f"Base Order: {BASE_ORDER_PCT}% of account",
            f"Grid: {GRID_SPACING_MODE.upper()} ({ATR_MULTIPLIER}x ATR)" if GRID_SPACING_MODE == "atr" else f"Grid: Fixed ${GRID_SPACING_FIXED}",
            "",
            f"TP: +{TP_TRIGGER_PCT}% → close {TP_CLOSE_PCT}%",
            f"Remaining SL: -{TP_REMAINING_SL_PCT}%",
            "",
            f"Max Risk: {MAX_ACCOUNT_RISK_PCT}%",
            f"Daily DD Pause: {MAX_DAILY_DRAWDOWN_PCT}%",
            f"Emergency SL: {EMERGENCY_SL_PCT}%",
            f"Trend Filter: {'ON' if TREND_FILTER_ENABLED else 'OFF'}",
            f"Funding Filter: {'ON' if FUNDING_FILTER_ENABLED else 'OFF'}",
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        ])
        await callback.message.reply(text, parse_mode="HTML")


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
