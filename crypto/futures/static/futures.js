/**
 * 期货模块前端工具函数
 * ======================
 * 提供期货页面间共享的工具函数。
 * 注意：各页面的核心逻辑已内嵌在对应 HTML 模板中，
 * 此文件用于需要跨页面复用的公共工具。
 */

var FuturesUtils = (function() {
    'use strict';

    /**
     * 格式化金额（保留2位小数）
     */
    function fmtMoney(v) {
        var n = parseFloat(v);
        if (isNaN(n)) return '--';
        return n.toFixed(2);
    }

    /**
     * 格式化百分比
     */
    function fmtPct(v, forceSign) {
        var n = parseFloat(v);
        if (isNaN(n)) return '--';
        var s = n.toFixed(2) + '%';
        return forceSign && n > 0 ? '+' + s : s;
    }

    /**
     * 格式化小数（保留4位）
     */
    function fmtShort(v) {
        var n = parseFloat(v);
        if (isNaN(n)) return '--';
        return n.toFixed(4);
    }

    /**
     * 根据 modify_flag 返回 CSS 颜色类
     */
    function getSignalClass(flag) {
        if (flag === 'rise') return 'signal-buy';
        if (flag === 'fall') return 'signal-sell';
        return 'signal-wait';
    }

    /**
     * 根据 modify_flag 返回中文描述
     */
    function getSignalText(flag) {
        if (flag === 'rise') return '上涨';
        if (flag === 'fall') return '下跌';
        return '观望';
    }

    /**
     * 对正负值返回颜色类
     */
    function pctClass(val) {
        var n = parseFloat(val);
        if (isNaN(n)) return '';
        return n > 0 ? 'val-green' : n < 0 ? 'val-red' : '';
    }

    /**
     * 通用 AJAX GET 请求
     */
    function fetchJSON(url) {
        return fetch(url).then(function(resp) {
            if (!resp.ok) throw new Error('HTTP ' + resp.status);
            return resp.json();
        });
    }

    /**
     * 通用 AJAX POST 请求
     */
    function postJSON(url, data) {
        return fetch(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(data),
        }).then(function(resp) {
            if (!resp.ok) throw new Error('HTTP ' + resp.status);
            return resp.json();
        });
    }

    // 公开接口
    return {
        fmtMoney: fmtMoney,
        fmtPct: fmtPct,
        fmtShort: fmtShort,
        getSignalClass: getSignalClass,
        getSignalText: getSignalText,
        pctClass: pctClass,
        fetchJSON: fetchJSON,
        postJSON: postJSON,
    };
})();
