"""
text_table.py — 괘선이 없거나 적은 '글자로만 된 표'를 찾아 칸 단위로 읽어 낸다.

예: Konold 외(2015)의 Table 1처럼 이름 / 시간 / 점수가 같은 x 위치에 줄줄이 놓인 표.
일반 추출로는 'Angela 21 61'처럼 한 줄로 뭉개지거나 머리행이 러닝 헤더로 오인된다.

방법
  1. 짧은 줄(25자 이하, 폭이 페이지의 30% 미만)만 모아 같은 높이끼리 '행'으로 묶는다.
  2. 짧은 칸이 2개 이상, 서로 15pt 이상 떨어져 있는 행을 '후보 행'으로 본다.
  3. 후보 행들의 칸 시작 x를 모아, 여러 행에서 반복되는 위치를 '열'로 정한다.
  4. 모든 칸이 열에 맞는 행이 위아래로 붙어 이어지는 구간을 표로 본다 (후보 행 3개 이상).
     '. . .'처럼 첫 열에만 있는 행도 표 안에 있으면 포함한다.

결과는 rotated_table.table_pieces()로 그릴 수 있는 같은 형식의 블록이다.
2단 본문은 줄이 길어서 짧은 줄 조건에 걸리지 않으므로 표로 오인되지 않는다.
"""
from __future__ import annotations

import os
import re
from typing import Any

import pymupdf

MAX_CELL_CHARS = 34
MIN_GAP = 15
COL_TOL = 6


def find_text_tables(lines: list[dict], page_rect: pymupdf.Rect) -> list[dict[str, Any]]:
    """lines: [{"html","text","bbox": Rect, "size", "id"}] (그림 영역 밖, 가로 방향 줄)
    반환: 표 블록 목록. 각 블록의 "line_ids"에 표에 포함된 줄 id가 들어 있다."""
    W = page_rect.width
    short = [l for l in lines if len(l["text"].strip()) <= MAX_CELL_CHARS and l["bbox"].width < W * 0.3]
    if len(short) < 6:
        return []

    # 1) 같은 높이끼리 행으로
    rows: list[list[dict]] = []
    for l in sorted(short, key=lambda l: (l["bbox"].y0 + l["bbox"].y1) / 2):
        yc = (l["bbox"].y0 + l["bbox"].y1) / 2
        if rows and abs(yc - rows[-1][0]["_yc"]) <= 3:
            rows[-1].append({**l, "_yc": rows[-1][0]["_yc"]})
        else:
            rows.append([{**l, "_yc": yc}])
    for r in rows:
        r.sort(key=lambda c: c["bbox"].x0)

    def is_candidate(r: list[dict]) -> bool:
        return len(r) >= 2 and all(r[i + 1]["bbox"].x0 - r[i]["bbox"].x1 >= MIN_GAP for i in range(len(r) - 1))

    tables: list[dict[str, Any]] = []
    used: set[int] = set()
    i = 0
    while i < len(rows):
        if not is_candidate(rows[i]) or id(rows[i]) in used:
            i += 1
            continue
        # 2) 후보 행이 이어지는 구간을 넓게 잡고 열을 정한다
        j = i
        run = [rows[i]]
        while j + 1 < len(rows):
            gap = rows[j + 1][0]["_yc"] - rows[j][0]["_yc"]
            if gap > max(c["size"] for c in rows[j]) * 3.2:
                break
            j += 1
            run.append(rows[j])
        cands = [r for r in run if is_candidate(r)]
        if len(cands) < 3:
            i = j + 1
            continue
        # 머리행은 칸 사이가 좁아 후보 행이 아닐 수 있다 → 바로 위의 짧은 줄들(최대 4행)도 머리행 후보로 붙인다
        k = i
        x_first = min(c["bbox"].x0 for r in cands for c in r)
        while k - 1 >= 0 and len(run) - (j - i + 1) < 4 and id(rows[k - 1]) not in used:
            gap = rows[k][0]["_yc"] - rows[k - 1][0]["_yc"]
            if gap > max(c["size"] for c in rows[k - 1]) * 2.6:
                break
            # 표 제목·본문 줄(왼쪽 끝에서 시작하는 한 줄)은 머리행이 아니다
            if len(rows[k - 1]) == 1 and rows[k - 1][0]["bbox"].x0 < x_first + 15 \
                    and len(rows[k - 1][0]["text"].strip()) > 12:
                break
            k -= 1
            run.insert(0, rows[k])
        # 열: 여러 행의 칸이 가로로 겹치는 범위끼리 묶는다 (가운데 정렬된 숫자 칸도 한 열로)
        spans_x = sorted((c["bbox"].x0, c["bbox"].x1) for r in cands for c in r)
        groups: list[list[float]] = []          # [x0, x1, count]
        for x0, x1 in spans_x:
            if groups and x0 <= groups[-1][1] + 2:
                groups[-1][1] = max(groups[-1][1], x1)
                groups[-1][2] += 1
            else:
                groups.append([x0, x1, 1])
        groups = [g for g in groups if g[2] >= max(2, len(cands) * 0.25)]
        # 표 옆에 나란히 놓인 표 제목 줄들(긴 글, 숫자 없음)로만 이뤄진 열은 버린다 (Springer 'Table 1 …' 옆 배치)
        def caption_like(g: list[float]) -> bool:
            cells = [c for r in cands for c in r if c["bbox"].x0 >= g[0] - 0.5 and c["bbox"].x1 <= g[1] + 0.5]
            return bool(cells) and all(len(c["text"].strip()) > 25 and not any(ch.isdigit() for ch in c["text"])
                                       for c in cells)
        groups = [g for g in groups if not caption_like(g)]
        if len(groups) < 2:
            i = j + 1
            continue
        col_x = [g[0] for g in groups]
        if os.environ.get("TT_DEBUG"):
            print("GROUPS", [(round(g[0]), round(g[1]), g[2]) for g in groups], len(cands))

        def col_of(c: dict) -> int | None:
            x0, x1 = c["bbox"].x0, c["bbox"].x1
            best, ov = None, 0.0
            for k, g in enumerate(groups):
                o = min(x1, g[1]) - max(x0, g[0])
                if o > ov:
                    best, ov = k, o
            return best

        # 3) 표의 가로 범위 안에 있는 칸만 본다 (같은 높이의 다른 단 글은 무시)
        xmax = max(c["bbox"].x1 for r in cands for c in r) + 6
        xmin = col_x[0] - 10

        def in_range(c: dict) -> bool:
            return xmin <= c["bbox"].x0 <= xmax and c["bbox"].x1 <= xmax + 20

        ranged = [[c for c in r if in_range(c)] for r in run]
        ranged = [r for r in ranged if r]
        def data_like(r: list[dict]) -> bool:
            return is_candidate(r) and len(r) >= 2 and sum(col_of(c) is not None for c in r) >= 2

        def has_num(r: list[dict]) -> bool:
            return any(any(ch.isdigit() for ch in c["text"]) for c in r if col_of(c) != 0)

        # 첫 데이터 행: 숫자 칸이 있는 첫 행 (숫자 표가 아니면 첫 후보 행)
        numeric_table = sum(has_num(r) for r in ranged if data_like(r)) >= 2
        first = next((k for k, r in enumerate(ranged) if data_like(r) and (has_num(r) or not numeric_table)), None)
        if first is None:
            i = j + 1
            continue
        # 첫 데이터 행의 행 머리글이 위 줄에서 시작했으면('No or few explanations/' + 'justifications …') 그 줄부터
        while first - 1 >= 0:
            r, cur = ranged[first - 1], ranged[first]
            c0 = [c for c in cur if col_of(c) == 0]
            if len(r) == 1 and col_of(r[0]) == 0 and c0 and c0[0]["text"].strip()[:1].islower() \
                    and cur[0]["_yc"] - r[0]["_yc"] <= r[0]["size"] * 1.8:
                first -= 1
            else:
                break
        # 위로: 여러 줄 머리행(첫 열 밖에도 칸이 있는 행)을 모은다. 첫 열에만 있는 한 줄은 표 제목이라 멈춘다
        top = first
        while top - 1 >= 0:
            r = ranged[top - 1]
            gap = ranged[top][0]["_yc"] - r[0]["_yc"]
            only_col0 = all(col_of(c) == 0 for c in r) and len(r) == 1
            if only_col0 and top - 2 >= 0:
                # 첫 열 머리글('Occupation') 바로 위에 다른 열 머리글 줄이 붙어 있으면 머리행의 일부
                r2 = ranged[top - 2]
                if len(r2) >= 1 and any(col_of(c) not in (0, None) for c in r2) and \
                        r[0]["_yc"] - r2[0]["_yc"] <= r[0]["size"] * 2.0 and gap <= r[0]["size"] * 2.0:
                    only_col0 = False
            if gap > r[0]["size"] * 2.6 or only_col0:
                break
            top -= 1
        # 아래로: 행 간격이 크게 벌어지기 전까지
        bot = first
        while bot + 1 < len(ranged) and ranged[bot + 1][0]["_yc"] - ranged[bot][0]["_yc"] <= ranged[bot][0]["size"] * 3.2:
            bot += 1
        table_rows = ranged[top:bot + 1]
        if sum(1 for r in table_rows if len(r) >= 2) < 3:
            i = j + 1
            continue
        # 긴 칸(설명 글)은 '짧은 줄' 조건에서 빠졌을 수 있다 → 표 행과 같은 높이의 긴 줄을 그 행에 넣는다
        short_ids = {l["id"] for l in short}
        x_left = col_x[0] - 10
        x_right = max(c["bbox"].x1 for r in cands for c in r) + 20
        y_top = table_rows[0][0]["_yc"] - 3
        y_bot = table_rows[-1][0]["_yc"] + 3
        for l in lines:
            if l["id"] in short_ids or l["bbox"].x0 < x_left or l["bbox"].x0 > x_right:
                continue
            yc = (l["bbox"].y0 + l["bbox"].y1) / 2
            if not (y_top <= yc <= y_bot):
                continue
            r = min(table_rows, key=lambda r: abs(r[0]["_yc"] - yc))
            if abs(r[0]["_yc"] - yc) <= 3 and r[-1]["bbox"].x1 <= l["bbox"].x0 + 2:
                r.append({**l, "_yc": r[0]["_yc"]})
                r.sort(key=lambda c: c["bbox"].x0)
        for r in run:
            used.add(id(r))

        def nearest(c: dict) -> int:
            k = col_of(c)
            if k is not None:
                return k
            cx = (c["bbox"].x0 + c["bbox"].x1) / 2
            return min(range(len(groups)), key=lambda k: abs((groups[k][0] + groups[k][1]) / 2 - cx))

        # 머리행 위에 여러 열을 덮는 한 줄('Implementation')이 있으면 묶음 머리글
        # 데이터 바로 위 머리행 줄이 열을 더 잘게 나누면(빈칸이 많은 표) 그 칸들을 열 기준으로 쓴다
        if first - 1 >= top:
            hdr = sorted(ranged[first - 1], key=lambda c: c["bbox"].x0)
            if len(hdr) > len(groups) and all(hdr[k + 1]["bbox"].x0 > hdr[k]["bbox"].x1 for k in range(len(hdr) - 1)):
                left = [g for g in groups if g[1] < hdr[0]["bbox"].x0]
                new = [list(g) for g in left] + [[c["bbox"].x0, c["bbox"].x1, 1] for c in hdr]
                for k in range(len(new) - 1):            # 이웃 열과의 가운데까지 넓힌다
                    mid = (new[k][1] + new[k + 1][0]) / 2
                    new[k][1], new[k + 1][0] = max(new[k][1], mid - 0.01), min(new[k + 1][0], mid + 0.01)
                groups[:] = new
                col_x[:] = [g[0] for g in groups]
        # 머리행 위에 여러 열을 덮는 줄('Implementation', '1990-1991' / 'Site 1')이 있으면 묶음 머리글
        super_rows: list[dict] = []
        k0 = next((k for k, r in enumerate(rows) if any(c.get("id") == ranged[top][0].get("id") for c in r)), None)
        above = []
        if top - 1 >= 0:
            above = [ranged[k] for k in range(top - 1, -1, -1)]
        if k0 is not None:
            first_above = above[0] if above else None
            k_start = k0 - 1
            if first_above is not None:
                pass
            above = above + [[c for c in rows[k] if in_range(c)] for k in range(k_start, -1, -1)]
        ref_y = ranged[top][0]["_yc"]
        seen: set = set()
        for cand in above:
            if not cand:
                continue
            key = tuple(c.get("id") for c in cand)
            if key in seen:
                continue
            seen.add(key)
            if len(cand) != 1 or cand[0]["bbox"].x0 <= groups[0][1] \
                    or ref_y - cand[0]["_yc"] > max(cand[0]["size"], ranged[top][0]["size"]) * 2.6 \
                    or ref_y - cand[0]["_yc"] <= 0:
                break
            super_rows.insert(0, cand[0])
            ref_y = cand[0]["_yc"]
            if len(super_rows) >= 3:
                break
        header_n = first - top          # 데이터 첫 행 위의 머리행 줄 수
        tables.append(_build(table_rows, col_x, nearest, header_n, super_rows))
        i = j + 1
    return tables


def _build(rows: list[list[dict]], col_x: list[float], col_of, header_n: int = 0,
           super_rows: list[dict] | None = None) -> dict[str, Any]:
    items: list[str] = []
    texts: list[str] = []
    ids: list[int] = []

    def add(c: dict) -> int:
        items.append(c["html"].strip())
        texts.append(c["text"].strip())
        ids.append(c["id"])
        return len(items) - 1

    ncol = len(col_x)
    grid: list[list[list[int]]] = []
    for r in rows:
        cells: list[list[int]] = [[] for _ in range(ncol)]
        for c in r:
            cells[col_of(c)].append(add(c))
        grid.append(cells)

    # 머리행: 데이터 위의 여러 줄을 열마다 하나로 합친다 ('Native' + 'American %' → 'Native American %')
    def has_digit(cells: list[list[int]]) -> bool:
        return any(ch.isdigit() for cell in cells for i in cell for ch in texts[i])

    # 한 줄 머리행의 칸이 여러 데이터 열을 덮으면('Teacher 1' → Obs. 2열) 묶음 머리행으로 (colspan)
    group_row: list[tuple[int | None, int]] | None = None
    if header_n == 1 and len(grid) >= 2:
        hcells = sorted(rows[0], key=lambda c: c["bbox"].x0)
        data_cx = [[] for _ in range(ncol)]
        for r in rows[1:]:
            for c in r:
                k = col_of(c)
                data_cx[k].append((c["bbox"].x0 + c["bbox"].x1) / 2)
        cx = [sum(v) / len(v) if v else None for v in data_cx]
        hcx = [(hc["bbox"].x0 + hc["bbox"].x1) / 2 for hc in hcells]
        spans = [[] for _ in hcells]
        for k in range(1, ncol):
            if cx[k] is not None and hcells:
                j = min(range(len(hcells)), key=lambda j: abs(hcx[j] - cx[k]))
                spans[j].append(k)
        # 머리 칸이 첫 열(행 이름) 위에 있으면 묶음이 아니다
        if hcells and hcells[0]["bbox"].x1 < (cx[1] or 0) - 30:
            spans = [[]]
        if hcells and any(len(c) >= 2 for c in spans) and all(spans) and \
                all(spans[i][-1] < spans[i + 1][0] for i in range(len(spans) - 1)):
            group_row = []
            col = 1
            hidx = [i for r in grid[:1] for cell in r for i in cell]
            hmap = {texts[i]: i for i in hidx}
            for hc, cols in zip(hcells, spans):
                if cols[0] > col:
                    group_row.append((None, cols[0] - col))
                group_row.append((hmap.get(hc["text"].strip()), len(cols)))
                col = cols[-1] + 1
            if col < ncol:
                group_row.append((None, ncol - col))
            grid = grid[1:]
            header_n = 0
    # 여러 줄 머리행의 맨 윗줄이 칸 하나뿐이면('Residuals by ethnicity') 여러 열을 덮는 묶음 머리글
    extra_sup: list[int] = []
    # 첫 데이터 행이 모두 기울임·굵게('Code' | 'Subprocess')이고 다음 행은 아니면 그 행이 열 머리글
    def styled(r: list[list[int]]) -> bool:
        cells = [items[i] for cell in r for i in cell]
        return bool(cells) and all(re.fullmatch(r"\s*<(i|b)>.*</\1>\s*", h) for h in cells)
    title_idx: list[int] = []
    if len(grid) > header_n + 1 and styled(grid[header_n]) and not styled(grid[header_n + 1]) \
            and sum(1 for cell in grid[header_n] if cell) >= 2:
        # 그 위의 칸 하나짜리 줄들은 표 제목 ('APPENDIX B', 'Code and Subprocess Reference')
        while header_n >= 1 and sum(1 for cell in grid[0] if cell) == 1:
            title_idx += [i for cell in grid[0] for i in cell]
            grid = grid[1:]
            rows = rows[1:]
            header_n -= 1
        header_n += 1

    def continues_below(k: int) -> bool:
        """머리행 k번째 줄의 칸이 바로 아랫줄 같은 열 칸으로 이어지는지('Native' → 'American')."""
        if k + 1 >= header_n:
            return False
        c = next(c for c in rows[k])
        below = [d for d in rows[k + 1] if col_of(d) == col_of(c)]
        return bool(below) and below[0]["bbox"].y0 - c["bbox"].y1 <= c["size"] * 0.6
    while header_n >= 2 and sum(1 for cell in grid[0] if cell) == 1 and \
            max(sum(1 for cell in g if cell) for g in grid[1:header_n]) >= 3 and not grid[0][0] \
            and not continues_below(0):
        extra_sup += [i for cell in grid[0] for i in cell]
        grid = grid[1:]
        rows = rows[1:]
        header_n -= 1
    header: list[int | None] = [None] * ncol
    if header_n == 0 and group_row is None and len(grid) >= 2 and not has_digit(grid[0]) and has_digit(grid[1]):
        header_n = 1
    if header_n:
        for c in range(ncol):
            idxs = [i for r in grid[:header_n] for i in r[c]]
            if idxs:
                items.append(" ".join(items[i] for i in idxs))
                texts.append(" ".join(texts[i] for i in idxs))
                header[c] = len(items) - 1
        grid = grid[header_n:]

    sup = None
    if extra_sup:
        super_rows = list(super_rows or [])
    if super_rows or extra_sup:
        if not super_rows:
            super_rows = []
        items.append(" · ".join([c["html"].strip() for c in super_rows] + [items[i] for i in extra_sup]))
        texts.append(" · ".join([c["text"].strip() for c in super_rows] + [texts[i] for i in extra_sup]))
        ids.extend(c["id"] for c in super_rows)
        sup = len(items) - 1
    # 행 머리글의 둘째 줄('(n = 26)')만 있는 행은 앞 행 머리글에 붙인다
    merged: list[list[list[int]]] = []
    for r in grid:
        only0 = r[0] and not any(r[1:])
        if merged and only0 and all(texts[i].lstrip().startswith("(") for i in r[0]):
            merged[-1][0] += r[0]
            continue
        merged.append(r)
    grid = merged
    # 여러 줄 행 머리글의 첫 줄('No or few explanations/')만 있는 행은 다음 행 머리글 앞에 붙인다
    merged = []
    pending: list[int] = []
    for k, r in enumerate(grid):
        only0 = r[0] and not any(r[1:])
        nxt = grid[k + 1] if k + 1 < len(grid) else None
        if only0 and nxt and nxt[0] and texts[nxt[0][0]][:1].islower():
            pending += r[0]
            continue
        if pending:
            r = [pending + r[0]] + r[1:]
            pending = []
        merged.append(r)
    grid = merged

    all_cells = [c for r in rows for c in r] + list(super_rows or [])
    x0 = min(c["bbox"].x0 for c in all_cells)
    x1 = max(c["bbox"].x1 for c in all_cells)
    bbox = pymupdf.Rect(x0, min(c["bbox"].y0 for c in all_cells), x1, max(c["bbox"].y1 for c in all_cells))
    size = sorted(c["size"] for c in all_cells)[len(all_cells) // 2]
    layout = {"title": title_idx or None, "super": sup, "group_row": group_row, "header": header, "rows": grid, "note": None, "bulleted": [],
              "col_x": [x - x0 for x in col_x] + [x1 - x0], "width": x1 - x0}
    return {"kind": "table", "bbox": bbox, "size": size, "items": items, "layout": layout,
            "html": "", "text": "\n".join(texts), "line_ids": ids}
