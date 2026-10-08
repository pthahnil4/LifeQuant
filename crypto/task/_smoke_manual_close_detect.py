# -*- coding: utf-8 -*-
"""临时冒烟：_detect_external_close 手动平仓检测"""
import sys, time
sys.path.insert(0, r'd:\python\cryptoTrade')
from crypto.task.trend_range_trader import TrendRangeTrader as T
from crypto.task.utils.position_order_manager import BUCKET_TREND, BUCKET_RANGE


class FakeMgr:
    def __init__(self):
        self.books = {
            BUCKET_TREND: {'held': {'long': 0.0, 'short': 0.0}},
            BUCKET_RANGE: {'held': {'long': 0.0, 'short': 0.0}},
        }

    def get_book(self, inst_id, bucket):
        return self.books[bucket]


def new_trader(pause_min=30):
    o = T.__new__(T)
    o._manual_pause = {}
    o._pause_logged = set()
    o._saved = []
    o._save_manual_pause = lambda: o._saved.append(dict(o._manual_pause))
    o._load_config = lambda: {'global_settings': {'manual_close_pause_minutes': pause_min}}
    o.pos_mgr = FakeMgr()
    return o


# 场景1：交易所侧手动全平（账本2.0+0.6 → 0）→ 应触发冷却
o = new_trader()
pre = {BUCKET_TREND: {'long': 2.0, 'short': 0.0},
       BUCKET_RANGE: {'long': 0.6, 'short': 0.0}}
o._detect_external_close('X-USDT-SWAP', pre, 'run1')
assert 'X-USDT-SWAP' in o._manual_pause, '场景1失败：未设置冷却'
assert o._manual_pause['X-USDT-SWAP'] > time.time() + 29 * 60
assert o._saved and 'X-USDT-SWAP' in o._pause_logged
print('场景1 通过：手动全平 → 冷却已设置并持久化')

# 场景2：无缩减 → 不触发
o2 = new_trader()
pre2 = {BUCKET_TREND: {'long': 0.0, 'short': 0.0},
        BUCKET_RANGE: {'long': 0.0, 'short': 0.0}}
o2._detect_external_close('X-USDT-SWAP', pre2, 'run1')
assert 'X-USDT-SWAP' not in o2._manual_pause
print('场景2 通过：无缩减 → 不触发')

# 场景3：粉尘级缩减(0.05张) → 不触发
o3 = new_trader()
o3.pos_mgr.books[BUCKET_TREND]['held']['long'] = 1.95
pre3 = {BUCKET_TREND: {'long': 2.0, 'short': 0.0},
        BUCKET_RANGE: {'long': 0.0, 'short': 0.0}}
o3._detect_external_close('X-USDT-SWAP', pre3, 'run1')
assert 'X-USDT-SWAP' not in o3._manual_pause
print('场景3 通过：粉尘缩减 → 不触发')

# 场景4：部分手动减仓(2.0→1.5) → 应触发冷却（防止系统把减掉的部分买回来）
o4 = new_trader()
o4.pos_mgr.books[BUCKET_TREND]['held']['long'] = 1.5
pre4 = {BUCKET_TREND: {'long': 2.0, 'short': 0.0},
        BUCKET_RANGE: {'long': 0.0, 'short': 0.0}}
o4._detect_external_close('X-USDT-SWAP', pre4, 'run1')
assert 'X-USDT-SWAP' in o4._manual_pause
print('场景4 通过：部分减仓 → 触发冷却')

# 场景5：配置冷却=0 → 只记录不冷却
o5 = new_trader(pause_min=0)
pre5 = {BUCKET_TREND: {'long': 1.0, 'short': 0.0},
        BUCKET_RANGE: {'long': 0.0, 'short': 0.0}}
o5._detect_external_close('X-USDT-SWAP', pre5, 'run1')
assert 'X-USDT-SWAP' not in o5._manual_pause
print('场景5 通过：配置0 → 不冷却')

# 场景6：空头(逐仓)手动平仓 → 同样触发
o6 = new_trader()
o6.pos_mgr.books[BUCKET_TREND]['held']['short'] = 0.0
pre6 = {BUCKET_TREND: {'long': 0.0, 'short': 1.2},
        BUCKET_RANGE: {'long': 0.0, 'short': 0.0}}
o6._detect_external_close('X-USDT-SWAP', pre6, 'run1')
assert 'X-USDT-SWAP' in o6._manual_pause
print('场景6 通过：空头手动平仓 → 触发冷却')

print('全部冒烟通过')
