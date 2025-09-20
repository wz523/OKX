# -*- coding: utf-8 -*-
"""网格系统（重建/挂单/撤单/自修复）
- 运行时从 cfg 动态读取：grid_step_usd / grid_levels_per_side / recenter_pct
- 自修复日志只在“有实际补齐”时打印，避免刷屏
"""
from __future__ import annotations
import time
import logging
from decimal import Decimal
from typing import List, Tuple

import cfg
from cfg import round_price, align_size, to_decimal, log_action

log = logging.getLogger("GVWAP")


class Grid:
    def __init__(self, acc):
        self.acc = acc
        self.center: Decimal = Decimal("0")
        self.buy_lv: List[Tuple[Decimal, Decimal]] = []   # [(px, sz)]
        self.sell_lv: List[Tuple[Decimal, Decimal]] = []  # [(px, sz)]
        self._last_autorepair_ts = 0.0
        self._last_flat_ts = {"long": 0.0, "short": 0.0}
        self._prev_pos = {"long": 0, "short": 0}
        # 只吃一次 + 扩展状态
        self._posted_buy = set(); self._posted_sell = set()
        self._consumed_buy = set(); self._consumed_sell = set()
        # 邻位补挂状态
        self._recent_submit = {}
        self._repost_count = {}

    # ===== 建网格，仅生成价位，不下单 =====
    def rebuild(self, center: Decimal):
        self.center = to_decimal(center)
        self.buy_lv.clear()
        self.sell_lv.clear()

        step = to_decimal(getattr(cfg, "GRID_STEP_USD", 1))
        levels = int(getattr(cfg, "GRID_LEVELS_PER_SIDE", 10) or 0)
        tick = Decimal(str(self.acc.mkt.spec.get("tickSz")))
        minSz = to_decimal(self.acc.mkt.spec.get("minSz", "0"))
        # 张数来自账户（可热更新）
        base_buy = to_decimal(getattr(self.acc, "base_sz_long", 0))
        base_sell = to_decimal(getattr(self.acc, "base_sz_short", 0))

        for i in range(1, levels + 1):
            px_b = round_price(self.center - step * i, tick, "buy")
            px_s = round_price(self.center + step * i, tick, "sell")
            if base_buy >= minSz:
                self.buy_lv.append((px_b, base_buy))
            if base_sell >= minSz:
                self.sell_lv.append((px_s, base_sell))

        log_action("rebuild.done", center=float(self.center), buy=len(self.buy_lv), sell=len(self.sell_lv))

    # ===== 首轮全部挂出网格单 =====
    def place_all(self):
        live_set = set(self.acc.live_grid_prices())
        buy_placed = 0
        sell_placed = 0

        for px, sz in self.buy_lv:
            if px in live_set:
                continue
            if getattr(self.acc, 'pause_long', False):
                continue
            self.acc.place_order("buy", sz, px=px, reduce_only=False, tag="GRID")
            self._posted_buy.add(px)
            buy_placed += 1

        for px, sz in self.sell_lv:
            if px in live_set:
                continue
            if getattr(self.acc, 'pause_short', False):
                continue
            self.acc.place_order("sell", sz, px=px, reduce_only=False, tag="GRID")
            self._posted_sell.add(px)
            sell_placed += 1

        log.info(f"已按当前网格挂单：buy={buy_placed} sell={sell_placed}")
        log_action("grid.place_all", buy=int(buy_placed), sell=int(sell_placed))

    def cancel_all_grid_orders(self):
        cnt = self.acc.cancel_orders_by_tag("GRID")
        log.info("已撤销全部网格挂单")
        return cnt

    def place_missing(self):
        """补齐缺失的网格价位。
        仅当当前价位和更外层价位都被吃掉时再补。每价位最多补 3 次，并对短时间重复发现进行去重（TTL）。
        """
        try:
            live_set = set(self.acc.live_grid_prices())
            ttl = int(getattr(cfg, "REPAIR_DEDUP_TTL_SEC", 20) or 0)
            now = time.time()
            placed = 0
            step = to_decimal(getattr(cfg, "GRID_STEP_USD", 1))

            # BUY side
            for px, sz in self.buy_lv:
                if px in live_set:
                    continue  # 此价位仍有挂单
                outer = px - step  # 更外层
                has_outer = any(abs(outer - pxx) < getattr(cfg, "EPS", 1e-6) for pxx, _ in self.buy_lv)
                if has_outer and (outer in live_set):
                    continue  # 外层仍在 → 不补该价
                if self._repost_count.get(px, 0) >= 3:
                    continue
                last = self._recent_submit.get(px)
                if last and now - last < ttl:
                    continue
                if getattr(self.acc, 'pause_long', False):
                    continue
                self.acc.place_order("buy", sz, px=px, reduce_only=False, tag="GRID")
                self._recent_submit[px] = now
                self._repost_count[px] = self._repost_count.get(px, 0) + 1
                placed += 1

            # SELL side
            for px, sz in self.sell_lv:
                if px in live_set:
                    continue
                outer = px + step
                has_outer = any(abs(outer - pxx) < getattr(cfg, "EPS", 1e-6) for pxx, _ in self.sell_lv)
                if has_outer and (outer in live_set):
                    continue
                if self._repost_count.get(px, 0) >= 3:
                    continue
                last = self._recent_submit.get(px)
                if last and now - last < ttl:
                    continue
                if getattr(self.acc, 'pause_short', False):
                    continue
                self.acc.place_order("sell", sz, px=px, reduce_only=False, tag="GRID")
                self._recent_submit[px] = now
                self._repost_count[px] = self._repost_count.get(px, 0) + 1
                placed += 1

            if placed:
                # 清理过期标记
                for k in list(self._recent_submit.keys()):
                    if now - self._recent_submit[k] > 600:
                        self._recent_submit.pop(k, None)
                log.info("已补齐缺失网格：新挂出 %d 条", placed)
                log_action("rearm.done", placed=int(placed))
        except Exception as e:
            log.warning("补齐缺失失败：%s", e)

    # ===== 自修复（已禁用，保留接口） =====
    def side_live_counts(self):
        live = set(self.acc.live_grid_prices())
        buy_live = sum(1 for px, _ in self.buy_lv if px in live)
        sell_live = sum(1 for px, _ in self.sell_lv if px in live)
        return buy_live, sell_live

    def autorepair_if_needed(self):
        """禁用自动自修复。改用邻位补挂 place_missing() 触发。"""
        return
