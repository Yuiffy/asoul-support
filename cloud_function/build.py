"""Build an allowlisted ZIP; never includes credentials or the repository tree."""
from pathlib import Path
import argparse
import zipfile

parser = argparse.ArgumentParser()
parser.add_argument('output', type=Path)
parser.add_argument('--dependencies', type=Path, help='Private pip target directory containing Linux wheels')
args = parser.parse_args()
source = Path(__file__).resolve().parent
args.output.parent.mkdir(parents=True, exist_ok=True)
with zipfile.ZipFile(args.output, 'w', zipfile.ZIP_DEFLATED) as package:
    for name in ('index.py', 'asoul_x25kn.py', 'daily_progress.py', 'wecom_notify.py',
                 'run_schedule.py', 'room_priority.py', 'relight_rules.py', 'THIRD_PARTY.md'):
        package.write(source / name, name)
    package.write(source / 'credential_refresh.py', 'credential_refresh.py')
    if args.dependencies:
        # Only these pinned packages and their licenses may enter the deployment.
        import importlib.metadata
        for name, version in (('cryptography', '48.0.0'), ('cffi', '2.0.0'), ('pycparser', '3.0')):
            matches = [d for d in importlib.metadata.distributions(path=[str(args.dependencies)])
                       if d.metadata['Name'].lower() == name]
            if len(matches) != 1 or matches[0].version != version:
                raise ValueError('Missing or unexpected dependency: ' + name)
        for path in sorted(args.dependencies.rglob('*')):
            if not path.is_file() or '__pycache__' in path.parts or path.suffix == '.pyc':
                continue
            relative = path.relative_to(args.dependencies)
            root = relative.parts[0]
            if (root in ('cryptography', 'cffi', 'pycparser') or
                    root.startswith(('cryptography-48.0.0.', 'cffi-2.0.0.', 'pycparser-3.0.', '_cffi_backend.'))):
                if path.suffix in ('.pyd', '.dll'):
                    raise ValueError('Windows dependency cannot run in FunctionGraph')
                package.write(path, str(relative))
print(args.output.resolve())
