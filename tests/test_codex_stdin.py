import subprocess
import unittest

from src import codex_runner

BIG = "가" * 4500     # 실측: argv 로 2KB 넘기면 codex 가 rc=-9 로 즉사한다


class CodexStdinTest(unittest.TestCase):
    """프롬프트는 argv 가 아니라 stdin 으로 가야 한다.

    argv 로 넘기면 2KB 정도부터 codex 프로세스가 신호로 즉사한다(rc=-9, stderr 없음).
    플래그·훅·codex 버전과 무관하게 재현됐고, 리뷰 프롬프트는 진작 그 크기를 넘는다.
    """

    def setUp(self):
        self.saved = subprocess.run
        self.calls = []

        class P:
            returncode = 0
            stdout = '{"ok": true}'
            stderr = ""

        def fake(args, **kw):
            self.calls.append((args, kw))
            # -o 로 지정된 파일에 최종 메시지를 써주는 codex 동작을 흉내낸다
            if "-o" in args:
                with open(args[args.index("-o") + 1], "w", encoding="utf-8") as f:
                    f.write('{"ok": true}')
            return P()

        subprocess.run = fake

    def tearDown(self):
        subprocess.run = self.saved

    def _args_kw(self):
        args, kw = self.calls[-1]
        return args, kw

    def test_run_sends_prompt_on_stdin(self):
        codex_runner.run(BIG, cwd="/tmp")
        args, kw = self._args_kw()
        self.assertEqual(kw.get("input"), BIG)
        self.assertNotIn(BIG, args, "프롬프트가 argv 에 실렸다 — 큰 입력에서 즉사한다")
        self.assertEqual(args[-1], codex_runner.STDIN_MARKER)

    def test_run_impl_sends_prompt_on_stdin(self):
        codex_runner.run_impl(BIG, cwd="/tmp")
        args, kw = self._args_kw()
        self.assertEqual(kw.get("input"), BIG)
        self.assertNotIn(BIG, args)
        self.assertEqual(args[-1], codex_runner.STDIN_MARKER)

    def test_no_argv_entry_is_prompt_sized(self):
        codex_runner.run(BIG, cwd="/tmp")
        args, _ = self._args_kw()
        biggest = max(len(a.encode()) for a in args)
        self.assertLess(biggest, 2000, f"argv 항목이 {biggest} 바이트 — 임계 근처다")

    def test_signal_exit_gets_an_actionable_hint(self):
        class Killed:
            returncode = -9
            stdout = ""
            stderr = ""

        subprocess.run = lambda *a, **k: Killed()
        with self.assertRaises(codex_runner.CodexError) as cm:
            codex_runner.run(BIG, cwd="/tmp")
        msg = str(cm.exception)
        self.assertIn("rc=-9", msg)
        self.assertIn("신호 9", msg)      # 빈 stderr 로 끝나면 원인을 알 수 없다
        self.assertIn("stdin", msg)


if __name__ == "__main__":
    unittest.main()
