#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
OKX API 配置模板 - StrategyAI 专用（MACD_Smoothed_ADX_Pro3_Project 副本）
==========================================================================

这是同目录 api_config.py 的**脱敏模板**（真实密钥文件不入库，见 .gitignore）。
本目录这份是策略自带的单账号配置，与 crypto/api_config.py 的多账号注册表
是两套独立结构，改一处不会同步另一处，两处都要填。

    copy api_config.example.py api_config.py

创建时间：2024-06-19
版本：v1.0
"""

# =============================================================================
# API 配置区域 - 请在这里配置您的API密钥
# =============================================================================

# API配置
API_CONFIG = {
    # 您的API密钥信息
    'api_key': "your_api_key_here",
    'secret_key': "your_secret_key_here",
    'passphrase': "your_passphrase_here",

    # 交易环境配置
    # "0" = 实盘交易 (真实资金，谨慎操作)
    # "1" = 模拟盘交易 (虚拟资金，用于测试)
    'flag': "1",  # 0=实盘, 1=模拟盘

    # 其他配置
    'use_server_time': False,  # 是否使用服务器时间
    'timeout': 30,             # 请求超时时间（秒）
}

# =============================================================================
# 配置获取函数
# =============================================================================

def get_api_config():
    """获取API配置信息"""
    return API_CONFIG.copy()

def validate_config(config):
    """验证API配置是否完整"""
    required_keys = ['api_key', 'secret_key', 'passphrase']

    for key in required_keys:
        if not config.get(key):
            return False, f"缺少必需的配置项: {key}"

        # 检查是否为默认值
        if config[key] in ["your_api_key_here", "your_secret_key_here", "your_passphrase_here"]:
            return False, f"请配置真实的{key}值"

    return True, "配置验证通过"

def print_config_info(config):
    """打印配置信息（隐藏敏感信息）"""
    print("🔧 API配置信息:")
    print(f"   API Key: {config['api_key'][:8]}***{config['api_key'][-4:]}")
    print(f"   Secret Key: {config['secret_key'][:8]}***{config['secret_key'][-4:]}")
    print(f"   Passphrase: {'*' * len(config['passphrase'])}")
    print(f"   环境: {'🔴 实盘' if config['flag'] == '0' else '🟢 模拟盘'}")
    print(f"   超时: {config.get('timeout', 30)}秒")

# =============================================================================
# 快捷获取函数
# =============================================================================

def get_account_api():
    """获取账户API实例"""
    import okx.Account as Account
    config = get_api_config()
    return Account.AccountAPI(
        config['api_key'],
        config['secret_key'],
        config['passphrase'],
        config.get('use_server_time', False),
        config['flag']
    )

def get_trade_api():
    """获取交易API实例"""
    import okx.Trade as Trade
    config = get_api_config()
    return Trade.TradeAPI(
        config['api_key'],
        config['secret_key'],
        config['passphrase'],
        config.get('use_server_time', False),
        config['flag']
    )

def get_market_api():
    """获取市场数据API实例"""
    import okx.MarketData as MarketData
    config = get_api_config()
    return MarketData.MarketAPI(flag=config['flag'])

def get_funding_api():
    """获取资金账户API实例"""
    import okx.Funding as Funding
    config = get_api_config()
    return Funding.FundingAPI(
        config['api_key'],
        config['secret_key'],
        config['passphrase'],
        config.get('use_server_time', False),
        config['flag']
    )

# =============================================================================
# 配置管理工具
# =============================================================================

def switch_to_live_trading():
    """切换到实盘交易"""
    print("⚠️  切换到实盘交易模式")
    print("   这将使用真实资金进行交易！")
    confirm = input("   请输入 'YES' 确认切换到实盘: ")

    if confirm == 'YES':
        API_CONFIG['flag'] = '0'
        print("✅ 已切换到实盘交易模式")
        print("🚨 请谨慎操作，注意风险控制！")
    else:
        print("❌ 切换已取消，保持模拟盘模式")

def switch_to_demo_trading():
    """切换到模拟盘交易"""
    API_CONFIG['flag'] = '1'
    print("✅ 已切换到模拟盘交易模式")

# =============================================================================
# 主函数 - 用于测试配置
# =============================================================================

def main():
    """测试配置是否正确"""
    print("=" * 60)
    print("🔧 OKX API 配置测试 - StrategyAI")
    print("=" * 60)

    # 获取配置
    config = get_api_config()

    # 验证配置
    is_valid, message = validate_config(config)

    if is_valid:
        print("✅ 配置验证通过")
        print_config_info(config)

        # 测试API连接
        try:
            print("\n🔍 测试API连接...")
            account_api = get_account_api()
            result = account_api.get_account_balance()

            if result.get('code') == '0':
                print("✅ API连接测试成功")
                total_eq = result['data'][0]['totalEq']
                print(f"   账户总权益: ${total_eq}")
            else:
                print("❌ API连接测试失败")
                print(f"   错误信息: {result.get('msg', '未知错误')}")

        except Exception as e:
            print(f"❌ API连接测试异常: {e}")

    else:
        print(f"❌ 配置验证失败: {message}")
        print("\n💡 解决方案:")
        print("   1. 检查api_config.py中的API密钥配置")
        print("   2. 确保API密钥格式正确")
        print("   3. 检查API权限设置")

    print("\n" + "=" * 60)

if __name__ == "__main__":
    main()
