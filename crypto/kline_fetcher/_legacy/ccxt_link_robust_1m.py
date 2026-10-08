import os
import sys
import runpy


def main():
    here = os.path.dirname(__file__)
    script = os.path.join(here, "ccxt_btc_robust_1m.py")
    save_path = os.path.join(here, "..", "data", "link_ohlcv_robust_1m_since_2022.csv")
    argv = [
        script,
        "--exchanges", "okx,bybit,bitget",
        "--symbol", "LINK/USDT",
        "--timeframe", "1m",
        "--save", save_path,
        "--resume",
    ]
    extra = sys.argv[1:]
    sys.argv = argv + extra
    runpy.run_path(script, run_name="__main__")


if __name__ == "__main__":
    main()