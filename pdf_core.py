"""
pdf_core.py — 논문 PDF 추출 · 번역 PDF 조판 엔진 (Streamlit UI와 분리된 순수 로직)

흐름
  extract_document()  : 모든 페이지 → 블록 목록 (읽기 순서, 문단 복원, 이탤릭, 그림·표 영역,
                        참고문헌 구간, 페이지 경계 문장 처리까지 끝난 상태)
  (번역)              : 각 블록의 "html"을 DeepL에 tag_handling="html"로 보낸다 (app.py)
  render_page()       : 번역된 블록 → 원문과 같은 크기의 번역 페이지
                        (넘치면 글자·줄간격 자동 축소, 하한선 아래면 "(계속)" 페이지)

블록 kind
  header    러닝 헤더/쪽수 — 번역 안 함, 출력 안 함
  heading   제목·소제목
  para      본문 문단
  list      글머리표 목록 (items)
  caption   FIGURE n / TABLE n 캡션
  figure    그림·표·도식 영역 — 원문에서 이미지로 잘라 그대로 넣음 (내부 글자는 번역 안 함)
  footnote  각주 — 페이지 아래 구분선 밑에 배치
  note      본문 중간의 작은 글씨(그림 옆 면담 대화문 등) — 제자리에 작게
  reference 참고문헌 한 항목 — 옵션에 따라 원문 유지
  table     가로로 돌려 놓은 표 (rotated_table.py) — 칸·항목 단위로 번역해 표로 다시 그림
"""
from __future__ import annotations

import re
import statistics
from pathlib import Path
from typing import Any

import pymupdf

import rotated_table

# ─────────────────────────── 설정 ───────────────────────────
FONT_DIR = Path(__file__).parent / "fonts"
FONT_REGULAR = "NanumGothic-Regular.ttf"   # 다른 글꼴로 바꾸려면 fonts/에 넣고 파일명만 변경
FONT_BOLD = "NanumGothic-Bold.ttf"

LINE_HEIGHT = 1.55        # 번역문 기본 줄간격
MIN_SCALE = 0.72          # 페이지에 맞추려고 줄일 수 있는 하한 (본문 10pt → 약 7.2pt)
ITALIC_MIN_CHARS = 3      # 이 글자 수 미만의 이탤릭(M, SD, p, F, t 등 통계 기호)은 볼드 처리 안 함
HEADER_ZONE = 72          # 페이지 위쪽 이 높이 안의 짧은 줄 = 러닝 헤더(쪽수, 저자명)
FOOTER_ZONE = 40          # 페이지 아래쪽 이 높이 안의 짧은 줄 = 꼬리말
FIG_DPI = 200             # 그림을 잘라 넣을 때 해상도
MIN_FIG = (40, 30)        # 이보다 작은 이미지/도형은 무시(아이콘, 선 등)

ITALIC_FLAG = 2
SANS_FONTS = ("helvetica", "arial", "frutiger", "univers")  # run-in 소제목에 흔한 글꼴

PROTECT_RE = re.compile(
    r"(https?://\S+|www\.\S+|doi:\s*\S+|\b10\.\d{4,9}/\S+|[\w.+-]+@[\w-]+\.[\w.]+)"
)
SENTENCE_END_RE = re.compile(r"[.?!:”\"’)\]]\s*$")
CAPTION_RE = re.compile(r"^(FIGURE|Figure|Fig\.|TABLE|Table)\s*\d+", re.S)
REF_HEAD_RE = re.compile(r"^(REFERENCES|References|Bibliography|BIBLIOGRAPHY|Works Cited|참고문헌)\s*$")
END_REF_RE = re.compile(r"^(APPENDIX|Appendix|부록)\b")
ABBREV = ("e.g.", "i.e.", "et al.", "cf.", "vs.", "pp.", "p.", "Vol.", "No.", "Fig.", "Eds.", "Ed.")
BULLETS = ("•", "–", "·", "▪", "◦")


# ─────────────────────── 1. 텍스트 추출 ───────────────────────
def _span_text(span: dict, size: float) -> str:
    """rawdict span → 문자열.
    양쪽 정렬된 논문은 공백 문자 없이 글자 간격만으로 단어를 띄운 줄이 있다
    ('students’statisticalthinking…'). 글자 사이 간격이 크면 공백을 넣는다."""
    out: list[str] = []
    prev = None
    for ch in span["chars"]:
        if prev is not None and ch["c"] != " " and prev["c"] != " ":
            gap = ch["bbox"][0] - prev["bbox"][2]
            limit = size * (0.03 if prev["c"] in "’'" else 0.08)
            if gap > limit:
                out.append(" ")
        out.append(ch["c"])
        prev = ch
    return "".join(out)


def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _protect(s: str) -> str:
    """HTML 문자열 안의 URL/DOI/이메일을 번역 금지 태그로 감싼다."""
    return PROTECT_RE.sub(lambda m: f'<span translate="no">{m.group(0)}</span>', s)


def body_size_of(page_dict: dict) -> float:
    """글자 수로 가중한 최빈 글자 크기 = 본문 크기."""
    sizes: list[float] = []
    for b in page_dict["blocks"]:
        for l in b.get("lines", []):
            for s in l["spans"]:
                sizes += [round(s["size"], 1)] * len(s["chars"])
    return statistics.mode(sizes) if sizes else 10.0


def _line(line: dict, body: float, mixed: bool = True) -> dict:
    """한 줄 → {html, text, x0, size, sans}. 이탤릭은 <i>, 위첨자 각주번호는 <sup>.
    mixed: 블록 안에 본문 글꼴(세리프)이 섞여 있는지. 블록 전체가 산세리프면(면담 대화문,
    그림 설명 등) 산세리프 시작을 run-in 소제목으로 보지 않는다."""
    html_parts: list[str] = []
    plain_parts: list[str] = []
    sans = ""
    max_size = 0.0
    base_y = max(s["bbox"][3] for s in line["spans"])

    def is_sans(sp: dict) -> bool:
        return any(f in sp["font"].lower() for f in SANS_FONTS)

    for idx, s in enumerate(line["spans"]):
        t = _span_text(s, s["size"])
        if not t:
            continue
        max_size = max(max_size, s["size"])
        e = esc(t)
        is_sup = s["size"] < body * 0.8 and s["bbox"][3] < base_y - 1 and t.strip().isdigit()
        if is_sup:
            e = f"<sup>{e.strip()}</sup>"
        elif s["flags"] & ITALIC_FLAG and len(t.strip()) >= ITALIC_MIN_CHARS:
            lead = e[: len(e) - len(e.lstrip())]
            e = f"{lead}<i>{e.strip()}</i>"
        elif idx == 0 and is_sans(s) and t.strip():
            # 본문과 다른 산세리프로 시작 → 'Describing data.' 같은 run-in 소제목
            sans = t
            if mixed:
                e = f"<b>{e}</b>"
        html_parts.append(e)
        plain_parts.append(t)
    return {"html": "".join(html_parts), "text": "".join(plain_parts),
            "x0": line["bbox"][0], "y0": line["bbox"][1], "size": max_size, "sans": sans}


def _drop_trailing_hyphen(html: str) -> str:
    """'...<i>idiosyn-</i>'처럼 닫는 태그 앞에 있는 줄끝 하이픈을 지운다."""
    m = re.search(r"-((?:</[a-z]+>)*)\s*$", html)
    return html[: m.start()] + m.group(1) if m else html


def join_lines(lines: list[dict]) -> tuple[str, str]:
    """PDF 줄바꿈을 문단으로 복원. 줄 끝 하이픈('educa-' + 'tion')은 붙이고, 나머지는 공백으로."""
    html, plain = "", ""
    for ln in lines:
        h, p = ln["html"].strip(), ln["text"].strip()
        if not p:
            continue
        if plain.endswith("-") and p[:1].islower():
            html, plain = _drop_trailing_hyphen(html), plain[:-1]
        elif plain:
            html += " "
            plain += " "
        html += h
        plain += p
    for t in ("i", "b"):                        # 줄마다 나뉜 태그를 하나로
        html = html.replace(f"</{t}><{t}>", "")
        html = re.sub(rf"</{t}>\s+<{t}>", " ", html)
    return html, plain


def _figure_regions(page: pymupdf.Page, raw: dict) -> list[pymupdf.Rect]:
    """그림·표·도식 영역: 이미지 블록 + 벡터 도형 묶음. 가까운 것끼리 합친다."""
    rects: list[pymupdf.Rect] = []
    for b in raw["blocks"]:
        if b["type"] == 1:
            r = pymupdf.Rect(b["bbox"])
            if r.width >= MIN_FIG[0] and r.height >= MIN_FIG[1]:
                rects.append(r)
    try:
        page_area = page.rect.width * page.rect.height
        drawings = [
            d for d in page.get_drawings()
            # 흰 배경 사각형, 페이지 전체를 덮는 틀은 그림이 아니다
            if not (d.get("fill") in ((1.0, 1.0, 1.0), None) and d.get("color") is None)
            and d["rect"].width * d["rect"].height < page_area * 0.6
        ]
        clusters = page.cluster_drawings(drawings=drawings, x_tolerance=12, y_tolerance=12) if drawings else []
        for r in clusters:
            if r.height < 4 or r.width < 4:      # 가는 구분선 등은 제외
                continue
            rects.append(pymupdf.Rect(r))
    except Exception:
        pass

    merged = True
    while merged:                                # 겹치거나 15pt 이내로 붙은 영역은 하나로
        merged = False
        out: list[pymupdf.Rect] = []
        for r in rects:
            for o in out:
                if (r + (-15, -15, 15, 15)).intersects(o):
                    o |= r
                    merged = True
                    break
            else:
                out.append(pymupdf.Rect(r))
        rects = out
    return [r for r in rects if r.width >= MIN_FIG[0] and r.height >= MIN_FIG[1]]


def _rotated_ratio(raw: dict) -> float:
    tot = rot = 0
    for b in raw["blocks"]:
        for l in b.get("lines", []):
            n = sum(len(s["chars"]) for s in l["spans"])
            tot += n
            if abs(l["dir"][0] - 1) > 0.01:
                rot += n
    return rot / tot if tot else 0.0


def extract_page(page: pymupdf.Page) -> dict[str, Any]:
    """원문 페이지 1장 → {"mode", "body_size", "blocks"}.

    mode = "text"  : 일반 페이지 (블록 단위로 번역)
           "image" : 가로로 돌려 앉힌 표처럼 글자 대부분이 회전된 페이지 → 원문을 그대로 넣는다
    """
    raw = page.get_text("rawdict", flags=pymupdf.TEXT_PRESERVE_IMAGES)
    body = body_size_of(raw)
    H = page.rect.height
    if _rotated_ratio(raw) > 0.5:
        # 가로로 돌려 놓은 표 페이지: 칸 구조를 읽어 표로 번역. 실패하면 원문 그대로.
        try:
            table = rotated_table.extract_rotated_table(page)
        except Exception:
            table = None
        if table is None:
            return {"mode": "image", "body_size": body, "blocks": []}
        return {"mode": "text", "body_size": table["size"], "blocks": [table]}

    figs = _figure_regions(page, raw)
    blocks: list[dict[str, Any]] = [
        {"kind": "figure", "bbox": r, "size": body, "html": "", "text": ""} for r in figs
    ]

    for b in raw["blocks"]:
        if b["type"] != 0:
            continue
        bbox = pymupdf.Rect(b["bbox"])
        mixed = any(not any(f in sp["font"].lower() for f in SANS_FONTS)
                    for l in b["lines"] for sp in l["spans"]
                    if "".join(c["c"] for c in sp["chars"]).strip())
        lines = [_line(l, body, mixed) for l in b["lines"] if abs(l["dir"][0] - 1) < 0.01]
        lines = [x for x in lines if x["text"].strip()]
        if not lines:
            continue
        plain_all = " ".join(x["text"].strip() for x in lines)

        # 그림 영역 안의 글자(축 이름, 범례, 표 칸)는 그림의 일부 → 번역하지 않는다.
        # 단, 캡션은 그림 영역에 붙어 있어도 따로 번역한다.
        center = pymupdf.Point((bbox.x0 + bbox.x1) / 2, (bbox.y0 + bbox.y1) / 2)
        if any(f.contains(center) for f in figs) and not CAPTION_RE.match(plain_all):
            continue

        size = max(x["size"] for x in lines)
        plains = [x["text"].strip() for x in lines]

        # 글머리표 목록: 한 블록 안에 '•'로 시작하는 줄이 여럿
        if sum(p.startswith(BULLETS) for p in plains) >= 2:
            items: list[list[dict]] = []
            strip = "".join(BULLETS) + " "
            for ln in lines:
                if ln["text"].strip().startswith(BULLETS) or not items:
                    items.append([])
                items[-1].append({**ln, "html": ln["html"].strip().lstrip(strip),
                                  "text": ln["text"].strip().lstrip(strip)})
            joined = [join_lines(it) for it in items]
            blocks.append({"kind": "list", "bbox": bbox, "size": body,
                           "items": [_protect(h) for h, _ in joined],
                           "html": "", "text": "\n".join("• " + p for _, p in joined)})
            continue

        html, plain = join_lines(lines)
        kind = "para"
        if (bbox.y1 < HEADER_ZONE or bbox.y0 > H - FOOTER_ZONE) and len(plain) < 100:
            kind = "header"
        elif CAPTION_RE.match(plain) and (plain[:5].isupper() or size < body * 0.95):
            kind = "caption"
        elif size < body * 0.85 and bbox.y0 > H * 0.45:
            kind = "footnote"           # 각주, 교신저자 안내 등 페이지 아래 작은 글씨
        elif len(plain) < 120 and size > body * 1.15:
            kind = "heading"            # 큰 글씨 = 제목 (문장부호로 끝나도)
        elif len(plain) < 80 and not SENTENCE_END_RE.search(plain) and (
            plain.isupper() or lines[0]["sans"].strip() == plain
        ):
            kind = "heading"
        centered = (abs((bbox.x0 + bbox.x1) / 2 - page.rect.width / 2) < 15
                    and bbox.width < page.rect.width * 0.7 and len(plain) < 120)
        blocks.append({"kind": kind, "bbox": bbox, "size": size, "html": _protect(html),
                       "text": plain, "lines": lines, "centered": centered,
                       "title": kind == "heading" and size > body * 1.4})

    # 작은 글씨라도 그 아래에 본문 문단이 있으면 각주가 아니다 (그림 옆 대화문, 표 주석 등)
    body_tops = [b["bbox"].y0 for b in blocks if b["kind"] == "para" and b["size"] >= body * 0.95]
    for b in blocks:
        if b["kind"] == "footnote" and not re.match(r"^(\d+|\*|†)", b["text"]) \
                and any(y > b["bbox"].y1 for y in body_tops):
            b["kind"] = "note"
        elif b["kind"] == "para" and b["size"] < body * 0.85:
            b["kind"] = "note"

    ordered = sort_reading_order(blocks, page.rect)
    ordered = _merge_continuations(ordered)
    return {"mode": "text", "body_size": body, "blocks": _merge_headings(ordered)}


def _merge_continuations(blocks: list[dict]) -> list[dict]:
    """줄마다 따로 잡힌 블록 중 앞 블록에서 이어지는 것('win-' + 'tertime.')을 합친다.
    조건: 같은 종류, 바로 아래(간격이 글자 크기 이하), 다음 블록이 소문자로 시작하거나
    앞 블록이 하이픈으로 끝남."""
    out: list[dict] = []
    for b in blocks:
        prev = out[-1] if out else None
        if (prev and prev["kind"] == b["kind"] and b["kind"] in ("para", "note", "footnote")
                and prev.get("lines") and b.get("lines")
                and -2 <= b["bbox"].y0 - prev["bbox"].y1 <= b["size"] * 1.0
                and (prev["text"].endswith("-") or b["text"][:1].islower())
                and not SENTENCE_END_RE.search(prev["text"])):
            h, t = join_lines(prev["lines"] + b["lines"])
            prev.update(html=_protect(h), text=t, lines=prev["lines"] + b["lines"])
            prev["bbox"] |= b["bbox"]
            continue
        out.append(b)
    return out


def _merge_headings(blocks: list[dict]) -> list[dict]:
    """여러 줄로 나뉜 제목(같은 글자 크기의 heading이 붙어 있음)을 하나로 합친다."""
    out: list[dict] = []
    for b in blocks:
        prev = out[-1] if out else None
        if (prev and prev["kind"] == b["kind"] == "heading" and abs(prev["size"] - b["size"]) < 0.5
                and 0 <= b["bbox"].y0 - prev["bbox"].y1 < b["size"] * 1.2):
            prev["html"] += " " + b["html"]
            prev["text"] += " " + b["text"]
            prev["bbox"] |= b["bbox"]
            prev["lines"] = prev.get("lines", []) + b.get("lines", [])
            continue
        out.append(b)
    return out


# ─────────────────────── 2. 읽기 순서 ───────────────────────
def detect_columns(blocks: list[dict], page_rect: pymupdf.Rect) -> int:
    """본문 블록 대부분이 페이지 폭의 절반보다 좁고 좌우로 나뉘어 있으면 2단."""
    body = [b for b in blocks if b["kind"] in ("para", "list", "heading")]
    if len(body) < 3:
        return 1
    mid = page_rect.width / 2
    narrow = [b for b in body if b["bbox"].width < page_rect.width * 0.55]
    left = [b for b in narrow if b["bbox"].x1 <= mid + 10]
    right = [b for b in narrow if b["bbox"].x0 >= mid - 10]
    return 2 if len(narrow) >= len(body) * 0.6 and left and right else 1


def sort_reading_order(blocks: list[dict], page_rect: pymupdf.Rect) -> list[dict]:
    """헤더 → (전체 폭 블록 / 왼쪽 단 / 오른쪽 단) → 각주 순서.

    2단일 때: 전체 폭 블록(제목·초록·양단에 걸친 그림)을 만나면 그때까지 쌓인 왼쪽 단,
    오른쪽 단을 차례로 내보낸다. 그래서 '그림 위의 2단 → 그림 → 그림 아래의 2단'도 맞게 나온다.
    한계: 3단 이상, 단을 가로지르는 표, 박스 기사 등은 순서가 어긋날 수 있다.
    """
    header = sorted([b for b in blocks if b["kind"] == "header"], key=lambda b: b["bbox"].y0)
    foot = sorted([b for b in blocks if b["kind"] == "footnote"], key=lambda b: b["bbox"].y0)
    body = [b for b in blocks if b["kind"] not in ("header", "footnote")]

    if detect_columns(blocks, page_rect) == 1:
        body.sort(key=lambda b: (round(b["bbox"].y0), b["bbox"].x0))
        return header + body + foot

    mid = page_rect.width / 2
    ordered: list[dict] = []
    left: list[dict] = []
    right: list[dict] = []
    for b in sorted(body, key=lambda b: b["bbox"].y0):
        if b["bbox"].width > page_rect.width * 0.6:
            ordered += sorted(left, key=lambda x: x["bbox"].y0) + sorted(right, key=lambda x: x["bbox"].y0)
            left, right = [], []
            ordered.append(b)
        elif (b["bbox"].x0 + b["bbox"].x1) / 2 < mid:
            left.append(b)
        else:
            right.append(b)
    ordered += sorted(left, key=lambda x: x["bbox"].y0) + sorted(right, key=lambda x: x["bbox"].y0)
    return header + ordered + foot


# ─────────────────────── 3. 참고문헌 ───────────────────────
def mark_references(pages: list[dict]) -> None:
    """'REFERENCES' 제목 뒤부터 'APPENDIX' 전까지를 참고문헌으로 표시하고 항목 단위로 다시 나눈다.

    PDF 블록은 참고문헌 항목 경계와 맞지 않는 경우가 많아서(한 블록에 앞 항목 끝 + 다음 항목 시작),
    줄 단위로 모은 뒤 '내어쓰기'(첫 줄은 왼쪽, 이어지는 줄은 들여쓰기)로 항목을 나눈다.
    한계: 제목이 다른 이름('Literature Cited' 등)이거나 번호식([1], [2]) 참고문헌은 감지가 불완전하다.
    """
    in_refs = False
    for pg in pages:
        if pg["mode"] != "text":
            continue
        new_blocks: list[dict] = []
        ref_lines: list[dict] = []

        def flush() -> None:
            if not ref_lines:
                return
            margin = min(l["x0"] for l in ref_lines)
            entries: list[list[dict]] = []
            for ln in ref_lines:
                if ln["x0"] <= margin + 3 or not entries:
                    entries.append([])
                entries[-1].append(ln)
            for e in entries:
                h, p = join_lines(e)
                new_blocks.append({"kind": "reference", "bbox": pymupdf.Rect(), "size": e[0]["size"],
                                   "html": _protect(h), "text": p})
            ref_lines.clear()

        for b in pg["blocks"]:
            if b["kind"] == "heading" and REF_HEAD_RE.match(b["text"].strip()):
                in_refs = True
                new_blocks.append(b)
                continue
            if b["kind"] == "heading" and END_REF_RE.match(b["text"].strip()):
                flush()
                in_refs = False
            if in_refs and b["kind"] in ("para", "footnote") and b.get("lines"):
                ref_lines.extend(b["lines"])
                continue
            flush()
            new_blocks.append(b)
        flush()
        pg["blocks"] = new_blocks


# ─────────────────── 4. 페이지 경계에서 끊긴 문장 ───────────────────
def _first_sentence_split(text: str) -> int:
    """첫 문장이 끝나는 위치(문자 인덱스). 약어(et al., e.g.)에서는 끊지 않는다."""
    for m in re.finditer(r"[.?!][”\"’)]?\s+(?=[A-Z“\"(])", text):
        before = text[: m.start() + 1]
        if any(before.endswith(a) for a in ABBREV):
            continue
        return m.end()
    return len(text)


def carry_cross_page(pages: list[dict]) -> None:
    """페이지 마지막 문단이 문장 중간에서 끊겼으면, 다음 페이지 첫 문단의 첫 문장을
    이쪽으로 가져온다. 문장은 시작한 페이지에서 끝까지 번역된다."""
    for i in range(len(pages) - 1):
        if pages[i]["mode"] != "text" or pages[i + 1]["mode"] != "text":
            continue
        body = [b for b in pages[i]["blocks"] if b["kind"] not in ("header", "footnote")]
        nxt = [b for b in pages[i + 1]["blocks"] if b["kind"] not in ("header", "footnote")]
        if not body or not nxt or body[-1]["kind"] != "para":
            continue
        last = body[-1]
        # 다음 쪽 맨 위에 그림이 있어도, 그 아래 첫 문단이 소문자로 시작하면 이어지는 문단이다
        first = next((b for b in nxt if b["kind"] == "para"), None)
        if first is None or (first is not nxt[0] and not first["text"][:1].islower()):
            continue
        if SENTENCE_END_RE.search(last["text"]):
            continue
        if last["size"] < pages[i]["body_size"] * 0.95 or first["size"] < pages[i + 1]["body_size"] * 0.95:
            continue                              # 작은 글씨(교신저자 안내 등)는 이어 붙이지 않음
        cut = _first_sentence_split(first["text"])
        moved = first["text"][:cut].strip()
        if cut >= len(first["text"]):           # 다음 문단 전체가 한 문장이면 통째로
            hcut = len(first["html"])
        else:
            tail = esc(moved[-20:])
            pos = first["html"].find(tail)
            if pos < 0:
                continue
            hcut = pos + len(tail)
            m = re.match(r"(\s*</(?:i|b|sup|span)>)+", first["html"][hcut:])
            if m:                               # 닫는 태그까지 함께 가져온다
                hcut += m.end()
        if last["text"].endswith("-") and moved[:1].islower():   # 'per-' + 'formed'
            last["html"] = _drop_trailing_hyphen(last["html"]) + first["html"][:hcut].strip()
            last["text"] = last["text"][:-1] + moved
        else:
            last["html"] += " " + first["html"][:hcut].strip()
            last["text"] += " " + moved
        first["html"] = first["html"][hcut:].strip()
        first["text"] = first["text"][cut:].strip()
        if not first["text"]:
            pages[i + 1]["blocks"].remove(first)
        else:
            first["no_indent"] = True


def extract_document(doc: pymupdf.Document) -> list[dict]:
    """문서 전체 추출. 특정 페이지가 실패해도 나머지는 계속한다(mode='error')."""
    pages: list[dict] = []
    for i in range(doc.page_count):
        try:
            pg = extract_page(doc[i])
        except Exception as e:
            pg = {"mode": "error", "body_size": 10.0, "blocks": [], "error": str(e)}
        pg["page_number"] = i + 1
        pages.append(pg)
    mark_references(pages)
    carry_cross_page(pages)
    _repeat_table_headers(pages)
    return pages


def _repeat_table_headers(pages: list[dict]) -> None:
    """두 쪽에 걸친 표에서 둘째 쪽에 머리행이 없으면 앞쪽 머리행을 복사해 넣는다(읽기 편하게)."""
    prev = None
    for pg in pages:
        tables = [b for b in pg["blocks"] if b["kind"] == "table"]
        if not tables:
            prev = None
            continue
        t = tables[0]
        lay = t["layout"]
        if prev is not None and all(h is None for h in lay["header"]) \
                and len(prev["layout"]["header"]) == len(lay["header"]):
            for c, h in enumerate(prev["layout"]["header"]):
                if h is not None:
                    t["items"].append(prev["items"][h])
                    lay["header"][c] = len(t["items"]) - 1
        prev = t


def translatable_units(pg: dict, translate_refs: bool = False,
                       translate_captions: bool = True) -> list[tuple[int, int | None, str]]:
    """번역할 조각 목록: (블록 번호, 목록 항목 번호 | None, html)."""
    units: list[tuple[int, int | None, str]] = []
    for bi, b in enumerate(pg["blocks"]):
        k = b["kind"]
        if k in ("header", "figure"):
            continue
        if (k == "reference" and not translate_refs) or (k == "caption" and not translate_captions):
            continue
        if k in ("list", "table"):
            units += [(bi, ii, h) for ii, h in enumerate(b["items"])]
        elif b["html"].strip():
            units.append((bi, None, b["html"]))
    return units


def apply_translations(pg: dict, units: list[tuple[int, int | None, str]], results: list[str]) -> None:
    """번역 결과를 블록에 붙인다 (tr / tr_items)."""
    for (bi, ii, _), tr in zip(units, results):
        b = pg["blocks"][bi]
        if ii is None:
            b["tr"] = tr
        else:
            b.setdefault("tr_items", list(b["items"]))[ii] = tr


# ─────────────────────── 5. 번역 페이지 조판 ───────────────────────
CSS = f"""
@font-face {{ font-family: kr; src: url({FONT_REGULAR}); }}
@font-face {{ font-family: kr; src: url({FONT_BOLD}); font-weight: bold; }}
* {{ font-family: kr; }}
body {{ color: #1a1a1a; }}
p {{ margin: 0 0 0.45em 0; line-height: {LINE_HEIGHT}; text-align: left; }}
p.para {{ text-indent: 1em; }}
p.center {{ text-align: center; }}
h1 {{ font-size: 1.5em; font-weight: bold; text-align: center; line-height: 1.35; margin: 0.4em 0 0.8em 0; }}
h2 {{ font-size: 1.05em; font-weight: bold; text-align: center; margin: 0.6em 0 0.5em 0; }}
h3 {{ font-size: 1.0em; font-weight: bold; margin: 0.6em 0 0.35em 0; }}
ul {{ margin: 0.1em 0 0.5em 1.6em; padding: 0; }}
li {{ line-height: {LINE_HEIGHT}; margin-bottom: 0.15em; }}
b {{ font-weight: bold; }}
sup {{ font-size: 0.7em; }}
.cap {{ font-size: 0.85em; text-align: center; margin: 0.2em 0 0.7em 0; }}
.fig {{ text-align: center; margin: 0.3em 0 0.2em 0; }}
.note {{ font-size: 0.85em; line-height: 1.45; margin: 0 0 0.25em 1.5em; }}
.fn {{ font-size: 0.8em; line-height: 1.45; color: #333; }}
.ref {{ font-size: 0.8em; line-height: 1.4; margin: 0 0 0.3em 1.6em; text-indent: -1.6em; }}
.tcap {{ text-align: center; font-size: 1.0em; margin: 0 0 0.6em 0; line-height: 1.35; }}
table.tbl {{ border-collapse: collapse; }}
table.first {{ border-top: 0.8px solid #333; }}
th {{ font-size: 0.85em; font-weight: bold; text-align: left; vertical-align: bottom;
      padding: 0.2em 0.3em; border-bottom: 0.6px solid #333; }}
th.sup {{ text-align: center; }}
td {{ font-size: 0.85em; vertical-align: top; padding: 0.25em 0.3em; line-height: 1.35; }}
p.ti {{ margin: 0 0 0.2em 0; padding-left: 0.8em; text-indent: -0.8em; text-align: left; line-height: 1.35; }}
p.tp {{ margin: 0 0 0.2em 0; text-align: left; line-height: 1.35; }}
.tnote {{ font-size: 0.8em; margin-top: 0.5em; border-top: 0.8px solid #333; padding-top: 0.3em; }}
u {{ text-decoration: underline; }}
hr {{ border: none; border-top: 0.5px solid #999; width: 30%; margin: 0.6em 0 0.4em 0; }}
"""


def to_reading_html(tr_html: str, italic_to_bold: bool = True) -> str:
    """번역 결과의 <i>(원문 이탤릭)를 <b>로 바꾸고, 번역금지 span은 벗긴다."""
    s = re.sub(r'<span translate="no">(.*?)</span>', r"\1", tr_html, flags=re.S)
    if italic_to_bold:
        return re.sub(r"<i>(.*?)</i>", r"<b>\1</b>", s, flags=re.S)
    return re.sub(r"</?i>", "", s)


def _pieces(blocks: list[dict], box_width: float = 360.0) -> list[str]:
    """번역된 블록 → 페이지 HTML 조각 목록(페이지가 넘칠 때 나누는 단위)."""
    out: list[str] = []
    for b in blocks:
        k = b["kind"]
        if k in ("header", "footnote"):
            continue
        tr = b.get("tr", b["html"])
        if k == "heading":
            if b.get("title"):
                out.append(f'<h1>{to_reading_html(tr)}</h1>')
            else:
                tag = "h2" if (b["text"].isupper() or b.get("centered")) else "h3"
                out.append(f"<{tag}>{to_reading_html(tr)}</{tag}>")
        elif k == "para":
            cls = "" if b.get("no_indent") else "para"
            if b.get("centered"):
                cls = "center"
            out.append(f'<p class="{cls}">{to_reading_html(tr)}</p>')
        elif k == "list":
            items = b.get("tr_items") or b["items"]
            out.append("<ul>" + "".join(f"<li>{to_reading_html(t)}</li>" for t in items) + "</ul>")
        elif k == "caption":
            out.append(f'<p class="cap">{to_reading_html(tr)}</p>')
        elif k == "note":
            out.append(f'<p class="note">{to_reading_html(tr)}</p>')
        elif k == "table":
            out += rotated_table.table_pieces(b, to_reading_html, box_width)
        elif k == "reference":
            # 참고문헌은 학술지명 이탤릭을 볼드로 바꾸지 않는다 (지저분해짐)
            out.append(f'<p class="ref">{to_reading_html(tr, italic_to_bold=False)}</p>')
        elif k == "figure" and b.get("img_name"):
            w, h = b["bbox"].width, b["bbox"].height
            out.append(f'<p class="fig"><img src="{b["img_name"]}" width="{w:.0f}" height="{h:.0f}"/></p>')
    foot = [b for b in blocks if b["kind"] == "footnote"]
    if foot:
        out.append("<hr/>" + "".join(
            f'<p class="fn">{to_reading_html(b.get("tr", b["html"]))}</p>' for b in foot))
    return out


def _label_html(text: str) -> str:
    return f'<p style="text-align:right;color:#888;font-size:8pt;margin:0">{esc(text)}</p>'


def printed_page_number(blocks: list[dict]) -> str | None:
    """러닝 헤더에서 학술지 인쇄 쪽수(예: 25)를 찾는다."""
    for b in blocks:
        if b["kind"] == "header":
            m = re.search(r"(?:^|\s)(\d{1,4})(?:\s|$)", b["text"])
            if m:
                return m.group(1)
    return None


def render_page(out: pymupdf.Document, src_doc: pymupdf.Document, pg: dict,
                note: str | None = None) -> int:
    """원문 페이지와 같은 크기의 번역 페이지를 out에 추가하고, 추가된 장수를 돌려준다.
    오른쪽 위에 'PDF p.4 · 인쇄 25쪽'처럼 원문 위치를 표시한다.
    note가 있으면(번역 실패·미번역) 원문 페이지를 그대로 넣고 안내 문구를 붙인다."""
    pno = pg["page_number"]
    src = src_doc[pno - 1]
    W, H = src.rect.width, src.rect.height
    archive = pymupdf.Archive(str(FONT_DIR))
    printed = printed_page_number(pg["blocks"])
    label = f"PDF p.{pno}" + (f" · 인쇄 {printed}쪽" if printed else "")

    # 회전된 표 페이지, 실패/미번역 페이지 → 원문 페이지를 그대로 (벡터 유지)
    if pg["mode"] != "text" or note:
        page = out.new_page(width=W, height=H)
        page.show_pdf_page(pymupdf.Rect(18, 40, W - 18, H - 10), src_doc, pno - 1)
        msg = note or ("가로 방향 표 페이지 — 원문 그대로" if pg["mode"] == "image" else "원문 그대로")
        page.insert_htmlbox(pymupdf.Rect(18, 14, W - 18, 38), _label_html(f"{label} · {msg}"),
                            css=CSS, archive=archive)
        return 1

    blocks = pg["blocks"]
    rects = [b["bbox"] for b in blocks if b["kind"] != "header" and not b["bbox"].is_empty] or [src.rect]
    left = max(min(r.x0 for r in rects) - 4, 30)
    right = min(max(r.x1 for r in rects) + 4, W - 30)
    box = pymupdf.Rect(left, HEADER_ZONE - 10, right, H - 36)

    for b in blocks:                           # 그림은 원문에서 잘라 PNG로
        if b["kind"] == "figure":
            name = f"fig_{pno}_{int(b['bbox'].y0)}_{int(b['bbox'].x0)}.png"
            pix = src.get_pixmap(clip=b["bbox"], dpi=FIG_DPI)
            archive.add(pix.tobytes("png"), name)
            b["img_name"] = name

    pieces = _pieces(blocks, box.width) or ['<p style="color:#999">(이 페이지에는 번역할 텍스트가 없습니다)</p>']
    css = CSS + f"body {{ font-size: {pg['body_size']:.1f}pt; }}"
    added = 0
    while pieces:
        # 남은 조각을 최대한 많이 담을 수 있는 개수 찾기 (전부 → 하나씩 줄이기)
        k = len(pieces)
        while k > 1:
            trial = pymupdf.open()
            spare, _ = trial.new_page(width=W, height=H).insert_htmlbox(
                box, "".join(pieces[:k]), css=css, archive=archive, scale_low=MIN_SCALE)
            trial.close()
            if spare >= 0:
                break
            k -= 1
        page = out.new_page(width=W, height=H)
        page.insert_htmlbox(box, "".join(pieces[:k]), css=css, archive=archive, scale_low=MIN_SCALE)
        page.insert_htmlbox(pymupdf.Rect(30, 26, W - 30, 50),
                            _label_html(label + (" (계속)" if added else "")), css=CSS, archive=archive)
        pieces = pieces[k:]
        added += 1
    return added


# ─────────────────────── 6. 텍스트 출력 ───────────────────────
def plain(html: str) -> str:
    s = re.sub(r"<[^>]+>", "", html)
    return s.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def page_text(pg: dict, translated: bool = True) -> str:
    """페이지 → 읽기용 일반 텍스트 (원문 또는 번역문)."""
    parts: list[str] = []
    for b in pg["blocks"]:
        k = b["kind"]
        if k == "header":
            continue
        if k == "figure":
            parts.append("[그림/표 — 원문 참조]" if translated else "[Figure/Table]")
        elif k == "list":
            items = (b.get("tr_items") or b["items"]) if translated else b["items"]
            parts.append("\n".join("• " + plain(t) for t in items))
        elif k == "table":
            items = (b.get("tr_items") or b["items"]) if translated else b["items"]
            lay = b["layout"]
            rows = []
            if lay.get("title"):
                rows.append(" ".join(plain(items[i]) for i in lay["title"]))
            for row in lay["rows"]:
                rows.append(" | ".join("; ".join(plain(items[i]) for i in cell) for cell in row))
            if lay.get("note") is not None:
                rows.append(plain(items[lay["note"]]))
            parts.append("\n".join(rows))
        else:
            parts.append(plain(b.get("tr", b["html"]) if translated else b["html"]))
    return "\n\n".join(parts)
