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
  table     표 — 가로로 돌려 놓은 표(rotated_table.py), 선 없는 글자 표(text_table.py).
            칸·항목 단위로 번역해 표로 다시 그림
  dialogue  면담·수업 대화문 ('Teacher: …') — 발화마다 한 줄
  meta      첫 쪽의 학술지명·DOI — 번역하지 않고 작게 표시
"""
from __future__ import annotations

import os
import re
import statistics
from pathlib import Path
from typing import Any

import pymupdf

import rotated_table
import text_table

# ─────────────────────────── 설정 ───────────────────────────
FONT_DIR = Path(__file__).parent / "fonts"
FONT_REGULAR = "NotoSansKR-Regular.ttf"   # Noto Sans CJK KR (SIL OFL). 다른 글꼴은 fonts/에 넣고 파일명만 변경
FONT_BOLD = "NotoSansKR-Bold.ttf"

LINE_HEIGHT = 1.32        # 번역문 기본 줄간격 (한글은 1.3 안팎이 원문 쪽수에 맞추면서도 읽기 편함)
FONT_BOOST = 1.08         # 같은 pt라도 한글이 작아 보여서 원문 본문 크기보다 조금 크게 시작
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
DOWNLOAD_NOTICE_RE = re.compile(r"(This content downloaded from|All use subject to|about\.jstor\.org/terms|"
                                r"Downloaded from|For personal use only|reproduced with permission)", re.I)
SPEAKER_RE = re.compile(r"^(?:[A-Z][A-Za-z.\-’']{0,20}(?: [A-Z][A-Za-z.\-’']{0,20}){0,2})(?: \[[^\]]{1,30}\])?:\s")
NUMBERED_HEADING_RE = re.compile(r"^\d{1,2}(?:\.\d{1,2}){0,3}\.?\s+[A-Z]")
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


SYMBOL_FONT_RE = re.compile(r"(AdvP\d|AdvPi|Pi\d|Symbol|Dingbat|Wingding|ZapfDing|MathematicalPi|UniversalPi)", re.I)
BOLD_NAME_RE = re.compile(r"(Bold|Black|Heavy|Semibold|Demi|\.B(?:I)?(?:\+\d+)?$|-B$)", re.I)
ITALIC_NAME_RE = re.compile(r"(Italic|Oblique|\.(?:B)?I(?:\+\d+)?$|-It$)", re.I)


def _is_bold(sp: dict) -> bool:
    return bool(sp["flags"] & 16) or bool(BOLD_NAME_RE.search(sp["font"]))


def _is_italic(sp: dict) -> bool:
    return bool(sp["flags"] & ITALIC_FLAG) or bool(ITALIC_NAME_RE.search(sp["font"]))


def _fix_symbol(t: str, sp: dict, first: bool) -> str:
    """기호 글꼴(Springer의 AdvP… 등)은 글자 코드가 엉뚱하게 잡힌다: • → '&', © → '#', · → ':'.
    위첨자로 올린 '.'도 주제어 구분점(·)이다."""
    core = t.strip()
    if SYMBOL_FONT_RE.search(sp["font"]):
        if core == "&":
            return t.replace("&", "•" if first else "·")
        if core == "#":
            return t.replace("#", "©")
        if core == ":":
            return t.replace(":", " ·")
    if core == "." and sp["flags"] & 1:
        return t.replace(".", " ·")
    return t


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

    sizes: list[float] = []
    seen_text = False
    for idx, s in enumerate(line["spans"]):
        t = _span_text(s, s["size"])
        if not t:
            continue
        t = _fix_symbol(t, s, first=not seen_text)
        seen_text = seen_text or bool(t.strip())
        max_size = max(max_size, s["size"])
        if not SYMBOL_FONT_RE.search(s["font"]):
            sizes += [s["size"]] * len(t.strip())
        e = esc(t)
        lead = e[: len(e) - len(e.lstrip())]
        is_sup = s["size"] < body * 0.8 and s["bbox"][3] < base_y - 1 and t.strip().isdigit()
        if is_sup:
            e = f"<sup>{e.strip()}</sup>"
        elif _is_italic(s) and len(t.strip()) >= ITALIC_MIN_CHARS:
            e = f"{lead}<i>{e.strip()}</i>"
        elif _is_bold(s) and len(t.strip()) >= 2 and mixed:
            e = f"{lead}<b>{e.strip()}</b>"
        elif idx == 0 and is_sans(s) and t.strip():
            # 본문과 다른 산세리프로 시작 → 'Describing data.' 같은 run-in 소제목
            sans = t
            if mixed:
                e = f"<b>{e}</b>"
        html_parts.append(e)
        plain_parts.append(t)
    main_size = statistics.median(sizes) if sizes else max_size
    return {"html": "".join(html_parts), "text": "".join(plain_parts),
            "x0": line["bbox"][0], "y0": line["bbox"][1], "size": main_size, "sans": sans,
            "bbox": pymupdf.Rect(line["bbox"]),
            "bold": all(_is_bold(sp) for sp in line["spans"]
                        if _span_text(sp, sp["size"]).strip() and not SYMBOL_FONT_RE.search(sp["font"]))}


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
    for t in ("i", "b", "u"):                   # 줄마다 나뉜 태그를 하나로
        html = html.replace(f"</{t}><{t}>", "")
        html = re.sub(rf"</{t}>\s+<{t}>", " ", html)
    return html, plain


def _page_is_image_backed(page: pymupdf.Page) -> bool:
    """쪽 전체가 한 장의 이미지인지 (스캔본 + OCR 글자층 PDF)."""
    area = page.rect.width * page.rect.height
    try:
        for img in page.get_images(full=True):
            for r in page.get_image_rects(img[0]):
                r = r & page.rect
                if r.width * r.height > area * 0.8:
                    return True
    except Exception:
        pass
    return False


def _ink_figure_regions(page: pymupdf.Page, raw: dict) -> list[pymupdf.Rect]:
    """스캔 쪽에서 그림 찾기: 글자가 없는 곳에 잉크가 뭉쳐 있으면 그림(그래프·화면 캡처 등).
    OCR 글자층의 줄 위치를 지우고 남은 잉크를 6pt 칸으로 묶어 이어진 덩어리를 찾는다."""
    import numpy as np

    dpi = 36
    k = 72 / dpi                                        # 1px = 2pt
    pix = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY)
    a = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.stride)[:, : pix.width]
    ink = a < 150
    # 글자 줄을 '전부' 지운 잉크: 진짜 그림은 글자 밖에도 선·점·면이 남고, 표나 OCR이 엉킨 글은 거의 안 남는다
    heights = sorted(pymupdf.Rect(l["bbox"]).height for b in raw["blocks"] for l in b.get("lines", []))
    line_h = heights[len(heights) // 2] if heights else 10

    def bogus(l: dict) -> bool:
        return pymupdf.Rect(l["bbox"]).height > line_h * 2.2

    def line_rect(b: dict, l: dict) -> pymupdf.Rect:
        """OCR 글자층은 줄 상자가 실제 인쇄된 글자보다 짧은 경우가 많다(JSTOR 스캔 등).
        본문 줄이면 같은 블록에서 가장 오른쪽 끝까지 늘려서 가린다."""
        r = pymupdf.Rect(l["bbox"])
        txt = "".join("".join(ch["c"] for ch in s_["chars"]) for s_ in l["spans"]).strip()
        if len(txt) >= 25:
            bx1 = max(pymupdf.Rect(q["bbox"]).x1 for q in b.get("lines", []))
            r.x1 = max(r.x1, bx1, r.x1 + (r.x1 - r.x0) * 0.15)
        return r

    resid = ink.copy()
    for b in raw["blocks"]:
        for l in b.get("lines", []):
            if bogus(l):
                continue
            r = line_rect(b, l)
            resid[max(0, int((r.y0 - 2) / k)):int((r.y1 + 2) / k) + 1,
                  max(0, int((r.x0 - 2) / k)):int((r.x1 + 2) / k) + 1] = False
    for b in raw["blocks"]:                             # 글자 줄은 지운다 (조금 넓게)
        for l in b.get("lines", []):
            # 그림 속 눈금 숫자나 OCR이 그림을 잘못 읽은 짧은 조각은 지우지 않는다 (그림의 일부)
            txt = "".join("".join(ch["c"] for ch in s_["chars"]) for s_ in l["spans"]).strip()
            letters = sum(ch.isalpha() for ch in txt)
            if len(txt) < 12 or letters < len(txt) * 0.55 or bogus(l):
                continue
            r = line_rect(b, l)
            x0, y0 = max(0, int((r.x0 - 2) / k)), max(0, int((r.y0 - 2) / k))
            x1, y1 = int((r.x1 + 2) / k) + 1, int((r.y1 + 2) / k) + 1
            ink[y0:y1, x0:x1] = False
    c = 3                                               # 3px = 6pt 칸
    h, w = ink.shape[0] // c, ink.shape[1] // c
    grid = ink[: h * c, : w * c].reshape(h, c, w, c).mean(axis=(1, 3)) > 0.06
    # 가까운 칸끼리 잇기 위해 두 칸(12pt)씩 넓힌다 — 흐린 상자그림·점그래프도 한 덩어리가 되게
    g = grid.copy()
    for _ in range(2):
        base = g.copy()
        g[1:, :] |= base[:-1, :]
        g[:-1, :] |= base[1:, :]
        g[:, 1:] |= base[:, :-1]
        g[:, :-1] |= base[:, 1:]
    seen = np.zeros_like(g)
    cands: list[tuple[pymupdf.Rect, float]] = []
    for yy in range(h):
        for xx in range(w):
            if not g[yy, xx] or seen[yy, xx]:
                continue
            stack = [(yy, xx)]
            seen[yy, xx] = True
            ys, xs, n = [yy, yy], [xx, xx], 0
            while stack:
                y, x = stack.pop()
                n += 1
                ys[0], ys[1] = min(ys[0], y), max(ys[1], y)
                xs[0], xs[1] = min(xs[0], x), max(xs[1], x)
                for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    ny, nx = y + dy, x + dx
                    if 0 <= ny < h and 0 <= nx < w and g[ny, nx] and not seen[ny, nx]:
                        seen[ny, nx] = True
                        stack.append((ny, nx))
            r = pymupdf.Rect((xs[0] + 2) * c * k, (ys[0] + 2) * c * k, (xs[1] - 1) * c * k, (ys[1] - 1) * c * k)
            # 가는 선(표 괘선, 밑줄)이나 작은 얼룩은 그림이 아니다
            if os.environ.get("INK_DEBUG"):
                print("cand", [round(v) for v in r], n)
            strip = r.width < 80 and r.height > r.width * 3
            if r.width >= 20 and r.height >= 40 and n >= 25 and not strip:
                py0, py1 = int(r.y0 / k), int(r.y1 / k)
                px0, px1 = int(r.x0 / k), int(r.x1 / k)
                total = int(ink[py0:py1, px0:px1].sum())
                outside = int(resid[py0:py1, px0:px1].sum())
                ratio = outside / total if total else 0.0
                if os.environ.get("INK_DEBUG"):
                    print("  ratio", [round(v) for v in r], round(ratio, 2))
                cands.append((r, ratio))
    # 그림 판정: 글자 밖 잉크 비율이 0.3 이상이면 그림. 0.15 이상인 조각은 옆에 나란히 붙은
    # 그림 조각이 있을 때만 그림으로 (여러 패널 그림에서 눈금 글자가 많은 패널)
    keep = [ratio >= 0.3 and r.width >= 50 for r, ratio in cands]
    changed = True
    while changed:
        changed = False
        for i, (r, ratio) in enumerate(cands):
            if keep[i] or ratio < 0.15:
                continue
            for j, (q, _) in enumerate(cands):
                if keep[j] and min(r.y1, q.y1) - max(r.y0, q.y0) >= 0.5 * min(r.height, q.height) \
                        and max(r.x0, q.x0) - min(r.x1, q.x1) <= 60:
                    keep[i] = changed = True
                    break
    rects = [r for (r, _), kp in zip(cands, keep) if kp]
    # 그림 둘레의 짧은 글(축 눈금, 범례, 그림 속 글자)도 그림에 포함한다
    out = []
    for r in rects:
        grown = pymupdf.Rect(r)
        for b in raw["blocks"]:
            for l in b.get("lines", []):
                lr = pymupdf.Rect(l["bbox"])
                txt = "".join(s_["text"] if "text" in s_ else "".join(ch["c"] for ch in s_["chars"])
                              for s_ in l["spans"]).strip()
                if len(txt) <= 30 and (lr & (r + (-14, -14, 14, 14))).is_valid and \
                        not (lr & (r + (-14, -14, 14, 14))).is_empty and not CAPTION_RE.match(txt):
                    grown |= lr
        out.append(grown)
    return out


FIG_CAPTION_RE = re.compile(r"^(FIGURE|Figure|Fig\.)\s*\d+")
# 캡션으로 볼 줄: 'Figure 4.' / 'Figure 4:' / 'FIGURE 4' / 'Fig. 4' / 줄에 'Figure'(번호)만 있는 경우
# ('Figure 4 presents …' 같은 본문 문장은 캡션이 아니다)
FIG_CAPTION_STRICT = re.compile(r"^\s*(FIGURE\s*\d+|Fig\.\s*\d+|Figure\s*\d+\s*[.:]|Figure\s*\d*\s*$)")


def _caption_anchored_regions(page: pymupdf.Page, raw: dict) -> list[pymupdf.Rect]:
    """스캔 쪽 그림 찾기 2: 'Figure n.' 캡션 바로 위, 위쪽 본문 문단이 끝나는 곳부터 캡션까지를 그림으로 본다.
    (학술지는 그림 캡션을 그림 아래에 둔다. 흐린 그래프도 놓치지 않는다)"""
    lines = []
    for b in raw["blocks"]:
        for l in b.get("lines", []):
            if abs(l["dir"][0] - 1) > 0.01:
                continue
            t = "".join("".join(ch["c"] for ch in s_["chars"]) for s_ in l["spans"]).strip()
            if t:
                lines.append((pymupdf.Rect(l["bbox"]), t, pymupdf.Rect(b["bbox"])))

    def prose(r: pymupdf.Rect, t: str) -> bool:
        return len(t) >= 30 and sum(ch.isalpha() for ch in t) >= len(t) * 0.7

    out = []
    caps = []
    for b in raw["blocks"]:
        bl = [l for l in b.get("lines", []) if abs(l["dir"][0] - 1) <= 0.01]
        texts = ["".join("".join(ch["c"] for ch in s_["chars"]) for s_ in l["spans"]).strip() for l in bl]
        for k, (l, t) in enumerate(zip(bl, texts)):
            # 줄 단위로 본다: OCR 글자층은 캡션을 그림 속 글자와 한 블록으로 묶기도 한다
            head = t if not re.fullmatch(r"(FIGURE|Figure|Fig\.)\s*", t) else \
                t + " " + (texts[k + 1] if k + 1 < len(texts) else "")
            # 본문 중간 줄이 우연히 'Figure 1.'로 시작하는 경우는 제외: 블록 첫 줄이거나 앞 줄이 짧을 때만
            starts_block = k == 0 or len(texts[k - 1]) < 30
            if starts_block and (FIG_CAPTION_STRICT.match(t) or FIG_CAPTION_STRICT.match(head)):
                caps.append((pymupdf.Rect(l["bbox"]), head))
                break
    for cap, t in caps:
        x0, x1 = cap.x0, cap.x1
        # 기본 위 경계: 러닝 헤더(쪽 맨 위 짧은 줄) 바로 아래
        head = [lr.y1 for lr, lt, _ in lines if lr.y1 < page.rect.height * 0.12]
        top = (max(head) + 3) if head else page.rect.height * 0.09
        # 캡션 위로 올라가며, 위아래로 이어진 본문 문단(2줄 이상)을 만나면 거기가 그림의 위 경계
        above = sorted([(lr, lt) for lr, lt, _ in lines if lr.y1 <= cap.y0 - 1 and
                        min(lr.x1, x1) - max(lr.x0, x0) > 0.5 * min(lr.width, x1 - x0)],
                       key=lambda z: -z[0].y1)
        for k, (lr, lt) in enumerate(above):
            if not prose(lr, lt):
                continue
            prev = [(qr, qt) for qr, qt in above[k + 1:]
                    if 0 <= lr.y0 - qr.y1 <= 8 and abs(qr.x0 - lr.x0) <= 6 and prose(qr, qt)]
            if prev:
                top = lr.y1 + 2
                break
        # 캡션이 쪽 가운데에 걸쳐 있으면 그림은 본문 전체 폭일 수 있다
        mid = page.rect.width / 2
        if x0 < mid - 40 and x1 > mid + 40:
            prose_x = [lr for lr, lt, _ in lines if prose(lr, lt)]
            if prose_x:
                x0 = min(x0, min(lr.x0 for lr in prose_x))
                x1 = max(x1, max(lr.x1 for lr in prose_x))
        region = pymupdf.Rect(x0 - 4, top, x1 + 4, cap.y0 - 2)
        if region.height >= 40 and region.width >= 50:
            out.append(region)
    return out


def _figure_regions(page: pymupdf.Page, raw: dict) -> list[pymupdf.Rect]:
    """그림·표·도식 영역: 이미지 블록 + 벡터 도형 묶음 (+ 스캔 쪽은 캡션 위 영역·잉크 덩어리). 가까운 것끼리 합친다."""
    rects: list[pymupdf.Rect] = []
    if _page_is_image_backed(page):
        try:
            anchored = _caption_anchored_regions(page, raw)
        except Exception:
            anchored = []
        try:
            ink = _ink_figure_regions(page, raw)
        except Exception:
            ink = []
        rects += anchored + ink        # 겹치는 것은 아래에서 하나로 합쳐진다
    area = page.rect.width * page.rect.height
    for b in raw["blocks"]:
        if b["type"] == 1:
            r = pymupdf.Rect(b["bbox"])
            if r.width >= MIN_FIG[0] and r.height >= MIN_FIG[1] and r.width * r.height < area * 0.8:
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
    # 나란히 놓인 조각(여러 패널로 된 그림)은 하나로: 높이가 절반 이상 겹치고 좌우 간격 60pt 이내
    merged = True
    while merged:
        merged = False
        for i in range(len(rects)):
            for j in range(i + 1, len(rects)):
                a, b = rects[i], rects[j]
                overlap = min(a.y1, b.y1) - max(a.y0, b.y0)
                gap = max(a.x0, b.x0) - min(a.x1, b.x1)
                if overlap >= 0.5 * min(a.height, b.height) and gap <= 60:
                    rects[i] = a | b
                    del rects[j]
                    merged = True
                    break
            if merged:
                break
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


_TESSDATA: str | None | bool = False


def _tessdata() -> str | None:
    """설치된 tesseract 언어 자료 폴더 (없으면 None)."""
    global _TESSDATA
    if _TESSDATA is False:
        _TESSDATA = None
        cands = [os.environ.get("TESSDATA_PREFIX", "")]
        try:
            cands.append(pymupdf.get_tessdata())
        except Exception:
            pass
        for root in ("/usr/share/tesseract-ocr", "/usr/share/tessdata", "/usr/local/share/tessdata"):
            for r, _, fs in os.walk(root):
                if "eng.traineddata" in fs:
                    cands.append(r)
        for c in cands:
            if c and os.path.exists(os.path.join(c, "eng.traineddata")):
                _TESSDATA = c
                break
    return _TESSDATA or None


_COMMON_WORDS = set("""a an the of to in on for and or as at by be is are was were it its this that these those
with from not no we our they their he she his her which who whom what when where how than then
there also can may might will would should could has have had do does did more most such each other""".split())


def _split_joined(word: str, vocab: set[str]) -> int | None:
    """'vehiclesfor' → 8 ('vehicles'+'for'): 붙어 읽힌 두 단어의 나눌 위치."""
    w = word.lower()
    if len(w) < 5 or not w.isalpha() or w in vocab:
        return None
    for i in range(len(w) - 1, 0, -1):
        a, b = w[:i], w[i:]
        if (a in vocab or a in _COMMON_WORDS) and (b in vocab or b in _COMMON_WORDS) and \
                (len(a) > 1 or a == "a") and len(b) > 1:
            return i
    return None


def _reocr_raw(page: pymupdf.Page, old_raw: dict) -> dict | None:
    """스캔 이미지 + OCR 글자층 PDF: 글자층이 이미지와 어긋나거나 줄 끝이 잘린 경우가 많다
    ('buildin', '80/6' ← building, 8%). 쪽 이미지를 tesseract로 다시 읽어 글자층을 바꾼다.
    tesseract가 없으면 None (원래 글자층 사용)."""
    td = _tessdata()
    if not td:
        return None
    try:
        tp = page.get_textpage_ocr(language="eng", dpi=300, full=True, tessdata=td)
        raw = page.get_text("rawdict", flags=pymupdf.TEXT_PRESERVE_IMAGES, textpage=tp)
    except Exception:
        return None
    chars = sum(len(s["chars"]) for b in raw["blocks"] for l in b.get("lines", []) for s in l["spans"])
    if chars < 200:
        return None
    _fix_drop_caps(raw)
    # 괘선·테두리를 글자로 읽은 조각('—_', '___') 제거
    for b in raw["blocks"]:
        for l in b.get("lines", []):
            for sp in l["spans"]:
                t = "".join(c["c"] for c in sp["chars"]).strip()
                if t and re.fullmatch(r"[—_~\-]*_[—_~\-]*", t):
                    sp["chars"] = [c for c in sp["chars"] if c["c"].isspace()]
    # tesseract 단어 = span. 단어 사이 공백이 빠진 곳('article'+'focuses')에 공백을 넣는다
    for b in raw["blocks"]:
        for l in b.get("lines", []):
            prev = None
            for sp in l["spans"]:
                if not sp["chars"]:
                    continue
                if prev is not None and not prev["chars"][-1]["c"].isspace() and not sp["chars"][0]["c"].isspace():
                    c0 = dict(sp["chars"][0]); c0["c"] = " "
                    sp["chars"].insert(0, c0)
                prev = sp
    layer_words = page.get_text("words")
    # 원래 글자층의 단어로 사전을 만들어, OCR이 붙여 읽은 단어를 나눈다 ('articlefocuses')
    vocab = {w.lower() for w in re.findall(r"[A-Za-z]+", " ".join(w[4] for w in layer_words))}
    for b in raw["blocks"]:
        for l in b.get("lines", []):
            for sp in l["spans"]:
                ch = sp["chars"]
                out: list[dict] = []
                i = 0
                while i < len(ch):
                    j = i
                    while j < len(ch) and ch[j]["c"].isalpha():
                        j += 1
                    if j > i:
                        word = "".join(c["c"] for c in ch[i:j])
                        cut = _split_joined(word, vocab)
                        out += ch[i:j]
                        if cut:
                            k = len(out) - (j - i) + cut
                            sp_c = dict(out[k - 1]); sp_c["c"] = " "
                            out.insert(k, sp_c)
                        i = j
                    else:
                        out.append(ch[i]); i += 1
                sp["chars"] = out
    _align_with_layer(raw, layer_words)
    _mark_bold_by_ink(page, raw)
    raw["_reocr"] = True
    return raw


def _fix_drop_caps(raw: dict) -> None:
    """문단 첫 글자를 크게 쓴 장식 대문자(drop cap): OCR이 둘째 줄 앞에 붙여 읽는다
    ('he mathematics …' / 'Tgoals for …'). 첫 줄 맨 앞으로 옮기고 줄 상자를 글자에 맞춰 다시 잡는다."""
    for b in raw["blocks"]:
        lines = b.get("lines") or []
        if len(lines) < 2:
            continue
        hs = [c["bbox"][3] - c["bbox"][1] for l in lines for sp in l["spans"] for c in sp["chars"]
              if c["c"].isalpha()]
        if len(hs) < 20:
            continue
        med = statistics.median(hs)
        bx0 = min(l["bbox"][0] for l in lines)
        for l in lines[1:3]:
            sp0 = next((sp for sp in l["spans"] if sp["chars"]), None)
            if sp0 is None:
                continue
            c0 = next((c for c in sp0["chars"] if not c["c"].isspace()), None)
            if c0 is None or not c0["c"].isupper() or c0["bbox"][0] > bx0 + 4 \
                    or c0["bbox"][3] - c0["bbox"][1] < med * 1.7:
                continue
            first = lines[0]
            fsp = next((sp for sp in first["spans"] if sp["chars"]), None)
            if fsp is None or not fsp["chars"][0]["c"].islower():
                continue
            sp0["chars"].remove(c0)
            h = fsp["chars"][0]
            w = (h["bbox"][2] - h["bbox"][0]) * 1.2
            c0 = dict(c0, bbox=(h["bbox"][0] - w, h["bbox"][1], h["bbox"][0], h["bbox"][3]),
                      origin=(h["bbox"][0] - w, h["origin"][1]))
            fsp["chars"].insert(0, c0)
            break
    for b in raw["blocks"]:
        for l in b.get("lines", []):
            boxes = [c["bbox"] for sp in l["spans"] for c in sp["chars"] if not c["c"].isspace()]
            if boxes:
                hs = sorted(bb[3] - bb[1] for bb in boxes)
                hmed = hs[len(hs) // 2]
                core = [bb for bb in boxes if bb[3] - bb[1] <= hmed * 1.5] or boxes
                l["bbox"] = (min(bb[0] for bb in boxes), min(bb[1] for bb in core),
                             max(bb[2] for bb in boxes), max(bb[3] for bb in core))


def _norm_tok(t: str) -> str:
    return t.lower().rstrip(".,;:")


def _pick_token(o: str, l: str, freq: dict[str, int]) -> str:
    """같은 자리의 OCR 단어(o)와 원래 글자층 단어(l) 중 믿을 만한 쪽.
    글자층: 줄 끝이 잘림('buildin'), %를 '0/6'으로 읽음. tesseract: 위첨자를 '?'로, n을 1로 읽음."""
    no, nl = _norm_tok(o), _norm_tok(l)
    if no == nl:
        return l                                   # 대소문자는 글자층이 더 정확 ('Strategies')
    if "%" in o and "%" not in l:
        return o
    if nl and no.startswith(nl):
        return o                                   # 글자층이 잘린 단어
    if re.search(r"[?>]", o) and not re.search(r"[?>]", l) and l[:1].isalnum():
        return l                                   # 위첨자 a, b를 ?로 읽은 경우
    os_, ls_ = o.lstrip("“”\"'>?"), l.lstrip("“”\"'")
    if len(ls_) > 3 and ls_[0].islower() and ls_[1:] == os_ and ls_[1:2].isupper():
        return ls_                                 # 앞 위첨자 a를 '“'로 읽은 경우 → 'aRepresentations'
    if freq.get(nl, 0) > freq.get(no, 0):
        return l                                   # 'Obio' → 'Ohio', '(1' → '(n'

    return o


def _align_with_layer(raw: dict, layer_words: list) -> None:
    """줄마다 원래 글자층의 같은 높이 단어들과 맞대어, 한 단어씩 어긋난 곳을 고친다."""
    import difflib
    from collections import Counter

    freq: Counter = Counter(_norm_tok(w[4]) for w in layer_words)
    for b in raw["blocks"]:
        for l in b.get("lines", []):
            for sp in l["spans"]:
                t = "".join(c["c"] for c in sp["chars"]).strip()
                if t:
                    freq[_norm_tok(t)] += 1
    # 같은 높이의 OCR 줄(표에서는 칸마다 따로 잡힘)을 한 행으로 묶어 글자층의 같은 행과 맞댄다
    rows: list[list[dict]] = []
    for l in sorted((l for b in raw["blocks"] for l in b.get("lines", [])),
                    key=lambda l: (l["bbox"][1] + l["bbox"][3]) / 2):
        yc = (l["bbox"][1] + l["bbox"][3]) / 2
        if rows and abs(yc - rows[-1][0]["_yc"]) <= (l["bbox"][3] - l["bbox"][1]) * 0.4:
            l["_yc"] = rows[-1][0]["_yc"]
            rows[-1].append(l)
        else:
            l["_yc"] = yc
            rows.append([l])
    for row in rows:
        row.sort(key=lambda l: l["bbox"][0])
        y0 = min(l["bbox"][1] for l in row)
        y1 = max(l["bbox"][3] for l in row)
        x0 = min(l["bbox"][0] for l in row)
        x1 = max(l["bbox"][2] for l in row)
        spans = [sp for l in row for sp in l["spans"] if "".join(c["c"] for c in sp["chars"]).strip()]
        if True:
            if not spans:
                continue
            lw = [w for w in layer_words
                  if y0 - 2 <= (w[1] + w[3]) / 2 <= y1 + 2 and w[2] > x0 - 30 and w[0] < x1 + 60]
            if not lw:
                continue
            # 글자층 단어도 줄(높이)별로 묶은 뒤 왼쪽→오른쪽
            lw.sort(key=lambda w: (w[1] + w[3]) / 2)
            lrows: list[list] = []
            for w in lw:
                if lrows and abs((w[1] + w[3]) / 2 - (lrows[-1][0][1] + lrows[-1][0][3]) / 2) <= (w[3] - w[1]) * 0.4:
                    lrows[-1].append(w)
                else:
                    lrows.append([w])
            lw = [w for r in lrows for w in sorted(r, key=lambda w: w[0])]
            ot = ["".join(c["c"] for c in sp["chars"]).strip() for sp in spans]
            lt = [w[4] for w in lw]
            sm = difflib.SequenceMatcher(a=[_norm_tok(t) for t in ot], b=[_norm_tok(t) for t in lt],
                                         autojunk=False)
            for op, a0, a1, b0, b1 in sm.get_opcodes():
                pairs = []
                if op == "equal":
                    pairs = list(zip(range(a0, a1), range(b0, b1)))
                elif op == "replace" and a1 - a0 == b1 - b0:
                    pairs = list(zip(range(a0, a1), range(b0, b1)))
                for ai, bi in pairs:
                    new = _pick_token(ot[ai], lt[bi], freq)
                    if new != ot[ai]:
                        _set_span_text(spans[ai], new)


def _set_span_text(sp: dict, text: str) -> None:
    ch = [c for c in sp["chars"] if c["c"].strip()]
    if not ch:
        return
    lead = [c for c in sp["chars"][: sp["chars"].index(ch[0])]]
    x0, y0, _, y1 = ch[0]["bbox"]
    x1 = ch[-1]["bbox"][2]
    w = (x1 - x0) / max(1, len(text))
    out = []
    for i, c in enumerate(text):
        d = dict(ch[min(i, len(ch) - 1)])
        d["c"] = c
        d["bbox"] = (x0 + w * i, y0, x0 + w * (i + 1), y1)
        d["origin"] = (x0 + w * i, d["origin"][1])
        out.append(d)
    sp["chars"] = lead + out


def _mark_bold_by_ink(page: pymupdf.Page, raw: dict) -> None:
    """OCR 글자에는 글꼴 정보가 없다 → 획 굵기(가로 방향 검은 픽셀 연속 길이)로 굵은 글씨를 찾는다.
    비슷한 높이의 단어들보다 1.4배 이상 굵으면 bold (표의 굵은 수치, 제목)."""
    import numpy as np

    dpi = 200
    k = dpi / 72
    pix = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY)
    a = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.stride)[:, : pix.width] < 128
    items = []
    for b in raw["blocks"]:
        for l in b.get("lines", []):
            for sp in l["spans"]:
                t = "".join(c["c"] for c in sp["chars"]).strip()
                if len(t) < 2:
                    continue
                x0, y0, x1, y1 = [int(v * k) for v in sp["bbox"]]
                crop = a[max(0, y0):y1, max(0, x0):x1]
                if crop.size == 0:
                    continue
                d = np.diff(np.pad(crop.astype(np.int8), ((0, 0), (1, 1))), axis=1)
                runs = (np.nonzero(d == -1)[1] - np.nonzero(d == 1)[1])
                if len(runs) < 8:
                    continue
                digit = sum(ch.isdigit() for ch in t) * 2 >= len(t)
                items.append((sp, sp["bbox"][3] - sp["bbox"][1], float(runs.mean()), digit))
    if len(items) < 10:
        return
    allw = [w for _, _, w, _ in items]
    for sp, h, w, dg in items:
        # 숫자는 획이 가늘어 글자와 따로 비교한다
        sim = [w2 for _, h2, w2, d2 in items if abs(h2 - h) <= h * 0.25 and d2 == dg]
        ref = statistics.median(sim) if len(sim) >= 5 else statistics.median(allw)
        if w >= ref * 1.4:
            sp["flags"] |= 16


def is_scanned(page: pymupdf.Page, raw: dict) -> bool:
    """글자는 거의 없고 큰 이미지가 쪽을 덮고 있으면 스캔 쪽.
    (스캔본에 OCR 글자층이 있어도 글자가 엉망인 경우가 많아, 글자층이 빈약하면 스캔으로 본다)"""
    chars = sum(len(s["chars"]) for b in raw["blocks"] for l in b.get("lines", []) for s in l["spans"])
    area = page.rect.width * page.rect.height
    img_area = 0.0
    for b in raw["blocks"]:
        if b["type"] == 1:
            r = pymupdf.Rect(b["bbox"]) & page.rect
            img_area += r.width * r.height
    return chars < 200 and img_area > area * 0.5


FOOTER_BAND = 70
_ROT_DOCS: dict[str, pymupdf.Document] = {}


def _word_quality(raw: dict) -> float:
    """OCR 결과가 실제 영어 단어처럼 보이는 비율 (돌아간 쪽을 읽으면 기호 범벅이 된다)."""
    words = re.findall(r"\S+", " ".join("".join(c["c"] for c in sp["chars"])
                                         for b in raw["blocks"] for l in b.get("lines", []) for sp in l["spans"]))
    if not words:
        return 0.0
    good = sum(1 for w in words if re.fullmatch(r"[(\"“]?[A-Za-z][a-z]+[.,;:)\"”]?|\d+%?|\(\d+\)", w))
    return good / len(words)


def _extract_rotated_scan(page: pymupdf.Page) -> dict[str, Any] | None:
    """스캔 쪽을 90°/270° 돌린 이미지로 다시 읽어, 더 그럴듯한 방향을 고른다.
    결과는 돌린 좌표계의 블록이며, 조판할 때 가로 쪽에 짠 뒤 다시 돌려 붙인다(render_page)."""
    W, H = page.rect.width, page.rect.height
    best = None
    for rot in (90, 270):
        tmp = pymupdf.open()
        tp = tmp.new_page(width=H, height=W)
        # 아래쪽 다운로드 안내 띠는 돌리면 세로 글자 쓰레기가 되므로 빼고 돌린다
        tp.show_pdf_page(tp.rect, page.parent, page.number, rotate=rot,
                         clip=pymupdf.Rect(0, 0, W, H - FOOTER_BAND))
        pix = tp.get_pixmap(dpi=300)
        img = pymupdf.open()
        ip = img.new_page(width=H, height=W)
        ip.insert_image(ip.rect, pixmap=pix)
        try:
            raw = _reocr_raw(ip, ip.get_text("rawdict"))
        except Exception:
            raw = None
        q = _word_quality(raw) if raw else 0.0
        if q >= 0.6 and (best is None or q > best[0]):
            best = (q, rot, img)
    if best is None:
        return None
    q, rot, img = best
    pg = extract_page(img[0], _in_rotated=True)
    if pg.get("mode") != "text":
        return None
    pg["rotated"] = rot
    key = f"rot{len(_ROT_DOCS)}_{id(img)}"
    _ROT_DOCS[key] = img                     # 문서 객체는 복사(deepcopy)할 수 없어 따로 보관
    pg["_rot_key"] = key
    return pg


def extract_page(page: pymupdf.Page, _in_rotated: bool = False) -> dict[str, Any]:
    """원문 페이지 1장 → {"mode", "body_size", "blocks"}.

    mode = "text"  : 일반 페이지 (블록 단위로 번역)
           "image" : 가로로 돌려 앉힌 표처럼 글자 대부분이 회전된 페이지 → 원문을 그대로 넣는다
           "scan"  : 글자가 사진으로 된 스캔 쪽 → OCR(build_ocr_page) 후 "text"가 된다
    """
    raw = page.get_text("rawdict", flags=pymupdf.TEXT_PRESERVE_IMAGES)
    if _in_rotated:
        raw = _reocr_raw(page, raw) or raw
    elif _page_is_image_backed(page) and not is_scanned(page, raw):
        new = _reocr_raw(page, raw)
        if new is not None and _word_quality(new) < 0.5:
            # 가로로 돌려 인쇄한 스캔 표: 돌려서 다시 읽어 본다
            rot = _extract_rotated_scan(page)
            if rot is not None:
                return rot
        raw = new or raw
    body = body_size_of(raw)
    H = page.rect.height
    if is_scanned(page, raw):
        return {"mode": "scan", "body_size": 10.0, "blocks": []}
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

    # 줄 단위로 먼저 읽는다 (선 없는 표를 찾으려면 블록을 가로질러 줄을 봐야 함)
    block_lines: list[tuple[pymupdf.Rect, list[dict]]] = []
    for b in raw["blocks"]:
        if b["type"] != 0:
            continue
        mixed = any(not any(f in sp["font"].lower() for f in SANS_FONTS)
                    for l in b["lines"] for sp in l["spans"]
                    if "".join(c["c"] for c in sp["chars"]).strip())
        lines = [_line(l, body, mixed) for l in b["lines"] if abs(l["dir"][0] - 1) < 0.01]
        lines = [x for x in lines if x["text"].strip()]
        for x in lines:
            x["id"] = id(x)
        if lines:
            block_lines.append((pymupdf.Rect(b["bbox"]), lines))

    figs = _grow_figures(figs, block_lines, page.rect)
    blocks: list[dict[str, Any]] = [_figure_block(r, raw, body, page) for r in figs]

    def in_fig(r: pymupdf.Rect) -> bool:
        c = pymupdf.Point((r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2)
        return any(f.contains(c) for f in figs)

    try:
        tables = text_table.find_text_tables(
            [l for _, ls in block_lines for l in ls if not in_fig(l["bbox"])], page.rect)
    except Exception:
        tables = []
    table_ids = {i for t in tables for i in t.pop("line_ids")}
    blocks += tables

    for bbox, lines in block_lines:
        if table_ids:
            lines = [l for l in lines if l["id"] not in table_ids]
            if not lines:
                continue
            bbox = pymupdf.Rect(lines[0]["bbox"])
            for l in lines[1:]:
                bbox |= l["bbox"]
        plain_all = " ".join(x["text"].strip() for x in lines)

        # 그림 영역 안의 글자(축 이름, 범례, 표 칸)는 그림의 일부 → 번역하지 않는다.
        # 단, 캡션은 그림 영역에 붙어 있어도 따로 번역한다.
        center = pymupdf.Point((bbox.x0 + bbox.x1) / 2, (bbox.y0 + bbox.y1) / 2)
        if any(f.contains(center) for f in figs) and not CAPTION_RE.match(plain_all):
            continue

        # 블록 글자 크기 = 줄들의 대표 크기 (기호 하나가 크다고 제목이 되지 않게)
        size = statistics.median(x["size"] for x in lines)
        plains = [x["text"].strip() for x in lines]

        # 대화문: 'Teacher: …', 'Anna: …'처럼 화자 이름으로 시작하는 줄이 여럿이면 한 줄씩 나눈다
        if sum(bool(SPEAKER_RE.match(p)) for p in plains) >= 2:
            turns: list[list[dict]] = []
            for ln in lines:
                if SPEAKER_RE.match(ln["text"].strip()) or not turns:
                    turns.append([])
                turns[-1].append(ln)
            joined = [join_lines(t) for t in turns]
            blocks.append({"kind": "dialogue", "bbox": bbox, "size": size,
                           "items": [_protect(h) for h, _ in joined],
                           "html": "", "text": "\n".join(p for _, p in joined)})
            continue

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
        if (bbox.y1 < max(HEADER_ZONE, H * 0.115) or bbox.y0 > H - FOOTER_ZONE) and len(plain) < 100:
            kind = "header"
        elif DOWNLOAD_NOTICE_RE.search(plain) or (re.fullmatch(r"\s*\d{1,4}\s*", plain) and bbox.y0 > H * 0.8):
            kind = "header"             # 'This content downloaded from …'(JSTOR), 쪽 아래 쪽번호
        elif CAPTION_RE.match(plain) and (plain[:5].isupper() or size < body * 0.95):
            kind = "caption"
        elif size < body * 0.85 and bbox.y0 > H * 0.45:
            kind = "footnote"           # 각주, 교신저자 안내 등 페이지 아래 작은 글씨
        elif len(plain) < 120 and size > body * 1.15:
            kind = "heading"            # 큰 글씨 = 제목 (문장부호로 끝나도)
        elif len(plain) < 80 and not SENTENCE_END_RE.search(plain) and (
            plain.isupper() or lines[0]["sans"].strip() == plain or all(l.get("bold") for l in lines)
            or NUMBERED_HEADING_RE.match(plain)
        ):
            kind = "heading"
        centered = (abs((bbox.x0 + bbox.x1) / 2 - page.rect.width / 2) < 15
                    and bbox.width < page.rect.width * 0.7 and len(plain) < 120)
        blocks.append({"kind": kind, "bbox": bbox, "size": size, "html": _protect(html),
                       "text": plain, "lines": lines, "centered": centered,
                       "title": kind == "heading" and size > body * 1.3})

    # 작은 글씨라도 그 아래에 본문 문단이 있으면 각주가 아니다 (그림 옆 대화문, 표 주석 등)
    body_tops = [b["bbox"].y0 for b in blocks if b["kind"] == "para" and b["size"] >= body * 0.95]
    for b in blocks:
        if b["kind"] == "footnote" and not re.match(r"^(\d+|\*|†)", b["text"]) \
                and any(y > b["bbox"].y1 for y in body_tops):
            b["kind"] = "note"
        elif b["kind"] == "para" and b["size"] < body * 0.85:
            b["kind"] = "note"

    _fix_kinds(blocks, page.rect, body)
    ordered = sort_reading_order(blocks, page.rect)
    ordered = _merge_continuations(ordered)
    _carry_across_columns(ordered, page.rect)
    ordered = _merge_caption_tail(_merge_headings(ordered))
    return {"mode": "text", "body_size": body, "blocks": _split_byline(ordered, page.rect)}


TITLE_CASE_SMALL = {"a", "an", "the", "of", "and", "or", "in", "on", "for", "to", "by", "with", "from", "at", "as"}


def _is_title_case(t: str) -> bool:
    words = re.findall(r"[A-Za-z][A-Za-z'’-]*", t)
    return bool(words) and words[0][0].isupper() and \
        all(w[0].isupper() or w.lower() in TITLE_CASE_SMALL for w in words)


NUMERIC_LABEL_RE = re.compile(r"^[\d\s.,%()=<>+\-–—/:*n]*$")


def _grow_figures(figs: list[pymupdf.Rect], block_lines: list, page_rect: pymupdf.Rect) -> list[pymupdf.Rect]:
    """그림 가장자리에 붙은 짧은 글자(축 눈금 '100%', 막대 위 '82%(n=37)', 범주명)가 그림 밖으로
    빠지지 않게 그림 영역을 넓힌다. 캡션·쪽 머리글 띠·긴 본문은 넣지 않는다."""
    H = page_rect.height
    figs = [pymupdf.Rect(f) for f in figs]
    taken: set[int] = set()
    changed = True
    while changed:
        changed = False
        for k, (bbox, lines) in enumerate(block_lines):
            if k in taken:
                continue
            text = " ".join(l["text"].strip() for l in lines)
            if len(text) > 45 or CAPTION_RE.match(text) or DOWNLOAD_NOTICE_RE.search(text) \
                    or bbox.y1 < 62 or bbox.y0 > H - 70:
                continue
            for fi, f in enumerate(figs):
                if f.contains(bbox):
                    break
                near = pymupdf.Rect(f.x0 - 8, f.y0 - 8, f.x1 + 8, f.y1 + 8)
                xov = min(bbox.x1, f.x1) - max(bbox.x0, f.x0)
                if near.intersects(bbox) and xov >= bbox.width * 0.6:
                    figs[fi] = pymupdf.Rect(f) | bbox
                    taken.add(k)
                    changed = True
                    break
    return figs


FIG_NUMBERISH_RE = re.compile(r"\(\s*[nN]\s*=|%")


def _figure_ocr_words(page: pymupdf.Page, clip: pymupdf.Rect) -> set[str] | None:
    """그림 영역만 400dpi로, 망점(회색 음영)을 흐리게 한 뒤 흑백으로 바꿔 다시 읽은 단어들."""
    import numpy as np

    td = _tessdata()
    if not td:
        return None
    try:
        dpi = 400
        pix = page.get_pixmap(dpi=dpi, clip=clip, colorspace=pymupdf.csGRAY)
        a = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.stride)[:, : pix.width].astype(np.float32)
        k = 3
        pad = np.pad(a, k // 2, mode="edge")
        acc = np.zeros_like(a)
        for i in range(k):
            for j in range(k):
                acc += pad[i:i + a.shape[0], j:j + a.shape[1]]
        bw = np.where(acc / (k * k) < 100, 0, 255).astype(np.uint8)
        pm = pymupdf.Pixmap(pymupdf.csGRAY, pix.width, pix.height, bw.tobytes(), False)
        doc = pymupdf.open()
        pg = doc.new_page(width=clip.width, height=clip.height)
        pg.insert_image(pg.rect, pixmap=pm)
        tp = pg.get_textpage_ocr(language="eng", dpi=dpi, full=True, tessdata=td)
        return {w.lower() for w in re.findall(r"[A-Za-z]{2,}", pg.get_text(textpage=tp))}
    except Exception:
        return None


def _figure_block(r: pymupdf.Rect, raw: dict, body: float, page: pymupdf.Page | None = None) -> dict[str, Any]:
    """그림 블록. 그림 안의 글자(범주명·범례·도식 상자 글자)는 단어 단위로 모아 문구로 묶고
    번역 대상으로 둔다 (그림은 원본 그대로, 같은 쪽에 '원문 | 번역' 대응표). 숫자·눈금·'82%(n=37)'은 제외."""
    words: list[tuple[pymupdf.Rect, str]] = []
    for b in raw["blocks"]:
        for l in b.get("lines", []):
            if abs(l["dir"][0] - 1) > 0.01:
                continue
            for sp in l["spans"]:
                cur: list[dict] = []
                for ch in sp["chars"] + [{"c": " ", "bbox": (0, 0, 0, 0)}]:
                    if ch["c"].isspace():
                        if cur:
                            bb = pymupdf.Rect(cur[0]["bbox"])
                            for x in cur[1:]:
                                bb |= pymupdf.Rect(x["bbox"])
                            if r.contains(pymupdf.Point((bb.x0 + bb.x1) / 2, (bb.y0 + bb.y1) / 2)):
                                words.append((bb, "".join(x["c"] for x in cur)))
                        cur = []
                    else:
                        cur.append(ch)
    words.sort(key=lambda w: ((w[0].y0 + w[0].y1) / 2, w[0].x0))
    # 1) 같은 줄에서 가까운 단어끼리 → 줄 조각
    rows: list[list] = []
    for w in words:
        yc = (w[0].y0 + w[0].y1) / 2
        if rows and abs((rows[-1][0][0].y0 + rows[-1][0][0].y1) / 2 - yc) < w[0].height * 0.5:
            rows[-1].append(w)
        else:
            rows.append([w])
    runs: list[list] = []
    for row in rows:
        row.sort(key=lambda w: w[0].x0)
        start = len(runs)
        for w in row:
            if len(runs) > start and 0 <= w[0].x0 - runs[-1][-1][0].x1 < max(w[0].height, 1) * 0.6:
                runs[-1].append(w)
            else:
                runs.append([w])

    def span(ws: list) -> pymupdf.Rect:
        r_ = pymupdf.Rect(ws[0][0])
        for w in ws[1:]:
            r_ |= w[0]
        return r_

    def cx(ws: list) -> float:
        r_ = span(ws)
        return (r_.x0 + r_.x1) / 2

    # 범주명은 보통 가운데 정렬: 붙어 읽힌 이웃 범주('performance'+'pressure for')는
    # 위아래 줄 조각의 가운데와 맞춰 나눈다
    out_runs: list[list] = []
    for run in runs:
        cs = [cx(o) for o in runs if o is not run]
        best = None
        if len(run) > 1 and not any(abs(cx(run) - c_) < 6 for c_ in cs):
            for k in range(1, len(run)):
                if any(abs(cx(run[:k]) - c_) < 6 for c_ in cs) and any(abs(cx(run[k:]) - c_) < 6 for c_ in cs):
                    best = k
                    break
        out_runs += [run[:best], run[best:]] if best else [run]
    pieces = [[span(r_), " ".join(w[1] for w in r_)] for r_ in out_runs]
    # 2) 위아래로 붙고 가운데가 맞는 줄 조각 → 한 문구 (여러 줄 범주명)
    pieces.sort(key=lambda p: (p[0].y0, p[0].x0))
    groups: list[list] = []
    for bb, t in pieces:
        for g in groups:
            gb = g[0]
            xov = min(bb.x1, gb.x1) - max(bb.x0, gb.x0)
            if -bb.height * 0.5 <= bb.y0 - gb.y1 <= bb.height * 0.9 and xov > min(bb.width, gb.width) * 0.5:
                g[0] = gb | bb
                g[1] += " " + t
                break
        else:
            groups.append([pymupdf.Rect(bb), t])
    groups.sort(key=lambda g: (round(g[0].y0 / 12), g[0].x0))
    # 스캔 그림: 잡티를 없앤 고해상도 이미지로 한 번 더 읽어, 두 번 다 같은 단어로 읽힌 문구만 쓴다
    check = _figure_ocr_words(page, r) if raw.get("_reocr") and page is not None else None
    items, boxes, unsure = [], [], []
    for bb, t in groups:
        t = t.strip()
        if NUMERIC_LABEL_RE.match(t) or FIG_NUMBERISH_RE.search(t) or len(re.findall(r"[A-Za-z]", t)) < 2 \
                or CAPTION_RE.match(t):
            continue
        toks = [w.lower() for w in re.findall(r"[A-Za-z]{2,}", t)]
        good = sum(1 for w in toks if check is None or w in check)
        if not toks or good < len(toks) * 0.8 or not re.search(r"[A-Za-z]{3,}", t):
            unsure.append([round(x) for x in bb])      # 판독이 불확실한 문구: 추측해서 번역하지 않는다
            continue
        items.append(_protect(esc(t)))
        boxes.append(bb)
    blk = {"kind": "figure", "bbox": r, "size": body, "html": "", "text": "\n".join(items),
           "items": items, "item_boxes": boxes}
    if unsure:
        blk["review"] = f"그림 속 문구 {len(unsure)}개는 글자를 확실히 읽지 못해 번역하지 않음 — 원문 그림 참조 (위치 {unsure[:6]})"
    return blk


def _fix_kinds(blocks: list[dict], page_rect: pymupdf.Rect, body: float) -> None:
    """글꼴 정보가 없거나 OCR로 블록이 잘게 나뉜 쪽에서 종류를 바로잡는다.
    - 쪽 위 머리글 구역에 걸린 '본문 폭의 줄'이 바로 아래 문단과 붙어 있으면 본문 (쪽 첫 줄)
    - 위아래가 비어 있는 짧은 Title Case 한 줄('Conceptual Framework')은 소제목"""
    W = page_rect.width
    figs = [b["bbox"] for b in blocks if b["kind"] in ("figure", "table")]
    for b in blocks:
        if b["kind"] in ("para", "note", "heading") and CAPTION_RE.match(b["text"]) and any(
                -4 <= b["bbox"].y0 - f.y1 <= 40 or -4 <= f.y0 - b["bbox"].y1 <= 40 for f in figs):
            b["kind"] = "caption"
    for b in blocks:
        t = b.get("text", "").replace(" ", "")
        if b["kind"] not in ("figure", "table") and 0 < len(t) <= 8 and len(set(t)) <= 2 and not any(ch.isdigit() for ch in t):
            b["kind"] = "header"                 # 괘선을 글자로 잘못 읽은 잡음('eee', '———')
            b["noise"] = True
    texty = [b for b in blocks if b["kind"] not in ("figure", "table")]
    for b in texty:
        r = b["bbox"]
        below = [o for o in texty if o is not b and -2 <= o["bbox"].y0 - r.y1 <= b["size"] * 0.9
                 and min(o["bbox"].x1, r.x1) - max(o["bbox"].x0, r.x0) > r.width * 0.5]
        above = [o for o in texty if o is not b and -2 <= r.y0 - o["bbox"].y1 <= b["size"] * 0.9
                 and min(o["bbox"].x1, r.x1) - max(o["bbox"].x0, r.x0) > r.width * 0.5]
        if b["kind"] == "header" and r.y1 < page_rect.height * 0.3 and r.width > W * 0.6 and below \
                and not DOWNLOAD_NOTICE_RE.search(b["text"]):
            b["kind"] = "para"
        elif b["kind"] in ("para", "note") and len(b.get("lines", [])) == 1 and len(b["text"]) < 70 \
                and not SENTENCE_END_RE.search(b["text"]) and _is_title_case(b["text"]) \
                and not below and not above and b["size"] >= body * 0.8:
            b["kind"] = "heading"
    # 한 단 쪽에서 좌우가 함께 들여 쓴 여러 줄 문단 = 긴 인용문
    paras = [b for b in blocks if b["kind"] == "para" and len(b.get("lines", [])) >= 3]
    if paras and sum(b["bbox"].width > W * 0.6 for b in paras) >= len(paras) * 0.6:
        L = statistics.median(b["bbox"].x0 for b in paras)
        R = statistics.median(b["bbox"].x1 for b in paras)
        for b in blocks:
            if b["kind"] == "para" and len(b.get("lines", [])) >= 2 and b["bbox"].x0 > L + 12 \
                    and b["bbox"].x1 < R - 12 and not b.get("centered"):
                b["quote"] = True


def _merge_caption_tail(blocks: list[dict]) -> list[dict]:
    """두 줄 표 제목의 둘째 줄('to Implementation')이 따로 잡혔으면 캡션에 붙인다."""
    out: list[dict] = []
    for b in blocks:
        prev = out[-1] if out else None
        if prev and prev["kind"] == "caption" and b["kind"] in ("para", "heading", "note") \
                and len(b["text"]) < 90 and b.get("centered") \
                and -2 <= b["bbox"].y0 - prev["bbox"].y1 < b["size"] * 0.9:
            prev["html"] += " " + b["html"]
            prev["text"] += " " + b["text"]
            prev["bbox"] |= b["bbox"]
            prev["lines"] = prev.get("lines", []) + b.get("lines", [])
            continue
        out.append(b)
    return out


def _split_byline(blocks: list[dict], page_rect: pymupdf.Rect) -> list[dict]:
    """논문 제목 아래 저자·소속(가운데 정렬 짧은 줄들)은 줄마다 따로 둔다.
    OCR이 '이름 + 소속'을 한 블록으로 묶으면 번역이 'Ohio University Marjorie …'처럼 엉킨다."""
    ti = next((i for i, b in enumerate(blocks) if b["kind"] == "heading" and b.get("title")), None)
    if ti is None:
        return blocks
    out = blocks[: ti + 1]
    i = ti + 1
    W = page_rect.width
    t = out[-1]
    # 제목 마지막 줄('Classrooms')이 따로 잡혔으면 제목에 붙인다
    while i < len(blocks) and blocks[i]["kind"] in ("para", "heading", "note") \
            and len(blocks[i].get("lines") or []) == 1 and blocks[i]["size"] >= t["size"] * 0.85 \
            and -2 <= blocks[i]["bbox"].y0 - t["bbox"].y1 < t["size"] * 1.2:
        b = blocks[i]
        t["html"] += " " + b["html"]
        t["text"] += " " + b["text"]
        t["bbox"] |= b["bbox"]
        t["lines"] = t.get("lines", []) + b.get("lines", [])
        i += 1
    t = out[-1]
    # 제목 마지막 줄('Classrooms')이 따로 잡혔으면 제목에 붙인다
    while i < len(blocks) and blocks[i]["kind"] in ("para", "heading", "note") \
            and len(blocks[i].get("lines") or []) == 1 and blocks[i]["size"] >= t["size"] * 0.85 \
            and -2 <= blocks[i]["bbox"].y0 - t["bbox"].y1 < t["size"] * 1.2:
        b = blocks[i]
        t["html"] += " " + b["html"]
        t["text"] += " " + b["text"]
        t["bbox"] |= b["bbox"]
        t["lines"] = t.get("lines", []) + b.get("lines", [])
        i += 1
    while i < len(blocks):
        b = blocks[i]
        cx = (b["bbox"].x0 + b["bbox"].x1) / 2
        lines = b.get("lines") or []
        if b["kind"] in ("para", "heading", "note") and lines and abs(cx - W / 2) < 30 \
                and all(len(l["text"]) < 70 for l in lines) and b["bbox"].width < W * 0.75:
            for l in lines:
                t = l["text"].strip()
                out.append({"kind": "para", "bbox": pymupdf.Rect(l["bbox"]), "size": l["size"],
                            "html": _protect(l["html"].strip()), "text": t, "lines": [l],
                            "centered": True, "no_indent": True, "title": False,
                            "byline": "name" if PERSON_NAME_RE.match(t) and not AFFIL_RE.search(t) else "affil"})
            i += 1
            continue
        break
    # 저자·소속 바로 다음의 긴 문단 = 초록 (원문에 'Abstract' 제목이 없어도 서식으로만 구분)
    if i > ti + 1 and i < len(blocks) and blocks[i]["kind"] == "para" and len(blocks[i]["text"]) > 200:
        blocks[i]["abstract"] = True
    return out + blocks[i:]


PERSON_NAME_RE = re.compile(r"^(?:[A-Z][a-zA-Z'’\-]+\.?|[A-Z]\.)(?:\s+(?:[A-Z][a-zA-Z'’\-]+\.?|[A-Z]\.|de|van|von|da)){1,4}$")
AFFIL_RE = re.compile(r"Universit|College|Institut|Center|Centre|School|Department|Laboratory|Foundation|"
                      r"Research|Board|Council|Ministry|Hospital|Corporation|Inc\b", re.I)


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
        if (prev and prev["kind"] == b["kind"] == "heading"
                and abs(prev["size"] - b["size"]) < max(0.5, b["size"] * 0.12)
                and -2 <= b["bbox"].y0 - prev["bbox"].y1 < b["size"] * 1.2):
            prev["html"] += " " + b["html"]
            prev["text"] += " " + b["text"]
            prev["bbox"] |= b["bbox"]
            prev["lines"] = prev.get("lines", []) + b.get("lines", [])
            continue
        out.append(b)
    return out


# ─────────────────────── 2. 읽기 순서 ───────────────────────
def _gutter(blocks: list[dict], page_rect: pymupdf.Rect) -> float | None:
    """2단이면 두 단 사이 가운데 x, 1단이면 None.
    본문 블록 대부분이 쪽 가운데선을 넘지 않고 좌우 양쪽에 있으면 2단으로 본다."""
    mid = page_rect.width / 2
    body = [b for b in blocks if b["kind"] in ("para", "list", "heading", "dialogue", "reference", "note")
            and not b["bbox"].is_empty and b["bbox"].width > 20]
    if len(body) < 3:
        return None
    left = [b for b in body if b["bbox"].x1 <= mid + 12]
    right = [b for b in body if b["bbox"].x0 >= mid - 12]
    if not left or not right or len(left) + len(right) < len(body) * 0.6:
        return None
    lx1 = max(b["bbox"].x1 for b in left)
    rx0 = min(b["bbox"].x0 for b in right)
    return (lx1 + rx0) / 2 if lx1 < rx0 else mid


def detect_columns(blocks: list[dict], page_rect: pymupdf.Rect) -> int:
    return 1 if _gutter(blocks, page_rect) is None else 2


def _spans(b: dict, gutter: float) -> bool:
    """가운데 단 사이를 가로지르는 블록(제목, 초록, 두 단에 걸친 그림·표).
    단 경계를 조금 넘는 정도(그림 눈금 글자 등)는 가로지르는 것으로 보지 않는다."""
    return b["bbox"].x0 < gutter - 40 and b["bbox"].x1 > gutter + 40


def column_segments(blocks: list[dict], page_rect: pymupdf.Rect) -> tuple[float | None, list[tuple]]:
    """본문 블록 → 위에서부터 [("span", [블록]) | ("cols", 왼쪽[], 오른쪽[])] 구간 목록.
    각주도 자기 단 안에 둔다(원문 위치 그대로). 위치 정보가 없는 블록(참고문헌 등)은 바로 앞 블록을 따른다."""
    gutter = _gutter(blocks, page_rect)
    body = [b for b in blocks if b["kind"] != "header"]
    if gutter is None:
        return None, [("span", body)]
    segs: list[tuple] = []
    left: list[dict] = []
    right: list[dict] = []
    last_side = "L"
    positioned = []
    for b in body:                       # 위치 없는 블록은 앞 블록에 붙여 둔다
        if b["bbox"].is_empty and positioned:
            positioned[-1][1].append(b)
        else:
            positioned.append((b, []))
    for b, tail in sorted(positioned, key=lambda t: t[0]["bbox"].y0):
        if not b["bbox"].is_empty and _spans(b, gutter):
            if left or right:
                segs.append(("cols", left, right))
                left, right = [], []
            segs.append(("span", [b] + tail))
            continue
        side = last_side if b["bbox"].is_empty else ("L" if (b["bbox"].x0 + b["bbox"].x1) / 2 < gutter else "R")
        (left if side == "L" else right).extend([b] + tail)
        last_side = side
    if left or right:
        segs.append(("cols", left, right))

    def key(x: dict) -> float:
        return x["bbox"].y0 if not x["bbox"].is_empty else 1e9

    out = []
    for seg in segs:
        if seg[0] == "cols":
            # 단 안에서는 원래 순서(y) — 위치 없는 블록은 앞 블록과 함께 움직이도록 안정 정렬
            out.append(("cols", _stable_y(seg[1]), _stable_y(seg[2])))
        else:
            out.append(seg)
    return gutter, out


def _stable_y(blocks: list[dict]) -> list[dict]:
    groups: list[list[dict]] = []
    for b in blocks:
        if b["bbox"].is_empty and groups:
            groups[-1].append(b)
        else:
            groups.append([b])
    groups.sort(key=lambda g: g[0]["bbox"].y0)
    return [b for g in groups for b in g]


def sort_reading_order(blocks: list[dict], page_rect: pymupdf.Rect) -> list[dict]:
    """헤더 → (가로지르는 블록 / 왼쪽 단 / 오른쪽 단 …) → 각주.
    2단이면 가로지르는 블록(제목·초록·두 단에 걸친 그림)을 만날 때마다 그때까지의 왼쪽 단, 오른쪽 단을
    차례로 내보낸다. 그래서 '그림 위의 2단 → 그림 → 그림 아래의 2단'도 맞게 나온다.
    한계: 3단 이상, 박스 기사 등은 순서가 어긋날 수 있다."""
    header = sorted([b for b in blocks if b["kind"] == "header"], key=lambda b: b["bbox"].y0)
    foot = [b for b in blocks if b["kind"] == "footnote"]
    rest = [b for b in blocks if b["kind"] not in ("header", "footnote")]
    gutter, segs = column_segments(rest, page_rect)
    if gutter is None:
        body = sorted(rest, key=lambda b: (round(b["bbox"].y0), b["bbox"].x0))
        return header + body + sorted(foot, key=lambda b: b["bbox"].y0)
    ordered: list[dict] = []
    for seg in segs:
        ordered += seg[1] if seg[0] == "span" else seg[1] + seg[2]
    # 각주는 왼쪽 단 → 오른쪽 단 순서
    foot.sort(key=lambda b: (0 if (b["bbox"].x0 + b["bbox"].x1) / 2 < gutter else 1, b["bbox"].y0))
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
            # 2단이면 단마다 따로 나눈다 (각 단의 왼쪽 여백이 항목 시작 기준)
            xs0 = [l["x0"] for l in ref_lines]
            xs1 = [l["bbox"].x1 if "bbox" in l else l["x0"] for l in ref_lines]
            mid = (min(xs0) + max(xs1)) / 2
            left = [l for l in ref_lines if l["x0"] < mid - 10]
            right = [l for l in ref_lines if l["x0"] >= mid - 10]
            if left and right and len(right) >= 3:
                ref_lines[:] = left
                flush_one()
                ref_lines[:] = right
            flush_one()

        def flush_one() -> None:
            if not ref_lines:
                return
            margin = min(l["x0"] for l in ref_lines)
            # 번호식 참고문헌([1] … / 1. …)이면 번호로 시작하는 줄에서만 새 항목
            numbered_style = sum(bool(re.match(r"^\s*(\[\d+\]|\d+\.)\s", l["text"])) for l in ref_lines) \
                >= max(3, len(ref_lines) // 4)
            entries: list[list[dict]] = []
            for ln in ref_lines:
                numbered = re.match(r"^\s*(\[\d+\]|\d+\.)\s", ln["text"])
                if ln["x0"] <= margin + 3 and (not numbered_style or numbered) or not entries:
                    entries.append([])
                entries[-1].append(ln)
            for e in entries:
                h, p = join_lines(e)
                r = pymupdf.Rect()
                for ln in e:
                    if "bbox" in ln:
                        r = pymupdf.Rect(ln["bbox"]) if r.is_empty else r | ln["bbox"]
                new_blocks.append({"kind": "reference", "bbox": r, "size": e[0]["size"],
                                   "html": _protect(h), "text": p})
            ref_lines.clear()

        paras = [b for b in pg["blocks"] if b["kind"] in ("para", "note", "footnote") and b.get("bbox") is not None]
        mid = (min(b["bbox"].x0 for b in paras) + max(b["bbox"].x1 for b in paras)) / 2 if paras else 0
        right_edge = max((b["bbox"].x1 for b in paras), default=0)
        for b in pg["blocks"]:
            # 오른쪽 정렬 짧은 줄('Manuscript received …')은 참고문헌 항목이 아니다
            if in_refs and b["kind"] in ("para", "note") and b.get("bbox") is not None \
                    and len(b.get("lines") or []) <= 2 and b["bbox"].x0 > mid + 20 \
                    and abs(b["bbox"].x1 - right_edge) < 6 and len(b["text"]) < 90 \
                    and not re.match(r"^[A-Z][A-Za-z'’\-]+,\s+[A-Z]\.", b["text"]):
                flush()
                b["right"] = True
                new_blocks.append(b)
                continue
            if b["kind"] in ("heading", "para", "note") and REF_HEAD_RE.match(b["text"].strip()):
                b["kind"] = "heading"
                in_refs = True
                new_blocks.append(b)
                continue
            if b["kind"] == "heading" and END_REF_RE.match(b["text"].strip()):
                flush()
                in_refs = False
            if in_refs and b["kind"] in ("para", "footnote", "note", "list") and b.get("lines"):
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
        if last["size"] < pages[i]["body_size"] * 0.95 or first["size"] < pages[i + 1]["body_size"] * 0.95:
            continue                              # 작은 글씨(교신저자 안내 등)는 이어 붙이지 않음
        if _carry_sentence(last, first):
            if not first["text"]:
                pages[i + 1]["blocks"].remove(first)


def _carry_sentence(last: dict, first: dict) -> bool:
    """last가 문장 중간에서 끊겼으면 first의 첫 문장을 last 끝으로 옮긴다. 옮겼으면 True."""
    if True:
        if SENTENCE_END_RE.search(last["text"]):
            return False
        cut = _first_sentence_split(first["text"])
        moved = first["text"][:cut].strip()
        if cut >= len(first["text"]):           # 다음 문단 전체가 한 문장이면 통째로
            hcut = len(first["html"])
        else:
            tail = esc(moved[-20:])
            pos = first["html"].find(tail)
            if pos < 0:
                return False
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
        if first["text"]:
            first["no_indent"] = True
        return True


def _carry_across_columns(blocks: list[dict], page_rect: pymupdf.Rect) -> None:
    """2단 쪽에서 왼쪽 단 끝 문단이 문장 중간에서 끊겨 오른쪽 단으로 이어지면,
    그 문장은 왼쪽 단에서 완결한다 (각 단이 따로 번역되므로)."""
    gutter, segs = column_segments([b for b in blocks if b["kind"] not in ("header", "footnote")], page_rect)
    if gutter is None:
        return
    for seg in segs:
        if seg[0] != "cols" or not seg[1] or not seg[2]:
            continue
        last, first = seg[1][-1], seg[2][0]
        if last["kind"] == "para" and first["kind"] == "para" and first["text"][:1].islower():
            if _carry_sentence(last, first) and not first["text"]:
                blocks.remove(first)


def extract_document(doc: pymupdf.Document, progress=None) -> list[dict]:
    """문서 전체 추출. 특정 페이지가 실패해도 나머지는 계속한다(mode='error')."""
    pages: list[dict] = []
    for i in range(doc.page_count):
        if progress:
            progress(i, doc.page_count)
        try:
            pg = extract_page(doc[i])
        except Exception as e:
            pg = {"mode": "error", "body_size": 10.0, "blocks": [], "error": str(e)}
        pg["page_number"] = i + 1
        pages.append(pg)
    # 논문 첫 쪽(제목이 있는 쪽, 보통 1쪽이지만 JSTOR 표지가 있으면 2쪽): 위쪽 저널 정보는 지우지 않고 보존
    ti = next((i for i, p in enumerate(pages[:3]) if any(b.get("title") for b in p.get("blocks", []))), 0)
    if ti and pages[ti]["mode"] == "text":
        for b in pages[ti]["blocks"]:
            if b["kind"] == "header" and not DOWNLOAD_NOTICE_RE.search(b["text"]) and not b.get("noise") \
                    and not re.fullmatch(r"\s*\d{1,4}\s*", b["text"]) and b["bbox"].y1 < 100:
                b["kind"] = "meta"
        metas = [b for b in pages[ti]["blocks"] if b["kind"] == "meta"]
        rest = [b for b in pages[ti]["blocks"] if b["kind"] != "meta"]
        heads = [b for b in rest if b["kind"] == "header"]
        pages[ti]["blocks"] = heads + metas + [b for b in rest if b["kind"] != "header"]
    if pages and pages[0]["mode"] == "text":
        for b in pages[0]["blocks"]:
            if b["kind"] == "header" and not re.fullmatch(r"\s*\d{1,4}\s*", b["text"]) and \
                    b["bbox"].y1 < pages[0].get("_h", 1e9):
                b["kind"] = "meta"
        metas = [b for b in pages[0]["blocks"] if b["kind"] == "meta"]
        rest = [b for b in pages[0]["blocks"] if b["kind"] != "meta"]
        heads = [b for b in rest if b["kind"] == "header"]
        pages[0]["blocks"] = heads + sorted(metas, key=lambda b: b["bbox"].y0) + [b for b in rest if b["kind"] != "header"]
    _assign_printed_numbers(pages)
    mark_references(pages)
    # 쪽 경계에서 끊긴 문장은 옮기지 않는다: 번역본 n쪽 = 원문 n쪽 내용 (원문 대조가 우선)
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
        if k == "figure":
            if translate_captions:
                units += [(bi, ii, h) for ii, h in enumerate(b.get("items") or [])]
            continue
        if k in ("header", "meta"):
            continue
        if (k == "reference" and not translate_refs) or (k == "caption" and not translate_captions):
            continue
        if k in ("list", "table", "dialogue"):
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
def make_css(lh: float = 1.32, compact: bool = False) -> str:
    css = _CSS_TEMPLATE.replace("{LH}", f"{lh:.2f}").replace("{LH_SMALL}", f"{max(1.15, lh - 0.1):.2f}")
    return css + (COMPACT_CSS if compact else "")


_CSS_TEMPLATE = f"""
@font-face {{ font-family: kr; src: url({FONT_REGULAR}); }}
@font-face {{ font-family: kr; src: url({FONT_BOLD}); font-weight: bold; }}
* {{ font-family: kr; }}
body {{ color: #111; }}
p {{ margin: 0 0 0.22em 0; line-height: {{LH}}; text-align: left; }}
p.para {{ text-indent: 1em; }}
p.center {{ text-align: center; }}
p.right {{ text-align: right; font-size: 0.9em; margin: 0; }}
p.byline {{ text-align: center; font-size: 0.9em; line-height: 1.3; margin: 0; }}
p.byname {{ text-align: center; font-size: 0.95em; font-weight: bold; line-height: 1.3; margin: 0.35em 0 0 0; }}
p.abstract {{ font-size: 0.95em; margin: 0.9em 1.6em 0.9em 1.6em; text-indent: 0; }}
p.quote {{ margin: 0.3em 1.6em 0.4em 1.6em; text-indent: 0; }}
h1 {{ font-size: 1.4em; font-weight: bold; text-align: center; line-height: 1.32; margin: 0.2em 0 0.7em 0; }}
h2 {{ font-size: 1.12em; font-weight: bold; text-align: center; line-height: 1.3; margin: 0.95em 0 0.35em 0; }}
h3 {{ font-size: 1.02em; font-weight: bold; line-height: 1.3; margin: 0.75em 0 0.25em 0; }}
ul {{ margin: 0.1em 0 0.4em 1.4em; padding: 0; }}
li {{ line-height: {{LH}}; margin-bottom: 0.12em; }}
b {{ font-weight: bold; }}
sup {{ font-size: 0.7em; }}
.cap {{ font-size: 0.9em; text-align: left; line-height: 1.38; margin: 0.25em 0 0.8em 0; }}
.fig {{ text-align: center; margin: 0.5em 0 0.15em 0; }}
table.figlab {{ border-collapse: collapse; margin: 0.1em 0 0.5em 0; }}
table.figlab td {{ font-size: 0.78em; line-height: 1.25; padding: 0.05em 0.4em; vertical-align: top;
                   border-bottom: 0.4px solid #bbb; }}
table.figlab td.src {{ color: #333; }}
.dlg {{ line-height: {{LH}}; margin: 0 0.6em 0.22em 0.6em; padding-left: 1.4em; text-indent: -1.4em; }}
.meta {{ font-size: 0.75em; color: #444; line-height: 1.35; margin: 0 0 0.6em 0; }}
.note {{ font-size: 0.9em; line-height: {{LH_SMALL}}; margin: 0 0 0.22em 1.2em; }}
.fn {{ font-size: 0.85em; line-height: {{LH_SMALL}}; color: #111; margin: 0 0 0.15em 0; }}
.ref {{ font-size: 0.85em; line-height: 1.38; margin: 0 0 0.3em 1.6em; text-indent: -1.6em; }}
.tcap {{ text-align: left; font-size: 0.95em; margin: 0.6em 0 0.35em 0; line-height: 1.35; }}
table.tbl {{ border-collapse: collapse; }}
table.first {{ border-top: 0.9px solid #000; }}
th {{ font-size: 0.86em; font-weight: bold; text-align: left; vertical-align: bottom; background-color: #eeeeee;
      padding: 0.15em 0.3em; border-bottom: 0.7px solid #000; line-height: 1.22; }}
th.sup {{ text-align: center; border-bottom: 0.4px solid #666; }}
td {{ font-size: 0.86em; vertical-align: top; padding: 0.08em 0.3em; line-height: 1.24; }}
td.num {{ text-align: center; }}
td.rowh {{ font-weight: normal; }}
table.last {{ border-bottom: 0.9px solid #000; }}
p.ti {{ margin: 0 0 0.1em 0; padding-left: 0.8em; text-indent: -0.8em; text-align: left; line-height: 1.22; }}
p.tp {{ margin: 0; text-align: inherit; line-height: 1.22; }}
.tnote {{ font-size: 0.82em; margin-top: 0.3em; padding-top: 0.2em; }}
u {{ text-decoration: underline; }}
i {{ font-style: italic; }}
hr {{ border: none; border-top: 0.5px solid #666; width: 30%; margin: 0.6em 0 0.35em 0; }}
"""

COMPACT_CSS = """
p { margin-bottom: 0.08em; } h1 { margin: 0 0 0.4em 0; } h2 { margin: 0.5em 0 0.2em 0; }
h3 { margin: 0.4em 0 0.15em 0; } .cap { margin: 0.1em 0 0.4em 0; } .tcap { margin: 0.3em 0 0.2em 0; }
p.abstract { margin: 0.5em 1.2em 0.5em 1.2em; } .fig { margin: 0.2em 0 0.1em 0; } hr { margin: 0.3em 0 0.2em 0; }
"""
CSS = make_css(LINE_HEIGHT)


def to_reading_html(tr_html: str, italic_to_bold: bool = True) -> str:
    """번역 결과의 <i>(원문 기울임 강조)를 '굵게 + 밑줄'로 바꾸고, 번역금지 span은 벗긴다.
    italic_to_bold=False(참고문헌)면 원문처럼 기울임으로 둔다."""
    s = re.sub(r'<span translate="no">(.*?)</span>', r"\1", tr_html, flags=re.S)
    if italic_to_bold:
        return re.sub(r"<i>(.*?)</i>", r"<b><u>\1</u></b>", s, flags=re.S)
    return s


def _pieces(blocks: list[dict], box_width: float = 360.0) -> list[str]:
    """번역된 블록 → HTML 조각 목록. 각주는 맨 끝에 구분선과 함께."""
    out: list[str] = []
    for bi, b in enumerate(blocks):
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
            if b.get("byline"):
                cls = "byname" if b["byline"] == "name" else "byline"
            elif b.get("abstract"):
                cls = "abstract"
            elif b.get("quote"):
                cls = "quote"
            elif b.get("right"):
                cls = "right"
            out.append(f'<p class="{cls}">{to_reading_html(tr)}</p>')
        elif k == "list":
            items = b.get("tr_items") or b["items"]
            out.append("<ul>" + "".join(f"<li>{to_reading_html(t)}</li>" for t in items) + "</ul>")
        elif k == "caption":
            nxt = blocks[bi + 1]["kind"] if bi + 1 < len(blocks) else ""
            cls = "tcap" if nxt == "table" or b["text"].lstrip().upper().startswith("TABLE") else "cap"
            out.append(f'<p class="{cls}">{to_reading_html(tr)}</p>')
        elif k == "note":
            out.append(f'<p class="note">{to_reading_html(tr)}</p>')
        elif k == "table":
            out += rotated_table.table_pieces(b, to_reading_html, box_width)
        elif k == "dialogue":
            items = b.get("tr_items") or b["items"]
            out.append("".join(f'<p class="dlg">{to_reading_html(t)}</p>' for t in items))
        elif k == "meta":
            out.append(f'<p class="meta">{to_reading_html(b["html"])}</p>')
        elif k == "reference":
            # 참고문헌은 학술지명 이탤릭을 볼드로 바꾸지 않는다 (지저분해짐)
            out.append(f'<p class="ref">{to_reading_html(tr, italic_to_bold=False)}</p>')
        elif k == "figure" and b.get("img_name"):
            w, h = b["bbox"].width, b["bbox"].height
            if w > box_width - 4:                       # 단 폭보다 넓으면 비율 유지하며 줄인다
                h, w = h * (box_width - 4) / w, box_width - 4
            out.append(f'<p class="fig"><img src="{b["img_name"]}" width="{w:.0f}" height="{h:.0f}"/></p>')
            if b.get("tr_items") and b.get("items"):
                # 그림 속 문구: 원문 | 번역 (그림은 원본 그대로 두어 글자·수치를 가리지 않는다)
                rows = "".join(f'<tr><td class="src">{esc(plain(o))}</td><td>{to_reading_html(t)}</td></tr>'
                               for o, t in zip(b["items"], b["tr_items"]))
                out.append(f'<table class="figlab">{rows}</table>')
    foot = [b for b in blocks if b["kind"] == "footnote"]
    if foot:
        out.append("<hr/>" + "".join(
            f'<p class="fn">{to_reading_html(b.get("tr", b["html"]))}</p>' for b in foot))
    return out


def _label_html(text: str) -> str:
    return f'<p style="text-align:right;color:#888;font-size:8pt;margin:0">{esc(text)}</p>'


def _page_number_candidates(blocks: list[dict]) -> list[int]:
    """머리글·꼬리글에서 인쇄 쪽수 후보. 다운로드 안내(날짜·IP), 권호·연도 줄은 제외."""
    out: list[int] = []
    for b in blocks:
        if b["kind"] not in ("header", "meta") or DOWNLOAD_NOTICE_RE.search(b["text"]):
            continue
        if re.search(r"\b(Vol|No|pp|Volume|Issue)\b\.?", b["text"]):
            continue
        for m in re.finditer(r"(?<![\d.,:/-])(\d{1,4})(?![\d.,:/%-])", b["text"]):
            n = int(m.group(1))
            if not 1800 <= n <= 2100:                     # 연도 제외
                out.append(n)
    return out


def _assign_printed_numbers(pages: list[dict]) -> None:
    """인쇄 쪽수 = PDF 쪽 번호 + 일정한 차이. 여러 쪽에서 같은 차이로 확인된 경우에만,
    그 쪽에서 실제로 읽힌 숫자가 차이와 맞을 때 표시한다 (날짜·표 숫자를 쪽수로 쓰지 않는다)."""
    from collections import Counter

    cands = {pg["page_number"]: _page_number_candidates(pg.get("blocks", [])) for pg in pages}
    offsets = Counter(n - pno for pno, ns in cands.items() for n in set(ns))
    best = offsets.most_common(1)
    ok = best and best[0][1] >= max(2, sum(1 for ns in cands.values() if ns) * 0.4)
    for pg in pages:
        pno = pg["page_number"]
        pg["printed"] = str(pno + best[0][0]) if ok and (pno + best[0][0]) in cands[pno] else None


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
    printed = pg["printed"] if "printed" in pg else printed_page_number(pg["blocks"])
    label = f"PDF p.{pno}" + (f" · 인쇄 {printed}쪽" if printed else "")

    if pg.get("rotated") and pg.get("_rot_key") not in _ROT_DOCS and not note:
        note = "가로 표 — 원문 분석 정보가 없어 원문 그대로 (PDF를 다시 올려 주세요)"
    # 가로로 돌려 인쇄한 스캔 표: 돌린 원문(가로 쪽)에 맞춰 짠 뒤, 원래 방향으로 돌려 붙인다
    if pg.get("rotated") and pg.get("_rot_key") in _ROT_DOCS and pg["mode"] == "text" and not note:
        inner = {k: v for k, v in pg.items() if k not in ("rotated", "_rot_key")}
        inner["page_number"] = 1
        inner["_nolabel"] = True
        tmp = pymupdf.open()
        render_page(tmp, _ROT_DOCS[pg["_rot_key"]], inner)
        pg["_render"] = inner.get("_render")
        page = out.new_page(width=W, height=H)
        page.show_pdf_page(pymupdf.Rect(0, 0, W, H), tmp, 0, rotate=-pg["rotated"])
        page.insert_htmlbox(pymupdf.Rect(30, 26, W - 30, 50), _label_html(label + " · 가로 표"),
                            css=CSS, archive=archive)
        return 1

    # 회전된 표 페이지, 실패/미번역 페이지 → 원문 페이지를 그대로 (벡터 유지)
    if pg["mode"] != "text" or note:
        page = out.new_page(width=W, height=H)
        page.show_pdf_page(pymupdf.Rect(18, 40, W - 18, H - 10), src_doc, pno - 1)
        msg = note or {"image": "가로 방향 표 페이지 — 원문 그대로",
                       "scan": "스캔 쪽 — 글자 읽기(OCR) 전이라 원문 그대로"}.get(pg["mode"], "원문 그대로")
        page.insert_htmlbox(pymupdf.Rect(18, 14, W - 18, 38), _label_html(f"{label} · {msg}"),
                            css=CSS, archive=archive)
        return 1

    blocks = pg["blocks"]
    for b in blocks:                           # 그림은 원문에서 잘라 PNG로
        if b["kind"] == "figure":
            name = f"fig_{pno}_{int(b['bbox'].y0)}_{int(b['bbox'].x0)}.png"
            pix = src.get_pixmap(clip=b["bbox"], dpi=FIG_DPI)
            archive.add(pix.tobytes("png"), name)
            b["img_name"] = name

    regions = page_regions(blocks, src.rect)
    html = [("".join(_pieces(bl, r.width)), r) for r, bl in regions]
    html = [(h, r) for h, r in html if h]
    if not html:
        html = [('<p style="color:#999">(이 페이지에는 번역할 텍스트가 없습니다)</p>', pymupdf.Rect(40, 60, W - 40, 120))]

    font, lh, compact = _fit(html, W, H, archive, pg["body_size"])
    pg["_render"] = {"font": font and round(font, 2), "lh": lh, "compact": compact, "fit": font is not None,
                     "target_font": round(pg["body_size"] * FONT_BOOST, 2),
                     "min_font": round(pg["body_size"] * MIN_FONT_RATIO, 2)}
    page = out.new_page(width=W, height=H)
    if font is None:
        # 가장 작은 글자로도 안 들어가는 극단적인 경우: 영역마다 따로 줄여서라도 이 쪽 안에 넣는다
        for h, r in html:
            page.insert_htmlbox(r, h, css=make_css(1.15) + f"body {{ font-size: {pg['body_size']:.1f}pt; }}",
                                archive=archive, scale_low=0)
    else:
        css = make_css(lh, compact) + f"body {{ font-size: {font:.2f}pt; }}"
        for h, r in html:
            page.insert_htmlbox(r, h, css=css, archive=archive, scale_low=1)
    if not pg.get("_nolabel"):
        page.insert_htmlbox(pymupdf.Rect(30, 26, W - 30, 50), _label_html(label), css=CSS, archive=archive)
    return 1


LINE_HEIGHTS = (1.6, 1.52, 1.45, 1.38, 1.32, 1.26, 1.2)
MIN_FONT_RATIO = 0.62      # 글자 크기 하한 = 원문 본문 크기 × 0.62 (이보다 작아야 하면 '맞추지 못함'으로 기록)   # 넉넉한 것부터 — 글자를 줄이기 전에 줄간격부터 줄인다


def _fit(html: list[tuple[str, pymupdf.Rect]], W: float, H: float, archive: pymupdf.Archive,
         body_size: float) -> tuple[float | None, float, bool]:
    """쪽 전체에 같은 글자 크기를 쓰면서 모든 영역에 들어가는 (글자 크기, 줄간격, 간격 압축 여부).
    줄이는 순서: ① 줄간격(1.6 → 1.2) ② 문단·제목·캡션 간격(compact) ③ 마지막으로 글자 크기.
    글자 크기 상한 = 원문 본문 × FONT_BOOST, 하한 = 원문 본문 × MIN_FONT_RATIO (안전장치)."""
    trial = pymupdf.open()

    def fits(font: float, lh: float, compact: bool) -> bool:
        css = make_css(lh, compact) + f"body {{ font-size: {font:.2f}pt; }}"
        pg_ = trial.new_page(width=W, height=H)
        ok = True
        for h, r in html:
            spare, _ = pg_.insert_htmlbox(r, h, css=css, archive=archive, scale_low=1)
            if spare < 0:
                ok = False
                break
        trial.delete_page(-1)
        return ok

    hi = body_size * FONT_BOOST
    lo = body_size * MIN_FONT_RATIO
    tight = LINE_HEIGHTS[-1]
    try:
        for compact in (False, True):
            if fits(hi, tight, compact):
                for lh in LINE_HEIGHTS:
                    if lh == tight or fits(hi, lh, compact):
                        return hi, lh, compact
        if not fits(lo, tight, True):
            return None, tight, True
        a, b = lo, hi
        for _ in range(7):
            m = (a + b) / 2
            if fits(m, tight, True):
                a = m
            else:
                b = m
        for lh in LINE_HEIGHTS:
            if lh == tight or fits(a, lh, True):
                return a, lh, True
        return a, tight, True
    finally:
        trial.close()


def page_regions(blocks: list[dict], page_rect: pymupdf.Rect) -> list[tuple[pymupdf.Rect, list[dict]]]:
    """번역문을 놓을 영역들. 원문의 단 구성과 위치를 따른다.
    1단: 본문 전체를 하나의 상자에.
    2단: 위에서부터 '가로지르는 블록'은 전체 폭 상자, 그 사이 구간은 왼쪽 단·오른쪽 단 상자 두 개.
    각 구간은 원문에서 그 구간이 시작하는 높이부터 다음 구간이 시작하는 높이까지 쓴다."""
    W, H = page_rect.width, page_rect.height
    body = [b for b in blocks if b["kind"] != "header"]
    if not body:
        return []
    placed = [b for b in body if not b["bbox"].is_empty]
    x0 = min((b["bbox"].x0 for b in placed), default=40)
    x1 = max((b["bbox"].x1 for b in placed), default=W - 40)
    # 한국어 줄이 조금 길어지므로 본문 폭을 좌우로 약간 넓힌다 (쪽 가장자리 28pt까지)
    x0 = max(28, min(x0 - 6, 40))
    x1 = min(W - 28, max(x1 + 6, W - 40))
    top_y = min((b["bbox"].y0 for b in placed), default=60)
    top = max(52.0, top_y - 30)
    bottom = H - 28

    gutter, segs = column_segments(body, page_rect)
    if gutter is None:
        return [(pymupdf.Rect(x0, top, x1, bottom), body)]

    def seg_top(seg: tuple) -> float:
        bl = seg[1] if seg[0] == "span" else seg[1] + seg[2]
        ys = [b["bbox"].y0 for b in bl if not b["bbox"].is_empty]
        return min(ys) if ys else top

    tops = [seg_top(sg) for sg in segs]
    tops[0] = top
    regions: list[tuple[pymupdf.Rect, list[dict]]] = []
    g = 7  # 단 사이 여백의 절반
    for k, seg in enumerate(segs):
        y0 = tops[k]
        y1 = (tops[k + 1] - 3) if k + 1 < len(segs) else bottom
        y1 = max(y1, y0 + 12)
        if seg[0] == "span":
            regions.append((pymupdf.Rect(x0, y0, x1, y1), seg[1]))
        else:
            if seg[1]:
                regions.append((pymupdf.Rect(x0, y0, gutter - g, y1), seg[1]))
            if seg[2]:
                regions.append((pymupdf.Rect(gutter + g, y0, x1, y1), seg[2]))
    return regions


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
            items = (b.get("tr_items") or []) if translated else (b.get("items") or [])
            head = "[그림 — 원문 참조]" if translated else "[Figure]"
            if items and translated and b.get("items"):
                head += " 그림 속 문구: " + " · ".join(f"{plain(o)} → {plain(t)}" for o, t in zip(b["items"], items))
            elif items:
                head += " " + " · ".join(plain(t) for t in items)
            parts.append(head)
        elif k == "list":
            items = (b.get("tr_items") or b["items"]) if translated else b["items"]
            parts.append("\n".join("• " + plain(t) for t in items))
        elif k == "dialogue":
            items = (b.get("tr_items") or b["items"]) if translated else b["items"]
            parts.append("\n".join(plain(t) for t in items))
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


# ─────────────────────── 7. 스캔 쪽 (OCR 결과) ───────────────────────
OCR_KINDS = {"header", "heading", "title", "para", "list_item", "caption", "footnote", "reference",
             "figure", "table", "note", "dialogue"}


def _clean_ocr_html(s: str) -> str:
    """OCR 결과 글에서 허용한 태그(<i> <b> <sup>)만 남기고 나머지는 이스케이프."""
    parts = re.split(r"(</?(?:i|b|sup)>)", s)
    return _protect("".join(p if re.fullmatch(r"</?(?:i|b|sup)>", p) else esc(p) for p in parts))


def build_ocr_page(page: pymupdf.Page, ocr_blocks: list[dict], page_number: int) -> dict[str, Any]:
    """OCR로 읽은 블록 목록 → 일반 페이지와 같은 형식의 페이지.
    ocr_blocks: [{"kind", "text", "rows", "bbox": [x0,y0,x1,y1] (쪽 크기에 대한 0~1 비율)}]"""
    W, H = page.rect.width, page.rect.height
    blocks: list[dict[str, Any]] = []
    list_buf: list[dict] = []

    def rect_of(b: dict) -> pymupdf.Rect:
        x0, y0, x1, y1 = (list(b.get("bbox") or [0, 0, 1, 1]) + [0, 0, 1, 1])[:4]
        r = pymupdf.Rect(max(0, x0) * W, max(0, y0) * H, min(1, x1) * W, min(1, y1) * H)
        r.normalize()
        return r

    def flush_list() -> None:
        if not list_buf:
            return
        r = rect_of(list_buf[0])
        for b in list_buf[1:]:
            r |= rect_of(b)
        items = [_clean_ocr_html(b["text"].lstrip("•·-–▪ ")) for b in list_buf]
        blocks.append({"kind": "list", "bbox": r, "size": 10.0, "items": items, "html": "",
                       "text": "\n".join("• " + plain(h) for h in items)})
        list_buf.clear()

    for b in ocr_blocks:
        kind = b.get("kind", "para")
        if kind not in OCR_KINDS:
            kind = "para"
        if kind == "list_item":
            list_buf.append(b)
            continue
        flush_list()
        r = rect_of(b)
        if kind == "figure":
            if r.width > 20 and r.height > 20:
                blocks.append({"kind": "figure", "bbox": r, "size": 10.0, "html": "", "text": ""})
            continue
        if kind == "table" and b.get("rows"):
            rows = [[_clean_ocr_html(c) for c in row] for row in b["rows"] if row]
            ncol = max(len(row) for row in rows)
            items: list[str] = []
            grid: list[list[list[int]]] = []
            for row in rows:
                cells: list[list[int]] = []
                for c in range(ncol):
                    if c < len(row) and plain(row[c]).strip():
                        items.append(row[c])
                        cells.append([len(items) - 1])
                    else:
                        cells.append([])
                grid.append(cells)
            header = [cell[0] if cell else None for cell in grid[0]]
            layout = {"title": None, "super": None, "header": header, "rows": grid[1:] or grid, "note": None,
                      "bulleted": [], "col_x": [r.width * k / ncol for k in range(ncol + 1)], "width": r.width}
            blocks.append({"kind": "table", "bbox": r, "size": 9.0, "items": items, "layout": layout,
                           "html": "", "text": "\n".join(plain(i) for i in items)})
            continue
        html = _clean_ocr_html(b.get("text", ""))
        if not plain(html).strip():
            continue
        if kind == "dialogue":
            turns = [t for t in re.split(r"\n+", b.get("text", "")) if t.strip()]
            blocks.append({"kind": "dialogue", "bbox": r, "size": 10.0,
                           "items": [_clean_ocr_html(t) for t in turns], "html": "",
                           "text": "\n".join(turns)})
            continue
        title = kind == "title"
        if title:
            kind = "heading"
        blocks.append({"kind": kind, "bbox": r, "size": 10.0, "html": html, "text": plain(html),
                       "centered": False, "title": title})
    flush_list()
    return {"mode": "text", "body_size": 10.0, "blocks": blocks, "page_number": page_number, "ocr": True}


def apply_ocr(doc: pymupdf.Document, pages: list[dict], ocr: dict[str, list[dict]]) -> list[dict]:
    """스캔 쪽을 OCR 결과로 바꾼 새 페이지 목록 (원래 목록은 그대로 둔다).
    OCR이 끝난 쪽끼리는 쪽 경계에서 끊긴 문장도 앞 쪽으로 모은다."""
    import copy
    out = copy.deepcopy(pages)
    changed = False
    for i, pg in enumerate(out):
        if pg["mode"] == "scan" and str(pg["page_number"]) in ocr:
            out[i] = build_ocr_page(doc[i], ocr[str(pg["page_number"])], pg["page_number"])
            changed = True
    if changed:
        carry_cross_page(out)
    return out


# ─────────────────────── 8. 용어 첫 등장에 영어 병기 ───────────────────────
def annotate_first_terms(pages: list[dict], glossary: dict[str, str]) -> None:
    """용어집 용어가 논문에서 처음 나오는 번역문 한 곳에 '번역어(English)'로 영어를 병기한다(제자리 수정).
    번역은 페이지별로 따로 하므로 '첫 등장'은 이렇게 다 모은 뒤 처리한다. 참고문헌·표는 건너뛴다."""
    pending = {en: ko for en, ko in glossary.items() if en and ko}
    if not pending:
        return
    for pg in pages:
        for b in pg["blocks"]:
            if b["kind"] in ("reference", "header", "meta", "figure", "table") or "tr" not in b:
                continue
            src = b["text"].lower()
            for en, ko in list(pending.items()):
                if en.lower() in src and ko in b["tr"] and f"{ko}(" not in b["tr"]:
                    b["tr"] = b["tr"].replace(ko, f"{ko}({en})", 1)
                    del pending[en]
            if not pending:
                return


# ─────────────────────── 9. 이전 번역본 재사용 ───────────────────────
def map_translated_pages(doc: pymupdf.Document, n_pages: int) -> dict[int, list[int]]:
    """이전에 만든 번역 PDF의 각 쪽이 원문 몇 쪽인지 → {원문 쪽 번호: [번역 PDF 쪽 인덱스, …]}.
    이 앱이 만든 PDF는 오른쪽 위 'PDF p.N' 표시로 찾는다('원문 그대로' 표시가 붙은 미번역 쪽은 제외).
    표시가 없는 PDF(다른 도구로 만든 번역본)는 쪽수가 원문과 같을 때만 1:1로 대응시킨다."""
    found: dict[int, list[int]] = {}
    any_label = False
    for i, page in enumerate(doc):
        top = page.get_text(clip=pymupdf.Rect(0, 0, page.rect.width, 56))
        m = re.search(r"PDF p\.(\d+)", top)
        if not m:
            continue
        any_label = True
        if "원문 그대로" in top:
            continue
        found.setdefault(int(m.group(1)), []).append(i)
    if any_label:
        return {k: v for k, v in found.items() if 1 <= k <= n_pages}
    if doc.page_count == n_pages:
        return {i + 1: [i] for i in range(n_pages)}
    return {}


def parse_page_list(text: str, n_pages: int) -> list[int]:
    """'3, 12, 20-23' → [3, 12, 20, 21, 22, 23]"""
    out: set[int] = set()
    for part in re.split(r"[,\s]+", text.strip()):
        m = re.fullmatch(r"(\d+)(?:\s*[-~]\s*(\d+))?", part)
        if not m:
            continue
        a = int(m.group(1))
        b = int(m.group(2) or a)
        for k in range(min(a, b), max(a, b) + 1):
            if 1 <= k <= n_pages:
                out.add(k)
    return sorted(out)


# ─────────────────────── 7. 번역 맥락·구조 기록 ───────────────────────
FLOW_KINDS = ("para", "list", "dialogue", "note", "heading")


def _flow(pg: dict) -> list[dict]:
    return [b for b in pg.get("blocks", []) if b["kind"] in FLOW_KINDS]


def unit_id(pno: int, bi: int, ii: int | None) -> str:
    return f"p{pno}-b{bi}" + (f"-{ii}" if ii is not None else "")


def unit_meta(pages: list[dict], pg: dict, units: list[tuple[int, int | None, str]]) -> list[dict]:
    """번역 조각마다 식별자·쪽·종류와, 쪽 경계에서 이어지는 문장의 앞뒤 맥락(참고용, 번역 대상 아님)."""
    pno = pg["page_number"]
    by_no = {p["page_number"]: p for p in pages}
    flow = _flow(pg)
    first_id = id(flow[0]) if flow else None
    last_id = id(flow[-1]) if flow else None
    prev_tail = next_head = ""
    if flow and flow[0]["kind"] == "para":
        prev = by_no.get(pno - 1)
        pf = _flow(prev) if prev and prev.get("mode") == "text" else []
        if pf and pf[-1]["kind"] == "para" and not SENTENCE_END_RE.search(pf[-1]["text"].rstrip()):
            prev_tail = pf[-1]["text"][-500:]
    if flow and flow[-1]["kind"] == "para" and not SENTENCE_END_RE.search(flow[-1]["text"].rstrip()):
        nxt = by_no.get(pno + 1)
        nf = _flow(nxt) if nxt and nxt.get("mode") == "text" else []
        if nf and nf[0]["kind"] == "para":
            next_head = nf[0]["text"][:500]
    out: list[dict] = []
    for bi, ii, _ in units:
        b = pg["blocks"][bi]
        kind = {"figure": "figure_label", "table": "table_cell", "dialogue": "dialogue_turn",
                "list": "list_item"}.get(b["kind"], b["kind"])
        if b.get("title"):
            kind = "title"
        m = {"id": unit_id(pno, bi, ii), "page": pno, "type": kind}
        if id(b) == first_id and prev_tail and ii is None:
            m["context_before"] = prev_tail
        if id(b) == last_id and next_head and ii is None:
            m["context_after"] = next_head
        out.append(m)
    return out


def structure_record(pages: list[dict], tpages: dict[int, dict | None], reviews: dict[int, list]) -> dict:
    """재작업용 구조·번역 데이터: 쪽 → 블록(식별자, 종류, 위치, 원문, 번역, 검토)."""
    doc: dict[str, Any] = {"pages": []}
    for pg in pages:
        pno = pg["page_number"]
        tpg = tpages.get(pno)
        blocks = []
        for bi, b in enumerate(pg.get("blocks", [])):
            tb = tpg["blocks"][bi] if tpg is not None and bi < len(tpg["blocks"]) else {}
            rec = {"id": unit_id(pno, bi, None), "kind": b["kind"],
                   "bbox": [round(x, 1) for x in b["bbox"]] if b.get("bbox") is not None else None,
                   "source": b.get("text", "")}
            if b.get("items"):
                rec["source_items"] = [plain(x) for x in b["items"]]
            if tb.get("tr") is not None:
                rec["translation"] = plain(tb["tr"])
            if tb.get("tr_items"):
                rec["translation_items"] = [plain(x) for x in tb["tr_items"]]
            if b.get("review"):
                rec["review"] = b["review"]
            if b["kind"] == "header":
                rec["excluded"] = ("괘선을 글자로 읽은 잡음" if b.get("noise")
                                   else "머리글·꼬리글·쪽 번호·다운로드 안내 (번역하지 않음)")
            elif b["kind"] == "reference":
                rec["excluded"] = "참고문헌 (원문 유지)"
            blocks.append(rec)
        doc["pages"].append({"page": pno, "printed": pg.get("printed"), "mode": pg.get("mode"),
                             "rotated": pg.get("rotated"), "render": (tpg or {}).get("_render"),
                             "review": reviews.get(pno, []), "blocks": blocks})
    return doc


def bookmarks(tpages: list[dict]) -> list[list]:
    """PDF 책갈피: 논문 제목(1단계) → 절 제목(2단계). 쪽 수는 늘리지 않는다."""
    toc: list[list] = []
    has_title = False
    for i, pg in enumerate(tpages, start=1):
        for b in pg.get("blocks", []):
            if b["kind"] != "heading":
                continue
            t = plain(b.get("tr") or b["html"]).strip()
            if not t:
                continue
            if b.get("title") and not has_title:
                toc.append([1, t[:120], i])
                has_title = True
            else:
                toc.append([2 if has_title else 1, t[:120], i])
    return toc
