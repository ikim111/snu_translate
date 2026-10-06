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

        # 3) 모든 칸이 열에 맞는 행만, 위아래로 이어지는 동안
        table_rows: list[list[dict]] = []
        for r in run:
            fit = [c for c in r if col_of(c["bbox"].x0) is not None]
            if not fit:
                if table_rows:
                    break
                continue
            if len(fit) < len(r) and table_rows and not is_candidate(fit):
                # 표 칸이 아닌 글이 끼어 있는 행(옆의 캡션 등)은 표 칸만 남긴다
                pass
            table_rows.append(fit)
        if sum(1 for r in table_rows if len(r) >= 2) < 3:
            i = j + 1
            continue
        for r in run:
            used.add(id(r))

        tables.append(_build(table_rows, col_x, col_of))
        i = j + 1
    return tables


def _build(rows: list[list[dict]], col_x: list[float], col_of) -> dict[str, Any]:
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

    # 머리행: 첫 행에 숫자가 없고 둘째 행에 숫자가 있으면
    def has_digit(cells: list[list[int]]) -> bool:
        return any(ch.isdigit() for cell in cells for i in cell for ch in texts[i])

    header: list[int | None] = [None] * ncol
    if len(grid) >= 2 and not has_digit(grid[0]) and has_digit(grid[1]):
        header = [cell[0] if cell else None for cell in grid[0]]
        grid = grid[1:]

    all_cells = [c for r in rows for c in r]
    x0 = min(c["bbox"].x0 for c in all_cells)
    x1 = max(c["bbox"].x1 for c in all_cells)
    bbox = pymupdf.Rect(x0, min(c["bbox"].y0 for c in all_cells), x1, max(c["bbox"].y1 for c in all_cells))
    size = sorted(c["size"] for c in all_cells)[len(all_cells) // 2]
    layout = {"title": None, "super": None, "header": header, "rows": grid, "note": None, "bulleted": [],
              "col_x": [x - x0 for x in col_x] + [x1 - x0], "width": x1 - x0}
    return {"kind": "table", "bbox": bbox, "size": size, "items": items, "layout": layout,
            "html": "", "text": "\n".join(texts), "line_ids": ids}
