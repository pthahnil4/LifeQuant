import io
from flask import Flask, render_template, jsonify, request, send_file, Response, stream_with_context
from .real_strategy_adapter import (
    calculate_strategy_data, 
    calculate_single_coin_data, 
    calculate_multi_period_data,
    get_all_coins, 
    get_selected_coins, 
    set_selected_coins,
    sync_coin_list,
    get_starred_coins,
    set_starred_coins,
    clear_starred_coins,
    clear_all_selections,
    get_fixed_coins,
    get_floating_coins,
    add_floating_coins,
    remove_floating_coin,
    clear_floating_coins,
    remove_fixed_coins,
    promote_floating_to_fixed,
    sync_fixed_from_csv,
    last_save_db_ok
)
from .api_routes import api_bp
from .plan_routes import plan_bp
from .calorie_routes import calorie_bp
from .expense_routes import expense_bp
from .journal_routes import journal_bp
from .diary_routes import diary_bp
from .alert_routes import alert_bp
from .discipline_routes import discipline_bp
from .capability_routes import capability_bp
from .instinct_routes import instinct_bp
from .checkin_routes import checkin_bp
from .indicator_nav_routes import indicator_nav_bp
from .kline_live_routes import kline_live_bp
from .strategy_util import get_strategy_detail
import traceback
from datetime import datetime, timedelta

app = Flask(__name__)

# 只读接口限频/退避（问题#8）：页面刷新与同进程的实盘调度、行情扫描、告警监控抢
# 的是同一个 API key 配额，不限频时一旦打爆就集体回 50011（表现为“页面暂时拿不到
# 数据”）。导入失败时置 None，_rl_read 退化为直连，绝不因限频模块不可用而弄挂页面。
try:
    from .task.utils.okx_ratelimit import limited as _rl_limited
except ImportError:
    _rl_limited = None


def _rl_read(group, func, *args, **kwargs):
    """只读 OKX 接口的统一出口：节流 + 50011 退避；限频模块缺失时直通。

    只给查询类调用用。下单/撤单/改单严禁塞进来（结果未知时自动重发会重复下单）。
    """
    return _rl_limited(group, func, *args, **kwargs) if _rl_limited else func(*args, **kwargs)

# =====================================================================
# 注册 API 路由蓝图（将 demo 封装为 HTTP 接口）
# =====================================================================
app.register_blueprint(api_bp)

# =====================================================================
# 注册任务计划蓝图（游戏化自我激励模块）
# =====================================================================
app.register_blueprint(plan_bp)

# =====================================================================
# 注册热量缺口管理蓝图（健康管理模块）
# =====================================================================
app.register_blueprint(calorie_bp)
app.register_blueprint(expense_bp)

# =====================================================================
# 注册随笔与复盘蓝图（思考沉淀模块）
# =====================================================================
app.register_blueprint(journal_bp)

# =====================================================================
# 注册日记本蓝图（按天记录 + 密码保护 + 人生/目标倒计时）
# =====================================================================
app.register_blueprint(diary_bp)

# =====================================================================
# 注册监控告警蓝图（异常行情与持仓盈亏监控报警）
# =====================================================================
app.register_blueprint(alert_bp)

# =====================================================================
# 注册分析纪律蓝图（小时槽合格判定/打卡闸门/反懈怠看板）
# =====================================================================
app.register_blueprint(discipline_bp)

# =====================================================================
# 注册 OKX 能力清单阅读页蓝图（/okx-capability：渲染 doc/ 下清单 + 工具矩阵）
# 纯只读文档页，不碰交易接口；markdown 库缺失时页面自动降级为纯文本
# =====================================================================
app.register_blueprint(capability_bp)

# =====================================================================
# 注册盘感模拟蓝图（/instinct：语料检索 + Wiki 规则卡 + 影子预测 A/B）
# 只读行情、只写 instinct_* 表；影子预测永不触达交易执行链路
# =====================================================================
app.register_blueprint(instinct_bp)

# =====================================================================
# 注册每日多维打卡蓝图（/checkin：热量/交易/学习三项自动采集 + 评级评价）
# 自动项只读采集；唯一写目标是 daily_checkins / trading_daily_snapshots
# 两张新表，详见 doc/每日多维打卡设计文档.md
# =====================================================================
app.register_blueprint(checkin_bp)

# =====================================================================
# 注册指标导航蓝图（/indicator-nav：长周期趋势方法可视化对比回测）
# 只读行情与本地 CSV；零 DB 写入、零交易接口触达，计算全部在
# indicator_nav_service（复用 strategy/trend_strategy_..._copy 7.py 的
# TREND_INDICATORS 注册表与方向计算）
# =====================================================================
app.register_blueprint(indicator_nav_bp)

# =====================================================================
# 注册 BTC 1分钟K线实时拉取蓝图（/kline-live：拉取进度看板 + 最新明细）
# 数据由定时任务 kline_live_btc 写 CSV（ccxt → OKX 公共行情，无密钥）；
# 页面 GET 只读内存状态与本地 CSV，零写入零触网；POST 仅改配置/手动触发。
# =====================================================================
app.register_blueprint(kline_live_bp)

# =====================================================================
# 合并模块（futureStockTrade）注册与降级提示：公共助手
# ---------------------------------------------------------------------
# 三个模块的行情源不同：期货/股票走 akshare（国内网络），K 线训练走 OKX 公共行情
# 加本地 SQLite。任何一处缺依赖、缺网络或环境变量关掉，都只是**该模块**不可用，
# 绝不能拖垮加密货币主功能——所以注册一律包在 try/except 里。
#
# 但「静默 404」是另一种伤害：用户点导航进来只看到裸 404，既不知道是被开关关了，
# 也不知道是依赖没装。因此这里补一个降级提示页（JSON 请求回 503），把原因和
# 恢复办法说清楚。前缀路由只在模块**确实没挂上**时注册，挂上了就完全不拦截。
# =====================================================================
_FST_MODULES = {}   # 模块名 → {'status': ok/disabled/failed, 'error': str, 'env': str, 'title': str}


def _register_fst_module(app, module_key, env_name, prefix, title, importer):
    """注册一个合并进来的蓝图；失败/关闭时登记状态并挂降级提示路由。

    importer 是个无参函数，负责「导入蓝图对象 + register_blueprint」，
    这样每个模块原有的注册细节（url_prefix、额外初始化）都能留在各自块里。
    """
    import os as _os

    _FST_MODULES[module_key] = {
        'title': title, 'env': env_name, 'prefix': prefix,
        'status': 'ok', 'error': '',
    }
    off = (_os.environ.get(env_name, '1').strip().lower()
           in ('0', 'false', 'no', 'off'))
    if off:
        _FST_MODULES[module_key]['status'] = 'disabled'
        print(f'[INFO] {title} 已按 {env_name}=0 关闭，访问 {prefix} 会看到关闭提示页')
    else:
        try:
            importer()
        except Exception as e:
            _FST_MODULES[module_key].update(status='failed', error=str(e))
            print(f'[WARNING] {title}注册失败（加密货币主功能不受影响）: {e}')
    if _FST_MODULES[module_key]['status'] != 'ok':
        _register_fst_fallback(app, module_key)
    return _FST_MODULES[module_key]


def _register_fst_fallback(app, module_key):
    """给未挂上的模块前缀挂一个提示路由（页面回 HTML，接口回 503 JSON）。"""
    meta = _FST_MODULES[module_key]
    prefix = meta['prefix'].rstrip('/') or '/'

    def _handler(sub=''):
        wants_json = (request.path.startswith('/api/')
                      or 'application/json' in (request.headers.get('Accept') or ''))
        if wants_json:
            return jsonify({
                'code': 503,
                'success': False,
                'message': f"{meta['title'].split('（')[0]}当前不可用（"
                           f"{meta['status']}）",
                'data': None,
            }), 503
        return render_template('fst_unavailable.html', meta=meta,
                               sub=sub, active_page=module_key), 503

    if prefix == '/':
        app.add_url_rule('/', view_func=_handler,
                         endpoint=f'fst_{module_key}_root', strict_slashes=False)
    else:
        app.add_url_rule(prefix + '/', view_func=_handler,
                         endpoint=f'fst_{module_key}_index', strict_slashes=False)
        app.add_url_rule(prefix + '/<path:sub>', view_func=_handler,
                         endpoint=f'fst_{module_key}_sub')


# =====================================================================
# 注册期货监控蓝图（/futures：akshare 期货行情 + Pro3 双周期策略 + 星标行情）
# ---------------------------------------------------------------------
# 由 futureStockTrade 合并迁移 Phase 2 引入。期货实时行情走 akshare，
# 依赖国内网络；因此整段用 try/except 包裹，注册失败只降级、不拖垮加密主功能。
# 可用环境变量 FST_ENABLE_FUTURES=0 显式关闭。
# 位置要求：必须在下方 _init_web_auth(app) 之前，否则 /futures 绕过访问闸门。
# =====================================================================
def _import_futures():
    from .futures.routes import futures_bp as _futures_bp
    app.register_blueprint(_futures_bp, url_prefix='/futures')


_register_fst_module(app, 'futures', 'FST_ENABLE_FUTURES', '/futures/',
                     '期货监控台（akshare 行情，需国内网络）', _import_futures)

# =====================================================================
# 注册股票监控蓝图（/stocks：akshare A 股行情 + Pro3 策略/传统指标双模式）
# ---------------------------------------------------------------------
# 由 futureStockTrade 合并迁移 Phase 3 引入。与期货同理：A 股实时行情走 akshare，
# 依赖国内网络，整段 try/except 包裹，注册失败只降级、不拖垮加密主功能。
# 可用环境变量 FST_ENABLE_STOCKS=0 显式关闭。
# 股票模块只读行情、零交易接口触达；自选股清单写在 crypto/stocks/stock_config.json。
# 位置要求：必须在下方 _init_web_auth(app) 之前，否则 /stocks 绕过访问闸门。
# =====================================================================
def _import_stocks():
    from .stocks.routes import stocks_bp as _stocks_bp
    app.register_blueprint(_stocks_bp, url_prefix='/stocks')


_register_fst_module(app, 'stocks', 'FST_ENABLE_STOCKS', '/stocks/',
                     '股票监控台（akshare 行情，需国内网络）', _import_stocks)

# =====================================================================
# K 线训练模块的 Flask 会话配置 + 蓝图注册（/kline：三品类训练 + 盲选 + 对战）
# ---------------------------------------------------------------------
# 由 futureStockTrade 合并迁移 Phase 4 引入（原 kline/ 顶层包，53 条路由）。
#
# 为什么这里要额外配 secret_key：宿主此前完全没有用过 Flask session（鉴权走
# web_auth 的签名无关 Cookie），所以 app.secret_key 一直是空的。K 线训练的登录态
# 是 session['kline_uid']，必须有签名密钥才能工作。取值口径见
# crypto/kline_training/settings.py：环境变量 → data/app_secret_key.txt →
# 首次启动现场生成随机密钥并落盘（已在 .gitignore）。绝不使用写死的默认值，
# 那等于任何人都能伪造登录 Cookie。
#
# 建表（store.init_db）用惰性初始化：store.py 内部每个数据访问前会 _ensure_init()，
# 这里只顺手触发一次，失败不影响其它模块（K 线训练页会自行报错，主功能不受累）。
# 位置要求：蓝图注册必须在下方 _init_web_auth(app) 之前，否则 /kline 绕过访问闸门。
# 可用环境变量 FST_ENABLE_KLINE=0 显式关闭。
# =====================================================================
def _import_kline():
    from .kline_training import settings as _kline_settings
    from .kline_training.routes import kline_bp as _kline_bp

    app.secret_key = _kline_settings.SECRET_KEY
    app.permanent_session_lifetime = _kline_settings.PERMANENT_SESSION_LIFETIME
    app.config['SESSION_COOKIE_SECURE'] = _kline_settings.SESSION_COOKIE_SECURE
    app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

    app.register_blueprint(_kline_bp, url_prefix='/kline')
    try:
        from .kline_training import store as _kline_store
        _kline_store.init_db()
    except Exception as _kline_db_e:
        print(f"[WARNING] K 线训练 SQLite 初始化失败: {_kline_db_e}")


_register_fst_module(app, 'kline', 'FST_ENABLE_KLINE', '/kline/',
                     'K 线训练台（三品类复盘训练）', _import_kline)

# =====================================================================
# 注册导航自定义管理蓝图 + 模板上下文（/navigation-settings、/api/navigation/*）
# ---------------------------------------------------------------------
# 必须在 Web 闸门注册之前：init_app 会 register_blueprint(nav_bp) 并挂 context
# processor（仅注入内存默认配置，不碰数据库）。导航写接口在闸门之上再叠加同源校验。
# =====================================================================
from .navigation_routes import init_app as _init_nav

# =====================================================================
# Web 访问闸门（审计问题#1：原先 0.0.0.0 监听 + 零鉴权，实盘下单/强平接口全暴露）
# ---------------------------------------------------------------------
# 必须放在所有蓝图注册之后：before_request 对蓝图路由同样生效，而 /auth/gate
# 这个端点由本模块自带。fail-safe 口径：没配口令时只放行本机，配了口令时
# 所有来源（含回环）都要过口令。判定逻辑与理由见 web_auth.py 文档。
# =====================================================================
from .web_auth import init_app as _init_web_auth
_init_nav(app)          # 先注册导航蓝图与 context processor，再挂全局闸门
_init_web_auth(app)

# =====================================================================
# 启动加速：DB 后台预热 + 调度器后台启动
# ---------------------------------------------------------------------
# 1. warmup_async：在守护线程内完成 init_db（探活 + 种子补全），
#    不阻塞 app.run()，首个 DB 请求到达时若未完成会自动重试。
# 2. register_default_jobs 会读 kv_store 告警配置并拉起监控引擎
#    依赖链（含 OKX 客户端），导入期同步执行会显著拖慢启动，
#    移入后台线程；交易调度本就是用户手动启动，晚几秒就绪无影响。
# =====================================================================
import threading as _threading
import os as _os_env

from .database import warmup_async as _db_warmup_async
from .db_performance import init_app as _init_db_performance
_init_db_performance(app)

# 后台任务总开关：导入 crypto.app 会顺带拉起调度器与两个监控线程，
# 它们会真发告警邮件、真调 OKX、真写台账。冒烟脚本与 CLI 工具用
# test_client 只想打接口，不应该在生产收件箱里留下测试信、也不应该让
# 巡检线程与测试互相干扰，因此留一个环境变量把这三处后台启动一起关掉。
# 默认不设置 = 行为与以前完全一致。
_NO_BACKGROUND = str(_os_env.environ.get('CRYPTO_NO_BACKGROUND', '')).strip().lower() \
    in ('1', 'true', 'yes', 'on')
if _NO_BACKGROUND:
    print('[Boot] CRYPTO_NO_BACKGROUND 已设置：跳过数据库预热、调度器与监控线程启动')
else:
    _db_warmup_async()


def _boot_scheduler_background():
    try:
        from .task.scheduler import task_scheduler, register_default_jobs
        register_default_jobs()
        task_scheduler.start()
    except Exception as _sched_err:
        import traceback as _tb
        _tb.print_exc()
        print(f"[Warning] 定时任务调度器启动失败: {_sched_err}")
        return

    # 重启自愈：先弄明白「这次是怎么起来的」（初次/撞内存/被杀/每日归零/冷启动），
    # 再把归因结果交给调度器按策略决定起不起、等多久、要不要先发预告信：
    #   夜间窗口 00:00-08:00 → 直接接回；白天 → 先发预告再留 10 分钟人工否决窗；
    #   初次上线 / 窗口内反复重启（熔断）→ 只发信不起。
    # 总闸门仍是页面上的 auto_resume 开关 + 「上次确实在跑」，两者缺一就维持
    # 人工启动（与改造前行为一致）。
    _boot_info = None
    try:
        from .process_lifecycle import classify_and_record_boot
        _boot_info = classify_and_record_boot()
        print(f"[Boot] 启动归因: {_boot_info.get('reason')} | {_boot_info.get('detail')}")
    except Exception as _bl_err:
        print(f"[Warning] 启动归因失败（自愈按未知处理，不自动拉起）: {_bl_err}")
    try:
        task_scheduler.schedule_auto_resume(boot_info=_boot_info)
    except Exception as _ar_err:
        print(f"[Warning] 实盘重启自愈任务未能登记: {_ar_err}")


if not _NO_BACKGROUND:
    # SIGTERM 捕获必须在主线程装（signal 只能在主线程注册），所以放在模块导入
    # 阶段而不是下面的后台线程里：宝塔/面板点“重启”发的是 SIGTERM，不装的话
    # 这种人工重启会被归因成「异常死亡（崩溃/被系统杀）」，白收一封事故信。
    try:
        from .process_lifecycle import install_sigterm_marker
        install_sigterm_marker()
    except Exception as _sig_err:
        print(f"[Warning] SIGTERM 捕获未安装（面板重启会被误判为异常死亡）: {_sig_err}")
    _threading.Thread(target=_boot_scheduler_background, daemon=True,
                      name='scheduler-boot').start()

# =====================================================================
# 稳定性监控（方案一 + 方案二）
# ---------------------------------------------------------------------
# 1. memory_watchdog：进程内内存自监控，每 2 分钟检查 RSS 并写入
#    logs/memory_history.jsonl（这份记录兼作启动归因用的心跳），超阈值
#    邮件预警，严重超限先落退出标记再主动退出，由外部管理器
#    （supervisord autorestart）自动拉起。
# 2. system_monitor：系统级健康检查（进程/内存/磁盘），每 60s 一轮，
#    异常邮件告警；进程崩溃时该线程随之消亡，完整兜底需另配 cron 独立运行
#    （python crypto/system_monitor.py，详见文件头说明）。
# =====================================================================
if not _NO_BACKGROUND:
    try:
        from .memory_watchdog import start_memory_watchdog
        start_memory_watchdog()
    except Exception as _mem_err:
        print(f"[Warning] 内存监控启动失败: {_mem_err}")

    try:
        from .system_monitor import start_system_monitor
        start_system_monitor()
    except Exception as _sysmon_err:
        print(f"[Warning] 系统监控启动失败: {_sysmon_err}")

# =====================================================================
# 【前端页面路由】
# 返回前端 HTML 页面
# =====================================================================
@app.route('/')
def index():
    return render_template('index.html', active_page='dashboard')


@app.route('/detail/<symbol>')
def coin_detail(symbol):
    """币种详情页：历史交易 + 策略回测"""
    return render_template('detail.html', symbol=symbol, active_page='dashboard')


@app.route('/multi-period/<symbol>')
def multi_period_overview(symbol):
    """多周期方向总览页：展示多个周期的趋势方向对比"""
    return render_template('multi_period.html', symbol=symbol, active_page='dashboard')


@app.route('/api-console')
def api_console():
    """API 接口控制台：市场/账户/资金/交易"""
    return render_template('api_console.html', active_page='api-console')


@app.route('/system-status')
def system_status_page():
    """系统状态页：内存自监控概况 + 内存历史趋势图"""
    return render_template('system_status.html', active_page='system-status')


@app.route('/task')
def task_page():
    """定时任务管理页"""
    return render_template('task.html', active_page='task')


@app.route('/analysis')
def analysis_page():
    """实盘分析记录页（从定时任务页提升为独立一级页面，全宽布局，与 Tab 共用 analysis.js）"""
    return render_template('analysis.html', active_page='analysis')


@app.route('/monitor-guide')
def monitor_guide_page():
    """监控台操作手册（分页标签式功能说明，静态内容无后端依赖）"""
    return render_template('monitor_guide.html', active_page='monitor-guide')


@app.route('/top-coins')
def top_coins_guide_page():
    """市值Top50固定币种导览页（实时行情复用 market-detail 接口 + 内置币种简介）"""
    return render_template('top_coins_guide.html', active_page='top-coins')


@app.route('/plan')
def plan_page():
    """任务计划页"""
    return render_template('task_plan.html', active_page='plan')

# =====================================================================
# 【后端数据 API 接口】
# =====================================================================

@app.route('/api/strategy/coins')
def get_coin_list():
    """返回所有可用币种列表"""
    return jsonify({
        "code": 200,
        "message": "success",
        "data": get_all_coins()
    })

@app.route('/api/strategy/config', methods=['GET'])
def get_config():
    """获取当前配置（全部币种 + 已选中币种 + 星标币种）"""
    return jsonify({
        "code": 200,
        "message": "success",
        "data": {
            "all_coins": get_all_coins(),
            "fixed_coins": get_fixed_coins(),
            "floating_coins": get_floating_coins(),
            "selected_coins": get_selected_coins(),
            "starred_coins": get_starred_coins()
        }
    })

def _coin_cfg_warn():
    """币种自选配置的写库失败提示后缀。

    DB 是主存、本地文件只是兜底，因此写库失败时界面必须说清楚“可能回到
    旧值”，不能让用户拿着一个其实没生效的配置继续监控。
    """
    return '' if last_save_db_ok() else '（⚠️ 数据库主存写入失败，仅本地文件已更新，刷新后可能回到旧值）'


def _coin_config_payload():
    """币种配置的统一响应负载：前端任一币种操作成功后据此整体刷新选择器/卡片。"""
    return {
        "all_coins": get_all_coins(),
        "fixed_coins": get_fixed_coins(),
        "floating_coins": get_floating_coins(),
        "selected_coins": get_selected_coins(),
        "starred_coins": get_starred_coins()
    }


@app.route('/api/strategy/config', methods=['POST'])
def save_config():
    """保存用户选择的币种"""
    try:
        data = request.get_json()
        selected = data.get('selected_coins', [])
        # 校验：只能选 all_coins 里存在的
        all_coins = get_all_coins()
        valid = [c for c in selected if c in all_coins]
        if not valid:
            valid = all_coins[:3] if all_coins else []
        set_selected_coins(valid)
        return jsonify({
            "code": 200,
            "message": "success" + _coin_cfg_warn(),
            "data": get_selected_coins()
        })
    except Exception as e:
        return jsonify({
            "code": 500,
            "message": str(e),
            "data": None
        })


@app.route('/api/strategy/sync-coins', methods=['POST'])
def sync_coins():
    """从CSV同步币种列表到config"""
    try:
        merged = sync_coin_list()
        return jsonify({
            "code": 200,
            "message": f"同步完成，共 {len(merged)} 个币种" + _coin_cfg_warn(),
            "data": {
                "all_coins": get_all_coins(),
                "fixed_coins": get_fixed_coins(),
                "floating_coins": get_floating_coins(),
                "selected_coins": get_selected_coins(),
                "starred_coins": get_starred_coins()
            }
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({
            "code": 500,
            "message": str(e),
            "data": None
        })


@app.route('/api/strategy/floating-coins/add', methods=['POST'])
def add_floating_coins_route():
    """批量添加浮动币种（从趋势扫描页勾选后加入监控）"""
    try:
        data = request.get_json(silent=True) or {}
        coins = data.get('coins', [])
        if not isinstance(coins, list) or not coins:
            return jsonify({"code": 400, "message": "请传入至少一个币种", "data": None})
        floating, added = add_floating_coins(coins)
        return jsonify({
            "code": 200,
            "message": f"已添加 {added} 个浮动币种（{len(coins) - added} 个已存在）" + _coin_cfg_warn(),
            "data": {
                "floating_coins": floating,
                "added": added,
                "all_coins": get_all_coins()
            }
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({"code": 500, "message": str(e), "data": None})


@app.route('/api/strategy/floating-coins/remove', methods=['POST'])
def remove_floating_coin_route():
    """删除单个浮动币种"""
    try:
        data = request.get_json(silent=True) or {}
        coin = (data.get('coin') or '').strip()
        if not coin:
            return jsonify({"code": 400, "message": "缺少 coin 参数", "data": None})
        floating = remove_floating_coin(coin)
        return jsonify({
            "code": 200,
            "message": "已移除" + _coin_cfg_warn(),
            "data": {
                "floating_coins": floating,
                "all_coins": get_all_coins(),
                "selected_coins": get_selected_coins()
            }
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({"code": 500, "message": str(e), "data": None})


@app.route('/api/strategy/floating-coins/clear', methods=['POST'])
def clear_floating_coins_route():
    """批量清空所有浮动币种（监控台一键清理）"""
    try:
        removed = clear_floating_coins()
        return jsonify({
            "code": 200,
            "message": (f"已清空 {removed} 个浮动币种" if removed else "当前没有浮动币种")
                       + (_coin_cfg_warn() if removed else ''),
            "data": {
                "floating_coins": get_floating_coins(),
                "all_coins": get_all_coins(),
                "selected_coins": get_selected_coins(),
                "removed": removed
            }
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({"code": 500, "message": str(e), "data": None})


@app.route('/api/strategy/fixed-coins/remove', methods=['POST'])
def remove_fixed_coins_route():
    """从固定列表移除选中的固定币种（下架币种清理），使其变为未监控状态。"""
    try:
        data = request.get_json(silent=True) or {}
        coins = data.get('coins', [])
        if not isinstance(coins, list) or not coins:
            return jsonify({"code": 400, "message": "请传入至少一个币种", "data": None})
        removed = remove_fixed_coins(coins)
        return jsonify({
            "code": 200,
            "message": f"已移除 {len(removed)} 个固定币种" + _coin_cfg_warn(),
            "data": _coin_config_payload()
        })
    except ValueError as ve:
        # 业务约束（如「至少保留一个固定币种」）→ 400
        return jsonify({"code": 400, "message": str(ve), "data": None})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"code": 500, "message": str(e), "data": None})


@app.route('/api/strategy/fixed-coins/promote', methods=['POST'])
def promote_fixed_coins_route():
    """将选中的浮动币种提升为固定币种。"""
    try:
        data = request.get_json(silent=True) or {}
        coins = data.get('coins', [])
        if not isinstance(coins, list) or not coins:
            return jsonify({"code": 400, "message": "请传入至少一个币种", "data": None})
        promoted = promote_floating_to_fixed(coins)
        msg = (f"已将 {len(promoted)} 个浮动币种提升为固定币种"
               if promoted else "选中币种已在固定列表中，无需提升")
        return jsonify({
            "code": 200,
            "message": msg + (_coin_cfg_warn() if promoted else ''),
            "data": _coin_config_payload()
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({"code": 500, "message": str(e), "data": None})


@app.route('/api/strategy/fixed-coins/sync-csv', methods=['POST'])
def sync_fixed_from_csv_route():
    """以 CSV 为准覆盖固定币种列表（保持 CSV 顺序），浮动币种保持不变。"""
    try:
        new_fixed = sync_fixed_from_csv()
        return jsonify({
            "code": 200,
            "message": f"已用 CSV 覆盖固定列表，共 {len(new_fixed)} 个币种" + _coin_cfg_warn(),
            "data": _coin_config_payload()
        })
    except ValueError as ve:
        return jsonify({"code": 400, "message": str(ve), "data": None})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"code": 500, "message": str(e), "data": None})


@app.route('/api/strategy/fixed-coins/refresh-marketcap', methods=['POST'])
def refresh_fixed_from_marketcap_route():
    """按 CoinGecko 实时市值榜重建固定币种池（Top50，含宇宙 DB/CSV 同步）。

    外网拉取失败时不做任何改动（口径见 market_cap_updater），回包非 200。
    """
    try:
        from .market_cap_updater import refresh_fixed_coins
        result = refresh_fixed_coins()
        label = lambda cs: '、'.join(c.replace('-USDT-SWAP', '') for c in cs) or '无'
        return jsonify({
            "code": 200,
            "message": (f"固定池已更新为市值 Top{result['total']}："
                        f"新增 {len(result['added'])} 个（{label(result['added'])}），"
                        f"移除 {len(result['removed'])} 个（{label(result['removed'])}）")
                       + _coin_cfg_warn(),
            "data": {
                "total": result['total'],
                "added_count": len(result['added']),
                "removed_count": len(result['removed']),
                "added": result['added'],
                "removed": result['removed'],
                "universe_added": result['universe_added'],
                "coin_config": _coin_config_payload(),
            }
        })
    except RuntimeError as re_err:
        # 业务拒绝（市值榜不可达/已有任务在跑）：未发生任何写入
        return jsonify({"code": 400, "message": str(re_err), "data": None})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"code": 500, "message": str(e), "data": None})


@app.route('/api/strategy/fixed-coins/market-detail', methods=['GET'])
def fixed_coins_market_detail_route():
    """固定币种实时明细（市值排名/价格/24H涨跌/成交额/流通市值/流动性代理）。

    ?force=1 跳过 120s 缓存强制重拉 CoinGecko。
    """
    try:
        from .market_cap_updater import fixed_coins_detail, _cache_lock, _snapshot_cache
        if request.args.get('force') in ('1', 'true'):
            with _cache_lock:      # 失效缓存，下一次取数强制走外网
                _snapshot_cache['ts'] = 0.0
        return jsonify({"code": 200, "message": "success", "data": fixed_coins_detail()})
    except RuntimeError as re_err:
        return jsonify({"code": 502, "message": f"行情源暂不可用：{re_err}", "data": None})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"code": 500, "message": str(e), "data": None})


@app.route('/api/strategy/starred', methods=['GET'])
def get_starred():
    """获取星标币种列表"""
    try:
        return jsonify({
            "code": 200,
            "message": "success",
            "data": get_starred_coins()
        })
    except Exception as e:
        return jsonify({
            "code": 500,
            "message": str(e),
            "data": None
        })


@app.route('/api/strategy/starred', methods=['POST'])
def set_starred():
    """保存星标币种列表，同时同步星标行情 CSV"""
    try:
        data = request.get_json()
        coins = data.get('starred_coins', [])
        set_starred_coins(coins)

        # 同步星标行情 CSV
        try:
            from .star_market import sync_from_starred_config
            sync_from_starred_config()
        except Exception:
            pass  # 同步失败不影响主流程

        return jsonify({
            "code": 200,
            "message": "success" + _coin_cfg_warn(),
            "data": get_starred_coins()
        })
    except Exception as e:
        return jsonify({
            "code": 500,
            "message": str(e),
            "data": None
        })


@app.route('/api/strategy/starred/clear', methods=['POST'])
def clear_starred():
    """清除所有星标，同时同步星标行情 CSV"""
    try:
        clear_starred_coins()

        # 同步星标行情 CSV
        try:
            from .star_market import sync_from_starred_config
            sync_from_starred_config()
        except Exception:
            pass

        return jsonify({
            "code": 200,
            "message": "所有星标已清除" + _coin_cfg_warn(),
            "data": []
        })
    except Exception as e:
        return jsonify({
            "code": 500,
            "message": str(e),
            "data": None
        })


@app.route('/api/strategy/clear-selections', methods=['POST'])
def clear_selections():
    """清除所有币种选择状态"""
    try:
        clear_all_selections()
        return jsonify({
            "code": 200,
            "message": "所有选择已清除" + _coin_cfg_warn(),
            "data": get_selected_coins()
        })
    except Exception as e:
        return jsonify({
            "code": 500,
            "message": str(e),
            "data": None
        })


@app.route('/api/strategy/coin/<symbol>')
def get_single_coin_data(symbol):
    try:
        bar = request.args.get('bar', '1H')
        data = calculate_single_coin_data(symbol, bar=bar)
        return jsonify({
            "code": 200,
            "message": "success",
            "data": data
        })
    except Exception as e:
        return jsonify({
            "code": 500,
            "message": str(e),
            "data": None
        })

@app.route('/api/strategy/detail/<symbol>')
def get_strategy_detail_api(symbol):
    """
    策略详情页 API —— 返回完整策略运行结果。
    支持参数：
      - bar:         短周期K线，默认 1H
      - strategy:    策略类型，single / dual / boll / keltner_boll，默认 single
      - long_bar:    长周期（仅dual/boll/keltner_boll使用），默认自动映射
      - entry_price: 开仓价格取值 open/close（仅single/dual），默认 close
      - exit_price:  平仓价格取值 open/close（仅single/dual），默认 open
      - signal_algo: 短周期信号算法 diff（平滑差，灵敏）/hybrid（状态机，稳定）（仅single/dual），默认 diff
      - trend_indicator: 长周期趋势方向方法名（仅keltner_boll），默认 keltner
      - entry_atr_mult:  开仓轨 ATR 外扩倍数（仅keltner_boll），默认 0.0
      - exit_atr_mult:   平仓轨 ATR 外扩倍数（仅keltner_boll），默认 0.0
      - atr_period:      外扩用 ATR 周期（仅keltner_boll），默认 14
    """
    try:
        bar = request.args.get('bar', '1H')
        strategy = request.args.get('strategy', 'single')
        long_bar = request.args.get('long_bar', None)
        entry_price = request.args.get('entry_price', None)
        exit_price = request.args.get('exit_price', None)
        signal_algo = request.args.get('signal_algo', None)
        trend_indicator = request.args.get('trend_indicator', None)
        entry_atr_mult = request.args.get('entry_atr_mult', None)
        exit_atr_mult = request.args.get('exit_atr_mult', None)
        atr_period = request.args.get('atr_period', None)
        data = get_strategy_detail(symbol, bar=bar, strategy=strategy, long_bar=long_bar,
                                   entry_price=entry_price, exit_price=exit_price,
                                   signal_algo=signal_algo, trend_indicator=trend_indicator,
                                   entry_atr_mult=entry_atr_mult, exit_atr_mult=exit_atr_mult,
                                   atr_period=atr_period)
        return jsonify({
            "code": 200,
            "message": "success",
            "data": data
        })
    except Exception as e:
        traceback.print_exc()
        # 网络类异常（TLS握手超时/连接重置等）的 str(e) 常为空，
        # 统一用 {type}: {e!r} 兄底，并对网络错误给出友好提示，避免前端显示空白错误。
        err_name = type(e).__name__
        if any(k in err_name for k in ('Timeout', 'Connect', 'Protocol', 'SSL', 'Network', 'Read')):
            msg = f"网络连接不稳定，获取 {symbol} 行情数据超时，请稍后重试（{err_name}）"
        else:
            msg = str(e) or f"{err_name}: {e!r}"
        return jsonify({
            "code": 500,
            "message": msg,
            "data": None
        })

# =====================================================================
# 【批量多周期趋势分析 API】
# =====================================================================

@app.route('/api/batch/update', methods=['POST'])
def batch_update():
    """启动批量多周期趋势更新（后台线程）"""
    try:
        from .batch_trend_updater import BatchTrendAnalyzer, BatchAlreadyRunning, get_progress

        analyzer = BatchTrendAnalyzer()
        try:
            analyzer.run_in_background()
        except BatchAlreadyRunning as exc:
            return jsonify({"code": 409, "message": str(exc), "data": get_progress()}), 409

        return jsonify({
            "code": 200,
            "message": "批量趋势分析已启动",
            "data": get_progress(),
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({
            "code": 500,
            "message": "批量任务启动失败: %s" % e,
            "data": None
        }), 500


def _batch_stream_cursor():
    """解析断点；浏览器自动重连时优先使用 Last-Event-ID。"""
    run_id = request.args.get('run_id')
    raw_cursor = request.args.get('cursor', '0')
    event_id = request.headers.get('Last-Event-ID')
    if event_id:
        run_id, raw_cursor = event_id.rsplit(':', 1)
    cursor = int(raw_cursor)
    if cursor < 0 or cursor > 2 ** 63 - 1 or (run_id is not None and len(run_id) > 64):
        raise ValueError('日志游标无效')
    return cursor, run_id


@app.route('/api/batch/progress', methods=['GET'])
def batch_progress():
    """查询批量更新进度"""
    try:
        from .batch_trend_updater import get_progress, get_batch_snapshot
        cursor, run_id = _batch_stream_cursor()
        data = (get_batch_snapshot(cursor, run_id) if request.args.get('with_logs') == '1'
                else get_progress())
        response = jsonify({"code": 200, "message": "success", "data": data})
        response.headers['Cache-Control'] = 'no-store'
        return response
    except ValueError:
        return jsonify({"code": 400, "message": "日志游标无效", "data": None}), 400
    except Exception as e:
        traceback.print_exc()
        return jsonify({"code": 500, "message": str(e), "data": None}), 500


@app.route('/api/batch/log/stream')
def batch_log_stream():
    """SSE 实时推送批量更新进度与日志行（替代前端轮询）。

    前端 EventSource 连接后即可收到：
      event: progress  → JSON（status/current/total/progress_pct/elapsed_seconds/message）
      event: log       → JSON（seq/ts/level/msg）逐行日志
    当批量任务 completed 或 error 时终止推送（关闭连接）。
    """
    import json as _json
    import time as _time
    from .batch_trend_updater import get_batch_snapshot

    try:
        initial_cursor, initial_run_id = _batch_stream_cursor()
    except ValueError:
        return jsonify({"code": 400, "message": "日志游标无效"}), 400

    def _generate():
        cursor, run_id = initial_cursor, initial_run_id
        yield 'retry: 2000\n\n'
        while True:
            # 同一个快照的终态与尾日志不可分割；先通知轮次，再发日志，最后 done。
            p = get_batch_snapshot(cursor, run_id)
            new_lines = p.pop('logs')
            cursor, run_id = p['cursor'], p['run_id']
            yield 'event: progress\ndata: %s\n\n' % _json.dumps(p, ensure_ascii=False)
            for line in new_lines:
                yield 'id: %s:%d\nevent: log\ndata: %s\n\n' % (
                    run_id, line['seq'], _json.dumps(line, ensure_ascii=False))
            if p['status'] in ('completed', 'error', 'idle'):
                yield 'event: done\ndata: %s\n\n' % _json.dumps(p, ensure_ascii=False)
                return
            # 即使任务阻塞在下载或策略锁上，进度帧也充当连接心跳。
            _time.sleep(1)

    return Response(
        stream_with_context(_generate()),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',  # Nginx 不缓冲
            'Connection': 'keep-alive',
        },
    )


@app.route('/api/batch/csv-data', methods=['GET'])
def batch_csv_data():
    """获取完整 CSV 数据（用于表格展示）"""
    try:
        from .batch_trend_updater import read_csv_for_display
        records = read_csv_for_display()

        # 统计
        up_count_15m = sum(1 for r in records if r.get('15m_趋势') == '上涨')
        down_count_15m = sum(1 for r in records if r.get('15m_趋势') == '下跌')
        up_count_1h = sum(1 for r in records if r.get('1H_趋势') == '上涨')
        down_count_1h = sum(1 for r in records if r.get('1H_趋势') == '下跌')
        up_count_4h = sum(1 for r in records if r.get('4H_趋势') == '上涨')
        down_count_4h = sum(1 for r in records if r.get('4H_趋势') == '下跌')
        up_count_1d = sum(1 for r in records if r.get('1D_趋势') == '上涨')
        down_count_1d = sum(1 for r in records if r.get('1D_趋势') == '下跌')
        wait_count = len(records) - up_count_1d - down_count_1d

        # 一致性统计
        consistent = [r for r in records
                      if r.get('4H_趋势') in ('上涨', '下跌')
                      and r.get('4H_趋势') == r.get('1D_趋势')]

        return jsonify({
            "code": 200,
            "message": "success",
            "data": {
                "records": records,
                "total": len(records),
                "statistics": {
                    "up_count_15m": up_count_15m,
                    "down_count_15m": down_count_15m,
                    "up_count_1h": up_count_1h,
                    "down_count_1h": down_count_1h,
                    "up_count_4h": up_count_4h,
                    "down_count_4h": down_count_4h,
                    "up_count_1d": up_count_1d,
                    "down_count_1d": down_count_1d,
                    "wait_count": wait_count,
                    "consistent_count": len(consistent),
                },
            },
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({
            "code": 500,
            "message": str(e),
            "data": None
        })


@app.route('/api/batch/csv-filtered', methods=['GET'])
def batch_csv_filtered():
    """获取筛选后的一致性币种数据（4H 和 1D 趋势相同）"""
    try:
        from .batch_trend_updater import get_filtered_records
        records = get_filtered_records()
        return jsonify({
            "code": 200,
            "message": "success",
            "data": {
                "records": records,
                "total": len(records),
            },
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({
            "code": 500,
            "message": str(e),
            "data": None
        })

@app.route('/api/strategy/multi-period/<symbol>')
def get_multi_period_data(symbol):
    """
    多周期方向总览 API —— 返回指定币种在多个周期的趋势方向。
    支持 ?bars=5m,15m,1H,2H,6H,1D,1W 参数指定需要查询的周期列表。
    """
    try:
        bars_param = request.args.get('bars', '5m,15m,1H,2H,6H,1D,1W')
        bars = [b.strip() for b in bars_param.split(',') if b.strip()]
        data = calculate_multi_period_data(symbol, bars=bars)
        return jsonify({
            "code": 200,
            "message": "success",
            "data": data
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({
            "code": 500,
            "message": str(e),
            "data": None
        })


# =====================================================================
# 星标币种行情管理 API
# =====================================================================

@app.route('/star-market')
def star_market_page():
    """星标币种行情展示页"""
    return render_template('star_market.html', active_page='star-market')


@app.route('/api/star-market/data', methods=['GET'])
def star_market_data():
    """获取星标币种行情数据"""
    try:
        from .star_market import get_data_for_display
        data = get_data_for_display()
        return jsonify({
            'code': 200,
            'message': 'success',
            'data': data
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({
            'code': 500,
            'message': str(e),
            'data': None
        })


@app.route('/api/star-market/refresh', methods=['POST'])
def star_market_refresh():
    """刷新星标币种行情（后台异步执行）"""
    try:
        from .star_market import refresh_full_data, get_refresh_progress

        # 检查是否已有任务在运行
        progress = get_refresh_progress()
        if progress['status'] == 'running':
            return jsonify({
                'code': 200,
                'message': '已有刷新任务正在运行中',
                'data': progress
            })

        refresh_full_data()  # 立即启动后台线程

        return jsonify({
            'code': 200,
            'message': '刷新任务已在后台启动',
            'data': get_refresh_progress()
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({
            'code': 500,
            'message': str(e),
            'data': None
        })


@app.route('/api/star-market/refresh-progress', methods=['GET'])
def star_market_refresh_progress():
    """查询星标行情刷新进度（轮询用）"""
    try:
        from .star_market import get_refresh_progress
        progress = get_refresh_progress()
        return jsonify({
            'code': 200,
            'message': 'success',
            'data': progress
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({
            'code': 500,
            'message': str(e),
            'data': None
        })


@app.route('/api/star-market/update', methods=['POST'])
def star_market_update():
    """更新星标币种指定字段（预测涨跌、建议操作）"""
    try:
        from .star_market import update_record
        data = request.get_json()
        if not data:
            return jsonify({'code': 400, 'message': '请求数据为空', 'data': None})

        code = data.get('code', '').strip()
        if not code:
            return jsonify({'code': 400, 'message': '缺少 code 参数', 'data': None})

        updates = data.get('updates', {})
        if not updates:
            return jsonify({'code': 400, 'message': '缺少 updates 字段', 'data': None})

        result = update_record(code, updates)

        if result['status'] == 'success':
            return jsonify({'code': 200, 'message': result['message'], 'data': result})
        else:
            return jsonify({'code': 400, 'message': result['message'], 'data': result})
    except Exception as e:
        traceback.print_exc()
        return jsonify({
            'code': 500,
            'message': str(e),
            'data': None
        })


@app.route('/api/star-market/clear-data', methods=['POST'])
def star_market_clear_data():
    """清空所有币种除名称、代码外的其他列数据"""
    try:
        from .star_market import clear_all_data
        result = clear_all_data()

        if result['status'] == 'success':
            return jsonify({'code': 200, 'message': result['message'], 'data': result})
        else:
            return jsonify({'code': 400, 'message': result['message'], 'data': result})
    except Exception as e:
        traceback.print_exc()
        return jsonify({
            'code': 500,
            'message': str(e),
            'data': None
        })


@app.route('/api/star-market/status', methods=['GET'])
def star_market_status():
    """获取最近刷新状态"""
    try:
        from .star_market import get_refresh_status
        status = get_refresh_status()
        return jsonify({
            'code': 200,
            'message': 'success',
            'data': status
        })
    except Exception as e:
        return jsonify({
            'code': 500,
            'message': str(e),
            'data': None
        })


@app.route('/api/star-market/export/<fmt>')
def star_market_export(fmt):
    """导出星标币种行情数据
    fmt: csv 或 xlsx
    """
    try:
        from .star_market import export_as_csv, export_as_xlsx

        if fmt == 'csv':
            content, filename = export_as_csv()
            if content is None:
                return jsonify({'code': 500, 'message': filename or '导出失败', 'data': None})
            return send_file(
                io.BytesIO(content),
                mimetype='text/csv',
                as_attachment=True,
                download_name=filename
            )

        elif fmt == 'xlsx':
            content, filename_or_err = export_as_xlsx()
            if content is None:
                return jsonify({'code': 500, 'message': filename_or_err, 'data': None})
            return send_file(
                io.BytesIO(content),
                mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                as_attachment=True,
                download_name=filename_or_err
            )

        else:
            return jsonify({'code': 400, 'message': f'不支持的导出格式: {fmt}', 'data': None})

    except Exception as e:
        traceback.print_exc()
        return jsonify({
            'code': 500,
            'message': str(e),
            'data': None
        })


@app.route('/api/star-market/refresh-trend', methods=['POST'])
def star_market_refresh_trend():
    """刷新指定周期的趋势方向列（15分钟或60分钟）"""
    try:
        from .star_market import refresh_trend_column
        data = request.get_json()
        if not data:
            return jsonify({'code': 400, 'message': '请求数据为空', 'data': None})

        bar = data.get('bar', '').strip()
        col = data.get('col', '').strip()
        if not bar or not col:
            return jsonify({'code': 400, 'message': '缺少 bar 或 col 参数', 'data': None})

        # 校验支持的周期
        valid_bars = {'15m': '15分钟', '1H': '60分钟'}
        if bar not in valid_bars or valid_bars[bar] != col:
            return jsonify({'code': 400, 'message': f'不支持的周期: bar={bar}, col={col}', 'data': None})

        result = refresh_trend_column(bar, col)
        return jsonify({
            'code': 200,
            'message': result.get('message', '刷新完成'),
            'data': result
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({
            'code': 500,
            'message': str(e),
            'data': None
        })


@app.route('/api/star-market/reorder', methods=['POST'])
def star_market_reorder():
    """保存拖拽排序后的币种顺序"""
    try:
        from .star_market import reorder_records
        data = request.get_json()
        if not data:
            return jsonify({'code': 400, 'message': '请求数据为空', 'data': None})

        ordered_codes = data.get('ordered_codes', [])
        if not ordered_codes:
            return jsonify({'code': 400, 'message': '缺少 ordered_codes 参数', 'data': None})

        result = reorder_records(ordered_codes)
        if result['status'] == 'success':
            return jsonify({'code': 200, 'message': result['message'], 'data': result})
        else:
            return jsonify({'code': 400, 'message': result['message'], 'data': result})
    except Exception as e:
        traceback.print_exc()
        return jsonify({
            'code': 500,
            'message': str(e),
            'data': None
        })


@app.route('/api/star-market/sync', methods=['POST'])
def star_market_sync():
    """手动触发星标行情 CSV 与监控台星标列表同步"""
    try:
        from .star_market import sync_from_starred_config
        result = sync_from_starred_config()

        if result['status'] == 'success':
            return jsonify({'code': 200, 'message': result['message'], 'data': result})
        else:
            return jsonify({'code': 400, 'message': result['message'], 'data': result})
    except Exception as e:
        traceback.print_exc()
        return jsonify({
            'code': 500,
            'message': str(e),
            'data': None
        })


# =====================================================================
# 全市场涨跌 / 成交量排行 + 趋势识别扫描 API
# =====================================================================

@app.route('/market-scan')
def market_scan_page():
    """全市场趋势扫描页（涨幅榜 / 跌幅榜 / 成交额榜 / 明确趋势币种）"""
    return render_template('market_scan.html', active_page='market-scan')


@app.route('/api/market-scan/data', methods=['GET'])
def market_scan_data():
    """获取最近一次扫描结果（缓存）。首次访问返回 has_data=False，由前端触发扫描"""
    try:
        from .market_scanner import get_scan_result, get_scan_progress
        result = get_scan_result()
        return jsonify({
            'code': 200,
            'message': 'success',
            'data': {
                'has_data': result is not None,
                'result': result,
                'progress': get_scan_progress(),
            }
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({'code': 500, 'message': str(e), 'data': None})


@app.route('/api/market-scan/scan', methods=['POST'])
def market_scan_start():
    """启动一次全市场扫描（后台异步执行）"""
    try:
        from .market_scanner import run_scan_background, get_scan_progress
        data = request.get_json(silent=True) or {}
        account = (data.get('account') or '').strip() or None
        run_scan_background(account)
        return jsonify({
            'code': 200,
            'message': '扫描任务已在后台启动',
            'data': get_scan_progress()
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({'code': 500, 'message': str(e), 'data': None})


@app.route('/api/market-scan/progress', methods=['GET'])
def market_scan_progress():
    """查询扫描进度（轮询用）"""
    try:
        from .market_scanner import get_scan_progress
        return jsonify({
            'code': 200,
            'message': 'success',
            'data': get_scan_progress()
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({'code': 500, 'message': str(e), 'data': None})


@app.route('/api/market-scan/rankings', methods=['POST'])
def market_scan_rankings():
    """仅获取三大排行榜（涨幅/跌幅/成交额），不做趋势扫描。

    只发 1 次 get_tickers 请求 + 内存排序，速度快，同步返回结果并写入缓存。
    """
    try:
        from .market_scanner import get_market_rankings, set_scan_result
        data = request.get_json(silent=True) or {}
        account = (data.get('account') or '').strip() or None
        result = get_market_rankings(account)
        set_scan_result(result)
        return jsonify({
            'code': 200,
            'message': '排行榜获取完成',
            'data': result
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({'code': 500, 'message': str(e), 'data': None})


@app.route('/api/market-scan/thresholds', methods=['GET'])
def market_scan_thresholds():
    """返回当前趋势扫描的筛选参数（供「筛选条件」弹窗展示）"""
    try:
        from .market_scanner import get_scan_thresholds
        return jsonify({
            'code': 200,
            'message': 'success',
            'data': get_scan_thresholds()
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({'code': 500, 'message': str(e), 'data': None})


# =====================================================================
# 定时任务管理 API
# =====================================================================

@app.route('/api/task/status', methods=['GET'])
def task_status():
    """获取调度器状态和任务列表"""
    try:
        from .task.scheduler import task_scheduler
        return jsonify({
            'code': 200,
            'message': 'success',
            'data': task_scheduler.get_status()
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({'code': 500, 'message': str(e), 'data': None})


@app.route('/api/task/start', methods=['POST'])
def task_start():
    """启动调度器"""
    try:
        from .task.scheduler import task_scheduler
        task_scheduler.start()
        return jsonify({'code': 200, 'message': '调度器已启动', 'data': None})
    except Exception as e:
        return jsonify({'code': 500, 'message': str(e), 'data': None})


@app.route('/api/task/stop', methods=['POST'])
def task_stop():
    """停止调度器"""
    try:
        from .task.scheduler import task_scheduler
        task_scheduler.shutdown()
        return jsonify({'code': 200, 'message': '调度器已停止', 'data': None})
    except Exception as e:
        return jsonify({'code': 500, 'message': str(e), 'data': None})


@app.route('/api/task/job/<job_id>', methods=['DELETE'])
def task_remove_job(job_id):
    """移除指定任务"""
    try:
        from .task.scheduler import task_scheduler
        ok = task_scheduler.remove_job(job_id)
        if ok:
            return jsonify({'code': 200, 'message': f'任务 {job_id} 已移除', 'data': None})
        else:
            return jsonify({'code': 404, 'message': f'任务 {job_id} 不存在', 'data': None})
    except Exception as e:
        return jsonify({'code': 500, 'message': str(e), 'data': None})


@app.route('/api/task/job/<job_id>/switch', methods=['POST'])
def task_switch_job(job_id):
    """单个定时任务开关（持久化到 kv_store，即点即生效、重启不丢）。

    关：从调度器移除任务（register_job 守卫也会拦住后续热重注册）；
    开：重跑默认注册（幂等 replace_existing，只把开关允许的任务重新拉起）。
    实盘交易不参与此开关（页面上有启停按钮与重启自愈开关）。
    注意 config.json task_switches 是单向急停：文件里写了 false 的任务，
    网页打开也不会生效，必须改回文件（返回消息里会明说）。
    """
    try:
        from .task.scheduler import task_scheduler, SWITCHABLE_JOB_LABELS, set_job_switch
        if job_id not in SWITCHABLE_JOB_LABELS:
            return jsonify({'code': 400,
                            'message': f'任务 {job_id} 不支持开关（实盘交易用页面上的启停按钮）',
                            'data': None})
        payload = request.get_json(silent=True) or {}
        if 'enabled' not in payload:
            return jsonify({'code': 400, 'message': '缺少 enabled 参数', 'data': None})
        enabled = bool(payload.get('enabled'))
        set_job_switch(job_id, enabled)
        if enabled:
            from .task.scheduler import register_default_jobs
            register_default_jobs()
            still_off = job_id not in {
                j['id'] for j in task_scheduler.get_all_jobs() if j.get('active') or j.get('paused')}
            msg = (f'任务 {job_id} 已重新开启' if not still_off else
                   f'开关已打开，但 {job_id} 未注册成功（检查 config.json 文件急停或任务自身开关）')
        else:
            task_scheduler.remove_job(job_id)
            msg = f'任务 {job_id} 已关闭（重启后仍保持关闭）'
        return jsonify({'code': 200, 'message': msg,
                        'data': task_scheduler.get_status().get('jobs')})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'code': 500, 'message': str(e), 'data': None})


@app.route('/api/task/switches/global', methods=['POST'])
def task_switch_global():
    """定时任务全局总开关：关=停掉全部后台定时任务（不含实盘交易线程），
    持久化 kv_store；开=重跑默认注册恢复各任务自身开关允许的部分。"""
    try:
        from .task.scheduler import task_scheduler, set_global_switch, SWITCHABLE_JOB_LABELS
        payload = request.get_json(silent=True) or {}
        if 'enabled' not in payload:
            return jsonify({'code': 400, 'message': '缺少 enabled 参数', 'data': None})
        enabled = bool(payload.get('enabled'))
        set_global_switch(enabled)
        if enabled:
            from .task.scheduler import register_default_jobs
            register_default_jobs()
            msg = '定时任务全局开关已打开（各任务按其自身开关恢复）'
        else:
            for job_id in SWITCHABLE_JOB_LABELS:
                task_scheduler.remove_job(job_id)
            msg = '定时任务全局开关已关闭：后台任务已全部停止（实盘交易线程不受影响）'
        return jsonify({'code': 200, 'message': msg,
                        'data': task_scheduler.get_status().get('jobs')})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'code': 500, 'message': str(e), 'data': None})


# =====================================================================
# 配置管理 API — 交易配置
# =====================================================================
import os as _os
import json as _json
import re as _re

_TRADING_CONFIG_PATH = _os.path.join(
    _os.path.dirname(__file__), 'task', 'config', 'config_trend_range.json'
)

_EMAIL_CONFIG_PATH = _os.path.join(
    _os.path.dirname(__file__), 'task', 'config', 'email_config.py'
)


def _load_trading_config(account=None):
    """读取交易配置：按账号 DB 优先（专属 strategy_config:{account} → 全局），
    带 TTL 缓存减少重复往返；DB 不可用/无数据时回退本地文件。

    account 为空时取当前实盘运行账号（_resolve_task_account），保证网页未显式
    指定账号时也与实盘同源；多账号共用一个库时各读各的、互不串味。"""
    if not account:
        account = _resolve_task_account()
    try:
        from . import config_store_repo
        _cfg = config_store_repo.load_strategy_config_cached(account)
        if _cfg is not None:
            return _cfg
    except Exception:
        pass
    with open(_TRADING_CONFIG_PATH, 'r', encoding='utf-8') as f:
        return _json.load(f)


def _save_trading_config(config, account=None):
    """写入交易配置：按账号 DB 主存（strategy_config:{account}）+ 本地文件双写。

    account 为空时取当前实盘运行账号（_resolve_task_account）。按账号存是多账号
    共用一个库不串味的关键：网页给哪个账号改配置，就写进哪个账号的专属 key，
    不会覆盖别的账号（本地测试账号改 1U 不会动到实盘主账号的 20U）。

    返回 (ok, msg)：ok=False 表示改动根本不会生效，调用方必须按失败上报。

    DB 写失败不能再静默吞掉：读取侧（TrendRangeTrader._load_config 与各接口）
    都是 DB 优先，所以“写库失败 + 写文件成功”= 界面提示已保存、实盘继续用
    旧杠杆/旧周期跑单。失败时再探一次读，区分两种实质不同的情况：
    - DB 还读得到（库里仍是旧值）→ 本次改动不会生效 → ok=False；
    - DB 整个读不到（库挂了）→ 交易端会自动回退读文件 → 改动其实生效，
      仅属持久化降级 → ok=True + 降级提醒。
    """
    if not account:
        account = _resolve_task_account()
    from .task.utils.logger import get_task_logger
    _log = get_task_logger()
    db_err = None
    _key = None
    try:
        from .database import session_scope
        from . import config_store_repo
        _key = config_store_repo.strategy_config_key(account)
        with session_scope() as _s:
            config_store_repo.save_json_config(_s, _key, config)
    except Exception as e:
        db_err = e
        _log.error(f"[Config] 交易配置写入数据库失败(account={account}): {e!r}")

    # 文件兜底照写（与历史行为一致）；写失败则直接抛出给接口的 except 分支
    with open(_TRADING_CONFIG_PATH, 'w', encoding='utf-8') as f:
        _json.dump(config, f, indent=2, ensure_ascii=False)

    if db_err is None:
        return True, None

    # 写失败后重读一次（save_json_config 已在提交前失效缓存，这里不会命中旧缓存）：
    # 读得到 = DB 健康但本次写没成功，库里仍是旧参数
    try:
        from . import config_store_repo as _csr
        probe = _csr.load_json_config_cached(_key or _csr.KEY_STRATEGY_CONFIG)
    except Exception:
        probe = None
    if probe is not None:
        return False, (f'交易配置未能写入数据库主存（{db_err}），'
                       f'本地文件已更新但交易端仍会读到旧参数，请修复数据库后重新保存')
    return True, '数据库不可用，本次仅写入本地文件兜底，请尽快修复数据库'


@app.route('/api/task/config/trading', methods=['GET'])
def get_trading_config():
    """获取交易配置（按账号：?account=xxx，缺省用当前实盘运行账号）"""
    try:
        account = (request.args.get('account') or '').strip() or None
        config = _load_trading_config(account)
        return jsonify({'code': 200, 'message': 'success', 'data': config})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'code': 500, 'message': str(e), 'data': None})


@app.route('/api/task/range-strategy-options', methods=['GET'])
def get_range_strategy_options():
    """B 仓位（区间波动）策略引擎可选项：策略名 + 长周期趋势方向方法列表。

    供交易配置页 task.html 的 B 仓位策略下拉动态填充，避免前端硬编码方法名
    与 keltner_boll 注册表漂移。
    """
    try:
        import os as _os, sys as _sys
        _sdir = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), 'strategy')
        if _sdir not in _sys.path:
            _sys.path.insert(0, _sdir)
        from keltner_boll import list_indicators, DEFAULT_TREND_INDICATOR
        return jsonify({'code': 200, 'message': 'success', 'data': {
            'strategies': [
                {'value': 'keltner_boll', 'label': 'Keltner×BOLL（ATR外扩+趋势方向过滤）'},
                {'value': 'boll', 'label': '纯布林带（存量）'},
            ],
            'direction_sources': [
                {'value': 'pro3', 'label': 'Pro3长周期方向（存量：keltner仅外扩边界+闸门）'},
                {'value': 'keltner', 'label': 'keltner_boll独立方向（与回测同源信号引擎）'},
            ],
            'trend_indicators': list_indicators(),
            'default_strategy': 'keltner_boll',
            'default_direction_source': 'pro3',
            'default_trend_indicator': DEFAULT_TREND_INDICATOR,
        }})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'code': 500, 'message': str(e), 'data': None})


@app.route('/api/task/contract-spec', methods=['GET'])
def get_contract_spec():
    """查询永续合约规格与金额⇄张数换算（公共接口，免鉴权）

    参数:
        instId:    合约ID（必填，如 GPS-USDT-SWAP）
        usd:       占用保证金预算（可选）→ 按 张数=floor(金额×杠杆÷每张面值) 返回合规张数
        contracts: 张数（可选）→ 返回对应持仓名义价值
        leverage:  杠杆倍数（可选，缺省 1x；0/非法值按 1x），金额→张数为放大因子
    返回 data: spec(ctVal/lotSz/minSz/tickSz) + price + contract_usd_value(每张面值USDT)
               + leverage + contracts/notional_usd(持仓价值)/margin_usd(占用保证金) 换算结果
    """
    try:
        inst_id = request.args.get('instId', '').strip()
        if not inst_id:
            return jsonify({'code': 400, 'message': '缺少 instId 参数', 'data': None})

        from .api_config import get_api_config
        from .task.utils.instrument_spec import get_instrument_spec_cache
        import okx.MarketData as _MarketData

        flag = str(get_api_config().get('flag', '0') or '0')
        cache = get_instrument_spec_cache(flag)

        # 现价（公共行情接口，失败不阻断，仅换算结果为 0）
        price = 0.0
        try:
            _mkt_api = _MarketData.MarketAPI(flag=flag)
            t = _rl_read('market_ticker', _mkt_api.get_ticker, instId=inst_id)
            if t and t.get('code') == '0' and t.get('data'):
                price = float(t['data'][0].get('last', 0) or 0)
        except Exception:
            pass

        kwargs = {}
        usd_arg = request.args.get('usd')
        contracts_arg = request.args.get('contracts')
        leverage_arg = request.args.get('leverage')
        if usd_arg is not None:
            kwargs['usd'] = float(usd_arg or 0)
        elif contracts_arg is not None:
            kwargs['contracts'] = float(contracts_arg or 0)
        if leverage_arg is not None:
            try:
                kwargs['leverage'] = float(leverage_arg or 0)
            except (TypeError, ValueError):
                kwargs['leverage'] = 1
        data = cache.preview(inst_id, price=price, **kwargs)
        if not data.get('spec'):
            return jsonify({'code': 404,
                            'message': f'{inst_id} 合约规格获取失败，请检查合约ID是否存在',
                            'data': data})
        return jsonify({'code': 200, 'message': 'success', 'data': data})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'code': 500, 'message': str(e), 'data': None})


@app.route('/api/task/trend-compare', methods=['GET'])
def task_trend_compare():
    """实盘 vs 趋势策略理论对比

    参数:
        instId: 合约ID（必填，须在交易配置 currencies 中存在）
        days:   对比时间范围（近N天，默认1，支持 1/3/7）
        start/end: 'YYYY-MM-DD HH:MM:SS'，显式指定时覆盖 days
    策略参数（周期/模式/算法/取价/杠杆）自动从交易配置读取，保证与实盘同源。
    """
    try:
        inst_id = request.args.get('instId', '').strip()
        if not inst_id:
            return jsonify({'code': 400, 'message': '缺少 instId 参数', 'data': None})

        config = _load_trading_config()
        cur = next((c for c in config.get('currencies', [])
                    if c.get('instId') == inst_id), None)
        if not cur:
            return jsonify({'code': 404, 'message': f'交易配置中不存在币种 {inst_id}', 'data': None})

        start = request.args.get('start') or None
        end = request.args.get('end') or None
        if not start:
            try:
                days = max(1, min(30, int(request.args.get('days', 1))))
            except (TypeError, ValueError):
                days = 1
            start = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d %H:%M:%S')

        # 惰性导入：对比服务首次调用才加载策略模块，避免拖慢应用启动
        from .task.trend_compare import get_compare_service
        result = get_compare_service().compare(
            inst_id=inst_id,
            short_period=cur.get('short_period', '5m'),
            long_period=cur.get('long_period', '4H'),
            period_mode=(cur.get('trend_position') or {}).get('period_mode', 'dual'),
            signal_algo=cur.get('signal_algo'),
            entry_price_type=cur.get('entry_price_type', 'close'),
            exit_price_type=cur.get('exit_price_type', 'open'),
            leverage=float(cur.get('leverage', 10) or 10),
            start=start, end=end,
        )
        return jsonify({'code': 200, 'message': 'success', 'data': result})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'code': 500, 'message': str(e), 'data': None})


# =============================================================================
# 实盘分析记录 API（手动快照 + 个人判断 + 事后复盘统计）
# =============================================================================

_ANALYSIS_JUDGMENTS = ('rise', 'fall', 'watch')


def _clean_plan_pair(body, price, judgment):
    """解析并校验结构价计划字段（批次16-B）：target_price / invalid_price。

    返回 (target, invalid, err_msg)；两者都未填/为空 → (None, None, None) 不挂计划。
    规则：成对填写；仅 rise/fall 可挂计划（计划与方向无关，先触哪边哪边说了算）；
    数值 >0 且均不等于快照价；目标 ≠ 失效。
    """
    def _num(key):
        v = body.get(key)
        if v is None or str(v).strip() == '':
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return 'bad'
    t, i = _num('target_price'), _num('invalid_price')
    if t == 'bad' or i == 'bad':
        return None, None, '结构价必须是数字'
    if t is None and i is None:
        return None, None, None
    if t is None or i is None:
        return None, None, '结构价需成对填写：目标价与失效价都要有'
    if t <= 0 or i <= 0:
        return None, None, '结构价必须大于 0'
    if t == i:
        return None, None, '目标价与失效价不能相同'
    if price and (t == float(price) or i == float(price)):
        return None, None, '结构价不能等于快照价'
    if judgment == 'watch':
        return None, None, '仅涨/跌判断可挂结构价计划（观望请看情况改判后再挂）'
    return t, i, None


def _analysis_grace_minutes() -> int:
    """分析纪律宽限期（分钟）：决定一条分析记录的 source 判为 live 还是 backfill。

    |now - ts| <= 宽限期 → live（当时就分析了），否则 backfill（事后补记）。
    判定完全在服务端，前端不可指定 source，避免为绕打卡闸门而伪造时间。
    """
    try:
        from . import discipline_repo
        return int(discipline_repo.load_config().get('grace_minutes', 15) or 15)
    except Exception:
        return 15


@app.route('/api/task/analysis/snapshot', methods=['GET'])
def task_analysis_snapshot():
    """生成分析快照：实时价格 + 长短周期方向（复用实盘同源的双周期策略引擎）

    参数:
        instId: 合约ID（必填，须在交易配置 currencies 中存在）
        account: 交易账号（可选，缺省用当前实盘运行账号/默认账号，与币种列表同源）
    策略参数（周期/算法/取价/BOLL）自动从该币种交易配置读取，保证与实盘同源。
    """
    try:
        inst_id = request.args.get('instId', '').strip()
        if not inst_id:
            return jsonify({'code': 400, 'message': '缺少 instId 参数', 'data': None})

        account = (request.args.get('account') or '').strip() or None
        config = _load_trading_config(account)
        cur = next((c for c in config.get('currencies', [])
                    if c.get('instId') == inst_id), None)
        if not cur:
            return jsonify({'code': 404, 'message': f'交易配置中不存在币种 {inst_id}', 'data': None})

        short_period = cur.get('short_period', '5m')
        long_period = cur.get('long_period', '4H')
        rcfg = cur.get('range_position') or {}

        # 惰性导入：首次调用才加载策略模块，避免拖慢应用启动
        from .task.strategy_adapter import DualPeriodStrategyAdapter
        analysis = DualPeriodStrategyAdapter().analyze(
            inst_id, short_period, long_period,
            boll_period=int(rcfg.get('boll_period', 20) or 20),
            boll_dev=float(rcfg.get('boll_dev', 2.0) or 2.0),
            signal_algo=cur.get('signal_algo'),
            entry_price_type=cur.get('entry_price_type', 'close'),
            exit_price_type=cur.get('exit_price_type', 'open'))
        if not analysis:
            return jsonify({'code': 500, 'message': f'{inst_id} 策略分析失败，请稍后重试', 'data': None})

        return jsonify({'code': 200, 'message': 'success', 'data': {
            'instId': inst_id,
            'ts': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'price': analysis.get('last_price', 0),
            'short_period': short_period,
            'long_period': long_period,
            'short_dir': analysis.get('direction') or '',
            'long_dir': analysis.get('long_direction') or '',
            'long_dir_prev': analysis.get('long_prev_direction') or '',
            'atr_pct': analysis.get('atr_percentage', 0),
            'boll_upper': analysis.get('boll_upper', 0),
            'boll_middle': analysis.get('boll_middle', 0),
            'boll_lower': analysis.get('boll_lower', 0),
        }})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'code': 500, 'message': str(e), 'data': None})


@app.route('/api/task/analysis/snapshot_batch', methods=['GET'])
def task_analysis_snapshot_batch():
    """批量生成分析快照：遍历交易配置中全部币种，一次返回所有快照。

    为何串行而不是线程池并发：DualPeriodStrategyAdapter.analyze() 内部会
    临时改写 pro3_singletimeframe 的模块级全局变量（FAST_MODE / PRINT_* 系列）
    并在 finally 恢复，多线程并发会造成全局状态竞争与恢复错乱，核心策略引擎
    的 backtrader/pandas 调用未验证线程安全。串行总耗时 ≈ N × 单币耗时，但
    相比前端逐个发 8 次请求仍是显著改善（省去 8 次 HTTP 往返与 UI 状态切换），
    且实现简单可靠。

    返回: data.items 为快照数组（字段与单币快照一致），data.errors 为失败币种。
    可选参数 account：指定交易账号（与币种列表/单币快照同源），缺省用运行/默认账号。
    """
    try:
        account = (request.args.get('account') or '').strip() or None
        config = _load_trading_config(account)
        currencies = [c for c in config.get('currencies', []) if str(c.get('instId') or '').strip()]
        if not currencies:
            return jsonify({'code': 400, 'message': '交易配置中无币种', 'data': None})

        from .task.strategy_adapter import DualPeriodStrategyAdapter
        adapter = DualPeriodStrategyAdapter()  # 复用同一实例，避免每次重复模块导入
        ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        items = []
        errors = []
        for cur in currencies:
            inst_id = str(cur.get('instId') or '').strip()
            try:
                short_period = cur.get('short_period', '5m')
                long_period = cur.get('long_period', '4H')
                rcfg = cur.get('range_position') or {}
                analysis = adapter.analyze(
                    inst_id, short_period, long_period,
                    boll_period=int(rcfg.get('boll_period', 20) or 20),
                    boll_dev=float(rcfg.get('boll_dev', 2.0) or 2.0),
                    signal_algo=cur.get('signal_algo'),
                    entry_price_type=cur.get('entry_price_type', 'close'),
                    exit_price_type=cur.get('exit_price_type', 'open'))
                if not analysis:
                    errors.append({'instId': inst_id, 'message': '策略分析返回空'})
                    continue
                items.append({
                    'instId': inst_id,
                    'ts': ts,
                    'price': analysis.get('last_price', 0),
                    'short_period': short_period,
                    'long_period': long_period,
                    'short_dir': analysis.get('direction') or '',
                    'long_dir': analysis.get('long_direction') or '',
                    'long_dir_prev': analysis.get('long_prev_direction') or '',
                    'atr_pct': analysis.get('atr_percentage', 0),
                    'boll_upper': analysis.get('boll_upper', 0),
                    'boll_middle': analysis.get('boll_middle', 0),
                    'boll_lower': analysis.get('boll_lower', 0),
                })
            except Exception as ex:
                errors.append({'instId': inst_id, 'message': str(ex)})
        return jsonify({'code': 200, 'message': f'批量快照完成：成功 {len(items)}/{len(currencies)}',
                        'data': {'items': items, 'errors': errors}})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'code': 500, 'message': str(e), 'data': None})


@app.route('/api/task/analysis/records', methods=['GET'])
def task_analysis_records_list():
    """分析记录列表（ts 降序）：查询前惰性回填到期的 1H/4H 复盘价格，
    响应附带个人判断 vs 策略方向命中率统计。

    参数: instId / start / end（'YYYY-MM-DD HH:MM:SS' 闭区间）/ judgment
    """
    try:
        from .database import session_scope
        from . import analysis_record_repo as repo

        inst_id = request.args.get('instId', '').strip() or None
        start = request.args.get('start', '').strip() or None
        end = request.args.get('end', '').strip() or None
        judgment = request.args.get('judgment', '').strip() or None
        if judgment and judgment not in _ANALYSIS_JUDGMENTS:
            return jsonify({'code': 400, 'message': f'judgment 仅支持 {list(_ANALYSIS_JUDGMENTS)}', 'data': None})

        # 回填与查询分离：K线拉取为网络 I/O，先提交释放连接再查列表
        with session_scope() as s:
            repo.backfill_due_reviews(s)
        with session_scope() as s:
            records = repo.query_records(s, inst_id=inst_id, start=start,
                                         end=end, judgment=judgment)
        return jsonify({'code': 200, 'message': 'success', 'data': {
            'records': records,
            'stats': repo.compute_stats(records),
        }})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'code': 500, 'message': str(e), 'data': None})


@app.route('/api/task/analysis/records', methods=['POST'])
def task_analysis_records_add():
    """保存分析记录：快照字段 + 个人判断/分析原因"""
    try:
        body = request.get_json(silent=True) or {}
        inst_id = str(body.get('instId') or '').strip()
        judgment = str(body.get('user_judgment') or '').strip()
        if not inst_id:
            return jsonify({'code': 400, 'message': '缺少 instId', 'data': None})
        if judgment not in _ANALYSIS_JUDGMENTS:
            return jsonify({'code': 400, 'message': f'判断仅支持 {list(_ANALYSIS_JUDGMENTS)}', 'data': None})
        try:
            price = float(body.get('price') or 0)
        except (TypeError, ValueError):
            price = 0.0
        if price <= 0:
            return jsonify({'code': 400, 'message': '快照价格无效，请重新生成快照', 'data': None})
        target, invalid, perr = _clean_plan_pair(body, price, judgment)
        if perr:
            return jsonify({'code': 400, 'message': perr, 'data': None})

        from .database import session_scope
        from . import analysis_record_repo as repo
        rec = {
            'ts': str(body.get('ts') or '').strip()
                  or datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'inst_id': inst_id,
            'price': price,
            'short_period': str(body.get('short_period') or ''),
            'long_period': str(body.get('long_period') or ''),
            'short_dir': str(body.get('short_dir') or ''),
            'long_dir': str(body.get('long_dir') or ''),
            'long_dir_prev': str(body.get('long_dir_prev') or '') or None,
            'atr_pct': float(body.get('atr_pct') or 0),
            'user_judgment': judgment,
            'user_reason': str(body.get('user_reason') or '').strip(),
            'target_price': target,
            'invalid_price': invalid,
            '_grace_minutes': _analysis_grace_minutes(),
        }
        with session_scope() as s:
            rec_id = repo.add_record(s, rec)
        return jsonify({'code': 200, 'message': '分析记录已保存', 'data': {'id': rec_id}})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'code': 500, 'message': str(e), 'data': None})


@app.route('/api/task/analysis/records_batch', methods=['POST'])
def task_analysis_records_add_batch():
    """批量保存分析记录：接收批量快照矩阵中勾选的多条，一次性写入。

    请求体: {'items': [快照字段 + user_judgment + user_reason, ...]}
    单条校验规则与单条保存一致；校验失败项跳过并计入 errors，成功项同 session 提交。
    """
    try:
        body = request.get_json(silent=True) or {}
        raw_items = body.get('items')
        if not isinstance(raw_items, list) or not raw_items:
            return jsonify({'code': 400, 'message': 'items 不能为空数组', 'data': None})

        from .database import session_scope
        from . import analysis_record_repo as repo

        valid_recs = []
        errors = []
        grace = _analysis_grace_minutes()
        for idx, item in enumerate(raw_items):
            if not isinstance(item, dict):
                errors.append({'index': idx, 'message': '项格式错误'})
                continue
            inst_id = str(item.get('instId') or '').strip()
            judgment = str(item.get('user_judgment') or '').strip()
            if not inst_id:
                errors.append({'index': idx, 'message': '缺少 instId'})
                continue
            if judgment not in _ANALYSIS_JUDGMENTS:
                errors.append({'index': idx, 'instId': inst_id, 'message': f'判断仅支持 {list(_ANALYSIS_JUDGMENTS)}'})
                continue
            try:
                price = float(item.get('price') or 0)
            except (TypeError, ValueError):
                price = 0.0
            if price <= 0:
                errors.append({'index': idx, 'instId': inst_id, 'message': '快照价格无效，请重新生成快照'})
                continue
            target, invalid, perr = _clean_plan_pair(item, price, judgment)
            if perr:
                errors.append({'index': idx, 'instId': inst_id, 'message': perr})
                continue
            valid_recs.append({
                'ts': str(item.get('ts') or '').strip()
                      or datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'inst_id': inst_id,
                'price': price,
                'short_period': str(item.get('short_period') or ''),
                'long_period': str(item.get('long_period') or ''),
                'short_dir': str(item.get('short_dir') or ''),
                'long_dir': str(item.get('long_dir') or ''),
                'long_dir_prev': str(item.get('long_dir_prev') or '') or None,
                'atr_pct': float(item.get('atr_pct') or 0),
                'user_judgment': judgment,
                'user_reason': str(item.get('user_reason') or '').strip(),
                'target_price': target,
                'invalid_price': invalid,
                '_grace_minutes': grace,
            })

        ids = []
        if valid_recs:
            with session_scope() as s:
                for rec in valid_recs:
                    ids.append(repo.add_record(s, rec))
        msg = f'批量保存完成：成功 {len(ids)} 条'
        if errors:
            msg += f'，失败 {len(errors)} 条'
        return jsonify({'code': 200, 'message': msg, 'data': {'ids': ids, 'errors': errors}})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'code': 500, 'message': str(e), 'data': None})


@app.route('/api/task/analysis/records/<int:rec_id>', methods=['PUT'])
def task_analysis_records_update(rec_id):
    """修改分析记录的个人判断/分析原因/结构价计划（快照字段不可改）。

    结构价：body 带 target_price/invalid_price 键才视为修改计划（前端编辑弹窗总是成对送）；
    已终局（win/loss/gap）的计划锁定不可改；切观望前须先清空未终局的计划。
    """
    try:
        body = request.get_json(silent=True) or {}
        judgment = str(body.get('user_judgment') or '').strip()
        if judgment not in _ANALYSIS_JUDGMENTS:
            return jsonify({'code': 400, 'message': f'判断仅支持 {list(_ANALYSIS_JUDGMENTS)}', 'data': None})

        from .database import session_scope
        from . import analysis_record_repo as repo
        with session_scope() as s:
            found = repo.query_by_ids(s, [rec_id])
        if not found:
            return jsonify({'code': 404, 'message': f'记录 {rec_id} 不存在', 'data': None})
        cur = found[0]

        target, invalid = repo._UNSET, repo._UNSET
        if 'target_price' in body or 'invalid_price' in body:
            new_t, new_i, perr = _clean_plan_pair(
                body, float(cur.get('price') or 0), judgment)
            if perr:
                return jsonify({'code': 400, 'message': perr, 'data': None})
            changed = ((new_t or 0.0) != (cur.get('target_price') or 0.0)
                       or (new_i or 0.0) != (cur.get('invalid_price') or 0.0))
            if changed and cur.get('plan_result') in ('win', 'loss', 'gap'):
                return jsonify({'code': 400, 'message': '结构价计划已终局，不可修改或清空', 'data': None})
            target, invalid = new_t, new_i
        elif judgment == 'watch' and cur.get('target_price') and cur.get('invalid_price') \
                and cur.get('plan_result') not in ('win', 'loss', 'gap'):
            return jsonify({'code': 400, 'message': '切为观望前请先清空结构价计划', 'data': None})

        with session_scope() as s:
            ok = repo.update_user_fields(
                s, rec_id, judgment, str(body.get('user_reason') or '').strip(),
                target_price=target, invalid_price=invalid)
        if not ok:
            return jsonify({'code': 404, 'message': f'记录 {rec_id} 不存在', 'data': None})
        return jsonify({'code': 200, 'message': '已更新', 'data': None})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'code': 500, 'message': str(e), 'data': None})


@app.route('/api/task/analysis/records/<int:rec_id>', methods=['DELETE'])
def task_analysis_records_delete(rec_id):
    """删除分析记录"""
    try:
        from .database import session_scope
        from . import analysis_record_repo as repo
        with session_scope() as s:
            ok = repo.delete_record(s, rec_id)
        if not ok:
            return jsonify({'code': 404, 'message': f'记录 {rec_id} 不存在', 'data': None})
        return jsonify({'code': 200, 'message': '已删除', 'data': None})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'code': 500, 'message': str(e), 'data': None})


@app.route('/api/task/analysis/records_batch', methods=['DELETE'])
def task_analysis_records_delete_batch():
    """批量删除分析记录

    请求体: {"ids": [1, 2, 3]}；不存在的 id 自动忽略，返回实际删除条数。
    """
    try:
        body = request.get_json(silent=True) or {}
        raw_ids = body.get('ids')
        if not isinstance(raw_ids, list) or not raw_ids:
            return jsonify({'code': 400, 'message': 'ids 不能为空数组', 'data': None})
        try:
            ids = [int(i) for i in raw_ids]
        except (TypeError, ValueError):
            return jsonify({'code': 400, 'message': 'ids 必须为整数数组', 'data': None})

        from .database import session_scope
        from . import analysis_record_repo as repo
        with session_scope() as s:
            deleted = repo.delete_records(s, ids)
        return jsonify({'code': 200, 'message': f'已删除 {deleted} 条记录',
                        'data': {'deleted': deleted}})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'code': 500, 'message': str(e), 'data': None})


@app.route('/api/task/residual-positions', methods=['GET'])
def task_residual_positions():
    """智能减仓残留仓位查询接口（供人工“扭亏为盈”操作参考）

    数据来源：
    - 残留记录：MySQL kv_store key='smart_reduce_residuals'（减仓时登记）
    - 当前张数/均价：本地账本 pos_book（与调度器同源）
    - 现价/浮动盈亏 upl/净已实现 realizedPnl：OKX get_positions 实时接口
      （账号优先取运行中的交易调度器账号，未运行时用默认账号）

    核心字段 target_price（扭亏目标价）：残留仓浮动盈亏刚好抵消整仓
    净已实现亏损的价格，涨/跌到该价位再手动平仓，整仓 realizedPnl 即转正。
    账本持仓已清零的过期记录会自动清理。
    """
    try:
        from .database import session_scope
        from . import config_store_repo, trader_state_repo
        from .api_config import get_account_api, get_api_config
        from .task.utils.instrument_spec import get_instrument_spec_cache

        # 1. 残留记录（减仓快照）
        with session_scope() as s:
            residuals = config_store_repo.load_json_config(
                s, config_store_repo.KEY_SMART_REDUCE_RESIDUALS) or {}

        # 2. 本地账本当前持仓（校验残留是否仍存在）
        ledger_account = _resolve_task_account()
        ledger_flag = str(get_api_config(ledger_account).get('flag') or '0')
        with session_scope() as s:
            pos_state = trader_state_repo.load_position_state(
                s, account=ledger_account, environment=ledger_flag)

        # 3. 交易所实时持仓：realizedPnl / upl / markPx（按 instId+mgnMode 索引）
        account = None
        try:
            from .task.scheduler import task_scheduler
            account = getattr(task_scheduler, '_trading_account', None)
        except Exception:
            pass
        ex_map = {}
        try:
            _acct_api = get_account_api(account)
            r = _rl_read('positions', _acct_api.get_positions, instType='SWAP')
            for p in (r.get('data') or []):
                if abs(float(p.get('pos') or 0)) <= 0.001:
                    continue
                ex_map[(p.get('instId'), p.get('mgnMode'))] = p
        except Exception as e:
            return jsonify({'code': 502,
                            'message': f'OKX 持仓查询失败: {e}', 'data': None})

        flag = '0'
        try:
            from .api_config import get_api_config
            flag = str(get_api_config(account).get('flag', '0') or '0')
        except Exception:
            pass
        spec_cache = get_instrument_spec_cache(flag)

        items, stale_keys = [], []
        for key, rec in (residuals or {}).items():
            inst = str(rec.get('inst_id') or '')
            bucket = str(rec.get('bucket') or '')
            direction = str(rec.get('direction') or '')
            bk = (pos_state.get(inst) or {}).get(bucket) or {}
            held = float((bk.get('held') or {}).get(direction) or 0)
            if held <= 0.01:
                stale_keys.append(key)  # 账本已清零（人工平仓/边界单成交）→ 过期
                continue
            avg_px = float((bk.get('avg_px') or {}).get(direction) or 0) \
                or float(rec.get('avg_px') or 0)
            mode = 'cross' if direction == 'long' else 'isolated'
            ex = ex_map.get((inst, mode)) or {}
            mark_px = float(ex.get('markPx') or 0)
            upl = float(ex.get('upl') or 0)
            realized_pnl = float(ex.get('realizedPnl')
                                 or rec.get('realized_pnl_at_reduce') or 0)
            spec = spec_cache.get_spec(inst)
            ct_val = float(spec.get('ct_val') or 0) if spec else 0.0
            # 扭亏目标价：残留 upl 抵消整仓净已实现亏损（rpnl<0）时的价格
            #   多头 P = avg − rpnl/(held×ctVal)；空头 P = avg + rpnl/(held×ctVal)
            target_px = 0.0
            if ct_val > 0 and held > 0 and realized_pnl < 0:
                sign = 1.0 if direction == 'long' else -1.0
                target_px = avg_px - sign * realized_pnl / (held * ct_val)
            items.append({
                'instId': inst,
                'bucket': bucket,
                'direction': direction,
                'direction_cn': '做多' if direction == 'long' else '做空',
                'bucket_cn': '趋势仓' if bucket == 'trend' else '区间仓',
                'held': held,
                'avg_px': avg_px,
                'mark_px': mark_px,
                'upl': round(upl, 6),
                'realized_pnl': round(realized_pnl, 6),
                'target_price': round(target_px, 8),
                'margin_est_usd': round(held * ct_val * mark_px / max(
                    float(ex.get('lever') or 1), 1e-9), 4)
                    if ct_val > 0 and mark_px > 0 else 0.0,
                'reduce_amount': rec.get('reduce_amount'),
                'keep_amount': rec.get('keep_amount'),
                'reduce_ts': rec.get('ts'),
                'scene': rec.get('scene'),
                'reason': rec.get('reason'),
            })

        # 4. 清理过期记录（账本持仓已清零的残留）并回写
        if stale_keys:
            for k in stale_keys:
                residuals.pop(k, None)
            try:
                with session_scope() as s:
                    config_store_repo.save_json_config(
                        s, config_store_repo.KEY_SMART_REDUCE_RESIDUALS, residuals)
            except Exception:
                pass

        items.sort(key=lambda x: x['realized_pnl'])  # 亏损最深的排前面，优先处理
        return jsonify({'code': 200, 'message': 'success', 'data': {
            'account': account or 'default',
            'count': len(items),
            'total_upl': round(sum(i['upl'] for i in items), 6),
            'total_realized_pnl': round(sum(i['realized_pnl'] for i in items), 6),
            'positions': items,
        }})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'code': 500, 'message': str(e), 'data': None})


# =============================================================================
# 持仓看板：当前持仓卡片 / 历史持仓查询 / 历史币种库
# =============================================================================

def _resolve_task_account():
    """账号解析：优先取运行中的交易调度器账号，未运行时用默认账号 DEFAULT_ACCOUNT。

    这里刻意对齐「交易配置」页下拉的默认口径（list_accounts 的 is_default=main）：
    网页所有不带 account 参数的配置读接口（分析记录币种列表 / 快照 / 剩余持仓等）
    都经本函数解析账号。旧实现未运行时返回 None，会回退到「全局 strategy_config」
    那份遗留配置，而交易配置页实际按账号 key（strategy_config:main）读写，两者
    币种天然不一致（例：main 只留 BTC、全局仍是 5 币），这正是分析记录页与定时
    任务页币种对不上的根因。返回 DEFAULT_ACCOUNT 后，未指定账号即落到 main 专属
    key，与交易配置页完全同源。
    """
    try:
        from .task.scheduler import task_scheduler
        acct = getattr(task_scheduler, '_trading_account', None)
        if acct:
            return acct
    except Exception:
        pass
    try:
        from .api_config import DEFAULT_ACCOUNT
        return DEFAULT_ACCOUNT or None
    except Exception:
        return None


def _load_coin_library():
    """读取历史币种库；库为空时用当前配置币种回填（首次兜底建档）"""
    from .database import session_scope
    from . import config_store_repo
    try:
        with session_scope() as s:
            lib = config_store_repo.load_json_config(
                s, config_store_repo.KEY_TASK_COIN_LIBRARY)
        if isinstance(lib, dict) and lib:
            return lib
    except Exception:
        pass
    now = datetime.now().timestamp()
    lib = {}
    try:
        for cur in (_load_trading_config().get('currencies') or []):
            inst = str(cur.get('instId') or '')
            if inst:
                lib[inst] = {'first_used': now, 'last_used': now, 'use_count': 1}
        if lib:
            with session_scope() as s:
                config_store_repo.save_json_config(
                    s, config_store_repo.KEY_TASK_COIN_LIBRARY, lib)
    except Exception:
        traceback.print_exc()
    return lib


def _save_coin_library(lib):
    from .database import session_scope
    from . import config_store_repo
    with session_scope() as s:
        config_store_repo.save_json_config(
            s, config_store_repo.KEY_TASK_COIN_LIBRARY, lib)


def _upsert_coin_library(inst_ids):
    """保存交易配置时调用：新币种建档 / 存量币种刷新 last_used。

    从配置中移除的币种不删除（仅变为"已停用"，留存历史记录）。
    """
    try:
        from .database import session_scope
        from . import config_store_repo
        if not inst_ids:
            return
        now = datetime.now().timestamp()
        try:
            with session_scope() as s:
                lib = config_store_repo.load_json_config(
                    s, config_store_repo.KEY_TASK_COIN_LIBRARY)
        except Exception:
            lib = None
        lib = lib if isinstance(lib, dict) else {}
        for inst in inst_ids:
            rec = lib.get(inst)
            if isinstance(rec, dict):
                rec['last_used'] = now
            else:
                lib[inst] = {'first_used': now, 'last_used': now, 'use_count': 1}
        _save_coin_library(lib)
    except Exception:
        traceback.print_exc()


@app.route('/api/task/positions/current', methods=['GET'])
def task_positions_current():
    """持仓看板：定时任务配置币种的实时持仓卡片数据

    数据来源：
    - 任务币种清单：交易配置 currencies（与调度器同源）
    - 实时持仓：OKX get_positions（净持仓，两篮子合并展示）
    - 篮子拆分：本地账本 pos_book（趋势仓/区间仓各自张数）
    """
    try:
        from .database import session_scope
        from . import trader_state_repo
        from .api_config import get_account_api, get_api_config
        from .task.utils.instrument_spec import get_instrument_spec_cache

        account = _resolve_task_account()
        config = _load_trading_config()
        currencies = config.get('currencies') or []

        # 1. 交易所实时持仓（按 instId 索引，过滤零持仓）
        try:
            _acct_api = get_account_api(account)
            r = _rl_read('positions', _acct_api.get_positions, instType='SWAP')
        except Exception as e:
            return jsonify({'code': 502,
                            'message': f'OKX 持仓查询失败: {e}', 'data': None})
        pos_map = {}
        for p in (r.get('data') or []):
            if abs(float(p.get('pos') or 0)) <= 0.001:
                continue
            pos_map[p.get('instId')] = p

        # 2. 本地账本篮子拆分（趋势仓/区间仓）
        try:
            with session_scope() as s:
                pos_state = trader_state_repo.load_position_state(
                    s, account=account, environment=str(get_api_config(account).get('flag') or '0'))
        except Exception:
            pos_state = {}

        # 3. 合约规格（面值，用于保证金估算）
        flag = '0'
        try:
            flag = str(get_api_config(account).get('flag', '0') or '0')
        except Exception:
            pass
        spec_cache = get_instrument_spec_cache(flag)

        cards = []
        for cur in currencies:
            inst = str(cur.get('instId') or '')
            tcfg = cur.get('trend_position') or {}
            rcfg = cur.get('range_position') or {}

            # 篮子张数拆分（本地账本）
            baskets = {}
            st = pos_state.get(inst) or {}
            for bucket in ('trend', 'range'):
                held = (st.get(bucket) or {}).get('held') or {}
                baskets[bucket] = {
                    'long': float(held.get('long') or 0),
                    'short': float(held.get('short') or 0),
                }

            card = {
                'instId': inst,
                'leverage': cur.get('leverage', 0),
                'trade_enabled': cur.get('trade_enabled', True),
                'trend_enabled': bool(tcfg.get('enabled')),
                'range_enabled': bool(rcfg.get('enabled')),
                'short_period': cur.get('short_period'),
                'long_period': cur.get('long_period'),
                'baskets': baskets,
                'position': None,
            }

            ex = pos_map.get(inst)
            if ex:
                spec = spec_cache.get_spec(inst)
                ct_val = float(spec.get('ct_val') or 0) if spec else 0.0
                pos = float(ex.get('pos') or 0)
                mark_px = float(ex.get('markPx') or 0)
                lever = float(ex.get('lever') or cur.get('leverage') or 1) or 1
                margin = (abs(pos) * ct_val * mark_px / lever
                          if ct_val > 0 and mark_px > 0 else 0.0)
                card['position'] = {
                    'direction': 'long' if pos > 0 else 'short',
                    'pos': abs(pos),
                    'avg_px': float(ex.get('avgPx') or 0),
                    'mark_px': mark_px,
                    'upl': round(float(ex.get('upl') or 0), 6),
                    'upl_ratio': float(ex.get('uplRatio') or 0),
                    'lever': lever,
                    'liq_px': float(ex.get('liqPx') or 0),
                    'mgn_mode': ex.get('mgnMode') or '',
                    'notional': round(float(ex.get('notionalUsd') or 0), 4),
                    'margin_est': round(margin, 4),
                    'realized_pnl': round(float(ex.get('realizedPnl') or 0), 6),
                }
            cards.append(card)

        return jsonify({'code': 200, 'message': 'success', 'data': {
            'account': account or 'default',
            'count': len(cards),
            'holding': sum(1 for c in cards if c['position']),
            'cards': cards,
        }})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'code': 500, 'message': str(e), 'data': None})


# OKX 历史持仓平仓类型映射（type 字段）
_POS_HIST_CLOSE_TYPE = {
    '1': '强平', '2': '普通平仓', '3': '交割平仓',
    '4': 'ADL减仓', '5': '对冲平仓',
}


@app.route('/api/task/positions/history', methods=['GET'])
def task_positions_history():
    """单币种历史持仓记录（OKX positions-history，仅近约 3 个月）

    参数: instId（必填）/ limit（默认20，上限100）/ after（游标翻页，传上一页最早 uTime）
    """
    inst_id = (request.args.get('instId') or '').strip()
    if not inst_id:
        return jsonify({'code': 400, 'message': 'instId 参数必填', 'data': None})
    try:
        limit = max(1, min(int(request.args.get('limit', 20)), 100))
    except (TypeError, ValueError):
        limit = 20
    after = (request.args.get('after') or '').strip()
    try:
        from .api_config import get_account_api
        account = _resolve_task_account()
        params = {'instType': 'SWAP', 'instId': inst_id, 'limit': str(limit)}
        if after:
            params['after'] = after
        _acct_api = get_account_api(account)
        r = _rl_read('positions', _acct_api.get_positions_history, **params)
        if r.get('code') != '0':
            return jsonify({'code': 502,
                            'message': f"OKX 查询失败: {r.get('msg') or '未知错误'}",
                            'data': None})

        def _f(v, default=0.0):
            try:
                return float(v) if v not in (None, '', 'None') else default
            except (TypeError, ValueError):
                return default

        items, total_pnl, profit_cnt = [], 0.0, 0
        total_fee, total_funding = 0.0, 0.0
        for p in (r.get('data') or []):
            rpnl = _f(p.get('realizedPnl'))
            fee = _f(p.get('fee'))
            funding = _f(p.get('fundingFee'))
            total_pnl += rpnl
            total_fee += fee
            total_funding += funding
            if rpnl > 0:
                profit_cnt += 1
            close_ts = int(_f(p.get('uTime')))
            open_ts = int(_f(p.get('cTime')))
            items.append({
                'close_time': datetime.fromtimestamp(close_ts / 1000).strftime(
                    '%Y-%m-%d %H:%M') if close_ts > 0 else '-',
                'open_time': datetime.fromtimestamp(open_ts / 1000).strftime(
                    '%Y-%m-%d %H:%M') if open_ts > 0 else '-',
                'direction': p.get('direction') or '',
                'pos': _f(p.get('pos')),
                'open_px': _f(p.get('openAvgPx')) or _f(p.get('avgPx')),
                'close_px': _f(p.get('closeAvgPx')),
                'pnl': round(rpnl, 6),
                'fee': round(fee, 6),
                'funding_fee': round(funding, 6),
                'lever': _f(p.get('lever')),
                'mgn_mode': p.get('mgnMode') or '',
                'close_type': _POS_HIST_CLOSE_TYPE.get(
                    str(p.get('type') or ''), str(p.get('type') or '')),
                'close_ts': close_ts,
            })

        return jsonify({'code': 200, 'message': 'success', 'data': {
            'instId': inst_id,
            'account': account or 'default',
            'count': len(items),
            'records': items,
            'summary': {
                'total_pnl': round(total_pnl, 6),
                'total_fee': round(total_fee, 6),
                'total_funding': round(total_funding, 6),
                'profit_count': profit_cnt,
                'win_rate': round(profit_cnt / len(items) * 100, 1) if items else 0,
            },
        }})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'code': 500, 'message': str(e), 'data': None})


@app.route('/api/task/coin-library', methods=['GET'])
def task_coin_library_list():
    """历史币种库：所有曾用过的定时任务币种（在用/已停用标记）"""
    try:
        lib = _load_coin_library()
        config = _load_trading_config()
        active_ids = {str(c.get('instId') or '')
                      for c in (config.get('currencies') or [])}
        coins = []
        for inst, rec in lib.items():
            rec = rec if isinstance(rec, dict) else {}
            coins.append({
                'instId': inst,
                'first_used': rec.get('first_used') or 0,
                'last_used': rec.get('last_used') or 0,
                'use_count': rec.get('use_count', 1),
                'active': inst in active_ids,
            })
        coins.sort(key=lambda x: (x['last_used'], x['instId']), reverse=True)
        return jsonify({'code': 200, 'message': 'success', 'data': {
            'count': len(coins),
            'active_count': sum(1 for c in coins if c['active']),
            'coins': coins,
        }})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'code': 500, 'message': str(e), 'data': None})


@app.route('/api/task/coin-library', methods=['DELETE'])
def task_coin_library_delete():
    """删除历史币种库记录（仅移除留存，不影响交易配置与实际持仓）

    body: {"inst_ids": [...]}；在用币种（仍在当前配置中）拒绝删除。
    """
    try:
        body = request.get_json(silent=True) or {}
        inst_ids = [str(i).strip() for i in (body.get('inst_ids') or [])]
        inst_ids = [i for i in inst_ids if i]
        if not inst_ids:
            return jsonify({'code': 400, 'message': 'inst_ids 不能为空', 'data': None})

        config = _load_trading_config()
        active_ids = {str(c.get('instId') or '')
                      for c in (config.get('currencies') or [])}
        blocked = [i for i in inst_ids if i in active_ids]
        if blocked:
            return jsonify({'code': 400,
                            'message': f"在用币种不可删除：{', '.join(blocked)}",
                            'data': None})

        lib = _load_coin_library()
        removed = [i for i in inst_ids if lib.pop(i, None) is not None]
        if removed:
            _save_coin_library(lib)
        return jsonify({'code': 200,
                        'message': f'已删除 {len(removed)} 个币种记录',
                        'data': {'removed': removed, 'remaining': len(lib)}})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'code': 500, 'message': str(e), 'data': None})


@app.route('/api/task/config/trading', methods=['POST'])
def save_trading_config():
    """保存交易配置（按账号：?account=xxx，缺省用当前实盘运行账号）"""
    try:
        account = (request.args.get('account') or '').strip() or None
        new_config = request.get_json()
        if not new_config:
            return jsonify({'code': 400, 'message': '请求数据为空', 'data': None})

        # 基础校验
        errors = _validate_trading_config(new_config)
        # 合约存在性硬校验：手滑写错合约名（如 NEAR→NAER）格式合规但交易所无此合约，
        # 一旦保存会留下对不到账的幽灵账本，经账户级对账闸门冻结全账户开仓。
        errors = errors + _validate_instruments_exist(new_config.get('currencies') or [], account)
        if errors:
            return jsonify({'code': 400, 'message': '配置校验失败', 'data': {'errors': errors}})

        cfg_ok, cfg_msg = _save_trading_config(new_config, account)
        # 历史币种库留存：新币建档 / 存量刷新最近使用时间（移除币种不删）
        _upsert_coin_library(
            [str(c.get('instId') or '') for c in (new_config.get('currencies') or [])
             if c.get('instId')])
        if not cfg_ok:
            # 主存未写入 = 交易端下个周期仍按旧参数跑，不能报成功
            return jsonify({'code': 500, 'message': cfg_msg, 'data': None})
        return jsonify({'code': 200,
                        'message': '交易配置已保存，将在下一个执行周期生效'
                                   + (f'（注意：{cfg_msg}）' if cfg_msg else ''),
                        'data': new_config})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'code': 500, 'message': str(e), 'data': None})


def _validate_instruments_exist(currencies, account=None):
    """校验每个 instId 在目标 OKX 环境（实盘/模拟盘）是否真实存在。

    仅对“确证不存在（False）”拦截报错，防止误拼合约名保存后产生幽灵账本；
    查询失败（None，网络/限频/SDK 异常）保守不拦，避免 OKX 抽风时误伤正常保存。
    """
    errs = []
    ids = []
    for i, cur in enumerate(currencies or []):
        inst = str((cur or {}).get('instId') or '').strip()
        # 格式不合规的已由 _validate_trading_config 报错，不重复联网查询
        if inst and _re.match(r'^[A-Z]+-USDT-SWAP$', inst):
            ids.append((i, inst))
    if not ids:
        return errs
    flag = '0'
    try:
        from .api_config import get_api_config
        flag = str(get_api_config(account).get('flag', '0') or '0')
    except Exception:
        pass
    try:
        from .task.utils.instrument_spec import get_instrument_spec_cache
        cache = get_instrument_spec_cache(flag)
    except Exception as e:
        app.logger.warning(f"合约存在性校验初始化失败（已跳过）: {e}")
        return errs
    env_cn = '实盘' if flag == '0' else '模拟盘'
    for i, inst in ids:
        try:
            exists = cache.instrument_exists(inst)
        except Exception as e:
            app.logger.warning(f"合约存在性查询异常（{inst}，按未知处理不拦）: {e}")
            continue
        if exists is False:
            errs.append(f'币种[{i}] {inst}: OKX {env_cn}无此合约，'
                        f'请核对合约名（避免误拼产生对不到账的幽灵币种）')
    return errs


def _validate_trading_config(config):
    """校验交易配置合法性（双仓位架构 v3.0：趋势跟踪 + 区间波动）"""
    errors = []
    valid_short = ['1m', '5m', '15m', '1H', '4H']
    valid_long = ['5m', '15m', '1H', '4H', '1D', '1W']

    currencies = config.get('currencies', [])
    if not currencies:
        errors.append('至少需要一个币种配置')

    seen_inst_ids = set()
    for i, cur in enumerate(currencies):
        prefix = f'币种[{i}]'
        inst_id = cur.get('instId', '')
        if not inst_id or not _re.match(r'^[A-Z]+-USDT-SWAP$', inst_id):
            errors.append(f'{prefix}: 合约ID格式不正确')
        elif inst_id in seen_inst_ids:
            errors.append(f'{prefix}: 合约ID重复: {inst_id}')
        seen_inst_ids.add(inst_id)

        if cur.get('short_period') not in valid_short:
            errors.append(f'{prefix}: 短周期必须是 {valid_short} 之一')
        if cur.get('long_period') not in valid_long:
            errors.append(f'{prefix}: 长周期必须是 {valid_long} 之一')

        lev = cur.get('leverage', 0)
        if not isinstance(lev, int) or isinstance(lev, bool) or lev <= 0:
            errors.append(f'{prefix}: leverage 必须为正整数')
        if not isinstance(cur.get('verbose_lifecycle', True), bool):
            errors.append(f'{prefix}: verbose_lifecycle 必须为布尔值')
        if not isinstance(cur.get('trade_enabled', True), bool):
            errors.append(f'{prefix}: trade_enabled 必须为布尔值（缺失视为开启）')

        if cur.get('manual_direction') not in ['auto', 'long', 'short']:
            errors.append(f'{prefix}: manual_direction 必须是 auto/long/short')

        # 信号算法与价格类型校验（可选字段，仅当存在时校验）
        if cur.get('signal_algo') is not None and cur.get('signal_algo') not in ['diff', 'hybrid']:
            errors.append(f'{prefix}: signal_algo 必须是 diff/hybrid')
        if cur.get('entry_price_type') is not None and cur.get('entry_price_type') not in ['open', 'close']:
            errors.append(f'{prefix}: entry_price_type 必须是 open/close')
        if cur.get('exit_price_type') is not None and cur.get('exit_price_type') not in ['open', 'close']:
            errors.append(f'{prefix}: exit_price_type 必须是 open/close')

        # ── 趋势跟踪仓位（按趋势策略信号限价开平）──
        tcfg = cur.get('trend_position')
        if not isinstance(tcfg, dict):
            errors.append(f'{prefix}: trend_position 必须为对象（趋势跟踪仓位配置）')
            tcfg = {}
        else:
            t_on = tcfg.get('enabled', False)
            if not isinstance(t_on, bool):
                errors.append(f'{prefix}: trend_position.enabled 必须为布尔值')
            if tcfg.get('period_mode') not in ['single', 'dual']:
                errors.append(f'{prefix}: trend_position.period_mode 必须是 '
                              'single(单周期双向) / dual(双周期单向)')
            tct = tcfg.get('contracts', 0)
            if not isinstance(tct, (int, float)) or tct < 0:
                errors.append(f'{prefix}: trend_position.contracts 必须为非负数')
            # 下单量双模式：contracts=按张数（存量）/ usd=按名义价值金额折算
            t_mode = tcfg.get('size_mode', 'contracts')
            if t_mode not in ['contracts', 'usd']:
                errors.append(f'{prefix}: trend_position.size_mode 必须是 contracts/usd')
            t_usd = tcfg.get('notional_usd', 0)
            if not isinstance(t_usd, (int, float)) or t_usd < 0:
                errors.append(f'{prefix}: trend_position.notional_usd 必须为非负数')
            elif t_on is True and t_mode == 'usd' and t_usd <= 0:
                errors.append(f'{prefix}: trend_position 已启用且为金额模式，notional_usd 必须为正数')
            elif t_on is True and t_mode != 'usd' and tct <= 0:
                errors.append(f'{prefix}: trend_position 已启用且为张数模式，contracts 必须为正数')
            if not isinstance(tcfg.get('exchange_algo_backup', True), bool):
                errors.append(f'{prefix}: trend_position.exchange_algo_backup 必须为布尔值')

        # ── 区间波动仓位（BOLL 边界限价往复刷区间）──
        rcfg = cur.get('range_position')
        if not isinstance(rcfg, dict):
            errors.append(f'{prefix}: range_position 必须为对象（区间波动仓位配置）')
        else:
            r_on = rcfg.get('enabled', False)
            if not isinstance(r_on, bool):
                errors.append(f'{prefix}: range_position.enabled 必须为布尔值')
            rct = rcfg.get('contracts', 0)
            if not isinstance(rct, (int, float)) or rct < 0:
                errors.append(f'{prefix}: range_position.contracts 必须为非负数')
            # 下单量双模式：contracts=按张数（存量）/ usd=按名义价值金额折算
            r_mode = rcfg.get('size_mode', 'contracts')
            if r_mode not in ['contracts', 'usd']:
                errors.append(f'{prefix}: range_position.size_mode 必须是 contracts/usd')
            r_usd = rcfg.get('notional_usd', 0)
            if not isinstance(r_usd, (int, float)) or r_usd < 0:
                errors.append(f'{prefix}: range_position.notional_usd 必须为非负数')
            elif r_on is True and r_mode == 'usd' and r_usd <= 0:
                errors.append(f'{prefix}: range_position 已启用且为金额模式，notional_usd 必须为正数')
            elif r_on is True and r_mode != 'usd' and rct <= 0:
                errors.append(f'{prefix}: range_position 已启用且为张数模式，contracts 必须为正数')
            bp = rcfg.get('boll_period', 20)
            if not isinstance(bp, int) or isinstance(bp, bool) or bp <= 1:
                errors.append(f'{prefix}: range_position.boll_period 必须为大于1的整数')
            bd = rcfg.get('boll_dev', 2.0)
            if not isinstance(bd, (int, float)) or bd <= 0:
                errors.append(f'{prefix}: range_position.boll_dev 必须为正数')
            am = rcfg.get('boll_amend_min_pct', 0.001)
            if not isinstance(am, (int, float)) or am < 0 or am >= 1:
                errors.append(f'{prefix}: range_position.boll_amend_min_pct '
                              '必须在 [0,1) 内（0.001=0.1%）')
            # ── B 仓位策略引擎（keltner_boll 默认 / boll 存量）──
            r_strategy = rcfg.get('strategy', 'keltner_boll')
            if r_strategy not in ('keltner_boll', 'boll'):
                errors.append(f'{prefix}: range_position.strategy 必须是 '
                              'keltner_boll(默认，ATR外扩+趋势方向过滤) / boll(存量纯布林带)')
            # direction_source：B 仓位方向来源。'pro3'(默认，存量：跟随 Pro3 长周期方向，
            # keltner_boll 仅外扩边界+方向过滤) / 'keltner'(独立信号引擎：方向/边界/开平价
            # 全部由 keltner_boll 按回测同源公式计算，脱离 Pro3)。'keltner' 需搭配 keltner_boll。
            r_dsrc = rcfg.get('direction_source', 'pro3')
            if r_dsrc not in ('pro3', 'keltner'):
                errors.append(f'{prefix}: range_position.direction_source 必须是 '
                              'pro3(默认，跟随Pro3长周期方向) / keltner(独立信号引擎)')
            elif r_dsrc == 'keltner' and r_strategy != 'keltner_boll':
                errors.append(f'{prefix}: range_position.direction_source=keltner '
                              "需搭配 strategy='keltner_boll'")
            # trend_indicator：长周期趋势方向方法名，取值须来自 keltner_boll 注册表
            ti = rcfg.get('trend_indicator')
            if ti is not None:
                try:
                    import os as _os, sys as _sys
                    _sdir = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), 'strategy')
                    if _sdir not in _sys.path:
                        _sys.path.insert(0, _sdir)
                    from keltner_boll import list_indicators as _kb_list
                    _valid_ti = _kb_list()
                except Exception:
                    _valid_ti = None
                if _valid_ti is not None and ti not in _valid_ti:
                    errors.append(f'{prefix}: range_position.trend_indicator 未知，'
                                  f'可选: {"/".join(_valid_ti)}')
            for k in ('entry_atr_mult', 'exit_atr_mult'):
                v = rcfg.get(k)
                if v is not None and (not isinstance(v, (int, float))
                                      or isinstance(v, bool) or v < 0):
                    errors.append(f'{prefix}: range_position.{k} 必须为非负数（ATR 外扩倍数）')
            ap = rcfg.get('atr_period')
            if ap is not None and (not isinstance(ap, int)
                                   or isinstance(ap, bool) or ap <= 0):
                errors.append(f'{prefix}: range_position.atr_period 必须为正整数')
            if rcfg.get('direction_filter') is not None \
                    and not isinstance(rcfg.get('direction_filter'), bool):
                errors.append(f'{prefix}: range_position.direction_filter 必须为布尔值')
            # 区间仓 B 独立风险退出（双仓位出场与风控增强_6596c3cb）：
            # 默认 enabled=False 且各数值为 0（0=单项不启用），字段均可缺失，
            # 缺失时 trader 使用内置默认。
            rr = rcfg.get('risk')
            if rr is not None:
                if not isinstance(rr, dict):
                    errors.append(f'{prefix}: range_position.risk 必须为对象')
                else:
                    if not isinstance(rr.get('enabled', False), bool):
                        errors.append(f'{prefix}: range_position.risk.enabled 必须为布尔值')
                    for k in ('hard_stop_atr_mult',):
                        v = rr.get(k)
                        if v is not None and (not isinstance(v, (int, float))
                                              or isinstance(v, bool) or v < 0):
                            errors.append(f'{prefix}: range_position.risk.{k} 必须为非负数（0=不启用）')
                    for k in ('max_hold_bars', 'breakdown_rounds', 'cooldown_bars'):
                        v = rr.get(k)
                        if v is not None and (not isinstance(v, int)
                                              or isinstance(v, bool) or v < 0):
                            errors.append(f'{prefix}: range_position.risk.{k} 必须为非负整数')

        # 止盈止损只对趋势跟踪仓位生效（区间波动自带对侧 BOLL 边界止盈）
        tp = tcfg.get('take_profit')
        if tp is not None:
            if not isinstance(tp, dict):
                errors.append(f'{prefix}: take_profit 必须为对象')
            else:
                if not isinstance(tp.get('enabled', False), bool):
                    errors.append(f'{prefix}: take_profit.enabled 必须为布尔值')
                # 主止盈 category 六选一（旧结构 type=fixed/trailing 已废弃）
                if 'category' in tp:
                    cat = tp.get('category', 'none')
                    if cat not in ['none', 'fixed_target', 'trailing', 'momentum', 'channel', 'ladder', 'combo']:
                        errors.append(f'{prefix}: take_profit.category 必须是 none/fixed_target/trailing/momentum/channel/ladder/combo')
                    # 各子块模式与数值参数校验
                    _num_fields = {
                        'fixed_target': ('pct', 'r_multiple', 'atr_multiple'),
                        'trailing': ('activate_pct', 'pullback_pct', 'chandelier_atr'),
                        'momentum': ('decay_bars', 'lookback', 'min_profit_pct'),
                        'channel': ('min_profit_pct',),
                        'time_stop': ('max_bars', 'min_profit_pct'),
                    }
                    _valid_modes = {
                        'fixed_target': ['pct', 'r_multiple', 'atr_multiple'],
                        'trailing': ['pullback_pct', 'chandelier', 'r_ladder'],
                        'momentum': ['hist_decay', 'divergence'],
                        'channel': ['outer', 'middle'],
                    }
                    for blk_name, fields in _num_fields.items():
                        blk = tp.get(blk_name)
                        if blk is None:
                            continue
                        if not isinstance(blk, dict):
                            errors.append(f'{prefix}: take_profit.{blk_name} 必须为对象')
                            continue
                        if blk_name in _valid_modes and blk.get('mode') is not None \
                                and blk.get('mode') not in _valid_modes[blk_name]:
                            errors.append(f'{prefix}: take_profit.{blk_name}.mode 必须是 {"/".join(_valid_modes[blk_name])}')
                        for k in fields:
                            v = blk.get(k)
                            if v is not None and (not isinstance(v, (int, float)) or v < 0):
                                errors.append(f'{prefix}: take_profit.{blk_name}.{k} 必须为非负数')
                    ts = tp.get('time_stop')
                    if isinstance(ts, dict) and not isinstance(ts.get('enabled', False), bool):
                        errors.append(f'{prefix}: take_profit.time_stop.enabled 必须为布尔值')
                    # 追踪 r_levels：[[reach_r, lock_r], ...]
                    tr = tp.get('trailing')
                    if isinstance(tr, dict) and tr.get('r_levels') is not None:
                        rl = tr.get('r_levels')
                        if not isinstance(rl, list) or any(
                                not isinstance(x, list) or len(x) != 2 or
                                not all(isinstance(y, (int, float)) for y in x) for x in rl):
                            errors.append(f'{prefix}: take_profit.trailing.r_levels 必须为 [[reach_r,lock_r],..] 数值数组')
                    # combo 参数校验（双仓位出场与风控增强）：数值字段非负，
                    # lock_steps 与 ladder.levels 同构，但为 [[reach_r, lock_r], ...]
                    cb = tp.get('combo')
                    if cb is not None:
                        if not isinstance(cb, dict):
                            errors.append(f'{prefix}: take_profit.combo 必须为对象')
                        else:
                            for k in ('init_r_atr', 'activate_r', 'chandelier_atr',
                                      'partial_r', 'breakeven_buffer_pct'):
                                v = cb.get(k)
                                if v is not None and (not isinstance(v, (int, float))
                                                      or isinstance(v, bool) or v < 0):
                                    errors.append(f'{prefix}: take_profit.combo.{k} 必须为非负数')
                            pr = cb.get('partial_ratio')
                            if pr is not None and (not isinstance(pr, (int, float))
                                                   or isinstance(pr, bool) or not (0 <= pr <= 1)):
                                errors.append(f'{prefix}: take_profit.combo.partial_ratio 必须在 [0,1] 内')
                            ls = cb.get('lock_steps')
                            if ls is not None:
                                if not isinstance(ls, list) or any(
                                        not isinstance(x, list) or len(x) != 2 or
                                        not all(isinstance(y, (int, float))
                                                and not isinstance(y, bool) for y in x)
                                        for x in ls):
                                    errors.append(f'{prefix}: take_profit.combo.lock_steps '
                                                  '必须为 [[reach_r,lock_r],..] 数值数组')
                    # 分批 levels：[{pct, close_ratio}, ...]，pct 递增且 close_ratio ∈ (0,1]
                    ld = tp.get('ladder')
                    if isinstance(ld, dict) and ld.get('levels') is not None:
                        lv = ld.get('levels')
                        if not isinstance(lv, list) or len(lv) == 0:
                            errors.append(f'{prefix}: take_profit.ladder.levels 必须为非空数组')
                        else:
                            prev_pct = 0
                            for j, item in enumerate(lv):
                                if not isinstance(item, dict) \
                                        or not isinstance(item.get('pct'), (int, float)) \
                                        or not isinstance(item.get('close_ratio'), (int, float)):
                                    errors.append(f'{prefix}: take_profit.ladder.levels[{j}] 必须含数值 pct/close_ratio')
                                    continue
                                if item['pct'] <= prev_pct:
                                    errors.append(f'{prefix}: take_profit.ladder.levels[{j}].pct 必须递增且>0')
                                prev_pct = item['pct']
                                if not (0 < item['close_ratio'] <= 1):
                                    errors.append(f'{prefix}: take_profit.ladder.levels[{j}].close_ratio 必须在(0,1]内')
                            if lv and isinstance(lv[-1], dict) and isinstance(lv[-1].get('close_ratio'), (int, float)) \
                                    and lv[-1]['close_ratio'] < 1:
                                errors.append(f'{prefix}: take_profit.ladder.levels 末档 close_ratio 必须为 1.0（清仓档）')
                else:
                    errors.append(f'{prefix}: take_profit 缺少 category 字段（旧结构已废弃）')
        sl = tcfg.get('stop_loss')
        if sl is not None:
            if not isinstance(sl, dict):
                errors.append(f'{prefix}: stop_loss 必须为对象')
            else:
                if not isinstance(sl.get('enabled', False), bool):
                    errors.append(f'{prefix}: stop_loss.enabled 必须为布尔值')
                if sl.get('type', 'fixed') not in ['fixed', 'atr']:
                    errors.append(f'{prefix}: stop_loss.type 必须是 fixed/atr')
                for k in ('fixed_pct', 'atr_multiple'):
                    v = sl.get(k)
                    if v is not None and (not isinstance(v, (int, float)) or v <= 0):
                        errors.append(f'{prefix}: stop_loss.{k} 必须为正数')

    gs = config.get('global_settings', {})
    rc = gs.get('risk_control', {})
    if not isinstance(rc.get('max_position_per_currency', 0), (int, float)):
        errors.append('单币种最大持仓必须为数字')
    if not isinstance(rc.get('max_position_per_currency_usd', 0), (int, float)):
        errors.append('单币种最大持仓(USD口径)必须为数字')
    if not isinstance(rc.get('max_total_position', 0), (int, float)):
        errors.append('总最大持仓必须为数字')

    # 人工强平后的自动开仓冷却时长（分钟，0=不冷却）
    mcp = gs.get('manual_close_pause_minutes', 30)
    if not isinstance(mcp, (int, float)) or isinstance(mcp, bool) or mcp < 0:
        errors.append('manual_close_pause_minutes 必须为非负数字')

    # 邮件通知同类冷却时长（分钟，范围与前端输入框 min/max 保持一致）
    ecd = gs.get('email_cooldown_minutes', 30)
    if not isinstance(ecd, (int, float)) or isinstance(ecd, bool) or not (1 <= ecd <= 1440):
        errors.append('email_cooldown_minutes 必须为 1~1440 之间的数字（分钟）')

    # 连续执行失败告警阈值（轮数，0=不告警）
    cfa = gs.get('consecutive_failure_alert_rounds', 5)
    if not isinstance(cfa, (int, float)) or isinstance(cfa, bool) or cfa < 0:
        errors.append('consecutive_failure_alert_rounds 必须为非负数字')

    # 睡眠时段强平：长周期反转时用户看不到提醒邮件，改为强平趋势仓
    nfc = gs.get('night_force_close')
    if nfc is not None:
        if not isinstance(nfc, dict):
            errors.append('night_force_close 必须为对象')
        else:
            if not isinstance(nfc.get('enabled', True), bool):
                errors.append('night_force_close.enabled 必须为布尔值')
            for k in ('start_hour', 'end_hour'):
                v = nfc.get(k, 0)
                if not isinstance(v, int) or isinstance(v, bool) or not (0 <= v <= 23):
                    errors.append(f'night_force_close.{k} 必须为 0-23 的整数（东八区）')

    # 反向持仓风控（仅趋势仓 period_mode=dual 时实际生效）
    rg = gs.get('reverse_position_guard')
    if rg is not None:
        if not isinstance(rg, dict):
            errors.append('reverse_position_guard 必须为对象')
        else:
            if not isinstance(rg.get('enabled', False), bool):
                errors.append('reverse_position_guard.enabled 必须为布尔值')
            gm = rg.get('grace_minutes', 10)
            if not isinstance(gm, (int, float)) or gm <= 0:
                errors.append('reverse_position_guard.grace_minutes 必须为正数')

    return errors


# =====================================================================
# 配置管理 API — 邮件配置
# =====================================================================

@app.route('/api/task/config/email', methods=['GET'])
def get_email_config():
    """获取邮件配置（隐藏授权码）"""
    try:
        from .task.config.email_config import EMAIL_CONFIG, ADMIN_EMAIL
        safe_config = {
            'from_email': EMAIL_CONFIG.get('from_email', ''),
            'smtp_host': EMAIL_CONFIG.get('smtp_host', ''),
            'smtp_port': EMAIL_CONFIG.get('smtp_port', 465),
            'use_ssl': EMAIL_CONFIG.get('use_ssl', True),
            'admin_email': ADMIN_EMAIL,
            'password_masked': '****'  # 不暴露真实授权码
        }
        return jsonify({'code': 200, 'message': 'success', 'data': safe_config})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'code': 500, 'message': str(e), 'data': None})


@app.route('/api/task/config/email', methods=['POST'])
def save_email_config():
    """保存邮件配置"""
    try:
        data = request.get_json()
        if not data:
            return jsonify({'code': 400, 'message': '请求数据为空', 'data': None})

        from_email = data.get('from_email', '').strip()
        smtp_host = data.get('smtp_host', 'smtp.qq.com').strip()
        smtp_port = int(data.get('smtp_port', 465))
        use_ssl = bool(data.get('use_ssl', True))
        admin_email = data.get('admin_email', '').strip()
        password = data.get('password', '').strip()

        # 校验
        email_pattern = r'^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$'
        if not _re.match(email_pattern, from_email):
            return jsonify({'code': 400, 'message': '发件人邮箱格式不正确', 'data': None})
        if not _re.match(email_pattern, admin_email):
            return jsonify({'code': 400, 'message': '收件人邮箱格式不正确', 'data': None})
        if smtp_port not in [465, 587, 25]:
            return jsonify({'code': 400, 'message': 'SMTP端口必须是 465/587/25', 'data': None})

        # 读取当前配置，保留旧密码（如果前端传的是 **** 表示不修改）
        try:
            from .task.config.email_config import EMAIL_CONFIG as _old_cfg
            old_password = _old_cfg.get('password', '')
        except Exception:
            old_password = ''

        final_password = password if password and password != '****' else old_password

        # 写入 email_config.py
        content = f'''#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
邮箱配置
========
集中管理邮件通知相关的所有配置项。
修改此处即可影响 email_tool.py 和 message_notifier.py 的行为。
"""

# =============================================================================
# SMTP 发件配置
# =============================================================================
EMAIL_CONFIG = {{
    'from_email':  '{from_email}',
    'password':    '{final_password}',
    'smtp_host':   '{smtp_host}',
    'smtp_port':   {smtp_port},
    'use_ssl':     {use_ssl},
}}

# =============================================================================
# 收件人配置
# =============================================================================
ADMIN_EMAIL = '{admin_email}'


def get_email_config() -> dict:
    """返回 SMTP 配置副本"""
    return EMAIL_CONFIG.copy()


def get_admin_email() -> str:
    """返回管理员邮箱地址"""
    return ADMIN_EMAIL
'''
        with open(_EMAIL_CONFIG_PATH, 'w', encoding='utf-8') as f:
            f.write(content)

        return jsonify({'code': 200, 'message': '邮件配置已保存', 'data': None})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'code': 500, 'message': str(e), 'data': None})


@app.route('/api/task/config/validate', methods=['GET'])
def validate_config():
    """校验当前配置文件合法性"""
    try:
        config = _load_trading_config()
        errors = _validate_trading_config(config)
        if errors:
            return jsonify({'code': 400, 'message': '配置存在问题', 'data': {'errors': errors}})
        return jsonify({'code': 200, 'message': '配置校验通过', 'data': None})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'code': 500, 'message': str(e), 'data': None})


# =====================================================================
# 实盘交易调度器 API
# =====================================================================

@app.route('/api/task/trading/start', methods=['POST'])
def trading_start():
    """启动实盘交易调度器（可指定账号）"""
    try:
        from .task.scheduler import task_scheduler
        data = request.get_json(silent=True) or {}
        account = (data.get('account') or '').strip() or None
        ok, msg = task_scheduler.start_trading_scheduler(account)
        return jsonify({'code': 200 if ok else 400, 'message': msg, 'data': None})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'code': 500, 'message': str(e), 'data': None})


@app.route('/api/task/trading/accounts', methods=['GET'])
def trading_accounts():
    """获取可选 OKX 账号列表（供前端定时任务选择执行账号）"""
    try:
        from .api_config import list_accounts
        return jsonify({'code': 200, 'message': 'success', 'data': list_accounts()})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'code': 500, 'message': str(e), 'data': None})


@app.route('/api/task/trading/stop', methods=['POST'])
def trading_stop():
    """停止实盘交易调度器"""
    try:
        from .task.scheduler import task_scheduler
        ok, msg = task_scheduler.stop_trading_scheduler()
        return jsonify({'code': 200 if ok else 400, 'message': msg, 'data': None})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'code': 500, 'message': str(e), 'data': None})


@app.route('/api/task/trading/status', methods=['GET'])
def trading_status():
    """获取实盘交易调度器状态"""
    try:
        from .task.scheduler import task_scheduler
        return jsonify({'code': 200, 'message': 'success', 'data': task_scheduler.get_trading_status()})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'code': 500, 'message': str(e), 'data': None})


@app.route('/api/task/trading/runtime', methods=['GET'])
def trading_runtime():
    """获取实盘「期望运行状态 + 重启自动拉起开关」（低频接口，会读库）"""
    try:
        from .task.scheduler import task_scheduler
        return jsonify({'code': 200, 'message': 'success',
                        'data': task_scheduler.get_runtime_status()})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'code': 500, 'message': str(e), 'data': None})


@app.route('/api/task/trading/runtime/auto-resume', methods=['POST'])
def trading_auto_resume_switch():
    """打开/关闭「进程重启后自动拉起实盘」开关

    自动拉起会在地位无人看管时重新开始下单，所以默认关闭，必须人工在页面
    打开一次；打开后只在「上一次确实是运行状态」时才拉起，手动停止会同时
    清除期望运行标记。
    """
    try:
        from .task.scheduler import task_scheduler
        data = request.get_json(silent=True) or {}
        enabled = bool(data.get('enabled'))
        rt = task_scheduler.set_auto_resume(enabled)
        return jsonify({
            'code': 200,
            'message': ('已打开重启自动拉起：下次进程重启后会自动恢复实盘调度（会发邮件告知）'
                        if enabled else '已关闭重启自动拉起：进程重启后实盘保持停止，需手动启动'),
            'data': rt,
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({'code': 500, 'message': str(e), 'data': None})


# =====================================================================
# 强制平仓 API
# =====================================================================

@app.route('/api/task/trading/force-close', methods=['POST'])
def trading_force_close():
    """强制平仓（全平/平多/平空）"""
    try:
        from .task.scheduler import task_scheduler
        data = request.get_json() or {}
        close_type = data.get('type', 'all')  # all / long / short
        if close_type not in ('all', 'long', 'short'):
            return jsonify({'code': 400, 'message': 'type 必须是 all/long/short', 'data': None})

        ok, msg = task_scheduler.force_close_positions(close_type)
        return jsonify({'code': 200 if ok else 400, 'message': msg, 'data': None})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'code': 500, 'message': str(e), 'data': None})


# =====================================================================
# 日志查看 API
# =====================================================================

_LOG_DIR = _os.path.join(_os.path.dirname(__file__), 'logs')
_VALID_LOG_FILES = {
    'task_scheduler': 'task_scheduler.log',
    'trade_operations': 'trade_operations.log',
}


@app.route('/api/task/logs', methods=['GET'])
def task_logs():
    """读取日志文件内容，支持关键字搜索和时间范围过滤
    参数:
        file: task_scheduler | trade_operations
        keyword: 搜索关键字
        start_time: 开始时间 (YYYY-MM-DD HH:MM:SS)
        end_time: 结束时间 (YYYY-MM-DD HH:MM:SS)
        limit: 最大返回行数，默认 500
        order: asc | desc，默认 desc（最新在前）
    """
    try:
        file_key = request.args.get('file', 'task_scheduler')
        if file_key not in _VALID_LOG_FILES:
            return jsonify({'code': 400, 'message': f'无效的日志文件: {file_key}', 'data': None})

        log_path = _os.path.join(_LOG_DIR, _VALID_LOG_FILES[file_key])
        if not _os.path.exists(log_path):
            return jsonify({'code': 200, 'message': '日志文件不存在（尚未产生日志）', 'data': {'lines': [], 'total': 0}})

        keyword = request.args.get('keyword', '').strip()
        start_time = request.args.get('start_time', '').strip()
        end_time = request.args.get('end_time', '').strip()
        limit = int(request.args.get('limit', 500))
        order = request.args.get('order', 'desc')

        with open(log_path, 'r', encoding='utf-8') as f:
            raw_lines = f.readlines()

        # 解析并过滤
        filtered = []
        for line in raw_lines:
            line = line.rstrip('\n')
            if not line:
                continue

            # 提取时间戳 [YYYY-MM-DD HH:MM:SS]
            line_time = ''
            if len(line) > 21 and line[0] == '[':
                line_time = line[1:20]

            # 时间范围过滤
            if start_time and line_time and line_time < start_time:
                continue
            if end_time and line_time and line_time > end_time:
                continue

            # 关键字过滤
            if keyword and keyword.lower() not in line.lower():
                continue

            filtered.append(line)

        # 排序
        if order == 'desc':
            filtered.reverse()

        # 截断
        total = len(filtered)
        if limit > 0:
            filtered = filtered[:limit]

        return jsonify({
            'code': 200,
            'message': 'success',
            'data': {
                'lines': filtered,
                'total': total,
                'returned': len(filtered),
                'file': file_key,
            }
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({'code': 500, 'message': str(e), 'data': None})


@app.route('/api/task/logs/download', methods=['GET'])
def task_logs_download():
    """下载日志文件
    参数: file: task_scheduler | trade_operations
    """
    try:
        file_key = request.args.get('file', 'task_scheduler')
        if file_key not in _VALID_LOG_FILES:
            return jsonify({'code': 400, 'message': f'无效的日志文件: {file_key}', 'data': None})

        log_path = _os.path.join(_LOG_DIR, _VALID_LOG_FILES[file_key])
        if not _os.path.exists(log_path):
            return jsonify({'code': 404, 'message': '日志文件不存在', 'data': None})

        return send_file(
            log_path,
            mimetype='text/plain',
            as_attachment=True,
            download_name=_VALID_LOG_FILES[file_key]
        )
    except Exception as e:
        traceback.print_exc()
        return jsonify({'code': 500, 'message': str(e), 'data': None})


@app.route('/api/task/logs/clear', methods=['POST'])
def task_logs_clear():
    """清除日志文件内容
    参数(JSON): file: task_scheduler | trade_operations
    """
    try:
        data = request.get_json(force=True, silent=True) or {}
        file_key = data.get('file', '')
        if file_key not in _VALID_LOG_FILES:
            return jsonify({'code': 400, 'message': f'无效的日志文件: {file_key}', 'data': None})

        log_path = _os.path.join(_LOG_DIR, _VALID_LOG_FILES[file_key])
        if not _os.path.exists(log_path):
            return jsonify({'code': 200, 'message': '日志文件不存在，无需清除', 'data': None})

        # 清空文件内容
        with open(log_path, 'w', encoding='utf-8') as f:
            f.truncate(0)

        name_map = {'task_scheduler': '定时任务日志', 'trade_operations': '交易操作日志'}
        return jsonify({'code': 200, 'message': f'{name_map.get(file_key, file_key)} 已清除', 'data': None})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'code': 500, 'message': str(e), 'data': None})


# =====================================================================
# 测试邮件 API
# =====================================================================

@app.route('/api/task/email/test', methods=['POST'])
def email_test():
    """发送测试邮件"""
    try:
        from .task.scheduler import task_scheduler
        data = request.get_json() or {}
        to_email = data.get('to_email', '').strip() or None
        ok, msg = task_scheduler.send_test_email(to_email)
        return jsonify({'code': 200 if ok else 400, 'message': msg, 'data': None})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'code': 500, 'message': str(e), 'data': None})


# =====================================================================
# 系统监控 API（内存自监控 + 系统级健康检查，/system-status 页面使用）
# =====================================================================

@app.route('/api/system/status', methods=['GET'])
def system_status():
    """系统概况：内存监控线程状态 + 最近一轮系统检查结果 + 本次启动归因"""
    try:
        from .memory_watchdog import get_memory_status
        from .system_monitor import get_last_result
        from .process_lifecycle import get_boot_info
        return jsonify({'code': 200, 'data': {
            'watchdog': get_memory_status(),
            'last_check': get_last_result(),
            # 启动归因由后台启动线程算一次并缓存；CRYPTO_NO_BACKGROUND 或
            # 归因层坏掉时为 None，前端按“未归因”展示，不能因此报错。
            'boot': get_boot_info(),
        }})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'code': 500, 'message': str(e), 'data': None})


@app.route('/api/system/check', methods=['POST'])
def system_check_now():
    """手动触发一轮系统级检查（同步执行，返回本轮结果）"""
    try:
        from .system_monitor import run_check
        return jsonify({'code': 200, 'message': '检测完成', 'data': run_check(source='embedded')})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'code': 500, 'message': str(e), 'data': None})


@app.route('/api/system/memory/history', methods=['GET'])
def system_memory_history():
    """内存历史趋势：读本地 logs/memory_history.jsonl
    参数:
        limit: 采样点数上限，默认 1000，最大 10000（按采样频率约含 4~35 天数据）
        source: 可选 app（进程内自监控）| guard（系统监控外部采样）
    """
    try:
        from .memory_watchdog import read_history, get_memory_status
        limit = min(max(int(request.args.get('limit', 1000)), 10), 10000)
        source = request.args.get('source', '').strip()
        if source not in ('', 'app', 'guard'):
            return jsonify({'code': 400, 'message': 'source 必须是 app|guard', 'data': None})
        st = get_memory_status()
        return jsonify({'code': 200, 'data': {
            'records': read_history(limit=limit, source=source or None),
            'warn_mb': st['warn_mb'],
            'kill_mb': st['kill_mb'],
            'history_file': st['history_file'],
        }})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'code': 500, 'message': str(e), 'data': None})
