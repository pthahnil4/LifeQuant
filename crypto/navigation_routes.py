#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
导航自定义管理 —— Flask 蓝图与接口编排
==========================================
职责：
- /navigation-settings        管理页（独立一级入口，可被导航自身管理）
- /api/navigation/*           配置读写、目录、移动、恢复、修复
- context processor           给 nav.html 注入内存默认配置（不碰数据库）

GET 绝不写库：配置读取用 navigation_repo.read_config（仅 SELECT）；context processor
只给内存默认值。写接口（PUT/POST）在 web_auth 闸门之上，再叠加同源 + 自定义头 + JSON 校验。

异常→HTTP 映射（设计文档第 6 章）：
  NavValidationError→400  RevisionConflict→409  StoredCorrupt→409(corrupt)
  DbUnavailable→503       过大→413
"""

import os
import logging

from flask import Blueprint, jsonify, render_template, request

from . import navigation_service as ns
from . import navigation_repo as repo
from .config_store_repo import CorruptConfigError

logger = logging.getLogger(__name__)

nav_bp = Blueprint('nav_bp', __name__)

NAV_REQUEST_HEADER = 'X-Nav-Request'
_PUBLIC_ORIGIN_ENV = 'CRYPTO_NAV_PUBLIC_ORIGIN'
_MAX_BODY_BYTES = 64 * 1024          # 写请求体上限 64 KiB


# =============================================================================
# 同源写保护
# =============================================================================
def _bearer_authorized() -> bool:
    """脚本用 Bearer 口令访问（无 Origin）时的显式放行判定；仍要过 JSON/版本/输入校验。"""
    try:
        from . import web_auth
    except Exception:
        return False
    header = str(request.headers.get('Authorization', '') or '').strip()
    if header[:7].lower() != 'bearer ':
        return False
    token = web_auth.configured_token()
    return bool(token) and header[7:].strip() == token


def _expected_origin() -> str:
    """同源比对的期望源：优先部署显式配置的公开源，否则用服务端请求来源。
    不盲信任客户端可伪造的转发头（未配 X-Forwarded 解析，也不为此全局放宽 ProxyFix）。"""
    cfg = str(os.environ.get(_PUBLIC_ORIGIN_ENV, '') or '').strip().rstrip('/')
    if cfg:
        return cfg
    return request.host_url.rstrip('/')


def _same_origin() -> bool:
    origin = (request.headers.get('Origin') or '').strip().rstrip('/')
    expected = _expected_origin()
    if origin:
        return origin == expected
    referer = (request.headers.get('Referer') or '').strip()
    if referer:
        # 用 scheme+host 前缀比对，避免整串 query 差异误判
        from urllib.parse import urlsplit
        sp = urlsplit(referer)
        ref_origin = f'{sp.scheme}://{sp.netloc}'.rstrip('/')
        return ref_origin == expected
    return False


def _write_guard():
    """写接口前置校验。返回 (ok, err_response)。

    浏览器 cookie 写：必须 JSON + X-Nav-Request + 同源；缺 Origin 用 Referer；都没有则拒。
    Bearer 脚本写：允许无 Origin，但仍要求 JSON + X-Nav-Request。
    """
    if request.content_type and 'application/json' not in request.content_type.lower():
        return False, (jsonify({'success': False, 'error': '请求体必须是 application/json'}), 400)
    if not request.headers.get(NAV_REQUEST_HEADER):
        return False, (jsonify({'success': False, 'error': '缺少导航写请求标识头'}), 403)
    if _bearer_authorized():
        return True, None
    if not _same_origin():
        return False, (jsonify({'success': False, 'error': '跨源写请求被拒绝'}), 403)
    return True, None


def _read_json_body(required=True):
    data = request.get_json(silent=True, force=False)
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ns.NavValidationError('请求体必须是 JSON 对象')
    return data


def _no_store(resp):
    resp.headers['Cache-Control'] = 'private, no-store'
    return resp


def _map_errors(fn):
    """统一把 repo/service 异常翻译成 JSON 响应。"""
    try:
        return fn()
    except ns.NavValidationError as e:
        return _no_store(jsonify({'success': False, 'error': str(e), 'code': 'invalid'})), 400
    except repo.RevisionConflict as e:
        return _no_store(jsonify({'success': False, 'error': '配置已被其他设备修改，请刷新后重试',
                                  'code': 'conflict', 'current_revision': e.current_revision})), 409
    except repo.StoredCorrupt as e:
        return _no_store(jsonify({'success': False, 'error': str(e), 'code': 'corrupt',
                                  'fingerprint': e.fingerprint})), 409
    except CorruptConfigError as e:
        return _no_store(jsonify({'success': False, 'error': '存量导航配置损坏', 'code': 'corrupt'})), 409
    except repo.DbUnavailable as e:
        return _no_store(jsonify({'success': False, 'error': str(e), 'code': 'db_unavailable'})), 503
    except Exception as e:                              # 兜底不泄漏堆栈给前端
        logger.exception('[Nav] 未预期异常')
        return _no_store(jsonify({'success': False, 'error': '服务器内部错误', 'code': 'internal'})), 500


# =============================================================================
# 页面路由
# =============================================================================
@nav_bp.route('/navigation-settings')
def navigation_settings_page():
    return render_template('navigation_settings.html', active_page='navigation-settings')


# =============================================================================
# 只读接口
# =============================================================================
@nav_bp.route('/api/navigation/config', methods=['GET'])
def api_get_config():
    def _do():
        state = repo.read_config()
        if state['status'] == 'db_error':
            return _no_store(jsonify({'success': False, 'error': '数据库暂不可用，导航为只读展示',
                                      'code': 'db_unavailable', 'editable': False})), 503
        if state['status'] == 'corrupt':
            return _no_store(jsonify({'success': False, 'code': 'corrupt', 'editable': False,
                                      'error': '导航配置已损坏，需在管理页确认后修复',
                                      'fingerprint': state['fingerprint']})), 200
        cfg = state['config']
        return _no_store(jsonify({'success': True, 'config': cfg, 'source': state['source'],
                                  'editable': True, 'warnings': []}))
    return _map_errors(_do)


@nav_bp.route('/api/navigation/catalog', methods=['GET'])
def api_get_catalog():
    # 纯静态目录：不查询交易接口、不探测外部 URL
    return _no_store(jsonify({'success': True, **ns.catalog_payload()}))


# =============================================================================
# 写接口
# =============================================================================
@nav_bp.route('/api/navigation/config', methods=['PUT'])
def api_save_config():
    ok, err = _write_guard()
    if not ok:
        return err
    if (request.content_length or 0) > _MAX_BODY_BYTES:
        return _no_store(jsonify({'success': False, 'error': '配置过大', 'code': 'too_large'})), 413

    def _do():
        body = _read_json_body()
        base_rev = body.get('base_revision')
        mutation_id = body.get('mutation_id')
        if base_rev is None:
            raise ns.NavValidationError('缺少 base_revision')
        payload = {'settings': body.get('settings'), 'items': body.get('items', [])}
        # 携带 expected_fingerprint → 走损坏修复分支
        if body.get('expected_fingerprint'):
            result = repo.repair_save(mutation_id, payload, body['expected_fingerprint'])
        else:
            result = repo.save_full(int(base_rev), mutation_id, payload)
        return _no_store(jsonify({'success': True, 'config': result['config'],
                                  'warnings': result['warnings']}))
    return _map_errors(_do)


@nav_bp.route('/api/navigation/move', methods=['POST'])
def api_move():
    ok, err = _write_guard()
    if not ok:
        return err

    def _do():
        body = _read_json_body()
        base_rev = body.get('base_revision')
        if base_rev is None:
            raise ns.NavValidationError('缺少 base_revision')
        item_id = body.get('item_id')
        if not isinstance(item_id, str) or not item_id.strip():
            raise ns.NavValidationError('缺少 item_id')
        result = repo.move_item(int(base_rev), body.get('mutation_id'), item_id.strip(),
                                body.get('parent_id'), body.get('before_id'))
        return _no_store(jsonify({'success': True, 'config': result['config']}))
    return _map_errors(_do)


@nav_bp.route('/api/navigation/reset', methods=['POST'])
def api_reset():
    ok, err = _write_guard()
    if not ok:
        return err

    def _do():
        body = _read_json_body()
        if not body.get('confirm'):
            raise ns.NavValidationError('恢复默认需要显式确认（confirm=true）')
        base_rev = body.get('base_revision')
        if base_rev is None:
            raise ns.NavValidationError('缺少 base_revision')
        result = repo.reset_default(int(base_rev), body.get('mutation_id'))
        return _no_store(jsonify({'success': True, 'config': result['config']}))
    return _map_errors(_do)


# =============================================================================
# 模板上下文：仅注入内存默认配置，不访问数据库（GET 渲染不写库）
# =============================================================================
def init_app(app):
    """注册蓝图并挂载 context processor（供全站 nav.html 使用）。"""
    app.register_blueprint(nav_bp)

    @app.context_processor
    def _nav_context():
        default = ns.build_default_config()
        # 无 JS 降级时用默认 items 计算一个高亮（best-effort，不读库）
        active_id = active_parent = None
        try:
            active_id, active_parent = ns.resolve_active(
                default['items'], request.path, request.args.get('_ap'))
        except Exception:
            pass
        return {
            'nav_default_config': default,
            'nav_active_id': active_id,
            'nav_active_parent_id': active_parent,
            'nav_request_path': request.path,
        }
