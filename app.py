"""
논문 정독용 PDF 번역기 (Streamlit + DeepL/OpenAI + PyMuPDF)

실행:  streamlit run app.py
구성:  app.py      화면, 캐시, 다운로드
       engines.py  번역 엔진 (DeepL / OpenAI)
       library.py  '내 서재' — 번역본을 GitHub 비공개 저장소에 보관
       pdf_core.py PDF 추출·읽기 순서·문단 복원·그림/표·참고문헌·번역 PDF 조판
       fonts/      번역 PDF에 넣을 한글 글꼴 (Noto Sans CJK KR, OFL)

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
import library
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


# ─────────────────────────── 내 서재 ───────────────────────────
def get_library() -> library.Library | None:
    """Secrets에 LIBRARY_REPO와 GITHUB_TOKEN이 있으면 서재를 쓴다."""
    repo, token = secret("LIBRARY_REPO"), secret("GITHUB_TOKEN")
    return library.Library(repo, token) if repo and token else None


lib = get_library()


def same_paper_and_settings(key_a: str, key_b: str) -> bool:
    """진행 파일 키 비교: 파일 이름은 달라도 PDF 해시와 번역 설정이 같으면 같은 번역."""
    return key_a.split("_")[-2:] == key_b.split("_")[-2:]


def render_library() -> None:
    st.title("📚 내 서재")
    if lib is None:
        st.info("서재가 아직 연결되지 않았습니다. Streamlit Secrets에 아래 두 줄을 넣으면 번역한 논문이 "
                "GitHub 비공개 저장소에 자동으로 보관됩니다.")
        st.code('LIBRARY_REPO = "ikim111/snu_translate_library"\nGITHUB_TOKEN = "github_pat_..."', language="toml")
        return
    c1, c2 = st.columns([4, 1])
    c1.caption(f"보관 위치: GitHub `{lib.repo}` (비공개)")
    if c2.button("새로고침") or "lib_items" not in st.session_state:
        try:
            lib.check()
            st.session_state.lib_items = lib.list_papers()
        except library.LibraryError as e:
            st.error(str(e))
            return
    items: list[dict] = st.session_state.lib_items
    if not items:
        st.write("아직 저장된 논문이 없습니다. 번역을 마치면 자동으로 여기에 저장됩니다.")
        return

    query = st.text_input("검색", placeholder="제목이나 파일 이름 일부")
    if query:
        q = query.lower()
        items = [m for m in items if q in m.get("title", "").lower() or q in m.get("filename", "").lower()]
    st.caption(f"{len(items)}편")

    for m in items:
        pid = m["id"]
        with st.container(border=True):
            st.markdown(f"**{m.get('title') or m.get('filename')}**")
            st.caption(f"{m.get('updated', '')} · {m.get('engine', '')} · "
                       f"번역 {m.get('translated', '?')}/{m.get('total_pages', '?')}쪽 "
                       f"(p.{m.get('range', ['?', '?'])[0]}–{m.get('range', ['?', '?'])[1]}) · {m.get('filename', '')}")
            b1, b2, b3 = st.columns([2, 2, 1])
            for col, name, label, mime in ((b1, "translation.pdf", "📕 번역 PDF", "application/pdf"),
                                           (b2, "original.pdf", "📄 원문 PDF", "application/pdf")):
                key = f"dl_{pid}_{name}"
                if key in st.session_state:
                    stem = Path(m.get("filename", pid)).stem[:60]
                    suffix = "_번역" if name == "translation.pdf" else ""
                    col.download_button(f"{label} 저장", st.session_state[key],
                                        file_name=f"{stem}{suffix}.pdf", mime=mime, key=f"{key}_btn")
                elif col.button(f"{label} 불러오기", key=f"{key}_get"):
                    try:
                        data = lib.get_file(pid, name)
                    except library.LibraryError as e:
                        col.error(str(e))
                        data = None
                    if data:
                        st.session_state[key] = data
                        st.rerun()
                    elif data is None:
                        col.warning("파일이 없습니다.")
            with b3.popover("삭제"):
                st.write("서재에서 이 논문을 지웁니다. (GitHub 기록에는 남습니다)")
                if st.button("삭제", key=f"del_{pid}", type="primary"):
                    try:
                        lib.delete_paper(pid)
                        st.session_state.lib_items = [x for x in st.session_state.lib_items if x["id"] != pid]
                        st.rerun()
                    except library.LibraryError as e:
                        st.error(str(e))


# ─────────────────────────── 사이드바 ───────────────────────────
with st.sidebar:
    menu = st.radio("메뉴", ["번역하기", "📚 내 서재"], horizontal=True, label_visibility="collapsed")
if menu == "📚 내 서재":
    render_library()
    st.stop()

with st.sidebar:
    st.header("번역 엔진")
    engine_name = st.radio("엔진", ["OpenAI", "DeepL"], horizontal=True, label_visibility="collapsed",
                           help="비교해 보니 학술 용어·문체는 OpenAI가 더 나았습니다.")
    if engine_name == "DeepL":
        api_key = st.text_input("DeepL API Key", value=secret("DEEPL_API_KEY"), type="password",
                                help="키는 저장되지 않습니다. SDK가 키 종류에 맞는 서버로 연결합니다.")
        model = ""
    else:
        api_key = st.text_input("OpenAI API Key", value=secret("OPENAI_API_KEY"), type="password",
                                help="platform.openai.com → API keys. 키는 저장되지 않습니다.")
        models = list(engines.OPENAI_MODELS)
        default_model = getattr(engines, "DEFAULT_OPENAI_MODEL", models[0])
        model = st.selectbox("모델", models, index=models.index(default_model) if default_model in models else 0,
                             format_func=lambda m: f"{m} ({engines.OPENAI_MODELS[m][0]})")
    target_label = st.selectbox("도착 언어", list(TARGETS))
    target = TARGETS[target_label]

    st.header("번역 설정")
    translate_captions = st.checkbox("그림·표 캡션 번역", value=True)
    translate_refs = st.checkbox("참고문헌 번역", value=False,
                                 help="끄면 참고문헌은 원문 그대로 둡니다 (저자명·제목 검색에 유리).")
    interleave = st.checkbox("원문 페이지 함께 넣기", value=False,
                             help="원문 1쪽 → 번역 1쪽 순서로 번갈아 넣습니다.")

def parse_glossary(text: str) -> dict[str, str]:
    entries: dict[str, str] = {}
    for line in text.splitlines():
        if "=" in line:
            en, ko = (x.strip() for x in line.split("=", 1))
            if en and ko:
                entries[en] = ko
    return entries


def glossary_to_text(entries: dict[str, str]) -> str:
    return "\n".join(f"{k} = {v}" for k, v in entries.items())


# 용어집: 서재에 저장된 것이 있으면 그걸로 시작 (없으면 기본값)
if "glossary_text" not in st.session_state:
    saved = None
    if lib is not None:
        try:
            saved = lib.get_glossary()
        except library.LibraryError:
            saved = None
    st.session_state.glossary_text = glossary_to_text(saved) if saved else DEFAULT_GLOSSARY
if "glossary_pending" in st.session_state:        # 추천 용어를 확정했을 때 (위젯 그리기 전에 반영)
    st.session_state.glossary_text = st.session_state.pop("glossary_pending")

with st.sidebar:
    st.header("용어집")
    glossary_text = st.text_area("영어 = 한국어 (한 줄에 하나)", key="glossary_text", height=160,
                                 help="같은 용어를 논문 전체에서 같은 번역어로 맞춥니다. 한국어 번역에만 적용됩니다. "
                                      "용어집을 바꾸면 같은 논문도 다시 번역합니다.")
    if lib is not None and st.button("💾 용어집 서재에 저장", width="stretch"):
        try:
            lib.save_glossary(parse_glossary(glossary_text))
            st.success("저장했습니다. 다음에 앱을 열 때도 이 용어집으로 시작합니다.")
        except library.LibraryError as e:
            st.error(str(e))


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

paper_id = pdf_hash[:16]
if st.session_state.get("pdf_hash") != pdf_hash:
    with st.spinner("PDF 구조 분석 중… (스캔본은 쪽 이미지를 다시 읽느라 쪽당 몇 초 걸립니다)"):
        prog = st.progress(0.0)
        st.session_state.base_pages = core.extract_document(
            src, progress=lambda i, n: prog.progress(i / n, text=f"원문 읽는 중… {i} / {n}쪽"))
        prog.empty()
    st.session_state.pdf_hash = pdf_hash
    st.session_state.pop("built", None)
    st.session_state.pop("pages_ocr_sig", None)
    # 스캔 쪽 글자 읽기(OCR) 결과: 서버 캐시 → 서재 순으로 찾는다
    ocr: dict[str, Any] = load_cache(f"ocr_{paper_id}")
    if not ocr and lib is not None and any(pg["mode"] == "scan" for pg in st.session_state.base_pages):
        try:
            raw = lib.get_file(paper_id, "ocr.json")
            if raw:
                ocr = json.loads(raw.decode("utf-8"))
                save_cache(f"ocr_{paper_id}", ocr)
        except (library.LibraryError, ValueError):
            pass
    st.session_state.ocr = ocr
ocr_data: dict[str, Any] = st.session_state.ocr
base_pages: list[dict] = st.session_state.base_pages
ocr_sig = (pdf_hash, tuple(sorted(ocr_data)))
if st.session_state.get("pages_ocr_sig") != ocr_sig:
    st.session_state.pages = core.apply_ocr(src, base_pages, ocr_data) if ocr_data else base_pages
    st.session_state.pages_ocr_sig = ocr_sig
pages: list[dict] = st.session_state.pages

n_pages = src.page_count
text_chars = sum(len(b["text"]) for pg in pages for b in pg["blocks"])
scan_pages = [pg for pg in pages if pg["mode"] == "scan"]
c1, c2, c3 = st.columns(3)
c1.metric("파일", uploaded.name[:28] + ("…" if len(uploaded.name) > 28 else ""))
c2.metric("전체 페이지", n_pages)
c3.metric("추출된 글자 수", f"{text_chars:,}")

if text_chars < 50 * n_pages and not scan_pages and not ocr_data:
    st.error("텍스트가 거의 없습니다. 글자를 읽을 수 없는 PDF입니다.")
    st.stop()

# ─────────────────────────── 스캔 쪽 글자 읽기 (OCR) ───────────────────────────
if scan_pages:
    ocr_key = api_key if engine_name == "OpenAI" else secret("OPENAI_API_KEY")
    ocr_model = model if engine_name == "OpenAI" else getattr(engines, "DEFAULT_OPENAI_MODEL",
                                                               list(engines.OPENAI_MODELS)[0])
    krw = engines.ocr_cost_krw(ocr_model, len(scan_pages))
    st.warning(f"📷 스캔된 쪽이 {len(scan_pages)}개 있습니다. 글자가 사진으로 되어 있어 먼저 **글자 읽기(OCR)**를 해야 "
               f"번역할 수 있습니다. OpenAI({ocr_model})가 쪽 이미지를 읽으며"
               + (f" 예상 비용은 약 {krw:,}원입니다." if krw is not None else ".")
               + " 읽은 결과는 저장되어 다시 비용이 들지 않습니다.")
    if not ocr_key:
        st.caption("OpenAI API Key가 필요합니다.")
    if st.button(f"📷 스캔 쪽 {len(scan_pages)}개 글자 읽기", type="primary", disabled=not ocr_key):
        # PyMuPDF는 여러 스레드에서 쓰면 안 되므로 이미지는 먼저 만들어 둔다
        pngs = {pg["page_number"]: src[pg["page_number"] - 1].get_pixmap(dpi=engines.OCR_DPI).tobytes("png")
                for pg in scan_pages}
        bar = st.progress(0.0)
        status = st.empty()
        fails: dict[int, str] = {}
        done = 0
        with ThreadPoolExecutor(max_workers=4) as pool:
            futs = {pool.submit(engines.ocr_page, ocr_key, ocr_model, png): pno for pno, png in pngs.items()}
            for fut in as_completed(futs):
                pno = futs[fut]
                try:
                    ocr_data[str(pno)] = fut.result()
                    save_cache(f"ocr_{paper_id}", ocr_data)
                except engines.EngineError as e:
                    fails[pno] = str(e)
                    if e.fatal:
                        for f in futs:
                            f.cancel()
                done += 1
                bar.progress(done / len(pngs))
                status.write(f"글자 읽는 중… {done} / {len(pngs)}쪽")
        if lib is not None and ocr_data:
            try:
                lib.check()
                lib.write(f"papers/{paper_id}/ocr.json",
                          json.dumps(ocr_data, ensure_ascii=False).encode("utf-8"), "스캔 쪽 글자 읽기 결과")
            except library.LibraryError as e:
                st.error(f"글자 읽기 결과를 서재에 저장하지 못했습니다: {e}")
        for pno, msg in sorted(fails.items()):
            st.error(f"Page {pno} 글자 읽기 실패 — {msg}")
        if not fails:
            st.rerun()
elif ocr_data:
    st.caption("📷 스캔 쪽은 저장된 글자 읽기(OCR) 결과로 번역합니다.")
bad = [pg["page_number"] for pg in pages if pg["mode"] == "error"]
if bad:
    st.warning(f"텍스트 추출에 실패한 페이지: {bad} — 이 페이지들은 원문 그대로 들어갑니다.")

start, end = st.slider("번역할 페이지 범위", 1, n_pages, (1, n_pages))
sel = [pg for pg in pages if start <= pg["page_number"] <= end]

# 번역 지시문 버전은 캐시 키에 넣지 않는다: 지시문이 바뀌어도 이미 번역한 쪽은 그대로 두고,
# 고치고 싶은 쪽만 '쪽별로 다시 번역'으로 새 지시문을 적용한다 (번역비 절약)
opts_sig = hashlib.md5(json.dumps([engine_name, model, target, translate_refs, translate_captions,
                                   glossary_entries],
                                  sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:8]
cache_key = f"{Path(uploaded.name).stem[:40]}_{pdf_hash[:16]}_{opts_sig}"
if st.session_state.get("cache_key") != cache_key:
    st.session_state.cache_key = cache_key
    st.session_state.cache = load_cache(cache_key)
    st.session_state.pop("built", None)
    st.session_state.lib_meta = None
    # 서버 캐시가 비었으면(앱이 잠들었다 깨어난 경우 등) 서재에 보관된 번역을 불러온다 → 번역비 없음
    if lib is not None:
        try:
            raw = lib.get_file(paper_id, "progress.json")
            if raw:
                data = json.loads(raw.decode("utf-8"))
                if same_paper_and_settings(data.get("_key", ""), cache_key):
                    if not st.session_state.cache:
                        st.session_state.cache = {k: v for k, v in data.items() if not k.startswith("_")}
                        save_cache(cache_key, st.session_state.cache)
                        st.toast("서재에 보관된 번역을 불러왔습니다.")
                else:
                    meta_raw = lib.get_file(paper_id, "meta.json")
                    st.session_state.lib_meta = json.loads(meta_raw) if meta_raw else {}
        except (library.LibraryError, ValueError):
            pass
cache: dict[str, Any] = st.session_state.cache
if st.session_state.get("lib_meta") is not None:
    m = st.session_state.lib_meta
    st.info(f"이 논문은 서재에 다른 설정({m.get('engine', '?')}, {m.get('updated', '?')})으로 번역한 기록이 있습니다. "
            "그 번역본을 그대로 받으려면 사이드바의 **📚 내 서재**로 가세요. 지금 설정으로 번역하면 서재의 기록이 새 번역으로 바뀝니다.")


def units_of(pg: dict) -> list[tuple[int, int | None, str]]:
    return core.translatable_units(pg, translate_refs, translate_captions)


def units_sig(pg: dict) -> str:
    return hashlib.md5("\x1f".join(h for *_, h in units_of(pg)).encode()).hexdigest()[:12]


def cached_ok(pg: dict) -> bool:
    """이 페이지의 번역이 캐시에 있고, 지금 추출한 원문 조각과 짝이 맞는지."""
    e = cache.get(str(pg["page_number"]))
    return bool(e) and e.get("sig") == units_sig(pg) and len(e.get("tr", [])) == len(units_of(pg))


# ── 이전 번역본(기준 PDF): 다시 번역하지 않는 쪽은 여기서 그대로 가져와 합친다 ──
if st.session_state.get("base_for") != pdf_hash:
    st.session_state.base_for = pdf_hash
    st.session_state.base_pdf = None
    st.session_state.base_src = ""
    if lib is not None:
        try:
            raw = lib.get_file(paper_id, "translation.pdf")
            if raw:
                st.session_state.base_pdf = raw
                st.session_state.base_src = "서재"
        except library.LibraryError:
            pass
base_doc = None
base_map: dict[int, list[int]] = {}
if st.session_state.get("base_pdf"):
    try:
        base_doc = pymupdf.open(stream=st.session_state.base_pdf, filetype="pdf")
        base_map = core.map_translated_pages(base_doc, n_pages)
    except Exception:
        base_doc, base_map = None, {}


def has_translation(pg: dict) -> bool:
    return cached_ok(pg) or pg["page_number"] in base_map


todo = [pg for pg in sel if pg["mode"] == "text" and not has_translation(pg)]
need_chars = sum(len(core.plain(h)) for pg in todo for *_, h in units_of(pg))
done_in_range = sum(1 for pg in sel if has_translation(pg))
if engine_name == "OpenAI":
    krw = engines.openai_cost_krw(model, need_chars)
    n_figs = sum(len(core.figure_jobs(pg)) for pg in todo)
    krw_f = engines.figure_cost_krw(model, n_figs) if translate_captions else 0
    cost = (f" · 예상 비용 약 {krw + (krw_f or 0):,}원" + (f" (그림 {n_figs}개 글자 읽기 포함)" if n_figs and krw_f else "")
            if krw is not None else "")
else:
    cost = " (DeepL 남은 한도는 번역 시작 시 확인해 보여 줍니다)"
st.info(f"선택 범위 {len(sel)}쪽 중 {done_in_range}쪽 번역 완료 · 남은 원문 약 {need_chars:,}자{cost}")

# ─────────────────────────── 용어 추천 ───────────────────────────
def paper_text_for_terms() -> str:
    """참고문헌을 뺀 본문 텍스트 (용어 추천용)."""
    parts = []
    for pg in pages:
        for b in pg["blocks"]:
            if b["kind"] in ("para", "heading", "caption", "list", "note", "table", "footnote"):
                parts.append(b["text"])
    return "\n".join(parts)


with st.expander("📖 이 논문의 용어 추천받기", expanded=False):
    st.caption("OpenAI가 이 논문에서 반복되는 핵심 용어와 번역어를 제안합니다. 확인·수정 후 확정하면 용어집에 추가되고"
               + (" 서재에 저장되어 다음 논문에도 쓰입니다." if lib is not None else "니다.")
               + " 번역 전에 해 두세요.")
    openai_key = api_key if engine_name == "OpenAI" else secret("OPENAI_API_KEY")
    term_model = model if engine_name == "OpenAI" else engines.DEFAULT_OPENAI_MODEL \
        if hasattr(engines, "DEFAULT_OPENAI_MODEL") else list(engines.OPENAI_MODELS)[0]
    if st.button("용어 추천받기", disabled=not openai_key):
        with st.spinner("논문을 읽고 용어를 고르는 중… (30초~1분)"):
            try:
                text = paper_text_for_terms()
                sugg = engines.suggest_terms(openai_key, term_model, text, parse_glossary(glossary_text))
                low = text.lower()
                for t in sugg:
                    t["count"] = low.count(t["en"].lower())
                sugg = [t for t in sugg if t["count"] >= 2] or sugg
                st.session_state.term_suggestions = (pdf_hash, sorted(sugg, key=lambda t: -t["count"]))
            except engines.EngineError as e:
                st.error(str(e))
    if not openai_key:
        st.caption("OpenAI API Key가 필요합니다.")

    sug = st.session_state.get("term_suggestions")
    if sug and sug[0] == pdf_hash:
        import pandas as pd
        df = pd.DataFrame([{"추가": True, "영어": t["en"], "한국어": t["ko"], "등장": t["count"], "설명": t["why"]}
                           for t in sug[1]])
        edited = st.data_editor(
            df, hide_index=True, width="stretch", key=f"terms_{pdf_hash[:8]}",
            column_config={
                "추가": st.column_config.CheckboxColumn(width="small"),
                "영어": st.column_config.TextColumn(disabled=True),
                "한국어": st.column_config.TextColumn(help="눌러서 고칠 수 있습니다"),
                "등장": st.column_config.NumberColumn(disabled=True, width="small"),
                "설명": st.column_config.TextColumn(disabled=True),
            })
        if st.button("✅ 선택한 용어 확정", type="primary"):
            merged = parse_glossary(glossary_text)
            for _, row in edited.iterrows():
                if row["추가"] and str(row["한국어"]).strip():
                    merged[str(row["영어"]).strip()] = str(row["한국어"]).strip()
            st.session_state.glossary_pending = glossary_to_text(merged)
            st.session_state.pop("term_suggestions", None)
            if lib is not None:
                try:
                    lib.save_glossary(merged)
                except library.LibraryError as e:
                    st.error(f"용어집을 서재에 저장하지 못했습니다: {e}")
            st.rerun()

with st.expander("이전 진행 상황 불러오기 / 저장하기"):
    st.caption("앱이 재시작되면 서버의 캐시가 사라질 수 있습니다. 진행 파일을 받아 두었다가 올리면 이어서 번역합니다.")
    up = st.file_uploader("진행 파일(.json)", type=["json"], key="cache_up")
    if up is not None:
        try:
            data = json.loads(up.getvalue().decode("utf-8"))
            if same_paper_and_settings(data.get("_key", ""), cache_key):
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


def figure_pngs(pg: dict) -> dict[str, bytes]:
    """그림 영역 이미지 (메인 스레드에서 미리 만든다 — PyMuPDF 문서는 여러 스레드에서 함께 쓰면 안 됨)."""
    if pg.get("rotated") and pg.get("_rot_key") in core._ROT_DOCS:
        page = core._ROT_DOCS[pg["_rot_key"]][0]
    else:
        page = src[pg["page_number"] - 1]
    return {core.fig_key(b): page.get_pixmap(clip=b["bbox"], dpi=engines.FIG_DPI).tobytes("png")
            for _, b in core.figure_jobs(pg)}


def translate_page(engine: engines.Engine, pg: dict, all_pages: list[dict],
                   pngs: dict[str, bytes]) -> tuple[list[str], list[dict], dict]:
    """한 쪽 번역. 쪽 경계에서 이어지는 문장은 앞뒤 쪽 원문을 '참고 맥락'으로 함께 보낸다(옮기지는 않음).
    그림 속 문구는 OpenAI면 그림 이미지를 직접 읽어 번역하고, DeepL이면 무료 OCR로 읽은 문구를 번역한다.
    반환: (번역 목록, 검토가 필요한 조각 [{id, review}], 그림 번역 {그림 위치: {src, tr, review}})"""
    units = units_of(pg)
    tr: list[str] = []
    reviews: list[dict] = []
    if units:
        meta = core.unit_meta(all_pages, pg, units)
        tr = engine.translate([h for *_, h in units], meta)
        reviews = [{"id": m["id"], "review": m.get("review", "")} for m in meta
                   if m.get("status") == "needs_review" or m.get("review")]
    figs: dict = {}
    for bi, b in core.figure_jobs(pg):
        key = core.fig_key(b)
        if engine_name == "OpenAI" and translate_captions:
            items = engines.figure_labels(api_key, model, pngs[key], target, glossary_entries)
            f = {"src": [x["source"] for x in items], "tr": [x["translation"] for x in items],
                 "review": [f'{x["source"]}: {x["review"] or "판독 확인 필요"}' for x in items
                            if x["status"] == "needs_review"]}
        elif b.get("items") and translate_captions:
            srcs = [core.plain(h) for h in b["items"]]
            f = {"src": srcs, "tr": engine.translate(b["items"]), "review": [b["review"]] if b.get("review") else []}
        else:
            continue
        figs[key] = f
        for r in f["review"]:
            reviews.append({"id": core.unit_id(pg["page_number"], bi, None), "review": f"그림 속 문구 — {r}"})
    return tr, reviews, figs


def run_translation(targets: list[dict]) -> None:
    """targets 쪽들을 번역해 캐시에 저장한다 (이미 번역된 쪽이어도 새로 번역해 덮어쓴다)."""
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
        pngs = {pg["page_number"]: figure_pngs(pg) for pg in targets}
        futures = {pool.submit(translate_page, engine, pg, pages, pngs[pg["page_number"]]): pg for pg in targets}
        status.write(f"번역 중… 0 / {len(targets)}쪽")
        for fut in as_completed(futures):
            pg = futures[fut]
            pno = pg["page_number"]
            try:
                tr, reviews, figs = fut.result()
                cache[str(pno)] = {"tr": tr, "sig": units_sig(pg), "review": reviews, "figs": figs,
                                   "pv": engines.PROMPT_VERSION if engine_name == "OpenAI" else "",
                                   "original": core.page_text(pg, translated=False)}
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
            bar.progress(done / len(targets))
            status.write(f"번역 중… {done} / {len(targets)}쪽 (방금 끝난 페이지: {pno})")

    status.write(f"번역 끝: {len(targets) - len(failures)} / {len(targets)}쪽")
    if fatal:
        st.error(f"{fatal} — 번역을 멈췄습니다. 지금까지 번역한 페이지는 저장되어 있습니다.")
    for pno, msg in sorted(failures.items()):
        if msg != fatal:
            st.error(f"Page {pno} 번역 실패 — {msg}")
    if failures:
        st.info("다시 누르면 남은 페이지부터 이어서 번역합니다.")
    st.session_state.pop("built", None)
    st.session_state.autosave = True          # 아래에서 PDF를 만든 뒤 서재에 저장


if st.button("번역 시작", type="primary", disabled=not todo,
             help="아직 번역하지 않은 쪽만 번역합니다." if base_map else None):
    run_translation(todo)

# ── 쪽별로 다시 번역: 마음에 안 드는 쪽만 새로 번역해 이전 번역본과 합친다 ──
with st.expander("🧩 마음에 안 드는 쪽만 다시 번역", expanded=bool(base_map) and not todo):
    if base_map:
        src_name = st.session_state.get("base_src") or "올린 파일"
        st.caption(f"이전 번역본({src_name})에서 {len(base_map)}쪽을 그대로 가져옵니다. "
                   "아래에 적은 쪽만 새로 번역해서 바꿔 끼우고, 합친 PDF를 다시 서재에 저장합니다.")
    else:
        st.caption("이전 번역본이 없습니다. 예전에 받아 둔 번역 PDF가 있으면 올려 주세요. "
                   "그 파일을 바탕으로 고칠 쪽만 새로 번역합니다.")
    up_base = st.file_uploader("이전 번역 PDF (선택)", type=["pdf"], key="base_up",
                               help="이 앱이 만든 번역 PDF면 쪽 표시(PDF p.N)로 맞추고, 다른 도구로 만든 번역본은 "
                                    "원문과 쪽수가 같을 때 쓸 수 있습니다.")
    base_bytes = up_base.getvalue() if up_base is not None else b""
    base_id = hashlib.md5(base_bytes).hexdigest() if base_bytes else ""
    if base_bytes and base_bytes != pdf_bytes and st.session_state.get("base_upload_id") != base_id:
        st.session_state.base_upload_id = base_id
        st.session_state.base_pdf = base_bytes
        st.session_state.base_src = "올린 파일"
        st.session_state.pop("built", None)
        st.rerun()
    redo_text = st.text_input("다시 번역할 쪽 (PDF 쪽 번호)", placeholder="예: 3, 12, 20-23",
                              help="번역 PDF 오른쪽 위의 'PDF p.N' 번호를 적으세요.")
    redo = [pages[k - 1] for k in core.parse_page_list(redo_text, n_pages)] if redo_text else []
    redo = [pg for pg in redo if pg["mode"] == "text"]
    if redo:
        rc = sum(len(core.plain(h)) for pg in redo for *_, h in units_of(pg))
        krw2 = engines.openai_cost_krw(model, rc) if engine_name == "OpenAI" else None
        if krw2 is not None and translate_captions:
            krw2 += engines.figure_cost_krw(model, sum(len(core.figure_jobs(pg)) for pg in redo)) or 0
        st.caption(f"{len(redo)}쪽 · 원문 약 {rc:,}자" + (f" · 예상 비용 약 {krw2:,}원" if krw2 is not None else ""))
    if st.button("이 쪽만 다시 번역", disabled=not redo):
        run_translation(redo)


# ─────────────────────────── 결과 만들기 ───────────────────────────
def translated_page(pg: dict) -> dict | None:
    """캐시의 번역을 입힌 페이지 사본. 번역이 없으면 None."""
    entry = cache.get(str(pg["page_number"]))
    if pg["mode"] != "text" or not cached_ok(pg):
        return None
    tpg = copy.deepcopy(pg)
    core.apply_translations(tpg, units_of(tpg), entry["tr"])
    core.apply_figure_translations(tpg, entry.get("figs") or {})
    return tpg


def build_outputs(page_list: list[dict] | None = None) -> dict[str, bytes]:
    out = pymupdf.open()
    txt: list[str] = []
    md: list[str] = [f"# {Path(uploaded.name).stem}\n"]
    plist = sel if page_list is None else page_list
    tpgs = {pg["page_number"]: translated_page(pg) for pg in plist}
    # 용어집 용어는 처음 나오는 곳에 영어 병기
    core.annotate_first_terms([t for t in tpgs.values() if t is not None], glossary_entries)
    for pg in plist:
        pno = pg["page_number"]
        tpg = tpgs[pno]
        if interleave:
            out.insert_pdf(src, from_page=pno - 1, to_page=pno - 1)
        if tpg is not None:
            core.render_page(out, src, tpg)
        elif base_doc is not None and pno in base_map:
            for i in base_map[pno]:               # 이전 번역본의 쪽을 그대로
                out.insert_pdf(base_doc, from_page=i, to_page=i)
        elif pg["mode"] == "text":
            core.render_page(out, src, pg, note="번역되지 않음 — 원문 그대로")
        else:
            core.render_page(out, src, pg)
        if tpg is not None:
            body = core.page_text(tpg)
        elif base_doc is not None and pno in base_map:
            body = "\n".join(base_doc[i].get_text() for i in base_map[pno]).strip()
        else:
            body = "(번역 없음 — 원문 참조)"
        orig = core.page_text(pg, translated=False)
        txt.append(f"──────────── Page {pno} ────────────\n\n{body}\n")
        md.append(f"# Page {pno}\n\n## Translation\n\n{body}\n\n## Original\n\n{orig}\n")
    # 책갈피(절 제목)와 문서 정보. 번역 쪽만 쓴다 (쪽 순서 = 출력 PDF 쪽 순서일 때)
    if not interleave and all(tpgs.get(pg["page_number"]) is not None for pg in plist):
        try:
            out.set_toc(core.bookmarks([tpgs[pg["page_number"]] for pg in plist]))
        except Exception:
            pass
    out.set_metadata({"title": f"{Path(uploaded.name).stem} (한국어 번역)", "creator": "snu_translate",
                      "producer": "PyMuPDF"})
    out.subset_fonts()
    pdf = out.tobytes(garbage=4, deflate=True)
    reviews = {pg["page_number"]: (cache.get(str(pg["page_number"])) or {}).get("review", []) for pg in plist}
    record = core.structure_record(plist, tpgs, reviews)
    return {"pdf": pdf, "txt": "\n".join(txt).encode("utf-8"), "md": "\n".join(md).encode("utf-8"),
            "json": json.dumps(record, ensure_ascii=False, indent=1).encode("utf-8")}


translated_count = sum(1 for pg in sel if has_translation(pg))
if translated_count:
    build_sig = (cache_key, start, end, interleave, len(cache),
                 hashlib.md5(json.dumps({k: v.get("tr") for k, v in cache.items()}, ensure_ascii=False)
                             .encode()).hexdigest(),
                 len(st.session_state.get("base_pdf") or b""))
    if st.session_state.get("built", (None,))[0] != build_sig:
        with st.spinner("번역 PDF 만드는 중…"):
            st.session_state.built = (build_sig, build_outputs())
    files = st.session_state.built[1]

    st.subheader("다운로드")
    stem = f"{Path(uploaded.name).stem[:60]}_번역_p{start}-{end}"
    d1, d2, d3, d4 = st.columns(4)
    d1.download_button("📕 번역 PDF", files["pdf"], file_name=f"{stem}.pdf", mime="application/pdf",
                       type="primary")
    d2.download_button("TXT", files["txt"], file_name=f"{stem}.txt", mime="text/plain")
    d3.download_button("Markdown", files["md"], file_name=f"{stem}.md", mime="text/markdown")
    d4.download_button("구조·번역 데이터", files["json"], file_name=f"{stem}_구조.json",
                       mime="application/json", help="쪽별 블록(식별자·위치·원문·번역·검토 기록). 다시 작업할 때 씁니다.")
    review_rows = [(int(k), r) for k, v in cache.items() if k.isdigit() and isinstance(v, dict)
                   for r in v.get("review", []) if start <= int(k) <= end]
    if review_rows:
        with st.expander(f"⚠️ 검토가 필요한 곳 {len(review_rows)}개 — 원문과 대조해 보세요"):
            for pno, r in sorted(review_rows, key=lambda x: x[0]):
                st.markdown(f"- **PDF p.{pno}** `{r['id']}` — {r.get('review') or '번역기가 확인 필요로 표시'}")

    def paper_title() -> str:
        for pg in pages[:3]:
            for b in pg["blocks"]:
                if b.get("title"):
                    return b["text"][:150]
        return Path(uploaded.name).stem

    def save_to_library() -> None:
        # 서재에는 범위와 상관없이 '논문 전체'를 저장한다. 나눠서 번역해도 지금까지 번역한
        # 모든 쪽이 한 파일에 모이고, 아직 번역하지 않은 쪽은 원문 그대로 들어간다.
        done_pages = [pg["page_number"] for pg in pages if has_translation(pg)]
        with st.spinner("서재용 전체 PDF 만드는 중…"):
            full_pdf = build_outputs(pages)["pdf"]
        meta = {
            "id": paper_id, "title": paper_title(), "filename": uploaded.name,
            "total_pages": n_pages, "range": [min(done_pages), max(done_pages)],
            "translated": len(done_pages),
            "engine": engine_name + (f" {model}" if model else ""), "cache_key": cache_key,
        }
        progress = json.dumps({"_key": cache_key, **cache}, ensure_ascii=False).encode("utf-8")
        with st.spinner("서재에 저장하는 중…"):
            lib.check()
            extra = {"ocr.json": json.dumps(ocr_data, ensure_ascii=False).encode("utf-8")} if ocr_data else {}
            lib.save_paper(meta, {**extra, "translation.pdf": full_pdf, "original.pdf": pdf_bytes,
                                  "progress.json": progress})
        st.session_state.pop("lib_items", None)
        st.session_state.lib_meta = None
        st.session_state.base_pdf = full_pdf
        st.session_state.base_src = "서재"

    if lib is not None:
        if st.session_state.pop("autosave", False):
            try:
                save_to_library()
                st.success("📚 서재에 저장했습니다. 사이드바의 '내 서재'에서 언제든 다시 받을 수 있습니다.")
            except library.LibraryError as e:
                st.error(f"서재 저장 실패: {e}")
        elif st.button("📚 서재에 저장"):
            try:
                save_to_library()
                st.success("서재에 저장했습니다.")
            except library.LibraryError as e:
                st.error(f"서재 저장 실패: {e}")
    else:
        st.caption("📚 서재를 연결하면 번역본이 자동 보관됩니다 (사이드바 → 내 서재 참고).")

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
        if tpg is None and base_doc is not None and view_p in base_map:
            st.caption("이전 번역본의 쪽입니다 — 고치려면 위 '🧩 마음에 안 드는 쪽만 다시 번역'에 이 쪽 번호를 적으세요.")
            for i in base_map[view_p]:
                st.image(base_doc[i].get_pixmap(dpi=110).tobytes("png"), width="stretch")
        elif tpg is None:
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
