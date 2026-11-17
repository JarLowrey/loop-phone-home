"""Tests for the driver's newer generic parts: the task hook's test receipts and the gate that trusts them, a session's
hook settings, companion files and gate checks, the monitor, the task and progress files' bookkeeping, the test-call
pattern and the prompts' placeholders. Run from runner/driver:
python -m unittest discover -s tests -p test_generic.py

Real temp git repositories stand in for a slot; nothing here starts a session, calls ntfy or pushes."""
import builtins
import datetime
import io
import json
import os
import pathlib
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from driver_base import DriverTest, fake_slot  # noqa: E402
import core as C  # noqa: E402
import monitor as MO  # noqa: E402
import prompts  # noqa: E402
import runners as RN  # noqa: E402
import sessions as X  # noqa: E402
import slots as SL  # noqa: E402
import task_hook as H  # noqa: E402
import tasks as T  # noqa: E402

WORLD_GD = ("class_name World\nextends RefCounted\n\n\nfunc step() -> void:\n\tpass\n\n\n"
            "func end_round(winner: StringName) -> void:\n\tpass\n")
TEST_WORLD_GD = "extends RefCounted\n\n\nfunc test_end_round() -> void:\n\tassert_eq(1, 1)\n"
PASSING = "3 passed, 0 failed\nsmoke: PASS"
FAILING = "2 passed, 1 failed\nFAIL test_end_round"
TEST_CALL = "python game/tools/run_tests.py"
SPEC = ("### [ ] T-01 World ends a round\n**Files.** `game/src/core/world.gd`\n\n"
        "**Done when.** test `test_end_round`, output `3 passed, 0 failed`\n")


def git(root, *args):
    done = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, encoding="utf-8")
    if done.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {done.stderr}")
    return done.stdout.strip()


def write(root, path, text):
    target = pathlib.Path(root) / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8", newline="\n")
    return target


def make_repo(root, files):
    """A git repository at root holding files, committed on branch main. Returns the commit."""
    for path, text in files.items():
        write(root, path, text)
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "t")
    git(root, "config", "core.autocrlf", "false")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "start")
    return git(root, "rev-parse", "HEAD")


REPO_FILES = {"game/src/core/world.gd": WORLD_GD, "game/tests/test_world.gd": TEST_WORLD_GD,
              "game/content/units.csv": "id,name\nu1,first\n", "README.md": "a project\n"}
# why: six git calls per test made a repository each time; one is made per run and each test gets a copy
TEMPLATE = {}


def template_repo():
    """(the path, the commit) of a repository holding REPO_FILES, made once per run."""
    if "root" not in TEMPLATE:
        holder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = pathlib.Path(holder.name) / "repo"
        TEMPLATE.update(holder=holder, base=make_repo(root, REPO_FILES), root=root)
    return TEMPLATE["root"], TEMPLATE["base"]


def remove_tree(path):
    """Deletes a temp folder, git's read-only object files included."""
    for folder, _, files in os.walk(path):
        for name in files:
            try:
                os.chmod(os.path.join(folder, name), stat.S_IWRITE)
            except OSError:
                pass
    shutil.rmtree(path, ignore_errors=True)


def bash_event(command, stdout="", stderr="", interrupted=False):
    """A PostToolUse event of a Bash call, as Claude Code sends it on stdin."""
    return {"hook_event_name": "PostToolUse", "tool_name": "Bash",
            "tool_input": {"command": command, "timeout": 600000},
            "tool_response": {"stdout": stdout, "stderr": stderr, "interrupted": interrupted, "isImage": False}}


class GenericTest(DriverTest):
    """DriverTest whose temp folder is removed afterwards, with a project repository at self.repo (outside C.LOGS'
    own files, which stay in self.temp)."""

    def setUp(self):
        super().setUp()
        self.addCleanup(remove_tree, self.temp)
        self.repo = self.temp / "repo"
        template, self.base = template_repo()
        shutil.copytree(template, self.repo)


# ---------------------------------------------------------------------------------------------------------------------
# 1. The task hook's receipts
# ---------------------------------------------------------------------------------------------------------------------


class TreeHashTest(GenericTest):
    SCOPE = ["game/src", "game/tests"]

    def hash(self):
        return H.tree_hash(str(self.repo), self.SCOPE)

    def test_same_tree_same_hash_and_an_undone_edit_returns_to_it(self):
        first = self.hash()
        self.assertEqual(first, self.hash())
        self.assertEqual(first, H.tree_hash(str(self.repo), ["game/src/", "game/tests/"]))
        write(self.repo, "game/src/core/world.gd", WORLD_GD + "\n# edited\n")
        edited = self.hash()
        self.assertNotEqual(first, edited)
        self.assertEqual(edited, self.hash())
        write(self.repo, "game/src/core/world.gd", WORLD_GD)
        self.assertEqual(first, self.hash())

    def test_a_new_untracked_file_and_a_deletion_change_it(self):
        first = self.hash()
        write(self.repo, "game/src/core/new_part.gd", "extends RefCounted\n")
        added = self.hash()
        self.assertNotEqual(first, added)
        (self.repo / "game/src/core/new_part.gd").unlink()
        self.assertEqual(first, self.hash())
        (self.repo / "game/tests/test_world.gd").unlink()
        self.assertNotEqual(first, self.hash())

    def test_files_outside_the_scope_do_not_matter(self):
        first = self.hash()
        write(self.repo, "README.md", "changed\n")
        write(self.repo, "docs/notes.md", "new\n")
        write(self.repo, "game/content/units.csv", "id,name\nu2,second\n")
        self.assertEqual(first, self.hash())


class ResponseTextTest(unittest.TestCase):
    def test_a_dict_gives_stdout_then_stderr(self):
        text = H.response_text({"stdout": "3 passed, 0 failed\n", "stderr": "warning: slow\n", "interrupted": False})
        self.assertEqual([line for line in text.splitlines() if line], ["3 passed, 0 failed", "warning: slow"])
        self.assertEqual(H.response_text({"stdout": None, "stderr": "only this"}), "only this")
        self.assertEqual(H.response_text({"stdout": "", "stderr": "", "output": "all of it"}), "all of it")

    def test_a_string_is_the_text_and_none_is_empty(self):
        self.assertEqual(H.response_text("  3 passed, 0 failed\n"), "3 passed, 0 failed")
        self.assertEqual(H.response_text(None), "")


class ReceiptHookTest(GenericTest):
    def setUp(self):
        super().setUp()
        self.receipt = self.temp / "receipt-1.json"

    def args(self, *argv, **values):
        config = {"root": str(self.repo), "slot": "1", "receipt": str(self.receipt),
                  "scope": ["game/src", "game/tests"], "test_call": C.TEST_CALL.pattern, "partial": ["--only"], "fail_marker": ["smoke: FAIL"],
                  "started": time.time() - 60}
        config.update(values)
        path = self.temp / "hook-test.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        return H.parse(["--config", str(path), *argv])

    def post(self, command, stdout, key="", **given):
        env = {H.RECEIPT_KEY_ENV: key} if key else {}
        with mock.patch.dict(os.environ, env):
            if not key:
                os.environ.pop(H.RECEIPT_KEY_ENV, None)
            H.after_bash(bash_event(command, stdout, **given), self.args())
        return json.loads(self.receipt.read_text(encoding="utf-8")) if self.receipt.exists() else None

    def test_a_passing_full_run_writes_a_signed_receipt_of_the_tree(self):
        record = self.post(TEST_CALL, PASSING, key="k1")
        self.assertTrue(record["passed"])
        self.assertEqual(record["output"], PASSING)
        self.assertEqual(record["tree"], H.tree_hash(str(self.repo), ["game/src", "game/tests"]))
        self.assertEqual(record["signature"], H.receipt_signature("k1", record["tree"], PASSING))
        self.assertAlmostEqual(record["written"], time.time(), delta=30)

    def test_without_the_key_the_receipt_is_unsigned(self):
        record = self.post(TEST_CALL, PASSING)
        self.assertTrue(record["passed"])
        self.assertEqual(record["signature"], "")

    def test_a_leading_cd_to_the_root_still_counts_as_a_full_run(self):
        self.assertIsNotNone(self.post(f'cd "{self.repo}" && {TEST_CALL}', PASSING, key="k1"))

    def test_partial_runs_and_other_commands_write_nothing(self):
        for command in (f"{TEST_CALL} --only test_world", "git status", "cat game/tools/run_tests.py",
                        "grep -n run_tests.py README.md"):
            self.assertIsNone(self.post(command, PASSING, key="k1"), command)
        with mock.patch.dict(os.environ, {H.RECEIPT_KEY_ENV: "k1"}):
            H.after_bash({"tool_name": "Read", "tool_input": {"file_path": "README.md"}, "tool_response": "x"},
                         self.args())
        self.assertFalse(self.receipt.exists())

    def test_passed_follows_the_totals_the_fail_markers_and_an_interrupt(self):
        cases = [(PASSING, {}, True),
                 (FAILING, {}, False),
                 ("3 passed, 0 failed\nsmoke: FAIL the window did not open", {}, False),
                 ("no totals here", {}, False),
                 ("1 passed, 1 failed\n3 passed, 0 failed", {}, True),
                 (PASSING, {"interrupted": True}, False)]
        for output, given, passed in cases:
            self.receipt.unlink(missing_ok=True)
            record = self.post(TEST_CALL, output, key="k1", **given)
            self.assertEqual(record["passed"], passed, (output, given))

    def test_a_filtered_or_chained_run_earns_no_passing_receipt(self):
        """The receipt is the gate's run, so its output must be the suite's own: a run piped through tail can drop a
        fail-marker line above the totals (the suite printed `smoke: FAIL ...` and tail kept only the totals line), and
        a command chained after the run can print a totals line of its own."""
        for command, shown in ((f"{TEST_CALL} 2>&1 | tail -n 1", "3 passed, 0 failed"),
                               (f"{TEST_CALL}; echo '3 passed, 0 failed'", FAILING + "\n3 passed, 0 failed")):
            self.receipt.unlink(missing_ok=True)
            record = self.post(command, shown, key="k1")
            self.assertTrue(record is None or not record["passed"], command)

    def decide(self, args, command=TEST_CALL):
        return H.decide({"tool_name": "Bash", "tool_input": {"command": command, "timeout": 600000}}, args)

    def test_a_second_full_run_on_the_same_tree_is_refused(self):
        self.post(TEST_CALL, PASSING, key="k1")
        args = self.args()
        self.assertEqual(self.decide(args), H.SECOND_RUN_REASON)
        self.assertEqual(self.decide(args, f"{TEST_CALL} --only test_world"), "")
        self.assertEqual(self.decide(args, "git status"), "")

    def test_a_second_full_run_is_allowed_after_an_edit(self):
        self.post(TEST_CALL, PASSING, key="k1")
        write(self.repo, "game/src/core/world.gd", WORLD_GD + "\n# edited\n")
        self.assertEqual(self.decide(self.args()), "")

    def test_a_receipt_from_before_the_session_or_a_failed_run_does_not_refuse(self):
        self.post(TEST_CALL, PASSING, key="k1")
        self.assertEqual(self.decide(self.args(started=time.time() + 60)), "")
        self.receipt.unlink()
        self.post(TEST_CALL, FAILING, key="k1")
        self.assertEqual(self.decide(self.args()), "")

    def test_parse_reads_the_config_file_and_the_command_line_wins(self):
        args = self.args(**{"full-timeout-ms": 1234, "deadline": 99.5})
        self.assertEqual(args.slot, "1")
        self.assertEqual(args.root, str(self.repo))
        self.assertEqual(args.scope, ["game/src", "game/tests"])
        self.assertEqual(args.fail_marker, ["smoke: FAIL"])
        self.assertEqual(args.full_timeout_ms, 1234)
        self.assertEqual(args.deadline, 99.5)
        self.assertFalse(args.post)
        args = self.args("--slot", "7", "--full-timeout-ms", "5", "--post")
        self.assertEqual(args.slot, "7")
        self.assertEqual(args.full_timeout_ms, 5)
        self.assertTrue(args.post)
        self.assertEqual(H.parse([]).totals_marker, "passed, ")

    def test_main_in_post_mode_writes_the_receipt_and_prints_nothing(self):
        self.args()
        config = str(self.temp / "hook-test.json")
        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(bash_event(TEST_CALL, PASSING)))), \
                mock.patch.dict(os.environ, {H.RECEIPT_KEY_ENV: "k1"}):
            self.assertEqual(H.main(["--config", config, "--post"]), 0)
        self.assertTrue(json.loads(self.receipt.read_text(encoding="utf-8"))["passed"])
        builtins.print.assert_not_called()

    def test_main_before_a_second_full_run_prints_the_refusal(self):
        self.post(TEST_CALL, PASSING, key="k1")
        self.args()
        event = {"tool_name": "Bash", "tool_input": {"command": TEST_CALL, "timeout": 600000}}
        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(event))):
            self.assertEqual(H.main(["--config", str(self.temp / "hook-test.json")]), 0)
        decision = json.loads(builtins.print.call_args[0][0])["hookSpecificOutput"]
        self.assertEqual(decision["permissionDecision"], "deny")
        self.assertEqual(decision["permissionDecisionReason"], H.SECOND_RUN_REASON)

    def test_main_ignores_stdin_that_is_not_json(self):
        with mock.patch.object(sys, "stdin", io.StringIO("not json")):
            self.assertEqual(H.main(["--post"]), 0)
        builtins.print.assert_not_called()


# ---------------------------------------------------------------------------------------------------------------------
# 2. The gate trusts a signed receipt of the tree it holds
# ---------------------------------------------------------------------------------------------------------------------


class TrustedReceiptTest(GenericTest):
    def setUp(self):
        super().setUp()
        self.slot = fake_slot("1", root=self.repo, base=self.base)
        write(self.repo, "game/src/core/world.gd", WORLD_GD + "\nfunc finish() -> void:\n\tpass\n")

    def receipt_path(self, name="1"):
        return C.LOGS / f"receipt-{name}.json"

    def sign(self, output=PASSING, name="1", key=None, tree=None):
        """Writes a receipt for slot name as the task hook would, signed with key (the slot's own by default)."""
        key = key if key is not None else X.receipt_key(fake_slot(name))
        tree = tree if tree is not None else H.tree_hash(str(self.repo), C.SCOPE)
        record = {"tree": tree, "output": output, "passed": True, "written": time.time(),
                  "signature": H.receipt_signature(key, tree, output) if key else ""}
        self.receipt_path(name).write_text(json.dumps(record), encoding="utf-8")
        return record

    def test_a_receipt_signed_with_the_slots_key_for_this_tree_is_trusted(self):
        self.sign()
        self.assertEqual(RN.trusted_receipt(self.repo, "1"), PASSING)

    def test_a_tampered_receipt_is_not_trusted(self):
        record = self.sign(FAILING)
        record["output"] = PASSING
        self.receipt_path().write_text(json.dumps(record), encoding="utf-8")
        self.assertEqual(RN.trusted_receipt(self.repo, "1"), "")

    def test_another_slots_key_is_not_trusted(self):
        X.receipt_key(self.slot)
        self.sign(key=X.receipt_key(fake_slot("2")))
        self.assertNotEqual(C.RUNTIME["receipt_keys"]["1"], C.RUNTIME["receipt_keys"]["2"])
        self.assertEqual(RN.trusted_receipt(self.repo, "1"), "")

    def test_an_unsigned_receipt_is_not_trusted(self):
        X.receipt_key(self.slot)
        self.sign(key="")
        self.assertEqual(RN.trusted_receipt(self.repo, "1"), "")

    def test_a_tree_changed_since_the_run_is_not_trusted(self):
        self.sign()
        write(self.repo, "game/tests/test_world.gd", TEST_WORLD_GD + "\n# later\n")
        self.assertEqual(RN.trusted_receipt(self.repo, "1"), "")

    def test_a_failing_output_is_not_trusted_even_when_signed(self):
        self.sign(FAILING)
        self.assertEqual(RN.trusted_receipt(self.repo, "1"), "")
        self.sign("3 passed, 0 failed\nsmoke: FAIL the window did not open")
        self.assertEqual(RN.trusted_receipt(self.repo, "1"), "")

    def test_no_key_no_file_or_an_unreadable_file_is_not_trusted(self):
        self.sign()
        C.RUNTIME.pop("receipt_keys")
        self.assertEqual(RN.trusted_receipt(self.repo, "1"), "")
        X.receipt_key(self.slot)
        self.receipt_path().unlink()
        self.assertEqual(RN.trusted_receipt(self.repo, "1"), "")
        self.receipt_path().write_text("{not json", encoding="utf-8")
        self.assertEqual(RN.trusted_receipt(self.repo, "1"), "")

    def test_the_gate_skips_its_test_run_for_a_trusted_receipt(self):
        self.sign()
        found = {"test_seconds": 0.0, "output": ""}
        with mock.patch.object(RN, "locked_test_run", return_value=FAILING) as run:
            reason, totals = RN.gate(self.slot, SPEC, False, found)
        run.assert_not_called()
        self.assertEqual((reason, totals), ("", "3 passed, 0 failed"))
        self.assertTrue(found["receipt"])
        self.assertEqual(found["output"], PASSING)

    def test_the_gate_runs_the_tests_when_the_receipt_is_stale(self):
        self.sign()
        write(self.repo, "game/src/core/world.gd", WORLD_GD + "\n# changed after the run\n")
        found = {"test_seconds": 0.0, "output": ""}
        with mock.patch.object(RN, "locked_test_run", return_value=FAILING) as run:
            reason, _ = RN.gate(self.slot, SPEC, False, found)
        run.assert_called_once_with(self.repo)
        self.assertIn("the driver's own test run", reason)
        self.assertIn("test_end_round", reason)
        self.assertNotIn("receipt", found)

    def test_check_passed_records_that_the_receipt_was_used(self):
        self.sign()
        with mock.patch.object(RN, "locked_test_run") as run:
            self.assertEqual(RN.check_passed(self.slot, SPEC), ("", "3 passed, 0 failed"))
        run.assert_not_called()
        gates = [json.loads(line) for line in (C.LOGS / "events.jsonl").read_text(encoding="utf-8").splitlines()
                 if '"event": "gate"' in line]
        self.assertTrue(gates[-1]["receipt"])
        self.assertEqual(gates[-1]["reason"], "")


# ---------------------------------------------------------------------------------------------------------------------
# 3. A task session's hook settings, and the test run's environment
# ---------------------------------------------------------------------------------------------------------------------


class TaskSettingsTest(GenericTest):
    def setUp(self):
        super().setUp()
        self.slot = fake_slot("2", root=self.repo, base=self.base)
        self.other = self.temp / "slots" / "1"
        patch = mock.patch.object(SL, "other_roots", return_value=[C.PROJECT, self.other])
        patch.start()
        self.addCleanup(patch.stop)

    def test_the_hook_file_holds_the_slots_arguments_and_a_stale_receipt_goes(self):
        stale = C.LOGS / "receipt-2.json"
        stale.write_text("{}", encoding="utf-8")
        settings = json.loads(X.task_settings(self.slot, 1234.5))
        self.assertFalse(stale.exists())
        hook = json.loads((C.LOGS / "hook-2.json").read_text(encoding="utf-8"))
        self.assertEqual(hook["root"], str(self.repo))
        self.assertEqual(hook["slot"], "2")
        self.assertEqual(hook["leases"], str(SL.LEASES))
        self.assertEqual(hook["forbid"], [str(C.PROJECT), str(self.other)])
        self.assertEqual(hook["receipt"], str(stale))
        self.assertEqual(hook["test_call"], C.TEST_CALL.pattern)
        self.assertEqual(hook["partial"], list(C.CFG.partial_test_flags))
        self.assertEqual(hook["scope"], list(C.SCOPE))
        self.assertEqual(hook["no_whole_read"], [C.TASKS_REL, C.PROGRESS_REL])
        self.assertEqual(hook["fail_marker"], ["smoke: FAIL"])
        self.assertEqual((hook["totals_marker"], hook["pass_marker"]), ("passed, ", " 0 failed"))
        self.assertEqual(hook["full_timeout_ms"], 600000)
        self.assertEqual(hook["deadline"], 1234.5)
        self.assertEqual(hook["logs"], C.LOGS_REL)
        self.assertAlmostEqual(hook["started"], time.time(), delta=30)
        self.assertIn(H.RECEIPT_KEY_ENV, settings["env"])

    def test_the_settings_hook_every_tool_before_and_bash_after(self):
        settings = json.loads(X.task_settings(self.slot, 0.0))
        pre = settings["hooks"]["PreToolUse"][0]
        post = settings["hooks"]["PostToolUse"][0]
        self.assertEqual(pre["matcher"], X.HOOKED_TOOLS)
        self.assertIn(f'--config "{C.LOGS / "hook-2.json"}"', pre["hooks"][0]["command"])
        self.assertEqual(post["matcher"], "Bash")
        self.assertTrue(post["hooks"][0]["command"].endswith(" --post"))
        self.assertEqual(post["hooks"][0]["command"], pre["hooks"][0]["command"] + " --post")
        self.assertEqual(settings["env"][H.RECEIPT_KEY_ENV], C.RUNTIME["receipt_keys"]["2"])
        self.assertEqual(settings["env"]["BASH_DEFAULT_TIMEOUT_MS"], X.BASH_TIMEOUT_MS)

    def test_the_hook_file_is_what_the_hook_parses(self):
        X.task_settings(self.slot, 99.0)
        args = H.parse(["--config", str(C.LOGS / "hook-2.json")])
        self.assertEqual((args.root, args.slot, args.deadline), (str(self.repo), "2", 99.0))
        self.assertEqual(args.test_call, C.TEST_CALL.pattern)
        self.assertTrue(H.full_run(TEST_CALL, args.test_call, args.partial))

    def test_without_a_slot_the_main_tree_has_no_receipt(self):
        settings = json.loads(X.task_settings(None, 0.0))
        hook = json.loads((C.LOGS / "hook-main.json").read_text(encoding="utf-8"))
        self.assertEqual(hook["root"], str(C.PROJECT))
        self.assertNotIn("receipt", hook)
        self.assertNotIn(H.RECEIPT_KEY_ENV, settings["env"])

    def test_the_real_hooks_write_a_receipt_the_gate_trusts_then_refuse_a_second_run(self):
        """The commands of the settings, run as Claude Code runs them: the event on stdin, the settings' env."""
        settings = json.loads(X.task_settings(self.slot, time.time() + 600))
        env = {**os.environ, **settings["env"]}
        post = settings["hooks"]["PostToolUse"][0]["hooks"][0]["command"]
        done = subprocess.run(post, shell=True, input=json.dumps(bash_event(TEST_CALL, PASSING + "\n")), text=True,
                              capture_output=True, cwd=self.repo, env=env, timeout=60)
        self.assertEqual((done.returncode, done.stdout), (0, ""), done.stderr)
        self.assertEqual(RN.trusted_receipt(self.repo, "2"), PASSING)
        pre = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        event = {"tool_name": "Bash", "tool_input": {"command": TEST_CALL, "timeout": 600000}}
        done = subprocess.run(pre, shell=True, input=json.dumps(event), text=True, capture_output=True, cwd=self.repo,
                              env=env, timeout=60)
        self.assertIn(H.SECOND_RUN_REASON, json.loads(done.stdout)["hookSpecificOutput"]["permissionDecisionReason"])

    def test_test_env_is_empty_without_test_jobs_env(self):
        with mock.patch.object(C.CFG, "test_jobs_env", ""):
            self.assertEqual(X.test_env(), {})
            self.assertEqual(set(json.loads(X.task_settings(self.slot, 0.0))["env"]),
                             {"BASH_DEFAULT_TIMEOUT_MS", "BASH_MAX_TIMEOUT_MS", H.RECEIPT_KEY_ENV})

    def test_test_env_shares_the_cpus_among_the_slots(self):
        with mock.patch.object(C.CFG, "test_jobs_env", "LOOP_TEST_JOBS"), \
                mock.patch.object(X.os, "cpu_count", return_value=16):
            with mock.patch.object(C, "PARALLEL_SLOTS", 1):
                self.assertEqual(X.test_env(), {"LOOP_TEST_JOBS": "8"})
                self.assertEqual(json.loads(X.task_settings(self.slot, 0.0))["env"]["LOOP_TEST_JOBS"], "8")
            with mock.patch.object(C, "PARALLEL_SLOTS", 3):
                self.assertEqual(X.test_jobs(), "4")
            with mock.patch.object(C, "PARALLEL_SLOTS", 15):
                self.assertEqual(X.test_jobs(), "2")
        with mock.patch.object(X.os, "cpu_count", return_value=64), mock.patch.object(C, "PARALLEL_SLOTS", 1):
            self.assertEqual(X.test_jobs(), "8")
        with mock.patch.object(X.os, "cpu_count", return_value=None), mock.patch.object(C, "PARALLEL_SLOTS", 1):
            self.assertEqual(X.test_jobs(), "2")


# ---------------------------------------------------------------------------------------------------------------------
# 4. Companion files and gate checks
# ---------------------------------------------------------------------------------------------------------------------


class CompanionTest(DriverTest):
    def setUp(self):
        super().setUp()
        patch = mock.patch.object(C.CFG, "companion_files", {".gd": ".uid"})
        patch.start()
        self.addCleanup(patch.stop)

    def test_a_scope_file_without_its_companion_is_missing_one(self):
        def nothing(path):
            return False
        world = "game/src/core/world.gd"
        self.assertEqual(RN.missing_companions([world], nothing), [world])
        self.assertEqual(RN.missing_companions([world, world + ".uid"], nothing), [])
        self.assertEqual(RN.missing_companions([world], lambda path: path == world + ".uid"), [])
        self.assertEqual(RN.missing_companions([world + ".uid", "game/content/units.csv"], nothing), [])

    def test_only_scope_paths_count(self):
        self.assertEqual(RN.missing_companions(["tools/helper.gd"], lambda path: False), [])
        self.assertEqual(RN.orphan_companions(["tools/helper.gd.uid"], lambda path: False), [])

    def test_a_companion_whose_file_is_gone_is_an_orphan(self):
        old = "game/src/core/old.gd"
        self.assertEqual(RN.orphan_companions([old + ".uid"], lambda path: False), [old + ".uid"])
        self.assertEqual(RN.orphan_companions([old + ".uid", old], lambda path: False), [])
        self.assertEqual(RN.orphan_companions([old + ".uid"], lambda path: path == old), [])

    def test_a_deleted_file_leaves_its_companion_an_orphan(self):
        gone = "game/src/core/gone.gd"
        self.assertEqual(RN.orphan_companions([gone], lambda path: path == gone + ".uid"), [gone + ".uid"])
        self.assertEqual(RN.orphan_companions([gone], lambda path: False), [])
        self.assertEqual(RN.orphan_companions([gone], lambda path: True), [])

    def test_no_companion_files_no_findings(self):
        with mock.patch.object(C.CFG, "companion_files", {}):
            self.assertEqual(RN.missing_companions(["game/src/core/world.gd"], lambda path: False), [])
            self.assertEqual(RN.orphan_companions(["game/src/core/world.gd.uid"], lambda path: False), [])


class GateCheckTest(DriverTest):
    def setUp(self):
        super().setUp()
        self.root = self.temp / "tree"
        self.root.mkdir()

    def test_check_command_runs_python_as_this_interpreter(self):
        for python in ("python", "python3", "py"):
            self.assertEqual(RN.check_command({"command": [python, "tools/check.py"]}, ["a.gd"]),
                             [sys.executable, "tools/check.py"])
        self.assertEqual(RN.check_command({"command": ["node", "check.js"]}, ["a.gd"]), ["node", "check.js"])
        self.assertEqual(RN.check_command({}, ["a.gd"]), [])

    def test_check_command_appends_the_paths_when_asked(self):
        check = {"command": ["python", "tools/check.py", "--strict"], "pass_paths": True}
        self.assertEqual(RN.check_command(check, ["game/src/a.gd", "game/src/b.gd"]),
                         [sys.executable, "tools/check.py", "--strict", "game/src/a.gd", "game/src/b.gd"])
        command = ["python", "tools/check.py"]
        RN.check_command({"command": command, "pass_paths": True}, ["x.gd"])
        self.assertEqual(command, ["python", "tools/check.py"])

    def test_a_string_command_stays_a_string(self):
        self.assertEqual(RN.check_command({"command": "lint --strict"}, ["game/src/a.gd"]), "lint --strict")
        self.assertEqual(RN.check_command({"command": "lint", "pass_paths": True}, ["game/src/a.gd", "game/src/b.gd"]),
                         "lint game/src/a.gd game/src/b.gd")

    def python(self, code, **check):
        return {"name": "lint", "command": ["python", "-c", code], **check}

    def test_a_passing_check_gives_nothing(self):
        self.assertEqual(RN.gate_check(self.root, self.python("print('ok')"), ["game/src/a.gd"]), "")

    def test_a_failing_check_gives_its_last_lines_and_the_names_fail_regex_finds(self):
        code = ("import sys\nfor line in ['checking', 'line two', 'FAIL world_a', 'FAIL world_b', 'FAIL world_a']:\n"
                "    print(line)\nsys.exit(3)")
        reason = RN.gate_check(self.root, self.python(code, fail_regex=r"^FAIL (\w+)"), ["game/src/a.gd"])
        self.assertEqual(reason, "lint: FAIL world_a; FAIL world_b; FAIL world_a; failing: world_a, world_b")
        self.assertEqual(RN.gate_check(self.root, self.python("import sys; sys.exit(4)"), []), "lint: exit 4")

    def test_a_check_runs_in_the_tree_with_its_paths_and_the_test_env(self):
        code = (f"import os, sys; print('in tree', os.path.samefile(os.getcwd(), {str(self.root)!r})); "
                "print(sys.argv[1:]); print('jobs', os.environ.get('LOOP_TEST_JOBS')); sys.exit(1)")
        check = self.python(code, paths=["game/src/"], pass_paths=True)
        with mock.patch.object(C.CFG, "test_jobs_env", "LOOP_TEST_JOBS"):
            reason = RN.gate_check(self.root, check, ["game/src/a.gd", "game/content/units.csv"])
        self.assertEqual(reason, f"lint: in tree True; ['game/src/a.gd']; jobs {X.test_jobs()}")

    def test_a_check_whose_paths_match_nothing_is_skipped(self):
        check = self.python("import sys; sys.exit(1)", paths=["game/content/*.csv"])
        self.assertEqual(RN.gate_check(self.root, check, ["game/src/a.gd"]), "")
        self.assertIn("lint", RN.gate_check(self.root, check, ["game/content/units.csv"]))

    def test_a_slow_check_times_out_and_a_missing_program_cannot_run(self):
        check = self.python("import time; time.sleep(10)", name="slow", timeout=0.5)
        started = time.time()
        self.assertEqual(RN.gate_check(self.root, check, []), "slow: timed out after 0.5 s")
        self.assertLess(time.time() - started, 8)
        missing = RN.gate_check(self.root, {"name": "gone", "command": ["no-such-program-for-the-gate"]}, [])
        self.assertTrue(missing.startswith("gone: could not run"), missing)


# ---------------------------------------------------------------------------------------------------------------------
# 5. The monitor
# ---------------------------------------------------------------------------------------------------------------------


def record(clean="pass", **exits):
    return {"head": "h", "clean": clean,
            "commands": {name: {"exit": code, "tail": f"{name} said {code}"} for name, code in exits.items()}}


class MonitorCommandsTest(DriverTest):
    def test_commands_take_monitor_commands_then_monitor_command(self):
        listed = [{"name": "bench", "command": ["python", "bench.py"], "timeout": 60}, {"command": "smoke --fast"},
                  {"name": "no command"}]
        with mock.patch.object(C.CFG, "monitor_commands", listed), \
                mock.patch.object(C.CFG, "monitor_command", ["python", "slow_suite.py"]):
            found = MO.commands()
        self.assertEqual(found, [{"name": "bench", "command": ["python", "bench.py"], "timeout": 60},
                                 {"name": "command 2", "command": "smoke --fast"},
                                 {"name": "monitor", "command": ["python", "slow_suite.py"]}])
        self.assertNotIn("name", listed[1])

    def test_no_commands_configured(self):
        with mock.patch.object(C.CFG, "monitor_commands", []), mock.patch.object(C.CFG, "monitor_command", []):
            self.assertEqual(MO.commands(), [])

    def test_filled_puts_in_head_previous_and_commits(self):
        values = {"head": "abc1234567", "previous": "def7654321", "commits": 3}
        self.assertEqual(MO.filled("bench --head {head} --base {previous} --n {commits}", values),
                         "bench --head abc1234567 --base def7654321 --n 3")
        self.assertEqual(MO.filled(["python", "bench.py", "{head}", "--since={previous}", "{commits}"], values),
                         [sys.executable, "bench.py", "abc1234567", "--since=def7654321", "3"])
        self.assertEqual(MO.filled(["node", "{unknown}"], values), ["node", "{unknown}"])


class MonitorJudgeTest(unittest.TestCase):
    def test_nothing_worse_is_no_alert(self):
        self.assertEqual(MO.judge([record(bench=0)], record(bench=0)), [])
        self.assertEqual(MO.judge([], record(bench=0)), [])

    def test_a_command_that_starts_failing_alerts(self):
        found = MO.judge([record(bench=0)], record(bench=2))
        self.assertEqual(len(found), 1)
        self.assertIn("bench exits 2", found[0])
        self.assertIn("bench said 2", found[0])
        self.assertEqual(len(MO.judge([], record(bench=2))), 1)

    def test_a_command_that_was_already_failing_does_not_alert_again(self):
        self.assertEqual(MO.judge([record(bench=0), record(bench=2)], record(bench=2)), [])

    def test_the_last_record_that_ran_the_command_is_the_one_compared(self):
        history = [record(bench=1), record(smoke=0)]
        self.assertEqual(MO.judge(history, record(bench=1, smoke=0)), [])
        self.assertEqual(len(MO.judge([record(bench=0), record(smoke=0)], record(bench=1))), 1)

    def test_a_failing_clean_checkout_always_alerts(self):
        found = MO.judge([record("1 passed, 1 failed")], record("1 passed, 1 failed", bench=0))
        self.assertEqual(found, ["the clean checkout's tests fail: 1 passed, 1 failed"])


class MonitorRunTest(GenericTest):
    def setUp(self):
        super().setUp()
        self.first = self.base
        write(self.repo, "game/src/core/world.gd", WORLD_GD + "\n# second\n")
        git(self.repo, "commit", "-q", "-am", "T-02: second")
        self.second = git(self.repo, "rev-parse", "HEAD")

    def history(self, *records):
        MO.HISTORY.write_text("".join(json.dumps(item) + "\n" for item in records), encoding="utf-8")

    def status(self):
        with mock.patch.object(SL, "main_tip", return_value=self.second):
            return MO.status_lines(self.repo)

    def test_status_lines_without_a_record(self):
        self.assertEqual(self.status(), ["No monitor record yet. `python <runner>/driver --monitor` makes one."])

    def test_status_lines_of_a_fresh_record(self):
        when = time.strftime("%Y-%m-%d %H:%M:%S")
        self.history({**record(bench=0), "head": self.first[:10], "time": when})
        line, = self.status()
        self.assertEqual(line, f"Last record: `{self.first[:10]}` on {when}, 1 commit(s) behind the main branch; "
                               "everything passed.")

    def test_status_lines_of_an_old_record_behind_the_tip(self):
        when = (datetime.datetime.now() - datetime.timedelta(days=5)).strftime("%Y-%m-%d %H:%M:%S")
        self.history({**record("2 passed, 1 failed", bench=3), "head": self.first[:10], "time": when})
        line, = self.status()
        self.assertIn("1 commit(s) behind", line)
        self.assertIn("failing: the clean checkout, bench.", line)
        self.assertIn("It is 5 days old", line)

    def test_an_old_record_of_the_tip_is_not_stale(self):
        when = (datetime.datetime.now() - datetime.timedelta(days=5)).strftime("%Y-%m-%d %H:%M:%S")
        self.history({**record(bench=0), "head": self.second[:10], "time": when})
        line, = self.status()
        self.assertIn("0 commit(s) behind", line)
        self.assertNotIn("days old", line)

    def test_status_lines_with_git_mocked_and_a_bad_time(self):
        self.history({**record(bench=0), "head": "abcdef1234", "time": "yesterday"})
        failed = mock.Mock(returncode=128, stdout="")
        with mock.patch.object(SL, "main_tip", return_value=""), mock.patch.object(C, "git", return_value=failed):
            line, = MO.status_lines()
        self.assertIn("? commit(s) behind", line)
        self.assertNotIn("days old", line)

    def test_watch_records_each_run_and_alerts_once_when_a_command_gets_worse(self):
        slot = fake_slot("monitor", root=self.repo, base=self.first)
        bench = {"name": "bench", "command": ["python", "bench.py", "--head", "{head}", "--base", "{previous}",
                                              "--commits", "{commits}"], "timeout": 30}
        slower = "\n".join(f"case {index}: ok" for index in range(8)) + "\nbench: 30 ms, 3x slower than before"
        results = [(0, "bench: 10 ms"), (3, slower), (3, slower)]
        with mock.patch.object(C.CFG, "monitor_commands", [bench]), mock.patch.object(C.CFG, "monitor_command", []), \
                mock.patch.object(C, "test_output", return_value=PASSING) as tests, \
                mock.patch.object(C, "command_result", side_effect=results) as run:
            first = MO.watch(slot, "T-01", ["T-01"])
            self.assertEqual(self.posts, [])
            slot.base = self.second
            second = MO.watch(slot, "T-02", ["T-02"])
            MO.watch(slot, "T-03", ["T-03"])
        head, previous = self.second[:10], self.first[:10]
        self.assertEqual(run.call_args_list[0], mock.call(
            [sys.executable, "bench.py", "--head", previous, "--base", previous, "--commits", "0"], self.repo, 30,
            X.test_env(), what="bench"))
        self.assertEqual(run.call_args_list[1][0][0],
                         [sys.executable, "bench.py", "--head", head, "--base", previous, "--commits", "1"])
        tests.assert_called_with(root=self.repo, env=X.test_env())
        self.assertEqual(first["commands"], {"bench": {"exit": 0, "tail": "bench: 10 ms"}})
        self.assertEqual((second["head"], second["previous"], second["clean"]), (head, previous, "pass"))
        self.assertEqual(second["commands"]["bench"]["tail"].splitlines(),
                         slower.splitlines()[-MO.TAIL_LINES:])
        lines = [json.loads(line) for line in MO.HISTORY.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([item["head"] for item in lines], [previous, head, head])
        self.assertEqual([item["tasks"] for item in lines], [["T-01"], ["T-02"], ["T-03"]])
        self.assertEqual(len(self.posts), 1)
        title, body, _ = self.posts[0]
        self.assertEqual(title, "Monitor got worse")
        self.assertIn("bench exits 3", body)
        self.assertIn(f"at {head}", body)
        self.assertIn("T-02", body)
        self.assertIn("3x slower", (C.LOGS / "monitor-bench.log").read_text(encoding="utf-8"))
        runs = [json.loads(line) for line in (C.LOGS / "events.jsonl").read_text(encoding="utf-8").splitlines()
                if '"monitor_run"' in line]
        self.assertEqual([bool(item["worse"]) for item in runs], [False, True, False])

    def test_watch_alerts_when_the_clean_checkout_fails(self):
        slot = fake_slot("monitor", root=self.repo, base=self.second)
        with mock.patch.object(C.CFG, "monitor_commands", []), mock.patch.object(C.CFG, "monitor_command", []), \
                mock.patch.object(C, "test_output", return_value=FAILING):
            found = MO.watch(slot, "T-02")
        self.assertEqual(found["clean"], "2 passed, 1 failed")
        self.assertEqual(found["commands"], {})
        self.assertEqual(len(self.posts), 1)
        self.assertIn("the clean checkout's tests fail: 2 passed, 1 failed", self.posts[0][1])


# ---------------------------------------------------------------------------------------------------------------------
# 6. The task, progress and status files
# ---------------------------------------------------------------------------------------------------------------------

ROW = "| 2026-11-03 | T-03 | done | 5 passed, 0 failed | third |"
HEADER = "| Date | Task | Result | Tests | Notes |\n|---|---|---|---|---|\n"


class BookkeepingTest(DriverTest):
    def setUp(self):
        super().setUp()
        self.root = self.temp / "tree"

    def progress(self, text=None):
        path = self.root / C.PROGRESS_REL
        if text is not None:
            write(self.root, C.PROGRESS_REL, text)
        return path.read_text(encoding="utf-8")

    def tasks(self, text=None):
        if text is not None:
            write(self.root, C.TASKS_REL, text)
        return (self.root / C.TASKS_REL).read_text(encoding="utf-8")

    def test_a_row_goes_after_the_last_row(self):
        self.progress("# Progress\n\n" + HEADER + "| 2026-11-01 | T-01 | done | 3 passed, 0 failed | a |\n"
                      "| 2026-11-02 | T-02 | done | 4 passed, 0 failed | b |\n\n## Blocked\n\n(task id: what failed)\n")
        T.add_progress_row(ROW, self.root)
        lines = self.progress().split("\n")
        self.assertEqual(lines[lines.index(ROW) - 1], "| 2026-11-02 | T-02 | done | 4 passed, 0 failed | b |")
        self.assertEqual(lines[lines.index(ROW) + 1], "")
        self.assertLess(lines.index(ROW), lines.index("## Blocked"))
        self.assertEqual([row[1] for row in T.PROGRESS_ROW.findall(self.progress())], ["T-01", "T-02", "T-03"])

    def test_a_table_without_rows_gets_the_row_after_its_header(self):
        self.progress("# Progress\n\n" + HEADER + "\n## Blocked\n")
        T.add_progress_row(ROW, self.root)
        lines = self.progress().split("\n")
        self.assertEqual(lines[lines.index(ROW) - 1], "|---|---|---|---|---|")
        self.assertLess(lines.index(ROW), lines.index("## Blocked"))

    def test_a_file_without_a_table_gets_one(self):
        self.progress("# Progress\n\nNotes only.\n")
        T.add_progress_row(ROW, self.root)
        lines = self.progress().split("\n")
        self.assertEqual(lines[lines.index(ROW) - 2:lines.index(ROW) + 1],
                         ["| Date | Task | Result | Tests | Notes |", "|---|---|---|---|---|", ROW])
        self.assertIn("Notes only.", lines)
        self.assertEqual(T.PROGRESS_ROW.findall(self.progress())[0][1], "T-03")

    def test_a_missing_progress_file_is_made_with_the_table(self):
        T.add_progress_row(ROW, self.root)
        self.assertIn(HEADER + ROW, self.progress())

    def test_add_blocked_adds_the_missing_section(self):
        self.progress("# Progress\n\n" + HEADER + "| 2026-11-01 | T-01 | done | 3 passed, 0 failed | a |\n")
        T.add_blocked("T-02", "the gate failed: 1 failed", self.root)
        text = self.progress()
        self.assertIn("## Blocked\n\n- T-02: the gate failed: 1 failed", text)
        self.assertEqual(T.blocked_reasons(self.root), {"T-02": "the gate failed: 1 failed"})
        T.add_blocked("T-04", "again", self.root)
        self.assertEqual(T.blocked_reasons(self.root), {"T-02": "the gate failed: 1 failed", "T-04": "again"})
        self.assertEqual(self.progress().count("## Blocked"), 1)

    def test_add_blocked_into_an_existing_section(self):
        self.progress("# Progress\n\n## Blocked\n\n(task id: what failed)\n\n## Open questions\n")
        T.add_blocked("T-02", "no Done when", self.root)
        lines = self.progress().split("\n")
        self.assertLess(lines.index("- T-02: no Done when"), lines.index("## Open questions"))
        self.assertEqual(T.blocked_reasons(self.root), {"T-02": "no Done when"})

    def test_add_open_question_adds_the_missing_section(self):
        self.progress("# Progress\n\n## Blocked\n\n")
        T.add_open_question("T-01", "picked 3 rounds per match", self.root)
        today = datetime.date.today().isoformat()
        self.assertTrue(self.progress().endswith(f"## Open questions\n- T-01 ({today}): picked 3 rounds per match\n"),
                        self.progress())
        self.assertIn("## Blocked", self.progress())

    def test_add_open_question_goes_before_resolved(self):
        self.progress("# Progress\n\n## Open questions\n\n- T-00 (2026-11-01): older\n\n### Resolved\n\n- old one\n")
        T.add_open_question("T-01", "picked 3", self.root)
        lines = self.progress().split("\n")
        mine = next(index for index, line in enumerate(lines) if line.startswith("- T-01 ("))
        self.assertEqual(lines[mine - 1], "- T-00 (2026-11-01): older")
        self.assertLess(mine, lines.index("### Resolved"))

    def test_set_heading_and_reopen_treat_ids_as_text(self):
        self.tasks("# Tasks\n\n## M1\n\n### [ ] T-102 Other\n\n### [ ] T-1.2 Dotted\n\n### [!] T-011 Blocked other\n\n"
                   "### [!] T-01+ Plus\n\n### [ ] T-01(b) Bracketed\n")
        self.progress("# Progress\n\n## Blocked\n\n- T-011: other reason\n- T-01+: plus reason\n")
        T.set_heading("T-1.2", "x", self.root)
        T.set_heading("T-01(b)", "!", self.root)
        text = self.tasks()
        self.assertIn("### [ ] T-102 Other", text)
        self.assertIn("### [x] T-1.2 Dotted", text)
        self.assertIn("### [!] T-01(b) Bracketed", text)
        with self.assertRaises(RuntimeError):
            T.set_heading("T-1.2", "x", self.root)
        self.assertTrue(T.reopen("T-01+", self.root))
        text = self.tasks()
        self.assertIn("### [ ] T-01+ Plus", text)
        self.assertIn("### [!] T-011 Blocked other", text)
        self.assertEqual(T.blocked_reasons(self.root), {"T-011": "other reason"})
        self.assertFalse(T.reopen("T-01+", self.root))
        self.assertFalse(T.reopen("T-77", self.root))

    def test_status_text_has_the_monitor_and_the_unparsed_headings(self):
        self.tasks("# Tasks\n\n## M1\n\n### [ ] T-01 First\n\n### [!] T-02 Second\n\n### [ ] T-03\n\n"
                   "### [xx] T-04 Two marks\n")
        self.progress("# Progress\n\n" + HEADER + "| 2026-11-01 | T-00 | done | 1 passed, 0 failed | a |\n\n"
                      "## Blocked\n\n- T-02: the gate failed\n")
        text = T.status_text(self.root)
        self.assertIn("## Unparsed headings (2)\n\n- ### [ ] T-03\n- ### [xx] T-04 Two marks\n", text)
        self.assertIn("## Monitor\n\n" + MO.status_lines(self.root)[0] + "\n", text)
        self.assertIn("No monitor record yet", text)
        self.assertIn("1. **T-01** · First  ← next", text)
        self.assertIn("- **T-02** · Second\n  - the gate failed", text)
        self.assertLess(text.index("## Monitor"), text.index("## Done (1)"))

    def test_write_status_without_a_status_file_writes_nothing(self):
        self.tasks("# Tasks\n\n### [ ] T-01 First\n")
        with mock.patch.object(C, "STATUS_REL", ""):
            self.assertIsNone(T.status_file(self.root))
            T.write_status(self.root)
        self.assertFalse((self.root / "loop" / "STATUS.md").exists())
        T.write_status(self.root)
        self.assertIn("**T-01** · First", (self.root / C.STATUS_REL).read_text(encoding="utf-8"))

    def test_the_status_file_is_bookkeeping(self):
        self.assertEqual(SL.BOOKKEEPING, [C.TASKS_REL, C.PROGRESS_REL, C.STATUS_REL])
        self.assertIn("loop/STATUS.md", SL.BOOKKEEPING)


# ---------------------------------------------------------------------------------------------------------------------
# 7. The test-call pattern
# ---------------------------------------------------------------------------------------------------------------------


class TestCallPatternTest(unittest.TestCase):
    def pattern(self, command, regex=""):
        with mock.patch.object(C, "TEST_COMMAND_TEXT", command), mock.patch.object(C.CFG, "test_call_regex", regex):
            return C.test_call_pattern()

    def check(self, pattern, runs, names_only):
        for command in runs:
            self.assertTrue(re.search(pattern, command, re.I), f"{command!r} should run the tests ({pattern})")
        for command in names_only:
            self.assertFalse(re.search(pattern, command, re.I), f"{command!r} only names them ({pattern})")

    def test_a_python_script(self):
        self.check(self.pattern("python tests/run.py"),
                   ["python tests/run.py", "python tests/run.py -k x", "python3 tests/run.py",
                    "git status && python tests/run.py", "(python tests/run.py)", "C:/tools/python.exe tests/run.py"],
                   ["grep run.py tests", "cat tests/run.py", "python tests/run.py.bak", "python tests/rerun.py.txt",
                    "echo python tests/run.py"])

    def test_the_py_launcher_runs_the_same_script(self):
        """py is in core.INTERPRETERS, and the Windows launcher is how a session there often runs a script."""
        self.check(self.pattern("python tests/run.py"), ["py tests/run.py"], [])

    def test_a_command_without_a_script_is_its_words(self):
        self.check(self.pattern("npm test"), ["npm test", "npm test -- --grep world", "ls; npm test", "npm  test"],
                   ["npm testing", "echo npm test", "npm run build"])

    def test_a_bare_script_path(self):
        self.check(self.pattern("./scripts/test.sh"),
                   ["./scripts/test.sh", "scripts/test.sh --only world", "ls && ./scripts/test.sh"],
                   ["cat scripts/test.sh", "grep -n x scripts/test.sh", "./scripts/test.sh.orig"])

    def test_an_explicit_regex_wins(self):
        self.assertEqual(self.pattern("python tests/run.py", r"make\s+check"), r"make\s+check")
        self.check(self.pattern("python tests/run.py", r"make\s+check"), ["make check"], ["python tests/run.py"])

    def test_no_command_matches_nothing(self):
        self.check(self.pattern(""), [], ["", "python tests/run.py", "npm test"])

    def test_the_fixture_test_call(self):
        self.check(C.TEST_CALL.pattern, [TEST_CALL, f"{TEST_CALL} --only world", f"cd . && {TEST_CALL}"],
                   ["cat game/tools/run_tests.py"])


# ---------------------------------------------------------------------------------------------------------------------
# 8. The prompts' placeholders
# ---------------------------------------------------------------------------------------------------------------------

PLACEHOLDER = re.compile(r"<[a-z_]+>")
PROMPTS = {"WORKER_PROMPT": {"rules": "R", "task": "T"}, "OPUS_TASK_PROMPT": {"rules": "R", "task": "T"},
           "TRIAGE_PROMPT": {"blocked": "- T-01: x"}, "OPUS_WRITE_PROMPT": {"rules": "R", "instruction": "I"}}


class PromptTest(unittest.TestCase):
    def test_every_placeholder_in_the_prompts_is_filled(self):
        for name in PROMPTS:
            self.assertEqual(PLACEHOLDER.findall(getattr(prompts, name)), [], name)

    def test_project_knows_every_placeholder_its_templates_use(self):
        """MAN_TEXT and OPUS_PROMPT keep words like <code> for the reader; only project()'s templates are filled."""
        source = pathlib.Path(prompts.__file__).read_text(encoding="utf-8")
        templates = re.findall(r'project\("""(.*?)"""\)', source, re.S)
        self.assertEqual(len(templates), len(PROMPTS))
        named = sorted(set(PLACEHOLDER.findall("".join(templates))))
        self.assertIn("<scope>", named)
        self.assertEqual(PLACEHOLDER.findall(prompts.project(" ".join(named))), [])

    def test_the_task_id_in_the_triage_prompt_stays_for_the_model(self):
        self.assertIn(f"{C.LOGS_REL}<task id>.*.md", prompts.TRIAGE_PROMPT)
        self.assertEqual(re.findall(r"<[^<>\n]+>", prompts.TRIAGE_PROMPT), ["<task id>"])

    def test_the_config_values_are_in_the_prompts(self):
        self.assertIn("game/src/, game/tests/, game/content/", prompts.WORKER_PROMPT)
        self.assertIn(f"({C.TEST_COMMAND_TEXT})", prompts.WORKER_PROMPT)
        self.assertIn(", with the Bash timeout parameter at 600000", prompts.WORKER_PROMPT)
        self.assertIn("(CLAUDE.md)", prompts.OPUS_TASK_PROMPT)
        self.assertIn(f"({C.LOOP_DIR})", prompts.OPUS_WRITE_PROMPT)
        self.assertIn(C.PROGRESS_REL, prompts.TRIAGE_PROMPT)

    def test_each_prompt_takes_its_session_values(self):
        for name, values in PROMPTS.items():
            text = getattr(prompts, name).format(**values)
            for value in values.values():
                self.assertIn(value, text, name)

    def test_config_text_with_braces_survives_the_sessions_format(self):
        with mock.patch.object(C.CFG, "project_rules", 'Dictionaries are written {"hp": 3}.'):
            template = prompts.project("Rules:<project_rules>\n\n{rules}\n{task}")
        self.assertIn('{"hp": 3}', template.format(rules="R", task="T"))


if __name__ == "__main__":
    unittest.main()
