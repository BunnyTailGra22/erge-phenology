#!/usr/bin/env python3
"""從 Google Sheet 匯出的活頁簿重建物候資料，並注入 index.html。

Rebuild the phenology dataset from the exported workbook and inject it into
index.html.

    python3 tools/build.py source.xlsx

管線：解析年度分頁 → 跨年個體比對 → 分類（快取優先）→ phenology_data.json
→ 寫回 index.html 的 window.DATA。預設離線執行；只有踏查出現快取沒有的物種
時才需要 --online 去問 TaiCoL。

結束碼 0 表示成功（不論資料有無變動），非 0 表示這批資料不該發布。
"""

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import match
import sheet_parse
import taxonomy

PHASES = ("leaf", "flower", "fruit")
YEAR_TAB = re.compile(r"^\d{4}$")
DATA_MARKER = "window.DATA="

# 物候相的分級用詞跟著儀表板的圖例走，由踏查團隊定稿，不從標題列剖析。
PHASE_LABELS = {
    "leaf": ["空枝/落葉", "嫩芽/新嫩葉", "老葉/成熟"],
    "flower": ["無花/終花", "花芽/澎大", "優勢盛開"],
    "fruit": ["無果/掉落", "始熟", "正熟茂盛"],
}
TAXONOMY_SOURCE = "TaiCoL 為主，iNaturalist 為輔"

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEFAULT_CACHE = os.path.join(HERE, "taxonomy_cache.json")
DEFAULT_LOCKED = os.path.join(HERE, "locked_matches.json")
DEFAULT_HTML = os.path.join(ROOT, "index.html")
DEFAULT_JSON = os.path.join(ROOT, "phenology_data.json")


def empty_obs():
    return {p: [None] * sheet_parse.MONTHS for p in PHASES}


def load_years(workbook_path):
    """讀出所有年度分頁（分頁名為四位數年份者），依年份排序。"""
    import openpyxl

    wb = openpyxl.load_workbook(workbook_path, data_only=True)
    tabs = sorted(ws.title for ws in wb.worksheets if YEAR_TAB.match(ws.title))
    if not tabs:
        raise SystemExit("找不到任何年度分頁（分頁名需為四位數年份）")
    return tabs, {tab: sheet_parse.parse_year(wb[tab]) for tab in tabs}


def build(workbook_path, cache_path, locked_path, online=False):
    years, raw = load_years(workbook_path)
    master_year = years[-1]

    locked = []
    if os.path.exists(locked_path):
        with open(locked_path, encoding="utf-8") as fh:
            locked = json.load(fh)

    alias = match.build_alias_index(raw[master_year])
    partial_log, fallback_log = [], []
    master = match.normalize(raw[master_year], alias, is_master=True)
    rows = {y: (master if y == master_year else match.normalize(raw[y], alias, partial_log))
            for y in years}

    individuals = []
    for i, r in enumerate(master, start=1):
        individuals.append({
            "uid": f"U{i:03d}",
            "seq": i,
            "rid": r["rid"],
            "name": r["name"],
            "species": r["species"],
            "form": r["form"],
            "side": r["side"],
            "note": r["note"],
            "obs": {y: empty_obs() for y in years},
            "obs_years": [],
        })

    stats = {}
    for y in years:
        mapping, unmatched = match.match_year(
            master, rows[y], locked=locked if y != master_year else (),
            fallback_log=fallback_log)
        for i, j in mapping.items():
            individuals[j]["obs"][y] = rows[y][i]["obs"]
        in_sheet = sum(1 for r in rows[y] if r["has_data"]
                       for p in r["obs"].values() for v in p if v is not None)
        assigned = sum(1 for ind in individuals
                       for p in ind["obs"][y].values() for v in p if v is not None)
        stats[y] = {
            "rows": len(rows[y]),
            "values_in_sheet": in_sheet,
            "values_assigned": assigned,
            "dropped": [
                {"row": rows[y][i]["row"], "name": rows[y][i]["name"]} for i in unmatched
            ],
        }

    for ind in individuals:
        ind["obs_years"] = [y for y in years
                            if any(v is not None for p in ind["obs"][y].values() for v in p)]

    cache = taxonomy.load_cache(cache_path)
    species = sorted({ind["species"] for ind in individuals})
    resolved, added, missing = taxonomy.resolve(species, cache, online=online)
    if added:
        taxonomy.save_cache(cache, cache_path)
    for ind in individuals:
        ind.update(resolved[ind["species"]])

    notes = []
    if locked:
        notes.append(f"{len(locked)} 筆人工鎖定")
    if fallback_log:
        notes.append(f"{len(fallback_log)} 筆同物種遞補")
    data = {
        "meta": {
            "years": years,
            "n_individuals": len(individuals),
            "master_year": master_year,
            "phases": PHASE_LABELS,
            "taxonomy_source": TAXONOMY_SOURCE,
            "corrections": "；".join(notes) if notes else "無",
        },
        "individuals": individuals,
    }
    report = {
        "master_year": master_year,
        "years": stats,
        "species": len(species),
        "taxonomy_added": added,
        "taxonomy_missing": missing,
        "partial_name_matches": sorted({(a, b) for a, b, _ in partial_log}),
        "fallback_matches": fallback_log,
    }
    return data, report


def inject(html_path, data):
    """把資料寫回 index.html 的 window.DATA 那一行；回傳是否真的有變動。"""
    with open(html_path, encoding="utf-8") as fh:
        html = fh.read()
    start = html.find(DATA_MARKER)
    if start == -1:
        raise SystemExit(f"{html_path} 找不到 {DATA_MARKER}")
    end = html.index("\n", start)
    line = DATA_MARKER + json.dumps(data, ensure_ascii=False, separators=(", ", ": ")) + ";"
    if html[start:end] == line:
        return False
    with open(html_path, "w", encoding="utf-8") as fh:
        fh.write(html[:start] + line + html[end:])
    return True


def check(data, report, previous):
    """發布前的把關；回傳問題清單，非空就不該發布。"""
    problems = []
    for year, s in report["years"].items():
        if s["dropped"]:
            problems.append(
                f"{year}: {len(s['dropped'])} 列有觀測值卻對不上主檔 "
                + ", ".join(f"第{d['row']}列 {d['name']!r}" for d in s["dropped"][:5]))
    if report["taxonomy_missing"]:
        problems.append("分類查不到：" + "、".join(report["taxonomy_missing"]))
    if not data["individuals"]:
        problems.append("沒有任何個體")

    if previous:
        for year in previous["meta"]["years"]:
            if year not in data["meta"]["years"]:
                problems.append(f"{year} 年度分頁消失了")
                continue
            before = sum(1 for ind in previous["individuals"]
                         for p in ind["obs"].get(year, {}).values() for v in p if v is not None)
            after = report["years"][year]["values_assigned"]
            # 已完成的年度只會補記錄，不會整批消失；掉超過一成通常是分頁被動過。
            if after < before * 0.9:
                problems.append(f"{year} 觀測值由 {before} 掉到 {after}")
    return problems


def summarize(data, report, previous):
    lines = [f"主檔年度 {report['master_year']}｜個體 {data['meta']['n_individuals']}"
             f"｜物種 {report['species']}"]
    prev_counts = {}
    if previous:
        for ind in previous["individuals"]:
            for year, phases in ind["obs"].items():
                prev_counts[year] = prev_counts.get(year, 0) + sum(
                    1 for p in phases.values() for v in p if v is not None)
    for year in data["meta"]["years"]:
        now = report["years"][year]["values_assigned"]
        was = prev_counts.get(year)
        delta = "" if was is None else (f"（{was} → {now}）" if was != now else "（無變動）")
        lines.append(f"  {year}: {now} 筆觀測值{delta}")
    for label, key in (("部分名稱比對", "partial_name_matches"),
                       ("同物種遞補", "fallback_matches"),
                       ("新增分類", "taxonomy_added")):
        if report[key]:
            lines.append(f"  {label}: {report[key]}")
    return "\n".join(lines)


def read_existing(html_path):
    """讀出 index.html 目前內嵌的資料，用來比對這次的變動。"""
    try:
        with open(html_path, encoding="utf-8") as fh:
            html = fh.read()
    except OSError:
        return None
    start = html.find(DATA_MARKER)
    if start == -1:
        return None
    line = html[start + len(DATA_MARKER):html.index("\n", start)].rstrip().rstrip(";")
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        return None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("workbook", help="Google Sheet 匯出的 .xlsx")
    ap.add_argument("--html", default=DEFAULT_HTML)
    ap.add_argument("--json", dest="json_out", default=DEFAULT_JSON)
    ap.add_argument("--cache", default=DEFAULT_CACHE)
    ap.add_argument("--locked", default=DEFAULT_LOCKED)
    ap.add_argument("--online", action="store_true",
                    help="允許向 TaiCoL 查詢快取沒有的物種")
    ap.add_argument("--check-only", action="store_true", help="只檢查，不寫檔")
    args = ap.parse_args(argv)

    previous = read_existing(args.html)
    data, report = build(args.workbook, args.cache, args.locked, online=args.online)

    print(summarize(data, report, previous))
    problems = check(data, report, previous)
    if problems:
        print("\n把關未通過，這批資料不發布：", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1

    if args.check_only:
        print("\n--check-only：未寫入任何檔案")
        return 0

    with open(args.json_out, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
        fh.write("\n")
    changed = inject(args.html, data)
    print(f"\n{os.path.basename(args.json_out)} 已更新；"
          f"index.html {'已更新' if changed else '無變動'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
