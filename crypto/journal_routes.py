#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
随笔记录与复盘系统 - Flask 蓝图
===============================
所有 /journal 和 /journal/api/* 路由
包括：随笔/复盘条目管理、标签管理、筛选搜索、置顶、经验提炼、统计看板、导出

【设计理念】
- 记录零摩擦：一条正文即可保存，不打断思考心流
- 复盘有结构：对象/决策/结果/教训四格引导，全部可选
- 数据诚实、情绪友好：复盘只沉淀教训，不指责

【存储】MySQL（迁移批次1）：数据访问统一走 journal_repo，
连接配置见 database.py（环境变量 CRYPTO_DB_URL 或外置目录 db_url.txt）。
对外 API 契约与 JSON 文件版完全一致。
"""

import json
import re
import datetime
import uuid
import logging
from flask import Blueprint, jsonify, request, render_template, Response

from .database import session_scope
from . import journal_repo as repo

logger = logging.getLogger(__name__)

journal_bp = Blueprint('journal_bp', __name__)

# =============================================================================
# 数据库初始化已收敛到 database.init_db() 进程内单例，由应用启动时后台
# 预热（warmup_async）完成，蓝图导入期不再同步建表，避免阻塞启动。
# =============================================================================

# =============================================================================
# 数据操作层
# =============================================================================

def _load_data():
    """从 MySQL 组装与 JSON 版一致的数据结构 {initialized, tags, notes}
    （供统计引擎 / 筛选 / 导出等纯函数复用）"""
    with session_scope() as session:
        return repo.load_journal_data(session)


def _new_id(prefix='note'):
    """生成唯一 ID"""
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


# 跨模块关联引用格式：'{module}:{ref}'（区别于条目间关联的裸 note_id）
# 目前支持 calorie:YYYY-MM-DD / diary:YYYY-MM-DD —— 分别回指热量记录与日记某日，
# 支持双向跳转；后续接入交易复盘等模块时在此扩展前缀即可。
_LINK_REF_RE = re.compile(r'^(?:calorie|diary):\d{4}-\d{2}-\d{2}$')


def _normalize_linked_from(raw):
    """校验并规范化关联来源：

    - 跨模块引用（如 calorie:2026-08-30）：格式合法即保留；
    - 条目间关联（裸 note_id）：由调用方另行校验条目存在性；
    - 其余不可识别格式一律置空，避免脏数据。
    """
    v = (raw or '').strip()
    if not v:
        return '', False
    if ':' in v:
        return (v, True) if _LINK_REF_RE.match(v) else ('', False)
    return v, False


def _now_str():
    """当前时间字符串 YYYY-MM-DD HH:MM:SS"""
    return datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def _today_str():
    """今天日期字符串 YYYY-MM-DD"""
    return datetime.datetime.now().strftime('%Y-%m-%d')


def _tag_counts(notes):
    """统计每个标签的条目数"""
    counts = {}
    for n in notes:
        for t in n.get('tags', []):
            counts[t] = counts.get(t, 0) + 1
    return counts


def _filter_notes(notes, tag=None, note_type=None, keyword=None, days=None):
    """服务端筛选（与前端筛选同规则，作兜底与导出复用）

    tag: 单个标签名（条目含该标签即命中）
    note_type: note / review
    keyword: 全文匹配 content + review 四格
    days: 近 N 天（0/None 表示不限）
    """
    result = notes
    if tag:
        result = [n for n in result if tag in n.get('tags', [])]
    if note_type in ('note', 'review'):
        result = [n for n in result if n.get('type') == note_type]
    if keyword:
        kw = keyword.strip().lower()
        if kw:
            def _hit(n):
                if kw in (n.get('content') or '').lower():
                    return True
                rv = n.get('review') or {}
                return any(kw in str(rv.get(k, '') or '').lower()
                           for k in ('subject', 'decision', 'outcome', 'lesson'))
            result = [n for n in result if _hit(n)]
    if days:
        try:
            days = int(days)
        except (TypeError, ValueError):
            days = 0
        if days > 0:
            cutoff = (datetime.datetime.now() - datetime.timedelta(days=days)).strftime('%Y-%m-%d %H:%M:%S')
            result = [n for n in result if (n.get('created_at') or '') >= cutoff]
    return result


# =============================================================================
# 统计引擎
# =============================================================================

def _calc_streak_days(notes):
    """连续记录天数：从今天（或昨天）往前连续有记录的日期数"""
    days = {(n.get('created_at') or '')[:10] for n in notes}
    days.discard('')
    if not days:
        return 0
    today = datetime.date.today()
    cursor = today if today.isoformat() in days else today - datetime.timedelta(days=1)
    streak = 0
    while cursor.isoformat() in days:
        streak += 1
        cursor -= datetime.timedelta(days=1)
    return streak


def _calc_best_streak_days(notes):
    """历史最佳连续记录天数"""
    days = sorted({(n.get('created_at') or '')[:10] for n in notes} - {''})
    if not days:
        return 0
    best = cur = 1
    prev = None
    for ds in days:
        d = datetime.date.fromisoformat(ds)
        if prev is not None and (d - prev).days == 1:
            cur += 1
            best = max(best, cur)
        else:
            cur = 1
        prev = d
    return best


_CALENDAR_WEEKS = 15  # 记录日历显示近 15 周（GitHub 贡献图风格）


def _build_calendar(notes, weeks=_CALENDAR_WEEKS):
    """构建近 15 周记录日历：每天返回 {date, count, level, future}"""
    count_by_day = {}
    for n in notes:
        key = (n.get('created_at') or '')[:10]
        if key:
            count_by_day[key] = count_by_day.get(key, 0) + 1
    today = datetime.date.today()
    monday = today - datetime.timedelta(days=today.weekday())
    start = monday - datetime.timedelta(weeks=weeks - 1)
    days = []
    for i in range(weeks * 7):
        d = start + datetime.timedelta(days=i)
        ds = d.isoformat()
        count = count_by_day.get(ds, 0)
        days.append({
            'date': ds,
            'count': count,
            'level': min(count, 4),  # 分级 0~4，一天 4 条及以上封顶
            'future': d > today
        })
    return {'start_date': start.isoformat(), 'weeks': weeks, 'days': days}


def _build_stats(data):
    """构建统计看板数据"""
    notes = data.get('notes', [])
    tags = data.get('tags', [])
    now = datetime.datetime.now()
    month_prefix = now.strftime('%Y-%m')

    counts = _tag_counts(notes)
    tag_distribution = [
        {'name': t['name'], 'color': t.get('color', '#999'), 'count': counts.get(t['name'], 0)}
        for t in tags
    ]

    return {
        'total': len(notes),
        'review_count': sum(1 for n in notes if n.get('type') == 'review'),
        'note_count': sum(1 for n in notes if n.get('type') != 'review'),
        'distilled_count': sum(1 for n in notes if n.get('distilled')),
        'month_count': sum(1 for n in notes if (n.get('created_at') or '').startswith(month_prefix)),
        'streak_days': _calc_streak_days(notes),
        'best_streak_days': _calc_best_streak_days(notes),
        'tag_distribution': tag_distribution,
        'calendar': _build_calendar(notes)
    }


# =============================================================================
# 导出引擎
# =============================================================================

def _build_export_markdown(notes, tags):
    """生成 Markdown 导出内容（按日期分组，含复盘四格）"""
    lines = ['# 随笔与复盘导出', '']
    lines.append(f'> 导出时间：{_now_str()} | 共 {len(notes)} 条记录')
    lines.append('')

    grouped = {}
    for n in notes:
        key = (n.get('created_at') or '')[:10] or '未知日期'
        grouped.setdefault(key, []).append(n)

    for date_str in sorted(grouped.keys(), reverse=True):
        lines.append(f'## {date_str}')
        lines.append('')
        items = sorted(grouped[date_str], key=lambda x: x.get('created_at', ''), reverse=True)
        for n in items:
            time_part = (n.get('created_at') or '')[11:16]
            type_label = '复盘' if n.get('type') == 'review' else '随笔'
            pinned_mark = ' 📌' if n.get('pinned') else ''
            tag_str = ''.join(f' `#{t}`' for t in n.get('tags', []))
            lines.append(f'### {time_part} 【{type_label}】{pinned_mark}{tag_str}')
            lines.append('')
            lines.append((n.get('content') or '').strip())
            rv = n.get('review') or {}
            fields = [('复盘对象', 'subject'), ('当时决策', 'decision'), ('实际结果', 'outcome'), ('经验教训', 'lesson')]
            rv_lines = [f'- **{label}**：{rv[key]}' for label, key in fields if rv.get(key)]
            if rv_lines:
                lines.append('')
                lines.extend(rv_lines)
            if n.get('distilled'):
                lines.append('')
                lines.append('> 💡 已提炼为经验')
            lines.append('')
            lines.append('---')
            lines.append('')
    return '\n'.join(lines)


def _export_filename(ext):
    """导出文件名：journal_export_YYYY-MM-DD.ext"""
    return f'journal_export_{_today_str()}.{ext}'


# =============================================================================
# API 路由
# =============================================================================

@journal_bp.route('/journal', methods=['GET'])
def journal_page():
    """随笔与复盘页面"""
    return render_template('journal.html', active_page='journal')


@journal_bp.route('/journal/api/list', methods=['GET'])
def api_list():
    """条目列表 + 标签字典 + 统计摘要（服务端筛选兜底）"""
    try:
        data = _load_data()
        tag = request.args.get('tag', '').strip()
        note_type = request.args.get('type', '').strip()
        keyword = request.args.get('keyword', '').strip()
        days = request.args.get('days', '0')

        notes = _filter_notes(data['notes'], tag=tag or None, note_type=note_type or None,
                              keyword=keyword or None, days=days)
        # 按创建时间倒序
        notes = sorted(notes, key=lambda x: x.get('created_at', ''), reverse=True)

        counts = _tag_counts(data['notes'])
        tags = [
            {'name': t['name'], 'color': t.get('color', '#999'), 'count': counts.get(t['name'], 0)}
            for t in data.get('tags', [])
        ]

        return jsonify({
            "code": 200, "message": "success",
            "data": {
                "notes": notes,
                "tags": tags,
                "stats": _build_stats(data)
            }
        })
    except Exception as e:
        logger.error(f"[Journal] api_list 错误: {e}", exc_info=True)
        return jsonify({"code": 500, "message": str(e), "data": None})


@journal_bp.route('/journal/api/add', methods=['POST'])
def api_add():
    """新增条目（随笔或复盘）

    参数：content(必填)、type、tags[]、review{}、linked_from
    未注册的新标签名自动创建（保持记录零摩擦）
    """
    try:
        body = request.get_json() or {}
        content = (body.get('content') or '').strip()
        if not content:
            return jsonify({"code": 400, "message": "内容不能为空", "data": None})

        note_type = body.get('type', 'note')
        if note_type not in ('note', 'review'):
            note_type = 'note'

        # 标签：过滤空值去重（注册动作在事务内完成）
        raw_tags = body.get('tags') or []
        tags = []
        for t in raw_tags:
            t = str(t).strip()
            if t and t not in tags:
                tags.append(t)

        # 复盘四格：只保留允许字段
        review = None
        if note_type == 'review':
            rv = body.get('review') or {}
            review = {
                'subject': str(rv.get('subject') or '').strip(),
                'decision': str(rv.get('decision') or '').strip(),
                'outcome': str(rv.get('outcome') or '').strip(),
                'lesson': str(rv.get('lesson') or '').strip()
            }

        note = {
            "id": _new_id('note'),
            "type": note_type,
            "content": content,
            "tags": tags,
            "review": review,
            "pinned": False,
            "distilled": False,
            "linked_from": '',
            "created_at": _now_str(),
            "updated_at": _now_str()
        }
        # 关联来源：支持条目间关联（裸 note_id）与跨模块关联（如 calorie:日期）
        linked_from, is_cross_ref = _normalize_linked_from(body.get('linked_from'))
        with session_scope() as session:
            # 未注册的新标签自动创建（保持记录零摩擦）
            for t in tags:
                repo.ensure_tag(session, t)
            # 条目间关联校验：来源条目不存在则清空（与 JSON 版行为一致）；
            # 跨模块引用不走条目存在性检查，由格式校验兜底
            if linked_from and not is_cross_ref \
                    and repo.get_note(session, linked_from) is None:
                linked_from = ''
            note['linked_from'] = linked_from
            repo.add_note(session, note)
        return jsonify({"code": 200, "message": "已记录 ✨", "data": note})
    except Exception as e:
        logger.error(f"[Journal] api_add 错误: {e}", exc_info=True)
        return jsonify({"code": 500, "message": str(e), "data": None})


@journal_bp.route('/journal/api/update', methods=['POST'])
def api_update():
    """编辑条目（正文/标签/复盘四格）"""
    try:
        body = request.get_json() or {}
        note_id = body.get('id', '')
        fields = body.get('fields', {})

        content = None
        if 'content' in fields:
            content = str(fields['content'] or '').strip()
            if not content:
                return jsonify({"code": 400, "message": "内容不能为空", "data": None})

        tags = None
        if 'tags' in fields:
            tags = []
            for t in (fields['tags'] or []):
                t = str(t).strip()
                if t and t not in tags:
                    tags.append(t)

        review = None
        if 'review' in fields:
            rv = fields['review'] or {}
            review = {
                'subject': str(rv.get('subject') or '').strip(),
                'decision': str(rv.get('decision') or '').strip(),
                'outcome': str(rv.get('outcome') or '').strip(),
                'lesson': str(rv.get('lesson') or '').strip()
            }

        with session_scope() as session:
            if repo.get_note(session, note_id) is None:
                return jsonify({"code": 404, "message": "条目不存在", "data": None})
            if tags is not None:
                for t in tags:  # 新标签自动注册（保持记录零摩擦）
                    repo.ensure_tag(session, t)
            note = repo.update_note(session, note_id, content=content, tags=tags, review=review)
        return jsonify({"code": 200, "message": "更新成功", "data": note})
    except Exception as e:
        logger.error(f"[Journal] api_update 错误: {e}", exc_info=True)
        return jsonify({"code": 500, "message": str(e), "data": None})


@journal_bp.route('/journal/api/delete', methods=['POST'])
def api_delete():
    """删除条目（同时清理其他条目对它的关联引用）"""
    try:
        body = request.get_json() or {}
        note_id = body.get('id', '')

        with session_scope() as session:
            # 删除条目 + 清理指向它的关联引用（repo 内一并处理）
            if not repo.delete_note(session, note_id):
                return jsonify({"code": 404, "message": "条目不存在", "data": None})

        return jsonify({"code": 200, "message": "删除成功", "data": None})
    except Exception as e:
        logger.error(f"[Journal] api_delete 错误: {e}", exc_info=True)
        return jsonify({"code": 500, "message": str(e), "data": None})


@journal_bp.route('/journal/api/pin', methods=['POST'])
def api_pin():
    """置顶切换"""
    try:
        body = request.get_json() or {}
        note_id = body.get('id', '')

        with session_scope() as session:
            note = repo.toggle_pin(session, note_id)
            if note is None:
                return jsonify({"code": 404, "message": "条目不存在", "data": None})

        msg = '已置顶 📌' if note['pinned'] else '已取消置顶'
        return jsonify({"code": 200, "message": msg, "data": note})
    except Exception as e:
        logger.error(f"[Journal] api_pin 错误: {e}", exc_info=True)
        return jsonify({"code": 500, "message": str(e), "data": None})


@journal_bp.route('/journal/api/distill', methods=['POST'])
def api_distill():
    """提炼经验切换（复盘条目专属）"""
    try:
        body = request.get_json() or {}
        note_id = body.get('id', '')

        with session_scope() as session:
            note, err = repo.toggle_distill(session, note_id)
            if note is None:
                code = 404 if err == '条目不存在' else 400
                return jsonify({"code": code, "message": err, "data": None})

        msg = '已收入经验库 💡' if note['distilled'] else '已从经验库移除'
        return jsonify({"code": 200, "message": msg, "data": note})
    except Exception as e:
        logger.error(f"[Journal] api_distill 错误: {e}", exc_info=True)
        return jsonify({"code": 500, "message": str(e), "data": None})


@journal_bp.route('/journal/api/tags', methods=['GET'])
def api_tags():
    """标签列表（含每个标签条目数）"""
    try:
        data = _load_data()
        counts = _tag_counts(data['notes'])
        tags = [
            {'name': t['name'], 'color': t.get('color', '#999'), 'count': counts.get(t['name'], 0)}
            for t in data.get('tags', [])
        ]
        return jsonify({"code": 200, "message": "success", "data": tags})
    except Exception as e:
        logger.error(f"[Journal] api_tags 错误: {e}", exc_info=True)
        return jsonify({"code": 500, "message": str(e), "data": None})


@journal_bp.route('/journal/api/tag-save', methods=['POST'])
def api_tag_save():
    """新增标签 / 修改标签颜色（同名标签视为改色）"""
    try:
        body = request.get_json() or {}
        name = (body.get('name') or '').strip()
        color = (body.get('color') or '').strip()

        if not name:
            return jsonify({"code": 400, "message": "标签名不能为空", "data": None})
        if len(name) > 8:
            return jsonify({"code": 400, "message": "标签名不能超过 8 个字", "data": None})

        with session_scope() as session:
            tag, created = repo.save_tag(session, name, color or None)
        msg = '标签已创建' if created else '标签颜色已更新'

        return jsonify({"code": 200, "message": msg, "data": tag})
    except Exception as e:
        logger.error(f"[Journal] api_tag_save 错误: {e}", exc_info=True)
        return jsonify({"code": 500, "message": str(e), "data": None})


@journal_bp.route('/journal/api/tag-delete', methods=['POST'])
def api_tag_delete():
    """删除标签（同步从所有条目中移除引用，不删条目）"""
    try:
        body = request.get_json() or {}
        name = (body.get('name') or '').strip()

        with session_scope() as session:
            # 删除标签；条目引用由外键级联摘除，不删条目
            if not repo.delete_tag(session, name):
                return jsonify({"code": 404, "message": "标签不存在", "data": None})

        return jsonify({"code": 200, "message": "标签已删除", "data": None})
    except Exception as e:
        logger.error(f"[Journal] api_tag_delete 错误: {e}", exc_info=True)
        return jsonify({"code": 500, "message": str(e), "data": None})


@journal_bp.route('/journal/api/stats', methods=['GET'])
def api_stats():
    """统计看板数据"""
    try:
        data = _load_data()
        return jsonify({"code": 200, "message": "success", "data": _build_stats(data)})
    except Exception as e:
        logger.error(f"[Journal] api_stats 错误: {e}", exc_info=True)
        return jsonify({"code": 500, "message": str(e), "data": None})


@journal_bp.route('/journal/api/export', methods=['GET'])
def api_export():
    """导出条目（文件流下载），支持当前筛选条件

    参数：format（md/json）、tag、type、keyword、days
    """
    try:
        data = _load_data()
        fmt = request.args.get('format', 'md').strip().lower()
        if fmt not in ('md', 'json'):
            return jsonify({"code": 400, "message": "format 只支持 md 或 json", "data": None})

        notes = _filter_notes(
            data['notes'],
            tag=request.args.get('tag', '').strip() or None,
            note_type=request.args.get('type', '').strip() or None,
            keyword=request.args.get('keyword', '').strip() or None,
            days=request.args.get('days', '0')
        )
        notes = sorted(notes, key=lambda x: x.get('created_at', ''), reverse=True)

        if fmt == 'json':
            payload = {
                "export_time": _now_str(),
                "count": len(notes),
                "tags": data.get('tags', []),
                "notes": notes
            }
            content = json.dumps(payload, ensure_ascii=False, indent=2)
            mimetype = 'application/json; charset=utf-8'
            filename = _export_filename('json')
        else:
            content = _build_export_markdown(notes, data.get('tags', []))
            mimetype = 'text/markdown; charset=utf-8'
            filename = _export_filename('md')

        return Response(
            content,
            mimetype=mimetype,
            headers={'Content-Disposition': f'attachment; filename={filename}'}
        )
    except Exception as e:
        logger.error(f"[Journal] api_export 错误: {e}", exc_info=True)
        return jsonify({"code": 500, "message": str(e), "data": None})
