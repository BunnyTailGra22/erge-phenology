"""解析二格山物候 Google Sheet 的年度分頁。

Parse the year tabs of the Mt. Erge phenology Google Sheet.

分頁欄位配置跨年不同（2022–2023 與 2024– 的月份區塊起始欄不一樣），
所以欄位一律由第 3 列的標題文字偵測，不寫死欄號。

除了物候代號，這裡也取出三個原本被丟掉、但表上一直都有的維度：
路段（無名稱、只有第 1 欄中文的區段標題列）、存活（『存』欄的 X）、
以及第 5 列記的實際踏查日——起始時間的不確定區間要靠它才算得出來。
"""

import re

MONTHS = 12
PHASE_MARKS = {"leaf": "葉", "flower": "花", "fruit": "果"}
HEADER_ROW = 3
MONTH_ROW = 4
SURVEY_DAY_ROW = 5
FIRST_DATA_ROW = 6
_CJK = re.compile(r'[\u4e00-\u9fff]')
_V2_HEADER = re.compile(r'0\s*空枝')


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


def parse_value(raw, warn=None, where=""):
    """把儲存格值轉成 0/1/2/3；空白視為未觀測 (None)。

    0 只會出現在 2027 起的新代號（0 未開始 / 1 已終止）；舊分頁本來就沒有 0。
    非空但讀不懂的儲存格（『1→2』的連續記法、『?』、『X』）仍然不納入資料，
    但會記進 ``warn``——以前是靜默丟棄，看不出表上有多少格在流失。
    """
    text = _s(raw)
    if not text:
        return None
    try:
        num = int(round(float(text)))
    except ValueError:
        if warn is not None:
            warn.append({"at": where, "text": text[:16]})
        return None
    if num in (0, 1, 2, 3):
        return num
    if warn is not None:
        warn.append({"at": where, "text": text[:16]})
    return None


def survey_days(ws):
    """第 5 列記的『當月實際踏查日』，取 1–31 的整數，其餘為 None。

    有了實際日期，物候起始就能落在真正的踏查日上，不確定區間＝夾住它的
    前後兩次踏查間距，而不是籠統的半個月。
    """
    layout = find_layout(ws)
    start = layout["leaf"]
    out = []
    for i in range(MONTHS):
        text = _s(ws.cell(SURVEY_DAY_ROW, start + i).value)
        try:
            day = int(round(float(text)))
        except ValueError:
            day = None
        out.append(day if day and 1 <= day <= 31 else None)
    return out


def codebook_of(ws):
    """分頁採用的代號版本：葉標題寫出『0 空枝』就是 2027 起的 v2。"""
    layout = find_layout(ws)
    return "v2" if _V2_HEADER.search(_s(ws.cell(HEADER_ROW, layout["leaf"]).value)) else "v1"


def parse_year(ws, warn=None):
    """回傳該年度分頁的資料列（依步道行走順序）。

    沿路的區段標題列（『火|蜀道>迎賓亭』等，只有第 1 欄有字）本身不是個體，
    但會把路段帶給它底下的每一株——這是表上一直記著、卻從未進到儀表板的
    空間維度。
    """
    layout = find_layout(ws)
    rows = []
    segment = ""
    for r in range(FIRST_DATA_ROW, ws.max_row + 1):
        name = _s(ws.cell(r, layout["name"]).value)
        if not name:
            head = _s(ws.cell(r, layout["rid"]).value)
            if len(head) >= 3 and _CJK.search(head):
                segment = head.replace("\n", " ")
            continue  # 區段標題列或空列
        obs = {}
        for phase in PHASE_MARKS:
            start = layout[phase]
            obs[phase] = [parse_value(ws.cell(r, start + i).value, warn,
                                      f"{ws.title}!r{r}") for i in range(MONTHS)]
        alive_raw = _s(ws.cell(r, layout["alive"]).value) if "alive" in layout else ""
        rows.append({
            "row": r,
            "rid": _s(ws.cell(r, layout["rid"]).value),
            "name": name,
            "form": _s(ws.cell(r, layout["form"]).value),
            "side": _s(ws.cell(r, layout["side"]).value),
            "note": _s(ws.cell(r, layout["note"]).value),
            "segment": segment,
            # 『存』欄打 X 代表該株已枯死（偶有全形 Ｘ）；留白視為仍存活。
            "alive": alive_raw.upper().replace("Ｘ", "X") != "X",
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
