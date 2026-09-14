"""Build an allowlisted ZIP; never includes credentials or the repository tree."""
from pathlib import Path
import argparse
import zipfile

parser = argparse.ArgumentParser()
parser.add_argument('output', type=Path)
args = parser.parse_args()
source = Path(__file__).resolve().parent
args.output.parent.mkdir(parents=True, exist_ok=True)
with zipfile.ZipFile(args.output, 'w', zipfile.ZIP_DEFLATED) as package:
    for name in ('index.py', 'asoul_x25kn.py', 'wecom_notify.py', 'THIRD_PARTY.md'):
        package.write(source / name, name)
print(args.output.resolve())
