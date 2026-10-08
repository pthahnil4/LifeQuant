#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
OKX API 设置向导
===============

快速设置API配置的向导工具

功能说明:
--------
1. 提供交互式界面配置OKX API密钥
2. 自动生成api_config.py配置文件
3. 支持选择交易环境（实盘/模拟盘）
4. 提供即时API连接测试

配置内容:
--------
1. API密钥信息
   - API Key
   - Secret Key
   - Passphrase
2. 交易环境设置
   - 模拟盘（推荐测试用）
   - 实盘（真实资金交易）
3. 其他配置
   - 超时设置
   - 服务器时间同步

使用方法:
--------
直接运行: python setup_wizard.py

工作流程:
--------
1. 引导获取API密钥
2. 输入密钥信息
3. 选择交易环境
4. 生成配置文件
5. 测试API连接

注意事项:
--------
1. API密钥请妥善保管
2. 建议先使用模拟盘测试
3. 实盘交易需谨慎
4. 配置文件包含敏感信息，注意安全

作者：OKX API Demo
创建时间：2024-06-19
版本：v1.0
"""

import os
from pathlib import Path

def main():
    print("=" * 60)
    print("🧙‍♂️ OKX API 设置向导")
    print("=" * 60)
    
    print("欢迎使用OKX API Demo！")
    print("这个向导将帮助您快速配置API密钥。")
    
    print(f"\n📋 配置步骤:")
    print(f"1. 获取API密钥")
    print(f"2. 配置api_config.py文件") 
    print(f"3. 测试连接")
    
    print(f"\n🔑 如何获取API密钥:")
    print(f"1. 访问 https://www.okx.com")
    print(f"2. 登录您的账户")
    print(f"3. 进入 个人中心 → API管理")
    print(f"4. 创建新的API密钥")
    print(f"5. 记录下 API Key、Secret Key、Passphrase")
    
    input(f"\n按回车键继续...")
    
    try:
        print(f"\n请输入您的API信息:")
        api_key = input("API Key: ").strip()
        secret_key = input("Secret Key: ").strip()
        passphrase = input("Passphrase: ").strip()
        
        print(f"\n请选择交易环境:")
        print(f"1. 🟢 模拟盘 (推荐，用于测试)")
        print(f"2. 🔴 实盘 (真实资金，谨慎操作)")
        
        while True:
            choice = input("请选择 (1 或 2): ").strip()
            if choice in ["1", "2"]:
                break
            print("请输入 1 或 2")
        
        flag = "1" if choice == "1" else "0"
        env_name = "模拟盘" if flag == "1" else "实盘"
        
        # 生成配置文件
        config_content = f'''#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
OKX API 配置文件
================

此文件由设置向导自动生成
"""

import os

# API配置
API_CONFIG = {{
    'api_key': "{api_key}",
    'secret_key': "{secret_key}",
    'passphrase': "{passphrase}",
    'flag': "{flag}",
    'use_server_time': False,
    'timeout': 30,
}}

# 环境变量配置（优先级更高）
ENV_CONFIG = {{
    'api_key': os.getenv('OKX_API_KEY'),
    'secret_key': os.getenv('OKX_SECRET_KEY'),
    'passphrase': os.getenv('OKX_PASSPHRASE'),
    'flag': os.getenv('OKX_FLAG', '{flag}'),
}}

def get_api_config():
    """获取API配置"""
    config = API_CONFIG.copy()
    for key, value in ENV_CONFIG.items():
        if value is not None:
            config[key] = value
    return config

def validate_config(config):
    """验证配置"""
    required_keys = ['api_key', 'secret_key', 'passphrase']
    for key in required_keys:
        if not config.get(key):
            return False, f"缺少必需的配置项: {{key}}"
        if config[key] in ["your_api_key_here", "your_secret_key_here", "your_passphrase_here"]:
            return False, f"请配置真实的{{key}}值"
    return True, "配置验证通过"

def print_config_info(config):
    """打印配置信息"""
    print("🔧 API配置信息:")
    print(f"   API Key: {{config['api_key'][:8]}}***{{config['api_key'][-4:]}}")
    print(f"   Secret Key: {{config['secret_key'][:8]}}***{{config['secret_key'][-4:]}}")
    print(f"   Passphrase: {{'*' * len(config['passphrase'])}}")
    print(f"   环境: {{'🔴 实盘' if config['flag'] == '0' else '🟢 模拟盘'}}")
    print(f"   超时: {{config.get('timeout', 30)}}秒")

def get_account_api():
    """获取账户API"""
    import okx.Account as Account
    config = get_api_config()
    return Account.AccountAPI(
        config['api_key'], config['secret_key'], config['passphrase'],
        config.get('use_server_time', False), config['flag']
    )

def get_trade_api():
    """获取交易API"""
    import okx.Trade as Trade
    config = get_api_config()
    return Trade.TradeAPI(
        config['api_key'], config['secret_key'], config['passphrase'],
        config.get('use_server_time', False), config['flag']
    )

def get_market_api():
    """获取市场数据API"""
    import okx.MarketData as MarketData
    config = get_api_config()
    return MarketData.MarketAPI(flag=config['flag'])
'''
        
        # 保存配置文件到项目根目录
        config_file = Path(__file__).parent.parent / "api_config.py"
        with open(config_file, 'w', encoding='utf-8') as f:
            f.write(config_content)
        
        print(f"\n✅ 配置完成！")
        print(f"📄 配置文件: {config_file}")
        print(f"🎯 交易环境: {env_name}")
        print(f"🔐 API Key: {api_key[:8]}***{api_key[-4:]}")
        
        print(f"\n🚀 下一步:")
        print(f"运行任意demo文件测试，例如:")
        print(f"py okx/demo/demo01_账户余额查询.py")
        
        # 询问是否立即测试
        test_now = input(f"\n是否立即测试API连接？(y/n): ").strip().lower()
        if test_now in ['y', 'yes', '是']:
            print(f"\n🔍 测试API连接...")
            try:
                # 导入配置并测试
                import sys
                sys.path.insert(0, str(Path(__file__).parent.parent))
                from api_config import get_account_api
                
                account_api = get_account_api()
                result = account_api.get_account_balance()
                
                if result.get('code') == '0':
                    print(f"✅ API连接成功！")
                    total_eq = result['data'][0]['totalEq']
                    print(f"📊 账户总权益: ${total_eq}")
                else:
                    print(f"❌ API连接失败: {result.get('msg', '未知错误')}")
                    print(f"💡 请检查API密钥是否正确")
                    
            except Exception as e:
                print(f"❌ 测试异常: {e}")
                print(f"💡 请检查网络连接和API配置")
        
    except KeyboardInterrupt:
        print(f"\n❌ 设置已取消")
    except Exception as e:
        print(f"\n❌ 设置失败: {e}")

if __name__ == "__main__":
    main()