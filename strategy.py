# -*- coding: utf-8 -*-
"""
Strategy module for the OKX grid bot.

This file implements the trading logic used by the OKX grid bot, including
position takeover, loss tracking, DCA (dollar cost averaging), trend-based
position adds and take-profit management.  It mirrors the upstream version of
the strategy from the wz523/OKX repository but fixes an indentation bug in
the trend-add logic that caused a Python `IndentationError` when running the
script.  Specifically, the nested checks for the `TREND_REQUIRE_PROFIT`
condition now properly wrap the inner `_lp/_lu` and `_sp/_su` checks, and
extraneous `continue` statements have been removed.

Note: This file expects that `okx_api.py`, `cfg.py`, `grid_sys.py`,
`risk_sys.py`, and `indicators.py` are available in the same directory or
importable Python path.  Imports and logging are preserved from upstream.
"""

import sys
import os
import logging
import time
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Dict, Any

# Local imports
import cfg
from okx_api import cancel_all
from market import Market
from account import Account
from grid_sys import Grid
from risk_sys import MarginGuard
from indicators import resonance, trend_filters_ok
from cfg import log_action

# Set up module-level logger
log = logging.getLogger("GVWAP")


def _setup_logging() -> None:
    """Configure the logger for this module.

    The strategy module itself does not attach additional handlers; it relies
    on the root logger configuration provided by `main.py` to avoid duplicate
    log output when imported.
    """
    log.setLevel(logging.INFO)


class Strategy:
    """Core trading strategy for the grid bot."""

    def __init__(self, acc: Account, grid: Grid, guard: MarginGuard) -> None:
        # Account / grid / guard references
        self.acc = acc
        self.grid = grid
        self.guard = guard

        # first-entry reference prices for loss calculations
        self.acc.first_entry_px_long = None
        self.acc.first_entry_px_short = None

        # DCA counters & window states
        self.dca_used_long = 0
        self.dca_used_short = 0
        self.in_dca_long = False
        self.in_dca_short = False
        self.last_signal_ts: float = 0.0

        # Trend add state (counts and timestamps)
        self.trend_open_long = 0
        self.trend_open_short = 0
        self.trend_daily_count_long = 0
        self.trend_daily_count_short = 0
        self.last_trend_ts_long: float = 0.0
        self.last_trend_ts_short: float = 0.0
        self.last_trend_px_long: float | None = None
        self.last_trend_px_short: float | None = None

        # Natural-day key for resetting daily trend counters
        self._trend_day = time.strftime('%Y-%m-%d', time.localtime())

    # ------------------------------------------------------------------
    # Position takeover and loss tracking
    # ------------------------------------------------------------------
    def takeover_positions(self) -> None:
        """Adopt any existing positions from the account on startup.

        This inspects the current positions via the account and records the
        first-entry price for both long and short sides (if any).  The first
        entry price is used as a baseline for loss-percentage calculations.
        """
        pos = self.acc.get_positions()
        log.info("启动接管持仓：%s", pos)
        # Initialize first-entry prices if positions already exist
        if pos.get("long", {}).get("pos", 0) > 0:
            self.acc.first_entry_px_long = Decimal(
                str(pos["long"].get("avgPx") or self.acc.mkt.refresh_mid()())
            )
        if pos.get("short", {}).get("pos", 0) > 0:
            self.acc.first_entry_px_short = Decimal(
                str(pos["short"].get("avgPx") or self.acc.mkt.refresh_mid()())
            )

    def _update_first_entry_edges(self) -> None:
        """Detect transitions into/out of positions and update first-entry prices."""
        pos = self.acc.get_positions()
        now_long = Decimal(str(pos.get("long", {}).get("pos", 0)))
        now_short = Decimal(str(pos.get("short", {}).get("pos", 0)))
        # Record first-entry price when opening a position
        if self.acc.first_entry_px_long is None and now_long > 0:
            self.acc.first_entry_px_long = Decimal(
                str(pos["long"].get("avgPx") or self.acc.mkt.refresh_mid()())
            )
        if self.acc.first_entry_px_short is None and now_short > 0:
            self.acc.first_entry_px_short = Decimal(
                str(pos["short"].get("avgPx") or self.acc.mkt.refresh_mid()())
            )
        # Clear first-entry price when closing a position
        if now_long <= 0:
            self.acc.first_entry_px_long = None
        if now_short <= 0:
            self.acc.first_entry_px_short = None

    def _loss_pct(self, side: str, px: Decimal, avgPx: Decimal) -> Decimal:
        """Compute loss percentage relative to the first entry or average price."""
        ref = getattr(self.acc, f"first_entry_px_{side}", None) or avgPx
        if ref is None or ref <= 0:
            return Decimal("0")
        return (ref - px) / ref if side == "long" else (px - ref) / ref

    def _notional_to_lots(self, usd: Decimal) -> str:
        """Convert a notional USD amount to a string representing lots."""
        px = Decimal(str(self.acc.mkt.refresh_mid()()))
        ct = Decimal(str(self.acc.mkt.spec.get("ctVal", 0.1)))
        lots = (usd / px) / ct if px > 0 and ct > 0 else Decimal("0.01")
        lots_f = cfg.align_size(
            lots, self.acc.mkt.spec.get('lotSz'), self.acc.mkt.spec.get('minSz')
        )
        return f"{lots_f:.2f}"

    # ------------------------------------------------------------------
    # Take-profit management
    # ------------------------------------------------------------------
    def _manage_take_profit(self, sig: Dict[str, Any]) -> None:
        """Handle taking profit based on unrealized PnL and trend signals."""
        try:
            pos = self.acc.get_positions()
            base_usd = Decimal(str(getattr(cfg, "TP_BASE_USD", Decimal("0.5"))))
            ratio = Decimal(str(getattr(cfg, "TP_PARTIAL_RATIO", Decimal("0.3"))))
            trail_usd = Decimal(str(getattr(cfg, "TP_TRAIL_USD", Decimal("0.5"))))
            trail_pct = Decimal(str(getattr(cfg, "TP_TRAIL_PCT", Decimal("0.007"))))

            for side in ("long", "short"):
                p = pos.get(side, {}) or {}
                qty = Decimal(str(p.get("pos") or 0))
                upl = Decimal(str(p.get("uPnl") or 0))

                # Determine if there is still trend on this side
                has_trend = bool(sig.get("bull")) if side == "long" else bool(sig.get("bear"))

                if qty <= 0:
                    # Reset state when no position
                    setattr(self.acc, f"partial_done_{side}", False)
                    setattr(self.acc, f"trail_active_{side}", False)
                    setattr(self.acc, f"trail_peak_upl_{side}", Decimal("0"))
                    continue

                # Maintain peak
                peak_attr = f"trail_peak_upl_{side}"
                peak = getattr(self.acc, peak_attr, Decimal("0"))
                if upl > peak:
                    setattr(self.acc, peak_attr, upl)
                    peak = upl

                partial_attr = f"partial_done_{side}"
                tr_active_attr = f"trail_active_{side}"
                partial_done = bool(getattr(self.acc, partial_attr, False))
                trail_active = bool(getattr(self.acc, tr_active_attr, False))

                # Initial take-profit: close all if no trend
                if not partial_done and upl >= base_usd:
                    if not has_trend:
                        # Close entire position
                        lot = self.acc.mkt.spec.get('lotSz'); minSz = self.acc.mkt.spec.get('minSz')
                        sz = (qty / lot).to_integral_value(rounding=ROUND_DOWN) * lot
                        if sz < minSz and qty > 0:
                            try:
                                from okx_api import close_position
                                close_position(self.acc.inst, posSide=side, td_mode=self.acc.td_mode)
                            except Exception as _e:
                                log.warning("close-position 失败：%s", _e)
                            sz = Decimal("0")
                        if sz > 0:
                            self.acc.place_order(
                                "sell" if side == "long" else "buy",
                                sz,
                                px=None,
                                reduce_only=True,
                                tag="TP",
                                posSide=side,
                            )
                            # Reset trend count
                            if side == "long":
                                self.trend_open_long = 0
                            else:
                                self.trend_open_short = 0
                        continue
                    else:
                        # Partial take-profit when there is trend
                        lot = self.acc.mkt.spec.get('lotSz'); minSz = self.acc.mkt.spec.get('minSz')
                        part = ((qty * ratio) / lot).to_integral_value(rounding=ROUND_DOWN) * lot
                        if part < minSz:
                            part = Decimal("0")
                        if part > 0:
                            self.acc.place_order(
                                "sell" if side == "long" else "buy",
                                part,
                                px=None,
                                reduce_only=True,
                                tag="TP",
                                posSide=side,
                            )
                            setattr(self.acc, partial_attr, True)
                            setattr(self.acc, tr_active_attr, True)
                            setattr(self.acc, peak_attr, upl)
                            log.info("[TP 部分] side=%s ratio=%.2f qty=%.6f", side, float(ratio), float(part))
                        continue

                # Already partially taken profit → check trailing
                if trail_active:
                    peak = getattr(self.acc, peak_attr, Decimal("0"))
                    dd = peak - upl
                    dd_pct = (dd / peak) if peak > 0 else Decimal("0")
                    if dd >= trail_usd or dd_pct >= trail_pct:
                        lot = self.acc.mkt.spec.get('lotSz'); minSz = self.acc.mkt.spec.get('minSz')
                        sz = (qty / lot).to_integral_value(rounding=ROUND_DOWN) * lot
                        if sz < minSz and qty > 0:
                            try:
                                from okx_api import close_position
                                close_position(self.acc.inst, posSide=side, td_mode=self.acc.td_mode)
                            except Exception as _e:
                                log.warning("close-position 失败：%s", _e)
                            sz = Decimal("0")
                        if sz > 0:
                            self.acc.place_order(
                                "sell" if side == "long" else "buy",
                                sz,
                                px=None,
                                reduce_only=True,
                                tag="TP",
                                posSide=side,
                            )
                            setattr(self.acc, tr_active_attr, False)
                            setattr(self.acc, partial_attr, False)
                            setattr(self.acc, peak_attr, Decimal("0"))
                            # Reset trend count
                            if side == "long":
                                self.trend_open_long = 0
                            else:
                                self.trend_open_short = 0
                            log.info("[TP 追踪全平] side=%s dd=%.6f dd_pct=%.6f", side, float(dd), float(dd_pct))
                        continue
        except Exception as e:
            log.warning("止盈管理异常：%s", e)

    # ------------------------------------------------------------------
    # Dollar-cost averaging (DCA)
    # ------------------------------------------------------------------
    def _dca_if_needed(self) -> None:
        """Add to a losing position when loss falls within configured windows."""
        log_action("dca.check")
        if not getattr(cfg, "DCA_ENABLE", True):
            return
        # Skip DCA if guard is paused
        if hasattr(self, 'guard') and getattr(self.guard, 'paused', False):
            log_action('dca.blocked', reason='guard_paused')
            return
        try:
            px = Decimal(str(self.acc.mkt.refresh_mid()()))
        except Exception:
            px = Decimal("0")
        pos = self.acc.get_positions()
        for side in ("long", "short"):
            p = pos.get(side, {})
            avgPx = Decimal(str(p.get("avgPx") or px))
            loss = self._loss_pct(side, px, avgPx)
            in_flag = bool(getattr(self, f"in_dca_{side}", False))
            # Enter DCA window: loss within configured min/max
            if (
                not in_flag
                and loss >= getattr(cfg, "DCA_MIN_PCT", Decimal("0.08"))
                and loss <= getattr(cfg, "DCA_MAX_PCT", Decimal("0.24"))
            ):
                setattr(self, f"in_dca_{side}", True)
                # Pause grid orders on this side
                if side == "long":
                    self.acc.pause_long = True
                else:
                    self.acc.pause_short = True
                # Cancel grid orders on this side
                try:
                    if hasattr(self.acc, "cancel_orders_by_tag_and_side"):
                        self.acc.cancel_orders_by_tag_and_side("GRID", side)
                    else:
                        self.acc.cancel_orders_by_tag("GRID")
                except Exception:
                    pass
                log_action("dca.window.enter", side=side, loss=float(loss))
                continue
            # Inside DCA window
            if in_flag:
                from indicators import dca_reverse_signal
                used = self.dca_used_long if side == "long" else self.dca_used_short
                cap = getattr(cfg, "DCA_TOTAL_CAP", 4)
                # Trigger reverse signal to add
                if used < cap and dca_reverse_signal(self.acc.inst, side):
                    usd = Decimal(str(getattr(cfg, "DCA_FIXED_NOTIONAL_USD", Decimal("8"))))
                    lots_str = self._notional_to_lots(usd)
                    lots = Decimal(str(lots_str))
                    if lots > 0:
                        if hasattr(self.guard, "paused") and self.guard.paused:
                            log.warning("[DCA阻断] 风控暂停，跳过下单 side=%s lots=%s", side, lots_str)
                        else:
                            self.acc.place_order(
                                "buy" if side == "long" else "sell",
                                lots,
                                px=None,
                                reduce_only=False,
                                tag="DCA",
                                posSide=side,
                            )
                            if side == "long":
                                self.dca_used_long += 1
                            else:
                                self.dca_used_short += 1
                            log.info("[DCA执行] side=%s loss=%.2f%% lots=%s", side, float(loss * 100), lots_str)
                # Exit window: price returns to or above avgPx for long (or below for short)
                pos2 = self.acc.get_positions()
                p2 = pos2.get(side, {})
                avgPx2 = Decimal(str(p2.get("avgPx") or px))
                if (side == "long" and px >= avgPx2) or (side == "short" and px <= avgPx2):
                    setattr(self, f"in_dca_{side}", False)
                    if side == "long":
                        self.acc.pause_long = False
                    else:
                        self.acc.pause_short = False
                    log_action("dca.window.exit", side=side, avg=float(avgPx2), px=float(px))
                    # Reset DCA used count
                    if side == "long":
                        self.dca_used_long = 0
                    else:
                        self.dca_used_short = 0

    # ------------------------------------------------------------------
    # Trend-based add logic
    # ------------------------------------------------------------------
    def _trend_add_if_needed(self, sig: Dict[str, Any]) -> None:
        """Add to a position when price/indicator resonance and volume conditions are met."""
        # Do nothing if guard is paused
        try:
            if hasattr(self.guard, "paused") and self.guard.paused:
                return
            # Reset daily counts on natural day change
            _today = time.strftime('%Y-%m-%d', time.localtime())
            if getattr(self, '_trend_day', None) != _today:
                self._trend_day = _today
                self.trend_daily_count_long = 0
                self.trend_daily_count_short = 0
            pos = self.acc.get_positions()
            _lp = Decimal(str(pos.get("long", {}).get("pos", 0)))
            _sp = Decimal(str(pos.get("short", {}).get("pos", 0)))
            _lu = Decimal(str(pos.get("long", {}).get("uPnl", 0)))
            _su = Decimal(str(pos.get("short", {}).get("uPnl", 0)))
            for side in ("long", "short"):
                # Only add when price and indicators resonate and volume resonates
                if side == "long":
                    # Only allow trend add when long position exists and profit > 0
                    if getattr(cfg, 'TREND_REQUIRE_PROFIT', True):
                        if not (_lp > 0 and _lu > 0):
                            continue
                    want = bool(sig.get("bull") and sig.get("vol_bull"))
                else:
                    # Only allow trend add when short position exists and profit > 0
                    if getattr(cfg, 'TREND_REQUIRE_PROFIT', True):
                        if not (_sp > 0 and _su > 0):
                            continue
                    want = bool(sig.get("bear") and sig.get("vol_bear"))
                if not want:
                    continue
                # Momentum gate + secondary confirmation
                try:
                    _ok = trend_filters_ok(self.acc.inst, side)
                except Exception:
                    _ok = False
                if not _ok:
                    continue
                # Do not add within DCA window
                if getattr(self, f"in_dca_{side}", False):
                    continue
                # Cooldown time
                cool = int(getattr(cfg, "TREND_COOLDOWN_SEC", 0) or 0)
                last_ts = self.last_trend_ts_long if side == "long" else self.last_trend_ts_short
                _now_ts = time.time()
                if cool > 0 and (_now_ts - last_ts) < cool:
                    continue
                # Minimum distance relative to current price
                min_dist = Decimal(str(getattr(cfg, "TREND_MIN_DISTANCE_PCT", Decimal("0")) or 0))
                try:
                    cur_px = Decimal(str(self.acc.mkt.refresh_mid()()))
                except Exception:
                    cur_px = None
                last_px_val = self.last_trend_px_long if side == "long" else self.last_trend_px_short
                if cur_px is not None and last_px_val is not None:
                    try:
                        last_px = Decimal(str(last_px_val))
                    except Exception:
                        last_px = None
                    if last_px is not None and min_dist > 0:
                        dist = abs(cur_px - last_px) / cur_px if cur_px != 0 else Decimal("1")
                        if dist < min_dist:
                            continue
                # Daily cap check
                cap = int(getattr(cfg, "TREND_DAILY_CAP", 0) or 0)
                daily_cnt = self.trend_daily_count_long if side == "long" else self.trend_daily_count_short
                if cap > 0 and daily_cnt >= cap:
                    continue
                # Calculate lots to add
                usd = Decimal(str(getattr(cfg, "TREND_NOTIONAL_USD", Decimal("8"))))
                lots_str = self._notional_to_lots(usd)
                lots = Decimal(str(lots_str))
                if lots <= 0:
                    continue
                # Place market order
                self.acc.place_order(
                    "buy" if side == "long" else "sell",
                    lots,
                    px=None,
                    reduce_only=False,
                    tag="ADD",
                    posSide=side,
                )
                # Update counters and timestamps
                now = time.time()
                if side == "long":
                    self.trend_open_long += 1
                    self.trend_daily_count_long += 1
                    self.last_trend_ts_long = now
                    self.last_trend_px_long = float(cur_px) if cur_px is not None else None
                else:
                    self.trend_open_short += 1
                    self.trend_daily_count_short += 1
                    self.last_trend_ts_short = now
                    self.last_trend_px_short = float(cur_px) if cur_px is not None else None
                log.info("[趋势加仓] side=%s lots=%s", side, lots_str)
                log_action("trend.add", side=side, lots=float(lots), usd=float(usd))
        except Exception as e:
            log.warning("趋势加仓异常：%s", e)

    # ------------------------------------------------------------------
    # Liquidation-price guard
    # ------------------------------------------------------------------
    def _liqpx_guard_check(self) -> None:
        """Pause the strategy if price is too close to liquidation and resume when safe."""
        try:
            liq_stop = Decimal(str(getattr(cfg, "LIQPX_STOP_USD", Decimal("80"))))
            liq_resume = Decimal(str(getattr(cfg, "LIQPX_RESUME_USD", Decimal("140"))))
            try:
                px_now = Decimal(str(self.acc.mkt.refresh_mid()()))
            except Exception:
                px_now = None
            pos = self.acc.get_positions()

            def _dist(d: Dict[str, Any]) -> Decimal | None:
                lp = d.get("liqPx")
                p = d.get("pos")
                if px_now is None or lp in (None, 0) or p is None or Decimal(str(p)) <= 0:
                    return None
                try:
                    lp_dec = Decimal(str(lp))
                except Exception:
                    return None
                return abs(px_now - lp_dec)

            d_long = _dist(pos.get("long", {})) or None
            d_short = _dist(pos.get("short", {})) or None
            liq_too_close = any([d is not None and d <= liq_stop for d in (d_long, d_short)])
            liq_safe = all([(d is None) or (d >= liq_resume) for d in (d_long, d_short)])
            # Pause or resume based on proximity to liquidation price
            if liq_too_close:
                if not getattr(self, "_liq_paused", False):
                    setattr(self, "_liq_paused", True)
                    self.guard.paused = True
                    log.error("[风控触发] 距强平价≤%sUSD → 暂停加仓/趋势/DCA", liq_stop)
                    try:
                        self.acc.cancel_pending_by_tags(["GRID", "DCA", "ADD"])
                        # Cancel orders on losing side only (GRID/DCA/ADD)
                        try:
                            pos = self.acc.get_positions()
                            losers: list[str] = []
                            for side_name in ("long", "short"):
                                p = pos.get(side_name, {})
                                if (p.get("pos") or 0) > 0 and (p.get("uPnl") or 0) < 0:
                                    losers.append(side_name)
                            for loser in losers:
                                ps = "long" if loser == "long" else "short"
                                for tag in ("GRID", "DCA", "ADD"):
                                    try:
                                        self.acc.cancel_orders_by_tag_and_side(tag, ps)
                                    except Exception:
                                        pass
                            if losers:
                                log.warning("[风控触发] 已撤亏损方向挂单：%s", ",".join(losers))
                        except Exception as _e:
                            log.warning("撤亏损方向挂单异常：%s", _e)
                    except Exception as e:
                        log.warning("风控撤单异常：%s", e)
            elif getattr(self, "_liq_paused", False) and liq_safe:
                setattr(self, "_liq_paused", False)
                # Only resume if guard was paused by this check; be conservative otherwise
                if getattr(self.guard, "paused", False):
                    self.guard.paused = False
                    log.warning("[风控恢复] 距强平价≥%sUSD → 恢复策略", liq_resume)
        except Exception as e:
            log.warning("强平价风控异常：%s", e)

    # ------------------------------------------------------------------
    # High-level manage functions
    # ------------------------------------------------------------------
    def manage(self, mkt: Market) -> None:
        """Single-cycle management (not intended for continuous loops)."""
        self._liqpx_guard_check()
        self.guard.refresh()
        self.grid.place_missing()
        if time.time() - self.last_signal_ts >= getattr(cfg, "SIGNAL_REFRESH_SEC", 30):
            sig = resonance(self.acc.inst)
            self.last_signal_ts = time.time()
            self._update_first_entry_edges()
            self._dca_if_needed()
            self._trend_add_if_needed(sig)
            self._manage_take_profit(sig)

    def manage_forever(self, mkt: Market) -> None:
        """Continuous management loop with built-in restarts and pacing."""
        self._liqpx_guard_check()
        tick_count = 0
        last_summary = 0
        while True:
            try:
                # Refresh guard
                self.guard.refresh()
                # Grid self-heal
                self.grid.place_missing()
                # Rebuild grid when one side is fully eaten
                live_b, live_s = self.grid.side_live_counts()
                if (
                    getattr(self.grid, "_had_full_live", False)
                    and (live_b + live_s) > 0
                    and (live_b == 0 or live_s == 0)
                    and (time.time() - getattr(self, "last_rebuild_ts", 0) >= getattr(cfg, "REBUILD_COOLDOWN_SEC", 12))
                ):
                    cen = Decimal(str(mkt.refresh_mid())) 
                    if hasattr(self.acc, "last_fill_px") and self.acc.last_fill_px:
                        cen = Decimal(str(self.acc.last_fill_px))
                    # 兜底：中心必须 > 0
                    if cen <= 0:
                        cen = Decimal(str(mkt.refresh_mid()()))
                    log.info(
                        "一侧吃满→重建网格：center=%.2f",
                        float(cen),
                        extra={"event": "grid.rebuild.trigger", "reason": "one_side_empty", "center": float(cen)},
                    )
                    self.last_rebuild_ts = time.time()
                    self.grid.cancel_all_grid_orders()
                    self.grid.rebuild(center=cen)
                    self.grid.place_all()
                # Signal + DCA + Trend + TP
                now = time.time()
                if now - getattr(self, "last_signal_ts", 0) >= getattr(cfg, "SIGNAL_REFRESH_SEC", 30):
                    sig = resonance(self.acc.inst)
                    self.last_signal_ts = now
                    self._update_first_entry_edges()
                    self._dca_if_needed()
                    self._trend_add_if_needed(sig)
                    self._manage_take_profit(sig)
                # Pace and summary
                time.sleep(max(1, int(cfg.TICK_REFRESH_SEC)))
                tick_count += 1
                if cfg.LOG_SUMMARY_EVERY_SEC and (now - last_summary) >= cfg.LOG_SUMMARY_EVERY_SEC:
                    pos = self.acc.get_positions()
                    log.debug(
                        "网格健康摘要 | center=%.2f | buy=%d sell=%d | pos=%s",
                        float(self.grid.center or 0),
                        len(self.grid.buy_lv),
                        len(self.grid.sell_lv),
                        pos,
                    )
                    last_summary = now
                # Auto restart
                if getattr(cfg, "AUTO_RESTART_AFTER_TICKS", 0) and tick_count >= cfg.AUTO_RESTART_AFTER_TICKS:
                    log.warning(
                        "达到 AUTO_RESTART_AFTER_TICKS=%s，准备安全重启进程",
                        cfg.AUTO_RESTART_AFTER_TICKS,
                    )
                    os.execv(sys.executable, [sys.executable] + sys.argv)
            except KeyboardInterrupt:
                # Graceful exit on Ctrl+C
                break
            except Exception as e:
                log.warning("管理循环异常：%s", e)


# ===== Added by ChatGPT: strategy entrypoint =====
def run_strategy_once(inst_id: str, td_mode: str) -> None:
    """Initialize and run the grid strategy loop.

    This function is imported by main.py and is responsible for wiring up
    Market, Account, Grid, MarginGuard, and Strategy, then starting the
    strategy run loop.
    """
    # Lazy imports to avoid side effects at module import time
    from decimal import Decimal
    try:
        mkt = Market(inst_id)
        acc = Account(inst_id, mkt, td_mode)
        grid = Grid(acc)
        guard = MarginGuard(acc, grid)
        strat = Strategy(acc, grid, guard)

        # adopt existing positions if any
        try:
            strat.takeover_positions()
        except Exception as _e:
            log.warning("takeover_positions failed: %s", _e)

        # build and place initial grid around current price
        try:
            center_price = Decimal(str(mkt.refresh_mid()))
            if center_price <= 0:
                # Fallback: attempt refresh and retry once
                mkt.refresh_mid()
                center_price = Decimal(str(mkt.refresh_mid()))
            grid.rebuild(center_price)
            grid.place_all()
        except Exception as _e:
            log.warning("grid init failed: %s", _e)

        # Start the main loop
        try:
            strat.manage_forever(mkt)
        except KeyboardInterrupt:
            log.info("收到手动中断(CTRL+C)，策略退出。")
        except Exception as _e:
            log.warning("策略循环异常：%s", _e, exc_info=True)
    except Exception as e:
        log.error("run_strategy_once 初始化异常：%s", e, exc_info=True)