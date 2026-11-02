"""Explicit post-update retirement of the three known prototype key files."""
import hashlib
import json
from pathlib import Path

import win_support as win


def retire(manifest, archive_dir):
    if win.connection_active():
        raise RuntimeError('Disconnect the phone app yourself before retiring old test keys.')
    entries = json.loads(Path(manifest).read_text())
    archive_dir = Path(archive_dir)
    archive_dir.mkdir(parents=True, exist_ok=True)
    results = []
    for entry in entries:
        source = Path(entry['path'])
        if not source.is_absolute() or source.name not in ('pairing.json', 'gateway.json'):
            raise ValueError('Invalid legacy key manifest')
        if not source.exists():
            continue
        data = source.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if digest != entry['sha256']:
            raise RuntimeError('An old key file changed; left it untouched: ' + str(source))
        archive = archive_dir / (hashlib.sha256(str(source).encode()).hexdigest() + '.dpapi')
        encrypted = win.protect(data)
        temporary = archive.with_suffix('.new')
        temporary.write_bytes(encrypted)
        if win.protect(temporary.read_bytes(), decrypt=True) != data:
            raise RuntimeError('Could not verify encrypted recovery copy; original kept')
        temporary.replace(archive)
        # Recheck immediately before unlink. Never recursively delete directories.
        if hashlib.sha256(source.read_bytes()).hexdigest() != digest:
            raise RuntimeError('An old key file changed; original kept')
        source.unlink()
        results.append(str(source))
    return results
