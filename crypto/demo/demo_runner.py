#!/usr/bin/env python3
# -*- coding: utf-8 -*-

#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
OKX API Demo 批量运行工具
=======================

功能概述：
----------
这是一个用于批量运行和管理OKX API Demo示例的工具。它提供了一个交互式界面，
允许用户以不同模式运行demo文件，并生成详细的运行报告。

主要功能：
----------
1. API配置管理
   - 支持环境变量方式配置API密钥
   - 自动检测API配置状态
   - 提供配置缺失提醒

2. Demo文件管理
   - 自动扫描demo目录
   - 过滤并排序demo文件
   - 支持批量和选择性运行

3. 运行模式
   - 模式1：运行所有demo
   - 模式2：选择性运行指定demo
   - 模式3：仅检查demo语法

4. 执行控制
   - 支持超时控制（默认60秒）
   - 可随时中断运行（Ctrl+C）
   - 自动处理运行异常
   - 添加运行间隔避免API限制

5. 结果分析
   - 实时显示运行状态
   - 统计成功/失败数量
   - 计算成功率
   - 捕获并显示错误信息

6. 报告生成
   - 生成详细的运行报告
   - 包含总体统计信息
   - 记录每个demo的运行结果
   - 提供失败原因分析
   - 自动保存到文件

使用说明：
----------
1. 运行前准备
   - 确保配置了API密钥
   - 建议在模拟盘环境测试
   - 检查网络连接状态

2. 运行方式
   - 直接运行：python demo_runner.py
   - 按提示选择运行模式
   - 根据需要选择要运行的demo

3. 注意事项
   - 每个demo限时60秒
   - 运行之间有2秒间隔
   - 失败后会保留错误信息
   - 报告保存在同目录下

作者：OKX API Demo
版本：v1.0
创建时间：2024-08-26
"""

import os
import sys
import time
import subprocess
from pathlib import Path

def check_api_config():
    """检查API配置"""
    print("\n🔍 检查API配置...")
    
    # 检查环境变量
    env_keys = ['OKX_API_KEY', 'OKX_SECRET_KEY', 'OKX_PASSPHRASE']
    has_env_config = all(os.getenv(key) for key in env_keys)
    
    if has_env_config:
        print("✅ 检测到环境变量配置")
        return True
    else:
        print("⚠️  未检测到环境变量配置")
        print("   请确保在demo文件中配置了API密钥")
        return False

def list_demos():
    """列出所有demo文件"""
    demo_dir = Path(__file__).parent
    demo_files = []
    
    for file in demo_dir.glob("demo*.py"):
        if file.name not in ["demo_runner.py", "__init__.py"]:
            demo_files.append(file)
    
    demo_files.sort()
    return demo_files

def run_single_demo(demo_file, timeout=60):
    """运行单个demo"""
    try:
        result = subprocess.run([
            sys.executable, str(demo_file)
        ], capture_output=True, text=True, timeout=timeout)
        
        return {
            'success': result.returncode == 0,
            'output': result.stdout,
            'error': result.stderr,
            'returncode': result.returncode
        }
        
    except subprocess.TimeoutExpired:
        return {
            'success': False,
            'output': '',
            'error': 'Demo运行超时',
            'returncode': -1
        }
    except Exception as e:
        return {
            'success': False,
            'output': '',
            'error': str(e),
            'returncode': -2
        }

def main():
    print("=" * 60)
    print("🚀 OKX API Demo 批量运行工具")
    print("=" * 60)
    
    # 检查API配置
    check_api_config()
    
    # 获取所有demo文件
    demo_files = list_demos()
    
    if not demo_files:
        print("❌ 未找到任何demo文件")
        return
    
    print(f"\n📋 发现 {len(demo_files)} 个demo文件:")
    for i, file in enumerate(demo_files, 1):
        print(f"   {i:2d}. {file.name}")
    
    print("\n⚠️  注意事项:")
    print("   - 请确保已配置API密钥")
    print("   - 建议在模拟盘环境运行")
    print("   - 每个demo超时时间为60秒")
    print("   - 可以随时按Ctrl+C中断")
    
    # 运行模式选择
    print(f"\n🎯 运行模式选择:")
    print(f"   1. 运行所有demo")
    print(f"   2. 选择性运行demo")
    print(f"   3. 仅检查demo语法")
    
    try:
        mode = input(f"\n请选择运行模式 (1-3): ").strip()
        
        if mode == "1":
            run_all_demos(demo_files)
        elif mode == "2":
            run_selected_demos(demo_files)
        elif mode == "3":
            check_syntax_only(demo_files)
        else:
            print("无效选择，退出")
            
    except KeyboardInterrupt:
        print("\n运行已取消")
        return

def run_all_demos(demo_files):
    """运行所有demo"""
    print(f"\n🚀 开始运行所有demo...")
    
    success_count = 0
    total_count = len(demo_files)
    results = []
    
    for i, demo_file in enumerate(demo_files, 1):
        print(f"\n{'='*60}")
        print(f"🏃 运行 Demo {i}/{total_count}: {demo_file.name}")
        print(f"{'='*60}")
        
        result = run_single_demo(demo_file)
        results.append((demo_file.name, result))
        
        if result['success']:
            print("✅ 运行成功")
            success_count += 1
        else:
            print("❌ 运行失败")
            if result['error']:
                print(f"错误信息: {result['error'][:200]}...")
        
        # 短暂停顿，避免API限制
        if i < total_count:
            time.sleep(2)
    
    # 生成报告
    generate_report(results, success_count, total_count)

def run_selected_demos(demo_files):
    """选择性运行demo"""
    print(f"\n请选择要运行的demo (多选请用逗号分隔，如: 1,3,5):")
    
    try:
        choices = input("选择: ").strip()
        if not choices:
            print("未选择任何demo")
            return
        
        indices = []
        for choice in choices.split(','):
            try:
                idx = int(choice.strip()) - 1
                if 0 <= idx < len(demo_files):
                    indices.append(idx)
                else:
                    print(f"无效选择: {choice}")
            except ValueError:
                print(f"无效格式: {choice}")
        
        if not indices:
            print("没有有效的选择")
            return
        
        selected_demos = [demo_files[i] for i in indices]
        print(f"\n将运行 {len(selected_demos)} 个demo:")
        for demo in selected_demos:
            print(f"   - {demo.name}")
        1
        # 运行选中的demo
        results = []
        success_count = 0
        
        for i, demo_file in enumerate(selected_demos, 1):
            print(f"\n{'='*60}")
            print(f"🏃 运行 Demo {i}/{len(selected_demos)}: {demo_file.name}")
            print(f"{'='*60}")
            
            result = run_single_demo(demo_file)
            results.append((demo_file.name, result))
            
            if result['success']:
                print("✅ 运行成功")
                success_count += 1
            else:
                print("❌ 运行失败")
                if result['error']:
                    print(f"错误信息: {result['error'][:200]}...")
            
            if i < len(selected_demos):
                time.sleep(2)
        
        generate_report(results, success_count, len(selected_demos))
        
    except KeyboardInterrupt:
        print("\n运行已取消")

def check_syntax_only(demo_files):
    """仅检查语法"""
    print(f"\n🔍 检查所有demo文件语法...")
    
    success_count = 0
    
    for demo_file in demo_files:
        try:
            result = subprocess.run([
                sys.executable, "-m", "py_compile", str(demo_file)
            ], capture_output=True, text=True)
            
            if result.returncode == 0:
                print(f"✅ {demo_file.name} - 语法正确")
                success_count += 1
            else:
                print(f"❌ {demo_file.name} - 语法错误")
                if result.stderr:
                    print(f"   错误: {result.stderr}")
                    
        except Exception as e:
            print(f"❌ {demo_file.name} - 检查异常: {e}")
    
    print(f"\n📊 语法检查完成:")
    print(f"   总计: {len(demo_files)} 个文件")
    print(f"   正确: {success_count} 个")
    print(f"   错误: {len(demo_files) - success_count} 个")

def generate_report(results, success_count, total_count):
    """生成运行报告"""
    print(f"\n{'='*60}")
    print("📊 运行报告")
    print(f"{'='*60}")
    
    print(f"📈 总体统计:")
    print(f"   总计: {total_count} 个demo")
    print(f"   成功: {success_count} 个")
    print(f"   失败: {total_count - success_count} 个")
    print(f"   成功率: {success_count/total_count*100:.1f}%")
    
    # 详细结果
    print(f"\n📋 详细结果:")
    for demo_name, result in results:
        status = "✅" if result['success'] else "❌"
        print(f"   {status} {demo_name}")
        if not result['success'] and result['error']:
            print(f"      错误: {result['error'][:100]}...")
    
    # 失败分析
    failed_demos = [name for name, result in results if not result['success']]
    if failed_demos:
        print(f"\n🔍 失败分析:")
        print(f"   失败的demo可能原因:")
        print(f"   - API密钥未配置或配置错误")
        print(f"   - 网络连接问题")
        print(f"   - 账户余额不足")
        print(f"   - API权限限制")
        
        print(f"\n💡 建议:")
        print(f"   - 检查API密钥配置")
        print(f"   - 确保网络连接正常")
        print(f"   - 在模拟盘环境测试")
        print(f"   - 查看具体错误信息")
    
    # 保存报告到文件
    try:
        report_file = Path(__file__).parent / "demo_report.txt"
        with open(report_file, 'w', encoding='utf-8') as f:
            f.write(f"OKX API Demo 运行报告\n")
            f.write(f"运行时间: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"成功率: {success_count}/{total_count} ({success_count/total_count*100:.1f}%)\n\n")
            
            for demo_name, result in results:
                status = "SUCCESS" if result['success'] else "FAILED"
                f.write(f"{demo_name}: {status}\n")
                if not result['success']:
                    f.write(f"  Error: {result['error']}\n")
            
        print(f"\n📄 报告已保存到: {report_file}")
        
    except Exception as e:
        print(f"\n⚠️  保存报告失败: {e}")

if __name__ == "__main__":
    main() 