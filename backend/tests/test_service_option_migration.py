"""Explicit, atomic current-value migration; historical evidence stays original."""
import json
import sqlite3

import pytest

from scripts.normalize_service_options import LEGACY_OPTIONS, migrate
from services.scoring.config_loader import load_scoring_config
from services.scoring.rules import evaluate_factor


def make_database(path, fields):
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE customers (id INTEGER PRIMARY KEY, custom_fields TEXT, updated_at TEXT)')
        db.execute('CREATE TABLE assessment_history (factor_snapshot TEXT)')
        db.execute('INSERT INTO customers VALUES (1, ?, ?)', (json.dumps(fields), 'original-time'))
        db.execute('INSERT INTO assessment_history VALUES (?)', (json.dumps(fields),))


@pytest.mark.parametrize('field,old,new,score', [
    (f, old, new, score) for f, values in LEGACY_OPTIONS.items() for old, new, score in values])
def test_migration_backups_current_values_and_preserves_history(tmp_path, field, old, new, score):
    database, backup = tmp_path / 'data.db', tmp_path / 'before.db'
    original = {field: old, 'unrelated': {'retained': True}}
    make_database(database, original)
    assert migrate(database)['fields'] == 1
    with sqlite3.connect(database) as db:
        assert json.loads(db.execute('SELECT custom_fields FROM customers').fetchone()[0]) == original
    result = migrate(database, apply=True, backup=backup)
    assert result['customers'] == result['fields'] == 1
    with sqlite3.connect(database) as db:
        current, timestamp = db.execute('SELECT custom_fields, updated_at FROM customers').fetchone()
        assert json.loads(current) == {**original, field: new}
        assert timestamp == 'original-time'
        assert json.loads(db.execute('SELECT factor_snapshot FROM assessment_history').fetchone()[0]) == original
    with sqlite3.connect(backup) as db:
        assert json.loads(db.execute('SELECT custom_fields FROM customers').fetchone()[0]) == original
    assert migrate(database, apply=True, backup=backup)['fields'] == 0
    factor = next(f for d in load_scoring_config().enabled_dimensions for f in d.enabled_factors if f.field == field)
    assert new in factor.input.options
    assert evaluate_factor(factor, new).score == score


def test_existing_backup_is_never_overwritten_and_customer_writes_roll_back(tmp_path):
    database, backup = tmp_path / 'data.db', tmp_path / 'before.db'
    original = {'svc_03': LEGACY_OPTIONS['svc_03'][0][0]}
    make_database(database, original)
    backup.write_bytes(b'keep existing backup')
    with pytest.raises(FileExistsError):
        migrate(database, apply=True, backup=backup)
    assert backup.read_bytes() == b'keep existing backup'
    with sqlite3.connect(database) as db:
        assert json.loads(db.execute('SELECT custom_fields FROM customers').fetchone()[0]) == original


def test_bad_customer_json_prevents_partial_migration(tmp_path):
    database = tmp_path / 'data.db'
    original = {'svc_03': LEGACY_OPTIONS['svc_03'][0][0]}
    make_database(database, original)
    with sqlite3.connect(database) as db:
        db.execute('INSERT INTO customers VALUES (2, ?, ?)', ('invalid-json', 'old'))
    with pytest.raises(ValueError):
        migrate(database, apply=True, backup=tmp_path / 'before.db')
    with sqlite3.connect(database) as db:
        assert json.loads(db.execute('SELECT custom_fields FROM customers WHERE id=1').fetchone()[0]) == original


def test_scoring_configuration_no_longer_accepts_retired_service_aliases():
    factors = {f.field: f for d in load_scoring_config().enabled_dimensions for f in d.enabled_factors}
    for field, aliases in LEGACY_OPTIONS.items():
        for old, new, expected in aliases:
            factor = factors[field]
            assert old not in factor.rule.params['map']
            assert evaluate_factor(factor, old).score == factor.rule.params['default_score']
            assert evaluate_factor(factor, new).score == expected
