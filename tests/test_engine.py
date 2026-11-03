import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from crucible import config, engine
from crucible.job import identity
from crucible.store import Store


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)
        (self.project / '.crucible').mkdir()
        (self.project / 'objective.py').write_text('import json,sys\nfrom pathlib import Path\nPath("metrics.json").write_text(json.dumps({"loss":float(sys.argv[1])**2}))\n')
        (self.project / 'evaluate.py').write_text('import json,sys\np=json.load(sys.stdin)\na=p["attempt"]\nm=json.load(open(a["artifacts"]["metrics.json"]))\nprint(json.dumps({"event":"normal","summary":"Evaluated","metrics":m,"comparison_key":"fixture","next":{"action":"stop","reason":"Complete"}}))\n')
        self.cfg = {'version': 1, 'goal': 'Test objective', 'limits': {'max_runs': 3, 'max_calls': 8, 'call_timeout_seconds': 5, 'debate_rounds': 1},
                    'models': [], 'initial': {'action': 'run', 'experiment': 'objective', 'parameters': {'x': 2, 'seed': 7}, 'reason': 'Baseline'},
                    'experiments': {'objective': {'command': ['{python}', '{project}/objective.py', '{x}'], 'parameters': {'x': {'type': 'number', 'min': -10, 'max': 10}, 'seed': {'type': 'integer', 'min': 0, 'max': 100}}, 'artifacts': ['metrics.json'], 'inputs': ['objective.py'], 'timeout_seconds': 3}},
                    'evaluator': {'command': ['{python}', '{project}/evaluate.py'], 'inputs': ['evaluate.py'], 'timeout_seconds': 3}}
        self.write_config()

    def write_config(self):
        (self.project / '.crucible/config.json').write_text(json.dumps(self.cfg))

    def evaluator(self, value):
        (self.project / 'evaluate.py').write_text('import json,sys\njson.load(sys.stdin)\nprint(' + repr(json.dumps(value)) + ')\n')

    def evaluation(self, event='normal', **extra):
        return {'event': event, 'summary': 'Test result', 'metrics': {'loss': 4}, 'comparison_key': 'fixture', **extra}

    def models(self):
        self.cfg['models'] = [{'name': name, 'backend': backend, 'command': [name], 'model': 'explicit-model'} for name, backend in [('gpt', 'opencode'), ('claude', 'claude')]]
        self.cfg['planner'] = 'gpt'
        self.write_config()

    def review(self, rebuttal=False):
        return {'supported_findings': ['Recorded loss'], 'concerns': [], 'evidence_refs': ['evaluation:1'], 'needs_rebuttal': rebuttal, 'next_test': {'hypothesis': 'objective', 'predictions': {'loss': 0}}}

    def response(self, value):
        return {'text': json.dumps(value), 'session_id': 'test', 'requested_model': 'explicit-model', 'observed_model': None, 'usage': {}, 'backend': 'test'}

    def test_real_experiment_and_outbox(self):
        result = engine.run(self.project)
        self.assertEqual(result['state'], 'stopped')
        self.assertEqual((result['run_count'], result['call_count']), (1, 0))
        attempt = result['current_attempt']
        self.assertEqual(attempt['evaluation']['metrics'], {'loss': 4})
        self.assertEqual(Path(attempt['path'], 'inputs/objective.py').read_bytes(), (self.project / 'objective.py').read_bytes())
        self.assertTrue(Path(attempt['path'], 'jobs').is_dir())
        notifications = engine.events(self.project)
        engine.ack(self.project, notifications[0]['id'])
        self.assertEqual(len(engine.events(self.project)), len(notifications) - 1)
        self.assertIn('Attempt 1', engine.report(self.project))

    def test_failure_and_partial_artifacts_never_evaluate(self):
        for source in ['raise SystemExit(7)\n', 'pass\n']:
            with self.subTest(source=source):
                (self.project / 'objective.py').write_text(source)
                result = engine.run(self.project)
                self.assertEqual(result['state'], 'failed')
                self.assertNotIn('evaluation', result['current_attempt'])
                engine.retry(self.project)
        self.assertEqual(result['run_count'], 2)

    def test_run_budget_counts_failures_and_successes(self):
        self.cfg['limits']['max_runs'] = 2
        self.write_config()
        self.evaluator(self.evaluation(next=self.cfg['initial']))
        result = engine.run(self.project)
        self.assertEqual(result['state'], 'run_budget')
        self.assertEqual(result['run_count'], 2)
        self.assertEqual(engine.run(self.project)['run_count'], 2)

    def test_bad_evaluator(self):
        self.evaluator(self.evaluation(metrics={'loss': float('nan')}))
        result = engine.run(self.project)
        self.assertEqual(result['state'], 'failed')
        self.assertNotIn('decision', result['current_attempt'])

    def test_bad_decision(self):
        self.evaluator(self.evaluation(next={'action': 'run', 'experiment': 'objective', 'parameters': {'x': 90, 'seed': 7}, 'reason': 'bad'}))
        result = engine.run(self.project)
        self.assertEqual(result['state'], 'failed')
        self.assertNotIn('decision', result['current_attempt'])

    def test_independent_review_same_packet_and_synthesis(self):
        self.models()
        self.evaluator(self.evaluation('anomaly'))
        responses = [self.response(self.review()), self.response(self.review()), self.response({'action': 'stop', 'reason': 'Evidence checked'})]
        with patch('crucible.providers.invoke', side_effect=responses) as invoke:
            result = engine.run(self.project)
        self.assertEqual(result['state'], 'stopped')
        self.assertEqual(result['call_count'], 3)
        self.assertEqual(invoke.call_args_list[0].args[1], invoke.call_args_list[1].args[1])
        self.assertIn('complete_reviews', invoke.call_args_list[2].args[1])
        self.assertIn('artifact:1:metrics.json', invoke.call_args_list[0].args[1])
        self.assertIn('evidence_limits', invoke.call_args_list[0].args[1])

    def test_model_failure_requires_retry_reuses_successful_calls(self):
        self.models()
        self.evaluator(self.evaluation('regression'))
        with patch('crucible.providers.invoke', side_effect=[self.response(self.review()), RuntimeError('transport failure')]):
            result = engine.run(self.project)
        self.assertEqual(result['state'], 'failed')
        with patch('crucible.providers.invoke') as invoke:
            self.assertEqual(engine.run(self.project)['state'], 'failed')
            invoke.assert_not_called()
        engine.retry(self.project)
        with patch('crucible.providers.invoke', side_effect=[self.response(self.review()), self.response({'action': 'stop', 'reason': 'Checked'})]) as invoke:
            result = engine.run(self.project)
        self.assertEqual(result['state'], 'stopped')
        self.assertEqual(result['run_count'], 1)
        self.assertEqual(result['call_count'], 4)
        self.assertEqual(invoke.call_count, 2)

    def test_call_budget_never_invokes_over_limit(self):
        self.models()
        self.cfg['limits']['max_calls'] = 1
        self.write_config()
        self.evaluator(self.evaluation('stagnation'))
        with patch('crucible.providers.invoke', return_value=self.response(self.review())) as invoke:
            result = engine.run(self.project)
        self.assertEqual(result['state'], 'call_budget')
        self.assertEqual(invoke.call_count, 1)
        with patch('crucible.providers.invoke') as invoke:
            engine.run(self.project)
            invoke.assert_not_called()

    def test_invalid_review_blocks_synthesis(self):
        self.models()
        self.evaluator(self.evaluation('theory_conflict'))
        review = self.review()
        review['evidence_refs'] = ['invented']
        with patch('crucible.providers.invoke', return_value=self.response(review)) as invoke:
            result = engine.run(self.project)
        self.assertEqual(result['state'], 'failed')
        self.assertEqual(invoke.call_count, 1)

    def test_evaluator_retry_reuses_experiment(self):
        value = self.evaluation(next={'action': 'stop', 'reason': 'Recovered'})
        (self.project / 'evaluate.py').write_text('import json,sys\nfrom pathlib import Path\njson.load(sys.stdin)\np=Path("evaluated.once")\nif not p.exists():\n p.touch()\n print("partial")\nelse:\n print(' + repr(json.dumps(value)) + ')\n')
        result = engine.run(self.project)
        self.assertEqual(result['state'], 'failed')
        self.assertEqual(result['current_attempt']['stage'], 'evaluator')
        job_path = result['current_attempt']['jobs']['experiment']['path']
        engine.retry(self.project)
        result = engine.run(self.project)
        self.assertEqual(result['state'], 'stopped')
        self.assertEqual(result['run_count'], 1)
        self.assertEqual(result['current_attempt']['jobs']['experiment']['path'], job_path)
        self.assertEqual(len(result['current_attempt']['previous_jobs']), 1)

    def test_rebuttal_is_bounded_and_planner_gets_all_opinions(self):
        self.models()
        self.evaluator(self.evaluation('anomaly'))
        responses = [self.response(self.review(True)) for _ in range(4)]
        responses.append(self.response({'action': 'stop', 'reason': 'Investigated'}))
        with patch('crucible.providers.invoke', side_effect=responses) as invoke:
            result = engine.run(self.project)
        self.assertEqual(result['state'], 'stopped')
        self.assertEqual(result['call_count'], 5)
        self.assertEqual(invoke.call_args_list[2].args[1], invoke.call_args_list[3].args[1])
        prompt = json.loads(invoke.call_args_list[4].args[1].split('\n', 1)[1])
        self.assertEqual(len(prompt['complete_reviews']), 4)

    def test_input_mutation_blocks_evaluation(self):
        (self.project / 'objective.py').write_text('from pathlib import Path\nPath("metrics.json").write_text("{\\"loss\\":4}")\nPath(__file__).write_text("changed")\n')
        result = engine.run(self.project)
        self.assertEqual(result['state'], 'failed')
        self.assertIn('inputs changed', result['last_error'])
        self.assertNotIn('evaluation', result['current_attempt'])

    def test_evaluator_cannot_silently_mutate_evidence(self):
        value = self.evaluation(next={'action': 'stop', 'reason': 'Done'})
        (self.project / 'evaluate.py').write_text('import json,sys\nfrom pathlib import Path\na=json.load(sys.stdin)["attempt"]\nPath(a["artifacts"]["metrics.json"]).write_text("changed")\nprint(' + repr(json.dumps(value)) + ')\n')
        result = engine.run(self.project)
        self.assertEqual(result['state'], 'failed')
        self.assertIn('Artifact changed', result['last_error'])
        self.assertNotIn('decision', result['current_attempt'])

    def test_investigated_run_requires_falsifiable_proposal(self):
        self.models()
        self.evaluator(self.evaluation('anomaly'))
        with patch('crucible.providers.invoke', side_effect=[self.response(self.review()), self.response(self.review()), self.response(self.cfg['initial'])]):
            result = engine.run(self.project)
        self.assertEqual(result['state'], 'failed')
        self.assertIn('hypothesis and predictions', result['last_error'])
        self.assertEqual(result['run_count'], 1)

    def worker(self):
        process = subprocess.Popen([sys.executable, '-c', 'from pathlib import Path; from crucible.engine import run; run(Path(' + repr(str(self.project)) + '))'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: process.poll() is None and process.kill())
        return process

    def wait_for(self, predicate):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(.03)
        self.fail('Timed out waiting for test condition')

    def slow_experiment(self, seconds=1):
        (self.project / 'objective.py').write_text('import time\nfrom pathlib import Path\ntime.sleep(' + str(seconds) + ')\nPath("metrics.json").write_text("{\\"loss\\":4}")\n')

    def test_duplicate_controller_pause_resume_and_receipts(self):
        self.slow_experiment()
        process = self.worker()
        self.wait_for(lambda: list((self.project / '.crucible/attempts').glob('*/jobs/*/started.json')))
        with self.assertRaisesRegex(RuntimeError, 'Another controller'):
            engine.run(self.project)
        engine.control(self.project, 'pause')
        process.wait(timeout=5)
        result = engine.status(self.project)
        self.assertEqual(result['state'], 'paused')
        engine.control(self.project, 'resume')
        result = engine.run(self.project)
        self.assertEqual(result['state'], 'stopped')
        self.assertEqual(result['run_count'], 1)

    def test_crashed_worker_recovers_completed_detached_job(self):
        self.slow_experiment(.4)
        process = self.worker()
        self.wait_for(lambda: list((self.project / '.crucible/attempts').glob('*/jobs/*/started.json')))
        process.kill()
        process.wait(timeout=5)
        self.wait_for(lambda: list((self.project / '.crucible/attempts').glob('*/jobs/*/result.json')))
        result = engine.run(self.project)
        self.assertEqual(result['state'], 'stopped')
        self.assertEqual(result['run_count'], 1)

    def test_cancel_preserves_failure_and_retry_is_new_run(self):
        self.slow_experiment(1)
        process = self.worker()
        self.wait_for(lambda: list((self.project / '.crucible/attempts').glob('*/jobs/*/child.json')))
        engine.control(self.project, 'cancel')
        process.wait(timeout=5)
        engine.control(self.project, 'resume')
        self.wait_for(lambda: list((self.project / '.crucible/attempts').glob('*/jobs/*/result.json')))
        result = engine.run(self.project)
        self.assertEqual(result['state'], 'failed')
        engine.retry(self.project)
        result = engine.run(self.project)
        self.assertEqual(result['state'], 'stopped')
        self.assertEqual(result['run_count'], 2)

    def test_unknown_launch_never_relaunched_by_reconcile(self):
        self.slow_experiment(.5)
        process = self.worker()
        self.wait_for(lambda: list((self.project / '.crucible/attempts').glob('*/jobs/*/result.json')))
        process.kill()
        process.wait(timeout=5)
        store = Store(self.project)
        attempt = store.attempts()[0]
        attempt.pop('returncode', None)
        attempt.pop('evaluation', None)
        attempt.pop('decision', None)
        attempt.update(status='running', stage='experiment')
        record = attempt['jobs']['experiment']
        record['runner'] = {'pid': 99999999, 'start': 'none', 'boot': 'none'}
        record['launch_time'] = 0
        directory = Path(record['path'])
        (directory / 'started.json').unlink()
        (directory / 'result.json').unlink()
        store.save_attempt(attempt)
        store.set('state', 'ready')
        store.close()
        result = engine.run(self.project)
        self.assertEqual(result['state'], 'reconciliation_required')
        self.assertEqual(engine.reconcile(self.project)['state'], 'reconciliation_required')
        self.assertEqual(engine.run(self.project)['run_count'], 1)


if __name__ == '__main__':
    unittest.main()
