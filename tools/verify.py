"""Offline verification of a public build against this source tree."""
import argparse
import hashlib
import json
import marshal
from pathlib import Path
import subprocess
import sys
import types

import pefile
from PyInstaller.archive.readers import CArchiveReader

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / 'src'
sys.path.insert(0, str(SRC))
from backend import HASHES


def normalize(code):
    return code.replace(co_filename='', co_consts=tuple(
        normalize(value) if isinstance(value, types.CodeType) else value
        for value in code.co_consts))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--app', type=Path, default=ROOT / 'dist/iPhone USB Internet')
    app = parser.parse_args().app.resolve()
    exe = app / 'iPhone USB Internet.exe'
    archive = CArchiveReader(str(exe))
    packaged = archive.open_embedded_archive('PYZ.pyz')
    count = 0
    for source in SRC.glob('*.py'):
        name = source.stem
        if name == 'app':
            code = marshal.loads(archive.extract('app'))
        elif name in packaged.toc:
            code = packaged.extract(name)
        else:
            continue  # The standalone phone script is bundled as data below.
        if normalize(code) != normalize(compile(source.read_bytes(), source.name, 'exec')):
            raise SystemExit('Packaged source mismatch: ' + source.name)
        count += 1
    for name in ('network.ps1', 'adapter_identity.ps1', 'phone_network_v3.py'):
        if (SRC / name).read_bytes() != (app / '_internal' / name).read_bytes():
            raise SystemExit('Packaged data mismatch: ' + name)
    for folder in ('ui', 'assets'):
        for source in (SRC / folder).rglob('*'):
            if source.is_file() and source.read_bytes() != (app / '_internal' / source.relative_to(SRC)).read_bytes():
                raise SystemExit('Packaged resource mismatch: ' + str(source.relative_to(SRC)))
    expected = json.loads((app / 'build-hashes.json').read_text(encoding='utf-8'))
    actual = {}
    for file in app.rglob('*'):
        if not file.is_file():
            continue
        if file.is_symlink() or not file.resolve().is_relative_to(app):
            raise SystemExit('Build path escapes package')
        if file.suffix.lower() == '.dpapi' or file.name in ('legacy-keys.json', 'PHONE RECEIVER COMMAND.txt'):
            raise SystemExit('Personal data in package: ' + file.name)
        if file.name != 'build-hashes.json':
            actual[file.relative_to(app).as_posix()] = hashlib.sha256(file.read_bytes()).hexdigest()
    if actual != expected:
        raise SystemExit('Build file inventory or hashes do not match')
    for name, digest in HASHES.items():
        if actual.get('_internal/vendor/' + name) != digest:
            raise SystemExit('Unrecognized network dependency: ' + name)
    pe = pefile.PE(str(exe))
    try:
        resources = {entry.id: entry for entry in pe.DIRECTORY_ENTRY_RESOURCE.entries}
        if 3 not in resources or 14 not in resources:
            raise SystemExit('Missing application icon')
        manifest = resources[24].directory.entries[0].directory.entries[0].data.struct
        data = pe.get_data(manifest.OffsetToData, manifest.Size)
        if b'asInvoker' not in data or b'requireAdministrator' in data:
            raise SystemExit('Unexpected launch privilege manifest')
    finally:
        pe.close()
    subprocess.run([str(exe), '--package-check'], check=True, timeout=30)
    print(f'PASS: {count} compiled modules, resources, dependency hashes, full inventory, unpaired payload and ordinary launch privileges.')


if __name__ == '__main__':
    main()
