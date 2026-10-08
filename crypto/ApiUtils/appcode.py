# -*- coding: utf-8 -*-
"""
ApiUtils 网关凭据解析（futureStockTrade 合并迁移 Phase 5 收口）
==========================================================

本目录下的三份第三方行情工具类（``alicloud_market`` / ``cme_futures_utils`` /
``jumdata_kline_utils``）原先把阿里云 API 市场的 AppCode 明文写在源码常量里，
``alicloud_market.py`` 甚至在注释里连 AppKey / AppSecret 一起写了。仓库是公开的，
这类明文一旦提交就等于把账号级凭据公开，且改回来也删不掉历史。

现在的口径与 ``crypto/stocks/gateway_config.py``、``crypto/kline_training/settings.py``
保持一致 —— **代码里不留任何明文凭据**，按优先级解析：

1. 环境变量（部署时注入，优先级最高）
2. 外置凭据文件 ``data/jumei_appcode.txt``（已在 .gitignore 内，只存本机）
3. 空串 → 由调用方在实例化时明确抛错，绝不静默带着空 Authorization 去请求

AppCode 解析失败时不影响模块导入，因此这三份工具类仍然可以安全地被
「未配置凭据」的环境 import。
"""
import os

# 本文件位于 <项目根>/crypto/ApiUtils/appcode.py，向上三级即项目根。
# 脚本直跑时（cwd 在别处、sys.path 里只有本目录）宿主 data_paths 未必能 import，
# 因此留一条按 __file__ 反推的兜底路径，避免「换个目录跑就取不到凭据」。
_PKG_ROOT = os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))

# 外置凭据文件名（与 gateway_config.py 用的是同一份，一个云市场账号一份 AppCode）
DATA_FILE = "jumei_appcode.txt"


def _fallback_data_file(filename: str) -> str:
    """按宿主 data_paths 的口径自己拼一次路径（环境变量优先，缺省项目内 data/）。"""
    env_dir = (os.environ.get('CRYPTO_PLAN_DATA_DIR') or '').strip()
    data_dir = env_dir or os.path.join(_PKG_ROOT, 'data')
    return os.path.join(data_dir, filename)


def _from_data_file(filename: str = DATA_FILE) -> str:
    """从外置数据目录读一行凭据；目录不可用 / 文件不存在 / 读失败都返回空串。"""
    path = ''
    try:
        try:
            from crypto.data_paths import resolve_data_file
        except Exception:
            from data_paths import resolve_data_file
        path = resolve_data_file(filename)
    except Exception:
        path = _fallback_data_file(filename)      # 脚本直跑的兜底
    try:
        if not path or not os.path.isfile(path):
            path = _fallback_data_file(filename)
            if not os.path.isfile(path):
                return ""
        with open(path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    return line
    except Exception:
        return ""
    return ""


def resolve_appcode(*env_names: str, data_file: str = DATA_FILE) -> str:
    """按「环境变量 → 外凭据文件 → 备用环境变量」的顺序取 AppCode，取不到返回空串。

    Args:
        env_names: 依次尝试的环境变量名，第一个非空的胜出。
        data_file: 外置凭据文件名，默认与股票/CME 网关共用同一份。
    """
    for name in env_names:
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    value = _from_data_file(data_file)
    if value:
        return value
    return ""


def require_appcode(*env_names: str, data_file: str = DATA_FILE) -> str:
    """同 :func:`resolve_appcode`，取不到时直接抛错，避免带着空凭据去打接口。"""
    value = resolve_appcode(*env_names, data_file=data_file)
    if not value:
        raise ValueError(
            "未配置阿里云 API 市场 AppCode：请设置环境变量 %s，"
            "或把 AppCode 写入外置凭据文件 %s（该文件已被 .gitignore 排除）。"
            % ("/".join(env_names or ("JUMEI_APPCODE",)), data_file)
        )
    return value
