#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
日记本 - Flask 蓝图
===================
所有 /diary 和 /diary/api/* 路由。
包括：按天记录、公开/隐私双模式、密码保护（argon2/bcrypt 哈希）、
人生计数器、目标倒计时器、日历聚合、JSON 导出/导入。

【设计理念】
- 按天组织：一天可多条，也可一条长记；日历与分页流两种视图并存
- 隐私是服务端事实：未解锁时隐私条目正文根本不出接口，前端拿不到密文
- 解锁态用签名 cookie 承载（2 小时），改/删密码立即让所有旧 cookie 失效

【存储】MySQL：数据访问统一走 diary_repo，连接配置见 database.py。
响应契约与 journal 模块一致：{code, message, data}。
"""

import json
import logging
import time
import uuid
from datetime import datetime

from flask import Blueprint, jsonify, request, render_template, Response

from .database import session_scope
from . import diary_repo as repo
from .diary_repo import DiaryPasswordError

logger = logging.getLogger(__name__)

diary_bp = Blueprint('diary_bp', __name__)

# 解锁失败限流（进程内，单用户场景足够）：同一来源 10 分钟内最多 10 次尝试
_UNLOCK_WINDOW_SEC = 600
_UNLOCK_MAX_FAILS = 10
_unlock_fails = {}   # ip -> [(ts, ...)]


# =============================================================================
# 隐私状态判定（服务端唯一事实源）
# =============================================================================

def _settings_dict(session):
    row = repo.get_or_create_settings(session)
    return row, row.to_dict()


def _is_unlocked(row) -> bool:
    """解锁判定：未设密码视为解锁态；设了密码则校验签名 cookie。"""
    if not row.password_hash:
        return True
    return repo.unlock_cookie_ok(request.cookies.get(repo.UNLOCK_COOKIE, ''),
                                 row.password_hash, int(time.time()))


def _locked_mode(row) -> str:
    """整体锁定模式：private 全书锁 / public 只锁单条隐私 / unlocked 全放行。"""
    if not row.password_hash:
        return 'unlocked'
    return 'locked' if row.visibility == 'private' else 'partial'


def _mask_entry(e: dict, locked_mode: str, unlocked: bool) -> dict:
    """按锁定模式对条目做正文脱敏；隐私信息绝不因前端漏判而泄漏。"""
    if locked_mode == 'unlocked' or unlocked:
        return e
    if locked_mode == 'locked' or e.get('private'):
        m = dict(e)
        m['content'] = ''
        m['mood'] = ''
        m['created_at'] = ''
        m['updated_at'] = ''
        m['locked'] = True
        return m
    return e


def _prune_settings(settings: dict, unlocked: bool) -> dict:
    """未解锁时连目标/生日等个人化数据也不透出（避免锁后仍被围观）。"""
    if unlocked:
        return settings
    return {'visibility': settings['visibility'], 'has_password': settings['has_password'],
            'birth_date': '', 'locked': True}


# =============================================================================
# 页面与只读接口
# =============================================================================

@diary_bp.route('/diary', methods=['GET'])
def diary_page():
    """日记本页面（?date=YYYY-MM-DD 支持跨模块跳转到指定日期）"""
    return render_template('diary.html', active_page='diary')


@diary_bp.route('/diary/api/bootstrap', methods=['GET'])
def api_bootstrap():
    """首屏数据包：设置摘要 + 解锁态 + 最新一页条目 + 目标 + 某日详情 + 统计"""
    try:
        limit = _int_arg('limit', 30, 1, 100)
        anchor = (request.args.get('before') or '').strip()
        day = (request.args.get('date') or '').strip()
        with session_scope() as session:
            row, settings = _settings_dict(session)
            unlocked = _is_unlocked(row)
            locked_mode = _locked_mode(row)
            entries = [_mask_entry(e, locked_mode, unlocked)
                       for e in repo.list_entries(session, limit=limit, before_id=anchor or None)]
            day_entries = None
            if day and unlocked:
                try:
                    day_entries = repo.entries_for_date(session, day)
                except ValueError:
                    day_entries = None
            stats = {'total_entries': repo.total_entry_count(session)}
            return jsonify({'code': 200, 'message': 'success', 'data': {
                'settings': _prune_settings(settings, unlocked),
                'unlocked': unlocked,
                'locked_mode': locked_mode,
                'entries': entries,
                'has_more': len(entries) >= limit,
                'day': day if day_entries is not None else '',
                'day_entries': day_entries,
                'goals': repo.list_goals(session) if unlocked else [],
                'life_age': _life_age(row, unlocked),
                'stats': stats,
            }})
    except Exception as e:
        logger.error(f"[Diary] api_bootstrap 错误: {e}", exc_info=True)
        return jsonify({'code': 500, 'message': str(e), 'data': None})


def _life_age(row, unlocked: bool):
    if not unlocked or not row.birth_date:
        return None
    return repo.life_day_age(row.birth_date)


@diary_bp.route('/diary/api/day', methods=['GET'])
def api_day():
    """某天的全部日记（未解锁时隐私条目脱敏）"""
    try:
        day = (request.args.get('date') or '').strip()
        if not day:
            return jsonify({'code': 400, 'message': '缺少 date 参数', 'data': None})
        with session_scope() as session:
            row, _ = _settings_dict(session)
            unlocked = _is_unlocked(row)
            locked_mode = _locked_mode(row)
            try:
                entries = repo.entries_for_date(session, day)
            except ValueError:
                return jsonify({'code': 400, 'message': '日期格式应为 YYYY-MM-DD', 'data': None})
            return jsonify({'code': 200, 'message': 'success', 'data': {
                'date': day,
                'entries': [_mask_entry(e, locked_mode, unlocked) for e in entries],
            }})
    except Exception as e:
        logger.error(f"[Diary] api_day 错误: {e}", exc_info=True)
        return jsonify({'code': 500, 'message': str(e), 'data': None})


@diary_bp.route('/diary/api/calendar', methods=['GET'])
def api_calendar():
    """某月日历格：每天条目数（隐私数在未解锁时只给存在性计数）"""
    try:
        month = (request.args.get('month') or '').strip() or datetime.now().strftime('%Y-%m')
        with session_scope() as session:
            row, _ = _settings_dict(session)
            unlocked = _is_unlocked(row)
            try:
                days = repo.month_calendar(session, month)
            except ValueError:
                return jsonify({'code': 400, 'message': '月份格式应为 YYYY-MM', 'data': None})
            if not unlocked:
                locked_mode = _locked_mode(row)
                if locked_mode == 'locked':
                    days = [{'date': d['date'], 'total': d['total'] if d['total'] - d['private'] else 0,
                             'private': d['private'], 'locked': True} for d in days]
            return jsonify({'code': 200, 'message': 'success', 'data': {'month': month, 'days': days}})
    except Exception as e:
        logger.error(f"[Diary] api_calendar 错误: {e}", exc_info=True)
        return jsonify({'code': 500, 'message': str(e), 'data': None})


# =============================================================================
# 条目写接口
# =============================================================================

def _int_arg(name, default, lo, hi):
    try:
        v = int(request.args.get(name, default))
    except (TypeError, ValueError):
        v = default
    return max(lo, min(v, hi))


@diary_bp.route('/diary/api/add', methods=['POST'])
def api_add():
    """新增日记（一天可多条）

    参数：date(YYYY-MM-DD，缺省今天)、content(必填)、mood、private、linked_from
    """
    try:
        body = request.get_json(silent=True) or {}
        content = str(body.get('content') or '').strip()
        if not content:
            return jsonify({'code': 400, 'message': '日记内容不能为空', 'data': None})
        day = str(body.get('date') or '').strip() or datetime.now().strftime('%Y-%m-%d')
        try:
            day = repo.valid_date_str(day)
        except ValueError:
            return jsonify({'code': 400, 'message': '日期格式应为 YYYY-MM-DD', 'data': None})
        with session_scope() as session:
            row, _ = _settings_dict(session)
            entry = repo.add_entry(session, {
                'id': 'dentry_' + uuid.uuid4().hex[:8],
                'date': day,
                'content': content,
                'mood': body.get('mood'),
                'private': bool(body.get('private')),
                'linked_from': _safe_linked_from(body.get('linked_from')),
            })
            unlocked = _is_unlocked(row)
            return jsonify({'code': 200, 'message': '已写入日记 ✨',
                            'data': _mask_entry(entry, _locked_mode(row), unlocked)})
    except Exception as e:
        logger.error(f"[Diary] api_add 错误: {e}", exc_info=True)
        return jsonify({'code': 500, 'message': str(e), 'data': None})


def _safe_linked_from(raw):
    """跨模块关联来源白名单：只收 '模块:参数' 或裸条目 id，防脏数据。"""
    v = str(raw or '').strip()[:64]
    if not v:
        return ''
    if ':' in v:
        prefix = v.split(':', 1)[0]
        if prefix in ('calorie', 'journal', 'plan', 'expense'):
            return v
        return ''
    return v


@diary_bp.route('/diary/api/update', methods=['POST'])
def api_update():
    """编辑日记（正文/心情/隐私标记/归属日期）"""
    try:
        body = request.get_json(silent=True) or {}
        entry_id = str(body.get('id') or '').strip()
        fields = body.get('fields') or {}
        with session_scope() as session:
            row, _ = _settings_dict(session)
            if repo.get_entry(session, entry_id) is None:
                return jsonify({'code': 404, 'message': '条目不存在', 'data': None})
            try:
                entry = repo.update_entry(
                    session, entry_id,
                    content=fields.get('content') if 'content' in fields else None,
                    mood=fields.get('mood') if 'mood' in fields else None,
                    private=fields.get('private') if 'private' in fields else None,
                    date=fields.get('date') if 'date' in fields else None)
            except ValueError as ve:
                return jsonify({'code': 400, 'message': str(ve), 'data': None})
            return jsonify({'code': 200, 'message': '更新成功',
                            'data': _mask_entry(entry, _locked_mode(row), _is_unlocked(row))})
    except Exception as e:
        logger.error(f"[Diary] api_update 错误: {e}", exc_info=True)
        return jsonify({'code': 500, 'message': str(e), 'data': None})


@diary_bp.route('/diary/api/delete', methods=['POST'])
def api_delete():
    """删除日记条目"""
    try:
        body = request.get_json(silent=True) or {}
        entry_id = str(body.get('id') or '').strip()
        with session_scope() as session:
            if not repo.delete_entry(session, entry_id):
                return jsonify({'code': 404, 'message': '条目不存在', 'data': None})
        return jsonify({'code': 200, 'message': '删除成功', 'data': None})
    except Exception as e:
        logger.error(f"[Diary] api_delete 错误: {e}", exc_info=True)
        return jsonify({'code': 500, 'message': str(e), 'data': None})


# =============================================================================
# 设置与密码
# =============================================================================

@diary_bp.route('/diary/api/settings', methods=['POST'])
def api_settings():
    """更新非密码设置：visibility（public/private）、birth_date（YYYY-MM-DD 或空串）

    规则：未设置密码时不允许切到 private（有锁没钥匙比没锁更危险，直接拒绝）。
    """
    try:
        body = request.get_json(silent=True) or {}
        visibility = body.get('visibility')
        birth_date = body.get('birth_date')
        if visibility is not None and visibility not in ('public', 'private'):
            return jsonify({'code': 400, 'message': 'visibility 只支持 public / private', 'data': None})
        if birth_date is not None:
            birth_date = str(birth_date).strip()
            if birth_date:
                try:
                    birth_date = repo.valid_date_str(birth_date)
                except ValueError:
                    return jsonify({'code': 400, 'message': '出生日期格式应为 YYYY-MM-DD', 'data': None})
                if birth_date > datetime.now().strftime('%Y-%m-%d'):
                    return jsonify({'code': 400, 'message': '出生日期不能是未来日期', 'data': None})
        with session_scope() as session:
            row, settings = _settings_dict(session)
            if visibility == 'private' and not row.password_hash:
                return jsonify({'code': 400, 'message': '请先设置访问密码，再开启隐私模式', 'data': None})
            row = repo.update_settings_fields(session, birth_date=birth_date, visibility=visibility)
            return jsonify({'code': 200, 'message': '设置已保存', 'data': row.to_dict()})
    except Exception as e:
        logger.error(f"[Diary] api_settings 错误: {e}", exc_info=True)
        return jsonify({'code': 500, 'message': str(e), 'data': None})


@diary_bp.route('/diary/api/password-set', methods=['POST'])
def api_password_set():
    """设置/更换访问密码（已有密码时必须带原密码）"""
    try:
        body = request.get_json(silent=True) or {}
        password = str(body.get('password') or '')
        old_password = body.get('old_password')
        with session_scope() as session:
            try:
                row = repo.set_password(session, password,
                                        old_password=str(old_password) if old_password else None)
            except DiaryPasswordError as pe:
                return jsonify({'code': 400, 'message': str(pe), 'data': None})
        resp = jsonify({'code': 200, 'message': '访问密码已保存 🔐', 'data': row.to_dict()})
        _set_unlock_cookie(resp, row.password_hash)
        return resp
    except Exception as e:
        logger.error(f"[Diary] api_password_set 错误: {e}", exc_info=True)
        return jsonify({'code': 500, 'message': str(e), 'data': None})


@diary_bp.route('/diary/api/password-remove', methods=['POST'])
def api_password_remove():
    """校验密码后清除；可见性同步退回 public，解锁 cookie 立即作废"""
    try:
        body = request.get_json(silent=True) or {}
        password = str(body.get('password') or '')
        with session_scope() as session:
            row, _ = _settings_dict(session)
            if not row.password_hash:
                return jsonify({'code': 400, 'message': '尚未设置密码', 'data': None})
            if not repo.verify_password(password, row.password_hash):
                return jsonify({'code': 401, 'message': '密码不正确', 'data': None})
            repo.update_settings_fields(session, visibility='public')
            row = repo.clear_password(session, password)
        resp = jsonify({'code': 200, 'message': '密码已清除，日记本已转为公开', 'data': row.to_dict()})
        _clear_unlock_cookie(resp)
        return resp
    except Exception as e:
        logger.error(f"[Diary] api_password_remove 错误: {e}", exc_info=True)
        return jsonify({'code': 500, 'message': str(e), 'data': None})


def _set_unlock_cookie(resp, password_hash: str):
    resp.set_cookie(repo.UNLOCK_COOKIE,
                    repo.unlock_cookie_value(password_hash, repo.new_unlock_expiry(int(time.time()))),
                    max_age=repo.UNLOCK_TTL_SECONDS, httponly=True, samesite='Lax')


def _clear_unlock_cookie(resp):
    resp.delete_cookie(repo.UNLOCK_COOKIE)


def _unlock_throttled(ip: str) -> bool:
    now = time.time()
    fails = [t for t in _unlock_fails.get(ip, []) if now - t < _UNLOCK_WINDOW_SEC]
    _unlock_fails[ip] = fails
    return len(fails) >= _UNLOCK_MAX_FAILS


def _unlock_record_fail(ip: str):
    _unlock_fails.setdefault(ip, []).append(time.time())


@diary_bp.route('/diary/api/unlock', methods=['POST'])
def api_unlock():
    """输入访问密码解锁（签名 cookie 2 小时）；连续失败限流"""
    try:
        ip = request.remote_addr or 'unknown'
        if _unlock_throttled(ip):
            return jsonify({'code': 429, 'message': '尝试次数过多，请 10 分钟后再试', 'data': None}), 429
        body = request.get_json(silent=True) or {}
        password = str(body.get('password') or '')
        with session_scope() as session:
            row, settings = _settings_dict(session)
            if not row.password_hash:
                return jsonify({'code': 400, 'message': '未设置密码，无需解锁', 'data': None})
            if not repo.verify_password(password, row.password_hash):
                _unlock_record_fail(ip)
                return jsonify({'code': 401, 'message': '密码不正确', 'data': None})
        _unlock_fails.pop(ip, None)
        resp = jsonify({'code': 200, 'message': '已解锁 🔓', 'data': settings})
        _set_unlock_cookie(resp, row.password_hash)
        return resp
    except Exception as e:
        logger.error(f"[Diary] api_unlock 错误: {e}", exc_info=True)
        return jsonify({'code': 500, 'message': str(e), 'data': None})


@diary_bp.route('/diary/api/lock', methods=['POST'])
def api_lock():
    """主动上锁（清除解锁 cookie），公共设备上用完随手锁门"""
    resp = jsonify({'code': 200, 'message': '已上锁 🔒', 'data': None})
    _clear_unlock_cookie(resp)
    return resp


# =============================================================================
# 目标倒计时
# =============================================================================

@diary_bp.route('/diary/api/goal-add', methods=['POST'])
def api_goal_add():
    try:
        body = request.get_json(silent=True) or {}
        with session_scope() as session:
            row, _ = _settings_dict(session)
            if not _is_unlocked(row):
                return jsonify({'code': 401, 'message': '隐私模式下需先解锁', 'data': None})
            try:
                goal = repo.add_goal(session, body.get('title'), body.get('due_date'),
                                     body.get('note') or '')
            except ValueError as ve:
                return jsonify({'code': 400, 'message': str(ve), 'data': None})
        return jsonify({'code': 200, 'message': '目标已立下 🎯', 'data': goal})
    except Exception as e:
        logger.error(f"[Diary] api_goal_add 错误: {e}", exc_info=True)
        return jsonify({'code': 500, 'message': str(e), 'data': None})


@diary_bp.route('/diary/api/goal-update', methods=['POST'])
def api_goal_update():
    try:
        body = request.get_json(silent=True) or {}
        goal_id = str(body.get('id') or '').strip()
        fields = body.get('fields') or {}
        with session_scope() as session:
            row, _ = _settings_dict(session)
            if not _is_unlocked(row):
                return jsonify({'code': 401, 'message': '隐私模式下需先解锁', 'data': None})
            try:
                goal = repo.update_goal(
                    session, goal_id,
                    title=fields.get('title') if 'title' in fields else None,
                    due_date=fields.get('due_date') if 'due_date' in fields else None,
                    note=fields.get('note') if 'note' in fields else None,
                    done=fields.get('done') if 'done' in fields else None)
            except ValueError as ve:
                return jsonify({'code': 400, 'message': str(ve), 'data': None})
            if goal is None:
                return jsonify({'code': 404, 'message': '目标不存在', 'data': None})
        return jsonify({'code': 200, 'message': '目标已更新', 'data': goal})
    except Exception as e:
        logger.error(f"[Diary] api_goal_update 错误: {e}", exc_info=True)
        return jsonify({'code': 500, 'message': str(e), 'data': None})


@diary_bp.route('/diary/api/goal-delete', methods=['POST'])
def api_goal_delete():
    try:
        body = request.get_json(silent=True) or {}
        goal_id = str(body.get('id') or '').strip()
        with session_scope() as session:
            row, _ = _settings_dict(session)
            if not _is_unlocked(row):
                return jsonify({'code': 401, 'message': '隐私模式下需先解锁', 'data': None})
            if not repo.delete_goal(session, goal_id):
                return jsonify({'code': 404, 'message': '目标不存在', 'data': None})
        return jsonify({'code': 200, 'message': '目标已删除', 'data': None})
    except Exception as e:
        logger.error(f"[Diary] api_goal_delete 错误: {e}", exc_info=True)
        return jsonify({'code': 500, 'message': str(e), 'data': None})


# =============================================================================
# 导出 / 导入（JSON）
# =============================================================================

@diary_bp.route('/diary/api/export', methods=['GET'])
def api_export():
    """导出整本日记为 JSON 文件流。

    未解锁时：隐私条目正文以占位符导出（不泄漏内容），并带 masked 标记；
    密码哈希永不进导出文件（换机导入后需重新设置密码）。
    """
    try:
        with session_scope() as session:
            row, settings = _settings_dict(session)
            unlocked = _is_unlocked(row)
            locked_mode = _locked_mode(row)
            payload = {
                'app': 'cryptoTrade-diary',
                'schema_version': 1,
                'export_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'masked': not unlocked and locked_mode != 'unlocked',
                'settings': {
                    'visibility': settings['visibility'],
                    'birth_date': settings['birth_date'] if unlocked else '',
                },
                'goals': repo.list_goals(session) if unlocked else [],
                'entries': [_mask_entry(e, locked_mode, unlocked) for e in repo.all_entries(session)],
            }
        content = json.dumps(payload, ensure_ascii=False, indent=2)
        filename = f'diary_export_{datetime.now().strftime("%Y-%m-%d")}.json'
        return Response(content, mimetype='application/json; charset=utf-8',
                        headers={'Content-Disposition': f'attachment; filename={filename}'})
    except Exception as e:
        logger.error(f"[Diary] api_export 错误: {e}", exc_info=True)
        return jsonify({'code': 500, 'message': str(e), 'data': None})


@diary_bp.route('/diary/api/import', methods=['POST'])
def api_import():
    """导入 JSON（merge 语义：同 id 跳过；正文为空或日期非法的条目跳过）。

    参数：payload（必填，导出文件解析后的对象）。返回导入统计。
    安全：隐私模式下未解锁禁止导入（防把空正文的脱敏数据盖坏本地状态判断）；
    密码与可见性不从文件导入，只导条目与目标。
    """
    try:
        body = request.get_json(silent=True) or {}
        payload = body.get('payload')
        if not isinstance(payload, dict) or payload.get('app') != 'cryptoTrade-diary':
            return jsonify({'code': 400, 'message': '文件格式不对：请使用本模块导出的 JSON', 'data': None})
        entries = payload.get('entries')
        if not isinstance(entries, list):
            return jsonify({'code': 400, 'message': '缺少 entries 数组', 'data': None})
        goals = payload.get('goals') if isinstance(payload.get('goals'), list) else []
        with session_scope() as session:
            row, _ = _settings_dict(session)
            if not _is_unlocked(row):
                return jsonify({'code': 401, 'message': '隐私模式下需先解锁再导入', 'data': None})
            stats = {'entries_added': 0, 'entries_skipped': 0,
                     'goals_added': 0, 'goals_skipped': 0}
            for raw in entries:
                if not isinstance(raw, dict) or raw.get('locked'):
                    stats['entries_skipped'] += 1
                    continue
                try:
                    day = repo.valid_date_str(raw.get('date'))
                except ValueError:
                    stats['entries_skipped'] += 1
                    continue
                content = str(raw.get('content') or '').strip()
                eid = str(raw.get('id') or '').strip()[:32]
                if not content:
                    stats['entries_skipped'] += 1
                    continue
                if eid and repo.get_entry(session, eid) is not None:
                    stats['entries_skipped'] += 1  # 同 id 视为已导入过，幂等跳过
                    continue
                repo.add_entry(session, {
                    'id': eid or None, 'date': day, 'content': content,
                    'mood': raw.get('mood'), 'private': bool(raw.get('private')),
                    'linked_from': _safe_linked_from(raw.get('linked_from')),
                    'created_at': raw.get('created_at'), 'updated_at': raw.get('updated_at'),
                })
                stats['entries_added'] += 1
            for raw in goals:
                if not isinstance(raw, dict):
                    stats['goals_skipped'] += 1
                    continue
                try:
                    gid = str(raw.get('id') or '').strip()[:32]
                    from .models import DiaryGoal
                    if gid and session.get(DiaryGoal, gid) is not None:
                        stats['goals_skipped'] += 1
                        continue
                    goal = repo.add_goal(session, raw.get('title'), raw.get('due_date'),
                                         raw.get('note') or '')
                    if gid:
                        # 保留原 id 便于跨文件引用稳定（add_goal 自带新 id，这里改写主键仅限新行）
                        row_obj = session.get(DiaryGoal, goal['id'])
                        row_obj.id = gid
                        session.flush()
                    stats['goals_added'] += 1
                except ValueError:
                    stats['goals_skipped'] += 1
                    continue
            birth = str((payload.get('settings') or {}).get('birth_date') or '').strip()
            if birth:
                try:
                    repo.update_settings_fields(session, birth_date=repo.valid_date_str(birth))
                except ValueError:
                    pass
        return jsonify({'code': 200, 'message':
                        f"导入完成：日记 {stats['entries_added']} 条 / 目标 {stats['goals_added']} 个",
                        'data': stats})
    except Exception as e:
        logger.error(f"[Diary] api_import 错误: {e}", exc_info=True)
        return jsonify({'code': 500, 'message': str(e), 'data': None})
