# [迁移] 本文件由 futureStockTrade 合并迁移 Phase 4 自动搬运（2026-10-08）。
# 原名: kline/database/__init__.py  →  现位置: crypto/kline_training/td_cache/__init__.py
# 导入改为 crypto.* 显式包路径，sys.path 拼接兜底移除，凭据默认值已清除。
"""数据库操作模块（TDengine 历史K线缓存连接）

【合并迁移 Phase 4 说明】本套 TDengine 缓存是原项目的闲置能力（宿主已有自己的
1 分钟 K 线链路：crypto/kline_fetcher + /kline-live），迁进来只为保留可选工具，
默认关闭：需要 CRYPTO_TDENGINE_CACHE=1 且同时提供 REST 地址与凭据才会启用。

凭据口径调整为「只从环境变量读取，无任何代码内默认值」：原 config.py 里写死的
公网 IP + root/弱口令属于凭据，一律不进仓库。未配置时本模块仍可正常导入
（连接函数会明确报「未配置」），不影响主应用启动。
"""
from crypto.kline_training import settings as config

TDENGINE_REST_URL = config.TDENGINE_REST_URL
USER = config.TDENGINE_USER
PASSWORD = config.TDENGINE_PASSWORD

try:
    from taosrest import connect
except Exception:      # taospy 未安装时保持模块可导入（闲置能力不该拖垮启动）
    connect = None


def get_connection():
    """获取 TDengine 数据库连接；未配置凭据或未装驱动时抛 RuntimeError。"""
    if connect is None:
        raise RuntimeError('TDengine 缓存未启用：缺少 taosrest 依赖')
    if not TDENGINE_REST_URL or not USER:
        raise RuntimeError('TDengine 缓存未配置：请设置环境变量 TDENGINE_REST_URL / '
                           'TDENGINE_USER / TDENGINE_PASSWORD')
    return connect(url=TDENGINE_REST_URL, user=USER, password=PASSWORD)
