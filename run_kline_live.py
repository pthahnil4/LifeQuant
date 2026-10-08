#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
监控台币种 1 分钟 K 线实时拉取 · 独立运行入口（实验通道）
==================================================
只做两件事：把 kline_live_btc 定时任务跑起来（ccxt → OKX 公共行情 → CSV 持续
增量落盘），并用一个独立端口提供 /kline-live 页面看拉取进度与最新数据。

币种清单跟监控台走（固定池 + 浮动池），每个币种两条流（永续 + 现货），CSV 按
币种区分（如 btc_ohlcv_live_1m_swap.csv / eth_ohlcv_live_1m_spot.csv）；开拉前先用
ccxt 的 load_markets() 校验该交易对（spot / swap 是两个不同 market），查不到的标
「ccxt 无此数据源」并跳过，不报错。

为什么不直接用 7777 那个进程：本机 7777 上是在跑的实盘应用，为一个只读行情
页面去重启它不值得；本脚本刻意 **不 import crypto.app**（导入即会拉起全套调度
任务、告警监控线程与交易链路），只挂 kline_live + 导航两个蓝图。

安全边界
--------
- 只注册一个 interval 任务（kline_live_btc），零下单、零交易接口触达；
- 行情全部走 ccxt 公共接口，不需要任何 API 密钥；
- 落盘目录 crypto/kline_fetcher/data/live/，与 task/kline_fixed_fetch.py 的
  *_since_2022.csv 物理隔离（两者按不同 symbol 写同一文件会造成价格串列）；
- 默认只绑 127.0.0.1:7788。同一份 live/ CSV 只允许一个写入者，所以本脚本运行
  期间不要在实盘进程里再启用同名任务（默认也没启用）。

启动
----
    python run_kline_live.py                       # http://127.0.0.1:7788/kline-live
    $env:KLINE_LIVE_PORT='7790'; python run_kline_live.py     # 换端口（PowerShell）
    python run_kline_live.py --bootstrap           # 只跑一轮抓取后退出（不起服务）
    python run_kline_live.py --status               # 只看各币种进度（纯本地，不触网）
    python run_kline_live.py --status --coin BTC,ETH # 只看几个币的进度

全量回填（「从 OKX 数据地板起，有多少取多少」）
----------------------------------------------
调度轮每 60 秒只有一个短预算，百万根靠它要跑好几天；真回填请用连续跑批模式（不起
Web、不注册调度，只跑游标）。币种用 --coin（缺省 all），类型用 --stream：

    python run_kline_live.py --backfill --coin BTC --stream all --full      # BTC 双流地板→今天
    python run_kline_live.py --backfill --coin ETH --stream swap --full     # 只补 ETH 永续
    python run_kline_live.py --backfill --coin BTC,ETH --stream spot --days 30   # 两个币现货近 30 天
    python run_kline_live.py --backfill --stream swap --rows 12000          # 只推进固定根数（测速率）
    python run_kline_live.py --backfill --full --budget 3600               # 最多跑 1 小时就收工

回填期间同一份 live/ CSV 只有本进程一个写入者，因此跑之前先确认 7788/7777 上的
定时任务已停。游标落在 kline_live_snapshot.json，Ctrl+C 或 --budget 到点都能断点续跑。
"""
import argparse
import os
import sys
import threading
import time
import warnings

warnings.filterwarnings(
    "ignore",
    category=DeprecationWarning,
    message=r"datetime\.datetime\.utcnow\(\) is deprecated",
)

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from flask import Flask, redirect          # noqa: E402

from crypto import kline_live_service as svc        # noqa: E402
from crypto.kline_live_routes import kline_live_bp  # noqa: E402
from crypto.navigation_routes import init_app as _init_nav  # noqa: E402


def build_app() -> Flask:
    """最小 Flask 壳：只挂 K 线页面 + 全站导航（nav.html 的上下文由导航模块注入）。"""
    crypto_dir = os.path.join(_HERE, 'crypto')
    app = Flask(__name__,
                template_folder=os.path.join(crypto_dir, 'templates'),
                static_folder=os.path.join(crypto_dir, 'static'),
                static_url_path='/static')
    app.register_blueprint(kline_live_bp)
    _init_nav(app)

    @app.route('/')
    def _home():
        return redirect('/kline-live')

    return app


def start_job():
    """启动调度器并注册任务；返回 (job_id, 描述)。"""
    from crypto.task.scheduler import task_scheduler
    task_scheduler.start()
    job_id, desc = svc.register_kline_live_job()
    if job_id:
        # interval 触发器第一次要等满一个周期才响，首轮单独跑一次，
        # 否则刚启动的几分钟里页面只有一张空看板。
        threading.Thread(target=svc.run_kline_live_job,
                         name='KlineLiveBootstrap', daemon=True).start()
    return job_id, desc


def _fmt_hms(sec):
    sec = int(max(0, sec))
    return f'{sec // 3600:d}小时{(sec % 3600) // 60:02d}分{sec % 60:02d}秒'


def _fmt_pct(v):
    return '目标未知' if v is None else f'{float(v):.2f}%'


def print_coin_progress(coin=None, cfg=None):
    """按币种打印回填进度（纯本地：只读 CSV 台账与快照游标，不发任何网络请求）。

    没跑过 ccxt 校验时“数据源”列给「未校验」而不是「无」——两者含义不同：后者会让人
    误以为这个币拉不了。
    """
    cfg = cfg or svc.load_config()
    st = svc.get_status(cfg)
    want = None
    if coin and coin.strip().lower() not in ('all', '*'):
        want = {c.strip().upper() for c in str(coin).split(',') if c.strip()}
    streams = {s['key']: s for s in st['streams']}
    rows = [c for c in st['coins'] if not want or c['coin'] in want]
    print('=' * 78)
    print(f'  📊 币种回填进度（{len(rows)}/{st["coins_total"]} 个币种 · '
          f'台账合计 {sum(c["rows_total"] for c in rows):,} 根）')
    print('=' * 78)
    if not rows:
        print('  没匹配到币种（检查 --coin 写法，币种名即可，如 BTC / ETH）')
        return 0
    for c in rows:
        src = ('可用' if c['available'] is True
               else ('无数据源' if c['available'] is False else '未校验'))
        head = (f'  {c["coin"]:<6} [{src}] 台账 {c["rows_total"]:>12,} 根 · '
                f'进度 {_fmt_pct(c["backfill_pct"])} · '
                f'尚差 {c["backfill_remaining_rows"]:,} 根 · '
                f'末根 {c["latest_time_local"] or "--"}')
        print(head)
        if c['market_note']:
            print(f'         ⛔ {c["market_note"]}')
        for k in c['streams']:
            s = streams.get(k) or {}
            print(f'         · {s.get("label", k):<12} '
                  f'{"已追平" if s.get("backfill_done") else "回填中" if s.get("head_cursor") else "未开始"}'
                  f' 台账 {int(s.get("rows_total") or 0):>10,} 根 '
                  f'进度 {_fmt_pct(s.get("backfill_pct")):<8} '
                  f'游标 {s.get("head_cursor_time_local") or "--"} '
                  f'速率 {s.get("backfill_rate") or "--"} 根/秒 '
                  f'预计剩余 {s.get("backfill_eta_sec") and _fmt_hms(s["backfill_eta_sec"]) or "--"}')
    print('-' * 78)
    print(f'  汇总：币种共 {st["coins_total"]} 个，有数据源 {st["coins_available"]} 个，'
          f'ccxt 无此数据源 {st["coins_missing"]} 个，未校验 {st["coins_unchecked"]} 个 '
          f'（共 {st["streams_total"]} 条流，本轮跳过 {st["streams_skipped"]} 条）')
    print(f'  落盘目录：{st["data_dir"]}')
    print('  说明：本入口只读本地文件，不触网；“未校验”意思是这个进程还没问过 ccxt 市场表')
    return 0


def run_backfill_mode(args):
    """连续跑批回填：不进 Web、不注册调度，只把游标从地板推到今天。

    --days / --rows 只改内存里的 cfg，不写 kv（不想把临时试跑口径变成常驻配置）。
    币种由 --coin 选（缺省 all = 配置里启用的全部币种），类型由 --stream 选。
    """
    cfg = svc.load_config()
    if args.days:
        cfg['backfill_mode'] = 'days'
        cfg['since_days'] = args.days
    if args.full:
        cfg['backfill_mode'] = 'earliest'
    types = None if args.stream == 'all' else [args.stream]
    try:
        keys = svc.stream_keys_for(coins=args.coin, types=types, cfg=cfg)
    except ValueError as e:
        print(f'[错误] {e}')
        return 2

    print('=' * 68)
    print(f'  ⛏️  全量回填（连续跑批）：{"、".join(svc._stream_label(k, cfg) for k in keys)}')
    print('=' * 68)
    print('  口径     : ' + ('最近 %g 天' % cfg['since_days']
                          if cfg['backfill_mode'] == 'days' else 'OKX 数据地板 → 现在'))
    print(f'  落盘目录 : {svc.live_data_dir()}')
    print(f'  限速     : 批间隔 {cfg["request_interval_ms"]}ms · 单批 300 根 · '
          f'单流单段预算 {cfg["round_budget_sec"]}s')
    print('  目标     : ' + ('不限量（一路追平）' if not args.rows else f'新增 {args.rows:,} 根'))
    print('  总预算   : ' + ('不限（跑完为止）' if not args.budget else _fmt_hms(args.budget)))
    print('  提示     : Ctrl+C 随时中断，游标已落盘，下次从原地继续')
    print('-' * 68, flush=True)

    total_appended = 0
    t_start = time.time()
    for idx, k in enumerate(keys, 1):
        label = svc._stream_label(k, cfg)
        print(f'--- [{idx}/{len(keys)}] {label} ---', flush=True)
        st = {'last_print': 0.0}

        def _progress(p, _st=st):
            now = time.time()
            if now - _st['last_print'] < 10 and not p.get('done'):
                return
            _st['last_print'] = now
            el = p['elapsed_sec']
            rate = p['appended'] / el if el > 1 else 0
            cur = svc._local_iso(p['cursor_ms'])
            extra = ''
            if args.rows:
                left = max(0, args.rows - p['appended'])
                extra = f' | 剩余 {left:,} 根 ≈ {_fmt_hms(left / rate) if rate > 1 else "-"}'
            elif p.get('rate'):
                pass
            print(f'  [{label}] +{p["appended"]:>9,} 根 | {rate:6.0f} 根/秒 | 游标 {cur}'
                  f' | 已跑 {el:5.0f}s{extra}', flush=True)

        r = svc.run_backfill(k, budget_sec=args.budget, until_rows=args.rows,
                             cfg=cfg, on_round=_progress)
        total_appended += r['appended']
        el = max(r['elapsed_sec'], 1e-6)
        print('-' * 68)
        print(f'  [{label}] 结束：新增 {r["appended"]:,} 根 / {r["elapsed_sec"]:.1f}s → '
              f'{r["appended"] / el:.0f} 根/秒（{r["appended"] * 60 / el:,.0f} 根/分钟）'
              f'，批数 {r["batches"]}，原因 {r["halt"]}，交易所侧缺 {r["gap_rows"]:,} 根')
        view = svc._streams_state.get(k) or {}
        target = svc._target_rows(cfg, view)
        covered = int(view.get('rows_total') or 0)
        if target:
            eta = (target - covered) / (r['appended'] / el) if r['appended'] else 0
            print(f'  [{label}] 台账 {covered:,} 根 / 全量目标 {target:,} 根 → '
                  f'剩余按实测速率外推 ≈ {_fmt_hms(eta)}')
        if r['error']:
            print(f'  [{label}] 错误：{r["error"]}'
                  + ('（该币种在此市场无数据源，已跳过）' if r['halt'] == 'no_market' else ''))
    print('=' * 68)
    print(f'  合计新增 {total_appended:,} 根，总耗时 {_fmt_hms(time.time() - t_start)}')
    print('  CSV 台账保留，下次启动按断点继续；要回到实时增量，重新跑不带 --backfill 的服务即可')
    return 0


def main():
    parser = argparse.ArgumentParser(description='监控台币种 1分钟K线实时拉取（独立实验通道）')
    parser.add_argument('--host', default=os.environ.get('KLINE_LIVE_HOST') or '127.0.0.1')
    parser.add_argument('--port', type=int,
                        default=int(os.environ.get('KLINE_LIVE_PORT') or 7788))
    parser.add_argument('--bootstrap', action='store_true',
                        help='只同步跑一轮抓取（首轮回填会跑较久）后退出，不起 Web 服务')
    parser.add_argument('--status', action='store_true',
                        help='只打印按币种的回填进度（纯本地读台账与快照，不拉取不触网）')
    parser.add_argument('--coin', default='all',
                        help='哪些币种（如 BTC 或 BTC,ETH；all=配置启用的全部监控台币种）')
    parser.add_argument('--backfill', action='store_true',
                        help='连续跑批全量回填模式（不起 Web / 不注册调度），配合下面几个参数')
    parser.add_argument('--stream', default='all', choices=['swap', 'spot', 'all'],
                        help='回填哪个类型的流（默认 all：每个币的永续 + 现货串行）')
    parser.add_argument('--full', action='store_true',
                        help='从 OKX 数据地板一路补到今天（earliest 口径）')
    parser.add_argument('--days', type=float, default=0,
                        help='只补最近 N 天（days 口径，计时/试跑用）')
    parser.add_argument('--rows', type=int, default=0,
                        help='只推进固定根数（测速率用，1 个月 = 43200）')
    parser.add_argument('--budget', type=int, default=0,
                        help='本次回填最多跑多少秒后收工（游标保留）')
    args = parser.parse_args()

    if args.status:
        return print_coin_progress(args.coin)

    if args.backfill:
        return run_backfill_mode(args)

    cfg = svc.load_config()
    streams = svc.active_streams(cfg)
    coins = sorted({s['coin'] for s in streams})
    print('=' * 68)
    print('  ⛓️  监控台币种 1 分钟 K 线实时拉取 · 独立实验通道')
    print('=' * 68)
    print(f'  页面地址 : http://{args.host if args.host != "0.0.0.0" else "127.0.0.1"}:{args.port}/kline-live')
    print(f'  币种范围 : {len(coins)} 个币种 × {len(cfg["streams"])} 种类型 = '
          f'{len(streams)} 条流'
          + (f'（已收窄：{",".join(cfg["coins"])}）' if cfg['coins']
             else f'（跟随监控台固定币种按市值前 {svc.DEFAULT_MONITOR_TOP_COINS}）'))
    print(f'  示例数据流 : {"、".join(s["label"] for s in streams[:4])}'
          + ('…' if len(streams) > 4 else ''))
    print(f'  调度周期 : 每 {cfg["interval_seconds"]} 秒 · 周期 {cfg["timeframe"]} · '
          f'口径 {cfg["backfill_mode"]} · 定时轮补历史：'
          f'{"开" if cfg.get("schedule_backfill") else "关（历史靠 --backfill 或页面跑批）"}')
    print(f'  落盘目录 : {svc.live_data_dir()}（按币种分文件，如 eth_ohlcv_live_1m_swap.csv）')
    print('  安全边界 : 不 import crypto.app、不启实盘调度、零交易接口，行情走公共接口')
    print('  停止方式 : Ctrl+C（不影响 7777 上的交易进程）')
    print('=' * 68)

    if args.bootstrap:
        print('[启动] --bootstrap：同步执行一轮抓取（首轮回填可能几分钟）…')
        print('[启动] 结果 =', svc.run_kline_live_job())
        return

    job_id, desc = start_job()
    if not job_id:
        print(f'[警告] 任务未注册（{desc}）：页面仍可只读查看已落盘的 CSV，'
              '可用页面上的「立即拉取一轮」按钮手动触发')
    else:
        print(f'[启动] 定时任务已注册：{job_id} · {desc}')

    try:
        # threaded=True：页面 5s 轮询 status + 15s 轮询 bars，单线程会互相堵住
        build_app().run(debug=False, host=args.host, port=args.port, threaded=True)
    finally:
        from crypto.task.scheduler import task_scheduler
        task_scheduler.shutdown()
        print('[退出] 调度器已停止（CSV 台账保留，下次启动按断点继续）')


if __name__ == '__main__':
    sys.exit(main() or 0)
