# -*- coding: utf-8 -*-
"""
统一入口：一次运行（once）/ 守护（daemon）
- 环境变量仅用于 OKX 三件套与基础开关；策略参数走 cfg/tuning 热更新
"""
from __future__ import annotations
import os, time, logging, argparse, sys
from logging.handlers import TimedRotatingFileHandler
import cfg
from pathlib import Path
from okx_api import set_api_config
from strategy import run_strategy_once

log = logging.getLogger("GVWAP")

def str2bool(s, default=False):
    if s is None: return default
    return str(s).strip().lower() in ("1","true","yes","y","on")

def load_dotenv_if_present():
    try:
        from dotenv import load_dotenv
        for cand in (".env","./.env","../.env"):
            if os.path.exists(cand):
                load_dotenv(cand)
                log.info("已从 %s 加载 OKX 配置（若系统环境已存在，则不会覆盖）", cand)
                return
    except Exception as e:
        log.warning("加载 .env 失败：%s", e)

def configure():
    set_api_config(
        simulated=str2bool(os.getenv("USE_SIMULATED","true"), True),
        dry_run=str2bool(os.getenv("DRY_RUN","false"), False),
        use_system_proxy=str2bool(os.getenv("USE_SYSTEM_PROXY","true"), True),
        api_key=os.getenv("OKX_API_KEY",""),
        api_secret=os.getenv("OKX_API_SECRET",""),
        api_passphrase=os.getenv("OKX_API_PASSPHRASE", os.getenv("OKX_PASSPHRASE","")),
    )

def run_once():
    inst_id = os.getenv("INST_ID","ETH-USDT-SWAP")
    td_mode = os.getenv("TD_MODE","cross")
    log.info("策略启动 - 合约: %s | 持仓模式: %s", inst_id, td_mode)

    # ✅ 启动时清空所有历史挂单（防止干扰）
    from okx_api import cancel_all
    canceled = cancel_all(inst_id)
    log.warning("[启动] 已清空 %d 条历史挂单，防止干扰", canceled)

    run_strategy_once(inst_id=inst_id, td_mode=td_mode)

def run_daemon():
    import random
    backoff, max_backoff, jitter = 5, 120, 5
    while True:
        try:
            run_once()
            log.info("策略正常退出。"); break
        except KeyboardInterrupt:
            log.info("收到手动中断(CTRL+C)，退出。"); break
        except Exception as e:
            sleep_s = min(max_backoff, backoff) + random.randint(0, jitter)
            log.warning("守护：异常：%r；%ds 后重启", e, sleep_s, exc_info=True)
            time.sleep(sleep_s); backoff = min(max_backoff, max(5, backoff*2))

def main():
    log_dir = Path(__file__).resolve().parent / 'logs'
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / 'gvwap.log'
    # 根 logger 一次性配置
    logging.getLogger().handlers.clear()
    logging.getLogger().setLevel(getattr(logging, os.getenv("LOG_LEVEL","INFO").upper()))
    # 设置控制台日志：INFO 级别
    console_fmt = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s')
    # 为 root logger 添加一个打印到 stderr 的 handler
    ch = logging.StreamHandler()
    ch.setFormatter(console_fmt)
    ch.setLevel(logging.INFO)
    logging.getLogger().addHandler(ch)
    # 文件日志：DEBUG 级别以记录 ACT JSON
    fh = TimedRotatingFileHandler(str(log_file), when='midnight', backupCount=7, encoding='utf-8')
    fh.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
    fh.setLevel(logging.DEBUG)
    logging.getLogger().addHandler(fh)
    logging.getLogger().info('日志文件: %s', str(log_file))
    # 确保 root logger 级别为 INFO（控制台保持 INFO 输出）
    load_dotenv_if_present(); configure()
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=("once","daemon"), default="once")
    a = ap.parse_args()
    if a.mode=="daemon": run_daemon()
    else: run_once()

if __name__ == "__main__":
    main()