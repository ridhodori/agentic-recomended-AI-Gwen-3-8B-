"""Verifikasi manual: get_top_sellers/get_top_categories sekarang pakai net
qty (sum item_qty, retur mengurangi) bukan hitung baris, dan mendukung
filter start_date/end_date opsional. Tidak butuh Ollama (cuma DataFrame).
Jalankan: ./Scripts/python.exe _verify_net_qty_ranking.py
"""

import pandas as pd

import query


def _fake_df():
    rows = [
        # product_name, category, price, desc, transaction_time, item_qty
        ("A", "Cat1", 1000, "", "2026-08-01 05:00:00", 5),
        ("A", "Cat1", 1000, "", "2026-08-01 06:00:00", -2),  # retur
        ("A", "Cat1", 1000, "", "2026-08-02 05:00:00", 10),
        ("B", "Cat1", 2000, "", "2026-08-01 05:00:00", 1),
    ]
    df = pd.DataFrame(rows, columns=[
        "product_name", "product_category_name_lvl_0", "product_price",
        "product_short_desc", "transaction_time", "item_qty",
    ])
    df["transaction_time"] = pd.to_datetime(df["transaction_time"])
    return df


def main():
    query._transactions_cache = _fake_df()

    # Net qty: A = 5 - 2 + 10 = 13, no date filter
    result = query._get_top_sellers_impl("A", 5, "", "")
    assert "terjual: 13x" in result, f"FAIL: net qty salah, dapat: {result}"
    print("PASS: get_top_sellers net qty (retur mengurangi) benar tanpa filter tanggal")

    # Date filter to just 2026-08-01: A = 5 - 2 = 3
    result = query._get_top_sellers_impl("A", 5, "2026-08-01", "2026-08-01")
    assert "terjual: 3x" in result, f"FAIL: filter tanggal salah, dapat: {result}"
    print("PASS: get_top_sellers filter tanggal (satu hari) benar")

    # Out-of-range date -> honest refusal string, not empty/crash
    result = query._get_top_sellers_impl("A", 5, "2099-01-01", "2099-01-02")
    assert "2099" in result and "tersedia" in result, f"FAIL: harusnya pesan rentang tidak tersedia, dapat: {result}"
    print("PASS: get_top_sellers tanggal di luar rentang -> pesan jujur, bukan mengarang")

    # get_top_categories: Cat1 net = 5-2+10+1 = 14
    result = query._get_top_categories_impl(5, False, "", "")
    assert "terjual: 14x" in result, f"FAIL: get_top_categories net qty salah, dapat: {result}"
    print("PASS: get_top_categories net qty benar tanpa filter tanggal")

    print("SEMUA CEK LULUS")


if __name__ == "__main__":
    main()
