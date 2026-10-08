"""
LifeQuant 启动入口
====================
项目的根级启动脚本，导入 crypto 模块中的 Flask 应用并运行。
"""
import sys
import os
import warnings

# Python 3.13 已弃用 datetime.datetime.utcnow()，okx SDK 内部仍在使用（第三方库代码无法修改），
# 每次发请求都会在终端刷屏两遍 DeprecationWarning。这里精确屏蔽该弃用警告，不影响其他警告。
warnings.filterwarnings(
    "ignore",
    category=DeprecationWarning,
    message=r"datetime\.datetime\.utcnow\(\) is deprecated",
)

# Windows 无控制台/GBK 环境下 print 含 emoji 会抛 UnicodeEncodeError 导致进程直接退出，
# 这里统一将标准输出重配置为 UTF-8（无法替换的字符降级处理）
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

# 确保项目根目录在 sys.path 中，以便导入 crypto 包
_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# =====================================================================
#  单实例守护 —— 必须放在 `from crypto.app import app` 之前
# =====================================================================
# Windows 下 Werkzeug 默认带 SO_REUSEADDR：第二个 `python app.py` 会「静默」复用
# 同一端口，浏览器请求被新旧实例随机劫持，表现为「访问口令明明输对却间歇性失败」
# 「某些接口时好时坏」这类极难复现的假故障。合并迁移之后 /futures、/stocks、
# /kline 全挂在同一个 7777 上，双实例并存的排查成本比原来高一个量级。
#
# 为什么要在导入应用之前探测：crypto/app.py 在 **import 阶段** 就会拉起数据库预热
# 与调度线程（见 crypto/app.py 的 _boot_scheduler_background / CRYPTO_NO_BACKGROUND）。
# 若把守护放在 app.run() 前，第二个实例会先把调度器跑起来才被拦下——那是一个会
# 下单的调度器。所以这里先探端口，占用就直接退出，连应用都不加载。
#
# 只作用于 `python app.py` 这条直启路径：Gunicorn/uWSGI 按文档以 app:app 加载，
# 走 import 不经过 __main__，多 worker 部署不受影响。
# 临时验证确实需要并存第二个实例时：设 CRYPTO_ALLOW_DUP_PORT=1 跳过探测，
# 但更推荐的做法仍然是换端口（例：CRYPTO_WEB_PORT=7788 python app.py）。
if __name__ == '__main__':
    # 监听地址/端口可用环境变量覆盖（默认 0.0.0.0 + 7777）。
    # 注：不要用 6000——它被 Chrome/Edge 列入“不安全端口”黑名单（X11），
    # 浏览器会直接拒连并报 ERR_UNSAFE_PORT；7777 合法。
    # 安全不靠改这里：真正兜底的是 crypto/web_auth.py 的访问闸门——
    # 没配口令时远程一律 403（等价于只绑本机），配了口令时远程输口令进入，
    # 因此绑 0.0.0.0 也不再是“实盘接口裸奔”。
    _host = (os.environ.get('CRYPTO_WEB_HOST') or '0.0.0.0').strip()
    _port = int(os.environ.get('CRYPTO_WEB_PORT') or 7777)
    _skip_guard = (os.environ.get('CRYPTO_ALLOW_DUP_PORT') or '').strip().lower() \
        in ('1', 'true', 'yes')

    if not _skip_guard:
        import socket
        _probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        _probe.settimeout(0.6)
        try:
            _occupied = _probe.connect_ex(('127.0.0.1', _port)) == 0
        except OSError:
            _occupied = False        # 探测本身失败（端口越界等）不该挡住启动
        finally:
            _probe.close()
        if _occupied:
            print(f'[ERROR] 端口 {_port} 已有服务在监听，拒绝再启动第二个实例。')
            print('        Windows 下多实例会因端口复用互相劫持请求，'
                  '表现为访问口令明明输对却间歇性失败。')
            print(f'        请先结束旧实例，或换一个端口启动：'
                  f'CRYPTO_WEB_PORT={_port + 1} python app.py')
            print(f'        查占用：netstat -ano | findstr :{_port}')
            print('        结束监听进程（PowerShell）：Get-NetTCPConnection '
                  f'-LocalPort {_port} -State Listen | '
                  'ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }')
            print('        确实需要并存第二个实例：CRYPTO_ALLOW_DUP_PORT=1 '
                  '（注意本项目的调度器会在启动时拉起，双实例可能重复下单）')
            sys.exit(1)

from crypto.app import app
from crypto.web_auth import configured_token as _web_token

if __name__ == '__main__':
    print("[启动] 前后端分离服务已启动！")
    print(f"[启动] 请在浏览器访问: http://127.0.0.1:{_port}")
    print(f"[启动] LifeQuant 策略监控台: http://127.0.0.1:{_port}/")
    print(f"[启动] API控制台: http://127.0.0.1:{_port}/api-console")
    # 合并迁移（futureStockTrade）进来的三个模块。按 blueprint 是否真的注册成功来
    # 打印，而不是无条件写死：FST_ENABLE_FUTURES/STOCKS/KLINE=0 关掉、或注册时因
    # 缺依赖降级（crypto/app.py 里三段 try/except）时，这里就不会给出死链。
    for _bp_name, _label, _prefix in (('futures', '期货监控台', '/futures/'),
                                      ('stocks', '股票监控台', '/stocks/'),
                                      ('kline', 'K线训练', '/kline/')):
        if _bp_name in app.blueprints:
            print(f"[启动] {_label}: http://127.0.0.1:{_port}{_prefix}")
    if _web_token():
        print("[安全] 访问口令已启用（CRYPTO_WEB_TOKEN 或 data/web_token.txt）："
              "所有设备都要先在登录页输一次口令")
    else:
        print(f"[安全] 未配置访问口令 → 只允许本机访问，从其它设备打开 "
              f"http://{_host}:{_port} 会被 403 拦下")
        print("[安全] 想让手机/电脑远程用：设 CRYPTO_WEB_TOKEN 环境变量，"
              "或把口令写进 data/web_token.txt（已在 .gitignore），重启生效")
    # threaded=True：Flask 开发服务器默认单线程，并发请求（页面加载+多个fetch）
    # 会互相阻塞甚至导致进程异常退出，开启多线程保证接口可用性
    app.run(debug=False, host=_host, port=_port, threaded=True)
