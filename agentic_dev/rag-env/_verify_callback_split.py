"""Verifikasi manual: root-level callback TIDAK mengisi
_current_turn_tool_outputs, spesialis-level callback TETAP mengisi --
membuktikan koreksi di 2026-09-05-graph-migration-design.md bagian 9.
Tidak butuh Ollama (cuma memanggil fungsi callback langsung, tidak
menjalankan model). Jalankan: ./Scripts/python.exe _verify_callback_split.py
"""

from types import SimpleNamespace

import query


def main():
    query._current_turn_tool_outputs.clear()

    fake_ctx = object()
    query._log_before_tool_root(SimpleNamespace(name="produk_specialist"), {"request": "test"}, fake_ctx)
    query._log_after_tool_root(
        SimpleNamespace(name="produk_specialist"),
        {"request": "test"},
        fake_ctx,
        "jawaban spesialis (sintesis LLM) dengan angka palsu 9999",
    )
    assert query._current_turn_tool_outputs == [], (
        f"FAIL: root callback seharusnya TIDAK mengisi _current_turn_tool_outputs, "
        f"tapi isinya: {query._current_turn_tool_outputs}"
    )
    print("PASS: root callback tidak mencemari _current_turn_tool_outputs")

    fake_ctx2 = object()
    query._log_before_tool(SimpleNamespace(name="get_top_sellers"), {"segment": "sabun"}, fake_ctx2)
    query._log_after_tool(
        SimpleNamespace(name="get_top_sellers"),
        {"segment": "sabun"},
        fake_ctx2,
        "data mentah asli: 123",
    )
    assert query._current_turn_tool_outputs == ["data mentah asli: 123"], (
        f"FAIL: specialist callback seharusnya TETAP mengisi _current_turn_tool_outputs, "
        f"tapi isinya: {query._current_turn_tool_outputs}"
    )
    print("PASS: specialist callback tetap mengisi _current_turn_tool_outputs seperti biasa")

    assert query.kategori_specialist.mode == "single_turn", "FAIL: kategori_specialist.mode harus 'single_turn'"
    assert query.produk_specialist.mode == "single_turn", "FAIL: produk_specialist.mode harus 'single_turn'"
    print("PASS: kedua specialist punya mode='single_turn'")

    print("SEMUA CEK LULUS")


if __name__ == "__main__":
    main()
