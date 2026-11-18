"""Run with python3 regression_store.py. Tests use a temporary thesis and database."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import store
from store import Store, StoreError, digest


class FixtureImages:
    def __init__(self, root):
        self.root = root

    def snapshot_assets(self, text, ch):
        return {"assets/plot.svg": digest((self.root / "assets/plot.svg").read_bytes())}


class StoreRegression(unittest.TestCase):
    first = "chapters/one.qmd"
    second = "chapters/two.qmd"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.root = self.dir / "thesis"
        (self.root / "chapters").mkdir(parents=True)
        (self.root / "assets").mkdir()
        self.asset = self.root / "assets/plot.svg"
        self.asset.write_text("<svg>甲</svg>")
        self.raw = "# 标题\r\n\r\n原句。\r\n\r\n<!-- 隐藏写作提示 -->\r\n".encode("utf-8")
        for ch in (self.first, self.second):
            (self.root / ch).write_bytes(self.raw)
        self.data = self.dir / "review"
        self.renderer = FixtureImages(self.root)
        self.ledger = Store(self.root, self.data, self.renderer)

    def payload(self, ch, id, **extra):
        ver = "live:" + digest((self.root / ch).read_bytes())
        return {"id": id, "ch": ch, "source_version": ver, "target_version": ver, "mode": "comment",
                "quote": "原句。", "text": "请收紧措辞。",
                "anchor": {"type": "text", "segs": [{"block": "b1", "off": 0, "text": "原句。",
                    "before": "", "after": "", "line_start": 3, "line_end": 3}]}, **extra}

    def refs(self, *notes):
        return [{"id": n["id"], "revision": n["revision"]} for n in notes]

    def error(self, status, method, *args):
        with self.assertRaises(StoreError) as ctx:
            method(*args)
        self.assertEqual(ctx.exception.status, status)

    def draft_file(self, raw):
        p = self.dir / "candidate.qmd"
        p.write_bytes(raw)
        return p

    def test_first_save_freezes_bytes_and_images_drift_requires_review(self):
        input_note = self.payload(self.first, "n")
        draft = self.ledger.save({**input_note, "status": "draft"})
        self.assertFalse(self.ledger.state()["rounds"])
        path = self.root / self.first
        path.write_bytes(self.raw.replace("原句".encode(), "变化".encode()))
        note = self.ledger.save({**input_note, "expected_revision": draft["revision"]})
        base = note["source_version"]
        self.assertTrue(note["recheck"])
        self.assertEqual(note["selected_version"], input_note["source_version"])
        self.assertEqual(self.ledger.read_version(self.first, base), path.read_bytes())
        self.error(409, self.ledger.submit, self.refs(note), "retry")
        checked = self.ledger.save({**note, "recheck": False, "quote": "变化。"})
        self.assertEqual(self.ledger.submit(self.refs(checked), "retry")["number"], 1)
        hashes = self.ledger.version_assets(self.first, base)
        self.asset.write_text("<svg>乙</svg>")
        self.assertNotEqual(hashes["assets/plot.svg"], digest(self.asset.read_bytes()))
        self.assertEqual(hashes, self.ledger.version_assets(self.first, base))

    def test_concurrent_idempotency_and_older_unresolved_in_handoff(self):
        notes = self.ledger.save_many([self.payload(self.first, "a"), self.payload(self.second, "b")])
        refs = self.refs(*notes)
        self.error(409, self.ledger.submit, [refs[0], {**refs[1], "revision": 1000}], "batch")
        self.assertFalse(self.ledger.state()["batches"])
        with ThreadPoolExecutor(max_workers=4) as ex:
            responses = list(ex.map(lambda _: self.ledger.submit(refs, "batch"), range(4)))
        self.assertEqual({r["number"] for r in responses}, {1})
        self.error(409, self.ledger.submit, refs[:1], "batch")
        self.error(409, self.ledger.submit, refs, "other")
        snap = (self.data / "submissions/0001.json").read_bytes()
        next_note = self.ledger.save(self.payload(self.first, "c"))
        self.ledger.submit(self.refs(next_note), "follow")
        self.assertEqual({a["id"] for a in json.loads((self.data/"latest.json").read_text())["annotations"]},
                         {"a", "b", "c"})
        f = self.draft_file(self.raw + b"\nNew.\n")
        v = self.ledger.publish(self.first, f, notes[0]["target_version"], ["a"], {"c": "需要页码。"})
        self.assertEqual(v["number"], 1)
        handoff = json.loads((self.data / "latest.json").read_text())
        self.assertEqual(handoff["parents"][self.first]["id"], v["id"])
        self.assertEqual({a["id"]: a["status"] for a in handoff["annotations"]}, {"b":"sent", "c":"question"})
        self.assertIn("第 3–3 行", (self.data / "latest.md").read_text())
        self.assertEqual(snap, (self.data / "submissions/0001.json").read_bytes())

    def test_cross_chapter_save_failure_is_atomic(self):
        good = self.payload(self.first, "x", group_id="spans")
        bad = self.payload(self.second, "y", group_id="spans", source_version="")
        self.error(400, self.ledger.save_many, [good, bad])
        self.assertFalse(self.ledger.state()["annotations"])
        self.assertFalse(self.ledger.state()["rounds"])
        self.assertFalse(list(self.data.glob("rounds/*/*.qmd")))
        with patch.object(store, "atomic_write", side_effect=OSError("磁盘已满")):
            with self.assertRaises(OSError):
                self.ledger.save(good)
        self.assertFalse(self.ledger.state()["rounds"])
        self.assertFalse(self.ledger.state()["annotations"])

    def test_edit_conflict_and_shadow_finalize_are_atomic(self):
        orig = self.ledger.save(self.payload(self.first, "p"))
        shadow_input = {**orig, "id": "editor-tab-1", "status": "draft", "revision": 0,
                        "editing_id": "p", "editing_revision": orig["revision"], "group_index": 0, "group_count": 1}
        shadow = self.ledger.save(shadow_input)
        self.assertEqual(shadow.get("editing_id"), "p")
        changed = {**orig, "text": "改写定义。", "saved_shadow_id": shadow["id"], "saved_shadow_revision": 40}
        self.error(409, self.ledger.save, changed)
        self.assertEqual(len(self.ledger.state()["annotations"]), 2)
        self.ledger.save({**changed, "saved_shadow_revision": shadow["revision"]})
        notes = self.ledger.state()["annotations"]
        self.assertEqual([(n["id"], n["text"]) for n in notes], [("p", "改写定义。")])
        self.error(409, self.ledger.save, {**orig, "text":"其他页晚到"})
        self.error(409, self.ledger.save, shadow)
        n = notes[0]
        tombstone = self.ledger.delete(n["id"], n["revision"])
        self.assertFalse(self.ledger.state()["annotations"])
        self.error(409, self.ledger.save, n)
        self.ledger.save({**n, "expected_revision": tombstone["revision"]})
        self.assertTrue(self.ledger.state()["annotations"])

    def test_multi_version_history_reopen_and_chapter_isolation(self):
        notes = self.ledger.save_many([self.payload(self.first, "a"), self.payload(self.second, "b")])
        base = notes[0]["source_version"]
        self.ledger.submit(self.refs(*notes), "batch")
        raw = self.raw + b"\r\nV1\r\n"
        v1 = self.ledger.publish(self.first, self.draft_file(raw), base, ["a"])["id"]
        self.error(409, self.ledger.save, self.payload(self.first, "late"))
        self.error(409, self.ledger.publish, self.first, self.draft_file(raw), base)
        cite = self.ledger.save(self.payload(self.first, "new", source_version=base, target_version=v1))
        self.assertEqual(cite["source_version"], base)
        self.assertEqual(cite["target_version"], v1)
        self.ledger.submit(self.refs(cite), "next")
        v2 = self.ledger.publish(self.first, self.draft_file(raw + b"V2\r\n"), v1, ["new"])["id"]
        self.assertEqual(self.ledger.read_version(self.first, base), self.raw)
        self.assertEqual(self.ledger.read_version(self.first, v1), raw)
        self.assertEqual(self.ledger.read_version(self.first, v2), raw + b"V2\r\n")
        self.assertEqual((self.root / self.first).read_bytes(), self.raw)
        with self.assertRaises(RuntimeError):
            with self.ledger.merge_guard(self.first, v2, v1):
                raise RuntimeError("模拟构建未通过")
        self.assertEqual(self.ledger.round_info(self.first)["latest"], v2)
        self.error(409, self.ledger.close_round, self.first, v1)
        before = json.loads((self.data/"submissions/0001.json").read_text())
        self.ledger.close_round(self.first, v2, v1)
        self.assertFalse(self.ledger.round_info(self.first))
        self.assertTrue(self.ledger.round_info(self.second))
        closed = self.ledger.round_info(self.first, base.split(":")[0])
        self.assertEqual(closed["merged_version"], v1)
        ledger = Store(self.root, self.data, self.renderer)
        self.assertEqual(ledger.state(), self.ledger.state())
        self.assertEqual(before, json.loads((self.data/"submissions/0001.json").read_text()))
        # A fresh round may quote a historical version while targeting today's live source.
        new = ledger.save(self.payload(self.first, "history", source_version=v1))
        self.assertEqual(new["source_version"], v1)
        self.assertNotEqual(new["round"], closed["id"])

    def test_export_failure_then_retry_does_not_resubmit(self):
        n = self.ledger.save(self.payload(self.first, "n"))
        selection = self.refs(n)
        with patch.object(self.ledger, "_export_pair", side_effect=OSError("磁盘剩余容量不足")):
            with self.assertRaises(OSError):
                self.ledger.submit(selection, "job")
        self.assertEqual(len(self.ledger.state()["batches"]), 1)
        self.ledger.submit(selection, "job")
        self.assertTrue((self.data / "latest.md").is_file())
        self.assertEqual(len(self.ledger.state()["batches"]), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
