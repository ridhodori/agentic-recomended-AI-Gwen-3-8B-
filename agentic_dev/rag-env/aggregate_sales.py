"""
Agregasi data penjualan mentah (transaction_data.csv) jadi jumlah "terjual" per
produk, lalu digabung ke katalog_produk.csv sebagai sinyal popularitas.

transaction_data.csv berisi satu baris per transaksi penjualan (product_name,
product_category_name_lvl_0, product_price, product_short_desc) TANPA user_id
atau timestamp -- jadi tidak bisa dipakai untuk riwayat per-user, hanya untuk
menghitung seberapa sering sebuah produk terjual.

Karena tidak ada product_id yang sama antara transaction_data.csv dan
katalog_produk.csv, penggabungan dilakukan berdasarkan nama produk yang
dinormalisasi (lower + whitespace dirapikan). Produk di katalog yang tidak
ditemukan di data penjualan diberi nilai terjual=0.
"""

import csv
import re
from collections import Counter

import pandas as pd

TRANSACTION_CSV = "D:/agentic/transaction_data/transaction_data.csv"
KATALOG_CSV = "D:/agentic/chroma_db/katalog_produk.csv"


def normalize_name(name):
    return re.sub(r"\s+", " ", (name or "").strip().lower())


def count_sales_per_product(path):
    counts = Counter()
    with open(path, encoding="utf-8", errors="replace") as f:
        reader = csv.DictReader(f)
        for row in reader:
            key = normalize_name(row.get("product_name"))
            if key:
                counts[key] += 1
    return counts


def main():
    print(f"Membaca dan menghitung penjualan dari {TRANSACTION_CSV} ...")
    sales_counts = count_sales_per_product(TRANSACTION_CSV)
    print(f"Total baris transaksi: {sum(sales_counts.values())}")
    print(f"Total nama produk unik di data penjualan: {len(sales_counts)}")

    df = pd.read_csv(KATALOG_CSV)
    df["terjual"] = df["nama"].apply(lambda n: sales_counts.get(normalize_name(n), 0))

    matched = (df["terjual"] > 0).sum()
    print(f"\nKatalog: {len(df)} produk, {matched} di antaranya cocok dengan data penjualan.")

    df.to_csv(KATALOG_CSV, index=False)
    print(f"Selesai: kolom 'terjual' ditambahkan ke {KATALOG_CSV}")


if __name__ == "__main__":
    main()
