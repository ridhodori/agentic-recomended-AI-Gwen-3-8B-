"""Verifikasi manual: _log_possible_per_day_miss() menulis baris NOTE ke
tool_calls.log ketika pertanyaan menyebut sinyal per-hari ("per hari"/"tiap
hari"/dst.) TAPI tool_outputs tidak berisi header tanggal (format
get_top_sellers_by_day) -- kanari observability untuk sisa miss-rate yang
ditemukan lewat pengujian 200-kasus (2026-09-06), lihat catatan di
_log_possible_per_day_miss. Tidak boleh menulis apa pun untuk kasus normal
(tanpa sinyal per-hari, atau breakdown per-hari yang benar, atau nol tool
dipanggil). Tidak butuh Ollama (baca/tulis tool_calls.log langsung).
Jalankan: ./Scripts/python.exe _verify_per_day_miss_canary.py
"""

import os

import query

query.TOOL_LOG_PATH = "D:/agentic/agentic_dev/rag-env/_tmp_verify_per_day_canary.log"


def _reset_log():
    if os.path.exists(query.TOOL_LOG_PATH):
        os.remove(query.TOOL_LOG_PATH)


def _log_contents():
    if not os.path.exists(query.TOOL_LOG_PATH):
        return ""
    with open(query.TOOL_LOG_PATH, encoding="utf-8") as f:
        return f.read()


def main():
    # Kasus 1: sinyal per-hari + tool_outputs TANPA header tanggal -> HARUS log
    _reset_log()
    query._log_possible_per_day_miss(
        "top 2 produk terlaris per hari dari tanggal 2026-08-01 sampai 2026-08-03",
        "- Sari Roti Sandwich Isi Cokelat 40 g | kategori: Makanan | terjual: 35368x | harga: 6000",
    )
    log = _log_contents()
    assert "KEMUNGKINAN PER-HARI MISS" in log, f"FAIL: harusnya ke-log, tidak ada: {log!r}"
    print("PASS: sinyal per-hari + tool_outputs tanpa header tanggal -> ke-log")

    # Kasus 2: sinyal per-hari + tool_outputs DENGAN header tanggal (benar) -> TIDAK log
    _reset_log()
    query._log_possible_per_day_miss(
        "top 2 produk terlaris per hari dari tanggal 2026-08-01 sampai 2026-08-03",
        "2026-08-01:\n  - Sari Roti Sandwich Isi Cokelat 40 g | kategori: Makanan | terjual: 11589x | harga: 6000\n"
        "2026-08-03:\n  - Teh Pucuk Harum Melati 350 ml | kategori: Minuman | terjual: 14322x | harga: 4100",
    )
    log = _log_contents()
    assert "KEMUNGKINAN PER-HARI MISS" not in log, f"FAIL: seharusnya TIDAK ke-log (breakdown sudah benar): {log!r}"
    print("PASS: sinyal per-hari + tool_outputs DENGAN header tanggal (breakdown benar) -> tidak ke-log")

    # Kasus 3: tanpa sinyal per-hari sama sekali -> TIDAK log
    _reset_log()
    query._log_possible_per_day_miss(
        "produk terlaris kategori Minuman",
        "- Teh Pucuk Harum Melati 350 ml | kategori: Minuman | terjual: 30854x | harga: 4100",
    )
    log = _log_contents()
    assert "KEMUNGKINAN PER-HARI MISS" not in log, f"FAIL: tanpa sinyal per-hari seharusnya tidak ke-log: {log!r}"
    print("PASS: pertanyaan tanpa sinyal per-hari -> tidak ke-log")

    # Kasus 4: sinyal per-hari TAPI nol tool dipanggil (kemungkinan penolakan sah) -> TIDAK log
    _reset_log()
    query._log_possible_per_day_miss(
        "top 2 produk terlaris per hari dari tanggal 2020-01-01 sampai 2020-01-03",
        "",
    )
    log = _log_contents()
    assert "KEMUNGKINAN PER-HARI MISS" not in log, f"FAIL: nol tool dipanggil seharusnya tidak ke-log: {log!r}"
    print("PASS: nol tool dipanggil (kemungkinan penolakan sah di luar rentang) -> tidak ke-log")

    _reset_log()
    print("SEMUA CEK LULUS")


if __name__ == "__main__":
    main()
