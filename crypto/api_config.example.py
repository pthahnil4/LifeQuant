#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
OKX API 配置模板 - StrategyAI 专用
=====================================

这是 crypto/api_config.py 的**脱敏模板**。真实密钥文件不入库（见 .gitignore），
新环境搭建步骤：

    copy crypto\\api_config.example.py crypto\\api_config.py   （Windows）
    cp crypto/api_config.example.py crypto/api_config.py      （*nix）

然后把 ACCOUNTS 里三个账号的 api_key / secret_key / passphrase 填成真实值。
所有策略与接口都通过 `get_api_config(account)` / `list_accounts()` 取配置，
新增账号只需在 ACCOUNTS 追加一条记录。

作者：OKX API Demo
创建时间：2024-06-19
版本：v1.0
"""

# =============================================================================
# API 配置区域 - 请在这里配置您的API密钥
# =============================================================================

# =============================================================================
# 多账号 API 密钥注册表
# -----------------------------------------------------------------------------
# 每个账号一条记录，key 为账号内部标识（供程序/接口选择使用），
# name 为界面展示名称。新增账号只需在此追加一条记录即可。
# =============================================================================
ACCOUNTS = {
    'main': {
        'name': '主账号（改成你的展示名）',
        'api_key': "your_api_key_here",
        'secret_key': "your_secret_key_here",
        'passphrase': "your_passphrase_here",
        'flag': "1",  # 0=实盘, 1=模拟盘
    },
    'stageone': {
        'name': '子账号 StageOne（按需删改）',
        'api_key': "your_api_key_here",
        'secret_key': "your_secret_key_here",
        'passphrase': "your_passphrase_here",
        'flag': "1",
    },
}

# 默认账号（不指定账号时使用）
DEFAULT_ACCOUNT = 'main'

# 账号通用配置（所有账号共享）
_COMMON_CONFIG = {
    'use_server_time': False,  # 是否使用服务器时间
    'timeout': 30,             # 请求超时时间（秒）
}

# =============================================================================
# 配置获取函数
# =============================================================================

def get_api_config(account=None):
    """获取指定账号的 API 配置信息

    参数:
        account: 账号标识（ACCOUNTS 的 key）。为 None 时使用 DEFAULT_ACCOUNT。
    返回:
        dict，包含 api_key/secret_key/passphrase/flag 及通用配置，
        并附带 account/account_name 以便识别当前账号。
    """
    key = account or DEFAULT_ACCOUNT
    if key not in ACCOUNTS:
        raise ValueError(f"未知账号: {key}，可选账号: {list(ACCOUNTS.keys())}")
    acct = ACCOUNTS[key]
    config = dict(_COMMON_CONFIG)
    config.update({
        'account': key,
        'account_name': acct.get('name', key),
        'api_key': acct['api_key'],
        'secret_key': acct['secret_key'],
        'passphrase': acct['passphrase'],
        'flag': acct.get('flag', '0'),
    })
    return config


def list_accounts():
    """列出所有可选账号（供前端下拉选择）

    返回: [{'key': 'main', 'name': '主账号', 'is_default': True}, ...]
    """
    return [
        {'key': k, 'name': v.get('name', k), 'is_default': k == DEFAULT_ACCOUNT}
        for k, v in ACCOUNTS.items()
    ]


# 向后兼容：模块级默认账号配置（旧代码可能直接引用 API_CONFIG）
API_CONFIG = get_api_config()

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

def get_account_api(account=None):
    """获取账户API实例"""
    import okx.Account as Account
    config = get_api_config(account)
    return Account.AccountAPI(
        config['api_key'],
        config['secret_key'],
        config['passphrase'],
        config.get('use_server_time', False),
        config['flag']
    )

def get_trade_api(account=None):
    """获取交易API实例"""
    import okx.Trade as Trade
    config = get_api_config(account)
    return Trade.TradeAPI(
        config['api_key'],
        config['secret_key'],
        config['passphrase'],
        config.get('use_server_time', False),
        config['flag']
    )

def get_market_api(account=None):
    """获取市场数据API实例"""
    import okx.MarketData as MarketData
    config = get_api_config(account)
    return MarketData.MarketAPI(flag=config['flag'])

def get_funding_api(account=None):
    """获取资金账户API实例"""
    import okx.Funding as Funding
    config = get_api_config(account)
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
