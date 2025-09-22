# -*- coding: utf-8 -*-
"""OKX API 轻量封装（满足本策略所需的函数签名）
提供：
- set_api_config(**kwargs)
- fetch_instrument(instId)
- fetch_ticker(instId)
- fetch_candles(instId, limit)
- fetch_positions(instId)
- fetch_open_orders(instId)
- place_limit(instId, side, sz, px, post_only, reduce_only, tag)
- place_market(instId, side, sz, reduce_only, tag)
- cancel_order(instId, order_id)
- cancel_all(instId)
- close_position(instId, td_mode)
- side_from_pos(posSide, action)
说明：
- 使用 .env/环境变量读取 OKX 三件套与模拟盘：OKX_API_KEY/SECRET/PASSPHRASE，OKX_SIMULATED=true/false
- TD 模式默认 cross（可通过环境变量 OKX_TD_MODE 调整）
- 遵循当前工程其它模块的调用约定，参数尽量最小化
"""
from __future__ import annotations
import os
import time

class _NoRetry(Exception):
    pass
import hmac
import base64
import hashlib
import logging
import cfg
from typing import Any, Dict, List
from decimal import Decimal

import requests
import random

log = logging.getLogger("GVWAP")
SEED_HEX = os.getenv("CLORD_SEED") or f"{random.getrandbits(32):08x}"


# --- clOrdId 唯一化工具 ---
_CL_COUNTER = 0
def _b36(n: int, width: int = 6) -> str:
    s = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    out = []
    for _ in range(width):
        out.append(s[n % 36]); n //= 36
    return "".join(reversed(out))


def _make_clordid(instId: str, side: str, px, sz, tag: str | None) -> str | None:
    """Deterministic idempotency key. Max 32 chars for OKX clOrdId."""
    try:
        key = f"{instId}|{side}|{px}|{sz}|{tag}|{SEED_HEX}"
        h = hashlib.sha1(key.encode()).hexdigest()[:20]
        return f"G{h}".upper()
    except Exception:
        return None



# ---- auto-load .env (project root / module dir / parent) ----
try:
    from pathlib import Path as _P
    for _base in (_P.cwd(), _P(__file__).resolve().parent, _P(__file__).resolve().parent.parent):
        _pf = _base / ".env"
        if _pf.exists():
            for _ln in _pf.read_text(encoding="utf-8", errors="ignore").splitlines():
                _s = _ln.strip()
                if not _s or _s.startswith("#") or "=" not in _ln:
                    continue
                _k, _v = _ln.split("=", 1)
                _k = _k.strip()
                _v = _v.strip().strip('"').strip("'")
                if _k and _k not in os.environ:
                    os.environ[_k] = _v
            break
except Exception as _e:
    pass
# ------------------------------------------------------------
BASE_URL = os.getenv("OKX_BASE_URL", "https://www.okx.com")
API_KEY = os.getenv("OKX_API_KEY", "").strip()
API_SECRET = os.getenv("OKX_API_SECRET", "").strip()
API_PASSPHRASE = os.getenv("OKX_API_PASSPHRASE", os.getenv("OKX_PASSPHRASE", "").strip()).strip()
SIMULATED = os.getenv("OKX_SIMULATED", os.getenv("USE_SIMULATED", "true")).lower() in ("1", "true", "yes")
TD_MODE_DEFAULT = os.getenv("OKX_TD_MODE", "cross")
TIMEOUT = int(os.getenv("OKX_HTTP_TIMEOUT", "10"))

SESSION = requests.Session()
SESSION.headers.update({"Content-Type": "application/json"})
SESSION.trust_env = True  # 默认遵循系统代理
if SIMULATED:
    SESSION.headers.update({"x-simulated-trading": "1"})

# ========== 配置入口 ==========

def set_api_config(**kwargs):
    """可选配置入口：兼容 main.py 调用。
    支持关键字：
      - use_system_proxy: bool | None（True=走系统代理；False=忽略系统代理）
      - base_url: str | None（覆盖 OKX BASE_URL）
      - simulated: bool | None（是否模拟盘，自动设置/移除 x-simulated-trading）
      - api_key/api_secret/api_passphrase: 覆盖三件套（仅限当前进程）
      - timeout: int | None（HTTP 超时秒）
    其它多余参数会被忽略（为了最大兼容）。
    """
    global BASE_URL, SIMULATED, API_KEY, API_SECRET, API_PASSPHRASE, TIMEOUT
    use_system_proxy = kwargs.get("use_system_proxy")
    base_url = kwargs.get("base_url")
    simulated = kwargs.get("simulated")
    api_key = kwargs.get("api_key")
    api_secret = kwargs.get("api_secret")
    api_passphrase = kwargs.get("api_passphrase")
    timeout = kwargs.get("timeout")

    if use_system_proxy is not None:
        SESSION.trust_env = bool(use_system_proxy)
    if base_url:
        BASE_URL = str(base_url)
    if simulated is not None:
        SIMULATED = bool(simulated)
        if SIMULATED:
            SESSION.headers.update({"x-simulated-trading": "1"})
        else:
            if "x-simulated-trading" in SESSION.headers:
                SESSION.headers.pop("x-simulated-trading", None)
    if api_key:
        API_KEY = str(api_key).strip()
    if api_secret:
        API_SECRET = str(api_secret).strip()
    if api_passphrase:
        API_PASSPHRASE = str(api_passphrase).strip()
    if timeout is not None:
        try:
            TIMEOUT = int(timeout)
        except Exception:
            pass


# ========== 基础签名与请求 ==========

def _ts_iso() -> str:
    # OKX 要求 ISO8601 毫秒精度
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())


def _sign(ts: str, method: str, path: str, body: str) -> str:
    msg = f"{ts}{method}{path}{body}".encode()
    mac = hmac.new(API_SECRET.encode(), msg, hashlib.sha256).digest()
    return base64.b64encode(mac).decode()


def _req(method: str, path: str, params: Dict[str, Any] | None = None, body: Dict[str, Any] | None = None, private: bool = False) -> Dict:
    url = f"{BASE_URL}{path}"
    body_str = "" if (body is None) else __import__("json").dumps(body, separators=(",", ":"))
    headers = {}
    if private:
        if not (API_KEY and API_SECRET and API_PASSPHRASE):
            raise RuntimeError("未配置 OKX API 三件套，无法调用私有接口")
        ts = _ts_iso()
        from urllib.parse import urlencode
        query = None if params is None else urlencode(params)
        sign_path = path if query is None else f"{path}?{query}"
        headers.update({
            "OK-ACCESS-KEY": API_KEY,
            "OK-ACCESS-SIGN": _sign(ts, method.upper(), sign_path, body_str),
            "OK-ACCESS-TIMESTAMP": ts,
            "OK-ACCESS-PASSPHRASE": API_PASSPHRASE,
        })
    for _ in range(3):
        try:
            if method.upper() == "GET":
                r = SESSION.get(url, params=params, headers=headers, timeout=TIMEOUT)
            else:
                r = SESSION.post(url, params=params, data=body_str.encode(), headers=headers, timeout=TIMEOUT)
            if r.status_code // 100 != 2:
                raise RuntimeError(f"HTTP {r.status_code}: {r.text}")
            jd = r.json()
            if jd.get("code") not in ("0", 0, None):
                data = jd.get('data') or []
                detail = '; '.join([f"{x.get('sCode') or x.get('code')}: {x.get('sMsg') or x.get('msg')}" for x in data][:3])
                raise RuntimeError(f"OKX错误: code={jd.get('code')} msg={jd.get('msg')} details=[{detail}]")
            return jd
        except Exception as e:
            err = str(e)
            log.warning("请求失败 %s %s: %s", method, path, err)
            time.sleep(1.2)
    log.warning("多次重试仍失败: %s %s", method, path)
    return {}


# ========== 公共接口 ==========

def fetch_instrument(instId: str) -> Dict[str, str]:
    jd = _req("GET", "/api/v5/public/instruments", params={"instType": "SWAP", "instId": instId})
    arr = jd.get("data", [])
    if not arr:
        raise RuntimeError("未获取到合约规格")
    d = arr[0]
    return {
        "tickSz": d.get("tickSz", "0.01"),
        "lotSz": d.get("lotSz", "0.01"),
        "minSz": d.get("minSz", "0.01"),
        "ctVal": d.get("ctVal", "0.1"),
    }


def fetch_ticker(instId: str) -> Dict[str, str]:
    jd = _req("GET", "/api/v5/market/ticker", params={"instId": instId})
    arr = jd.get("data", [])
    return {"last": arr[0].get("last", "0")} if arr else {"last": "0"}


def fetch_candles(instId: str, limit: int = 200) -> List[List[str]]:
    # 使用 1m bar
    jd = _req("GET", "/api/v5/market/candles", params={"instId": instId, "bar": "1m", "limit": str(limit)})
    return jd.get("data", [])


# ========== 私有接口 ==========

def fetch_positions(instId: str) -> List[Dict[str, Any]]:
    jd = _req("GET", "/api/v5/account/positions", params={"instType": "SWAP", "instId": instId}, private=True)
    return jd.get("data", [])


def fetch_open_orders(instId: str) -> List[Dict[str, Any]]:
    jd = _req("GET", "/api/v5/trade/orders-pending", params={"instType": "SWAP", "instId": instId}, private=True)
    return jd.get("data", [])


def _posSide_from_side(side: str) -> str:
    # 在双向持仓模式：buy→long；sell→short
    return "long" if side == "buy" else "short"


def place_limit(instId: str, side: str, sz: Decimal, px: Decimal,
                post_only: bool = True, reduce_only: bool = False, tag: str | None = None,
                posSide: str | None = None, td_mode: str | None = None) -> str:
    """限价单下单。发生 51016（clOrdId 重复）等导致 ordId 缺失时，按 clOrdId 回查原单并返回 ordId。"""
    clid = _make_clordid(instId, side, px, sz, tag)
    body = {
        "instId": instId,
        "tdMode": (td_mode or TD_MODE_DEFAULT),
        "side": side,
        "posSide": (posSide or _posSide_from_side(side)),
        "ordType": "post_only" if post_only else "limit",
        "px": str(px),
        "sz": str(sz),
        "tag": tag,
        "clOrdId": clid or None,
        "reduceOnly": "true" if reduce_only else "false",
    }
    if body.get("clOrdId") is None:
        body.pop("clOrdId", None)
    try:
        jd = _req("POST", "/api/v5/trade/order", body=body, private=True)
        data = jd.get("data", [])
        if data and data[0].get("ordId"):
            return data[0].get("ordId") or ""
    except Exception as e:
        # 非重复单错误继续抛出
        if "51016" not in str(e):
            raise
    # 回查重复单
    if clid:
        od = fetch_order_by_clordid(instId, clid)
        if od:
            return od.get("ordId", "") or ""
    return ""

def place_market(instId: str, side: str, sz: Decimal,
                 reduce_only: bool = False, tag: str | None = None,
                 posSide: str | None = None, td_mode: str | None = None) -> str:
    """市价单下单。携带 clOrdId，若 ordId 缺失则按 clOrdId 回查。"""
    clid = _make_clordid(instId, side, "MKT", sz, tag)
    body = {
        "instId": instId,
        "tdMode": (td_mode or TD_MODE_DEFAULT),
        "side": side,
        "posSide": (posSide or _posSide_from_side(side)),
        "ordType": "market",
        "sz": str(sz),
        "tag": tag,
        "clOrdId": clid or None,
        "reduceOnly": "true" if reduce_only else "false",
    }
    if body.get("clOrdId") is None:
        body.pop("clOrdId", None)
    try:
        jd = _req("POST", "/api/v5/trade/order", body=body, private=True)
        data = jd.get("data", [])
        if data and data[0].get("ordId"):
            return data[0].get("ordId") or ""
    except Exception as e:
        if "51016" not in str(e):
            raise
    if clid:
        od = fetch_order_by_clordid(instId, clid)
        if od:
            return od.get("ordId", "") or ""
    return ""

def cancel_order(instId: str, order_id: str) -> None:
    body = {"instId": instId, "ordId": order_id}
    _req("POST", "/api/v5/trade/cancel-order", body=body, private=True)


def cancel_all(instId: str) -> int:
    cnt = 0
    for o in fetch_open_orders(instId):
        try:
            cancel_order(instId, o.get("ordId"))
            cnt += 1
        except Exception as e:
            log.warning("取消订单失败：%s", e)
    return cnt


def close_position(instId: str, td_mode: str = None) -> None:
    # 统一使用 close-position API，分别对 long/short 侧提交
    for posSide in ("long", "short"):
        body = {"instId": instId, "posSide": posSide, "mgnMode": TD_MODE_DEFAULT if (td_mode or TD_MODE_DEFAULT) == "cross" else "isolated"}
        try:
            _req("POST", "/api/v5/trade/close-position", body=body, private=True)
        except Exception as e:
            log.warning("close-position(%s) 失败：%s", posSide, e)


def side_from_pos(posSide: str, action: str) -> str:
    """给出 posSide(long/short) 与意图(open/close)，返回 buy/sell。
    - open long → buy；close long → sell
    - open short → sell；close short → buy
    """
    posSide = (posSide or "").lower()
    action = (action or "open").lower()
    if posSide == "long":
        return "buy" if action == "open" else "sell"
    else:
        return "sell" if action == "open" else "buy"


def _get_proxies(url: str):
    try:
        if not getattr(cfg, 'proxy_enabled')():
            return None
        if getattr(cfg, 'PROXY_BYPASS_OKX', False) and 'okx.com' in url:
            return None
        http = os.environ.get('HTTP_PROXY')
        https = os.environ.get('HTTPS_PROXY')
        proxies = {}
        if http: proxies['http'] = http
        if https: proxies['https'] = https
        return proxies or None
    except Exception:
        return None

def fetch_order_by_clordid(instId: str, clOrdId: str) -> Dict[str, Any]:
    jd = _req("GET", "/api/v5/trade/order", params={"instId": instId, "clOrdId": clOrdId}, private=True)
    arr = jd.get("data", []) if isinstance(jd, dict) else []
    return arr[0] if arr else {}



def _make_clordid(instId: str, side: str, px, sz, tag: str | None) -> str | None:
    """Unique but short. Keep <=32 chars for OKX."""
    try:
        global _CL_COUNTER
        _CL_COUNTER = (_CL_COUNTER + 1) % (36**6)  # rolling counter
        base = f"{instId}|{side}|{px}|{sz}|{tag}|{SEED_HEX}"
        h = hashlib.sha1(base.encode()).hexdigest()[:16]  # 16 hex
        suf = _b36(_CL_COUNTER, 6)                        # 6 chars
        return f"G{h}{suf}".upper()                      # len=23
    except Exception:
        return None




def place_limit(instId: str, side: str, sz: Decimal, px: Decimal,
                post_only: bool = True, reduce_only: bool = False, tag: str | None = None,
                posSide: str | None = None, td_mode: str | None = None) -> str:
    def _try_once(new_id: bool):
        clid = _make_clordid(instId, side, px, sz, tag) if new_id else clid0
        body = {
            "instId": instId, "tdMode": (td_mode or TD_MODE_DEFAULT),
            "side": side, "posSide": (posSide or _posSide_from_side(side)),
            "ordType": "post_only" if post_only else "limit",
            "px": str(px), "sz": str(sz), "tag": tag,
            "clOrdId": clid, "reduceOnly": "true" if reduce_only else "false",
        }
        jd = _req("POST", "/api/v5/trade/order", body=body, private=True)
        data = jd.get("data", []) if isinstance(jd, dict) else []
        if data and data[0].get("ordId"):
            return data[0]["ordId"]
        od = fetch_order_by_clordid(instId, clid)
        st = str(od.get("state", "")).lower()
        if od.get("ordId") and st not in {"canceled", "filled"}:
            return od.get("ordId", "")
        return ""

    clid0 = _make_clordid(instId, side, px, sz, tag)
    return _try_once(False) or _try_once(True)




def place_market(instId: str, side: str, sz: Decimal,
                 reduce_only: bool = False, tag: str | None = None,
                 posSide: str | None = None, td_mode: str | None = None) -> str:
    def _try_once(new_id: bool):
        clid = _make_clordid(instId, side, "MKT", sz, tag) if new_id else clid0
        body = {
            "instId": instId, "tdMode": (td_mode or TD_MODE_DEFAULT),
            "side": side, "posSide": (posSide or _posSide_from_side(side)),
            "ordType": "market", "sz": str(sz), "tag": tag, "clOrdId": clid,
            "reduceOnly": "true" if reduce_only else "false",
        }
        jd = _req("POST", "/api/v5/trade/order", body=body, private=True)
        data = jd.get("data", []) if isinstance(jd, dict) else []
        if data and data[0].get("ordId"):
            return data[0]["ordId"]
        od = fetch_order_by_clordid(instId, clid)
        st = str(od.get("state", "")).lower()
        if od.get("ordId") and st not in {"canceled", "filled"}:
            return od.get("ordId", "")
        return ""

    clid0 = _make_clordid(instId, side, "MKT", sz, tag)
    return _try_once(False) or _try_once(True)

