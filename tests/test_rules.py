"""Unit tests for the shared quest rules (quest_core/rules.py).

They use a throwaway database file, so they never touch real data.
Standard library only. Run from the project folder:
    python -m unittest discover -s tests -v
"""
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

# Point the store at a temporary database BEFORE importing it.
_tmp = tempfile.TemporaryDirectory()
os.environ["QUEST_DB_PATH"] = os.path.join(_tmp.name, "test_quests.db")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from quest_core import rules  # noqa: E402


class CreateQuestTests(unittest.TestCase):
    def test_new_quest_is_in_progress_and_stored(self):
        quest, created = rules.create_quest()
        self.assertTrue(created)
        self.assertEqual(quest["status"], "in_progress")
        self.assertEqual(rules.get_quest(quest["instance_id"])["instance_id"], quest["instance_id"])

    def test_same_idempotency_key_returns_same_quest(self):
        first, created_first = rules.create_quest("key-same")
        second, created_second = rules.create_quest("key-same")
        self.assertTrue(created_first)
        self.assertFalse(created_second)
        self.assertEqual(first["instance_id"], second["instance_id"])

    def test_simultaneous_requests_with_one_key_create_one_quest(self):
        ids = []
        def create():
            quest, _ = rules.create_quest("key-race")
            ids.append(quest["instance_id"])
        threads = [threading.Thread(target=create) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(set(ids)), 1)


class ResolveQuestTests(unittest.TestCase):
    def setUp(self):
        self.quest, _ = rules.create_quest()
        self.qid = self.quest["instance_id"]

    def test_resolve_in_progress_quest(self):
        quest, changed = rules.resolve_quest(self.qid, "completed")
        self.assertTrue(changed)
        self.assertEqual(quest["status"], "completed")
        self.assertEqual(rules.get_quest(self.qid)["status"], "completed")

    def test_repeating_same_status_is_safe(self):
        rules.resolve_quest(self.qid, "skipped")
        quest, changed = rules.resolve_quest(self.qid, "skipped")
        self.assertFalse(changed)
        self.assertEqual(quest["status"], "skipped")

    def test_different_status_on_resolved_quest_conflicts(self):
        rules.resolve_quest(self.qid, "completed")
        with self.assertRaises(rules.QuestAlreadyResolved) as ctx:
            rules.resolve_quest(self.qid, "abandoned")
        self.assertEqual(ctx.exception.current_status, "completed")

    def test_unknown_quest(self):
        with self.assertRaises(rules.QuestNotFound):
            rules.resolve_quest("00000000-0000-0000-0000-000000000000", "completed")

    def test_invalid_id(self):
        with self.assertRaises(rules.InvalidInput):
            rules.resolve_quest("abc", "completed")

    def test_invalid_status(self):
        for bad in ("banana", "in_progress", ""):
            with self.assertRaises(rules.InvalidInput):
                rules.resolve_quest(self.qid, bad)

    def test_simultaneous_conflicting_resolves_have_one_winner(self):
        results = []
        def resolve(status):
            try:
                results.append(("ok", status, rules.resolve_quest(self.qid, status)[1]))
            except rules.QuestAlreadyResolved:
                results.append(("conflict", status, None))
        threads = [threading.Thread(target=resolve, args=(s,)) for s in ["completed", "skipped"] * 4]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        final = rules.get_quest(self.qid)["status"]
        changed = [r for r in results if r[0] == "ok" and r[2]]
        self.assertEqual(len(changed), 1)  # exactly one request actually changed it
        for kind, status, _ in results:
            # Everyone asking for the winning status succeeds; everyone else conflicts.
            self.assertEqual(kind == "ok", status == final)


if __name__ == "__main__":
    unittest.main()
