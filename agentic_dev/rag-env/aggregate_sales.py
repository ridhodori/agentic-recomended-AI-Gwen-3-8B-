"""
Agregasi data penjualan mentah (transaction_data/transaction_day*.csv) jadi
net qty "terjual" per produk, lalu digabung ke katalog_produk.csv sebagai
sinyal popularitas.

Data transaksi terdiri dari beberapa file harian (satu file per hari,
transaction_dayN.csv), masing-masing berisi: product_name,
product_category_name_lvl_0, product_price, product_short_desc,
transaction_time, item_qty. TANPA user_id -- jadi tidak bisa dipakai untuk
riwayat per-user, hanya untuk menghitung net unit yang benar-benar terjual
(item_qty bisa negatif untuk retur/pembatalan -- retur MENGURANGI total,
bukan diabaikan, lihat 2026-09-06-transaction-data-v2-design.md bagian 2).

Karena tidak ada product_id yang sama antara data transaksi dan
katalog_produk.csv, penggabungan dilakukan berdasarkan nama produk yang
dinormalisasi (lower + whitespace dirapikan). Produk di katalog yang tidak
ditemukan di data penjualan diberi nilai terjual=0.
"""

import csv
import glob
import re
from collections import Counter

import pandas as pd

TRANSACTION_CSV_GLOB = "D:/agentic/transaction_data/transaction_day*.csv"
KATALOG_CSV = "D:/agentic/chroma_db/katalog_produk.csv"


def normalize_name(name):
    return re.sub(r"\s+", " ", (name or "").strip().lower())


def sum_net_qty_per_product(paths):
    """Net qty (item_qty negatif/retur mengurangi) per nama produk
    ternormalisasi, digabung dari SEMUA file yang diberikan."""
    totals = Counter()
    for path in paths:
        with open(path, encoding="utf-8", errors="replace") as f:
            reader = csv.DictReader(f)
            for row in reader:
                key = normalize_name(row.get("product_name"))
                if not key:
                    continue
                try:
                    qty = float(row.get("item_qty", 0) or 0)
                except ValueError:
                    qty = 0
                totals[key] += qty
    return totals


def main():
    paths = sorted(glob.glob(TRANSACTION_CSV_GLOB))
    print(f"Membaca dan menghitung penjualan (net qty) dari {len(paths)} file: {paths} ...")
    sales_totals = sum_net_qty_per_product(paths)
    print(f"Total unit net terjual (semua produk): {sum(sales_totals.values()):.0f}")
    print(f"Total nama produk unik di data penjualan: {len(sales_totals)}")

    df = pd.read_csv(KATALOG_CSV)
    df["terjual"] = df["nama"].apply(lambda n: sales_totals.get(normalize_name(n), 0))

    matched = (df["terjual"] > 0).sum()
    print(f"\nKatalog: {len(df)} produk, {matched} di antaranya cocok dengan data penjualan.")

    df.to_csv(KATALOG_CSV, index=False)
    print(f"Selesai: kolom 'terjual' (net qty) ditambahkan ke {KATALOG_CSV}")


if __name__ == "__main__":
    main()
