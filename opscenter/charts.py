#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SVG 图表辅助：把数据序列换算成折线/面积坐标（服务端算，前端零依赖绘制）。"""


def line_chart(values, width=800, height=260, pad_l=8, pad_r=8, pad_t=16, pad_b=28,
               threshold=None, threshold_label='', thresholds=None):
    """返回 dict：含 polyline 点串、面积 path、阈值线 y、坐标标签所需信息。

    values: [float] 旧→新（None 会被跳过）。为空则返回 available=False。
    threshold / threshold_label：单阈值（向后兼容，画一条 danger 线）。
    thresholds: [{'value','label','color'}] 多阈值；给出时优先使用。
    """
    vals = [v for v in values if isinstance(v, (int, float))]
    if len(vals) < 2:
        return {'available': False}

    # 归一化阈值列表（兼容旧的单 threshold 参数）
    ths = list(thresholds) if thresholds else []
    if not ths and threshold is not None:
        ths = [{'value': threshold, 'label': threshold_label, 'color': 'danger'}]

    lo, hi = min(vals), max(vals)
    for t in ths:
        if isinstance(t.get('value'), (int, float)):
            hi = max(hi, t['value'])
    span = (hi - lo) or 1
    # 上下各留 8% 视觉余量
    hi_p = hi + span * 0.08
    lo_p = max(0, lo - span * 0.08)
    range_p = (hi_p - lo_p) or 1

    inner_w = width - pad_l - pad_r
    inner_h = height - pad_t - pad_b
    n = len(vals)

    def X(i):
        return pad_l + inner_w * (i / (n - 1))

    def Y(v):
        return pad_t + inner_h * (1 - (v - lo_p) / range_p)

    pts = ['%.1f,%.1f' % (X(i), Y(v)) for i, v in enumerate(vals)]
    line_points = ' '.join(pts)
    first_x, last_x = X(0), X(n - 1)
    baseline = pad_t + inner_h
    area_path = 'M%.1f,%.1f L%s L%.1f,%.1f L%.1f,%.1f Z' % (
        first_x, Y(vals[0]),
        ' L'.join(pts[1:]),
        last_x, baseline, first_x, baseline)

    th_lines = [
        {'y': round(Y(t['value']), 1), 'label': t.get('label', ''),
         'color': t.get('color', 'danger'), 'value': t['value']}
        for t in ths if isinstance(t.get('value'), (int, float)) and Y(t['value']) >= 0
    ]

    return {
        'available': True,
        'viewbox': '0 0 %d %d' % (width, height),
        'line_points': line_points,
        'area_path': area_path,
        'last_x': round(last_x, 1), 'last_y': round(Y(vals[-1]), 1),
        'min': round(lo, 1), 'max': round(hi, 1),
        'th_lines': th_lines,
        # ---- 向后兼容字段（health/dashboard 仍用单阈值） ----
        'threshold_y': th_lines[0]['y'] if th_lines else None,
        'threshold_label': th_lines[0]['label'] if th_lines else threshold_label,
        'baseline': round(baseline, 1),
        'pad_l': pad_l, 'pad_r': pad_r,
    }
