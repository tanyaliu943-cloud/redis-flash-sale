# Open Data 來源整理

## Open Food Facts 商品欄位

本專題使用商品名稱、條碼與 Nutri-Score 作為外部開放資料匯入 Redis 的示範；售價、庫存、每人限購、補貨門檻與補貨量均為專題自行設定的模擬值。

| 商品 | Redis ID | 條碼 | Open Food Facts 欄位與取得方式 |
|---|---|---|---|
| Nutella（能多益榛果可可醬） | P004 | `3017624010701` | 商品名稱與 Nutri-Score E，取自官方 API 教學頁的靜態範例，不是本程式即時查詢 |
| Coca-Cola Original（可口可樂原味） | P005 | `5449000000996` | 商品名稱與 Nutri-Score E，官方產品 API 查詢紀錄（2026-11-15） |
| Prince（LU Prince 巧克力餅乾） | P006 | `7622210449283` | 商品名稱與 Nutri-Score E，官方產品 API 查詢紀錄（2026-11-15） |
| 100% Real Orange Juice（100% 柳橙汁） | P007 | `0180411000803` | 商品名稱與 Nutri-Score C，取自官方 API 教學頁的靜態範例，不是本程式即時查詢 |

P004 的範例保存於 `open_food_facts_sample.json`，P005–P007 的來源網址、取得方式和模擬欄位保存於 `open_food_facts_products.json`。匯入程式只寫入缺少的 Hash 欄位，不會重設已有商品的庫存或價格。

官方來源：

- [Open Food Facts API 教學](https://openfoodfacts.github.io/documentation/docs/Product-Opener/api/tutorial-off-api/)
- [Coca-Cola Original API 記錄](https://world.openfoodfacts.org/api/v2/product/5449000000996?fields=code,product_name,nutrition_grades,brands)
- [Prince API 記錄](https://world.openfoodfacts.org/api/v2/product/7622210449283?fields=code,product_name,nutrition_grades,brands)
- [Open Food Facts 授權說明](https://openfoodfacts.github.io/documentation/docs/Product-Opener/api/tutorials/license-be-on-the-legal-side/)

## 專題模擬商品

鮮奶、無糖豆漿、優格、吐司、早餐麥片與洋芋片（P001、P002、P008–P011）為專題模擬商品，不宣稱其名稱、條碼或營養資訊來自 Open Food Facts。所有商品的價格、庫存、限購數與補貨規則都只是搶購流程展示資料。

## 授權與資料品質

Open Food Facts 資料庫採 ODbL；產品頁面的資料與圖片可能適用不同授權條件。本專題沒有複製或使用產品圖片，引用與再利用方式以[官方授權說明](https://openfoodfacts.github.io/documentation/docs/Product-Opener/api/tutorials/license-be-on-the-legal-side/)為準。資料由社群維護，欄位可能缺漏或變動；P004、P007 使用官方教學靜態範例，P005、P006 是記錄日期的 API 查詢結果。

## 簡報引用文字

本專題選取 Open Food Facts 的 Nutella、Coca-Cola Original、Prince 與 100% Real Orange Juice 商品資料，示範將商品名稱、條碼與 Nutri-Score 匯入 Redis；其中部分資料取自官方 API 教學範例，另有部分為 2026-11-15 的產品 API 查詢紀錄。其他商品以及所有售價、庫存、限購與補貨值皆為專題模擬資料。資料庫與內容的再利用依 Open Food Facts 官方授權說明標示。
