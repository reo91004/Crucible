"""Bounded text views whose hashes still identify complete artifacts."""

import hashlib
from pathlib import Path


def describe(path, limit=65536):
    path = Path(path)
    before = path.stat()
    digest = hashlib.sha256()
    prefix = bytearray()
    size = 0
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
            size += len(block)
            if len(prefix) <= limit:
                prefix.extend(block[:limit + 1 - len(prefix)])
    after = path.stat()
    if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
        raise ValueError('Artifact changed while snapshotting')
    value = {'sha256': digest.hexdigest(), 'bytes': size}
    if size > limit:
        value['content_omitted'] = f'Exceeds {limit}-byte packet limit; only metadata and evaluator summary are available'
    else:
        try:
            value['text'] = prefix.decode('utf-8')
        except UnicodeDecodeError:
            value['content_omitted'] = 'Binary artifact; only metadata and evaluator summary are available'
    return value
