# -*- coding: utf-8 -*-
"""网格系统（重建/挂单/撤单/自修复）
- 动态读取 cfg：grid_step_usd / grid_levels_per_side / recenter_pct 等
- 目标：
  1) 邻位触发补挂：价位A被吃→暂不补；当A的相邻一格也被吃→补回A
  2) 30分钟兜底：缺位持续≥TTL仍可补（即使邻位没缺）
  3) 频率限制：同一价位在一个TTL窗口内最多补5次
  4) 远离≥5层后返回→计数重置（优先）
  5) 某一侧从有仓到全平→无条件补齐该侧所有缺失价位
"""
from __future__ import annotations
import time
import logging
from decimal import Decimal
from typing import List, Tuple, Dict, Optional

import cfg
from cfg import round_price, align_size, to_decimal, log_action

log = logging.getLogger("GVWAP")


class Grid:
    def __init__(self, acc):
        self.acc = acc  # Account 实例，内含 mkt、inst、td_mode 等
        # 网格参数（兼容小写/大写）
        self.grid_step = Decimal(str(getattr(cfg, "grid_step_usd", getattr(cfg, "GRID_STEP_USD", 15))))
        self.levels = int(getattr(cfg, "grid_levels_per_side", getattr(cfg, "GRID_LEVELS_PER_SIDE", 10)))
        # 兜底与限流
        self.ttl_sec = int(getattr(cfg, "REARM_NEIGHBOR_TTL_SEC", 1800))  # 30分钟
        self.repost_max = int(getattr(cfg, "REPOST_MAX_PER_WINDOW", 5))
        self.far_steps = int(getattr(cfg, "REPOST_FAR_AWAY_STEPS", 5))

        # 状态
        self.center = Decimal("0")
        self.buy_lv: List[Tuple[Decimal, Decimal]] = []   # [(raw_px, size)]
        self.sell_lv: List[Tuple[Decimal, Decimal]] = []  # [(raw_px, size)]

        # 缺失→首次缺失时间戳
        self._missing_since: Dict[Decimal, float] = {}
        # 价位→(窗口起点ts, 次数)
        self._repost_count: Dict[Decimal, Tuple[float, int]] = {}
        # 平仓边沿检测
        self._last_pos_long = Decimal("0")
        self._last_pos_short = Decimal("0")
        self._last_flat_ts_long = 0.0
        self._last_flat_ts_short = 0.0

        # 统计
        self._last_live_counts = (0, 0)
        self._had_full_live = False

    # ====== 构建与挂单 ======
    def rebuild(self, center: Decimal):
        """按 center 重建价位，不落单。实际下单由 place_all/ place_missing 执行。"""
        self.center = Decimal(str(center))

        # 从 cfg 或账户读取基础张数
        def base_size(side: str) -> Decimal:
            if side == "buy":
                v = getattr(self.acc, "base_sz_long", None) or getattr(cfg, "BASE_SZ_LONG", None) or "0.01"
            else:
                v = getattr(self.acc, "base_sz_short", None) or getattr(cfg, "BASE_SZ_SHORT", None) or "0.01"
            try:
                return Decimal(str(v))
            except Exception:
                return Decimal("0.01")

        bsz = base_size("buy")
        ssz = base_size("sell")

        step = self.grid_step
        # 构建价梯（不包含 center 本身，常见做法）
        self.buy_lv = [(self.center - step * i, bsz) for i in range(1, self.levels + 1)]
        self.sell_lv = [(self.center + step * i, ssz) for i in range(1, self.levels + 1)]

        log.info("网格重建：center=%s buy[%s..%s] sell[%s..%s]",
                 self.center,
                 str(self.buy_lv[-1][0]) if self.buy_lv else "None",
                 str(self.buy_lv[0][0]) if self.buy_lv else "None",
                 str(self.sell_lv[0][0]) if self.sell_lv else "None",
                 str(self.sell_lv[-1][0]) if self.sell_lv else "None")
        log_action("grid.rebuild", center=float(self.center), step=float(step),
                   levels=int(self.levels))

    def _effective_limit_px(self, side: str, px: Decimal) -> Decimal:
        """考虑 maker 偏移与 tick 对齐，得到与实际挂单一致的“比较价位”。"""
        try:
            tick = Decimal(str(self.acc.mkt.spec.get("tickSz")))
        except Exception:
            tick = Decimal("0.1")
        offs = int(getattr(cfg, "MAKER_OFFSET_TICKS", 1) or 0)
        px_adj = Decimal(str(px))
        if getattr(cfg, "POST_ONLY", True) and offs > 0:
            delta = tick * offs
            if str(side).lower() == "buy":
                px_adj = px_adj - delta
            else:
                px_adj = px_adj + delta
            if px_adj <= 0:
                px_adj = tick
        try:
            px_adj = round_price(px_adj, tick, side)
        except Exception:
            # 兜底：直接量化到最接近tick
            q = (px_adj / tick).to_integral_value()
            px_adj = q * tick
        return px_adj

    def _live_set(self):
        return set(Decimal(str(p)) for p in self.acc.live_grid_prices())

    def _post_one(self, side: str, px: Decimal, sz: Decimal) -> Optional[str]:
        # 下单前按交易所最小单位对齐
        try:
            lot = Decimal(str(self.acc.mkt.spec.get("lotSz")))
            min_sz = Decimal(str(self.acc.mkt.spec.get("minSz")))
        except Exception:
            lot = Decimal("0.001")
            min_sz = Decimal("0.001")
        px_cmp = self._effective_limit_px(side, px)
        sz_aligned = align_size(sz, lot, min_sz)
        if sz_aligned <= 0:
            return None
        # 对齐价格到 tick，避免 51006
        px_aligned = self._effective_limit_px(side, px)
        oid = self.acc.place_order(side, sz_aligned, px_aligned, reduce_only=False, tag="GRID",
                                   posSide=("long" if side == "buy" else "short"))
        return oid

    def place_all(self):
        """按当前网格一次性补齐所有缺失价位。"""
        live = self._live_set()
        placed = 0

        # BUY
        for px, sz in self.buy_lv:
            cmpx = self._effective_limit_px("buy", px)
            if cmpx in live:
                continue
            if self._post_one("buy", px, sz):
                placed += 1
        # SELL
        for px, sz in self.sell_lv:
            cmpx = self._effective_limit_px("sell", px)
            if cmpx in live:
                continue
            if self._post_one("sell", px, sz):
                placed += 1

        if placed:
            log.info("已按当前网格挂单：buy=%d sell=%d",
                     sum(1 for px, _ in self.buy_lv if self._effective_limit_px("buy", px) in self._live_set()),
                     sum(1 for px, _ in self.sell_lv if self._effective_limit_px("sell", px) in self._live_set()))
            log_action("grid.place_all", placed=int(placed))

    # ====== 运行时自修复 ======

    def cancel_all_grid_orders(self) -> int:
        """撤掉所有网格挂单（按 tag=GRID）。返回撤单数量。"""
        try:
            n = self.acc.cancel_orders_by_tag("GRID")
            if n:
                log.info("撤掉现有网格挂单：%d 条", n)
                try:
                    log_action("grid.cancel_all", count=int(n))
                except Exception:
                    pass
            return int(n or 0)
        except Exception as e:
            log.warning("撤掉网格单失败：%s", e)
            return 0

    def _update_flat_edges(self):
        """检测从有仓→全平的边沿，用于整侧补挂"""
        try:
            pos = self.acc.get_positions()
            cur_long = to_decimal(pos.get("long", {}).get("pos"))
            cur_short = to_decimal(pos.get("short", {}).get("pos"))
        except Exception:
            return
        if self._last_pos_long > 0 and cur_long <= 0:
            self._last_flat_ts_long = time.time()
        if self._last_pos_short > 0 and cur_short <= 0:
            self._last_flat_ts_short = time.time()
        self._last_pos_long, self._last_pos_short = cur_long, cur_short

    def _faraway_reset_if_needed(self, now_px: Decimal):
        """价格相对某价位远离≥N层则重置其计数与首次缺失时间。"""
        to_clear = []
        for px_cmp, (win_ts, cnt) in self._repost_count.items():
            steps = abs((now_px - px_cmp) / self.grid_step)
            try:
                far = steps >= self.far_steps
            except Exception:
                far = False
            if far:
                to_clear.append(px_cmp)
        for px_cmp in to_clear:
            self._repost_count.pop(px_cmp, None)
            self._missing_since.pop(px_cmp, None)

    def _window_ok_and_inc(self, px_cmp: Decimal, now_ts: float) -> bool:
        win_ts, cnt = self._repost_count.get(px_cmp, (now_ts, 0))
        if now_ts - win_ts >= self.ttl_sec:
            # 新窗口
            win_ts, cnt = now_ts, 0
        if cnt >= self.repost_max:
            return False
        self._repost_count[px_cmp] = (win_ts, cnt + 1)
        return True

    def _neighbors_missing(self, ladder: List[Decimal], live_set: set, px_cmp: Decimal) -> bool:
        """相邻任一价位也缺失则返回 True。ladder 已是按价格递增的完整价梯（含买卖两侧）。"""
        try:
            i = ladder.index(px_cmp)
        except ValueError:
            return False
        cond = False
        if i - 1 >= 0:
            cond = cond or (ladder[i - 1] not in live_set)
        if i + 1 < len(ladder):
            cond = cond or (ladder[i + 1] not in live_set)
        return cond

    def _full_ladder(self) -> List[Decimal]:
        """统一价梯（递增），用于“邻位触发”。使用 effective px 以匹配 live_set。"""
        ladder = [self._effective_limit_px("buy", px) for (px, _sz) in sorted(self.buy_lv, key=lambda x: x[0])] + \
                 [self._effective_limit_px("sell", px) for (px, _sz) in sorted(self.sell_lv, key=lambda x: x[0])]
        ladder = sorted(set(ladder))
        return ladder

    def _rearm_side_all_missing(self, side: str, live_set: set) -> int:
        """无条件补齐某侧所有缺失价位（用于从有仓到全平后的一次性恢复）。"""
        placed = 0
        lv = self.buy_lv if side == "long" else self.sell_lv
        for raw_px, sz in lv:
            px_cmp = self._effective_limit_px("buy" if side == "long" else "sell", raw_px)
            if px_cmp in live_set:
                continue
            if self._post_one("buy" if side == "long" else "sell", raw_px, sz):
                self._missing_since.pop(px_cmp, None)
                placed += 1
        return placed

    def place_missing(self):
        """按规则补齐缺失价位。"""
        # 停用时尊重 pause 标志
        live_set = self._live_set()
        now_ts = time.time()
        now_px = Decimal(str(self.acc.mkt.refresh_mid()))

        # 平仓边沿检测与整侧补
        self._update_flat_edges()
        placed_total = 0
        if self._last_flat_ts_long and now_ts - self._last_flat_ts_long <= 60:
            placed_total += self._rearm_side_all_missing("long", live_set)
        if self._last_flat_ts_short and now_ts - self._last_flat_ts_short <= 60:
            placed_total += self._rearm_side_all_missing("short", live_set)
        if placed_total:
            log.info("平仓后整侧补挂：已补 %d 条", placed_total)
            log_action("rearm.side_flat", placed=int(placed_total))

        # 远离重置优先
        self._faraway_reset_if_needed(now_px)

        ladder = self._full_ladder()
        placed = 0

        # 逐侧扫描缺失
        def handle_side(side: str, levels: List[Tuple[Decimal, Decimal]]):
            nonlocal placed
            for raw_px, sz in levels:
                # 已存在则清理缺失起点
                px_cmp = self._effective_limit_px("buy" if side == "long" else "sell", raw_px)
                if px_cmp in live_set:
                    self._missing_since.pop(px_cmp, None)
                    continue

                # 节流：侧暂停时跳过——必须把 continue 放在 pop 之后
                if side == "long" and getattr(self.acc, "pause_long", False):
                    continue
                if side == "short" and getattr(self.acc, "pause_short", False):
                    continue

                # 记录首次缺失时间
                first_ts = self._missing_since.get(px_cmp)
                if not first_ts:
                    self._missing_since[px_cmp] = now_ts
                    first_ts = now_ts

                allow_by_neighbor = self._neighbors_missing(ladder, live_set, px_cmp)
                allow_by_ttl = (now_ts - first_ts >= self.ttl_sec)

                if not (allow_by_neighbor or allow_by_ttl):
                    continue

                # 频率窗口检查
                if not self._window_ok_and_inc(px_cmp, now_ts):
                    continue

                # 下单
                if self._post_one("buy" if side == "long" else "sell", raw_px, sz):
                    placed += 1

        # BUY 侧：价格低于 center 的价位
        handle_side("long", sorted(self.buy_lv, key=lambda x: x[0]))
        # SELL 侧
        handle_side("short", sorted(self.sell_lv, key=lambda x: x[0]))

        if placed:
            log.info("已补齐缺失网格：新挂出 %d 条", placed)
            log_action("rearm.done", placed=int(placed))

    # ====== 监控辅助 ======
    def side_live_counts(self) -> Tuple[int, int]:
        """返回当前仍在挂的 GRID 买/卖价位数量（与 maker 偏移一致的比较价位）。"""
        live = self._live_set()
        buy_cmp = {self._effective_limit_px('buy', px) for (px, _sz) in self.buy_lv}
        sell_cmp = {self._effective_limit_px('sell', px) for (px, _sz) in self.sell_lv}
        live_b = sum(1 for px in buy_cmp if px in live)
        live_s = sum(1 for px in sell_cmp if px in live)
        self._last_live_counts = (live_b, live_s)
        if buy_cmp and sell_cmp:
            self._had_full_live = self._had_full_live or (live_b >= len(buy_cmp) and live_s >= len(sell_cmp))
        return live_b, live_s