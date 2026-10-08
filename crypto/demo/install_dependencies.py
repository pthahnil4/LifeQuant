#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
OKX Demo 依赖自动安装脚本
========================

一次性解决所有依赖问题，再也不用每次手动安装！

功能说明:
--------
1. 自动检测所有必需的Python包是否已安装
2. 提供交互式安装界面，让用户选择是否安装缺失的包
3. 支持快速修复模式（--quick参数），只安装核心依赖
4. 安装完成后自动验证所有包的可用性

使用方法:
--------
1. 普通模式: python install_dependencies.py
2. 快速修复: python install_dependencies.py --quick

依赖包分类:
--------
- OKX API基础依赖: httpx, requests等
- 数据分析: pandas, numpy
- 技术指标: ta
- 可视化: matplotlib, seaborn等
- 量化交易: backtrader
- 数据库: influxdb-client

注意事项:
--------
1. 需要Python 3.8+
2. 建议在虚拟环境中运行
3. 部分包可能需要系统级依赖
4. 安装过程中需要网络连接

作者：OKX API Demo
创建时间：2024-06-19
版本：v1.0
"""

import subprocess
import sys
import importlib
from pathlib import Path

# 所有可能需要的依赖包
REQUIRED_PACKAGES = {
    # OKX API基础依赖
    'importlib-metadata': 'importlib_metadata',
    'httpx[http2]': 'httpx',
    'keyring': 'keyring',
    'loguru': 'loguru',
    'requests': 'requests',
    'Twisted': 'twisted',
    'pyOpenSSL': 'OpenSSL',
    
    # 数据分析
    'pandas': 'pandas',
    'numpy': 'numpy',
    
    # 技术指标
    'ta': 'ta',
    
    # 可视化
    'matplotlib': 'matplotlib',
    'seaborn': 'seaborn',
    'plotly': 'plotly',
    
    # 金融数据
    'yfinance': 'yfinance',
    
    # 科学计算
    'scipy': 'scipy',
    
    # 机器学习
    'scikit-learn': 'sklearn',
    
    # 量化交易
    'backtrader': 'backtrader',
    
    # 图表库
    'pyecharts': 'pyecharts',
    
    # 数据库
    'influxdb-client': 'influxdb_client',
    
    # 网页解析
    'beautifulsoup4': 'bs4',
}

def check_package(package_name, import_name):
    """检查包是否已安装"""
    try:
        importlib.import_module(import_name)
        return True
    except ImportError:
        return False

def install_package(package_name):
    """安装单个包"""
    try:
        print(f"📦 正在安装 {package_name}...")
        subprocess.check_call([
            sys.executable, "-m", "pip", "install", package_name
        ])
        print(f"✅ {package_name} 安装成功")
        return True
    except subprocess.CalledProcessError as e:
        print(f"❌ {package_name} 安装失败: {e}")
        return False

def main():
    """主函数"""
    print("=" * 60)
    print("🔧 OKX Demo 依赖自动安装脚本")
    print("=" * 60)
    
    print("🔍 检查依赖包状态...")
    
    missing_packages = []
    installed_packages = []
    
    for package_name, import_name in REQUIRED_PACKAGES.items():
        if check_package(package_name, import_name):
            installed_packages.append(package_name)
            print(f"✅ {package_name}")
        else:
            missing_packages.append(package_name)
            print(f"❌ {package_name}")
    
    print(f"\n📊 检查结果:")
    print(f"   已安装: {len(installed_packages)} 个")
    print(f"   缺失: {len(missing_packages)} 个")
    
    if not missing_packages:
        print(f"\n🎉 所有依赖都已安装，可以正常使用所有demo！")
        return
    
    print(f"\n📋 需要安装的包:")
    for package in missing_packages:
        print(f"   - {package}")
    
    # 询问是否安装
    choice = input(f"\n是否立即安装缺失的依赖？(y/n): ").strip().lower()
    
    if choice not in ['y', 'yes', '是']:
        print("❌ 安装已取消")
        return
    
    print(f"\n🚀 开始安装缺失的依赖...")
    
    success_count = 0
    failed_packages = []
    
    for package_name in missing_packages:
        if install_package(package_name):
            success_count += 1
        else:
            failed_packages.append(package_name)
    
    print(f"\n📊 安装完成:")
    print(f"   成功: {success_count} 个")
    print(f"   失败: {len(failed_packages)} 个")
    
    if failed_packages:
        print(f"\n❌ 安装失败的包:")
        for package in failed_packages:
            print(f"   - {package}")
        print(f"\n💡 解决方案:")
        print(f"   1. 检查网络连接")
        print(f"   2. 尝试手动安装: pip install {' '.join(failed_packages)}")
        print(f"   3. 部分包可能需要额外的系统依赖")
    else:
        print(f"\n🎉 所有依赖安装成功！")
        print(f"💡 现在您可以运行任何demo文件，不会再遇到模块导入错误！")
    
    # 验证安装
    print(f"\n🔍 验证安装结果...")
    
    all_good = True
    for package_name, import_name in REQUIRED_PACKAGES.items():
        if check_package(package_name, import_name):
            print(f"✅ {package_name}")
        else:
            print(f"❌ {package_name} 验证失败")
            all_good = False
    
    if all_good:
        print(f"\n🎊 完美！所有依赖都可以正常导入！")
    else:
        print(f"\n⚠️  部分依赖可能仍有问题，但大部分功能应该正常")

def quick_fix():
    """快速修复常见依赖问题"""
    print("🚀 快速修复模式...")
    
    # 最常用的核心依赖
    core_packages = [
        'pandas',
        'numpy', 
        'ta',
        'matplotlib',
        'requests'
    ]
    
    for package in core_packages:
        install_package(package)
    
    print("✅ 快速修复完成！")

if __name__ == "__main__":
    # 检查是否有命令行参数
    if len(sys.argv) > 1 and sys.argv[1] == '--quick':
        quick_fix()
    else:
        main()