"""
논문 정독용 PDF 번역기 (Streamlit + DeepL/OpenAI + PyMuPDF)

실행:  streamlit run app.py
구성:  app.py      화면, 캐시, 다운로드
       engines.py  번역 엔진 (DeepL / OpenAI)
       pdf_core.py PDF 추출·읽기 순서·문단 복원·그림/표·참고문헌·번역 PDF 조판
       fonts/      번역 PDF에 넣을 한글 글꼴 (나눔고딕, OFL)

번역 PDF는 원문과 같은 판형으로, 원문 1쪽 = 번역 1쪽이 되도록 만든다.
(내용이 넘치면 글자·줄간격을 줄이고, 그래도 넘치면 '(계속)' 페이지를 붙인다)
"""
from __future__ import annotations

import copy
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import pymupdf
import streamlit as st

import engines
import pdf_core as core

# ─────────────────────────── 설정 ───────────────────────────
CACHE_DIR = Path(".translation_cache")
TARGETS = {"한국어 (KO)": "KO", "영어 (EN-US)": "EN-US"}
DEFAULT_GLOSSARY = "self-efficacy = 자기효능감\nmathematical confidence = 수학 자신감\nstatistical thinking = 통계적 사고"

st.set_page_config(page_title="논문 번역기", page_icon="📄", layout="wide")


def secret(name: str) -> str:
    """st.secrets 값 (secrets.toml이 없어도 오류 없이 빈 문자열)."""
    try:
        return str(st.secrets.get(name, ""))
    except Exception:
        return ""


# ─────────────────────── 접근 비밀번호 (선택) ───────────────────────
# Streamlit Cloud의 Secrets에 APP_PASSWORD를 넣으면 비밀번호를 아는 사람만 쓸 수 있다.
# DEEPL_API_KEY / OPENAI_API_KEY를 Secrets에 넣는 경우에는 꼭 설정할 것 (안 그러면 남이 내 한도를 쓴다).
if secret("APP_PASSWORD") and not st.session_state.get("authed"):
    st.title("📄 논문 번역기")
    pw = st.text_input("비밀번호", type="password")
    if pw and pw == secret("APP_PASSWORD"):
        st.session_state.authed = True
        st.rerun()
    elif pw:
        st.error("비밀번호가 다릅니다.")
    st.stop()


# ─────────────────────────── 사이드바 ───────────────────────────
with st.sidebar:
    st.header("번역 엔진")
    engine_name = st.radio("엔진", ["DeepL", "OpenAI"], horizontal=True, label_visibility="collapsed")
    if engine_name == "DeepL":
        api_key = st.text_input("DeepL API Key", value=secret("DEEPL_API_KEY"), type="password",
                                help="키는 저장되지 않습니다. SDK가 키 종류에 맞는 서버로 연결합니다.")
        model = ""
    else:
        api_key = st.text_input("OpenAI API Key", value=secret("OPENAI_API_KEY"), type="password",
                                help="platform.openai.com → API keys. 키는 저장되지 않습니다.")
        model = st.selectbox("모델", list(engines.OPENAI_MODELS),
                             format_func=lambda m: f"{m} ({engines.OPENAI_MODELS[m][0]})")
    target_label = st.selectbox("도착 언어", list(TARGETS))
    target = TARGETS[target_label]

    st.header("번역 설정")
    translate_captions = st.checkbox("그림·표 캡션 번역", value=True)
    translate_refs = st.checkbox("참고문헌 번역", value=False,
                                 help="끄면 참고문헌은 원문 그대로 둡니다 (저자명·제목 검색에 유리).")
    interleave = st.checkbox("원문 페이지 함께 넣기", value=False,
                             help="원문 1쪽 → 번역 1쪽 순서로 번갈아 넣습니다.")

    st.header("용어집")
    glossary_text = st.text_area("영어 = 한국어 (한 줄에 하나)", value=DEFAULT_GLOSSARY, height=120,
                                 help="같은 용어를 논문 전체에서 같은 번역어로 맞춥니다. 한국어 번역에만 적용됩니다.")


def parse_glossary(text: str) -> dict[str, str]:
    entries: dict[str, str] = {}
    for line in text.splitlines():
        if "=" in line:
            en, ko = (x.strip() for x in line.split("=", 1))
            if en and ko:
                entries[en] = ko
    return entries


glossary_entries = parse_glossary(glossary_text) if target == "KO" else {}


# ─────────────────────────── 캐시 ───────────────────────────
def cache_path(key: str) -> Path:
    CACHE_DIR.mkdir(exist_ok=True)
    return CACHE_DIR / f"{key}.json"


def load_cache(key: str) -> dict[str, Any]:
    p = cache_path(key)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_cache(key: str, data: dict[str, Any]) -> None:
    try:
        cache_path(key).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass   # 디스크 캐시 실패해도 session_state에는 남아 있다


# ─────────────────────────── 메인 화면 ───────────────────────────
st.title("📄 논문 정독용 번역기")
st.caption("원문 1쪽 = 번역 1쪽. 그림·표는 원문 그대로, 이탤릭은 볼드로 표시됩니다.")

uploaded = st.file_uploader("논문 PDF를 끌어다 놓으세요", type=["pdf"])
if not uploaded:
    st.stop()

pdf_bytes = uploaded.getvalue()
pdf_hash = hashlib.sha256(pdf_bytes).hexdigest()
try:
    src = pymupdf.open(stream=pdf_bytes, filetype="pdf")
except Exception as e:
    st.error(f"PDF를 열 수 없습니다: {e}")
    st.stop()

if st.session_state.get("pdf_hash") != pdf_hash:
    with st.spinner("PDF 구조 분석 중…"):
        st.session_state.pages = core.extract_document(src)
    st.session_state.pdf_hash = pdf_hash
    st.session_state.pop("built", None)
pages: list[dict] = st.session_state.pages

n_pages = src.page_count
text_chars = sum(len(b["text"]) for pg in pages for b in pg["blocks"])
c1, c2, c3 = st.columns(3)
c1.metric("파일", uploaded.name[:28] + ("…" if len(uploaded.name) > 28 else ""))
c2.metric("전체 페이지", n_pages)
c3.metric("추출된 글자 수", f"{text_chars:,}")

if text_chars < 50 * n_pages:
    st.error("텍스트가 거의 없습니다. 스캔된 PDF로 보입니다. 이 버전은 OCR을 지원하지 않습니다.")
    st.stop()
bad = [pg["page_number"] for pg in pages if pg["mode"] == "error"]
if bad:
    st.warning(f"텍스트 추출에 실패한 페이지: {bad} — 이 페이지들은 원문 그대로 들어갑니다.")

start, end = st.slider("번역할 페이지 범위", 1, n_pages, (1, n_pages))
sel = [pg for pg in pages if start <= pg["page_number"] <= end]

opts_sig = hashlib.md5(json.dumps([engine_name, model, target, translate_refs, translate_captions, glossary_entries],
                                  sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:8]
cache_key = f"{Path(uploaded.name).stem[:40]}_{pdf_hash[:16]}_{opts_sig}"
if st.session_state.get("cache_key") != cache_key:
    st.session_state.cache_key = cache_key
    st.session_state.cache = load_cache(cache_key)
    st.session_state.pop("built", None)
cache: dict[str, Any] = st.session_state.cache


def units_of(pg: dict) -> list[tuple[int, int | None, str]]:
    return core.translatable_units(pg, translate_refs, translate_captions)


todo = [pg for pg in sel if pg["mode"] == "text" and str(pg["page_number"]) not in cache]
need_chars = sum(len(core.plain(h)) for pg in todo for *_, h in units_of(pg))
done_in_range = sum(1 for pg in sel if str(pg["page_number"]) in cache)
if engine_name == "OpenAI":
    krw = engines.openai_cost_krw(model, need_chars)
    cost = f" · 예상 비용 약 {krw:,}원" if krw is not None else ""
else:
    cost = " (DeepL 남은 한도는 번역 시작 시 확인해 보여 줍니다)"
st.info(f"선택 범위 {len(sel)}쪽 중 {done_in_range}쪽 번역 완료 · 남은 원문 약 {need_chars:,}자{cost}")

with st.expander("이전 진행 상황 불러오기 / 저장하기"):
    st.caption("앱이 재시작되면 서버의 캐시가 사라질 수 있습니다. 진행 파일을 받아 두었다가 올리면 이어서 번역합니다.")
    up = st.file_uploader("진행 파일(.json)", type=["json"], key="cache_up")
    if up is not None:
        try:
            data = json.loads(up.getvalue().decode("utf-8"))
            if data.get("_key") == cache_key:
                cache.update({k: v for k, v in data.items() if not k.startswith("_")})
                save_cache(cache_key, cache)
                st.success("불러왔습니다.")
            else:
                st.warning("이 PDF·설정과 맞지 않는 진행 파일입니다 (엔진·모델, 도착 언어, 참고문헌·캡션 옵션, 용어집이 같아야 합니다).")
        except Exception as e:
            st.error(f"진행 파일을 읽지 못했습니다: {e}")
    st.download_button("현재 진행 파일 받기", json.dumps({"_key": cache_key, **cache}, ensure_ascii=False),
                       file_name=f"{cache_key}.json", mime="application/json")

# ─────────────────────────── 번역 실행 ───────────────────────────
def make_engine() -> engines.Engine:
    if engine_name == "DeepL":
        return engines.make_deepl(api_key, target, glossary_entries,
                                  st.session_state.setdefault("deepl_glossaries", {}))
    return engines.make_openai(api_key, model, target, glossary_entries)


def translate_page(engine: engines.Engine, pg: dict) -> list[str]:
    units = units_of(pg)
    return engine.translate([h for *_, h in units]) if units else []


if st.button("번역 시작", type="primary", disabled=not todo):
    if not api_key:
        st.error(f"사이드바에 {engine_name} API Key를 입력하세요.")
        st.stop()
    try:
        engine = make_engine()
        note = engine.check()
    except engines.EngineError as e:
        st.error(str(e))
        st.stop()
    except Exception as e:
        st.error(f"번역 엔진을 준비하지 못했습니다: {e}")
        st.stop()
    if note:
        st.caption(note)

    bar = st.progress(0.0)
    status = st.empty()
    failures: dict[int, str] = {}
    fatal: str | None = None
    st.caption("번역 중에는 화면의 다른 버튼을 누르지 마세요(새로 실행되며 멈춥니다). 멈춰도 끝난 페이지는 저장됩니다.")

    # 페이지 단위로 번역하고, 끝나는 대로 저장한다. OpenAI는 여러 페이지를 동시에 보낸다.
    done = 0
    with ThreadPoolExecutor(max_workers=engine.workers) as pool:
        futures = {pool.submit(translate_page, engine, pg): pg for pg in todo}
        status.write(f"번역 중… 0 / {len(todo)}쪽")
        for fut in as_completed(futures):
            pg = futures[fut]
            pno = pg["page_number"]
            try:
                tr = fut.result()
                cache[str(pno)] = {"tr": tr, "original": core.page_text(pg, translated=False)}
                save_cache(cache_key, cache)
            except engines.EngineError as e:
                failures[pno] = str(e)
                if e.fatal and fatal is None:
                    fatal = str(e)
                    for f in futures:            # 아직 시작 안 한 페이지는 취소
                        f.cancel()
            except Exception as e:
                failures[pno] = f"번역 실패: {e}"
            done += 1
            bar.progress(done / len(todo))
            status.write(f"번역 중… {done} / {len(todo)}쪽 (방금 끝난 페이지: {pno})")

    status.write(f"번역 완료 페이지: {sum(1 for pg in sel if str(pg['page_number']) in cache)} / {len(sel)}")
    if fatal:
        st.error(f"{fatal} — 번역을 멈췄습니다. 지금까지 번역한 페이지는 저장되어 있습니다.")
    for pno, msg in sorted(failures.items()):
        if msg != fatal:
            st.error(f"Page {pno} 번역 실패 — {msg}")
    if failures:
        st.info("'번역 시작'을 다시 누르면 남은 페이지부터 이어서 번역합니다.")
    st.session_state.pop("built", None)


# ─────────────────────────── 결과 만들기 ───────────────────────────
def translated_page(pg: dict) -> dict | None:
    """캐시의 번역을 입힌 페이지 사본. 번역이 없으면 None."""
    entry = cache.get(str(pg["page_number"]))
    if pg["mode"] != "text" or entry is None:
        return None
    tpg = copy.deepcopy(pg)
    core.apply_translations(tpg, units_of(tpg), entry["tr"])
    return tpg


def build_outputs() -> dict[str, bytes]:
    out = pymupdf.open()
    txt: list[str] = []
    md: list[str] = [f"# {Path(uploaded.name).stem}\n"]
    for pg in sel:
        pno = pg["page_number"]
        tpg = translated_page(pg)
        if interleave:
            out.insert_pdf(src, from_page=pno - 1, to_page=pno - 1)
        if tpg is not None:
            core.render_page(out, src, tpg)
        elif pg["mode"] == "text":
            core.render_page(out, src, pg, note="번역되지 않음 — 원문 그대로")
        else:
            core.render_page(out, src, pg)
        body = core.page_text(tpg) if tpg else "(번역 없음 — 원문 참조)"
        orig = core.page_text(pg, translated=False)
        txt.append(f"──────────── Page {pno} ────────────\n\n{body}\n")
        md.append(f"# Page {pno}\n\n## Translation\n\n{body}\n\n## Original\n\n{orig}\n")
    out.subset_fonts()
    pdf = out.tobytes(garbage=4, deflate=True)
    return {"pdf": pdf, "txt": "\n".join(txt).encode("utf-8"), "md": "\n".join(md).encode("utf-8")}


translated_count = sum(1 for pg in sel if str(pg["page_number"]) in cache)
if translated_count:
    build_sig = (cache_key, start, end, interleave, len(cache))
    if st.session_state.get("built", (None,))[0] != build_sig:
        with st.spinner("번역 PDF 만드는 중…"):
            st.session_state.built = (build_sig, build_outputs())
    files = st.session_state.built[1]

    st.subheader("다운로드")
    stem = f"{Path(uploaded.name).stem[:60]}_번역_p{start}-{end}"
    d1, d2, d3 = st.columns(3)
    d1.download_button("📕 번역 PDF", files["pdf"], file_name=f"{stem}.pdf", mime="application/pdf",
                       type="primary")
    d2.download_button("TXT", files["txt"], file_name=f"{stem}.txt", mime="text/plain")
    d3.download_button("Markdown", files["md"], file_name=f"{stem}.md", mime="text/markdown")

    # ── 미리보기: 원문 페이지와 번역 페이지를 나란히 ──
    st.subheader("미리보기")
    view_p = st.number_input("페이지", min_value=start, max_value=end, value=start, step=1)
    pg = pages[view_p - 1]
    left, right = st.columns(2)
    with left:
        st.caption(f"원문 p.{view_p}")
        st.image(src[view_p - 1].get_pixmap(dpi=110).tobytes("png"), width="stretch")
    with right:
        st.caption(f"번역 p.{view_p}")
        tpg = translated_page(pg)
        if tpg is None:
            st.info("이 페이지는 아직 번역되지 않았습니다." if pg["mode"] == "text" else "원문 그대로 들어가는 페이지입니다.")
        else:
            one = pymupdf.open()
            core.render_page(one, src, tpg)
            for p in one:
                st.image(p.get_pixmap(dpi=110).tobytes("png"), width="stretch")
    with st.expander("이 페이지 텍스트로 보기"):
        if tpg is not None:
            st.markdown("**[Translation]**")
            st.write(core.page_text(tpg))
        st.markdown("**[Original]**")
        st.write(core.page_text(pg, translated=False))
