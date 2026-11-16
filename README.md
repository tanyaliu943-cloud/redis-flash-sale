# 超市限量商品搶購系統

Redis 課程 Side Project：以多商品購物車、限量搶購、付款保留、FIFO 候補和模擬自動補貨為主題。購物車一次送出後，各商品分別建立訂單並各自進入付款或候補流程。商品選單包含 10 項日常食品；其中 4 項有 Open Food Facts 來源紀錄，其餘為明確標示的專題模擬資料。

## 加分項功能

- **更完整的功能**：多商品購物車、每人限購、品項獨立付款／候補、待付款保留、取消／逾時後遞補、模擬自動補貨、顧客訂單查詢與營運統計。
- **更好的 UI/UX**：商品、店員管理、訂單分析分頁；顯示庫存與候補人數；可查顧客訂單及篩選訂單狀態；低庫存補貨倒數與成功提示。
- **更多 Redis 進階功能**：Hash、String、List Queue、Sorted Set、Set 次級索引、Lua 原子下單與購物車冪等鍵、WATCH/MULTI/EXEC、Pipelines、Streams Consumer Group、Pub/Sub、失敗重試與 Dead Letter。

## 啟動方式

先確認 WSL Ubuntu 中的 Redis 服務正在執行，再於 Windows PowerShell：

```powershell
cd redis_flash_sale
python -m streamlit run app.py
```

預設連線到 `localhost:6379`。若使用其他 Redis 主機，可設定 `REDIS_URL`；使用 `rediss://` 開頭的網址時會以 TLS 連線。`worker.py` 和匯入程式也會使用相同設定。

第一次建立商品目錄時，在另一個 PowerShell 視窗執行：

```powershell
python import_open_data.py
```

匯入程式會補齊 10 項商品缺少的欄位，不會重設已存在商品的庫存或價格，也不會刪除 P003 測試商品、歷史訂單或 Redis 訂單資料。匯入後回到 App，按「重新整理商品資料」。

### 啟動背景事件工作者

先開一個 PowerShell 視窗啟動工作者，再開另一個視窗啟動 Streamlit：

```powershell
cd "C:\Users\User\Documents\Codex\2026-11-15\new-chat\outputs\redis_flash_sale"
python worker.py
```

工作者以 Stream Consumer Group 讀取新訂單事件，成功處理後才 `XACK`，並將通知存入 `notification:events`、同時發送 Pub/Sub。它會回收閒置超過 30 秒的待處理事件；連續失敗 3 次的事件會進入 `order:events:dead_letter`。首次啟動時從新事件開始，請在開始展示下單前先啟動工作者。Streamlit 的「訂單分析」分頁會顯示工作者狀態和處理數。

## 模擬自動補貨規則

- 補貨門檻：庫存小於或等於 1 件。
- 模擬到貨量：每次 5 件。
- 模擬交貨時間：排程後 15 秒。
- Streamlit 頁面連線期間，每 5 秒檢查付款逾時與補貨期限。
- 到貨時，系統在同一個 Redis Transaction 中增加庫存，並依 List 順序遞補可滿足的候補訂單。
- Streamlit 畫面開啟期間會監控全部 10 項展示商品，不只目前選擇的庫存概覽商品。
- 若庫存在到貨前已高於補貨門檻，系統會取消該補貨排程。

供應商與到貨為課程展示用模擬資料，不會真的向超市或供應商下單。

## Redis 資料模型

| 用途 | Redis 結構／Key 範例 |
|---|---|
| 商品資料 | Hash：`product:P004` |
| 訂單資料 | Hash：`order:<order_id>` |
| 購物車摘要／品項清單 | Hash/List：`cart:<cart_id>`、`cart:<cart_id>:orders` |
| 送單冪等鍵 | String：`idempotency:cart:<cart_id>:product:<product_id>`（24 小時） |
| 每人已購數量 | String counter：`user:<user_id>:product:<product_id>:quantity` |
| FIFO 候補 | List：`waitlist:P004`，`RPUSH` 入列、`LPOP` 遞補 |
| 付款期限 | Sorted Set：`payment:deadlines` |
| 補貨期限 | Sorted Set：`restock:deadlines` |
| 訂單總索引 | Set：`idx:orders:product:P004` |
| 訂單狀態索引 | Set：`idx:orders:product:P004:status:<status>` |
| 顧客訂單索引 | Set：`idx:orders:product:P004:user:<user_id>` |
| 訂單／補貨事件 | Stream：`order:events` |
| 背景通知工作佇列 | Consumer Group：`order-notification-workers` |
| 已處理通知 | Stream：`notification:events` |
| 失敗事件 | Dead Letter Stream：`order:events:dead_letter` |
| 即時通知 | Pub/Sub channel：`product:P004:updates`（由 `worker.py` 發布） |

## 商品與 Open Data

Open Food Facts 商品是 P004 Nutella、P005 Coca-Cola Original、P006 Prince 與 P007 100% Real Orange Juice；其中 P004、P007 是官方教學頁的靜態範例，P005、P006 有記錄日期的官方產品 API 查詢來源。P001、P002、P008–P011 為專題模擬商品。所有售價、庫存、每人限購與補貨數量均為模擬值。來源與引用細節見 [Open Data 來源整理](data/open_data_sources.md)。

## 展示與技術說明

加分項對照、Redis Command 範例、UML 流程與錄影腳本見 [加分項實作與展示](docs/加分項實作與展示.md)。

## 公開部署準備

本專題已支援以環境變數 `REDIS_URL` 連線到雲端 Redis，並提供 `requirements.txt`。`.env` 和 Streamlit 私密設定檔已列入 `.gitignore`；請把正式連線網址只填在部署平台的 Secret／Environment Variables 設定，不要放進 GitHub。

免費雲端展示使用 Streamlit 網頁和 Redis；商品目錄會在第一次開啟時自動建立。根目錄的 `render.yaml` 已備好 Render Blueprint。免費方案不部署常駐背景工作者，因此線上 Worker／Pub/Sub 不會運作；完整事件工作流程可在本機啟動 `worker.py` 示範。部署限制與初始化方式見 [雲端部署說明](docs/DEPLOYMENT.md)。

開發規劃與階段紀錄可見 [Notion 專題紀錄草稿](docs/NOTION_PROJECT_LOG.md)。
