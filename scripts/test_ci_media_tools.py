"""Exercise CI tool installation without root, APT, or network access."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).with_name('install_ci_media_tools.sh')


class MediaToolsTests(unittest.TestCase):
    def run_installer(self, *, installed=False, apt_failure=False, broken_after=False, pdf=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mock = root / 'bin'
            mock.mkdir()
            for name, body in {
                'ffmpeg': 'test "$INSTALLED" = 1 || { test -f "$STATE/installed" && test "$BROKEN_AFTER" != 1; }',
                'ffprobe': 'test "$INSTALLED" = 1 || { test -f "$STATE/installed" && test "$BROKEN_AFTER" != 1; }',
                'pdftotext': 'test "$INSTALLED" = 1 || test -f "$STATE/installed"',
                'pdftoppm': 'test "$INSTALLED" = 1 || test -f "$STATE/installed"',
                'sudo': 'exec "$@"',
                'timeout': 'echo "$1" >> "$STATE/timeouts"; shift; exec "$@"',
                'apt-get': '''printf '%s\\n' "$*" >> "$STATE/apt.log"
for option in "$@"; do
 case "$option" in Dir::Etc::sourcelist=*) cat "${option#*=}" > "$STATE/sources";; esac
done
[ "$APT_FAILURE" != 1 ] || exit 42
case " $* " in *' install '*) touch "$STATE/installed";; esac''',
            }.items():
                path = mock / name
                path.write_text('#!/bin/sh\n' + body + '\n')
                path.chmod(0o755)
            env = dict(os.environ, PATH=str(mock) + ':' + os.environ['PATH'], STATE=str(root),
                       VERSION_CODENAME='noble', INSTALLED=str(int(installed)),
                       APT_FAILURE=str(int(apt_failure)), BROKEN_AFTER=str(int(broken_after)))
            result = subprocess.run(['bash', str(SCRIPT), *(['--pdf'] if pdf else [])], env=env,
                                    text=True, capture_output=True)
            return result, {p.name: p.read_text() for p in root.iterdir() if p.is_file()}

    def test_working_tools_skip_apt(self):
        result, files = self.run_installer(installed=True, pdf=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('apt.log', files)

    def test_missing_tools_install_from_official_signed_sources_with_bounds(self):
        result, files = self.run_installer(pdf=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('https://archive.ubuntu.com/ubuntu', files['sources'])
        self.assertIn('https://security.ubuntu.com/ubuntu', files['sources'])
        self.assertNotIn('trusted=yes', files['sources'])
        self.assertIn('ffmpeg poppler-utils', files['apt.log'])
        self.assertIn('Acquire::Retries=2', files['apt.log'])
        self.assertEqual(files['timeouts'].splitlines(), ['90s', '240s'])

    def test_failed_apt_cannot_report_success(self):
        result, _ = self.run_installer(apt_failure=True)
        self.assertNotEqual(result.returncode, 0)

    def test_successful_apt_still_requires_working_media_tools(self):
        result, _ = self.run_installer(broken_after=True)
        self.assertNotEqual(result.returncode, 0)


if __name__ == '__main__':
    unittest.main()
