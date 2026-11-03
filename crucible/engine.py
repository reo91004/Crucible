"""Durable, bounded local experiment controller."""
import fcntl
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import time
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path

from . import config, providers
from .evidence import describe
from .job import alive, atomic, identity, live_group
from .store import Store, now


class Halt(RuntimeError):
    """A recorded control or budget boundary, not an experiment result."""


@contextmanager
def _lock(store):
    with (store.root / 'worker.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Another controller holds this project worker lock') from None
        store.set('worker', identity())
        try:
            yield
        finally:
            store.set('worker', None)
            fcntl.flock(lock, fcntl.LOCK_UN)


def _boundary(store):
    action = store.get('control', 'running')
    if action != 'running':
        store.set('state', action)
        raise Halt(action)


def _consistent(cfg, attempt=None):
    current = config.load(Path(cfg['_project']))
    expected = attempt['provenance']['config_sha256'] if attempt else config.fingerprint(cfg)
    if config.fingerprint(current) != expected:
        raise ValueError('Configuration changed during active work')
    if attempt:
        if config.provenance(cfg, attempt['proposal'])['inputs'] != attempt['provenance']['inputs']:
            raise ValueError('Declared inputs changed during active attempt')
        for name, expected_hash in attempt.get('artifact_sha256', {}).items():
            if config._sha(Path(attempt['artifacts'][name])) != expected_hash:
                raise ValueError(f'Artifact changed after execution: {name}')


def _unresolved(store, attempt):
    issues = []
    for stage, record in attempt.get('jobs', {}).items():
        directory = Path(record['path'])
        try:
            result = json.loads((directory / 'result.json').read_text())
            valid = result.get('token') == record['token']
            if 'cleanup incomplete' in result.get('error', '').lower():
                child = json.loads((directory / 'child.json').read_text())
                valid = valid and not live_group(child['identity']['pid'])
        except (OSError, ValueError, KeyError, TypeError):
            valid = False
        if not valid:
            issues.append(f'{attempt["id"]}:{stage}:unresolved execution')
    for row in store.db.execute("SELECT stage FROM calls WHERE attempt=? AND status='running'", (attempt['id'],)):
        issues.append(f'{attempt["id"]}:{row["stage"]}:unresolved provider')
    return issues


def _job(store, attempt, stage, command, timeout, stdin=None):
    jobs = attempt.setdefault('jobs', {})
    record = jobs.get(stage)
    if record is None:
        _boundary(store)
        directory = Path(attempt['path']) / 'jobs' / f'{stage}-{uuid.uuid4().hex}'
        directory.mkdir(parents=True)
        record = {'path': str(directory), 'token': uuid.uuid4().hex, 'launch_time': time.time()}
        request = {'token': record['token'], 'command': command, 'cwd': attempt['path'], 'timeout': timeout}
        if stdin is not None:
            request['stdin'] = stdin
        atomic(directory / 'request.json', request)
        jobs[stage] = record
        store.save_attempt(attempt)
        # Persist launch intent before spawn; missing receipts after a crash require reconciliation.
        with (directory / 'runner.log').open('ab') as output:
            process = subprocess.Popen([sys.executable, str(Path(__file__).with_name('job.py')), str(directory)],
                                       stdout=output, stderr=output, stdin=subprocess.DEVNULL, start_new_session=True)
        record['runner'] = identity(process.pid)
        threading.Thread(target=process.wait, daemon=True).start()
        store.save_attempt(attempt)
    directory = Path(record['path'])
    while True:
        result_path = directory / 'result.json'
        if result_path.exists():
            result = json.loads(result_path.read_text())
            if result.get('token') != record['token']:
                raise ValueError('Job receipt token mismatch')
            record['result'] = result
            store.save_attempt(attempt)
            if result['returncode'] != 0 or result.get('error') or result['cancelled'] or result['timed_out']:
                raise RuntimeError(f'{stage} failed: {result}; logs: {directory}')
            return directory, result
        started = directory / 'started.json'
        who = record.get('runner')
        if started.exists():
            receipt = json.loads(started.read_text())
            if receipt.get('token') != record['token']:
                raise ValueError('Job start token mismatch')
            who = receipt['identity']
        if not alive(who):
            # Allow the newly spawned runner to publish its identity, but never relaunch it.
            if time.time() - record['launch_time'] > 2:
                store.set('state', 'reconciliation_required')
                raise Halt('Job outcome unknown; reconcile verified receipts before continuing')
        _boundary(store)
        time.sleep(.05)


def _manifest(attempt):
    return {key: attempt[key] for key in ('id', 'project', 'path', 'proposal', 'command', 'artifacts', 'returncode', 'provenance')}


def _evaluator(value):
    if not isinstance(value, dict) or value.get('event') not in {'normal', 'anomaly', 'regression', 'stagnation', 'theory_conflict', 'insufficient', 'error'}:
        raise ValueError('Evaluator must return a valid event')
    if not isinstance(value.get('summary'), str) or not value['summary'].strip() or not isinstance(value.get('comparison_key'), str) or not value['comparison_key'].strip():
        raise ValueError('Evaluator requires summary and comparison_key')
    if not isinstance(value.get('metrics'), dict) or any(type(v) not in (int, float) or not math.isfinite(v) for v in value['metrics'].values()):
        raise ValueError('Evaluator metrics must be finite numbers')
    return value


def _review(value, evidence_ids):
    if not isinstance(value, dict):
        raise ValueError('Review must be a JSON object')
    for field in ('supported_findings', 'concerns', 'evidence_refs'):
        if not isinstance(value.get(field), list) or any(not isinstance(v, str) for v in value[field]):
            raise ValueError(f'Review requires string list {field}')
    if not value['evidence_refs'] or not set(value['evidence_refs']) <= set(evidence_ids):
        raise ValueError('Review must cite actual packet evidence IDs')
    test = value.get('next_test')
    if type(value.get('needs_rebuttal')) is not bool or not isinstance(test, dict) or not isinstance(test.get('hypothesis'), str) or not test['hypothesis'].strip() or not isinstance(test.get('predictions'), dict):
        raise ValueError('Review requires needs_rebuttal and a concrete next_test')
    return value


def _call(store, cfg, attempt, stage, model, prompt, validator):
    _consistent(cfg, attempt)
    previous = store.call(attempt['id'], stage)
    if previous and previous['status'] == 'complete':
        return previous['data']['parsed']
    if previous and previous['status'] in {'running', 'recovered'}:
        data = previous['data']
        try:
            data['response'] = providers.recover(Path(data['path']))
            data['parsed'] = validator(json.loads(data['response']['text']))
            store.save_call(previous['id'], 'complete', data)
            return data['parsed']
        except providers.ProviderUncertain as error:
            store.set('state', 'reconciliation_required')
            raise Halt(str(error)) from error
        except Exception as error:
            data['error'] = repr(error)
            store.save_call(previous['id'], 'failed', data)
            raise
    if previous and previous['status'] == 'failed':
        raise RuntimeError(f'Model stage {stage} is {previous["status"]}; explicit retry required')
    _boundary(store)
    count = store.db.execute('SELECT count(*) FROM calls').fetchone()[0]
    if count >= cfg['limits']['max_calls']:
        store.set('state', 'call_budget')
        raise Halt('Model call budget exhausted')
    data = {'model': model, 'prompt': prompt, 'created': now()}
    identifier = store.new_call(attempt['id'], stage, data)
    directory = store.root / 'calls' / str(identifier)
    data['path'] = str(directory)
    store.save_call(identifier, 'running', data)
    try:
        response = providers.invoke(model, prompt, directory, cfg['limits']['call_timeout_seconds'])
        data['response'] = response
        data['parsed'] = validator(json.loads(response['text']))
    except providers.ProviderUncertain as error:
        store.set('state', 'reconciliation_required')
        raise Halt(str(error)) from error
    except Exception as error:
        data['error'] = repr(error)
        store.save_call(identifier, 'failed', data)
        raise
    store.save_call(identifier, 'complete', data)
    return data['parsed']


def _decide(store, cfg, attempt):
    evaluation = attempt['evaluation']
    event = evaluation['event']
    if event in {'normal', 'insufficient', 'error'} and 'next' in evaluation:
        decision = config.validate_decision(cfg, evaluation['next'])
        if event != 'normal':
            store.event('explicit_recovery', {'attempt': attempt['id'], 'evaluation': evaluation})
        return decision
    if event in {'insufficient', 'error'}:
        raise ValueError(f'Evaluator {event}: concrete recovery decision required')
    artifact_evidence = {}
    for name, filename in attempt['artifacts'].items():
        item = describe(filename)
        artifact_evidence[f'artifact:{attempt["id"]}:{name}'] = item
    packet = {'goal': cfg['goal'], 'evidence': {f'attempt:{attempt["id"]}': _manifest(attempt), f'evaluation:{attempt["id"]}': evaluation},
              'allowed_experiments': {name: {'parameters': spec['parameters']} for name, spec in cfg['experiments'].items()}}
    packet['evidence'].update(artifact_evidence)
    for previous in store.attempts():
        if previous['id'] < attempt['id'] and 'evaluation' in previous:
            packet['evidence'][f'evaluation:{previous["id"]}'] = previous['evaluation']
    packet['evidence_limits'] = 'Only packet evidence is accessible. Omitted artifact contents are unavailable: qualify findings and request a concrete diagnostic if they are needed.'
    models = cfg['models']
    planner = next((model for model in models if model['name'] == cfg.get('planner')), None)
    if planner is None:
        raise ValueError('Configure a planner model for this evaluator event')
    investigated = event != 'normal'
    reviews = []
    if investigated:
        if len(models) < 2:
            raise ValueError('Independent investigation requires at least two configured models')
        evidence_hash = hashlib.sha256(json.dumps(packet, sort_keys=True).encode()).hexdigest()
        attempt['case'] = {'comparison_key': evaluation['comparison_key'], 'event': event, 'evidence_sha256': evidence_hash}
        store.save_attempt(attempt)
        instruction = 'Independently review only this evidence. Return JSON with supported_findings:[str], concerns:[str], evidence_refs:[actual evidence IDs], needs_rebuttal:bool, next_test:{hypothesis:str,predictions:object}.\n'
        prompt = instruction + json.dumps(packet, sort_keys=True)
        validate = lambda value: _review(value, packet['evidence'])
        for model in models:
            reviews.append({'model': model['name'], 'review': _call(store, cfg, attempt, 'review:' + model['name'], model, prompt, validate)})
        for round_number in range(cfg['limits']['debate_rounds']):
            if not any(item['review']['needs_rebuttal'] for item in reviews):
                break
            rebuttal_prompt = instruction + json.dumps({'packet': packet, 'previous_opinions': reviews,
                                                        'unresolved_questions': [c for item in reviews for c in item['review']['concerns']]}, sort_keys=True)
            additions = []
            for model in models:
                additions.append({'model': model['name'], 'review': _call(store, cfg, attempt, f'rebuttal:{round_number}:{model["name"]}', model, rebuttal_prompt, validate)})
            reviews = additions
        attempt['reviews'] = reviews
        store.save_attempt(attempt)
    def validate_decision(value):
        decision = config.validate_decision(cfg, value)
        if investigated and decision['action'] == 'run':
            if not isinstance(decision.get('hypothesis'), str) or not decision['hypothesis'].strip() or not isinstance(decision.get('predictions'), dict) or not decision['predictions']:
                raise ValueError('Investigated run requires hypothesis and predictions')
        return decision
    # Every completed opinion is supplied to synthesis, including independent first-round reviews.
    completed = [json.loads(row[0])['parsed'] for row in store.db.execute('SELECT data FROM calls WHERE attempt=? AND status=? ORDER BY id', (attempt['id'], 'complete'))]
    prompt = 'Choose the next registered experiment or stop/hold. Return one JSON decision with action, reason; run also has experiment and parameters. Investigated runs require hypothesis and predictions.\n' + json.dumps({'packet': packet, 'complete_reviews': completed}, sort_keys=True)
    return _call(store, cfg, attempt, 'planner', planner, prompt, validate_decision)


def _advance(store, cfg, attempt):
    spec = cfg['experiments'][attempt['proposal']['experiment']]
    _consistent(cfg, attempt)
    if 'returncode' not in attempt:
        _, result = _job(store, attempt, 'experiment', attempt['command'], spec.get('timeout_seconds'))
        _consistent(cfg, attempt)
        artifacts = {}
        for name in spec['artifacts']:
            path = (Path(attempt['path']) / name).resolve()
            if not path.is_relative_to(Path(attempt['path'])) or not path.is_file():
                raise ValueError(f'Missing or escaping artifact: {name}')
            artifacts[name] = str(path)
        artifact_hashes = {name: config._sha(Path(filename)) for name, filename in artifacts.items()}
        attempt.update(returncode=result['returncode'], artifacts=artifacts, artifact_sha256=artifact_hashes, stage='evaluator')
        store.save_attempt(attempt)
    _boundary(store)
    if 'evaluation' not in attempt:
        command = config.render_command(cfg, attempt['proposal'], Path(attempt['path']), evaluator=True)
        history = [{'id': old['id'], 'proposal': old['proposal'], 'evaluation': old['evaluation']} for old in store.attempts() if old['id'] < attempt['id'] and 'evaluation' in old]
        directory, _ = _job(store, attempt, 'evaluator', command, cfg['evaluator']['timeout_seconds'], json.dumps({'attempt': _manifest(attempt), 'history': history}))
        _consistent(cfg, attempt)
        value = _evaluator(json.loads((directory / 'stdout.log').read_text()))
        if 'next' in value:
            config.validate_decision(cfg, value['next'])
        attempt.update(evaluation=value, stage='decision')
        atomic(Path(attempt['path']) / 'evaluation.json', value)
        store.save_attempt(attempt)
        store.event('evaluation', {'attempt': attempt['id'], 'evaluation': value})
    _boundary(store)
    for name, filename in attempt['artifacts'].items():
        if config._sha(Path(filename)) != attempt['artifact_sha256'][name]:
            raise ValueError(f'Artifact changed after experiment validation: {name}')
    if 'decision' not in attempt:
        attempt['decision'] = _decide(store, cfg, attempt)
        _consistent(cfg, attempt)
        store.save_attempt(attempt)
    _consistent(cfg, attempt)
    _boundary(store)
    attempt.update(status='complete', stage='complete', finished=now())
    store.complete(attempt)


def run(project: Path, *, once=False):
    project = Path(project).resolve()
    cfg = config.load(project)
    store = Store(project)
    acquired = False
    try:
        with _lock(store):
            acquired = True
            if store.get('state') in {'failed', 'reconciliation_required'}:
                return _status(store) | {'worker': None}
            store.set('state', 'running')
            while True:
                attempt = None
                try:
                    _boundary(store)
                    attempts = store.attempts()
                    pending = [a for a in attempts if a['status'] == 'running']
                    if pending:
                        attempt = pending[-1]
                        _consistent(cfg, attempt)
                    else:
                        _consistent(cfg)
                        decision = config.validate_decision(cfg, store.get('next_decision', cfg['initial']))
                        if decision['action'] != 'run':
                            store.set('state', 'stopped' if decision['action'] == 'stop' else 'held')
                            break
                        if len(attempts) >= cfg['limits']['max_runs']:
                            store.set('state', 'run_budget')
                            break
                        identifier = len(attempts) + 1
                        path = store.root / 'attempts' / str(identifier)
                        path.mkdir(parents=True, exist_ok=True)
                        provenance = config.provenance(cfg, decision)
                        atomic(path / 'config.json', {key: value for key, value in cfg.items() if not key.startswith('_')})
                        attempt = {'id': identifier, 'project': str(project), 'path': str(path), 'proposal': decision,
                                   'command': config.render_command(cfg, decision, path), 'provenance': provenance,
                                   'status': 'running', 'stage': 'experiment', 'created': now()}
                        snapshots = path / 'inputs'
                        for name in provenance['inputs']:
                            target = snapshots / name
                            target.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copyfile(project / name, target)
                        atomic(path / 'manifest.json', attempt)
                        store.save_attempt(attempt)
                        store.event('attempt_started', {'attempt': identifier, 'proposal': decision})
                    _advance(store, cfg, attempt)
                    if once:
                        store.set('state', 'ready')
                        break
                except Halt as error:
                    store.set('last_error', str(error))
                    break
                except Exception as error:
                    unresolved = _unresolved(store, attempt) if attempt is not None else []
                    if attempt is not None and not unresolved:
                        attempt.update(status='failed', error=repr(error))
                        store.save_attempt(attempt)
                    store.set('state', 'reconciliation_required' if unresolved else 'failed')
                    store.set('last_error', repr(error))
                    store.event('failure', {'attempt': attempt['id'] if attempt else None, 'error': repr(error)})
                    break
    finally:
        if acquired:
            _write_report(store)
        store.close()
    return status(project)


def _status(store):
    attempts = store.attempts()
    worker = store.get('worker')
    current = None
    if attempts:
        current = {key: value for key, value in attempts[-1].items() if key != 'provenance'}
    return {'state': store.get('state', 'ready'), 'control': store.get('control', 'running'),
            'worker': worker if alive(worker) else None, 'current_attempt': current,
            'run_count': len(attempts), 'call_count': store.db.execute('SELECT count(*) FROM calls').fetchone()[0],
            'next_decision': store.get('next_decision'), 'last_error': store.get('last_error')}


def status(project):
    store = Store(project)
    try:
        return _status(store)
    finally:
        store.close()


def decide(project, decision):
    """Record explicit operator steering without replacing previous evidence."""
    cfg = config.load(project)
    config.validate_decision(cfg, decision)
    store = Store(project)
    try:
        with _lock(store):
            if any(attempt['status'] == 'running' for attempt in store.attempts()):
                raise ValueError('Resolve the active attempt before changing the next decision')
            store.set('next_decision', decision)
            store.set('control', 'running')
            store.set('state', 'ready')
            store.event('operator_decision', {'decision': decision})
        return _status(store)
    finally:
        store.close()


def control(project, action):
    if action not in {'pause', 'resume', 'stop', 'cancel'}:
        raise ValueError('Control must be pause/resume/stop/cancel')
    store = Store(project)
    try:
        flag = {'pause': 'paused', 'resume': 'running', 'stop': 'stopped', 'cancel': 'cancelled'}[action]
        store.set('control', flag)
        store.event('control', {'action': action})
        if action == 'cancel':
            for attempt in store.attempts():
                if attempt['status'] == 'running':
                    for job in attempt.get('jobs', {}).values():
                        (Path(job['path']) / 'cancel').touch()
        if store.get('state') not in {'failed', 'reconciliation_required'}:
            store.set('state', flag)
        return _status(store)
    finally:
        store.close()


def retry(project):
    store = Store(project)
    try:
        with _lock(store):
            if store.get('state') != 'failed':
                raise ValueError('Retry requires a failed stage; reconcile uncertain jobs first')
            attempts = store.attempts()
            if not attempts or attempts[-1]['status'] != 'failed':
                raise ValueError('No failed attempt to retry')
            attempt = attempts[-1]
            unresolved = _unresolved(store, attempt)
            if unresolved:
                store.set('state', 'reconciliation_required')
                raise ValueError('Retry refused: unresolved execution ownership; reconcile first')
            if attempt['stage'] == 'experiment':
                # A rerun is a new budgeted attempt, preserving all failed experiment evidence.
                store.set('next_decision', attempt['proposal'])
            else:
                stage = attempt['stage']
                if stage in attempt.get('jobs', {}):
                    old = attempt['jobs'].pop(stage)
                    attempt.setdefault('previous_jobs', []).append(old)
                with store.db:
                    store.db.execute("UPDATE calls SET status='retry_authorized' WHERE attempt=? AND status='failed'", (attempt['id'],))
                attempt.update(status='running')
                attempt.pop('error', None)
                store.save_attempt(attempt)
            store.set('state', 'ready')
            store.set('control', 'running')
            store.event('retry', {'attempt': attempt['id'], 'stage': attempt['stage']})
        return _status(store)
    finally:
        _write_report(store)
        store.close()


def reconcile(project):
    store = Store(project)
    try:
        with _lock(store):
            if store.get('state') == 'failed':
                raise ValueError('Failed stages require explicit retry, not reconcile')
            unresolved = []
            failed = False
            for attempt in store.attempts():
                if attempt['status'] != 'running':
                    continue
                rows = store.db.execute("SELECT id,data FROM calls WHERE attempt=? AND status='running'", (attempt['id'],)).fetchall()
                for row in rows:
                    data = json.loads(row['data'])
                    try:
                        data['response'] = providers.recover(Path(data['path']))
                        store.save_call(row['id'], 'recovered', data)
                    except providers.ProviderUncertain as error:
                        unresolved.append(f'{attempt["id"]}:provider:{error}')
                    except providers.ProviderError as error:
                        data['error'] = repr(error)
                        store.save_call(row['id'], 'failed', data)
                        attempt.update(status='failed', error='Recovered provider failure; explicit retry required')
                        store.save_attempt(attempt)
                        failed = True
                for stage, record in attempt.get('jobs', {}).items():
                    directory = Path(record['path'])
                    if (directory / 'result.json').exists():
                        receipt = json.loads((directory / 'result.json').read_text())
                        if receipt.get('token') != record['token']:
                            unresolved.append(f'{attempt["id"]}:{stage}:wrong token')
                        elif 'cleanup incomplete' in receipt.get('error', '').lower():
                            child = json.loads((directory / 'child.json').read_text())
                            if live_group(child['identity']['pid']):
                                unresolved.append(f'{attempt["id"]}:{stage}:process group still active')
                        continue
                    started = directory / 'started.json'
                    who = json.loads(started.read_text()) if started.exists() else None
                    if not (who and who.get('token') == record['token'] and alive(who.get('identity'))):
                        unresolved.append(f'{attempt["id"]}:{stage}:unknown outcome')
            store.set('state', 'reconciliation_required' if unresolved else ('failed' if failed else 'ready'))
            store.set('last_error', '; '.join(unresolved) if unresolved else None)
            store.event('reconcile', {'unresolved': unresolved})
        return _status(store)
    finally:
        store.close()


def events(project, *, all_events=False):
    store = Store(project)
    try:
        query = 'SELECT * FROM events' + ('' if all_events else ' WHERE acknowledged=0') + ' ORDER BY id'
        return [dict(row) | {'data': json.loads(row['data'])} for row in store.db.execute(query)]
    finally:
        store.close()


def ack(project, event_id):
    store = Store(project)
    try:
        with store.db:
            cursor = store.db.execute('UPDATE events SET acknowledged=1 WHERE id=?', (event_id,))
            if not cursor.rowcount:
                raise ValueError(f'Unknown event ID {event_id}')
        return {'id': event_id, 'acknowledged': True}
    finally:
        store.close()


def _write_report(store):
    lines = ['# Crucible report', '', 'Local software evidence only. Model agreement is not scientific validation.', '',
             f'State: {store.get("state", "ready")}', f'Last error: {store.get("last_error")}', '']
    for attempt in store.attempts():
        display = {key: value for key, value in attempt.items() if key not in ('provenance', 'jobs')}
        display['provenance_file'] = str(Path(attempt['path']) / 'manifest.json')
        lines.extend([f'## Attempt {attempt["id"]}: {attempt["status"]}', '',
                      f'Proposal: {attempt["proposal"]["reason"]}',
                      f'Evaluation: {attempt.get("evaluation", {}).get("summary", "Not completed")}',
                      f'Next: {attempt.get("decision", {}).get("reason", "Not decided")}',
                      '', '```json', json.dumps(display, indent=2), '```', ''])
    for row in store.db.execute('SELECT * FROM calls ORDER BY id'):
        lines.extend([f'## Model call {row["id"]}: {row["stage"]} / {row["status"]}', '', '```json', json.dumps(json.loads(row['data']), indent=2), '```', ''])
    content = '\n'.join(lines)
    (store.root / 'REPORT.md').write_text(content)
    return content


def report(project):
    store = Store(project)
    try:
        return _write_report(store)
    finally:
        store.close()
