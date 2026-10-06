"""
논문 정독용 PDF 번역기 (Streamlit + DeepL/OpenAI + PyMuPDF)

실행:  streamlit run app.py
구성:  app.py      화면, 캐시, 다운로드
       engines.py  번역 엔진 (DeepL / OpenAI)
       library.py  '내 서재' — 번역본을 GitHub 비공개 저장소에 보관
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

prompt_ver = engines.PROMPT_VERSION if engine_name == "OpenAI" else ""
opts_sig = hashlib.md5(json.dumps([engine_name, model, prompt_ver, target, translate_refs, translate_captions,
                                   glossary_entries],
                                  sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:8]
cache_key = f"{Path(uploaded.name).stem[:40]}_{pdf_hash[:16]}_{opts_sig}"
paper_id = pdf_hash[:16]
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


todo = [pg for pg in sel if pg["mode"] == "text" and not cached_ok(pg)]
need_chars = sum(len(core.plain(h)) for pg in todo for *_, h in units_of(pg))
done_in_range = sum(1 for pg in sel if cached_ok(pg))
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
                cache[str(pno)] = {"tr": tr, "sig": units_sig(pg),
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
            bar.progress(done / len(todo))
            status.write(f"번역 중… {done} / {len(todo)}쪽 (방금 끝난 페이지: {pno})")

    status.write(f"번역 완료 페이지: {sum(1 for pg in sel if cached_ok(pg))} / {len(sel)}")
    if fatal:
        st.error(f"{fatal} — 번역을 멈췄습니다. 지금까지 번역한 페이지는 저장되어 있습니다.")
    for pno, msg in sorted(failures.items()):
        if msg != fatal:
            st.error(f"Page {pno} 번역 실패 — {msg}")
    if failures:
        st.info("'번역 시작'을 다시 누르면 남은 페이지부터 이어서 번역합니다.")
    st.session_state.pop("built", None)
    st.session_state.autosave = True          # 아래에서 PDF를 만든 뒤 서재에 저장


# ─────────────────────────── 결과 만들기 ───────────────────────────
def translated_page(pg: dict) -> dict | None:
    """캐시의 번역을 입힌 페이지 사본. 번역이 없으면 None."""
    entry = cache.get(str(pg["page_number"]))
    if pg["mode"] != "text" or not cached_ok(pg):
        return None
    tpg = copy.deepcopy(pg)
    core.apply_translations(tpg, units_of(tpg), entry["tr"])
    return tpg


def build_outputs(page_list: list[dict] | None = None) -> dict[str, bytes]:
    out = pymupdf.open()
    txt: list[str] = []
    md: list[str] = [f"# {Path(uploaded.name).stem}\n"]
    for pg in (sel if page_list is None else page_list):
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


translated_count = sum(1 for pg in sel if cached_ok(pg))
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

    def paper_title() -> str:
        for pg in pages[:3]:
            for b in pg["blocks"]:
                if b.get("title"):
                    return b["text"][:150]
        return Path(uploaded.name).stem

    def save_to_library() -> None:
        # 서재에는 범위와 상관없이 '논문 전체'를 저장한다. 나눠서 번역해도 지금까지 번역한
        # 모든 쪽이 한 파일에 모이고, 아직 번역하지 않은 쪽은 원문 그대로 들어간다.
        done_pages = [pg["page_number"] for pg in pages if cached_ok(pg)]
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
            lib.save_paper(meta, {"translation.pdf": full_pdf, "original.pdf": pdf_bytes,
                                  "progress.json": progress})
        st.session_state.pop("lib_items", None)
        st.session_state.lib_meta = None

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
