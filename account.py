# -*- coding: utf-8 -*-
"""账户与下单封装
- 统一使用 tag 标记（GRID/ADD/TP/DCA），便于识别与撤单
- 确认成交超时改为读取 cfg.CONFIRM_FILL_TIMEOUT_MS（动态）
"""
from __future__ import annotations
import time
import logging
from decimal import Decimal
from typing import Dict, List

import cfg
from cfg import to_decimal, round_price, align_size, log_action
from okx_api import (
    fetch_positions, fetch_open_orders, place_limit, place_market,
    cancel_order, cancel_all, side_from_pos
)

log = logging.getLogger("GVWAP")


class Account:
    def __init__(self, inst: str, mkt, td_mode: str, fills_log_path: str | None = None):
        self.inst = inst
        self.api_retry_count = 0
        self.api_unhealthy = False
        self.missing_orders = []
        self._last_missing_ts = 0.0
        self.last_fill_px = None
        self.td_mode = td_mode
        self.mkt = mkt
        self.fills_log_path = fills_log_path
        # 基础张数（随 cfg 热更新）
        self.base_sz_long = Decimal("0"); self.base_sz_short = Decimal("0")
        self.trend_sz_long = Decimal("0"); self.trend_sz_short = Decimal("0")
        # 趋势并发与节流
        self.trend_open_long = 0; self.trend_open_short = 0
        self.trend_last_ts_long = 0.0; self.trend_last_ts_short = 0.0
        self.trend_daily_count_long = 0; self.trend_daily_count_short = 0
        # 止盈状态
        self.partial_done_long = False; self.partial_done_short = False
        self.trail_active_long = False; self.trail_active_short = False
        self.trail_peak_upl_long = Decimal("0"); self.trail_peak_upl_short = Decimal("0")
        # DCA 状态
        self.dca_used_slots_long = []; self.dca_used_slots_short = []
        self.dca_used_total_long = Decimal("0"); self.dca_used_total_short = Decimal("0")
        self.dca_initial_notional_long = None; self.dca_initial_notional_short = None

    # ===== 查询 =====
    def get_positions(self) -> Dict[str, Dict[str, Decimal]]:
        data = fetch_positions(self.inst)
        # 统一返回结构
        ret = {
            "long": {"pos": Decimal("0"), "avgPx": Decimal("0"), "liqPx": Decimal("0"), "uPnl": Decimal("0")},
            "short": {"pos": Decimal("0"), "avgPx": Decimal("0"), "liqPx": Decimal("0"), "uPnl": Decimal("0")},
        }
        for d in data:
            side = d.get("posSide", "").lower()
            if side not in ("long", "short"):
                continue
            ret[side]["pos"] = to_decimal(d.get("pos", 0))
            ret[side]["avgPx"] = to_decimal(d.get("avgPx", 0))
            ret[side]["liqPx"] = to_decimal(d.get("liqPx", 0))
            ret[side]["uPnl"] = to_decimal(d.get("upl", d.get("uPnl", 0)))
        return ret

    def get_margin_ratio_min_pct(self) -> Decimal:
        """获取账户最小保证金率百分比（基点，例 1200=12.00%）。
        优先使用 cfg.SIM_MARGIN_RATIO；否则读取私有 /account/positions 的 mgnRatio 最小值。
        失败时回退到 1200。
        """
        # 配置优先（用于模拟）
        try:
            sim_ratio = getattr(cfg, "SIM_MARGIN_RATIO", None)
            if sim_ratio is not None:
                return Decimal(str(sim_ratio))
        except Exception:
            pass
        # 实时读取
        try:
            data = fetch_positions(self.inst)
            vals = []
            for d in data or []:
                v = d.get("mgnRatio") or d.get("marginRatio") or d.get("marginRatioPct")
                if v is None:
                    continue
                dv = Decimal(str(v))
                # 统一换算到“基点”：12.34 -> 1234；0.1234 -> 1234
                pct_bp = (dv * Decimal("10000")).quantize(Decimal("1")) if dv <= 1 else (dv * Decimal("100")).quantize(Decimal("1"))
                vals.append(pct_bp)
            if vals:
                return min(vals)
        except Exception as e:
            logging.getLogger(__name__).warning("读取保证金率失败，使用默认：%s", e)
        return Decimal("1200")

    def live_grid_prices(self) -> List[Decimal]:
        """返回当前存活的 GRID 挂单价位（识别 tag 或 clOrdId 前缀）"""
        prices = []
        for o in fetch_open_orders(self.inst):
            tag = (o.get("tag") or o.get("clOrdId") or "").upper()
            if "GRID" in tag:
                try:
                    prices.append(to_decimal(o.get("px", 0)))
                except Exception:
                    pass
        return prices

    # ===== 下/撤单封装 =====
    def place_order(self, side: str, sz: Decimal, px: Decimal | None, reduce_only: bool, tag: str, posSide: str | None = None) -> str:
        """返回 orderId（由 okx_api 实现）"""
        # 市价单直接走 market
        if px is None:
            return place_market(self.inst, side, sz, reduce_only=reduce_only, td_mode=self.td_mode, tag=tag, posSide=posSide)

        # Post-Only 时做 maker 偏移，降低被拒概率
        px_adj = Decimal(str(px))
        try:
            tick = Decimal(str(self.mkt.spec.get("tickSz")))
        except Exception:
            tick = Decimal("0.1")

        offs = int(getattr(cfg, "MAKER_OFFSET_TICKS", 1) or 0)
        if getattr(cfg, "POST_ONLY", True) and offs > 0:
            delta = tick * offs
            if str(side).lower() == "buy":
                px_adj = px_adj - delta
            else:
                px_adj = px_adj + delta
            if px_adj <= 0:
                px_adj = tick
            # 对齐到 tick
            try:
                px_adj = round_price(px_adj, tick, side)
            except Exception:
                pass

        return place_limit(self.inst, side, sz, px_adj,
                           post_only=getattr(cfg, "POST_ONLY", True),
                           reduce_only=reduce_only, td_mode=self.td_mode, tag=tag, posSide=posSide)

    def cancel_orders_by_tag_and_side(self, tag_sub: str, posSide: str) -> int:
        """按标签+方向撤单"""
        cnt = 0
        side_l = str(posSide or "").lower()
        for o in fetch_open_orders(self.inst):
            tag = (o.get("tag") or o.get("clOrdId") or "").upper()
            if tag_sub.upper() in tag and str(o.get("posSide", "")).lower() == side_l:
                try:
                    cancel_order(self.inst, o.get("ordId"))
                    cnt += 1
                    try:
                        log_action("cancel.by_tag_side", tag=tag_sub, posSide=posSide, ord_id=o.get("ordId"), px=o.get("px"))
                    except Exception:
                        pass
                except Exception as e:
                    log.warning("撤单失败：%s", e)
        return cnt

    def cancel_orders_by_tag(self, tag_sub: str) -> int:
        """按标签撤单，返回撤单数量"""
        cnt = 0
        for o in fetch_open_orders(self.inst):
            tag = (o.get("tag") or o.get("clOrdId") or "").upper()
            if tag_sub.upper() in tag:
                try:
                    cancel_order(self.inst, o.get("ordId"))
                    cnt += 1
                    try:
                        log_action("cancel.by_tag", tag=tag_sub, ord_id=o.get("ordId"), px=o.get("px"))
                    except Exception:
                        pass
                except Exception as e:
                    log.warning("撤单失败：%s", e)
        return cnt

    def cancel_pending_by_tags(self, tags):
        """批量按标签撤单"""
        total = 0
        for t in tags:
            try:
                total += self.cancel_orders_by_tag(t)
            except Exception as e:
                log.warning("批量撤单失败(tag=%s): %s", t, e)
        return total

    def place_limit(self, tag: str, side: str, posSide: str, px, sz, post_only: bool = True, reduce_only: bool = False, td_mode: str | None = None) -> str:
        """账户层的限价单封装，保持与 strategy 调用一致。"""
        try:
            _px = Decimal(str(px)) if px is not None else None
        except Exception:
            _px = None
        _sz = Decimal(str(sz)) if sz is not None else Decimal("0")
        return place_limit(self.inst, side=side, sz=_sz, px=_px,
                           post_only=post_only, reduce_only=reduce_only, td_mode=self.td_mode, tag=tag, posSide=posSide)

    def retry_missing_orders(self):
        """针对 API 异常导致未成功挂单的记录尝试补挂"""
        now = time.time()
        if now - self._last_missing_ts < getattr(cfg, 'MISSING_RETRY_SEC', 3):
            return 0
        self._last_missing_ts = now
        if self.api_unhealthy:
            return 0
        ok = 0
        remain = []
        for od in self.missing_orders:
            jd = place_limit(self.inst, od['side'], od['sz'], od['px'],
                             post_only=getattr(cfg, "POST_ONLY", True),
                             reduce_only=od['reduce_only'], td_mode=self.td_mode, tag=od['tag'], posSide=None)
            if jd is None:
                remain.append(od)
            else:
                ok += 1
        self.missing_orders = remain
        if ok:
            log.info('缺单补挂成功 %s 笔，剩余 %s', ok, len(remain))
        return ok

    def metrics_add_fill_guess(self, side: str, sz: Decimal, px: Decimal, tag: str = "GRID"):
        """当自修复发现缺层时，粗略记录一次“可能成交”的估计，用于摘要与日终估算。"""
        try:
            notional = (Decimal(px) * Decimal(sz)).copy_abs()
        except Exception:
            notional = Decimal("0")
        # 计数
        try:
            self.metrics["fills_guess"] = int(self.metrics.get("fills_guess", 0) or 0) + 1
        except Exception:
            self.metrics = getattr(self, "metrics", {})
            self.metrics["fills_guess"] = int(self.metrics.get("fills_guess", 0) or 0) + 1
        # 名义额与费用估算（按 maker 费率）
        try:
            fee_pct = Decimal(str(getattr(cfg, "FEE_MAKER_PCT", 0.0004)))
        except Exception:
            fee_pct = Decimal("0.0004")
        self.metrics["fills_notional_usd"] = Decimal(self.metrics.get("fills_notional_usd", 0) or 0) + notional
        self.metrics["fees_est_usd"] = Decimal(self.metrics.get("fees_est_usd", 0) or 0) + notional * fee_pct
