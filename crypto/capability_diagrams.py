# -*- coding: utf-8 -*-
"""OKX 能力清单页 —— 图示增强层（注入到渲染后的 HTML）

设计原则
--------
- 文档 `doc/OKX交易操作能力清单.md` 是**唯一内容源**，本模块只加"看得懂的图"，
  不复制任何参数/端点文字，避免文档与页面两处维护互相打架。
- 每个条目按"标题纯文本包含 match 字符串"定位，注入到该标题之后。
  因此改动文档里的标题措辞时，只需同步这里的 match。
  可用 `python data/okx_capability_check.py` 校验是否出现 MISS。
- 全部用内联 SVG + viewBox，等比缩放自适应移动端；不引任何外部图表库。
- 所有图示共用 480x152 坐标系：x=38 起为价格轴，横轴基线由 _axis(yb) 给出。
"""

# 写操作动词判定与 data/okx_rw_split.ps1 保持一致（本项目推导口径，非官方标注）
WRITE_VERBS = ('place', 'cancel', 'amend', 'close', 'transfer', 'leverage', 'create',
               'stop', 'batch', 'set', 'trail', 'purchase', 'redeem', 'subscribe',
               'withdraw', 'adjust', 'topup')


def is_write_command(cli_path: str) -> bool:
    """CLI 路径最后一段以写动词开头 => 视为会改变账户/订单/持仓状态。"""
    leaf = (cli_path or '').split()[-1] if cli_path else ''
    return bool(leaf) and any(leaf == w or leaf.startswith(w + '-') for w in WRITE_VERBS)


def _axis(yb):
    """价格轴 + 时间轴两条基线，yb = 横轴所在 y 坐标。"""
    return ('<line x1="38" y1="14" x2="38" y2="{yb}" stroke="#c9d3dd"/>'
            '<line x1="38" y1="{yb}" x2="470" y2="{yb}" stroke="#c9d3dd"/>').format(yb=yb)


def _frame(inner, alt=''):
    """把图元包进 <svg>；alt 里的直双引号必须转义，否则会截断 aria-label。"""
    return ('<figure class="cap-fig"><svg viewBox="0 0 480 152" role="img" aria-label="{alt}">'
            '{inner}</svg></figure>').format(alt=(alt or '示意图').replace('"', '&quot;'),
                                              inner=inner)


SCENARIOS = [
    # ---------------------------------------------------------------- 冰山委托
    {
        'match': '冰山委托',
        'title': '冰山 iceberg：母单总量隐藏，按 szLimit 均匀「露一小块」反复吃',
        'caption': ('场景：想买 10 BTC 但不想让盘口看出自己的量。挂一张冰山单，每次只暴露 '
                    '<code>szLimit</code> 大小的子单，成交掉再自动补一张，直到 <code>sz</code> '
                    '全部吃完或价格触及 <code>pxLimit</code> 上限。对手方看到的始终是小单，'
                    '这是最贴近「均匀梯度下单」的原生能力。'),
        'svg': (
            _axis(126) +
            '<text x="4" y="30" font-size="10" fill="#7a8794">价格</text>'
            '<text x="398" y="146" font-size="10" fill="#7a8794">时间 →</text>'
            '<rect x="40" y="52" width="428" height="44" fill="#eaf3ff" opacity=".7"/>'
            '<text x="46" y="66" font-size="10" fill="#3b7fbf">±pxVar（0.0001~0.01）价格让渡带</text>'
            + ''.join(
                '<rect x="{x}" y="78" width="26" height="22" rx="3" fill="#4a90d9"/>'
                '<text x="{tx}" y="114" font-size="9" fill="#7a8794" text-anchor="middle">{i}</text>'.format(
                    x=58 + k * 46, tx=71 + k * 46, i='子%d' % (k + 1)) for k in range(8)) +
            '<text x="252" y="30" font-size="10" fill="#c0392b">母单 sz = 隐藏总量，盘口只显示当前子单</text>'
        ),
    },
    # -------------------------------------------------------------------- TWAP
    {
        'match': '按时间维度的分批建仓',
        'title': '时间加权 twap：按 timeInterval 切片，每片固定 szLimit，价格越界就暂停',
        'caption': ('场景：某时刻决定「今天买 3000 USDT」，但一次性吃单会推高成本。用 TWAP 把它摊到 '
                    'N 个时间片，每片 <code>szLimit = 3000/N</code>；价格跳出 <code>pxVar</code> '
                    '区间时该片暂停等待而不追价，<code>pxLimit</code> 是绝对价格天花板。'
                    '本项目 <code>demo37</code> 实测：某些环境下 <code>tdMode</code> 需用 isolated 才收单。'),
        'svg': (
            _axis(126) +
            '<text x="4" y="30" font-size="10" fill="#7a8794">价格</text>'
            '<text x="398" y="146" font-size="10" fill="#7a8794">时间 →</text>'
            '<rect x="40" y="50" width="428" height="42" fill="#eaf3ff" opacity=".6"/>'
            + ''.join(
                '<line x1="{x}" y1="40" x2="{x}" y2="126" stroke="#b9c8d8" stroke-dasharray="3,3"/>'
                '<text x="{tx}" y="140" font-size="9" fill="#7a8794" text-anchor="middle">{lab}</text>'.format(
                    x=40 + k * 61, tx=70 + k * 61, lab='t+%d' % (k * 2)) for k in range(7)) +
            '<polyline points="40,96 96,74 152,86 208,60 264,70 320,52 376,66 432,44" '
            'fill="none" stroke="#e67e22" stroke-width="2"/>'
            '<text x="46" y="36" font-size="10" fill="#3b7fbf">每片下单量固定 szLimit；价格出带 → 该片暂停</text>'
        ),
    },
    # ---------------------------------------------------------------- 计划委托
    {
        'match': '策略委托类型',
        'rank': 1,
        'title': '计划委托 trigger：不触及 triggerPx 就永远不进场',
        'caption': ('场景：空仓等突破。价格上穿 <code>triggerPx</code> 才以 <code>orderPx</code> 下单；'
                    '<code>orderPx=-1</code> 为市价追入，填具体价则为限价（可能不成交）。'
                    '要「触发后必成交、剩余量不留」就带 <code>advanceOrdType=fok|iok</code>。'),
        'svg': (
            _axis(128) +
            '<text x="4" y="28" font-size="10" fill="#7a8794">价格</text>'
            '<line x1="40" y1="94" x2="470" y2="94" stroke="#c0392b" stroke-width="1.5" stroke-dasharray="5,4"/>'
            '<text x="42" y="108" font-size="10" fill="#c0392b">triggerPx 触发价</text>'
            '<polyline points="40,120 110,110 180,116 250,98 258,80 330,62 400,48 462,40" '
            'fill="none" stroke="#e67e22" stroke-width="2"/>'
            '<circle cx="254" cy="90" r="5" fill="#27ae60"/>'
            '<text x="262" y="86" font-size="10" fill="#27ae60">触发瞬间下 orderPx 单</text>'
            '<text x="330" y="126" font-size="10" fill="#95a5a6">触发后转为普通订单进主队列</text>'
        ),
    },
    # ---------------------------------------------------------- 移动止盈止损
    {
        'match': '策略委托类型',
        'rank': 2,
        'title': '移动止盈止损 move_order_stop：只朝有利方向推进的「棘轮」',
        'caption': ('场景：浮盈想拿住，又不想坐过山车。价格创新高 → 止损位跟着上移；价格回落 → '
                    '止损位<strong>不动</strong>。回撤达到 <code>callbackRatio</code>（百分比）或 '
                    '<code>callbackSpread</code>（绝对价差）即触发离场，'
                    '<code>activePx</code> 可设「涨到这个价才开始跟踪」。'),
        'svg': (
            _axis(128) +
            '<text x="4" y="28" font-size="10" fill="#7a8794">价格</text>'
            '<polyline points="40,118 90,96 140,104 200,74 250,84 310,52 370,60 430,34" '
            'fill="none" stroke="#e67e22" stroke-width="2"/>'
            '<polyline points="40,124 90,102 140,102 200,80 250,80 310,58 370,58 430,40" '
            'fill="none" stroke="#27ae60" stroke-width="1.6" stroke-dasharray="4,3"/>'
            '<text x="42" y="116" font-size="10" fill="#27ae60">止损位：只随新高上移</text>'
            '<line x1="430" y1="34" x2="430" y2="66" stroke="#c0392b" stroke-width="1"/>'
            '<text x="300" y="28" font-size="10" fill="#c0392b">回撤 callback → 触发</text>'
        ),
    },
    # ---------------------------------------------------------------- 追逐委托
    {
        'match': '策略委托类型',
        'rank': 3,
        'title': '追逐委托 chase：跟着最优价爬，爬够 maxChase 就停手挂住',
        'caption': ('场景：限价单挂不动、想尽快成交。<code>chaseType=distance|ratio</code> 决定按价位差'
                    '还是按比例追对手价。关键是 <code>maxChaseType/maxChaseVal</code> —— '
                    '它给追逐设上限，否则极端行情下会一路追到离谱价位。<strong>必须设上限。</strong>'
                    '注意实际枚举名是 <code>chase</code>，不存在 <code>chase_limit</code>。'),
        'svg': (
            _axis(126) +
            '<text x="4" y="26" font-size="10" fill="#7a8794">价格</text>'
            '<line x1="40" y1="56" x2="470" y2="56" stroke="#8e44ad" stroke-width="1.4" stroke-dasharray="6,4"/>'
            '<text x="42" y="50" font-size="10" fill="#8e44ad">最优卖价（对手价，一路抬）</text>'
            '<polyline points="40,96 96,96 96,86 152,86 152,76 208,76 208,66 264,66 264,96 462,96" '
            'fill="none" stroke="#2980b9" stroke-width="2"/>'
            '<text x="96" y="112" font-size="10" fill="#2980b9">我的委托价：贴上去 → 又落后 → 再贴</text>'
            '<line x1="264" y1="66" x2="264" y2="96" stroke="#c0392b" stroke-width="1" stroke-dasharray="3,2"/>'
            '<text x="272" y="122" font-size="10" fill="#c0392b">到 maxChase → 停止追逐，挂住等成交</text>'
        ),
    },
    # --------------------------------------------------------------------- OCO
    {
        'match': '策略委托类型',
        'rank': 4,
        'title': '双向止盈止损 OCO：两条腿互斥，任一触发即成单、另一条自动撤',
        'caption': ('场景：持仓要同时挂止盈和止损，又不想成交一边后另一边变成裸单。OCO 由交易所侧'
                    '保证互斥，比本地轮询撤单可靠得多。单向 <code>conditional</code> 则是「只挂一边」。'
                    '<code>oco.stopType=1</code> = 到价转市价强平，保证一定出得去。'),
        'svg': (
            _axis(132) +
            '<rect x="196" y="86" width="96" height="26" rx="6" fill="#34495e"/>'
            '<text x="244" y="103" font-size="11" fill="#fff" text-anchor="middle">持仓</text>'
            '<path d="M244,86 C244,58 120,72 108,36" fill="none" stroke="#27ae60" stroke-width="1.8"/>'
            '<path d="M244,112 C244,120 356,110 372,132" fill="none" stroke="#c0392b" stroke-width="1.8"/>'
            '<text x="48" y="28" font-size="10" fill="#27ae60">tpTriggerPx → 止盈平多</text>'
            '<text x="300" y="146" font-size="10" fill="#c0392b">slTriggerPx → 止损平多</text>'
            '<text x="278" y="62" font-size="10" fill="#7f8c8d">互斥：成交一边即撤另一边</text>'
        ),
    },
    # -------------------------------------------------------------- 价格阶梯下单
    {
        'match': '手工阶梯',
        'title': '阶梯下单：一次 batch 提交多档价差，单次上限 20 笔',
        'caption': ('场景：突破后分三档进（越涨越买以确认趋势），或建仓后分档止盈。'
                    '用 <code>spot batch-orders --orders @ladder.json</code> 一次提交。'
                    '两个必踩的坑：① 返回体逐笔 <code>sCode</code>，任一笔非 0 整条命令退出码就是 1，'
                    '脚本判定必须 parse JSON 而非只看 exit code；② 超 20 笔要自己切片。'),
        'svg': (
            _axis(128) +
            '<text x="4" y="26" font-size="10" fill="#7a8794">价格</text>'
            '<text x="398" y="146" font-size="10" fill="#7a8794">时间 →</text>'
            + ''.join(
                '<line x1="40" y1="{y}" x2="{bx}" y2="{y}" stroke="#95a5a6" stroke-width="1" stroke-dasharray="4,3"/>'
                '<rect x="{bx}" y="{ry}" width="58" height="16" rx="3" fill="#4a90d9"/>'
                '<text x="{tx}" y="{ty}" font-size="9" fill="#fff" text-anchor="middle">{lab}</text>'.format(
                    y=y, bx=bx, ry=y - 12, tx=bx + 29, ty=y - 1, lab=lab)
                for y, bx, lab in [(110, 74, '档1 · 1张'), (86, 186, '档2 · 2张'), (62, 300, '档3 · 3张')]) +
            '<text x="42" y="146" font-size="10" fill="#7f8c8d">买1 @P → 买2 @P×1.005 → 买3 @P×1.01（同向同产品族）</text>'
        ),
    },
    # ------------------------------------------------------- DCA 马丁格尔
    {
        'match': '机器人层',
        'rank': 2,
        'title': 'DCA 马丁格尔：越跌越买，间距按 pxStepsMult 拉大、仓位按 volMult 放大',
        'caption': ('场景：现货 DCA 定投想「跌得深就买得多」。<code>pxSteps</code> 是第一道补仓间距，'
                    '<code>pxStepsMult</code> 让后续间距逐级变宽，<code>volMult</code> 让补仓量逐级变大。'
                    '<code>maxSafetyOrds</code> 是补仓次数上限，<strong>填 0 = 完全不补仓</strong>'
                    '（退化成普通定投），别误读成「无限」。合约 DCA 可用 <code>triggerStrategy=price</code>，'
                    '现货没有该档。'),
        'svg': (
            _axis(132) +
            '<text x="4" y="24" font-size="10" fill="#7a8794">价格</text>'
            '<polyline points="40,30 100,54 160,66 226,92 300,106 386,124" '
            'fill="none" stroke="#c0392b" stroke-width="2"/>'
            + ''.join('<rect x="{x}" y="{y}" width="16" height="{h}" rx="2" fill="#27ae60"/>'.format(
                x=x, y=y, h=h) for x, y, h in [(92, 40, 14), (152, 50, 16), (218, 74, 18),
                                                (292, 88, 18), (378, 104, 20)]) +
            '<text x="86" y="34" font-size="9" fill="#27ae60">首仓</text>'
            '<text x="140" y="66" font-size="9" fill="#27ae60">×volMult</text>'
            '<text x="270" y="82" font-size="9" fill="#27ae60">×volMult²</text>'
            '<line x1="100" y1="118" x2="160" y2="118" stroke="#8e44ad" stroke-width="1.4"/>'
            '<text x="102" y="130" font-size="9" fill="#8e44ad">pxSteps</text>'
            '<line x1="226" y1="118" x2="300" y2="118" stroke="#8e44ad" stroke-width="1.4"/>'
            '<text x="228" y="130" font-size="9" fill="#8e44ad">间距 ×pxStepsMult 变宽</text>'
        ),
    },
    # -------------------------------------------------------------- 网格机器人
    {
        'match': '机器人层',
        'rank': 1,
        'title': '网格 Grid：区间内铺 N 条线，跌买涨卖',
        'caption': ('场景：震荡行情吃波动。<code>runType=1</code> 等差（每格价差相同，适合窄区间）'
                    'vs <code>runType=2</code> 等比（每格涨幅相同，适合宽区间）。'
                    '<code>gridNum</code> 越大单格利润越薄，要先扣手续费再定格数。'
                    '它走 <code>/api/v5/tradingBot/grid/*</code>，<strong>不是</strong>策略委托接口，'
                    '所以撤单也不能用普通 <code>cancel-algos</code>。'),
        'svg': (
            _axis(132) +
            '<text x="4" y="24" font-size="10" fill="#7a8794">价格</text>'
            '<rect x="40" y="30" width="428" height="96" fill="#f4f8fb"/>'
            '<line x1="40" y1="30" x2="470" y2="30" stroke="#e67e22" stroke-width="1.4"/>'
            '<text x="466" y="26" font-size="9" fill="#e67e22" text-anchor="end">pxUp 涨破区间</text>'
            '<line x1="40" y1="126" x2="470" y2="126" stroke="#e67e22" stroke-width="1.4"/>'
            '<text x="42" y="140" font-size="9" fill="#e67e22">pxDown 跌破区间</text>'
            + ''.join(
                '<line x1="40" y1="{y}" x2="470" y2="{y}" stroke="#4a90d9" opacity=".7"/>'
                '<text x="466" y="{ty}" font-size="8" fill="#4a90d9" text-anchor="end">{lab}</text>'.format(
                    y=y, ty=y - 2, lab=lab)
                for y, lab in [(46, '卖'), (62, '卖'), (78, '中轴'), (94, '买'), (110, '买')]) +
            '<polyline points="46,78 78,58 110,98 142,62 174,102 206,70 238,94 270,66 302,98 334,74 366,90 398,68 430,86 462,72" '
            'fill="none" stroke="#2c3e50" stroke-width="1.6"/>'
        ),
    },
    # ------------------------------------------------------ 防未来函数校验
    {
        'match': '前置查询能力',
        'title': '--backtest-time：同一时间戳，两套算法互相校验',
        'caption': ('场景：回测里怀疑本地 MACD/ADX 算错或用了未收盘数据。用 '
                    '<code>okx market indicator --indicatorType MACD,BB,ADX --bar 1H '
                    '--backtest-time &lt;历史K线收盘毫秒&gt;</code> 取「当时交易所可见」的指标值，'
                    '与本地逐根比对 —— 若本地值混进了当根未收盘数据，两者必然不一致，直接暴露未来函数。'
                    '<strong>不支持 1m</strong>，且是单点取值，不能整段拉。'),
        'svg': (
            _axis(126) +
            '<text x="4" y="26" font-size="10" fill="#7a8794">指标</text>'
            '<polyline points="40,104 96,88 152,96 208,70 264,78 320,52 376,64 432,42" '
            'fill="none" stroke="#27ae60" stroke-width="2"/>'
            '<polyline points="40,100 96,92 152,88 208,78 264,72 320,60 376,58 432,46" '
            'fill="none" stroke="#c0392b" stroke-width="1.6" stroke-dasharray="5,3"/>'
            '<text x="42" y="118" font-size="9" fill="#27ae60">本地 talib：只用已收盘数据</text>'
            '<text x="300" y="36" font-size="9" fill="#c0392b">服务端 indicator @ backtest-time</text>'
            '<line x1="264" y1="46" x2="264" y2="122" stroke="#8e44ad" stroke-width="1" stroke-dasharray="2,2"/>'
            '<text x="268" y="136" font-size="9" fill="#8e44ad">同一根 K 线收盘时刻</text>'
        ),
    },
]


def scenario_html(entry):
    """图示条目 → 一段可插入文档流的 HTML。"""
    return ('<div class="cap-scenario">'
            '<div class="cap-scenario-hd">🖼️ {title}</div>'
            '{svg}'
            '<div class="cap-scenario-cap">{caption}</div>'
            '</div>').format(title=entry['title'],
                             svg=_frame(entry['svg'], alt=entry['title']),
                             caption=entry['caption'])
