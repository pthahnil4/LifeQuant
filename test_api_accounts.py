#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
测试 api_config.py 中配置的三个账号的API密钥是否正常可用
仅执行只读查询（get_account_balance），不会产生任何交易操作
"""

import sys
import os

# 确保能找到 crypto 模块
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from crypto.api_config import ACCOUNTS, get_api_config, validate_config
import okx.Account as Account


def test_account(account_key):
    """测试单个账号的API连接"""
    print(f"\n{'='*60}")
    print(f"  账号: {account_key}")
    print(f"  名称: {ACCOUNTS[account_key].get('name', account_key)}")
    print(f"{'='*60}")

    # 1. 获取配置
    try:
        config = get_api_config(account_key)
    except Exception as e:
        print(f"  ❌ 配置获取失败: {e}")
        return False

    # 2. 验证配置完整性
    is_valid, msg = validate_config(config)
    if not is_valid:
        print(f"  ❌ 配置验证失败: {msg}")
        return False

    # 打印脱敏信息（不泄露完整密钥）
    print(f"  API Key: {config['api_key'][:8]}...{config['api_key'][-4:]}")
    print(f"  Secret Key: {config['secret_key'][:8]}...{config['secret_key'][-4:]}")
    print(f"  环境: {'实盘' if config['flag'] == '0' else '模拟盘'}")

    # 3. 测试API调用 - 仅只读查询
    try:
        account_api = Account.AccountAPI(
            config['api_key'],
            config['secret_key'],
            config['passphrase'],
            config.get('use_server_time', False),
            config['flag']
        )

        result = account_api.get_account_balance()

        # 检查返回码
        code = result.get('code')
        if code == '0':
            data = result.get('data', [])
            if data:
                info = data[0]
                total_eq = info.get('totalEq', 'N/A')
                frozen_bal = info.get('frozenBal', 'N/A')
                total_bal = info.get('totalBal', 'N/A')
                print(f"  ✅ API调用成功！")
                print(f"  ┌─────────────────────────────────────────────")
                print(f"  │ 账户总权益 (USD):    {total_eq}")
                print(f"  │ 账户总余额 (USD):    {total_bal}")
                print(f"  │ 冻结金额 (USD):      {frozen_bal}")
                print(f"  └─────────────────────────────────────────────")
                
                # 列出持有币种（如有）
                details = info.get('details', [])
                if details:
                    print(f"  持有币种明细:")
                    for d in details:
                        ccy = d.get('ccy', '')
                        eq = d.get('eq', '0')
                        if float(eq) > 0:
                            print(f"    - {ccy}: {eq} USD")
                else:
                    print(f"  当前无持仓或余额为零")
                return True
            else:
                print(f"  ⚠️  API调用成功但返回数据为空")
                print(f"  返回: {result}")
                return True
        else:
            print(f"  ❌ API调用失败")
            print(f"  错误码: {code}")
            print(f"  错误信息: {result.get('msg', '未知错误')}")
            return False

    except Exception as e:
        print(f"  ❌ API调用异常: {type(e).__name__}: {e}")
        return False


def main():
    print("=" * 60)
    print("  OKX 多账号API密钥连通性测试")
    print("  " + "=" * 40)
    print(f"  测试时间: {__import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  OKX SDK 版本: {__import__('okx').__version__}")
    print("=" * 60)

    account_keys = list(ACCOUNTS.keys())
    print(f"\n共发现 {len(account_keys)} 个账号配置: {', '.join(account_keys)}")

    results = {}
    success_count = 0

    for key in account_keys:
        ok = test_account(key)
        results[key] = ok
        if ok:
            success_count += 1

    # 汇总结果
    print(f"\n{'='*60}")
    print(f"  测试汇总")
    print(f"{'='*60}")
    for key, ok in results.items():
        status_icon = "✅" if ok else "❌"
        name = ACCOUNTS[key].get('name', key)
        print(f"  {status_icon} {key:12s} | {name}")
    print(f"\n  结果: {success_count}/{len(account_keys)} 个账号通过测试")

    if success_count == len(account_keys):
        print("  🎉 所有账号API密钥均正常可用！")
    else:
        print("  ⚠️  部分账号存在问题，请检查上述错误信息。")

    print(f"\n{'='*60}")


if __name__ == "__main__":
    main()