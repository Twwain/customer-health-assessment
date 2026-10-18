"""Local tracing preserves parser/stream behavior and never writes credentials."""
import json

import pytest

from scripts.quality_evidence import QualityEvidence
from services.ai import fact_check, local_quality
from services.ai.llm_adapter import LLMMessage
from test_fact_check import ScriptedAdapter


def test_model_capture_masks_joined_chunks_and_keeps_stream_unchanged(tmp_path):
    secret = 'configured-private-value'
    recorder = QualityEvidence(tmp_path / 'trace', secrets=[secret])

    class Adapter(ScriptedAdapter):
        def stream_chat_completion(self, messages, **kwargs):
            yield 'configured-'
            yield 'private-value'

    chunks = list(recorder.adapter(Adapter([])).stream_chat_completion(
        [LLMMessage('user', '密码: secret123 ' + secret)], temperature=0))
    assert ''.join(chunks) == secret
    saved = next((tmp_path / 'trace').glob('*.json')).read_text(encoding='utf-8')
    assert secret not in saved and 'secret123' not in saved
    assert json.loads(saved)['completed'] is True


def test_capture_records_swallowed_parse_error_and_restores_parser(tmp_path):
    original = fact_check.parse_result
    recorder = QualityEvidence(tmp_path / 'trace')
    with recorder.parsers():
        result = fact_check.parse_result('{"issues":[{"claim_id":"unknown","source_id":"knowledge",'
                                         '"evidence_id":"0","reason":"错误"}]}', '草稿', {'knowledge': '来源'})
    assert result.status == 'unverified'
    assert fact_check.parse_result is original
    saved = json.loads(next((tmp_path / 'trace').glob('*fact-parse.json')).read_text(encoding='utf-8'))
    assert saved['caught_exceptions'][0]['message'] == '问题字段不完整'
    assert saved['result']['status'] == result.status


def test_capture_keeps_failed_patch_exception_and_restores_hooks(tmp_path):
    original = local_quality.apply_patches
    recorder = QualityEvidence(tmp_path / 'trace')
    with pytest.raises(ValueError, match='未提供可靠局部修订'):
        with recorder.parsers():
            local_quality.apply_patches('{"dependencies":[{"id":"u0","depends_on":[]}],"patches":[]}',
                                        local_quality.parse_answer('草稿'), {'u0'}, {})
    assert local_quality.apply_patches is original
    saved = json.loads(next((tmp_path / 'trace').glob('*patch-parse.json')).read_text(encoding='utf-8'))
    assert saved['error']['type'] == 'ValueError'


def test_capture_does_not_overwrite_an_existing_evidence_directory(tmp_path):
    with pytest.raises(FileExistsError):
        QualityEvidence(tmp_path)
