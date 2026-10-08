#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
WebSocket配置文件
================

提供WebSocket连接所需的配置信息
"""

def get_business_url():
    """获取WebSocket业务频道地址"""
    return "wss://ws.okx.com:8443/ws/v5/business"

def get_public_url():
    """获取WebSocket公共频道地址"""
    return "wss://ws.okx.com:8443/ws/v5/public"

def get_connection_config():
    """获取连接配置"""
    return {
        "ping_interval": 20,
        "ping_timeout": 10,
        "close_timeout": 10,
    }

def print_ws_config():
    """打印WebSocket配置"""
    print("WebSocket配置:")
    print(f"  业务频道: {get_business_url()}")
    print(f"  公共频道: {get_public_url()}")
    print(f"  连接配置: {get_connection_config()}")