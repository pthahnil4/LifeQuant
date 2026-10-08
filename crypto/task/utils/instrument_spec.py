#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
OKX 永续合约规格缓存与金额⇄张数换算
======================================

背景：OKX 永续合约下单数量单位是"张"，但每张的名义价值因币种而异 ——
USDT 本位永续的合约面值(ctVal)以**标的币**计价（如 GPS-USDT-SWAP
ctVal=10 GPS），折算成 USDT 需乘以实时价格，且各币种没有统一面值标准。
因此网页侧让用户直接输入 USDT 金额（**占用保证金预算**），由本模块换算成合规张数：

    1张名义价值(USDT) = ctVal × 现价
    张数 = floor(目标USDT × 杠杆 ÷ 每张名义价值 ÷ lotSz) × lotSz
           （保证金×杠杆=可开持仓价值；向下取整，实际占用保证金
            不超输入金额；杠杆为 None/0 时按 1x 处理）
    换算结果 < minSz 时返回 0（低于最小下单量，应拒单）

数据来源：公共接口 GET /api/v5/public/instruments（无需鉴权），
字段 ctVal / ctValCcy / lotSz / minSz / tickSz。

缓存策略：规格几乎不变（仅交易所调整时变动），内存缓存 + 磁盘持久化
（instrument_spec_cache.json），TTL 默认 24 小时；接口失败时降级使用
过期缓存，保证断网/接口抖动不阻断交易链路。

作者：AI Assistant
创建时间：2026-08-19
"""

import os
import json
import math
import time
import threading
from decimal import Decimal
from typing import Dict, Optional

import okx.PublicData as PublicData

try:
    from .logger import get_task_logger
except ImportError:
    from logger import get_task_logger

# 只读接口限频/退避（问题#8）：合约规格接口上限 20 次/2s，批量预热时易触顶。
# 导入失败时置 None，直通原始调用，不因限频模块缺失而阻断交易链路。
try:
    from .okx_ratelimit import limited as _rl_limited
except ImportError:
    try:
        from okx_ratelimit import limited as _rl_limited
    except ImportError:
        _rl_limited = None


def _rl(group, func, *args, **kwargs):
    """只读 OKX 接口的统一出口：节流 + 50011 退避；限频模块缺失时直通。"""
    return _rl_limited(group, func, *args, **kwargs) if _rl_limited else func(*args, **kwargs)

task_log = get_task_logger()

# 缓存已迁移 MySQL（迁移批次8，kv_store key='instrument_spec_cache'）：
# 读 DB 优先、不可用/无数据时回退磁盘文件；写 DB 主存 + 磁盘双写（文件保留为兜底数据源）。
# 双模式导入：Flask 包内（cryptoTrade 根在 sys.path）/ 独立脚本（仅 task/utils 目录）
try:
    from crypto.database import session_scope as _db_session_scope
    from crypto import config_store_repo as _config_store_repo
except ImportError:
    _db_session_scope = None
    _config_store_repo = None


def _lot_decimals(lot_sz: float) -> int:
    """lotSz 的小数位数（1 → 0 位，0.1 → 1 位，0.01 → 2 位）"""
    try:
        exp = Decimal(str(lot_sz)).normalize().as_tuple().exponent
        return max(0, -int(exp)) if isinstance(exp, int) else 0
    except Exception:
        return 0


def _is_not_exist_response(r) -> bool:
    """交易所是否明确回答"这个合约不存在"（区别于网络超时/限频等临时失败）。

    口径来自运行时实测：不存在的 instId 固定回 code=51001、
    msg="Instrument ID, Instrument ID code, or Spread ID doesn't exist."。
    只有这里判定为真，调用方才允许把该合约当作"幽灵条目"跳过对账 ——
    临时失败必须按未知处理，否则一次网络抖动就能让真实持仓脱离对账。
    """
    if not isinstance(r, dict):
        return False
    if str(r.get('code') or '') == '51001':
        return True
    return "doesn't exist" in str(r.get('msg') or '').lower()


class InstrumentSpecCache:
    """合约规格缓存 + 金额/张数换算器（公共接口，无需 API Key）"""

    # 规格缺失（首次拉取失败/断网）时的兜底下单步长（张）：多数 USDT 永续
    # lotSz=0.1，与历史硬编码行为一致 —— 宁可退回存量行为，也不凭空放大下单量
    FALLBACK_LOT_SZ = 0.1

    def __init__(self, public_api=None, flag: str = '0',
                 cache_file: str = None, ttl_hours: float = 24.0):
        """
        Args:
            public_api: 已初始化的 okx.PublicData.PublicAPI 实例（可复用执行器的）；
                        不传则按 flag 自建。注意新版 SDK 构造函数首参是 api_key，
                        公共查询必须用 PublicAPI(flag=flag)，不能传 False 作首参。
            flag: '0'实盘 / '1'模拟盘（模拟盘域名与合约规格独立）
            cache_file: 磁盘缓存路径，默认与本模块同目录
            ttl_hours: 缓存有效期（小时），过期自动刷新
        """
        self.flag = flag
        self._api = public_api
        self.ttl_seconds = ttl_hours * 3600
        # 缓存文件外置到统一数据目录（crypto/data_paths.py，四级优先级解析）
        try:
            from crypto.data_paths import resolve_data_file
        except ImportError:
            def resolve_data_file(filename, legacy_path=None):
                return legacy_path
        self.cache_file = cache_file or resolve_data_file(
            'instrument_spec_cache.json',
            os.path.join(os.path.dirname(os.path.abspath(__file__)), 'instrument_spec_cache.json'))
        self._mem: Dict[str, Dict] = {}
        # 交易所明确回"合约不存在"(51001) 的合约 → 判定时戳。只在内存，
        # 不随规格缓存落库：缓存被清或币种重新上架时会自动重新探测。
        self._not_exist: Dict[str, float] = {}
        self._lock = threading.Lock()
        self._load_disk()

    # =================================================================
    # 规格获取（内存 → 磁盘 → 远程，逐级降级）
    # =================================================================

    def _get_api(self):
        if self._api is None:
            self._api = PublicData.PublicAPI(flag=self.flag)
        return self._api

    def _load_disk(self):
        """加载缓存：迁移批次8 起 DB 优先，DB 不可用/无数据时回退磁盘文件"""
        if _db_session_scope is not None and _config_store_repo is not None:
            try:
                with _db_session_scope() as s:
                    data = _config_store_repo.load_json_config(
                        s, _config_store_repo.KEY_INSTRUMENT_SPEC_CACHE)
                if isinstance(data, dict) and data:
                    self._mem = data
                    return
            except Exception as e:
                task_log.warning(f"[合约规格] 加载DB缓存失败，回退磁盘文件: {e}")
        try:
            if os.path.exists(self.cache_file):
                with open(self.cache_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        self._mem = data
        except Exception as e:
            task_log.warning(f"[合约规格] 加载磁盘缓存失败: {e}")

    def _save_disk(self):
        """保存缓存：DB 主存 + 磁盘文件双写（文件保留为兜底数据源）"""
        if _db_session_scope is not None and _config_store_repo is not None:
            try:
                with _db_session_scope() as s:
                    _config_store_repo.save_json_config(
                        s, _config_store_repo.KEY_INSTRUMENT_SPEC_CACHE, self._mem)
            except Exception as e:
                task_log.warning(f"[合约规格] 保存DB缓存失败: {e}")
        try:
            tmp = self.cache_file + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(self._mem, f, indent=2, ensure_ascii=False)
            os.replace(tmp, self.cache_file)
        except Exception as e:
            task_log.warning(f"[合约规格] 保存磁盘缓存失败: {e}")

    def _fetch_remote(self, inst_id: str) -> Optional[Dict]:
        """调用公共接口拉取合约规格，失败返回 None"""
        try:
            r = _rl('instruments', self._get_api().get_instruments,
                    instType='SWAP', instId=inst_id)
            if not r or r.get('code') != '0' or not r.get('data'):
                if _is_not_exist_response(r):
                    # 与"拉取失败"分开记：前者是该合约压根不存在（多次每轮重试
                    # 只会白打接口并拖慢整轮），后者下次还该重试
                    self._not_exist[inst_id] = time.time()
                    task_log.warning(
                        f"[合约规格] {inst_id} 交易所在该环境下无此合约"
                        f"（51001）: {(r or {}).get('msg')}")
                else:
                    task_log.warning(
                        f"[合约规格] {inst_id} 接口返回异常: "
                        f"{(r or {}).get('msg') or (r or {}).get('code')}")
                return None
            d = r['data'][0]
            self._not_exist.pop(inst_id, None)   # 查到了即撤销不存在判定
            spec = {
                'ct_val': float(d.get('ctVal') or 0),
                'ct_val_ccy': d.get('ctValCcy', ''),
                'lot_sz': float(d.get('lotSz') or 0),
                'min_sz': float(d.get('minSz') or 0),
                'tick_sz': float(d.get('tickSz') or 0),
                'settle_ccy': d.get('settleCcy', ''),
                # 该合约支持的最大杠杆（公共接口 lever 字段），不同币种上限不同
                # 如 GPS=20, KAITO=50, BTC=100；用户配置杠杆超过此值会被交易所拒单
                'max_lever': float(d.get('lever') or 0),
                'fetched_ts': time.time(),
            }
            if spec['ct_val'] <= 0 or spec['lot_sz'] <= 0:
                task_log.warning(f"[合约规格] {inst_id} 规格字段异常: {spec}")
                return None
            return spec
        except Exception as e:
            task_log.warning(f"[合约规格] {inst_id} 拉取失败: {e}")
            return None

    def get_spec(self, inst_id: str, refresh: bool = False) -> Optional[Dict]:
        """获取合约规格（带缓存）。彻底拿不到时返回 None。

        Returns: {'ct_val','ct_val_ccy','lot_sz','min_sz','tick_sz',
                  'settle_ccy','max_lever','fetched_ts'}
        """
        with self._lock:
            cached = self._mem.get(inst_id)
            fresh = cached and not refresh and \
                (time.time() - float(cached.get('fetched_ts', 0))) < self.ttl_seconds
            if fresh:
                return dict(cached)

            spec = self._fetch_remote(inst_id)
            if spec:
                self._mem[inst_id] = spec
                self._save_disk()
                return dict(spec)

            # 远程失败 → 降级使用过期缓存（有规格总比没有强，规格极少变动）
            if cached:
                task_log.warning(
                    f"[合约规格] {inst_id} 远程拉取失败，降级使用过期缓存"
                    f"（缓存时间 {time.strftime('%Y-%m-%d %H:%M', time.localtime(float(cached.get('fetched_ts', 0))))}）")
                return dict(cached)
            return None

    def instrument_exists(self, inst_id: str) -> Optional[bool]:
        """合约在交易所是否存在：True 存在 / False 明确不存在 / None 未知。

        只用于把“账本里看得到、但交易所已无此合约”的幽灵条目从对账范围里
        摘出去，**不得作为能否下单的依据**（下单前另有 minSz/额度/风控校验）。
        判定优先级：有缓存规格 → 存在；无缓存但近期回过 51001 → 不存在；
        两者都没命中则拉一次（网络失败返回 None，由调用方按保守口径处理）。
        """
        with self._lock:
            if self._mem.get(inst_id):
                return True
            ts = float(self._not_exist.get(inst_id) or 0)
            if ts and (time.time() - ts) < self.ttl_seconds:
                return False
        if self.get_spec(inst_id):
            return True
        with self._lock:
            return False if inst_id in self._not_exist else None

    # =================================================================
    # 金额 ⇄ 张数 换算
    # =================================================================

    def contract_usd_value(self, inst_id: str, price: float) -> float:
        """单张合约的名义价值(USDT) = ctVal × 现价；无法计算返回 0"""
        spec = self.get_spec(inst_id)
        price = float(price or 0)
        if not spec or price <= 0:
            return 0.0
        return spec['ct_val'] * price

    def usd_to_contracts(self, inst_id: str, usd: float, price: float,
                         leverage: float = 1, enforce_min_sz: bool = True) -> float:
        """目标金额(USDT) → 合规张数（金额=占用保证金预算，杠杆为放大因子）。

        公式：张数 = floor(目标USDT × 杠杆 ÷ 每张名义价值 ÷ lotSz) × lotSz
        例：KAITO ctVal=1、币价0.3453、预算1 USDT 保证金、杠杆20x
            → 每张0.3453 USDT，floor(1×20÷0.3453)=57 张
            （持仓价值≈19.68 USDT，实际占用保证金≈1 USDT）
        leverage 为 None/0/非法值时默认按 1x（无杠杆，存量行为）。
        向下取整到 lotSz 的整数倍（保证实际占用保证金不超过用户输入）；
        enforce_min_sz=True 时低于最小下单量返回 0（调用方应拒单/跳过）。
        风控上限换算等场景传 enforce_min_sz=False，只按步长取整。
        """
        spec = self.get_spec(inst_id)
        usd = float(usd or 0)
        price = float(price or 0)
        try:
            lev = float(leverage or 0)
        except (TypeError, ValueError):
            lev = 0.0
        if lev <= 0:
            lev = 1.0
        if not spec or usd <= 0 or price <= 0:
            return 0.0
        per_contract = spec['ct_val'] * price
        if per_contract <= 0:
            return 0.0
        lot_sz = spec['lot_sz']
        # +1e-9 容错浮点误差，避免 57.9999999 被 floor 成 57
        lots = math.floor(usd * lev / per_contract / lot_sz + 1e-9)
        contracts = round(lots * lot_sz, _lot_decimals(lot_sz))
        if enforce_min_sz and contracts + 1e-12 < spec['min_sz']:
            return 0.0
        return contracts

    def steps(self, inst_id: str) -> tuple:
        """返回该合约的 (lotSz 下单步长, minSz 最小下单量)，规格缺失回退 (0.1, 0.1)。

        所有下单量的取整与“能否下单”判定必须一律走本方法 —— 各合约步长差异
        极大（XRP=0.01 / NEAR=0.1 / POL=1），任何硬编码步长都会在某类合约上
        出错：2026-09-01 排查确认，硬编码 0.1 张使 XRP 折算出的 0.03 张被抹成
        0 而永不挂单，POL 的 0.5 张则必被交易所拒单（msg=All operations failed）。
        """
        spec = self.get_spec(inst_id)
        if not spec:
            return self.FALLBACK_LOT_SZ, self.FALLBACK_LOT_SZ
        lot = float(spec.get('lot_sz') or 0) or self.FALLBACK_LOT_SZ
        min_sz = float(spec.get('min_sz') or 0) or lot
        return lot, min_sz

    def quantize(self, inst_id: str, amount: float,
                 enforce_min_sz: bool = True) -> float:
        """张数规范化：向下取整到 lotSz 整数倍（宁少勿多，绝不放大敞口）。

        enforce_min_sz=True 时结果低于 minSz 返回 0.0（调用方须跳过下单，
        否则交易所必拒）；False 时只做步长取整，用于展示与量差比较。
        """
        amount = float(amount or 0)
        if amount <= 0:
            return 0.0
        lot, min_sz = self.steps(inst_id)
        out = round(math.floor(amount / lot + 1e-9) * lot, _lot_decimals(lot))
        if enforce_min_sz and out + 1e-12 < min_sz:
            return 0.0
        return out

    def contracts_to_usd(self, inst_id: str, contracts: float, price: float,
                         leverage: float = 1) -> float:
        """张数 → 持仓名义价值(USDT) = 张数 × 每张面值，用于前端展示与风控口径统一。

        名义价值与杠杆无关（杠杆只影响占用保证金）；
        对应占用保证金 = 名义价值 ÷ 杠杆，由展示层自行换算。
        leverage 参数保留仅为兼容既有调用签名，不参与计算。"""
        per_contract = self.contract_usd_value(inst_id, price)
        if per_contract <= 0:
            return 0.0
        return float(contracts or 0) * per_contract

    def preview(self, inst_id: str, price: float = 0,
                usd: float = None, contracts: float = None,
                leverage: float = 1) -> Dict:
        """供 Web API 使用的综合预览：规格 + 现价 + 双向换算结果

        usd 入参视为占用保证金预算：contracts 按 ×杠杆 折算，
        notional_usd 为持仓名义价值（=张数×每张面值），
        margin_usd 为实际占用保证金（=名义价值÷杠杆）。"""
        spec = self.get_spec(inst_id)
        price = float(price or 0)
        result = {
            'instId': inst_id,
            'price': price,
            'leverage': leverage,
            'spec': None,
            'contract_usd_value': 0.0,
            'contracts': 0.0,
            'notional_usd': 0.0,
            'margin_usd': 0.0,
        }
        if not spec:
            return result
        result['spec'] = {k: spec.get(k) for k in
                          ('ct_val', 'ct_val_ccy', 'lot_sz', 'min_sz',
                           'tick_sz', 'settle_ccy', 'max_lever', 'fetched_ts')}
        result['max_lever'] = spec.get('max_lever', 0)
        result['contract_usd_value'] = self.contract_usd_value(inst_id, price)
        if usd is not None:
            result['contracts'] = self.usd_to_contracts(
                inst_id, usd, price, leverage=leverage)
            result['notional_usd'] = self.contracts_to_usd(
                inst_id, result['contracts'], price)
            # 实际占用保证金 = 名义价值 ÷ 杠杆（展示用，换算口径与下单一致）
            try:
                lev = float(leverage or 0)
            except (TypeError, ValueError):
                lev = 0.0
            result['margin_usd'] = (result['notional_usd'] / lev) if lev > 0 \
                else result['notional_usd']
        elif contracts is not None:
            result['contracts'] = float(contracts or 0)
            result['notional_usd'] = self.contracts_to_usd(
                inst_id, contracts, price, leverage=leverage)
        return result


# =====================================================================
# 单例访问（按 flag 区分实盘/模拟盘）
# =====================================================================

_instances: Dict[str, InstrumentSpecCache] = {}
_instances_lock = threading.Lock()


def get_instrument_spec_cache(flag: str = '0') -> InstrumentSpecCache:
    """获取指定环境的合约规格缓存单例"""
    key = str(flag or '0')
    with _instances_lock:
        if key not in _instances:
            _instances[key] = InstrumentSpecCache(flag=key)
        return _instances[key]
