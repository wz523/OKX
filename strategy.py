
# -*- coding: utf-8 -*-
import sys, os, logging, time
from okx_api import cancel_all
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Dict, Any

import cfg
from market import Market
from account import Account
from grid_sys import Grid
from risk_sys import MarginGuard
from indicators import resonance, trend_filters_ok
from cfg import log_action

log = logging.getLogger("GVWAP")
def _setup_logging():
    # 只设置级别，不新增 handler；统一走 main.py 的 root handlers，避免重复打印
    log.setLevel(logging.INFO)
class Strategy:
    def __init__(self, acc: Account, grid: Grid, guard: MarginGuard):
        self.acc = acc
        self.grid = grid
        self.guard = guard
        # first-entry reference prices for loss%
        self.acc.first_entry_px_long = None
        self.acc.first_entry_px_short = None
        # DCA counters & window states
        self.dca_used_long = 0
        self.dca_used_short = 0
        self.in_dca_long = False
        self.in_dca_short = False
        self.last_signal_ts = 0.0
        # 趋势加仓状态（按日计数与当前开仓数量）
        self.trend_open_long = 0
        self.trend_open_short = 0
        self.trend_daily_count_long = 0
        self.trend_daily_count_short = 0
        self.last_trend_ts_long = 0.0
        self.last_trend_ts_short = 0.0
        self.last_trend_px_long = None
        self.last_trend_px_short = None
        # 趋势按自然日计数的日期键
        self._trend_day = time.strftime('%Y-%m-%d', time.localtime())


    def takeover_positions(self):
        # 直接读取当前持仓（要求 .env 配置正确）
        pos = self.acc.get_positions(); log.info("启动接管持仓：%s", pos)
        # 初始化 first-entry（若已有持仓）
        if pos.get("long", {}).get("pos", 0) > 0:
            self.acc.first_entry_px_long = Decimal(str(pos["long"].get("avgPx") or self.acc.mkt.px()))
        if pos.get("short", {}).get("pos", 0) > 0:
            self.acc.first_entry_px_short = Decimal(str(pos["short"].get("avgPx") or self.acc.mkt.px()))

    def _update_first_entry_edges(self):
        # 边沿检测：0->>0 设置首笔；>0->0 清空
        pos = self.acc.get_positions()
        now_long = Decimal(str(pos.get("long", {}).get("pos", 0)))
        now_short = Decimal(str(pos.get("short", {}).get("pos", 0)))
        if self.acc.first_entry_px_long is None and now_long > 0:
            self.acc.first_entry_px_long = Decimal(str(pos["long"].get("avgPx") or self.acc.mkt.px()))
        if self.acc.first_entry_px_short is None and now_short > 0:
            self.acc.first_entry_px_short = Decimal(str(pos["short"].get("avgPx") or self.acc.mkt.px()))
        if now_long <= 0:
            self.acc.first_entry_px_long = None
        if now_short <= 0:
            self.acc.first_entry_px_short = None

    def _loss_pct(self, side: str, px: Decimal, avgPx: Decimal) -> Decimal:
        ref = getattr(self.acc, f"first_entry_px_{side}", None) or avgPx
        if ref is None or ref <= 0:
            return Decimal("0")
        return (ref - px) / ref if side == "long" else (px - ref) / ref

    def _notional_to_lots(self, usd: Decimal) -> str:
        px = Decimal(str(self.acc.mkt.px()))
        ct = Decimal(str(self.acc.mkt.spec.get("ctVal", 0.1)))
        lots = (usd / px) / ct if px>0 and ct>0 else Decimal("0.01")
        lots_f = cfg.align_size(lots, self.acc.mkt.spec.get('lotSz'), self.acc.mkt.spec.get('minSz'))
        return f"{lots_f:.2f}"

    
    def _manage_take_profit(self, sig: Dict[str, Any]):
        """R3 止盈：当浮盈≥TP_BASE_USD 时触发。
        无趋势→全平；有趋势→先部分止盈，再按回撤阈值追踪至全平。"""
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

                # 方向是否仍有趋势
                has_trend = bool(sig.get("bull")) if side == "long" else bool(sig.get("bear"))

                if qty <= 0:
                    # 清理状态
                    setattr(self.acc, f"partial_done_{side}", False)
                    setattr(self.acc, f"trail_active_{side}", False)
                    setattr(self.acc, f"trail_peak_upl_{side}", Decimal("0"))
                    continue

                # 维护峰值
                peak_attr = f"trail_peak_upl_{side}"
                peak = getattr(self.acc, peak_attr, Decimal("0"))
                if upl > peak:
                    setattr(self.acc, peak_attr, upl)
                    peak = upl

                partial_attr = f"partial_done_{side}"
                tr_active_attr = f"trail_active_{side}"
                partial_done = bool(getattr(self.acc, partial_attr, False))
                trail_active = bool(getattr(self.acc, tr_active_attr, False))

                # 起始止盈
                if not partial_done and upl >= base_usd:
                    if not has_trend:
                        # 无趋势：全平
                        lot = self.acc.mkt.spec.get('lotSz'); minSz = self.acc.mkt.spec.get('minSz')
                        sz = (qty / lot).to_integral_value(rounding=ROUND_DOWN) * lot
                        if sz < minSz and qty > 0:
                            try:
                                from okx_api import close_position
                                close_position(self.acc.inst, td_mode=self.acc.td_mode)
                            except Exception as _e:
                                log.warning("close-position 失败：%s", _e)
                            sz = Decimal("0")
                        if sz > 0:
                            self.acc.place_order("sell" if side == "long" else "buy",
                                                 sz, px=None, reduce_only=True, tag="TP", posSide=side)
                            # 重置趋势计数
                            if side == "long":
                                self.trend_open_long = 0
                            else:
                                self.trend_open_short = 0
                        continue
                    else:
                        # 有趋势：先平部分
                        lot = self.acc.mkt.spec.get('lotSz'); minSz = self.acc.mkt.spec.get('minSz')
                        part = ((qty * ratio) / lot).to_integral_value(rounding=ROUND_DOWN) * lot
                        if part < minSz:
                            part = Decimal("0")
                        if part > 0:
                            self.acc.place_order("sell" if side == "long" else "buy",
                                                 part, px=None, reduce_only=True, tag="TP", posSide=side)
                            setattr(self.acc, partial_attr, True)
                            setattr(self.acc, tr_active_attr, True)
                            setattr(self.acc, peak_attr, upl)
                            log.info("[TP 部分] side=%s ratio=%.2f qty=%.6f", side, float(ratio), float(part))
                        continue

                # 已部分止盈 → 检查追踪
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
                                close_position(self.acc.inst, td_mode=self.acc.td_mode)
                            except Exception as _e:
                                log.warning("close-position 失败：%s", _e)
                            sz = Decimal("0")
                        if sz > 0:
                            self.acc.place_order("sell" if side == "long" else "buy",
                                                 sz, px=None, reduce_only=True, tag="TP", posSide=side)
                            setattr(self.acc, tr_active_attr, False)
                            setattr(self.acc, partial_attr, False)
                            setattr(self.acc, peak_attr, Decimal("0"))
                            # 重置趋势计数
                            if side == "long":
                                self.trend_open_long = 0
                            else:
                                self.trend_open_short = 0
                            log.info("[TP 追踪全平] side=%s dd=%.6f dd_pct=%.6f", side, float(dd), float(dd_pct))
        except Exception as e:
            log.warning("止盈管理异常：%s", e)

    def _dca_if_needed(self):
        """亏损方向加仓：当亏损在区间内进入 DCA 窗口，窗口内用 dca_reverse_signal 触发每次加仓。"""
        log_action("dca.check")
        if not getattr(cfg, "DCA_ENABLE", True):
            return
        # 风控暂停则不执行DCA
        if hasattr(self, 'guard') and getattr(self.guard, 'paused', False):
            log_action('dca.blocked', reason='guard_paused')
            return

        try:
            px = Decimal(str(self.acc.mkt.px()))
        except Exception:
            px = Decimal("0")
        pos = self.acc.get_positions()
        for side in ("long", "short"):
            p = pos.get(side, {})
            avgPx = Decimal(str(p.get("avgPx") or px))
            loss = self._loss_pct(side, px, avgPx)
            in_flag = bool(getattr(self, f"in_dca_{side}", False))
            # 入窗：亏损落在区间
            if (not in_flag) and loss >= getattr(cfg, "DCA_MIN_PCT", Decimal("0.08")) and loss <= getattr(cfg, "DCA_MAX_PCT", Decimal("0.24")):
                setattr(self, f"in_dca_{side}", True)
                # 暂停该侧网格
                if side == "long":
                    self.acc.pause_long = True
                else:
                    self.acc.pause_short = True
                # 撤掉该侧网格挂单
                try:
                    if hasattr(self.acc, "cancel_orders_by_tag_and_side"):
                        self.acc.cancel_orders_by_tag_and_side("GRID", side)
                    else:
                        self.acc.cancel_orders_by_tag("GRID")
                except Exception:
                    pass
                log_action("dca.window.enter", side=side, loss=float(loss))
                continue
            # 在窗口内处理
            if in_flag:
                from indicators import dca_reverse_signal
                used = self.dca_used_long if side == "long" else self.dca_used_short
                cap = getattr(cfg, "DCA_TOTAL_CAP", 4)
                # 触发反向信号→加仓
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
                # 离开窗口：价格回到均价或盈利
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

    
                    # 重置 DCA 次数计数
                    if side == "long":
                        self.dca_used_long = 0
                    else:
                        self.dca_used_short = 0

    def _trend_add_if_needed(self, sig: Dict[str, Any]):
        """趋势加仓：当 MACD+VWAP 共振且量能条件满足时，在盈利方向加仓。

        依赖指标共振：sig['bull']/['bear'] + sig['vol_bull']/['vol_bear']。
        冷却：cfg.TREND_COOLDOWN_SEC；每日上限：cfg.TREND_DAILY_CAP。
        最小距离：cfg.TREND_MIN_DISTANCE_PCT，基于当前价与上次趋势加仓价。
        """
        try:
            # 风控暂停则不加仓
            if hasattr(self.guard, "paused") and self.guard.paused:
                return
            # 自然日切换时重置每日计数
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
                # 信号满足：价格与指标共振且量能共振
                if side == "long":
                    # 仅当多头持仓>0 且浮盈>0 才允许趋势加仓
                    if getattr(cfg, 'TREND_REQUIRE_PROFIT', True):
                        if not (_lp > 0 and _lu > 0):
                            continue
                        continue
                    want = bool(sig.get("bull") and sig.get("vol_bull"))
                else:
                    # 仅当空头持仓>0 且浮盈>0 才允许趋势加仓
                    if getattr(cfg, 'TREND_REQUIRE_PROFIT', True):
                    if not (_sp > 0 and _su > 0):
                        continue
                        continue
                    want = bool(sig.get("bear") and sig.get("vol_bear"))
                if not want:
                    continue
                # 动量闸门 + 二次确认
                try:
                    _ok = trend_filters_ok(self.inst, side)
                except Exception:
                    _ok = False
                if not _ok:
                    continue

                # DCA 窗口内不加仓
                if getattr(self, f"in_dca_{side}", False):
                    continue
                # 冷却时间
                cool = int(getattr(cfg, "TREND_COOLDOWN_SEC", 0) or 0)
                last_ts = self.last_trend_ts_long if side == "long" else self.last_trend_ts_short
                _now_ts = time.time()
                if cool > 0 and (_now_ts - last_ts) < cool:
                    continue
                # 最小距离（相对当前价）
                min_dist = Decimal(str(getattr(cfg, "TREND_MIN_DISTANCE_PCT", Decimal("0")) or 0))
                try:
                    cur_px = Decimal(str(self.acc.mkt.px()))
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
                # 每日次数限制
                cap = int(getattr(cfg, "TREND_DAILY_CAP", 0) or 0)
                daily_cnt = self.trend_daily_count_long if side == "long" else self.trend_daily_count_short
                if cap > 0 and daily_cnt >= cap:
                    continue
                # 计算加仓张数
                usd = Decimal(str(getattr(cfg, "TREND_NOTIONAL_USD", Decimal("8"))))
                lots_str = self._notional_to_lots(usd)
                lots = Decimal(str(lots_str))
                if lots <= 0:
                    continue
                # 下市价单
                self.acc.place_order(
                    "buy" if side == "long" else "sell",
                    lots,
                    px=None,
                    reduce_only=False,
                    tag="ADD",
                    posSide=side,
                )
                # 更新计数与时间戳
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

    def _liqpx_guard_check(self):
        """基于强平价距离的本地风控：距离≤LIQPX_STOP_USD 则暂停；恢复需≥LIQPX_RESUME_USD。"""
        try:
            liq_stop = Decimal(str(getattr(cfg, "LIQPX_STOP_USD", Decimal("80"))))
            liq_resume = Decimal(str(getattr(cfg, "LIQPX_RESUME_USD", Decimal("140"))))
            try:
                px_now = Decimal(str(self.acc.mkt.px()))
            except Exception:
                px_now = None
            pos = self.acc.get_positions()
            def _dist(d):
                lp = d.get("liqPx")
                p = d.get("pos")
                if px_now is None or lp in (None, 0) or p is None or Decimal(str(p)) <= 0:
                    return None
                try:
                    lp = Decimal(str(lp))
                except Exception:
                    return None
                return abs(px_now - lp)
            d_long = _dist(pos.get("long", {})) or None
            d_short = _dist(pos.get("short", {})) or None
            liq_too_close = any([d is not None and d <= liq_stop for d in (d_long, d_short)])
            liq_safe = all([(d is None) or (d >= liq_resume) for d in (d_long, d_short)])
            # 暂停或恢复
            if liq_too_close:
                if not getattr(self, "_liq_paused", False):
                    setattr(self, "_liq_paused", True)
                    self.guard.paused = True
                    log.error("[风控触发] 距强平价≤%sUSD → 暂停加仓/趋势/DCA", liq_stop)
                    try:
                        self.acc.cancel_pending_by_tags(["GRID", "DCA", "ADD"])
                    # 追加：撤销亏损方向的委托（仅撤 GRID/DCA/ADD）
                        try:
                            pos = self.acc.get_positions()
                            losers = []
                            for side_name in ("long","short"):
                                p = pos.get(side_name, {})
                                if (p.get("pos") or 0) > 0 and (p.get("uPnl") or 0) < 0:
                                    losers.append(side_name)
                            for loser in losers:
                                ps = "long" if loser=="long" else "short"
                                for tag in ("GRID","DCA","ADD"):
                                    try:
                                        self.acc.cancel_orders_by_tag_and_side(tag, ps)
                                    except Exception as _:
                                        pass
                            if losers:
                                log.warning("[风控触发] 已撤亏损方向挂单：%s", ",".join(losers))
                        except Exception as _e:
                            log.warning("撤亏损方向挂单异常：%s", _e)
                    except Exception as e:
                        log.warning("风控撤单异常：%s", e)
            elif getattr(self, "_liq_paused", False) and liq_safe:
                setattr(self, "_liq_paused", False)
                # 仅当 guard 因我们触发而暂停时才恢复；保守起见直接尝试恢复，若其他风控仍生效会再次暂停
                if getattr(self.guard, "paused", False):
                    self.guard.paused = False
                    log.warning("[风控恢复] 距强平价≥%sUSD → 恢复策略", liq_resume)
        except Exception as e:
            log.warning("强平价风控异常：%s", e)

    def manage(self, mkt: Market):
        self._liqpx_guard_check()
        """单次维护（不建议循环内重复调用）。执行风控、网格修复、刷新信号，并依次处理 DCA、趋势加仓和止盈。"""
        self.guard.refresh()
        self.grid.place_missing()
        if time.time() - self.last_signal_ts >= getattr(cfg, "SIGNAL_REFRESH_SEC", 30):
            sig = resonance(self.acc.inst)
            self.last_signal_ts = time.time()
            self._update_first_entry_edges()
            self._dca_if_needed()
            self._trend_add_if_needed(sig)
            self._manage_take_profit(sig)



    def manage_forever(self, mkt: Market):
        self._liqpx_guard_check()
        """常驻维护循环：风控→自修复→信号→DCA→心跳；支持自动重启与边吃满边重建"""
        tick_count = 0
        last_summary = 0
        while True:
            try:
                # 风控刷新
                self.guard.refresh()
                # 网格自修复
                self.grid.place_missing()
                # 吃满一侧→在当前价格重建（近似“最后成交价”）
                live_b, live_s = self.grid.side_live_counts()
                if getattr(self.grid, "_had_full_live", False) and (live_b + live_s) > 0 and (live_b == 0 or live_s == 0) and (time.time() - getattr(self, "last_rebuild_ts", 0) >= getattr(cfg, "REBUILD_COOLDOWN_SEC", 12)):
                    cen = Decimal(str(mkt.px()))
                    if hasattr(self.acc, "last_fill_px") and self.acc.last_fill_px:
                        cen = Decimal(str(self.acc.last_fill_px))
                    log.info("一侧吃满→重建网格：center=%.2f", float(cen), extra={"event":"grid.rebuild.trigger","reason":"one_side_empty","center":float(cen)})
                    self.last_rebuild_ts = time.time()
                    self.grid.cancel_all_grid_orders()
                    self.grid.rebuild(center=cen)
                    self.grid.place_all()
                # 信号+DCA+趋势+止盈
                now = time.time()
                if now - getattr(self, "last_signal_ts", 0) >= getattr(cfg, "SIGNAL_REFRESH_SEC", 30):
                    sig = resonance(self.acc.inst)
                    self.last_signal_ts = now
                    self._update_first_entry_edges()
                    self._dca_if_needed()
                    self._trend_add_if_needed(sig)
                    self._manage_take_profit(sig)
                # 节奏&摘要
                time.sleep(max(1, int(cfg.TICK_REFRESH_SEC)))
                tick_count += 1
                if cfg.LOG_SUMMARY_EVERY_SEC and (now - last_summary) >= cfg.LOG_SUMMARY_EVERY_SEC:
                    pos = self.acc.get_positions()
                    log.debug("网格健康摘要 | center=%.2f | buy=%d sell=%d | pos=%s",
                             float(self.grid.center or 0), len(self.grid.buy_lv), len(self.grid.sell_lv), pos)
                    last_summary = now
                # 自动重启
                if getattr(cfg, "AUTO_RESTART_AFTER_TICKS", 0) and tick_count >= cfg.AUTO_RESTART_AFTER_TICKS:
                    log.warning("达到 AUTO_RESTART_AFTER_TICKS=%s，准备安全重启进程", cfg.AUTO_RESTART_AFTER_TICKS)
                    os.execv(sys.executable, [sys.executable] + sys.argv)
            except KeyboardInterrupt:
                log.info("收到中断，安全退出。"); break
            except Exception as e:
                log.warning("循环异常：%r", e, exc_info=True)
                time.sleep(3)

def run_strategy_once(inst_id: str, td_mode: str):
    """最小化变更：
    - 启动先撤单 + 验单
    - 再重建网格并挂单
    - 退出统一撤单
    """
    mkt = Market(inst_id); mkt.load_spec(); mkt.refresh_mid()
    acc = Account(inst_id, mkt, td_mode)
    try:
        # === 启动：统一撤单 + 验单（ACT） ===
        try:
            log_action("cancel.all.startup", inst=inst_id)
            cancel_all(inst_id)
        except Exception:
            pass
        try:
            live_after = list(acc.live_grid_prices())
            log_action("startup.live_orders.after_cancel", count=len(live_after))
        except Exception:
            pass

        # === 原有逻辑：建网格并挂单 ===
        try:
            cfg._load_sizes_with_risk(mkt, acc)
        except Exception:
            pass

        grid = Grid(acc)
        center = Decimal(str(mkt.px()))
        grid.rebuild(center=center)
        grid.place_all()

        guard = MarginGuard(acc, grid)
        strat = Strategy(acc, grid, guard)
        strat.takeover_positions()  # 接管现有持仓

        # === 常驻 ===
        strat.manage_forever(mkt)

    finally:
        # === 退出：统一撤单（ACT） ===
        try:
            log_action("cancel.all.exit", inst=inst_id)
            # 尝试多轮撤单直到清空或达到上限
            for _i in range(5):
                cancel_all(inst_id)
                time.sleep(1.0)
                from okx_api import fetch_open_orders
                open_left = fetch_open_orders(inst_id)
                if not open_left:
                    break
            else:
                log.warning("退出时仍有未撤销挂单，已尽最大努力。")
        except Exception:
            log.warning("退出撤单流程出现异常", exc_info=False)

