import io
import json
from pathlib import Path

import pymupdf
import streamlit as st
from streamlit.testing.v1 import AppTest

import engines


def test_translation_and_same_count_progress_import_refresh_pdf(monkeypatch, tmp_path):
    source = pymupdf.open()
    p = source.new_page(width=439.37, height=666.142)
    p.insert_textbox(pymupdf.Rect(40, 90, 395, 550),
                     "The students were confident about learning mathematics. " * 6, fontsize=10)
    uploaded = io.BytesIO(source.tobytes())
    uploaded.name = "regression.pdf"
    progress_upload = [None]
    monkeypatch.setattr(st, "file_uploader", lambda label, *a, **kw:
                        progress_upload[0] if kw.get("key") == "cache_up" else uploaded)
    monkeypatch.setattr(engines, "make_openai", lambda *args: engines.Engine(
        "Mock", lambda: "Mock API", lambda texts: ["학생들은 수학 학습에 자신감을 보였다. " * 6 for _ in texts]))
    app = Path(__file__).resolve().parents[1] / "app.py"
    monkeypatch.chdir(tmp_path)
    at = AppTest.from_file(str(app)).run()
    assert not at.exception
    key_input = next(w for w in at.text_input if w.label == "OpenAI API Key")
    key_input.set_value("dummy").run()
    next(w for w in at.button if w.label == "번역 시작").click().run()
    assert not at.exception
    original_pdf = at.session_state["built"][1]["pdf"]
    cache = json.loads(json.dumps(at.session_state["cache"]))
    assert len(cache) == 1
    cache["1"]["tr"] = ["학생들은 수학 학습에서 서로 협력하였다. " * 6 for _ in cache["1"]["tr"]]
    progress_upload[0] = io.BytesIO(json.dumps({"_key": at.session_state["cache_key"], **cache}).encode())
    at.run()
    assert not at.exception
    corrected_pdf = at.session_state["built"][1]["pdf"]
    assert corrected_pdf != original_pdf
    pdf = pymupdf.open(stream=corrected_pdf, filetype="pdf")
    assert pdf.page_count == 1
    assert "협력" in pdf[0].get_text()
