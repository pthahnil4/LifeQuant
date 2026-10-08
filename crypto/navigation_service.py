#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
导航自定义管理 —— 纯逻辑服务层
================================
本模块只做「不碰数据库、不碰 HTTP」的纯计算：默认目录、结构校验、
URL 安全校验、归一化、移动与删除规则。读写落库在 navigation_repo.py，
接口编排在 navigation_routes.py。

设计口径（见「导航自定义管理技术方案与设计文档」）：
- 配置为带元信息的外层对象，items 是链接与分类的平面节点数组，
  通过 parent_id 建立「最多两层」的父子关系（顶层=链接或分类，分类内=链接）。
- 数据库决定用户意图；隐藏/删除是显式状态，绝不因“为空”而偷偷恢复默认。
- 所有业务链接（含内置项）一律可改名/改URL/改图标/排序/隐藏/删除，
  builtin_key 只作来源标记，不赋予不可删除权限。
"""

import re
import uuid
from urllib.parse import urlsplit

# ------------------------------------------------------------------ 常量与上限
SCHEMA_VERSION = 1
CONFIG_KEY = 'navigation_config:default'

MAX_NODES = 80                 # 节点总数上限
MAX_NAME_CHARS = 24            # 名称最大 Unicode 字符
MAX_URL_CHARS = 2048           # URL 最大字符
MAX_ICON_CHARS = 8             # 纯文本图标最大字符（emoji 组合可能占多 code point）
MAX_CANONICAL_BYTES = 48 * 1024  # 规范化后完整 JSON 的 UTF-8 字节上限

DESKTOP_MAX_VISIBLE_MIN = 3
DESKTOP_MAX_VISIBLE_MAX = 8
DEFAULT_DESKTOP_MAX_VISIBLE = 6

LINK = 'link'
GROUP = 'group'

# 内置页面目录里禁止作为导航目标的动作端点前缀（导航只做页面跳转）
_BLOCKED_INTERNAL_PREFIXES = (
    '/api/', '/auth/', '/plan/api/', '/calorie/api/', '/journal/api/',
    '/instinct/api/', '/alert/api/', '/diary/api/',
)
_BLOCKED_INTERNAL_EXACT = {
    '/auth/gate', '/auth/logout', '/api/task/trading/force-close',
}

# 已知敏感 query 参数（不得把访问口令/凭据嵌进导航 URL）
_SENSITIVE_QUERY_KEYS = {'token', 'password', 'passwd', 'secret', 'apikey',
                         'api_key', 'accesskey', 'auth', 'key'}

_ALLOWED_TARGETS = {'_self', '_blank'}
_ICON_BUILTIN_KEYS = {
    'dashboard', 'trend', 'star', 'analysis', 'task', 'alert', 'instinct',
    'plan', 'journal', 'calorie', 'expense', 'diary', 'okx', 'api', 'system',
    'nav', 'coin', 'guide', 'wiki',
}
_ICON_TYPE_KEYS = {'builtin', 'text', None}


class NavValidationError(ValueError):
    """配置结构/字段校验失败；调用方据此返回 400。"""


# =============================================================================
# 内置页面目录（默认分类来源 + 管理页可添加项来源）
# =============================================================================
# 默认导航：顶层顺序 监控台→分析记录→行情→交易→个人管理→工具与系统。
# 这些只是「数据库无记录时」的初始值，用户可自由改动、隐藏、删除。
DEFAULT_ITEMS = [
    {'id': 'link-dashboard', 'type': LINK, 'parent_id': None, 'name': '监控台',
     'url': '/', 'icon': {'type': 'builtin', 'value': 'dashboard'},
     'order': 0, 'visible': True, 'target': '_self', 'builtin_key': 'dashboard'},
    {'id': 'link-analysis', 'type': LINK, 'parent_id': None, 'name': '分析记录',
     'url': '/analysis', 'icon': {'type': 'builtin', 'value': 'analysis'},
     'order': 1, 'visible': True, 'target': '_self', 'builtin_key': 'analysis'},
    {'id': 'group-market', 'type': GROUP, 'parent_id': None, 'name': '行情',
     'url': None, 'icon': {'type': 'builtin', 'value': 'trend'},
     'order': 2, 'visible': True, 'target': None, 'builtin_key': None},
    {'id': 'link-market-scan', 'type': LINK, 'parent_id': 'group-market', 'name': '趋势扫描',
     'url': '/market-scan', 'icon': {'type': 'builtin', 'value': 'trend'},
     'order': 0, 'visible': True, 'target': '_self', 'builtin_key': 'market-scan'},
    {'id': 'link-star-market', 'type': LINK, 'parent_id': 'group-market', 'name': '星标行情',
     'url': '/star-market', 'icon': {'type': 'builtin', 'value': 'star'},
     'order': 1, 'visible': True, 'target': '_self', 'builtin_key': 'star-market'},
    {'id': 'link-indicator-nav', 'type': LINK, 'parent_id': 'group-market', 'name': '指标导航',
     'url': '/indicator-nav', 'icon': {'type': 'builtin', 'value': 'trend'},
     'order': 2, 'visible': True, 'target': '_self', 'builtin_key': 'indicator-nav'},
    {'id': 'link-kline-live', 'type': LINK, 'parent_id': 'group-market', 'name': 'K线拉取',
     'url': '/kline-live', 'icon': {'type': 'builtin', 'value': 'trend'},
     'order': 3, 'visible': True, 'target': '_self', 'builtin_key': 'kline-live'},
    {'id': 'group-trade', 'type': GROUP, 'parent_id': None, 'name': '交易',
     'url': None, 'icon': {'type': 'builtin', 'value': 'task'},
     'order': 3, 'visible': True, 'target': None, 'builtin_key': None},
    {'id': 'link-task', 'type': LINK, 'parent_id': 'group-trade', 'name': '定时任务',
     'url': '/task', 'icon': {'type': 'builtin', 'value': 'task'},
     'order': 0, 'visible': True, 'target': '_self', 'builtin_key': 'task'},
    {'id': 'link-alert', 'type': LINK, 'parent_id': 'group-trade', 'name': '风险警报',
     'url': '/alert', 'icon': {'type': 'builtin', 'value': 'alert'},
     'order': 1, 'visible': True, 'target': '_self', 'builtin_key': 'alert'},
    {'id': 'link-instinct', 'type': LINK, 'parent_id': 'group-trade', 'name': '盘感模拟',
     'url': '/instinct', 'icon': {'type': 'builtin', 'value': 'instinct'},
     'order': 2, 'visible': True, 'target': '_self', 'builtin_key': 'instinct'},
    {'id': 'group-personal', 'type': GROUP, 'parent_id': None, 'name': '个人管理',
     'url': None, 'icon': {'type': 'builtin', 'value': 'plan'},
     'order': 4, 'visible': True, 'target': None, 'builtin_key': None},
    {'id': 'link-plan', 'type': LINK, 'parent_id': 'group-personal', 'name': '任务计划',
     'url': '/plan', 'icon': {'type': 'builtin', 'value': 'plan'},
     'order': 0, 'visible': True, 'target': '_self', 'builtin_key': 'plan'},
    {'id': 'link-checkin', 'type': LINK, 'parent_id': 'group-personal', 'name': '每日评级总控台',
     'url': '/checkin', 'icon': {'type': 'builtin', 'value': 'task'},
     'order': 1, 'visible': True, 'target': '_self', 'builtin_key': 'checkin'},
    {'id': 'link-journal', 'type': LINK, 'parent_id': 'group-personal', 'name': '随笔复盘',
     'url': '/journal', 'icon': {'type': 'builtin', 'value': 'journal'},
     'order': 2, 'visible': True, 'target': '_self', 'builtin_key': 'journal'},
    {'id': 'link-calorie', 'type': LINK, 'parent_id': 'group-personal', 'name': '热量缺口',
     'url': '/calorie', 'icon': {'type': 'builtin', 'value': 'calorie'},
     'order': 3, 'visible': True, 'target': '_self', 'builtin_key': 'calorie'},
    {'id': 'link-expense', 'type': LINK, 'parent_id': 'group-personal', 'name': '支出管理',
     'url': '/expense', 'icon': {'type': 'builtin', 'value': 'expense'},
     'order': 4, 'visible': True, 'target': '_self', 'builtin_key': 'expense'},
    {'id': 'link-diary', 'type': LINK, 'parent_id': 'group-personal', 'name': '日记本',
     'url': '/diary', 'icon': {'type': 'builtin', 'value': 'diary'},
     'order': 5, 'visible': True, 'target': '_self', 'builtin_key': 'diary'},
    {'id': 'group-market-multi', 'type': GROUP, 'parent_id': None, 'name': '多市场',
     'url': None, 'icon': {'type': 'builtin', 'value': 'trend'},
     'order': 5, 'visible': True, 'target': None, 'builtin_key': None},
    # 以下两项由 futureStockTrade 合并迁移 Phase 2 引入（期货监控台 + 期货星标行情）
    {'id': 'link-futures', 'type': LINK, 'parent_id': 'group-market-multi', 'name': '期货监控台',
     'url': '/futures/', 'icon': {'type': 'builtin', 'value': 'trend'},
     'order': 0, 'visible': True, 'target': '_self', 'builtin_key': 'futures'},
    {'id': 'link-futures-star', 'type': LINK, 'parent_id': 'group-market-multi', 'name': '期货星标行情',
     'url': '/futures/star-market', 'icon': {'type': 'builtin', 'value': 'star'},
     'order': 1, 'visible': True, 'target': '_self', 'builtin_key': 'futures-star-market'},
    # 以下一项由 futureStockTrade 合并迁移 Phase 3 引入（A 股监控台）
    {'id': 'link-stocks', 'type': LINK, 'parent_id': 'group-market-multi', 'name': '股票监控台',
     'url': '/stocks/', 'icon': {'type': 'builtin', 'value': 'coin'},
     'order': 2, 'visible': True, 'target': '_self', 'builtin_key': 'stocks'},
    # 以下一项由 futureStockTrade 合并迁移 Phase 4 引入（K 线训练 / 复盘对战）
    {'id': 'link-kline', 'type': LINK, 'parent_id': 'group-market-multi', 'name': 'K线训练台',
     'url': '/kline/', 'icon': {'type': 'builtin', 'value': 'analysis'},
     'order': 3, 'visible': True, 'target': '_self', 'builtin_key': 'kline'},
    {'id': 'group-system', 'type': GROUP, 'parent_id': None, 'name': '工具与系统',
     'url': None, 'icon': {'type': 'builtin', 'value': 'system'},
     'order': 6, 'visible': True, 'target': None, 'builtin_key': None},
    {'id': 'link-okx-capability', 'type': LINK, 'parent_id': 'group-system', 'name': 'OKX 能力',
     'url': '/okx-capability', 'icon': {'type': 'builtin', 'value': 'okx'},
     'order': 0, 'visible': True, 'target': '_self', 'builtin_key': 'okx-capability'},
    {'id': 'link-api-console', 'type': LINK, 'parent_id': 'group-system', 'name': 'API 接口',
     'url': '/api-console', 'icon': {'type': 'builtin', 'value': 'api'},
     'order': 1, 'visible': True, 'target': '_self', 'builtin_key': 'api-console'},
    {'id': 'link-system-status', 'type': LINK, 'parent_id': 'group-system', 'name': '系统状态',
     'url': '/system-status', 'icon': {'type': 'builtin', 'value': 'system'},
     'order': 2, 'visible': True, 'target': '_self', 'builtin_key': 'system-status'},
    {'id': 'link-navigation-settings', 'type': LINK, 'parent_id': 'group-system', 'name': '导航配置',
     'url': '/navigation-settings', 'icon': {'type': 'builtin', 'value': 'nav'},
     'order': 3, 'visible': True, 'target': '_self', 'builtin_key': 'navigation-settings'},
]

# 管理页「从内置目录添加」可选池：默认导航未包含、但值得暴露的页面入口。
# 只在用户主动添加时进入导航，升级不会强塞。
CATALOG_PAGES = [
    {'builtin_key': 'monitor-guide', 'name': '监控台操作手册', 'url': '/monitor-guide'},
    {'builtin_key': 'top-coins', 'name': 'Top50币种导览', 'url': '/top-coins'},
    {'builtin_key': 'instinct-wiki', 'name': '盘感 Wiki 管理台', 'url': '/instinct/wiki'},
    # futureStockTrade 合并迁移 Phase 2/3：多市场页面。
    # 已保存过自定义导航的设备（source=db）不会自动出现这些入口，需在
    # 「导航配置 → 从内置目录添加」点一次；全新库（source=default）自带。
    {'builtin_key': 'futures', 'name': '期货监控台', 'url': '/futures/'},
    {'builtin_key': 'futures-star-market', 'name': '期货星标行情', 'url': '/futures/star-market'},
    {'builtin_key': 'stocks', 'name': '股票监控台', 'url': '/stocks/'},
    # futureStockTrade 合并迁移 Phase 4：K 线训练与异步对战榜单。
    {'builtin_key': 'kline', 'name': 'K线训练台', 'url': '/kline/'},
    {'builtin_key': 'kline-pk', 'name': 'K线对战榜单', 'url': '/kline/pk'},
    {'builtin_key': 'kline-blind', 'name': 'K线盲测训练', 'url': '/kline/blind'},
]

# 内置页面 active_page 别名 → 站内路径（当前页高亮兼容用；以真实 URL 为准）。
ACTIVE_PAGE_ALIASES = {
    'dashboard': '/', 'analysis': '/analysis', 'market-scan': '/market-scan',
    'star-market': '/star-market', 'task': '/task', 'alert': '/alert',
    'instinct': '/instinct', 'plan': '/plan', 'journal': '/journal',
    'checkin': '/checkin',
    'calorie': '/calorie', 'expense': '/expense', 'diary': '/diary',
    'okx-capability': '/okx-capability',
    'api-console': '/api-console', 'system-status': '/system-status',
    'navigation-settings': '/navigation-settings', 'monitor-guide': '/monitor-guide',
    'top-coins': '/top-coins', 'indicator-nav': '/indicator-nav',
    'kline-live': '/kline-live',
    # futureStockTrade 合并迁移 Phase 2：期货侧页面（仅作 request.path 缺失时的兜底别名）
    'futures': '/futures/', 'futures-star-market': '/futures/star-market',
    'stocks': '/stocks/',
    # futureStockTrade 合并迁移 Phase 4：K 线训练侧页面
    'kline': '/kline/', 'kline-pk': '/kline/pk', 'kline-blind': '/kline/blind',
    'kline-profile': '/kline/profile', 'kline-auth': '/kline/auth',
}


def default_settings() -> dict:
    return {'desktop_max_visible': DEFAULT_DESKTOP_MAX_VISIBLE}


def build_default_config() -> dict:
    """数据库无记录时返回的内存默认配置（revision=0，不写种子）。"""
    import copy
    return {
        'schema_version': SCHEMA_VERSION,
        'revision': 0,
        'updated_at': None,
        'last_mutation_id': None,
        'settings': default_settings(),
        'items': copy.deepcopy(DEFAULT_ITEMS),
    }


def catalog_payload() -> dict:
    """GET /api/navigation/catalog 的静态内容：内置页面 + 图标键，不探测外部 URL。"""
    return {
        'pages': [dict(p) for p in CATALOG_PAGES],
        'icons': sorted(_ICON_BUILTIN_KEYS),
    }


# =============================================================================
# URL 安全校验
# =============================================================================
_CTRL_RE = re.compile(r'[\x00-\x1f\x7f]')


def _has_credentials(netloc: str) -> bool:
    return '@' in (netloc or '')


def validate_url(raw):
    """校验并规范化一个导航目标 URL，返回规范化字符串；非法抛 NavValidationError。

    规则：接受单斜杠开头站内路径，或绝对 http/https URL。拒绝危险协议、
    协议相对 URL、反斜杠绕过、控制字符、带用户名密码的 URL、动作端点、敏感 query。
    """
    if raw is None:
        raise NavValidationError('链接必须填写 URL')
    if not isinstance(raw, str):
        raise NavValidationError('URL 格式非法')
    url = raw.strip()
    if not url:
        raise NavValidationError('链接必须填写 URL')
    if len(url) > MAX_URL_CHARS:
        raise NavValidationError(f'URL 超长（最多 {MAX_URL_CHARS} 字符）')
    if _CTRL_RE.search(url):
        raise NavValidationError('URL 含非法控制字符')
    # 反斜杠绕过：// 开头是协议相对，/\ 混用也一律拒绝
    if url.startswith('//') or url.startswith('/\\'):
        raise NavValidationError('不支持协议相对或反斜杠 URL')

    if url.startswith('/'):
        # 站内绝对路径
        path = url
        # 禁止 //host 形式（已在上面拦），这里再确保第 2 字符不是斜杠
        if len(path) >= 2 and path[1] == '/':
            raise NavValidationError('不支持协议相对 URL')
        split = urlsplit(path)
        _check_internal_path(split.path)
        _check_query(split.query)
        return _normalize_internal(path)

    # 绝对 URL：仅允许 http/https
    try:
        split = urlsplit(url)
    except ValueError:
        raise NavValidationError('URL 解析失败')
    scheme = (split.scheme or '').lower()
    if scheme not in ('http', 'https'):
        raise NavValidationError('仅允许站内路径或 http/https 地址')
    if not split.netloc or _has_credentials(split.netloc):
        raise NavValidationError('URL 不得包含用户名/密码或缺少主机')
    _check_query(split.query)
    return url


def _check_internal_path(path: str):
    low = (path or '').lower()
    for pref in _BLOCKED_INTERNAL_PREFIXES:
        if low.startswith(pref):
            raise NavValidationError('导航目标不能指向接口或动作端点')
    if low.rstrip('/') in {e.rstrip('/') for e in _BLOCKED_INTERNAL_EXACT}:
        raise NavValidationError('导航目标不能指向动作端点')


def _check_query(query: str):
    if not query:
        return
    for pair in query.split('&'):
        key = pair.split('=', 1)[0].strip().lower()
        if key in _SENSITIVE_QUERY_KEYS:
            raise NavValidationError('URL 不得包含口令/凭据等敏感参数')


def _normalize_internal(path: str) -> str:
    """站内路径去掉尾部多余斜杠（根路径保留 '/'）。"""
    if path == '/':
        return '/'
    stripped = path.rstrip('/')
    return stripped or '/'


# =============================================================================
# 图标校验
# =============================================================================
def validate_icon(icon):
    """图标为 null，或 {type:'builtin',value:key} / {type:'text',value:纯文本}。
    拒绝 HTML/SVG/图片 URL —— 只回传结构，渲染端用 textContent 落地。"""
    if icon is None:
        return None
    if not isinstance(icon, dict):
        raise NavValidationError('图标格式非法')
    itype = icon.get('type')
    if itype not in _ICON_TYPE_KEYS:
        raise NavValidationError('图标类型非法')
    value = icon.get('value')
    if itype is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise NavValidationError('图标值不能为空')
    if itype == 'builtin':
        v = value.strip()
        if v not in _ICON_BUILTIN_KEYS:
            raise NavValidationError(f'未知内置图标键: {v}')
        return {'type': 'builtin', 'value': v}
    # text：限制长度，禁止尖括号（渲染端仍按纯文本处理，双保险）
    v = value.strip()
    if len(v) > MAX_ICON_CHARS:
        raise NavValidationError(f'文本图标过长（最多 {MAX_ICON_CHARS} 字符）')
    if '<' in v or '>' in v:
        raise NavValidationError('文本图标不允许包含标记字符')
    return {'type': 'text', 'value': v}


# =============================================================================
# 节点归一化与结构校验
# =============================================================================
def _clean_name(name):
    if not isinstance(name, str):
        raise NavValidationError('名称必填')
    n = name.strip()
    if not n:
        raise NavValidationError('名称不能为空')
    if len(n) > MAX_NAME_CHARS:
        raise NavValidationError(f'名称过长（最多 {MAX_NAME_CHARS} 字符）')
    return n


def normalize_items(raw_items):
    """校验并规范化 items：返回按（顶层/子分组 order）稳定排列的节点列表。

    - 拒绝：type 非法、ID 重复、name 非法、层级越界（分类嵌套 / 父不是分类）、
      父不存在、链接挂在链接下、visible 非布尔。
    - 允许（仅返回 warning 列表由上层附带）：名称或 URL 重复。
    - 链接必填合法 URL（走安全校验），分类必须 url=None、target=None。
    - order 重排为同级连续 0..n-1（顶层一组，每个分类内各一组）。
    """
    if not isinstance(raw_items, list):
        raise NavValidationError('items 必须是数组')
    if len(raw_items) > MAX_NODES:
        raise NavValidationError(f'节点过多（最多 {MAX_NODES} 个）')

    warnings = []
    seen_ids = set()
    parsed = []
    for idx, it in enumerate(raw_items):
        if not isinstance(it, dict):
            raise NavValidationError(f'第 {idx} 个节点格式非法')
        nid = it.get('id')
        ntype = it.get('type')
        if not isinstance(nid, str) or not nid.strip():
            raise NavValidationError('节点缺少 id')
        nid = nid.strip()
        if nid in seen_ids:
            raise NavValidationError(f'节点 id 重复: {nid}')
        seen_ids.add(nid)
        if ntype not in (LINK, GROUP):
            raise NavValidationError(f'节点 type 非法: {ntype}')

        name = _clean_name(it.get('name'))
        visible = it.get('visible', True)
        if not isinstance(visible, bool):
            raise NavValidationError('visible 必须是布尔值')
        icon = validate_icon(it.get('icon'))
        parent = it.get('parent_id')
        parent = parent.strip() if isinstance(parent, str) and parent.strip() else None

        url = None
        target = None
        if ntype == LINK:
            url = validate_url(it.get('url'))
            tgt = it.get('target') or '_self'
            if tgt not in _ALLOWED_TARGETS:
                raise NavValidationError(f'打开方式非法: {tgt}')
            target = tgt
        else:  # GROUP
            if it.get('url'):
                raise NavValidationError('分类不能设置跳转地址')
            if it.get('target'):
                raise NavValidationError('分类不能有打开方式')
            if parent is not None:
                raise NavValidationError('分类只能位于顶层')

        parsed.append({
            'id': nid, 'type': ntype, 'name': name, 'url': url, 'icon': icon,
            'parent_id': parent, 'visible': visible, 'target': target,
            'builtin_key': (it.get('builtin_key') or None),
            '_raw_order': it.get('order'),
        })

    by_id = {p['id']: p for p in parsed}

    # 父存在性 + 父必须是分类（两层上限）
    for p in parsed:
        if p['type'] == LINK and p['parent_id'] is not None:
            parent_node = by_id.get(p['parent_id'])
            if parent_node is None:
                raise NavValidationError(f'节点 {p["id"]} 的父分类不存在')
            if parent_node['type'] != GROUP:
                raise NavValidationError('链接只能挂在分类下或位于顶层')

    # 重复名称/URL 只警告
    names = {}
    urls = {}
    for p in parsed:
        if p['name'] in names:
            warnings.append(f'名称重复: {p["name"]}')
        names[p['name']] = True
        if p['url']:
            if p['url'] in urls:
                warnings.append(f'地址重复: {p["url"]}')
            urls[p['url']] = True

    ordered = _reorder_and_flatten(parsed)
    # 去内部字段
    for node in ordered:
        node.pop('_raw_order', None)
    return ordered, _dedupe(warnings)


def _reorder_and_flatten(parsed):
    """按同级 order 稳定排序，返回扁平列表（顶层组在前，各分类紧跟其子项）。

    order 缺失/非法时按数组出现顺序兜底，最终规范化为连续 0..n-1。
    """
    top = [p for p in parsed if p['parent_id'] is None]
    children = {}
    for p in parsed:
        if p['parent_id'] is not None:
            children.setdefault(p['parent_id'], []).append(p)

    def sort_key(p):
        o = p.get('_raw_order')
        return o if isinstance(o, int) and o >= 0 else 10 ** 9

    top_sorted = _stable_sort_by_order(top)
    result = []
    for g in top_sorted:
        # 顶层 order 规范化
        result.append(g)
        if g['type'] == GROUP:
            kids = _stable_sort_by_order(children.get(g['id'], []))
            for k in kids:
                result.append(k)

    # 重排 order：顶层 0..n-1，每个分类内 0..m-1
    top_index = 0
    for node in result:
        if node['parent_id'] is None:
            node['order'] = top_index
            top_index += 1
    groups = [n for n in result if n['type'] == GROUP]
    for g in groups:
        kids = [n for n in result if n['parent_id'] == g['id']]
        for i, k in enumerate(kids):
            k['order'] = i
    return result


def _stable_sort_by_order(items):
    decorated = []
    for i, p in enumerate(items):
        o = p.get('_raw_order')
        key = o if isinstance(o, int) and o >= 0 else 10 ** 9
        decorated.append((key, i, p))
    decorated.sort(key=lambda t: (t[0], t[1]))
    return [d[2] for d in decorated]


def _dedupe(seq):
    seen = set()
    out = []
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def validate_and_normalize(payload):
    """校验整份配置外层 + items，返回规范化后的 {settings, items}（不含 revision 等元信息）。"""
    if not isinstance(payload, dict):
        raise NavValidationError('配置必须是对象')
    settings = _validate_settings(payload.get('settings'))
    items, warnings = normalize_items(payload.get('items', []))
    canonical = {'settings': settings, 'items': items}
    _check_size(canonical)
    return canonical, warnings


def _validate_settings(raw):
    if raw is None or not isinstance(raw, dict):
        raw = {}
    out = {}
    dmv = raw.get('desktop_max_visible', DEFAULT_DESKTOP_MAX_VISIBLE)
    if not isinstance(dmv, int) or isinstance(dmv, bool):
        raise NavValidationError('desktop_max_visible 必须是整数')
    if dmv < DESKTOP_MAX_VISIBLE_MIN or dmv > DESKTOP_MAX_VISIBLE_MAX:
        raise NavValidationError(
            f'desktop_max_visible 需在 {DESKTOP_MAX_VISIBLE_MIN}~{DESKTOP_MAX_VISIBLE_MAX} 之间')
    out['desktop_max_visible'] = dmv
    return out


def _check_size(canonical):
    import json
    blob = json.dumps(canonical, ensure_ascii=False).encode('utf-8')
    if len(blob) > MAX_CANONICAL_BYTES:
        raise NavValidationError('配置过大，请精简节点或名称')


# =============================================================================
# 移动语义（一次原子移动）
# =============================================================================
def apply_move(items, item_id, parent_id, before_id):
    """把 item_id 移动到 parent_id（None=顶层）下、before_id 之前（None=末尾）。

    返回新的规范化 items；非法移动抛 NavValidationError。纯函数，不改入参。
    """
    import copy
    nodes = copy.deepcopy(items)
    by_id = {n['id']: n for n in nodes}
    if item_id not in by_id:
        raise NavValidationError('被移动节点不存在')
    moved = by_id[item_id]

    # 规范化入参 parent/before
    target_parent = (parent_id or None)
    if target_parent is not None and isinstance(target_parent, str):
        target_parent = target_parent.strip() or None

    if moved['type'] == GROUP and target_parent is not None:
        raise NavValidationError('分类只能位于顶层')
    if target_parent is not None:
        parent_node = by_id.get(target_parent)
        if parent_node is None or parent_node['type'] != GROUP:
            raise NavValidationError('目标分类不存在或非法')

    if before_id is not None:
        before_id = (before_id or None)
        if isinstance(before_id, str):
            before_id = before_id.strip() or None
    if before_id == item_id:
        raise NavValidationError('不能以自身作为插入锚点')
    if before_id is not None:
        anchor = by_id.get(before_id)
        if anchor is None:
            raise NavValidationError('插入锚点不存在')
        if anchor.get('parent_id') != target_parent:
            raise NavValidationError('锚点与目标分类不同级')
        if anchor['type'] == GROUP and target_parent is None and moved['type'] == LINK:
            # 允许链接插到分类前面（都在顶层混排），无冲突
            pass

    # 从原位置移除
    nodes = [n for n in nodes if n['id'] != item_id]
    moved['parent_id'] = target_parent

    # 计算目标同级序列（不含 moved），把 moved 插到 before 前或末尾
    siblings = [n for n in nodes if n.get('parent_id') == target_parent]
    siblings.sort(key=lambda n: n.get('order', 10 ** 9))
    if before_id is None:
        insert_at = len(siblings)
    else:
        insert_at = next((i for i, n in enumerate(siblings) if n['id'] == before_id), len(siblings))
    siblings.insert(insert_at, moved)

    # 重新赋 order（同级连续），并把 siblings 写回 nodes 顺序
    for i, n in enumerate(siblings):
        n['order'] = i
    # 重组：按（顶层顺序 + 分类内顺序）扁平输出，交给 normalize 兜底规范化
    rebuilt = _rebuild_full_list(nodes, siblings, target_parent)
    normalized, _ = normalize_items(rebuilt)
    return normalized


def _rebuild_full_list(nodes, siblings, target_parent):
    """把调整后的 siblings 顺序合并回完整节点列表（保持其余节点不动）。"""
    sib_ids = {n['id'] for n in siblings}
    others = [n for n in nodes if n['id'] not in sib_ids]
    # others 已含 moved 之外的同组，moved 在 siblings 里；直接拼接后由 normalize 重排
    return others + siblings


def delete_group(items, group_id, delete_children=False):
    """删除分类。默认把子项按原序提升到该分类原顶层位置；
    delete_children=True 时连同子链接一并删除。返回规范化新 items。"""
    import copy
    nodes = copy.deepcopy(items)
    by_id = {n['id']: n for n in nodes}
    grp = by_id.get(group_id)
    if grp is None or grp['type'] != GROUP:
        raise NavValidationError('目标分类不存在')

    grp_order = grp.get('order', 10 ** 9)
    kids = [n for n in nodes if n.get('parent_id') == group_id]
    kids.sort(key=lambda n: n.get('order', 10 ** 9))

    remaining = [n for n in nodes if n['id'] != group_id and n.get('parent_id') != group_id]
    if not delete_children:
        for k in kids:
            k['parent_id'] = None
            k['order'] = grp_order  # 占位，normalize 会按同级重排
        remaining += kids
    normalized, _ = normalize_items(remaining)
    return normalized


# =============================================================================
# 当前页高亮解析（供前端与服务端共用同一口径的辅助）
# =============================================================================
def _node_path(url):
    """取节点 URL 的站内路径（绝对 http(s) 取 path，站内原样）并规范化。"""
    if not url:
        return None
    if url.startswith('http'):
        raw = urlsplit(url).path or '/'
    else:
        raw = url
    return _normalize_internal(raw)


def resolve_active(items, current_path, active_page=None):
    """返回当前应高亮的节点 id 与其 parent_id（无命中时 (None, None)）。

    口径：以真实 URL 路径为准；current_path 缺失时才用 active_page 别名兜底。
    重复 URL 时按展示顺序（同级 order）选第一个，不再凭 builtin_key 误高亮。
    """
    cur = _normalize_internal(current_path) if current_path else None
    if cur is None and active_page:
        cur = _normalize_internal(ACTIVE_PAGE_ALIASES.get(active_page) or '/')
    if cur is None:
        return None, None
    # 按（顶层 order，子项 order）稳定排序后取首个精确路径命中
    ordered = sorted(
        [n for n in items if n.get('type') == LINK],
        key=lambda n: (n.get('parent_id') or '', n.get('order', 10 ** 9)))
    for n in ordered:
        if _node_path(n.get('url')) == cur:
            return n['id'], n.get('parent_id')
    return None, None


def new_custom_id() -> str:
    return 'custom-' + uuid.uuid4().hex[:12]
