# 資料管線 ｜ Data pipeline

把 Google Sheet 的踏查記錄重建成儀表板用的資料，並寫回 `index.html`。
Rebuilds the dashboard dataset from the survey Google Sheet and injects it into `index.html`.

## 一鍵重跑 ｜ One command

```bash
python3 -m pip install openpyxl
python3 tools/build.py path/to/source.xlsx
```

裝套件請用 `python3 -m pip`，不要用裸的 `pip`：有些環境裡 `pip` 與 `python3`
指向不同版本的 Python，裸 `pip` 會裝到跑不到的地方。

來源檔是 Google Sheet「**二格山稜線x植物物候踏查**」匯出的 `.xlsx`
(`1FTf9yk94tVLM2x2Tc_n3YnNeePBp7iLfMJvymup3O_w`)。
會更新 `phenology_data.json` 與 `index.html` 內嵌的 `window.DATA`。

常用選項：

| 選項 | 用途 |
|---|---|
| `--check-only` | 只跑解析與把關，不寫任何檔案 |
| `--force` | 資料與上一版相同時仍重寫（預設無變動就不寫檔） |
| `--asof` | 指定資料截止日；預設為最近一次踏查日 |
| `--online` | 允許向 TaiCoL 查詢快取沒有的物種（平常不需要） |
| `--html` / `--json` / `--cache` / `--locked` | 改用其他路徑 |

結束碼 `0` 代表這批資料可以發布，非 `0` 代表把關未通過、不應發布。

## 每週自動更新 ｜ Weekly automation

`.github/workflows/weekly-update.yml` 每週一 06:17（臺灣時間）自動執行：

1. 下載 Google Sheet（`fetch_sheet.py`）
2. 重建並把關（`build.py --online`；新物種會自動向 TaiCoL 查詢並寫入快取）
3. **資料有變動才** commit 到 `main`，GitHub Pages 隨之更新；沒變動就什麼都不做
4. 通知（GitHub issue，同一張持續更新，恢復後自動關閉）：
   * `⚠️ 每週資料更新失敗`——下載不到、把關未通過等，附執行紀錄
   * `⏰ 踏查資料超過 45 天未更新`——最近一次踏查日距今超過 45 天

也可在 GitHub → Actions → 每週資料更新 → **Run workflow** 手動執行；
勾選 `force` 會在資料沒變時仍重新注入（改了 `index.html` 版面後使用）。

**資料截止日**＝最近一次踏查日（第 5 列），不是執行當天。

### Sheet 存取（擇一）

| 方式 | 設定 | 適用 |
|---|---|---|
| 公開連結（預設） | Sheet → 共用 → 「知道連結的任何人」→ 檢視者 | 最簡單；Sheet 內容任何拿到連結的人都看得到 |
| 服務帳戶 | 在 Google Cloud 建服務帳戶並下載 JSON 金鑰 → 把服務帳戶 email 加為 Sheet 檢視者 → GitHub repo → Settings → Secrets and variables → Actions → 新增 secret `GOOGLE_SERVICE_ACCOUNT`（貼上整份 JSON） | Sheet 需保持不公開時 |

有設 `GOOGLE_SERVICE_ACCOUNT` 就用服務帳戶，否則用公開連結。換 Sheet 時可設 repo variable `SHEET_ID`。

### 通知會寄給誰

開 issue 會通知 repo 的 watcher。請踏查團隊負責人在 repo 首頁按 **Watch → All Activity**（或 Custom → Issues）。

> 公開 repo 若連續 60 天沒有任何 commit，GitHub 會自動停用排程。
> 正常每月都有新資料就不會發生；超過 45 天沒新踏查時會先收到上面的提醒。

## 檔案 ｜ Files

| 檔案 | 內容 |
|---|---|
| `build.py` | 主流程與發布前把關 |
| `fetch_sheet.py` | 下載 Sheet 為 .xlsx（公開連結或服務帳戶） |
| `sheet_parse.py` | 年度分頁解析（欄位由標題列偵測，不寫死欄號） |
| `match.py` | 跨年個體比對 |
| `taxonomy.py` | 分類與保育狀態（快取優先，TaiCoL 為後援） |
| `taxonomy_cache.json` | 物種 → 學名/科屬/特有/保育；跟著版本庫走，所以平常離線可跑 |
| `locked_matches.json` | 人工鎖定的跨年對應（自動比對修不好時才加） |

## 分頁格式 ｜ Sheet layout

* 分頁名為四位數年份者才會讀（`2022`…`2026`），其餘分頁忽略。**最新的年份即主檔**。
* 第 3 列是欄位標題、第 4 列是月份 1–12、第 6 列起是資料。
* 欄位位置**由標題文字偵測**：2022–2023 與 2024 以後的月份區塊起始欄不同，兩種都讀得動。
  標題若被改動到認不出來，會直接報錯而不是默默讀錯欄。
* 只有 `1` / `2` / `3` 會被當成觀測值；`?`、空白與其他文字一律視為未觀測。

## 跨年個體比對 ｜ Cross-year matching

編號（掛牌號）跨年不穩定，所以不當比對鍵。改以**最新年度為主檔**，用
`正規化物種名 + 形相 + 步道左右 + 同鍵出現順序` 逐層放寬比對：

1. 物種 + 形相 + 左右 + 順序
2. 物種 + 形相 + 順序（左右在早年常留白或寫成 `L (R)`）
3. 物種 + 順序（形相在早年也可能留白）
4. 同物種任一剩餘名額（最後手段，會列在報告裡供人工複核）

名稱本身也會漂移（`豬腳楠` / `紅楠` 是同一株）。主檔名稱欄裡並列的別名會自動
收成對照表，所以**不需要手動維護別名清單**——只要主檔那格把兩個名字都寫上。
一個名字可能同時是某株的主要名稱、又是另一株的別名，這種情況會保留多個候選，
交給形相／左右／順序決定。

沒有任何觀測值的列不參與比對：它們不帶資訊，配上了也不會改變輸出，卻會佔掉名額。

年度由新到舊處理。主檔年度已除名、但往年留有觀測值的個體，會接在登錄主檔後面
繼續存在並標記 `retired: true`（儀表板上顯示「已不再追蹤」徽章），更早的年度仍
對得上它們。否則踏查團隊只要從最新年度刪掉一列，那一株的整段歷史就會跟著消失。
沒有任何觀測值的除名列不會保留——本來就沒有東西要留。

自動比對命中率約 97–99%；真的修不好的再寫進 `locked_matches.json`：

```json
[{"name": "狹葉櫟", "nth": 1,
  "master_species": "白背櫟", "master_form": "", "master_side": "R", "master_nth": 1}]
```

## 發布前把關 ｜ Pre-publish checks

任何一項不過就以非 0 結束、不寫檔：

* 有觀測值的列對不上主檔（**正常情況應為 0**）
* 出現快取與 TaiCoL 都查不到的物種
* 個體數為 0
* 已完成年度的觀測值比上次少超過一成（通常代表分頁被動過）

主檔除名而保留下來的個體會列在報告的「主檔除名保留」，值得人看一眼：那代表
踏查團隊從最新年度拿掉了一株。

## 已知的資料脾氣 ｜ Known quirks

* `日本/南投五月茶` 會被正規化成物種名 **`日本`**（規則是取 `/` 前段）。
  現行儀表板一直是這樣，為了不動到既有資料而保留；要修的話得同時想好
  物種下拉與跨年比對的影響。
* `奧氏虎皮楠` 在 2023 分頁有 3 列、主檔只有 2 株，會用到第 4 層遞補。
  觀測值不會遺失，但落在哪一株帶有不確定性。
