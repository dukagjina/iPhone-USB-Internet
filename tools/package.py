"""Create an allowlisted source archive and checksums for a GitHub prerelease.

This command never uploads, installs, connects a phone, or reads personal keys.
"""
import hashlib
import json
from pathlib import Path
import re
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from app_info import VERSION

ROOT_FILES = (
    '.gitignore', '.gitattributes', 'README.md', 'INSTALL.md', 'LICENSE', 'COMMERCIAL_USE.txt',
    'EDUCATIONAL USE.txt', 'PRIVACY.txt', 'THIRD PARTY NOTICES.txt',
    'TRADEMARKS.txt', 'RELEASE CHECKLIST.txt', 'requirements-build.txt',
    'installer.iss', 'package.json',
)
SOURCE_DIRS = ('src', 'tests', 'tools', 'docs', 'licenses')
SUFFIXES = {'.py', '.ps1', '.cjs', '.js', '.html', '.css', '.svg', '.png', '.ico', '.txt', '.md'}
PERSONAL_PATH = re.compile(rb'(?i)[a-z]:[\\/]+Users[\\/]+|' + rb'/' + rb'Users/[^/\s]+/')


def public_files():
    files = [ROOT / name for name in ROOT_FILES]
    if (ROOT / 'package-lock.json').is_file():
        files.append(ROOT / 'package-lock.json')
    for name in SOURCE_DIRS:
        directory = ROOT / name
        if not directory.is_dir():
            raise SystemExit('Missing source directory: ' + name)
        for path in directory.rglob('*'):
            if path.is_symlink():
                raise SystemExit('Symlink is not allowed in source: ' + str(path.relative_to(ROOT)))
            if path.is_file() and '__pycache__' not in path.parts:
                if path.suffix.lower() not in SUFFIXES:
                    raise SystemExit('Unexpected file in source: ' + str(path.relative_to(ROOT)))
                files.append(path)
    for path in files:
        if path.is_symlink() or not path.resolve().is_relative_to(ROOT):
            raise SystemExit('Source path escapes project: ' + str(path))
        if not path.is_file():
            raise SystemExit('Missing source file: ' + str(path.relative_to(ROOT)))
        if PERSONAL_PATH.search(path.read_bytes()):
            raise SystemExit('Personal filesystem path in source: ' + str(path.relative_to(ROOT)))
    return sorted(files)


def main():
    if not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', VERSION):
        raise SystemExit('Unexpected release version')
    dist = ROOT / 'dist'
    installer = dist / 'installer/iPhone-USB-Internet-Setup.exe'
    checksum_file = installer.with_suffix('.exe.sha256')
    checksum = hashlib.sha256(installer.read_bytes()).hexdigest()
    if checksum_file.read_text(encoding='ascii').split() != [checksum, installer.name]:
        raise SystemExit('Installer checksum does not match. Rebuild before packaging.')
    files = public_files()
    manifest = {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    archive = dist / f'iPhone-USB-Internet-{VERSION}-source.zip'
    temporary = archive.with_suffix('.zip.tmp')
    with zipfile.ZipFile(temporary, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as bundle:
        for name in list(manifest) + ['SOURCE MANIFEST.json']:
            data = (ROOT / name).read_bytes() if name in manifest else json.dumps(manifest, indent=2).encode('utf-8')
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.external_attr = 0o100644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            bundle.writestr(info, data)
    with zipfile.ZipFile(temporary) as bundle:
        if bundle.testzip() is not None or set(bundle.namelist()) != set(manifest) | {'SOURCE MANIFEST.json'}:
            raise SystemExit('Source archive integrity check failed')
        for name, expected in manifest.items():
            if hashlib.sha256(bundle.read(name)).hexdigest() != expected:
                raise SystemExit('Source archive mismatch: ' + name)
    temporary.replace(archive)
    source_checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
    (dist / 'SHA256SUMS.txt').write_text(
        checksum + '  installer/' + installer.name + '\n' +
        source_checksum + '  ' + archive.name + '\n', encoding='ascii')
    print(f'Prepared {archive.name} with {len(files)} reviewed source files and SHA256SUMS.txt.')


if __name__ == '__main__':
    main()
