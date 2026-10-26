"""Тесты бэкенда Айко: песочница файлов, git, подтверждение команд, история, HTTP-защита.

Запуск:  .venv/bin/python -m unittest discover -s tests
Модель не вызывается — вместо неё подставляется сценарий ответов.
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agent as agent_mod  # noqa: E402
import devtools  # noqa: E402
import http_api  # noqa: E402
import twitch  # noqa: E402
import usage  # noqa: E402
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
        # вся память Айко — во временной папке: open_project кладёт заметки проекта в NOTES_DIR
        self._paths = (workspace.NOTES_DIR, workspace.SENPAI_PATH, workspace.RECENT_PATH, workspace.USAGE_PATH,
                       workspace.SKILLS_DIR)
        workspace.NOTES_DIR = self.notes
        workspace.SENPAI_PATH = self.notes / "_senpai.md"
        workspace.RECENT_PATH = self.notes / "_projects.json"
        workspace.USAGE_PATH = self.notes / "_usage.json"
        workspace.SKILLS_DIR = self.tmp / "skills"
        self.ws = workspace.Workspace(self.root, self.notes / "proj.md")

    def tearDown(self):
        (workspace.NOTES_DIR, workspace.SENPAI_PATH, workspace.RECENT_PATH, workspace.USAGE_PATH,
         workspace.SKILLS_DIR) = self._paths
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
        prompts = []
        agent._llm_text = lambda prompt: prompts.append(prompt) or "- договорились про тесты"
        agent.history = [{"role": "user", "content": "old"}] + [{"role": "assistant", "content": "a"}] * 5
        agent.history.append({"role": "user", "content": "now"})
        for i in range(50):
            agent.history.append({"role": "assistant", "content": None, "tool_calls": [{"id": str(i)}]})
            agent.history.append({"role": "tool", "tool_call_id": str(i), "content": "y" * 1000})
        agent._compact()
        self.assertEqual(agent.history[0]["content"], "now")
        self.assertTrue(agent.history[1].get("tool_calls"))
        self.assertLess(len(agent.history[2]["content"]), 400)
        # отрезанное начало ушло в резюме, а резюме — в системный промпт
        self.assertIn("Семпай: old", prompts[0])
        self.assertIn("договорились про тесты", agent.system_prompt())

    def test_summary_merges_and_survives_failure(self):
        agent = self.make_agent()
        self.ws.summary_path.parent.mkdir(parents=True, exist_ok=True)
        self.ws.summary_path.write_text("- старое резюме\n", encoding="utf-8")
        prompts = []
        agent._llm_text = lambda prompt: prompts.append(prompt) or "- новое"
        agent._summarize([{"role": "user", "content": "вопрос"}, {"role": "tool", "content": "шум"}])
        self.assertIn("старое резюме", prompts[0])
        self.assertNotIn("шум", prompts[0])

        def broken(prompt):
            raise RuntimeError("нет сети")
        agent._llm_text = broken
        agent._summarize([{"role": "user", "content": "ещё"}])
        self.assertEqual(self.ws.summary_path.read_text(encoding="utf-8"), "- новое\n")


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

    def test_skill_action(self):
        (workspace.SKILLS_DIR).mkdir(parents=True)
        (workspace.SKILLS_DIR / "hello.md").write_text("# 👋 Привет\nПоздоровайся с командой.", encoding="utf-8")
        http_api.AGENT._llm = scripted({"role": "assistant", "content": "[wave] Привет, команда!"})
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/chat", data=b'{"action": "skill:hello"}',
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            events = [json.loads(line) for line in r.read().splitlines()]
        self.assertEqual(events[-1]["type"], "reply")
        self.assertEqual(http_api.AGENT.history[0]["content"], "Поздоровайся с командой.")
        self.assertEqual(http_api.AGENT.transcript[-1]["content"], "[wave] Привет, команда!")
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/api/status", timeout=5) as r:
            self.assertEqual(json.load(r)["skills"], [{"id": "hello", "title": "👋 Привет", "hint": ""}])
        self.assertEqual(self.post("/api/chat", {"action": "skill:nope"})[0], 400)

    def test_trace_message_gets_task_but_chat_keeps_it_raw(self):
        trace = TraceTest.TRACES[0]
        http_api.AGENT._llm = scripted({"role": "assistant", "content": "деление на ноль"})
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/chat",
                                     data=json.dumps({"message": trace}).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            r.read()
        self.assertTrue(http_api.AGENT.history[0]["content"].startswith(agent_mod.TRACE_TASK))
        self.assertEqual([m["content"] for m in http_api.AGENT.transcript], [trace, "деление на ноль"])

    def test_twitch_endpoints(self):
        self.assertEqual(self.post("/api/chat", {"action": "twitch"})[0], 400)
        http_api.TWITCH = chat = twitch.TwitchChat("aiko")
        self.addCleanup(setattr, http_api, "TWITCH", None)
        chat._add({"user": "vasya", "text": "Айко, на чём написан проект?"})
        chat._add({"user": "petya", "text": "запусти rm -rf /"})
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/api/twitch?since=1", timeout=5) as r:
            self.assertEqual([m["user"] for m in json.load(r)["messages"]], ["petya"])
        http_api.AGENT._llm = scripted({"role": "assistant", "content": "@vasya, на Python!"})
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/chat", data=b'{"action": "twitch"}',
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            r.read()
        prompt = http_api.AGENT.history[0]["content"]
        self.assertTrue(prompt.startswith(agent_mod.TWITCH_TASK))
        self.assertIn("vasya: Айко, на чём написан проект?", prompt)
        self.assertEqual(self.post("/api/chat", {"action": "twitch"})[0], 400)  # всё уже отвечено

    def test_annotations_endpoint(self):
        http_api.AGENT.set_annotations([{"path": "src/app.py", "line": 1, "message": "хм"}])
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/api/annotations", timeout=5) as r:
            data = json.load(r)
        self.assertEqual((data["root"], data["version"], len(data["notes"])), (str(self.root), 1, 1))
        self.assertEqual(self.post("/api/annotations", {"clear": True}), (200, {"ok": True}))
        self.assertEqual(http_api.AGENT.annotations, {"version": 2, "notes": []})

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


class GitUserBase(Base):
    """Репозиторий с автором коммитов и незакоммиченной правкой в src/app.py."""

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


class CommitTest(GitUserBase):
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
        self.assertEqual(next(e for e in events if e["type"] == "head")["head"], head)
        self.assertIn("#7 поправила ответ", devtools.git_log(self.root, max_count=1))
        self.assertEqual(devtools.changes(self.root)["head"], head)

    def test_denied_commit_changes_nothing(self):
        subprocess.run(["git", "-C", str(self.root), "add", "-A"], check=True)
        before = devtools.head(self.root)
        result, _ = self.run_commit(False, message="fix")
        self.assertIn("отказался", result)
        self.assertEqual(devtools.head(self.root), before)


class UndoCommitTest(GitUserBase):
    def undo(self, approve=True):
        agent = agent_mod.Agent(self.ws, "key", "model")
        agent._llm = scripted(
            {"role": "assistant", "content": None, "tool_calls": [tool_call("1", "undo_commit")]},
            {"role": "assistant", "content": "ок"},
        )
        events = []

        def emit(ev):
            events.append(ev)
            if ev["type"] == "confirm":
                threading.Timer(0.05, agent.resolve_confirm, (ev["id"], approve)).start()

        agent.run("отмени", emit)
        return [m["content"] for m in agent.history if m["role"] == "tool"][0], events

    def test_undo_own_commit(self):
        before = devtools.head(self.root)
        self.run_commit(True, message="oops", add_all=True)
        result, events = self.undo()
        self.assertIn("отменён", result)
        self.assertEqual(next(e for e in events if e["type"] == "confirm")["subject"], "oops")
        self.assertEqual(devtools.head(self.root), before)
        self.assertEqual(devtools.staged_files(self.root), ["src/app.py"])  # правки не потерялись
        self.assertEqual(next(e for e in events if e["type"] == "head")["head"], before)

    def test_refuses_foreign_and_pushed(self):
        result, events = self.undo()
        self.assertIn("делала не я", result)
        self.run_commit(True, message="mine", add_all=True)
        remote = self.tmp / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
        git = ["git", "-C", str(self.root)]
        subprocess.run([*git, "remote", "add", "origin", str(remote)], check=True)
        subprocess.run([*git, "push", "-q", "origin", "HEAD:main"], check=True)
        subprocess.run([*git, "fetch", "-q", "origin"], check=True)
        result, events = self.undo()
        self.assertIn("запушен", result)
        self.assertFalse([e for e in events if e["type"] == "confirm"])


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


class SkillsTest(Base):
    def write(self, folder: Path, name: str, text: str) -> None:
        folder.mkdir(parents=True, exist_ok=True)
        (folder / name).write_text(text, encoding="utf-8")

    def test_skill_files(self):
        self.write(workspace.SKILLS_DIR, "tests.md", "# 🧪 Тесты\n> прогнать\n\nЗапусти тесты.")
        self.write(workspace.SKILLS_DIR, "lint.md", "# Линт\nЗапусти линтер.")
        self.write(workspace.SKILLS_DIR, "Bad Name.md", "# Плохое имя\nx")
        self.write(workspace.SKILLS_DIR, "notitle.md", "без заголовка")
        self.write(workspace.SKILLS_DIR, "empty.md", "# Пусто\n> только подсказка")
        # проектный скилл с тем же именем важнее общего
        self.write(self.root / ".aiko" / "skills", "lint.md", "# Наш линт\nmake lint, пожалуйста.")
        skills = self.ws.skills()
        self.assertEqual(sorted(skills), ["lint", "tests"])
        self.assertEqual(skills["tests"], {"title": "🧪 Тесты", "hint": "прогнать", "prompt": "Запусти тесты."})
        self.assertEqual((skills["lint"]["title"], skills["lint"]["prompt"]), ("Наш линт", "make lint, пожалуйста."))

    def test_bundled_skills_parse(self):
        workspace.SKILLS_DIR = workspace.HERE / "skills"
        skills = self.ws.skills()
        self.assertIn("tests", skills)
        self.assertTrue(all(v["title"] and v["prompt"] for v in skills.values()))


class StaleNotesTest(Base):
    def test_notes_go_stale_after_many_commits(self):
        self.assertEqual(self.ws.notes_staleness(), 0)  # заметок нет
        self.ws.write_notes("# заметки")
        os.utime(self.ws.notes_path, (1_000_000_000, 1_000_000_000))  # «изучала» в 2001 году
        git = ["git", "-C", str(self.root), "-c", "user.name=t", "-c", "user.email=t@t"]
        for i in range(workspace.STALE_AFTER_COMMITS - 2):
            subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", f"c{i}"], check=True)
        self.assertEqual(self.ws.notes_staleness(), 0)  # 24 коммита с init — ещё терпимо
        subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "last"], check=True)
        self.assertEqual(self.ws.notes_staleness(), workspace.STALE_AFTER_COMMITS)
        agent = agent_mod.Agent(self.ws, "key", "model")
        self.assertIn("заметки устарели", agent.system_prompt())
        self.ws.write_notes("# свежие")
        later = time.time() + 60  # git сравнивает до секунды, а коммиты теста — в ту же секунду
        os.utime(self.ws.notes_path, (later, later))
        self.assertEqual(self.ws.notes_staleness(), 0)


class ProjectMemoryAndEditorTest(Base):
    def test_project_memory_survives_relearn(self):
        agent = agent_mod.Agent(self.ws, "key", "model")
        self.ws.call("remember_project", {"fact": "тесты  запускаются через make test"})
        self.ws.write_notes("# новые заметки")
        prompt = agent.system_prompt()
        self.assertIn("тесты запускаются через make test", prompt)
        self.assertIn("# новые заметки", prompt)
        self.assertFalse(workspace.SENPAI_PATH.exists())  # общая память о семпае не тронута

    def test_annotations_are_validated(self):
        agent = agent_mod.Agent(self.ws, "key", "model")
        agent._llm = scripted(
            {"role": "assistant", "content": None, "tool_calls": [tool_call("1", "annotate", notes=[
                {"path": "src/app.py", "line": 2, "severity": "error", "message": "магическое   число"},
                {"path": "src/app.py", "line": "x", "message": "кривая строка"},
                {"path": "../../etc/passwd", "line": 1, "message": "побег"},
                {"path": ".env", "line": 1, "message": "секрет"},
                {"path": "nope.py", "line": 1, "message": "нет файла"},
                {"path": "README.md", "line": -5, "severity": "panic", "message": "заголовок"},
                "мусор",
            ])]},
            {"role": "assistant", "content": "готово"},
        )
        events = []
        agent.run("ревью", events.append)
        self.assertEqual(agent.annotations, {"version": 1, "notes": [
            {"path": "src/app.py", "line": 2, "severity": "error", "message": "магическое число"},
            {"path": "README.md", "line": 1, "severity": "warning", "message": "заголовок"},
        ]})
        self.assertEqual(len(next(e for e in events if e["type"] == "annotations")["notes"]), 2)

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


class UsageTest(Base):
    def test_stream_parsing_and_cost(self):
        chunks = [
            {"choices": [{"delta": {"content": "[happy] При"}}]},
            {"choices": [{"delta": {"content": "вет"}}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}],
             "usage": {"total_tokens": 120, "cost": 0.0021}},
        ]
        lines = [b": OPENROUTER PROCESSING\n"] + [f"data: {json.dumps(c)}\n".encode() for c in chunks] + [b"data: [DONE]\n"]

        class Resp(list):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        sent = []
        original = agent_mod.openrouter_request
        agent_mod.openrouter_request = lambda body, key: sent.append(json.loads(body)) or Resp(lines)
        try:
            events = []
            agent = agent_mod.Agent(self.ws, "key", "model")
            agent.run("привет", events.append)
            agent.run("ещё", lambda ev: None)
        finally:
            agent_mod.openrouter_request = original
        self.assertEqual(sent[0]["usage"], {"include": True})
        self.assertEqual("".join(e["text"] for e in events if e["type"] == "delta"), "[happy] Привет")
        self.assertEqual([e for e in events if e["type"] == "usage"], [{"type": "usage", "cost": 0.0021, "today": 0.0021}])
        self.assertEqual(agent.history[-1]["content"], "[happy] Привет")
        self.assertEqual(usage.today(), {"cost": 0.0042, "tokens": 240, "requests": 2})

    def test_bad_usage_is_ignored(self):
        usage.add({"cost": "много"})
        self.assertEqual(usage.today()["requests"], 0)


class DayReportTest(GitUserBase):
    def test_goodbye_detection(self):
        agent = agent_mod.Agent(self.ws, "key", "model")
        for bye in ["Пока!", "пока, Айко", "ну всё, пока-пока", "Спасибо, до завтра!", "спокойной ночи ❤",
                    "ладно, я спать", "bye"]:
            self.assertIn("семпай прощается", agent.prepare(bye), bye)
        for text in ["пока не понимаю, как работает quest.py", "пока что тесты падают", "Пока ты тут — глянь diff",
                     "до завтра нужно починить CI, поможешь?" + " очень" * 30]:
            self.assertEqual(agent.prepare(text), text, text)

    def test_report_facts(self):
        git = ["git", "-C", str(self.root)]
        subprocess.run([*git, "commit", "-qam", "починила main"], check=True)
        self.ws.quest.complete("tests")
        usage.add({"cost": 0.12, "total_tokens": 10})
        report = agent_mod.Agent(self.ws, "key", "model").prepare("пока", {"reminders": 3})
        self.assertIn("починила main", report)
        self.assertIn("Коммитов сегодня: 2", report)  # init из make_project — тоже сегодня
        self.assertIn("Прогнать тесты", report)
        self.assertIn("$0.12", report)
        self.assertIn("напоминаний (помидорки, перерывы): 3", report)
        self.assertNotIn("напоминаний", agent_mod.Agent(self.ws, "key", "model").prepare("пока", {"reminders": "x"}))


class TraceTest(unittest.TestCase):
    TRACES = [
        'Traceback (most recent call last):\n  File "src/app.py", line 2, in main\nZeroDivisionError',
        "TypeError: x is undefined\n    at render (/home/s/web/app.js:42:7)\n    at main (app.js:1:1)",
        "Exception in thread main java.lang.NullPointerException\n\tat com.acme.App.run(App.java:12)",
        "panic: runtime error: index out of range\n\ngoroutine 1 [running]:\nmain.main()",
        "thread 'main' panicked at src/main.rs:4:5:\nattempt to divide by zero",
    ]

    def test_detects_traces(self):
        for trace in self.TRACES:
            wrapped = agent_mod.with_trace_task(trace)
            self.assertTrue(wrapped.startswith(agent_mod.TRACE_TASK), trace)
            self.assertIn(trace, wrapped)

    def test_plain_messages_untouched(self):
        for text in ["посмотри at app.py:12, пожалуйста", "что такое panic в Go?", "Traceback — это что?"]:
            self.assertEqual(agent_mod.with_trace_task(text), text)


class TwitchTest(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(twitch.parse(":vasya!vasya@vasya.tmi.twitch.tv PRIVMSG #aiko :Айко,   привет!"),
                         {"user": "vasya", "text": "Айко, привет!"})
        tagged = "@badge-info=;display-name=Vasya_Pro;color=#FF0000 :vasya_pro!vasya_pro@x PRIVMSG #aiko :hi"
        self.assertEqual(twitch.parse(tagged)["user"], "Vasya_Pro")
        self.assertEqual(twitch.parse(":a!a@a PRIVMSG #c :\x01ACTION машет\x01")["text"], "машет")
        self.assertIsNone(twitch.parse(":tmi.twitch.tv 001 justinfan1 :Welcome"))
        self.assertIsNone(twitch.parse("PING :tmi.twitch.tv"))
        with self.assertRaises(ValueError):
            twitch.TwitchChat("bad channel; JOIN #x")

    def test_session_with_fake_irc(self):
        srv = socket.create_server(("127.0.0.1", 0))
        got = []

        def irc():
            for round_ in range(2):  # второй раз — после обрыва: клиент должен переподключиться
                conn, _ = srv.accept()
                with conn:
                    conn.settimeout(5)
                    buf = b""
                    while b"JOIN #aiko" not in buf:
                        buf += conn.recv(1024)
                    got.append(buf.decode())
                    conn.sendall(b"PING :tmi.twitch.tv\r\n"
                                 + f":v!v@v PRIVMSG #aiko :вопрос {round_}\r\n".encode()
                                 + b":tmi.twitch.tv 366 justinfan :End\r\n")
                    while b"PONG" not in buf:
                        buf += conn.recv(1024)
                    got.append("pong")
        threading.Thread(target=irc, daemon=True).start()
        chat = twitch.TwitchChat("Aiko", "127.0.0.1", srv.getsockname()[1]).start()
        try:
            for _ in range(100):
                if len(chat.since(0)) == 2:
                    break
                time.sleep(0.05)
        finally:
            chat.stop()
            srv.close()
        self.assertIn("NICK justinfan", got[0])
        self.assertEqual(got.count("pong"), 2)
        self.assertEqual([m["text"] for m in chat.since(0)], ["вопрос 0", "вопрос 1"])
        self.assertEqual([m["text"] for m in chat.take_unanswered()], ["вопрос 0", "вопрос 1"])
        self.assertEqual(chat.take_unanswered(), [])


class SeasonTest(unittest.TestCase):
    def test_seasons(self):
        from datetime import date
        self.assertIn("Хэллоуин", agent_mod.season_hint(date(2026, 10, 31)))
        self.assertIn("новогодние", agent_mod.season_hint(date(2027, 1, 3)))
        self.assertEqual(agent_mod.season_hint(date(2026, 7, 1)), "")


if __name__ == "__main__":
    unittest.main()
