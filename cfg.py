# -*- coding: utf-8 -*-
"""
配置与工具（JSONC 热更新 + 全局参数 + 常用工具函数）
- 更稳的 JSONC 路径解析 + 启动时打印“实际使用的配置文件路径”
- 其它模块仅需 `import cfg`，并以 `cfg.X` 动态读取（避免热更新后常量被拷贝）
"""
from __future__ import annotations
import os
import re
import json
import logging
from decimal import Decimal, getcontext, ROUND_DOWN, ROUND_UP
from typing import Any, Dict

# 动作级 DEBUG 日志（ACT JSON）——写入文件日志（DEBUG 级），控制台保持简洁
import json, time, logging
from decimal import Decimal
USE_SYSTEM_PROXY = True  # 默认跟随系统代理

PROXY_BYPASS_OKX = False  # True=对 okx.com 直连

import os
EPS = 1e-08  # 价格容差兜底

PROXY_OVERRIDE = os.environ.get("GVWAP_PROXY", "").lower()  # on/off/auto/""

def proxy_enabled():
    v = PROXY_OVERRIDE
    if v == "on": return True
    if v == "off": return False
    return bool(USE_SYSTEM_PROXY)

MISSING_RETRY_SEC = 3  # 缺单重试间隔秒

API_UNHEALTHY_AFTER = 3  # 连续失败多少次标记为不健康

def log_action(event: str, **kwargs):
    try:
        rec = {"t": int(time.time()), "event": event}
        for k, v in kwargs.items():
            try:
                if isinstance(v, Decimal):
                    rec[k] = str(v)
                else:
                    rec[k] = v
            except Exception:
                rec[k] = str(v)
        logging.getLogger("GVWAP").debug("ACT %s", json.dumps(rec, ensure_ascii=False, default=str))
    except Exception as e:
        logging.getLogger("GVWAP").debug("ACT encode_error %r", e)


getcontext().prec = 28
log = logging.getLogger("GVWAP")

# ========== 常用工具函数 ==========

def to_decimal(x: Any) -> Decimal:
    try:
        if x is None:
            return Decimal("0")
        s = str(x).strip()
        if s == "" or s.lower() in ("none", "nan", "null"):
            return Decimal("0")
        return Decimal(s)
    except Exception:
        return Decimal("0")


def round_price(price: Decimal, tickSz: Decimal, side: str) -> Decimal:
    if side == "buy":
        return (price / tickSz).to_integral_value(rounding=ROUND_DOWN) * tickSz
    return (price / tickSz).to_integral_value(rounding=ROUND_UP) * tickSz


def align_size(size: Decimal, lotSz: Decimal, minSz: Decimal) -> Decimal:
    if size < minSz:
        size = minSz
    n = (size / lotSz).to_integral_value(rounding=ROUND_UP)
    return n * lotSz


# ========== JSONC 读取（稳路径 + 打印路径） ==========
PROJ_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_TUNING_BASENAME = "tuning_gvwap.jsonc"
_ENV_TUNING = os.getenv("TUNING_FILE", "").strip()
_LOGGED_TUNING_PATH = False  # 仅首次打印一次路径


def _resolve_tuning_file() -> str:
    candidates = []
    if _ENV_TUNING:
        candidates.append(_ENV_TUNING)
    candidates.append(os.path.join(PROJ_DIR, DEFAULT_TUNING_BASENAME))
    candidates.append(os.path.join(os.getcwd(), DEFAULT_TUNING_BASENAME))
    for p in candidates:
        if p and os.path.exists(p):
            return os.path.abspath(p)
    return os.path.join(PROJ_DIR, DEFAULT_TUNING_BASENAME)


TUNING_FILE = _resolve_tuning_file()


def load_jsonc(path: str = None) -> Dict[str, Any]:
    """读取 JSONC：支持 // 与 /* */ 注释；读取失败返回 {}。"""
    global _LOGGED_TUNING_PATH
    path = path or TUNING_FILE
    if not os.path.isabs(path):
        path = os.path.join(PROJ_DIR, path)
    if not _LOGGED_TUNING_PATH:
        log.info("调参文件路径: %s", path)
        _LOGGED_TUNING_PATH = True
    if not os.path.exists(path):
        log.warning("未找到调参文件：%s（将使用 cfg.py 默认值）", path)
        return {}
    txt = open(path, "r", encoding="utf-8").read()
    # 去注释
    txt = re.sub(r"/\*.*?\*/", "", txt, flags=re.S)
    txt = re.sub(r"(^|\s)//.*?$", "", txt, flags=re.M)
    try:
        return json.loads(txt)
    except Exception as e:
        log.error("解析 JSONC 失败：%s（请检查逗号/括号/数字格式）", e)
        return {}


# ========== 全局可热改参数（默认值；tuning_gvwap.jsonc 可覆盖） ==========
# --- 网格（按名义 USDT 定量） ---
USE_TARGET_NOTIONAL = True
TARGET_NOTIONAL_USD_LONG = Decimal("50")
TARGET_NOTIONAL_USD_SHORT = Decimal("50")
BASE_MULTIPLIER_LONG = 1
BASE_MULTIPLIER_SHORT = 1

# --- 趋势加仓 ---
# 当 MACD+VWAP 共振信号出现时，可在盈利方向加仓。
# TREND_NOTIONAL_USD 定义每次趋势加仓的名义美元。TREND_DAILY_CAP 可限制每日次数（默认不限）。
TREND_NOTIONAL_USD = Decimal("8")
TREND_DAILY_CAP = 5   # 0 表示不限制每日次数# 0 表示不限制每日次数
TREND_COOLDOWN_SEC = 180  # 信号之间无冷却# 信号之间无冷却
TREND_MAX_NOTIONAL_USD = Decimal("0")  # 不限制总趋势名义

TREND_MIN_DISTANCE_PCT = Decimal("0.008")  # 每次趋势加仓与上一次的最小价格距离比例

# 趋势动量与二次确认
TREND_MOMENTUM_ALPHA = Decimal("1.2")   # 1m MACD直方图强度需≥近N根平均的α倍
TREND_MOMENTUM_WINDOW = 20              # N 根
TREND_REQUIRE_TWO_BARS = True           # 需要最近2根1m直方图同向
# 保留旧字段以兼容历史读取，但不再使用
TREND_USE_TARGET = True
TREND_TARGET_USD_LONG = TREND_NOTIONAL_USD
TREND_TARGET_USD_SHORT = TREND_NOTIONAL_USD
TREND_MULTIPLIER_LONG = 1
TREND_MULTIPLIER_SHORT = 1

# --- 风险档位（统一缩放） ---
RISK_PROFILE = "balanced"  # light / balanced / aggressive
RISK_SCALE_GRID = Decimal("1.0")
RISK_SCALE_TREND = Decimal("1.0")

# --- 网格形态 ---
GRID_STEP_USD = Decimal("15")
GRID_LEVELS_PER_SIDE = 10
RECENTER_PCT = Decimal("0.05")
POST_ONLY = True

ALLOW_TAKER_ON_BAND = True  # 51006 价带冲突时，降级为市价(IOC)以保证连贯

MAKER_OFFSET_TICKS = 1

TREND_REQUIRE_PROFIT = True  # 趋势加仓是否要求当前方向浮盈>0

# --- 盈利/止盈 ---
# 新版止盈参数：当浮盈达到 TP_BASE_USD 时启动止盈逻辑。
# 无趋势信号时全平；有趋势信号时先按 TP_PARTIAL_RATIO 平仓，再根据回撤阈值
# (TP_TRAIL_PCT 或 TP_TRAIL_USD) 追踪止盈。
TP_BASE_USD = Decimal("0.5")       # 起步止盈触发点（美元）
TP_PARTIAL_RATIO = Decimal("0.3")   # 有趋势时第一次止盈平仓比例
TP_TRAIL_USD = Decimal("0.5")       # 追踪止盈的最低金额回撤阈值
TP_TRAIL_PCT = Decimal("0.007")     # 追踪止盈的百分比回撤阈值
# 保留旧的止盈变量以兼容其它模块，但不再使用
SCALP_TRIGGER_USD = TP_BASE_USD
PARTIAL_TP_RATIO = TP_PARTIAL_RATIO
TRAIL_CALLBACK_PCT = TP_TRAIL_PCT
TRAIL_CALLBACK_USD_FLOOR = TP_TRAIL_USD
USE_CLOSE_POSITION_FOR_FLAT = False

# --- 打印/日志 ---
SIGNAL_PRINT_MODE = "change_only"   # change_only / heartbeat / verbose
SIGNAL_HEARTBEAT_EVERY = 10
POSITION_LEDGER_ENABLE = True
CONFIRM_FILL_TIMEOUT_MS = 4000

# --- DCA（亏损方向加仓） ---
# 当亏损区间在 DCA_MIN_PCT~DCA_MAX_PCT 之间时，启动 DCA 窗口。
# DCA_FIXED_NOTIONAL_USD 定义每次加仓的名义美元，DCA_TOTAL_CAP 控制单侧最多加仓次数。
# DCA 动量与二次确认
DCA_MOMENTUM_ALPHA = Decimal("1.2")
DCA_MOMENTUM_WINDOW = 20
DCA_REQUIRE_TWO_BARS = True

DCA_ENABLE = True
# 未使用 DCA_MIN_DIST_TO_LIQ_PCT（旧逻辑）
DCA_MIN_PCT = Decimal("0.08")      # 启动 DCA 的最小亏损百分比
DCA_MAX_PCT = Decimal("0.24")      # 启动 DCA 的最大亏损百分比
DCA_FIXED_NOTIONAL_USD = Decimal("8")  # 每次DCA加仓名义美元
DCA_TOTAL_CAP = 4                 # 每个方向最多加仓次数
# 保留旧字段以兼容历史读取
DCA_MIN_DIST_TO_LIQ_PCT = Decimal("10.0")
DCA_SLOTS = []


# --- 保证金率风控 ---
MARGIN_STOP_PCT = Decimal("1000")
MARGIN_RESUME_PCT = Decimal("1200")
MARGIN_CHECK_SEC = 5

# --- 时序 ---
CANDLE_LIMIT = 300
SIGNAL_REFRESH_SEC = 30
TICK_REFRESH_SEC = 3

# --- 新增：外部操作兼容/自恢复 ---
GRID_AUTOREPAIR = True               # 自动补齐缺失网格
AUTOREPAIR_INTERVAL_SEC = 30         # 补齐检查频率（秒）
EXTERNAL_ADD_RECONCILE = True        # 识别外部加仓，同步趋势并发


# --- 离位重装 + 次数上限（互斥于 GRID_ONE_SHOT） ---
REARM_ENABLED = False              # 已弃用：邻位补挂逻辑在 grid_sys.py 内实现
REARM_HYSTERESIS_STEPS = Decimal("0")
REARM_USD_FLOOR = Decimal("0")
REARM_ALLOW_REPEATS = 0
REARM_RESET_DISTANCE_LEVELS = 0
REARM_WINDOW_SEC = 0
REARM_COOLDOWN_SEC = 0
REARM_COUNT_THRESHOLD_RATIO = Decimal("1.0")

# --- 网格重装/扩展控制 ---
GRID_ONE_SHOT = False                 # 每个价位只吃一次；不再重装
RECONCILE_TOLERANCE_RATIO = Decimal("0.2")  # 容差：与趋势标准张数的比值差≤20%


# ========== 应用 JSONC（被策略循环调用） ==========

def apply_tuning(cfg: Dict[str, Any], mkt, acc, grid, guard):
    """把 cfg 的键覆盖到全局变量，并做必要的副作用（重建网格/重算尺寸）"""
    global GRID_ONE_SHOT, GRID_EXTEND_MAX_LEVELS, GRID_EXTEND_TTL_SEC
    global LOG_DIR, LOG_TO_FILE, LOG_JSON, LOG_ROTATE, LOG_BACKUP_DAYS, LOG_LEVEL, LOG_HEARTBEAT_SEC, LOG_SUMMARY_EVERY_SEC, LOG_CONSOLE_DAILY_SUMMARY, LOG_SUMMARY_TO_CSV, FEE_MAKER_PCT, FEE_TAKER_PCT
    global REARM_ENABLED, REARM_HYSTERESIS_STEPS, REARM_USD_FLOOR, REARM_ALLOW_REPEATS, REARM_RESET_DISTANCE_LEVELS, REARM_WINDOW_SEC, REARM_COOLDOWN_SEC, REARM_COUNT_THRESHOLD_RATIO
    global USE_TARGET_NOTIONAL, TARGET_NOTIONAL_USD_LONG, TARGET_NOTIONAL_USD_SHORT
    global BASE_MULTIPLIER_LONG, BASE_MULTIPLIER_SHORT
    global TREND_USE_TARGET, TREND_TARGET_USD_LONG, TREND_TARGET_USD_SHORT, TREND_MULTIPLIER_LONG, TREND_MULTIPLIER_SHORT, TREND_REQUIRE_PROFIT
    global TREND_COOLDOWN_SEC, TREND_DAILY_CAP, TREND_MAX_NOTIONAL_USD, TREND_MIN_DISTANCE_PCT
    global GRID_STEP_USD, GRID_LEVELS_PER_SIDE, RECENTER_PCT, POST_ONLY, ALLOW_TAKER_ON_BAND
    global SIGNAL_PRINT_MODE, SIGNAL_HEARTBEAT_EVERY, POSITION_LEDGER_ENABLE, CONFIRM_FILL_TIMEOUT_MS
    global SCALP_TRIGGER_USD, PARTIAL_TP_RATIO, TRAIL_CALLBACK_PCT, TRAIL_CALLBACK_USD_FLOOR, USE_CLOSE_POSITION_FOR_FLAT
    global DCA_ENABLE, DCA_MIN_DIST_TO_LIQ_PCT, DCA_MIN_PCT, DCA_MAX_PCT, DCA_SLOTS, DCA_TOTAL_CAP
    global MARGIN_STOP_PCT, MARGIN_RESUME_PCT, MARGIN_CHECK_SEC
    global RISK_PROFILE, RISK_SCALE_GRID, RISK_SCALE_TREND
    global GRID_AUTOREPAIR, AUTOREPAIR_INTERVAL_SEC, EXTERNAL_ADD_RECONCILE, RECONCILE_TOLERANCE_RATIO, REARM_ON_FLAT_IMMEDIATE, FLAT_IMMEDIATE_TTL_SEC

    # 名义/乘数
    globals()["REARM_ON_FLAT_IMMEDIATE"] = bool(cfg.get("rearm_on_flat_immediate", REARM_ON_FLAT_IMMEDIATE))
    globals()["FLAT_IMMEDIATE_TTL_SEC"] = int(cfg.get("flat_immediate_ttl_sec", FLAT_IMMEDIATE_TTL_SEC))
    USE_TARGET_NOTIONAL = bool(cfg.get("use_target_notional", USE_TARGET_NOTIONAL))
    TARGET_NOTIONAL_USD_LONG = to_decimal(cfg.get("target_notional_usd_long", TARGET_NOTIONAL_USD_LONG))
    TARGET_NOTIONAL_USD_SHORT = to_decimal(cfg.get("target_notional_usd_short", TARGET_NOTIONAL_USD_SHORT))
    BASE_MULTIPLIER_LONG = int(cfg.get("base_multiplier_long", BASE_MULTIPLIER_LONG))
    BASE_MULTIPLIER_SHORT = int(cfg.get("base_multiplier_short", BASE_MULTIPLIER_SHORT))

    # 趋势
    # 趋势加仓参数
    TREND_NOTIONAL_USD = to_decimal(cfg.get("trend_notional_usd", TREND_NOTIONAL_USD))
    TREND_DAILY_CAP = int(cfg.get("trend_daily_cap", TREND_DAILY_CAP))
    TREND_COOLDOWN_SEC = int(cfg.get("trend_cooldown_sec", TREND_COOLDOWN_SEC))
    TREND_MAX_NOTIONAL_USD = to_decimal(cfg.get("trend_max_notional_usd", TREND_MAX_NOTIONAL_USD))
    TREND_MIN_DISTANCE_PCT = to_decimal(cfg.get("trend_min_distance_pct", TREND_MIN_DISTANCE_PCT))
    # 保留旧字段赋值，避免兼容问题
    TREND_USE_TARGET = True
    TREND_TARGET_USD_LONG = TREND_NOTIONAL_USD
    TREND_TARGET_USD_SHORT = TREND_NOTIONAL_USD
    TREND_MULTIPLIER_LONG = 1
    TREND_MULTIPLIER_SHORT = 1

    # 风险档位
    rp = str(cfg.get("risk_profile", RISK_PROFILE)).lower().strip()
    if rp in ("light", "balanced", "aggressive"):
        RISK_PROFILE = rp
    profile_map = {"light": Decimal("0.5"), "balanced": Decimal("1.0"), "aggressive": Decimal("1.5")}
    grid_scale_default = profile_map.get(RISK_PROFILE, Decimal("1.0"))
    trend_scale_default = grid_scale_default
    RISK_SCALE_GRID = to_decimal(cfg.get("risk_scale_grid", grid_scale_default))
    RISK_SCALE_TREND = to_decimal(cfg.get("risk_scale_trend", trend_scale_default))

    # 网格形态
    old_step = GRID_STEP_USD; old_levels = GRID_LEVELS_PER_SIDE
    GRID_STEP_USD = to_decimal(cfg.get("grid_step_usd", GRID_STEP_USD))
    GRID_LEVELS_PER_SIDE = int(cfg.get("grid_levels_per_side", GRID_LEVELS_PER_SIDE))
    RECENTER_PCT = to_decimal(cfg.get("recenter_pct", RECENTER_PCT))
    POST_ONLY = bool(cfg.get("post_only", POST_ONLY))

    # 打印/日志
    SIGNAL_PRINT_MODE = cfg.get("signal_print_mode", SIGNAL_PRINT_MODE)
    SIGNAL_HEARTBEAT_EVERY = int(cfg.get("signal_heartbeat_every", SIGNAL_HEARTBEAT_EVERY))
    POSITION_LEDGER_ENABLE = bool(cfg.get("position_ledger_enable", POSITION_LEDGER_ENABLE))
    CONFIRM_FILL_TIMEOUT_MS = int(cfg.get("confirm_fill_timeout_ms", CONFIRM_FILL_TIMEOUT_MS))

    # 盈利/止盈
    # 新版止盈参数
    TP_BASE_USD = to_decimal(cfg.get("tp_base_usd", TP_BASE_USD))
    TP_PARTIAL_RATIO = to_decimal(cfg.get("tp_partial_ratio", TP_PARTIAL_RATIO))
    TP_TRAIL_PCT = to_decimal(cfg.get("tp_trail_pct", TP_TRAIL_PCT))
    TP_TRAIL_USD = to_decimal(cfg.get("tp_trail_usd", TP_TRAIL_USD))
    # 保留旧字段赋值，避免报错
    SCALP_TRIGGER_USD = TP_BASE_USD
    PARTIAL_TP_RATIO = TP_PARTIAL_RATIO
    TRAIL_CALLBACK_PCT = TP_TRAIL_PCT
    TRAIL_CALLBACK_USD_FLOOR = TP_TRAIL_USD
    USE_CLOSE_POSITION_FOR_FLAT = bool(cfg.get("use_close_position_for_flat", USE_CLOSE_POSITION_FOR_FLAT))
    ALLOW_TAKER_ON_BAND = bool(cfg.get("allow_taker_on_band", ALLOW_TAKER_ON_BAND))

    # DCA
    DCA_ENABLE = bool(cfg.get("dca_enable", DCA_ENABLE))
    DCA_MIN_PCT = to_decimal(cfg.get("dca_min_pct", DCA_MIN_PCT))
    DCA_MAX_PCT = to_decimal(cfg.get("dca_max_pct", DCA_MAX_PCT))
    DCA_FIXED_NOTIONAL_USD = to_decimal(cfg.get("dca_fixed_notional_usd", DCA_FIXED_NOTIONAL_USD))
    DCA_TOTAL_CAP = int(cfg.get("dca_total_cap", DCA_TOTAL_CAP))

    # 风控
    MARGIN_STOP_PCT = to_decimal(cfg.get("margin_stop_pct", MARGIN_STOP_PCT))
    MARGIN_RESUME_PCT = to_decimal(cfg.get("margin_resume_pct", MARGIN_RESUME_PCT))
    MARGIN_CHECK_SEC = int(cfg.get("margin_check_sec", MARGIN_CHECK_SEC))

    # 新增：外部操作兼容/自恢复
    GRID_AUTOREPAIR = bool(cfg.get("grid_autorepair", GRID_AUTOREPAIR))
    AUTOREPAIR_INTERVAL_SEC = int(cfg.get("autorepair_interval_sec", AUTOREPAIR_INTERVAL_SEC))
    EXTERNAL_ADD_RECONCILE = bool(cfg.get("external_add_reconcile", EXTERNAL_ADD_RECONCILE))
    RECONCILE_TOLERANCE_RATIO = to_decimal(cfg.get("reconcile_tolerance_ratio", RECONCILE_TOLERANCE_RATIO))
    # 网格重装/扩展控制（热更新）
    GRID_ONE_SHOT = bool(cfg.get("grid_one_shot", GRID_ONE_SHOT))
    # 离位重装 + 次数上限（热更新）
    REARM_ENABLED = bool(cfg.get("rearm_enabled", REARM_ENABLED))
    REARM_HYSTERESIS_STEPS = to_decimal(cfg.get("rearm_hysteresis_steps", REARM_HYSTERESIS_STEPS))
    REARM_USD_FLOOR = to_decimal(cfg.get("rearm_usd_floor", REARM_USD_FLOOR))
    REARM_ALLOW_REPEATS = int(cfg.get("rearm_allow_repeats", REARM_ALLOW_REPEATS))
    REARM_RESET_DISTANCE_LEVELS = int(cfg.get("rearm_reset_distance_levels", REARM_RESET_DISTANCE_LEVELS))
    REARM_WINDOW_SEC = int(cfg.get("rearm_window_sec", REARM_WINDOW_SEC))
    REARM_COOLDOWN_SEC = int(cfg.get("rearm_cooldown_sec", REARM_COOLDOWN_SEC))
    REARM_COUNT_THRESHOLD_RATIO = to_decimal(cfg.get("rearm_count_threshold_ratio", REARM_COUNT_THRESHOLD_RATIO))



    # 若步长或层数变化 → 撤旧并重建
    if (GRID_STEP_USD != old_step) or (GRID_LEVELS_PER_SIDE != old_levels):
        try:
            grid.cancel_all_grid_orders()
        except Exception:
            pass
        if mkt.mid > 0:
            grid.rebuild(mkt.mid)
            if hasattr(guard, "paused") and not guard.paused:
                grid.place_all()
        log.info("调参生效：grid_step_usd=%s → %s, grid_levels=%s → %s, recenter_pct=%s",
                 old_step, GRID_STEP_USD, old_levels, GRID_LEVELS_PER_SIDE, RECENTER_PCT)

    # 重算尺寸（把“基础张数”日志降到 DEBUG，避免刷屏）
    _load_sizes_with_risk(mkt, acc)

def _load_sizes_with_risk(mkt, acc):
    """按当前价格、ctVal/lotSz/minSz，把名义金额换算成张数，并应用“风险缩放”。"""
    lotSz = mkt.spec.get("lotSz", Decimal("0")); minSz = mkt.spec.get("minSz", Decimal("0")); ctVal = mkt.spec.get("ctVal", Decimal("0.1"))
    px = mkt.mid if mkt.mid > 0 else Decimal("0")

    def from_usd(target: Decimal) -> Decimal:
        if px <= 0 or target <= 0:
            return align_size(lotSz * Decimal(max(1, BASE_MULTIPLIER_LONG)), lotSz, minSz)
        n = target / (ctVal * to_decimal(px))
        return align_size(n, lotSz, minSz)

    # --- 按最新逻辑计算基础/趋势张数 ---
    # 网格基础名义：固定 10U，每侧均使用 TARGET_NOTIONAL_USD_LONG/SHORT；应用风险缩放
    grid_long_usd = TARGET_NOTIONAL_USD_LONG * RISK_SCALE_GRID
    grid_short_usd = TARGET_NOTIONAL_USD_SHORT * RISK_SCALE_GRID
    # 趋势加仓名义：固定 TREND_NOTIONAL_USD；应用风险缩放（若启用）
    trend_long_usd = TREND_NOTIONAL_USD * RISK_SCALE_TREND
    trend_short_usd = TREND_NOTIONAL_USD * RISK_SCALE_TREND

    # 计算基础张数（网格）
    acc.base_sz_long = from_usd(grid_long_usd)
    acc.base_sz_short = from_usd(grid_short_usd)
    # 计算趋势加仓张数
    acc.trend_sz_long = from_usd(trend_long_usd)
    acc.trend_sz_short = from_usd(trend_short_usd)

    # ↓ 改为 DEBUG，避免刷屏
    log.debug("基础张数：grid(L/S)=(%s/%s) trend(L/S)=(%s/%s) | 风险缩放 grid=%.2f trend=%.2f",
              acc.base_sz_long, acc.base_sz_short, acc.trend_sz_long, acc.trend_sz_short,
              float(RISK_SCALE_GRID), float(RISK_SCALE_TREND))

# 平仓后立即重挂（方案B开关，默认True）
REARM_ON_FLAT_IMMEDIATE = True
FLAT_IMMEDIATE_TTL_SEC = 30  # 平仓后“分侧”立即补挂的时间窗(秒)


# ====== 日志配置（最小改动，支持热更） ======
LOG_DIR = "./logs"                 # 日志目录
LOG_TO_FILE = True                 # 写文件
LOG_JSON = True                    # 明细写 JSONL
LOG_ROTATE = "time"                # 按天滚动
LOG_BACKUP_DAYS = 14               # 日志保留天数
LOG_LEVEL = "INFO"                 # 控制台门槛
LOG_HEARTBEAT_SEC = 5              # 心跳频率(秒)，0/None=关闭
LOG_SUMMARY_EVERY_SEC = 60         # 每1分钟网格健康摘要
LOG_CONSOLE_DAILY_SUMMARY = True   # 控制台日终成交汇总
LOG_SUMMARY_TO_CSV = True          # 同步导出 CSV 摘要


# ====== 日志配置（支持热更） ======
LOG_DIR = LOG_DIR  # 保持上面的默认值；如需固定路径，请设置环境变量 LOG_DIR_OVERRIDE
LOG_TO_FILE = True
LOG_JSON = True
LOG_ROTATE = "time"               # 按天滚动
LOG_BACKUP_DAYS = 14
LOG_LEVEL = "INFO"                # 控制台门槛
LOG_HEARTBEAT_SEC = 0             # 心跳（本次未启用，摘要已足够）
LOG_SUMMARY_EVERY_SEC = 60        # 每1分钟摘要
LOG_CONSOLE_DAILY_SUMMARY = True  # 控制台日终成交汇总
LOG_SUMMARY_TO_CSV = True         # 同步导出CSV摘要
# 手续费（估算用，便于计算日终费用/净值）。OKX 常见挂单0.04%、吃单0.06%按需改。
FEE_MAKER_PCT = 0.0004
FEE_TAKER_PCT = 0.0006
# === R3 止盈参数 ===

# === 风控与趋势默认值（追加） ===
LIQPX_STOP_USD = Decimal("80")  # 距强平价≤该美元差时暂停策略
LIQPX_RESUME_USD = Decimal("140")  # 距强平价≥该美元差时恢复策略
TREND_REQUIRE_BASE = False  # 首轮不特殊处理，直接按规则加仓