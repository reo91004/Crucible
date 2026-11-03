import json
from contextlib import closing
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from crucible import engine, job, providers
from crucible.cli import init


def wait_for(check, seconds=6):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(.02)
    raise AssertionError('Test lifecycle event did not arrive')


class RecoveryRegressions(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='crucible-recovery-')
        self.project = Path(self.temporary.name)
        init(self.project)
        self.path = self.project / '.crucible/config.json'

    def tearDown(self):
        self.temporary.cleanup()

    def edit(self, change):
        value = json.loads(self.path.read_text())
        change(value)
        self.path.write_text(json.dumps(value))

    def test_timeout_cleans_term_ignoring_descendant_after_leader_exit(self):
        directory = self.project / 'job'
        directory.mkdir()
        script = directory / 'leader.py'
        child = "import os,signal,time; from pathlib import Path; signal.signal(signal.SIGTERM,signal.SIG_IGN); Path('descendant.pid').write_text(str(os.getpid())); time.sleep(60)"
        script.write_text('import subprocess,sys,time\nsubprocess.Popen([sys.executable,"-c",' + repr(child) + '])\ntime.sleep(60)\n')
        request = {'token': 'group-cleanup', 'command': [sys.executable, str(script)], 'cwd': str(directory), 'timeout': 1}
        job.atomic(directory / 'request.json', request)
        runner = subprocess.Popen([sys.executable, str(Path(job.__file__)), str(directory)])
        try:
            wait_for(lambda: (directory / 'descendant.pid').exists())
            pid = int((directory / 'descendant.pid').read_text())
            runner.wait(timeout=8)
            receipt = json.loads((directory / 'result.json').read_text())
            self.assertTrue(receipt['timed_out'])
            self.assertIsNone(job.identity(pid), 'TERM-ignoring descendant survived timeout receipt')
        finally:
            if runner.poll() is None:
                runner.kill()
                runner.wait()
            if (directory / 'descendant.pid').exists():
                try:
                    os.kill(int((directory / 'descendant.pid').read_text()), signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_paused_live_job_config_change_never_authorizes_duplicate_retry(self):
        script = self.project / 'objective.py'
        script.write_text('import time\ntime.sleep(3)\n' + script.read_text())
        results = []
        worker = threading.Thread(target=lambda: results.append(engine.run(self.project)))
        worker.start()
        try:
            def started():
                jobs = list((self.project / '.crucible/attempts').glob('*/jobs/*/child.json'))
                return jobs[0] if jobs else None
            child_receipt = wait_for(started)
            engine.control(self.project, 'pause')
            worker.join(timeout=3)
            self.assertFalse(worker.is_alive())
            self.edit(lambda c: c.update(goal='Changed during a live paused attempt'))
            engine.control(self.project, 'resume')
            result = engine.run(self.project)
            self.assertEqual(result['state'], 'reconciliation_required')
            self.assertEqual(result['current_attempt']['status'], 'running')
            with self.assertRaises(ValueError):
                engine.retry(self.project)
            self.assertEqual(engine.status(self.project)['run_count'], 1)
        finally:
            engine.control(self.project, 'cancel')
            worker.join(timeout=5)
            if 'child_receipt' in locals():
                wait_for(lambda: (child_receipt.parent / 'result.json').exists())

    def test_evaluator_cannot_change_declared_inputs_and_complete(self):
        script = self.project / 'evaluate.py'
        script.write_text("from pathlib import Path\np=Path(__file__); p.write_text(p.read_text()+'\\n# changed\\n')\n" + script.read_text())
        result = engine.run(self.project)
        self.assertEqual(result['state'], 'failed')
        self.assertIn('inputs changed', result['last_error'])
        self.assertNotEqual(result['current_attempt']['status'], 'complete')

    def test_post_model_changes_are_checked_before_decision_commit(self):
        for changed in ('config', 'input', 'artifact'):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory(prefix='crucible-post-model-') as temp:
                project = Path(temp)
                init(project)
                cfgpath = project / '.crucible/config.json'
                cfg = json.loads(cfgpath.read_text())
                cfg['models'] = [{'name': 'planner', 'backend': 'opencode', 'model': 'test/model'}]
                cfg['planner'] = 'planner'
                cfgpath.write_text(json.dumps(cfg))
                (project / 'evaluate.py').write_text('import json; print(json.dumps({"event":"normal","summary":"Read the evidence","metrics":{"loss":9},"comparison_key":"test"}))')
                def mutate(*_args):
                    if changed == 'config':
                        cfg['goal'] = 'changed while model was running'
                        cfgpath.write_text(json.dumps(cfg))
                    elif changed == 'input':
                        with (project / 'objective.py').open('a') as stream:
                            stream.write('\n# changed\n')
                    else:
                        (project / '.crucible/attempts/1/metrics.json').write_text('{"loss":0}')
                    return {'text': '{"action":"stop","reason":"Model proposal"}'}
                with patch.object(providers, 'invoke', side_effect=mutate):
                    result = engine.run(project)
                self.assertEqual(result['state'], 'failed')
                with closing(sqlite3.connect(project / '.crucible/state.sqlite')) as db:
                    self.assertEqual(db.execute('SELECT count(*) FROM decisions').fetchone()[0], 0)

    def test_uncertain_model_stays_owned_until_recovery_without_duplicate_call(self):
        self.edit(lambda c: (c['evaluator']['command'].append('--review'), c.update(models=[{'name': 'a', 'backend': 'opencode', 'model': 'test/a'}, {'name': 'b', 'backend': 'opencode', 'model': 'test/b'}])))
        with patch.object(providers, 'invoke', side_effect=providers.ProviderUncertain('unknown runner')):
            result = engine.run(self.project)
        self.assertEqual(result['state'], 'reconciliation_required')
        self.assertEqual(result['call_count'], 1)
        with self.assertRaises(ValueError):
            engine.retry(self.project)
        review = {'supported_findings': ['Repeated observation'], 'concerns': [], 'evidence_refs': ['evaluation:2'], 'needs_rebuttal': False, 'next_test': {'hypothesis': 'Different point', 'predictions': {'changed': 'Different loss'}}}
        response = {'text': json.dumps(review)}
        with patch.object(providers, 'recover', return_value=response):
            self.assertEqual(engine.reconcile(self.project)['state'], 'ready')
            def invoke(_model, prompt, *_args):
                return {'text': json.dumps({'action': 'stop', 'reason': 'Test finished'})} if prompt.startswith('Choose ') else response
            with patch.object(providers, 'invoke', side_effect=invoke) as new_calls:
                state = engine.run(self.project)
        self.assertEqual(state['state'], 'stopped', state['last_error'])
        self.assertEqual(state['call_count'], 3)
        self.assertEqual(new_calls.call_count, 2)


if __name__ == '__main__':
    unittest.main()
