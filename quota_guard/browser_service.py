"""Visible, isolated platform session. Never extracts cookies or bearer tokens.

The optional cost decoder is based on community-observed platform responses,
not a public API contract. Unknown shapes fail closed. A deletion requires a
durable UI-side intent and a fresh, single-use authorization before its click.
"""
from concurrent.futures import Future
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
from hashlib import sha256
from pathlib import Path
from queue import Queue
from threading import Thread, Event
from urllib.parse import urlparse, parse_qs
import json
import os
import re
import sys
import uuid

from .core import GuardError, money

TZ = timezone(timedelta(hours=8))
BASE = 'https://platform.deepseek.com'
COST_PATH = '/api/v0/usage/by_api_key/cost'

# Observed platform trash icon. Never fall back to an unnamed button index.
TRASH_PATH = 'M6.5 7v4a.5.5 0 0 0 1 0V7a.5.5 0 0 0-1 0zM9 6.5a.5.5 0 0 1 .5.5v4a.5.5 0 0 1-1 0V7a.5.5 0 0 1 .5-.5zM10 4h3a.5.5 0 0 1 0 1h-.553l-.752 6.776A2.5 2.5 0 0 1 9.21 14H6.79a2.5 2.5 0 0 1-2.485-2.224L3.552 5H3a.5.5 0 0 1 0-1h3a2 2 0 1 1 4 0zM8 3a1 1 0 0 0-1 1h2a1 1 0 0 0-1-1zM4.559 5l.74 6.666A1.5 1.5 0 0 0 6.79 13h2.42a1.5 1.5 0 0 0 1.49-1.334L11.442 5H4.56z'
DELETE_WARNING = '该 API key 将立即被禁用。使用此密钥发出的 API 请求将被拒绝，这可能会导致仍然依赖它的任何系统崩溃。 一旦删除，你将无法再查看或修改此 API key。'


class DeletePermit:
    """Worker prepares; UI checks the ledger and flushes intent before approval."""
    def __init__(self):
        self.ready = Event()
        self.decided = Event()
        self.allowed = False

    def decide(self, allowed):
        if not self.decided.is_set():
            self.allowed = bool(allowed)
            self.decided.set()


class NoUsageData(GuardError):
    """A valid but empty report may be checked again without inventing costs."""


def parse_cost_response(payload, url, start_date, end_date):
    """Validate currency, time range and series before Decimal aggregation."""
    query = parse_qs(urlparse(url).query)
    if urlparse(url).hostname != 'platform.deepseek.com' or urlparse(url).path != COST_PATH:
        raise GuardError('不是预期的平台费用来源。')
    if query.get('tz') != ['28800']:
        raise GuardError('平台费用时区不是 UTC+8，请在网页调整后重试。')
    begin = int(datetime.fromisoformat(start_date).replace(tzinfo=TZ).timestamp())
    finish = int((datetime.fromisoformat(end_date).replace(tzinfo=TZ)+timedelta(days=1)).timestamp())
    try:
        if payload['code'] != 0 or payload['data']['biz_code'] != 0:
            raise GuardError('平台未返回成功费用结果，请检查登录状态。')
        data = payload['data']['biz_data']
        left, right = data['start'], data['end']
        if not isinstance(left, int) or not isinstance(right, int) or left > begin or right < finish:
            raise GuardError('平台返回范围没有覆盖完整预算周期，不能替代累计消费。')
        if data['bucket'] not in ('1d', '1h'):
            raise GuardError('暂不支持该费用时间分桶，未应用数据。')
        if int(query['start'][0]) != left or int(query['end'][0]) != right:
            raise GuardError('平台请求与返回时间范围不一致。')
        groups = data['data']
        if not isinstance(groups, list):
            raise GuardError('费用结构不是列表，未应用数据。')
        if not groups:
            raise NoUsageData('费用结果为空；不能仅凭空数组给所有人员记零消费。')
        totals, seen = {}, set()
        with localcontext() as ctx:
            ctx.prec = 50
            for group in groups:
                if group['currency'] != 'CNY':
                    raise GuardError('存在非人民币费用，不能混合相加。')
                for series in group['series']:
                    key = series['api_key']['tracking_id']
                    if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,200}', key) or key.startswith('sk-'):
                        raise GuardError('费用中缺少合法 Tracking ID。')
                    model = series['model']
                    if not isinstance(model, str) or not model:
                        raise GuardError('费用中缺少计费分类。')
                    for bucket in series['buckets']:
                        stamp = bucket['time']
                        if not isinstance(stamp, int) or not left <= stamp < right:
                            raise GuardError('费用时间超出响应范围。')
                        identity = (key, model, stamp)
                        if identity in seen:
                            raise GuardError('发现重复费用分桶，停止汇总以免重复计费。')
                        seen.add(identity)
                        cost = money(bucket['cost'])
                        if begin <= stamp < finish:
                            totals[key] = totals.get(key, Decimal(0)) + cost
        if not totals:
            raise NoUsageData('所选预算周期没有可核验的逐 Key 费用记录。')
        return totals
    except (KeyError, TypeError, ValueError, IndexError) as exc:
        if isinstance(exc, GuardError):
            raise
        raise GuardError('平台费用结构变化，已停止应用；请保留原始账单核查。') from None


class BrowserService:
    """All Playwright objects stay on one worker thread; UI receives Futures."""
    def __init__(self):
        self.queue = Queue()
        self.closed = False
        self.thread = Thread(target=self._worker, name='platform-browser', daemon=True)
        self.thread.start()

    def submit(self, action, **kwargs):
        future = Future()
        if self.closed:
            future.set_exception(GuardError('浏览器服务已关闭。'))
        else:
            self.queue.put((action, kwargs, future))
        return future

    def close(self):
        self.closed = True
        self.queue.put(None)
        self.thread.join(timeout=45)

    def _worker(self):
        self.pw = self.context = self.page = self.browser = None
        self.anchor = None
        try:
            while True:
                task = self.queue.get()
                if task is None:
                    break
                action, args, future = task
                try:
                    if action not in ('login', 'collect', 'prepare_revoke', 'verify_revoke', 'revoke', 'selftest'):
                        raise GuardError('不支持的浏览器动作。')
                    self._ensure_browser()
                    result = getattr(self, '_' + action)(**args)
                    future.set_result(result)
                except Exception as exc:
                    # Playwright exception text may include page values. Never log it.
                    future.set_exception(exc if isinstance(exc, GuardError) else GuardError(
                        '平台操作未完成，请检查网络、登录状态或页面是否改版。未确认任何删除成功。'))
        finally:
            if self.context:
                try:
                    self.context.close()
                except Exception:
                    pass
            if self.pw:
                if self.browser:
                    self.browser.close()
                self.pw.stop()

    def _ensure_browser(self):
        if self.context:
            return
        root = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parents[1]))
        bundled = root / 'browser-runtime'
        if not bundled.is_dir() and not getattr(sys, 'frozen', False):
            bundled = root.parent / 'browser-runtime'  # Existing local work copy.
        if bundled.is_dir():
            os.environ['PLAYWRIGHT_BROWSERS_PATH'] = str(bundled)
        from playwright.sync_api import sync_playwright
        self.pw = sync_playwright().start()
        # Ephemeral context: no password, token or login state saved to a file.
        self.browser = self.pw.chromium.launch(headless=False)
        self.context = self.browser.new_context(locale='zh-CN', timezone_id='Asia/Shanghai', accept_downloads=True)
        self.page = self.context.new_page()
        self.page.set_default_timeout(12000)

    def _login(self):
        self.page.goto(BASE + '/api_keys', wait_until='domcontentloaded')
        return {'message': '请在独立浏览器中登录；关闭程序后不保留登录状态。'}

    def _selftest(self):
        """Packaging check only: start the bundled browser without a network call."""
        self.page.goto('about:blank')
        return {'browser_started': True, 'version': self.browser.version}

    def _keys(self):
        self.page.goto(BASE + '/api_keys', wait_until='domcontentloaded')
        self.page.get_by_role('heading', name='API keys', exact=True).wait_for()
        table = self.page.get_by_role('table')
        table.wait_for()
        rows = table.get_by_role('row').all()
        keys = []
        for row in rows[1:]:
            cells = row.get_by_role('cell').all_text_contents()
            if len(cells) < 5:
                raise GuardError('Key 列表结构发生变化。')
            name, tracking = cells[0].strip(), cells[1].strip()
            if not re.fullmatch(r'[0-9a-fA-F-]{36}', tracking):
                raise GuardError('Key 列表 Tracking ID 格式无法核验。')
            keys.append({'name': name, 'tracking_id': tracking})
        if len({k['tracking_id'] for k in keys}) != len(keys):
            raise GuardError('Key 列表存在重复身份标识。')
        return keys

    def _collect(self, start_date, account_id='', known_keys=()):
        keys = self._keys()
        if not keys:
            raise GuardError('平台没有可绑定的 Key；无法确认当前账户。')
        # A local binding marker, NOT a DeepSeek account identifier. Existing
        # bindings must have a surviving public Tracking ID to prove continuity.
        identity = check_identity(keys, account_id, known_keys)
        self.anchor = key_fingerprint(keys)
        self.known_keys = {k['tracking_id'] for k in keys}
        responses = []
        def capture(response):
            parsed = urlparse(response.url)
            if parsed.hostname == 'platform.deepseek.com' and parsed.path == COST_PATH and response.status == 200:
                try:
                    if int(response.headers.get('content-length', '0')) <= 10_000_000:
                        body = response.body()
                        if len(body) > 10_000_000:
                            return
                        responses.append((response.url, json.loads(body, parse_float=Decimal)))
                        del responses[:-4]
                except Exception:
                    pass
        self.page.on('response', capture)
        end = datetime.now(TZ).date().isoformat()
        result = {'keys': keys, 'account_id': identity, 'totals': None,
                  'start_date': start_date, 'end_date': end,
                  'source_id': '', 'message': 'Key 列表已读取；尚未取得可核验的完整周期费用。'}
        try:
            self.page.goto(BASE + '/usage', wait_until='domcontentloaded')
            self.page.get_by_role('button', name='导出', exact=True).wait_for()
            # Bounded cooperative wait for the page's own network responses.
            for _ in range(40):
                if responses:
                    break
                self.page.wait_for_timeout(250)
            if responses:
                url, payload = responses[-1]
                try:
                    result['totals'] = parse_cost_response(payload, url, start_date, end)
                    result['source_id'] = sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()
                except GuardError as exc:
                    result['message'] = str(exc)
                    result['retryable'] = isinstance(exc, NoUsageData)
            if key_fingerprint(self._keys()) != self.anchor:
                raise GuardError('读取费用期间 Key 列表发生变化，本次结果未应用。')
        finally:
            self.page.remove_listener('response', capture)
        return result

    def _prepare_revoke(self, name, tracking_id):
        keys = self._keys()
        if {'name': name, 'tracking_id': tracking_id} not in keys:
            raise GuardError('名字或 Tracking ID 不一致，未打开删除操作。')
        identity = key_fingerprint(keys)
        if not self.anchor or identity != self.anchor:
            raise GuardError('账户或 Key 集合变化，未打开删除操作。')
        # No brittle unnamed button indexing: select exact row for the user,
        # let them click the trash icon and final confirmation themselves.
        self.page.get_by_role('row').filter(has_text=tracking_id).scroll_into_view_if_needed()
        self.page.bring_to_front()
        return {'message': '已定位对应 Key 行；请自行核对后点击垃圾桶及确认。'}

    def _verify_revoke(self, name, tracking_id, known_keys=()):
        keys = self._keys()
        current = {k['tracking_id'] for k in keys}
        expected = set(known_keys) or getattr(self, 'known_keys', set())
        # Persisted bindings survive application restarts. A remaining bound Key
        # proves continuity; a completely empty/unrelated account cannot do so.
        if not current.intersection(expected - {tracking_id}):
            raise GuardError('没有其他存活的已知 Key 可核验账户连续性；不能确认停用结果。')
        absent = tracking_id in expected and tracking_id not in current
        if not absent and {'name': name, 'tracking_id': tracking_id} not in keys:
            raise GuardError('当前 Key 的名字或身份发生变化，需人工核实。')
        if absent:
            self.known_keys = current
            self.anchor = key_fingerprint(keys)
        return {'absent': absent, 'present': not absent}

    def _revoke(self, name, tracking_id, permit):
        """One attempt only. The caller must persist unknown outcomes, not retry."""
        keys = self._keys()
        target = {'name': name, 'tracking_id': tracking_id}
        if target not in keys or key_fingerprint(keys) != self.anchor:
            raise GuardError('撤销前账户或 Key 列表发生变化，已停止。')
        expected = {k['tracking_id'] for k in keys}
        if len(expected) < 2:
            raise GuardError('这是账户内最后一个 Key，无法用其他 Key 核验删除结果；请使用手动处理。')
        row = self.page.get_by_role('row').filter(
            has=self.page.get_by_role('cell', name=tracking_id, exact=True))
        if row.count() != 1 or row.get_by_role('cell', name=name, exact=True).count() != 1:
            raise GuardError('不能唯一定位待撤销 Key。')
        button = row.get_by_role('button').filter(has=self.page.locator(f'path[d="{TRASH_PATH}"]'))
        if button.count() != 1:
            raise GuardError('平台删除按钮结构发生变化，已停止。')
        # The platform's dialog does not show a target ID. Lock this document
        # while opening it, and retain exact nodes rather than re-resolving a
        # generic confirmation button. Any human interaction aborts this attempt.
        token = uuid.uuid4().hex
        self.page.evaluate("""token => {
            const state = {token, interrupted: false};
            state.events = ['pointerdown', 'keydown', 'click', 'touchstart'];
            state.block = event => {
                if (event.isTrusted) {
                    state.interrupted = true;
                    event.preventDefault(); event.stopImmediatePropagation();
                }
            };
            for (const type of state.events) document.addEventListener(type, state.block, true);
            window.__quotaGuardInteraction = state;
        }""", token)
        try:
            button.evaluate('(node) => node.click()')
            confirm = self.page.get_by_role('button', name='删除', exact=True)
            warning = self.page.get_by_text(DELETE_WARNING, exact=True)
            confirm.wait_for(state='visible')
            if confirm.count() != 1 or not warning.is_visible():
                raise GuardError('撤销确认框不符合已核验结构，未点击确认。')
            confirm_node = confirm.element_handle()
            warning_node = warning.element_handle()
            permit.ready.set()
            if not permit.decided.wait(20) or not permit.allowed:
                cancel = self.page.get_by_role('button', name='取消', exact=True)
                if cancel.count() == 1:
                    cancel.evaluate('(node) => node.click()')
                return {'cancelled': True}
            # Check and click in one document task. A replaced modal or changed
            # document cannot redirect this click to a newly matched button.
            clicked = confirm_node.evaluate("""(node, args) => {
                const state = window.__quotaGuardInteraction;
                if (!state || state.token !== args.token || state.interrupted ||
                    location.href !== args.url || !node.isConnected ||
                    !args.warning.isConnected || !node.getClientRects().length ||
                    !args.warning.getClientRects().length || node.textContent.trim() !== '删除') return false;
                node.click(); return true;
            }""", {'token': token, 'url': BASE + '/api_keys', 'warning': warning_node})
            if not clicked:
                raise GuardError('确认期间页面或操作目标发生变化，未提交撤销。')
            # No retry: a timeout after this click may mean it already ran.
            confirm_node.wait_for_element_state('hidden')
        finally:
            self.page.evaluate("""token => {
                const state = window.__quotaGuardInteraction;
                if (state && state.token === token) {
                    for (const type of state.events) document.removeEventListener(type, state.block, true);
                    delete window.__quotaGuardInteraction;
                }
            }""", token)
        result = self._verify_revoke(name, tracking_id, expected)
        if not result['absent']:
            raise GuardError('平台仍显示该 Key，撤销结果未确认；已暂停，不自动重试。')
        return result


def key_fingerprint(keys):
    return sha256(json.dumps(sorted((k['tracking_id'], k['name']) for k in keys),
                             ensure_ascii=False).encode()).hexdigest()


def check_identity(keys, account_id, known_keys):
    if account_id:
        current = {k['tracking_id'] for k in keys}
        if not current.intersection(known_keys):
            raise GuardError('没有仍存在的已绑定 Key 可确认账户连续性；请人工核查，未重建或清零数据。')
        return account_id
    return key_fingerprint(keys)
