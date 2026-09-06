"""Verifikasi manual: get_top_sellers sekarang menerima segment="" (tanpa
filter, bukan lagi error "Segmen kosong") supaya pertanyaan umum "top N
produk terlaris" (tanpa nama produk/kategori) bisa dijawab dari data nyata,
dan tool baru get_top_sellers_by_day mengembalikan ranking TERPISAH per
tanggal (bukan satu ranking gabungan) untuk pertanyaan "terlaris per hari"
lintas beberapa tanggal. Tidak butuh Ollama/ChromaDB (cuma manipulasi
DataFrame).
Jalankan: ./Scripts/python.exe _verify_top_sellers_general_and_by_day.py
"""

import pandas as pd

import query

# ProdukX leads day 1, ProdukY leads days 2 and 3 -- overall (13 vs 15) AND
# per-day rankings both matter: overall ProdukY leads throughout the range,
# but day 1's actual winner (ProdukX) is invisible in a range that collapses
# all 3 days into one number. Names are deliberately distinct from the
# category string ("Cat1") so a segment filter can't accidentally match both
# via the category side of the OR mask.
def _fake_df():
    rows = [
        ("ProdukX", "Cat1", 1000, "", "2026-08-01 05:00:00", 10),
        ("ProdukY", "Cat1", 2000, "", "2026-08-01 05:00:00", 4),
        ("ProdukX", "Cat1", 1000, "", "2026-08-02 05:00:00", 1),
        ("ProdukY", "Cat1", 2000, "", "2026-08-02 05:00:00", 2),
        ("ProdukX", "Cat1", 1000, "", "2026-08-03 05:00:00", 2),
        ("ProdukY", "Cat1", 2000, "", "2026-08-03 05:00:00", 9),
    ]
    df = pd.DataFrame(rows, columns=[
        "product_name", "product_category_name_lvl_0", "product_price",
        "product_short_desc", "transaction_time", "item_qty",
    ])
    df["transaction_time"] = pd.to_datetime(df["transaction_time"])
    return df


def main():
    query._transactions_cache = _fake_df()

    # Bug #1: plain "top N terlaris" with no segment named must return real
    # data (ProdukY=15 overall, ProdukX=13), not the old "Segmen kosong" refusal.
    result = query._get_top_sellers_impl("", 5, "", "")
    assert "Segmen kosong" not in result, f"FAIL: empty segment still refuses: {result}"
    assert "ProdukY" in result.splitlines()[0] and "terjual: 15x" in result.splitlines()[0], (
        f"FAIL: ProdukY (15) should lead overall: {result}"
    )
    print("PASS: get_top_sellers with segment='' answers from real data instead of refusing")

    # Existing named-segment behavior must be unaffected (no regression).
    result = query._get_top_sellers_impl("ProdukX", 5, "", "")
    assert "terjual: 13x" in result and "ProdukY" not in result, f"FAIL: segment filter regressed: {result}"
    print("PASS: get_top_sellers with a real segment still filters correctly")

    # A combined range collapses all days into ONE ranking -- ProdukY leads
    # overall (15 vs 13), which HIDES that ProdukX actually won day 1.
    combined = query._get_top_sellers_impl("", 5, "2026-08-01", "2026-08-03")
    assert "ProdukY" in combined.splitlines()[0], f"FAIL(sanity): expected ProdukY to lead the collapsed range: {combined}"

    # Fix: get_top_sellers_by_day returns ONE ranking PER calendar day, so
    # day 1's real winner (ProdukX) is visible instead of hidden by the range.
    by_day = query._get_top_sellers_by_day_impl("", 5, "2026-08-01", "2026-08-03")
    lines = by_day.splitlines()
    assert "2026-08-01:" in lines and "2026-08-02:" in lines and "2026-08-03:" in lines, (
        f"FAIL: should list all 3 days present in the range: {by_day}"
    )
    day1_line = lines[lines.index("2026-08-01:") + 1]
    day3_line = lines[lines.index("2026-08-03:") + 1]
    assert day1_line.strip().startswith("- ProdukX") and "terjual: 10x" in day1_line, (
        f"FAIL: day 1 winner should be ProdukX (net 10): {day1_line}"
    )
    assert day3_line.strip().startswith("- ProdukY") and "terjual: 9x" in day3_line, (
        f"FAIL: day 3 winner should be ProdukY (net 9): {day3_line}"
    )
    print("PASS: get_top_sellers_by_day gives a separate, correct ranking per day (catches the day-1 winner hidden by the collapsed range)")

    # top_n is honored per day (not just overall) -- top_n=1 across 3 days
    # must give exactly 3 bullet lines (one winner per day), not 1.
    by_day_top1 = query._get_top_sellers_by_day_impl("", 1, "2026-08-01", "2026-08-03")
    bullet_lines = [l for l in by_day_top1.splitlines() if l.strip().startswith("-")]
    assert len(bullet_lines) == 3, f"FAIL: top_n=1 across 3 days should give 3 bullet lines total, got {len(bullet_lines)}: {by_day_top1}"
    print("PASS: get_top_sellers_by_day honors top_n independently for each day")

    # Tool registration + verify_and_revise ground-truth tracking.
    # google-adk stores tools as raw functions in Agent.tools (wrapping into
    # FunctionTool with .name happens later via canonical_tools()), so fall
    # back to __name__ -- same convention as _verify_bilingual_instructions.py.
    def _tool_name(t):
        return getattr(t, "name", None) or getattr(t, "__name__", None)

    produk_tools = {_tool_name(t) for t in query.produk_specialist.tools}
    assert "get_top_sellers_by_day" in produk_tools, f"FAIL: not registered on produk_specialist: {produk_tools}"
    assert "get_top_sellers_by_day" in query._DATA_TOOL_NAMES, "FAIL: missing from _DATA_TOOL_NAMES"
    print("PASS: get_top_sellers_by_day registered on produk_specialist and tracked as a data tool")

    print("SEMUA CEK LULUS")


if __name__ == "__main__":
    main()
