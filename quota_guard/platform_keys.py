"""Candidate native Key HTTP adapter from public platform static code, 2026-10-31.

The deployed server contract has not been exercised by this module's tests.
Only the application's own page supplies an in-memory management bearer and
explicitly allowlisted observed headers. A successful POST is only submitted;
the caller must independently verify the target's absence and account identity.
"""
from contextlib import contextmanager
from dataclasses import dataclass, field
import json
import re
import time

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from .core import GuardError
from .page_notices import dismiss_optional_notice
from .platform_http import is_cost_endpoint


BASE = 'https://platform.deepseek.com'
LIST_URL = BASE + '/api/v0/users/get_api_keys'
EDIT_URL = BASE + '/api/v0/users/edit_api_keys'
PAGE_URL = BASE + '/api_keys'
USAGE_PAGE_URL = BASE + '/usage'
HTTP_TIMEOUT_MS = 20_000
BOOTSTRAP_TIMEOUT_MS = 15_000
LIST_BOOTSTRAP_MS = 5_000
MAX_BODY_BYTES = 1_000_000
# These exact names appear in the observed public wrapper. Their server-side
# necessity is unverified; absent headers are not invented or read elsewhere.
OBSERVED_HEADER_NAMES = (
    'x-client-bundle-id', 'x-client-platform', 'x-client-version',
    'x-client-locale', 'x-client-timezone-offset', 'x-device-id', 'x-device-model',
)
# The saved official web wrapper constructs deviceModel="". Preserve that
# observed value; absence is a separate state. No other empty field is allowed.
EMPTY_HEADER_NAMES = frozenset({'x-device-model'})
MAX_CLIENT_HEADER_LENGTH = 1024
_TRACKING = re.compile(r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}')
_REDACTED = re.compile(r'sk-[A-Za-z0-9]+\*+[A-Za-z0-9]+')
_PREPARATION_MESSAGES = {
    'KEY_SESSION_MISMATCH': '管理授权必须来自本程序当前独立浏览器。',
    'KEY_PAGE_CHANGED': '平台登录已失效或离开预期页面，请重新登录。',
    'KEY_LIST_REQUEST_NOT_OBSERVED': '未观察到本次 Key 列表授权请求，未发送删除。',
    'KEY_READ_REQUEST_NOT_OBSERVED': '未观察到本次登录的已知只读授权请求，未发送删除。',
    'KEY_AUTH_HEADER_MISSING': '本次只读请求缺少管理授权，请重新登录。',
    'KEY_AUTH_HEADER_INVALID': '本次只读请求的管理授权格式不受支持，未发送删除。',
    'KEY_CLIENT_HEADER_INVALID': '本次只读请求的客户端头不符合校验，未发送删除。',
    'KEY_HEADER_READ_FAILED': '未能取得本次只读请求的授权头，未发送删除。',
    'KEY_NOTICE_REQUIRES_USER': '可选邮箱提醒无法安全关闭，请先手动处理；未发送删除。',
    'KEY_BOOTSTRAP_TIMEOUT': '建立管理会话超过期限，未发送删除。',
    'KEY_BOOTSTRAP_FAILED': '建立管理会话失败，未发送删除。',
    'KEY_DIAGNOSTIC_FAILED': '管理会话诊断未完成，未发送删除。',
}


class KeyPreparationError(GuardError):
    """Only a fixed local code/message may cross the worker boundary."""
    def __init__(self, reason_code):
        self.reason_code = (reason_code if reason_code in _PREPARATION_MESSAGES
                            else 'KEY_BOOTSTRAP_FAILED')
        super().__init__(_PREPARATION_MESSAGES[self.reason_code])


@dataclass(frozen=True, repr=False)
class DeleteTarget:
    tracking_id: str
    name: str
    sensitive_id: str = field(repr=False)
    created_at: int


def _valid_name(value):
    return (isinstance(value, str) and 0 < len(value) <= 512
            and value == value.strip() and not any(ord(char) < 32 for char in value))


def _identity(value):
    if (not isinstance(value, dict) or not _valid_name(value.get('name'))
            or not isinstance(value.get('tracking_id'), str)
            or not _TRACKING.fullmatch(value['tracking_id'])):
        raise GuardError('平台 Key 身份字段无效，未确认操作目标。')
    return value['name'], value['tracking_id']


def _target(value):
    name, tracking_id = _identity(value)
    sensitive_id, created_at = value.get('sensitive_id'), value.get('created_at')
    if (not isinstance(sensitive_id, str) or len(sensitive_id) > 160
            or not _REDACTED.fullmatch(sensitive_id)
            or type(created_at) is not int or not 0 < created_at <= 253402300799):
        raise GuardError('平台 Key 目标字段不符合已观察结构，未确认操作目标。')
    return DeleteTarget(tracking_id, name, sensitive_id, created_at)


def _strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON field')
        result[key] = value
    return result


def _reject_constant(_value):
    raise ValueError('non-finite JSON number')


class ConsoleKeyManager:
    """Single-threaded, session-scoped candidate; never retries a submission."""
    def __init__(self, context, *, emit=None):
        self._context = context
        self._emit = emit or (lambda *_args, **_kwargs: None)
        self._headers = {}
        self._issued = None
        self._attempted = set()
        self.failure_reason = None
        self._bootstrap_source = None
        self._requests = []
        self._matched_count = 0
        self._observation_reason = None
        self._capture_depth = 0
        self._capture_page = None

    def clear(self):
        self._headers.clear()
        self._issued = None
        self._attempted.clear()
        self.failure_reason = None
        self._bootstrap_source = None
        self._requests.clear()
        self._matched_count = 0
        self._observation_reason = None

    def _preparation_failure(self, reason_code):
        error = KeyPreparationError(reason_code)
        self._headers.clear()
        self._issued = None
        self._requests.clear()
        self._matched_count = 0
        self._observation_reason = None
        self.failure_reason = error.reason_code
        return error

    @contextmanager
    def capture(self, page):
        """Observe before the caller's first Key navigation; no header reads here.

        prepare() may nest this scope, so using the first list request does not
        require navigating a second time. Fresh outer scopes discard previously
        held authorization, but never reset the set of attempted mutations.
        """
        if (page.context is not self._context
                or self._capture_depth and page is not self._capture_page):
            raise self._preparation_failure('KEY_SESSION_MISMATCH')
        if not self._capture_depth:
            self._headers.clear()
            self._issued = None
            self.failure_reason = None
            self._bootstrap_source = 'key_list'
            self._requests.clear()
            self._matched_count = 0
            self._observation_reason = None
            try:
                self._context.on('request', self._queue_request)
            except Exception:
                self._bootstrap_source = None
                raise self._preparation_failure('KEY_BOOTSTRAP_FAILED') from None
            self._capture_page = page
        self._capture_depth += 1
        completed = False
        try:
            yield self
            completed = True
        finally:
            if not completed:
                self._headers.clear()
                self._issued = None
                self._requests.clear()
                self._matched_count = 0
                self._observation_reason = None
            self._capture_depth -= 1
            if not self._capture_depth:
                self._capture_page = None
                self._bootstrap_source = None
                self._requests.clear()
                try:
                    self._context.remove_listener('request', self._queue_request)
                except Exception:
                    # A failing teardown must not expose SDK text or replace a
                    # more specific failure already raised by the guarded body.
                    if completed:
                        raise self._preparation_failure('KEY_BOOTSTRAP_FAILED') from None
                    self._headers.clear()
                    self._issued = None

    def _matches_source(self, request):
        if request.method != 'GET':
            return False
        if self._bootstrap_source == 'cost_read':
            return is_cost_endpoint(request.url)
        return request.url == LIST_URL

    def _queue_request(self, request):
        # Context events also cover worker-owned requests. Do not make blocking
        # Playwright calls from this callback; read named headers in the poll.
        if self._matches_source(request):
            self._matched_count += 1
            # Keep only recent candidates from the fixed active read endpoint.
            self._requests.append(request)
            self._requests[:] = self._requests[-16:]

    def _observe(self, request):
        if not self._matches_source(request):
            return
        # Each matching candidate must stand on its own. A rejected candidate
        # must not leave prior authorization/target material usable; attempted
        # mutations stay consumed across this invalidation.
        self._headers.clear()
        self._issued = None
        try:
            bearer = request.header_value('authorization')
            if bearer is None:
                self._observation_reason = 'KEY_AUTH_HEADER_MISSING'
                return
            if (not isinstance(bearer, str) or len(bearer) > 16384
                    or not bearer.startswith('Bearer ') or len(bearer) <= 7
                    or any(not 33 <= ord(char) <= 126 for char in bearer[7:])
                    or bearer[7:].lower().startswith('sk-')):
                self._observation_reason = 'KEY_AUTH_HEADER_INVALID'
                return
            observed = {'Authorization': bearer, 'Accept': 'application/json'}
            empty_names = []
            for name in OBSERVED_HEADER_NAMES:
                value = request.header_value(name)
                if value is None:
                    continue
                kind = None
                if not isinstance(value, str):
                    kind = 'type'
                elif not value and name not in EMPTY_HEADER_NAMES:
                    kind = 'empty'
                elif len(value) > MAX_CLIENT_HEADER_LENGTH:
                    kind = 'too_long'
                elif any(ord(char) < 32 or ord(char) == 127 for char in value):
                    kind = 'control_char'
                if kind is not None:
                    self._observation_reason = 'KEY_CLIENT_HEADER_INVALID'
                    self._header_report('key_http_client_header_rejected', name, value,
                                        rejection_kind=kind, reason_code='KEY_CLIENT_HEADER_INVALID')
                    return
                observed[name] = value
                if value == '':
                    empty_names.append(name)
            for name in empty_names:
                self._header_report('key_http_empty_header_accepted', name, '')
            self._headers = observed
            self._observation_reason = None
        except KeyPreparationError:
            raise
        except Exception:
            self._observation_reason = 'KEY_HEADER_READ_FAILED'
            return

    def _header_report(self, event, name, value, **outcome):
        # Fixed type labels only: never repr(value), str(value), an arbitrary
        # class name, a value prefix/hash, or an exception message.
        value_type = 'none' if value is None else 'other'
        for builtin, label in ((str, 'str'), (bool, 'bool'), (int, 'int'), (float, 'float'),
                               (bytes, 'bytes'), (list, 'list'), (tuple, 'tuple'), (dict, 'dict')):
            if type(value) is builtin:
                value_type = label
                break
        metadata = {
            'header_name': name, 'value_type': value_type,
            'source_kind': 'cost_read' if self._bootstrap_source == 'cost_read' else 'key_list',
            'matched_request_count': min(self._matched_count, 10000),
            'bearer_format_passed': True,
        }
        if isinstance(value, str):
            # 1025 is a capped overflow bucket, not the actual oversized length.
            metadata['value_length'] = min(len(value), MAX_CLIENT_HEADER_LENGTH + 1)
        try:
            self._report(event, channel='console_http', **metadata, **outcome)
        except GuardError:
            raise KeyPreparationError('KEY_DIAGNOSTIC_FAILED') from None

    def prepare(self, page):
        self.failure_reason = None
        if (page.context is not self._context
                or self._capture_depth and page is not self._capture_page):
            raise self._preparation_failure('KEY_SESSION_MISMATCH')
        if self._headers:
            return
        deadline = time.monotonic() + BOOTSTRAP_TIMEOUT_MS / 1000
        reason = 'KEY_BOOTSTRAP_FAILED'
        try:
            with self.capture(page):
                # Only when the first list produced no matching request may a
                # fresh known cost GET supply headers. This is not evidence of
                # delete permission: read_target must still verify the Key GET
                # contract and the caller must independently verify the DOM.
                for source, destination in (('key_list', PAGE_URL), ('cost_read', USAGE_PAGE_URL)):
                    phase_deadline = (min(deadline, time.monotonic() + LIST_BOOTSTRAP_MS / 1000)
                                      if source == 'key_list' else deadline)
                    if source != self._bootstrap_source:
                        self._bootstrap_source = source
                        self._requests.clear()
                        self._matched_count = 0
                        self._observation_reason = None
                    reason = 'KEY_BOOTSTRAP_TIMEOUT'
                    remaining = int((phase_deadline - time.monotonic()) * 1000)
                    if remaining <= 0:
                        raise KeyPreparationError(reason)
                    reason = 'KEY_DIAGNOSTIC_FAILED'
                    self._report('key_http_bootstrap_started', phase=source, channel='console_http')
                    if not self._matched_count:
                        reason = 'KEY_BOOTSTRAP_FAILED'
                        try:
                            page.goto(destination, wait_until='domcontentloaded', timeout=remaining)
                        except PlaywrightTimeoutError:
                            raise KeyPreparationError('KEY_BOOTSTRAP_TIMEOUT') from None
                    while True:
                        if page.url != destination:
                            raise KeyPreparationError('KEY_PAGE_CHANGED')
                        remaining = int((phase_deadline - time.monotonic()) * 1000)
                        if remaining <= 1:
                            break
                        # Locator handlers do not run during request polling.
                        # Two bounded helper waits share the remaining budget;
                        # filled forms and unrecognized dialogs are untouched.
                        reason = 'KEY_NOTICE_REQUIRES_USER'
                        dismissed = dismiss_optional_notice(page, timeout_ms=min(1500, max(1, remaining // 2)))
                        if dismissed:
                            reason = 'KEY_DIAGNOSTIC_FAILED'
                            self._report('key_http_notice_dismissed', phase=source, channel='console_http')
                        reason = 'KEY_BOOTSTRAP_TIMEOUT'
                        if time.monotonic() >= phase_deadline:
                            raise KeyPreparationError(reason)
                        queued, self._requests = self._requests, []
                        for request in queued:
                            self._observe(request)
                            if self._headers:
                                break
                        if self._headers:
                            if time.monotonic() >= phase_deadline:
                                raise KeyPreparationError(reason)
                            reason = 'KEY_DIAGNOSTIC_FAILED'
                            self._report('key_http_bootstrap_ready', phase=source, channel='console_http', count=self._matched_count)
                            return
                        reason = 'KEY_BOOTSTRAP_FAILED'
                        page.wait_for_timeout(min(100, max(1, int((phase_deadline - time.monotonic()) * 1000))))
                    unavailable = self._observation_reason or ('KEY_LIST_REQUEST_NOT_OBSERVED' if source == 'key_list'
                                                               else 'KEY_READ_REQUEST_NOT_OBSERVED')
                    reason = 'KEY_DIAGNOSTIC_FAILED'
                    self._report('key_http_bootstrap_unavailable', phase=source, channel='console_http',
                                 reason_code=unavailable, count=self._matched_count)
                    # Do not hide rejected headers by trying a different source.
                    if self._matched_count or source == 'cost_read':
                        raise KeyPreparationError(unavailable)
        except Exception as exc:
            error = self._preparation_failure(exc.reason_code if isinstance(exc, KeyPreparationError) else reason)
            try:
                self._report('key_http_bootstrap_failed', reason_code=error.reason_code, channel='console_http')
            except GuardError:
                pass
            raise error from None

    def _options(self):
        if not self._headers:
            raise GuardError('Key 管理会话尚未授权，请先完成登录核验。')
        return dict(headers=dict(self._headers), timeout=HTTP_TIMEOUT_MS,
                    max_redirects=0, max_retries=0, fail_on_status_code=False,
                    ignore_https_errors=False)

    def _report(self, event, **fields):
        try:
            self._emit(event, **fields)
        except Exception:
            raise GuardError('Key 管理诊断记录未完成，已停止本次操作。') from None

    def _payload(self, response, url):
        if response.status != 200 or response.url != url:
            if response.status in (401, 403) or 300 <= response.status < 400 or response.url != url:
                self._headers.clear()
                self._issued = None
            raise GuardError('平台 Key 接口未返回可确认的正常结果，请核实平台状态。')
        length = response.headers.get('content-length')
        media_type = response.headers.get('content-type', '').partition(';')[0].strip().lower()
        if (media_type != 'application/json' or
                length is not None and (not length.isdigit() or int(length) > MAX_BODY_BYTES)):
            raise GuardError('平台 Key 响应类型或大小不符合读取限制，请核实平台状态。')
        body = response.body()
        if len(body) > MAX_BODY_BYTES:
            raise GuardError('平台 Key 响应超过读取限制，请核实平台状态。')
        try:
            payload = json.loads(body, object_pairs_hook=_strict_object, parse_constant=_reject_constant)
        except (ValueError, UnicodeError, RecursionError):
            raise GuardError('平台 Key 响应不是有效 JSON，请核实平台状态。') from None
        if not isinstance(payload, dict) or type(payload.get('code')) is not int or payload['code'] != 0:
            raise GuardError('平台 Key 响应未通过状态校验，请核实平台状态。')
        data = payload.get('data')
        if (not isinstance(data, dict) or type(data.get('biz_code')) is not int
                or data['biz_code'] != 0 or 'biz_data' not in data
                or data['biz_data'] is not None and not isinstance(data['biz_data'], dict)):
            raise GuardError('平台 Key 响应未通过业务校验，请核实平台状态。')
        return data['biz_data']

    @staticmethod
    def _dispose(response):
        if response is not None:
            try:
                response.dispose()
            except Exception:
                pass

    def _read_targets(self):
        options, response = self._options(), None
        try:
            response = self._context.request.get(LIST_URL, **options)
            data = self._payload(response, LIST_URL)
            rows = data.get('api_keys') if isinstance(data, dict) else None
            if not isinstance(rows, list) or len(rows) > 1000:
                raise GuardError('平台未返回完整 Key 列表，未确认操作目标。')
            targets = [_target(row) for row in rows]
            if (len({item.tracking_id for item in targets}) != len(targets)
                    or len({item.sensitive_id for item in targets}) != len(targets)):
                raise GuardError('平台 Key 完整列表中存在重复身份，未确认操作目标。')
            return targets
        except GuardError:
            raise
        except Exception:
            raise GuardError('读取平台 Key 列表失败，未能核实当前状态。') from None
        finally:
            self._dispose(response)

    def read_keys(self):
        """Read a fresh full list, including after submission; issue no permit.

        The caller compares this result with a separately refreshed DOM list.
        Neither an empty result alone nor a prior POST proves a deletion.
        """
        return [{'name': item.name, 'tracking_id': item.tracking_id}
                for item in self._read_targets()]

    def read_target(self, name, tracking_id, expected_keys):
        self._issued = None
        requested = _identity({'name': name, 'tracking_id': tracking_id})
        if not isinstance(expected_keys, (list, tuple)) or not expected_keys:
            raise GuardError('缺少已经核验的完整 Key 列表，未确认操作目标。')
        expected = [_identity(item) for item in expected_keys]
        if (len({item[1] for item in expected}) != len(expected) or requested not in expected
                or tracking_id in self._attempted):
            raise GuardError('已核验 Key 列表无效或目标已尝试提交，禁止重复操作。')
        targets = self._read_targets()
        if {(item.name, item.tracking_id) for item in targets} != set(expected):
            raise GuardError('HTTP 与已核验的完整 Key 列表不一致，未确认操作目标。')
        selected = next(item for item in targets if (item.name, item.tracking_id) == requested)
        self._report('key_http_target_verified', key_count=len(targets))
        self._issued = selected
        return selected

    def submit_once(self, target, *, before_send):
        if (not isinstance(target, DeleteTarget) or target is not self._issued
                or target.tracking_id in self._attempted or not callable(before_send)):
            raise GuardError('删除目标未取得本会话核验或已尝试提交，禁止重复操作。')
        options = self._options()
        options['data'] = {
            'action': 'delete', 'name': None, 'redacted_key': target.sensitive_id,
            'created_at': target.created_at, 'tracking_id': target.tracking_id,
        }
        # All construction, logging and method lookup precede the caller's final
        # durable pre-send gate. No waits or logging occur between it and POST.
        post = self._context.request.post
        self._report('key_http_submission_prepared')
        self._attempted.add(target.tracking_id)
        self._issued = None
        response = None
        try:
            before_send()
        except GuardError:
            raise
        except Exception:
            raise GuardError('发送前核验未完成，未发送删除请求。') from None
        try:
            response = post(EDIT_URL, **options)
            data = self._payload(response, EDIT_URL)
            # The static mapper exposes an optional apiKey; no observed server
            # response proves that deletion returns an object. Empty metadata
            # is only submitted, never absence. Validate any nonempty target.
            returned = data.get('api_key') if data else None
            if returned is not None and returned != {}:
                if _target(returned) != target:
                    raise GuardError('删除响应目标无法对应，结果仍需独立核实。')
            return {'submitted': True}
        except GuardError:
            raise
        except Exception:
            # A timeout may follow a server-side mutation. Do not retry or leak
            # SDK headers/body; the caller records an unknown result.
            raise GuardError('平台删除请求结果未能确认，禁止自动重试，请核实平台状态。') from None
        finally:
            self._dispose(response)
