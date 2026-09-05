"""Verifikasi manual: _load_transactions() sekarang baca banyak file harian
(glob transaction_data/transaction_day*.csv) dan parse transaction_time jadi
datetime asli, bukan lagi baca satu file transaction_data.csv yang sudah
tidak ada. Tidak butuh Ollama/ChromaDB (cuma manipulasi DataFrame).
Jalankan: ./Scripts/python.exe _verify_transactions_loading.py
"""

import os
import tempfile

import pandas as pd

import query

CSV_HEADER = "product_name,product_category_name_lvl_0,product_price,product_short_desc,transaction_time,item_qty\n"


def main():
    tmpdir = tempfile.mkdtemp()
    day1 = os.path.join(tmpdir, "transaction_day1.csv")
    day2 = os.path.join(tmpdir, "transaction_day2.csv")
    with open(day1, "w", encoding="utf-8") as f:
        f.write(CSV_HEADER)
        f.write("A,Cat1,1000,desc,2026-08-0105:01:54,2\n")
        f.write("B,Cat2,2000,desc,2026-08-0110:00:00,-1\n")
    with open(day2, "w", encoding="utf-8") as f:
        f.write(CSV_HEADER)
        f.write("A,Cat1,1000,desc,2026-08-0209:30:00,3\n")

    query.TRANSACTION_CSV_GLOB = os.path.join(tmpdir, "transaction_day*.csv")
    query._transactions_cache = None
    df = query._load_transactions()

    assert len(df) == 3, f"FAIL: expected 3 baris gabungan, dapat {len(df)}"
    print("PASS: dua file harian tergabung jadi satu DataFrame (3 baris)")

    assert pd.api.types.is_datetime64_any_dtype(df["transaction_time"]), (
        f"FAIL: transaction_time seharusnya datetime, dapat {df['transaction_time'].dtype}"
    )
    print("PASS: transaction_time ter-parse jadi datetime asli")

    parsed = df.loc[df["product_name"] == "A", "transaction_time"].iloc[0]
    assert parsed == pd.Timestamp("2026-08-01 05:01:54"), f"FAIL: parsing salah, dapat {parsed}"
    print("PASS: format tanpa separator (YYYY-MM-DDHH:MM:SS) ter-parse benar")

    assert df.loc[df["product_name"] == "B", "item_qty"].iloc[0] == -1
    print("PASS: item_qty negatif (retur) tetap terbaca apa adanya")

    print("SEMUA CEK LULUS")


if __name__ == "__main__":
    main()
