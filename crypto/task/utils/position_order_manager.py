#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
双仓位限价挂单管理器
=====================

为定时任务提供两个**互相独立**的仓位账本与限价挂单生命周期管理：

┌──────────────┬────────────────────────────┬──────────────────────────────┐
│ 篮子         │ 趋势跟踪 trend             │ 区间波动 range               │
├──────────────┼────────────────────────────┼──────────────────────────────┤
│ 信号来源     │ 趋势策略（Pro3）           │ BOLL 边界                    │
│ 方向         │ single=双向 / dual=单向    │ 单向（长周期方向）           │
│ 开仓价       │ 反转bar 的 open/close      │ 长多→下轨 / 长空→上轨        │
│ 平仓价       │ 反转bar 的 open/close      │ 对侧边界（长多→上轨）        │
│ 追价         │ 不追价（宁愿错过）         │ 随边界移动 amend 追价        │
│ 循环         │ 一次信号一次挂单           │ 平仓成交→立即重挂开仓（循环）│
└──────────────┴────────────────────────────┴──────────────────────────────┘

交易所层面隔离（本地记账方案）：
- 账户为净持仓模式，多头统一走全仓(cross)、空头统一走逐仓(isolated)。
- 两篮子方向相同时共享同一个净头寸，因此各自持仓张数(held)与**加权入场均价
  (avg_px)** 全部由本地账本记录 —— 交易所返回的 avgPx 是两篮子混合均价，
  不能用于单个篮子的盈亏/止盈计算。
- 平仓(reduce-only)挂单总量按「真实同向持仓 − 其他篮子已挂平仓量」封顶，
  避免两篮子同时挂单导致计划平仓量超过净持仓。
- 每轮开始调用 reconcile() 用真实持仓给账本封顶，防止手动平仓/强平/爆仓
  造成账本长期虚高。

状态持久化：按账号和环境隔离的 V2 KV 账本；发送请求前保存订单意图，
成交水位与账本同事务落库。恢复/保存失败时停止交易。旧四表仅供兼容工具读取。

作者：AI Assistant
创建时间：2026-07-28
"""

import time
import threading
import uuid
import math
import copy
from typing import Dict, Optional

try:
    from .logger import get_task_logger, get_trade_logger
    from .trade_journal import record_fill as journal_fill
except ImportError:
    from logger import get_task_logger, get_trade_logger
    from trade_journal import record_fill as journal_fill

# 交易状态持久化不可用时必须停止交易，不能按空仓启动或仅靠内存下单。
try:
    from crypto.database import session_scope
    from crypto import trader_state_repo as state_repo
except ImportError:
    session_scope = None
    state_repo = None

task_log = get_task_logger()
trade_log = get_trade_logger()


# 篮子标识
BUCKET_TREND = 'trend'
BUCKET_RANGE = 'range'
BUCKETS = (BUCKET_TREND, BUCKET_RANGE)

# 每个篮子的槽位（一个开仓槽 + 一个平仓槽）
SLOT_ENTRY = 'entry'
SLOT_EXIT = 'exit'
SLOTS = (SLOT_ENTRY, SLOT_EXIT)

# 槽位状态机：IDLE(未挂) → PENDING(已挂未成交) → FILLED(已成交) / EXPIRED(已撤销)
ST_IDLE = 'IDLE'
ST_PENDING = 'PENDING'
ST_FILLED = 'FILLED'
ST_EXPIRED = 'EXPIRED'

# 中文标签（生命周期日志可读性）
BUCKET_LABEL = {BUCKET_TREND: '趋势跟踪', BUCKET_RANGE: '区间波动'}
SLOT_LABEL = {SLOT_ENTRY: '开仓', SLOT_EXIT: '平仓'}


class DualPositionOrderManager:
    """趋势跟踪 + 区间波动 双仓位挂单与账本管理器"""

    # 「下单结果未知」孤儿单结算窗口（秒）：只带 clOrdId、从没拿到 ordId 的在途单，
    # 若交易所**权威地**回答“查无此单”（probe_order_by_cl_id 返回 absent），且存续已超过
    # 本窗口（覆盖 OKX 传播/归档延迟 + 数个调度轮），即判定其从未落地 → 主动过期释放币种，
    # 避免 SSL 断连这类“请求根本没到交易所”的失败把币种永久锁死在“禁止交易”。
    # 查询本身失败(unknown/error)不在此列，仍保守保留占位，绝不误清、绝不误重发。
    UNKNOWN_ABSENT_SETTLE_SEC = 300.0

    def __init__(self, trade_executor, state_file: str = None, spec_cache=None,
                 account=None, environment='0'):
        """
        Args:
            trade_executor: TradeExecutor 实例（复用下单/查单/撤单/改单能力）
            state_file: 已废弃（保留签名兼容旧调用方）——状态已迁移 MySQL
            spec_cache: InstrumentSpecCache 实例，提供各合约 lotSz/minSz；
                缺省时全部按 0.1 张步长兜底（存量行为，仅供无规格的测试场景）
        """
        self.executor = trade_executor
        self.spec_cache = spec_cache
        self._lock = threading.RLock()
        self.state_file = state_file  # 仅兼容保留，不再读写文件
        self.account = account
        self.environment = str(environment)
        # 【2026-09-28 按用户要求彻底移除 G1/G2/G3 闸门】不再维护 _storage_ok /
        # _storage_error / _storage_fatal / _storage_transient / _sync_ok /
        # _account_sync_ok：账本写失败或对账失败都不停单、不隔离币种，交易一律
        # 按内存账本继续（DB 恢复后下一次 _save 自动把增量补写回去）。
        self._entry_blocked = set()
        self._targets = {}
        # 开仓额度归零诊断：_entry_room 写入具体原因，_place_entry 据此补一行日志；
        # 独立字典做同原因去重，不污染持久化账本 state。
        self._last_room_reason = ''
        # 本轮最近一次额度计算的明细（目标/已占用/账本外/可补），供挂单日志
        # 拼出“目标5 已占用2(含人工2) 本次补3”的算账三段式
        self._last_room_detail = {}
        self._entry_block_logged = {}
        self._fill_generation = 0
        self.manual_pause_seconds = 1800
        self.close_pause_seconds = 60
        # 币种级对账结论 {inst_id: 'ok'|'absent'|'syncing'}：仅保留 absent（幽灵合约/
        # 已移除币种的账本残留，零阻断权、不占额度）；已移除 unresolved(G1 隔离) 语义。
        self._inst_sync_state = {}
        # 本轮检测到“系统外平仓（手动/强平）”的币种：用于把冗余平仓单的
        # 撤销原因写成可审计的“检测到人工平仓事实”，而不是笼统的“持仓已清零”。
        self._manual_close_seen = set()
        self.state = self._load_state()
        self._saved_state = copy.deepcopy(self.state)
        self._run_id = ''
        self._verbose = True
        # 本轮"下单出口失败"暂存（供调度轮排空后升级为告警邮件）：
        # 仅记录 execute_trade/execute_reduce_only_order 真正被交易所拒/异常，
        # 风控主动拦截、量为0的正常跳过不计入，避免把"本就不该下单"误报成故障。
        # need_verify=True 表示"结果未知、该单可能已落地"，需单独升级人工核实邮件。
        self._round_place_failures = []
        # 本轮"账本与真实持仓大幅背离"暂存（供调度轮排空后升级为告警邮件）：
        # reconcile 缩减/吸收分支每次命中都记一条原始事实（不做阈值判断，阈值在交易
        # 侧配置热加载），仅记账不改变对账行为。缩减≈强平/爆仓/手动平仓（危险），
        # 吸收≈人工同向加仓（提示）。
        self._round_divergences = []
        # 杠杆回核时间戳（内存态）：{inst_id:mode -> 上次回交易所核对的秒级时间}。
        # lev_set 缓存只能证明“本进程设过”，人工在 App 改杠杆、交易所侧重置
        # 或账户杠杆模式变更都会使缓存失真，盲信缓存会一直沿用错误杠杆
        # 开仓（2026-09-01 排查结论），故需周期性回交易所核对
        self._lev_verified = {}

    # =================================================================
    # 下单量步长（按合约规格，绝不硬编码）
    # =================================================================

    # 无规格来源时的兜底步长（张）与“视为无持仓”的粉尘阈值（张）
    FALLBACK_LOT_SZ = 0.1
    POS_DUST = 1e-10

    def _steps(self, inst_id: str) -> tuple:
        """该合约的 (lotSz 步长, minSz 最小下单量)。

        各合约步长差异极大（XRP=0.01 / NEAR=0.1 / POL=1），历史实现把 0.1 张
        当成通用最小步长，导致：XRP 折算出的 0.03 张被 round(…,1) 抹成 0，
        区间仓永远不挂单（表现为“仓位B纹丝不动”）；POL 配置的 0.5 张低于其
        minSz=1，每轮下单必被交易所拒（2026-09-01 排查结论）。
        """
        if self.spec_cache is not None:
            try:
                return self.spec_cache.steps(inst_id)
            except Exception as e:
                task_log.warning(f"[双仓位] {inst_id} 读取合约步长失败，按兜底0.1张: {e}")
        return self.FALLBACK_LOT_SZ, self.FALLBACK_LOT_SZ

    def _q(self, inst_id: str, amount: float) -> float:
        """下单量向下取整到该合约 lotSz 步长；低于 minSz 返回 0（不可下单）。"""
        amount = float(amount or 0)
        if amount <= 0:
            return 0.0
        if self.spec_cache is not None:
            try:
                return self.spec_cache.quantize(inst_id, amount)
            except Exception:
                pass
        lot, _ = self._steps(inst_id)
        out = round(int(amount / lot + 1e-9) * lot, 12)
        return out if out >= lot - 1e-9 else 0.0

    def _in_pos(self, held: float) -> bool:
        """仅忽略浮点残差；低于最小下单量的真实仓位也不能从账本抹去。"""
        return float(held or 0) > self.POS_DUST

    # =================================================================
    # 状态持久化与结构初始化（MySQL：迁移批次5）
    # =================================================================

    def _load_state(self) -> Dict:
        try:
            with session_scope() as session:
                return state_repo.load_position_state(session, **self._scope())
        except Exception as e:
            raise RuntimeError('交易状态恢复失败，禁止按空仓启动') from e

    def _scope(self):
        return ({'account': self.account, 'environment': self.environment}
                if self.account is not None else {})

    def _save(self, inst_id: str = None):
        """立即落库。传 inst_id 时只写该合约（单次 ≤9 行，交易链路常态），
        不传时整树重写（仅迁移/兜底场景）。

        【为何全程持 self._lock】2026-09-28 单进程反复“版本冲突”真凶：
        save_inst_position 的乐观锁 expected=int(state['_revision']) 是在拿 DB
        行锁【之前】从内存读的。若 _save 不串行化，同进程两线程同时保存同一币
        会都读到同一个旧 expected，后提交者必被误判“存在其他写入者”→假的版本
        冲突→G3 致命停摆。把“读内存版本→写库→回写内存版本”整段关进临界区，
        同进程内就彻底串行。RLock 可重入：synchronize/close_market/poll_fills 等
        已持锁的调用方再进 _save 不会死锁。锁内只做一次小写入（≤9 行），不拖性能。
        """
        with self._lock:
            saved = self._saved_state.get(inst_id) if inst_id else self._saved_state
            current = self.state.get(inst_id) if inst_id else self.state
            if saved == current:
                return True
            try:
                return self._write_state(inst_id)
            except RuntimeError as e:
                # 【2026-09-28 根治版本冲突刷屏】单币账本内存版本号陈旧：典型场景是
                # 本进程启动加载账本时，被 TTL 接管的旧进程仍在写库，接管后内存
                # expected 永久停在旧值 → 每次 _save 都撞“存在其他写入者”，而旧实现
                # 不重载版本号 → 死循环刷屏且 DB 永不落库。自愈：从库读回该币权威
                # _revision、采用后重试一次，修复版本链，后续保存恢复正常。
                if inst_id and self.account is not None and '版本冲突' in str(e):
                    if self._resync_revision(inst_id):
                        try:
                            return self._write_state(inst_id)
                        except Exception as e2:
                            task_log.error(
                                f"[双仓位] {inst_id} 账本落库失败（版本号自愈后仍失败，不停单，"
                                f"按内存账本继续，恢复后自动补写）: {e2}")
                            return False
                # 【2026-09-28 按用户要求移除 G3】账本落库失败不再 latch、不再停摆、
                # 不再区分致命/瞬时：只记一行日志，交易按内存账本照常继续；DB/网络
                # 恢复后，下一次 _save 会把未落库的增量自动补写回去。
                task_log.error(f"[双仓位] 账本落库失败（不停单，按内存账本继续，恢复后自动补写）: {e}")
                return False
            except Exception as e:
                task_log.error(f"[双仓位] 账本落库失败（不停单，按内存账本继续，恢复后自动补写）: {e}")
                return False

    def _write_state(self, inst_id: str = None) -> bool:
        """执行一次账本落库（调用方须持 self._lock）。成功则回写内存 _revision
        与 _saved_state 快照并返回 True；失败抛异常交 _save 统一处置。"""
        with session_scope() as session:
            if inst_id:
                revision = state_repo.save_inst_position(
                    session, inst_id, self.state.get(inst_id) or {}, **self._scope())
            else:
                revision = state_repo.save_position_state(session, self.state, **self._scope())
        if self.account is not None:
            for inst, value in ({inst_id: revision} if inst_id else revision).items():
                self.state[inst]['_revision'] = value
        if inst_id:
            self._saved_state[inst_id] = copy.deepcopy(self.state[inst_id])
        else:
            self._saved_state = copy.deepcopy(self.state)
        return True

    def _resync_revision(self, inst_id: str) -> bool:
        """从库读回该币权威 _revision 并同步到内存，修复陈旧版本链（调用方须持
        self._lock）。返回 True=已同步到可用版本号、可重试落库；False=读取失败/
        账本损坏，不重试，交上层记错误。"""
        if self.account is None:
            return False
        try:
            with session_scope() as session:
                rev = state_repo.load_inst_revision(session, inst_id, **self._scope())
        except Exception as e:
            task_log.error(f"[双仓位] {inst_id} 重载账本版本号失败: {e}")
            return False
        if rev is None:
            return False
        st = self.state.get(inst_id)
        if not isinstance(st, dict):
            return False
        old = int(st.get('_revision', 0) or 0)
        st['_revision'] = int(rev)
        if old != int(rev):
            task_log.warning(
                f"[双仓位] {inst_id} 内存账本版本号 {old} 落后于库内 {rev}，已同步后重试落库"
                f"（多为接管旧进程残留写入，已自愈）")
        return True

    def reload_state(self):
        """从 DB 重新加载整份账本到内存（含权威 _revision）。用于启动取得租约、
        确认本进程为唯一持有者之后，把内存基线对齐到库内最新值——堵住“加载账本时
        被接管的旧进程仍在写库、导致内存版本号永久陈旧”的窗口。仅启动阶段调用。"""
        with self._lock:
            fresh = self._load_state()
            self.state = fresh
            self._saved_state = copy.deepcopy(fresh)

    @staticmethod
    def _new_slot() -> Dict:
        return {'state': ST_IDLE, 'ord_id': None, 'price': 0.0, 'amount': 0.0,
                'placed_ts': 0.0, 'acc_filled': 0.0, 'dir': None,
                'cl_ord_id': None, 'fill_notional': 0.0,
                'unknown': False, 'cancel_requested': False, 'market': False}

    def _new_bucket(self) -> Dict:
        """篮子账本。held/avg_px 按方向分开记账 —— 单周期双向模式下方向翻转期间
        旧向持仓（平仓单未成交）与新向持仓可能同时存在，单一方向字段无法表达。"""
        return {
            'held': {'long': 0.0, 'short': 0.0},    # 各方向持仓张数（本地账本）
            'avg_px': {'long': 0.0, 'short': 0.0},  # 各方向加权入场均价（本地账本）
            'slots': {s: self._new_slot() for s in SLOTS},
            'prev_open_confirmed': False,
            'prev_close_confirmed': False,
            'last_desired': None,   # 上一轮期望方向（用于识别新反转信号）
            # 交易所侧兜底委托 {'long': {...}, 'short': {...}}
            'algo': {'long': None, 'short': None},
        }

    def _inst(self, inst_id: str) -> Dict:
        """获取/初始化某合约的状态（兼容旧结构缺字段）"""
        s = self.state.setdefault(inst_id, {})
        for b in BUCKETS:
            if not isinstance(s.get(b), dict):
                s[b] = self._new_bucket()
            bk = s[b]
            for fld in ('held', 'avg_px'):
                if not isinstance(bk.get(fld), dict):
                    bk[fld] = {'long': 0.0, 'short': 0.0}
                for d in ('long', 'short'):
                    bk[fld].setdefault(d, 0.0)
            if not isinstance(bk.get('algo'), dict):
                bk['algo'] = {'long': None, 'short': None}
            for d in ('long', 'short'):
                bk['algo'].setdefault(d, None)
            bk.setdefault('prev_open_confirmed', False)
            bk.setdefault('prev_close_confirmed', False)
            bk.setdefault('last_desired', None)
            if not isinstance(bk.get('slots'), dict):
                bk['slots'] = {sl: self._new_slot() for sl in SLOTS}
            for sl in SLOTS:
                if not isinstance(bk['slots'].get(sl), dict):
                    bk['slots'][sl] = self._new_slot()
                else:
                    for k, v in self._new_slot().items():
                        bk['slots'][sl].setdefault(k, v)
        s.setdefault('lev_set', None)
        return s

    # =================================================================
    # 日志
    # =================================================================

    def set_context(self, run_id: str = '', verbose: bool = True):
        """设置本轮日志上下文（调度轮次ID + 生命周期日志详细开关）"""
        new_round = (run_id or '') != self._run_id
        self._run_id = run_id or ''
        self._verbose = bool(verbose)
        # 每轮开始清空上一轮遗留的下单失败记录，避免跨轮重复计入
        self._round_place_failures = []
        # 同上：账本背离事件也按币、按轮清一次（本轮仅在动作前那次 reconcile 排空告警，
        # 平仓动作后 _refresh_positions 的再对账属程序自身行为，不计入背离告警）
        self._round_divergences = []
        # 币种级对账结论与人工平仓标记均为“本轮事实”，跨轮不沿用（否则上一轮
        # 失败的币会被永久隔离，达不到自愈目的）
        self._manual_close_seen = set()
        if new_round:
            self._entry_blocked.clear()
            # 重新赋值而非 .clear()：__new__ 裸实例桩上也能跑（项目冒烟惯例）
            self._inst_sync_state = {}
            self._targets.clear()
        # settlement_pending / external_order_watch 属于风险状态，不随日志上下文清除。

    def _note_place_failure(self, inst_id: str, bucket: str, slot: str,
                            amount: float, price: float, error,
                            need_verify: bool = False):
        """暂存一次下单出口失败（bucket/slot 定位是哪个篮子哪种单）。"""
        self._round_place_failures.append({
            'inst_id': inst_id, 'bucket': bucket, 'slot': slot,
            'amount': float(amount or 0), 'price': float(price or 0),
            'error': str(error or '无返回'), 'need_verify': bool(need_verify)})

    def drain_place_failures(self):
        """取出并清空本轮累计的下单失败记录（调度轮在挂单阶段结束后调用）。"""
        with self._lock:
            out = self._round_place_failures
            self._round_place_failures = []
            return out

    def drain_divergences(self):
        """取出并清空本轮累计的账本背离事件（调度轮在动作前对账之后调用）。"""
        with self._lock:
            out = self._round_divergences
            self._round_divergences = []
            return out

    def _rid(self) -> str:
        """轮次ID前缀（场景日志统一格式）"""
        return f"[{self._run_id}] " if self._run_id else ''

    def _life(self, msg: str, key: bool = False):
        """挂单生命周期场景日志（key=True 的关键事件不受 verbose 开关约束）。

        msg 统一场景模板：'{inst_id} | 【动作】仓位 | 数量 @ 价格 | 原因：xxx'
        """
        if not (self._verbose or key):
            return
        task_log.info(f"{self._rid()}{msg}")

    @staticmethod
    def _tag(bucket: str, slot: str) -> str:
        return f"{BUCKET_LABEL.get(bucket, bucket)}·{SLOT_LABEL.get(slot, slot)}"

    # =================================================================
    # 对外：账本查询与外部平仓同步
    # =================================================================

    def get_book(self, inst_id: str, bucket: str) -> Dict:
        """读取篮子账本 {'held': {long,short}, 'avg_px': {long,short}}"""
        with self._lock:
            bk = self._inst(inst_id)[bucket]
            return {'held': {d: round(float(bk['held'].get(d, 0) or 0), 12)
                             for d in ('long', 'short')},
                    'avg_px': {d: float(bk['avg_px'].get(d, 0) or 0)
                               for d in ('long', 'short')}}

    def get_position(self, inst_id: str, bucket: str, direction: str) -> tuple:
        """读取某篮子某方向的 (持仓张数, 加权均价)"""
        with self._lock:
            bk = self._inst(inst_id)[bucket]
            return (round(float(bk['held'].get(direction, 0) or 0), 12),
                    float(bk['avg_px'].get(direction, 0) or 0))

    def algo_in_place(self, inst_id: str, bucket: str, direction: str) -> bool:
        """交易所侧兜底委托是否确实在位（有 algo_id）。
        供"兜底委托不在位"告警判定：sync 后仍为 False 且本轮确有持仓+有效触发价，
        即说明这层"宕机保险"没挂上（下单失败/被撤后重挂又失败）。"""
        with self._lock:
            rec = self._inst(inst_id)[bucket]['algo'].get(direction)
            return bool(rec and rec.get('algo_id'))

    def pending_snapshot(self, inst_id: str, bucket: str) -> Dict:
        """槽位状态快照（供日志展示）"""
        with self._lock:
            bk = self._inst(inst_id)[bucket]
            now = time.time()
            snap = {}
            for sl in SLOTS:
                d = bk['slots'][sl]
                age = int(now - float(d.get('placed_ts', 0) or 0)) if (
                    d.get('state') == ST_PENDING and d.get('placed_ts')) else 0
                snap[sl] = {'state': d.get('state'), 'price': float(d.get('price', 0) or 0),
                            'amount': float(d.get('amount', 0) or 0), 'age_sec': age,
                            'dir': d.get('dir')}
            return snap

    def note_external_close(self, inst_id: str, bucket: str, direction: str,
                            amount: float, reason: str = ''):
        """仅兼容旧离线工具；在线账本必须通过成交水位或真实持仓对账更新。"""
        if self.account is not None:
            raise RuntimeError('在线账本禁止手工扣减，请调用 synchronize')
        with self._lock:
            bk = self._inst(inst_id)[bucket]
            before = float(bk['held'].get(direction, 0) or 0)
            bk['held'][direction] = round(
                max(0.0, before - max(0.0, float(amount or 0))), 12)
            if not self._in_pos(bk['held'][direction]):
                bk['held'][direction] = 0.0
                bk['avg_px'][direction] = 0.0
            self._life(
                f"{inst_id} | 【账本扣减】{BUCKET_LABEL.get(bucket, bucket)} | "
                f"{'多头' if direction == 'long' else '空头'}持仓 {before}→{bk['held'][direction]}张 | "
                f"原因：外部平仓({reason})", key=True)
            self._save(inst_id)

    def clear_bucket(self, inst_id: str, bucket: str, reason: str = '',
                     direction: str = None) -> int:
        """撤销该篮子挂单 + 清空账本（长周期反转强平 / 手动重置用）。

        direction 为 None 时清空双向账本，否则只清指定方向。返回撤单数。
        在线账本禁止直接清空；该方法仅为旧离线工具保留。
        """
        if self.account is not None:
            raise RuntimeError('在线账本禁止清空，请确认实际成交后 synchronize')
        with self._lock:
            bk = self._inst(inst_id)[bucket]
            n = 0
            for sl in SLOTS:
                if direction and bk['slots'][sl].get('dir') not in (None, direction):
                    continue
                if self._cancel_slot(inst_id, bucket, sl, reason or '清空篮子'):
                    n += 1
            for d in (('long', 'short') if not direction else (direction,)):
                bk['held'][d] = 0.0
                bk['avg_px'][d] = 0.0
                if self._cancel_algo(inst_id, bucket, d, reason or '清空篮子'):
                    n += 1
            bk['prev_open_confirmed'] = False
            bk['prev_close_confirmed'] = False
            bk['last_desired'] = None
            self._save(inst_id)
            return n

    def cancel_bucket_orders(self, inst_id: str, bucket: str, reason: str = '') -> int:
        """仅撤销该篮子未成交挂单，保留持仓账本。返回撤单数。"""
        with self._lock:
            n = 0
            for sl in SLOTS:
                if self._cancel_slot(inst_id, bucket, sl, reason or '撤销挂单'):
                    n += 1
            self._save(inst_id)
            return n

    # =================================================================
    # 每轮对账：用真实持仓给本地账本封顶
    # =================================================================

    def reconcile(self, inst_id: str, cross_pos: float, isolated_pos: float,
                  price: float = 0.0, real=None, averages=None):
        """将已解释程序成交后的账本与稳定快照对齐，返回外部差异。

        生产调用必须通过 synchronize 获取稳定快照。数量不做固定张数死区；
        篮子归属仍沿用原策略，真实头寸的完整维度另外保存在 exchange_positions。
        """
        with self._lock:
            s = self._inst(inst_id)
            real = real or {'long': max(0.0, float(cross_pos or 0)) + max(0.0, float(isolated_pos or 0)),
                            'short': abs(min(0.0, float(cross_pos or 0))) + abs(min(0.0, float(isolated_pos or 0)))}
            events = []
            for d in ('long', 'short'):
                total = sum(float(s[b]['held'].get(d, 0) or 0) for b in BUCKETS)
                actual = float(real[d])
                if not math.isfinite(actual) or actual < 0:
                    raise ValueError('无效的真实持仓')
                delta = actual - total
                if abs(delta) <= self.POS_DUST:
                    continue
                names = [b for b in BUCKETS if float(s[b]['held'].get(d, 0) or 0) > 0]
                if not names:
                    names = [next((b for b in BUCKETS if s[b].get('last_desired') == d), BUCKET_TREND)]
                allocated = 0.0
                old_cost = sum(float(s[b]['held'][d]) * float(s[b]['avg_px'][d]) for b in BUCKETS)
                # 由交易所平均成本反推新增成本；无法确认时标记估算，绝不假装成交价。
                avg = float((averages or {}).get(d, 0) or 0)
                added_px = ((actual * avg - old_cost) / delta if delta > 0 and avg > 0 else 0)
                estimated = added_px <= 0
                added_px = added_px if added_px > 0 else float(price or avg or 0)
                for i, b in enumerate(names):
                    old = float(s[b]['held'][d])
                    new = (actual - allocated if i == len(names) - 1
                           else round(actual * old / total, 12))
                    new = max(0.0, new)
                    allocated += new
                    if delta > 0:
                        s[b]['avg_px'][d] = ((float(s[b]['avg_px'][d]) * old + added_px * (new - old)) / new
                                              if new > 0 else 0.0)
                        s[b].setdefault('cost_estimated', {})[d] = estimated
                    elif new <= self.POS_DUST:
                        s[b]['avg_px'][d] = 0.0
                    s[b]['held'][d] = new
                kind = 'absorb' if delta > 0 else 'shrink'
                event = {'inst_id': inst_id, 'kind': kind, 'direction': d,
                         'total': total, 'real': actual, 'excess': max(0.0, delta),
                         'scale': actual / total if total else 0,
                         'ratio': delta / total if total else 0,
                         'detail': f'{d}账本{total:g}→真实{actual:g}张'}
                events.append(event)
                self._round_divergences.append(event)
                self._life(f'{inst_id} | 【对账·{kind}】{event["detail"]}', key=True)
                if delta < 0:
                    # 账本>真实持仓：系统外平仓（手动/强平/爆仓）。记下这个
                    # 事实，后续冗余平仓单被撤销时能写成“检测到人工平仓事实”。
                    self._note_manual_close(inst_id)
                    self.hold_entries(inst_id, self.manual_pause_seconds, persist=False)
            self._save(inst_id)
            return events

    def hold_entries(self, inst_id, seconds=None, persist=True):
        """先持久化人工干预闸门，再撤单/平仓；本轮即使冷却设为零也禁止补仓。"""
        self._entry_blocked.add(inst_id)
        s = self._inst(inst_id)
        s['pause_until'] = max(float(s.get('pause_until', 0)),
                               time.time() + max(0, self.manual_pause_seconds if seconds is None else seconds))
        if persist:
            return self._save(inst_id)
        return True

    @staticmethod
    def _remaining(pt):
        return max(0.0, float(pt.get('amount', 0)) - float(pt.get('acc_filled', 0))) if pt.get('state') == ST_PENDING else 0.0

    def _pending_records(self, inst_id):
        s = self._inst(inst_id)
        return ([s[b]['slots'][sl] for b in BUCKETS for sl in SLOTS]
                + list(s.get('market_orders', {}).values()))

    def _book_totals(self, inst_id):
        s = self._inst(inst_id)
        return {d: sum(float(s[b]['held'][d]) for b in BUCKETS) for d in ('long', 'short')}

    def _foreign_orders_pending(self, inst_id, orders, snapshot):
        """外部单离开挂单列表不代表无成交；保存观察记录，终态及持仓均确认才解除。"""
        s = self._inst(inst_id)
        known = {str(p.get('ord_id')) for p in self._pending_records(inst_id) if p.get('ord_id')}
        algos = {str(r.get('algo_id')) for b in BUCKETS for r in s[b]['algo'].values() if r}
        watch = s.setdefault('external_order_watch', {})
        live = set()
        for order in orders:
            algo = bool(order.get('_algo'))
            oid = str(order.get('algoId') if algo else order.get('ordId'))
            if oid in (algos if algo else known):
                continue
            key = ('algo:' if algo else 'order:') + oid
            live.add(key)
            watch.setdefault(key, {'order': order, 'baseline': self._book_totals(inst_id)})
        for key, rec in list(watch.items()):
            if key in live:
                continue
            order = rec['order']
            try:
                if order.get('_algo'):
                    info = self.executor.get_algo_order_details(order['algoId']) or {}
                    if info.get('state') in ('canceled', 'cancelled', 'order_failed'):
                        del watch[key]
                    # 触发型外部单无法可靠分摊子单，保留阻断并等待人工核实。
                    continue
                status, info = self.executor.probe_order(inst_id, order['ordId'])
                if status != 'ok' or info.get('state') not in ('filled', 'canceled', 'mmp_canceled'):
                    continue
                acc = float(info['accFillSz'])
                if not math.isfinite(acc) or acc < 0:
                    continue
                if acc <= self.POS_DUST:
                    del watch[key]
                    continue
                d = 'long' if order.get('tdMode') == 'cross' else 'short'
                opening = order.get('side') == ('buy' if d == 'long' else 'sell')
                expected = max(0.0, float(rec['baseline'][d]) + (acc if opening else -acc))
                if abs(snapshot['real'][d] - expected) <= self.POS_DUST:
                    del watch[key]
            except (KeyError, TypeError, ValueError):
                continue
        s['external_open_orders'] = [r['order'] for r in watch.values()]
        return bool(watch)

    def _position_snapshot(self, inst_id):
        raw = self.executor.try_get_positions_by_mode(inst_id)
        if raw is None:
            return None
        real = {'long': 0.0, 'short': 0.0}
        costs = {'long': 0.0, 'short': 0.0}
        signature, rows = [], []
        updated = {}
        supported = True
        for mode, items in raw.items():
            for row in items:
                qty = float(row.get('pos') or 0)
                avg = float(row.get('avgPx') or 0)
                if not math.isfinite(qty) or not math.isfinite(avg):
                    return None
                side = row.get('posSide') or 'net'
                d = side if side in ('long', 'short') else (
                    ('long' if qty > 0 else 'short') if qty else ('long' if mode == 'cross' else 'short'))
                stamp = int(row.get('uTime') or 0)
                updated[d] = min(updated.get(d, stamp), stamp)
                real[d] += abs(qty)
                costs[d] += abs(qty) * avg
                if qty:
                    supported &= side == 'net' and mode == self._mode_of(d)
                signature.append((mode, side, str(row.get('posId', '')), qty, avg, stamp))
                rows.append(dict(row, mgnMode=mode))
        return {'real': real, 'averages': {d: costs[d] / real[d] if real[d] else 0 for d in real},
                'rows': rows, 'updated': updated, 'signature': sorted(signature), 'supported': bool(supported)}

    def synchronize(self, inst_id, price=0.0, cancel_entries=True):
        """成交→持仓→订单→成交→持仓的稳定性屏障；未知状态不做推测性补账。

        REST 无法提供跨接口原子快照。有限重读仍不稳定就停手，下单出口再执行一次。
        外部普通/策略增仓单不擅自撤销，但会阻断自动增仓。
        """
        with self._lock:
            # 本轮已问过但尚未出结论：非刷新路径不得拿“上一轮的失败”误判超额
            self._set_sync_state(inst_id, 'syncing')
            s = self._inst(inst_id)
            all_fills = []
            try:
                # 【2026-09-28 按用户要求移除 G3】不再检查 _storage_ok、不再返回
                # persist_halt 短路整轮：账本写不进也照常对账与交易（按内存账本继续，
                # DB 恢复后 _save 自动补写增量）。
                mode = self.executor.try_get_position_mode()
                for _ in range(3):
                    self.poll_algo_triggers(inst_id, BUCKET_TREND)
                    all_fills += self.poll_fills(inst_id, allow_cancel=cancel_entries)['fills']
                    generation = self._fill_generation
                    first = self._position_snapshot(inst_id)
                    orders = self.executor.try_get_open_orders(inst_id)
                    all_fills += self.poll_fills(inst_id, allow_cancel=cancel_entries)['fills']
                    last = self._position_snapshot(inst_id)
                    if first is None or last is None or orders is None or mode is None:
                        raise RuntimeError('持仓、订单或账户模式查询不完整')
                    if generation != self._fill_generation or first['signature'] != last['signature']:
                        continue
                    s['exchange_positions'] = last['rows']
                    s['exchange_totals'] = last['real']
                    s['exchange_ts'] = time.time()
                    if any(p.get('state') == ST_PENDING and (p.get('unknown') or p.get('qfail_logged') or p.get('cancel_requested'))
                           for p in self._pending_records(inst_id)) or s.get('algo_unknown'):
                        self._save(inst_id)
                        raise RuntimeError('存在未知订单，保留成交水位及风险预占，禁止补单')
                    foreign = self._foreign_orders_pending(inst_id, orders, last)
                    totals = self._book_totals(inst_id)
                    differs = any(abs(last['real'][d] - totals[d]) > self.POS_DUST for d in totals)
                    # 成交先可见：两次相同旧快照不能抹掉已确认成交。该标记与水位一起持久化。
                    if s.get('settlement_pending') and differs:
                        times = s.get('settlement_times', {})
                        # 同一交易所时间轴：较成交更晚的仓位更新可以解释并发人工干预。
                        # 空列表没有时间证据，不能据此提前认定已成交头寸消失。
                        fresh = all(int(times.get(d, 0)) > 0 and
                                    last['updated'].get(d, 0) > int(times[d])
                                    for d in totals if abs(last['real'][d] - totals[d]) > self.POS_DUST)
                        if not fresh:
                            self._save(inst_id)
                            raise RuntimeError('成交已入账，持仓尚未一致；保留水位，等待确认（持续不一致需人工核实）')
                    # 持仓先可见：先确认本系统在途单，不能先吸收、再重复入账。
                    pending = [p for p in self._pending_records(inst_id) if p.get('state') == ST_PENDING]
                    if differs and pending:
                        if cancel_entries:
                            for b in BUCKETS:
                                for sl in SLOTS:
                                    self._cancel_slot(inst_id, b, sl, '持仓与订单更新时序未一致')
                            continue
                        self._save(inst_id)
                        raise RuntimeError('持仓变化尚不能与在途订单区分，暂停对账写入')
                    # 兜底单可能刚触发而详情尚未传播；确认撤销后才能认定为人工变化。
                    active_algos = [(b, d) for b in BUCKETS for d in ('long', 'short') if s[b]['algo'].get(d)]
                    if differs and active_algos:
                        if cancel_entries:
                            for b, d in active_algos:
                                self._cancel_algo(inst_id, b, d, '核实外部持仓变化')
                            continue
                        raise RuntimeError('兜底委托与持仓变化尚未确认')
                    s.pop('settlement_pending', None)
                    s.pop('settlement_times', None)
                    events = self.reconcile(inst_id, 0, 0, price=price,
                                             real=last['real'], averages=last['averages'])
                    supported = last['supported'] and mode == 'net_mode'
                    # 【2026-09-28 按用户要求移除 G2】blocked 只反映本币自己的风险：
                    # 人工冷却 / 外部挂单(foreign) / 模式不支持 / 市价单在途。
                    # 不再引用已移除的账户级闸门 _account_entry_gate。
                    blocked = (inst_id in self._entry_blocked or float(s.get('pause_until', 0)) > time.time()
                               or foreign
                               or not supported or any(p.get('state') == ST_PENDING
                                                       for p in s.get('market_orders', {}).values()))
                    need_recheck = False
                    if cancel_entries:
                        for b in BUCKETS:
                            pt = s[b]['slots'][SLOT_ENTRY]
                            if pt.get('state') != ST_PENDING:
                                continue
                            target = self._targets.get(inst_id)
                            excess = bool(target is not None and self._remaining(pt) >
                                          self._entry_room(inst_id, pt.get('dir'), b, refresh=False) + self.POS_DUST)
                            if events or blocked or excess or pt.get('cancel_requested'):
                                self._cancel_slot(inst_id, b, SLOT_ENTRY, '对账变化/目标上限/人工冷却')
                                need_recheck = True
                    if need_recheck:
                        continue
                    s.pop('sync_error', None)
                    # 【2026-09-28 按用户要求移除 G3】落库失败不再 raise 停单：
                    # _save 内部已记一行日志并返回 False，这里照常继续（以内存账本为准）。
                    self._save(inst_id)
                    if not supported:
                        raise RuntimeError('真实持仓已记录，但模式不受当前策略支持（仅净持仓全仓多/逐仓空）')
                    self._set_sync_state(inst_id, 'ok')
                    return {'success': True, 'cross': last['real']['long'],
                            'isolated': -last['real']['short'], 'fills': all_fills,
                            'entry_allowed': not blocked and not foreign}
                raise RuntimeError('快照持续变化或撤单未确认，本轮禁止交易')
            except Exception as e:
                # 【2026-09-28 按用户要求移除 G1 币种隔离】对账失败不再标 unresolved、
                # 不再打【币种级停开仓 G1】、不再逐币刷屏或触发隔离邮件；仅记 sync_error
                # 供诊断与幽灵合约判定（_other_booked_exposure/_place_entry 只认 absent）。
                s['sync_error'] = str(e)
                self._save(inst_id)
                return {'success': False, 'error': str(e), 'fills': all_fills}

    # =================================================================
    # 币种对账结论读写（仅保留 absent 幽灵合约语义；G1/G2/G3 已按用户要求移除）
    # =================================================================

    def _sync_state(self, inst_id: str) -> str:
        """取该币本轮对账结论；桩对象（__new__ 裸实例）缺容器时按未知处理。"""
        return (getattr(self, '_inst_sync_state', None) or {}).get(inst_id) or ''

    def _set_sync_state(self, inst_id: str, state: str):
        # 先补容器再写入：__new__ 构造的裸实例桩没有 _inst_sync_state 时也不能崩
        store = getattr(self, '_inst_sync_state', None)
        if store is None:
            store = self._inst_sync_state = {}
        store[inst_id] = state

    def mark_sync_absent(self, inst_ids) -> None:
        """轮初对账范围划分结果：标为“无事实可核对”。

        被标的币种既不进自己的额度计算（本来也不在配置里），也不得
        参与全账户总仓位遍历 —— 这就是幽灵合约/空账本“零阻断权”的落地口径。
        """
        for inst in inst_ids or []:
            self._set_sync_state(inst, 'absent')

    def _note_manual_close(self, inst_id: str):
        """记一笔“本轮发生过系统外平仓”，供撤单原因与审计日志使用。"""
        seen = getattr(self, '_manual_close_seen', None)
        if seen is None:
            self._manual_close_seen = {inst_id}
        else:
            seen.add(inst_id)

    def _exit_zero_reason(self, inst_id: str, default: str) -> str:
        """平仓挂单失效原因：检测到人工平仓事实时显式标注，便于审计。"""
        if inst_id in (getattr(self, '_manual_close_seen', None) or ()):
            return '检测到人工平仓事实，撤销冗余平仓单'
        return default

    def _other_booked_exposure(self, inst_id: str) -> float:
        """其它币种占用的总仓额度（张）——“计占用”而不是“一票归零”。

        口径：
        - absent（幽灵合约/已移除币种的账本残留）直接跳过，不占任何额度；
        - 正常币种取 max(交易所快照, 账本合计) + 未成交开仓挂单剩余量（与旧口径一致）；
        - 带风险标记（sync_error/settlement_pending/外部挂单/兜底单未知/
          在途单结果未知）的币种按**最坏情形**计入：额外计入外部挂单未成交量
          与结果未知委托的全额。只有真把总仓上限击穿，才会把新币挤到额度外；
          不再因为一个废弃币的错误标记就让全账户开仓额度蒸发。
        """
        total = 0.0
        for inst, state in self.state.items():
            if inst == inst_id or not isinstance(state, dict):
                continue
            if self._sync_state(inst) == 'absent':
                continue                # 幽灵/空账本：零阻断权，也不占额度
            booked = sum(float((state.get(b, {}).get('held') or {}).get(d, 0) or 0)
                         for b in BUCKETS for d in ('long', 'short'))
            snap = sum(float(state.get('exchange_totals', {}).get(d, 0) or 0)
                       for d in ('long', 'short'))
            entry_pending = 0.0
            unknown_exposure = 0.0
            for b in BUCKETS:
                px = (state.get(b, {}).get('slots') or {}).get(SLOT_ENTRY, {})
                if px.get('state') == ST_PENDING:
                    entry_pending += self._remaining(px)
                    if px.get('unknown') or px.get('cancel_requested'):
                        # 委托是否已落地未确认：按全额预占，不因未知而抹掉敞口
                        unknown_exposure += max(0.0, float(px.get('amount', 0) or 0)
                                                - self._remaining(px))
            total += max(snap, booked) + entry_pending + unknown_exposure
            if state.get('external_open_orders') or state.get('algo_unknown'):
                # 外部/兜底委托可能刚触发而持仓未变：按其未成交量保守预占
                for od in (state.get('external_open_orders') or []):
                    try:
                        sz = float(od.get('ordSz') or od.get('sz') or 0)
                        filled = float(od.get('accFillSz') or 0)
                    except (TypeError, ValueError):
                        sz = filled = 0.0
                    total += max(0.0, sz - filled)
        return total

    def set_entry_targets(self, inst_id, targets, max_position=0, max_total=0):
        self._targets[inst_id] = {'directions': dict(targets),
                                  'max_position': float(max_position or 0),
                                  'max_total': float(max_total or 0)}

    def _entry_room(self, inst_id, direction, bucket=None, refresh=True):
        """本币种该方向还可开多少张（0 = 本轮不许加仓）。

        下单量是**减法算出来的**，不是“看到有仓就跳过”：
        `可补开 = 方向总额度 − 已占用`，不够就补、够就算了、超了不动。
        已占用取 `max(交易所快照, 本地账本)`，所以人工提前开的仓一定被算进去，
        “人工已开 + 系统再开一遍”这条路物理上关着。

        【2026-09-28 按用户要求移除 G1/G2/G3 闸门】本方法不再因“持久化不可用/
        账户形状未知/本币对账未确认”而归零额度；对账失败时改用陈旧快照算账，仍受
        max_position/max_total 单币与总仓硬上限兜底。仅保留：
        - 人工强平/手动平仓冷却（_entry_blocked + pause_until，只锁本币开仓）；
        - absent（无事实可核对的账本残留/幽灵合约）→ 零阻断权、不占额度。
        平仓/撤单/止盈止损等“削减风险”动作永远不走本方法。
        """
        # 额度归零时把具体原因写入 _last_room_reason，供 _place_entry 打诊断日志：
        # 过去这里静默 return 0.0，心跳只剩“无动作”，幽灵账本/上限都分不清是哪个。
        if direction not in ('long', 'short'):
            self._last_room_reason = '方向无效'
            return 0.0
        if refresh:
            # 仅刷新真实持仓/成交数据（含 reconcile）供下面算账；不再据对账结论拦截
            # 开仓（移除 G1）。synchronize 内部失败只记 sync_error，不向外抛出。
            self.synchronize(inst_id)
        s = self._inst(inst_id)
        if inst_id in self._entry_blocked or float(s.get('pause_until', 0)) > time.time():
            self._last_room_reason = '人工强平/手动平仓冷却中（只锁本币开仓）'
            return 0.0
        target = self._targets.get(inst_id)
        if target is None:
            self._last_room_reason = '未设开仓额度目标(set_entry_targets 未执行)'
            return 0.0
        # 占用取「交易所快照」与「本地账本」的较大值：人工提前开的仓一定被算进去，
        # 未成交减仓单绝不提前释放开仓额度；部分成交只按剩余量预占。
        real = {d: max(float(s.get('exchange_totals', {}).get(d, 0)),
                       sum(float(s[b]['held'][d]) for b in BUCKETS)) for d in ('long', 'short')}
        booked = {d: sum(float(s[b]['held'][d]) for b in BUCKETS) for d in ('long', 'short')}
        pending = {d: sum(self._remaining(s[b]['slots'][SLOT_ENTRY]) for b in BUCKETS
                          if b != bucket and s[b]['slots'][SLOT_ENTRY].get('dir') == d)
                   for d in ('long', 'short')}
        room = float(target['directions'].get(direction, 0)) - real[direction] - pending[direction]
        occupied = sum(real.values()) + sum(pending.values())
        if target['max_position'] > 0:
            room = min(room, target['max_position'] - occupied)
        if target['max_total'] > 0:
            # 他币不再“一票归零”：按保守占用计入，只有真击穿总仓上限才卡住本币
            room = min(room, target['max_total'] - (occupied + self._other_booked_exposure(inst_id)))
        room = self._q(inst_id, max(0.0, room))
        # 算账可见性：目标/已占用（其中账本外=人工/未入账部分）/本次可补
        unbooked = sum(max(0.0, real[d] - booked[d]) for d in ('long', 'short'))
        self._last_room_detail = {
            'target': float(target['directions'].get(direction, 0) or 0),
            'occupied': occupied,
            'unbooked': unbooked,
            'room': room,
            'max_position': float(target['max_position'] or 0),
            'max_total': float(target['max_total'] or 0),
        }
        self._last_room_reason = (
            '' if room > 0 else
            f'额度已满(目标{self._last_room_detail["target"]:g}−已占用{occupied:.2g}'
            f'≤0 或触单币/总仓上限)')
        return room

    def _block_logged(self) -> Dict:
        """取「同原因去重」容器；`__new__` 裸实例桩缺容器时先补再返回。"""
        store = getattr(self, '_entry_block_logged', None)
        if store is None:
            store = self._entry_block_logged = {}
        return store

    def _note_entry_blocked(self, inst_id, bucket, direction, detail=''):
        """开仓被静默跳过时补一行诊断日志；同一原因去重，原因变化或恢复后可再次提示。

        背景：区间/趋势仓在 _place_entry 前置就可能因额度=0 被静默 return，
        心跳只留“无动作”无法定位。此处把 _entry_room 记录的原因显式化，
        供下次快速区分：幽灵账本拖累/对账闸门/人工冷却/额度已满/折算不足。
        """
        why = detail or getattr(self, '_last_room_reason', '') or '原因未知'
        tag = f"{bucket}|{direction}|{why}"
        logged = self._block_logged()
        if logged.get(inst_id) == tag:
            return
        logged[inst_id] = tag
        task_log.info(
            f"{self._rid()}{inst_id} | 【开仓跳过】{BUCKET_LABEL.get(bucket, bucket)}"
            f"{'开多' if direction == 'long' else '开空'}未挂单：{why}")

    def _submit_order(self, inst_id, bucket, slot, direction, amount, price=0, market=False):
        s = self._inst(inst_id)
        container = s.setdefault('market_orders', {}) if market else s[bucket]['slots']
        key = f'{bucket}:{direction}' if market else slot
        # 【2026-09-28 按用户要求移除 G3】不再检查 _storage_ok；仅保留“订单仍在途”
        # 去重，防止同一 slot 重复下单。
        if container.get(key, {}).get('state') == ST_PENDING:
            return {'success': False, 'error': '订单仍在途'}
        pt = dict(self._new_slot(), state=ST_PENDING, amount=amount, price=price,
                  dir=direction, placed_ts=time.time(), cl_ord_id='ct' + uuid.uuid4().hex[:30],
                  unknown=True, bucket=bucket, market=market)
        container[key] = pt
        # 【2026-09-28 按用户要求移除 G3】意图落库失败不再阻止发单：cl_ord_id 已在
        # 内存 pt 中，同进程内在途去重仍生效；DB 恢复后 _save 自动补写。
        self._save(inst_id)
        params = dict(inst_id=inst_id, amount=amount, price=price or None,
                      trading_mode=self._mode_of(direction), order_type='market' if market else 'limit',
                      cl_ord_id=pt['cl_ord_id'])
        params['side'] = ('buy' if direction == 'long' else 'sell') if slot == SLOT_ENTRY else ('sell' if direction == 'long' else 'buy')
        try:
            api = self.executor.execute_trade if slot == SLOT_ENTRY else self.executor.execute_reduce_only_order
            res = api(**params) or {}
        except Exception as e:
            res = {'success': False, 'need_verify': True, 'error': str(e)}
        if res.get('success') and res.get('order_id'):
            pt['ord_id'] = res['order_id']
            pt['unknown'] = False
        elif res.get('rejected') and not res.get('need_verify'):
            pt['state'] = ST_EXPIRED
            pt['unknown'] = False
        else:
            res = dict(res, success=False, need_verify=True)
        # 【2026-09-28 按用户要求移除 G3】响应落库失败不再停单：内存 pt 已更新
        # ord_id/unknown，以内存账本为准，DB 恢复后 _save 自动补写。
        self._save(inst_id)
        return res

    def close_market(self, inst_id, bucket, direction, amount):
        """市价平仓只有确认全成才返回 True，跨轮次复用未终结意图。"""
        with self._lock:
            s = self._inst(inst_id)
            key = f'{bucket}:{direction}'
            pt = s.get('market_orders', {}).get(key)
            if pt and pt.get('state') == ST_PENDING:
                self._poll_order(inst_id, bucket, SLOT_EXIT, pt, [])
                if pt.get('state') != ST_FILLED:
                    return False
            if pt and pt.get('state') == ST_FILLED and not pt.get('reported'):
                pt['reported'] = True
                return self._save(inst_id)
            if self.account is not None and not self.synchronize(inst_id).get('success'):
                return False
            # 普通 exit 与市价单共用减仓预占；兜底保护保持在位，reduceOnly 为最终保障。
            for b in BUCKETS:
                if bucket == 'account' or b == bucket:
                    self._cancel_slot(inst_id, b, SLOT_EXIT, '市价减仓前确认撤单')
            if self.account is not None and not self.synchronize(inst_id).get('success'):
                return False
            totals = self._book_totals(inst_id)
            room = self._exit_room(inst_id, None, direction, totals['long'], -totals['short'])
            held = totals[direction] if bucket == 'account' else float(s[bucket]['held'][direction])
            amount = self._q(inst_id, min(amount, room, held))
            if amount <= 0:
                return False
            self._submit_order(inst_id, bucket, SLOT_EXIT, direction, amount, market=True)
            pt = s['market_orders'][key]
            self._poll_order(inst_id, bucket, SLOT_EXIT, pt, [])
            if pt.get('state') == ST_FILLED:
                pt['reported'] = True
                return self._save(inst_id)
            return False

    # =================================================================
    # 内部：槽位生命周期
    # =================================================================

    def _observe_order(self, inst_id, bucket, slot, pt, info, fills):
        """累计成交水位幂等入账；包括部分成交、撤单尾单和市价单。"""
        st = info.get('state')
        try:
            acc = float(info['accFillSz'])
            previous = float(pt.get('acc_filled', 0))
            avg = float(info.get('avgPx') or pt.get('price') or 0)
            if (st not in ('live', 'partially_filled', 'filled', 'canceled', 'mmp_canceled')
                    or not math.isfinite(acc) or acc < previous - self.POS_DUST
                    or not math.isfinite(avg) or (st == 'filled' and acc <= 0)):
                raise ValueError('成交快照无效或水位倒退')
        except (ValueError, KeyError, TypeError):
            pt['unknown'] = True
            pt['qfail_logged'] = True
            return None
        pt['unknown'] = False
        pt.pop('qfail_logged', None)
        if info.get('sz'):
            pt['amount'] = max(acc, float(info['sz']))
        delta = max(0.0, acc - previous)
        if delta > self.POS_DUST:
            notional = acc * avg
            px = (notional - float(pt.get('fill_notional', previous * avg))) / delta
            px = px if px > 0 else avg
            if bucket == 'account':
                s = self._inst(inst_id)
                d = pt['dir']
                total = sum(float(s[b]['held'][d]) for b in BUCKETS)
                for b in BUCKETS:
                    old = float(s[b]['held'][d])
                    s[b]['held'][d] = max(0.0, old - delta * old / total) if total else 0.0
                    if s[b]['held'][d] <= self.POS_DUST:
                        s[b]['avg_px'][d] = 0.0
            else:
                # 市价单不占普通 exit 槽，因此显式传入方向。
                self._credit(inst_id, bucket, slot, delta, px, direction=pt.get('dir'))
            pt['acc_filled'] = acc
            pt['fill_notional'] = notional
            self._fill_generation += 1
            s = self._inst(inst_id)
            s['settlement_pending'] = True
            try:
                stamp = int(info.get('fillTime') or info.get('uTime') or 0)
            except (ValueError, TypeError):
                stamp = 0
            times = s.setdefault('settlement_times', {})
            d = pt.get('dir')
            previous_stamp = times.get(d, stamp)
            times[d] = max(previous_stamp, stamp) if previous_stamp > 0 and stamp > 0 else 0
            if self._save(inst_id):
                self._journal_credit(inst_id, bucket, slot, pt, delta, px)
            if fills is not None:
                fills.append({'bucket': bucket, 'slot': slot, 'amount': delta,
                              'price': px, 'dir': pt.get('dir')})
        if st == 'filled':
            pt['state'] = ST_FILLED
            if pt.get('market'):
                self.hold_entries(inst_id, self.close_pause_seconds, persist=False)
        elif st in ('canceled', 'mmp_canceled'):
            pt['state'] = ST_EXPIRED
        if pt['state'] != ST_PENDING or delta > self.POS_DUST:
            self._save(inst_id)
            return f'{bucket}.{slot}{st}(增量{delta:g})'
        return None

    def _poll_order(self, inst_id, bucket, slot, pt, fills):
        if pt.get('state') != ST_PENDING:
            return None
        authoritative_absent = False
        try:
            if pt.get('ord_id'):
                status, info = self.executor.probe_order(inst_id, pt['ord_id'])
            elif pt.get('cl_ord_id'):
                status, info = self.executor.probe_order_by_cl_id(inst_id, pt['cl_ord_id'], tries=1)
                if status == 'found' and info and info.get('ordId'):
                    pt['ord_id'] = info['ordId']
                    status = 'ok'
                elif status == 'absent':
                    # 查询成功且明确为空/不存在 = 权威结论：原请求未在交易所落地
                    authoritative_absent = True
            else:
                status, info = 'error', None
        except Exception:
            status, info = 'error', None
        if status != 'ok' or not info:
            # 默认保守：结果未知一律保留占位、禁止补单（防重复下单）。
            # 例外：从没拿到 ordId、只有 clOrdId 的**平仓(reduce-only)孤儿单**，若交易所
            # **权威地**回答“查无此单”且已存续超过传播窗口，判定其从未落地 → 主动过期
            # 释放币种，避免永久锁死。开仓(entry)单不适用（防重复开仓，见“absent 永不重发”
            # 安全不变量）；查询本身失败(unknown/error)一律不在此列，仍保守保留。
            if (slot == SLOT_EXIT and authoritative_absent and not pt.get('ord_id')
                    and float(pt.get('placed_ts', 0) or 0) > 0
                    and (time.time() - float(pt.get('placed_ts', 0) or 0))
                    >= self.UNKNOWN_ABSENT_SETTLE_SEC):
                age = time.time() - float(pt.get('placed_ts', 0) or 0)
                pt['state'] = ST_EXPIRED
                pt['unknown'] = False
                pt.pop('qfail_logged', None)
                self._save(inst_id)
                task_log.warning(
                    f"{self._rid()}{inst_id} | 【未知单定性】{BUCKET_LABEL.get(bucket, bucket)}"
                    f"·{SLOT_LABEL.get(slot, slot)} 存续{age / 60:.1f}分钟，交易所权威查无此单"
                    f"(clOrdId={pt.get('cl_ord_id')})，判定从未落地→已过期释放，下轮可正常重挂")
                return f'{bucket}.{slot}absent-expired'
            pt['qfail_logged'] = True
            pt['unknown'] = True
            return None
        return self._observe_order(inst_id, bucket, slot, pt, info, fills)

    def _reconcile_slot(self, inst_id: str, bucket: str, slot: str,
                        fills: list) -> Optional[str]:
        return self._poll_order(inst_id, bucket, slot,
                                self._inst(inst_id)[bucket]['slots'][slot], fills)

    def _credit(self, inst_id: str, bucket: str, slot: str, amount: float, fill_px: float,
                direction=None):
        """成交入账：开仓槽增持并更新加权均价，平仓槽减持（归零清均价）。

        入账方向取自槽位的 dir 字段 —— 双向模式下同一篮子可能同时持有多空。
        """
        if amount <= 0:
            return
        bk = self._inst(inst_id)[bucket]
        d = direction or bk['slots'][slot].get('dir')
        if d not in ('long', 'short'):
            task_log.warning(
                f"[双仓位] {inst_id} {self._tag(bucket, slot)} 成交入账缺少方向标识，跳过")
            return
        held = float(bk['held'].get(d, 0) or 0)
        avg = float(bk['avg_px'].get(d, 0) or 0)
        if slot == SLOT_ENTRY:
            new_held = round(held + amount, 12)
            if new_held > 0:
                bk['avg_px'][d] = round(
                    (avg * held + float(fill_px) * amount) / new_held, 8)
            bk['held'][d] = new_held
        else:
            bk['held'][d] = round(max(0.0, held - amount), 12)
            if not self._in_pos(bk['held'][d]):
                bk['held'][d] = 0.0
                bk['avg_px'][d] = 0.0

    def _journal_credit(self, inst_id: str, bucket: str, slot: str,
                        pt: Dict, amount: float, fill_px: float):
        """限价成交写入结构化流水（实盘 vs 策略对比引擎的数据源）"""
        d = pt.get('dir')
        if amount <= 0 or d not in ('long', 'short'):
            return
        journal_fill(
            inst_id, bucket, d,
            'open' if slot == SLOT_ENTRY else 'close',
            fill_px, amount, reason='signal',
            ord_id=pt.get('ord_id'), run_id=self._run_id)

    @staticmethod
    def _filled_size(info: Dict) -> float:
        try:
            return float((info or {}).get('accFillSz', 0) or 0)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _fill_price(info: Dict) -> float:
        """成交均价（限价单通常等于委托价，可能更优）"""
        try:
            return float((info or {}).get('avgPx', 0) or 0)
        except (TypeError, ValueError):
            return 0.0

    def _cancel_slot(self, inst_id: str, bucket: str, slot: str, reason: str) -> bool:
        """撤单受理不释放槽位；必须重查终态，并入账撤单期间的成交尾巴。"""
        bk = self._inst(inst_id)[bucket]
        pt = bk['slots'][slot]
        if pt.get('state') != ST_PENDING:
            return False
        pt['cancel_requested'] = True
        if not self._save(inst_id):
            return False
        self._reconcile_slot(inst_id, bucket, slot, [])
        if pt.get('state') == ST_PENDING and pt.get('ord_id'):
            try:
                self.executor.cancel_normal_order(inst_id, pt['ord_id'])
            except Exception:
                pass
            self._reconcile_slot(inst_id, bucket, slot, [])
        if pt.get('state') == ST_EXPIRED:
            bk['slots'][slot] = self._new_slot()
            self._save(inst_id)
            # 撤单成功也留一行原因（以前只在校验失败时留痕）：人工干预/闸门
            # 降级后的“为什么要撤这张单”必须能审计（P4 算账可见性）
            self._life(f'{inst_id} | 【撤单】{self._tag(bucket, slot)} | 原因：{reason}', key=True)
            return True
        if pt.get('state') == ST_PENDING:
            self._life(f'{inst_id} | 【撤单未确认】{self._tag(bucket, slot)} 保留预占，原因：{reason}', key=True)
        self._save(inst_id)
        return False

    def _amend_slot_price(self, inst_id: str, bucket: str, slot: str,
                          new_px: float, min_pct: float) -> Optional[str]:
        """挂单价追随目标价改单（BOLL 边界移动用）。返回动作描述或 None。"""
        bk = self._inst(inst_id)[bucket]
        pt = bk['slots'][slot]
        if pt.get('state') != ST_PENDING or not pt.get('ord_id') or new_px <= 0:
            return None
        if pt.get('unknown') or pt.get('cancel_requested'):
            return None
        if slot == SLOT_ENTRY and self.account is not None:
            if self._entry_room(inst_id, pt.get('dir'), bucket) < self._remaining(pt):
                self._cancel_slot(inst_id, bucket, slot, '改价前真实持仓检查未通过')
                return None
            if bk['slots'][slot] is not pt or pt.get('state') != ST_PENDING:
                return None
        old_px = float(pt.get('price', 0) or 0)
        new_r = round(new_px, 6)
        if old_px <= 0 or new_r == round(old_px, 6):
            return None
        if min_pct > 0 and abs(new_r - old_px) / old_px < min_pct:
            return None
        if self.executor.amend_order(inst_id, pt['ord_id'], new_price=new_r):
            pt['price'] = new_r
            pct = abs(new_r - old_px) / old_px * 100
            trade_log.info(
                f"[双仓位] {inst_id} {self._tag(bucket, slot)} BOLL边界移动改价 "
                f"{old_px:.6g}→{new_r:.6g}")
            self._life(
                f"{inst_id} | 【改单】{self._tag(bucket, slot)} {old_px:.6g}→{new_r:.6g} | "
                f"原因：BOLL边界移动(幅度{pct:.3f}%)")
            return f'{bucket}.{slot}改价{old_px:.6g}→{new_r:.6g}'
        task_log.warning(
            f"[双仓位] {inst_id} {self._tag(bucket, slot)} 改价失败（保留原价{old_px:.6g}）")
        return None

    def _amend_slot_size(self, inst_id: str, bucket: str, slot: str,
                         new_amt: float, reason: str = '跟随篮子持仓变化',
                         cancel_reason: str = '篮子持仓归零，平仓挂单撤销') -> Optional[str]:
        """挂单量同步（平仓单跟随篮子持仓 / 开仓单跟随配置目标量变化）。
        返回动作描述或 None。new_amt<=0 时撤销挂单（目标量为 0 不应留单）。"""
        bk = self._inst(inst_id)[bucket]
        pt = bk['slots'][slot]
        if pt.get('state') != ST_PENDING or not pt.get('ord_id'):
            return None
        cur = float(pt.get('amount', 0) or 0)
        lot, _ = self._steps(inst_id)
        target = self._q(inst_id, new_amt)
        if slot == SLOT_ENTRY:
            if self.account is not None:
                target = min(target, self._entry_room(inst_id, pt.get('dir'), bucket))
                pt = bk['slots'][slot]
                if pt.get('state') != ST_PENDING:
                    return None
            # 增量改单也必须经统一风控。撤后下一轮重建，避免 newSz 总量/剩余量混淆。
            if pt.get('unknown') or pt.get('cancel_requested') or abs(target - self._remaining(pt)) > self.POS_DUST:
                self._cancel_slot(inst_id, bucket, slot, '目标剩余量变化，确认撤单后重建')
            return None
        if pt.get('unknown') or pt.get('cancel_requested'):
            return None
        if target <= 0:
            # 篮子归零导致的撤单：若本轮已确认系统外平仓，显式写明事实依据
            why = (self._exit_zero_reason(inst_id, cancel_reason)
                   if slot == SLOT_EXIT else cancel_reason)
            self._cancel_slot(inst_id, bucket, slot, why)
            return f'{bucket}.{slot}撤单(目标量为0)'
        # 差值不足一个步长视为无变化；直接比较浮点差会因二进制表示误差把
        # 恰好一步的差值（0.3-0.2=0.09999…）误判为无变化，故按步长比例取整
        target += float(pt.get('acc_filled', 0))
        if abs(target - cur) < lot - 1e-9:
            return None
        # 减仓改量也采用撤后重建，未知改量结果不能提前释放预占。
        self._cancel_slot(inst_id, bucket, slot, '平仓目标量变化，确认撤单后重建')
        return None

    # =================================================================
    # 内部：下单
    # =================================================================

    @staticmethod
    def _mode_of(direction: str) -> str:
        """方向 → 保证金模式（约定：多头全仓、空头逐仓）"""
        return 'cross' if direction == 'long' else 'isolated'

    # 挂单管理器杠杆缓存命中后的交易所回核周期（秒）：
    # 既避免每轮多余查询，又保证最长一天内一定能发现并纠正失真
    LEV_VERIFY_INTERVAL = 86400

    def _set_leverage_if_needed(self, inst_id: str, s: Dict, leverage, mode: str):
        """按配置设置交易所侧杠杆；缓存命中也周期性回交易所核对实际值。

        原实现盲信本地缓存 lev_set：一旦交易所侧实际杠杆与缓存不一致
        （人工在 App 改过、交易所重置、账户杠杆模式变更），系统永不纠正，
        后续开仓一直沿用错误杠杆（2026-09-01 排查结论）。
        """
        if not leverage:
            return
        # 按保证金模式分键缓存：多头(cross)与空头(isolated)各自记录，
        # 避免多空交替时单键缓存反复失效导致每轮重复调 set_leverage
        lev = s.get('lev_set')
        if not isinstance(lev, dict):
            lev = {}
            s['lev_set'] = lev
        vkey = f'{inst_id}:{mode}'
        if lev.get(mode) != leverage:
            if self.executor.set_leverage(inst_id, leverage, mode, None):
                lev[mode] = leverage
                self._lev_verified[vkey] = time.time()
            return
        # 缓存命中：超过回核周期回交易所核对一次（进程重启后首次开仓即核对）
        if time.time() - self._lev_verified.get(vkey, 0) < self.LEV_VERIFY_INTERVAL:
            return
        actual = self.executor.get_leverage_setting(inst_id, mode)
        if actual is None:
            # 查询失败（网络/API）：保守信任缓存，不因校验失败阻断交易
            return
        self._lev_verified[vkey] = time.time()
        if abs(actual - float(leverage)) < 1e-6:
            return
        task_log.warning(
            f"{self._rid()}{inst_id} | 【杠杆校正】{mode}模式交易所侧实际杠杆="
            f"{actual:g}x ≠ 配置{leverage}x，已重新设置（可能被人工在App改过）")
        if self.executor.set_leverage(inst_id, leverage, mode, None):
            lev[mode] = leverage
        else:
            # 重设失败：清除缓存使下轮重试，不让错误杠杆静默建仓
            lev.pop(mode, None)

    def _place_entry(self, inst_id: str, bucket: str, direction: str,
                     amount: float, price: float, leverage=None,
                     max_position: float = 0, max_total: float = 0,
                     cross_pos: float = None, isolated_pos: float = None,
                     reason: str = '') -> bool:
        """挂开仓限价单（净持仓模式不传 posSide，靠 cross/isolated 区分多空）"""
        s = self._inst(inst_id)
        bk = s[bucket]
        mode = self._mode_of(direction)
        side = 'buy' if direction == 'long' else 'sell'
        amount = self._q(inst_id, amount)
        if amount <= 0 or price <= 0:
            self._note_entry_blocked(
                inst_id, bucket, direction,
                f'下单量/价格无效(折算后={amount} 价={price}，低于 minSz 或现价缺失)')
            return False
        # 人工同向加仓已由每轮 reconcile 吸收进账本并纳入正常调度，
        # 不再以“账本欠账”为由拒单（否则人工开仓后系统永久不开不平）
        led = {'long': 0.0, 'short': 0.0}
        for b in BUCKETS:
            for d in ('long', 'short'):
                led[d] += float(s[b]['held'].get(d, 0) or 0)
        real = {'long': max(0.0, float(cross_pos or 0)) if cross_pos is not None else None,
                'short': abs(min(0.0, float(isolated_pos or 0)))
                if isolated_pos is not None else None}
        # 账本外真实持仓超额（双向合计）同样计入上限占用，防止上限被绕过
        extra_unbooked = sum(
            max(0.0, real[d] - led[d]) for d in ('long', 'short')
            if real[d] is not None)
        # 单币种仓位上限：本次挂单 + 两篮子双向账本 + 已挂未成交开仓单合计
        # 超限拒单 —— 风控配置必须在交易链路强制执行，而非仅前端展示
        if max_position and max_position > 0:
            occupied = extra_unbooked
            for b in BUCKETS:
                for d in ('long', 'short'):
                    occupied += float(s[b]['held'].get(d, 0) or 0)
                pt = s[b]['slots'][SLOT_ENTRY]
                if pt.get('state') == ST_PENDING:
                    occupied += self._remaining(pt)
            if round(occupied + amount, 4) > float(max_position):
                task_log.warning(
                    f"{self._rid()}{inst_id} | 【风控拦截】{self._tag(bucket, SLOT_ENTRY)}拒单 | "
                    f"本次{amount}张 + 已占用{occupied:.1f}张 > 单币种上限"
                    f"{float(max_position):.0f}张")
                return False
        # 全账户总仓位上限：跨全部币种的账本持仓 + 未成交开仓挂单合计
        # （本币种账本外的真实持仓超额一并计入）
        if max_total and max_total > 0:
            total_occ = extra_unbooked
            for inst, st in self.state.items():
                if not isinstance(st, dict):
                    continue
                # 幽灵合约/已移除币种的账本残留（交易所确认无仓无单）：
                # 零阻断权也不占总额度，口径与 _other_booked_exposure 一致
                if self._sync_state(inst) == 'absent':
                    continue
                for b in BUCKETS:
                    bx = st.get(b) or {}
                    for d in ('long', 'short'):
                        total_occ += float((bx.get('held') or {}).get(d, 0) or 0)
                    px = (bx.get('slots') or {}).get(SLOT_ENTRY) or {}
                    if px.get('state') == ST_PENDING:
                        total_occ += self._remaining(px)
            if round(total_occ + amount, 4) > float(max_total):
                task_log.warning(
                    f"{self._rid()}{inst_id} | 【风控拦截】{self._tag(bucket, SLOT_ENTRY)}拒单 | "
                    f"本次{amount}张 + 全账户已占用{total_occ:.1f}张 > 总仓位上限"
                    f"{float(max_total):.0f}张")
                return False
        if self.account is not None:
            self._last_room_detail = {}
            amount = min(amount, self._entry_room(inst_id, direction))
            if amount <= 0:
                self._note_entry_blocked(inst_id, bucket, direction)
                return False
        elif inst_id in self._entry_blocked or float(s.get('pause_until', 0)) > time.time():
            self._note_entry_blocked(inst_id, bucket, direction, '人工冷却/禁开仓中')
            return False
        # 算账三段式（P4）：目标 / 已占用（其中账本外=人工或外部未入账部分）/ 本次补开。
        # “已占用”取 max(交易所快照, 本地账本)，所以人工提前开的仓一定看得见。
        _rd = getattr(self, '_last_room_detail', None) or {}
        self._last_room_detail = {}
        calc = ''
        if _rd:
            calc = (f" | 算账：目标{_rd.get('target', 0):g} 已占用{_rd.get('occupied', 0):.2g}"
                    f"(其中账本外/人工{_rd.get('unbooked', 0):.2g}) 本次补开{amount:g}")
        # 能走到真正下单：清除去重标记，便于下次再被挡时重新提示
        self._block_logged().pop(inst_id, None)
        self._set_leverage_if_needed(inst_id, s, leverage, mode)
        res = self._submit_order(inst_id, bucket, SLOT_ENTRY, direction, amount, round(price, 6))
        if res and res.get('success'):
            trade_log.info(
                f"[双仓位] {inst_id} {self._tag(bucket, SLOT_ENTRY)} 限价"
                f"{'开多' if direction == 'long' else '开空'} {amount}张 @ "
                f"{price:.6g} ({mode}) | 原因：{reason or '-'}{calc}")
            _act = '开多' if direction == 'long' else '开空'
            self._life(
                f"{inst_id} | 【{_act}·挂单】{BUCKET_LABEL.get(bucket, bucket)} | "
                f"{amount}张 @限价{price:.6g} | 原因：{reason or '-'}{calc}", key=True)
            return True
        err = res.get('error') if res else '无返回'
        self._note_place_failure(inst_id, bucket, SLOT_ENTRY, amount, price, err,
                                 need_verify=bool((res or {}).get('need_verify')))
        task_log.warning(f"[双仓位] {inst_id} {self._tag(bucket, SLOT_ENTRY)} 挂单失败: {err}")
        return False

    def _place_exit(self, inst_id: str, bucket: str, direction: str,
                    amount: float, price: float, reason: str = '') -> bool:
        """挂平仓限价单（reduce-only）。direction 为**持仓方向**。"""
        bk = self._inst(inst_id)[bucket]
        mode = self._mode_of(direction)
        side = 'sell' if direction == 'long' else 'buy'
        amount = self._q(inst_id, amount)
        if amount <= 0 or price <= 0:
            return False
        if self.account is not None:
            if not self.synchronize(inst_id).get('success'):
                return False
            totals = self._book_totals(inst_id)
            room = self._exit_room(inst_id, bucket, direction, totals['long'], -totals['short'])
            amount = self._q(inst_id, min(amount, room, float(bk['held'][direction])))
            if amount <= 0:
                return False
        res = self._submit_order(inst_id, bucket, SLOT_EXIT, direction, amount, round(price, 6))
        if res and res.get('success'):
            trade_log.info(
                f"[双仓位] {inst_id} {self._tag(bucket, SLOT_EXIT)} 限价"
                f"{'平多' if direction == 'long' else '平空'} {amount}张 @ "
                f"{price:.6g} ({mode}) | 原因：{reason or '-'}")
            _act = '平多' if direction == 'long' else '平空'
            self._life(
                f"{inst_id} | 【{_act}·挂单】{BUCKET_LABEL.get(bucket, bucket)} | "
                f"{amount}张 @限价{price:.6g} | 原因：{reason or '-'}", key=True)
            return True
        err = res.get('error') if res else '无返回'
        self._note_place_failure(inst_id, bucket, SLOT_EXIT, amount, price, err,
                                 need_verify=bool((res or {}).get('need_verify')))
        task_log.warning(f"[双仓位] {inst_id} {self._tag(bucket, SLOT_EXIT)} 挂单失败: {err}")
        return False

    def _exit_room(self, inst_id: str, bucket: str, direction: str,
                   cross_pos: float, isolated_pos: float) -> float:
        """本篮子可挂平仓量上限 = 真实同向持仓 − 全部篮子已挂平仓量。

        净持仓模式下两篮子共享头寸，reduce-only 之外再加一层软件封顶，
        避免同向两篮子同时挂平仓单导致计划平仓量超过真实持仓。
        """
        s = self._inst(inst_id)
        real = max(0.0, float(cross_pos or 0)) if direction == 'long' \
            else abs(min(0.0, float(isolated_pos or 0)))
        pending = 0.0
        for b in BUCKETS:
            pt = s[b]['slots'][SLOT_EXIT]
            if pt.get('state') == ST_PENDING and pt.get('dir') == direction:
                if b == bucket:
                    continue  # 本篮子自己的挂单不计入（供下单/改量目标量使用）
                pending += self._remaining(pt)
        for pt in s.get('market_orders', {}).values():
            if pt.get('state') == ST_PENDING and pt.get('dir') == direction:
                if pt.get('bucket') in (bucket, 'account'):
                    return 0.0
                pending += self._remaining(pt)
        # 只做浮点收敛，不在此按步长取整 —— 步长取整由调用方 _q() 统一处理，
        # 这里若沿用 1 位小数会把小步长合约（XRP=0.01）的可平量直接抹成 0
        return round(max(0.0, real - pending), 12)

    def _reset_slot(self, bk: Dict, slot: str):
        """将终态（FILLED/EXPIRED）槽位复位为 IDLE，以便下一轮重新挂单"""
        if bk['slots'][slot].get('state') != ST_PENDING:
            bk['slots'][slot] = self._new_slot()

    # =================================================================
    # 对外：趋势跟踪仓位调度
    # =================================================================

    def process_trend(self, inst_id: str, plan: Dict,
                      cross_pos: float = 0.0, isolated_pos: float = 0.0) -> Dict:
        """趋势跟踪篮子一轮调度。

        plan 字段：
            period_mode : 'single'（单周期→双向）/ 'dual'（双周期→单向）
            target_dir  : 期望持仓方向 'long'/'short'， None 表示无信号
            entry_px    : 开仓限价（反转 bar 的 open/close，由配置决定）
            exit_px     : 平仓限价
            contracts   : 目标持仓张数
            leverage    : 杠杆
            allow_entry : 是否允许开新仓（凌晨强平/风控可关闭）
            open_window / close_window : dual 模式三时段窗口标识

        Returns: {'actions': [...], 'fills': [...]}
        """
        with self._lock:
            acts, fills = [], []
            bk = self._inst(inst_id)[BUCKET_TREND]
            for sl in SLOTS:
                a = self._reconcile_slot(inst_id, BUCKET_TREND, sl, fills)
                if a:
                    acts.append(a)

            desired = plan.get('target_dir')
            desired = desired if desired in ('long', 'short') else None
            args = (inst_id, bk, plan, desired, cross_pos, isolated_pos)
            if (plan.get('period_mode') or 'dual').lower() == 'single':
                acts += self._trend_single(*args)
            else:
                acts += self._trend_dual(*args)
            self._save(inst_id)
            return {'actions': acts, 'fills': fills}

    def _trend_single(self, inst_id: str, bk: Dict, plan: Dict, desired: Optional[str],
                      cross_pos: float, isolated_pos: float) -> list:
        """单周期双向：短周期方向翻转即平旧开新，一次反转信号只挂一次单（不追价）。"""
        acts = []
        if not desired:
            return acts
        entry_px = float(plan.get('entry_px') or 0)
        exit_px = float(plan.get('exit_px') or 0)
        contracts = self._q(inst_id, plan.get('contracts'))
        allow_entry = bool(plan.get('allow_entry', True))
        # 方向来源（配置锁定/页面锁定/信号驱动）随挂单原因入日志，
        # 使 trade_operations.log 能直接区分人工锁定与信号驱动的开平仓
        dsrc = str(plan.get('dir_source') or '信号驱动')
        opp = 'short' if desired == 'long' else 'long'

        # 新反转信号：撤销上一轮未成交挂单并复位槽位，开启新一轮挂单机会
        if bk.get('last_desired') != desired:
            prev = bk.get('last_desired')
            for sl in SLOTS:
                if bk['slots'][sl].get('state') == ST_PENDING:
                    if self._cancel_slot(inst_id, BUCKET_TREND, sl,
                                         f"短周期方向翻转({prev or '无'}→{desired})，旧挂单作废"):
                        acts.append(f'trend.{sl}撤单(方向翻转)')
                # 撤单结果不确定时 _cancel_slot 会保持 PENDING，不能强制复位丢账
                if bk['slots'][sl].get('state') != ST_PENDING:
                    bk['slots'][sl] = self._new_slot()
            bk['last_desired'] = desired

        held_d = float(bk['held'].get(desired, 0) or 0)
        held_o = float(bk['held'].get(opp, 0) or 0)

        # ---- 平仓槽：只服务于“反向持仓” ----
        ex = bk['slots'][SLOT_EXIT]
        if self._in_pos(held_o):
            room = self._exit_room(inst_id, BUCKET_TREND, opp, cross_pos, isolated_pos)
            target = self._q(inst_id, min(held_o, room))
            if ex.get('state') == ST_PENDING:
                if ex.get('dir') != opp:
                    if self._cancel_slot(inst_id, BUCKET_TREND, SLOT_EXIT, '平仓单方向不符'):
                        acts.append('trend.exit撤单(方向不符)')
                else:
                    a = self._amend_slot_size(inst_id, BUCKET_TREND, SLOT_EXIT, target)
                    if a:
                        acts.append(a)
            elif ex.get('state') == ST_IDLE and exit_px > 0 and target > 0:
                if self._place_exit(inst_id, BUCKET_TREND, opp, target, exit_px,
                                    reason=f"短周期方向翻转，平{opp}旧仓（方向来源：{dsrc}）"):
                    acts.append(f'trend.exit挂单{target}张@{exit_px:.6g}')
            # FILLED / EXPIRED → 本轮不补挂（不追价，等下次反转信号）
        else:
            if ex.get('state') == ST_PENDING:
                if self._cancel_slot(inst_id, BUCKET_TREND, SLOT_EXIT,
                                     self._exit_zero_reason(inst_id, '无反向持仓，平仓单作废')):
                    acts.append('trend.exit撤单(无需平仓)')
            else:
                self._reset_slot(bk, SLOT_EXIT)

        # ---- 开仓槽：仅当目标方向无仓时挂单 ----
        en = bk['slots'][SLOT_ENTRY]
        need_entry = allow_entry and not self._in_pos(held_d) and contracts > 0
        if en.get('state') == ST_PENDING and (not need_entry or en.get('dir') != desired):
            why = '目标方向已持仓' if self._in_pos(held_d) else (
                '禁止开新仓' if not allow_entry else (
                    '折算张数不足最小下单量' if contracts <= 0 else '开仓单方向不符'))
            if self._cancel_slot(inst_id, BUCKET_TREND, SLOT_ENTRY, why):
                acts.append(f'trend.entry撤单({why})')
        elif need_entry and en.get('state') == ST_PENDING and en.get('dir') == desired:
            # 金额模式：每轮按现价/配置折算张数会变，存量挂单须改量跟上，
            # 否则切换 size_mode 后永远沿用旧张数
            a = self._amend_slot_size(inst_id, BUCKET_TREND, SLOT_ENTRY, contracts,
                                      reason='配置目标量变化(张数/金额模式折算)')
            if a:
                acts.append(a)
        # FILLED 但持仓已清零（人工平仓/强平）：信号仍有效，复位终态槽位重挂，
        # 否则人工平掉后系统永远不会再开仓（方向未翻转就没有复位机会）
        if need_entry and bk['slots'][SLOT_ENTRY].get('state') == ST_FILLED \
                and not self._in_pos(held_d):
            bk['slots'][SLOT_ENTRY] = self._new_slot()
        if need_entry and bk['slots'][SLOT_ENTRY].get('state') == ST_IDLE and entry_px > 0:
            if self._place_entry(inst_id, BUCKET_TREND, desired, contracts, entry_px,
                                 plan.get('leverage'),
                                 max_position=float(plan.get('max_position') or 0),
                                 max_total=float(plan.get('max_total_position') or 0),
                                 cross_pos=cross_pos, isolated_pos=isolated_pos,
                                 reason=f"短周期方向翻转为{desired}，空仓开新"
                                        f"（方向来源：{dsrc}）"):
                acts.append(f'trend.entry挂单{contracts}张@{entry_px:.6g}')
        return acts

    def _trend_dual(self, inst_id: str, bk: Dict, plan: Dict, desired: Optional[str],
                    cross_pos: float, isolated_pos: float) -> list:
        """双周期单向：只做长周期方向，沿用三时段开/平仓窗口。

        窗口上升沿复位对应槽位（新一轮挂单机会），窗口结束撤销未成交挂单。
        长周期反转后的反向持仓不在此处处理（白天发邮件、凌晨由 trend_range_trader 强平）。
        """
        acts = []
        ow = bool(plan.get('open_window'))
        cw = bool(plan.get('close_window'))
        entry_px = float(plan.get('entry_px') or 0)
        exit_px = float(plan.get('exit_px') or 0)
        contracts = self._q(inst_id, plan.get('contracts'))
        allow_entry = bool(plan.get('allow_entry', True))
        # 方向来源（配置锁定/页面锁定/信号驱动）随挂单原因入日志
        dsrc = str(plan.get('dir_source') or '信号驱动')

        if desired and bk.get('last_desired') != desired:
            bk['last_desired'] = desired

        # 窗口上升沿 → 复位终态槽位
        if ow and not bk.get('prev_open_confirmed'):
            self._reset_slot(bk, SLOT_ENTRY)
        if cw and not bk.get('prev_close_confirmed'):
            self._reset_slot(bk, SLOT_EXIT)
        bk['prev_open_confirmed'] = ow
        bk['prev_close_confirmed'] = cw

        held_d = float(bk['held'].get(desired, 0) or 0) if desired else 0.0

        # ---- 开仓窗口 ----
        if ow and desired and allow_entry:
            need = self._q(inst_id, contracts - held_d)
            # 窗口持续期内终态槽位（已成交后被人工平掉/被外部撤销）必须能
            # 复位重挂，否则窗口内剩下的 bar 永远无法开仓；窗口本身由真实
            # 三时段共振判定（方向锁定不会使窗口恒开，2026-09-01 语义修正）
            if need > 0 and entry_px > 0 and \
                    bk['slots'][SLOT_ENTRY].get('state') != ST_PENDING:
                self._reset_slot(bk, SLOT_ENTRY)
                if self._place_entry(inst_id, BUCKET_TREND, desired, need, entry_px,
                                     plan.get('leverage'),
                                     max_position=float(plan.get('max_position') or 0),
                                     max_total=float(plan.get('max_total_position') or 0),
                                     cross_pos=cross_pos, isolated_pos=isolated_pos,
                                     reason=f'双周期共振，开仓窗口确认（方向来源：{dsrc}）'):
                    acts.append(f'trend.entry挂单{need}张@{entry_px:.6g}')
            elif bk['slots'][SLOT_ENTRY].get('state') == ST_PENDING:
                # 窗口内存量挂单同步目标量（金额模式折算张数随价格/配置变化）
                a = self._amend_slot_size(inst_id, BUCKET_TREND, SLOT_ENTRY, need,
                                          reason='配置目标量变化(张数/金额模式折算)',
                                          cancel_reason='目标量不足最小下单量')
                if a:
                    acts.append(a)
        elif bk['slots'][SLOT_ENTRY].get('state') == ST_PENDING:
            why = '禁止开新仓' if (ow and not allow_entry) else '开仓窗口结束'
            if self._cancel_slot(inst_id, BUCKET_TREND, SLOT_ENTRY, why):
                acts.append(f'trend.entry撤单({why})')

        # ---- 平仓窗口 ----
        if cw and desired and self._in_pos(held_d) and exit_px > 0:
            room = self._exit_room(inst_id, BUCKET_TREND, desired, cross_pos, isolated_pos)
            target = self._q(inst_id, min(held_d, room))
            ex = bk['slots'][SLOT_EXIT]
            if ex.get('state') == ST_PENDING:
                if ex.get('dir') != desired:
                    if self._cancel_slot(inst_id, BUCKET_TREND, SLOT_EXIT, '平仓单方向不符'):
                        acts.append('trend.exit撤单(方向不符)')
                else:
                    a = self._amend_slot_size(inst_id, BUCKET_TREND, SLOT_EXIT, target)
                    if a:
                        acts.append(a)
            elif ex.get('state') != ST_PENDING and target > 0:
                # 终态槽位复位重挂：平仓窗口持续期应一直尝试平到目标量，
                # 避免外部撤单/成交后持仓裸露无人处理（同开仓槽逻辑）
                self._reset_slot(bk, SLOT_EXIT)
                if self._place_exit(inst_id, BUCKET_TREND, desired, target, exit_px,
                                    reason=f'双周期短周期翻转，平仓窗口确认（方向来源：{dsrc}）'):
                    acts.append(f'trend.exit挂单{target}张@{exit_px:.6g}')
        elif bk['slots'][SLOT_EXIT].get('state') == ST_PENDING:
            why = (self._exit_zero_reason(inst_id, '持仓已清零')
                   if not self._in_pos(held_d) else '平仓窗口结束')
            if self._cancel_slot(inst_id, BUCKET_TREND, SLOT_EXIT, why):
                acts.append(f'trend.exit撤单({why})')
        return acts

    # =================================================================
    # 对外：区间波动仓位调度
    # =================================================================

    def process_range(self, inst_id: str, plan: Dict,
                      cross_pos: float = 0.0, isolated_pos: float = 0.0) -> Dict:
        """区间波动（boll_limit）篮子一轮调度——固定双周期单向、无限循环。

        无持仓 → 在开仓边界（长多=下轨 / 长空=上轨）挂限价开仓单，边界漂移时 amend 追价；
        成交 → 立即在对侧边界挂 reduce-only 平仓单；平仓成交 → 下一轮重新挂开仓单。

        plan 字段：target_dir / entry_px / exit_px / contracts / leverage /
                   amend_min_pct / allow_entry
        """
        with self._lock:
            acts, fills = [], []
            bk = self._inst(inst_id)[BUCKET_RANGE]
            for sl in SLOTS:
                a = self._reconcile_slot(inst_id, BUCKET_RANGE, sl, fills)
                if a:
                    acts.append(a)

            desired = plan.get('target_dir')
            desired = desired if desired in ('long', 'short') else None
            entry_px = float(plan.get('entry_px') or 0)
            exit_px = float(plan.get('exit_px') or 0)
            contracts = self._q(inst_id, plan.get('contracts'))
            min_pct = float(plan.get('amend_min_pct') or 0)
            allow_entry = bool(plan.get('allow_entry', True))
            # 方向来源（配置锁定/页面锁定/信号驱动）随挂单原因入日志
            dsrc = str(plan.get('dir_source') or '信号驱动')

            # 长周期反转：未成交挂单全部撤销换方向重挂（已持仓的强平由 trend_range_trader 执行）
            if desired and bk.get('last_desired') and bk['last_desired'] != desired:
                prev = bk['last_desired']
                for sl in SLOTS:
                    if bk['slots'][sl].get('state') == ST_PENDING:
                        if self._cancel_slot(inst_id, BUCKET_RANGE, sl,
                                             f"长周期反转({prev}→{desired})，区间挂单撤销重挂"):
                            acts.append(f'range.{sl}撤单(长周期反转)')
                    # 撤单结果不确定时 _cancel_slot 会保持 PENDING，不能强制复位丢账
                    if bk['slots'][sl].get('state') != ST_PENDING:
                        bk['slots'][sl] = self._new_slot()
            if desired:
                bk['last_desired'] = desired
            if not desired:
                return {'actions': acts, 'fills': fills}

            held_d = float(bk['held'].get(desired, 0) or 0)

            # ---- 开仓槽：仅无持仓时有效 ----
            en = bk['slots'][SLOT_ENTRY]
            if self._in_pos(held_d) or not allow_entry:
                if en.get('state') == ST_PENDING:
                    why = '已持仓，转挂平仓单' if self._in_pos(held_d) else '禁止开新仓'
                    if self._cancel_slot(inst_id, BUCKET_RANGE, SLOT_ENTRY, why):
                        acts.append(f'range.entry撤单({why})')
                else:
                    self._reset_slot(bk, SLOT_ENTRY)  # 终态槽位复位，便于下一轮重挂
            elif en.get('state') == ST_PENDING:
                if en.get('dir') != desired:
                    if self._cancel_slot(inst_id, BUCKET_RANGE, SLOT_ENTRY, '开仓单方向不符'):
                        acts.append('range.entry撤单(方向不符)')
                else:
                    # 追价 + 同步量：金额模式下每轮折算张数随价格/配置变化，
                    # 存量挂单必须改量跟上，否则切换 size_mode 后永远沿用旧张数
                    for a in (self._amend_slot_price(inst_id, BUCKET_RANGE, SLOT_ENTRY,
                                                     entry_px, min_pct),
                              self._amend_slot_size(inst_id, BUCKET_RANGE, SLOT_ENTRY,
                                                    contracts,
                                                    reason='配置目标量变化(张数/金额模式折算)',
                                                    cancel_reason='金额模式折算张数不足最小下单量')):
                        if a:
                            acts.append(a)
            elif contracts > 0 and entry_px > 0:
                # IDLE / FILLED / EXPIRED 均重挂 —— 区间仓位需持续循环刷边界
                if self._place_entry(inst_id, BUCKET_RANGE, desired, contracts, entry_px,
                                    plan.get('leverage'),
                                    max_position=float(plan.get('max_position') or 0),
                                    max_total=float(plan.get('max_total_position') or 0),
                                    cross_pos=cross_pos, isolated_pos=isolated_pos,
                                    reason=f"长周期{desired}，BOLL"
                                           f"{'下轨' if desired == 'long' else '上轨'}限价"
                                           f"（方向来源：{dsrc}）"):
                    acts.append(f'range.entry挂单{contracts}张@{entry_px:.6g}')

            # ---- 平仓槽：仅有持仓时有效 ----
            ex = bk['slots'][SLOT_EXIT]
            if not self._in_pos(held_d):
                if ex.get('state') == ST_PENDING:
                    why = self._exit_zero_reason(inst_id, '持仓已清零')
                    if self._cancel_slot(inst_id, BUCKET_RANGE, SLOT_EXIT, why):
                        acts.append(f'range.exit撤单({why})')
                else:
                    self._reset_slot(bk, SLOT_EXIT)
            else:
                room = self._exit_room(inst_id, BUCKET_RANGE, desired,
                                       cross_pos, isolated_pos)
                target = self._q(inst_id, min(held_d, room))
                if ex.get('state') == ST_PENDING:
                    if ex.get('dir') != desired:
                        if self._cancel_slot(inst_id, BUCKET_RANGE, SLOT_EXIT, '平仓单方向不符'):
                            acts.append('range.exit撤单(方向不符)')
                    else:
                        for a in (self._amend_slot_price(inst_id, BUCKET_RANGE, SLOT_EXIT,
                                                         exit_px, min_pct),
                                  self._amend_slot_size(inst_id, BUCKET_RANGE, SLOT_EXIT,
                                                        target)):
                            if a:
                                acts.append(a)
                elif exit_px > 0 and target > 0:
                    if self._place_exit(inst_id, BUCKET_RANGE, desired, target, exit_px,
                                        reason=f'对侧BOLL边界止盈（方向来源：{dsrc}）'):
                        acts.append(f'range.exit挂单{target}张@{exit_px:.6g}')

            self._save(inst_id)
            return {'actions': acts, 'fills': fills}

    # =================================================================
    # 对外：每轮前置轮询（成交入账 / 兜底委托触发检测）
    # =================================================================

    def poll_fills(self, inst_id: str, ttl_seconds: float = 0, allow_cancel: bool = True) -> Dict:
        """提前检测两个篮子全部槽位的成交并入账。

        在止盈止损检查之前调用：上一轮挂出的限价单若已成交，先入账才能
        让刚成交的开仓单本轮就受止损保护，而不是等下一轮(60s+)。
        与后续 process_trend/process_range 内的 _reconcile_slot 幂等（槽位
        进入终态后不会重复入账）。
        ttl_seconds: 挂单存续上限（秒），>0 启用 —— 超龄挂单主动撤销（TTL 到期），
        防止区间仓持久挂单/异常残留单在交易所侧无限期存续；撤销后槽位复位，
        同轮调度按最新价重挂。Returns: {'actions': [...], 'fills': [...]}
        """
        with self._lock:
            acts, fills = [], []
            for bucket in BUCKETS:
                for sl in SLOTS:
                    a = self._reconcile_slot(inst_id, bucket, sl, fills)
                    if a:
                        acts.append(a)
                    pt = self._inst(inst_id)[bucket]['slots'][sl]
                    if allow_cancel and pt.get('state') == ST_PENDING and pt.get('cancel_requested'):
                        self._cancel_slot(inst_id, bucket, sl, '继续确认撤单')
            if allow_cancel and float(ttl_seconds or 0) > 0:
                # TTL 挂单时限**只属于趋势跟踪仓**：趋势仓 auto 模式挂单靠开仓窗口
                # 结束先撤销，TTL 仅兜底防程序漏撤/崩溃残留。区间波动仓是随 BOLL
                # 边界改单追价的**持久挂单**，本就该长期存续，绝不能用 TTL 撤了重挂
                # （2026-09-28 用户明确纠正：只有趋势跟踪存在挂单时限）。
                for sl in SLOTS:
                    a = self._expire_ttl(inst_id, BUCKET_TREND, sl, float(ttl_seconds))
                    if a:
                        acts.append(a)
            for key, pt in list(self._inst(inst_id).get('market_orders', {}).items()):
                self._poll_order(inst_id, pt.get('bucket', 'account'), SLOT_EXIT, pt, fills)
            self._save(inst_id)
            return {'actions': acts, 'fills': fills}

    def _expire_ttl(self, inst_id: str, bucket: str, slot: str,
                    ttl_seconds: float) -> Optional[str]:
        """挂单 TTL 到期撤销：存续超过上限的 PENDING 挂单主动撤销（部分成交先入账）。

        **仅用于趋势跟踪仓**：趋势仓 auto 模式挂单通常由窗口结束先撤销，TTL 仅兜底
        防程序漏撤/崩溃残留时订单在交易所侧无限期存续。区间波动仓是随 BOLL 边界
        改单追价的持久挂单，本就应长期存续，不适用 TTL —— 调用方 poll_fills 只对
        BUCKET_TREND 触发本方法。返回动作描述或 None。
        """
        pt = self._inst(inst_id)[bucket]['slots'][slot]
        if pt.get('state') != ST_PENDING or not pt.get('placed_ts'):
            return None
        age = time.time() - float(pt.get('placed_ts', 0) or 0)
        if age < ttl_seconds:
            return None
        if self._cancel_slot(
                inst_id, bucket, slot,
                f"TTL到期(存续{age / 60:.0f}分钟>上限{ttl_seconds / 60:.0f}分钟，同轮重挂)"):
            return f'{bucket}.{slot}TTL撤销'
        return None

    def poll_algo_triggers(self, inst_id: str, bucket: str) -> list:
        """effective 仅代表触发；按子订单实际成交增量扣账，不按委托量乐观扣账。"""
        with self._lock:
            s = self._inst(inst_id)
            bk = s[bucket]
            s['algo_unknown'] = False
            events = []
            for d in ('long', 'short'):
                rec = bk['algo'].get(d)
                if not rec:
                    continue
                try:
                    if rec.get('algo_id'):
                        detail = self.executor.get_algo_order_details(rec['algo_id'])
                    else:
                        status, detail = self.executor.probe_algo_by_client_id(rec.get('cl_ord_id'), tries=1)
                        if status == 'found' and detail:
                            rec['algo_id'] = detail.get('algoId')
                    if not detail:
                        s['algo_unknown'] = True
                        continue
                    st = str(detail.get('state') or '').lower()
                    if st == 'effective':
                        ids = detail.get('ordIdList') or ([detail['ordId']] if detail.get('ordId') else [])
                        if not ids:
                            s['algo_unknown'] = True
                            continue
                        children = rec.setdefault('children', {})
                        for oid in ids:
                            oid = str(oid.get('ordId')) if isinstance(oid, dict) else str(oid)
                            pt = children.setdefault(oid, dict(self._new_slot(), state=ST_PENDING,
                                                      ord_id=oid, amount=rec.get('amount', 0), dir=d))
                            self._poll_order(inst_id, bucket, SLOT_EXIT, pt, [])
                        if all(p.get('state') in (ST_FILLED, ST_EXPIRED) for p in children.values()):
                            bk['algo'][d] = None
                            events.append(f'{d}兜底委托子订单已确认终态')
                        else:
                            s['algo_unknown'] = True
                    elif st in ('canceled', 'cancelled', 'order_failed'):
                        bk['algo'][d] = None
                    elif st not in ('live', 'pause'):
                        s['algo_unknown'] = True
                except Exception as e:
                    s['algo_unknown'] = True
                    task_log.warning(f'{inst_id} | 兜底委托未确认: {e}')
            self._save(inst_id)
            return events

    # =================================================================
    # 对外：交易所侧止盈止损兜底委托
    # =================================================================

    @staticmethod
    def _px_eq(a, b) -> bool:
        """触发价是否等效（千分之一以内不重挂，避免频繁摧毁重建）"""
        a = float(a or 0)
        b = float(b or 0)
        if a <= 0 and b <= 0:
            return True
        if a <= 0 or b <= 0:
            return False
        return abs(a - b) / max(a, b) < 0.001

    def _cancel_algo(self, inst_id: str, bucket: str, direction: str,
                     reason: str = '') -> bool:
        """撤销某篮子某方向的交易所兜底委托"""
        bk = self._inst(inst_id)[bucket]
        rec = bk['algo'].get(direction)
        if not rec or not rec.get('algo_id'):
            return False
        ok = False
        try:
            ok = bool(self.executor.cancel_algo_order(rec['algo_id'], inst_id))
        except Exception as e:
            task_log.warning(f"[双仓位] {inst_id} 撤销兜底委托异常: {e}")
        detail = self.executor.get_algo_order_details(rec['algo_id']) if ok else {}
        confirmed = detail.get('state') in ('canceled', 'cancelled', 'order_failed')
        if confirmed:
            bk['algo'][direction] = None
        else:
            rec['cancel_requested'] = True
        self._save(inst_id)
        self._life(
            f"{inst_id} | 【兜底委托·撤销】{BUCKET_LABEL.get(bucket, bucket)}"
            f"{'多头' if direction == 'long' else '空头'} | 原因：{reason or '不再需要'}"
            + ('' if confirmed else '（撤单未确认）'), key=True)
        return confirmed

    def sync_exchange_algo(self, inst_id: str, bucket: str, direction: str,
                           sl_trigger_px: float = None, tp_trigger_px: float = None,
                           amount: float = None) -> Optional[str]:
        """同步交易所侧止盈止损兜底委托（双保险的“保险”那一层）。

        本地评估为主：六类止盈中只有固定目标止盈和固定止损能表达为交易所委托，
        跟踪/动能/通道/分批/时间止盈必须由 tp_engine 本地评估。本方法只负责把
        可表达的部分挂到交易所，防范程序宕机/网络中断期间的极端行情。

        触发价基于**本地账本均价**计算（由调用方传入），委托量不超过本篮子持仓。
        持仓归零或参数变化时自动撤单重挂。返回动作描述或 None。
        """
        with self._lock:
            bk = self._inst(inst_id)[bucket]
            # 【2026-09-28 按用户要求移除 G3】不再检查 _storage_ok；仅要求“本币本轮
            # 对账已确认(ok)”才挂/改交易所侧兜底委托，避免在未确认持仓上挂错量错向的止盈止损。
            if self.account is not None and self._sync_state(inst_id) != 'ok':
                return None
            rec = bk['algo'].get(direction)
            if rec and not rec.get('algo_id'):
                return None
            held = float(bk['held'].get(direction, 0) or 0)
            amt = self._q(inst_id, min(float(amount) if amount else held, held))
            has_px = (sl_trigger_px and sl_trigger_px > 0) or \
                     (tp_trigger_px and tp_trigger_px > 0)

            if not self._in_pos(held) or amt <= 0 or not has_px:
                if self._cancel_algo(inst_id, bucket, direction,
                                     '持仓归零' if not self._in_pos(held) else '无有效触发价'):
                    self._save(inst_id)
                    return f'{bucket}.algo撤销'
                return None

            # 量差不足一个下单步长视为未变（重挂也无法表达更精细的量）
            _lot, _ = self._steps(inst_id)
            if rec and rec.get('algo_id') \
                    and abs(float(rec.get('amount', 0) or 0) - amt) < _lot - 1e-9 \
                    and self._px_eq(rec.get('sl'), sl_trigger_px) \
                    and self._px_eq(rec.get('tp'), tp_trigger_px):
                return None  # 参数未变 → 保留现有委托

            if rec and rec.get('algo_id'):
                if not self._cancel_algo(inst_id, bucket, direction, '触发价/数量变更，重挂'):
                    return None

            side = 'sell' if direction == 'long' else 'buy'
            rec = {'algo_id': None, 'cl_ord_id': 'cta' + uuid.uuid4().hex[:29],
                   'amount': amt, 'sl': float(sl_trigger_px or 0),
                   'tp': float(tp_trigger_px or 0), 'ts': time.time()}
            bk['algo'][direction] = rec
            if not self._save(inst_id):
                return None
            try:
                res = self.executor.create_tp_sl_order(
                    inst_id=inst_id, side=side, amount=amt,
                    trading_mode=self._mode_of(direction),
                    tp_trigger_px=tp_trigger_px, sl_trigger_px=sl_trigger_px,
                    algo_cl_ord_id=rec['cl_ord_id'])
            except Exception as e:
                task_log.warning(f"[双仓位] {inst_id} 挂兜底委托异常: {e}")
                return None
            if res and res.get('success'):
                rec['algo_id'] = res.get('algo_id')
                self._save(inst_id)
                parts = []
                if tp_trigger_px:
                    parts.append(f"止盈@{float(tp_trigger_px):.6g}")
                if sl_trigger_px:
                    parts.append(f"止损@{float(sl_trigger_px):.6g}")
                trade_log.info(
                    f"[双仓位] {inst_id} {BUCKET_LABEL.get(bucket, bucket)}"
                    f"{'多头' if direction == 'long' else '空头'} 交易所兜底委托已挂 "
                    f"{amt}张 " + ' '.join(parts))
                self._life(
                    f"{inst_id} | 【兜底委托·挂单】{BUCKET_LABEL.get(bucket, bucket)}"
                    f"{'多头' if direction == 'long' else '空头'} | {amt}张 | "
                    + ' '.join(parts), key=True)
                return f'{bucket}.algo挂单'
            if (res or {}).get('rejected') and not (res or {}).get('need_verify'):
                bk['algo'][direction] = None
                self._save(inst_id)
            task_log.warning(
                f"[双仓位] {inst_id} 兜底委托挂单失败: "
                f"{(res or {}).get('error', '无返回')}")
            return None
