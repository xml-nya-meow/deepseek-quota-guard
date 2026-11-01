"""Bounded local development diagnostics, separate from financial audit records.

No browser headers, cookies, response bodies or credentials are serialized.
Unknown complex inputs are described by structure. This log is disposable;
ledger events are not, and must never depend on this writer succeeding.
"""
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
import re
import threading
import time
import uuid


_SECRET = re.compile(r'password|passwd|secret|token|cookie|authorization|otp|verification|验证码|密码', re.I)
_IDENTITY = re.compile(r'^(name|person|tracking_id|account_id|known_keys|keys|target|identity)$', re.I)
_RAW = re.compile(r'^(body|payload|response|headers|args|kwargs|html|dom|raw|input|result)$', re.I)
# Header diagnostics are a separate closed vocabulary, not arbitrary scalar
# strings. These fields never contain the observed value or its representation.
_HEADER_ENUMS = {
    'header_name': frozenset({
        'x-client-bundle-id', 'x-client-platform', 'x-client-version',
        'x-client-locale', 'x-client-timezone-offset', 'x-device-id', 'x-device-model',
    }),
    'rejection_kind': frozenset({'empty', 'type', 'too_long', 'control_char'}),
    'value_type': frozenset({'str', 'bool', 'int', 'float', 'bytes', 'list', 'tuple', 'dict', 'none', 'other'}),
    'source_kind': frozenset({'key_list', 'cost_read'}),
}
_HEADER_COUNTER_LIMITS = {'value_length': 1025, 'matched_request_count': 10000}
_HEADER_BOOLEANS = frozenset({'bearer_format_passed'})
_SCALARS = {
    'stage', 'action', 'status', 'reason', 'error_type', 'error_code', 'message',
    'generation', 'session', 'task_id', 'action_id', 'pid', 'worker_pid', 'parent_pid',
    'elapsed_ms', 'elapsed', 'duration_ms', 'started_at', 'timeout', 'timeout_ms',
    'count', 'key_count', 'cost_count', 'bytes', 'reply_count', 'queue_size', 'attempt',
    'success', 'cancelled', 'allowed', 'ready', 'automatic', 'busy', 'submitted',
    'complete', 'totals_available', 'start_date', 'end_date', 'http_status', 'version',
    'operation', 'button', 'outcome', 'error', 'label', 'kind', 'event',
    'panels', 'menu_visible', 'remaining_ms', 'retryable', 'cleanup_complete', 'state',
    'reason_code', 'request_start', 'request_end', 'response_start', 'response_end',
    'group_count', 'series_count', 'bucket_count', 'function_series_count',
    'source_id', 'response_code', 'biz_code', 'operation_id', 'click_completed', 'channel', 'phase',
}


def safe_text(value):
    value = str(value)
    value = re.sub(r'sk-[\w.*-]+', '[credential]', value, flags=re.I)
    value = re.sub(r'(?i)\bBearer\s+[^\s,;]+', 'Bearer [credential]', value)
    value = re.sub(r'(?i)(authorization|cookie|password|access_token|refresh_token)\s*[:=]\s*[^\r\n]+', r'\1=[redacted]', value)
    value = re.sub(r'\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+', '[credential]', value)
    value = re.sub(r'https?://[^\s<>"\x27]+', '[url]', value)
    value = re.sub(r'[A-Za-z]:[\\/][^\r\n<>"\x27]+', '[local-path]', value)
    value = re.sub(r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}', '[email]', value)
    return value[:500]


def shape(value, depth=0):
    """Keep cardinality/types, never copy strings from unknown input objects."""
    if depth >= 3:
        return {'type': type(value).__name__}
    if isinstance(value, dict):
        # Unknown dictionary keys can themselves be tokens or personal IDs.
        fields = {str(k): shape(v, depth + 1) for k, v in value.items() if str(k) in _SCALARS}
        return {'type': 'dict', 'count': len(value), 'fields': fields}
    if isinstance(value, (list, tuple, set)):
        return {'type': type(value).__name__, 'count': len(value),
                'items': [shape(v, depth + 1) for v in list(value)[:3]]}
    if isinstance(value, str):
        return {'type': 'str', 'length': len(value)}
    return {'type': type(value).__name__}


def safe_fields(fields):
    result = {}
    for key, value in fields.items():
        key = str(key)
        if key in _HEADER_ENUMS:
            if type(value) is str and value in _HEADER_ENUMS[key]:
                result[key] = value
        elif key in _HEADER_COUNTER_LIMITS:
            if type(value) is int and value >= 0:
                result[key] = min(value, _HEADER_COUNTER_LIMITS[key])
        elif key in _HEADER_BOOLEANS:
            if type(value) is bool:
                result[key] = value
        elif _SECRET.search(key):
            result[safe_text(key)] = '[redacted]'
        elif _IDENTITY.fullmatch(key):
            result[key] = {'fingerprint': sha256(str(value).encode()).hexdigest()[:12]}
        elif _RAW.fullmatch(key) or key not in _SCALARS:
            result[safe_text(key)] = shape(value)
        elif isinstance(value, (bool, int, float)) or value is None:
            result[key] = value
        elif isinstance(value, Decimal):
            result[key] = str(value)
        elif isinstance(value, str):
            result[key] = safe_text(value)
        else:
            result[key] = shape(value)
    return result


class DiagnosticLog:
    """One file per writer; bounded rotation prevents unlimited disk growth."""
    def __init__(self, folder, *, max_bytes=2_000_000, keep_files=12):
        self.folder = Path(folder)
        self.max_bytes = max(1024, max_bytes)
        self.keep_files = max(2, keep_files)
        self.lock = threading.Lock()
        self.session = uuid.uuid4().hex[:12]
        self.started = time.monotonic()
        self.path = None
        self.disabled = False

    def _new_file(self):
        self.folder.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        self.path = self.folder / f'diagnostic-{stamp}-{self.session}.jsonl'
        candidates = sorted(self.folder.glob('diagnostic-*.jsonl'), key=lambda p: p.name)
        # Only our specific disposable files; never recurse into ledger data.
        for old in candidates[:max(0, len(candidates) - self.keep_files + 1)]:
            try:
                old.unlink()
            except OSError:
                pass

    def emit(self, event, **fields):
        record = {'time': datetime.now(timezone.utc).isoformat(), 'session': self.session,
                  'elapsed_ms': round((time.monotonic() - self.started) * 1000),
                  'event': safe_text(event), 'data': safe_fields(fields)}
        try:
            with self.lock:
                if self.path is None or not self.path.exists() or self.path.stat().st_size >= self.max_bytes:
                    self._new_file()
                with self.path.open('a', encoding='utf-8') as stream:
                    stream.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + '\n')
                self.disabled = False
            return True
        except (OSError, TypeError, ValueError):
            self.disabled = True
            return False
