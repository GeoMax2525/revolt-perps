"""
engine.py — Martingale Grid Engine

Core trading logic:
1. Open base position in trend direction
2. If price moves against, add grid levels (doubling)
3. At +100% profit → close 50%, SL to -40% on rest
4. Enhanced partial TPs at +50%, +150%
5. All 8 safety layers enforced
"""

import asyncio
import logging
import time
from datetime import datetime, timedelta
from dataclasses import dataclass, field

from config import (
    STARTING_BALANCE, LEVERAGE, DIRECTION,
    BASE_ORDER_PCT, GRID_SPACING_MODE, GRID_SPACING_FIXED,
    ATR_MULTIPLIER, DOUBLING_FACTOR, MAX_GRID_LEVELS,
    TP_TRIGGER_PCT, TP_CLOSE_PCT, TP_REMAINING_SL_PCT,
    PARTIAL_TP_1_PCT, PARTIAL_TP_1_TRIGGER,
    PARTIAL_TP_2_PCT, PARTIAL_TP_2_TRIGGER,
    MAX_ACCOUNT_RISK_PCT, MAX_DAILY_DRAWDOWN_PCT, EMERGENCY_SL_PCT,
    TREND_FILTER_ENABLED, FUNDING_FILTER_ENABLED, FUNDING_RATE_MAX,
)
from price_feed import get_btc_price, get_atr, get_ema, get_funding_rate

logger = logging.getLogger(__name__)


@dataclass
class GridLevel:
    """Single grid level (one order in the ladder)."""
    level: int
    entry_price: float
    size_usd: float
    filled: bool = False
    fill_price: float = 0.0
    fill_time: float = 0.0


@dataclass
class GridCycle:
    """One complete grid cycle from entry to close."""
    cycle_id: int
    direction: str  # "long" or "short"
    start_time: float = 0.0
    levels: list = field(default_factory=list)
    active: bool = True
    total_size_usd: float = 0.0
    avg_entry_price: float = 0.0
    remaining_pct: float = 100.0
    realized_pnl: float = 0.0
    partial_tp1_done: bool = False
    partial_tp2_done: bool = False
    main_tp_done: bool = False
    sl_price: float = 0.0
    close_reason: str = ""
    close_pnl: float = 0.0


class MartingaleGridEngine:
    """The core trading engine with all 8 safety layers."""

    def __init__(self):
        self.balance = float(STARTING_BALANCE)
        self.peak_balance = self.balance
        self.daily_start_balance = self.balance
        self.daily_reset_time = time.time()
        self.cycle_count = 0
        self.active_cycle: GridCycle | None = None
        self.completed_cycles: list[GridCycle] = []
        self.paused = False
        self.pause_reason = ""
        self.grid_spacing = float(GRID_SPACING_FIXED)

        # Stats
        self.total_wins = 0
        self.total_losses = 0
        self.total_pnl = 0.0

    async def initialize(self):
        """Load ATR and EMA for first cycle."""
        if GRID_SPACING_MODE == "atr":
            atr = await get_atr()
            self.grid_spacing = round(atr * ATR_MULTIPLIER, 2)
            logger.info("Grid spacing set to $%.2f (ATR=%.2f x %.1f)",
                        self.grid_spacing, atr, ATR_MULTIPLIER)
        else:
            self.grid_spacing = GRID_SPACING_FIXED
            logger.info("Grid spacing fixed at $%.2f", self.grid_spacing)

    # ── Safety Layer Checks ──────────────────────────────────────────────────

    async def _check_trend_filter(self, price: float) -> str:
        """Safety Layer 5: Only trade in trend direction."""
        if not TREND_FILTER_ENABLED:
            return DIRECTION if DIRECTION != "auto" else "long"

        ema = await get_ema()
        if ema <= 0:
            return "long"  # fallback

        if price > ema:
            direction = "long"
        elif price < ema:
            direction = "short"
        else:
            direction = "long"

        logger.info("Trend filter: price=$%.2f EMA200=$%.2f → %s", price, ema, direction)
        return direction

    async def _check_funding_filter(self, direction: str) -> bool:
        """Safety Layer 8: Skip if funding rate is against position."""
        if not FUNDING_FILTER_ENABLED:
            return True

        rate = await get_funding_rate()
        if direction == "long" and rate > FUNDING_RATE_MAX:
            logger.info("Funding filter: rate=%.4f%% > max, skip long", rate * 100)
            return False
        if direction == "short" and rate < -FUNDING_RATE_MAX:
            logger.info("Funding filter: rate=%.4f%% < -max, skip short", rate * 100)
            return False
        return True

    def _check_daily_drawdown(self) -> bool:
        """Safety Layer 7: Auto-pause if down >10% in 24h."""
        # Reset daily tracker
        if time.time() - self.daily_reset_time > 86400:
            self.daily_start_balance = self.balance
            self.daily_reset_time = time.time()

        if self.daily_start_balance > 0:
            dd_pct = (self.daily_start_balance - self.balance) / self.daily_start_balance * 100
            if dd_pct >= MAX_DAILY_DRAWDOWN_PCT:
                self.paused = True
                self.pause_reason = f"Daily drawdown {dd_pct:.1f}% >= {MAX_DAILY_DRAWDOWN_PCT}%"
                logger.warning("SAFETY: %s — pausing", self.pause_reason)
                return False
        return True

    def _check_emergency_sl(self) -> bool:
        """Safety Layer 7b: Emergency full close if account drops too much."""
        if STARTING_BALANCE > 0:
            total_loss_pct = (STARTING_BALANCE - self.balance) / STARTING_BALANCE * 100
            if total_loss_pct >= EMERGENCY_SL_PCT:
                logger.warning("EMERGENCY SL: account down %.1f%% — closing everything",
                               total_loss_pct)
                return False
        return True

    def _check_max_risk(self) -> bool:
        """Safety Layer 2: Max account risk per grid."""
        if self.active_cycle:
            exposure = self.active_cycle.total_size_usd * LEVERAGE
            max_exposure = self.balance * MAX_ACCOUNT_RISK_PCT / 100
            if exposure >= max_exposure:
                logger.info("Max risk: exposure $%.2f >= limit $%.2f",
                            exposure, max_exposure)
                return False
        return True

    # ── Grid Management ──────────────────────────────────────────────────────

    async def open_new_cycle(self):
        """Start a new grid cycle."""
        if self.active_cycle:
            logger.info("Already have active cycle %d", self.active_cycle.cycle_id)
            return

        if self.paused:
            logger.info("Bot paused: %s", self.pause_reason)
            return

        if not self._check_daily_drawdown():
            return
        if not self._check_emergency_sl():
            return

        price = await get_btc_price()
        if price <= 0:
            return

        # Determine direction
        if DIRECTION == "auto":
            direction = await self._check_trend_filter(price)
        else:
            direction = DIRECTION

        # Check funding
        if not await self._check_funding_filter(direction):
            return

        # Refresh ATR-based spacing
        if GRID_SPACING_MODE == "atr":
            atr = await get_atr()
            self.grid_spacing = round(atr * ATR_MULTIPLIER, 2)

        # Create cycle
        self.cycle_count += 1
        base_size = self.balance * BASE_ORDER_PCT / 100

        cycle = GridCycle(
            cycle_id=self.cycle_count,
            direction=direction,
            start_time=time.time(),
        )

        # Fill base order immediately
        level = GridLevel(
            level=1,
            entry_price=price,
            size_usd=base_size,
            filled=True,
            fill_price=price,
            fill_time=time.time(),
        )
        cycle.levels.append(level)
        cycle.total_size_usd = base_size
        cycle.avg_entry_price = price

        self.active_cycle = cycle
        logger.info(
            "CYCLE %d OPENED: %s at $%.2f | base=$%.2f | spacing=$%.2f | leverage=%dx",
            cycle.cycle_id, direction.upper(), price, base_size,
            self.grid_spacing, LEVERAGE,
        )

    def _add_grid_level(self, price: float):
        """Add next grid level (doubling)."""
        cycle = self.active_cycle
        if not cycle or not cycle.active:
            return

        # Safety Layer 1: Max grid depth
        if len(cycle.levels) >= MAX_GRID_LEVELS:
            logger.warning("MAX GRID DEPTH reached (%d levels) — no more adds",
                           MAX_GRID_LEVELS)
            return

        # Safety Layer 2: Max risk check
        if not self._check_max_risk():
            return

        prev_size = cycle.levels[-1].size_usd
        new_size = prev_size * DOUBLING_FACTOR
        new_level = len(cycle.levels) + 1

        level = GridLevel(
            level=new_level,
            entry_price=price,
            size_usd=new_size,
            filled=True,
            fill_price=price,
            fill_time=time.time(),
        )
        cycle.levels.append(level)

        # Recalculate average entry
        total_cost = sum(l.size_usd for l in cycle.levels)
        weighted_entry = sum(l.fill_price * l.size_usd for l in cycle.levels) / total_cost
        cycle.total_size_usd = total_cost
        cycle.avg_entry_price = weighted_entry

        logger.info(
            "GRID LEVEL %d: %s $%.2f at $%.2f | total=$%.2f avg=$%.2f",
            new_level, cycle.direction.upper(), new_size, price,
            total_cost, weighted_entry,
        )

    # ── Price Action Handler ─────────────────────────────────────────────────

    async def on_price_update(self, price: float):
        """Called every tick with current price. Manages the active cycle."""
        if not self.active_cycle or not self.active_cycle.active:
            return

        cycle = self.active_cycle

        # Calculate current P&L
        if cycle.direction == "long":
            pnl_pct = (price - cycle.avg_entry_price) / cycle.avg_entry_price * 100 * LEVERAGE
        else:
            pnl_pct = (cycle.avg_entry_price - price) / cycle.avg_entry_price * 100 * LEVERAGE

        position_value = cycle.total_size_usd * (cycle.remaining_pct / 100)
        unrealized_pnl = position_value * pnl_pct / 100

        # ── Check grid level adds (price moving against us) ──────────
        if cycle.direction == "long" and price <= cycle.avg_entry_price - self.grid_spacing:
            self._add_grid_level(price)
        elif cycle.direction == "short" and price >= cycle.avg_entry_price + self.grid_spacing:
            self._add_grid_level(price)

        # ── Enhanced Partial TP 1: +50% → close 25% ─────────────────
        if not cycle.partial_tp1_done and pnl_pct >= PARTIAL_TP_1_TRIGGER:
            close_pct = PARTIAL_TP_1_PCT
            close_value = position_value * close_pct / 100
            profit = close_value * pnl_pct / 100
            cycle.realized_pnl += profit
            cycle.remaining_pct -= close_pct
            cycle.partial_tp1_done = True
            self.balance += profit
            logger.info(
                "PARTIAL TP1: closed %d%% at +%.1f%% | +$%.2f | remaining=%.0f%%",
                close_pct, pnl_pct, profit, cycle.remaining_pct,
            )

        # ── Main TP: +100% → close 50%, SL to -40% ──────────────────
        if not cycle.main_tp_done and pnl_pct >= TP_TRIGGER_PCT:
            close_pct = TP_CLOSE_PCT
            close_value = position_value * close_pct / 100
            profit = close_value * pnl_pct / 100
            cycle.realized_pnl += profit
            cycle.remaining_pct -= close_pct
            cycle.main_tp_done = True
            self.balance += profit

            # Move SL to -40% from current price
            if cycle.direction == "long":
                cycle.sl_price = price * (1 - TP_REMAINING_SL_PCT / 100)
            else:
                cycle.sl_price = price * (1 + TP_REMAINING_SL_PCT / 100)

            logger.info(
                "MAIN TP: closed %d%% at +%.1f%% | +$%.2f | SL moved to $%.2f | remaining=%.0f%%",
                close_pct, pnl_pct, profit, cycle.sl_price, cycle.remaining_pct,
            )

        # ── Enhanced Partial TP 2: +150% → close 25% ────────────────
        if not cycle.partial_tp2_done and cycle.main_tp_done and pnl_pct >= PARTIAL_TP_2_TRIGGER:
            close_pct = PARTIAL_TP_2_PCT
            remaining_value = cycle.total_size_usd * (cycle.remaining_pct / 100)
            profit = remaining_value * pnl_pct / 100 * (close_pct / cycle.remaining_pct)
            cycle.realized_pnl += profit
            cycle.remaining_pct -= close_pct
            cycle.partial_tp2_done = True
            self.balance += profit
            logger.info(
                "PARTIAL TP2: closed %d%% at +%.1f%% | +$%.2f | remaining=%.0f%%",
                close_pct, pnl_pct, profit, cycle.remaining_pct,
            )

        # ── Check SL (after main TP, use moved SL) ──────────────────
        if cycle.main_tp_done and cycle.sl_price > 0:
            hit_sl = (cycle.direction == "long" and price <= cycle.sl_price) or \
                     (cycle.direction == "short" and price >= cycle.sl_price)
            if hit_sl:
                await self._close_cycle("sl_after_tp", price, pnl_pct)
                return

        # ── Check max grid exhaustion SL ─────────────────────────────
        if len(cycle.levels) >= MAX_GRID_LEVELS and not cycle.main_tp_done:
            # Max grid reached and haven't hit TP — set emergency SL
            max_loss_pct = -50  # -50% on full position = emergency exit
            if pnl_pct <= max_loss_pct:
                await self._close_cycle("max_grid_sl", price, pnl_pct)
                return

        # ── Emergency account SL ─────────────────────────────────────
        if not self._check_emergency_sl():
            await self._close_cycle("emergency_sl", price, pnl_pct)
            return

    async def _close_cycle(self, reason: str, price: float, pnl_pct: float):
        """Close the active cycle."""
        cycle = self.active_cycle
        if not cycle:
            return

        remaining_value = cycle.total_size_usd * (cycle.remaining_pct / 100)
        remaining_pnl = remaining_value * pnl_pct / 100
        total_pnl = cycle.realized_pnl + remaining_pnl

        self.balance += remaining_pnl
        cycle.active = False
        cycle.close_reason = reason
        cycle.close_pnl = total_pnl

        if total_pnl >= 0:
            self.total_wins += 1
        else:
            self.total_losses += 1
        self.total_pnl += total_pnl

        self.peak_balance = max(self.peak_balance, self.balance)
        self.completed_cycles.append(cycle)
        self.active_cycle = None

        logger.info(
            "CYCLE %d CLOSED: %s | reason=%s | pnl=$%.2f (%.1f%%) | "
            "realized=$%.2f | levels=%d | balance=$%.2f",
            cycle.cycle_id, cycle.direction.upper(), reason,
            total_pnl, pnl_pct, cycle.realized_pnl,
            len(cycle.levels), self.balance,
        )

    # ── Status ───────────────────────────────────────────────────────────────

    def get_status(self) -> dict:
        """Return current engine status."""
        cycle = self.active_cycle
        total_trades = self.total_wins + self.total_losses
        wr = (self.total_wins / total_trades * 100) if total_trades > 0 else 0

        status = {
            "balance": round(self.balance, 2),
            "starting_balance": STARTING_BALANCE,
            "pnl": round(self.balance - STARTING_BALANCE, 2),
            "pnl_pct": round((self.balance - STARTING_BALANCE) / STARTING_BALANCE * 100, 1),
            "total_cycles": len(self.completed_cycles),
            "wins": self.total_wins,
            "losses": self.total_losses,
            "win_rate": round(wr, 1),
            "total_pnl": round(self.total_pnl, 2),
            "peak_balance": round(self.peak_balance, 2),
            "paused": self.paused,
            "pause_reason": self.pause_reason,
            "grid_spacing": self.grid_spacing,
            "leverage": LEVERAGE,
        }

        if cycle:
            status["active_cycle"] = {
                "id": cycle.cycle_id,
                "direction": cycle.direction,
                "levels": len(cycle.levels),
                "max_levels": MAX_GRID_LEVELS,
                "avg_entry": round(cycle.avg_entry_price, 2),
                "total_size": round(cycle.total_size_usd, 2),
                "remaining_pct": round(cycle.remaining_pct, 1),
                "realized_pnl": round(cycle.realized_pnl, 2),
                "sl_price": round(cycle.sl_price, 2) if cycle.sl_price else None,
                "main_tp_done": cycle.main_tp_done,
                "age_minutes": round((time.time() - cycle.start_time) / 60, 1),
            }

        return status
