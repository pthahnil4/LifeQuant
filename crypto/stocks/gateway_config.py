# -*- coding: utf-8 -*-
"""
行情网关凭据配置（聚美智数 / 阿里云 API 市场）
=============================================

由 futureStockTrade 合并迁移 Phase 3 新建，用来替代原项目的根级 ``config.py``。

为什么新建而不是搬 ``config.py``：原 ``config.py`` 里混着 SECRET_KEY 默认值、
SMTP 授权码明文、TDengine 口令与 MySQL 连接串等**敏感项**。股票/CME 这两个行情
客户端实际只读下面 6 个 ``JUMEI_*`` 名字，因此本模块只提供这 6 个名字，其余敏感
项一律不进 cryptoTrade（密钥收口详见 doc/futureStockTrade合并迁移方案与清单.md Phase 5）。

凭据解析优先级（与宿主 web_auth / db_url.txt 的外置目录约定一致）：

1. 环境变量（部署时注入，优先级最高）
2. 外置数据文件 ``data/jumei_appcode.txt`` / ``data/jumei_cme_appcode.txt``
   （只存 AppCode 一行纯文本，已在 .gitignore 内，不随源码发布）
3. 空字符串 → 调用方 ``JumeiAPIError`` 明确报"未配置凭据"，绝不静默用别人的密钥

本模块被 ``crypto/stocks/jumei_api.py`` 与 ``crypto/futures/cme_api.py`` 以
``from crypto.stocks import gateway_config as config`` 的方式读取，保持这两份客户端
"try: 读 config → except: 回退环境变量" 的原有结构不变（迁移只做导入改写，不改逻辑）。

注意：这两个网关目前**未被任何页面接线**（原项目同样无人 import，属可用但闲置的能力），
迁进来是为了保留 CME 期货 / A 股第三方行情入口，未来接线时直接调用即可。
"""
import os

# ---------------------------------------------------------------------
#  网关默认地址（非密钥，可安全入库）
# ---------------------------------------------------------------------
JUMEI_API_HOST = (os.environ.get('JUMEI_API_HOST')
                  or 'https://jmgphqcxhs.market.alicloudapi.com').strip()
JUMEI_CME_API_HOST = (os.environ.get('JUMEI_CME_API_HOST')
                      or 'https://jmcmeqh.market.alicloudapi.com').strip()


def _from_data_file(filename: str) -> str:
    """从外置数据目录读取一行凭据；不可用/不存在/读失败都返回空串。"""
    try:
        from crypto.data_paths import resolve_data_file
    except Exception:
        try:
            from data_paths import resolve_data_file
        except Exception:
            return ''
    try:
        path = resolve_data_file(filename)
        if not path or not os.path.isfile(path):
            return ''
        with open(path, 'r', encoding='utf-8') as f:
            return f.read().strip()
    except Exception:
        return ''


def _resolve_appcode(env_name: str, fallback_env: str, data_file: str) -> str:
    """AppCode 解析：环境变量 → 外置凭据文件 → 备用环境变量 → 空串。"""
    value = (os.environ.get(env_name) or '').strip()
    if value:
        return value
    value = _from_data_file(data_file)
    if value:
        return value
    return (os.environ.get(fallback_env) or '').strip()


# ---------------------------------------------------------------------
#  AppCode 鉴权凭据（敏感，只走环境变量或外置文件，代码内不留明文）
# ---------------------------------------------------------------------
JUMEI_APPCODE = _resolve_appcode('JUMEI_APPCODE', '', 'jumei_appcode.txt')
# CME 与股票网关属同一云市场账号，未单独配置时复用股票侧 AppCode（与原 config 行为一致）
JUMEI_CME_APPCODE = _resolve_appcode('JUMEI_CME_APPCODE', 'JUMEI_APPCODE',
                                     'jumei_cme_appcode.txt') or JUMEI_APPCODE

# ---------------------------------------------------------------------
#  超时（秒）
# ---------------------------------------------------------------------
JUMEI_TIMEOUT = int(os.environ.get('JUMEI_TIMEOUT') or 15)
JUMEI_CME_TIMEOUT = int(os.environ.get('JUMEI_CME_TIMEOUT')
                        or os.environ.get('JUMEI_TIMEOUT') or 15)


def appcode_state() -> dict:
    """给系统状态页/诊断用：只报告"是否已配置"，绝不返回凭据本身。"""
    return {
        'stock_host': JUMEI_API_HOST,
        'cme_host': JUMEI_CME_API_HOST,
        'stock_appcode_configured': bool(JUMEI_APPCODE),
        'cme_appcode_configured': bool(JUMEI_CME_APPCODE),
        'timeout': JUMEI_TIMEOUT,
        'cme_timeout': JUMEI_CME_TIMEOUT,
    }
