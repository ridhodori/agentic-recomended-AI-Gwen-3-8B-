"""Verifikasi manual: aggregate_sales.py sekarang baca banyak file harian
(glob) dan hitung net qty (sum item_qty, retur mengurangi) bukan hitung
baris. Tidak butuh Ollama/ChromaDB.
Jalankan: ./Scripts/python.exe _verify_aggregate_sales_net_qty.py
"""

import csv
import os
import tempfile

import aggregate_sales

CSV_HEADER = ["product_name", "product_category_name_lvl_0", "product_price", "product_short_desc", "transaction_time", "item_qty"]


def _write_csv(path, rows):
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(CSV_HEADER)
        w.writerows(rows)


def main():
    tmpdir = tempfile.mkdtemp()
    day1 = os.path.join(tmpdir, "transaction_day1.csv")
    day2 = os.path.join(tmpdir, "transaction_day2.csv")
    _write_csv(day1, [
        ["Produk A", "Cat1", 1000, "", "2026-08-0105:00:00", 5],
        ["Produk A", "Cat1", 1000, "", "2026-08-0106:00:00", -2],
    ])
    _write_csv(day2, [
        ["Produk A", "Cat1", 1000, "", "2026-08-0205:00:00", 10],
        ["Produk B", "Cat1", 500, "", "2026-08-0205:00:00", 1],
    ])

    totals = aggregate_sales.sum_net_qty_per_product([day1, day2])
    key_a = aggregate_sales.normalize_name("Produk A")
    key_b = aggregate_sales.normalize_name("Produk B")

    assert totals[key_a] == 13, f"FAIL: Produk A harusnya net 5-2+10=13, dapat {totals[key_a]}"
    print("PASS: net qty tergabung dari 2 file, retur mengurangi total")

    assert totals[key_b] == 1, f"FAIL: Produk B harusnya 1, dapat {totals[key_b]}"
    print("PASS: produk yang cuma ada di satu file tetap terhitung benar")

    print("SEMUA CEK LULUS")


if __name__ == "__main__":
    main()
