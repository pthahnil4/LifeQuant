#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
冒烟测试：币种自选配置写库失败的如实上报（问题#7）
==================================================
安全等级：🔒 纯离线 —— DB 层与配置文件路径全部打桩，写的是临时文件，
不连 DB、不调 OKX、不发信。

历史坑：save_config 里是 `except Exception: pass`。DB 写失败时本地文件照写、
HTTP 照回 200，而 load_config 是 **DB 优先** —— 用户以为改好了，刷新页面又
回到旧值，还拿着一个其实没生效的配置去监控行情。修复后 save_config 返回
DB 主存写入结果，并记入线程本地 last_save_db_ok()（一次性消费）供路由拼接
提示语。

为什么用线程本地而不是给业务函数加返回值：add/remove/clear 这些函数已有
自己的返回契约（如 `floating, added = add_floating_coins(...)`），改签名会
波及交易链路调用方。

场景清单：
1. DB 写抛异常 → save_config 返回 False，last_save_db_ok() 为 False，
   文件仍然落盘（兜底不能丢）
2. last_save_db_ok() 取完即清 → 第二次取回 True，不把上次失败残留给本请求
3. DB 正常 → 返回 True
4. session_scope 自身抛异常 → 同样如实报 False
5. DB 层根本没导入成功（_db_session_scope=None）→ 返回 False 且留痕；
   本轮没保存过时取回 True（不编造失败）

何时重跑：改 crypto/real_strategy_adapter.py 的 save_config / last_save_db_ok，
或改 app.py 的 _coin_cfg_warn()。
"""
import os
import sys
import json
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (_HERE, os.path.dirname(_HERE)):
    sys.path.insert(0, _p)

import crypto.real_strategy_adapter as rsa  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=''):
    (PASS if cond else FAIL).append(name)
    print(f"  {'[OK]' if cond else '[FAIL]'} {name}" + (f"  {detail}" if detail else ''))


# ------------------------------------------------------------------ 沙箱装配
_TMP = tempfile.mkdtemp(prefix='smoke_cfg_save_')
rsa.CONFIG_PATH = os.path.join(_TMP, 'config.json')          # 别写真 config.json
_ORIG_REPO = rsa._config_store_repo
_ORIG_SCOPE = rsa._db_session_scope

CFG = {"all_coins": ["BTC-USDT-SWAP"], "default_selected": [],
       "starred_coins": [], "floating_coins": ["X-USDT-SWAP"]}


class _BoomRepo:
    KEY_COIN_SELECTION = 'coin_selection'

    @staticmethod
    def save_json_config(session, key, value):
        raise RuntimeError('模拟主存不可写')


class _GoodRepo(_BoomRepo):
    @staticmethod
    def save_json_config(session, key, value):
        return True


def _scope_ok():
    class _Ctx:
        def __enter__(self):
            return object()

        def __exit__(self, *a):
            return False
    return _Ctx()


def _scope_boom():
    class _Ctx:
        def __enter__(self):
            raise RuntimeError('模拟 session 打不开')

        def __exit__(self, *a):
            return False
    return _Ctx()


def read_file_cfg():
    with open(rsa.CONFIG_PATH, 'r', encoding='utf-8') as f:
        return json.load(f)


try:
    # 1) DB 写失败：返回 False + 标记 False，但文件仍写（兜底不丢）
    rsa._config_store_repo, rsa._db_session_scope = _BoomRepo, _scope_ok
    rsa.last_save_db_ok()                     # 清场，避免上个线程残留
    ret = rsa.save_config(dict(CFG))
    check("1 DB写失败返回 False", ret is False, ret)
    check("1b last_save_db_ok 报失败", rsa.last_save_db_ok() is False)
    check("1c 失败时文件仍落盘（兜底）",
          read_file_cfg().get("floating_coins") == ["X-USDT-SWAP"])

    # 2) 一次性消费：取完即清，不把上次失败残留给下一个请求
    check("2 第二次取恢复为 True（不污染他请求）", rsa.last_save_db_ok() is True)

    # 3) DB 正常：返回 True
    rsa._config_store_repo, rsa._db_session_scope = _GoodRepo, _scope_ok
    check("3 DB写成功返回 True", rsa.save_config(dict(CFG)) is True)
    check("3b 标记同步为 True", rsa.last_save_db_ok() is True)

    # 4) session_scope 自身抛异常，同样要如实报失败
    rsa._config_store_repo, rsa._db_session_scope = _GoodRepo, _scope_boom
    check("4 session 打不开也报 False", rsa.save_config(dict(CFG)) is False)
    check("4b 标记同步为 False", rsa.last_save_db_ok() is False)

    # 5) DB 层未导入成功（模块级 except ImportError 走了 None）
    rsa._config_store_repo, rsa._db_session_scope = None, None
    check("5 DB层缺失报 False", rsa.save_config(dict(CFG)) is False)
    check("5b 本轮没保存过时不编造失败",
          (rsa.last_save_db_ok(), rsa.last_save_db_ok()) == (False, True))
finally:
    rsa._config_store_repo, rsa._db_session_scope = _ORIG_REPO, _ORIG_SCOPE
    try:
        os.remove(rsa.CONFIG_PATH)
        os.rmdir(_TMP)
    except OSError:
        pass

print("\n" + "=" * 52)
print(f"  PASS {len(PASS)}  /  FAIL {len(FAIL)}")
if FAIL:
    print("  失败项: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
