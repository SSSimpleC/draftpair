"""Local document-review ledger.

Store(root, data_dir, renderer=None) opens or creates the ledger. Each public call
opens its own short-lived SQLite connection and can be used from a threaded server.
The database is authoritative. Handoffs and version files are recoverable projections.
Export a separate candidate before editing it.
The renderer optionally implements snapshot_assets(text, chapter_id) -> asset hash map.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import sqlite3
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone


def stamp():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def pretty(value):
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def handoff_command(arguments, windows):
    if windows:
        # PowerShell treats these four typographic marks as single quotes too.
        escaped = str.maketrans({mark: mark * 2 for mark in "'‘’‚‛"})
        return '& ' + ' '.join("'" + arg.translate(escaped) + "'" for arg in arguments)
    return shlex.join(arguments)


def atomic_write(path, data, *, preserve_mode=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    file = tempfile.NamedTemporaryFile(dir=path.parent, prefix=".writing-", delete=False)
    temp = Path(file.name)
    try:
        with file:
            if preserve_mode and os.name != 'nt' and path.is_file():
                os.fchmod(file.fileno(), path.stat().st_mode & 0o777)
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        # Ordinary Windows readers can briefly hold a handle without delete-sharing.
        deadline = time.monotonic() + 1
        while True:
            try:
                os.replace(temp, path)
                break
            except PermissionError as error:
                if getattr(error, 'winerror', None) not in {5, 32, 33} or time.monotonic() >= deadline:
                    raise
                time.sleep(.025)
    finally:
        temp.unlink(missing_ok=True)


class StoreError(ValueError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


class ReviewConnection(sqlite3.Connection):
    version_copies: list


def checked(condition, message, status=400):
    if not condition:
        raise StoreError(status, message)


class Store:
    def __init__(self, root, data_dir, renderer=None, is_chapter=None):
        self.root = Path(root).resolve(strict=True)
        self.data_dir = Path(data_dir).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db = self.data_dir / "review.sqlite3"
        self.renderer = renderer
        self.is_chapter = is_chapter
        with self._connection() as con:
            con.execute("PRAGMA journal_mode=WAL")
            con.executescript("""
                CREATE TABLE IF NOT EXISTS rounds (
                    id TEXT PRIMARY KEY, ch TEXT NOT NULL, started TEXT NOT NULL,
                    closed TEXT, merged_version TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_open ON rounds(ch) WHERE closed IS NULL;
                CREATE TABLE IF NOT EXISTS versions (
                    round_id TEXT NOT NULL REFERENCES rounds(id), n INTEGER NOT NULL,
                    created TEXT NOT NULL, hash TEXT NOT NULL, qmd BLOB NOT NULL, assets TEXT NOT NULL,
                    PRIMARY KEY(round_id,n)
                );
                CREATE TABLE IF NOT EXISTS notes (id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS batches (
                    n INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT UNIQUE NOT NULL,
                    fingerprint TEXT NOT NULL, data TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS merging (
                    ch TEXT PRIMARY KEY, round_id TEXT UNIQUE NOT NULL REFERENCES rounds(id),
                    owner TEXT UNIQUE NOT NULL
                );
            """)
        with self._tx(write=True) as con:
            self._projections(con)

    @contextlib.contextmanager
    def _connection(self):
        con = sqlite3.connect(self.db, isolation_level=None, timeout=15, factory=ReviewConnection)
        try:
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA foreign_keys=ON")
            con.execute("PRAGMA synchronous=FULL")
            con.version_copies = []
            yield con
        finally:
            con.close()

    @contextlib.contextmanager
    def _tx(self, write=False):
        with self._connection() as con:
            con.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            created = []
            try:
                yield con
                for path, raw in con.version_copies:
                    atomic_write(path, raw)
                    created.append(path)
                con.commit()
            except BaseException:
                con.rollback()
                for path in created:
                    path.unlink(missing_ok=True)
                raise

    def _chapter(self, ch):
        checked(isinstance(ch, str) and ch and "\x00" not in ch, "章节路径无效。")
        rel = Path(ch)
        safe = rel.parts and not rel.is_absolute() and ".." not in rel.parts
        checked(safe and ch == rel.as_posix(), "文稿需要项目内的相对路径。")
        if self.is_chapter is not None:
            checked(self.is_chapter(ch), "文稿未在项目目录中登记。")
        path = (self.root / rel).resolve()
        checked(path.is_relative_to(self.root), "文稿路径越出项目目录。")
        return path

    @staticmethod
    def _version_key(version):
        checked(isinstance(version, str), "缺少版本。")
        match = re.fullmatch(r"([0-9a-f]{32}):(0|[1-9][0-9]{0,8})", version)
        checked(match, "历史版本编号无效。")
        return match[1], int(match[2])

    @staticmethod
    def _live_version(version):
        return isinstance(version, str) and bool(re.fullmatch(r"live:[a-f0-9]{64}", version))

    def _version_row(self, con, ch, version):
        rid, n = self._version_key(version)
        row = con.execute("""SELECT v.* FROM versions v JOIN rounds r ON v.round_id=r.id
                             WHERE r.ch=? AND r.id=? AND v.n=?""", (ch, rid, n)).fetchone()
        checked(row, "该章没有这个稿本。", 404)
        return row

    def _version_file(self, ch, rid, n):
        return self.data_dir / "rounds" / rid / f"V{n}{Path(ch).suffix}"

    def _vmeta(self, ch, row):
        rid, n = row["round_id"], row["n"]
        return {"id": f"{rid}:{n}", "round": rid, "number": n, "time": row["created"],
                "source_hash": row["hash"], "path": str(self._version_file(ch, rid, n))}

    def _round_meta(self, con, row):
        versions = [self._vmeta(row["ch"], v) for v in con.execute(
            "SELECT round_id,n,created,hash FROM versions WHERE round_id=? ORDER BY n", (row["id"],))]
        return {"id": row["id"], "round": row["id"], "ch": row["ch"], "started": row["started"],
                "closed": row["closed"], "merged_version": row["merged_version"], "versions": versions,
                "base_hash": versions[0]["source_hash"], "latest": versions[-1]["id"]}

    @staticmethod
    def _open(con, ch):
        return con.execute("SELECT * FROM rounds WHERE ch=? AND closed IS NULL", (ch,)).fetchone()

    @staticmethod
    def _merge_owner(con, ch):
        row = con.execute("SELECT owner FROM merging WHERE ch=?", (ch,)).fetchone()
        return row[0] if row else None

    def _available(self, con, ch):
        owner = self._merge_owner(con, ch)
        checked(not owner, f"该章正在合并，请先完成合并核对。记录 {owner}", 409)

    def merge_owner(self, ch):
        with self._tx() as con:
            return self._merge_owner(con, ch)

    def reserve_merge(self, ch, expected, selected, owner):
        """Persist a chapter-only reservation through crashes, validation, and visual review."""
        self._chapter(ch)
        with self._tx(write=True) as con:
            current = self._merge_owner(con, ch)
            checked(current in (None, owner), f"请先处理该章已有合并记录 {current}", 409)
            active = self._open(con, ch)
            checked(active and self._head(con, active["id"]) == expected, "待合并版本已变化。", 409)
            row = self._version_row(con, ch, selected)
            checked(row["round_id"] == active["id"] and row["n"] > 0, "待合并稿不属于本轮候选。")
            if not current:
                con.execute("INSERT INTO merging VALUES(?,?,?)", (ch, active["id"], owner))

    def release_merge(self, ch, owner):
        """Release only this journal's reservation, without affecting any later round."""
        with self._tx(write=True) as con:
            con.execute("DELETE FROM merging WHERE ch=? AND owner=?", (ch, owner))

    def round_info(self, ch, roundid=None):
        """Return metadata of the active round (or a named historical round), else None."""
        self._chapter(ch)
        with self._tx() as con:
            row = self._open(con, ch) if roundid is None else con.execute(
                "SELECT * FROM rounds WHERE ch=? AND id=?", (ch, roundid)).fetchone()
            return self._round_meta(con, row) if row else None

    def state(self):
        """Snapshot of all saved annotations, submitted batches, rounds, and active chapters."""
        with self._tx() as con:
            rounds = [self._round_meta(con, row) for row in con.execute("SELECT * FROM rounds ORDER BY rowid")]
            notes = self._notes(con)
            revision = sum(n["revision"] for n in notes) + sum(len(r["versions"]) + bool(r["closed"]) for r in rounds)
            return {"annotations": [n for n in notes if n["status"] != "gone"], "revision": revision,
                    "batches": [self._batch_meta(json.loads(row["data"])) for row in con.execute(
                        "SELECT data FROM batches ORDER BY n")],
                    "chapters": {r["ch"]: r for r in rounds if not r["closed"]}, "rounds": rounds}

    def read_source(self, ch):
        """Read the current registered document and validate its original UTF-8 bytes."""
        try:
            raw = self._chapter(ch).read_bytes()
        except FileNotFoundError as exc:
            raise StoreError(404, "正式章节文件已移走。") from exc
        self._decode(raw)
        return raw

    @staticmethod
    def _decode(raw):
        checked(b"\0" not in raw, "请选择 UTF-8 文本源文件。")
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise StoreError(400, "文稿源文件需要 UTF-8 编码。") from exc

    def read_version(self, ch, version):
        """Return original source bytes for a live:<sha256> or historical version id."""
        self._chapter(ch)
        if self._live_version(version):
            raw = self.read_source(ch)
            checked(version == f"live:{digest(raw)}", "正式正文已更新，请刷新。", 409)
            return raw
        with self._tx() as con:
            return bytes(self._version_row(con, ch, version)["qmd"])

    def version_info(self, ch, version):
        self._chapter(ch)
        if self._live_version(version):
            raw = self.read_version(ch, version)
            return {"id": version, "round": None, "number": None, "time": None,
                    "source_hash": digest(raw), "path": str(self._chapter(ch))}
        with self._tx() as con:
            return self._vmeta(ch, self._version_row(con, ch, version))

    def version_assets(self, ch, version):
        self._chapter(ch)
        if self._live_version(version):
            return None
        with self._tx() as con:
            return json.loads(self._version_row(con, ch, version)["assets"])

    def _add_version(self, con, ch, rid, n, raw, assets=None):
        text = self._decode(raw)
        if assets is None:
            assets = self.renderer.snapshot_assets(text, ch) if self.renderer else {}
        con.execute("INSERT INTO versions VALUES(?,?,?,?,?,?)",
                    (rid, n, stamp(), digest(raw), raw, encode(assets)))
        # Immutable derivative; repair projections from the committed blob after an interruption.
        con.version_copies.append((self._version_file(ch, rid, n), raw))
        return f"{rid}:{n}"

    @staticmethod
    def _head(con, rid):
        n = con.execute("SELECT max(n) FROM versions WHERE round_id=?", (rid,)).fetchone()[0]
        return f"{rid}:{n}"

    @staticmethod
    def _notes(con):
        return [json.loads(row[0]) for row in con.execute("SELECT data FROM notes ORDER BY rowid")]

    @staticmethod
    def _note(con, nid):
        row = con.execute("SELECT data FROM notes WHERE id=?", (nid,)).fetchone()
        return json.loads(row[0]) if row else None

    @staticmethod
    def _put(con, note):
        con.execute("INSERT INTO notes VALUES(?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                    (note["id"], encode(note)))

    def _save(self, con, inp):
        checked(isinstance(inp, dict), "批注格式无效。")
        ch = inp.get("ch")
        self._chapter(ch)
        nid = inp.get("id") or uuid.uuid4().hex
        checked(isinstance(nid, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,180}", nid), "批注编号无效。")
        existing = self._note(con, nid)
        expected = inp.get("expected_revision", inp.get("revision", 0))
        checked(isinstance(expected, int) and expected == (existing["revision"] if existing else 0),
                "这条批注已在其他页面更新，请刷新后核对。", 409)
        if existing:
            checked(existing["ch"] == ch, "批注所属章节不可更换。")
            checked(existing["status"] in {"pending", "draft", "gone"}, "已提交的批注有固定记录。", 409)
        status = inp.get("status", "pending")
        checked(status in {"pending", "draft"}, "只能保存待提交批注或输入草稿。")
        mode = inp.get("mode", "comment")
        checked(mode in {"comment", "replace"}, "批注方式无效。")
        anchor = inp.get("anchor")
        checked(isinstance(anchor, dict) and isinstance(anchor.get("type"), str), "缺少选文位置。")
        if "segs" in anchor:
            checked(isinstance(anchor["segs"], list) and all(isinstance(s, dict) for s in anchor["segs"]),
                    "选文列表格式无效。")
            for seg in anchor["segs"]:
                checked(all(isinstance(seg.get(k, ""), str) for k in ("before", "after", "text")), "选文上下文应为文本。")
        if inp.get("group_id"):
            checked(isinstance(inp["group_id"], str) and isinstance(inp.get("group_index", 0), int)
                    and isinstance(inp.get("group_count", 1), int)
                    and 0 <= inp.get("group_index", 0) < inp.get("group_count", 1), "跨章选文顺序无效。")
        if anchor["type"] == "text":
            segs = anchor.get("segs")
            checked(isinstance(segs, list) and segs and all(isinstance(s, dict) and
                    isinstance(s.get("block"), str) and isinstance(s.get("text"), str)
                    and isinstance(s.get("off"), int) and s["off"] >= 0 for s in segs), "选文定位不完整。")
        quote, text, repl = (inp.get(k, "") for k in ("quote", "text", "repl"))
        checked(all(isinstance(s, str) for s in (quote, text, repl)), "意见和替换文字须是文本。")
        if status == "pending":
            checked(bool(text.strip()) if mode == "comment" else repl != quote,
                    "请填写意见或有改动的替换文字。")
        source, target = inp.get("source_version"), inp.get("target_version")
        checked(isinstance(source, str) and source and isinstance(target, str) and target,
                "请提供所引稿本和处理目标版本。")
        for version in (source, target):
            if not self._live_version(version):
                self._version_row(con, ch, version)
        extra = ("group_id", "group_index", "group_count", "index", "count", "editing_id", "editing_revision",
                 "label", "context", "line_start", "line_end")
        note = {k: inp[k] for k in extra if k in inp}
        note.update({"id": nid, "ch": ch, "round": None, "source_version": source, "target_version": target,
                     "status": status, "mode": mode, "anchor": anchor, "quote": quote, "text": text, "repl": repl,
                     "revision": expected + 1, "at": stamp(), "created": existing["created"] if existing else stamp(),
                     "recheck": bool(inp.get("recheck", existing.get("recheck", False) if existing else False))})
        row = self._open(con, ch)
        # Autosaving text must not freeze a round. Preserve even old open editors as drafts.
        if status == "draft":
            if existing and existing["status"] == "pending":
                raise StoreError(409, "已保存的意见请使用独立的输入草稿修改。")
            if not self._live_version(target):
                note["round"] = self._version_key(target)[0]
            self._put(con, note)
            return note
        self._available(con, ch)
        if not row:
            checked(self._live_version(target), "这轮批注已经归档，请核对新稿。", 409)
            raw = self.read_source(ch)
            rid = uuid.uuid4().hex
            con.execute("INSERT INTO rounds(id,ch,started) VALUES(?,?,?)", (rid, ch, stamp()))
            self._add_version(con, ch, rid, 0, raw)
            row = self._open(con, ch)
        rid = row["id"]
        head = self._head(con, rid)
        base = rid + ":0"
        rawhash = self._version_row(con, ch, base)["hash"]
        if self._live_version(target):
            checked(head == base, "已有新候选稿，请核对后重新定位。", 409)
            note["target_version"] = head
            if target != "live:" + rawhash:
                note["recheck"] = True
        else:
            checked(target == head, "候选稿已更新，请核对后重新定位。", 409)
        if self._live_version(source):
            note["selected_version"] = source
            note["source_version"] = base
            if source != "live:" + rawhash:
                note["recheck"] = True
        elif existing and existing.get("selected_version"):
            note["selected_version"] = existing["selected_version"]
        note["round"] = rid
        sid = inp.get("saved_shadow_id")
        if sid:
            shadow = self._note(con, sid)
            if shadow is None and inp.get("saved_shadow_revision") == 0:
                # A fast Save can beat the first debounced autosave; tombstone its
                # editor id so a late request cannot resurrect the obsolete draft.
                checked(sid != nid and existing, "编辑器的输入草稿编号无效。")
                shadow = {**note, "id": sid, "editing_id": nid, "revision": 0}
            else:
                checked(shadow and shadow["status"] == "draft" and shadow["ch"] == ch and
                        shadow.get("editing_id") == nid and shadow["revision"] == inp.get("saved_shadow_revision")
                        and shadow.get("editing_revision") == expected,
                        "输入草稿或已保存的意见已改变，请先恢复并核对。", 409)
            shadow.update(status="gone", revision=shadow["revision"] + 1, at=stamp())
            self._put(con, shadow)
        self._put(con, note)
        return note

    def save(self, payload):
        """Create/edit/relocate one annotation. expected_revision (or revision) is required for updates."""
        with self._tx(write=True) as con:
            return self._save(con, payload)

    def save_many(self, payloads):
        """Atomically save e.g. per-chapter segments of one cross-chapter selection."""
        checked(isinstance(payloads, list) and payloads, "缺少要保存的批注。")
        with self._tx(write=True) as con:
            return [self._save(con, payload) for payload in payloads]

    def delete(self, id, revision):
        """Withdraw an input draft or pending note. Tombstone guards against late autosaves."""
        with self._tx(write=True) as con:
            note = self._note(con, id)
            checked(note, "找不到该批注。", 404)
            checked(note["revision"] == revision and note["status"] in {"pending", "draft"},
                    "批注已更新或已提交，请刷新。", 409)
            note.update(status="gone", revision=revision + 1, at=stamp())
            self._put(con, note)
            return note

    @staticmethod
    def _batch_meta(batch):
        return {k: batch[k] for k in ("id", "number", "time", "ids", "chapters")}

    def submit(self, items, key):
        """Atomically submit exactly [{id,revision}, ...]. Repeating a key and item set returns the same batch."""
        checked(isinstance(key, str) and 0 < len(key) <= 250, "提交缺少请求编号。")
        checked(isinstance(items, list) and items, "没有要提交的批注。")
        checked(all(isinstance(it, dict) and isinstance(it.get("id"), str) and
                    isinstance(it.get("revision"), int) for it in items), "批注清单缺少编号或修订号。")
        ids = [it["id"] for it in items]
        checked(len(ids) == len(set(ids)), "同一条批注只能提交一次。")
        fingerprint = digest(encode(sorted((it["id"], it["revision"]) for it in items)).encode())
        with self._tx(write=True) as con:
            prev = con.execute("SELECT * FROM batches WHERE key=?", (key,)).fetchone()
            if prev:
                checked(prev["fingerprint"] == fingerprint, "这个请求编号已用于另一组批注。", 409)
                batch = json.loads(prev["data"])
            else:
                created = stamp()
                n = con.execute("INSERT INTO batches(key,fingerprint,data) VALUES(?,?,?)",
                                (key, fingerprint, "{}")).lastrowid
                notes = []
                for it in items:
                    note = self._note(con, it["id"])
                    checked(note and note["status"] == "pending" and note["revision"] == it["revision"],
                            "批注已改变，请核对最新清单再提交。", 409)
                    self._available(con, note["ch"])
                    row = self._open(con, note["ch"])
                    checked(row and row["id"] == note["round"], "批注所属轮次已归档。", 409)
                    checked(not note.get("recheck") and note["target_version"] == self._head(con, row["id"]),
                            "部分意见仍需核对位置或处理目标。", 409)
                    note.update(status="sent", batch=n, submitted=created, revision=note["revision"] + 1)
                    self._put(con, note)
                    notes.append(note)
                parents = {note["ch"]: self._vmeta(note["ch"], self._version_row(con, note["ch"], note["target_version"]))
                           for note in notes}
                batch = {"id": n, "number": n, "time": created, "ids": ids, "chapters": list(parents),
                         "annotations": notes, "parents": parents}
                con.execute("UPDATE batches SET data=? WHERE n=?", (encode(batch), n))
        self._refresh_exports()
        return self._batch_meta(batch)

    def publish(self, ch, inputfile, parent_version, applied_ids=(), questions=None):
        """Publish a new candidate from inputfile against the exact current parent.

        applied_ids mark submitted notes addressed. questions maps submitted ids to
        explanations. Older unresolved batches stay visible until explicitly addressed.
        """
        self._chapter(ch)
        raw = Path(inputfile).read_bytes()
        checked(isinstance(applied_ids, (list, tuple, set)), "已处理批注应为编号列表。")
        questions = {} if questions is None else questions
        checked(isinstance(questions, dict) and all(isinstance(s, str) and s.strip() for s in questions.values()),
                "待确认问题应为批注编号到原因的映射。")
        checked(all(isinstance(i, str) for i in [*applied_ids, *questions]), "批注编号应为文本。")
        ids = [*applied_ids, *questions]
        checked(len(set(ids)) == len(ids), "同一条意见不能同时被重复处理或列为问题。")
        with self._tx(write=True) as con:
            self._available(con, ch)
            row = self._open(con, ch)
            checked(row and self._head(con, row["id"]) == parent_version,
                    "处理目标已更新或已经归档，请核对最新稿。", 409)
            pending = {}
            for nid in ids:
                note = self._note(con, nid)
                checked(note and note["round"] == row["id"] and note["ch"] == ch and note["status"] in {"sent", "question"},
                        "批注不在本轮已提交、待处理意见中：" + nid, 409)
                pending[nid] = note
            _, pn = self._version_key(parent_version)
            version = self._add_version(con, ch, row["id"], pn + 1, raw)
            for nid, note in pending.items():
                status = "question" if nid in questions else "applied"
                note.update(status=status, applied_version=version, question=questions.get(nid, ""),
                            revision=note["revision"] + 1, at=stamp())
                self._put(con, note)
            # Unsubmitted opinions pinned to a now superseded target need explicit relocation.
            for note in self._notes(con):
                if note["round"] == row["id"] and note["status"] == "pending" and note["target_version"] != version:
                    note.update(recheck=True, revision=note["revision"] + 1)
                    self._put(con, note)
            result = self._vmeta(ch, self._version_row(con, ch, version))
        self._refresh_exports()
        return result

    def _archive(self, con, ch, expected_version, merged_version):
        row = self._open(con, ch)
        checked(row and self._head(con, row["id"]) == expected_version,
                "该章已有新稿或轮次已归档，请重新核对。", 409)
        merged = merged_version or expected_version
        rid, n = self._version_key(merged)
        checked(rid == row["id"] and n > 0, "请选择本轮已经发布的候选稿。")
        self._version_row(con, ch, merged)
        con.execute("UPDATE rounds SET closed=?, merged_version=? WHERE id=?", (stamp(), merged, rid))
        return self._round_meta(con, con.execute("SELECT * FROM rounds WHERE id=?", (rid,)).fetchone())

    def close_round(self, ch, expected_version, merged_version=None, owner=None):
        """Archive after external merge succeeds (latest CAS, possibly an explicitly earlier merged candidate)."""
        self._chapter(ch)
        with self._tx(write=True) as con:
            checked(self._merge_owner(con, ch) == owner, "该章合并记录不匹配。", 409)
            result = self._archive(con, ch, expected_version, merged_version)
            con.execute("DELETE FROM merging WHERE ch=?", (ch,))
        self._refresh_exports()
        return result

    @contextlib.contextmanager
    def merge_guard(self, ch, expected_latest, selected_version=None):
        """Reserve the writing transaction during an external merge. Raise in body to leave the round open."""
        self._chapter(ch)
        with self._tx(write=True) as con:
            self._available(con, ch)
            archived = self._archive(con, ch, expected_latest, selected_version)
            yield archived
        self._refresh_exports()

    def _refresh_exports(self):
        # The committed DB survives copy failure; retrying submission with the same key repairs the handoff.
        with self._tx(write=True) as con:
            self._projections(con, repair=False)

    def _projections(self, con, repair=True):
        if repair:
            for row in con.execute("SELECT r.ch, v.* FROM versions v JOIN rounds r ON r.id=v.round_id"):
                path = self._version_file(row["ch"], row["round_id"], row["n"])
                if not path.is_file() or digest(path.read_bytes()) != row["hash"]:
                    atomic_write(path, bytes(row["qmd"]))
        last = None
        for row in con.execute("SELECT data FROM batches ORDER BY n"):
            batch = json.loads(row[0])
            name = f'{batch["number"]:04d}'
            self._export_pair(self.data_dir / "submissions" / name, batch, immutable=True)
            last = batch
        if not last:
            return
        notes = self._notes(con)
        opened = {r["id"]: r["ch"] for r in con.execute("SELECT * FROM rounds WHERE closed IS NULL")}
        unresolved = [n for n in notes if n["round"] in opened and n["status"] in {"question", "sent"}]
        parents = {}
        for note in unresolved:
            ch = note["ch"]
            parents[ch] = self._vmeta(ch, self._version_row(con, ch, self._head(con, note["round"])))
        latest = {**self._batch_meta(last), "parents": parents, "annotations": unresolved,
                  "ids": [a["id"] for a in unresolved], "chapters": list(parents),
                  "latest_batch": self._batch_meta(last)}
        self._export_pair(self.data_dir / "latest", latest)

    def _export_pair(self, base, data, immutable=False):
        contents = ((base.with_suffix(".json"), pretty(data)), (base.with_suffix(".md"), self._markdown(data)))
        for path, content in contents:
            encoded = content.encode("utf-8")
            if immutable and path.is_file() and (path.suffix == ".md" or path.read_bytes() == encoded):
                continue
            atomic_write(path, encoded)

    def _markdown(self, data):
        windows = os.name == 'nt'
        lines = [f'# 文稿批注 · 第 {data["number"]} 批', '', f'提交于 {data["time"]}', '']
        if "latest_batch" in data:
            lines += ['以下汇总各批次仍待处理和待确认的意见。条目保留原提交批次和所引版本。', '']
        lines += ['## 处理方式', '',
                  '从下列父稿导出独立候选文件。保留作者的源文件格式和项目写作约定，对照原句、上下文、源码行号和所引版本修改。逐字替换采用作者给出的文字。选文引自旧稿时，在当前父稿中核对对应位置。',
                  '', '发布时逐条关联落实的批注。需要作者裁决时，用 --questions 指定 JSON 文件，键为批注编号，值为待确认的问题。作者明确指定文稿和稿次后才合并到正式文件。', '']
        if not data["annotations"]:
            lines.append('当前没有仍待处理的已提交意见。')
        cli = [sys.executable, str(Path(__file__).with_name('review.py').resolve()),
               '--root', str(self.root), '--data', str(self.data_dir)]
        for ch, meta in data["parents"].items():
            candidate = str(self.data_dir / 'candidates' / meta["id"].replace(':', '-') / ch)
            export = cli + ['export', '--chapter', ch, '--version', meta["id"], '--output', candidate]
            publish = cli + ['publish', '--chapter', ch, '--input', candidate, '--parent', meta["id"], '--applied', '批注编号']
            lines += [f'## {ch}', '', f'父稿版本 `{meta["id"]}`', '', f'父稿原文件 `{meta["path"]}`', '',
                      '先导出父稿，修改导出的文件，然后按实际处理的批注编号发布。', '',
                      '```powershell' if windows else '```sh',
                      handoff_command(export, windows), handoff_command(publish, windows), '```', '']
            for a in data["annotations"]:
                if a["ch"] != ch:
                    continue
                lines += [f'### {a["id"]} · 第 {a["batch"]} 批', '',
                          f'所引版本 `{a["source_version"]}`，提交时处理目标 `{a["target_version"]}`。', '']
                if a.get("group_id"):
                    part = int(a.get("group_index", 0)) + 1
                    count = a.get("group_count", "?")
                    lines.extend((f'跨章批注组 `{a["group_id"]}`，选区第 {part}/{count} 段，共用一条作者意见。',
                                  '跨章替换文字只用于整组选区一次。逐章核对各段后处理；分章位置不明确时请列为待确认。', ''))
                anchor = a["anchor"]
                segs = anchor.get("segs") or [anchor]
                for seg in segs:
                    start = seg.get("line_start", a.get("line_start"))
                    end = seg.get("line_end", a.get("line_end", start))
                    loc = f'第 {start}–{end} 行' if start is not None else '章节位置'
                    ctx = seg.get("before", "") + "【" + seg.get("text", a["quote"]) + "】" + seg.get("after", "")
                    lines += [f'{loc} · {anchor["type"]} · {seg.get("block", ch)}' +
                              (f' / {seg["sub"]}' if seg.get("sub") else ''), '', ctx, '']
                lines += ['选文', '', a["quote"], '', '逐字替换为' if a["mode"] == "replace" else '作者意见', '',
                          a["repl"] if a["mode"] == "replace" else a["text"], '']
                if a.get("question"):
                    lines += ['待作者确认', '', a["question"], '']
        return "\n".join(lines) + "\n"
