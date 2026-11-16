import json
from pathlib import Path

import redis
from redis_config import create_redis_client

PROJECT_DIR = Path(__file__).resolve().parent
DATA_DIR = PROJECT_DIR / "data"


def load_json(filename):
    return json.loads((DATA_DIR / filename).read_text(encoding="utf-8-sig"))


def main():
    tutorial_sample = load_json("open_food_facts_sample.json")
    sourced_products = load_json("open_food_facts_products.json")["products"]
    simulated_products = load_json("simulated_products.json")["products"]

    # P004 is from the official tutorial sample; its retail fields are mock values.
    sourced_products.insert(
        0,
        {
            "product_id": "P004",
            "name": tutorial_sample["product"]["product_name"],
            "display_name_zh": "能多益榛果可可醬",
            "barcode": tutorial_sample["barcode"],
            "nutrition_grade": tutorial_sample["product"]["nutrition_grades"],
            "data_source": "Open Food Facts",
            "source_url": tutorial_sample["source_url"],
            "source_method": "官方 API 教學頁的靜態範例，非本程式即時查詢",
            "price": "180",
            "stock": "1",
            "limit_per_user": "2",
        },
    )

    db = create_redis_client(socket_connect_timeout=2)
    db.ping()

    imported = []
    for product in sourced_products + simulated_products:
        product_id = product["product_id"]
        fields = {key: value for key, value in product.items() if key != "product_id"}
        key = f"product:{product_id}"

        # HSETNX makes the importer repeatable without resetting an existing
        # product's stock, price, or other values changed during a demo.
        pipe = db.pipeline(transaction=False)
        for field, value in fields.items():
            pipe.hsetnx(key, field, str(value))
        pipe.execute()
        imported.append(product_id)

    sourced_count = len(sourced_products)
    simulated_count = len(simulated_products)
    print(f"商品目錄已補齊：{len(imported)} 項（Open Food Facts {sourced_count} 項、專題模擬 {simulated_count} 項）。")
    print("既有商品欄位（包含庫存）已保留；P003 測試商品與歷史訂單不會刪除。")
    print("正式選單商品：" + ", ".join(imported))
    print("回到 Streamlit 後按「重新整理商品資料」即可載入選單。")


if __name__ == "__main__":
    main()
