"""Bandingkan test_report.jsonl (pasca-migrasi) dengan
test_report_pre_migration.jsonl (baseline sebelum migrasi Graph) -- spec
2026-09-05-graph-migration-design.md bagian 7 langkah 5: cek tidak ada
error baru, tidak ada regresi tool-routing, dan ukur latensi kasus
compound (bukan asumsi).
Jalankan setelah test_agent_cases.py selesai:
./Scripts/python.exe compare_migration_report.py
"""

import json

BEFORE_PATH = "D:/agentic/agentic_dev/rag-env/test_report_pre_migration.jsonl"
AFTER_PATH = "D:/agentic/agentic_dev/rag-env/test_report.jsonl"
COMPOUND_CASE_IDS = ["CP01", "CP02", "CP03", "CP04", "CP05"]


def _load(path):
    records = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            rec = json.loads(line)
            records[rec["case_id"]] = rec
    return records


def main():
    before = _load(BEFORE_PATH)
    after = _load(AFTER_PATH)

    print(f"Jumlah kasus: sebelum={len(before)}, sesudah={len(after)}")
    print("(kalau salah satu bukan 104, salah satu file kemungkinan terpotong/run belum selesai)")

    new_errors = []
    tool_mismatches = []
    for case_id, after_rec in after.items():
        before_rec = before.get(case_id)
        if before_rec is None:
            continue
        if after_rec["had_error"] and not before_rec["had_error"]:
            new_errors.append(case_id)
        if sorted(after_rec["actual_tools"]) != sorted(before_rec["actual_tools"]):
            tool_mismatches.append((case_id, before_rec["actual_tools"], after_rec["actual_tools"]))

    print(f"Kasus dengan error BARU (tidak ada sebelum migrasi): {new_errors or 'tidak ada'}")

    print(f"\nPerbedaan actual_tools ({len(tool_mismatches)} kasus -- REVIEW MANUAL, bukan otomatis gagal):")
    for case_id, before_tools, after_tools in tool_mismatches:
        print(f"  {case_id}: sebelum={before_tools} sesudah={after_tools}")

    print("\nLatensi kasus compound (CP01-CP05, spec bagian 7 langkah 5):")
    for case_id in COMPOUND_CASE_IDS:
        b = before.get(case_id)
        a = after.get(case_id)
        if not b or not a:
            print(f"  {case_id}: tidak ada di salah satu report, lewati")
            continue
        b_dur = sum(t["duration_sec"] for t in b["turns"])
        a_dur = sum(t["duration_sec"] for t in a["turns"])
        delta = a_dur - b_dur
        pct = (delta / b_dur * 100) if b_dur else 0
        print(f"  {case_id}: sebelum={b_dur:.1f}s sesudah={a_dur:.1f}s ({delta:+.1f}s, {pct:+.0f}%)")


if __name__ == "__main__":
    main()
