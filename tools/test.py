"""Run each offline suite in a separate process for clean Tk teardown."""
from pathlib import Path
import subprocess,sys
ROOT=Path(__file__).resolve().parents[1]
for test in sorted((ROOT/'tests').glob('test_*.py')):
    subprocess.run([sys.executable,'-B',str(test)],check=True,cwd=ROOT)
