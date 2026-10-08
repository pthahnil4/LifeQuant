#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
统一数据目录解析（共享模块）
==================================
所有本地 JSON 兜底文件（db_url.txt、缓存双写文件）统一存放在项目内 data/ 目录。
数据权威源为 MySQL，本目录文件仅作 DB 不可用时的兜底。

目录优先级：
  1. 环境变量 CRYPTO_PLAN_DATA_DIR（显式指定，最高优先，宝塔面板可配置）
  2. 项目内 data/ 目录（默认，随项目一起部署）
"""

import os
import shutil
import logging

logger = logging.getLogger(__name__)

# 项目内 crypto/ 目录（旧数据文件所在地，作为回退与迁移源）
_CRYPTO_DIR = os.path.dirname(os.path.abspath(__file__))
# 项目根目录下的 data/ 目录（默认数据目录）
_DEFAULT_DATA_DIR = os.path.join(os.path.dirname(_CRYPTO_DIR), 'data')


def resolve_data_dir() -> str:
    """解析数据目录：环境变量 CRYPTO_PLAN_DATA_DIR 优先，缺省项目内 data/ 目录"""
    env_dir = os.environ.get('CRYPTO_PLAN_DATA_DIR', '').strip()
    if env_dir:
        return env_dir
    return _DEFAULT_DATA_DIR


def resolve_data_file(filename: str, legacy_path: str = None) -> str:
    """解析数据文件的最终读写路径，并在首次启用外置时自动迁移项目内旧文件。

    Args:
        filename: 数据文件名（如 'scheduler_state.json'）
        legacy_path: 项目内旧文件路径；不传时默认取 crypto/ 下同名文件

    Returns:
        外置目录可用 → 外置路径；否则 → 项目内旧路径（保持旧行为）
    """
    legacy_path = legacy_path or os.path.join(_CRYPTO_DIR, filename)
    data_dir = resolve_data_dir()
    if not data_dir:
        return legacy_path
    try:
        os.makedirs(data_dir, exist_ok=True)
    except OSError as e:
        logger.warning(f"[DataPaths] 外置数据目录创建失败({data_dir}): {e}，回退项目内路径")
        return legacy_path
    target = os.path.join(data_dir, filename)
    if not os.path.exists(target) and os.path.isfile(legacy_path):
        try:
            shutil.copyfile(legacy_path, target)
            logger.info(f"[DataPaths] 已迁移数据文件: {legacy_path} -> {target}")
        except OSError as e:
            logger.warning(f"[DataPaths] 数据文件迁移失败({filename}): {e}")
    return target
