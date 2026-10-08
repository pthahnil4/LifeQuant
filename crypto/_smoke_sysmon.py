#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""冒烟测试：内存自监控 + 系统监控（不触发真实邮件）"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 告警主题里带 emoji（🚨），断言会把它原样打印出来；Windows 控制台默认 GBK，
# 直接 print 会抛 UnicodeEncodeError 让整个脚本崩溃。与 task/utils/logger.py
# 的控制台处理保持一致，强制切到 UTF-8。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding='utf-8')
    except Exception:
        pass

from crypto import memory_watchdog as mw
from crypto import system_monitor as sm

failures = []


def check(name, cond, detail=''):
    print(f"{'[OK]  ' if cond else '[FAIL]'} {name}" + (f"  {detail}" if detail else ''))
    if not cond:
        failures.append(name)


# 1. RSS 读取（跨平台）
rss = mw.get_process_rss_mb()
check('当前进程RSS读取', rss is not None and rss > 0, f'{rss:.1f}MB')

# 2. JSONL 写入与读取
mw.append_history_record({'timestamp': '2026-08-28 10:00:00', 'rss_mb': 123.4, 'source': 'app', 'pid': 1})
mw.append_history_record({'timestamp': '2026-08-28 10:01:00', 'rss_mb': 234.5, 'source': 'guard', 'mem_usage_pct': 55.0})
records = mw.read_history(limit=100)
check('JSONL写入读取', len(records) >= 2, f'共{len(records)}条')
check('JSONL按source过滤', len(mw.read_history(limit=100, source='guard')) >= 1)

# 3. 状态摘要
st = mw.get_memory_status()
check('get_memory_status字段', all(k in st for k in ('rss_mb', 'warn_mb', 'kill_mb', 'history_file')))
print('   ', {k: st[k] for k in ('rss_mb', 'warn_mb', 'kill_mb')})

# 4. 系统检查各读取函数
mem_pct, swap_pct = sm.read_system_memory()
check('系统内存读取', mem_pct is not None, f'mem={mem_pct}, swap={swap_pct}')
disk_pct, free_gb = sm.read_disk_usage()
check('磁盘读取', disk_pct is not None, f'disk={disk_pct:.1f}%, free={free_gb:.1f}GB')

# 记录测试前历史行数，用于结束后清理测试数据产生的记录
_pre_test_lines = 0
if os.path.exists(mw.history_file_path()):
    with open(mw.history_file_path(), encoding='utf-8') as f:
        _pre_test_lines = len(f.readlines())

# 5. run_check 全流程（阈值全部抬高到不可能触发，确保不发邮件）
sm.MEM_USAGE_PCT = 101
sm.SWAP_USAGE_PCT = 101
sm.DISK_FREE_PCT = -1
sm.LOAD_PER_CPU = 999
sm.PROCESS_WARN_MB = 999999
result = sm.run_check(source='embedded')
check('run_check(embedded)无异常', not result['issues'], str(result['snapshot']))
check('run_check快照含磁盘', 'disk_usage_pct' in result['snapshot'])
result2 = sm.run_check(source='guard')
check('run_check(guard)执行成功', 'checked_at' in result2,
      f"process_alive={result2['process_alive']}")

# 5b. 分级告警策略验证（拦截邮件发送，不发真实邮件）
_real_send = sm.send_alert_email
sent_topics = []
sm.send_alert_email = lambda topic, content: (sent_topics.append(topic), True)[1]
_real_read_mem = sm.read_system_memory
# 冷却状态必须隔离到临时文件：run_check 会读写真实冷却文件，旧写法有两个问题
# —— ① 最近 30 分钟内真发过告警（或上一次跑本脚本写脏）时，“应发邮件”必然
# 假失败；② 脚本 stub 返回 True 后会把冷却记录落到真实文件，反而把消费端的
# 真告警压掉 30 分钟。改指临时路径后两者都消失，也不再需要删真实状态文件。
_real_state_path = sm._state_path
_state_tmp = os.path.join(tempfile.gettempdir(), 'smoke_sysmon_cooldown.json')
if os.path.exists(_state_tmp):
    os.remove(_state_tmp)
sm._state_path = lambda: _state_tmp
try:
    # 严重：内存 95% ≥ 90% → 发邮件，且写入 issues 字段
    sm.MEM_USAGE_PCT = 90
    sm.read_system_memory = lambda: (95.0, 10.0)
    r3 = sm.run_check(source='embedded')
    check('严重异常(内存≥90%)触发邮件', r3['emailed'] and sent_topics,
          str(sent_topics))
    check('严重异常写入issues', any('内存使用率' in t for t in r3['issues']))

    # 一般：内存正常但 SWAP 偏高 → 不发邮件，仅记录
    sent_topics.clear()
    sm.MEM_USAGE_PCT = 101
    sm.SWAP_USAGE_PCT = 20
    sm.read_system_memory = lambda: (50.0, 60.0)
    r4 = sm.run_check(source='embedded')
    check('一般异常(SWAP)不发邮件', not r4['emailed'] and not sent_topics)
    check('一般异常仅写入issues', any('SWAP' in t for t in r4['issues']))
finally:
    sm.send_alert_email = _real_send
    sm.read_system_memory = _real_read_mem
    sm._state_path = _real_state_path
    if os.path.exists(_state_tmp):
        os.remove(_state_tmp)

# 5c. 清理测试产生的记录与冷却状态（含第2步的两条假采样，保留原有真实采样）
with open(mw.history_file_path(), encoding='utf-8') as f:
    _lines = f.readlines()
_keep = [l for l in _lines[:_pre_test_lines]
         if '"timestamp": "2026-08-28 10:00:00"' not in l
         and '"timestamp": "2026-08-28 10:01:00"' not in l]
with open(mw.history_file_path(), 'w', encoding='utf-8') as f:
    f.writelines(_keep)
# 冷却状态已在 5b 里隔到临时文件并随 finally 删除，不能再动真实状态文件

# 6. watchdog 启动幂等性（线程级，只验证状态标记）
mw.start_memory_watchdog(interval_sec=3600)
mw.start_memory_watchdog(interval_sec=3600)
check('watchdog幂等启动', mw._state['started'] is True)

print()
if failures:
    print(f'冒烟测试失败: {failures}')
    sys.exit(1)
print('冒烟测试全部通过')
