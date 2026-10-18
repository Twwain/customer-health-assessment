"""Opt-in local validation evidence; never imported by the production service.

Use only with an authorized local dataset. Business text remains private in the
chosen output directory; credentials are masked before anything is written.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
import json
from pathlib import Path
import sys
import threading
from unittest.mock import patch

from services.ai import fact_check, guardrails, local_quality


class QualityEvidence:
    def __init__(self, directory: Path, *, secrets=()):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=False)
        self.secrets = tuple(value for value in secrets if isinstance(value, str) and value)
        self.label = ''
        self._lock = threading.Lock()
        self._sequence = 0

    def _clean(self, value):
        if isinstance(value, str):
            for secret in self.secrets:
                value = value.replace(secret, '***')
            return guardrails.sanitize_text(value)[0]
        if isinstance(value, dict):
            return {key: self._clean(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [self._clean(item) for item in value]
        return value

    def write(self, operation, data):
        with self._lock:
            self._sequence += 1
            path = self.directory / f'{self._sequence:03d}-{operation}.json'
            path.write_text(json.dumps(self._clean({'label': self.label, **data}),
                                       ensure_ascii=False, indent=2), encoding='utf-8')

    def adapter(self, delegate):
        recorder = self

        class RecordingAdapter:
            available = delegate.available
            model = delegate.model

            def status(self):
                return delegate.status()

            def stream_chat_completion(self, messages, **kwargs):
                # Join before masking: a credential may span streamed chunks.
                chunks = []
                error = None
                completed = False
                try:
                    for chunk in delegate.stream_chat_completion(messages, **kwargs):
                        chunks.append(chunk)
                        yield chunk
                    completed = True
                except Exception as exc:
                    error = {'type': type(exc).__name__, 'message': str(exc)}
                    raise
                finally:
                    recorder.write('model', {
                        'messages': [dict(message) if isinstance(message, dict) else asdict(message)
                                     for message in messages],
                        'response': ''.join(chunks), 'completed': completed, 'error': error,
                        'settings': {key: kwargs.get(key) for key in ('temperature', 'max_tokens', 'extra')},
                    })

        return RecordingAdapter()

    @contextmanager
    def parsers(self):
        """Observe real parser results and swallowed failures without changing them."""
        original_parse = fact_check.parse_result
        original_patch = local_quality.apply_patches
        original_review = local_quality.apply_review

        def parse(text, draft, sources, **kwargs):
            failures = []
            previous_trace = sys.gettrace()

            def trace(frame, event, arg):
                if event == 'exception' and frame.f_code is original_parse.__code__:
                    failures.append({'type': arg[0].__name__, 'message': str(arg[1]),
                                     'line': frame.f_lineno})
                return trace

            try:
                sys.settrace(trace)
                result = original_parse(text, draft, sources, **kwargs)
            finally:
                sys.settrace(previous_trace)
            self.write('fact-parse', {
                'response': text, 'draft': draft, 'sources': sources,
                'result': asdict(result), 'caught_exceptions': failures,
            })
            return result

        def observe(original, operation):
            def wrapped(text, answer, known_bad, sources):
                inputs = {'response': text, 'units': answer.data(),
                          'known_bad': sorted(known_bad), 'sources': sources}
                try:
                    rendered, diagnostic = original(text, answer, known_bad, sources)
                except Exception as exc:
                    self.write(operation, {**inputs, 'error': {'type': type(exc).__name__, 'message': str(exc)}})
                    raise
                self.write(operation, {**inputs, 'rendered': rendered, 'diagnostic': diagnostic})
                return rendered, diagnostic
            return wrapped

        with patch.object(fact_check, 'parse_result', parse), \
             patch.object(local_quality, 'apply_patches', observe(original_patch, 'patch-parse')), \
             patch.object(local_quality, 'apply_review', observe(original_review, 'review-parse')):
            yield
