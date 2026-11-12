"""Clean the raw Olist tables and build a star schema for Power BI.

    python src/build_model.py

Reads  data/raw/*.csv
Writes data/processed/fact_sales.csv     one row per order item (grain: order line)
       data/processed/dim_product.csv
       data/processed/dim_customer.csv
       data/processed/dim_seller.csv
       docs/data_quality_report.md       what was removed or fixed, and why

All CSVs use a dot as the decimal separator and ISO dates (yyyy-mm-dd).
"""
import pathlib

import pandas as pd

ROOT = pathlib.Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"
OUT = ROOT / "data" / "processed"
DOCS = ROOT / "docs"

# Analysis window: the first and last full months of trading.
# Sep-Dec 2016 is a sparse launch period and Sep-Oct 2018 is cut off mid-month.
START, END = "2017-01-01", "2018-08-31"

# Orders that never turned into a sale.
NOT_A_SALE = {"canceled", "unavailable"}

# The two categories missing from Olist's own translation table.
EXTRA_TRANSLATIONS = {
    "pc_gamer": "pc_gamer",
    "portateis_cozinha_e_preparadores_de_alimentos": "portable_kitchen_food_preparers",
}

# Brazilian states and IBGE macro-regions. Full state names help Power BI's map geocode correctly.
STATES = {
    "AC": ("Acre", "North"), "AP": ("Amapá", "North"), "AM": ("Amazonas", "North"),
    "PA": ("Pará", "North"), "RO": ("Rondônia", "North"), "RR": ("Roraima", "North"),
    "TO": ("Tocantins", "North"),
    "AL": ("Alagoas", "Northeast"), "BA": ("Bahia", "Northeast"), "CE": ("Ceará", "Northeast"),
    "MA": ("Maranhão", "Northeast"), "PB": ("Paraíba", "Northeast"), "PE": ("Pernambuco", "Northeast"),
    "PI": ("Piauí", "Northeast"), "RN": ("Rio Grande do Norte", "Northeast"), "SE": ("Sergipe", "Northeast"),
    "DF": ("Distrito Federal", "Central-West"), "GO": ("Goiás", "Central-West"),
    "MT": ("Mato Grosso", "Central-West"), "MS": ("Mato Grosso do Sul", "Central-West"),
    "ES": ("Espírito Santo", "Southeast"), "MG": ("Minas Gerais", "Southeast"),
    "RJ": ("Rio de Janeiro", "Southeast"), "SP": ("São Paulo", "Southeast"),
    "PR": ("Paraná", "South"), "RS": ("Rio Grande do Sul", "South"), "SC": ("Santa Catarina", "South"),
}

log: list[tuple[str, str]] = []


def note(step: str, detail: str) -> None:
    log.append((step, detail))
    print(f"[{step}] {detail}")


def read(name: str, **kw) -> pd.DataFrame:
    return pd.read_csv(RAW / f"{name}.csv", encoding="utf-8-sig", **kw)


def add_geography(df: pd.DataFrame, state_col: str, prefix: str) -> pd.DataFrame:
    df[f"{prefix}_state_name"] = df[state_col].map(lambda s: STATES[s][0])
    df[f"{prefix}_region"] = df[state_col].map(lambda s: STATES[s][1])
    return df


def pretty(category: str) -> str:
    return category.replace("_", " ").strip().capitalize()


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    DOCS.mkdir(parents=True, exist_ok=True)

    date_cols = ["order_purchase_timestamp", "order_delivered_customer_date", "order_estimated_delivery_date"]
    orders = read("olist_orders_dataset", parse_dates=date_cols)
    items = read("olist_order_items_dataset")
    payments = read("olist_order_payments_dataset")
    reviews = read("olist_order_reviews_dataset", parse_dates=["review_answer_timestamp"])
    customers = read("olist_customers_dataset")
    products = read("olist_products_dataset")
    sellers = read("olist_sellers_dataset")
    translation = read("product_category_name_translation")

    raw_orders, raw_items = len(orders), len(items)
    note("Raw input", f"{raw_orders:,} orders and {raw_items:,} order items")

    # 1. Keep complete months only.
    in_window = orders["order_purchase_timestamp"].between(START, f"{END} 23:59:59")
    note("Date window", f"Removed {(~in_window).sum():,} orders purchased outside {START} to {END} (incomplete months)")
    orders = orders[in_window]

    # 2. Keep real sales only.
    is_sale = ~orders["order_status"].isin(NOT_A_SALE)
    note("Order status", f"Removed {(~is_sale).sum():,} canceled or unavailable orders")
    orders = orders[is_sale]

    # 3. Orders need at least one item to carry revenue.
    has_items = orders["order_id"].isin(items["order_id"])
    note("Orders without items", f"Removed {(~has_items).sum():,} orders with no order lines")
    orders = orders[has_items].copy()

    # 4. Delivery metrics, only where the order was actually delivered.
    delivered = (orders["order_status"] == "delivered") & orders["order_delivered_customer_date"].notna()
    missing_date = ((orders["order_status"] == "delivered") & orders["order_delivered_customer_date"].isna()).sum()
    note("Delivery dates", f"{missing_date:,} delivered orders have no delivery date; they count as sales but are excluded from delivery metrics")
    orders["is_delivered"] = delivered.astype(int)
    orders["delivery_days"] = (
        (orders["order_delivered_customer_date"] - orders["order_purchase_timestamp"]).dt.total_seconds() / 86400
    ).round(1).where(delivered)
    late = orders["order_delivered_customer_date"].dt.normalize() > orders["order_estimated_delivery_date"].dt.normalize()
    orders["is_late"] = late.astype("Int64").where(delivered)
    orders["days_late"] = (
        (orders["order_delivered_customer_date"].dt.normalize() - orders["order_estimated_delivery_date"].dt.normalize()).dt.days
    ).where(delivered).astype("Int64")

    # Labels for charts, so the report does not need calculated columns.
    orders["delivery_status"] = "Not delivered yet"
    orders.loc[delivered & (orders["is_late"] == 0), "delivery_status"] = "On time"
    orders.loc[delivered & (orders["is_late"] == 1), "delivery_status"] = "Late"
    buckets = pd.cut(orders["delivery_days"], [-1, 7, 14, 21, 30, 10_000],
                     labels=["0-7 days", "8-14 days", "15-21 days", "22-30 days", "Over 30 days"])
    orders["delivery_time_band"] = buckets.astype(str).where(delivered)
    orders["delivery_band_order"] = buckets.cat.codes.add(1).where(delivered).astype("Int64")

    # 5. Reviews: some orders have several; keep the most recent answer.
    dup = reviews["order_id"].duplicated(keep=False).sum()
    reviews = reviews.sort_values("review_answer_timestamp").drop_duplicates("order_id", keep="last")
    note("Reviews", f"{dup:,} review rows belonged to orders with more than one review; kept the latest per order")
    orders = orders.merge(reviews[["order_id", "review_score"]], on="order_id", how="left")
    note("Reviews", f"{orders['review_score'].isna().sum():,} orders have no review (left blank, not zero)")

    # 6. Payments: main method = the one carrying the largest amount.
    main_pay = (payments.sort_values("payment_value")
                .drop_duplicates("order_id", keep="last")[["order_id", "payment_type"]])
    installments = payments.groupby("order_id", as_index=False)["payment_installments"].max()
    orders = orders.merge(main_pay, on="order_id", how="left").merge(installments, on="order_id", how="left")
    orders["payment_type"] = (orders["payment_type"].fillna("not_defined")
                              .str.replace("_", " ").str.capitalize())

    # 7. Customers: Olist issues a new customer_id per order; customer_unique_id is the person.
    orders = orders.merge(customers, on="customer_id", how="left")

    # 8. Surrogate integer keys keep the model small and the relationships fast.
    keys = {}
    for col, name in [("order_id", "order_key"), ("customer_unique_id", "customer_key")]:
        keys[name] = {v: i + 1 for i, v in enumerate(orders[col].unique())}
        orders[name] = orders[col].map(keys[name])

    # ---------- fact_sales (one row per order line) ----------
    fact = items.merge(orders, on="order_id", how="inner")
    product_keys = {v: i + 1 for i, v in enumerate(sorted(fact["product_id"].unique()))}
    seller_keys = {v: i + 1 for i, v in enumerate(sorted(fact["seller_id"].unique()))}
    fact["product_key"] = fact["product_id"].map(product_keys)
    fact["seller_key"] = fact["seller_id"].map(seller_keys)
    fact["order_date"] = fact["order_purchase_timestamp"].dt.date
    fact["delivered_date"] = fact["order_delivered_customer_date"].dt.date
    fact["estimated_delivery_date"] = fact["order_estimated_delivery_date"].dt.date
    fact = fact.rename(columns={"order_item_id": "line_number", "freight_value": "freight",
                                "payment_installments": "installments"})
    fact_cols = ["order_key", "line_number", "order_date", "customer_key", "product_key", "seller_key",
                 "price", "freight", "order_status", "payment_type", "installments",
                 "is_delivered", "delivered_date", "estimated_delivery_date",
                 "delivery_days", "is_late", "days_late", "delivery_status",
                 "delivery_time_band", "delivery_band_order", "review_score"]
    fact = fact[fact_cols].sort_values(["order_date", "order_key", "line_number"])
    for col in ["installments", "review_score"]:
        fact[col] = fact[col].astype("Int64")
    note("Output", f"fact_sales: {len(fact):,} order lines from {fact['order_key'].nunique():,} orders")

    # ---------- dim_product ----------
    translation = pd.concat([translation, pd.DataFrame(
        {"product_category_name": list(EXTRA_TRANSLATIONS), "product_category_name_english": list(EXTRA_TRANSLATIONS.values())})])
    prod = products[products["product_id"].isin(product_keys)].merge(translation, on="product_category_name", how="left")
    note("Products", f"{prod['product_category_name'].isna().sum():,} products have no category; labelled 'Unknown'")
    note("Products", f"Added English names for {len(EXTRA_TRANSLATIONS)} categories missing from the translation table")
    prod["category"] = prod["product_category_name_english"].fillna("unknown").map(pretty)
    prod["weight_kg"] = (prod["product_weight_g"] / 1000).round(3)
    prod["product_key"] = prod["product_id"].map(product_keys)
    dim_product = prod[["product_key", "category", "weight_kg", "product_photos_qty"]].rename(
        columns={"product_photos_qty": "photos"}).sort_values("product_key")
    dim_product["photos"] = dim_product["photos"].astype("Int64")

    # ---------- dim_customer ----------
    # Location from the customer's most recent order; repeat flag from order count in the window.
    latest = orders.sort_values("order_purchase_timestamp").drop_duplicates("customer_key", keep="last")
    stats = orders.groupby("customer_key").agg(first_order_date=("order_purchase_timestamp", "min"),
                                               order_count=("order_key", "nunique")).reset_index()
    cust = latest[["customer_key", "customer_city", "customer_state"]].merge(stats, on="customer_key")
    cust["customer_city"] = cust["customer_city"].str.title()
    cust["first_order_date"] = cust["first_order_date"].dt.date
    cust["customer_type"] = cust["order_count"].map(lambda n: "Repeat" if n > 1 else "One-time")
    dim_customer = add_geography(cust, "customer_state", "customer").sort_values("customer_key")
    dim_customer = dim_customer[["customer_key", "customer_city", "customer_state", "customer_state_name",
                                 "customer_region", "first_order_date", "order_count", "customer_type"]]

    # ---------- dim_seller ----------
    sel = sellers[sellers["seller_id"].isin(seller_keys)].copy()
    sel["seller_key"] = sel["seller_id"].map(seller_keys)
    sel["seller_city"] = sel["seller_city"].str.title()
    dim_seller = add_geography(sel, "seller_state", "seller").sort_values("seller_key")
    dim_seller = dim_seller[["seller_key", "seller_city", "seller_state", "seller_state_name", "seller_region"]]

    # ---------- checks ----------
    assert fact[["order_key", "line_number"]].duplicated().sum() == 0, "duplicate order lines"
    assert fact["product_key"].isin(dim_product["product_key"]).all(), "orphan product"
    assert fact["customer_key"].isin(dim_customer["customer_key"]).all(), "orphan customer"
    assert fact["seller_key"].isin(dim_seller["seller_key"]).all(), "orphan seller"
    assert dim_product["product_key"].is_unique and dim_customer["customer_key"].is_unique and dim_seller["seller_key"].is_unique
    assert (fact["price"] > 0).all() and (fact["freight"] >= 0).all()
    assert fact["review_score"].dropna().between(1, 5).all()
    assert (fact["delivery_days"].dropna() >= 0).all()
    note("Checks", "Passed: unique keys, no orphan rows, prices positive, review scores 1-5, no negative delivery times")

    for name, df in [("fact_sales", fact), ("dim_product", dim_product),
                     ("dim_customer", dim_customer), ("dim_seller", dim_seller)]:
        df.to_csv(OUT / f"{name}.csv", index=False, encoding="utf-8")
        note("Output", f"{name}.csv: {len(df):,} rows, {df.shape[1]} columns")

    # ---------- data quality report ----------
    lines = ["# Data quality report", "",
             "Generated by `src/build_model.py`. Every row removed or changed is listed here.", "",
             "| Step | What happened |", "|---|---|"]
    lines += [f"| {s} | {d} |" for s, d in log]
    (DOCS / "data_quality_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
