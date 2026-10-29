"""Exercise UI callbacks against the real ledger with an offline browser double."""
from concurrent.futures import Future
from datetime import datetime
from decimal import Decimal
from pathlib import Path
import tempfile
import time
import threading
import tkinter as tk
import unittest
from unittest.mock import patch

from quota_guard.core import DataStore, Person, TZ, GuardError
from quota_guard.ui import App
from quota_guard.browser_service import check_identity, key_fingerprint


class OfflineBrowser:
    def __init__(self, results):
        self.results = results
        self.calls = []

    def submit(self, action, **kwargs):
        self.calls.append((action, kwargs))
        future = Future()
        future.set_result(self.results[action])
        return future

    def close(self):
        pass


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.start = datetime.now(TZ).date().isoformat()
        self.store = DataStore(self.folder)
        self.store.initialize(14, '1500', self.start)
        self.store.save_people([Person('测试甲', Decimal(0)), Person('测试乙', Decimal(80))])
        self.keys = [{'name': '测试甲', 'tracking_id': 'test-key-a'},
                     {'name': '测试乙', 'tracking_id': 'test-key-b'}]
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = App(self.root, self.folder)
        self.patches = [patch('quota_guard.ui.messagebox.showinfo'),
                        patch('quota_guard.ui.messagebox.showerror'),
                        patch('quota_guard.ui.messagebox.askyesno', return_value=True)]
        self.mocks = [p.start() for p in self.patches]
        self.pump()

    def tearDown(self):
        self.app.stop(quiet=True)
        self.app.folder_lock.close()
        self.root.destroy()
        for p in self.patches:
            p.stop()
        self.temp.cleanup()

    def pump(self):
        end = time.monotonic() + .2
        while time.monotonic() < end:
            self.root.update()
            time.sleep(.01)

    def test_empty_then_revoke_result_is_logged_and_restorable(self):
        browser = OfflineBrowser({
            'collect': {'keys': self.keys, 'account_id': key_fingerprint(self.keys),
                        'totals': None, 'message': '没有费用记录'},
            'prepare_revoke': {'message': '已定位'},
            'verify_revoke': {'absent': True},
        })
        self.app.browser = browser
        self.app.collect()
        self.pump()
        self.assertEqual(self.app.rows[0]['status'], 'triggered')
        self.assertIsNone(self.app.rows[1]['spent'])
        self.app.tree.selection_set('0')
        self.app.prepare_revoke()
        self.pump()
        self.app.verify_revoke()
        self.pump()
        states = [e['status'] for e in self.store.events() if e['kind'] == 'action']
        self.assertEqual(states, ['absent_verified', 'prepared', 'pending'])
        self.assertEqual(self.app.rows[0]['status'], 'retired')
        self.assertEqual(self.store.active_tracking_ids(), {'test-key-b'})
        self.mocks[1].assert_not_called()

    def test_changed_workbook_during_collection_is_not_applied(self):
        browser = OfflineBrowser({'collect': {'keys': self.keys,
            'account_id': key_fingerprint(self.keys), 'totals': {'test-key-a': Decimal(100)},
            'source_id': 'fake', 'start_date': self.start, 'end_date': self.start}})
        self.app.browser = browser
        self.app.collect()
        self.store.save_people([Person('测试甲', Decimal(-1))])
        self.pump()
        self.assertEqual(self.store.active_tracking_ids(), set())
        self.mocks[1].assert_called_once()

    def test_empty_report_keeps_one_observer_without_inventing_zero(self):
        self.store.save_people([Person('测试甲', Decimal(100)), Person('测试乙', Decimal(80))])
        self.app.browser = OfflineBrowser({'collect': {'keys': self.keys,
            'account_id': key_fingerprint(self.keys), 'totals': None,
            'message': '暂无记录', 'retryable': True}})
        self.app.start()
        self.pump()
        first_timer = self.app.timer
        self.assertTrue(self.app.observing)
        self.assertIsNotNone(first_timer)
        self.app.collect()
        self.pump()
        self.assertNotEqual(self.app.timer, first_timer)
        self.assertNotIn(first_timer, self.root.tk.call('after', 'info'))
        self.assertIsNone(self.app.rows[1]['spent'])

    def test_automatic_zero_budget_is_logged_and_skipped_people_untouched(self):
        self.store.save_people([Person('测试甲', Decimal(0)), Person('测试乙', Decimal(-1))])
        browser = OfflineBrowser({'collect': {'keys': self.keys,
            'account_id': key_fingerprint(self.keys), 'totals': None,
            'message': '暂无记录', 'retryable': True}, 'revoke': {'absent': True}})
        self.app.browser = browser
        self.app.start()
        self.pump(); self.pump()
        calls = [(action, args.get('name')) for action, args in browser.calls]
        self.assertEqual(calls, [('collect', None), ('revoke', '测试甲')])
        self.assertEqual(self.store.latest_action('test-key-a'), 'absent_verified')
        self.assertEqual(self.app.rows[1]['status'], 'skipped')

    def test_unconfirmed_action_blocks_automatic_retry(self):
        self.store.bind_keys(self.keys, account_id=key_fingerprint(self.keys))
        self.store.record_action('测试甲', 'test-key-a', 'requested', 'synthetic interrupted attempt')
        self.app.browser = OfflineBrowser({'collect': {'keys': self.keys,
            'account_id': key_fingerprint(self.keys), 'totals': None,
            'message': '暂无记录', 'retryable': True}})
        self.app.start(); self.pump()
        self.assertFalse(self.app.observing)
        self.assertEqual([action for action, _ in self.app.browser.calls], ['collect'])

    def test_delete_permit_follows_durable_requested_record(self):
        browser = OfflineBrowser({'collect': {'keys': self.keys,
            'account_id': key_fingerprint(self.keys), 'totals': None,
            'message': '暂无记录', 'retryable': True}})
        original = browser.submit
        observed = []
        def submit(action, **kwargs):
            if action != 'revoke':
                return original(action, **kwargs)
            future = Future()
            permit = kwargs['permit']
            def worker():
                permit.ready.set()
                if not permit.decided.wait(3):
                    future.set_exception(RuntimeError('No permit'))
                    return
                observed.append((permit.allowed, self.store.latest_action('test-key-a')))
                future.set_result({'absent': True})
            threading.Thread(target=worker, daemon=True).start()
            return future
        browser.submit = submit
        self.app.browser = browser
        self.app.start()
        self.pump(); self.pump(); self.pump()
        self.assertEqual(observed, [(True, 'requested')])
        self.assertEqual(self.store.latest_action('test-key-a'), 'absent_verified')

    def test_next_person_queue_error_does_not_rewrite_finished_person(self):
        self.store.bind_keys(self.keys, account_id=key_fingerprint(self.keys))
        self.store.record_action('测试甲', 'test-key-a', 'absent_verified', 'synthetic completed')
        self.app.automatic = self.app.observing = True
        with patch.object(self.app, '_process_next', side_effect=OSError('synthetic disk error')):
            self.app._schedule_processing()
            self.pump()
        self.assertEqual(self.store.latest_action('test-key-a'), 'absent_verified')
        self.assertFalse(self.app.observing)

    def test_display_failure_does_not_change_confirmed_absence_to_unknown(self):
        self.store.bind_keys(self.keys, account_id=key_fingerprint(self.keys))
        self.app.refresh_rows()
        self.app.tree.selection_set('0')
        self.app.browser = OfflineBrowser({'verify_revoke': {'absent': True}})
        with patch.object(self.app, 'refresh_rows', side_effect=OSError('synthetic display failure')):
            self.app.verify_revoke()
            self.pump()
        self.assertEqual(self.store.latest_action('test-key-a'), 'absent_verified')

    def test_close_consumes_completed_result_before_destroying(self):
        self.app.browser = OfflineBrowser({'collect': {'keys': self.keys,
            'account_id': key_fingerprint(self.keys), 'totals': {'test-key-b': Decimal(5)},
            'source_id': 'close-result', 'start_date': self.start, 'end_date': self.start}})
        self.app.collect()
        with patch.object(self.root, 'destroy'):
            self.app.close()
            self.pump(); self.pump()
        self.assertEqual(self.store.evaluate()[1]['spent'], Decimal(5))
        self.assertFalse(self.app.busy)

    def test_identity_continues_with_survivor_but_rejects_other_account(self):
        original = key_fingerprint(self.keys)
        survivor = self.keys[1:]
        self.assertEqual(check_identity(survivor, original, {'test-key-b'}), original)
        with self.assertRaises(GuardError):
            check_identity([{'name': '测试乙', 'tracking_id': 'foreign-key'}], original, {'test-key-b'})
        with self.assertRaises(GuardError):
            check_identity(survivor, original, set())


if __name__ == '__main__':
    unittest.main()
