"""Verifikasi manual: _maybe_correct_per_day_miss() + _log_after_tool()
sekarang MENGOREKSI (bukan cuma mencatat) kasus get_top_sellers salah
terpanggil untuk permintaan breakdown per hari eksplisit -- ditambahkan
setelah eksperimen temperature (2026-09-06, rag-setup-windows.md Known
Issues) membuktikan sisa miss-rate itu bukan noise acak yang bisa
dihilangkan lewat sampling-parameter tuning. Tidak butuh Ollama (cuma
manipulasi DataFrame + panggilan langsung ke callback).
Jalankan: ./Scripts/python.exe _verify_per_day_auto_correct.py
"""

import os

import pandas as pd

import query


class _FakeTool:
    def __init__(self, name):
        self.name = name


def _fake_df():
    rows = [
        ("ProdukX", "Cat1", 1000, "", "2026-08-01 05:00:00", 10),
        ("ProdukY", "Cat1", 2000, "", "2026-08-01 05:00:00", 4),
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
    query.TOOL_LOG_PATH = "D:/agentic/agentic_dev/rag-env/_tmp_verify_per_day_autocorrect.log"
    if os.path.exists(query.TOOL_LOG_PATH):
        os.remove(query.TOOL_LOG_PATH)

    # Case 1: THE bug -- get_top_sellers called with a genuine multi-day
    # range, question has an explicit per-day signal -> MUST be corrected to
    # the get_top_sellers_by_day result (same args), not the wrong original.
    tool = _FakeTool("get_top_sellers")
    args = {"segment": "", "top_n": 5, "start_date": "2026-08-01", "end_date": "2026-08-03"}
    wrong_response = query._get_top_sellers_impl(**args)
    corrected = query._maybe_correct_per_day_miss(
        tool, args, "top 2 produk terlaris per hari dari tanggal 2026-08-01 sampai 2026-08-03"
    )
    assert corrected is not None, "FAIL: harusnya dikoreksi (sinyal per-hari + rentang multi-hari + tool salah)"
    assert corrected != wrong_response, "FAIL: hasil koreksi seharusnya beda dari get_top_sellers asli"
    expected = query._get_top_sellers_by_day_impl(**args)
    assert corrected == expected, f"FAIL: hasil koreksi harus PERSIS sama dgn get_top_sellers_by_day langsung.\ngot: {corrected}\nexpected: {expected}"
    assert "2026-08-01:" in corrected and "2026-08-03:" in corrected, f"FAIL: koreksi harus berformat per-hari: {corrected}"
    print("PASS: get_top_sellers + sinyal per-hari + rentang multi-hari -> dikoreksi jadi hasil get_top_sellers_by_day")

    with open(query.TOOL_LOG_PATH, encoding="utf-8") as f:
        log = f.read()
    assert "PER-HARI AUTO-DIKOREKSI" in log, f"FAIL: harus tercatat di tool_calls.log: {log!r}"
    print("PASS: koreksi tercatat di tool_calls.log (audit trail)")

    # Case 2: get_top_sellers_by_day itself is never "corrected" (already the right tool).
    tool_by_day = _FakeTool("get_top_sellers_by_day")
    corrected = query._maybe_correct_per_day_miss(tool_by_day, args, "top 2 produk terlaris per hari dari tanggal 2026-08-01 sampai 2026-08-03")
    assert corrected is None, f"FAIL: get_top_sellers_by_day tidak pernah perlu dikoreksi: {corrected!r}"
    print("PASS: get_top_sellers_by_day tidak pernah dikoreksi (sudah tool yang benar)")

    # Case 3: get_top_sellers called for a PLAIN range question (no per-day signal) -> untouched.
    corrected = query._maybe_correct_per_day_miss(
        tool, args, "produk terlaris dari tanggal 2026-08-01 sampai 2026-08-03"
    )
    assert corrected is None, f"FAIL: tanpa sinyal per-hari seharusnya TIDAK dikoreksi: {corrected!r}"
    print("PASS: rentang biasa tanpa sinyal per-hari -> get_top_sellers TIDAK diutak-atik")

    # Case 4: single-day "range" (start == end) + per-day signal -> untouched
    # (get_top_sellers and by_day give an identical answer for one day, no
    # correction needed, matches the PD13 edge case already documented).
    single_day_args = {"segment": "", "top_n": 5, "start_date": "2026-08-01", "end_date": "2026-08-01"}
    corrected = query._maybe_correct_per_day_miss(
        tool, single_day_args, "produk terlaris per hari tanggal 2026-08-01"
    )
    assert corrected is None, f"FAIL: rentang satu-hari tidak perlu dikoreksi: {corrected!r}"
    print("PASS: rentang satu-hari (start==end) + sinyal per-hari -> tidak dikoreksi (sudah ekuivalen)")

    # Case 5: end-to-end through _log_after_tool -- confirms the ADK
    # after_tool_callback override contract (non-None return replaces the
    # tool_response the specialist LLM actually sees).
    class _FakeToolContext:
        pass

    query._current_turn_tool_outputs.clear()
    query._current_turn_question = "5 produk terlaris per hari dari tanggal 2026-08-01 sampai 2026-08-03"
    result = query._log_after_tool(tool, args, _FakeToolContext(), wrong_response)
    assert result == expected, f"FAIL: _log_after_tool harus mengembalikan hasil koreksi (bukan None): {result!r}"
    assert query._current_turn_tool_outputs == [expected], (
        f"FAIL: _current_turn_tool_outputs (dipakai verify_and_revise) harus berisi hasil TERKOREKSI, "
        f"bukan yang asli: {query._current_turn_tool_outputs!r}"
    )
    print("PASS: _log_after_tool mengembalikan hasil koreksi (dipakai ADK sbg tool_response baru) DAN "
          "mengisi _current_turn_tool_outputs dengan data yang sudah benar")

    # Case 6: normal (non-buggy) tool call through _log_after_tool -> unaffected.
    query._current_turn_tool_outputs.clear()
    query._current_turn_question = "produk terlaris kategori Minuman"
    normal_response = "- ProdukA | kategori: Cat1 | terjual: 5x | harga: 1000"
    result = query._log_after_tool(tool, {"segment": "Minuman", "top_n": 5}, _FakeToolContext(), normal_response)
    assert result is None, f"FAIL: giliran normal seharusnya tidak dikoreksi: {result!r}"
    assert query._current_turn_tool_outputs == [normal_response], "FAIL: giliran normal harus tetap pakai tool_response asli"
    print("PASS: giliran normal (bukan per-hari miss) tidak terpengaruh sama sekali")

    os.remove(query.TOOL_LOG_PATH)
    print("SEMUA CEK LULUS")


if __name__ == "__main__":
    main()
