"""Test doubles: FakeDynalist (read/edit semantics + fault injection) and FakeLLM."""
from __future__ import annotations

import copy
import itertools
from collections import defaultdict, deque
from typing import Any, Callable

from tutorbot.dynalist.client import F_INDEX, F_NEW_IDS, F_NODE, F_PARENT, UnknownOutcome


class FakeDynalist:
    """Implements the `Transport` signature: fake(path, body) -> (status, json)."""

    def __init__(self) -> None:
        self.docs: dict[str, dict[str, dict]] = {}
        self.versions: dict[str, int] = defaultdict(int)
        self._ids = itertools.count(1)
        self.clock_ms = 1_700_000_000_000
        self.calls: list[tuple[str, dict]] = []
        self.applied_changes: list[tuple[str, dict]] = []   # (file_id, change)
        self._faults: dict[str, deque] = defaultdict(deque)

    # --- document building (test helpers) -------------------------------------
    def add_doc(self, file_id: str) -> None:
        self.docs[file_id] = {"root": self._mk("root", "", "")}

    def _tick(self) -> int:
        self.clock_ms += 1000
        return self.clock_ms

    def _mk(self, nid: str, content: str, note: str, created: int | None = None,
            modified: int | None = None, checked: bool = False) -> dict:
        t = self._tick()
        return {"id": nid, "content": content, "note": note, "created": created or t,
                "modified": modified or t, "children": [], "checked": checked,
                "checkbox": False, "collapsed": False, "heading": 0, "color": 0}

    def add(self, file_id: str, parent: str, content: str, note: str = "", index: int = -1,
            created: int | None = None, modified: int | None = None) -> str:
        doc = self.docs[file_id]
        nid = f"n{next(self._ids)}"
        doc[nid] = self._mk(nid, content, note, created, modified)
        kids = doc[parent]["children"]
        kids.insert(len(kids) if index == -1 else index, nid)
        self.versions[file_id] += 1
        return nid

    def children(self, file_id: str, nid: str) -> list[dict]:
        doc = self.docs[file_id]
        return [doc[c] for c in doc[nid]["children"]]

    def count_nodes(self, file_id: str) -> int:
        return len(self.docs[file_id]) - 1

    # --- fault injection ---------------------------------------------------------
    def fail(self, path: str, code: str = "TooManyRequests", times: int = 1, status: int = 200) -> None:
        for _ in range(times):
            self._faults[path].append(("error", code, status))

    def crash_after_apply(self, path: str = "doc/edit") -> None:
        """Next call applies its changes, then raises UnknownOutcome (response lost)."""
        self._faults[path].append(("crash_after_apply",))

    # --- transport -----------------------------------------------------------------
    def __call__(self, path: str, body: dict) -> tuple[int, dict]:
        self.calls.append((path, copy.deepcopy(body)))
        fault = self._faults[path].popleft() if self._faults[path] else None
        if fault and fault[0] == "error":
            _, code, status = fault
            return (status if status != 200 else 200), {"_code": code, "_msg": "injected"}
        handler: Callable[[dict], dict] = {
            "file/list": self._file_list, "doc/read": self._doc_read, "doc/edit": self._doc_edit,
        }[path]
        resp = handler(body)
        if fault and fault[0] == "crash_after_apply":
            raise UnknownOutcome("Timeout", "injected: response lost after apply")
        return 200, resp

    def _file_list(self, body: dict) -> dict:
        return {"_code": "Ok", "files": [{"id": f, "title": f, "type": "document"} for f in self.docs]}

    def _doc_read(self, body: dict) -> dict:
        fid = body["file_id"]
        if fid not in self.docs:
            return {"_code": "NotFound", "_msg": "file"}
        return {"_code": "Ok", "version": self.versions[fid],
                "nodes": copy.deepcopy(list(self.docs[fid].values()))}

    def _parent_of(self, doc: dict, nid: str) -> str | None:
        for n in doc.values():
            if nid in n["children"]:
                return n["id"]
        return None

    def _delete_subtree(self, doc: dict, nid: str) -> None:
        for c in list(doc[nid]["children"]):
            self._delete_subtree(doc, c)
        del doc[nid]

    def _doc_edit(self, body: dict) -> dict:
        fid = body["file_id"]
        if fid not in self.docs:
            return {"_code": "NotFound", "_msg": "file"}
        doc = self.docs[fid]
        new_ids: list[str] = []
        for ch in body["changes"]:
            act = ch["action"]
            if act == "insert":
                if ch[F_PARENT] not in doc:
                    return {"_code": "NodeNotFound", "_msg": ch[F_PARENT]}
                nid = f"n{next(self._ids)}"
                t = self._tick()
                doc[nid] = self._mk(nid, ch.get("content", ""), ch.get("note", ""),
                                    checked=ch.get("checked", False))
                kids = doc[ch[F_PARENT]]["children"]
                idx = ch.get(F_INDEX, -1)
                kids.insert(len(kids) if idx == -1 else idx, nid)
                new_ids.append(nid)
            elif act == "edit":
                if ch[F_NODE] not in doc:
                    return {"_code": "NodeNotFound", "_msg": ch[F_NODE]}
                n = doc[ch[F_NODE]]
                for k in ("content", "note", "checked"):
                    if k in ch:
                        n[k] = ch[k]
                n["modified"] = self._tick()
            elif act == "delete":
                if ch[F_NODE] not in doc:
                    return {"_code": "NodeNotFound", "_msg": ch[F_NODE]}
                p = self._parent_of(doc, ch[F_NODE])
                if p:
                    doc[p]["children"].remove(ch[F_NODE])
                self._delete_subtree(doc, ch[F_NODE])
            elif act == "move":
                nid = ch[F_NODE]
                if nid not in doc or ch[F_PARENT] not in doc:
                    return {"_code": "NodeNotFound", "_msg": nid}
                p = self._parent_of(doc, nid)
                if p:
                    doc[p]["children"].remove(nid)
                kids = doc[ch[F_PARENT]]["children"]
                idx = ch.get(F_INDEX, -1)
                kids.insert(len(kids) if idx == -1 else idx, nid)
            else:
                return {"_code": "Invalid", "_msg": act}
            self.applied_changes.append((fid, copy.deepcopy(ch)))
        self.versions[fid] += 1
        return {"_code": "Ok", F_NEW_IDS: new_ids}


class FakeLLM:
    """Per-task canned responses. Queue values or a callable(messages)->dict."""

    def __init__(self) -> None:
        self.responses: dict[str, Any] = {}
        self.calls: list[tuple[str, Any]] = []

    def set(self, task: str, *responses: Any) -> None:
        self.responses[task] = deque(responses) if len(responses) != 1 or not callable(responses[0]) \
            else responses[0]

    def complete_json(self, task: str, messages: Any, schema: Any = None) -> dict:
        self.calls.append((task, messages))
        r = self.responses.get(task)
        if r is None:
            raise AssertionError(f"FakeLLM: no response configured for task {task!r}")
        if callable(r):
            return r(messages)
        item = r[0] if len(r) == 1 else r.popleft()
        if isinstance(item, Exception):
            raise item
        return item
