#!/usr/bin/env python
# -*- coding: utf-8 -*-
# [迁移] 本文件由 futureStockTrade 合并迁移 Phase 4 自动搬运（2026-10-08）。
# 原名: kline/kline_blueprint.py  →  现位置: crypto/kline_training/routes.py
# 导入改为 crypto.* 显式包路径，sys.path 拼接兜底移除，会话键 uid → kline_uid。
"""
K线训练模块 Flask 蓝图（多品类）
================================
提供K线训练功能的页面路由和 API 接口。URL 前缀为 /kline/。

品类：加密币(crypto)、期货(futures)、股票(stock) —— 三品类均已打通，统一走
品类无关的 ``training_service`` + ``providers`` + ``registry`` 数据层。

路由概览
--------
页面：
  - ``/``                         首页（分类选择）
  - ``/train/<category>/<symbol>`` 泛化训练页（三品类共用 kline_train.html）
  - ``/crypto/<symbol>``          向后兼容 → 等价于 /train/crypto/<symbol>
API（品类感知）：
  - ``/api/categories``                    品类元信息（long_only/periods）
  - ``/api/symbols/<category>``            符号清单（?grouped=true 分组）
  - ``/api/kline-data/<category>/<symbol>`` 训练图表数据（支持 period/start/end/…）
  - ``/api/kline-more/<category>/<symbol>`` 惰性加载 before_ts 之前历史
API（向后兼容，仅 crypto）：
  - ``/api/available-symbols``、``/api/health``
  - ``/api/kline-data/<symbol>``、``/api/kline-more/<symbol>``

错误响应约定
------------
业务/网络错误一律返回与 code 一致的 HTTP 状态码（ConnectionError→502，
ValueError→400，其他→500）。前端仍读 body 的 code 字段。
"""

import traceback
from functools import wraps

from flask import (Blueprint, abort, g, jsonify, redirect, render_template,
                   request, session, url_for)


def _err(message, status, extra=None):
    """统一错误响应（HTTP 状态码与 body.code 保持一致）"""
    return jsonify({"code": status, "message": message, "data": extra}), status


def _ok(data):
    """统一成功响应"""
    return jsonify({"code": 200, "message": "success", "data": data})


def _api_exception(e):
    """把底层异常翻译成对前端友好的错误码与提示"""
    if isinstance(e, ConnectionError):
        # OKX 不可达：几乎都是代理问题，把诊断信息一并返回给前端展示
        detail = str(e)
        try:
            from crypto.kline_training.kline_adapter import okx_healthcheck
            probe = okx_healthcheck()
            probe["message"] = detail
            return _err(detail, 502, probe)
        except Exception:
            return _err(detail, 502)
    if isinstance(e, ValueError):
        return _err(str(e), 400)
    traceback.print_exc()
    return _err(f"服务内部错误: {type(e).__name__}: {e}", 500)


def _validate_cat(category, symbol, period):
    """按品类校验品种与周期。

    返回 (错误响应, None) 或 (None, (category, code, provider))。
    """
    from crypto.kline_training import registry
    from crypto.kline_training.providers import get_provider, normalize_category
    try:
        cat = normalize_category(category)
        rec = registry.resolve(cat, symbol)
    except ValueError as e:
        return _err(str(e), 400), None
    provider = get_provider(cat)
    if period not in provider.periods:
        return _err(f"品类 {cat} 不支持周期: {period}，可选: "
                    f"{', '.join(provider.periods)}", 400), None
    return None, (cat, rec['code'], provider)


# 兼容旧白名单常量（实际按品类 provider.periods 校验）
VALID_PERIODS = {'1m', '5m', '15m', '30m', '1H', '4H', '1D', '1W'}


# ====================================================================
#  创建蓝图
# ====================================================================
kline_bp = Blueprint(
    'kline',
    __name__,
    template_folder='templates',
    static_folder='static',
)


# ====================================================================
#  轻量鉴权：current_user / login_required / 全局注入
# ====================================================================

def current_user():
    """从 Flask session 取当前登录用户（每请求缓存于 g）；未登录返回 None。"""
    if 'cu' in g:
        return g.cu
    uid = session.get('kline_uid')
    u = None
    if uid:
        from crypto.kline_training import store as db
        row = db.get_user_by_id(uid)
        if row:
            u = {'id': row['id'], 'email': row.get('email'),
                 'nickname': row.get('nickname'), 'phone': row.get('phone')}
            try:
                db.touch_user_last_seen(row['id'])   # 心跳：登录用户每请求刷新在线
            except Exception:
                pass
        else:
            session.pop('kline_uid', None)          # 用户已不存在 → 失效登录态
    g.cu = u
    return u


def login_required(fn):
    """保护路由：未登录时 API 返回 401、页面重定向到登录页。"""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not current_user():
            if '/api/' in request.path or request.is_json:
                return _err('请先登录', 401)
            nxt = request.path
            return redirect(url_for('kline.auth_page', next=nxt))
        return fn(*args, **kwargs)
    return wrapper


@kline_bp.context_processor
def _inject_user():
    """向本蓝图渲染的模板注入当前登录用户（含 {% include %} 进来的工具条）。

    口径变化（合并迁移 Phase 4）：原项目用 app_context_processor 往**全站**模板
    注入 current_user，是因为它的 nav.html 要显示用户名。合并进宿主后导航是
    数据驱动的，宿主 nav.html 没有用户区，而 current_user() 每次调用都要读一次
    SQLite（还带心跳写库）。因此收窄成蓝图级 context_processor：
      - 只有 K 线自己的页面会触发这次查询，加密货币/期货/股票页面零开销；
      - K 线模板 include 的 kline_account_bar.html 共享同一份上下文，照常可取。
    """
    return {'current_user': current_user()}


# ====================================================================
#  页面路由
# ====================================================================

@kline_bp.route('/')
def kline_index():
    """K线训练首页（分类选择页）"""
    return render_template('kline_index.html', active_page='kline')


def _render_train(category, symbol):
    """校验并渲染泛化训练页；非法品类/符号 → 404"""
    from crypto.kline_training import registry
    from crypto.kline_training.providers import get_provider, normalize_category
    try:
        cat = normalize_category(category)
        rec = registry.resolve(cat, symbol)
    except ValueError:
        abort(404)
    provider = get_provider(cat)
    return render_template(
        'kline_train.html',
        category=cat,
        symbol=rec['code'],
        display_name=rec.get('name', rec['code']),
        exchange=rec.get('exchange', ''),
        category_label=provider.display_name,
        long_only=bool(provider.long_only),
        periods=list(provider.periods),
        active_page='kline',
    )


@kline_bp.route('/train/<category>/<symbol>')
def kline_train(category, symbol):
    """泛化训练页（加密币/期货/股票共用）"""
    return _render_train(category, symbol)


@kline_bp.route('/crypto/<symbol>')
def kline_crypto(symbol):
    """加密币训练页（向后兼容旧链接 → 泛化训练页）"""
    return _render_train('crypto', symbol)


@kline_bp.route('/blind')
def kline_blind():
    """盲选训练页（服务器随机分配隐藏品种，前端调 /api/blind/start 取题）"""
    return render_template(
        'kline_train.html',
        blind=True,
        category='blind',
        symbol='',
        display_name='神秘品种',
        exchange='',
        category_label='盲选训练',
        long_only=False,
        periods=[],
        active_page='kline',
    )


# ====================================================================
#  页面路由 —— PK 对战（大厅 / 训练 / 单场榜 / 权益对比）
# ====================================================================

@kline_bp.route('/pk')
@login_required
def pk_index():
    """对战大厅：创建/加入/我的对战/排行榜。"""
    return render_template('pk_index.html', active_page='pk')


def _pk_train_context(m):
    """为 kline_train.html 构造 PK 训练页上下文（题面锁定；盲选未结束隐品种/日期）。"""
    from crypto.kline_training import registry
    from crypto.kline_training.providers import get_provider
    provider = get_provider(m['category'])
    finished = m['status'] == 'finished'
    masked = bool(m.get('blind')) and not finished
    if masked:
        display_name, exchange = '神秘品种', ''
    else:
        try:
            rec = registry.resolve(m['category'], m['symbol'])
            display_name = rec.get('name', m['symbol'])
            exchange = rec.get('exchange', '')
        except Exception:
            display_name, exchange = m['symbol'], ''
    return {
        'pk': True, 'pk_match_id': m['id'], 'pk_code': m['code'],
        'pk_status': m['status'],
        'pk_pvp_mode': (m.get('pvp_mode') or 'code'),
        'pk_auto_sync': bool(m.get('auto_sync')),
        'blind': masked,                      # 复用盲选水印/遮罩渲染（仅 pk-blind）
        'category': m['category'],
        'symbol': ('' if masked else m['symbol']),   # 隐藏时不把真实代码写入页面
        'display_name': display_name, 'exchange': exchange,
        'category_label': provider.display_name,
        'long_only': bool(provider.long_only),        # 如实（盲选也告知品类→long_only）
        'periods': [],                                # 锁定周期：不渲染周期按钮
    }


@kline_bp.route('/pk/train/<ident>')
@login_required
def pk_train(ident):
    """PK 训练页：同题锁定。邀请码模式未加入自动占位加入；组队模式由
    前端轮询房间状态驱动大厅/就绪/进行中，非成员可观战。"""
    from crypto.kline_training import store as db
    m = _pk_find(ident)
    if not m:
        abort(404)
    me = current_user()['id']
    team = (m.get('pvp_mode') or 'code') == 'team'
    if not team and not db.get_result(m['id'], me) and m['status'] != 'finished':
        db.submit_result(m['id'], me)
        participants, _ = db.match_completion(m['id'])
        if m['status'] == 'open' and participants >= 2:
            db.update_match_status(m['id'], 'in-progress')
        m = db.get_match(m['id'])
    ctx = _pk_train_context(m)
    ctx['pk_spectate'] = team and not db.get_result(m['id'], me)
    return render_template('kline_train.html', active_page='pk', **ctx)


@kline_bp.route('/pk/board/<ident>')
@login_required
def pk_board(ident):
    """单场排行榜页。"""
    m = _pk_find(ident)
    if not m:
        abort(404)
    return render_template('pk_board.html', match_id=m['id'], code=m['code'],
                           active_page='pk')


@kline_bp.route('/pk/compare/<ident>')
@login_required
def pk_compare(ident):
    """多人权益曲线叠加对比页。"""
    m = _pk_find(ident)
    if not m:
        abort(404)
    return render_template('pk_compare.html', match_id=m['id'], code=m['code'],
                           active_page='pk')


# ====================================================================
#  API 路由 —— 品类元信息 / 符号
# ====================================================================

@kline_bp.route('/api/categories', methods=['GET'])
def api_categories():
    """三品类元信息（display_name / long_only / periods），供首页与前端渲染"""
    try:
        from crypto.kline_training import registry
        return _ok(registry.list_categories())
    except Exception as e:
        return _api_exception(e)


@kline_bp.route('/api/symbols/<category>', methods=['GET'])
def api_symbols(category):
    """某品类的符号清单。?grouped=true 返回按行业/种类分组的结构。"""
    grouped = request.args.get('grouped', 'false').lower() == 'true'
    try:
        from crypto.kline_training import registry
        data = (registry.list_symbol_groups(category) if grouped
                else registry.list_symbols(category))
        return _ok(data)
    except Exception as e:
        return _api_exception(e)


@kline_bp.route('/api/available-symbols', methods=['GET'])
def api_available_symbols():
    """[向后兼容] 加密币品种列表（沿用 kline_adapter.CRYPTO_SYMBOLS）"""
    try:
        from crypto.kline_training.kline_adapter import get_available_symbols
        return _ok(get_available_symbols())
    except Exception as e:
        return _api_exception(e)


@kline_bp.route('/api/health', methods=['GET'])
def api_health():
    """连通性自检：探测代理/直连两条通道能否访问 OKX，用于页面故障提示"""
    try:
        from crypto.kline_training.kline_adapter import okx_healthcheck
        return _ok(okx_healthcheck())
    except Exception as e:
        return _api_exception(e)


# ====================================================================
#  API 路由 —— 品类感知训练数据
# ====================================================================

@kline_bp.route('/api/kline-data/<category>/<symbol>', methods=['GET'])
def api_kline_data_cat(category, symbol):
    """获取品类感知的K线图表数据（含指标）

    Query Parameters
    ----------------
    period : str        周期，默认 '1H'（须在该品类 periods 白名单内）
    start, end : str    固定区间（unix秒/毫秒 或 'YYYY-MM-DD' 等），可选
    limit : int         无区间时返回最近 N 根，默认 500
    full_data : bool    训练模式，配合 target_bars
    target_bars : int   训练模式返回根数，默认 600，范围 50~2000
    """
    period = request.args.get('period', '1H')
    error, ctx = _validate_cat(category, symbol, period)
    if error:
        return error
    cat, code, _provider = ctx

    try:
        from crypto.kline_training import training_service as ts

        start = request.args.get('start') or None
        end = request.args.get('end') or None
        limit = request.args.get('limit', 500, type=int)
        full_data = request.args.get('full_data', 'false').lower() == 'true'
        target_bars = request.args.get('target_bars', 600, type=int)

        # 参数范围校验（防止构造超大请求打爆上游与内存）
        target_bars = max(50, min(target_bars, 2000))
        limit = max(50, min(limit, 2000))

        data = ts.get_training_chart_data(
            cat, code, period, start=start, end=end,
            limit=limit, full_data=full_data, target_bars=target_bars)

        if not data or not data.get('candles'):
            return _err(data.get('error', '未获取到K线数据') if data
                        else '未获取到K线数据', 502)
        return _ok(data)
    except Exception as e:
        return _api_exception(e)


@kline_bp.route('/api/kline-more/<category>/<symbol>', methods=['GET'])
def api_kline_more_cat(category, symbol):
    """[品类感知] 惰性加载：获取 before_ts 之前的更多历史K线"""
    period = request.args.get('period', '1H')
    error, ctx = _validate_cat(category, symbol, period)
    if error:
        return error
    cat, code, _provider = ctx

    try:
        from crypto.kline_training import training_service as ts

        before_ts = request.args.get('before_ts', None, type=int)
        if not before_ts:
            return _err('缺少参数 before_ts', 400)
        target_bars = request.args.get('target_bars', 200, type=int)
        target_bars = max(50, min(target_bars, 1000))

        data = ts.load_more_chart_data(cat, code, period,
                                       before_ts=before_ts, target_bars=target_bars)
        return _ok(data)
    except Exception as e:
        return _api_exception(e)


# ====================================================================
#  API 路由 —— 盲选训练（分配 / 揭晓）
# ====================================================================

@kline_bp.route('/api/blind/start', methods=['POST', 'GET'])
def api_blind_start():
    """盲选开始：服务器随机选品类+品种+周期+历史区间，答案按 training_id 存
    SQLite，**只返回 training_id + 去标识化 chart**（答案不下发）。

    可选参数（JSON body 或 query）：bar_count（默认 250）、categories（默认三品类）。
    """
    try:
        from crypto.kline_training import training_service as ts
        from crypto.kline_training import store as db

        payload = request.get_json(silent=True) or {}
        bar_count = int(payload.get('bar_count')
                        or request.args.get('bar_count') or 250)
        bar_count = max(60, min(bar_count, 2000))

        cats = payload.get('categories') or request.args.get('categories') or ''
        if isinstance(cats, str):
            cats = [c.strip() for c in cats.split(',') if c.strip()]
        cats = cats or None

        result = ts.start_blind(bar_count=bar_count, categories=cats)
        training_id = result['training_id']
        ans = result['answer']
        db.create_blind(
            training_id,
            category=ans['category'], symbol=ans['symbol'], name=ans['name'],
            period=ans['period'], start_ts=ans['start_ts'], end_ts=ans['end_ts'],
            bar_count=ans['bar_count'], seed=ans['seed'])
        return _ok({'training_id': training_id, 'chart': result['chart']})
    except Exception as e:
        return _api_exception(e)


@kline_bp.route('/api/blind/<training_id>/reveal', methods=['GET', 'POST'])
def api_blind_reveal(training_id):
    """揭晓盲选答案：返回真实品类/品种/名称/区间，并标记为已揭晓。"""
    try:
        from crypto.kline_training import store as db
        from crypto.kline_training import registry
        from crypto.kline_training.providers import get_provider

        row = db.get_blind(training_id)
        if not row:
            return _err('盲选记录不存在或已过期', 404)

        cat = row.get('category')
        exchange, category_label, long_only = '', cat, False
        try:
            rec = registry.resolve(cat, row.get('symbol'))
            exchange = rec.get('exchange', '')
            provider = get_provider(cat)
            category_label = provider.display_name
            long_only = bool(provider.long_only)
        except Exception:
            rec = {'name': row.get('name'), 'code': row.get('symbol')}

        db.mark_blind_revealed(training_id)
        return _ok({
            'training_id': training_id,
            'category': cat,
            'symbol': row.get('symbol'),
            'name': row.get('name') or rec.get('name'),
            'exchange': exchange,
            'category_label': category_label,
            'long_only': long_only,
            'period': row.get('period'),
            'start_ts': row.get('start_ts'),
            'end_ts': row.get('end_ts'),
            'bar_count': row.get('bar_count'),
            'seed': row.get('seed'),
            'revealed': True,
        })
    except Exception as e:
        return _api_exception(e)


# ====================================================================
#  API 路由 —— 训练会话 / 成绩持久化
# ====================================================================

_VALID_MODES = ('free', 'range', 'blind', 'pk')


@kline_bp.route('/api/session', methods=['POST'])
def api_session_save():
    """保存一次训练成绩（统计 + 操作流水 + 权益曲线）。

    匿名即可提交（user_id 暂为 None，P3 鉴权后归属账号）。盲选成绩可携
    blind_id，合并存入 stats_json 以保留与盲选答案的关联（无需改表结构）。
    """
    try:
        from crypto.kline_training import store as db
        p = request.get_json(silent=True) or {}

        stats = p.get('stats') or {}
        blind_id = p.get('blind_id')
        if blind_id and isinstance(stats, dict):
            stats = dict(stats)
            stats['_blind_id'] = blind_id

        mode = p.get('mode') or 'free'
        if mode not in _VALID_MODES:
            mode = 'free'

        session_id = db.save_session(
            category=(p.get('category') or 'unknown'),
            symbol=(p.get('symbol') or ''),
            period=(p.get('period') or ''),
            mode=mode,
            user_id=(current_user() or {}).get('id'),
            seed=p.get('seed'),
            start_ts=p.get('start_ts'),
            bar_count=p.get('bar_count'),
            stats=stats,
            trades=p.get('trades') or [],
            equity=p.get('equity') or [])
        return _ok({'session_id': session_id})
    except Exception as e:
        return _api_exception(e)


@kline_bp.route('/api/session/history', methods=['GET'])
def api_session_history():
    """最近训练成绩列表（精简字段，不回传完整 trades/equity）。

    暂无鉴权→返回全局最近记录；P3 鉴权后可按登录用户过滤。
    """
    try:
        from crypto.kline_training import store as db
        limit = request.args.get('limit', 20, type=int)
        limit = max(1, min(limit, 100))
        cu = current_user()
        rows = db.list_sessions(user_id=(cu['id'] if cu else None), limit=limit)
        out = [{
            'id': r.get('id'), 'category': r.get('category'),
            'symbol': r.get('symbol'), 'period': r.get('period'),
            'mode': r.get('mode'), 'return_pct': r.get('return_pct'),
            'win_rate': r.get('win_rate'),
            'max_drawdown_pct': r.get('max_drawdown_pct'),
            'profit_loss_ratio': r.get('profit_loss_ratio'),
            'final_balance': r.get('final_balance'),
            'total_trades': r.get('total_trades'), 'sharpe': r.get('sharpe'),
            'created_at': r.get('created_at'),
        } for r in rows]
        return _ok(out)
    except Exception as e:
        return _api_exception(e)


@kline_bp.route('/api/session/<int:session_id>', methods=['GET'])
def api_session_detail(session_id):
    """单次训练成绩详情（含完整 stats/trades/equity，供回放/对比）"""
    try:
        from crypto.kline_training import store as db
        row = db.get_session(session_id)
        if not row:
            return _err('训练记录不存在', 404)
        return _ok(row)
    except Exception as e:
        return _api_exception(e)


# ====================================================================
#  鉴权：页面 + API（注册 / 登录 / 登出 / 当前用户 / 改密 / 备注）
# ====================================================================

@kline_bp.route('/auth')
def auth_page():
    """登录 / 注册页（注册即邮箱验证码登录；下次可用邮箱/手机/昵称+口令）。"""
    return render_template('auth.html', next=request.args.get('next') or '',
                           active_page='kline')


@kline_bp.route('/api/auth/send-code', methods=['POST'])
def api_auth_send_code():
    """发送邮箱验证码（purpose: register|reset）。带重发冷却。"""
    try:
        import time
        from crypto.kline_training import settings as config
        from crypto.kline_training import store as db
        from crypto.kline_training import auth_service as au
        p = request.get_json(silent=True) or {}
        email = p.get('email') or ''
        purpose = (p.get('purpose') or 'register').strip().lower()
        if purpose not in ('register', 'reset'):
            purpose = 'register'
        rec = db.get_verify_code(au.norm_email(email), purpose)
        if rec:
            elapsed = int(time.time()) - rec['created_at']
            if elapsed < config.EMAIL_CODE_RESEND_COOLDOWN:
                wait = config.EMAIL_CODE_RESEND_COOLDOWN - elapsed
                return _err(f'请 {wait} 秒后再获取验证码', 429)
        r = au.send_code(email, purpose=purpose)
        data = {'sent': bool(r['ok']), 'purpose': purpose}
        if not r['ok'] and r.get('dev_code'):
            data['dev_code'] = r['dev_code']        # 开发兜底：直接回传验证码
            data['notice'] = 'SMTP 未就绪，已走开发兜底（验证码见返回/日志）'
        elif not r['ok']:
            return _err('验证码邮件发送失败：' + (r.get('err') or ''), 502)
        return _ok(data)
    except Exception as e:
        return _api_exception(e)


@kline_bp.route('/api/auth/register', methods=['POST'])
def api_auth_register():
    """邮箱验证码注册（注册即登录）。返回用户 + 自动生成的默认口令。"""
    try:
        from crypto.kline_training import auth_service as au
        p = request.get_json(silent=True) or {}
        user, pw = au.register(p.get('email'), p.get('code'),
                               p.get('nickname'), p.get('phone'))
        session['kline_uid'] = user['id']
        session.permanent = True
        return _ok({'user': user, 'default_password': pw})
    except Exception as e:
        return _api_exception(e)


@kline_bp.route('/api/auth/login', methods=['POST'])
def api_auth_login():
    """登录：邮箱/手机号/昵称 + 口令；或 email + code（验证码登录/找回密码）。

    失败时区分「账号不存在」(404) 与「密码错误」(401)，不再混为一谈。
    """
    try:
        from crypto.kline_training import auth_service as au
        p = request.get_json(silent=True) or {}
        if p.get('code'):
            user = au.login_by_code(p.get('email'), p.get('code'))
            err = None
        else:
            user, err = au.login(p.get('identifier') or p.get('email'),
                                 p.get('password'))
        if not user:
            if err == 'not_found':
                return _err('账号不存在：没有匹配该邮箱/手机号/昵称的账号', 404)
            if err == 'empty':
                return _err('请输入账号（邮箱/手机号/昵称）', 400)
            return _err('密码错误：账号存在，但口令不正确', 401)
        session['kline_uid'] = user['id']
        session.permanent = True
        return _ok({'user': user})
    except Exception as e:
        return _api_exception(e)


@kline_bp.route('/api/auth/logout', methods=['POST'])
def api_auth_logout():
    """登出：清除会话登录态。"""
    session.pop('kline_uid', None)
    return _ok({'ok': True})


@kline_bp.route('/api/auth/me', methods=['GET'])
def api_auth_me():
    """当前登录用户（未登录返回 data.user=None）。"""
    return _ok({'user': current_user()})


@kline_bp.route('/api/auth/password', methods=['POST'])
@login_required
def api_auth_password():
    """登录用户自行修改密码。"""
    try:
        from crypto.kline_training import auth_service as au
        p = request.get_json(silent=True) or {}
        au.change_password(current_user()['id'], p.get('new_password'))
        return _ok({'ok': True})
    except Exception as e:
        return _api_exception(e)


# ====================================================================
#  个人中心（自助改昵称 / 手机号 / 密码）+ 管理员重置
# ====================================================================

@kline_bp.route('/profile')
@login_required
def profile_page():
    """个人资料页：自助修改昵称/手机号/密码；管理员额外展示重置面板。"""
    from crypto.kline_training import settings as config
    cu = current_user()
    return render_template(
        'profile.html', active_page='profile',
        me=cu, is_admin=(cu.get('email') or '').lower() in config.ADMIN_EMAILS)


@kline_bp.route('/api/profile/nickname', methods=['POST'])
@login_required
def api_profile_nickname():
    """改昵称（非空、≤ 24、全局唯一）。仅改自己。"""
    try:
        from crypto.kline_training import auth_service as au
        p = request.get_json(silent=True) or {}
        au.update_nickname(current_user()['id'], p.get('nickname'))
        return _ok({'ok': True, 'nickname': (p.get('nickname') or '').strip()})
    except Exception as e:
        return _api_exception(e)


@kline_bp.route('/api/profile/phone', methods=['POST'])
@login_required
def api_profile_phone():
    """改手机号（选填，非空需格式合法且全局唯一）。仅改自己。"""
    try:
        from crypto.kline_training import auth_service as au
        p = request.get_json(silent=True) or {}
        au.update_phone(current_user()['id'], p.get('phone'))
        return _ok({'ok': True, 'phone': (p.get('phone') or '').strip() or None})
    except Exception as e:
        return _api_exception(e)


@kline_bp.route('/api/profile/change-password', methods=['POST'])
@login_required
def api_profile_change_password():
    """改密码：需 old_password 验证 + new_password（≥ 4 位）。仅改自己。"""
    try:
        from crypto.kline_training import auth_service as au
        p = request.get_json(silent=True) or {}
        me = current_user()['id']
        if not au.verify_password(me, p.get('old_password')):
            return _err('原密码不正确', 401)
        au.change_password(me, p.get('new_password'))
        return _ok({'ok': True})
    except Exception as e:
        return _api_exception(e)


@kline_bp.route('/api/admin/reset-password', methods=['POST'])
def api_admin_reset_password():
    """管理员重置任意用户密码。仅白名单邮箱可调用（config.ADMIN_EMAILS）。"""
    try:
        import logging
        from crypto.kline_training import store as db
        p = request.get_json(silent=True) or {}
        admin_email = p.get('admin_email') or ''
        target = p.get('target_email') or p.get('target') or ''
        new_password = p.get('new_password') or ''
        row = db.reset_password_by_admin(admin_email, target, new_password)
        logging.getLogger(__name__).warning(
            '[ADMIN] %s 重置了用户 id=%s(%s) 的密码',
            (admin_email or '').strip().lower(), row['id'], row['email'])
        return _ok({'ok': True, 'user': row})
    except PermissionError as e:
        return _err(str(e) or '无管理员权限', 403)
    except Exception as e:
        return _api_exception(e)


@kline_bp.route('/api/users', methods=['GET'])
@login_required
def api_users():
    """全员名单（全透明）；附当前用户为各人设的备注、展示名与在线状态。"""
    try:
        import time
        from crypto.kline_training import store as db
        me = current_user()['id']
        notes = db.get_notes_map(me)
        now = int(time.time())
        rows = db.list_users()
        for r in rows:
            r['note'] = notes.get(r['id'], '')
            r['is_me'] = (r['id'] == me)
            r['display'] = r['note'] or r['nickname'] or r['email']
            ls = r.get('last_seen') or 0
            r['online'] = (now - ls) <= _ONLINE_WINDOW
        return _ok(rows)
    except Exception as e:
        return _api_exception(e)


@kline_bp.route('/api/presence', methods=['POST'])
@login_required
def api_presence():
    """心跳端点：仅刷新当前用户 last_seen（供无其它请求时的空闲页保持在线）。"""
    try:
        from crypto.kline_training import store as db
        db.touch_user_last_seen(current_user()['id'])
        return _ok({'ok': True})
    except Exception as e:
        return _api_exception(e)


@kline_bp.route('/api/users/<int:target_id>/note', methods=['POST'])
@login_required
def api_user_note(target_id):
    """当前用户给 target 设/改/清备注（note 为空则清除）。"""
    try:
        from crypto.kline_training import store as db
        me = current_user()['id']
        if target_id == me:
            return _err('不能给自己设备注', 400)
        if not db.get_user_by_id(target_id):
            return _err('用户不存在', 404)
        p = request.get_json(silent=True) or {}
        note = db.set_user_note(me, target_id, p.get('note'))
        return _ok({'note': note})
    except Exception as e:
        return _api_exception(e)


# ====================================================================
#  API 路由 —— PK 异步同题对战
#  （创建 / 加入 / 详情 / 同题取图 / 提交 / 单场榜 / 权益对比 / 我的 / 全局榜）
# ====================================================================

# 房主可调交易参数默认值（决策③：默认值 + 可调；longOnly 由品类强制，不可调）
_ONLINE_WINDOW = 60          # 在线判定窗口（秒）：last_seen 距今 ≤ 此值视为在线
_TEAM_MIN_PLAYERS = 2        # 自由组队开局最少人数
_ENGINE_DEFAULTS = {
    'baseAmount': 100, 'commissionRate': 0.0005, 'leverage': 10,
    'positionSizing': True, 'maxLayers': 10, 'maintenanceMarginRate': 0.005,
    'tpSlEnabled': False, 'tpSlMode': 'percent', 'tpPercent': 5, 'slPercent': 3,
    'protectiveSlPercent': 0,
}


def _sanitize_engine_config(raw, long_only):
    """白名单化 + 范围钳制房主提交的引擎参数，并强制 longOnly=品类值。"""
    cfg = dict(_ENGINE_DEFAULTS)
    raw = raw or {}

    def num(key, lo, hi, cast=float):
        if key in raw and raw[key] is not None:
            try:
                cfg[key] = cast(max(lo, min(hi, cast(raw[key]))))
            except Exception:
                pass
    num('baseAmount', 1, 1_000_000_000)
    num('commissionRate', 0, 0.05)
    num('leverage', 1, 125)
    num('maxLayers', 1, 50, int)
    num('maintenanceMarginRate', 0.0001, 0.2)
    num('tpPercent', 0, 1000)
    num('slPercent', 0, 1000)
    num('protectiveSlPercent', 0, 1000)
    if 'positionSizing' in raw:
        cfg['positionSizing'] = bool(raw['positionSizing'])
    if 'tpSlEnabled' in raw:
        cfg['tpSlEnabled'] = bool(raw['tpSlEnabled'])
    if raw.get('tpSlMode') in ('percent', 'macd', 'sar'):
        cfg['tpSlMode'] = raw['tpSlMode']
    cfg['longOnly'] = bool(long_only)      # 强制按品类（A股只多）
    return cfg


def _pk_find(ident):
    """按对战码（优先）或数字 id 查对战；找不到返回 None。"""
    from crypto.kline_training import store as db
    s = str(ident or '').strip()
    if not s:
        return None
    m = db.get_match_by_code(s)
    if m:
        return m
    if s.isdigit():
        return db.get_match(int(s))
    return None


def _pk_display(notes, uid, nickname, email):
    return notes.get(uid) or nickname or ((email or '').split('@')[0] or '用户%s' % uid)


def _pk_board(match_id, notes):
    """单场排行榜：附带展示名与是否已提交真实成绩。"""
    from crypto.kline_training import store as db
    out = []
    for r in db.get_results(match_id):
        uid = r['user_id']
        out.append({
            'user_id': uid,
            'nickname': r.get('nickname'),
            'display': _pk_display(notes, uid, r.get('nickname'), r.get('email')),
            'return_pct': r.get('return_pct'),
            'max_drawdown_pct': r.get('max_drawdown_pct'),
            'win_rate': r.get('win_rate'),
            'profit_loss_ratio': r.get('profit_loss_ratio'),
            'final_balance': r.get('final_balance'),
            'total_trades': r.get('total_trades'),
            'rank': r.get('rank'),
            'completed': r.get('return_pct') is not None,
            'submitted_at': r.get('submitted_at'),
        })
    return out


def _pk_now_ms():
    import time
    return int(time.time() * 1000)


def _pk_speed_ms(bar_count, target_sec):
    """由目标总时长反推每根毫秒，钳制到 [300,3000] 可玩区间。"""
    if not target_sec or not bar_count:
        return None
    ms = int(round(target_sec * 1000.0 / max(1, int(bar_count))))
    return max(300, min(3000, ms))


def _pk_team_topic(p, cat, provider, bar_count):
    """自由组队选题：与明题/盲选一致。返回 symbol/period/start_ts/end_ts/bar_count/seed。

    非法输入抛 ValueError（由路由转 400）。
    """
    import pandas as pd
    from crypto.kline_training import registry
    from crypto.kline_training import training_service as ts
    from crypto.kline_training.providers.base import parse_time, period_to_seconds
    if p.get('blind'):
        r = ts.start_blind(bar_count=bar_count, categories=[cat])
        ans = r['answer']
        return dict(symbol=ans['symbol'], period=ans['period'],
                    start_ts=int(ans['start_ts']), end_ts=int(ans['end_ts']),
                    bar_count=int(ans['bar_count']), seed=ans['seed'])
    symbol = p.get('symbol')
    if not symbol:
        raise ValueError('明题对战需指定品种 symbol')
    rec = registry.resolve(cat, symbol)
    symbol = rec['code']
    period = p.get('period') or '1D'
    if not provider.supports_period(period):
        raise ValueError('品类 %s 不支持周期 %s' % (cat, period))
    st = parse_time(p.get('start'))
    if st is None:
        raise ValueError('明题对战需指定起始日期 start')
    psec = period_to_seconds(period) or 86400
    et = st + pd.Timedelta(seconds=int((bar_count + 5) * psec))
    chart = ts.get_training_chart_data(
        cat, symbol, period, start=int(st.timestamp()), end=int(et.timestamp()))
    candles = (chart or {}).get('candles') or []
    if len(candles) < max(10, bar_count // 3):
        raise ValueError('该起点取到的K线不足，请更换起始日期或周期')
    mm = chart['meta']
    return dict(symbol=symbol, period=period,
                start_ts=int(mm['date_range']['start']),
                end_ts=int(mm['date_range']['end']),
                bar_count=int(mm['bar_count']), seed=mm['seed'])


def _team_members(match_id, notes, me_id):
    """组队房间成员进度：展示名/角色/邀请态/在线/是否已提交（不含他人指标）。"""
    import time
    from crypto.kline_training import store as db
    now = int(time.time())
    res = {r['user_id']: r for r in db.get_results(match_id)}
    out = []
    for pl in db.get_players(match_id):
        uid = pl['user_id']
        rr = res.get(uid)
        out.append({
            'user_id': uid,
            'display': _pk_display(notes, uid, pl.get('nickname'), pl.get('email')),
            'role': pl.get('role'), 'state': pl.get('state'),
            'online': (now - (pl.get('last_seen') or 0)) <= _ONLINE_WINDOW,
            'submitted': bool(rr and rr.get('return_pct') is not None),
            'is_me': (uid == me_id),
        })
    return out


def _pk_view(m, me_id):
    """构建对战视图。盲选且未结束时隐藏品种/日期（品类与 long_only 如实）。

    组队模式额外返回成员进度/同步时钟；进行中隐蓜他人具体指标（reveal 控制）。"""
    from crypto.kline_training import store as db
    from crypto.kline_training import registry
    from crypto.kline_training.providers import get_provider
    blind = bool(m.get('blind'))
    finished = m.get('status') == 'finished'
    hide = blind and not finished
    team = (m.get('pvp_mode') or 'code') == 'team'
    cat = m['category']
    provider = get_provider(cat)
    sym_name = None
    if not hide:
        try:
            sym_name = registry.resolve(cat, m['symbol']).get('name')
        except Exception:
            sym_name = m['symbol']
    notes = db.get_notes_map(me_id) if me_id else {}
    my = db.get_result(m['id'], me_id) if me_id else None
    participants, completed = db.match_completion(m['id'])
    my_player = db.get_player(m['id'], me_id) if me_id else None
    members = _team_members(m['id'], notes, me_id) if team else []
    if team:
        members_online = [x for x in members if x['state'] in ('accepted', 'ready')]
        participants = len(members_online)
        completed = sum(1 for x in members_online if x['submitted'])
    # 隐私：组队未结束不公开他人具体指标（board 置空，只给成员进度）
    reveal = (not team) or finished
    board = _pk_board(m['id'], notes) if reveal else []
    return {
        'id': m['id'], 'code': m['code'], 'status': m['status'],
        'blind': blind, 'category': cat,
        'category_label': provider.display_name,
        'long_only': bool(provider.long_only),
        'period': m['period'], 'bar_count': m.get('bar_count'),
        'symbol': (None if hide else m['symbol']),
        'symbol_name': (None if hide else sym_name),
        'start_ts': (None if hide else m.get('start_ts')),
        'end_ts': (None if hide else m.get('end_ts')),
        'engine_config': m.get('engine_config') or {},
        'host_id': m.get('host_id'), 'is_host': m.get('host_id') == me_id,
        'created_at': m.get('created_at'),
        'joined': bool(my),
        'pvp_mode': (m.get('pvp_mode') or 'code'), 'team': team,
        'auto_sync': bool(m.get('auto_sync')),
        'target_sec': m.get('target_sec'), 'speed_ms': m.get('speed_ms'),
        'start_at': m.get('start_at'), 'paused_at': m.get('paused_at'),
        'pause_ms': (m.get('pause_ms') or 0), 'end_at': m.get('end_at'),
        'server_now': _pk_now_ms(), 'reveal': reveal,
        'my_player_state': (my_player or {}).get('state'),
        'members': members,
        'my_result': (None if not my else {
            'rank': my.get('rank'), 'return_pct': my.get('return_pct'),
            'max_drawdown_pct': my.get('max_drawdown_pct'),
            'win_rate': my.get('win_rate'),
            'profit_loss_ratio': my.get('profit_loss_ratio'),
            'final_balance': my.get('final_balance'),
            'total_trades': my.get('total_trades'),
            'session_id': my.get('session_id'),
            'completed': my.get('return_pct') is not None,
        }),
        'participants': participants, 'completed': completed,
        'board': board,
    }


@kline_bp.route('/api/pk/create', methods=['POST'])
@login_required
def api_pk_create():
    """建房：房主选品类(+品种/周期/起点 明题 或 盲选)+ 参数，得到对战码。

    明题：需 category/symbol/period/start（固定区间，房主定 bar_count）。
    盲选：只需 category + bar_count，服务器在该品类内随机选题（品种/区间隐藏，
    品类与 long_only 如实）。房主自动占位加入。
    """
    try:
        from crypto.kline_training import store as db
        from crypto.kline_training import registry
        import pandas as pd
        from crypto.kline_training import training_service as ts
        from crypto.kline_training.providers import get_provider, normalize_category
        from crypto.kline_training.providers.base import parse_time, period_to_seconds

        p = request.get_json(silent=True) or {}
        me = current_user()['id']
        try:
            cat = normalize_category(p.get('category'))
        except Exception as e:
            return _err(str(e), 400)
        provider = get_provider(cat)
        blind = bool(p.get('blind'))
        try:
            bar_count = int(p.get('bar_count') or 120)
        except Exception:
            bar_count = 120
        bar_count = max(30, min(bar_count, 2000))
        ecfg = _sanitize_engine_config(p.get('engine_config'), provider.long_only)

        if blind:
            r = ts.start_blind(bar_count=bar_count, categories=[cat])
            ans = r['answer']
            symbol, period = ans['symbol'], ans['period']
            start_ts, end_ts, actual = ans['start_ts'], ans['end_ts'], ans['bar_count']
            seed = ans['seed']
        else:
            symbol = p.get('symbol')
            if not symbol:
                return _err('明题对战需指定品种 symbol', 400)
            try:
                rec = registry.resolve(cat, symbol)
            except Exception as e:
                return _err(str(e), 400)
            symbol = rec['code']
            period = p.get('period') or '1D'
            if not provider.supports_period(period):
                return _err('品类 %s 不支持周期 %s' % (cat, period), 400)
            st = parse_time(p.get('start'))
            if st is None:
                return _err('明题对战需指定起始日期 start', 400)
            psec = period_to_seconds(period) or 86400
            et = st + pd.Timedelta(seconds=int((bar_count + 5) * psec))
            chart = ts.get_training_chart_data(
                cat, symbol, period, start=int(st.timestamp()),
                end=int(et.timestamp()))
            candles = (chart or {}).get('candles') or []
            if len(candles) < max(10, bar_count // 3):
                return _err('该起点取到的K线不足，请更换起始日期或周期', 400)
            mm = chart['meta']
            start_ts = mm['date_range']['start']
            end_ts = mm['date_range']['end']
            actual = mm['bar_count']
            seed = mm['seed']

        match_id, code = db.create_match(
            category=cat, symbol=symbol, period=period, seed=seed, host_id=me,
            start_ts=int(start_ts), end_ts=int(end_ts), bar_count=int(actual),
            engine_config=ecfg, blind=1 if blind else 0)
        db.submit_result(match_id, me)          # 房主自动占位
        return _ok(_pk_view(db.get_match(match_id), me))
    except Exception as e:
        return _api_exception(e)


@kline_bp.route('/api/pk/join', methods=['POST'])
@login_required
def api_pk_join():
    """凭对战码加入（全员即好友）。加入即占位；满 2 人自动转 in-progress。"""
    try:
        from crypto.kline_training import store as db
        p = request.get_json(silent=True) or {}
        me = current_user()['id']
        m = _pk_find(p.get('code') or p.get('match_id'))
        if not m:
            return _err('对战不存在（请核对对战码）', 404)
        if m['status'] == 'finished':
            return _err('该对战已结束，无法加入', 400)
        if not db.get_result(m['id'], me):
            db.submit_result(m['id'], me)
        participants, _ = db.match_completion(m['id'])
        if m['status'] == 'open' and participants >= 2:
            db.update_match_status(m['id'], 'in-progress')
            m['status'] = 'in-progress'
        return _ok(_pk_view(m, me))
    except Exception as e:
        return _api_exception(e)


@kline_bp.route('/api/pk/mine', methods=['GET'])
@login_required
def api_pk_mine():
    """我参与/创建的战房列表（含我的名次与完成度）。"""
    try:
        from crypto.kline_training import store as db
        me = current_user()['id']
        out = []
        for m in db.list_matches_by_user(me, limit=50):
            hide = bool(m.get('blind')) and m.get('status') != 'finished'
            my = db.get_result(m['id'], me)
            participants, completed = db.match_completion(m['id'])
            out.append({
                'id': m['id'], 'code': m['code'], 'status': m['status'],
                'blind': bool(m.get('blind')), 'category': m['category'],
                'pvp_mode': (m.get('pvp_mode') or 'code'),
                'auto_sync': bool(m.get('auto_sync')),
                'period': m['period'], 'bar_count': m.get('bar_count'),
                'symbol': (None if hide else m['symbol']),
                'host_id': m.get('host_id'), 'is_host': m.get('host_id') == me,
                'participants': participants, 'completed': completed,
                'my_rank': (my or {}).get('rank'),
                'my_return': (my or {}).get('return_pct'),
                'created_at': m.get('created_at'),
            })
        return _ok(out)
    except Exception as e:
        return _api_exception(e)


@kline_bp.route('/api/pk/<ident>', methods=['GET'])
@login_required
def api_pk_get(ident):
    """对战详情 + 单场排行榜（盲选未结束隐藏题面）。"""
    try:
        m = _pk_find(ident)
        if not m:
            return _err('对战不存在', 404)
        return _ok(_pk_view(m, current_user()['id']))
    except Exception as e:
        return _api_exception(e)


@kline_bp.route('/api/pk/<ident>/chart', methods=['GET'])
@login_required
def api_pk_chart(ident):
    """同题取图：按对战锁定的 (品类,品种,周期,区间) 返回一致K线。盲选未结束则
    去标识化（仅遮品种与日期，保留品类与 long_only）。"""
    try:
        from crypto.kline_training import store as db
        from crypto.kline_training import training_service as ts
        m = _pk_find(ident)
        if not m:
            return _err('对战不存在', 404)
        me = current_user()['id']
        team = (m.get('pvp_mode') or 'code') == 'team'
        is_participant = bool(db.get_result(m['id'], me))
        spectate = team and not is_participant and m['status'] in ('playing', 'paused')
        if not is_participant and not spectate:
            return _err('请先加入对战', 403)
        data = ts.get_training_chart_data(
            m['category'], m['symbol'], m['period'],
            start=m.get('start_ts'), end=m.get('end_ts'))
        if not data or not data.get('candles'):
            return _err('取题失败，请重试', 502)
        if bool(m.get('blind')) and m['status'] != 'finished':
            meta = data.get('meta') or {}
            meta.update({'code': '??????', 'symbol': '??????', 'name': '神秘品种',
                         'display_name': '神秘品种', 'exchange': '',
                         'date_range': None})
            data['meta'] = meta
        data['pk'] = {
            'match_id': m['id'], 'code': m['code'], 'blind': bool(m.get('blind')),
            'status': m['status'], 'category': m['category'],
            'bar_count': m.get('bar_count'),
            'engine_config': m.get('engine_config') or {},
            'long_only': (data.get('meta') or {}).get('long_only'),
            'pvp_mode': (m.get('pvp_mode') or 'code'),
            'auto_sync': bool(m.get('auto_sync')),
            'speed_ms': m.get('speed_ms'), 'start_at': m.get('start_at'),
            'paused_at': m.get('paused_at'), 'pause_ms': (m.get('pause_ms') or 0),
            'server_now': _pk_now_ms(),
            'spectate': spectate,
        }
        return _ok(data)
    except Exception as e:
        return _api_exception(e)


@kline_bp.route('/api/pk/<ident>/submit', methods=['POST'])
@login_required
def api_pk_submit(ident):
    """提交成绩（跑完最后一根自动调用）：存 session(mode=pk)+写 result+重排名；
    全员完赛自动转 finished。"""
    try:
        from crypto.kline_training import store as db
        p = request.get_json(silent=True) or {}
        m = _pk_find(ident)
        if not m:
            return _err('对战不存在', 404)
        if m['status'] == 'finished':
            return _err('对战已结束，成绩已锁定', 400)
        me = current_user()['id']
        if not db.get_result(m['id'], me):
            return _err('请先加入对战', 403)
        team = (m.get('pvp_mode') or 'code') == 'team'
        if team and m['status'] not in ('playing', 'paused'):
            return _err('对战尚未开始或已结束，无法提交', 400)
        stats = p.get('stats') or {}
        sid = db.save_session(
            category=m['category'], symbol=m['symbol'], period=m['period'],
            mode='pk', user_id=me, seed=m['seed'], start_ts=m.get('start_ts'),
            bar_count=m.get('bar_count'), stats=stats,
            trades=p.get('trades') or [], equity=p.get('equity') or [])
        db.submit_result(
            m['id'], me, return_pct=stats.get('returnPct'),
            max_drawdown_pct=stats.get('maxDrawdownPct'),
            win_rate=stats.get('winRate'),
            profit_loss_ratio=stats.get('profitLossRatio'),
            final_balance=stats.get('finalBalance'),
            total_trades=stats.get('totalTrades'), session_id=sid)
        participants, completed = db.match_completion(m['id'])
        if m['status'] == 'open' and participants >= 2:
            db.update_match_status(m['id'], 'in-progress')
        if participants >= 2 and completed >= participants:
            db.update_match_status(m['id'], 'finished')
        return _ok(_pk_view(db.get_match(m['id']), me))
    except Exception as e:
        return _api_exception(e)


@kline_bp.route('/api/pk/<ident>/compare', methods=['GET'])
@login_required
def api_pk_compare(ident):
    """权益曲线对比：返回各已完赛者的 equity 曲线与关键指标（供多人叠加）。"""
    try:
        from crypto.kline_training import store as db
        m = _pk_find(ident)
        if not m:
            return _err('对战不存在', 404)
        me = current_user()['id']
        notes = db.get_notes_map(me)
        hide = bool(m.get('blind')) and m['status'] != 'finished'
        players = []
        for r in db.get_results(m['id']):
            if r.get('return_pct') is None:
                continue
            eq = []
            if r.get('session_id'):
                s = db.get_session(r['session_id'])
                if s:
                    eq = s.get('equity') or []
            players.append({
                'user_id': r['user_id'],
                'display': _pk_display(notes, r['user_id'], r.get('nickname'),
                                       r.get('email')),
                'rank': r.get('rank'), 'return_pct': r.get('return_pct'),
                'max_drawdown_pct': r.get('max_drawdown_pct'),
                'win_rate': r.get('win_rate'),
                'profit_loss_ratio': r.get('profit_loss_ratio'),
                'final_balance': r.get('final_balance'),
                'total_trades': r.get('total_trades'), 'equity': eq,
            })
        return _ok({
            'match': {'id': m['id'], 'code': m['code'], 'status': m['status'],
                      'blind': bool(m.get('blind')), 'category': m['category'],
                      'period': m['period'], 'bar_count': m.get('bar_count'),
                      'symbol': (None if hide else m['symbol'])},
            'players': players,
        })
    except Exception as e:
        return _api_exception(e)


@kline_bp.route('/api/leaderboard', methods=['GET'])
def api_leaderboard():
    """全局排行榜（按最佳收益率）：可按 category/period 过滤；全透明无需登录可看。"""
    try:
        from crypto.kline_training import store as db
        category = request.args.get('category') or None
        period = request.args.get('period') or None
        limit = max(1, min(request.args.get('limit', 50, type=int) or 50, 200))
        rows = db.leaderboard(category=category, period=period, limit=limit)
        cu = current_user()
        notes = db.get_notes_map(cu['id']) if cu else {}
        for r in rows:
            r['display'] = _pk_display(notes, r['user_id'], r.get('nickname'),
                                       r.get('email'))
        return _ok(rows)
    except Exception as e:
        return _api_exception(e)


# ====================================================================
#  API 路由 —— 自由组队（建房邀请 / 接受 / 开始 / 就绪 / 暂停 / 继续 / 结束）
#  房间状态机：lobby →(房主开始)→ ready →(全员就绪)→ playing ⇄ paused → finished
# ====================================================================

def _team_only(m):
    return (m.get('pvp_mode') or 'code') == 'team'


@kline_bp.route('/api/pk/team/create', methods=['POST'])
@login_required
def api_pk_team_create():
    """自由组队建房：房主设定题面/参数/推进方式，并多选邀请在线好友。

    建房后房主自动成成员(accepted)，被邀请者置 invited（需接受）。auto_sync
    时按目标总时长反推每根毫秒。"""
    try:
        from crypto.kline_training import store as db
        from crypto.kline_training.providers import get_provider, normalize_category
        p = request.get_json(silent=True) or {}
        me = current_user()['id']
        try:
            cat = normalize_category(p.get('category'))
        except Exception as e:
            return _err(str(e), 400)
        provider = get_provider(cat)
        try:
            bar_count = int(p.get('bar_count') or 120)
        except Exception:
            bar_count = 120
        bar_count = max(30, min(bar_count, 2000))
        ecfg = _sanitize_engine_config(p.get('engine_config'), provider.long_only)
        auto_sync = bool(p.get('auto_sync'))
        try:
            target_sec = int(p.get('target_sec')) if p.get('target_sec') else None
        except Exception:
            target_sec = None
        if auto_sync:
            target_sec = max(60, min(target_sec or 300, 900))
        try:
            topic = _pk_team_topic(p, cat, provider, bar_count)
        except ValueError as e:
            return _err(str(e), 400)
        speed_ms = _pk_speed_ms(topic['bar_count'], target_sec) if auto_sync else None
        match_id, code = db.create_match(
            category=cat, symbol=topic['symbol'], period=topic['period'],
            seed=topic['seed'], host_id=me, start_ts=topic['start_ts'],
            end_ts=topic['end_ts'], bar_count=topic['bar_count'],
            engine_config=ecfg, blind=1 if p.get('blind') else 0,
            pvp_mode='team', auto_sync=1 if auto_sync else 0,
            target_sec=target_sec, speed_ms=speed_ms, status='lobby')
        db.upsert_player(match_id, me, role='host', state='accepted')
        db.submit_result(match_id, me)             # 房主占位成绩
        ids = []
        for x in (p.get('invite_user_ids') or []):
            try:
                uid = int(x)
            except Exception:
                continue
            if uid != me and uid not in ids:
                ids.append(uid)
        invited = 0
        for uid in ids[:16]:
            if db.get_user_by_id(uid):
                db.upsert_player(match_id, uid, role='player', state='invited')
                invited += 1
        view = _pk_view(db.get_match(match_id), me)
        view['invited'] = invited
        return _ok(view)
    except Exception as e:
        return _api_exception(e)


@kline_bp.route('/api/pk/invites', methods=['GET'])
@login_required
def api_pk_invites():
    """我收到的待接受组队邀请。"""
    try:
        from crypto.kline_training import store as db
        return _ok(db.list_my_invites(current_user()['id']))
    except Exception as e:
        return _api_exception(e)


@kline_bp.route('/api/pk/<ident>/accept', methods=['POST'])
@login_required
def api_pk_accept(ident):
    """接受邀请→成为成员（占位成绩）。"""
    try:
        from crypto.kline_training import store as db
        m = _pk_find(ident)
        if not m:
            return _err('对战不存在', 404)
        if not _team_only(m):
            return _err('非组队对战', 400)
        if m['status'] in ('finished', 'canceled'):
            return _err('对战已结束', 400)
        me = current_user()['id']
        pl = db.get_player(m['id'], me)
        if not pl or pl['state'] != 'invited':
            return _err('你没有该对战的待接受邀请', 403)
        db.set_player_state(m['id'], me, 'accepted')
        if not db.get_result(m['id'], me):
            db.submit_result(m['id'], me)
        return _ok(_pk_view(db.get_match(m['id']), me))
    except Exception as e:
        return _api_exception(e)


@kline_bp.route('/api/pk/<ident>/decline', methods=['POST'])
@login_required
def api_pk_decline(ident):
    """拒绝邀请。"""
    try:
        from crypto.kline_training import store as db
        m = _pk_find(ident)
        if not m:
            return _err('对战不存在', 404)
        me = current_user()['id']
        pl = db.get_player(m['id'], me)
        if pl and pl['state'] == 'invited':
            db.set_player_state(m['id'], me, 'declined')
        return _ok({'ok': True})
    except Exception as e:
        return _api_exception(e)


@kline_bp.route('/api/pk/<ident>/start', methods=['POST'])
@login_required
def api_pk_start(ident):
    """房主开始比赛：需 ≥ 2 名已接受成员，lobby → ready（等待全员点确定）。"""
    try:
        from crypto.kline_training import store as db
        m = _pk_find(ident)
        if not m:
            return _err('对战不存在', 404)
        if not _team_only(m):
            return _err('非组队对战', 400)
        me = current_user()['id']
        if m['host_id'] != me:
            return _err('只有房主可以开始比赛', 403)
        if m['status'] != 'lobby':
            return _err('当前状态不可开始', 400)
        if db.count_team_members(m['id']) < _TEAM_MIN_PLAYERS:
            return _err('至少需要 %d 名成员接受邀请后才能开始' % _TEAM_MIN_PLAYERS, 400)
        db.update_match_status(m['id'], 'ready')
        return _ok(_pk_view(db.get_match(m['id']), me))
    except Exception as e:
        return _api_exception(e)


@kline_bp.route('/api/pk/<ident>/confirm', methods=['POST'])
@login_required
def api_pk_confirm(ident):
    """成员点「确定就绪」；全员就绪→ playing（自动同步记录全局 start_at）。"""
    try:
        from crypto.kline_training import store as db
        m = _pk_find(ident)
        if not m:
            return _err('对战不存在', 404)
        if not _team_only(m):
            return _err('非组队对战', 400)
        me = current_user()['id']
        if m['status'] != 'ready':
            return _err('房主尚未点击开始', 400)
        pl = db.get_player(m['id'], me)
        if not pl or pl['state'] not in ('accepted', 'ready'):
            return _err('你不是本房间成员', 403)
        if pl['state'] != 'ready':
            db.set_player_state(m['id'], me, 'ready')
        if db.all_members_ready(m['id']):
            db.update_match(m['id'], status='playing', start_at=_pk_now_ms())
        return _ok(_pk_view(db.get_match(m['id']), me))
    except Exception as e:
        return _api_exception(e)


@kline_bp.route('/api/pk/<ident>/pause', methods=['POST'])
@login_required
def api_pk_pause(ident):
    """房主暂停（仅自动同步且进行中）：冻结全局时钟。"""
    try:
        from crypto.kline_training import store as db
        m = _pk_find(ident)
        if not m:
            return _err('对战不存在', 404)
        me = current_user()['id']
        if m['host_id'] != me:
            return _err('只有房主可以暂停', 403)
        if not _team_only(m) or not m.get('auto_sync'):
            return _err('仅自动同步对战可暂停', 400)
        if m['status'] != 'playing':
            return _err('对战未在进行中', 400)
        db.update_match(m['id'], status='paused', paused_at=_pk_now_ms())
        return _ok(_pk_view(db.get_match(m['id']), me))
    except Exception as e:
        return _api_exception(e)


@kline_bp.route('/api/pk/<ident>/resume', methods=['POST'])
@login_required
def api_pk_resume(ident):
    """房主继续：累加暂停时长后恢复 playing。"""
    try:
        from crypto.kline_training import store as db
        m = _pk_find(ident)
        if not m:
            return _err('对战不存在', 404)
        me = current_user()['id']
        if m['host_id'] != me:
            return _err('只有房主可以继续', 403)
        if m['status'] != 'paused':
            return _err('对战未处于暂停', 400)
        now = _pk_now_ms()
        paused_at = m.get('paused_at') or now
        pause_ms = (m.get('pause_ms') or 0) + max(0, now - paused_at)
        db.update_match(m['id'], status='playing', paused_at=0, pause_ms=pause_ms)
        return _ok(_pk_view(db.get_match(m['id']), me))
    except Exception as e:
        return _api_exception(e)


@kline_bp.route('/api/pk/<ident>/end', methods=['POST'])
@login_required
def api_pk_end(ident):
    """房主提前结束：强制 finished；未提交者保留占位→榜单列为缺赛。"""
    try:
        from crypto.kline_training import store as db
        m = _pk_find(ident)
        if not m:
            return _err('对战不存在', 404)
        me = current_user()['id']
        if m['host_id'] != me:
            return _err('只有房主可以结束', 403)
        if m['status'] == 'finished':
            return _ok(_pk_view(m, me))
        db.update_match(m['id'], status='finished', end_at=_pk_now_ms())
        return _ok(_pk_view(db.get_match(m['id']), me))
    except Exception as e:
        return _api_exception(e)


# ====================================================================
#  API 路由 —— 向后兼容（仅 crypto，行为与旧版完全一致）
# ====================================================================

def _validate_crypto(symbol_raw, period):
    """[兼容] 旧的加密币校验（CRYPTO_SYMBOLS + VALID_PERIODS）"""
    from crypto.kline_training.kline_adapter import CRYPTO_SYMBOLS
    symbol = (symbol_raw or '').upper()
    if symbol not in CRYPTO_SYMBOLS:
        return _err(f"不支持的品种: {symbol_raw}，可选: {', '.join(CRYPTO_SYMBOLS)}",
                    400), None
    if period not in VALID_PERIODS:
        return _err(f"不支持的周期: {period}，可选: {', '.join(sorted(VALID_PERIODS))}",
                    400), None
    return None, symbol


@kline_bp.route('/api/kline-data/<symbol>', methods=['GET'])
def api_kline_data(symbol):
    """[兼容] 加密币K线数据（沿用 kline_adapter.get_kline_chart_data）"""
    period = request.args.get('period', '1H')
    error, symbol = _validate_crypto(symbol, period)
    if error:
        return error
    try:
        from crypto.kline_training.kline_adapter import get_kline_chart_data

        limit = request.args.get('limit', 500, type=int)
        full_data = request.args.get('full_data', 'false').lower() == 'true'
        target_bars = request.args.get('target_bars', 350, type=int)
        target_bars = max(100, min(target_bars, 2000))
        if not full_data:
            limit = max(50, min(limit, 2000))

        data = get_kline_chart_data(symbol=symbol, period=period, limit=limit,
                                    full_data=full_data, target_bars=target_bars)
        if not data or not data.get('candles'):
            return _err(data.get('error', '未获取到K线数据') if data
                        else '未获取到K线数据', 502)
        return _ok(data)
    except Exception as e:
        return _api_exception(e)


@kline_bp.route('/api/kline-more/<symbol>', methods=['GET'])
def api_kline_more(symbol):
    """[兼容] 加密币惰性加载（沿用 kline_adapter.load_more_kline）"""
    period = request.args.get('period', '1H')
    error, symbol = _validate_crypto(symbol, period)
    if error:
        return error
    try:
        from crypto.kline_training.kline_adapter import load_more_kline

        before_ts = request.args.get('before_ts', None, type=int)
        if not before_ts:
            return _err('缺少参数 before_ts', 400)
        target_bars = request.args.get('target_bars', 200, type=int)
        target_bars = max(50, min(target_bars, 1000))

        data = load_more_kline(symbol=symbol, period=period,
                               before_ts=before_ts, target_bars=target_bars)
        return _ok(data)
    except Exception as e:
        return _api_exception(e)
