"""解析二格山物候 Google Sheet 的年度分頁。

Parse the year tabs of the Mt. Erge phenology Google Sheet.

分頁欄位配置跨年不同（2022–2023 與 2024– 的月份區塊起始欄不一樣），
所以欄位一律由第 3 列的標題文字偵測，不寫死欄號。
"""

import re

MONTHS = 12
PHASE_MARKS = {"leaf": "葉", "flower": "花", "fruit": "果"}
HEADER_ROW = 3
MONTH_ROW = 4
FIRST_DATA_ROW = 6


def _s(v):
    """儲存格轉字串，去頭尾空白；空值回傳空字串。"""
    if v is None:
        return ""
    return str(v).strip()


def find_layout(ws):
    """由第 3/4 列標題推出各欄位置，回傳 dict。"""
    head = {c: _s(ws.cell(HEADER_ROW, c).value) for c in range(1, ws.max_column + 1)}
    layout = {}
    for col, text in head.items():
        if not text:
            continue
        if text.startswith("編號"):
            layout["rid"] = col
        elif text.startswith("植物名稱"):
            layout["name"] = col
        elif text.startswith("形相"):
            layout["form"] = col
        elif text.startswith("R&L"):
            layout["side"] = col
        elif text.startswith("H"):
            layout["height"] = col
        elif text.startswith("DBH"):
            layout["dbh"] = col
        elif text == "存":
            layout["alive"] = col
        elif text.startswith("換線"):
            layout["retag"] = col
        elif text.startswith("備註"):
            layout["note"] = col
        else:
            for phase, mark in PHASE_MARKS.items():
                if text.startswith(mark):
                    layout[phase] = col

    missing = [k for k in ("rid", "name", "form", "side", "note", *PHASE_MARKS) if k not in layout]
    if missing:
        raise ValueError(f"{ws.title}: 找不到欄位 {missing}")

    # 月份列必須是 1..12，否則欄位配置與預期不符。
    for phase in PHASE_MARKS:
        start = layout[phase]
        got = [_s(ws.cell(MONTH_ROW, start + i).value) for i in range(MONTHS)]
        want = [str(i + 1) for i in range(MONTHS)]
        if got != want:
            raise ValueError(f"{ws.title}: {phase} 月份標題為 {got}，預期 {want}")
    return layout


def parse_value(raw):
    """把儲存格值轉成 1/2/3；'?'、空白、其他文字視為未觀測 (None)。"""
    text = _s(raw)
    if not text:
        return None
    try:
        num = int(round(float(text)))
    except ValueError:
        return None
    return num if num in (1, 2, 3) else None


def parse_year(ws):
    """回傳該年度分頁的資料列（依步道行走順序）。"""
    layout = find_layout(ws)
    rows = []
    for r in range(FIRST_DATA_ROW, ws.max_row + 1):
        name = _s(ws.cell(r, layout["name"]).value)
        if not name:
            continue  # 區段標題列（只有第 1 欄）或空列
        obs = {}
        for phase in PHASE_MARKS:
            start = layout[phase]
            obs[phase] = [parse_value(ws.cell(r, start + i).value) for i in range(MONTHS)]
        rows.append({
            "row": r,
            "rid": _s(ws.cell(r, layout["rid"]).value),
            "name": name,
            "form": _s(ws.cell(r, layout["form"]).value),
            "side": _s(ws.cell(r, layout["side"]).value),
            "note": _s(ws.cell(r, layout["note"]).value),
            "obs": obs,
        })
    return rows


_PAREN = re.compile(r"[（(].*?[)）]")
_SPLIT = re.compile(r"[\n/／｜|]")


def species_of(name):
    """由『植物名稱』推出正規化物種名。

    名稱欄常見『中名／別名』『中名｜別名』或換行併寫兩個名字，並以 * 標記
    需留意的個體；取最前面那個名字、去掉括號註記與星號即可。
    """
    first = _SPLIT.split(name)[0]
    first = _PAREN.sub("", first)
    return first.replace("*", "").replace("＊", "").strip()


def variants_of(name):
    """列出名稱欄裡所有寫過的名字（中名與別名），供跨年比對。

    名稱欄常見『豬腳楠\n紅楠』『白背櫟／狹葉櫟』『日本/南投五月茶(密花五月茶)』
    這類一格兩名的寫法，而早年分頁可能只寫其中一個。把兩個名字都收下來，
    跨年比對時只要任一名字對得上就算同一株。
    """
    out = []
    for chunk in _SPLIT.split(name):
        chunk = chunk.strip()
        if not chunk:
            continue
        stripped = _PAREN.sub("", chunk).replace("*", "").replace("＊", "").strip()
        if stripped:
            out.append(stripped)
        # 整段都在括號裡時（例：『(密花五月茶)』），括號內容才是名字。
        inner = re.findall(r"[（(](.*?)[)）]", chunk)
        for token in inner:
            token = token.replace("*", "").replace("＊", "").strip()
            if token and token not in out:
                out.append(token)
    return out
