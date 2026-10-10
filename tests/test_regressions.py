import copy
import json
from types import SimpleNamespace

import pytest
import pymupdf

import engines
import library
import pdf_core as core
from progress import fingerprint


def paragraph(text, rect=(40, 70, 390, 600)):
    return dict(kind="para", text=text, html=text, size=10, bbox=pymupdf.Rect(rect))


def page(blocks, number=1):
    return dict(mode="text", body_size=10, blocks=blocks, page_number=number)


def test_page_fragments_stay_on_their_page(monkeypatch):
    doc = pymupdf.open()
    for _ in range(2):
        doc.new_page(width=439.37, height=666.142)
    originals = [page([paragraph("The students were")]),
                 page([paragraph("confident. Another sentence.")], 2)]
    monkeypatch.setattr(core, "extract_page", lambda p: copy.deepcopy(originals[p.number]))
    result = core.extract_document(doc)
    assert result[0]["blocks"][0]["text"] == "The students were"
    assert result[1]["blocks"][0]["text"] == "confident. Another sentence."


def test_ocr_keeps_page_fragments():
    doc = pymupdf.open()
    for _ in range(2):
        doc.new_page(width=439.37, height=666.142)
    scans = [dict(mode="scan", blocks=[], page_number=i) for i in (1, 2)]
    ocr = {str(i): [dict(kind="para", text=text, bbox=[.1, .1, .9, .9])] for i, text in
           [(1, "Students were"), (2, "confident.")]}
    result = core.apply_ocr(doc, scans, ocr)
    assert result[0]["blocks"][0]["text"] == "Students were"
    assert result[1]["blocks"][0]["text"] == "confident."


@pytest.mark.parametrize("source,translated", [
    ("<i>confidence</i>", "자신감"),
    ("<i>confidence</i>", "<i>자신감"),
    ("[2P.12] reported 24 students", "[2P.13] 학생 24명"),
    ("24 students in 2010", "2010년 학생 23명"),
    ("A long sentence. " * 20, "요약"),
    ("Some text", ""),
])
def test_suspect_translation_rejected(source, translated):
    with pytest.raises(engines.EngineError, match="검토 필요"):
        engines.validate_results([source], [translated])


def test_valid_translation():
    engines.validate_results(["<i>Confidence</i> [2P.12]: 24 students, 2010"],
                             ["<i>자신감</i> [2P.12]: 2010년 학생 24명"])


def mock_openai(monkeypatch, translations):
    import openai
    replies = iter(translations)
    inputs = []
    def create(**kwargs):
        inputs.append(json.loads(kwargs["input"]))
        return SimpleNamespace(output_text=json.dumps({"translations": next(replies)}))
    monkeypatch.setattr(openai, "OpenAI", lambda **kw: SimpleNamespace(
        responses=SimpleNamespace(create=create)))
    return inputs


def test_bad_retry_is_not_accepted(monkeypatch):
    mock_openai(monkeypatch, [["학생 2명"], ["학생 3명"]])
    engine = engines.make_openai("dummy", "test", "KO", {})
    with pytest.raises(engines.EngineError, match="숫자"):
        engine.translate(["4 students"])


def test_context_is_separate_and_retry_can_succeed(monkeypatch):
    inputs = mock_openai(monkeypatch, [["학생 2명"], ["학생 4명"]])
    engine = engines.make_openai("dummy", "test", "KO", {})
    assert engine.translate_context(["4 students"], "context only") == ["학생 4명"]
    assert all(i["segments"] == ["4 students"] and i["adjacent_page_context"] == "context only" for i in inputs)


def test_same_page_count_different_translation_changes_download_fingerprint():
    old = {"1": {"tr": ["이전 번역"]}}
    new = {"1": {"tr": ["수정 번역"]}}
    assert len(old) == len(new)
    assert fingerprint(old) != fingerprint(new)


class MemoryLibrary(library.Library):
    def __init__(self):
        self.files = {}
    def read(self, path):
        return self.files.get(path)
    def write(self, path, data, message):
        self.files[path] = data


def test_partial_revision_preserves_complete_and_restores_matching_settings():
    lib = MemoryLibrary()
    meta = dict(id="paper", translated=22, total_pages=22, cache_key="name_paper_old")
    old = lib.save_paper(meta, {"translation.pdf": b"complete", "progress.json": b"old progress"})
    new = lib.save_paper({**meta, "translated": 3, "cache_key": "name_paper_new"},
                         {"translation.pdf": b"partial", "progress.json": b"new progress"})
    assert old["id"] != new["id"]
    assert lib.get_file(old["id"], "translation.pdf") == b"complete"
    assert len(lib.list_papers()) == 2
    assert lib.find_progress("paper", "renamed_paper_old") == b"old progress"
    assert lib.find_progress("paper", "renamed_paper_new") == b"new progress"
    newer = lib.save_paper({**meta, "translated": 4}, {"progress.json": b"newer progress"})
    assert lib.find_progress("paper", "renamed_paper_old") == b"newer progress"
    assert newer["id"] != old["id"]


def test_legacy_library_progress_restores():
    lib = MemoryLibrary()
    lib.files["papers/paper/progress.json"] = b"legacy"
    assert lib.find_progress("paper", "name_paper_old") == b"legacy"


def test_render_preserves_count_size_and_korean_text():
    src, out = pymupdf.open(), pymupdf.open()
    for _ in range(2):
        src.new_page(width=439.37, height=666.142)
    for i in (1, 2):
        block = paragraph("Source")
        block["tr"] = "학생의 <i>자신감</i>을 조사하였다."
        core.render_page(out, src, page([block], i))
    assert out.page_count == 2
    assert all(p.rect == src[p.number].rect for p in out)
    assert "자신감" in out[0].get_text()
    assert "<b><u>자신감</u></b>" in core.to_reading_html("<i>자신감</i>")


def test_dense_page_reports_failure_without_tiny_pdf():
    src, out = pymupdf.open(), pymupdf.open()
    src.new_page(width=439.37, height=666.142)
    block = paragraph("Source")
    block["tr"] = "학생의 수학 자신감에 관한 연구 결과입니다. " * 3000
    with pytest.raises(core.LayoutError, match="p.1"):
        core.render_page(out, src, page([block]))
    assert out.page_count == 0


def test_figure_labels_are_translation_units_and_exported():
    block = dict(kind="figure", bbox=pymupdf.Rect(40, 80, 200, 180), size=10,
                 html="", text="", items=["Confidence"], img_name="figure.png")
    pg = page([block])
    units = core.translatable_units(pg)
    assert units == [(0, 0, "Confidence")]
    core.apply_translations(pg, units, ["자신감"])
    assert "자신감" in core.page_text(pg)
    assert "Confidence → 자신감" in "".join(core._pieces(pg["blocks"]))


def test_figure_ocr_attached_to_correct_page():
    doc = pymupdf.open()
    doc.new_page()
    pg = page([dict(kind="figure", html="", text="", bbox=pymupdf.Rect(40, 80, 200, 180))])
    result = core.apply_ocr(doc, [pg], {"figure:1:0": ["Frequency"]})
    assert result[0]["blocks"][0]["items"] == ["Frequency"]
    assert "items" not in pg["blocks"][0]
