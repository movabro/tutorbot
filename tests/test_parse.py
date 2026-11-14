from datetime import date

from tests.fakes import FakeDynalist
from tutorbot.dynalist.tree import NodeIndex
from tutorbot.parse.classify import Classifier, is_skip_answer
from tutorbot.parse.dates import resolve_entry_date
from tutorbot.parse.hashing import content_hash, norm
from tutorbot.parse.tags import extract_tags, has_tag

F = "doc1"


def build(fn):
    fk = FakeDynalist()
    fk.add_doc(F)
    ids = fn(fk)
    idx = NodeIndex.from_doc_read(F, fk._doc_read({"file_id": F}))
    return fk, idx, ids


def h(idx, nid, reg=None):
    return content_hash(idx, nid, Classifier(idx, registry=reg).is_bot)


def basic(fk):
    s = fk.add(F, "root", "#공부 러스트 소유권 공부", "note")
    a = fk.add(F, s, "child A")
    b = fk.add(F, s, "child B")
    return s, a, b


# ---------------- hashing ----------------
def test_same_tree_same_hash():
    _, i1, (s1, *_) = build(basic)
    _, i2, (s2, *_) = build(basic)
    assert h(i1, s1) == h(i2, s2)
    assert h(i1, s1).startswith("v1:")


def test_bot_child_does_not_change_hash():
    fk, idx, (s, a, b) = build(basic)
    before = h(idx, s)
    fk.add(F, s, "🤖 피드백 #tutorbot", "tb:feedback:1")
    idx2 = NodeIndex.from_doc_read(F, fk._doc_read({"file_id": F}))
    assert h(idx2, s) == before


def test_registered_bot_child_without_tag_excluded():
    fk, idx, (s, a, b) = build(basic)
    before = h(idx, s)
    x = fk.add(F, s, "plain looking")
    idx2 = NodeIndex.from_doc_read(F, fk._doc_read({"file_id": F}))
    assert h(idx2, s) != before
    assert h(idx2, s, {x: "feedback"}) == before


def test_checked_collapsed_do_not_change():
    fk, idx, (s, a, b) = build(basic)
    before = h(idx, s)
    fk.docs[F][a]["checked"] = True
    fk.docs[F][a]["collapsed"] = True
    fk.docs[F][s]["color"] = 3
    idx2 = NodeIndex.from_doc_read(F, fk._doc_read({"file_id": F}))
    assert h(idx2, s) == before


def test_order_change_changes_hash():
    fk, idx, (s, a, b) = build(basic)
    before = h(idx, s)
    fk.docs[F][s]["children"].reverse()
    idx2 = NodeIndex.from_doc_read(F, fk._doc_read({"file_id": F}))
    assert h(idx2, s) != before


def test_note_change_changes_hash():
    fk, idx, (s, a, b) = build(basic)
    before = h(idx, s)
    fk.docs[F][a]["note"] = "new note"
    idx2 = NodeIndex.from_doc_read(F, fk._doc_read({"file_id": F}))
    assert h(idx2, s) != before


def test_normalization():
    assert norm("  a \t b  \n\n c ") == "a b\nc"
    assert norm("\u1100\u1161") == norm("가")  # NFC
    assert norm(None) == ""
    _, i1, (s1, *_) = build(lambda fk: (fk.add(F, "root", "#공부  x   y"),))
    _, i2, (s2, *_) = build(lambda fk: (fk.add(F, "root", " #공부 x y "),))
    assert h(i1, s1) == h(i2, s2)


def test_move_does_not_change_hash():
    fk, idx, (s, a, b) = build(basic)
    before = h(idx, s)
    p = fk.add(F, "root", "container")
    fk.docs[F]["root"]["children"].remove(s)
    fk.docs[F][p]["children"].append(s)
    idx2 = NodeIndex.from_doc_read(F, fk._doc_read({"file_id": F}))
    assert h(idx2, s) == before


# ---------------- dates ----------------
TZ = "Asia/Seoul"


def test_date_self_beats_ancestor():
    def f(fk):
        p = fk.add(F, "root", "2026-01-01")
        s = fk.add(F, p, "#공부 내용 !(2026-02-03)")
        return (s,)
    _, idx, (s,) = build(f)
    assert resolve_entry_date(idx, idx.get(s), TZ) == (date(2026, 2, 3), "self")


def test_date_ancestor_forms():
    for text in ("2026-01-05 월요일", "2026.01.05", "2026/01/05", "!(2026-01-05)"):
        def f(fk, text=text):
            p = fk.add(F, "root", text)
            return (fk.add(F, p, "#공부 x"),)
        _, idx, (s,) = build(f)
        assert resolve_entry_date(idx, idx.get(s), TZ) == (date(2026, 1, 5), "ancestor"), text


def test_date_nearest_ancestor_wins():
    def f(fk):
        a = fk.add(F, "root", "2026-01-01")
        b = fk.add(F, a, "2026-01-09")
        return (fk.add(F, b, "#공부 x"),)
    _, idx, (s,) = build(f)
    assert resolve_entry_date(idx, idx.get(s), TZ)[0] == date(2026, 1, 9)


def test_date_created_timezone_boundary():
    # 2026-03-10T15:30Z == 2026-03-11 00:30 KST
    from datetime import datetime, timezone
    ms = int(datetime(2026, 3, 10, 15, 30, tzinfo=timezone.utc).timestamp() * 1000)
    _, idx, (s,) = build(lambda fk: (fk.add(F, "root", "#공부 x", created=ms),))
    assert resolve_entry_date(idx, idx.get(s), TZ) == (date(2026, 3, 11), "created")
    assert resolve_entry_date(idx, idx.get(s), "UTC") == (date(2026, 3, 10), "created")


# ---------------- tags / classify ----------------
def test_tag_regex():
    assert extract_tags("#공부 #rust, x#no (#paren) #a.b") == ["공부", "rust", "paren", "a"]
    assert has_tag("hello #Study", ["study"])
    assert not has_tag("hello", ["study"])


def test_nested_study_outermost_is_unit():
    def f(fk):
        o = fk.add(F, "root", "#공부 outer")
        i = fk.add(F, o, "#공부 inner")
        return o, i
    _, idx, (o, i) = build(f)
    units = Classifier(idx).find_units()
    assert [u.node.id for u in units] == [o]
    assert units[0].nested_ignored == [i]


def test_bot_subtree_excluded_from_units():
    def f(fk):
        bot = fk.add(F, "root", "🤖 커리큘럼 #tutorbot", "tb:curriculum_root:1")
        inner = fk.add(F, bot, "#공부 fake")
        marker = fk.add(F, "root", "#공부 hi", "tb:weird:1")
        return bot, inner, marker
    _, idx, _ids = build(f)
    assert Classifier(idx).find_units() == []


def test_alias_tags_and_kinds_and_topic():
    def f(fk):
        return (fk.add(F, "root", "#goal #rust Rust"), fk.add(F, "root", "#dump #rust"),
                fk.add(F, "root", "#study #Rust things"), fk.add(F, "root", "#지식덤프 x"))
    _, idx, ids = build(f)
    units = Classifier(idx).find_units()
    assert [u.kind for u in units] == ["goal", "dump", "study", "dump"]
    assert units[0].topic_tags == ["rust"]
    assert units[2].topic_tags == ["rust"]


def test_quiz_answer_area():
    def f(fk):
        q = fk.add(F, "root", "[Q1] question?")
        g = fk.add(F, q, "🤖 3/5", "tb:grade:1")
        a = fk.add(F, q, "my answer")
        a2 = fk.add(F, a, "detail")
        return q, g, a
    _, idx, (q, g, a) = build(f)
    c = Classifier(idx, registry={q: "quiz_item", g: "grade"})
    assert c.answer_text(idx.get(q)) == "my answer\n  detail"
    assert c.is_bot(idx.get(g)) and not c.is_bot(idx.get(a))
    assert c.answer_hash(idx.get(q)) is not None


def test_quiz_unanswered():
    def f(fk):
        q = fk.add(F, "root", "[Q1] question?")
        fk.add(F, q, "🤖 x", "tb:grade:1")
        return (q,)
    _, idx, (q,) = build(f)
    c = Classifier(idx, registry={q: "quiz_item"})
    assert c.answer_text(idx.get(q)) == ""
    assert c.answer_hash(idx.get(q)) is None


def test_skip_answers():
    sk = ["?", "모름", "pass", "패스"]
    assert is_skip_answer(" Pass ", sk) and is_skip_answer("모 름", sk) and is_skip_answer("?", sk)
    assert not is_skip_answer("passport", sk)
