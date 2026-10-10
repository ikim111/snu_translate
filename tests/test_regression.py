"""회귀 검사 — 실제 논문 PDF로 추출·구조를 확인한다 (번역 API는 쓰지 않음).

논문 PDF는 저작권 때문에 저장소에 넣지 않는다. 내 컴퓨터에 있는 파일 경로를 환경 변수로 주면 실행된다.
    STEIN_PDF=…/Stein_Grover_Henningsen1996….pdf BEHRENS_PDF=…/Behrens1997….pdf python -m pytest tests -q
경로가 없으면 해당 검사는 건너뛴다. 단위 검사(맨 아래)는 항상 실행된다.
"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pymupdf  # noqa: E402

import pdf_core as c  # noqa: E402


def _load(env):
    path = os.environ.get(env)
    if not path or not Path(path).exists():
        pytest.skip(f"{env} 없음")
    d = pymupdf.open(path)
    return d, c.extract_document(d)


@pytest.fixture(scope="module")
def stein():
    return _load("STEIN_PDF")


@pytest.fixture(scope="module")
def behrens():
    return _load("BEHRENS_PDF")


def kinds(pg, k):
    return [b for b in pg["blocks"] if b["kind"] == k]


def table_view(t):
    it, lay = t["items"], t["layout"]
    return ([c.plain(it[h]) if h is not None else None for h in lay["header"]],
            [[" / ".join(c.plain(it[i]) for i in cell) for cell in r] for r in lay["rows"]],
            c.plain(it[lay["super"]]) if lay.get("super") is not None else None)


def test_stein_title_and_byline(stein):
    _, pages = stein
    p2 = pages[1]
    titles = [b for b in p2["blocks"] if b["kind"] == "heading" and b.get("title")]
    assert len(titles) == 1 and "Building Student Capacity" in titles[0]["text"] and "Classrooms" in titles[0]["text"]
    by = [b["text"] for b in p2["blocks"] if b.get("byline")]
    assert by[:3] == ["Mary Kay Stein", "Learning Research and Development Center,", "University of Pittsburgh"]
    assert "Ohio University" in by and "Marjorie Henningsen" in by
    assert any(b["kind"] == "meta" and "American Educational Research Journal" in b["text"] for b in p2["blocks"])


def test_stein_ocr_fixes(stein):
    _, pages = stein
    t11 = " ".join(b["text"] for b in pages[10]["blocks"])
    assert "(8%)" in t11 and "80/6" not in t11
    assert "building" in " ".join(b["text"] for b in pages[1]["blocks"])
    assert pages[2]["blocks"][[b["kind"] for b in pages[2]["blocks"]].index("para")]["text"].startswith("The mathematics")


def test_stein_page_boundaries_not_moved(stein):
    _, pages = stein
    flow = lambda pg: [b for b in pg["blocks"] if b["kind"] in c.FLOW_KINDS]
    assert flow(pages[21])[-1]["text"].rstrip().endswith("does not")
    assert flow(pages[22])[0]["text"].startswith("automatically guarantee")
    assert flow(pages[20])[0]["text"].startswith("Tasks that were set up")


def test_stein_headings(stein):
    _, pages = stein
    heads = {(pg["page_number"], b["text"]) for pg in pages for b in pg["blocks"] if b["kind"] == "heading"}
    for want in [(5, "Conceptual Framework"), (5, "Mathematical Tasks"), (10, "Purpose of the Study"),
                 (11, "Sampling Procedure"), (12, "Coding"), (8, "Cognitively Demanding Tasks")]:
        assert want in heads, want


def test_stein_numbered_items(stein):
    _, pages = stein
    items = [b["text"][:3] for b in pages[9]["blocks"] if b.get("item")]
    assert items == ["(1)", "(2)", "(3)"]


def test_stein_tables(stein):
    _, pages = stein
    head, rows, sup = table_view(kinds(pages[20], "table")[0])                    # 표 3
    assert sup == "Implementation" and len(head) == 5 and len(rows) == 4
    assert rows[0][1:] == ["87% (20)", "4% (1)", "0", "9% (2)"]
    assert any("<b>87%</b>" in x for x in kinds(pages[20], "table")[0]["items"])     # 굵은 대각선 수치
    head, rows, sup = table_view(kinds(pages[21], "table")[0])                    # 표 4
    assert len(head) == 4 and len(rows) == 2 and rows[1][2] == "74% (65)"
    p24 = pages[23]                                                               # 표 5 (가로 스캔)
    assert p24.get("rotated")
    head, rows, sup = table_view(kinds(p24, "table")[0])
    assert len(head) == 8 and len(rows) == 7
    doing = [r for r in rows if r[0].startswith("“Doing")][0]
    assert doing[1:] == ["17% (10)", "", "14% (8)", "3% (2)", "38% (22)", "26% (15)", "2% (1)"]
    t1 = kinds(pages[11], "table")[0]                                             # 표 1
    assert [s for _, s in t1["layout"]["group_row"]] == [2, 2]


def test_stein_figures(stein):
    _, pages = stein
    f28 = kinds(pages[27], "figure")[0]
    assert f28["bbox"].y0 < 72                         # 100% 눈금까지 포함
    f18 = kinds(pages[17], "figure")[0]
    assert f18["bbox"].x1 > 330                        # 나란한 세 그래프 모두


def test_stein_references_and_numbers(stein):
    _, pages = stein
    refs35 = kinds(pages[34], "reference")
    assert refs35[0]["text"].startswith("Schroyer") and refs35[-1]["text"].startswith("Wittrock")
    assert [b["text"] for b in pages[34]["blocks"] if b.get("right")] == \
        ["Manuscript received August 10, 1994", "Accepted February 3, 1995"]
    assert kinds(pages[32], "reference")[0]["text"].startswith("Anderson")
    assert [pg.get("printed") for pg in pages[2:6]] == ["456", "457", "458", "459"]
    assert pages[23].get("printed") is None


def test_behrens(behrens):
    d, pages = behrens
    assert len(pages) == d.page_count
    tables = [(pg["page_number"], t) for pg in pages for t in kinds(pg, "table")]
    assert [p for p, _ in tables] == [5, 10, 11, 12]
    head, rows, sup = table_view(tables[2][1])
    assert sup == "Ethnicity" and head == ["Occupation", "Native American", "Hispanic", "White"]
    assert rows[0] == ["X-ray technician", "-19", "-24", "-23.5"]
    assert sum(len(kinds(pg, "reference")) for pg in pages) > 100
    assert sum(len(kinds(pg, "figure")) for pg in pages) >= 15


# ─────────────── 단위 검사 ───────────────
def test_pick_token():
    f = {"ohio": 3, "obio": 1, "(n": 4, "(1": 1}
    assert c._pick_token("building", "buildin", f) == "building"
    assert c._pick_token("(8%).", "(80/6).", f) == "(8%)."
    assert c._pick_token("Obio", "Ohio", f) == "Ohio"
    assert c._pick_token("(1", "(n", f) == "(n"
    assert c._pick_token("24", "-24", f) == "-24"
    assert c._pick_token("“Representations", "\"aRepresentations", f) == "aRepresentations"


def test_apply_scan_check():
    out = c.apply_scan_check("of mathematical tasks, a relative of academic tasks; Can not discern",
                             ["mathematical tasks", "academic tasks"], [("Can not", "Cannot")])
    assert out == "of <i>mathematical tasks</i>, a relative of <i>academic tasks</i>; Cannot discern"


def test_split_joined():
    assert c._split_joined("vehiclesfor", {"vehicles", "for"}) == 8
    assert c._split_joined("building", {"building"}) is None
