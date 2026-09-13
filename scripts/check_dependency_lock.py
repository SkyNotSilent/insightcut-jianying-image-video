"""Check declared dependencies against the locked versions on this platform."""
from importlib.metadata import version
from pathlib import Path
from packaging.requirements import Requirement

root = Path(__file__).resolve().parents[1] / 'ai-kepu-video-server'
for name in ['requirements.txt', 'requirements-dev.txt']:
    for line in (root / name).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith(('#', '-')):
            continue
        requirement = Requirement(line)
        if requirement.marker and not requirement.marker.evaluate():
            continue
        installed = version(requirement.name)
        assert installed in requirement.specifier, f'{requirement.name}: {installed} does not satisfy {requirement.specifier}; regenerate lock'
print('Installed lock satisfies declared dependencies')
