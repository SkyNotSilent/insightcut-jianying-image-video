"""Fail closed: only a current, successful master push may publish a CI artifact."""
import json
import os
from pathlib import Path
import sys

REQUIRED_JOBS = {
    'Backend tests and audit', 'Frontend tests, build and audit',
    'Repository hygiene', 'Real full-stack Playwright',
    'Filesystem (windows-latest)', 'Filesystem (macos-latest)',
    'Workflow and secret checks', 'Website build and browser checks',
}


def may_publish(run, jobs, latest_sha, repository):
    return (
        run.get('event') == 'push'
        and run.get('head_branch') == 'master'
        and run.get('head_repository', {}).get('full_name') == repository
        and run.get('path') == '.github/workflows/ci.yml'
        and run.get('conclusion') == 'success'
        and run.get('head_sha') == latest_sha
        and len(latest_sha) == 40
        and REQUIRED_JOBS <= {job.get('name') for job in jobs}
        and all(job.get('conclusion') == 'success' for job in jobs)
    )


if __name__ == '__main__':
    run = json.loads(Path(sys.argv[1]).read_text())
    jobs = json.loads(Path(sys.argv[2]).read_text())['jobs']
    sha = Path(sys.argv[3]).read_text().strip()
    allowed = may_publish(run, jobs, sha, os.environ['GITHUB_REPOSITORY'])
    print(f'publish={str(allowed).lower()}')
