"""Offline browser checks: synthetic DOM only, every network request is blocked."""
import os
from pathlib import Path
import threading
import time
import unittest

from playwright.sync_api import sync_playwright

from quota_guard.browser_service import BrowserService, DeletePermit, DELETE_WARNING, TRASH_PATH, key_fingerprint
from quota_guard.core import GuardError


TARGET = "00000000-0000-0000-0000-000000000001"
DECOY = "00000000-0000-0000-0000-000000000002"
SURVIVOR = "00000000-0000-0000-0000-000000000003"
KEYS = [{"name": "Fixture target", "tracking_id": TARGET},
        {"name": "Fixture target", "tracking_id": DECOY},
        {"name": "Fixture survivor", "tracking_id": SURVIVOR}]


class RevocationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        runtime = Path(__file__).resolve().parents[1] / "browser-runtime"
        if not runtime.is_dir():
            runtime = Path(__file__).resolve().parents[2] / "browser-runtime"
        if runtime.is_dir():
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(runtime)
        cls.pw = sync_playwright().start()
        try:
            # The portable bundle contains full Chromium, not a separate
            # headless-shell download. Run that exact bundled executable.
            cls.browser = cls.pw.chromium.launch(headless=True, executable_path=cls.pw.chromium.executable_path)
        except Exception:
            cls.pw.stop()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.context = self.browser.new_context()
        self.context.route("**/*", lambda route: route.abort())
        self.context.route("https://platform.deepseek.com/api_keys", lambda route: route.fulfill(
            status=200, content_type="text/html", body="<h1>API keys</h1><table><thead><tr><th>Name</th><th>ID</th></tr></thead><tbody></tbody></table>"))
        self.page = self.context.new_page()
        self.page.set_default_timeout(1000)
        self.page.goto("https://platform.deepseek.com/api_keys")
        self.page.evaluate("""({keys, trash, warning}) => {
            window.deleted = []; window.finalClicks = 0; window.replaceConfirmation = false;
            window.openDialog = (id) => {
                document.querySelector('[role=dialog]')?.remove();
                const dialog = document.createElement('div'); dialog.setAttribute('role', 'dialog');
                dialog.dataset.target = id;
                const text = document.createElement('p'); text.textContent = warning; dialog.append(text);
                const cancel = document.createElement('button'); cancel.textContent = '取消';
                cancel.onclick = () => dialog.remove(); dialog.append(cancel);
                const confirm = document.createElement('button'); confirm.textContent = '删除';
                confirm.onclick = () => {
                    window.finalClicks++; window.deleted.push(id);
                    document.querySelector(`tr[data-id="${id}"]`).remove(); dialog.remove();
                };
                dialog.append(confirm); document.body.append(dialog);
            };
            keys.forEach(key => {
                const row = document.createElement('tr'); row.dataset.id = key.tracking_id;
                row.dataset.name = key.name;
                [key.name, key.tracking_id, 'fixture', 'fixture'].forEach(value => {
                    const cell = document.createElement('td'); cell.textContent = value; row.append(cell);
                });
                const actions = document.createElement('td');
                const unrelated = document.createElement('button'); unrelated.textContent = 'Other action';
                actions.append(unrelated);
                const button = document.createElement('button');
                const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
                svg.setAttribute('width', '16'); svg.setAttribute('height', '16');
                const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
                path.setAttribute('d', trash); svg.append(path); button.append(svg);
                button.onclick = () => {
                    openDialog(key.tracking_id);
                    if (window.replaceConfirmation) setTimeout(() => openDialog(keys[1].tracking_id), 50);
                };
                actions.append(button); row.append(actions); document.querySelector('tbody').append(row);
            });
        }""", {"keys": KEYS, "trash": TRASH_PATH, "warning": DELETE_WARNING})
        # Bypass worker startup and navigation only; _revoke still exercises real
        # Playwright locators, row matching, modal checks and consequential clicks.
        self.service = object.__new__(BrowserService)
        self.service.page = self.page
        self.service.anchor = key_fingerprint(KEYS)
        self.service.known_keys = {k["tracking_id"] for k in KEYS}
        self.service._keys = lambda: self.page.locator("tbody tr").evaluate_all(
            "rows => rows.map(row => ({name: row.dataset.name, tracking_id: row.dataset.id}))")
        self.threads = []

    def tearDown(self):
        for thread in self.threads:
            thread.join(timeout=2)
        self.context.close()

    def permit(self, allowed=True, delay=0):
        permit = DeletePermit()
        def authorize():
            if permit.ready.wait(2):
                if delay:
                    time.sleep(delay)
                permit.decide(allowed)
        thread = threading.Thread(target=authorize, daemon=True)
        self.threads.append(thread)
        thread.start()
        return permit

    def revoke(self, permit=None):
        return self.service._revoke("Fixture target", TARGET, permit or self.permit())

    def test_exact_tracking_id_selects_target_among_duplicate_names(self):
        self.assertTrue(self.revoke()["absent"])
        self.assertEqual(self.page.evaluate("window.deleted"), [TARGET])
        self.assertEqual({k["tracking_id"] for k in self.service._keys()}, {DECOY, SURVIVOR})

    def test_missing_trash_icon_refuses_before_permit_or_confirmation(self):
        self.page.locator(f'tr[data-id="{TARGET}"] path').evaluate("path => path.remove()")
        permit = DeletePermit()
        with self.assertRaises(GuardError):
            self.revoke(permit)
        self.assertFalse(permit.ready.is_set())
        self.assertEqual(self.page.evaluate("window.finalClicks"), 0)

    def test_denied_permit_cancels_without_deletion(self):
        self.assertTrue(self.revoke(self.permit(False))["cancelled"])
        self.assertEqual(self.page.evaluate("window.deleted"), [])
        self.assertEqual(self.page.get_by_role("button", name="删除", exact=True).count(), 0)

    def test_repeat_target_is_never_clicked_again(self):
        permit = self.permit()
        self.revoke(permit)
        with self.assertRaises(GuardError):
            self.revoke(permit)
        self.assertEqual(self.page.evaluate("window.finalClicks"), 1)

    def test_confirmation_timeout_does_not_repeat_consequential_click(self):
        # Model a successful server-side effect followed by a stuck dialog. The
        # adapter must report uncertainty rather than click the button again.
        self.page.evaluate("""() => {
            const original = window.openDialog;
            window.openDialog = id => {
                original(id);
                const dialog = document.querySelector('[role=dialog]');
                dialog.querySelectorAll('button')[1].onclick = () => {
                    window.finalClicks++; window.deleted.push(id);
                    document.querySelector(`tr[data-id="${id}"]`).remove();
                };
            };
        }""")
        with self.assertRaises(Exception):
            self.revoke()
        self.assertEqual(self.page.evaluate("window.deleted"), [TARGET])
        self.assertEqual(self.page.evaluate("window.finalClicks"), 1)

    def test_unrecognized_confirmation_warning_refuses_click(self):
        self.page.evaluate("""() => {
            const original = window.openDialog;
            window.openDialog = id => {
                original(id);
                document.querySelector('[role=dialog] p').textContent = 'Changed fixture warning';
            };
        }""")
        permit = DeletePermit()
        with self.assertRaises(GuardError):
            self.revoke(permit)
        self.assertFalse(permit.ready.is_set())
        self.assertEqual(self.page.evaluate("window.finalClicks"), 0)

    def test_wrong_name_or_changed_key_set_refuses_without_click(self):
        with self.assertRaises(GuardError):
            self.service._revoke("Wrong fixture", TARGET, DeletePermit())
        self.page.locator(f'tr[data-id="{DECOY}"]').evaluate("row => row.remove()")
        with self.assertRaises(GuardError):
            self.revoke(DeletePermit())
        self.assertEqual(self.page.evaluate("window.finalClicks"), 0)

    def test_persisted_known_keys_verify_absence_after_browser_restart(self):
        self.page.locator(f'tr[data-id="{TARGET}"]').evaluate("row => row.remove()")
        self.service.known_keys = set()
        self.service.anchor = None
        result = self.service._verify_revoke("Fixture target", TARGET, {TARGET, SURVIVOR})
        self.assertTrue(result["absent"])

    def test_no_surviving_known_key_cannot_confirm_absence(self):
        self.page.locator(f'tr[data-id="{TARGET}"]').evaluate("row => row.remove()")
        with self.assertRaises(GuardError):
            self.service._verify_revoke("Fixture target", TARGET, {TARGET})

    def test_replaced_confirmation_cannot_delete_another_key(self):
        self.page.evaluate("window.replaceConfirmation = true")
        with self.assertRaises(Exception):
            self.revoke(self.permit(delay=.25))
        self.assertEqual(self.page.evaluate("window.deleted"), [])
        self.assertEqual(self.page.evaluate("window.finalClicks"), 0)

    def test_trusted_user_interaction_interrupts_and_unlocks_document(self):
        permit = DeletePermit()
        permit.allowed = True
        # Run a trusted browser input on the same Playwright thread at the exact
        # authorization wait boundary; no cross-thread browser calls are used.
        def interrupted_wait(_timeout):
            self.page.mouse.click(10, 10)
            return True
        permit.decided.wait = interrupted_wait
        with self.assertRaises(GuardError):
            self.revoke(permit)
        self.assertEqual(self.page.evaluate("window.finalClicks"), 0)
        self.assertTrue(self.page.evaluate("window.__quotaGuardInteraction === undefined"))


if __name__ == "__main__":
    unittest.main()
