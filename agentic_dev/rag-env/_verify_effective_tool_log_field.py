"""Verifikasi manual: field `effective_tool=` baru di tool_calls.log
(ditulis _write_tool_log_line saat _maybe_correct_per_day_miss mengoreksi
giliran) benar-benar membuat test_agent_cases.py::_new_tool_calls melaporkan
tools_called yang cocok dengan expected_tools kasus seperti PD04
(["get_top_sellers_by_day"]) -- SEBELUM perbaikan ini, actual_tools SELALU
melaporkan nama tool asli (get_top_sellers) walau kontennya sudah dikoreksi,
lihat rag-setup-windows.md Known Issues. Juga mengecek baris lama (tanpa
effective_tool) dan baris normal (tanpa koreksi) tetap terparse identik
seperti sebelumnya -- perubahan format harus 100% backward-compatible.
Tidak butuh Ollama. Jalankan: ./Scripts/python.exe _verify_effective_tool_log_field.py
"""

import os
import sys

import pandas as pd

import query
import test_agent_cases as tac


class _FakeTool:
    def __init__(self, name):
        self.name = name


class _FakeToolContext:
    pass


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
    log_path = "D:/agentic/agentic_dev/rag-env/_tmp_verify_effective_tool.log"
    query.TOOL_LOG_PATH = log_path
    tac.TOOL_LOG_PATH = log_path
    if os.path.exists(log_path):
        os.remove(log_path)
    open(log_path, "w", encoding="utf-8").close()

    # Case 1: a per-hari-miss turn (get_top_sellers wrongly called, gets
    # corrected) -- the log line must carry effective_tool=get_top_sellers_by_day,
    # and _new_tool_calls must report THAT as tools_called (matching what a
    # case like PD04 expects), while still exposing the raw call separately.
    tool = _FakeTool("get_top_sellers")
    args = {"segment": "", "top_n": 5, "start_date": "2026-08-01", "end_date": "2026-08-03"}
    query._current_turn_question = "top 2 produk terlaris per hari dari tanggal 2026-08-01 sampai 2026-08-03"
    before = tac._count_lines(log_path)
    query._log_after_tool(tool, args, _FakeToolContext(), "wrong combined ranking")
    data_tools, specialists, raw_tools = tac._new_tool_calls(log_path, before)
    assert data_tools == ["get_top_sellers_by_day"], f"FAIL: tools_called harus melaporkan tool efektif: {data_tools!r}"
    assert raw_tools == ["get_top_sellers"], f"FAIL: raw_tools_called harus tetap melaporkan tool asli: {raw_tools!r}"
    print("PASS: giliran per-hari-miss (dikoreksi) -> tools_called=get_top_sellers_by_day, raw_tools_called=get_top_sellers")

    # Case 2: a normal, non-corrected get_top_sellers call -- log line has no
    # effective_tool suffix at all (format unchanged from before this fix),
    # and both tools_called and raw_tools_called report the same real name.
    before = tac._count_lines(log_path)
    query._current_turn_question = "produk terlaris kategori Minuman"
    query._log_after_tool(tool, {"segment": "Minuman", "top_n": 5}, _FakeToolContext(), "- ProdukA | terjual: 5x")
    with open(log_path, encoding="utf-8") as f:
        last_line = f.readlines()[-1]
    assert "effective_tool=" not in last_line, f"FAIL: giliran normal tidak boleh punya effective_tool: {last_line!r}"
    data_tools, specialists, raw_tools = tac._new_tool_calls(log_path, before)
    assert data_tools == ["get_top_sellers"] == raw_tools, f"FAIL: giliran normal harus sama di keduanya: {data_tools!r} {raw_tools!r}"
    print("PASS: giliran normal -> baris log TIDAK berubah format, tools_called == raw_tools_called == get_top_sellers")

    # Case 3: backward compatibility -- a hand-written OLD-format log line
    # (no effective_tool field at all, exactly the format written before this
    # session's fix) must still parse exactly as before.
    before = tac._count_lines(log_path)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write("2026-09-06T10:00:00 | get_top_categories | args={'top_n': 5} | 0.50s | OK\n")
    data_tools, specialists, raw_tools = tac._new_tool_calls(log_path, before)
    assert data_tools == ["get_top_categories"] == raw_tools, f"FAIL: baris format lama harus tetap terparse: {data_tools!r}"
    print("PASS: baris tool_calls.log format LAMA (pra-fix) tetap terparse identik (backward-compatible)")

    # Case 4: specialist-level lines (root delegation) must still be routed
    # to `specialists`, not `data_tools`/`raw_data_tools`, unaffected by the
    # new field.
    before = tac._count_lines(log_path)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write("2026-09-06T10:00:01 | produk_specialist | args={} | 1.20s | OK\n")
    data_tools, specialists, raw_tools = tac._new_tool_calls(log_path, before)
    assert specialists == ["produk_specialist"] and data_tools == [] and raw_tools == [], (
        f"FAIL: baris specialist tidak boleh masuk data_tools/raw_data_tools: {data_tools!r} {raw_tools!r} {specialists!r}"
    )
    print("PASS: baris level-specialist (root delegation) tetap terklasifikasi benar, tidak terpengaruh field baru")

    os.remove(log_path)
    print("SEMUA CEK LULUS")


if __name__ == "__main__":
    main()
