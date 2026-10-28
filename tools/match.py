"""跨年個體比對：以最新年度為主檔，把各年度的資料列接上同一株植物。

Cross-year individual matching, anchored on the most recent year as the registry.

編號（掛牌號）跨年不穩定（留白、重複、重新編號），所以不當比對鍵，改用
  正規化物種名 + 形相 + 步道左右 + 同鍵出現順序（＝步道行走順序）
並逐層放寬。名稱本身也跨年漂移（『豬腳楠』／『紅楠』是同一株），因此先用
主檔名稱欄裡並列的別名把各年名稱收斂成同一個物種名。
"""

import collections

from sheet_parse import species_of, variants_of

_SIDE_KEEP = "RL"
_MIN_PARTIAL = 2
_UNSURE = "?？"


def _certain(name):
    """去掉辨識存疑的問號標記（『雀梅藤?』與『雀梅藤』視為同名）。"""
    return name.strip().rstrip(_UNSURE).strip()


def side_of(raw):
    """步道左右正規化：全形轉半形、去雜訊；同時出現 R 與 L 視為 'R/L'。"""
    text = (raw or "").replace("Ｒ", "R").replace("Ｌ", "L").upper()
    found = [c for c in text if c in _SIDE_KEEP]
    if not found:
        return ""
    return "R/L" if len(set(found)) > 1 else found[0]


def build_alias_index(master_rows):
    """由主檔名稱欄的並列別名，建出 別名 → 主檔物種名（可多個）對照。

    一個名字可能同時是某株的主要名稱、又是另一株的別名（主檔同時有『紅楠』與
    『豬腳楠／紅楠』），所以對照值是一串候選；主要名稱排在別名前面。
    """
    alias = collections.defaultdict(list)

    def add(key, species):
        key = _certain(key)
        if key and species not in alias[key]:
            alias[key].append(species)

    # 第一輪：主檔某株的「主要名稱」優先指向牠自己。
    for r in master_rows:
        add(species_of(r["name"]), species_of(r["name"]))
    # 第二輪：名稱欄裡並列的其他別名，接在後面。
    for r in master_rows:
        for name in variants_of(r["name"]):
            add(name, species_of(r["name"]))
    return dict(alias)


def candidate_species(name, alias, partial_log=None):
    """列出某年度寫的名稱可能對應到的主檔物種名，依把握程度排序。

    一個名字可能指向不只一株：主檔同時有『紅楠』與『豬腳楠／紅楠』兩列時，
    早年只寫『紅楠』的那列兩邊都說得通，交由後續的形相／左右／順序決定。
    依序收集：完全相同的別名 → 部分相符（一方是另一方的前綴或後綴，例如早年
    寫『短柱』而主檔寫『短柱山茶』）。部分相符會記錄下來供人工複核。
    """
    names = [n for n in (_certain(v) for v in variants_of(name)) if n]
    out = []
    for candidate in names:
        for species in alias.get(candidate, ()):
            if species not in out:
                out.append(species)
    if out:
        # 有名字對得上就不再猜。部分相符只是最後的補救，範圍太寬，
        # 拿來跟精確結果並列反而會多出『長尾栲→鋸齒長尾栲』這種假候選。
        return out
    for candidate in names:
        if len(candidate) < _MIN_PARTIAL:
            continue
        for known, owners in alias.items():
            if len(known) < _MIN_PARTIAL:
                continue
            if known.startswith(candidate) or known.endswith(candidate) \
               or candidate.startswith(known) or candidate.endswith(known):
                for species in owners:
                    if species not in out:
                        if partial_log is not None:
                            partial_log.append((name, known, species))
                        out.append(species)
    return out or [species_of(name)]


def has_data(row):
    """該列是否留下任何觀測值。"""
    return any(v is not None for phase in row["obs"].values() for v in phase)


def normalize(rows, alias, partial_log=None, is_master=False):
    """替每列補上比對用欄位（收斂後物種名、左右、各層序號）。

    序號一律照步道行走順序（列序）計算，之後不論用哪一層比對都取同一組序號。
    主檔年度不做名稱收斂——主檔就是物種名的依據，收斂反而會把兩株互為別名的
    個體壓成同一個物種名。
    """
    seen = collections.Counter()
    form_seen = collections.Counter()
    species_seen = collections.Counter()
    name_seen = collections.Counter()
    for r in rows:
        r["candidates"] = [species_of(r["name"])] if is_master \
            else candidate_species(r["name"], alias, partial_log)
        r["species"] = r["candidates"][0]
        r["side_n"] = side_of(r["side"])
        # 每個候選物種各自累計順序，換一個候選也還是照步道行走順序數。
        ranks = {}
        for species in r["candidates"]:
            seen[(species, r["form"], r["side_n"])] += 1
            form_seen[(species, r["form"])] += 1
            species_seen[species] += 1
            ranks[species] = (
                seen[(species, r["form"], r["side_n"])],
                form_seen[(species, r["form"])],
                species_seen[species],
            )
        r["ranks"] = ranks
        r["nth"] = ranks[r["species"]][0]
        r["form_nth"] = ranks[r["species"]][1]
        r["species_nth"] = ranks[r["species"]][2]
        name_seen[r["name"]] += 1
        r["name_nth"] = name_seen[r["name"]]
        r["has_data"] = has_data(r)
    return rows


def add_alias(alias, name, species):
    """把一個名稱（含其並列別名）登記到對照表，指向指定的物種名。"""
    for variant in [species] + variants_of(name):
        key = _certain(variant)
        if not key:
            continue
        owners = alias.setdefault(key, [])
        if species not in owners:
            owners.append(species)


def rank_registry(registry):
    """重算登錄主檔每一列的各層序號（依登錄順序）。

    主檔年度已除名、但往年留有觀測值的個體會被接在後面，接上之後序號要重編，
    否則更早的年度會對不上它們。
    """
    seen = collections.Counter()
    form_seen = collections.Counter()
    species_seen = collections.Counter()
    for r in registry:
        seen[(r["species"], r["form"], r["side_n"])] += 1
        form_seen[(r["species"], r["form"])] += 1
        species_seen[r["species"]] += 1
        r["nth"] = seen[(r["species"], r["form"], r["side_n"])]
        r["form_nth"] = form_seen[(r["species"], r["form"])]
        r["species_nth"] = species_seen[r["species"]]
    return registry


def _index(master_rows):
    """主檔的三層索引：嚴格鍵 → 忽略左右 → 只看物種。"""
    strict, no_side, species_only = {}, {}, {}
    for i, r in enumerate(master_rows):
        strict[(r["species"], r["form"], r["side_n"], r["nth"])] = i
        no_side.setdefault((r["species"], r["form"], r["form_nth"]), i)
        species_only.setdefault((r["species"], r["species_nth"]), i)
    return strict, no_side, species_only


def match_year(master_rows, rows, locked=(), fallback_log=None):
    """把某年度的資料列對應到主檔；回傳 ({年度列索引: 主檔列索引}, 未命中索引)。

    逐層放寬：
      1. 物種 + 形相 + 左右 + 序號
      2. 物種 + 形相 + 序號（左右在早年常留白或寫成 'L (R)'）
      3. 物種 + 序號（形相在早年也可能留白）
      4. 同物種任一剩餘名額（最後手段，會記入 fallback_log 供人工複核）
    一個主檔個體只會被認領一次；沒有觀測值的列不參與配對。
    """
    strict, no_side, species_only = _index(master_rows)
    taken = set()
    mapping = {}

    # 人工鎖定優先，且不再參與自動比對。
    for lock in locked:
        target = strict.get((
            lock["master_species"], lock["master_form"],
            lock["master_side"], lock.get("master_nth", 1),
        ))
        if target is None:
            continue
        for i, r in enumerate(rows):
            if r["name"] == lock["name"] and r["name_nth"] == lock.get("nth", 1):
                mapping[i] = target
                taken.add(target)
                break

    # 只替有觀測值的列配對。留白的列不帶任何資訊，配上了也不會改變輸出，
    # 卻會佔掉名額、把真正有資料的列擠掉，所以直接略過。
    order = [i for i, r in enumerate(rows) if r["has_data"]]
    tiers = (
        lambda r, sp, rk: strict.get((sp, r["form"], r["side_n"], rk[0])),
        lambda r, sp, rk: no_side.get((sp, r["form"], rk[1])),
        lambda r, sp, rk: species_only.get((sp, rk[2])),
    )
    for tier in tiers:
        for i in order:
            if i in mapping:
                continue
            r = rows[i]
            for species in r["candidates"]:
                cand = tier(r, species, r["ranks"][species])
                if cand is not None and cand not in taken:
                    mapping[i] = cand
                    taken.add(cand)
                    break

    # 最後一層：同物種還有空位就補上。年度分頁該物種的列數多於主檔時（掛牌
    # 增減、同株分列），這層確保帶著觀測值的列不會整筆掉在地上。
    free = collections.defaultdict(list)
    for j, r in enumerate(master_rows):
        if j not in taken:
            free[r["species"]].append(j)
    for i in order:
        if i in mapping:
            continue
        for species in rows[i]["candidates"]:
            pool = free.get(species)
            if pool:
                j = pool.pop(0)
                mapping[i] = j
                taken.add(j)
                if fallback_log is not None:
                    fallback_log.append((rows[i]["name"], rows[i]["row"], master_rows[j]["row"]))
                break

    unmatched = [i for i in order if i not in mapping]
    return mapping, unmatched
