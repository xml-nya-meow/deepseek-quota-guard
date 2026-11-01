"""Read-only console HTTP adapter for the application's own login session.

These are observed console endpoints, not DeepSeek's public model API contract.
The adapter only sends GET requests to one fixed endpoint. It never reads a
browser profile, cookies, local storage, or a model API key. A bearer header is
observed only on the app's own browser's request to that same endpoint, retained
in worker memory, and discarded when the session is replaced.
"""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json
import re
import time
from urllib.parse import urlencode, urlsplit

from .core import GuardError


BASE = 'https://platform.deepseek.com'
COST_PATH = '/api/v0/usage/by_api_key/cost'
TZ = timezone(timedelta(hours=8))
MAX_BODY_BYTES = 10_000_000
HTTP_TIMEOUT_MS = 20_000
BOOTSTRAP_TIMEOUT_MS = 15_000


class CostReadError(GuardError):
    """Safe client-owned message and stable code; no server text or headers."""
    def __init__(self, message, *, reason_code, retryable=False):
        super().__init__(message)
        self.reason_code = reason_code
        self.retryable = bool(retryable)


def is_cost_endpoint(url):
    try:
        parsed = urlsplit(url)
        return (parsed.scheme == 'https' and parsed.netloc == 'platform.deepseek.com'
                and parsed.path == COST_PATH and not parsed.fragment)
    except (TypeError, ValueError):
        return False


def cost_url(start_date, end_date):
    """UI dates are inclusive; the observed HTTP interval is [start, end)."""
    try:
        first, last = date.fromisoformat(start_date), date.fromisoformat(end_date)
        if first > last:
            raise ValueError('reversed range')
        begin = int(datetime.combine(first, datetime.min.time(), TZ).timestamp())
        finish = int(datetime.combine(last + timedelta(days=1), datetime.min.time(), TZ).timestamp())
    except (TypeError, ValueError, OverflowError):
        raise CostReadError('统计日期范围无效，请修改统计起点。',
                            reason_code='INVALID_DATE_RANGE') from None
    return BASE + COST_PATH + '?' + urlencode({'start': begin, 'end': finish, 'tz': 28800})


def cost_metadata(payload):
    """Only numeric structure/range metadata; never copy account or fee values."""
    fields = {}
    if not isinstance(payload, dict):
        return fields
    if type(payload.get('code')) is int:
        fields['response_code'] = payload['code']
    envelope = payload.get('data')
    if not isinstance(envelope, dict):
        return fields
    if type(envelope.get('biz_code')) is int:
        fields['biz_code'] = envelope['biz_code']
    data = envelope.get('biz_data')
    if not isinstance(data, dict):
        return fields
    for source, destination in (('start', 'response_start'), ('end', 'response_end')):
        if type(data.get(source)) is int:
            fields[destination] = data[source]
    groups = data.get('data')
    if not isinstance(groups, list):
        return fields
    fields.update(group_count=len(groups), series_count=0, bucket_count=0, function_series_count=0)
    for group in groups:
        if not isinstance(group, dict):
            continue
        functions = group.get('function_series')
        if isinstance(functions, list):
            fields['function_series_count'] += len(functions)
        series = group.get('series')
        if isinstance(series, list):
            fields['series_count'] += len(series)
            for entry in series:
                if isinstance(entry, dict) and isinstance(entry.get('buckets'), list):
                    fields['bucket_count'] += len(entry['buckets'])
    return fields


class ConsoleCostReader:
    """Thread-confined, session-scoped HTTP reader; no implicit network retries."""
    def __init__(self, context, *, emit=None):
        self._context = context
        self._authorization = None
        self._emit = emit or (lambda *_args, **_kwargs: None)

    def clear(self):
        self._authorization = None

    def _observe(self, request):
        # An exact endpoint and method prevent forwarding unrelated credentials.
        if request.method != 'GET' or not is_cost_endpoint(request.url):
            return
        try:
            value = request.header_value('authorization')
        except Exception:
            return
        # Do not accept a model API key or a value with line breaks. The value is
        # deliberately never included in repr, diagnostics, IPC, or exceptions.
        if isinstance(value, str) and len(value) <= 16384 and re.fullmatch(r'Bearer [^\s]+', value):
            if not value[7:].lower().startswith('sk-'):
                self._authorization = value

    def prepare(self, page):
        if self._authorization is not None:
            return
        # Only initial session bootstrap needs a page navigation. No calendar,
        # filters, UI date selection, or default response supplies ledger costs.
        deadline = time.monotonic() + BOOTSTRAP_TIMEOUT_MS / 1000
        page.on('request', self._observe)
        try:
            page.goto(BASE + '/usage', wait_until='domcontentloaded', timeout=BOOTSTRAP_TIMEOUT_MS)
            parsed = urlsplit(page.url)
            if parsed.scheme != 'https' or parsed.netloc != 'platform.deepseek.com' or parsed.path != '/usage':
                raise CostReadError('平台登录已失效或离开用量页面，请重新登录。', reason_code='AUTH_REQUIRED')
            while self._authorization is None and time.monotonic() < deadline:
                parsed = urlsplit(page.url)
                if parsed.scheme != 'https' or parsed.netloc != 'platform.deepseek.com' or parsed.path != '/usage':
                    raise CostReadError('平台登录已失效或离开用量页面，请重新登录。', reason_code='AUTH_REQUIRED')
                page.wait_for_timeout(100)
            if self._authorization is None:
                raise CostReadError('未取得本次登录的费用接口授权，请重新登录后读取。不会改用日期菜单。',
                                    reason_code='AUTH_REQUIRED')
        except CostReadError:
            self.clear()
            raise
        except Exception:
            self.clear()
            raise CostReadError('建立费用读取会话失败，请检查网络后重新读取。',
                                reason_code='SESSION_UNAVAILABLE', retryable=True) from None
        finally:
            page.remove_listener('request', self._observe)

    def read(self, start_date, end_date):
        if self._authorization is None:
            raise CostReadError('费用读取会话尚未授权，请先完成登录。', reason_code='AUTH_REQUIRED')
        url = cost_url(start_date, end_date)
        response = None
        try:
            # BrowserContext.request shares only this app's isolated cookie jar.
            # Redirects are disabled so the explicit bearer cannot be forwarded.
            response = self._context.request.get(url, headers={
                'Authorization': self._authorization, 'Accept': 'application/json',
            }, timeout=HTTP_TIMEOUT_MS, max_redirects=0, max_retries=0,
                fail_on_status_code=False, ignore_https_errors=False)
            self._emit('cost_http_response', http_status=response.status)
            if response.status in (401, 403):
                self.clear()
                raise CostReadError('平台费用接口未授权，请重新登录；原费用保留。', reason_code='AUTH_REQUIRED')
            if 300 <= response.status < 400 or response.url != url:
                self.clear()
                raise CostReadError('费用接口发生跳转，未发送后续请求，请重新登录。', reason_code='REDIRECT_REJECTED')
            if response.status != 200:
                raise CostReadError('费用接口返回错误，本次没有更新费用。', reason_code='HTTP_ERROR',
                                    retryable=response.status == 429 or response.status >= 500)
            length = response.headers.get('content-length')
            if length is not None and (not length.isdigit() or int(length) > MAX_BODY_BYTES):
                raise CostReadError('费用响应大小不符合读取限制，未更新费用。', reason_code='RESPONSE_TOO_LARGE')
            media_type = response.headers.get('content-type', '').partition(';')[0].strip().lower()
            if media_type != 'application/json':
                raise CostReadError('费用接口未返回 JSON 账单，可能需要重新登录。', reason_code='RESPONSE_NOT_JSON')
            body = response.body()
            if len(body) > MAX_BODY_BYTES:
                raise CostReadError('费用响应超过读取限制，未更新费用。', reason_code='RESPONSE_TOO_LARGE')
            try:
                def reject_constant(_value):
                    raise ValueError('non-finite JSON number')
                payload = json.loads(body, parse_float=Decimal, parse_constant=reject_constant)
            except (ValueError, UnicodeError, RecursionError):
                raise CostReadError('费用响应不是有效 JSON，未更新费用。', reason_code='RESPONSE_NOT_JSON') from None
            self._emit('cost_response_finished', bytes=len(body), http_status=response.status,
                       **cost_metadata(payload))
            return url, payload
        except CostReadError:
            raise
        except Exception:
            # SDK errors can include request headers. Return only fixed text.
            raise CostReadError('费用接口请求超时或网络中断，请稍后重新读取。',
                                reason_code='NETWORK_ERROR', retryable=True) from None
        finally:
            if response is not None:
                try:
                    response.dispose()
                except Exception:
                    pass
