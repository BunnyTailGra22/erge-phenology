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
import collections
import datetime as _dt
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
# 2027 起的代號：把舊的 1 拆成 0（尚未開始）與 1（已終止），
# 「結束」與「物候期長度」因此可以實測。v2 可無損退回 v1（0、1 併回 1）。
PHASE_LABELS_V2 = {
    "leaf": ["空枝(未展葉)", "落葉中", "嫩芽/新嫩葉", "老葉/成熟"],
    "flower": ["無花", "終花", "花芽/澎大", "盛開"],
    "fruit": ["無果", "掉落/散播", "始熟(未熟果)", "正熟"],
}
TAXONOMY_SOURCE = "TaiCoL 為主，iNaturalist 為輔"
# 表上有這幾段的區段標題與個體列，但從未定期踏查（踏查團隊 2026-11 確認），
# 不列入路段，也不列入個體。比對時忽略標題尾端的問號（例：「天梯?」）。
UNMONITORED_SEGMENTS = ("羊腸小徑", "天梯", "土之道")


def _seg_key(seg):
    return (seg or "").strip().rstrip("?？").strip()


def drop_unmonitored(individuals):
    """移除未定期踏查路段的個體；回傳 (保留的, 移除的, 有觀測值而保留的)。

    這些路段的個體若哪天出現觀測值，就不再是「沒有定期紀錄」，保留下來並在
    把關時列出，交給人判斷，不默默丟掉資料。
    """
    keep, dropped, flagged = [], [], []
    for ind in individuals:
        if _seg_key(ind.get("segment")) not in UNMONITORED_SEGMENTS:
            keep.append(ind)
        elif ind.get("obs_years"):
            keep.append(ind)
            flagged.append(ind)
        else:
            dropped.append(ind)
    return keep, dropped, flagged


def recount_meta(meta, individuals, years):
    """個體增減後，重算 meta 裡跟個體數有關的欄位。"""
    segments = []
    for ind in individuals:
        if ind["segment"] and ind["segment"] not in segments:
            segments.append(ind["segment"])
    meta.update({
        "n_individuals": len(individuals),
        "segments": segments,
        "n_gone_cells": sum(1 for i in individuals for y in years
                            for g in i["gone"][y] if g),
        "n_alive": sum(1 for i in individuals if i["alive"]),
        "n_dead": sum(1 for i in individuals if not i["alive"]),
    })
    return segments

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEFAULT_CACHE = os.path.join(HERE, "taxonomy_cache.json")
DEFAULT_LOCKED = os.path.join(HERE, "locked_matches.json")
DEFAULT_HTML = os.path.join(ROOT, "index.html")
DEFAULT_JSON = os.path.join(ROOT, "phenology_data.json")


def empty_obs():
    return {p: [None] * sheet_parse.MONTHS for p in PHASES}


def empty_gone():
    return [False] * sheet_parse.MONTHS


def load_years(workbook_path, warn=None):
    """讀出所有年度分頁（分頁名為四位數年份者），依年份排序。

    同時取出每年的實際踏查日與代號版本：前者讓起始時間落在真正的日期上，
    後者讓 2027 的新代號（含 0）被正確解讀。
    """
    import openpyxl

    wb = openpyxl.load_workbook(workbook_path, data_only=True)
    tabs = sorted(ws.title for ws in wb.worksheets if YEAR_TAB.match(ws.title))
    if not tabs:
        raise SystemExit("找不到任何年度分頁（分頁名需為四位數年份）")
    rows = {tab: sheet_parse.parse_year(wb[tab], warn) for tab in tabs}
    days = {tab: sheet_parse.survey_days(wb[tab]) for tab in tabs}
    books = {tab: sheet_parse.codebook_of(wb[tab]) for tab in tabs}
    return tabs, rows, days, books


def year_stats(year_rows, individuals, year, unmatched):
    return {
        "rows": len(year_rows),
        "values_in_sheet": sum(1 for r in year_rows if r["has_data"]
                               for p in r["obs"].values() for v in p if v is not None),
        "values_assigned": sum(1 for ind in individuals
                               for p in ind["obs"][year].values() for v in p if v is not None),
        "dropped": [{"row": year_rows[i]["row"], "name": year_rows[i]["name"]}
                    for i in unmatched],
    }


def build(workbook_path, cache_path, locked_path, online=False, asof=None):
    unreadable = []
    years, raw, survey_days, codebooks = load_years(workbook_path, unreadable)
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

    def make_individual(row, seq):
        return {
            "uid": f"U{seq:03d}",
            "seq": seq,
            "rid": row["rid"],
            "name": row["name"],
            "species": row["species"],
            "form": row["form"],
            "side": row["side"],
            "note": row["note"],
            "segment": row.get("segment", ""),
            "alive": row.get("alive", True),
            "retired": False,
            "obs": {y: empty_obs() for y in years},
            # 該月該株已不在（物候格的 X）；分母要把這些月份扣掉。
            "gone": {y: empty_gone() for y in years},
            "obs_years": [],
        }

    # 登錄主檔以最新年度為準，但主檔年度除名的個體若往年留有觀測值，會接在
    # 後面繼續存在（retired），否則那些紀錄會隨著今年的除名一起消失。
    registry = list(master)
    individuals = [make_individual(r, i) for i, r in enumerate(master, start=1)]
    for i in range(len(master)):
        individuals[i]["obs"][master_year] = master[i]["obs"]
        individuals[i]["gone"][master_year] = master[i].get("gone", empty_gone())

    stats = {master_year: year_stats(master, individuals, master_year, [])}
    carried = []
    # 由新到舊：較晚的年度先把除名個體接進登錄主檔，更早的年度才對得上它們。
    for y in reversed(years[:-1]):
        mapping, unmatched = match.match_year(
            registry, rows[y], locked=locked, fallback_log=fallback_log)
        for i, j in mapping.items():
            individuals[j]["obs"][y] = rows[y][i]["obs"]
            individuals[j]["gone"][y] = rows[y][i].get("gone", empty_gone())
        still = []
        for i in unmatched:
            row = rows[y][i]
            if not row["has_data"]:
                continue
            registry.append(row)
            ind = make_individual(row, len(individuals) + 1)
            ind["retired"] = True
            ind["obs"][y] = row["obs"]
            ind["gone"][y] = row.get("gone", empty_gone())
            individuals.append(ind)
            carried.append({"name": row["name"], "species": row["species"], "last_year": y})
            match.add_alias(alias, row["name"], row["species"])
            still.append(i)
        if still:
            match.rank_registry(registry)
        stats[y] = year_stats(rows[y], individuals, y,
                              [i for i in unmatched if i not in still])

    for ind in individuals:
        ind["obs_years"] = [y for y in years
                            if any(v is not None for p in ind["obs"][y].values() for v in p)]
    # uid 在過濾前就編好，移除後不重編，分享連結裡的個體編號才不會變。
    individuals, unmonitored, unmonitored_with_data = drop_unmonitored(individuals)

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
    if carried:
        notes.append(f"{len(carried)} 株主檔已除名、保留歷史")
    meta = {
        "years": years,
        "master_year": master_year,
        "phases": PHASE_LABELS,
        "phases2": PHASE_LABELS_V2,
        "codebook": codebooks,
        "survey_days": survey_days,
        "unreadable": len(unreadable),
        "generated": _dt.date.today().isoformat(),
        "asof": asof or _dt.date.today().isoformat(),
        "taxonomy_source": TAXONOMY_SOURCE,
        "corrections": "；".join(notes) if notes else "無",
    }
    segments = recount_meta(meta, individuals, years)
    data = {"meta": meta, "individuals": individuals}
    report = {
        "master_year": master_year,
        "years": stats,
        "species": len(species),
        "taxonomy_added": added,
        "taxonomy_missing": missing,
        "partial_name_matches": sorted({(a, b) for a, b, _ in partial_log}),
        "fallback_matches": fallback_log,
        "carried_over": carried,
        "segments": segments,
        "unmonitored_dropped": len(unmonitored),
        "unmonitored_with_data": [(i["uid"], i["name"], i["segment"]) for i in unmonitored_with_data],
        "survey_months": {y: sum(1 for d in survey_days[y] if d) for y in years},
        "codebooks": codebooks,
        "zeros": {y: sum(1 for ind in individuals
                         for p in ind["obs"][y].values() for v in p if v == 0) for y in years},
        "unreadable": unreadable,
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
    # 路段與踏查日是表上一直有的欄位；突然歸零代表版面被動過而不是資料變了。
    if not report["segments"]:
        problems.append("路段為 0 段：區段標題列沒解析到，路段篩選與調查覆蓋會失去這個維度")
    if report.get("unmonitored_with_data"):
        problems.append("未定期踏查路段出現觀測值（已保留，請確認是否改為定期路段）："
                        + "、".join(f"{u} {n}（{s}）" for u, n, s in report["unmonitored_with_data"][:5]))
    blank_days = [y for y, n in report["survey_months"].items() if n == 0]
    if blank_days:
        problems.append(f"{'、'.join(blank_days)} 讀不到任何踏查日（第 5 列）")
    # 舊代號不該出現 0；出現就是分頁改了代號卻沒改標題。
    stray = {y: n for y, n in report["zeros"].items()
             if n and report["codebooks"].get(y) != "v2"}
    if stray:
        problems.append(f"舊代號(v1)分頁出現 0 值：{stray}")

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


def changelog(data, report, previous):
    """這次重建相對於上一版的差異，寫進 meta 讓『本月』頁可以直接顯示。"""
    out = {"n": data["meta"]["n_individuals"], "prev_n": None, "delta_n": None,
           "by_year": {}, "new_species": report["taxonomy_added"]}
    if not previous:
        return out
    out["prev_n"] = previous["meta"]["n_individuals"]
    out["delta_n"] = out["n"] - out["prev_n"]
    # 上一版還含有未定期踏查路段的個體、這次被排除時，株數變化是資料整理，
    # 不是現地增減；註明讓「本月」卡片不把它當成新紀錄或死亡。
    gone_unmon = [i for i in previous["individuals"]
                  if _seg_key(i.get("segment")) in UNMONITORED_SEGMENTS
                  and not i.get("obs_years")]
    if gone_unmon:
        out["cleanup"] = {"n": len(gone_unmon),
                          "segments": [s for s in UNMONITORED_SEGMENTS
                                       if any(_seg_key(i.get("segment")) == s for i in gone_unmon)]}
    for year in data["meta"]["years"]:
        before = sum(1 for ind in previous["individuals"]
                     for p in ind["obs"].get(year, {}).values() for v in p if v is not None)
        after = report["years"][year]["values_assigned"]
        if after != before:
            out["by_year"][year] = after - before
    return out


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
    lines.append(f"  路段 {len(report['segments'])} 段: {report['segments']}")
    lines.append(f"  未定期踏查路段移除 {report.get('unmonitored_dropped', 0)} 株: {list(UNMONITORED_SEGMENTS)}")
    lines.append(f"  存活 {data['meta']['n_alive']} 株 / 已枯死 {data['meta']['n_dead']} 株"
                 f" / 標記『該月已不在』{data['meta']['n_gone_cells']} 個月格")
    lines.append(f"  各年踏查月數: {report['survey_months']}｜代號版本: {report['codebooks']}")
    if report["unreadable"]:
        tally = collections.Counter(w["text"] for w in report["unreadable"])
        lines.append(f"  非空但讀不懂的儲存格 {len(report['unreadable'])} 格（未納入）: "
                     f"{tally.most_common(6)}")
        lines.append(f"    例如 {[w['at'] for w in report['unreadable'][:4]]}")
    for label, key in (("部分名稱比對", "partial_name_matches"),
                       ("同物種遞補", "fallback_matches"),
                       ("主檔除名保留", "carried_over"),
                       ("新增分類", "taxonomy_added")):
        if report[key]:
            lines.append(f"  {label}: {report[key]}")
    return "\n".join(lines)


def read_existing(html_path, json_path=None):
    """讀出上一版資料，用來比對這次的變動。

    以 index.html 內嵌的為準；改版面時那一行會暫時是模板佔位字串，這時退回
    讀 phenology_data.json——否則換一次 template 就會白白失去把關的比較基準。
    """
    def from_html():
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

    data = from_html()
    if data is None and json_path:
        try:
            with open(json_path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            data = None
    return data


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
    ap.add_argument("--previous", default=None,
                    help="比較基準的 json；預設讀 index.html 內嵌資料，讀不到時退回 --json")
    ap.add_argument("--asof", default=None,
                    help="來源 Sheet 的 modifiedTime (YYYY-MM-DD)，顯示為頁首的資料截止日")
    args = ap.parse_args(argv)

    previous = read_existing(args.html, args.previous or args.json_out)
    data, report = build(args.workbook, args.cache, args.locked,
                         online=args.online, asof=args.asof)
    data["meta"]["changelog"] = changelog(data, report, previous)

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
