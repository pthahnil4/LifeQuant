# -*- coding: utf-8 -*-
"""临时冒烟：跨进程调度单实例租约（trader_state_repo 的 acquire/renew/release 决策）

背景：账本用 _revision 乐观锁；两个调度进程同写一份账本会抛
"交易账本版本冲突：存在其他写入者"（2026-09-28 实盘事故）。
【2026-09-28 按用户要求移除 G3】该冲突不再触发全局停摆，但双写本身仍会
互相覆盖账本，故租约机制保留：启动抢占、每轮续约，第二个进程检测到存活
持有者即拒绝启动，从根上杜绝双写。

本冒烟只验**纯决策函数** _lease_decide 与键构造 _lease_key，全离线：
不联网、不碰库、不发邮件、不碰真实账号（原子写入由 patch_json_config 保证，
其正确性已由 _smoke_config_store 覆盖）。

覆盖：
1. 空闲（无租约）→ 抢占成功，acquired_ts=now
2. 本进程重入 → 续约，保留 acquired_ts、刷新 renewed_ts
3. 其他进程存活持有（未过期）→ LeaseRefusedError（携带对方 holder）
4. 其他进程陈旧持有（超 TTL 未续约）→ 允许接管，acquired_ts 重置为 now
5. 过期边界（renewed+ttl == now）→ 判为已过期，可接管
6. 租约键 ≤64、按 (account,environment) 分区、稳定可复现
7. holder_id 每进程唯一
"""
import sys

sys.path.insert(0, r'd:\python\cryptoTrade')

from crypto import trader_state_repo as R  # noqa: E402

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

NOW = 1_000_000.0
ME = 'HOST:111:aaaaaaaa'
OTHER = 'HOST:222:bbbbbbbb'


# 场景1：空闲 → 抢占
lease = R._lease_decide(None, ME, ttl=180.0, now=NOW)
assert lease['holder_id'] == ME, f"场景1失败: {lease}"
assert lease['acquired_ts'] == NOW
assert lease['renewed_ts'] == NOW
assert lease['ttl'] == 180.0
print('场景1 通过：空闲账本 -> 抢占租约')

# 场景2：本进程重入 → 续约（保留 acquired_ts）
cur = {'holder_id': ME, 'acquired_ts': NOW - 500, 'renewed_ts': NOW - 60, 'ttl': 180.0}
lease = R._lease_decide(cur, ME, 180.0, NOW)
assert lease['holder_id'] == ME
assert lease['acquired_ts'] == NOW - 500, f"场景2失败：acquired_ts 未保留 {lease}"
assert lease['renewed_ts'] == NOW
print('场景2 通过：本进程续约保留 acquired_ts、刷新 renewed_ts')

# 场景3：其他进程存活持有 → 拒绝
cur = {'holder_id': OTHER, 'acquired_ts': NOW - 30, 'renewed_ts': NOW - 10, 'ttl': 180.0}
try:
    R._lease_decide(cur, ME, 180.0, NOW)
    raise AssertionError('场景3失败：存活持有者却未拒绝')
except R.LeaseRefusedError as e:
    assert e.holder.get('holder_id') == OTHER, f"场景3失败：未携带对方 holder {e.holder}"
print('场景3 通过：检测到其他存活进程 -> LeaseRefusedError（拒绝启动）')

# 场景4：其他进程陈旧持有（超 TTL）→ 接管
cur = {'holder_id': OTHER, 'acquired_ts': NOW - 1000, 'renewed_ts': NOW - 300, 'ttl': 180.0}
lease = R._lease_decide(cur, ME, 180.0, NOW)   # expires = NOW-120 < NOW
assert lease['holder_id'] == ME, f"场景4失败：陈旧租约未接管 {lease}"
assert lease['acquired_ts'] == NOW, '场景4失败：接管后 acquired_ts 应重置为 now'
print('场景4 通过：其他进程超 TTL 未续约（崩溃/卡死）-> 允许接管')

# 场景5：过期边界（renewed+ttl == now，不 > now）→ 判为已过期，可接管
cur = {'holder_id': OTHER, 'acquired_ts': NOW - 200, 'renewed_ts': NOW - 180, 'ttl': 180.0}
lease = R._lease_decide(cur, ME, 180.0, NOW)   # expires == NOW
assert lease['holder_id'] == ME, f"场景5失败：边界应判过期 {lease}"
print('场景5 通过：renewed+ttl==now 判为已过期，可接管')

# 场景6：租约键
k1 = R._lease_key('acctA', '0')
k2 = R._lease_key('acctA', '1')
k3 = R._lease_key('acctB', '0')
assert k1.startswith('trader_lease:')
assert len(k1) <= 64 and len(k2) <= 64 and len(k3) <= 64, '场景6失败：键超 KVStore 上限'
assert k1 != k2 and k1 != k3 and k2 != k3, '场景6失败：不同 (account,env) 键应互不相同'
assert k1 == R._lease_key('acctA', '0'), '场景6失败：键应稳定可复现'
print('场景6 通过：租约键 ≤64、按账号/环境分区、稳定可复现')

# 场景7：holder_id 唯一
a, b = R.make_lease_holder_id(), R.make_lease_holder_id()
assert a != b, '场景7失败：holder_id 应每次唯一'
print('场景7 通过：holder_id 每进程唯一')

print('全部冒烟通过')
