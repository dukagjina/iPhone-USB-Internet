"""Build a distributable unpaired application and optional Inno Setup installer."""
import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / 'src'
sys.path.insert(0, str(SRC))
from backend import HASHES


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--vendor-dir', type=Path, default=ROOT/'vendor')
    parser.add_argument('--output', type=Path, default=ROOT/'dist')
    parser.add_argument('--installer', action='store_true')
    args = parser.parse_args()
    output = args.output.resolve()
    vendor = args.vendor_dir.resolve()
    for name, expected in HASHES.items():
        source = vendor/name
        if not source.is_file() or hashlib.sha256(source.read_bytes()).hexdigest() != expected:
            raise SystemExit('Missing or unrecognized vendor dependency: '+name)
    command=[sys.executable,'-m','PyInstaller','--noconfirm','--clean','--onedir','--windowed',
        '--name','iPhone USB Internet','--distpath',str(output),
        '--workpath',str(ROOT/'build/pyinstaller'),'--specpath',str(ROOT/'build'),
        '--icon',str(SRC/'assets/iphone-usb.ico'),'--version-file',str(SRC/'assets/version.txt')]
    for source,destination in [(SRC/'ui','ui'),(SRC/'assets','assets'),
            (SRC/'network.ps1','.'),(SRC/'adapter_identity.ps1','.'),
            (SRC/'phone_network_v3.py','.'),(vendor/'tun2socks.exe','vendor'),(vendor/'wintun.dll','vendor')]:
        command+=['--add-data',str(source)+';'+destination]
    command+=[str(SRC/'app.py')]
    subprocess.run(command,check=True)
    app=output/'iPhone USB Internet'
    for name in ('LICENSE','EDUCATIONAL USE.txt','COMMERCIAL_USE.txt','TRADEMARKS.txt','PRIVACY.txt','THIRD PARTY NOTICES.txt'):
        shutil.copy2(ROOT/name,app/name)
    for name in ('README.md', 'INSTALL.md'):
        shutil.copy2(ROOT/name, app/name)
    licenses=app/'licenses'
    shutil.copytree(ROOT/'licenses',licenses,dirs_exist_ok=True)
    shutil.copy2(Path(sys.base_prefix)/'LICENSE.txt',licenses/'python.txt')
    shutil.copy2(Path(sys.base_prefix)/'tcl/tk8.6/license.terms',licenses/'tk8.6.txt')
    for dependency in ('pyinstaller','pywin32','pywebview','pythonnet','clr_loader','proxy_tools','bottle','typing_extensions','cffi','pycparser'):
        distribution=importlib.metadata.distribution(dependency)
        for item in distribution.files or ():
            if Path(str(item)).name.lower() in ('license','license.txt','license.md','copying','copying.txt'):
                shutil.copy2(distribution.locate_file(item),licenses/(dependency+'-'+Path(str(item)).name))
    for forbidden in ('pairing.dpapi','legacy-keys.json','PHONE RECEIVER COMMAND.txt'):
        if (app/forbidden).exists():
            raise SystemExit('Private data found in release payload. Build into a fresh output directory.')
    subprocess.run([str(app/'iPhone USB Internet.exe'),'--package-check'],check=True,timeout=30)
    hashes={file.relative_to(app).as_posix():hashlib.sha256(file.read_bytes()).hexdigest()
            for file in sorted(app.rglob('*')) if file.is_file() and file.name!='build-hashes.json'}
    (app/'build-hashes.json').write_text(json.dumps(hashes,indent=2),encoding='utf-8')
    if args.installer:
        candidates=[Path(os.environ.get('LOCALAPPDATA',''))/'Programs/Inno Setup 6/ISCC.exe',
            Path(r'C:\Program Files (x86)\Inno Setup 6\ISCC.exe'),Path(r'C:\Program Files\Inno Setup 6\ISCC.exe')]
        compiler=next((p for p in candidates if p.is_file()),None)
        if not compiler:raise SystemExit('Inno Setup 6 is required to build the installer.')
        subprocess.run([str(compiler),'/DPayloadDir='+str(app),'/DOutputPath='+str(output/'installer'),str(ROOT/'installer.iss')],check=True)
        installer=output/'installer/iPhone-USB-Internet-Setup.exe'
        checksum=hashlib.sha256(installer.read_bytes()).hexdigest()
        installer.with_suffix('.exe.sha256').write_text(checksum+'  '+installer.name+'\n',encoding='ascii')
    print('Built unpaired release. No phone, driver, routing or installation action was performed.')


if __name__=='__main__':main()
