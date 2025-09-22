
# -*- coding: utf-8 -*-
from typing import Dict, List, Tuple
from okx_api import fetch_candles

# ---------- helpers ----------

def _fetch_kl_compat(instId: str, bar: str = "1m", limit: int = 200):
    """
    兼容老/新两种 fetch_candles 签名与两种返回类型：
    - 老：fetch_candles(instId, limit) -> list
    - 新：fetch_candles(instId, bar, limit) -> list 或 Candles(get支持)
    返回统一为 list（K线数组）
    """
    try:
        data = fetch_candles(instId, bar, limit)
    except TypeError:
        # 老签名：第二参数是 limit
        data = fetch_candles(instId, limit)
    # 兼容返回 list 或支持 .get('data')
    if isinstance(data, list):
        return data
    try:
        return data.get("data", [])  # Candles 容器或 dict
    except Exception:
        return list(data) if data is not None else []

def _to_float(x):
    try:
        return float(x)
    except Exception:
        return 0.0

def _ema(series: List[float], span: int) -> List[float]:
    if not series:
        return []
    k = 2.0 / (span + 1.0)
    out = []
    ema_val = series[0]
    for v in series:
        ema_val = v * k + ema_val * (1.0 - k)
        out.append(ema_val)
    return out

def _macd_full(prices: List[float], fast=12, slow=26, signal=9) -> Tuple[List[float], List[float], List[float]]:
    if not prices:
        return [], [], []
    ema_fast = _ema(prices, fast)
    ema_slow = _ema(prices, slow)
    macd_line = [a - b for a, b in zip(ema_fast, ema_slow)]
    sig = _ema(macd_line, signal)
    hist = [a - b for a, b in zip(macd_line, sig)]
    return macd_line, sig, hist

def _macd_last_hist(prices: List[float], fast=12, slow=26, signal=9) -> float:
    _, _, hist = _macd_full(prices, fast=fast, slow=slow, signal=signal)
    return hist[-1] if hist else 0.0

def _vwap_from_candles(candles: List[List[str]]) -> float:
    # okx candles: [ts, o, h, l, c, vol, volCcy, ...]; data is reverse chronological
    if not candles:
        return 0.0
    num = 0.0
    den = 0.0
    for k in candles:
        h = _to_float(k[2]); l = _to_float(k[3]); c = _to_float(k[4]); v = _to_float(k[5])
        tp = (h + l + c) / 3.0
        num += tp * v
        den += v
    return num / den if den > 0 else _to_float(candles[0][4])

# ---------- public signals used across codebase ----------
def vwap_signal(instId: str, limit: int = 200) -> Dict[str, float]:
    """
    返回: {'vwap': float, 'px': float, 'allow_long': bool, 'allow_short': bool}
    """
    k1 = _fetch_kl_compat(instId, "1m", limit)
    k1 = list(reversed(k1))  # old->new
    vwap_px = _vwap_from_candles(k1)
    last_px = _to_float(k1[-1][4]) if k1 else vwap_px
    return {
        "vwap": float(vwap_px),
        "px": float(last_px),
        "allow_long": bool(last_px >= vwap_px),
        "allow_short": bool(last_px <= vwap_px),
    }

def macd_multi_tf(instId: str, fast=12, slow=26, signal=9, limit: int = 200) -> Dict[str, float]:
    """
    返回: {'h1': float, 'h5': float, 'h15': float, 'bull': bool, 'bear': bool}
    bull: 三周期hist > 0; bear: 三周期hist < 0
    """
    k1 = list(reversed(_fetch_kl_compat(instId, "1m", limit)))
    k5 = list(reversed(_fetch_kl_compat(instId, "5m", limit)))
    k15 = list(reversed(_fetch_kl_compat(instId, "15m", limit)))
    c1 = [_to_float(x[4]) for x in k1]
    c5 = [_to_float(x[4]) for x in k5]
    c15 = [_to_float(x[4]) for x in k15]
    h1 = _macd_last_hist(c1, fast=fast, slow=slow, signal=signal)
    h5 = _macd_last_hist(c5, fast=fast, slow=slow, signal=signal)
    h15 = _macd_last_hist(c15, fast=fast, slow=slow, signal=signal)
    return {
        "h1": float(h1),
        "h5": float(h5),
        "h15": float(h15),
        "bull": bool(h1>0 and h5>0 and h15>0),
        "bear": bool(h1<0 and h5<0 and h15<0),
    }

def macd_multi_native(instId: str, **kwargs) -> Dict[str, float]:
    """
    与 macd_multi_tf 一致的返回，保留旧接口兼容。
    """
    return macd_multi_tf(instId, **kwargs)

def resonance(instId: str) -> Dict[str, bool]:
    """
    计算三周期 MACD + 1m VWAP + 量能共振，返回：
    {
        "bull": bool,        # VWAP 同向 + MACD(1/5/15m) 同向向上
        "bear": bool,        # VWAP 同向 + MACD(1/5/15m) 同向向下
        "vol_bull": bool,    # 近3根均量 > 近20根均量 * 1.1
        "vol_bear": bool,    # 近3根均量 < 近20根均量 * 0.9
        "vwap_px": float,
        "last_px": float
    }
    """
    # 拉取 OKX K 线（返回倒序）
    k1 = _fetch_kl_compat(instId, "1m", 200)
    k5 = _fetch_kl_compat(instId, "5m", 200)
    k15 = _fetch_kl_compat(instId, "15m", 200)
    # 转为正序（旧->新）计算
    k1 = list(reversed(k1))
    k5 = list(reversed(k5))
    k15 = list(reversed(k15))

    closes1 = [_to_float(x[4]) for x in k1]
    closes5 = [_to_float(x[4]) for x in k5]
    closes15 = [_to_float(x[4]) for x in k15]
    vols1 = [_to_float(x[5]) for x in k1]

    # MACD 三周期
    h1 = _macd_last_hist(closes1)
    h5 = _macd_last_hist(closes5)
    h15 = _macd_last_hist(closes15)

    vwap_px = _vwap_from_candles(k1)
    last_px = closes1[-1] if closes1 else vwap_px

    bull_macd = (h1 > 0 and h5 > 0 and h15 > 0)
    bear_macd = (h1 < 0 and h5 < 0 and h15 < 0)
    vwap_bull = last_px >= vwap_px
    vwap_bear = last_px <= vwap_px

    # 量能共振（样本不足时默认 False）
    if len(vols1) >= 20:
        ma20 = sum(vols1[-20:]) / 20.0
        ma3 = sum(vols1[-3:]) / 3.0 if len(vols1) >= 3 else ma20
        vol_bull = ma3 > ma20 * 1.1
        vol_bear = ma3 < ma20 * 0.9
    else:
        vol_bull = False
        vol_bear = False

    return {
        "bull": bool(bull_macd and vwap_bull),
        "bear": bool(bear_macd and vwap_bear),
        "vol_bull": bool(vol_bull),
        "vol_bear": bool(vol_bear),
        "vwap_px": float(vwap_px),
        "last_px": float(last_px),
    }

# ---------- R3: DCA 反向信号（1m 金/死叉 + VWAP 同向，且 5m/15m 不唱反调） ----------
def _macd_hist_series(prices: List[float], fast=12, slow=26, signal=9) -> List[float]:
    _, _, hist = _macd_full(prices, fast=fast, slow=slow, signal=signal)
    return hist

def _cross_last(hist: List[float]) -> str | None:
    if len(hist) < 2 or hist[-2] is None or hist[-1] is None:
        return None
    a, b = hist[-2], hist[-1]
    if a < 0 and b > 0:
        return "golden"
    if a > 0 and b < 0:
        return "death"
    return None

def dca_reverse_signal(instId: str, side: str) -> bool:
    # DCA触发：1m刚跨零 + VWAP同向 + 5m/15m同向支持
    # 新增：动量闸门 + 二次确认（跨零后等2根1m收盘仍同向）
    try:
        k1 = _fetch_kl_compat(instId, "1m", 200)
        k5 = _fetch_kl_compat(instId, "5m", 200)
        k15 = _fetch_kl_compat(instId, "15m", 200)
        k1 = list(reversed(k1)); k5 = list(reversed(k5)); k15 = list(reversed(k15))
        closes1 = [_to_float(x[4]) for x in k1]
        closes5 = [_to_float(x[4]) for x in k5]
        closes15 = [_to_float(x[4]) for x in k15]
        hist1 = _macd_hist_series(closes1)
        cross = _cross_last(hist1)
        vwap_px = _vwap_from_candles(k1)
        last_px = closes1[-1] if closes1 else vwap_px
        h5 = _macd_last_hist(closes5)
        h15 = _macd_last_hist(closes15)
        hist5 = _macd_hist_series(closes5)
        hist15 = _macd_hist_series(closes15)
        cross5 = _cross_last(hist5)
        cross15 = _cross_last(hist15)
        if side == "long":
            cond_cross = (cross == "golden")
            # 二次确认：[-3]<0 且 [-2]>0 且 [-1]>0
            cond_two = bool(len(hist1) >= 3 and hist1[-3] < 0 and hist1[-2] > 0 and hist1[-1] > 0)
            cond_vwap = (last_px >= vwap_px)
            ok_multi = ((h5 > 0 and h15 > 0) or (cross5 == "golden" and cross15 == "golden"))
        else:
            cond_cross = (cross == "death")
            # 二次确认：[-3]>0 且 [-2]<0 且 [-1]<0
            cond_two = bool(len(hist1) >= 3 and hist1[-3] > 0 and hist1[-2] < 0 and hist1[-1] < 0)
            cond_vwap = (last_px <= vwap_px)
            ok_multi = ((h5 < 0 and h15 < 0) or (cross5 == "death" and cross15 == "death"))
        # 动量闸门
        try:
            from cfg import DCA_MOMENTUM_ALPHA, DCA_MOMENTUM_WINDOW, DCA_REQUIRE_TWO_BARS
        except Exception:
            DCA_MOMENTUM_ALPHA, DCA_MOMENTUM_WINDOW, DCA_REQUIRE_TWO_BARS = 1.2, 20, True
        mom_ok = _momentum_gate(hist1, float(DCA_MOMENTUM_ALPHA), int(DCA_MOMENTUM_WINDOW))
        two_ok = (cond_two if DCA_REQUIRE_TWO_BARS else True)
        return bool(cond_cross and cond_vwap and ok_multi and mom_ok and two_ok)
    except Exception:
        return False



def _momentum_gate(hist: List[float], alpha: float, window: int) -> bool:
    if not hist:
        return False
    w = max(1, int(window))
    arr = [abs(x) for x in hist[-w:]] if len(hist) >= w else [abs(x) for x in hist]
    base = sum(arr) / float(len(arr)) if arr else 0.0
    cur = abs(hist[-1])
    if base <= 0:
        return False
    return bool(cur >= alpha * base)

def _two_bar_same_side(hist: List[float], side: str) -> bool:
    if len(hist) < 2:
        return False
    if side == "long":
        return bool(hist[-1] > 0 and hist[-2] > 0)
    else:
        return bool(hist[-1] < 0 and hist[-2] < 0)

def trend_filters_ok(instId: str, side: str) -> bool:
    # 用于趋势加仓的动量闸门 + 二次确认（1m MACD直方图）
    # - 动量：|hist[-1]| ≥ α × mean(|hist[-N:]|)
    # - 二次确认：最近2根1m直方图同向
    # 参数来自 cfg：TREND_MOMENTUM_ALPHA, TREND_MOMENTUM_WINDOW, TREND_REQUIRE_TWO_BARS
    try:
        from cfg import TREND_MOMENTUM_ALPHA, TREND_MOMENTUM_WINDOW, TREND_REQUIRE_TWO_BARS
    except Exception:
        TREND_MOMENTUM_ALPHA, TREND_MOMENTUM_WINDOW, TREND_REQUIRE_TWO_BARS = 1.2, 20, True
    k1 = _fetch_kl_compat(instId, "1m", 200)
    k1 = list(reversed(k1))
    closes1 = [_to_float(x[4]) for x in k1]
    hist1 = _macd_hist_series(closes1)
    mom_ok = _momentum_gate(hist1, float(TREND_MOMENTUM_ALPHA), int(TREND_MOMENTUM_WINDOW))
    if not mom_ok:
        return False
    if TREND_REQUIRE_TWO_BARS:
        if not _two_bar_same_side(hist1, side):
            return False
    return True
