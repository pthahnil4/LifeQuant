# -*- coding: utf-8 -*-
"""一次性迁移：把「全局单份交易配置」拆成「按账号各一份」，根治多账号串味。

背景
----
本地测试账号（StageOne，1U 小额）与服务器实盘主账号（Hunter，20U）共用同一个
远程 MySQL。改造前 strategy_config 只有一份全局记录，服务器写进去的 20U 被本地
「DB 优先」读到，用测试账号去下 20U 的单 → 可用保证金不足 51008。

代码已改为按账号读写（config_store_repo.strategy_config_key / load_strategy_config_cached）。
本脚本负责把存量数据就位：

    [1] 全局 strategy_config（=服务器 Hunter 的 20U）  → strategy_config:main
    [2] 本地文件 config_trend_range.json（=本地 1U）    → strategy_config:stageone
    [3] trading_runtime.account: stageone（被本地污染） → main
        （否则服务器进程重启走 auto_resume 自愈时，会误用 StageOne 子账号跑实盘）

安全边界
--------
- 默认 **dry-run 只预览**，加 --apply 才真正写库；
- 幂等：strategy_config:main / :stageone 已存在则跳过，不覆盖（除非 --force）；
- 只新增两个账号专属 key + 修正 trading_runtime.account，**绝不动全局 strategy_config**
  （保留它作为新代码的兜底源，部署时序上更安全）；
- 不下单、不碰 OKX、不改持仓账本。

用法
----
    python crypto/task/_migrate_per_account_config.py            # 预览
    python crypto/task/_migrate_per_account_config.py --apply    # 执行
    python crypto/task/_migrate_per_account_config.py --apply --force  # 覆盖已存在的专属 key
"""

import os
import sys
import json
import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
# 项目根（crypto/ 的上级）：与 _diag_db_config_state.py 一致的 sys.path 兜底
sys.path.insert(0, os.path.dirname(os.path.dirname(_HERE)))

sys.stdout.reconfigure(encoding='utf-8', errors='replace', line_buffering=True)

from crypto.database import session_scope, get_engine      # noqa: E402
from crypto import config_store_repo as csr                # noqa: E402
from crypto.models import KVStore                          # noqa: E402

# 本地文件（stageone 迁移源，应为 1U 测试配置）
_FILE_CFG = os.path.join(_HERE, 'config', 'config_trend_range.json')


def _notional_summary(cfg):
    """提取各币种 趋势/区间 notional_usd，一眼看出这份配置是几 U"""
    if not cfg:
        return '(无)'
    parts = []
    for c in (cfg.get('currencies') or []):
        inst = str(c.get('instId') or '?')
        t = (c.get('trend_position') or {}).get('notional_usd')
        r = (c.get('range_position') or {}).get('notional_usd')
        parts.append(f"{inst.split('-')[0]}(趋{t}/区{r})")
    n = len(cfg.get('currencies') or [])
    return f"{n}币种 " + ' '.join(parts) if parts else '(无币种)'


def main():
    argv = sys.argv[1:]
    apply = '--apply' in argv
    force = '--force' in argv

    # 本地文件（stageone 源）
    file_cfg = None
    try:
        with open(_FILE_CFG, 'r', encoding='utf-8') as f:
            file_cfg = json.load(f)
    except Exception as e:
        print(f"!! 读本地文件失败（stageone 迁移源缺失）: {e}")

    eng = get_engine()
    safe = str(eng.url).split('@')[-1] if '@' in str(eng.url) else str(eng.url)
    print(f"目标库: {safe}")
    print(f"模式  : {'APPLY（写库）' if apply else 'DRY-RUN（只预览，加 --apply 才写）'}"
          + ('  [force=覆盖已存在专属key]' if force else ''))

    with session_scope() as session:
        session.execute(KVStore.__table__.select().limit(0))  # 连通性探活

        global_cfg = csr.load_json_config(session, csr.KEY_STRATEGY_CONFIG)
        main_key = csr.strategy_config_key('main')
        stageone_key = csr.strategy_config_key('stageone')
        main_exist = session.get(KVStore, main_key) is not None
        stageone_exist = session.get(KVStore, stageone_key) is not None
        rt = csr.load_json_config(session, csr.KEY_TRADING_RUNTIME) or {}

        print('\n──── 迁移计划 ────')
        print(f"[1] 全局 strategy_config  →  {main_key}")
        print(f"      源(全局): {_notional_summary(global_cfg)}")
        print(f"      目标已存在={main_exist}" + ('  → 跳过（--force 才覆盖）' if main_exist and not force else '  → 将写入'))

        print(f"[2] 本地文件 config_trend_range.json  →  {stageone_key}")
        print(f"      源(文件): {_notional_summary(file_cfg)}")
        print(f"      目标已存在={stageone_exist}" + ('  → 跳过（--force 才覆盖）' if stageone_exist and not force else '  → 将写入'))

        print(f"[3] trading_runtime.account: {rt.get('account')!r}  →  'main'")
        print(f"      desired_running={rt.get('desired_running')} auto_resume={rt.get('auto_resume')} last_event={rt.get('last_event')!r}")

        if not apply:
            print('\n(dry-run 结束，未写库。确认以上计划无误后，加 --apply 执行)')
            return 0

        print('\n──── 执行写入 ────')
        changed = []
        if global_cfg and (not main_exist or force):
            csr.save_json_config(session, main_key, global_cfg)
            changed.append(f'{main_key} ← 全局配置（服务器 Hunter 无缝读到）')
        if file_cfg and (not stageone_exist or force):
            csr.save_json_config(session, stageone_key, file_cfg)
            changed.append(f'{stageone_key} ← 本地文件（本地 StageOne 测试 1U）')
        if str(rt.get('account')) != 'main':
            rt['account'] = 'main'
            rt['last_event'] = 'migrate_per_account_config'
            rt['updated_at'] = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            csr.save_json_config(session, csr.KEY_TRADING_RUNTIME, rt)
            changed.append('trading_runtime.account ← main（消除服务器重启误用子账号隐患）')

        for c in changed:
            print(f'  ✓ {c}')
        if not changed:
            print('  (无需变更：专属 key 均已存在且 runtime.account 已是 main)')

    print('\n──── 迁移后必读 ────')
    print('1. 服务器：部署本次新代码后重启 → Hunter 读 strategy_config:main(20U)，无缝；')
    print('   即便新代码先上、迁移后补，也有全局兜底，仓位不会骤降到 1U。')
    print('2. 本地：重启定时任务（账号选 StageOne）→ 读 strategy_config:stageone(1U)，51008 消失。')
    print('3. 网页：账号下拉切换即加载对应账号独立配置，保存只写该账号，互不覆盖。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
