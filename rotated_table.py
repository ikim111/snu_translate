"""
rotated_table.py — 가로로 돌려 앉힌 표 페이지(예: 논문의 큰 표)를 칸 단위로 읽어 낸다.

학술지는 폭이 넓은 표를 페이지를 90° 돌려서 싣는 경우가 많다. 이런 페이지는 글자가 세로로
누워 있어서 일반 추출로는 표 구조를 알 수 없다. 여기서는

  1. 페이지 회전을 풀어(rotation_matrix) 사람이 보는 방향의 좌표로 바꾸고
  2. 가로 괘선으로 제목 / 머리행 / 본문 / 주석 영역을 나누고
  3. 줄의 x 시작 위치를 모아 열을, 첫 열(행 이름)의 위치로 행을 나눈 뒤
  4. 칸마다 글머리표(•) 단위로 항목을 만든다.

굵은 글씨는 <b>, 밑줄(괘선 아래 그어진 선)은 <u>, 이탤릭은 <i>로 남긴다.
(이 논문의 Table 2처럼 '밑줄 = 수정한 기술, 굵은 글씨 = 추가한 기술' 같은 의미가 있는 경우가 있다.)

구조를 알아내지 못하면 None을 돌려주고, 그 페이지는 원문 그대로 들어간다.
"""
from __future__ import annotations

import re
from typing import Any

import pymupdf

BOLD_FLAG = 16
ITALIC_FLAG = 2
BULLETS = ("•", "▪", "◦", "–")


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _span_html(text: str, flags: int, underlined: bool) -> str:
    e = _esc(text)
    core = e.strip()
    if not core:
        return e
    lead = e[: len(e) - len(e.lstrip())]
    tail = e[len(e.rstrip()):]
    if flags & BOLD_FLAG:
        core = f"<b>{core}</b>"
    elif flags & ITALIC_FLAG and len(core) >= 3:
        core = f"<i>{core}</i>"
    if underlined:
        core = f"<u>{core}</u>"
    return lead + core + tail


def _merge_tags(html: str) -> str:
    for t in ("b", "i", "u"):
        html = html.replace(f"</{t}><{t}>", "")
        html = re.sub(rf"</{t}>(\s+)<{t}>", r"\1", html)
    return html


def _join(lines: list[dict]) -> tuple[str, str]:
    """줄들을 한 문단으로. 줄끝 하이픈은 붙인다."""
    html, text = "", ""
    for ln in lines:
        h, t = ln["html"].strip(), ln["text"].strip()
        if not t:
            continue
        if text.endswith("-") and t[:1].islower():
            m = re.search(r"-((?:</[a-z]+>)*)$", html)
            if m:
                html = html[: m.start()] + m.group(1)
            text = text[:-1]
        elif text:
            html += " "
            text += " "
        html += h
        text += t
    return _merge_tags(html), text


def _clusters(values: list[float], gap: float) -> list[float]:
    """정렬한 값들을 gap보다 크게 벌어지는 곳에서 나눠 각 무리의 최솟값을 돌려준다."""
    starts: list[float] = []
    prev = None
    for v in sorted(values):
        if prev is None or v - prev > gap:
            starts.append(v)
        prev = v
    return starts


def extract_rotated_table(page: pymupdf.Page) -> dict[str, Any] | None:
    m = page.rotation_matrix
    W, H = page.rect.width, page.rect.height
    raw = page.get_text("dict")

    # 가장 많은 글자 방향 = 표 본문의 방향 (쪽 번호처럼 방향이 다른 줄은 버림)
    dir_count: dict[tuple, int] = {}
    for b in raw["blocks"]:
        for l in b.get("lines", []):
            k = (round(l["dir"][0]), round(l["dir"][1]))
            dir_count[k] = dir_count.get(k, 0) + sum(len(s["text"]) for s in l["spans"])
    if not dir_count:
        return None
    main_dir = max(dir_count, key=dir_count.get)

    # 가로 괘선과 밑줄 (회전을 푼 좌표)
    segs: list[pymupdf.Rect] = []
    for d in page.get_drawings():
        # 한 도형 안에 선이 여러 개 있을 수 있으므로 선 하나하나를 따로 본다
        for it in d["items"]:
            if it[0] == "l":
                r = pymupdf.Rect(it[1], it[2])
            elif it[0] == "re":
                r = pymupdf.Rect(it[1])
            else:
                continue
            r = r * m
            r.normalize()
            if r.height < 1.5 and r.width > 4:
                segs.append(r)

    lines: list[dict] = []
    for b in raw["blocks"]:
        for l in b.get("lines", []):
            if (round(l["dir"][0]), round(l["dir"][1])) != main_dir:
                continue
            parts_h, parts_t = [], []
            for s in l["spans"]:
                if not s["text"]:
                    continue
                sb = pymupdf.Rect(s["bbox"]) * m
                sb.normalize()
                under = any(abs(g.y0 - sb.y1) < 3.5 and min(g.x1, sb.x1) - max(g.x0, sb.x0) > sb.width * 0.5
                            for g in segs if g.width < W * 0.5)
                parts_h.append(_span_html(s["text"], s["flags"], under))
                parts_t.append(s["text"])
            text = "".join(parts_t)
            if not text.strip():
                continue
            bb = pymupdf.Rect(l["bbox"]) * m
            bb.normalize()
            lines.append({"html": "".join(parts_h), "text": text, "bbox": bb,
                          "size": max(s["size"] for s in l["spans"])})
    if len(lines) < 6:
        return None

    # 영역 나누기: 표 폭의 절반 이상인 가로 괘선
    xs0 = min(l["bbox"].x0 for l in lines)
    xs1 = max(l["bbox"].x1 for l in lines)
    rules = sorted({round(g.y0) for g in segs if g.width > (xs1 - xs0) * 0.5})
    # 제목: 첫 괘선 위 / 머리행: 첫 괘선과 '머리행 끝 괘선' 사이 (위쪽 절반 안의 마지막 괘선)
    title_lines = [l for l in lines if rules and l["bbox"].y1 <= rules[0] + 1 and rules[0] < H * 0.5]
    rest = [l for l in lines if l not in title_lines]
    header_lines: list[dict] = []
    super_lines: list[dict] = []
    upper = [r for r in rules[1:] if r < H * 0.55]
    if title_lines and upper:
        header_end = upper[-1]
        header_lines = [l for l in rest if l["bbox"].y1 <= header_end + 1]
        rest = [l for l in rest if l not in header_lines]
        # 머리행 안의 중간 괘선 위쪽 글은 여러 열에 걸친 '상위 머리행'(예: Construct and Question)
        mids = upper[:-1]
        if mids:
            super_lines = [l for l in header_lines if l["bbox"].y1 <= mids[-1] + 1]
            header_lines = [l for l in header_lines if l not in super_lines]
    note_lines: list[dict] = []
    if rules and rules[-1] > H * 0.6:
        note_lines = [l for l in rest if l["bbox"].y0 >= rules[-1] - 1]
        rest = [l for l in rest if l not in note_lines]
    body = rest
    if len(body) < 4:
        return None

    # 열: 줄 시작 x를 모아 무리 짓기 (글머리표 뒤 이어지는 줄은 몇 pt 안쪽이라 같은 무리)
    # 위첨자 각주 기호처럼 아주 짧은 줄은 열을 만들지 않는다
    col_starts = _clusters([l["bbox"].x0 for l in body if len(l["text"].strip()) > 3], gap=14)
    if len(col_starts) < 2 or len(col_starts) > 8:
        return None

    def col_of(x: float) -> int:
        idx = 0
        for i, c in enumerate(col_starts):
            if x >= c - 3:
                idx = i
        return idx

    for l in body:
        l["col"] = col_of(l["bbox"].x0)

    # 행: 첫 열(행 이름)에 새 글이 시작되는 위치
    label_lines = sorted([l for l in body if l["col"] == 0], key=lambda l: l["bbox"].y0)
    row_tops: list[float] = []
    prev_y1 = None
    for l in label_lines:
        if prev_y1 is None or l["bbox"].y0 - prev_y1 > l["size"] * 0.6:
            row_tops.append(l["bbox"].y0)
        prev_y1 = l["bbox"].y1
    if not row_tops:
        row_tops = [min(l["bbox"].y0 for l in body)]
    row_tops[0] = min(row_tops[0], min(l["bbox"].y0 for l in body))

    def row_of(y: float) -> int:
        idx = 0
        for i, t in enumerate(row_tops):
            if y >= t - 2:
                idx = i
        return idx

    ncol = len(col_starts)
    grid: list[list[list[dict]]] = [[[] for _ in range(ncol)] for _ in row_tops]
    for l in body:
        grid[row_of(l["bbox"].y0)][l["col"]].append(l)

    items: list[str] = []
    texts: list[str] = []
    bulleted: list[int] = []

    def add(lines_: list[dict]) -> int:
        h, t = _join(sorted(lines_, key=lambda x: (x["bbox"].y0, x["bbox"].x0)))
        items.append(h)
        texts.append(t)
        return len(items) - 1

    def cell_items(cell: list[dict]) -> list[int]:
        """칸 안에서 글머리표마다 항목 하나. 글머리표가 없으면 칸 전체가 한 항목."""
        cell = sorted(cell, key=lambda x: x["bbox"].y0)
        groups: list[list[dict]] = []
        for ln in cell:
            if ln["text"].strip().startswith(BULLETS) or not groups:
                groups.append([])
            groups[-1].append(ln)
        out = []
        for g in groups:
            first = g[0]
            stripped = re.sub(r"^\s*[•▪◦–]\s*", "", first["text"])
            has_bullet = stripped != first["text"]
            if has_bullet:
                first = {**first, "text": stripped,
                         "html": re.sub(r"^(\s|<[a-z]+>)*[•▪◦–]\s*", lambda mm: re.sub(r"[•▪◦–]\s*", "", mm.group(0)),
                                        first["html"])}
            idx = add([first] + g[1:])
            if has_bullet:
                bulleted.append(idx)
            out.append(idx)
        return out

    layout: dict[str, Any] = {"title": None, "super": None, "header": [None] * ncol, "rows": [], "note": None,
                              "bulleted": bulleted,
                              "col_x": [c - xs0 for c in col_starts] + [xs1 - xs0]}
    if title_lines:
        layout["title"] = [add([l]) for l in sorted(title_lines, key=lambda x: x["bbox"].y0)]
    if super_lines:
        layout["super"] = add(super_lines)
    if header_lines:
        for c in range(ncol):
            hl = [l for l in header_lines
                  if col_of((l["bbox"].x0 + l["bbox"].x1) / 2 - (l["bbox"].width / 2 if c == 0 else 0)) == c]
            if hl:
                layout["header"][c] = add(hl)
    for r, row in enumerate(grid):
        layout["rows"].append([cell_items(cell) if cell else [] for cell in row])
    if note_lines:
        layout["note"] = add(note_lines)

    bbox = pymupdf.Rect(xs0, min(l["bbox"].y0 for l in lines), xs1, max(l["bbox"].y1 for l in lines))
    size = sorted(l["size"] for l in body)[len(body) // 2]
    return {"kind": "table", "bbox": bbox, "size": size, "items": items, "layout": layout,
            "html": "", "text": "\n".join(texts)}


def table_pieces(block: dict, to_html, box_width: float = 560.0) -> list[str]:
    """번역된 표 블록 → HTML 조각들(제목 / 머리행+첫 행 / 다음 행 …).
    행 단위로 나눠 두어야 표가 한 쪽에 다 들어가지 않을 때 '(계속)' 쪽으로 넘길 수 있다.
    to_html: 번역 조각을 읽기용 HTML로 바꾸는 함수."""
    tr = block.get("tr_items") or block["items"]
    lay = block["layout"]
    get = lambda i: to_html(tr[i]) if i is not None else ""
    pieces: list[str] = []
    if lay.get("title"):
        pieces.append('<p class="tcap">' + "<br/>".join(get(i) for i in lay["title"]) + "</p>")
    xs = lay["col_x"]
    total = xs[-1] or 1
    # MuPDF는 퍼센트 폭을 무시하므로 pt 단위 폭을 쓴다 (행마다 따로 그린 표의 열이 어긋나지 않게).
    # 칸 안쪽 여백(좌우 0.3em)만큼 빼 준다.
    widths = [max(20, (xs[i + 1] - xs[i]) / total * box_width - 6) for i in range(len(xs) - 1)]
    head = ""
    if lay.get("super") is not None:
        head += (f'<tr><th style="width:{widths[0]:.0f}pt"></th>'
                 f'<th colspan="{len(widths) - 1}" class="sup">{get(lay["super"])}</th></tr>')
    if any(h is not None for h in lay["header"]):
        head += "<tr>" + "".join(f'<th style="width:{w:.0f}pt">{get(h)}</th>'
                                 for h, w in zip(lay["header"], widths)) + "</tr>"
    for r, row in enumerate(lay["rows"]):
        cells = []
        for c, (idxs, w) in enumerate(zip(row, widths)):
            bl = set(lay.get("bulleted", []))
            body = "".join(f'<p class="ti">• {get(i)}</p>' if i in bl else f'<p class="tp">{get(i)}</p>'
                           for i in idxs)
            src_txt = " ".join(re.sub(r"<[^>]+>", "", block["items"][i]) for i in idxs).strip()
            num = c > 0 and bool(re.fullmatch(r"[\d\s.,%()=<>+\-–—/n*]*", src_txt))
            cells.append(f'<td class="{"num" if num else ""}" style="width:{w:.0f}pt">{body}</td>')
        cls = "tbl first" if r == 0 else "tbl"
        if r == len(lay["rows"]) - 1:
            cls += " last"
        pieces.append(f'<table class="{cls}">' + (head if r == 0 else "") +
                      "<tr>" + "".join(cells) + "</tr></table>")
    if lay.get("note") is not None:
        pieces.append(f'<p class="tnote">{get(lay["note"])}</p>')
    return pieces
