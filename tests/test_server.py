"""Тесты бэкенда Айко: песочница файлов, git, подтверждение команд, история, HTTP-защита.

Запуск:  .venv/bin/python -m unittest discover -s tests
Модель не вызывается — вместо неё подставляется сценарий ответов.
"""

import json
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agent as agent_mod  # noqa: E402
import devtools  # noqa: E402
import http_api  # noqa: E402
import workspace  # noqa: E402


def make_project(root: Path) -> None:
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text("def main():\n    return 42\n", encoding="utf-8")
    (root / "README.md").write_text("# Demo\n", encoding="utf-8")
    (root / ".env").write_text("SECRET=1\n", encoding="utf-8")
    (root / "node_modules").mkdir()
    (root / "node_modules" / "junk.js").write_text("x", encoding="utf-8")
    git = ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "init"], check=True)


def scripted(*messages):
    """Подменяет модель: отдаёт заготовленные сообщения по очереди."""
    queue = list(messages)
    return lambda msgs, emit: queue.pop(0)


def tool_call(cid: str, name: str, **args) -> dict:
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / "proj"
        self.root.mkdir()
        make_project(self.root)
        self.notes = self.tmp / "notes"
        # общая память и список проектов — тоже во временной папке
        self._paths = (workspace.SENPAI_PATH, workspace.RECENT_PATH)
        workspace.SENPAI_PATH = self.notes / "_senpai.md"
        workspace.RECENT_PATH = self.notes / "_projects.json"
        self.ws = workspace.Workspace(self.root, self.notes / "proj.md")

    def tearDown(self):
        workspace.SENPAI_PATH, workspace.RECENT_PATH = self._paths
        shutil.rmtree(self.tmp)


class WorkspaceTest(Base):
    def test_reads_inside_project(self):
        self.assertIn("return 42", self.ws.read_file("src/app.py"))

    def test_blocks_escape_and_secrets(self):
        self.assertIn("вне проекта", self.ws.call("read_file", {"path": "../../etc/passwd"}))
        self.assertIn("секреты", self.ws.read_file(".env"))
        self.assertNotIn(".env", self.ws.search("SECRET"))

    def test_ignores_node_modules(self):
        self.assertIn("пропущено", self.ws.list_dir("."))
        self.assertEqual(self.ws.search("x", "*.js"), "Ничего не нашлось.")

    def test_outfit_is_sanitized(self):
        self.ws.set_outfit(name="X" * 99, top_color="red<script>", accessory="rocket", badge_text="PYTHON")
        o = self.ws.outfit()
        self.assertEqual((len(o["name"]), o["top_color"], o["accessory"], o["badge_text"]),
                         (32, "#ffffff", "none", "PYT"))

    def test_remember_is_capped(self):
        for i in range(workspace.MAX_SENPAI_LINES + 5):
            self.ws.remember(f"факт {i}")
        lines = workspace.SENPAI_PATH.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), workspace.MAX_SENPAI_LINES)
        self.assertTrue(lines[-1].endswith(f"факт {workspace.MAX_SENPAI_LINES + 4}"))


class DevtoolsTest(Base):
    def test_git_read_only_tools(self):
        self.assertIn("init", devtools.git_log(self.root))
        self.assertIn("src/app.py", devtools.git_hotspots(self.root))
        (self.root / "new.txt").write_text("hi", encoding="utf-8")
        self.assertIn("?? new.txt", devtools.git_diff(self.root))

    def test_rejects_option_injection(self):
        self.assertIn("Некорректный", devtools.git_show(self.root, "--output=/tmp/pwn"))

    def test_run_command_keeps_only_tail(self):
        out = devtools.run_command(self.root, "seq 1 100000")
        self.assertIn("код выхода 0", out)
        self.assertIn("100000", out)
        self.assertLess(len(out), devtools.MAX_RUN_CHARS + 200)


class AgentTest(Base):
    def make_agent(self, *messages):
        agent = agent_mod.Agent(self.ws, "key", "model")
        agent._llm = scripted(*messages)
        return agent

    def test_confirmation_required(self):
        agent = self.make_agent(
            {"role": "assistant", "content": None, "tool_calls": [
                tool_call("1", "run_command", command="echo yes-ran", reason="t"),
                tool_call("2", "run_command", command="echo never-ran", reason="t")]},
            {"role": "assistant", "content": "готово"},
        )
        answers = iter([True, False])

        def emit(ev):
            if ev["type"] == "confirm":
                threading.Timer(0.05, agent.resolve_confirm, (ev["id"], next(answers))).start()

        agent.run("go", emit)
        results = [m["content"] for m in agent.history if m["role"] == "tool"]
        self.assertIn("yes-ran", results[0])
        self.assertIn("запретил", results[1])

    def test_allow_always_is_exact(self):
        agent = self.make_agent()
        confirms = []

        def emit(ev):
            if ev["type"] == "confirm":
                confirms.append(ev["command"])
                threading.Timer(0.05, agent.resolve_confirm, (ev["id"], True, None, True)).start()

        for command in ("echo ok", "echo ok", "echo ok; echo pwn"):
            agent._llm = scripted(
                {"role": "assistant", "content": None, "tool_calls": [tool_call("1", "run_command", command=command, reason="t")]},
                {"role": "assistant", "content": "готово"},
            )
            agent.run("go", emit)
        # второй раз «echo ok» не спрашивали, а команду с хвостом — спросили
        self.assertEqual(confirms, ["echo ok", "echo ok; echo pwn"])
        self.assertIn("`echo ok`", agent.system_prompt())
        self.ws.set_allowed("echo ok", False)
        self.assertEqual(self.ws.allowed_commands(), ["echo ok; echo pwn"])

    def test_history_repaired_after_disconnect(self):
        agent = self.make_agent({"role": "assistant", "content": None, "tool_calls": [
            tool_call("1", "list_dir", path="."), tool_call("2", "list_dir", path="src")]})
        calls = 0

        def emit(ev):
            nonlocal calls
            calls += 1
            if calls == 2:  # браузер ушёл на втором инструменте
                raise BrokenPipeError

        with self.assertRaises(BrokenPipeError):
            agent.run("go", emit)
        self.assertEqual([m["role"] for m in agent.history], ["user"])

    def test_compaction_keeps_tool_pairs(self):
        agent = self.make_agent()
        agent.history = [{"role": "user", "content": "old"}] + [{"role": "assistant", "content": "a"}] * 5
        agent.history.append({"role": "user", "content": "now"})
        for i in range(50):
            agent.history.append({"role": "assistant", "content": None, "tool_calls": [{"id": str(i)}]})
            agent.history.append({"role": "tool", "tool_call_id": str(i), "content": "y" * 1000})
        agent._compact()
        self.assertEqual(agent.history[0]["content"], "now")
        self.assertTrue(agent.history[1].get("tool_calls"))
        self.assertLess(len(agent.history[2]["content"]), 400)


class HttpTest(Base):
    def setUp(self):
        super().setUp()
        http_api.API_KEY, http_api.MODEL = "key", "model"
        http_api.AGENTS.clear()
        http_api.open_project(self.root)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), http_api.Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        super().tearDown()

    def post(self, path, body, headers=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)

    def test_status(self):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/api/status", timeout=5) as r:
            self.assertEqual(json.load(r)["project"], "proj")

    def test_rejects_foreign_sites(self):
        self.assertEqual(self.post("/api/chat", {"message": "hi"}, {"Origin": "http://evil.com"})[0], 403)
        self.assertEqual(self.post("/api/chat", {"message": "hi"}, {"Content-Type": "text/plain"})[0], 403)
        self.assertEqual(self.post("/api/confirm", {}, {"Host": "evil.com"})[0], 403)

    def test_changes_endpoint(self):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/api/changes", timeout=5) as r:
            self.assertEqual(json.load(r)["git"], True)

    def test_context_endpoint(self):
        status, body = self.post("/api/context", {"path": str(self.root / "src" / "app.py"), "line": 1})
        self.assertEqual((status, body["ok"]), (200, True))
        self.assertEqual(http_api.AGENT.editor["path"], "src/app.py")
        self.assertEqual(self.post("/api/context", {"path": "/tmp"})[1]["ok"], False)

    def test_allowed_can_only_be_revoked(self):
        http_api.AGENT.ws.set_allowed("make test", True)
        self.assertEqual(self.post("/api/allowed", {"command": "make test"}), (200, {"allowed": []}))
        self.assertEqual(self.post("/api/allowed", {"command": "rm -rf ~", "allow": True}), (200, {"allowed": []}))

    def test_switch_project(self):
        other = self.tmp / "other"
        other.mkdir()
        self.assertEqual(self.post("/api/project", {"path": str(other)}), (200, {"ok": True, "project": "other"}))
        self.assertEqual(self.post("/api/project", {"path": "/no/such/dir"})[0], 400)
        self.assertEqual(http_api.recent_projects()[:2], [str(other), str(self.root)])


class QuestAndMemoryTest(Base):
    def test_auto_quest_and_event(self):
        agent = agent_mod.Agent(self.ws, "key", "model")
        agent._llm = scripted(
            {"role": "assistant", "content": None, "tool_calls": [tool_call("1", "write_notes", content="# x")]},
            {"role": "assistant", "content": "[happy] изучила!"},
        )
        events = []
        agent.run("[learn]", events.append, label="[learn]")
        quest = [e for e in events if e["type"] == "quest"]
        self.assertEqual(quest[0]["gained"], 30)
        self.assertEqual(self.ws.quest.state()["level"], "Новичок")
        self.assertIn("уже был", self.ws.complete_quest("learn"))
        self.assertIn("Нет такого шага", self.ws.call("complete_quest", {"step": "hack"}))

    def test_transcript_survives_restart(self):
        agent = agent_mod.Agent(self.ws, "key", "model")
        agent._llm = scripted({"role": "assistant", "content": "привет, семпай"})
        agent.run("привет", lambda ev: None)
        agent._llm = scripted({"role": "assistant", "content": "скучно"})
        agent.run("(служебное)", lambda ev: None, label="[idle]")
        again = agent_mod.Agent(self.ws, "key", "model")  # «перезапуск сервера»
        self.assertEqual([m["content"] for m in again.transcript], ["привет", "привет, семпай", "скучно"])
        self.assertEqual(again.history[0], {"role": "user", "content": "привет"})

    def test_github_remote_parsing(self):
        git = ["git", "-C", str(self.root)]
        self.assertIsNone(devtools.github_repo(self.root))
        subprocess.run([*git, "remote", "add", "origin", "git@github.com:aiko/chan.git"], check=True)
        self.assertEqual(devtools.github_repo(self.root), ("aiko", "chan"))
        subprocess.run([*git, "remote", "set-url", "origin", "https://github.com/a-b/c.d/"], check=True)
        self.assertEqual(devtools.github_repo(self.root), ("a-b", "c.d"))


class GithubTest(Base):
    def setUp(self):
        super().setUp()
        subprocess.run(["git", "-C", str(self.root), "remote", "add", "origin", "https://github.com/a/b"], check=True)
        self._github = devtools._github

    def tearDown(self):
        devtools._github = self._github
        super().tearDown()

    def test_issues_skip_pull_requests(self):
        pr = {"number": 1, "title": "bump", "pull_request": {}}
        issue = {"number": 2, "title": "Опечатка в README", "labels": [{"name": "good first issue"}], "body": "x"}
        devtools._github = lambda path: [pr] * 50 + [issue] * 5
        out = devtools.github_issues(self.root, limit=2)
        self.assertNotIn("bump", out)
        self.assertEqual(out.count("Опечатка"), 2)

    def test_offline_is_reported(self):
        def offline(path):
            raise urllib.error.URLError("no network")
        urlopen = urllib.request.urlopen
        urllib.request.urlopen = lambda *a, **k: offline(None)
        try:
            with self.assertRaisesRegex(RuntimeError, "недоступен"):
                self._github("/repos/a/b/issues")
        finally:
            urllib.request.urlopen = urlopen


class ReminderAndWatchTest(Base):
    def test_reminder_validated_and_emitted(self):
        agent = agent_mod.Agent(self.ws, "key", "model")
        agent._llm = scripted(
            {"role": "assistant", "content": None, "tool_calls": [
                tool_call("1", "set_reminder", minutes=0.1, text="рано"),
                tool_call("2", "set_reminder", minutes=25, text="перерыв   для глаз")]},
            {"role": "assistant", "content": "ок"},
        )
        events = []
        agent.run("помидорка", events.append)
        self.assertEqual([e for e in events if e["type"] == "reminder"],
                         [{"type": "reminder", "minutes": 25.0, "text": "перерыв для глаз"}])

    def test_changes_signature(self):
        clean = devtools.changes(self.root)
        self.assertEqual((clean["git"], clean["sig"]), (True, ""))
        (self.root / "src" / "app.py").write_text("def main():\n    return 7\n", encoding="utf-8")
        first = devtools.changes(self.root)
        self.assertEqual(first["files"], ["src/app.py"])
        (self.root / "new.py").write_text("x = 1\n", encoding="utf-8")
        second = devtools.changes(self.root)
        self.assertNotEqual(first["sig"], second["sig"])
        self.assertIn("new.py", second["files"])


class CommitTest(Base):
    def setUp(self):
        super().setUp()
        for key, value in (("user.name", "t"), ("user.email", "t@t")):
            subprocess.run(["git", "-C", str(self.root), "config", key, value], check=True)
        (self.root / "src" / "app.py").write_text("def main():\n    return 7\n", encoding="utf-8")

    def run_commit(self, approve, edited=None, **args):
        agent = agent_mod.Agent(self.ws, "key", "model")
        agent._llm = scripted(
            {"role": "assistant", "content": None, "tool_calls": [tool_call("1", "commit", **args)]},
            {"role": "assistant", "content": "ок"},
        )
        events = []

        def emit(ev):
            events.append(ev)
            if ev["type"] == "confirm":
                threading.Timer(0.05, agent.resolve_confirm, (ev["id"], approve, edited)).start()

        agent.run("закоммить", emit)
        return [m["content"] for m in agent.history if m["role"] == "tool"][0], events

    def test_nothing_staged_is_not_committed(self):
        result, events = self.run_commit(True, message="fix")
        self.assertIn("в индексе пусто", result)
        self.assertFalse([e for e in events if e["type"] == "confirm"])

    def test_commit_with_edited_message(self):
        before = devtools.head(self.root)
        result, events = self.run_commit(True, "#7 поправила ответ", message="fix", add_all=True)
        confirm = next(e for e in events if e["type"] == "confirm")
        self.assertEqual((confirm["kind"], confirm["files"]), ("commit", ["src/app.py"]))
        self.assertIn("Закоммитила", result)
        head = devtools.head(self.root)
        self.assertNotEqual(head, before)
        self.assertEqual(next(e for e in events if e["type"] == "committed")["head"], head)
        self.assertIn("#7 поправила ответ", devtools.git_log(self.root, max_count=1))
        self.assertEqual(devtools.changes(self.root)["head"], head)

    def test_denied_commit_changes_nothing(self):
        subprocess.run(["git", "-C", str(self.root), "add", "-A"], check=True)
        before = devtools.head(self.root)
        result, _ = self.run_commit(False, message="fix")
        self.assertIn("отказался", result)
        self.assertEqual(devtools.head(self.root), before)


class DiffTest(Base):
    def test_screen_diff(self):
        with self.assertRaisesRegex(ValueError, "нечего"):
            self.ws.diff_snippet()
        (self.root / "src" / "app.py").write_text("def main():\n    return 7\n", encoding="utf-8")
        diff = self.ws.diff_snippet("src/app.py")
        self.assertIn("-    return 42", diff)
        self.assertIn("+    return 7", diff)
        self.assertIn("+# Demo", self.ws.diff_snippet(commit="HEAD"))
        with self.assertRaisesRegex(ValueError, "Некорректный"):
            self.ws.diff_snippet(commit="--output=/tmp/x")
        with self.assertRaisesRegex(ValueError, "Не могу"):
            self.ws.diff_snippet(".env")

    def test_diff_is_trimmed_and_emitted(self):
        (self.root / "big.txt").write_text("".join(f"{i}\n" for i in range(500)), encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "big.txt"], check=True)
        agent = agent_mod.Agent(self.ws, "key", "model")
        agent._llm = scripted(
            {"role": "assistant", "content": None, "tool_calls": [tool_call("1", "show_diff", staged=True)]},
            {"role": "assistant", "content": "вот"},
        )
        events = []
        agent.run("покажи", events.append)
        diff = next(e for e in events if e["type"] == "diff")
        self.assertEqual(diff["title"], "diff · staged")
        self.assertEqual(len(diff["diff"].splitlines()), devtools.MAX_DIFF_LINES + 1)
        self.assertIn("сузь до файла", diff["diff"])


class ProjectMemoryAndEditorTest(Base):
    def test_project_memory_survives_relearn(self):
        agent = agent_mod.Agent(self.ws, "key", "model")
        self.ws.call("remember_project", {"fact": "тесты  запускаются через make test"})
        self.ws.write_notes("# новые заметки")
        prompt = agent.system_prompt()
        self.assertIn("тесты запускаются через make test", prompt)
        self.assertIn("# новые заметки", prompt)
        self.assertFalse(workspace.SENPAI_PATH.exists())  # общая память о семпае не тронута

    def test_editor_context(self):
        agent = agent_mod.Agent(self.ws, "key", "model")
        self.assertFalse(agent.set_editor({"path": "/etc/passwd"}))
        self.assertFalse(agent.set_editor({"path": str(self.root / ".env")}))
        self.assertTrue(agent.set_editor({"path": str(self.root / "src" / "app.py"), "line": 2,
                                          "end_line": 2, "selection": "    return 42"}))
        prompt = agent.system_prompt()
        self.assertIn("`src/app.py`", prompt)
        self.assertIn("return 42", prompt)
        agent.set_editor({"path": None})
        self.assertNotIn("src/app.py", agent.editor_hint())


class VoiceTest(unittest.TestCase):
    def test_emotion_shifts_voice(self):
        import llm
        self.assertEqual(llm._shift("+30Hz", 8, "Hz"), "+38Hz")
        self.assertEqual(llm._shift("+8%", -12, "%"), "-4%")
        self.assertEqual(llm._shift("странно", 5, "Hz"), "странно")
        sad, plain = llm.voice_params("sad"), llm.voice_params("неизвестная")
        self.assertEqual(plain["volume"], "+0%")
        self.assertLess(int(sad["pitch"][:-2]), int(plain["pitch"][:-2]))


class SeasonTest(unittest.TestCase):
    def test_seasons(self):
        from datetime import date
        self.assertIn("Хэллоуин", agent_mod.season_hint(date(2026, 10, 31)))
        self.assertIn("новогодние", agent_mod.season_hint(date(2027, 1, 3)))
        self.assertEqual(agent_mod.season_hint(date(2026, 7, 1)), "")


if __name__ == "__main__":
    unittest.main()
