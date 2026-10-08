#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
批量更新Demo文件配置
===================

将所有demo文件修改为使用统一的api_config.py配置文件

功能说明:
--------
1. 自动扫描demo目录下的所有demo*.py文件
2. 检查每个文件是否已使用统一配置
3. 替换旧的API配置区域为统一配置
4. 更新配置信息的打印格式

更新内容:
--------
1. 导入统一配置模块
2. 添加配置验证功能
3. 统一API参数命名
4. 美化配置信息输出

使用方法:
--------
直接运行: python update_all_demos.py

工作流程:
--------
1. 扫描demo文件
2. 检查配置使用情况
3. 替换配置区域
4. 更新打印格式
5. 输出更新报告

注意事项:
--------
1. 会修改文件内容，建议先备份
2. 只处理demo*.py格式的文件
3. 跳过已使用统一配置的文件
4. 需要api_config.py文件存在

作者：OKX API Demo
创建时间：2024-06-19
版本：v1.0
"""

import os
import re
from pathlib import Path

def update_demo_file(file_path):
    """更新单个demo文件"""
    print(f"🔄 更新文件: {file_path.name}")
    
    try:
        # 读取文件内容
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # 检查是否已经使用了统一配置
        if 'from api_config import' in content:
            print(f"   ✅ 已使用统一配置，跳过")
            return True
        
        # 查找API配置区域并替换
        new_config_section = '''# =============================================================================
# API 配置区域 - 使用统一配置文件
# =============================================================================

# 从配置文件导入API配置
from api_config import get_api_config, validate_config, print_config_info

# 获取API配置
config = get_api_config()

# 验证配置
is_valid, message = validate_config(config)
if not is_valid:
    print(f"❌ API配置错误: {message}")
    print("💡 请检查 api_config.py 文件中的配置")
    exit(1)

# 提取配置参数
apikey = config['api_key']
secretkey = config['secret_key']
passphrase = config['passphrase']
flag = config['flag']'''

        # 匹配API配置区域的正则表达式
        config_pattern = r'# =============================================================================\s*\n# API 配置区域.*?\nflag = "[01]"'
        
        # 替换配置区域
        if re.search(config_pattern, content, re.DOTALL):
            content = re.sub(config_pattern, new_config_section, content, flags=re.DOTALL)
            
            # 替换打印配置信息的部分
            print_pattern = r'print\(f"📊 当前模式: \{\'🔴 实盘交易\' if flag == \'0\' else \'🟢 模拟盘交易\'\}"\)'
            if re.search(print_pattern, content):
                content = re.sub(print_pattern, 'print_config_info(config)', content)
            
            # 写回文件
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(content)
            
            print(f"   ✅ 更新成功")
            return True
        else:
            print(f"   ⚠️  未找到标准配置区域，手动检查")
            return False
            
    except Exception as e:
        print(f"   ❌ 更新失败: {e}")
        return False

def main():
    """主函数"""
    print("=" * 60)
    print("🔧 批量更新Demo文件配置")
    print("=" * 60)
    
    # 获取demo目录
    demo_dir = Path(__file__).parent
    
    # 查找所有demo文件
    demo_files = []
    for file in demo_dir.glob("demo*.py"):
        if file.name not in ["demo_runner.py", "update_all_demos.py"]:
            demo_files.append(file)
    
    demo_files.sort()
    
    print(f"📋 发现 {len(demo_files)} 个demo文件")
    
    # 批量更新
    success_count = 0
    
    for demo_file in demo_files:
        if update_demo_file(demo_file):
            success_count += 1
    
    print(f"\n📊 更新完成:")
    print(f"   总计: {len(demo_files)} 个文件")
    print(f"   成功: {success_count} 个")
    print(f"   失败: {len(demo_files) - success_count} 个")
    
    if success_count == len(demo_files):
        print(f"\n🎉 所有文件更新成功！")
        print(f"💡 现在所有demo都使用统一的 api_config.py 配置文件")
        print(f"   只需要在 api_config.py 中修改一次API密钥即可")
    else:
        print(f"\n⚠️  部分文件更新失败，请手动检查")

if __name__ == "__main__":
    main()