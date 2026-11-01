"""Non-financial desktop preferences, kept separately from every ledger."""
from __future__ import annotations

import json
from pathlib import Path
import re

from .core import DataStore


PREFERENCES_FILENAME = '界面偏好.json'
DEFAULT_POLL_INTERVAL_SECONDS = 10
MIN_POLL_INTERVAL_SECONDS = 1
MAX_POLL_INTERVAL_SECONDS = 3600


def validate_poll_interval(value) -> int:
    message = '自动管理间隔必须是 1～3600 之间的整数秒。'
    if type(value) is int:
        interval = value
    elif isinstance(value, str) and re.fullmatch(r'[0-9]{1,4}', value.strip()):
        interval = int(value.strip())
    else:
        raise ValueError(message)
    if not MIN_POLL_INTERVAL_SECONDS <= interval <= MAX_POLL_INTERVAL_SECONDS:
        raise ValueError(message)
    return interval


def load_poll_interval(home: Path) -> tuple[int, str | None]:
    path = Path(home) / PREFERENCES_FILENAME
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        if (not isinstance(data, dict) or set(data) != {'schema', 'poll_interval_seconds'}
                or type(data['schema']) is not int or data['schema'] != 1
                or type(data['poll_interval_seconds']) is not int):
            raise ValueError('unsupported preferences')
        return validate_poll_interval(data['poll_interval_seconds']), None
    except FileNotFoundError:
        return DEFAULT_POLL_INTERVAL_SECONDS, None
    except (OSError, UnicodeError, ValueError, TypeError):
        return DEFAULT_POLL_INTERVAL_SECONDS, '间隔偏好无法读取，暂用 10 秒；原文件保留，可重新保存间隔。'


def save_poll_interval(home: Path, value) -> int:
    interval = validate_poll_interval(value)
    DataStore._write_json(Path(home) / PREFERENCES_FILENAME,
                         {'schema': 1, 'poll_interval_seconds': interval})
    return interval
