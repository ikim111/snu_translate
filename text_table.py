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
        for r in run:
            used.add(id(r))

        def nearest(c: dict) -> int:
            k = col_of(c)
            if k is not None:
                return k
            cx = (c["bbox"].x0 + c["bbox"].x1) / 2
            return min(range(len(groups)), key=lambda k: abs((groups[k][0] + groups[k][1]) / 2 - cx))

        # 머리행 위에 여러 열을 덮는 한 줄('Implementation')이 있으면 묶음 머리글
        super_row = None
        cand = ranged[top - 1] if top - 1 >= 0 else None
        if cand is None:
            k0 = next((k for k, r in enumerate(rows) if r is run[0]), 0)
            cand = [c for c in rows[k0 - 1] if in_range(c)] if k0 > 0 else None
        if cand and len(cand) == 1 and ranged[top][0]["_yc"] - cand[0]["_yc"] <= cand[0]["size"] * 2.6 \
                and cand[0]["bbox"].x0 > groups[0][1]:
            super_row = cand[0]
        # 데이터 바로 위 머리행 줄이 열을 더 잘게 나누면(빈칸이 많은 표) 그 칸들을 열 기준으로 쓴다
        if first - 1 >= top:
            hdr = sorted(ranged[first - 1], key=lambda c: c["bbox"].x0)
            if len(hdr) > len(groups) and all(hdr[k + 1]["bbox"].x0 > hdr[k]["bbox"].x1 for k in range(len(hdr) - 1)):
                # 첫 열(행 머리글)은 데이터 칸 머리글보다 왼쪽에 있다: 기존 첫 그룹을 유지
                left = [g for g in groups if g[1] < hdr[0]["bbox"].x0]
                new = [list(g) for g in left] + [[c["bbox"].x0, c["bbox"].x1, 1] for c in hdr]
                for k in range(len(new) - 1):            # 이웃 열과의 가운데까지 넓힌다
                    mid = (new[k][1] + new[k + 1][0]) / 2
                    new[k][1], new[k + 1][0] = max(new[k][1], mid - 0.01), min(new[k + 1][0], mid + 0.01)
                groups[:] = new
                col_x[:] = [g[0] for g in groups]
        header_n = first - top          # 데이터 첫 행 위의 머리행 줄 수
        tables.append(_build(table_rows, col_x, nearest, header_n, super_row))
        i = j + 1
    return tables


def _build(rows: list[list[dict]], col_x: list[float], col_of, header_n: int = 0,
           super_row: dict | None = None) -> dict[str, Any]:
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

    header: list[int | None] = [None] * ncol
    if header_n == 0 and len(grid) >= 2 and not has_digit(grid[0]) and has_digit(grid[1]):
        header_n = 1
    if header_n:
        for c in range(ncol):
            idxs = [i for r in grid[:header_n] for i in r[c]]
            if idxs:
                items.append(" ".join(items[i] for i in idxs))
                texts.append(" ".join(texts[i] for i in idxs))
                header[c] = len(items) - 1
        grid = grid[header_n:]

    sup = add(super_row) if super_row is not None else None
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

    all_cells = [c for r in rows for c in r] + ([super_row] if super_row is not None else [])
    x0 = min(c["bbox"].x0 for c in all_cells)
    x1 = max(c["bbox"].x1 for c in all_cells)
    bbox = pymupdf.Rect(x0, min(c["bbox"].y0 for c in all_cells), x1, max(c["bbox"].y1 for c in all_cells))
    size = sorted(c["size"] for c in all_cells)[len(all_cells) // 2]
    layout = {"title": None, "super": sup, "header": header, "rows": grid, "note": None, "bulleted": [],
              "col_x": [x - x0 for x in col_x] + [x1 - x0], "width": x1 - x0}
    return {"kind": "table", "bbox": bbox, "size": size, "items": items, "layout": layout,
            "html": "", "text": "\n".join(texts), "line_ids": ids}
