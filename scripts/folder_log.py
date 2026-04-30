import json
import os
import time

_FILENAME = '.folder_log.json'


def _force_rerun() -> bool:
    return os.getenv('FORCE_RERUN', 'false').strip().lower() not in ('false', '0', 'no')


def read(folder: str) -> list:
    path = os.path.join(folder, _FILENAME)
    if not os.path.exists(path):
        return []
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def has_run(folder: str, script: str) -> bool:
    """Return True if *script* has a completed run recorded for *folder*.

    Always returns False when FORCE_RERUN=true is set, so callers re-run
    and stamp the new log record with force_rerun=true.
    """
    if _force_rerun():
        return False
    return any(r.get('script') == script for r in read(folder))


def append_run(folder: str, script: str, **data) -> None:
    """Append a completed-run record to *folder*/.folder_log.json."""
    records = read(folder)
    record: dict = {'timestamp': time.strftime('%Y-%m-%dT%H:%M:%S'), 'script': script}
    if _force_rerun():
        record['force_rerun'] = True
    record.update(data)
    records.append(record)
    path = os.path.join(folder, _FILENAME)
    tmp = path + '.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(records, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError as e:
        print(f'  [folder_log] could not write log for {os.path.basename(folder)}: {e}')