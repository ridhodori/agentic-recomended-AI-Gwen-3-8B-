"""Verifikasi manual: get_peak_hours (jam ramai) dan get_trending_products/
get_trending_categories (pertumbuhan net qty antar dua tanggal terbaru di
data). Tidak butuh Ollama (cuma DataFrame).
Jalankan: ./Scripts/python.exe _verify_trending_and_peak_hours.py
"""

import pandas as pd

import query


def _fake_df_hours():
    rows = [
        ("A", "Cat1", 1000, "", "2026-08-01 05:00:00", 5),
        ("A", "Cat1", 1000, "", "2026-08-01 05:30:00", 3),
        ("A", "Cat1", 1000, "", "2026-08-01 14:00:00", 1),
        ("B", "Cat2", 2000, "", "2026-08-01 05:15:00", 2),
    ]
    df = pd.DataFrame(rows, columns=[
        "product_name", "product_category_name_lvl_0", "product_price",
        "product_short_desc", "transaction_time", "item_qty",
    ])
    df["transaction_time"] = pd.to_datetime(df["transaction_time"])
    return df


def _fake_df_trending():
    rows = [
        # tanggal sebelumnya (2026-08-01): A=10, B=5
        ("A", "Cat1", 1000, "", "2026-08-01 05:00:00", 10),
        ("B", "Cat2", 2000, "", "2026-08-01 05:00:00", 5),
        # tanggal terbaru (2026-08-02): A=2 (turun), B=5 (tetap), C baru muncul=20
        ("A", "Cat1", 1000, "", "2026-08-02 05:00:00", 2),
        ("B", "Cat2", 2000, "", "2026-08-02 05:00:00", 5),
        ("C", "Cat1", 3000, "", "2026-08-02 05:00:00", 20),
    ]
    df = pd.DataFrame(rows, columns=[
        "product_name", "product_category_name_lvl_0", "product_price",
        "product_short_desc", "transaction_time", "item_qty",
    ])
    df["transaction_time"] = pd.to_datetime(df["transaction_time"])
    return df


def main():
    query._transactions_cache = _fake_df_hours()
    result = query._get_peak_hours_impl("", 5)
    assert "jam 05:00-05:59 | terjual: 10x" in result, f"FAIL: jam 05 harusnya 5+3+2=10, dapat: {result}"
    assert "jam 14:00-14:59 | terjual: 1x" in result, f"FAIL: jam 14 harusnya 1, dapat: {result}"
    lines = [l for l in result.splitlines() if l.startswith("-")]
    assert lines[0].startswith("- jam 05"), f"FAIL: harusnya jam 05 di urutan pertama (terbanyak), dapat: {result}"
    print("PASS: get_peak_hours group-by-jam + urutan descending benar")

    query._transactions_cache = _fake_df_trending()
    result = query._get_trending_products_impl("", 5)
    assert "2026-08-01" in result and "2026-08-02" in result, f"FAIL: harusnya sebut kedua tanggal, dapat: {result}"
    lines = [l for l in result.splitlines() if l.startswith("-")]
    assert lines[0].startswith("- C"), f"FAIL: C (produk baru, 0->20) harusnya pertumbuhan terbesar, dapat: {result}"
    assert "- A" in "\n".join(lines) and "perubahan: -8" in result, f"FAIL: A harusnya turun 8 (10->2), dapat: {result}"
    print("PASS: get_trending_products bandingkan 2 tanggal terbaru, produk baru dianggap 0 sebelumnya, urutan descending")

    result = query._get_trending_categories_impl(5)
    assert "Cat1" in result, f"FAIL: Cat1 harusnya muncul (A+C), dapat: {result}"
    print("PASS: get_trending_categories jalan di atas data yang sama")

    print("SEMUA CEK LULUS")


if __name__ == "__main__":
    main()
