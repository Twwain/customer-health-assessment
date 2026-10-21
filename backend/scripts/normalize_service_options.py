"""One-off, backed-up normalization of current V3 service factor spellings.

Explicit --database and --apply are required for writes. Assessment history is
immutable evidence and is intentionally excluded from this representation edit.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.scoring.config_loader import load_scoring_config
from services.scoring.rules import evaluate_factor


LEGACY_OPTIONS = {
    'svc_03': [('<15分钟且SLA>95%', '<15min且达标率>95%', 10),
               ('<30分钟且SLA>90%', '<30min且>90%', 7),
               ('<1小时且SLA>80%', '<1h且>80%', 4)],
    'svc_04': [('<4小时且一次解决率>90%', '<4h且一次解决率>90%', 10),
               ('<8小时且一次解决率>80%', '<8h且>80%', 7),
               ('<24小时且一次解决率>70%', '<24h且>70%', 4)],
}


def normalize_fields(fields):
    if not isinstance(fields, dict):
        raise ValueError('custom_fields必须为对象，已停止迁移')
    result = dict(fields)
    changes = 0
    for field, options in LEGACY_OPTIONS.items():
        for old, new, _ in options:
            if result.get(field) == old:
                result[field] = new
                changes += 1
                break
    return result, changes


def migrate(database, *, apply=False, backup=None):
    database = Path(database).resolve(strict=True)
    scoring = load_scoring_config()
    factors = {f.field: f for d in scoring.enabled_dimensions for f in d.enabled_factors}
    for field, options in LEGACY_OPTIONS.items():
        factor = factors[field]
        for _, new, expected in options:
            if new not in factor.input.options or evaluate_factor(factor, new).score != expected:
                raise ValueError('当前配置与迁移目标不一致，已停止迁移')
    with closing(sqlite3.connect(database.as_uri() + '?mode=rw', uri=True, timeout=5)) as connection, connection:
        if apply:
            connection.execute('BEGIN IMMEDIATE')
        edits = []
        changed_fields = 0
        for cid, raw in connection.execute('SELECT id, custom_fields FROM customers ORDER BY id'):
            fields, count = normalize_fields(json.loads(raw or '{}'))
            if count:
                edits.append((json.dumps(fields, ensure_ascii=False), cid))
                changed_fields += count
        report = dict(applied=apply, customers=len(edits), fields=changed_fields, backup=None)
        if apply and edits:
            if backup is None:
                raise ValueError('写入前必须指定备份文件')
            backup = Path(backup).resolve()
            backup.parent.mkdir(parents=True, exist_ok=True)
            with backup.open('xb'):
                pass
            # A separate reader can copy the committed WAL state while the write
            # reservation prevents concurrent customer updates during migration.
            with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as reader:
                with closing(sqlite3.connect(backup)) as destination:
                    reader.backup(destination)
            connection.executemany('UPDATE customers SET custom_fields=? WHERE id=?', edits)
            report['backup'] = str(backup)
        return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', required=True)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--backup')
    args = parser.parse_args()
    print(json.dumps(migrate(args.database, apply=args.apply, backup=args.backup), ensure_ascii=False))
