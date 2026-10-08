#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
随笔模块数据访问层（MySQL 版）
================================
替代原 journal_routes.py 中的 _load_data/_save_data JSON 文件读写。
业务语义与 JSON 版逐条对齐：
- 标签自动注册 + 色板轮换取色（_ensure_tag / _next_tag_color）
- 删标签同步摘除所有条目引用（由 note_tags 外键级联完成）
- 删条目同步清理其他条目的 linked_from 引用
- 标签顺序 = 创建顺序（journal_tags.created_at），与 JSON 数组顺序一致
"""

import logging

from sqlalchemy import select, delete, update

from .models import JournalTag, JournalNote, NoteTag
from .database import session_scope

logger = logging.getLogger(__name__)

# 预置 6 个标签（与 JSON 版 _DEFAULT_TAGS 一致）
DEFAULT_TAGS = [
    {'name': '交易心得', 'color': '#4a90d9'},
    {'name': '策略思考', 'color': '#7e57c2'},
    {'name': '复盘', 'color': '#e67e22'},
    {'name': '教训', 'color': '#e74c3c'},
    {'name': '经验', 'color': '#27ae60'},
    {'name': '生活随笔', 'color': '#95a5a6'}
]

# 标签自动分配色板（新增标签时轮换取色）
TAG_COLOR_PALETTE = [
    '#4a90d9', '#7e57c2', '#e67e22', '#e74c3c', '#27ae60', '#95a5a6',
    '#16a085', '#d81b60', '#5c6bc0', '#f39c12', '#00897b', '#8d6e63'
]


# =============================================================================
# 内部工具
# =============================================================================

def _next_tag_color(existing_tags):
    """为新标签轮换分配颜色（取色板中尚未被使用的第一个颜色）"""
    used = {t.get('color', '') for t in existing_tags}
    for c in TAG_COLOR_PALETTE:
        if c not in used:
            return c
    return TAG_COLOR_PALETTE[len(existing_tags) % len(TAG_COLOR_PALETTE)]


def _tags_of(session, note_id):
    """取条目标签名列表（按标签创建顺序，与原 JSON 数组顺序对齐）"""
    rows = session.execute(
        select(NoteTag.tag_name, JournalTag.created_at)
        .join(JournalTag, NoteTag.tag_name == JournalTag.name)
        .where(NoteTag.note_id == note_id)
        .order_by(JournalTag.created_at, JournalTag.name)
    ).all()
    return [r[0] for r in rows]


def _note_with_tags(session, note):
    """ORM 对象 -> 与 JSON 版结构一致的 dict"""
    return note.to_dict(tags=_tags_of(session, note.id))


def _parse_ts(s):
    """'YYYY-MM-DD HH:MM:SS' -> datetime（解析失败返回 None，由默认值兜底）"""
    if not s:
        return None
    import datetime
    try:
        return datetime.datetime.strptime(s, '%Y-%m-%d %H:%M:%S')
    except (TypeError, ValueError):
        return None


# =============================================================================
# 读取
# =============================================================================

def load_tags(session):
    """标签字典列表（含创建顺序）；空表时自动补种子预置标签
    （对齐 JSON 版“数据缺失时重建默认数据”的兜底行为）"""
    rows = session.execute(
        select(JournalTag).order_by(JournalTag.created_at, JournalTag.name)
    ).scalars().all()
    if not rows:
        session.add_all([JournalTag(name=t['name'], color=t['color'])
                         for t in DEFAULT_TAGS])
        session.flush()
        rows = session.execute(
            select(JournalTag).order_by(JournalTag.created_at, JournalTag.name)
        ).scalars().all()
    return [t.to_dict() for t in rows]


def load_notes(session):
    """全部条目（按创建时间正序，与原文件追加顺序一致）

    性能约定：全量仅 2 条 SQL（notes + note_tags 各一条），
    标签在内存按 note_id 分组，避免逐条标签查询在远端 MySQL
    上产生 N+1 网络往返。
    """
    rows = session.execute(
        select(JournalNote).order_by(JournalNote.created_at, JournalNote.id)
    ).scalars().all()
    if not rows:
        return []
    tag_rows = session.execute(
        select(NoteTag.note_id, NoteTag.tag_name, JournalTag.created_at)
        .join(JournalTag, NoteTag.tag_name == JournalTag.name)
        .where(NoteTag.note_id.in_([n.id for n in rows]))
        .order_by(JournalTag.created_at, JournalTag.name)
    ).all()
    tags_map = {}
    for note_id, tag_name, _ts in tag_rows:
        tags_map.setdefault(note_id, []).append(tag_name)
    return [n.to_dict(tags=tags_map.get(n.id, [])) for n in rows]


def load_journal_data(session):
    """组装与 JSON 版 _load_data() 完全一致的数据结构，
    供统计引擎 / 筛选 / 导出等纯函数复用"""
    return {
        'initialized': True,
        'tags': load_tags(session),
        'notes': load_notes(session)
    }


def get_note(session, note_id):
    note = session.get(JournalNote, note_id)
    return _note_with_tags(session, note) if note else None


# =============================================================================
# 标签操作
# =============================================================================

def ensure_tag(session, name, color=None):
    """确保标签存在：不存在则创建（色值缺省时轮换取色）；返回标签 dict"""
    name = (name or '').strip()
    if not name:
        return None
    tag = session.get(JournalTag, name)
    if tag is None:
        tag = JournalTag(name=name, color=color or _next_tag_color(load_tags(session)))
        session.add(tag)
        session.flush()
    return tag.to_dict()


def save_tag(session, name, color=None):
    """新增标签 / 修改标签颜色（同名视为改色）；返回 (标签dict, 是否新建)"""
    tag = session.get(JournalTag, name)
    if tag is not None:
        if color:
            tag.color = color
        return tag.to_dict(), False
    tag = JournalTag(name=name, color=color or _next_tag_color(load_tags(session)))
    session.add(tag)
    session.flush()
    return tag.to_dict(), True


def delete_tag(session, name):
    """删除标签（条目引用由外键级联摘除，不删条目）；返回是否存在并删除"""
    tag = session.get(JournalTag, name)
    if tag is None:
        return False
    session.execute(delete(NoteTag).where(NoteTag.tag_name == name))
    session.delete(tag)
    session.flush()
    return True


# =============================================================================
# 条目操作
# =============================================================================

def _set_note_tags(session, note_id, tag_names):
    """重建条目标签关联（标签须已存在）"""
    session.execute(delete(NoteTag).where(NoteTag.note_id == note_id))
    for t in tag_names:
        session.add(NoteTag(note_id=note_id, tag_name=t))
    session.flush()


def add_note(session, note):
    """新增条目。note 为与 JSON 版结构一致的 dict（id/时间戳由调用方生成）"""
    row = JournalNote(
        id=note['id'],
        type=note.get('type', 'note'),
        content=note.get('content', ''),
        pinned=bool(note.get('pinned', False)),
        distilled=bool(note.get('distilled', False)),
        linked_from=note.get('linked_from') or '',
        created_at=_parse_ts(note.get('created_at')),
        updated_at=_parse_ts(note.get('updated_at'))
    )
    rv = note.get('review') or {}
    if row.type == 'review':
        row.review_subject = rv.get('subject', '')
        row.review_decision = rv.get('decision', '')
        row.review_outcome = rv.get('outcome', '')
        row.review_lesson = rv.get('lesson', '')
    session.add(row)
    session.flush()
    _set_note_tags(session, row.id, note.get('tags') or [])


def update_note(session, note_id, content=None, tags=None, review=None):
    """编辑条目（content / tags / review 均可选）；返回更新后 dict 或 None"""
    note = session.get(JournalNote, note_id)
    if note is None:
        return None
    if content is not None:
        note.content = content
    if tags is not None:
        _set_note_tags(session, note_id, tags)
    if review is not None and note.type == 'review':
        note.review_subject = review.get('subject', '')
        note.review_decision = review.get('decision', '')
        note.review_outcome = review.get('outcome', '')
        note.review_lesson = review.get('lesson', '')
    import datetime
    note.updated_at = datetime.datetime.now()
    session.flush()
    return _note_with_tags(session, note)


def delete_note(session, note_id):
    """删除条目 + 清理其他条目对它的 linked_from 引用；返回是否删除成功"""
    note = session.get(JournalNote, note_id)
    if note is None:
        return False
    session.execute(delete(NoteTag).where(NoteTag.note_id == note_id))
    session.execute(
        update(JournalNote).where(JournalNote.linked_from == note_id)
        .values(linked_from=''))
    session.delete(note)
    session.flush()
    return True


def toggle_pin(session, note_id):
    """置顶切换；返回更新后 dict 或 None"""
    note = session.get(JournalNote, note_id)
    if note is None:
        return None
    note.pinned = not note.pinned
    import datetime
    note.updated_at = datetime.datetime.now()
    session.flush()
    return _note_with_tags(session, note)


def toggle_distill(session, note_id):
    """提炼经验切换；返回 (更新后dict, 错误消息)，错误时 dict 为 None"""
    note = session.get(JournalNote, note_id)
    if note is None:
        return None, '条目不存在'
    if note.type != 'review':
        return None, '只有复盘条目可以提炼经验'
    note.distilled = not note.distilled
    import datetime
    note.updated_at = datetime.datetime.now()
    session.flush()
    return _note_with_tags(session, note), None
