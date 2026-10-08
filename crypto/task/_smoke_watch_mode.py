# -*- coding: utf-8 -*-
"""观察模式离线回归：python -B crypto/task/_smoke_watch_mode.py。
执行真实函数体，交易所、邮件、持久化均用内存替身，并禁止 socket 联网。
"""
import ast
import copy
import datetime
import socket
import sys
import threading
import time
import types
import unittest
from unittest.mock import Mock, patch

from _smoke_manual_override_regression import (
    HERE, INST, FakeExecutor, MemoryRepo, load_definitions, memory_session,
)
from utils.watch_mode import WATCH_MODE_NOTICE, watch_mode, watch_mode_active


class WatchRegression(unittest.TestCase):
    def setUp(self):
        network = patch.object(socket.socket, 'connect', side_effect=AssertionError('禁止联网'))
        network.start()
        self.addCleanup(network.stop)
        self.repo = MemoryRepo()
        manager = load_definitions(HERE / 'utils/position_order_manager.py',
                                   session_scope=memory_session, state_repo=self.repo)
        self.ex = FakeExecutor()
        self.m = manager.DualPositionOrderManager(self.ex, account='watch-test', environment='1')
        cls = load_definitions(HERE / 'trend_range_trader.py',
                               DualPositionOrderManager=manager.DualPositionOrderManager,
                               generate_run_id=lambda: 'WATCH-TEST').TrendRangeTrader
        self.t = cls.__new__(cls)
        self.t.pos_mgr = self.m
        self.t.trade_executor = self.ex
        self.cfg = {'currencies': [{'instId': INST, 'trade_enabled': False}],
                    'global_settings': {'reverse_position_guard': {'enabled': True, 'grace_minutes': 1},
                                        'consecutive_failure_alert_rounds': 1}}
        self.t._load_config = lambda: self.cfg
        for attr in ('_manual_pause', 'manual_override_cache', 'last_directions', '_reverse_guard',
                     '_watch_edges', '_watch_rev_notified', '_watch_single_prev', '_spike_prev',
                     '_spike_until', '_tp_close_until', '_place_fail_streak', '_consec_fail',
                     'manual_direction_config'):
            setattr(self.t, attr, {})
        for attr in ('_watch_active', '_pause_logged', '_spike_logged', '_tp_cd_logged'):
            setattr(self.t, attr, set())
        self.t._trade_lock = threading.RLock()
        self.t.stop_event = threading.Event()
        self.t._run_count = 0
        self.t._resolve_size = lambda *a: 1.0
        self.t._steps = lambda *a: (.01, .01)
        self.t._handle_ledger_divergence = lambda *a: None
        self.t._save_directions = lambda: None
        self.t._save_reverse_guard = Mock()
        self.t._close_reverse_conflict = Mock(side_effect=AssertionError('观察模式不得强平'))
        self.t._close_bucket = Mock(side_effect=AssertionError('观察模式不得平仓'))
        self.t._close_bucket_smart = Mock(side_effect=AssertionError('观察模式不得反转清仓'))
        self.t._sync_trend_algo = Mock(side_effect=AssertionError('观察模式不得改兜底单'))
        self.t.tp_engine = types.SimpleNamespace(
            clear_state=Mock(side_effect=AssertionError('观察模式不得清状态')),
            evaluate=lambda cfg, ctx: {'action': 'none'})
        self.t.message_notifier = Mock()
        self.analysis = dict(direction='long', short_prev_direction='long',
                             short_prev_prev_direction='short', long_direction='long',
                             long_prev_direction='long', last_price=90,
                             boll_lower=91, boll_upper=110, reversal_close=90)
        self.t._get_strategy_adapter = lambda: types.SimpleNamespace(analyze=lambda *a, **k: self.analysis)

    def seed(self, direction='long', pending=True):
        book = self.m._inst(INST)['trend']
        book['held'][direction] = 1.0
        book['avg_px'][direction] = 100.0
        self.ex.positions[direction] = 1.0
        if pending:
            book['slots']['entry'] = dict(self.m._new_slot(), state='PENDING', ord_id='O1',
                                          amount=.3, dir='long', price=85, placed_ts=time.time() - 10000)
            self.ex.add('O1', .3)
        self.m._save(INST)

    def cycle(self, **kwargs):
        return self.t.analyze_and_trade_real(INST, '1m', '15m',
                                             trend_cfg={'stop_loss': {'enabled': True}},
                                             range_cfg={}, trade_enabled=False, **kwargs)

    def assert_no_writes(self):
        self.assertEqual(self.ex.calls, [])
        self.assertEqual(self.ex.cancels, [])
        self.t._close_reverse_conflict.assert_not_called()
        self.t._close_bucket.assert_not_called()
        self.t._close_bucket_smart.assert_not_called()
        self.t._sync_trend_algo.assert_not_called()
        self.assertFalse(watch_mode_active())

    def test_first_cycle_sl_and_pending_order_are_read_only(self):
        self.seed()
        self.t._eval_sl = lambda *a: (True, '触发止损')
        before = copy.deepcopy(self.ex.orders)
        result = self.cycle()
        self.assertTrue(result['success'], result)
        self.assertTrue(result['watch_mode'])
        self.assertTrue(result['tp_sl_triggered'])
        self.assertFalse(result['trade_executed'])
        self.assertEqual(self.ex.orders, before)
        self.assert_no_writes()
        self.assertTrue(self.t.message_notifier.send_watch_mode_alert.called)

    def test_reversal_and_night_window_do_not_close(self):
        self.seed('short', pending=False)
        self.t.last_directions[INST] = {'long': 'short'}
        self.t._in_night_window = lambda *a: True
        result = self.cycle()
        self.assertTrue(result['success'], result)
        self.assertEqual(self.t.last_directions[INST]['long'], 'short')
        self.assert_no_writes()

    def test_reverse_first_warning_and_expired_timer_never_close(self):
        self.seed('short', pending=False)
        for timer in ({}, {INST: {'detected_ts': time.time() - 3600}}):
            for reversed_now in (False, True):
                with self.subTest(timer=bool(timer), reversed_now=reversed_now):
                    self.t._reverse_guard = copy.deepcopy(timer)
                    result = self.t._check_reverse_position_guard(
                        INST, 'long', 0, -1, '1m', '15m', 100, reversed_now, observe_only=True)
                    self.assertFalse(result['closed'])
                    self.assertIn(WATCH_MODE_NOTICE, result['text'])
                    self.assertEqual(self.t._reverse_guard, timer)
                    self.assert_no_writes()
        self.t._save_reverse_guard.assert_not_called()
        warning = self.t.message_notifier.send_reverse_position_warning.call_args
        self.assertEqual(warning.args, (INST, 'long', 'short', 1.0, 100, 1.0, 0, -1))
        self.assertEqual(warning.kwargs, {'source_note': '程序旧方向持仓(趋势仓)', 'observe_only': True})
        self.t.message_notifier.send_reverse_position_closed.assert_not_called()

    def test_batch_sync_and_analysis_failure_leave_old_order_untouched(self):
        self.seed()
        self.analysis = None
        self.t.run_real_trading_batch()
        self.assertEqual(self.t._run_count, 1)
        self.assertEqual(self.ex.orders['O1']['state'], 'live')
        self.assertTrue(self.t.message_notifier.send_system_alert.called)
        self.assert_no_writes()

    def test_existing_fill_cycle_keeps_original_detailed_notification(self):
        self.seed()
        self.ex.orders['O1'].update(state='filled', accFillSz='.3', avgPx='85')
        self.ex.positions['long'] = 1.3
        result = self.cycle()
        self.assertTrue(result['success'], result)
        mail = self.t.message_notifier.send_trade_operation_email.call_args
        self.assertIsNotNone(mail)
        self.assertEqual(mail.kwargs['symbol'], INST)
        self.assertEqual(mail.kwargs['short_period'], '1m')
        self.assertEqual(mail.kwargs['long_period'], '15m')
        self.assertEqual(mail.kwargs['trade_direction'], '开多(趋势跟踪限价成交)')
        self.assertEqual(mail.kwargs['price'], 85)
        self.assert_no_writes()

    def test_spike_watch_mail_preserves_comparison_and_threshold(self):
        self.t._spike_prev[INST] = {'price': 100, 'ts': time.time() - 30}
        self.t._spike_guard_cfg = lambda: {'spike_enabled': True, 'spike_change_pct': 5.0,
                                          'spike_pause_minutes': 5}
        self.t._classify_spike = lambda *args: (True, -8.0)
        with watch_mode():
            self.t._note_spike_guard(INST, 92)
        mail = self.t.message_notifier.send_watch_mode_alert.call_args
        self.assertIsNotNone(mail)
        rows = dict(mail.kwargs['detail_rows'])
        self.assertEqual(rows['上一轮现价'], '100')
        self.assertEqual(rows['本轮现价'], '92')
        self.assertEqual(rows['判定阈值'], '5.0%')
        self.assertEqual(rows['暂停时长（实盘配置）'], '5 分钟')
        self.assertEqual(self.t._spike_until, {})
        self.assert_no_writes()

    def test_batch_mixed_modes_only_live_coin_allows_cancel(self):
        self.cfg['currencies'].append({'instId': 'LIVE', 'trade_enabled': True})
        seen = []
        self.t._read_positions_by_mode = lambda inst, cancel_entries=True: (
            seen.append((inst, cancel_entries)) or {'success': True})
        self.t.analyze_and_trade_real = lambda **kw: {'success': True}
        self.t.run_real_trading_batch()
        self.assertIn((INST, False), seen)
        self.assertIn(('LIVE', True), seen)

    def test_live_reverse_guard_still_warns_then_closes(self):
        self.cfg['currencies'][0]['trade_enabled'] = True
        self.seed('short', pending=False)
        self.t._close_reverse_conflict = Mock(return_value=True)
        first = self.t._check_reverse_position_guard(INST, 'long', 0, -1, '1m', '15m', 100, False)
        self.assertFalse(first['closed'])
        self.t.message_notifier.send_reverse_position_warning.assert_called_once()
        self.t._reverse_guard[INST]['detected_ts'] = time.time() - 3600
        later = self.t._check_reverse_position_guard(INST, 'long', 0, -1, '1m', '15m', 100, False)
        self.assertTrue(later['closed'])
        self.t._close_reverse_conflict.assert_called_once()

    def test_watch_configuration_overrides_stale_true_argument(self):
        self.analysis = None
        result = self.t.analyze_and_trade_real(INST, '1m', '15m', trade_enabled=True)
        self.assertEqual(result['error'], '双周期趋势分析失败')
        self.assert_no_writes()

    def test_manual_force_close_is_blocked_before_state_changes(self):
        self.seed()
        before = copy.deepcopy(self.m.state)
        result = self.t.force_close_manual(INST)
        self.assertEqual(result, [WATCH_MODE_NOTICE])
        self.assertEqual(self.m.state, before)
        self.assert_no_writes()

    def test_tp_all_and_partial_do_not_commit_or_clear(self):
        self.seed(pending=False)
        self.t._eval_sl = lambda *a: (False, '')
        for action in ('close_all', 'close_partial'):
            def evaluate(cfg, ctx):
                self.assertTrue(ctx['observe_only'])
                return {'action': action, 'reason': '测试止盈', 'close_ratio': .5}
            self.t.tp_engine.evaluate = evaluate
            result = self.cycle()
            self.assertTrue(result['success'], result)
        self.assert_no_writes()
        self.t.tp_engine.clear_state.assert_not_called()


class BoundaryRegression(unittest.TestCase):
    def setUp(self):
        network = patch.object(socket.socket, 'connect', side_effect=AssertionError('禁止联网'))
        network.start()
        self.addCleanup(network.stop)
        cls = load_definitions(HERE / 'utils/trade_executor.py', names={'TradeExecutor'}).TradeExecutor
        self.ex = cls.__new__(cls)
        self.sdk = Mock(return_value={'code': '0', 'data': [{'sCode': '0', 'ordId': 'OK'}]})
        self.ex.trade_api = types.SimpleNamespace(**{name: self.sdk for name in
            ('place_order', 'place_algo_order', 'cancel_order', 'cancel_algo_order', 'amend_order', 'close_positions')})
        self.ex.account_api = types.SimpleNamespace(set_leverage=self.sdk)
        self.ex.get_last_price = lambda inst: 100

    def test_sdk_write_sites_cannot_bypass_gate(self):
        tree = ast.parse((HERE / 'utils/trade_executor.py').read_text(encoding='utf-8'))
        direct = []
        for n in ast.walk(tree):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute):
                owner = n.func.value
                if isinstance(owner, ast.Attribute) and owner.attr in ('trade_api', 'account_api'):
                    if n.func.attr.startswith(('place', 'cancel', 'amend', 'set_', 'close')):
                        direct.append(n.func.attr)
        self.assertEqual(direct, [])

    def test_buy_sell_cancel_amend_leverage_and_close_are_blocked(self):
        with watch_mode():
            for side in ('buy', 'sell'):
                result = self.ex.execute_trade(INST, side, 1, 100, cl_ord_id='stable')
                self.assertFalse(result['success'])
                self.assertTrue(result['rejected'])
            self.assertFalse(self.ex.cancel_normal_order(INST, 'O1'))
            self.assertFalse(self.ex.cancel_algo_order('A1', INST))
            self.assertFalse(self.ex.amend_order(INST, 'O1', new_price=99))
            self.assertFalse(self.ex.set_leverage(INST, 10))
            self.assertFalse(self.ex.close_position(INST))
            self.assertEqual(self.ex._write(self.sdk, instId=INST)['code'], 'WATCH_MODE')
        self.sdk.assert_not_called()
        self.assertTrue(self.ex.execute_trade(INST, 'buy', 1, 100, cl_ord_id='live')['success'])
        self.sdk.assert_called_once()

    def test_hot_switch_and_mixed_batch_write_block(self):
        self.ex.watch_mode_check = lambda inst: inst == INST
        result = self.ex._write(self.sdk, [{'instId': 'LIVE'}, {'instId': INST}])
        self.assertEqual(result['code'], 'WATCH_MODE')
        self.sdk.assert_not_called()
        self.ex.watch_mode_check = Mock(side_effect=RuntimeError('模式不可读'))
        self.assertEqual(self.ex._write(self.sdk, instId=INST)['code'], 'WATCH_MODE')
        self.sdk.assert_not_called()

    def test_context_nesting_exception_and_thread_isolation(self):
        with self.assertRaises(RuntimeError):
            with watch_mode():
                with watch_mode(False):
                    self.assertTrue(watch_mode_active())
                    values = []
                    thread = threading.Thread(target=lambda: values.append(watch_mode_active()))
                    thread.start()
                    thread.join()
                    self.assertEqual(values, [False])
                    raise RuntimeError('模拟异常')
        self.assertFalse(watch_mode_active())


class MailRegression(unittest.TestCase):
    def setUp(self):
        network = patch.object(socket.socket, 'connect', side_effect=AssertionError('禁止联网'))
        network.start()
        self.addCleanup(network.stop)
        config = types.ModuleType('config.email_config')
        config.get_admin_email = lambda: 'watch@example.invalid'
        stub = patch.dict(sys.modules, {'config.email_config': config})
        stub.start()
        self.addCleanup(stub.stop)
        templates = load_definitions(HERE / 'notification/email_tool.py', names={'EmailTemplates'},
                                     datetime=datetime.datetime).EmailTemplates
        cls = load_definitions(HERE / 'notification/message_notifier.py', names={'MessageNotifier'},
                               EmailTemplates=templates).MessageNotifier
        self.n = cls.__new__(cls)
        self.n.email_cooldown_minutes = 0
        self.n.last_sent_times = {}
        self.n._email_dedup_window_seconds = 300
        self.n._recent_email_fingerprints = {}
        self.sent = []
        self.n.email_tool = types.SimpleNamespace(send_html_email=lambda **kw: self.sent.append(kw) or True)

    def assert_notice(self):
        self.assertTrue(self.sent)
        for mail in self.sent:
            self.assertIn('观察者模式', mail['subject'])
            self.assertTrue(mail['subject'].endswith('）'))
            self.assertIn('观察者模式', mail['html_content'])
            self.assertNotIn(WATCH_MODE_NOTICE, mail['subject'])
            self.assertNotIn(WATCH_MODE_NOTICE, mail['html_content'])
            self.assertNotIn('【观察模式】', mail['subject'])

    def test_all_watch_alert_templates_and_reverse_warning(self):
        with watch_mode():
            self.n.send_system_alert(INST, '异常', '测试')
            self.n.send_risk_alert(INST, '风险')
            self.n.send_price_volatility_alert(INST, -5, 15)
            self.n.send_position_pnl_alert(INST, 'trend', 'long', -20, 100, 80)
            self.n.send_liq_distance_alert(INST, 1, 80, 81, 'long')
            self.n.send_profit_alert(INST, 'long', 10, 100, 110)
            self.n.send_reverse_position_warning(INST, 'long', 'short', 1, 100, 10)
            self.n.send_alert_digest([{'inst_id': INST, 'type': 'price', 'value': -5}])
        self.assertEqual(len(self.sent), 8)
        self.assert_notice()
        self.assertNotIn('分钟后将强平', self.sent[-2]['subject'])
        self.assertNotIn('将强制平仓', self.sent[-2]['html_content'])

    def test_explicit_watch_mail_and_live_mail_do_not_leak(self):
        self.n.send_watch_mode_alert(INST, 'test', '提醒')
        self.assert_notice()
        self.n.send_system_alert('LIVE', '恢复', '正常')
        self.assertNotIn(WATCH_MODE_NOTICE, self.sent[-1]['subject'])
        self.assertNotIn(WATCH_MODE_NOTICE, self.sent[-1]['html_content'])

    def test_mixed_digest_and_outside_cycle_failure_mail(self):
        self.n.watch_mode_check = lambda inst: inst == INST
        self.n.send_system_alert(INST, '连续失败', '取数失败')
        self.n.send_alert_digest([{'inst_id': INST}, {'inst_id': 'LIVE'}])
        self.assert_notice()
        for field in ('subject', 'html_content'):
            self.assertIn(f'仅 {INST}', self.sent[-1][field])
            self.assertIn('其他币种按各自配置运行', self.sent[-1][field])
        self.n.send_system_alert('LIVE', '失败', '取数失败')
        self.assertNotIn(WATCH_MODE_NOTICE, self.sent[-1]['subject'])

    def test_task_template_discloses_read_only_and_existing_order_risk(self):
        from jinja2 import Environment
        template = (HERE.parent / 'templates/task.html').read_text(encoding='utf-8')
        Environment().parse(template)
        self.assertIn('仅在标题末尾及正文页脚加“（观察者模式）”', template)
        self.assertIn('进入首轮也不撤单', template)
        self.assertNotIn('开启首轮会撤销该币种全部未成交挂单', template)

    def test_failed_delivery_keeps_notice_in_dead_letter(self):
        self.n.email_tool.send_html_email = Mock(return_value=False)
        self.n._dead_letter_store = Mock()
        with watch_mode():
            self.assertFalse(self.n._dispatch_email(['watch@example.invalid'], '失败预警',
                                                    '<body>风险</body>', max_retries=1))
        args = self.n._dead_letter_store.call_args.args
        self.assertEqual(args[1], '失败预警（观察者模式）')
        self.assertEqual(args[2], '<body>风险<p style="color:#999;font-size:12px">（观察者模式）</p></body>')

    def test_dispatch_preserves_subject_and_entire_body_with_suffix_only(self):
        subject = f'行情异动 - {INST} 急跌 -6.25% 现价 87.5'
        body = ('<html><body><h2>行情异动</h2><p>方向：急跌；周期：15m</p>'
                '<table><tr><td>价格</td><td>87.5</td></tr></table>'
                '<div class="footer"><p>系统自动发送，请勿回复</p></div></body></html>')
        self.n._dispatch_email(['watch@example.invalid'], subject, body, observe_only=True)
        mail = self.sent[-1]
        self.assertEqual(mail['subject'], subject + '（观察者模式）')
        self.assertEqual(mail['html_content'], body.replace(
            '系统自动发送，请勿回复</p>', '系统自动发送，请勿回复（观察者模式）</p>'))
        self.n._dispatch_email(['watch@example.invalid'], mail['subject'],
                               mail['html_content'], observe_only=True)
        self.assertEqual(len(self.sent), 1)  # 备注不重复，内容指纹也不改变。
        self.n._dispatch_email(['watch@example.invalid'], subject, body)
        self.assertEqual(self.sent[-1]['subject'], subject)
        self.assertEqual(self.sent[-1]['html_content'], body)

    def test_fragment_and_custom_body_put_note_after_information(self):
        for body in ('<div>价格87.5、止损85、周期15m</div>',
                     '<HTML><BODY class="mail">持仓1.5张，均价100</BODY></HTML>'):
            self.n._dispatch_email(['watch@example.invalid'], '风险明细', body, observe_only=True)
            html = self.sent[-1]['html_content']
            note = '<p style="color:#999;font-size:12px">（观察者模式）</p>'
            self.assertEqual(html.replace(note, ''), body)
            self.assertGreater(html.index(note), html.index('15m') if '15m' in body else html.index('均价100'))
            if '</BODY>' in html:
                self.assertLess(html.index(note), html.index('</BODY>'))

    def test_watch_event_keeps_title_rows_and_hint(self):
        self.n.send_watch_mode_alert(INST, 'entry', '趋势仓开多',
                                     [('交易周期', '1m / 15m'), ('参考价', '98.25'),
                                      ('计划数量', '2.5张')], hint='等待信号确认')
        mail = self.sent[-1]
        self.assertEqual(mail['subject'], f'趋势仓开多 - {INST}（观察者模式）')
        for text in (f'<h2>趋势仓开多 - {INST}</h2>', '1m / 15m', '98.25', '2.5张', '等待信号确认'):
            self.assertIn(text, mail['html_content'])
            self.assertLess(mail['html_content'].index(text), mail['html_content'].index('（观察者模式）'))
        self.assertNotIn('【观察模式】', mail['html_content'])
        self.assertEqual(mail['html_content'].count('观察者模式'), 1)

    def test_reverse_warning_retains_all_original_fields_in_both_modes(self):
        for watched in (True, False):
            self.n.watch_mode_check = lambda inst: watched and inst == INST
            self.n.send_reverse_position_warning(INST, 'long', 'short', 2.5, 98.75, 10,
                                                 cross_pos=3.0, isolated_pos=-2.5,
                                                 source_note='程序旧方向2张+账本外人工0.5张')
            mail = self.sent[-1]
            for text in ('合约', INST, '长周期方向', '看多 (LONG)', '反向持仓',
                         '空头 (SHORT) 2.5 张', '冲突来源', '程序旧方向2张+账本外人工0.5张',
                         '全仓/逐仓持仓', '全仓 3.0 张 / 逐仓 -2.5 张', '当前价格', '98.7500', '检测时间'):
                self.assertIn(text, mail['html_content'])
            if watched:
                self.assertTrue(mail['subject'].endswith(f'{INST}（观察者模式）'))
                self.assertNotIn('分钟后', mail['subject'] + mail['html_content'])
            else:
                self.assertTrue(mail['subject'].endswith(f'{INST}（10分钟后将强平）'))
                self.assertIn('10 分钟后系统将执行强制平仓', mail['html_content'])
                self.assertNotIn('观察者模式', mail['subject'] + mail['html_content'])

    def test_existing_fill_mail_keeps_period_direction_price_and_time(self):
        with watch_mode():
            self.n.send_trade_operation_email(INST, '1m', '15m', '开多(趋势跟踪限价成交)',
                                              '2026-09-25 12:34:56', 98.75, True)
        mail = self.sent[-1]
        self.assertTrue(mail['subject'].endswith(f'{INST} 开多(趋势跟踪限价成交)（观察者模式）'))
        for text in ('短周期 1m / 长周期 15m', '开多(趋势跟踪限价成交)',
                     '2026-09-25 12:34:56', '98.7500', '成交来自既有挂单或交易所侧委托'):
            self.assertIn(text, mail['html_content'])

    def test_monitor_notifier_uses_selected_account(self):
        repo = types.SimpleNamespace(load_strategy_config_cached=Mock(return_value={
            'currencies': [{'instId': INST, 'trade_enabled': False},
                           {'instId': 'LIVE', 'trade_enabled': True}]}))
        monitor = load_definitions(HERE / 'monitor/alert_monitor.py', names={'_new_notifier'},
                                   MessageNotifier=lambda: self.n, config_store_repo=repo,
                                   load_alert_config=lambda: {'account': 'watch-test'})
        notifier = monitor._new_notifier()
        repo.load_strategy_config_cached.assert_called_once_with('watch-test')
        self.assertTrue(notifier.watch_mode_check(INST))
        self.assertFalse(notifier.watch_mode_check('LIVE'))
        self.assertFalse(notifier.watch_mode_check(None))


if __name__ == '__main__':
    unittest.main(verbosity=2)
