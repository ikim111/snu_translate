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

from typing import Any

import pymupdf

MAX_CELL_CHARS = 25
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
        xs = sorted(c["bbox"].x0 for r in cands for c in r)
        cols: list[list[float]] = []
        for x in xs:
            if cols and x - cols[-1][-1] <= COL_TOL:
                cols[-1].append(x)
            else:
                cols.append([x])
        cols = [c for c in cols if len(c) >= max(2, len(cands) * 0.5)]
        if len(cols) < 2:
            i = j + 1
            continue
        col_x = [min(c) for c in cols]

        def col_of(x: float) -> int | None:
            for k, cx in enumerate(col_x):
                if abs(x - cx) <= COL_TOL:
                    return k
            return None

        # 3) 표의 가로 범위 안에 있는 칸만 본다 (같은 높이의 다른 단 글은 무시)
        xmax = max(c["bbox"].x1 for r in cands for c in r) + 6
        xmin = col_x[0] - 10

        def in_range(c: dict) -> bool:
            return xmin <= c["bbox"].x0 <= xmax

        ranged = [[c for c in r if in_range(c)] for r in run]
        ranged = [r for r in ranged if r]
        def data_like(r: list[dict]) -> bool:
            return is_candidate(r) and len(r) >= 2 and sum(col_of(c["bbox"].x0) is not None for c in r) >= 2

        def has_num(r: list[dict]) -> bool:
            return any(any(ch.isdigit() for ch in c["text"]) for c in r if col_of(c["bbox"].x0) != 0)

        # 첫 데이터 행: 숫자 칸이 있는 첫 행 (숫자 표가 아니면 첫 후보 행)
        numeric_table = sum(has_num(r) for r in ranged if data_like(r)) >= 3
        first = next((k for k, r in enumerate(ranged) if data_like(r) and (has_num(r) or not numeric_table)), None)
        if first is None:
            i = j + 1
            continue
        # 위로: 여러 줄 머리행(첫 열 밖에도 칸이 있는 행)을 모은다. 첫 열에만 있는 한 줄은 표 제목이라 멈춘다
        top = first
        while top - 1 >= 0:
            r = ranged[top - 1]
            gap = ranged[top][0]["_yc"] - r[0]["_yc"]
            only_col0 = all(col_of(c["bbox"].x0) == 0 for c in r) and len(r) == 1
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

        def nearest(x: float) -> int:
            return min(range(len(col_x)), key=lambda k: abs(col_x[k] - x))

        header_n = first - top          # 데이터 첫 행 위의 머리행 줄 수
        tables.append(_build(table_rows, col_x, nearest, header_n))
        i = j + 1
    return tables


def _build(rows: list[list[dict]], col_x: list[float], col_of, header_n: int = 0) -> dict[str, Any]:
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
            cells[col_of(c["bbox"].x0)].append(add(c))
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

    all_cells = [c for r in rows for c in r]
    x0 = min(c["bbox"].x0 for c in all_cells)
    x1 = max(c["bbox"].x1 for c in all_cells)
    bbox = pymupdf.Rect(x0, min(c["bbox"].y0 for c in all_cells), x1, max(c["bbox"].y1 for c in all_cells))
    size = sorted(c["size"] for c in all_cells)[len(all_cells) // 2]
    layout = {"title": None, "super": None, "header": header, "rows": grid, "note": None, "bulleted": [],
              "col_x": [x - x0 for x in col_x] + [x1 - x0], "width": x1 - x0}
    return {"kind": "table", "bbox": bbox, "size": size, "items": items, "layout": layout,
            "html": "", "text": "\n".join(texts), "line_ids": ids}
