#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""随笔与复盘模块冒烟测试（MySQL 版）：用 Flask test_client 验证 journal_bp 全部 API

测试隔离策略（保护真实数据）：
  1. 运行前快照 journal_tags / journal_notes / note_tags 三表
  2. 清空三表后执行全部 API 用例
  3. finally 中无条件恢复快照（无论用例成败）

前置条件：环境变量 CRYPTO_TEST_DB_URL 指向专用隔离测试库。
未配置或不合规时退出码 2 —— 本用例会清空三张随笔表，绝不允许在业务库上跑。
"""

import json
import os
import sys

# Windows 终端默认 GBK，强制 UTF-8 输出避免中文/emoji 乱码报错
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from crypto.test_isolation import (  # noqa: E402
    require_isolated_test_db, ensure_test_schema, TestDbNotConfigured)

try:
    _TEST_DB_URL = require_isolated_test_db()
except TestDbNotConfigured as e:
    print(f'❌ {e}')
    sys.exit(2)

from sqlalchemy import select, delete  # noqa: E402
from crypto import journal_routes as jr  # noqa: E402
from crypto.database import get_engine, session_scope  # noqa: E402
from crypto.models import JournalTag, JournalNote, NoteTag  # noqa: E402
from flask import Flask  # noqa: E402

print(f"[Smoke] 目标数据库（隔离测试库）: {_TEST_DB_URL.split('@')[-1]}")
# 表结构补齐：全新隔离库（或本地 SQLite 文件）首次跑时表还不存在；只建表不动数据
ensure_test_schema()

# =============================================================================
# 真实数据快照 + 清场（测试不污染生产数据）
# =============================================================================

def _snapshot():
    with session_scope() as s:
        tags = [(t.name, t.color, t.created_at) for t in
                s.execute(select(JournalTag)).scalars()]
        notes = [(n.id, n.type, n.content, n.review_subject, n.review_decision,
                  n.review_outcome, n.review_lesson, bool(n.pinned), bool(n.distilled),
                  n.linked_from, n.created_at, n.updated_at) for n in
                 s.execute(select(JournalNote)).scalars()]
        links = [(r.note_id, r.tag_name) for r in s.execute(select(NoteTag)).scalars()]
    return tags, notes, links


def _clean():
    with session_scope() as s:
        s.execute(delete(NoteTag))
        s.execute(delete(JournalNote))
        s.execute(delete(JournalTag))


def _restore(snap):
    tags, notes, links = snap
    with session_scope() as s:
        s.execute(delete(NoteTag))
        s.execute(delete(JournalNote))
        s.execute(delete(JournalTag))
        for name, color, created_at in tags:
            s.add(JournalTag(name=name, color=color, created_at=created_at))
        s.flush()
        for row in notes:
            s.add(JournalNote(
                id=row[0], type=row[1], content=row[2],
                review_subject=row[3], review_decision=row[4],
                review_outcome=row[5], review_lesson=row[6],
                pinned=row[7], distilled=row[8], linked_from=row[9],
                created_at=row[10], updated_at=row[11]))
        s.flush()
        for note_id, tag_name in links:
            s.add(NoteTag(note_id=note_id, tag_name=tag_name))


_SNAP = _snapshot()
print(f"[Smoke] 已快照真实数据: 标签 {len(_SNAP[0])} / 条目 {len(_SNAP[1])}，测试后自动恢复")
_clean()

app = Flask(__name__, template_folder=os.path.join(_HERE, 'templates'))
app.register_blueprint(jr.journal_bp)
client = app.test_client()

PASS = 0
FAIL = 0


def check(name, cond, extra=''):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name} {extra}")


def post(url, body):
    return client.post(url, data=json.dumps(body), content_type='application/json')


try:
    print('\n=== 1. 页面路由 ===')
    r = client.get('/journal')
    check('页面返回 200', r.status_code == 200)
    check('页面含 journal.js 引用', b'journal.js' in r.data)

    print('\n=== 2. 初始列表（预置 6 标签）===')
    r = client.get('/journal/api/list')
    data = r.get_json()['data']
    check('列表接口 code=200', r.get_json()['code'] == 200)
    check('预置 6 个标签', len(data['tags']) == 6, str(len(data['tags'])))
    check('初始无条目', len(data['notes']) == 0)

    print('\n=== 3. 新增随笔（含自动创建新标签）===')
    r = post('/journal/api/add', {'content': '今天状态不错，BOLL 收口等变盘。', 'tags': ['交易心得', '灵感']})
    res = r.get_json()
    check('新增成功', res['code'] == 200)
    note_id = res['data']['id']
    check('新标签自动注册', any(t['name'] == '灵感' for t in client.get('/journal/api/tags').get_json()['data']))

    print('\n=== 4. 新增复盘（四格 + 关联）===')
    r = post('/journal/api/add', {
        'content': '追高被套，止损晚了。', 'type': 'review', 'tags': ['复盘', '教训'],
        'review': {'subject': 'BTC 多单', 'decision': '追高', 'outcome': '-3%', 'lesson': '不追单'},
        'linked_from': note_id
    })
    res = r.get_json()
    check('复盘新增成功', res['code'] == 200)
    review_id = res['data']['id']
    check('关联 id 生效', res['data']['linked_from'] == note_id)

    print('\n=== 5. 空内容校验 ===')
    r = post('/journal/api/add', {'content': '   '})
    check('空内容返回 400', r.get_json()['code'] == 400)

    print('\n=== 6. 编辑 ===')
    r = post('/journal/api/update', {'id': note_id, 'fields': {'content': '修改后的内容', 'tags': ['经验']}})
    res = r.get_json()
    check('编辑成功', res['code'] == 200 and res['data']['content'] == '修改后的内容')

    print('\n=== 7. 置顶 / 提炼 ===')
    r = post('/journal/api/pin', {'id': note_id})
    check('置顶成功', r.get_json()['data']['pinned'] is True)
    r = post('/journal/api/distill', {'id': note_id})
    check('随笔提炼被拒', r.get_json()['code'] == 400)
    r = post('/journal/api/distill', {'id': review_id})
    check('复盘提炼成功', r.get_json()['data']['distilled'] is True)

    print('\n=== 8. 标签管理 ===')
    r = post('/journal/api/tag-save', {'name': '风控', 'color': '#16a085'})
    check('新建标签', r.get_json()['code'] == 200)
    r = post('/journal/api/tag-save', {'name': '风控', 'color': '#d81b60'})
    check('同名改色', r.get_json()['data']['color'] == '#d81b60')
    r = post('/journal/api/tag-save', {'name': '这个名字超过八个字了吧'})
    check('超长标签名被拒', r.get_json()['code'] == 400)
    r = post('/journal/api/tag-delete', {'name': '灵感'})
    check('删除标签', r.get_json()['code'] == 200)
    r = client.get('/journal/api/list').get_json()
    check('条目引用同步清理', all('灵感' not in n.get('tags', []) for n in r['data']['notes']))

    print('\n=== 9. 统计看板 ===')
    r = client.get('/journal/api/stats')
    s = r.get_json()['data']
    check('统计 code=200', r.get_json()['code'] == 200)
    check('total=2', s['total'] == 2, str(s['total']))
    check('review_count=1', s['review_count'] == 1)
    check('distilled_count=1', s['distilled_count'] == 1)
    check('streak_days=1', s['streak_days'] == 1)
    check('日历 105 天', len(s['calendar']['days']) == 105)

    print('\n=== 10. 筛选 ===')
    r = client.get('/journal/api/list?type=review')
    check('按类型筛选', len(r.get_json()['data']['notes']) == 1)
    r = client.get('/journal/api/list?keyword=%E4%BF%AE%E6%94%B9')
    check('关键词筛选', len(r.get_json()['data']['notes']) == 1)
    r = client.get('/journal/api/list?tag=%E5%A4%8D%E7%9B%98')
    check('标签筛选', len(r.get_json()['data']['notes']) == 1)
    r = client.get('/journal/api/list?days=7')
    check('近 7 天筛选', len(r.get_json()['data']['notes']) == 2)

    print('\n=== 11. 导出 ===')
    r = client.get('/journal/api/export?format=md')
    text = r.data.decode('utf-8')
    check('MD 导出 200', r.status_code == 200)
    check('MD 含标题', '# 随笔与复盘导出' in text)
    check('MD 含正文', '修改后的内容' in text)
    check('MD 含复盘四格', '**经验教训**' in text)
    check('附件头正确', 'attachment' in r.headers.get('Content-Disposition', ''))
    r = client.get('/journal/api/export?format=json')
    payload = json.loads(r.data.decode('utf-8'))
    check('JSON 导出 count=2', payload['count'] == 2)
    r = client.get('/journal/api/export?format=xml')
    check('非法格式被拒', r.get_json()['code'] == 400)

    print('\n=== 12. 删除 ===')
    r = post('/journal/api/delete', {'id': review_id})
    check('删除成功', r.get_json()['code'] == 200)
    r = post('/journal/api/delete', {'id': 'note_notexist'})
    check('删除不存在返回 404', r.get_json()['code'] == 404)

    print('\n=== 13. 删除条目级联清理关联引用 ===')
    r = post('/journal/api/add', {'content': '被引用的源条目'})
    src_id = r.get_json()['data']['id']
    r = post('/journal/api/add', {'content': '引用者', 'linked_from': src_id})
    ref_id = r.get_json()['data']['id']
    post('/journal/api/delete', {'id': src_id})
    r = client.get('/journal/api/list').get_json()
    ref_note = next((n for n in r['data']['notes'] if n['id'] == ref_id), None)
    check('引用方 linked_from 已清空', ref_note is not None and ref_note['linked_from'] == '')
    post('/journal/api/delete', {'id': ref_id})
finally:
    print('\n=== 恢复真实数据快照 ===')
    _restore(_SNAP)
    with session_scope() as s:
        n_tags = len(s.execute(select(JournalTag)).scalars().all())
        n_notes = len(s.execute(select(JournalNote)).scalars().all())
    ok = (n_tags == len(_SNAP[0]) and n_notes == len(_SNAP[1]))
    print(f"  {'✅' if ok else '❌'} 恢复完成: 标签 {n_tags} / 条目 {n_notes}")

print(f'\n================ 结果：{PASS} 通过 / {FAIL} 失败 ================')
sys.exit(1 if FAIL else 0)
