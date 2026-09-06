"""Verifikasi manual: _log_possible_arg_extraction_miss() -- kanari LOG-ONLY
baru (2026-09-07, hasil audit shortcoming problem #2) yang menangkap
kemungkinan start_date/end_date GAGAL diekstrak dari pertanyaan meski tool
yang benar sudah terpanggil. Sengaja tidak mengoreksi apa pun -- lihat
docstring fungsi di query.py.

Dua hal yang diuji:
1. Regex-nya sendiri (_DATE_TOKEN_RE + _RANGE_CONNECTOR_RE) terhadap
   pertanyaan ASLI dari test_agent_cases.py CASES -- harus TIDAK false-positive
   pada kasus tanggal tunggal (DT*) atau kasus non-tanggal, dan HARUS
   menyala pada kasus rentang (RD*/PD*) yang argumennya sengaja dibuat rusak
   di sini untuk simulasi extraction-miss.
2. Fungsi end-to-end lewat tool_calls.log -- args rusak (start_date kosong
   padahal pertanyaan minta rentang) HARUS menulis NOTE; args yang benar
   (rentang asli, atau tanggal tunggal yang memang tunggal) TIDAK boleh
   menulis apa pun.

Tidak butuh Ollama. Jalankan: ./Scripts/python.exe _verify_arg_extraction_canary.py
"""

import os

import query
from test_agent_cases import CASES


class _FakeTool:
    def __init__(self, name):
        self.name = name


def main():
    log_path = "D:/agentic/agentic_dev/rag-env/_tmp_verify_arg_extraction.log"
    query.TOOL_LOG_PATH = log_path
    if os.path.exists(log_path):
        os.remove(log_path)
    open(log_path, "w", encoding="utf-8").close()

    # --- Part 1: regex sanity against real CASES questions ---------------
    by_id = {c[0]: c for c in CASES}

    single_date_ids = ["DT02", "DT07"]  # tanggal tunggal -- HARUS tidak menyala
    range_ids = ["RD02", "RD04", "RD07", "RD09", "RD15", "PD04"]  # rentang -- HARUS terdeteksi "smells like range"
    no_date_ids = ["EDU02"]  # sama sekali tanpa tanggal

    for cid in single_date_ids:
        q = by_id[cid][2][0]
        smells = bool(query._DATE_TOKEN_RE.search(q) and query._RANGE_CONNECTOR_RE.search(q))
        assert not smells, f"FAIL: {cid} tanggal tunggal seharusnya TIDAK terdeteksi sbg rentang: {q!r}"
    print(f"PASS: {single_date_ids} (tanggal tunggal) tidak terdeteksi sbg rentang (regex tidak overfire)")

    for cid in range_ids:
        q = by_id[cid][2][0]
        smells = bool(query._DATE_TOKEN_RE.search(q) and query._RANGE_CONNECTOR_RE.search(q))
        assert smells, f"FAIL: {cid} rentang seharusnya terdeteksi: {q!r}"
    print(f"PASS: {range_ids} (rentang asli, berbagai frasa) semuanya terdeteksi sbg 'smells like range'")

    for cid in no_date_ids:
        q = by_id[cid][2][0]
        smells = bool(query._DATE_TOKEN_RE.search(q) and query._RANGE_CONNECTOR_RE.search(q))
        assert not smells, f"FAIL: {cid} tanpa tanggal seharusnya tidak terdeteksi: {q!r}"
    print(f"PASS: {no_date_ids} (tanpa tanggal sama sekali) tidak terdeteksi")

    # --- Part 2: end-to-end canary firing on simulated extraction misses -
    tool = _FakeTool("get_top_sellers")

    # RD02's real question, but args simulate a TOTAL extraction failure
    # (model caught neither date at all) -- canary MUST fire. This is the
    # ONLY failure mode the narrowed canary targets, see docstring for why
    # partial cases (one side empty, or start==end) were excluded after the
    # 2026-09-07 evidence run showed those are valid designs, not misses.
    q_rd02 = by_id["RD02"][2][0]
    broken_args = {"segment": "", "top_n": 5, "start_date": "", "end_date": ""}
    query._log_possible_arg_extraction_miss(tool, broken_args, q_rd02)
    with open(log_path, encoding="utf-8") as f:
        log = f.read()
    assert "ARG-EXTRACTION MISS TOTAL" in log, f"FAIL: harus menyala utk kegagalan ekstraksi total: {log!r}"
    print("PASS: args gagal total (start_date DAN end_date kosong) pada pertanyaan rentang nyata -> kanari MENYALA")

    # Same question, but args are CORRECT (real range) -- canary must NOT fire.
    os.remove(log_path)
    open(log_path, "w", encoding="utf-8").close()
    good_args = {"segment": "", "top_n": 5, "start_date": "2026-08-01", "end_date": "2026-08-03"}
    query._log_possible_arg_extraction_miss(tool, good_args, q_rd02)
    with open(log_path, encoding="utf-8") as f:
        log = f.read()
    assert log == "", f"FAIL: args benar seharusnya tidak menulis apa pun: {log!r}"
    print("PASS: args benar (rentang nyata) pada pertanyaan rentang -> kanari DIAM")

    # Partial extraction (only one side captured, e.g. end_date empty) --
    # DTB07's real, documented design (open-ended range, defaults to
    # earliest available date) -- canary must NOT fire, confirmed by the
    # live evidence run this was previously a false positive.
    partial_args = {"segment": "", "top_n": 5, "start_date": "2026-08-01", "end_date": ""}
    query._log_possible_arg_extraction_miss(tool, partial_args, q_rd02)
    with open(log_path, encoding="utf-8") as f:
        log = f.read()
    assert log == "", f"FAIL: ekstraksi sebagian (satu sisi terisi) adalah desain valid (DTB07), bukan miss: {log!r}"
    print("PASS: ekstraksi sebagian (cuma start_date terisi) -> kanari DIAM (rentang terbuka valid, spt DTB07)")

    # Single-date question (DT02) with single-date args (correct, degenerate
    # range) -- canary must NOT fire (nothing to detect, not a range question).
    q_dt02 = by_id["DT02"][2][0]
    single_args = {"segment": "Minuman", "top_n": 5, "start_date": "2026-08-01", "end_date": "2026-08-01"}
    query._log_possible_arg_extraction_miss(tool, single_args, q_dt02)
    with open(log_path, encoding="utf-8") as f:
        log = f.read()
    assert log == "", f"FAIL: pertanyaan tanggal tunggal tidak boleh memicu kanari: {log!r}"
    print("PASS: pertanyaan tanggal tunggal (bukan rentang) -> kanari DIAM walau args 'degenerate'")

    # DTB04-style: real range question, args correctly have start_date ==
    # end_date (both non-empty, question literally says "X sampai X") --
    # confirmed valid design via live evidence run, canary must NOT fire.
    dtb04_args = {"segment": "", "top_n": 10, "start_date": "2026-08-01", "end_date": "2026-08-01"}
    query._log_possible_arg_extraction_miss(tool, dtb04_args, "produk terlaris tanggal 2026-08-01 sampai 2026-08-01")
    with open(log_path, encoding="utf-8") as f:
        log = f.read()
    assert log == "", f"FAIL: rentang satu-hari (start==end, keduanya terisi) adalah desain valid (DTB04): {log!r}"
    print("PASS: rentang satu-hari 'X sampai X' (keduanya terisi, sama) -> kanari DIAM (desain valid, spt DTB04)")

    # CPD01-style: "rentang" refers to PRICE range (get_price_range), not a
    # date range -- date args are a correct single-day extraction. Canary
    # must NOT fire even though the connector-word regex alone would match.
    cpd01_args = {"end_date": "2026-08-01", "top_n": 1, "segment": "Sabun Mandi", "start_date": "2026-08-01"}
    query._log_possible_arg_extraction_miss(
        tool, cpd01_args, "produk terlaris kategori Sabun Mandi tanggal 2026-08-01, dan berapa rentang harganya"
    )
    with open(log_path, encoding="utf-8") as f:
        log = f.read()
    assert log == "", f"FAIL: 'rentang harga' bukan rentang tanggal, args sudah benar (CPD01): {log!r}"
    print("PASS: 'rentang harganya' (rentang HARGA, bukan tanggal) -> kanari DIAM walau args sudah benar (spt CPD01)")

    # Tool not in _DATE_ARG_TOOLS (e.g. find_cross_sell_candidates) -> never fires.
    other_tool = _FakeTool("find_cross_sell_candidates")
    query._log_possible_arg_extraction_miss(other_tool, {}, q_rd02)
    with open(log_path, encoding="utf-8") as f:
        log = f.read()
    assert log == "", f"FAIL: tool tanpa argumen tanggal tidak boleh dicek: {log!r}"
    print("PASS: tool di luar _DATE_ARG_TOOLS -> kanari tidak pernah dicek")

    os.remove(log_path)
    print("SEMUA CEK LULUS")


if __name__ == "__main__":
    main()
