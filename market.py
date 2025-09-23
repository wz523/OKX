
# -*- coding: utf-8 -*-
from decimal import Decimal
from okx_api import fetch_instrument, fetch_ticker

class Market:
    def __init__(self, inst: str):
        self.inst = inst
        self.mid = 0.0
        self.bid = 0.0
        self.ask = 0.0
        self.last = 0.0
        self.spec = {"tickSz": Decimal("0.01"), "lotSz": Decimal("0.01"), "minSz": Decimal("0.01"), "ctVal": Decimal("0.1")}
        self.load_spec()

    def normalize_spec(self):
        for k in ("tickSz", "lotSz", "minSz", "ctVal"):
            v = self.spec.get(k)
            if v is None: continue
            if not isinstance(v, Decimal):
                try: self.spec[k] = Decimal(str(v))
                except Exception: pass

    def load_spec(self):
        try:
            d = fetch_instrument(self.inst) or {}
            if d:
                self.spec = {
                    "tickSz": Decimal(str(d.get("tickSz", "0.01"))),
                    "lotSz":  Decimal(str(d.get("lotSz",  "0.01"))),
                    "minSz":  Decimal(str(d.get("minSz",  "0.01"))),
                    "ctVal":  Decimal(str(d.get("ctVal",  "0.1"))),
                }
        except Exception:
            pass
        self.normalize_spec()
        return self.spec

    
    def refresh_mid(self):
        try: d = fetch_ticker(self.inst) or {}
        except Exception: d = {}
        ask = d.get("askPx") or d.get("ask") or 0
        bid = d.get("bidPx") or d.get("bid") or 0
        last = d.get("last") or d.get("lastPx") or 0
        try: ask = float(ask)
        except Exception: ask = 0.0
        try: bid = float(bid)
        except Exception: bid = 0.0
        try: last = float(last)
        except Exception: last = 0.0
        self.ask = ask; self.bid = bid; self.last = last
        if ask and bid: self.mid = (ask + bid) / 2.0
        elif last: self.mid = last
        return self.mid



    
    def best_price(self, side: str) -> float:
        self.refresh_mid()
        if side == 'buy':
            return self.ask or self.mid
        elif side == 'sell':
            return self.bid or self.mid
        return self.mid

    def ref_price(self, mode: str = 'mid') -> float:
        self.refresh_mid()
        if mode == 'mid': return self.mid
        if mode == 'last': return self.last or self.mid
        if mode == 'ask': return self.ask or self.mid
        if mode == 'bid': return self.bid or self.mid
        return self.mid

def px(self) -> float:
        self.refresh_mid()
        return self.mid
