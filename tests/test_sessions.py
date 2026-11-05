"""세션 기록 수집 — Claude Code · Codex 세션 파일을 구간으로 잘라 요청 · PR · 커밋을 건진다.

세션 파일은 임시 디렉토리에 만든다."""
import datetime
import json
import os
import shutil
import tempfile
import unittest

from src import config, sessions


def _ts(h, m, day=4):
    return datetime.datetime(2026, 11, day, h, m).timestamp()


def _iso(h, m, day=4):
    return datetime.datetime(2026, 11, day, h, m).astimezone(datetime.timezone.utc) \
        .strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _write_jsonl(path, rows):
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n")


def _user(text, h, m, **kw):
    d = {"type": "user", "entrypoint": "cli", "cwd": "/w/app", "gitBranch": "fix/PH-1",
         "sessionId": "s1", "timestamp": _iso(h, m), "message": {"role": "user", "content": text}}
    d.update(kw)
    return d


def _assistant(parts, h, m, **kw):
    d = {"type": "assistant", "entrypoint": "cli", "cwd": "/w/app", "gitBranch": "fix/PH-1",
         "sessionId": "s1", "timestamp": _iso(h, m), "message": {"role": "assistant", "content": parts}}
    d.update(kw)
    return d


class CleanPromptTest(unittest.TestCase):
    def test_builtin_command_dropped_skill_kept(self):
        self.assertEqual(sessions.clean_prompt("<command-name>/clear</command-name>"), "")
        self.assertEqual(
            sessions.clean_prompt("<command-message>x</command-message>\n<command-name>/dev-webview-deploy</command-name>"
                                  "<command-args>--worktree a</command-args>"),
            "/dev-webview-deploy --worktree a")

    def test_injected_text_dropped(self):
        for t in ("<local-command-stdout>ok</local-command-stdout>", "<task-notification>x",
                  "[Request interrupted by user]", "Caveat: the messages below"):
            self.assertEqual(sessions.clean_prompt(t), "", t)

    def test_reminder_block_before_prompt_is_not_the_whole_message(self):
        # 하네스가 앞 블록에 system-reminder 를 끼우면 합친 문자열 전체가 주입물처럼 보였다
        content = [{"type": "text", "text": "<system-reminder>CLAUDE.md …</system-reminder>"},
                   {"type": "text", "text": "fe_house 채널 형식 학습해줘"}]
        self.assertEqual(sessions.user_prompt(content), "fe_house 채널 형식 학습해줘")


class ParseClaudeTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        self.path = os.path.join(self.dir, "s1.jsonl")

    def parse(self, rows, since=_ts(14, 0), until=_ts(18, 0)):
        _write_jsonl(self.path, rows)
        return sessions.parse_claude(self.path, since, until)

    def test_window_prompts_prior_context_and_signals(self):
        s = self.parse([
            {"type": "ai-title", "aiTitle": "PH-1 배지 고착", "sessionId": "s1"},
            _user("PH-1 배지 문제 고쳐줘", 13, 30),
            _user("PR 올려줘", 15, 0),
            _assistant([{"type": "tool_use", "name": "Bash", "input": {
                "command": "git commit -m \"$(cat <<'EOF'\nfix(PH-1): 배지가 1로 남는다\n\n본문\nEOF\n)\""}}], 15, 1),
            _assistant([{"type": "tool_use", "name": "Bash", "input": {
                "command": 'gh pr create --base main --title "[PH-1] 배지 고착" --body x'}}], 15, 2),
            _assistant([{"type": "tool_use", "name": "Skill", "input": {"skill": "dev-webview-deploy"}}], 15, 3),
            {"type": "pr-link", "sessionId": "s1", "prUrl": "https://github.com/o/app/pull/7",
             "timestamp": _iso(15, 4)},
            _assistant([{"type": "text", "text": "PR 을 올렸습니다."}], 15, 5),
            _user("밖에서 한 말", 19, 0),
        ])
        self.assertEqual(s["title"], "PH-1 배지 고착")
        self.assertEqual([p for _, p in s["prompts"]], ["PR 올려줘"])
        self.assertEqual([p for _, p in s["prior"]], ["PH-1 배지 문제 고쳐줘"])
        self.assertEqual(s["commits"], ["fix(PH-1): 배지가 1로 남는다"])
        self.assertEqual(s["pr_actions"], ["PR 생성: [PH-1] 배지 고착"])
        self.assertEqual(s["skills"], ["dev-webview-deploy"])
        self.assertEqual(s["prs"], ["https://github.com/o/app/pull/7"])
        self.assertEqual(s["last_reply"], "PR 을 올렸습니다.")
        self.assertEqual(s["branch"], "fix/PH-1")

    def test_headless_subsession_excluded(self):
        # red-team 리뷰어처럼 대화형 세션이 띄운 headless 세션 — 일은 부모 세션에 이미 잡힌다
        self.assertIsNone(self.parse([_user("리뷰해", 15, 0, entrypoint="sdk-cli")]))
        # 데스크톱 앱 · IDE 처럼 cli 가 아닌 대화형 진입점은 센다
        self.assertIsNotNone(self.parse([_user("고쳐줘", 15, 0, entrypoint="claude-vscode")]))

    def test_lookout_bot_worktree_excluded_but_checkout_is_not(self):
        bot = os.path.join(config.HERMES_HOME, "worktrees", "o__app--pr1")
        self.assertIsNone(self.parse([_user("리뷰", 15, 0, cwd=bot)]))
        # 같은 체크아웃에서 사람이 직접 한 개발 세션은 센다(실측: 이걸 빼서 세션이 사라졌다)
        self.assertIsNotNone(self.parse([_user("기능 추가", 15, 0, cwd=config.HERMES_HOME)]))

    def test_sidechain_meta_and_compact_summary_ignored(self):
        s = self.parse([
            _user("진짜 요청", 15, 0),
            _user("서브에이전트 지시", 15, 1, isSidechain=True),
            _user("meta", 15, 2, isMeta=True),
            _user("This session is being continued…", 15, 3, isCompactSummary=True),
        ])
        self.assertEqual([p for _, p in s["prompts"]], ["진짜 요청"])

    def test_agent_tail_without_prompt_counts_only_if_substantial(self):
        tail = [_user("큰 작업 맡김", 13, 50)]
        self.assertIsNone(self.parse(tail + [_assistant([{"type": "text", "text": "끝"}], 14, 1)]))
        busy = tail + [_assistant([{"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}], 14, i)
                       for i in range(1, 1 + sessions.MIN_TAIL_ACTIVITY)]
        s = self.parse(busy)
        self.assertIsNotNone(s)
        self.assertEqual(s["prompts"], [])
        self.assertIn("구간 안 요청 없음", sessions.digest(s, "S1"))


class ParseCodexTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        self.path = os.path.join(self.dir, "rollout-x.jsonl")

    def _rows(self, originator="codex-tui"):
        return [
            {"timestamp": _iso(14, 30), "type": "session_meta", "payload": {
                "id": "c1", "cwd": "/w/app", "originator": originator,
                "git": {"branch": "feat/x", "repository_url": "https://github.com/o/app.git"}}},
            {"timestamp": _iso(14, 31), "type": "event_msg", "payload": {"type": "user_message", "message": "목록 정렬 고쳐"}},
            {"timestamp": _iso(14, 40), "type": "response_item", "payload": {
                "type": "function_call", "arguments": json.dumps({"command": ["bash", "-lc", "git commit -m 'fix: 정렬'"]})}},
            {"timestamp": _iso(14, 41), "type": "event_msg", "payload": {"type": "agent_message", "message": "고쳤습니다"}},
        ]

    def test_parsed(self):
        _write_jsonl(self.path, self._rows())
        s = sessions.parse_codex(self.path, _ts(14, 0), _ts(18, 0))
        self.assertEqual((s["repo"], s["branch"]), ("app", "feat/x"))
        self.assertEqual([p for _, p in s["prompts"]], ["목록 정렬 고쳐"])
        self.assertEqual(s["commits"], ["fix: 정렬"])
        self.assertEqual(s["last_reply"], "고쳤습니다")

    def test_exec_runs_excluded(self):
        _write_jsonl(self.path, self._rows("codex_exec"))
        self.assertIsNone(sessions.parse_codex(self.path, _ts(14, 0), _ts(18, 0)))


if __name__ == "__main__":
    unittest.main()
