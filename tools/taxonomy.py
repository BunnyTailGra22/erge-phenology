"""物種分類與保育狀態：以本地快取為主，需要時才問 TaiCoL。

Taxonomy and conservation status, cached locally; TaiCoL is queried only for
species the cache has never seen.

快取（taxonomy_cache.json）跟著版本庫走，所以平常整條管線離線就能跑完；
只有踏查新增了沒看過的物種時才需要連線，且連不上時會照常產出、把該物種標為
待補，不會讓整批更新失敗。
"""

import json
import urllib.parse
import urllib.request

TAICOL_API = "https://api.taicol.tw/v2/taxon"
FIELDS = (
    "sci", "zh", "fam", "gen", "fam_sci", "gen_sci", "order",
    "endemic", "redlist", "iucn", "cites", "protected", "sensitive",
    "threat", "taxsrc",
)
EMPTY = dict.fromkeys(FIELDS)
_UNSURE = "?？"


def cache_key(species):
    """查詢用的物種名：去掉辨識存疑的問號（『刺杜密?』與『刺杜密』同一種）。"""
    return species.strip().rstrip(_UNSURE).strip()


def load_cache(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def save_cache(cache, path):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(cache, fh, ensure_ascii=False, indent=1, sort_keys=True)
        fh.write("\n")


def _threat_of(iucn, redlist):
    """把 IUCN 與國家紅皮書壓成一個受脅標記，沒有受脅疑慮就留空。"""
    for code in (iucn, redlist):
        if code and code not in ("NLC", "NA", "NE", "DD", "LC"):
            return code
    return None


def fetch_taicol(species, timeout=20):
    """向 TaiCoL 查一個中名；查不到或連不上都回傳 None（由呼叫端決定怎麼辦）。"""
    url = f"{TAICOL_API}?{urllib.parse.urlencode({'common_name': species})}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            payload = json.load(resp)
    except Exception:
        return None
    records = payload.get("data") or []
    if not records:
        return None
    rec = records[0]
    iucn = rec.get("iucn_category") or None
    redlist = rec.get("red_list_category") or None
    return {
        "sci": rec.get("simple_name") or rec.get("name") or None,
        "zh": rec.get("common_name_c") or species,
        "fam": rec.get("family_c") or None,
        "gen": rec.get("genus_c") or None,
        "fam_sci": rec.get("family") or None,
        "gen_sci": rec.get("genus") or None,
        "order": rec.get("order_c") or None,
        "endemic": bool(rec.get("is_endemic")),
        "redlist": redlist,
        "iucn": iucn,
        "cites": rec.get("cites_listing") or None,
        "protected": rec.get("protected_category") or None,
        "sensitive": rec.get("sensitive_category") or None,
        "threat": _threat_of(iucn, redlist),
        "taxsrc": "TaiCoL",
    }


def resolve(species_list, cache, online=False):
    """回傳 (物種 → 分類資料, 這次新查到的物種, 仍然查不到的物種)。"""
    resolved, added, missing = {}, [], []
    for species in species_list:
        key = cache_key(species)
        if key in cache:
            resolved[species] = cache[key]
            continue
        record = fetch_taicol(key) if online else None
        if record:
            cache[key] = record
            resolved[species] = record
            added.append(species)
        else:
            resolved[species] = dict(EMPTY)
            missing.append(species)
    return resolved, added, missing
