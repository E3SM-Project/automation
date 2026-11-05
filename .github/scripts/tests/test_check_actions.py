import importlib.util
import pathlib
import tempfile
import unittest

SCRIPT = pathlib.Path(__file__).parents[1] / "check_actions.py"
SPEC = importlib.util.spec_from_file_location("check_actions", SCRIPT)
CHECK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECK)

DEPENDABOT = 'directories:\n  - "/"\n  - "/foo"\n'
TESTS = "jobs:\n  foo:\n    steps:\n      - uses: ./foo\n"
README = "| Action | What |\n| --- | --- |\n| `foo` | does foo |\n"


def make_repo(root, dependabot=DEPENDABOT, tests=TESTS, readme=README, actions=("foo",)):
    (root / ".github" / "workflows").mkdir(parents=True)
    (root / ".github" / "dependabot.yml").write_text(dependabot)
    (root / ".github" / "workflows" / "test-actions.yaml").write_text(tests)
    (root / "README.md").write_text(readme)
    for a in actions:
        (root / a).mkdir()
        (root / a / "action.yml").write_text("name: x\n")


class CheckActionsTests(unittest.TestCase):
    def run_check(self, **kw):
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d)
            make_repo(root, **kw)
            return CHECK.check(root)

    def test_all_registered(self):
        self.assertEqual(self.run_check(), [])

    def test_missing_everywhere(self):
        problems = self.run_check(dependabot='directories:\n  - "/"\n', tests="jobs: {}\n", readme="# x\n")
        self.assertEqual(len(problems), 3)
        self.assertTrue(all(p.startswith("foo:") for p in problems))

    def test_missing_only_dependabot(self):
        problems = self.run_check(dependabot='directories:\n  - "/"\n')
        self.assertEqual(len(problems), 1)
        self.assertIn("dependabot.yml", problems[0])

    def test_missing_only_test_job(self):
        problems = self.run_check(tests="jobs: {}\n")
        self.assertEqual(len(problems), 1)
        self.assertIn("test-actions.yaml", problems[0])

    def test_missing_only_readme_row(self):
        problems = self.run_check(readme="| Action | What |\n| `foo-other` | x |\n")
        self.assertEqual(len(problems), 1)
        self.assertIn("README.md", problems[0])

    def test_name_prefix_is_not_a_match(self):
        # "foo" must not be satisfied by entries for "foobar"
        problems = self.run_check(
            dependabot='directories:\n  - "/foobar"\n', tests="- uses: ./foobar\n", readme="| `foobar` | x |\n")
        self.assertTrue(any(p.startswith("foo:") for p in problems))

    def test_stale_dependabot_entry(self):
        problems = self.run_check(dependabot=DEPENDABOT + '  - "/gone"\n')
        self.assertEqual(len(problems), 1)
        self.assertTrue(problems[0].startswith("gone:"))

    def test_ignores_dirs_without_action_and_dot_dirs(self):
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d)
            make_repo(root)
            (root / "docs").mkdir()                      # no action.yml
            (root / ".hidden").mkdir()
            (root / ".hidden" / "action.yml").write_text("name: x\n")
            self.assertEqual(CHECK.find_actions(root), ["foo"])
            self.assertEqual(CHECK.check(root), [])


if __name__ == "__main__":
    unittest.main()
