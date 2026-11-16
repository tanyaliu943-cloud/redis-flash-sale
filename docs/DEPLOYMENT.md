# 雲端部署說明

## 免費展示版本

根目錄的 `render.yaml` 會建立位於新加坡的免費 Streamlit 網頁與免費 Redis Key Value。商品目錄在新的 Redis 尚未有商品時由 App 自動建立；使用 `HSETNX`，不會重設已存在的商品庫存或價格。

Render 免費方案適合課程展示，不適合保存重要資料：免費網頁閒置 15 分鐘可能休眠，免費 Key Value 只有 25 MB，而且重啟時資料會清空。官方目前沒有免費常駐背景工作者；因此免費版本不會在雲端執行 `worker.py`。線上頁面的下單資料會寫入 Redis Streams，但 Consumer Group、通知 Stream 和 Pub/Sub 的背景處理請在本機啟動 `worker.py` 示範。線上 Worker 狀態會顯示未啟動。

## 建立 Render 免費服務

1. 將專題公開發佈到 GitHub。
2. 登入 Render，選擇建立 Blueprint，連結專題 GitHub repository。
3. 確認 Blueprint 只建立一個免費 Web 服務和一個免費 Key Value，並選擇新加坡區域後建立。
4. Render 會設定內部 `REDIS_URL`，不必在程式碼或 GitHub 填寫 Redis 密碼。
5. 開啟部署產生的 `https://<服務名稱>.onrender.com` 網址；首次開啟時 App 會將 10 項商品資料寫入新 Redis。
6. 送出測試訂單並查看訂單分析。若要展示 Consumer Group、Worker 和 Pub/Sub，請在本機 Redis 環境啟動 `python worker.py`，並用本機 App 完成事件展示。

## 免費版本的功能差異

- 線上網頁：可瀏覽商品、搶購、管理訂單與查看寫入 Redis 的資料。
- 線上背景工作者：未部署；線上事件不會由 Worker 消費，也不會有雲端 Pub/Sub 即時通知。
- 本機展示：沿用目前的本機 Redis 與 `worker.py`，完整展示 Streams Consumer Group、重試、Dead Letter 和 Pub/Sub。
- 資料保存：Render 免費 Key Value 重啟時可能清空，請勿將它當成正式訂單資料庫。

## 本機使用雲端 Redis（選用）

若要讓本機 App 或 Worker 連到雲端 Redis，需先在 Render 啟用外部連線並限制可連入的 IP，再在本機設定 `REDIS_URL`。不要把含密碼的網址寫進 `.env.example`、截圖或 GitHub。

```powershell
$env:REDIS_URL = "rediss://default:<密碼>@<主機>:<連接埠>/0"
python worker.py
```

若日後願意使用付費常駐 Worker，可改用 Render 背景工作者並設定相同的 `REDIS_URL`；建立前請先檢查當時的費用。

## 參考文件

- [Render Blueprint YAML](https://render.com/docs/blueprint-spec)
- [Render Key Value](https://render.com/docs/key-value)
- [Render 免費方案限制](https://render.com/docs/free)
- [Render 價格](https://render.com/pricing)
- [redis-py 連線方式](https://redis.io/docs/latest/develop/clients/redis-py/connect/)
