import copy
import unittest
from pages_gate import may_publish, REQUIRED_JOBS


class PagesGateTests(unittest.TestCase):
    def setUp(self):
        self.sha = 'a' * 40
        self.repo = 'owner/project'
        self.run = {'event': 'push', 'head_branch': 'master', 'head_repository': {'full_name': self.repo}, 'path': '.github/workflows/ci.yml', 'conclusion': 'success', 'head_sha': self.sha}
        self.jobs = [{'name': name, 'conclusion': 'success'} for name in REQUIRED_JOBS]

    def test_current_master(self):
        self.assertTrue(may_publish(self.run, self.jobs, self.sha, self.repo))

    def test_rejects_untrusted_or_failed_runs(self):
        for change in [{'event': 'pull_request'}, {'event': 'workflow_dispatch'}, {'head_branch': 'feature'}, {'head_repository': {'full_name': 'fork/project'}}, {'path': '.github/workflows/other.yml'}, {'conclusion': 'failure'}, {'head_sha': 'b' * 40}]:
            with self.subTest(change=change):
                self.assertFalse(may_publish(self.run | change, self.jobs, self.sha, self.repo))

    def test_rejects_missing_failed_or_skipped_checks(self):
        self.assertFalse(may_publish(self.run, self.jobs[:-1], self.sha, self.repo))
        for conclusion in ['failure', 'skipped', 'cancelled', None]:
            jobs = copy.deepcopy(self.jobs)
            jobs[0]['conclusion'] = conclusion
            self.assertFalse(may_publish(self.run, jobs, self.sha, self.repo))


if __name__ == '__main__':
    unittest.main()
