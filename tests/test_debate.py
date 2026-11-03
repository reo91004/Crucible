import fcntl
import json
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from crucible import debate, providers
from crucible.job import atomic
from crucible.store import Store


class DebateTests(unittest.TestCase):
    def _record_artifact(self, raw):
        path = self.project / 'recorded.bin'
        path.write_bytes(raw)
        store = Store(self.project)
        try:
            store.save_attempt({'id': 1, 'status': 'complete', 'artifacts': {'recorded': str(path)}, 'artifact_sha256': {'recorded': hashlib.sha256(raw).hexdigest()}})
        finally:
            store.close()
        case = debate.start(self.project, 'What can be inferred from the available evidence?')
        snapshot = json.loads((self.project / '.crucible/debates' / case['id'] / 'snapshot.json').read_text())
        return snapshot['packet']['evidence']['artifact:1:recorded']

    def test_latest_large_artifact_is_hashed_without_including_content(self):
        raw = b'x' * 65537
        item = self._record_artifact(raw)
        self.assertEqual(item['sha256'], hashlib.sha256(raw).hexdigest())
        self.assertEqual(item['bytes'], len(raw))
        self.assertNotIn('text', item)
        self.assertIn('content_omitted', item)

    def test_latest_binary_artifact_keeps_metadata_and_omission_notice(self):
        raw = b'\xff\x00\x01'
        item = self._record_artifact(raw)
        self.assertEqual(item['sha256'], hashlib.sha256(raw).hexdigest())
        self.assertEqual(item['bytes'], len(raw))
        self.assertNotIn('text', item)
        self.assertIn('Binary', item['content_omitted'])

    def test_changed_snapshot_cannot_reuse_an_old_discussion(self):
        case = debate.start(self.project, 'Original question')
        path = self.project / '.crucible/debates' / case['id'] / 'snapshot.json'
        value = json.loads(path.read_text())
        value['question'] = 'Changed question'
        path.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, 'snapshot changed'):
            debate.run(self.project, case['id'])
        self.assertEqual(self.prompts, [])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)
        (self.project / '.crucible').mkdir()
        (self.project / 'experiment.py').write_text('import time\nfrom pathlib import Path\ntime.sleep(1.5)\nPath("metrics.json").write_text("{}")\n')
        (self.project / 'evaluate.py').write_text('import json,sys\njson.load(sys.stdin)\nprint(json.dumps({"event":"normal","summary":"done","metrics":{},"comparison_key":"test","next":{"action":"stop","reason":"done"}}))\n')
        self.cfg = {'version': 1, 'goal': 'Test',
                    'models': [{'name': 'a', 'backend': 'opencode', 'model': 'model-a'},
                               {'name': 'b', 'backend': 'claude', 'model': 'model-b'}],
                    'planner': 'a', 'limits': {'max_runs': 2, 'max_calls': 10, 'call_timeout_seconds': 3},
                    'initial': {'action': 'run', 'experiment': 'test', 'parameters': {'seed': 7}, 'reason': 'baseline'},
                    'experiments': {'test': {'command': ['{python}', '{project}/experiment.py'],
                                           'parameters': {'seed': {'type': 'integer'}},
                                           'artifacts': ['metrics.json'], 'inputs': ['experiment.py'], 'timeout_seconds': 5}},
                    'evaluator': {'command': ['{python}', '{project}/evaluate.py'], 'inputs': ['evaluate.py']}}
        (self.project / '.crucible/config.json').write_text(json.dumps(self.cfg))
        self.prompts = []
        self.invoke = patch.object(providers, 'invoke', side_effect=self.fake).start()
        self.addCleanup(patch.stopall)

    def fake(self, model, prompt, directory, timeout):
        self.prompts.append(prompt)
        if 'Synthesize all reviews.' in prompt:
            value = {'conclusion': 'Unresolved without experiments', 'agreements': [], 'disagreements': [],
                     'next_checks': ['Measure'], 'evidence_refs': ['question']}
        else:
            value = {'supported_findings': [], 'concerns': ['No measurements'], 'evidence_refs': ['question'],
                     'needs_rebuttal': True, 'next_test': {'hypothesis': 'Measure', 'predictions': {}}}
        directory.mkdir(parents=True, exist_ok=True)
        (directory / 'prompt.txt').write_text(prompt)
        return {'text': json.dumps(value)}

    def start(self, **kwargs):
        return debate.start(self.project, 'Does this claim hold?', **kwargs)

    def run_case(self, case, **kwargs):
        return debate.run(self.project, case['id'], **kwargs)

    def test_independent_packet_completion_and_idempotent_event(self):
        case = self.start()
        result = self.run_case(case)
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(self.prompts[0], self.prompts[1])
        self.assertEqual(len(result['calls']), 3)
        self.assertIn('No empirical evidence', result['report']['evidence_limits'])
        self.run_case(case)
        self.assertEqual(self.invoke.call_count, 3)
        store = Store(self.project)
        self.addCleanup(store.close)
        self.assertEqual(store.db.execute('SELECT COUNT(*) FROM calls').fetchone()[0], 0)
        self.assertEqual(store.db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0], 0)
        self.assertEqual(store.db.execute("SELECT COUNT(*) FROM events WHERE kind='manual_debate_completed'").fetchone()[0], 1)

    def test_rebuttal_only_when_requested(self):
        result = self.run_case(self.start(rounds=1))
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(len(result['calls']), 5)
        self.assertEqual(self.prompts[2], self.prompts[3])
        self.assertIn('prior_reviews', self.prompts[2])

    def test_snapshot_survives_config_and_evidence_changes(self):
        (self.project / 'evidence.txt').write_text('Original evidence')
        case = self.start(evidence=['evidence.txt'])
        before = (debate._directory(self.project, case['id']) / 'snapshot.json').read_bytes()
        (self.project / 'evidence.txt').write_text('Changed evidence')
        (self.project / '.crucible/config.json').write_text('{}')
        result = self.run_case(case)
        self.assertEqual(result['status'], 'complete')
        self.assertIn('Original evidence', self.prompts[0])
        self.assertNotIn('Changed evidence', self.prompts[0])
        self.assertEqual(before, (debate._directory(self.project, case['id']) / 'snapshot.json').read_bytes())

    def test_evidence_and_case_boundaries(self):
        (self.project / 'linked').symlink_to('/etc/passwd')
        (self.project / 'large').write_bytes(b'x' * 65537)
        for path in ('../outside', '/etc/passwd', 'linked', 'large', 'missing'):
            with self.subTest(path=path), self.assertRaises((ValueError, OSError)):
                self.start(evidence=[path])
        for case_id in ('../outside', '/tmp', 'a' * 32 + '/..'):
            with self.assertRaises(ValueError):
                debate.run(self.project, case_id)
        with self.assertRaises(ValueError):
            self.start(rounds=11)

    def test_default_latest_completed_attempt_and_hash_validation(self):
        (self.project / 'metrics.json').write_text('{"loss":1}')
        item = debate._file(self.project, 'metrics.json')
        store = Store(self.project)
        self.addCleanup(store.close)
        store.save_attempt({'id': 1, 'status': 'complete', 'evaluation': {'summary': 'Measured'},
                            'artifacts': {'metrics': str(self.project / 'metrics.json')},
                            'artifact_sha256': {'metrics': item['sha256']}})
        store.save_attempt({'id': 2, 'status': 'running', 'artifacts': {}})
        result = self.run_case(self.start())
        self.assertEqual(result['status'], 'complete')
        self.assertIn('evaluation:1', self.prompts[0])
        self.assertIn('artifact:1:metrics', self.prompts[0])
        (self.project / 'metrics.json').write_text('changed')
        with self.assertRaises(ValueError):
            self.start()

    def test_failure_blocks_synthesis_and_explicit_retry_keeps_budget(self):
        self.invoke.side_effect = providers.ProviderError('known failure')
        case = self.start(max_calls=4)
        self.assertEqual(self.run_case(case)['status'], 'failed')
        self.run_case(case)
        self.assertEqual(self.invoke.call_count, 1)
        self.invoke.side_effect = self.fake
        result = self.run_case(case, retry=True)
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(len(result['calls']), 4)
        self.assertEqual(result['calls'][0]['status'], 'failed')

    def test_invalid_response_and_unknown_reference_block_synthesis(self):
        for text in ('not json', json.dumps({'supported_findings': [], 'concerns': [], 'evidence_refs': ['invented'],
                                         'needs_rebuttal': False, 'next_test': {'hypothesis': 'x', 'predictions': {}}})):
            self.invoke.side_effect = None
            self.invoke.return_value = {'text': text}
            result = self.run_case(self.start())
            self.assertEqual(result['status'], 'failed')
            self.assertEqual(len(result['calls']), 1)
            self.assertIsNone(result['report'])

    def test_invalid_synthesis_never_publishes_completion(self):
        original = self.fake
        def bad_synthesis(*args):
            response = original(*args)
            if 'Synthesize all reviews.' in args[1]:
                value = json.loads(response['text'])
                value['evidence_refs'] = ['missing-evidence']
                response['text'] = json.dumps(value)
            return response
        self.invoke.side_effect = bad_synthesis
        result = self.run_case(self.start())
        self.assertEqual(result['status'], 'failed')
        self.assertIsNone(result['report'])
        store = Store(self.project)
        self.addCleanup(store.close)
        self.assertEqual(store.db.execute("SELECT COUNT(*) FROM events WHERE kind='manual_debate_completed'").fetchone()[0], 0)

    def test_case_lock_prevents_concurrent_runner(self):
        case = self.start()
        with (debate._directory(self.project, case['id']) / 'lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(RuntimeError, 'active runner'):
                self.run_case(case)
        self.assertEqual(self.invoke.call_count, 0)

    def test_budget_counts_failures(self):
        self.invoke.side_effect = providers.ProviderError('failed')
        case = self.start(max_calls=1)
        self.run_case(case)
        result = self.run_case(case, retry=True)
        self.assertEqual(result['status'], 'budget_exhausted')
        self.assertEqual(self.invoke.call_count, 1)

    def test_uncertain_call_recovers_without_duplicate_even_with_retry(self):
        self.invoke.side_effect = providers.ProviderUncertain('unknown actor')
        case = self.start()
        result = self.run_case(case)
        self.assertEqual(result['status'], 'reconciliation_required')
        with patch.object(providers, 'recover', side_effect=providers.ProviderUncertain('still unknown')) as recover:
            self.assertEqual(self.run_case(case, retry=True)['status'], 'reconciliation_required')
            self.assertEqual(recover.call_count, 1)
        self.assertEqual(self.invoke.call_count, 1)
        self.invoke.side_effect = self.fake
        response = self.fake({}, 'review', self.project / 'fake', 1)
        with patch.object(providers, 'recover', return_value=response) as recover:
            result = self.run_case(case)
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(recover.call_count, 1)
        self.assertEqual(self.invoke.call_count, 3)

    def test_running_receipt_recovery_and_completed_review_reuse(self):
        case = self.start(max_calls=4)
        original = self.fake
        def fail_second(*args):
            if len(self.prompts) == 1:
                raise providers.ProviderUncertain('interruption')
            return original(*args)
        self.invoke.side_effect = fail_second
        self.run_case(case)
        state = debate.status(self.project, case['id'])
        state['calls'][1]['status'] = 'running'
        atomic(debate._directory(self.project, case['id']) / 'case.json', state)
        response = original({}, 'review', self.project / 'fake', 1)
        self.invoke.side_effect = original
        with patch.object(providers, 'recover', return_value=response):
            result = self.run_case(case)
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(self.invoke.call_count, 3)
        self.assertEqual(len(result['calls']), 3)

    def test_manual_debate_during_real_worker_leaves_control_unchanged(self):
        env = {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1])}
        command = [sys.executable, '-c', 'from pathlib import Path; from crucible.engine import run; import sys; run(Path(sys.argv[1]), once=True)', str(self.project)]
        worker = subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(lambda: worker.kill() if worker.poll() is None else None)
        deadline = time.monotonic() + 3
        store = Store(self.project)
        self.addCleanup(store.close)
        while not store.attempts() and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertIsNone(worker.poll())
        self.assertTrue(store.attempts())
        before = list(store.db.execute('SELECT key,value FROM state ORDER BY key'))
        result = self.run_case(self.start(evidence=[]))
        self.assertEqual(result['status'], 'complete')
        self.assertIsNone(worker.poll())
        self.assertEqual([tuple(r) for r in before], [tuple(r) for r in store.db.execute('SELECT key,value FROM state ORDER BY key')])
        output, error = worker.communicate(timeout=8)
        self.assertEqual(worker.returncode, 0, error.decode())
        self.assertEqual(len(store.attempts()), 1)


if __name__ == '__main__':
    unittest.main()
