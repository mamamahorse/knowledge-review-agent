"""Atomic, content-bound checkpoints for local PDF page extraction only."""

from hashlib import sha256
import json
from pathlib import Path
from uuid import uuid4

from agent_cancel import checkpoint

VERSION = 1


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(',', ':'))


def write_record(path, record):
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(uuid4().hex + '.tmp')
    try:
        pending.write_text(encoded(record), encoding='utf-8')
        pending.replace(path)
    finally:
        pending.unlink(missing_ok=True)


def read_page(root, raw_hash, config, page, extract, *, force=False):
    identity = dict(version=VERSION, raw_hash=raw_hash, config=config, page=page)
    key = sha256(encoded(identity).encode()).hexdigest()
    path = Path(root) / (key + '.json')
    checkpoint()
    if path.is_file() and not force:
        try:
            record = json.loads(path.read_text(encoding='utf-8'))
            result = record['result']
            if record['identity'] == identity and record['status'] == 'completed' \
                    and record['digest'] == sha256(encoded(result).encode()).hexdigest() \
                    and isinstance(result['items'], list) and isinstance(result['issues'], list):
                return result
        except (OSError, ValueError, KeyError, TypeError):
            pass  # An incomplete/corrupt checkpoint is never treated as a successful page.
    try:
        result = extract()
        checkpoint()
    except Exception as exc:
        # A failing concurrent attempt must not overwrite another attempt's completed page.
        write_record(path.with_suffix('.failed.json'),
                     dict(identity=identity, status='failed', error_type=type(exc).__name__))
        raise
    write_record(path, dict(identity=identity, status='completed', result=result,
                           digest=sha256(encoded(result).encode()).hexdigest()))
    return result
