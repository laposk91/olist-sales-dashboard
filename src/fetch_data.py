"""Download the raw Olist tables used in this project into data/raw/.

    python src/fetch_data.py

Source: Olist, "work-at-olist-data" (MIT License)
https://github.com/olist/work-at-olist-data
"""
import pathlib
import urllib.request

BASE = "https://raw.githubusercontent.com/olist/work-at-olist-data/master/datasets/"
FILES = [
    "olist_customers_dataset.csv",
    "olist_order_items_dataset.csv",
    "olist_order_payments_dataset.csv",
    "olist_order_reviews_dataset.csv",
    "olist_orders_dataset.csv",
    "olist_products_dataset.csv",
    "olist_sellers_dataset.csv",
    "product_category_name_translation.csv",
]
RAW = pathlib.Path(__file__).resolve().parents[1] / "data" / "raw"


def main() -> None:
    RAW.mkdir(parents=True, exist_ok=True)
    for name in FILES:
        target = RAW / name
        if target.exists():
            print(f"skip  {name} (already downloaded)")
            continue
        print(f"get   {name}")
        urllib.request.urlretrieve(BASE + name, target)
    print(f"Raw data is in {RAW}")


if __name__ == "__main__":
    main()
