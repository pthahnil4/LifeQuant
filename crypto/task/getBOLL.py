from typing import Dict, Optional
import math
import pandas as pd
import numpy as np
from crypto.api_config import get_market_api

def _fetch_candles(inst_id: str, bar: str, limit: int = 100) -> Optional[pd.DataFrame]:
    market_api = get_market_api()
    per_req = max(1, min(100, int(limit)))
    use_mark = True
    try:
        res = market_api.get_mark_price_candlesticks(instId=inst_id, bar=bar, limit=str(per_req))
        data = res.get("data") or []
        if not data:
            use_mark = False
            res = market_api.get_candlesticks(instId=inst_id, bar=bar, limit=str(per_req))
            data = res.get("data") or []
        if not data:
            return None
        collected = list(data)
        after_ts = data[-1][0]
        remaining = max(0, limit - len(collected))
        loops = max(0, math.ceil(remaining / per_req))
        loops = min(loops, 5)
        for _ in range(loops):
            if use_mark:
                res2 = market_api.get_mark_price_candlesticks(instId=inst_id, bar=bar, after=after_ts, limit=str(per_req))
                chunk = res2.get("data") or []
            else:
                res2 = market_api.get_candlesticks(instId=inst_id, bar=bar, after=after_ts, limit=str(per_req))
                chunk = res2.get("data") or []
            if not chunk:
                break
            collected.extend(chunk)
            after_ts = chunk[-1][0]
        if not collected:
            return None
        df = pd.DataFrame(collected)
        if df.empty:
            return None
        df = df.iloc[::-1].copy()
        df.columns = list(range(df.shape[1]))
        core = df[[0, 1, 2, 3, 4]].copy()
        core.columns = ["timestamp", "open", "high", "low", "close"]
        core["timestamp"] = pd.to_datetime(core["timestamp"].astype(int) / 1000, unit="s")
        core["timestamp"] = core["timestamp"] + pd.Timedelta(hours=8)
        for c in ["open", "high", "low", "close"]:
            core[c] = core[c].astype(float)
        core.set_index("timestamp", inplace=True)
        return core
    except Exception:
        return None

def _compute_boll(df: pd.DataFrame, period: int = 20, std_mult: float = 2.0) -> pd.DataFrame:
    out = df.copy()
    out["MB"] = out["close"].rolling(window=period).mean()
    out["STD"] = out["close"].rolling(window=period).std()
    out["UB"] = out["MB"] + std_mult * out["STD"]
    out["LB"] = out["MB"] - std_mult * out["STD"]
    return out

def _detect_trend(df: pd.DataFrame, lookback: int = 10) -> str:
    tail = df["MB"].tail(max(2, lookback))
    if tail.isna().any():
        return "flat"
    slope = tail.diff().mean()
    if slope is None or np.isnan(slope):
        return "flat"
    if slope > 0:
        return "up"
    if slope < 0:
        return "down"
    return "flat"

def get_bollinger_analysis(instId: str, bar: str, limit: int = 100) -> Optional[Dict]:
    df = _fetch_candles(instId, bar, limit=limit)
    if df is None or len(df) < 25:
        return None
    boll = _compute_boll(df)
    last = boll.iloc[-1]
    trend = _detect_trend(boll)
    return {
        "analysis": {
            "trend": trend
        },
        "bollinger_bands": {
            "upper": float(last["UB"]),
            "lower": float(last["LB"]),
            "mid": float(last["MB"])
        },
        "basic_info": {
            "current_price": float(last["close"]),
            "instId": instId,
            "bar": bar,
            "timestamp": boll.index[-1].isoformat()
        },
        # 新增：当前最新K线的高低价（用于BOLL触达检查）
        "current_candle": {
            "high": float(last["high"]),
            "low": float(last["low"]),
            "open": float(last["open"]),
            "close": float(last["close"])
        }
    }
