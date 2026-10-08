#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""美食打卡点冒烟测试（隔离库版）：验证 calorie_bp 的 food-spot 全部 API

测试隔离策略：
  1. require_isolated_test_db() 强校验 CRYPTO_TEST_DB_URL（不合规直接退出码 2），
     同时把 CRYPTO_PLAN_DATA_DIR 挪到临时目录 —— 照片落盘跟随该目录，
     测试写入的图片不会污染真实 data/ 目录。
  2. calorie_food_spots / calorie_food_spot_photos 为批次14新表，用例只在
     新表内造删数据；结束时无条件清空两表并删除临时照片目录。

覆盖：创建（含多图上传）/格式白名单拒收/回显/编辑/删单照/删整站级联清文件/
无效编号防穿越/GET 零写入/增量建表接线检查。
"""

import io
import os
import shutil
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
from flask import Flask  # noqa: E402
from crypto import calorie_routes as cr  # noqa: E402
from crypto.database import session_scope  # noqa: E402
from crypto.models import FoodSpot, FoodSpotPhoto  # noqa: E402

print(f"[Smoke] 目标数据库（隔离测试库）: {_TEST_DB_URL.split('@')[-1]}")
ensure_test_schema()

# 照片目录（resolve_data_dir 已被隔离到临时目录）
PHOTO_DIR = cr._spot_photo_dir()
print(f"[Smoke] 照片临时目录: {PHOTO_DIR}")

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


def _png_bytes(tag='a'):
    """最小合法 PNG（1x1）；内容仅作字节差异断言用，不要求可渲染"""
    return bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A]) + tag.encode() * 8


def _raw_multipart(fields, files):
    """构造真实 multipart/form-data 请求体。

    Werkzeug test client 的 dict 表单对同名字段列表只发最后一个文件，
    多图上传用例必须手工拼 boundary 才能一次带多张。
    返回 (body_bytes, content_type_header)。
    """
    boundary = '----FoodSpotSmokeBoundary7f3a'
    buf = io.BytesIO()
    for name, value in fields.items():
        buf.write(f'--{boundary}\r\nContent-Disposition: form-data; '
                  f'name="{name}"\r\n\r\n{value}\r\n'.encode('utf-8'))
    for name, filename, content in files:
        buf.write(f'--{boundary}\r\nContent-Disposition: form-data; '
                  f'name="{name}"; filename="{filename}"\r\n'
                  f'Content-Type: application/octet-stream\r\n\r\n'.encode('utf-8'))
        buf.write(content)
        buf.write(b'\r\n')
    buf.write(f'--{boundary}--\r\n'.encode('utf-8'))
    return buf.getvalue(), f'multipart/form-data; boundary={boundary}'


client_app = Flask(__name__, template_folder=os.path.join(_HERE, 'templates'))
client_app.register_blueprint(cr.calorie_bp)
client = client_app.test_client()

spot_id = ''
photo_ids = []

try:
    print('\n=== 1. 列表接口（空表） ===')
    r = client.get('/calorie/api/food-spots')
    data = r.get_json()
    check('列表 success', data['success'] is True)
    check('新表可查询（返回 spots 数组）', isinstance(data.get('spots'), list))

    print('\n=== 2. 创建校验 ===')
    r = client.post('/calorie/api/food-spot', data={'shop_name': '  '})
    check('空店铺名返回 400', r.status_code == 400)
    r = client.post('/calorie/api/food-spot',
                    data={'shop_name': '测试面馆', 'address': '一中路2号',
                          'category': '面馆', 'review': '汤头不错'})
    data = r.get_json()
    check('纯文本创建成功', r.status_code == 200 and data['success'] is True, str(data))
    spot_id = (data.get('spot') or {}).get('id', '')
    check('返回记录含 id/photos 结构', bool(spot_id)
          and data['spot']['photos'] == [], str(data.get('spot')))

    print('\n=== 3. 多图上传 ===')
    body, ctype = _raw_multipart(
        {'id': spot_id, 'shop_name': '测试面馆', 'address': '一中路2号',
         'category': '面馆', 'review': '汤头不错'},
        [('images[]', 'dish_special.png', _png_bytes('a')),
         ('images[]', 'shop_front.png', _png_bytes('b')),
         ('images[]', 'notes.txt', b'not an image')])
    r = client.post('/calorie/api/food-spot', data=body,
                    headers={'Content-Type': ctype})
    data = r.get_json()
    check('两张图片入库', data['success'] is True and len(data['spot']['photos']) == 2,
          str(data))
    photos = data['spot']['photos']
    photo_ids = [p['id'] for p in photos]
    check('position 顺序稳定', [p['position'] for p in photos] == [0, 1], str(photos))
    check('原始文件名保留展示用', photos[0]['original_name'] == 'dish_special.png')
    check('非图片格式被拒收', all(not p['original_name'].endswith('.txt') for p in photos))
    on_disk = sorted(os.listdir(PHOTO_DIR)) if PHOTO_DIR else []
    check('磁盘文件数与库索引一致', len(on_disk) == 2, str(on_disk))

    print('\n=== 4. 照片回显 ===')
    r = client.get(photos[0]['url'])
    check('回显 200 且字节内容正确', r.status_code == 200
          and r.data == _png_bytes('a'), str(r.status_code))
    r = client.get('/calorie/api/food-spot/photo/not-a-photo-id')
    check('非法 photo_id 返回 400（防穿越）', r.status_code == 400, str(r.status_code))
    # %2F 会被 Werkzeug 解码进 path 使路由不匹配（404）；两种拒绝都可接受，绝不能回显文件
    r = client.get('/calorie/api/food-spot/photo/fsp_..%2F..%2Fetc')
    check('编码路径穿越被拒（400/404）', r.status_code in (400, 404), str(r.status_code))
    r = client.get('/calorie/api/food-spot/photo/fsp_0000000000000000')
    check('不存在的照片返回 404', r.status_code == 404)

    print('\n=== 5. 编辑回填 ===')
    r = client.post('/calorie/api/food-spot', data={
        'id': spot_id, 'shop_name': '测试面馆(改)', 'address': '一中路2号',
        'category': '面馆', 'review': '二刷了，加辣更好吃'})
    data = r.get_json()
    check('更新文本字段生效', data['spot']['shop_name'] == '测试面馆(改)'
          and '二刷' in data['spot']['review'], str(data.get('spot')))
    check('已上传照片不因编辑丢失', len(data['spot']['photos']) == 2)
    r = client.post('/calorie/api/food-spot', data={'id': 'fspot_deadbeef',
                                                    'shop_name': '幽灵'})
    check('更新不存在的打卡点返回 404', r.status_code == 404)

    print('\n=== 6. 删除单张照片 ===')
    r = client.delete(f'/calorie/api/food-spot/photo/{photo_ids[0]}')
    check('删照片接口 success', r.get_json()['success'] is True)
    with session_scope() as s:
        left = s.execute(select(FoodSpotPhoto.filename)
                         .where(FoodSpotPhoto.spot_id == spot_id)).scalars().all()
    check('库中只剩 1 条照片索引', len(left) == 1, str(left))
    r = client.delete(f'/calorie/api/food-spot/photo/{photo_ids[0]}')
    check('重复删除同一照片返回 404', r.status_code == 404)

    print('\n=== 7. 删除打卡点（级联清文件） ===')
    r = client.delete(f'/calorie/api/food-spot/{spot_id}')
    check('删除打卡点 success', r.get_json()['success'] is True)
    with session_scope() as s:
        n_spot = len(s.execute(select(FoodSpot.id)).scalars().all())
        n_photo = len(s.execute(select(FoodSpotPhoto.id)).scalars().all())
    check('主表与照片表均已清空', n_spot == 0 and n_photo == 0, f'{n_spot}/{n_photo}')
    left_files = sorted(os.listdir(PHOTO_DIR)) if PHOTO_DIR else []
    check('磁盘照片已全部清理', left_files == [], str(left_files))
    r = client.delete(f'/calorie/api/food-spot/{spot_id}')
    check('重复删除打卡点返回 404', r.status_code == 404)
    spot_id = ''

    print('\n=== 8. GET 零写入 ===')
    r = client.get('/calorie/api/food-spots')
    check('GET 列表不创建任何行', r.get_json()['total'] == 0)
    files_after_get = sorted(os.listdir(PHOTO_DIR)) if PHOTO_DIR else []
    check('GET 不落盘任何文件', files_after_get == [], str(files_after_get))

    print('\n=== 9. 增量建表接线（_NEW_TABLE_NAMES） ===')
    from crypto.database import _NEW_TABLE_NAMES
    check('calorie_food_spots 已入补建清单', 'calorie_food_spots' in _NEW_TABLE_NAMES)
    check('calorie_food_spot_photos 已入补建清单',
          'calorie_food_spot_photos' in _NEW_TABLE_NAMES)

finally:
    print('\n=== 10. 清场 ===')
    try:
        with session_scope() as s:
            s.execute(delete(FoodSpotPhoto))
            s.execute(delete(FoodSpot))
    except Exception as e:
        print(f'  ⚠️ 清表失败（忽略）: {e}')
    if PHOTO_DIR and os.path.isdir(PHOTO_DIR):
        shutil.rmtree(PHOTO_DIR, ignore_errors=True)

print(f"\n{'=' * 50}\n冒烟测试结果: {PASS} 通过 / {FAIL} 失败\n{'=' * 50}")
sys.exit(1 if FAIL else 0)
