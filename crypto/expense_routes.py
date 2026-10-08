"""统一支出网页与 API；不调用交易服务，GET 保持只读。"""
import csv
import io
from functools import wraps
from urllib.parse import urlsplit

from flask import Blueprint, jsonify, render_template, request, Response, current_app
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from werkzeug.exceptions import BadRequest, RequestEntityTooLarge

from .database import session_scope
from . import expense_service as svc, expense_repo as repo
from . import expense_budget_repo as budgets, expense_queries as queries

expense_bp = Blueprint('expense_bp', __name__, url_prefix='/expense')


def error(message, status, details=None):
    return jsonify(success=False, data=None, error={'message': message, 'details': details}), status


@expense_bp.before_request
def protect_write():
    if request.method in ('GET', 'HEAD', 'OPTIONS'):
        return None
    if request.content_length and request.content_length > 128 * 1024:
        raise RequestEntityTooLarge()
    if request.mimetype != 'application/json' or request.headers.get('X-Expense-Request') != '1':
        return error('写入须使用 JSON 和专用请求头', 400)
    if request.headers.get('Sec-Fetch-Site') == 'cross-site':
        return error('不允许跨站写入', 403)
    origin = request.headers.get('Origin') or request.headers.get('Referer')
    if origin:
        try:
            source, target = urlsplit(origin), urlsplit(request.host_url)
            same = source.scheme == target.scheme and source.netloc == target.netloc
        except ValueError:
            same = False
        if not same:
            return error('不允许跨站写入', 403)
    # 浏览器不能跨域附带专用请求头，且模块从不开放 CORS。
    body = request.get_json()
    if not isinstance(body, dict):
        return error('请求正文须为 JSON 对象', 400)


@expense_bp.after_request
def private_response(response):
    response.headers['Cache-Control'] = 'no-store, private'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'same-origin'
    return response


@expense_bp.errorhandler(svc.ExpenseError)
def business_error(exc):
    return error(str(exc), exc.status, exc.details)


@expense_bp.errorhandler(BadRequest)
def bad_json(exc):
    return error('JSON 格式不正确', 400)


@expense_bp.errorhandler(RequestEntityTooLarge)
def too_large(exc):
    return error('请求正文过大', 413)


@expense_bp.errorhandler(IntegrityError)
def integrity_error(exc):
    return error('数据冲突，请刷新后重试', 409)


@expense_bp.errorhandler(SQLAlchemyError)
@expense_bp.errorhandler(RuntimeError)
def database_error(exc):
    return error('数据库暂不可用；请保留表单并使用原保存标识重试', 503)


def api(write=False, setup=False, summary=False):
    def decorate(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            with session_scope() as session:
                settings = repo.lock_settings(session, setup=setup) if write else repo.get_settings(session)
                data = fn(session, settings, request.get_json() if write else request.args, *args, **kwargs)
            # 到这里主事务已提交；统计失败不能将成功保存变成失败。
            if summary:
                try:
                    with session_scope() as session:
                        data['dashboard'] = queries.dashboard(session, repo.get_settings(session), {})
                except Exception:
                    data['statistics_pending'] = True
                    data['message'] = '已保存，统计待刷新'
                    current_app.logger.warning('支出记录已提交，附带统计待刷新')
            return jsonify(success=True, data=data, error=None)
        return wrapped
    return decorate


@expense_bp.get('')
def page():
    return render_template('expense.html', active_page='expense')


@expense_bp.get('/api/bootstrap')
@api()
def bootstrap(session, settings, args):
    recent = list(dict.fromkeys(session.scalars(select(repo.Record.category_id).where(
        repo.Record.deleted_at.is_(None)).order_by(repo.Record.created_at.desc()).limit(100))))[:8]
    return {'settings': repo.settings_dict(settings), 'recent_categories': recent, **repo.dictionary(session)}


@expense_bp.get('/api/settings')
@api()
def settings_get(session, settings, args):
    return repo.settings_dict(settings)


@expense_bp.post('/api/setup')
@api(write=True, setup=True)
def setup_post(session, settings, body):
    result = repo.setup(session, settings, body)
    budgets.open_period(session, settings, svc.today(settings.timezone))
    return result


@expense_bp.patch('/api/settings')
@api(write=True)
def settings_patch(session, settings, body):
    return repo.update_settings(session, settings, body)


@expense_bp.get('/api/dashboard')
@api()
def dashboard(session, settings, args):
    return queries.dashboard(session, settings, args)


@expense_bp.get('/api/charts')
@api()
def charts(session, settings, args):
    return queries.analytics(session, settings, args)


@expense_bp.get('/api/records')
@api()
def records_get(session, settings, args):
    return queries.records(session, settings, args)


@expense_bp.post('/api/records')
@api(write=True, summary=True)
def records_post(session, settings, body):
    return repo.save_record(session, settings, body)


@expense_bp.get('/api/records/<key>')
@api()
def record_get(session, settings, args, key):
    return queries.detail(session, key)


@expense_bp.patch('/api/records/<key>')
@api(write=True, summary=True)
def record_patch(session, settings, body, key):
    return repo.save_record(session, settings, body, key)


@expense_bp.delete('/api/records/<key>')
@api(write=True, summary=True)
def record_delete(session, settings, body, key):
    return repo.delete_or_restore(session, repo.require(session, repo.Record, key), body)


@expense_bp.post('/api/records/<key>/restore')
@api(write=True, summary=True)
def record_restore(session, settings, body, key):
    return repo.delete_or_restore(session, repo.require(session, repo.Record, key), body, restore=True)


@expense_bp.get('/api/preview')
@api()
def preview(session, settings, args):
    return queries.preview(session, settings, args)


@expense_bp.route('/api/<dictionary>', methods=['GET', 'POST'])
@expense_bp.route('/api/<dictionary>/<key>', methods=['PATCH', 'DELETE'])
def dictionaries(dictionary, key=None):
    if dictionary not in ('categories', 'tags'):
        raise svc.ExpenseError('接口不存在', 404)
    model = repo.Category if dictionary == 'categories' else repo.Tag
    if request.method == 'GET':
        with session_scope() as session:
            data = repo.dictionary(session)[dictionary]
    else:
        with session_scope() as session:
            repo.lock_settings(session)
            data = repo.edit_dictionary(session, model, request.get_json(), key, request.method == 'DELETE')
    return jsonify(success=True, data=data, error=None)


@expense_bp.post('/api/periods/open')
@api(write=True)
def period_open(session, settings, body):
    when = svc.day(body.get('date', svc.today(settings.timezone).isoformat()))
    return budgets.period_dict(budgets.open_period(session, settings, when))


@expense_bp.get('/api/periods/<key>')
@api()
def period_get(session, settings, args, key):
    row = repo.require(session, repo.Period, key)
    result = budgets.period_dict(row)
    result['history'] = [repo.snapshot(log) for log in session.scalars(select(repo.ChangeLog).where(
        repo.ChangeLog.entity_type == 'expense_periods', repo.ChangeLog.entity_id == key)
        .order_by(repo.ChangeLog.created_at.desc()).limit(100))]
    return result


@expense_bp.patch('/api/periods/<key>/budget')
@api(write=True)
def budget_patch(session, settings, body, key):
    return budgets.update_budget(session, repo.require(session, repo.Period, key), body)


@expense_bp.get('/api/bill-templates')
@api()
def templates_get(session, settings, args):
    return [budgets.template_dict(row) for row in session.scalars(select(repo.BillTemplate).order_by(repo.BillTemplate.name))]


@expense_bp.post('/api/bill-templates')
@api(write=True)
def template_post(session, settings, body):
    return budgets.save_template(session, settings, body)


@expense_bp.route('/api/bill-templates/<key>', methods=['PATCH', 'DELETE'])
@api(write=True)
def template_update(session, settings, body, key):
    return budgets.save_template(session, settings, body, key, request.method == 'DELETE')


@expense_bp.get('/api/bills')
@api()
def bills_get(session, settings, args):
    start, end, _, _ = queries.range_for(session, settings, args)
    return [b for b in budgets.list_bills(session, end) if b['due_date'] >= start.isoformat() or b['status'] == 'open']


@expense_bp.post('/api/bills/<key>/<action>')
@api(write=True, summary=True)
def bill_post(session, settings, body, key, action):
    return budgets.bill_action(session, settings, key, action, body)


@expense_bp.post('/api/daily-checks')
@api(write=True)
def check_post(session, settings, body):
    return repo.daily_check(session, settings, body)


@expense_bp.get('/api/export')
def export():
    output = io.StringIO(newline='')
    output.write('\ufeff')
    writer = csv.writer(output)
    writer.writerow(['日期', '金额', '标题', '类型', '分类', '标签', '商户', '支付方式', '备注', '状态', '记录ID'])
    with session_scope() as session:
        settings = repo.get_settings(session)
        dictionary = repo.dictionary(session)
        categories = {r['id']: r['name'] for r in dictionary['categories']}
        tags = {r['id']: r['name'] for r in dictionary['tags']}
        args = {**request.args.to_dict(), 'page_size': '100'}
        page_number = 1
        while True:
            args['page'] = str(page_number)
            result = queries.records(session, settings, args)
            for row in result['records']:
                writer.writerow([row['business_date'], row['amount'], svc.safe_csv(row['title']), row['kind'],
                    svc.safe_csv(categories.get(row['category_id'], '')),
                    svc.safe_csv(' / '.join(tags.get(t, '') for t in row['tag_ids'])),
                    svc.safe_csv(row['merchant']), svc.safe_csv(row['payment_method']), svc.safe_csv(row['note']),
                    '已作废' if row['deleted_at'] else '有效', row['id']])
            if page_number * 100 >= result['total']:
                break
            page_number += 1
    return Response(output.getvalue(), content_type='text/csv; charset=utf-8',
                    headers={'Content-Disposition': 'attachment; filename="expenses.csv"'})
