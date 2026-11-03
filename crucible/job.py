"""Detached command runner with atomic receipts, deadlines and cancellation."""
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


def atomic(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + '.tmp')
    with temporary.open('w') as stream:
        json.dump(value, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def identity(pid=None):
    pid = pid or os.getpid()
    if sys.platform == 'darwin':
        result = subprocess.run(['ps', '-p', str(pid), '-o', 'stat=', '-o', 'lstart=', '-o', 'command='], capture_output=True, text=True)
        value = result.stdout.strip()
        if result.returncode or not value or value.startswith('Z'):
            return None
        return {'pid': pid, 'process': value}
    try:
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        if fields[0] == 'Z':
            return None
        return {'pid': pid, 'start': fields[19], 'boot': Path('/proc/sys/kernel/random/boot_id').read_text().strip()}
    except FileNotFoundError:
        return None


def alive(who):
    return bool(who and identity(who['pid']) == who)


def live_group(group):
    result = subprocess.run(['ps', '-e', '-o', 'pid=', '-o', 'pgid=', '-o', 'stat='], capture_output=True, text=True, check=True)
    return [int(parts[0]) for line in result.stdout.splitlines()
            if len(parts := line.split()) >= 3 and int(parts[1]) == group and not parts[2].startswith('Z')]


def terminate_group(process):
    """The leader exiting does not establish that its descendants exited."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + 2
    while live_group(process.pid) and time.monotonic() < deadline:
        time.sleep(.05)
    if live_group(process.pid):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.wait(timeout=2)
    deadline = time.monotonic() + 2
    while live_group(process.pid) and time.monotonic() < deadline:
        time.sleep(.05)
    if live_group(process.pid):
        raise RuntimeError('Process group cleanup incomplete')


def execute(directory):
    directory = Path(directory)
    request = json.loads((directory / 'request.json').read_text())
    atomic(directory / 'started.json', {'token': request['token'], 'identity': identity(), 'time': time.time()})
    result = {'token': request['token'], 'returncode': None, 'cancelled': False, 'timed_out': False}
    with (directory / 'stdout.log').open('wb') as stdout, (directory / 'stderr.log').open('wb') as stderr:
        try:
            process = subprocess.Popen(request['command'], cwd=request['cwd'], stdin=subprocess.PIPE if 'stdin' in request else subprocess.DEVNULL,
                                       stdout=stdout, stderr=stderr, start_new_session=True)
            atomic(directory / 'child.json', {'token': request['token'], 'identity': identity(process.pid)})
            if 'stdin' in request:
                # Evaluator packets are small, but communicate avoids a full stdin pipe deadlock.
                import threading
                def feed():
                    try:
                        process.stdin.write(request['stdin'].encode())
                        process.stdin.close()
                    except BrokenPipeError:
                        pass
                threading.Thread(target=feed, daemon=True).start()
            deadline = time.monotonic() + request['timeout'] if request['timeout'] is not None else None
            while process.poll() is None:
                result['cancelled'] = (directory / 'cancel').exists()
                result['timed_out'] = deadline is not None and time.monotonic() >= deadline
                if result['cancelled'] or result['timed_out']:
                    terminate_group(process)
                    break
                time.sleep(.05)
            result['returncode'] = process.wait()
        except Exception as error:
            result['error'] = repr(error)
        result['finished'] = time.time()
        atomic(directory / 'result.json', result)


if __name__ == '__main__':
    execute(sys.argv[1])
