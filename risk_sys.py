# -*- coding: utf-8 -*-
"""行情与信号
- 信号节流：SIGNAL_REFRESH_SEC；心跳打印受 cfg.SIGNAL_PRINT_MODE 控制
- 动态读取 cfg：CANDLE_LIMIT / SIGNAL_PRINT_MODE / SIGNAL_HEARTBEAT_EVERY
- 方向判定：LONG 需 px ≥ VWAP 且 MACD(5m/15m) 同时看多；SHORT 需 px ≤ VWAP 且 MACD 同时看空
- change_only：仅在 allow_long/allow_short 状态变化时打印一次（止住刷屏）
"""
from __future__ import annotations
import time
import logging
from decimal import Decimal
from typing import Dict

import cfg
from cfg import to_decimal
from indicators import vwap_signal, macd_multi_tf, resonance
from okx_api import fetch_candles

log = logging.getLogger("GVWAP")

class MarginGuard:
    """保证金风控：
    - 定期刷新最小保证金率（百分比）
    - 低于 STOP → paused=True，并撤掉未成交的 ADD/DCA 挂单
    - 高于 RESUME → 解除暂停
    """
    def __init__(self, acc, grid):
        self.acc = acc
        self.grid = grid
        # 让 Account 能拿到 guard（用于 place_order 拦截 ADD/DCA）
        setattr(self.acc, "_risk_guard", self)
        self.paused = False
        self._last_ts = 0.0
        self._last_ratio = None

    def refresh(self):
        now = time.time()
        if now - self._last_ts < max(1, int(getattr(cfg, "MARGIN_CHECK_SEC", 5))):
            return
        self._last_ts = now
        ratio = self.acc.get_margin_ratio_min_pct()
        self._last_ratio = ratio
        stop_th = getattr(cfg, "MARGIN_STOP_PCT", Decimal("1000"))
        resume_th = getattr(cfg, "MARGIN_RESUME_PCT", Decimal("1200"))
        if ratio <= stop_th:
            if not self.paused:
                self.paused = True
                log.error("[风控触发] 保证金率=%s%% ≤ STOP=%s%% → 暂停加仓/趋势/DCA", ratio, stop_th)
            # 撤掉未成交的 GRID/DCA/ADD 挂单（确保反复调用也安全）
            try:
                self.acc.cancel_pending_by_tags(["GRID", "DCA", "ADD"])
            except Exception as e:
                log.warning("风控撤单异常：%s", e)
        elif self.paused and ratio >= resume_th:
            self.paused = False
            log.warning("[风控恢复] 保证金率=%s%% ≥ RESUME=%s%% → 恢复策略加仓/趋势/DCA", ratio, resume_th)

