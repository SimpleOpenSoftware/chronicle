# screenpipe — AI that knows everything you've seen, said, or heard
# https://screenpipe.com

"""Exercise the workflow's actual rebase/report shell without network access."""

import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

WORKFLOW = (
    Path(__file__).resolve().parents[2] / ".github/workflows/sync-screenpipe-fork.yml"
)


def step(text, name):
    block = text.split(f"      - name: {name}\n", 1)[1].split("\n      - name:", 1)[0]
    script = block.split("        run: |\n", 1)[1]
    return block, "\n".join(line[10:] for line in script.splitlines() if line.strip())


class ScreenpipeSyncWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.text = WORKFLOW.read_text()
        self.env = {
            **os.environ,
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
        }
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.invalid")
        (self.root / "capture").write_text("base\n")
        self.git("add", "capture")
        self.git("commit", "-m", "base")
        self.git("checkout", "-b", "chronicle-recorder")
        (self.root / "capture").write_text("fork\n")
        self.git("commit", "-am", "fork")
        self.original = self.git("rev-parse", "HEAD")
        self.git("checkout", "main")

    def git(self, *args):
        return subprocess.check_output(
            ["git", *args],
            cwd=self.root,
            env=self.env,
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()

    def shell(self, script, **env):
        return subprocess.run(
            ["bash", "-eo", "pipefail", "-c", script],
            cwd=self.root,
            env={**self.env, **env},
            capture_output=True,
            text=True,
        )

    def rebase(self, conflict):
        (self.root / ("capture" if conflict else "upstream")).write_text("upstream\n")
        self.git("add", ".")
        self.git("commit", "-m", "upstream")
        self.git("checkout", "chronicle-recorder")
        output = self.root / "output"
        result = self.shell(
            step(self.text, "Rebase")[1], ONTO="main", GITHUB_OUTPUT=str(output)
        )
        return result, output.read_text()

    def test_conflict_restores_fork_reports_then_fails(self):
        result, output = self.rebase(conflict=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("clean=false", output)
        self.assertEqual(self.git("rev-parse", "HEAD"), self.original)
        self.assertFalse((self.root / ".git/rebase-merge").exists())
        stub = self.root / "bin"
        stub.mkdir()
        gh = stub / "gh"
        gh.write_text(
            '#!/bin/bash\nif [ "$2" = list ]; then exit 0; fi\nprintf "%s\\n" "$@" >> "$REPORT_LOG"\n'
        )
        gh.chmod(0o755)
        report = self.root / "report"
        result = self.shell(
            step(self.text, "Open an issue on conflict")[1],
            PATH=f'{stub}:{os.environ["PATH"]}',
            REPORT_LOG=str(report),
            BRANCH="chronicle-recorder",
            ONTO="upstream/main",
            FORK="example/fork",
            UPSTREAM="https://example.invalid/upstream",
            BEHIND="1",
            GITHUB_REPOSITORY="example/chronicle",
            GITHUB_SERVER_URL="https://example.invalid",
            GITHUB_RUN_ID="1",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("create", report.read_text())
        self.assertIn("chronicle-recorder", report.read_text())
        self.assertNotIn("sync-fork.sh", report.read_text())
        block, script = step(self.text, "Fail on rebase conflict")
        self.assertIn("if: always() && steps.rebase.outputs.clean == 'false'", block)
        self.assertLess(
            self.text.index("- name: Open an issue"),
            self.text.index("- name: Fail on rebase"),
        )
        self.assertNotEqual(self.shell(script).returncode, 0)

    def test_clean_rebase_keeps_both_changes(self):
        result, output = self.rebase(conflict=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("clean=true", output)
        self.assertEqual((self.root / "capture").read_text(), "fork\n")
        self.assertEqual((self.root / "upstream").read_text(), "upstream\n")
        self.assertNotEqual(self.git("rev-parse", "HEAD"), self.original)

    def test_branch_lease_and_manual_dry_run_guards(self):
        self.assertIn("ref: chronicle-recorder\n", self.text)
        self.assertEqual(
            re.findall(r"BRANCH: (\S+)", self.text), ["chronicle-recorder"] * 2
        )
        block, script = step(self.text, "Push rebased branch")
        self.assertIn("steps.rebase.outputs.clean == 'true'", block)
        self.assertIn("inputs.push && inputs.push != 'false'", block)
        self.assertIn('git push --force-with-lease origin "HEAD:$BRANCH"', script)
        for name in re.findall(r"^      - name: (.+)$", self.text, re.M):
            if (
                "        run: |"
                not in self.text.split(f"      - name: {name}\n", 1)[1].split(
                    "\n      - name:", 1
                )[0]
            ):
                continue
            result = subprocess.run(
                ["bash", "-n"],
                input=step(self.text, name)[1],
                text=True,
                capture_output=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
