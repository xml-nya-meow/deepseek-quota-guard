"""Decode observed console costs without making requests or writing a ledger.

Production still rejects nonempty function_series. The separately named offline
candidate follows the saved official client (4179.a95866c86d.js): uT at character
offset 521149 maps both series leaves, 566854 maps function_series, and 570517
adds both categories. This static evidence is not a real Search/Fetch bill or a
guarantee of complete server accounting. No environment or UI switch enables it.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, localcontext
import re
from urllib.parse import parse_qs, urlparse

from .core import GuardError, TZ, money
from .platform_http import CostReadError, is_cost_endpoint


_KEY_ID = re.compile(r'[A-Za-z0-9_-]{8,200}')
_MODEL_SERIES = 'series'
_FUNCTION_SERIES = 'function_series'


class NoUsageData(CostReadError):
    """A valid but empty report may be checked again without inventing costs."""
    def __init__(self, message):
        super().__init__(message, reason_code='NO_USAGE_DATA', retryable=True)


@dataclass(frozen=True)
class OfflineFunctionCostCandidate:
    """Observed cumulative amounts for offline comparison, never a write permit.

    Missing keys are explicitly absent from totals, not filled with zero. Even
    when none are missing, production_validated remains False: a syntactically
    valid fixture cannot prove actual tool categories or complete billing.
    """
    totals: dict[str, Decimal]
    category_totals: dict[str, dict[str, Decimal]]
    missing_tracking_ids: frozenset[str]
    production_validated: bool = field(default=False, init=False)


def _valid_key(value):
    return (isinstance(value, str) and _KEY_ID.fullmatch(value) is not None
            and not value.startswith('sk-'))


def _accumulate_series(series, category, totals, seen, *, left, right, begin,
                       finish, seconds, expected_keys):
    """One leaf validator for model and function costs; never infer unit prices."""
    if not isinstance(series, list):
        raise CostReadError('费用计费分类不是列表，未应用数据。', reason_code='SCHEMA_UNSUPPORTED')
    for entry in series:
        key = entry['api_key']['tracking_id']
        if not _valid_key(key):
            raise CostReadError('费用中缺少合法 Tracking ID。', reason_code='KEY_ID_INVALID')
        if expected_keys is not None and key not in expected_keys:
            raise CostReadError('离线样本含未明确绑定的 Tracking ID，不分摊其费用。',
                                reason_code='KEY_ID_UNBOUND')
        model = entry['model']
        if not isinstance(model, str) or not model:
            raise CostReadError('费用中缺少计费分类。', reason_code='SCHEMA_UNSUPPORTED')
        if not isinstance(entry['buckets'], list):
            raise CostReadError('费用分桶明细不是列表，未应用数据。', reason_code='SCHEMA_UNSUPPORTED')
        for bucket in entry['buckets']:
            stamp = bucket['time']
            if type(stamp) is not int or stamp < left or stamp + seconds > right:
                raise CostReadError('费用时间超出响应范围。', reason_code='PERIOD_MISMATCH')
            if (stamp - begin) % seconds:
                raise CostReadError('费用分桶与 UTC+8 统计边界不对齐，未应用数据。',
                                    reason_code='BUCKET_MISALIGNED')
            identity = (category, key, model, stamp)
            if identity in seen:
                raise CostReadError('发现重复费用分桶，停止汇总以免重复计费。',
                                    reason_code='DUPLICATE_COST_BUCKET')
            seen.add(identity)
            try:
                cost = money(bucket['cost'])
            except GuardError:
                raise CostReadError('费用金额格式或精度不合法，未应用数据。',
                                    reason_code='AMOUNT_INVALID') from None
            if begin <= stamp < finish:
                totals[key] = totals.get(key, Decimal(0)) + cost


def _parse_categories(payload, url, start_date, end_date, *, offline_functions,
                      expected_keys=None):
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    if not is_cost_endpoint(url):
        raise CostReadError('不是预期的平台费用来源。', reason_code='SOURCE_MISMATCH')
    if query.get('tz') != ['28800']:
        raise CostReadError('平台费用时区不是 UTC+8，本次费用未应用。', reason_code='TIMEZONE_MISMATCH')
    begin = int(datetime.fromisoformat(start_date).replace(tzinfo=TZ).timestamp())
    finish = int((datetime.fromisoformat(end_date).replace(tzinfo=TZ)
                  + timedelta(days=1)).timestamp())
    try:
        if type(payload['code']) is not int or type(payload['data']['biz_code']) is not int:
            raise CostReadError('平台费用状态结构变化，未更新费用。', reason_code='SCHEMA_UNSUPPORTED')
        if payload['code'] != 0 or payload['data']['biz_code'] != 0:
            raise CostReadError('平台未返回成功费用结果，请检查登录状态。', reason_code='PLATFORM_REJECTED')
        data = payload['data']['biz_data']
        left, right = data['start'], data['end']
        if type(left) is not int or type(right) is not int or left > begin or right < finish:
            raise CostReadError('平台返回范围没有覆盖完整预算周期，不能替代累计消费。',
                                reason_code='PERIOD_INCOMPLETE')
        interval = data['bucket']
        if type(interval) is int and interval in (3600, 86400):
            seconds = interval
        elif isinstance(interval, str) and interval in ('1h', '1d'):
            seconds = {'1h': 3600, '1d': 86400}[interval]
        else:
            raise CostReadError('暂不支持该费用时间分桶，未应用数据。', reason_code='BUCKET_UNSUPPORTED')
        if query.get('start') != [str(left)] or query.get('end') != [str(right)]:
            raise CostReadError('平台请求与返回时间范围不一致。', reason_code='PERIOD_MISMATCH')
        groups = data['data']
        if not isinstance(groups, list):
            raise CostReadError('费用结构不是列表，未应用数据。', reason_code='SCHEMA_UNSUPPORTED')
        if not groups:
            raise NoUsageData('费用结果为空；不能仅凭空数组给所有人员记零消费。')
        categories = {_MODEL_SERIES: {}, _FUNCTION_SERIES: {}}
        seen = set()
        with localcontext() as context:
            context.prec = 50
            for group in groups:
                if group['currency'] != 'CNY':
                    raise CostReadError('存在非人民币费用，不能混合相加。', reason_code='CURRENCY_UNSUPPORTED')
                functions = group.get(_FUNCTION_SERIES, [])
                if not isinstance(functions, list) or functions and not offline_functions:
                    raise CostReadError('费用包含尚未支持的函数计费明细，未应用不完整金额。',
                                        reason_code='CHARGE_TYPE_UNSUPPORTED')
                for category, series in ((_MODEL_SERIES, group[_MODEL_SERIES]),
                                         (_FUNCTION_SERIES, functions)):
                    _accumulate_series(series, category, categories[category], seen,
                                       left=left, right=right, begin=begin, finish=finish,
                                       seconds=seconds, expected_keys=expected_keys)
        if not any(categories.values()):
            raise NoUsageData('所选预算周期没有可核验的逐 Key 费用记录。')
        return categories
    except (KeyError, TypeError, ValueError, IndexError) as exc:
        if isinstance(exc, CostReadError):
            raise
        raise CostReadError('平台费用结构变化，已停止应用；请保留原始账单核查。',
                            reason_code='SCHEMA_UNSUPPORTED') from None


def parse_cost_response(payload, url, start_date, end_date):
    """Production decoder: retain the nonempty-function rejection boundary."""
    return _parse_categories(payload, url, start_date, end_date,
                             offline_functions=False)[_MODEL_SERIES]


def parse_cost_response_with_functions_offline(payload, url, start_date, end_date,
                                               *, expected_tracking_ids):
    """Parse explicit synthetic/redacted samples without enabling production.

    The caller must supply all known identity bindings for this offline sample.
    Unknown identities are rejected, missing identities remain missing, and the
    result is deliberately not the production collection-result dictionary.
    Function category names come from each observed leaf's model field; no
    guessed Search/Fetch labels, usage counts, total fields, or quoted prices
    are added. Every result replaces a previous cumulative sample, not a delta.
    """
    if (not isinstance(expected_tracking_ids, (list, tuple, set, frozenset))
            or not expected_tracking_ids or not all(_valid_key(key) for key in expected_tracking_ids)
            or len(set(expected_tracking_ids)) != len(expected_tracking_ids)):
        raise CostReadError('离线候选需要明确且唯一的 Tracking ID 绑定。', reason_code='KEY_ID_INVALID')
    expected = frozenset(expected_tracking_ids)
    categories = _parse_categories(payload, url, start_date, end_date,
                                   offline_functions=True, expected_keys=expected)
    totals = {}
    with localcontext() as context:
        context.prec = 50
        for subtotal in categories.values():
            for key, amount in subtotal.items():
                totals[key] = totals.get(key, Decimal(0)) + amount
    return OfflineFunctionCostCandidate(totals, categories, expected.difference(totals))
