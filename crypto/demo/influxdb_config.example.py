#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
InfluxDB 配置模板
================

真实文件 crypto/demo/influxdb_config.py 不入库（内含长期有效的 API token 与服务器
公网 IP）。新环境搭建：

    copy influxdb_config.example.py influxdb_config.py

然后填 URL / TOKEN / ORG。demo51~demo65 共 11 个脚本 `from influxdb_config import
InfluxDBConfig`，文件名不能改。

作者：AI Assistant
创建时间：2025年1月
"""

class InfluxDBConfig:
    """InfluxDB配置类"""
    
    def __init__(self):
        # InfluxDB 2.x 连接配置
        self.URL = "http://127.0.0.1:8086"       # InfluxDB 服务地址
        self.TOKEN = "your_influx_token_here"     # API token（2.x 为 bearer token）
        self.ORG = "your_org_name"                # 组织名
        
        # 默认桶列表
        self.DEFAULT_BUCKETS = ["your_bucket", "_tasks", "_monitoring"]
        
        # 超时设置
        self.TIMEOUT = 30000  # 30秒
    
    def get_client_config(self):
        """获取客户端配置字典"""
        return {
            "url": self.URL,
            "token": self.TOKEN,
            "org": self.ORG,
            "timeout": self.TIMEOUT
        }
    
    def get_bucket(self, index=0):
        """获取指定索引的桶名"""
        if 0 <= index < len(self.DEFAULT_BUCKETS):
            return self.DEFAULT_BUCKETS[index]
        return self.DEFAULT_BUCKETS[0]  # 默认返回第一个桶
