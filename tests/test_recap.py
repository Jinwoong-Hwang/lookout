"""세션 요약 — 슬롯 계산 · 확정할 날 고르기 · 묶음 정리 · 생성 흐름 · 스케줄.

엔진은 부르지 않는다(codex_runner.run 을 가짜로). 세션 수집도 가짜로(sessions.collect)."""
import datetime
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from src import codex_runner, config, db, recap, sessions


def _ts(h, m, day=4):
    return datetime.datetime(2026, 11, day, h, m).timestamp()


def _session(sid="s1", h=15, branch="fix/PH-9", end_h=16, activity=3):
    s = sessions._new_session("claude", sid, "/x")
    s.update(repo="app", branch=branch, title=f"{branch} 작업", start=_ts(h, 0), end=_ts(end_h, 0),
             prompts=[(_ts(h, 0), "고쳐줘")], prs=[f"https://github.com/o/app/pull/{h}"],
             commits=["fix: x"], activity=activity)
    return s


class SlotTest(unittest.TestCase):
    SLOTS = ["09:40", "14:05", "18:10"]

    def due(self, h, m, day=4):
        return recap.due_slot(datetime.datetime(2026, 11, day, h, m), self.SLOTS, 5, True)

    def test_due_from_lead_until_next(self):
        self.assertIsNone(self.due(9, 34))
        self.assertEqual(self.due(9, 35), "09:40")
        self.assertEqual(self.due(12, 0), "09:40")
        self.assertEqual(self.due(14, 0), "14:05")
        self.assertEqual(self.due(18, 5), "18:10")

    def test_too_late_slot_skipped(self):
        # 맥이 자다 깨서 세 시간 넘게 지난 슬롯을 뒤늦게 만들지 않는다
        self.assertIsNone(self.due(13, 0))
        self.assertIsNone(self.due(23, 0))

    def test_weekend(self):
        self.assertIsNone(self.due(9, 40, day=7))   # 2026-11-07 토요일

    def test_bad_slot_values_ignored(self):
        with mock.patch.dict(config.CFG, {"recap_slots": ["9:40", "25:00", "x", "18:10"]}):
            self.assertEqual(recap.slots(), ["09:40", "18:10"])

    def test_prev_workday_skips_weekend(self):
        monday = datetime.date(2026, 11, 9)
        self.assertEqual(recap.prev_workday(monday, True), datetime.date(2026, 11, 6))
        self.assertEqual(recap.prev_workday(monday, False), datetime.date(2026, 11, 8))


class LatestModelTest(unittest.TestCase):
    def test_lowest_priority_listed_model(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"models": [
                {"slug": "hidden-best", "priority": 0, "visibility": "hide"},
                {"slug": "gpt-b", "priority": 2, "visibility": "list"},
                {"slug": "gpt-a", "priority": 1, "visibility": "list"},
            ]}, f)
        self.addCleanup(os.remove, f.name)
        self.assertEqual(recap.latest_codex_model(f.name), "gpt-a")
        self.assertEqual(recap.latest_codex_model(f.name + ".missing"), "")

    def test_empty_config_means_latest_and_high(self):
        # update.sh 는 새 문자열 키를 "" 로 넣는다 — 빈 값이 기본값이어야 한다
        with mock.patch.dict(config.CFG, {"recap_model": "", "recap_effort": ""}), \
                mock.patch.object(recap, "latest_codex_model", return_value="gpt-a"):
            self.assertEqual(recap.engine_opts(), ("gpt-a", "high"))


class CodexArgsTest(unittest.TestCase):
    def test_model_and_effort_passed(self):
        calls = []

        class P:
            returncode, stdout, stderr = 0, "", ""

        def fake(args, **kw):
            calls.append(args)
            with open(args[args.index("-o") + 1], "w") as f:
                f.write("{}")
            return P()

        with mock.patch.object(subprocess, "run", fake):
            codex_runner.run("p", model="gpt-a", effort="high")
            codex_runner.run("p")
        self.assertIn("gpt-a", calls[0])
        self.assertIn('model_reasoning_effort="high"', calls[0])
        self.assertNotIn('model_reasoning_effort="high"', calls[1])
        self.assertEqual(calls[0][-1], "-")    # 프롬프트는 여전히 stdin


class CleanGroupsTest(unittest.TestCase):
    def test_unknown_refs_dropped_and_each_session_in_one_group(self):
        got = recap.clean_groups([
            {"title": "PH-2  두 번째", "refs": ["S3", "S9"], "summary": "a"},
            {"title": "PH-1 첫 번째", "refs": ["S2", "S1"], "summary": "b"},
            {"title": "중복", "refs": ["S1"]},          # S1 은 이미 PH-1 에
            {"title": "", "refs": ["S4"]},              # 이름 없음
            "x",
        ], ["S1", "S2", "S3", "S4"])
        self.assertEqual([g["title"] for g in got], ["PH-1 첫 번째", "PH-2 두 번째"])  # 먼저 시작한 묶음부터
        self.assertEqual(got[0]["refs"], ["S1", "S2"])                    # 시작 순
        self.assertEqual(got[1]["refs"], ["S3"])
        self.assertNotIn("state", got[0])                                 # 완료 · 진행 판정은 안 한다


class GenerateTest(unittest.TestCase):
    DAY = "2026-11-04"

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        p = mock.patch.dict(config.CFG, {"db_path": os.path.join(self.dir, "t.sqlite"),
                                         "recap_slots": ["09:40", "14:05", "18:10"],
                                         "recap_model": "m", "recap_effort": "high",
                                         "recap_weekdays_only": True})
        p.start()
        self.addCleanup(p.stop)
        db.init()
        self.prompts = []
        self.found = [_session("s1", 10, "fix/PH-1", 11), _session("s2", 13, "fix/PH-1", 14),
                      _session("s3", 15, "main", 15)]
        self.found[1]["pr_actions"] = ["PR 머지: 13"]
        self.reply = json.dumps({
            "groups": [{"title": "PH-1 배지", "refs": ["S1", "S2"],
                        "summary": "원인 찾아 고치고 머지"}],
            "sessions": [{"ref": "S1", "summary": "원인 분석"}, {"ref": "S2", "summary": "리뷰 반영 후 머지"},
                         {"ref": "S3", "summary": "사용법 질문"}]}, ensure_ascii=False)

    def _gen(self, now=datetime.datetime(2026, 11, 4, 14, 0), day=DAY, tag="14:05", force=False,
             found=None, reply=None):
        def fake_run(prompt, **kw):
            self.prompts.append((prompt, kw))
            return self.reply if reply is None else reply
        with mock.patch.object(sessions, "collect", return_value=self.found if found is None else found) as col, \
                mock.patch.object(codex_runner, "run", fake_run):
            st = recap.generate(day, tag, now, force)
        self.window = col.call_args[0]
        return st

    def _row(self, day=DAY):
        with db.connect() as c:
            return recap.get(c, day)

    def test_ready_groups_sessions_and_chores(self):
        self.assertEqual(self._gen(), "ready")
        # 그날 00:00 부터 지금까지 — 전에 올린 글 · 전날 요약은 넘기지 않는다
        self.assertEqual(self.window, (datetime.datetime(2026, 11, 4).timestamp(), _ts(14, 0)))
        prompt, kw = self.prompts[0]
        self.assertIn("S1 · Claude · app · fix/PH-1", prompt)
        self.assertIn("아직 하루가 안 끝났다", prompt)
        self.assertEqual((kw["model"], kw["effort"]), ("m", "high"))
        v = recap.view(self._row())
        self.assertFalse(v["final"])
        g = v["groups"][0]
        self.assertEqual((g["title"], [s["ref"] for s in g["sessions"]]), ("PH-1 배지", ["S1", "S2"]))
        self.assertEqual(g["marks"], ["머지"])                             # 기록에 있는 사실만
        self.assertEqual(g["commits"], 2)
        self.assertEqual(len(g["prs"]), 2)
        self.assertEqual([s["summary"] for s in v["chores"]], ["사용법 질문"])   # 묶음 밖 = 잡무
        text = recap.recap_text(v)
        self.assertIn("\nPH-1 배지 — o/app#10, o/app#13\n", text)
        self.assertIn("  - 13:00 리뷰 반영 후 머지", text)
        self.assertIn("잡무\n  - 15:00 사용법 질문", text)

    def test_old_rows_with_state_show_in_time_order(self):
        self._gen(reply=json.dumps({
            "groups": [{"title": "늦게", "state": "완료", "refs": ["S3"]},
                       {"title": "일찍", "state": "진행", "refs": ["S1", "S2"]}],
            "sessions": []}, ensure_ascii=False))
        with db.connect() as c:   # 상태 순으로 저장돼 있던 예전 행을 흉내
            c.execute("UPDATE session_recaps SET groups=?", (json.dumps(
                [{"title": "늦게", "state": "완료", "refs": ["S3"]},
                 {"title": "일찍", "state": "진행", "refs": ["S1", "S2"]}], ensure_ascii=False),))
        self.assertEqual([g["title"] for g in recap.view(self._row())["groups"]], ["일찍", "늦게"])

    def test_unchanged_sessions_skip_engine(self):
        self._gen()
        self.assertEqual(self._gen(now=datetime.datetime(2026, 11, 4, 18, 5), tag="18:10"), "unchanged")
        self.assertEqual(len(self.prompts), 1)
        self.assertEqual(self._row()["window_end"], datetime.datetime(2026, 11, 4, 18, 5).timestamp())
        # 손으로 다시 만들면 세션이 그대로여도 엔진을 부른다
        self.assertEqual(self._gen(tag=recap.MANUAL, force=True), "ready")
        self.assertEqual(len(self.prompts), 2)

    def test_new_activity_regenerates(self):
        self._gen()
        self.found[-1] = _session("s3", 15, "main", 17, activity=9)
        self.assertEqual(self._gen(now=datetime.datetime(2026, 11, 4, 18, 5), tag="18:10"), "ready")
        self.assertEqual(len(self.prompts), 2)

    def test_past_day_is_whole_day_and_final(self):
        self.assertEqual(self._gen(now=datetime.datetime(2026, 11, 5, 9, 35), tag=recap.FINAL), "ready")
        self.assertEqual(self.window[1], datetime.datetime(2026, 11, 5).timestamp())
        self.assertIn("하루 전체", self.prompts[0][0])
        self.assertTrue(recap.is_final(self._row()))

    def test_no_sessions_is_empty_without_engine(self):
        self.assertEqual(self._gen(found=[]), "empty")
        self.assertEqual(self.prompts, [])
        self.assertEqual(self._row()["status"], "empty")

    def test_failure_keeps_previous_summary(self):
        self._gen()
        self.found[-1] = _session("s3", 15, "main", 17, activity=9)
        st = self._gen(now=datetime.datetime(2026, 11, 4, 18, 5), tag="18:10",
                       reply=json.dumps({"groups": [], "sessions": []}))
        self.assertEqual(st, "failed")
        row = self._row()
        self.assertEqual((row["status"], row["slot"]), ("failed", "18:10"))
        self.assertIn("비었다", row["error"])
        self.assertEqual(recap.view(row)["groups"][0]["title"], "PH-1 배지")   # 보던 건 남는다

    def test_should_run(self):
        self.assertTrue(recap._should_run(None, "14:05"))
        now = db.now()
        row = {"day": self.DAY, "status": "ready", "slot": "14:05", "attempts": 1, "updated_at": now,
               "window_end": _ts(14, 0)}
        self.assertFalse(recap._should_run(row, "14:05"))
        self.assertTrue(recap._should_run(row, "18:10"))                        # 다음 슬롯 차례
        self.assertTrue(recap._should_run(row, recap.FINAL))                    # 아직 하루 전체가 아니다
        final = {**row, "window_end": datetime.datetime(2026, 11, 5).timestamp()}
        self.assertFalse(recap._should_run(final, recap.FINAL))
        failed = {**row, "status": "failed"}
        self.assertFalse(recap._should_run(failed, "14:05"))                    # 쿨다운
        self.assertTrue(recap._should_run({**failed, "updated_at": now - 700}, "14:05"))
        self.assertFalse(recap._should_run({**failed, "attempts": 3, "updated_at": 0}, "14:05"))
        self.assertTrue(recap._should_run({**failed, "attempts": 3, "updated_at": 0}, "18:10"))
        running = {**row, "status": "running"}
        self.assertFalse(recap._should_run(running, "18:10"))                   # 아직 도는 중
        self.assertTrue(recap._should_run({**running, "updated_at": 0}, "14:05"))  # 죽었다

    def test_days_to_finalize(self):
        with db.connect() as c:
            # 월요일 아침: 금 · 토 · 일, 그리고 지난주에 확정 못 한 날
            recap._start_row(c, "2026-11-04", "18:10")
            recap._finish_row(c, "2026-11-04", "ready", _ts(18, 5), "fp")
            recap._start_row(c, "2026-11-05", recap.FINAL)
            recap._finish_row(c, "2026-11-05", "ready", datetime.datetime(2026, 11, 6).timestamp(), "fp")
            self.assertEqual(recap.days_to_finalize(c, datetime.date(2026, 11, 9)),
                             ["2026-11-04", "2026-11-06", "2026-11-07", "2026-11-08"])
            self.assertEqual(recap.days_to_finalize(c, datetime.date(2026, 11, 5)), ["2026-11-04"])

    def test_morning_slot_finalizes_yesterday_then_today(self):
        calls = []
        with mock.patch.dict(config.CFG, {"recap_enabled": True}), \
                mock.patch.object(recap, "generate", side_effect=lambda d, t, n: calls.append((d, t)) or "ready"), \
                mock.patch.object(recap, "_notify"):
            recap.run_scheduled(datetime.datetime(2026, 11, 5, 9, 35))
            recap.run_scheduled(datetime.datetime(2026, 11, 5, 14, 0))
        self.assertEqual(calls, [("2026-11-04", recap.FINAL), ("2026-11-05", "09:40"),
                                 ("2026-11-05", "14:05")])

    def test_dead_running_row_closed_as_failed(self):
        with db.connect() as c:
            recap._start_row(c, self.DAY, "18:10")
            c.execute("UPDATE session_recaps SET attempts=3, updated_at=0")
        with mock.patch.dict(config.CFG, {"recap_enabled": True}), \
                mock.patch.object(recap, "_notify"), mock.patch.object(recap, "generate") as gen:
            self.assertEqual(recap.run_scheduled(datetime.datetime(2026, 11, 4, 18, 30)), "done")
        gen.assert_not_called()
        self.assertEqual(self._row()["status"], "failed")

    def test_notify_opens_first_made_day(self):
        self._gen(now=datetime.datetime(2026, 11, 5, 9, 35), tag=recap.FINAL)
        self._gen(now=datetime.datetime(2026, 11, 5, 9, 35), day="2026-11-05", tag="09:40")
        with mock.patch.object(recap.notify, "send") as send:
            recap._notify([("2026-11-04", "ready"), ("2026-11-05", "ready")])
        self.assertEqual(send.call_count, 1)
        # 아침 알림을 누르면 막 확정한 지난 날이 열린다
        self.assertTrue(send.call_args.kwargs["open_url"].endswith("/#recap/2026-11-04"))
        self.assertIn("11-04 (수) 확정", send.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
