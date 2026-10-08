# -*- coding: utf-8 -*-
"""只读诊断：实盘是否被多实例并发跑（启动归因失真的根因排查工具）。

为什么需要它
------------
`process_lifecycle` 的启动归因、心跳、退出标记、自愈熔断台账都建立在
**「同一时刻只有一个 app.py 实例」** 这个前提上。一旦出现第二个实例，在
2026-09-11 之前的行为是：

1. 第二个实例启动时读到的是**第一个实例几秒前刚写的心跳**，于是被判成
   `abnormal_death`（"没有退出标记但 6s 前还在写心跳"）——它本人活得好好的，
   死的是别人；
2. 每次误判都吃掉一格自愈熔断额度（6h 内 3 次），额度耗尽后**真需要自愈时
   不再自动接回**，实盘静默停摆；
3. 更坏的是它的自愈计划是 `allowed=True`：白天等 600s 之后就真的再起一个
   调度器 → 两个调度器对同一账户下单。当天只是运气好，那两个实例都在
   600s 到点前因抢不到 Web 端口自己退了。

现在归因层已按心跳里的 pid 判活，并发实例会归到 `parallel_instance`
（不占额度、不自动拉起、但发信）。本脚本用来**确认现场**：到底有几个实例、
端口归谁、台账里还剩几条误判、什么时候才会自动接回。

本脚本一次把三件事摊开：谁是 app.py 实例、7777 端口归谁、台账与熔断余额如何。

安全边界：纯只读，不 kill 进程、不改任何状态文件、不调交易所写接口。

用法
----
    python crypto/task/_diag_process_instances.py        # 现场快照：实例数 / 端口归属 / 台账 / 熔断余额
    python crypto/task/_diag_process_instances.py ab     # 拿生产心跳做归因 A/B（新旧口径对照）

`ab` 这条不是可选项：`parallel_instance` 修复的 22 个冒烟场景当初全绿，而修复本身
是废的（伪造心跳时每条都带 pid，生产最新一条却是不带 pid 的 embedded）。只有拿真实
数据跑一次才知道接没接线。改归因逻辑后请跑它。`ab` 只读拷贝心跳到临时目录，
不碰生产状态文件，也不会真的起调度器。
"""

import datetime
import os
import sys

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (_HERE, os.path.dirname(_HERE), os.path.dirname(os.path.dirname(_HERE))):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import psutil  # noqa: E402

# 本项目入口脚本名（凡命令行里出现它的都算一个实例候选）
ENTRY_HINTS = ('app.py',)
WEB_PORT = os.environ.get('CRYPTO_WEB_PORT') or '7777'


def _fmt_ts(ts):
    if not ts:
        return '-'
    return datetime.datetime.fromtimestamp(ts).strftime('%m-%d %H:%M:%S')


def list_instances():
    """列出所有命令行里带 app.py 的进程（含父进程信息，用于追是谁起的）。

    ⚠ 必须排掉自己：判定方式是"命令行里出现 app.py"，而用 ``python -c '...app.py...'``
    写的探测脚本本身就满足这个条件（进程名也是 python）——2026-09-11 我就这样把自己
    扫成了"实例"，还据此断定"pid 每 100 秒换一个 = 启动死循环"，全是子虚乌有。
    """
    rows = []
    me = os.getpid()
    for p in psutil.process_iter(['pid', 'ppid', 'name', 'create_time', 'cmdline']):
        if p.info['pid'] == me:
            continue
        try:
            cl = ' '.join(p.info['cmdline'] or [])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if not any(h in cl for h in ENTRY_HINTS):
            continue
        if 'python' not in (p.info['name'] or '').lower():
            continue  # 排除编辑器/搜索工具顺手匹配到 app.py 的情况
        if not any(os.path.basename(t).lower() == 'app.py' for t in (cl.split() or [])):
            # 只认真正把 app.py 当入口参数传的。注意不能用"最后一个 token"判定：
            # `python app.py --flag x` 与绝对路径入口都会被那样误杀，而把实例数报成 0
            # 比报错更糟（本工具存在的意义就是数实例）。
            continue
        rows.append(p.info)
    return rows


def parent_chain(pid):
    """往上追 3 层父进程，判断是人工终端起的还是守护进程拉起的。"""
    out = []
    try:
        p = psutil.Process(pid)
    except Exception:
        return out
    for _ in range(3):
        try:
            pp = p.parent()
        except Exception:
            pp = None
        if pp is None:
            break
        try:
            out.append(f"{pp.pid}:{pp.name()}")
        except Exception:
            break
        p = pp
    return out


def port_owner(port):
    """谁在 LISTEN 这个端口 —— 端口被占说明后来者根本没起来 Web 服务。

    注意 psutil 没有 `psutil.LISTEN` 常量，状态就是字符串 'LISTEN'
    （写错会在只在多实例场景才走到的分支上抛 AttributeError）。
    """
    owners = []
    for c in psutil.net_connections(kind='tcp'):
        if c.status == 'LISTEN' and c.laddr and str(c.laddr.port) == str(port):
            owners.append(c.pid)
    return sorted(set(x for x in owners if x))


def _print_unblock(recs, now, pl, boots):
    """熔断什么时候自己松开 —— 这个数算错比不算更糟（会让人以为已经安全了）。

    口径必须照抄 ``plan_resume`` 的判定：``count >= RESUME_MAX`` 就拦，而 count
    是「窗口内自愈类记录数 **含本次** 」。将来真崩死重启时，本进程自己还会再落
    一条账，所以要求「窗口内已有老账 ≤ RESUME_MAX - 2」。上限 3 就意味着老账最多
    剩 1 条才松手。把"最早那条掉出窗口"当成解封时刻会整整早算一个窗口周期，
    我第一版就是这么错的（4 条误判 → 报了 19:05，实际要等到 23:38）。
    """
    marks = sorted(
        pl._to_epoch(r.get('ts')) for r in recs
        if r.get('reason') in pl.SELFHEAL_REASONS and pl._to_epoch(r.get('ts'))
    )
    keep = pl.RESUME_MAX - 2          # 窗口内允许保留的老账条数（不含未来那次重启）
    idx = len(marks) - keep - 1       # 第 keep+1 新的那条：它掉出窗口才解封
    if keep < 0 or idx < 0:
        print(f"  窗口内老账 {len(marks)} 条，尚未触顶（本次计数含自身为 {boots}）")
        return
    unblock = marks[idx] + pl.RESUME_WINDOW_SEC + 1
    # +1s 的两层来由：判定用的是 ``now - ts > window_sec``（严格大于），边界那一秒仍计数；
    # 且台账里的 ts 是截到整秒的字符串，真实落盘时刻可能比它晚不到 1s。
    # 别把这里报的时刻当成"精确到秒的承诺"——它是保守估计，实测只会更晚不会更早。
    # （注释曾经写过"23:38:38 才放行"，那是模拟脚本恰好采样到的时刻，不是翻转时刻，
    #   照它理解就会把一个 1s 的边界误差说成 19s。）
    print(f"  ⚠ 已熔断：要等窗口内老账只剩 {keep} 条才会自动接回，"
          f"最早约 {_fmt_ts(unblock)}（还有 {max(0.0, unblock - now) / 3600:.1f} 小时）")
    print(f"     计数依据（自愈类启动时刻）: {[f'{_fmt_ts(m)}' for m in marks]}")
    print(f"     这期间若真发生崩溃：不会自动接回，但会发信求救；"
          f"人工在 /task 点「启动交易」立刻生效，不受此限制")
    print(f"     明早 04:03 的每日归零重启属 daily_restart，不占自愈额度、不受熔断影响")


def show_ledger():
    """启动台账 + 熔断余额（复用被测模块自己的口径，别另算一套）。"""
    try:
        from crypto import process_lifecycle as pl
    except Exception as e:
        try:
            import process_lifecycle as pl
        except Exception as e2:
            print(f"  (无法导入 process_lifecycle: {e2})")
            return
    try:
        recs = pl.read_ledger()
    except Exception as e:
        print(f"  (读台账失败: {e})")
        return
    now = __import__('time').time()
    print(f"\n──── 启动台账（最近 {len(recs)} 条） ────")
    for r in recs[-12:]:
        print(f"  seq={r.get('seq'):<4} {r.get('ts')} {str(r.get('reason')):<15} "
              f"pid={r.get('pid')} prev_exit={r.get('prev_exit')} "
              f"心跳滞后={r.get('heartbeat_lag_sec')}s")
    try:
        boots = pl.selfheal_boots(now=now)
        print(f"\n──── 自愈熔断余额 ────")
        print(f"  窗口内自愈类启动次数 = {boots} / 上限 {pl.RESUME_MAX}"
              f"（窗口 {pl.RESUME_WINDOW_SEC // 3600}h）")
        if boots >= pl.RESUME_MAX:
            _print_unblock(recs, now, pl, boots)
    except Exception as e:
        print(f"  (算熔断余额失败: {e})")
    try:
        marker = pl.read_exit_marker()
        print(f"\n──── 退出标记 ────\n  {marker}")
    except Exception as e:
        print(f"  (读退出标记失败: {e})")


def list_other_pythons():
    """列出其余 python 进程：只按 'app.py' 匹配会把"用别的方式起本项目"
    的实例漏掉（换入口文件名、用 -c/-m 起、或命令行被改写），这里兜底摊开。"""
    rows = []
    for p in psutil.process_iter(['pid', 'ppid', 'name', 'create_time', 'cmdline']):
        try:
            nm = (p.info['name'] or '').lower()
            cl = ' '.join(p.info['cmdline'] or [])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if 'python' not in nm or not cl:
            continue
        if any(h in cl for h in ENTRY_HINTS):
            continue
        rows.append((p.info['pid'], p.info['ppid'], p.info['create_time'], cl))
    return rows


def run_ab_child(pl):
    """子进程里跑：用刚拷进临时目录的真实心跳，A/B 对照新旧两套归因。

    必须是**真新进程**（不是在本函数里改改全局变量）：归因要用「本进程出生时刻」
    排除自身记录、比对外部进程创建时间，只有真起一个进程才是 faithful 的现场。
    """
    info = pl.classify_boot()
    plan = pl.plan_resume(info)
    print(f'[A] 现口径归因   : {info["reason"]}')
    print(f'    detail       : {info.get("detail")}')
    print(f'    心跳作者 pid : {info.get("heartbeat_pid")}'
          f'（该条心跳距今 {info.get("heartbeat_pid_age_sec")}s；'
          f'最新一条心跳距今 {info.get("heartbeat_lag_sec")}s）')
    print(f'    计划         : allowed={plan["allowed"]} pre_notify={plan["pre_notify"]} '
          f'silent={plan["silent"]} selfheal_count={plan["selfheal_count"]}')

    _real = pl._pid_alive
    pl._pid_alive = lambda pid, born_before=None: False      # 等价于"从不问一句存活"
    try:
        info_b = pl.classify_boot()
        plan_b = pl.plan_resume(info_b)
    finally:
        pl._pid_alive = _real
    print(f'[B] 旧口径归因   : {info_b["reason"]} → allowed={plan_b["allowed"]} '
          f'selfheal_count={plan_b["selfheal_count"]}（会占额度，白天 600s 后起第二个调度器）')
    same = info['reason'] == info_b['reason']
    print('\n结论: ' + ('A 与 B 相同 —— 说明这次没触发并发分支（正常：同机只有 '
                        '一个实例在跑时本就该走老口径）'
                        if same else
                        'A 认出发的是并发不是暴死：不占额度、不下单、发信说明；'
                        'B 是改前行为（会误吃一格熔断额度）'))
    return 0


def run_ab():
    """`... _diag_process_instances.py ab` —— 拿生产心跳复现"再起一个实例"。

    为什么要固化成子命令：这条修复的冒烟场景（22 项）当初全绿，而修复本身是废的
    —— 伪造心跳时给每条都写了 pid，生产最新一条却是 embedded（不带 pid）。
    只有拿真实数据跑一次才知道接没接线。以后改归因，跑这一条。
    """
    import shutil
    import subprocess
    import tempfile

    try:
        from crypto import process_lifecycle as pl
    except Exception:
        import process_lifecycle as pl
    prod_hb = pl.heartbeat_path()          # 此刻 LOG_DIR 还是生产目录
    if not os.path.exists(prod_hb):
        print(f'找不到生产心跳文件：{prod_hb}')
        return 1
    tmp = tempfile.mkdtemp(prefix='ab_boot_')
    try:
        shutil.copy(prod_hb, os.path.join(tmp, pl.HEARTBEAT_FILE_NAME))
        print(f'心跳来源（只读拷贝）: {prod_hb}')
        print(f'末行: '
              f'{shim_tail(prod_hb)}')
        env = dict(os.environ, CRYPTO_LIFECYCLE_DIR=tmp)
        r = subprocess.run([sys.executable, os.path.abspath(__file__), '--ab-child'],
                           env=env, capture_output=True, text=True, encoding='utf-8')
        print(r.stdout, end='')
        if r.stderr.strip():
            print(r.stderr)
        return r.returncode
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def shim_tail(path):
    try:
        with open(path, 'rb') as f:
            f.seek(max(0, os.path.getsize(path) - 400))
            return f.read().decode('utf-8', 'replace').strip().splitlines()[-1]
    except Exception as e:
        return f'(读末行失败: {e})'


def main():
    if len(sys.argv) > 2 and sys.argv[2] == '--ab-child' or \
            len(sys.argv) > 1 and sys.argv[1] == '--ab-child':
        try:
            from crypto import process_lifecycle as pl
        except Exception:
            import process_lifecycle as pl
        return run_ab_child(pl)
    if len(sys.argv) > 1 and sys.argv[1].lower() == 'ab':
        return run_ab()

    rows = list_instances()
    owners = port_owner(WEB_PORT)
    print(f"──── app.py 实例（命令行含 {'/'.join(ENTRY_HINTS)}）：{len(rows)} 个 ────")
    for info in rows:
        me = info['pid']
        try:
            rss = psutil.Process(me).memory_info().rss / 1024 / 1024
        except Exception:
            rss = 0
        mark = '  ← 持有 Web 端口' if me in owners else '  ← 没抢到端口（Web 起不来，但线程照跑）'
        print(f"  pid={me:<6} 起于 {_fmt_ts(info['create_time'])} RSS={rss:6.0f}MB"
              f" 父进程链={'>'.join(parent_chain(me)) or '?'}{mark}")
        print(f"        cmd={' '.join(info['cmdline'] or [])[:150]}")

    others = list_other_pythons()
    if others:
        print(f"\n──── 其它 python 进程（不属于本项目入口，仅备查）：{len(others)} 个 ────")
        for pid, ppid, ct, cl in others:
            print(f"  pid={pid:<6} ppid={ppid:<6} 起于 {_fmt_ts(ct)}  {cl[:120]}")

    if len(rows) > 1:
        print(f"\n⚠ 多实例并发！启动归因/心跳/熔断台账都以单实例为前提，")
        print(f"  后来者会读到前者的新鲜心跳被误判成 abnormal_death，每误判一次吃一格自愈额度。")
        print(f"  先确认哪个是你要留的（通常是持有 {WEB_PORT} 端口那个），再处理其余的。")
    elif not rows:
        print("\n⚠ 一个 app.py 实例都没有：实盘没在跑。")
    else:
        print(f"\n单实例，正常。")

    print(f"\n端口 {WEB_PORT} 的 LISTEN 归属: {owners or '无人监听'}")
    show_ledger()
    return 0


if __name__ == '__main__':
    sys.exit(main())
