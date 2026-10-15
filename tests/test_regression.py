"""회귀 검사 — 실제 논문 PDF로 추출·구조를 확인한다 (번역 API는 쓰지 않음).

논문 PDF는 저작권 때문에 저장소에 넣지 않는다. 내 컴퓨터에 있는 파일 경로를 환경 변수로 주면 실행된다.
    STEIN_PDF=…/Stein….pdf BEHRENS_PDF=…/Behrens1997….pdf FRAMING_PDF=…/Gutiérrez2009 Framing Equity.pdf python -m pytest tests -q
경로가 없으면 해당 검사는 건너뛴다. 단위 검사(맨 아래)는 항상 실행된다.
"""
import os
import re
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


@pytest.fixture(scope="module")
def framing():
    return _load("FRAMING_PDF")          # Gutiérrez (2009) Framing Equity — 겹쳐 찍은 글자, 기울여 그린 이탤릭


@pytest.fixture(scope="module")
def porter():
    return _load("PORTER_PDF")           # Porter (2002) — 장식 첫 글자, 쪽 위 표 제목, 옆 범례 그림, 여러 줄 칸 표


@pytest.fixture(scope="module")
def tran():
    return _load("TRAN_PDF")             # Tran & Tarr (2018) — 칸 사이 좁은 글자 표(부록 2), 그래프 아래 범례


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


def test_framing_equity(framing):
    d, pages = framing
    allt = [b["text"] for pg in pages for b in pg["blocks"]]
    # 겹쳐 찍은 제목·초록이 한 번씩만
    p1 = pages[0]["blocks"]
    assert sum("Framing Equity" in b["text"] for b in p1) == 1
    assert sum(b["text"].strip() == "Abstract" for b in p1) == 1
    assert not any("Rochelle Gutiérrez Rochelle" in t for t in allt)
    # 2쪽: 위쪽 제목 + 좌우 2단
    assert any(b["kind"] == "heading" and "Framing Equity" in b["text"] for b in pages[1]["blocks"])
    assert c._gutter(pages[1]["blocks"], d[1].rect) is not None
    # 3쪽: 도식 전체(위 타원 포함), 도식 제목, 2단
    fig = kinds(pages[2], "figure")[0]
    assert fig["bbox"].y0 < 70 and fig["bbox"].y1 > 160
    assert any(b["kind"] == "caption" and "Dimensions of Equity" in b["text"] for b in pages[2]["blocks"])
    assert c._gutter(pages[2]["blocks"], d[2].rect) is not None
    # 4쪽: 단 맨 위 참고문헌 첫 줄이 버려지지 않음
    refs = [b["text"] for b in kinds(pages[3], "reference")]
    assert any(t.startswith("Abreu") and "valorization" in t for t in refs)
    assert any(t.startswith("Gutstein") and "Reading and writing the world" in t for t in refs)
    assert any("Mérida" in t for t in refs)
    # 5쪽: 제목, 질문은 참고문헌이 아니라 번역할 본문, 7번 기울임
    p5 = pages[4]["blocks"]
    assert any(b["kind"] == "heading" and "Post" in b["text"] for b in p5)
    qs = [b for b in p5 if re.match(r"\d\.", b["text"])]
    assert len(qs) == 8 and all(b["kind"] == "para" for b in qs)
    assert "<i>make note of the ways" in qs[6]["html"]
    # 2쪽 기울임·밑줄 강조
    h2 = " ".join(b.get("html") or "" for b in pages[1]["blocks"])
    for w in ("Access", "Achievement", "Identity", "past"):
        assert f"<i>{w}</i>" in h2


def test_porter(porter):
    d, pages = porter
    p1 = pages[0]["blocks"]
    assert not any(b["kind"] == "heading" and b["text"].rstrip().endswith(" T") for b in p1)
    assert any(b["text"].startswith("The content of instruction plays") for b in p1)
    p4 = pages[3]["blocks"]
    t2 = next(b for b in p4 if b["text"].startswith("Table 2 presents"))
    assert t2["bbox"].x1 < 300                                  # 오른쪽 단 문단을 삼키지 않음
    assert any(b["kind"] == "caption" and b["text"].startswith("Table 2") for b in p4)
    fig7 = kinds(pages[6], "figure")[0]
    assert fig7["bbox"].x1 > 540                                # 오른쪽 범례까지 그림
    tb = kinds(pages[10], "table")[0]
    assert tb["layout"]["title"]                                 # 'Appendix' 제목이 표 제목으로
    assert len(tb["layout"]["rows"]) <= 4
    assert not any(it.startswith(("ematical", "cal ideas", "gate ")) for it in tb["items"])


def test_tran(tran):
    d, pages = tran
    t = kinds(pages[19], "table")[0]                            # 부록 2 표 5: 4열 표
    hdr = [t["items"][i] if i is not None else "" for i in t["layout"]["header"]]
    assert hdr == ["Process component", "Level A", "Level B", "Level C"]
    col0 = [t["items"][r[0][0]] for r in t["layout"]["rows"] if r[0]]
    assert "I. Formulate question" in col0 and "IV. Interpret Results" in col0
    assert any("Teachers pose questions of interest" == t["items"][i] for r in t["layout"]["rows"] for i in r[1])
    for pn in (13, 14):                                         # 범례·가로축 이름은 그림 안
        assert not any(b["kind"] == "heading" and b["text"].strip() in ("Level A", "Level B", "Level C", "High", "Low")
                       for b in pages[pn - 1]["blocks"])
