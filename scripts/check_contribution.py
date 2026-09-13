"""Offline repository-policy checks, safe in untrusted PRs."""
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def forbidden_path(name):
    path = Path(name)
    return (
        (path.name == '.env' or path.name.startswith('.env.'))
        and not path.name.endswith('.example')
    ) or path.suffix.lower() in {'.db', '.sqlite', '.sqlite3'}


def main():
    errors = []
    if (ROOT / 'AGENTS.md').read_bytes() != (ROOT / 'CLAUDE.md').read_bytes():
        errors.append('AGENTS.md and CLAUDE.md differ')
    names = subprocess.check_output(['git', 'ls-files', '-z'], cwd=ROOT).decode().split('\0')
    errors.extend(f'Private runtime path tracked: {name}' for name in names if forbidden_path(name))
    for name in ['LICENSE', 'CONTRIBUTING.md', 'SECURITY.md']:
        if not (ROOT / name).is_file():
            errors.append(f'Missing {name}')
    guide = (ROOT / 'CONTRIBUTING.md').read_text()
    for token in ['requirements-dev.lock', 'ffprobe', 'upstream', 'master', 'playwright']:
        if token not in guide:
            errors.append(f'Contribution guide missing {token}')
    if 'cd ../../..' in (ROOT / 'docs/engineering-workflow.md').read_text():
        errors.append('Engineering guide leaves repository root')
    print('\n'.join(errors) if errors else 'Contribution policy checks passed')
    return bool(errors)


if __name__ == '__main__':
    sys.exit(main())
