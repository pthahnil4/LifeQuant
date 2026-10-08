# -*- coding: utf-8 -*-
"""
K 线训练模块运行配置（合并迁移 Phase 4 新建）
============================================

由 futureStockTrade 合并迁移 Phase 4 新建，用来替代原项目的根级 ``config.py``
在 K 线训练链路上的那一半。

为什么新建而不是搬 ``config.py``
--------------------------------
原 ``config.py`` 把「K 线训练真正要读的 8 个名字」和一批**敏感默认值**写在同一
个文件里，照搬等于把凭据钉进版本库：

- ``SECRET_KEY`` 的兜底值是一个写死的短字符串 —— 用它签发的 Cookie 任何人都能
  伪造，等于「登录即裸奔」。
- ``SMTP_CONFIG['password']`` 的兜底值是 QQ 邮箱授权码明文。
- ``TDENGINE_PASSWORD`` 的兜底值是数据库弱口令，同文件还写死了公网 IP。

本模块只暴露 K 线训练实际读取的名字（``SQLITE_PATH`` / ``SECRET_KEY`` /
``SESSION_COOKIE_SECURE`` / ``PERMANENT_SESSION_LIFETIME`` / ``ADMIN_EMAILS`` /
``EMAIL_CODE_TTL`` / ``EMAIL_CODE_RESEND_COOLDOWN`` / ``EMAIL_DEV_FALLBACK`` /
``SMTP_CONFIG``，以及闲置的 ``TDENGINE_*``），并保证 **任何密钥都没有代码内默认值**。

凭据解析优先级（与宿主 web_auth / db_url.txt / gateway_config 的外置约定一致）
------------------------------------------------------------------------------
1. 环境变量（部署时注入，优先级最高）
2. 外置数据文件（``data/`` 目录，已在 .gitignore 内，不随源码发布）
3. 空值 / 现场生成随机值并落盘 —— 绝不回落到写死的字符串

模块别名约定
------------
K 线训练各文件原本写作 ``import config``（模块级 ``config.SQLITE_PATH`` 等）。
迁移统一改成 ``from crypto.kline_training import settings as config``，
因此这里必须提供与原名完全一致的属性名，业务逻辑一行不用改。
"""
import os
import secrets as _secrets
from datetime import timedelta

# ---------------------------------------------------------------------
#  外置数据目录解析（宿主统一入口）
# ---------------------------------------------------------------------


def _data_file(filename: str) -> str:
    """把文件名解析为外置数据目录中的绝对路径；解析失败时回落到相对路径。"""
    try:
        from crypto.data_paths import resolve_data_file
        return resolve_data_file(filename)
    except Exception:
        try:
            from data_paths import resolve_data_file
            return resolve_data_file(filename)
        except Exception:
            return filename


def _read_secret_file(filename: str) -> str:
    """读取外置凭据文件的首行非注释文本；不存在或读失败返回空串。"""
    try:
        path = _data_file(filename)
        if not path or not os.path.isfile(path):
            return ''
        with open(path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#'):
                    return line
    except Exception:
        return ''
    return ''


def _env(*names: str) -> str:
    """按顺序取第一个非空环境变量。"""
    for name in names:
        value = (os.environ.get(name) or '').strip()
        if value:
            return value
    return ''


def _env_bool(*names: str, default: bool = False) -> bool:
    """布尔开关：显式给出 1/true/yes/on 才算开，0/false/no/off 才算关，未给用默认值。"""
    for name in names:
        raw = (os.environ.get(name) or '').strip().lower()
        if raw in ('1', 'true', 'yes', 'on'):
            return True
        if raw in ('0', 'false', 'no', 'off'):
            return False
    return default


def _env_int(*names: str, default: int = 0) -> int:
    """整数项：取第一个能转成 int 的环境变量，否则用默认值。"""
    for name in names:
        raw = (os.environ.get(name) or '').strip()
        if raw:
            try:
                return int(raw)
            except ValueError:
                break
    return default


# =====================================================================
#  SQLite 训练库（D3：数据原样搬进宿主 data/，格式后续再统一）
# =====================================================================
# 原路径为 futureStockTrade/data/training.db，迁移时已用 sqlite 备份 API 完整
# 复制为 data/kline_training.db（含全部用户/成绩/对战记录）。*.db 已在 .gitignore。
SQLITE_PATH = _env('CRYPTO_KLINE_SQLITE_PATH') or _data_file('kline_training.db')


# =====================================================================
#  会话签名密钥（Flask app.secret_key）
# =====================================================================
# K 线训练的登录态走 Flask 签名 Cookie，必须有稳定密钥。取值顺序：
#   1) CRYPTO_SECRET_KEY 环境变量
#   2) data/app_secret_key.txt（已在 .gitignore）
#   3) 首次启动现场生成 32 字节随机密钥并写入上面那个文件
# 第 3 条保证「密钥永不进版本库、也永不是写死的字符串」。
# 注意：该文件丢失会让所有已登录用户重新登录（成绩与账号本身不受影响）。


def _resolve_secret_key() -> str:
    key = _env('CRYPTO_SECRET_KEY', 'FST_SECRET_KEY')
    if key:
        return key
    path = _data_file('app_secret_key.txt')
    existing = _read_secret_file('app_secret_key.txt')
    if existing:
        return existing
    key = _secrets.token_hex(32)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            f.write('# Flask 会话签名密钥，由 K 线训练模块首次启动自动生成。\n'
                    '# 删除本文件会导致全部登录态失效（账号数据不受影响）。\n'
                    '# 已在 .gitignore 内，请勿提交。\n'
                    + key + '\n')
        try:
            os.chmod(path, 0o600)
        except Exception:
            pass   # Windows 无 POSIX 权限位，忽略
        print(f'[kline_training] 已生成会话签名密钥并保存到 {path}（不要提交到版本库）')
    except Exception as e:
        # 落盘失败仍用随机密钥继续：本次进程内可用，重启后需重新登录，
        # 但绝不会退化成「可被伪造的固定密钥」。
        print(f'[WARNING][kline_training] 会话密钥落盘失败({e})，本次启动使用临时随机密钥')
    return key


SECRET_KEY = _resolve_secret_key()

# 会话 Cookie 是否仅经 HTTPS 传输（自托管 http 场景保持关闭，与原项目默认一致）
SESSION_COOKIE_SECURE = _env_bool('CRYPTO_SESSION_COOKIE_SECURE',
                                  'FST_SESSION_COOKIE_SECURE', default=False)

# 登录态有效期：勾选「记住我」后 30 天（原项目 60*60*24*30 秒，语义不变）
PERMANENT_SESSION_LIFETIME = timedelta(days=_env_int('CRYPTO_KLINE_SESSION_DAYS',
                                                     default=30) or 30)


# =====================================================================
#  管理员白名单（重置他人密码等高权限操作）
# =====================================================================
# 复用宿主已有的管理员邮箱口径（task/config/email_config.ADMIN_EMAILS），
# 不再新增一份「谁是管理员」的配置；环境变量 CRYPTO_KLINE_ADMIN_EMAILS 可覆盖。
# 两边都取不到 → 空集合 = 无人是管理员（fail-safe，宁可拒绝也不放行）。


def _resolve_admin_emails() -> set:
    raw = _env('CRYPTO_KLINE_ADMIN_EMAILS', 'FST_ADMIN_EMAILS')
    if not raw:
        raw = _read_secret_file('kline_admin_emails.txt')   # 可选：外置一行邮箱清单
    if not raw:
        cfg = _host_email_admins()
        raw = ','.join(cfg)
    return {e.strip().lower() for e in raw.split(',') if e.strip()}


def _host_email_admins() -> list:
    """从宿主邮件配置读管理员邮箱。

    宿主只维护一个 ADMIN_EMAIL（「任务系统 → 邮件配置」页面写入），没有独立的管理员
    清单，所以这里按 get_admin_email() → 发件人 的顺序取，取不到就返回空列表，
    让 ADMIN_EMAILS 保持空集合（无人是管理员）。
    """
    out = []
    try:
        from crypto.task.config.email_config import get_admin_email
        one = (get_admin_email() or '').strip()
        if one:
            out.append(one)
    except Exception:
        pass
    if not out:
        try:
            from crypto.task.config.email_config import get_email_config
            one = (get_email_config() or {}).get('from_email', '').strip()
            if one:
                out.append(one)
        except Exception:
            pass
    return out


ADMIN_EMAILS = _resolve_admin_emails()


# =====================================================================
#  邮箱验证码策略
# =====================================================================
EMAIL_CODE_TTL = _env_int('CRYPTO_KLINE_EMAIL_CODE_TTL', 'FST_EMAIL_CODE_TTL', default=300)
EMAIL_CODE_RESEND_COOLDOWN = _env_int('CRYPTO_KLINE_EMAIL_CODE_COOLDOWN',
                                      'FST_EMAIL_CODE_COOLDOWN', default=60)

# 开发兜底：SMTP 发送失败时把验证码回传给前端/日志，方便本地自测注册流程。
# 原项目默认「开」，合并到实盘宿主后默认改为「关」——真实发件通道（宿主
# email_config.py）就在机器上，没必要把验证码明文留在响应体与日志里。
# 本地要自测时：set CRYPTO_KLINE_EMAIL_DEV_FALLBACK=1
EMAIL_DEV_FALLBACK = _env_bool('CRYPTO_KLINE_EMAIL_DEV_FALLBACK',
                               'FST_EMAIL_DEV_FALLBACK', default=False)


# =====================================================================
#  SMTP 通道：复用宿主「任务系统 → 邮件配置」，不再自带一份授权码
# =====================================================================
# mailer.py 仍按 config.SMTP_CONFIG 的 5 个键读取，键名保持不变；取值改为
# 直接引用宿主唯一数据源 crypto/task/config/email_config.py（gitignore 文件，
# 由 Web 页面维护）。这样全站只有一份发件账号，改一处全站生效。
SMTP_HOST_FALLBACK = 'smtp.qq.com'


def _resolve_smtp_config() -> dict:
    cfg = {'from_email': '', 'password': '', 'smtp_host': SMTP_HOST_FALLBACK,
           'smtp_port': 465, 'use_ssl': True}
    try:
        from crypto.task.config.email_config import get_email_config
        host_cfg = get_email_config() or {}
        cfg['from_email'] = (host_cfg.get('from_email') or '').strip()
        cfg['password'] = host_cfg.get('password') or ''
        cfg['smtp_host'] = (host_cfg.get('smtp_host') or SMTP_HOST_FALLBACK).strip()
        try:
            cfg['smtp_port'] = int(host_cfg.get('smtp_port', 465))
        except Exception:
            cfg['smtp_port'] = 465
        cfg['use_ssl'] = bool(host_cfg.get('use_ssl', True))
    except Exception:
        # 宿主邮件配置尚未生成（新环境）：留空让 mailer 走失败分支，
        # 由 EMAIL_DEV_FALLBACK 决定是否回传验证码，绝不回落到任何硬编码授权码。
        pass
    # 环境变量最后压顶：CI/容器化部署可以完全绕开文件
    cfg['from_email'] = _env('CRYPTO_KLINE_SMTP_FROM', 'FST_SMTP_FROM') or cfg['from_email']
    cfg['password'] = _env('CRYPTO_KLINE_SMTP_PASSWORD', 'FST_SMTP_PASSWORD') or cfg['password']
    cfg['smtp_host'] = _env('CRYPTO_KLINE_SMTP_HOST', 'FST_SMTP_HOST') or cfg['smtp_host']
    port = _env('CRYPTO_KLINE_SMTP_PORT', 'FST_SMTP_PORT')
    if port.isdigit():
        cfg['smtp_port'] = int(port)
    return cfg


SMTP_CONFIG = _resolve_smtp_config()


# =====================================================================
#  TDengine 历史K线缓存（闲置能力，默认关闭）
# =====================================================================
# 原项目用 TDengine 缓存 NEAR 1 分钟K线。宿主已有自己的 1 分钟K线链路
# （crypto/kline_fetcher + /kline-live），这套缓存目前无人调用，只作为可选工具
# 保留在 crypto/kline_training/td_cache/。凭据一律只从环境变量注入，
# 原 config.py 里写死的服务器地址与出厂账号口令一律不进仓库（也不写在注释里）。
TDENGINE_REST_URL = _env('TDENGINE_REST_URL', 'CRYPTO_TDENGINE_REST_URL')
TDENGINE_USER = _env('TDENGINE_USER', 'CRYPTO_TDENGINE_USER')
TDENGINE_PASSWORD = _env('TDENGINE_PASSWORD', 'CRYPTO_TDENGINE_PASSWORD')
TDENGINE_DATABASE = _env('CRYPTO_TDENGINE_DATABASE') or 'near_usdt'
TDENGINE_STABLE = _env('CRYPTO_TDENGINE_STABLE') or 'kline_1m'
TDENGINE_CHILD = _env('CRYPTO_TDENGINE_CHILD') or 'near_okx'

# 总开关：默认关闭。开启需要同时给到 REST 地址与凭据，缺一即视为未配置。
TDENGINE_CACHE_ENABLED = (_env_bool('CRYPTO_TDENGINE_CACHE', 'FST_TDENGINE_CACHE',
                                    default=False)
                          and bool(TDENGINE_REST_URL) and bool(TDENGINE_USER))


# =====================================================================
#  诊断辅助：只报「有没有配」，绝不打印密钥本身
# =====================================================================

def state() -> dict:
    """返回脱敏配置状态，供冒烟脚本与健康检查打印。"""
    return {
        'sqlite_path': SQLITE_PATH,
        'sqlite_exists': os.path.isfile(SQLITE_PATH),
        'secret_key_source': ('env' if _env('CRYPTO_SECRET_KEY', 'FST_SECRET_KEY')
                              else ('data_file' if _read_secret_file('app_secret_key.txt')
                                    else 'ephemeral')),
        'session_cookie_secure': SESSION_COOKIE_SECURE,
        'permanent_session_days': PERMANENT_SESSION_LIFETIME.days,
        'admin_emails_count': len(ADMIN_EMAILS),
        'smtp_configured': bool(SMTP_CONFIG.get('from_email')
                                and SMTP_CONFIG.get('password')),
        'email_dev_fallback': EMAIL_DEV_FALLBACK,
        'tdengine_cache_enabled': TDENGINE_CACHE_ENABLED,
    }
