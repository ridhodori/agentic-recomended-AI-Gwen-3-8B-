# Upgrade Data Transaksi v2 (transaction_time + item_qty) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix `query.py`/`aggregate_sales.py` (currently broken -- they point at a `transaction_data.csv` that no longer exists), switch "terjual" from a row-count to net `item_qty` sum, and add three new time-aware capabilities (date-range filter, day-over-day trending, peak selling hours) -- all while rewriting every LLM-facing prompt (tool docstrings, the 3 specialist instructions, the internal verify/revise prompt) to English for better instruction-following, with an explicit safeguard so entity values extracted from Indonesian user questions are never translated when passed to tools.

**Architecture:** No structural change -- stays on the existing google-adk router (`root_agent`) + 2 specialists (`kategori_specialist`, `produk_specialist`) design from the Graph migration. New capabilities are added as new tool functions on the existing specialists (no new specialist/agent). Data loading moves from a single hardcoded CSV path to a glob over `transaction_data/transaction_day*.csv`, concatenated and parsed once into a real datetime column.

**Tech Stack:** Python 3.10+, pandas, google-adk (`Agent`, `LiteLlm`, `Runner`, `SqliteSessionService`), ChromaDB 1.5.9, Ollama (`qwen3-agent:latest` + `nomic-embed-text`). No new dependencies -- verification uses this project's existing standalone `_verify_*.py` script convention (see `_verify_router_wiring.py`), not pytest (not installed, and introducing it would be an unrequested tooling change).

**Spec:** `2026-09-06-transaction-data-v2-design.md` (sections 1-6, all approved)

## Deviations found while planning (not in the spec, discovered by inspecting the actual installed libraries)

1. **`build_index.py` has a silent-staleness bug that would defeat the whole point of "rebuild the index after regenerating `katalog_produk.csv`" (spec section 2).** It calls `collection.add(ids=[...], ...)`. Verified directly against the installed `chromadb==1.5.9`: `add()` on an ID that already exists in the collection does **not** raise and does **not** update the document -- it silently keeps the OLD document. Since every product ID already exists in the `products` collection from the last build, re-running `build_index.py` as-is would re-embed nothing and leave every product's embedded `terjual` text stale forever. Fix: switch to `collection.upsert(...)` (same call signature, overwrites existing IDs). This is Task 6 below -- not in the original spec, added here.
2. `transaction_time` parses cleanly with `pd.to_datetime(s, format="%Y-%m-%d%H:%M:%S")` with no manual separator insertion needed (confirmed: pandas/strptime handles fixed-width concatenated fields fine as long as every row is exactly 18 chars, which was verified true for all ~3M rows across the 3 files).

## Global Constraints

- "Terjual" (units sold) is ALWAYS `sum(item_qty)` per group (net of returns) -- never a row count, never a gross-only (positive-only) sum. Applies to `get_top_sellers`, `get_top_categories`, `get_peak_hours`, `get_trending_products`, `get_trending_categories`, and `aggregate_sales.py`.
- Transaction file glob is exactly `transaction_data/transaction_day*.csv` (not a bare `*.csv`) -- both in `query.py` and `aggregate_sales.py`.
- **Language split (critical, easy to get backwards):** ONLY these get translated to English: (a) each tool's Python docstring (`Args`/`Returns` -- this is what ADK turns into the JSON schema the model sees), (b) `ROOT_INSTRUCTION`/`KATEGORI_INSTRUCTION`/`PRODUK_INSTRUCTION`, (c) the internal grading prompt inside `_verify_and_revise_impl`. Everything else stays Indonesian: every tool's **return value** (the actual string data sent back to the model, e.g. `"Tidak ada produk yang cocok."`) stays Indonesian exactly as today, because that text often gets echoed straight into the final Indonesian answer. Code comments stay Indonesian (developer documentation, never sent to the model).
- Every one of the 3 instructions (root/kategori/produk) MUST contain both of these rules verbatim in spirit: (1) always respond in Bahasa Indonesia regardless of the instruction's language, (2) never translate an extracted product/category/segment value into English, because tool arguments are matched via literal substring search against Indonesian data.
- "Trending" always compares the two most recent **distinct dates actually present** in the currently-loaded data (`sorted(dt.date.unique())[-2:]`) -- never a hardcoded day pair. This is what makes it self-scaling as more `transaction_dayN.csv` files are added later.
- No new pip dependencies. No pytest. Verification scripts follow the existing `_verify_*.py` pattern: plain script, `import query` (or `import aggregate_sales`), assert with a descriptive message, print `PASS: ...`, `python.exe _verify_whatever.py` to run.
- Stay on google-adk. Do not introduce LangChain/LangGraph.

---

## File Structure

- **Modify `query.py`**: data loading (glob + datetime parsing), `get_top_sellers`/`get_top_categories` (net qty + date filters), new tools `get_peak_hours`/`get_trending_products`/`get_trending_categories`, all 3 instructions (English + dynamic date range + tool registration), `_verify_and_revise_impl`'s internal prompt (English), module docstring.
- **Modify `aggregate_sales.py`**: glob over daily files, net qty sum instead of row count.
- **Modify `build_index.py`**: one-line fix, `add()` -> `upsert()`.
- **Modify `test_agent_cases.py`**: new test cases for the 3 new capabilities, updated case for trending-without-explicit-date-words, updated module docstring.
- **Create `_verify_transactions_loading.py`, `_verify_net_qty_ranking.py`, `_verify_trending_and_peak_hours.py`, `_verify_aggregate_sales_net_qty.py`, `_verify_chromadb_upsert.py`, `_verify_bilingual_instructions.py`**: new standalone verification scripts, one per task below, following the existing `_verify_*.py` convention. These stay in the repo afterward as regression checks (like `_verify_router_wiring.py` already does), not thrown away.

---

### Task 1: Fix data loading -- glob multiple daily files, parse `transaction_time`

**Files:**
- Modify: `query.py` (imports near top; `TRANSACTION_CSV` constant and `_load_transactions()`, currently around lines 62-104)
- Create: `_verify_transactions_loading.py`

**Interfaces:**
- Produces: `TRANSACTION_CSV_GLOB: str` (module constant, replaces `TRANSACTION_CSV`), `_load_transactions() -> pd.DataFrame` (same name/signature as before, but now returns a DataFrame with `transaction_time` as a real `datetime64` column, concatenated from every file matching the glob).

- [ ] **Step 1: Write the failing verify script**

Create `_verify_transactions_loading.py`:

```python
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
```

- [ ] **Step 2: Run it, confirm it fails**

Run: `./Scripts/python.exe _verify_transactions_loading.py`
Expected: `FileNotFoundError` or `AttributeError: module 'query' has no attribute 'TRANSACTION_CSV_GLOB'` -- the glob constant doesn't exist yet, `_load_transactions()` still reads the single hardcoded (nonexistent) `transaction_data.csv`.

- [ ] **Step 3: Implement**

Add `import glob` to the import block at the top of `query.py` (alongside the existing `import asyncio`, `import os`, etc.).

Replace:
```python
TRANSACTION_CSV = "D:/agentic/transaction_data/transaction_data.csv"
```
with:
```python
TRANSACTION_CSV_GLOB = "D:/agentic/transaction_data/transaction_day*.csv"
```

Replace `_load_transactions()`:
```python
def _load_transactions():
    global _transactions_cache
    if _transactions_cache is None:
        _transactions_cache = pd.read_csv(TRANSACTION_CSV)
    return _transactions_cache
```
with:
```python
def _load_transactions():
    global _transactions_cache
    if _transactions_cache is None:
        paths = sorted(glob.glob(TRANSACTION_CSV_GLOB))
        df = pd.concat((pd.read_csv(p) for p in paths), ignore_index=True)
        df["transaction_time"] = pd.to_datetime(df["transaction_time"], format="%Y-%m-%d%H:%M:%S")
        _transactions_cache = df
    return _transactions_cache
```

- [ ] **Step 4: Run it, confirm it passes**

Run: `./Scripts/python.exe _verify_transactions_loading.py`
Expected: `SEMUA CEK LULUS` printed, all 4 `PASS:` lines shown, exit code 0.

- [ ] **Step 5: Commit**

```bash
git add query.py _verify_transactions_loading.py
git commit -m "fix: load transaction_data/transaction_day*.csv, parse transaction_time to datetime

query.py pointed at the old single-file transaction_data.csv, which no
longer exists (replaced by transaction_day1/2/3.csv with new
transaction_time/item_qty columns). Glob + concat all daily files and
parse transaction_time once into a real datetime column so downstream
tools can filter/group by date and hour.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 2: Net-qty ranking + optional date filters (`get_top_sellers`, `get_top_categories`)

**Files:**
- Modify: `query.py` (`_get_top_sellers_impl`/`get_top_sellers`, `_get_top_categories_impl`/`get_top_categories`, currently around lines 169-274)
- Create: `_verify_net_qty_ranking.py`

**Interfaces:**
- Consumes: `_load_transactions()` from Task 1 (returns df with datetime `transaction_time`, numeric `item_qty`).
- Produces: `_filter_by_date(df, start_date, end_date) -> tuple[pd.DataFrame, str]` (shared helper, second element is a non-empty Indonesian error string if the filter can't be honored, empty string otherwise). `get_top_sellers(segment, top_n=5, start_date="", end_date="")`, `get_top_categories(top_n=5, terendah=False, start_date="", end_date="")` -- both new trailing params default to `""` (no filter, same as current behavior).

- [ ] **Step 1: Write the failing verify script**

Create `_verify_net_qty_ranking.py`:

```python
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
```

- [ ] **Step 2: Run it, confirm it fails**

Run: `./Scripts/python.exe _verify_net_qty_ranking.py`
Expected: `TypeError: _get_top_sellers_impl() takes 2 positional arguments but 4 were given` (current signature is `(segment, top_n)` only, and ranks by row count so `terjual: 13x` would never appear even if the call succeeded).

- [ ] **Step 3: Implement**

Add this helper above `_get_top_sellers_impl`:

```python
def _filter_by_date(df: pd.DataFrame, start_date: str, end_date: str) -> tuple[pd.DataFrame, str]:
    """Filter df berdasarkan rentang tanggal transaction_time (inklusif di
    kedua ujung). start_date/end_date kosong berarti tanpa batas di sisi
    itu -- default tanpa filter sama sekali kalau keduanya kosong (lihat
    2026-09-06-transaction-data-v2-design.md bagian 3a). Mengembalikan
    (df_terfilter, pesan_error) -- pesan_error non-kosong kalau formatnya
    salah atau rentangnya di luar data yang tersedia, supaya tool bisa
    menolak jujur alih-alih mengarang dari df kosong."""
    if not start_date and not end_date:
        return df, ""
    available_min = df["transaction_time"].dt.date.min()
    available_max = df["transaction_time"].dt.date.max()
    try:
        start = pd.Timestamp(start_date).date() if start_date else available_min
        end = pd.Timestamp(end_date).date() if end_date else available_max
    except ValueError:
        return df.iloc[0:0], (
            f"Format tanggal tidak valid (pakai YYYY-MM-DD): start_date='{start_date}' end_date='{end_date}'."
        )
    if start > available_max or end < available_min:
        return df.iloc[0:0], (
            f"Tidak ada data untuk rentang {start_date or available_min} s.d. {end_date or available_max} -- "
            f"data yang tersedia cuma {available_min} s.d. {available_max}."
        )
    mask = (df["transaction_time"].dt.date >= start) & (df["transaction_time"].dt.date <= end)
    return df[mask], ""
```

Replace `_get_top_sellers_impl` and `get_top_sellers`:
```python
def _get_top_sellers_impl(segment: str, top_n: int, start_date: str, end_date: str) -> str:
    if not segment.strip():
        return "Segmen kosong, tidak bisa mencari produk terlaris."

    df = _load_transactions()
    df, date_error = _filter_by_date(df, start_date, end_date)
    if date_error:
        return date_error

    mask = df["product_name"].str.contains(segment, case=False, na=False) | df[
        "product_category_name_lvl_0"
    ].str.contains(segment, case=False, na=False)
    filtered = df[mask]

    if filtered.empty:
        return f"Tidak ada data penjualan untuk segmen '{segment}'."

    top = (
        filtered.groupby("product_name")
        .agg(
            terjual=("item_qty", "sum"),
            harga=("product_price", "first"),
            kategori=("product_category_name_lvl_0", "first"),
        )
        .sort_values("terjual", ascending=False)
        .head(top_n)
    )
    return "\n".join(
        f"- {name} | kategori: {row.kategori} | terjual: {row.terjual:.0f}x | harga: {row.harga:.0f}"
        for name, row in top.iterrows()
    )


async def get_top_sellers(
    segment: str, top_n: int = 5, start_date: str = "", end_date: str = ""
) -> str:
    """
    Find best-selling products from raw transaction data, filtered by segment.

    Args:
      segment: Product/category keyword to filter by, e.g. "sabun mandi" or "minuman".
      top_n: Number of top-selling products to return.
      start_date: Optional start date (YYYY-MM-DD), inclusive. Empty ("") means no
        lower bound -- use every available date.
      end_date: Optional end date (YYYY-MM-DD), inclusive. Empty ("") means no upper
        bound -- use every available date.

    Returns:
      str: List of best-selling products with category, net units sold (returns
        subtracted), and price, one per line.
    """
    return await asyncio.to_thread(_get_top_sellers_impl, segment, top_n, start_date, end_date)
```

Replace `_get_top_categories_impl` and `get_top_categories`:
```python
def _get_top_categories_impl(top_n: int, terendah: bool, start_date: str, end_date: str) -> str:
    df = _load_transactions()
    df, date_error = _filter_by_date(df, start_date, end_date)
    if date_error:
        return date_error

    counts = df.groupby("product_category_name_lvl_0")["item_qty"].sum()
    top = counts.sort_values(ascending=terendah).head(top_n)

    if top.empty:
        return "Tidak ada data kategori."

    return "\n".join(f"- {cat} | terjual: {count:.0f}x" for cat, count in top.items())


async def get_top_categories(
    top_n: int = 5, terendah: bool = False, start_date: str = "", end_date: str = ""
) -> str:
    """
    Find product categories with the highest (or lowest) net units sold from raw
    transaction data.

    Args:
      top_n: Number of categories to return.
      terendah: True to sort by lowest net units sold first, False (default) for
        highest first.
      start_date: Optional start date (YYYY-MM-DD), inclusive. Empty ("") means no
        lower bound -- use every available date.
      end_date: Optional end date (YYYY-MM-DD), inclusive. Empty ("") means no upper
        bound -- use every available date.

    Returns:
      str: List of categories with net units sold, one per line.
    """
    return await asyncio.to_thread(_get_top_categories_impl, top_n, terendah, start_date, end_date)
```

Note: `_get_top_categories_impl` switched from `value_counts()` (auto-sorted) to `groupby().sum()` (NOT auto-sorted) -- the explicit `.sort_values(ascending=terendah)` above is required for both the `terendah=True` and `terendah=False` cases, unlike the old code which only sorted explicitly for the `terendah` branch.

- [ ] **Step 4: Run it, confirm it passes**

Run: `./Scripts/python.exe _verify_net_qty_ranking.py`
Expected: `SEMUA CEK LULUS`, all 4 `PASS:` lines.

- [ ] **Step 5: Commit**

```bash
git add query.py _verify_net_qty_ranking.py
git commit -m "feat: rank by net item_qty instead of row count, add date-range filters

get_top_sellers/get_top_categories now sum item_qty (returns subtract
from the total) instead of counting transaction rows, and accept
optional start_date/end_date to answer date-specific questions -- both
default to '' (no filter, same behavior as before).

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 3: New tool -- `get_peak_hours`

**Files:**
- Modify: `query.py` (add new functions after `get_price_range`, before `ROOT_INSTRUCTION`; add to `produk_specialist.tools`)
- Create: `_verify_trending_and_peak_hours.py` (shared with Task 4, both are date/hour aggregation tools)

**Interfaces:**
- Consumes: `_load_transactions()` (Task 1).
- Produces: `get_peak_hours(segment="", top_n=5) -> str`.

- [ ] **Step 1: Write the failing verify script**

Create `_verify_trending_and_peak_hours.py`:

```python
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
```

- [ ] **Step 2: Run it, confirm it fails**

Run: `./Scripts/python.exe _verify_trending_and_peak_hours.py`
Expected: `AttributeError: module 'query' has no attribute '_get_peak_hours_impl'`.

- [ ] **Step 3: Implement**

Add after `get_price_range`/`async def get_price_range` in `query.py`:

```python
def _get_peak_hours_impl(segment: str, top_n: int) -> str:
    df = _load_transactions()
    if segment.strip():
        mask = df["product_name"].str.contains(segment, case=False, na=False) | df[
            "product_category_name_lvl_0"
        ].str.contains(segment, case=False, na=False)
        df = df[mask]
        if df.empty:
            return f"Tidak ada data penjualan untuk segmen '{segment}'."

    by_hour = df.groupby(df["transaction_time"].dt.hour)["item_qty"].sum()
    top = by_hour.sort_values(ascending=False).head(top_n)

    if top.empty:
        return "Tidak ada data jam penjualan."

    return "\n".join(f"- jam {hour:02d}:00-{hour:02d}:59 | terjual: {qty:.0f}x" for hour, qty in top.items())


async def get_peak_hours(segment: str = "", top_n: int = 5) -> str:
    """
    Find the busiest hours of the day (by net units sold) from raw transaction data,
    optionally filtered by segment. Useful for staffing or promo-timing decisions.

    Args:
      segment: Optional product/category keyword to filter by, e.g. "sabun mandi".
        Empty ("") means use every available transaction, not just one segment.
      top_n: Number of top hours to return.

    Returns:
      str: List of the busiest hours (0-23, as recorded in the data) with net units
        sold, one per line, sorted busiest first.
    """
    return await asyncio.to_thread(_get_peak_hours_impl, segment, top_n)
```

Add `get_peak_hours` to `produk_specialist`'s `tools=[...]` list (further down in the file, where `produk_specialist = Agent(...)` is defined).

- [ ] **Step 4: Run it, confirm the peak-hours assertions pass** (trending assertions will still fail until Task 4)

Run: `./Scripts/python.exe _verify_trending_and_peak_hours.py`
Expected: the two `PASS:` lines for peak hours print, then `AttributeError: module 'query' has no attribute '_get_trending_products_impl'`.

- [ ] **Step 5: Commit**

```bash
git add query.py _verify_trending_and_peak_hours.py
git commit -m "feat: add get_peak_hours tool for staffing/promo-timing questions

Groups net item_qty by hour-of-day (0-23) extracted from
transaction_time, optionally filtered by segment. Registered on
produk_specialist.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 4: New tools -- `get_trending_products`, `get_trending_categories`

**Files:**
- Modify: `query.py` (add after `get_peak_hours`; add to `produk_specialist.tools`/`kategori_specialist.tools`)
- Modify: `_verify_trending_and_peak_hours.py` (already written in Task 3, just needs the implementation to make its trending assertions pass)

**Interfaces:**
- Consumes: `_load_transactions()` (Task 1).
- Produces: `_latest_two_dates(df) -> tuple[date, date] | None` (shared helper), `get_trending_products(segment="", top_n=5) -> str`, `get_trending_categories(top_n=5) -> str`.

- [ ] **Step 1: Confirm the trending assertions in `_verify_trending_and_peak_hours.py` still fail**

Run: `./Scripts/python.exe _verify_trending_and_peak_hours.py`
Expected (from end of Task 3): `AttributeError: module 'query' has no attribute '_get_trending_products_impl'`.

- [ ] **Step 2: Implement**

Add after `get_peak_hours` in `query.py`:

```python
def _latest_two_dates(df: pd.DataFrame):
    """(tanggal_sebelumnya, tanggal_terbaru) yang BENAR-BENAR ada di df,
    bukan hardcode day2/day3 -- otomatis mengikuti data terbaru kalau
    transaction_dayN.csv baru ditambahkan nanti (lihat
    2026-09-06-transaction-data-v2-design.md bagian 3b). None kalau df
    punya kurang dari 2 tanggal berbeda."""
    dates = sorted(df["transaction_time"].dt.date.unique())
    if len(dates) < 2:
        return None
    return dates[-2], dates[-1]


def _trending_impl(df: pd.DataFrame, group_col: str, top_n: int) -> str:
    """Logika bersama get_trending_products/get_trending_categories --
    beda cuma kolom yang di-groupby (product_name vs
    product_category_name_lvl_0)."""
    dates = _latest_two_dates(df)
    if dates is None:
        return "Tidak cukup data (butuh minimal 2 tanggal berbeda) untuk menghitung tren."
    prev_date, latest_date = dates

    prev_qty = df[df["transaction_time"].dt.date == prev_date].groupby(group_col)["item_qty"].sum()
    latest_qty = df[df["transaction_time"].dt.date == latest_date].groupby(group_col)["item_qty"].sum()
    growth = latest_qty.subtract(prev_qty, fill_value=0).sort_values(ascending=False).head(top_n)

    if growth.empty:
        return "Tidak ada data untuk menghitung tren."

    lines = [f"Dibandingkan {prev_date} vs {latest_date}:"]
    for name, delta in growth.items():
        lines.append(
            f"- {name} | {prev_date}: {prev_qty.get(name, 0):.0f}x -> "
            f"{latest_date}: {latest_qty.get(name, 0):.0f}x | perubahan: {delta:+.0f}"
        )
    return "\n".join(lines)


def _get_trending_products_impl(segment: str, top_n: int) -> str:
    df = _load_transactions()
    if segment.strip():
        mask = df["product_name"].str.contains(segment, case=False, na=False) | df[
            "product_category_name_lvl_0"
        ].str.contains(segment, case=False, na=False)
        df = df[mask]
        if df.empty:
            return f"Tidak ada data penjualan untuk segmen '{segment}'."
    return _trending_impl(df, "product_name", top_n)


async def get_trending_products(segment: str = "", top_n: int = 5) -> str:
    """
    Find products with the biggest growth in net units sold between the two most
    recent dates present in the transaction data (self-scaling: as more daily
    files are added, "trending" automatically follows the newest data).

    Args:
      segment: Optional product/category keyword to filter by, e.g. "sabun mandi".
        Empty ("") means consider every product, not just one segment.
      top_n: Number of top-growing products to return.

    Returns:
      str: The two dates being compared, followed by the top-growing products with
        their net units sold on each date and the change, sorted by biggest growth
        first. A product present on only one of the two dates is treated as having
        0 units on the missing date (so a brand-new hit or a vanished product both
        show up as a large change, not skipped).
    """
    return await asyncio.to_thread(_get_trending_products_impl, segment, top_n)


def _get_trending_categories_impl(top_n: int) -> str:
    df = _load_transactions()
    return _trending_impl(df, "product_category_name_lvl_0", top_n)


async def get_trending_categories(top_n: int = 5) -> str:
    """
    Find product categories with the biggest growth in net units sold between the
    two most recent dates present in the transaction data (self-scaling, see
    get_trending_products).

    Args:
      top_n: Number of top-growing categories to return.

    Returns:
      str: The two dates being compared, followed by the top-growing categories
        with net units sold on each date and the change, sorted by biggest growth
        first.
    """
    return await asyncio.to_thread(_get_trending_categories_impl, top_n)
```

Add `get_trending_products` to `produk_specialist`'s `tools=[...]` list, and `get_trending_categories` to `kategori_specialist`'s `tools=[...]` list.

- [ ] **Step 3: Run it, confirm it passes**

Run: `./Scripts/python.exe _verify_trending_and_peak_hours.py`
Expected: `SEMUA CEK LULUS`, all 5 `PASS:` lines.

- [ ] **Step 4: Commit**

```bash
git add query.py _verify_trending_and_peak_hours.py
git commit -m "feat: add get_trending_products/get_trending_categories

Compares net item_qty between the two most recent distinct dates
actually present in the loaded data (not a hardcoded day pair, so it
keeps working as more transaction_dayN.csv files are added). A
product/category present on only one of the two dates is treated as 0
on the missing date. get_trending_products on produk_specialist,
get_trending_categories on kategori_specialist.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 5: `aggregate_sales.py` -- glob multiple files, net qty instead of row count

**Files:**
- Modify: `aggregate_sales.py` (whole file, currently 59 lines)
- Create: `_verify_aggregate_sales_net_qty.py`

**Interfaces:**
- Produces: `TRANSACTION_CSV_GLOB: str`, `sum_net_qty_per_product(paths: list[str]) -> Counter` (replaces `count_sales_per_product(path)`).

- [ ] **Step 1: Write the failing verify script**

Create `_verify_aggregate_sales_net_qty.py`:

```python
"""Verifikasi manual: aggregate_sales.py sekarang baca banyak file harian
(glob) dan hitung net qty (sum item_qty, retur mengurangi) bukan hitung
baris. Tidak butuh Ollama/ChromaDB.
Jalankan: ./Scripts/python.exe _verify_aggregate_sales_net_qty.py
"""

import csv
import os
import tempfile

import aggregate_sales

CSV_HEADER = ["product_name", "product_category_name_lvl_0", "product_price", "product_short_desc", "transaction_time", "item_qty"]


def _write_csv(path, rows):
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(CSV_HEADER)
        w.writerows(rows)


def main():
    tmpdir = tempfile.mkdtemp()
    day1 = os.path.join(tmpdir, "transaction_day1.csv")
    day2 = os.path.join(tmpdir, "transaction_day2.csv")
    _write_csv(day1, [
        ["Produk A", "Cat1", 1000, "", "2026-08-0105:00:00", 5],
        ["Produk A", "Cat1", 1000, "", "2026-08-0106:00:00", -2],
    ])
    _write_csv(day2, [
        ["Produk A", "Cat1", 1000, "", "2026-08-0205:00:00", 10],
        ["Produk B", "Cat1", 500, "", "2026-08-0205:00:00", 1],
    ])

    totals = aggregate_sales.sum_net_qty_per_product([day1, day2])
    key_a = aggregate_sales.normalize_name("Produk A")
    key_b = aggregate_sales.normalize_name("Produk B")

    assert totals[key_a] == 13, f"FAIL: Produk A harusnya net 5-2+10=13, dapat {totals[key_a]}"
    print("PASS: net qty tergabung dari 2 file, retur mengurangi total")

    assert totals[key_b] == 1, f"FAIL: Produk B harusnya 1, dapat {totals[key_b]}"
    print("PASS: produk yang cuma ada di satu file tetap terhitung benar")

    print("SEMUA CEK LULUS")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run it, confirm it fails**

Run: `./Scripts/python.exe _verify_aggregate_sales_net_qty.py`
Expected: `AttributeError: module 'aggregate_sales' has no attribute 'sum_net_qty_per_product'`.

- [ ] **Step 3: Implement**

Replace the whole content of `aggregate_sales.py`:

```python
"""
Agregasi data penjualan mentah (transaction_data/transaction_day*.csv) jadi
net qty "terjual" per produk, lalu digabung ke katalog_produk.csv sebagai
sinyal popularitas.

Data transaksi terdiri dari beberapa file harian (satu file per hari,
transaction_dayN.csv), masing-masing berisi: product_name,
product_category_name_lvl_0, product_price, product_short_desc,
transaction_time, item_qty. TANPA user_id -- jadi tidak bisa dipakai untuk
riwayat per-user, hanya untuk menghitung net unit yang benar-benar terjual
(item_qty bisa negatif untuk retur/pembatalan -- retur MENGURANGI total,
bukan diabaikan, lihat 2026-09-06-transaction-data-v2-design.md bagian 2).

Karena tidak ada product_id yang sama antara data transaksi dan
katalog_produk.csv, penggabungan dilakukan berdasarkan nama produk yang
dinormalisasi (lower + whitespace dirapikan). Produk di katalog yang tidak
ditemukan di data penjualan diberi nilai terjual=0.
"""

import csv
import glob
import re
from collections import Counter

import pandas as pd

TRANSACTION_CSV_GLOB = "D:/agentic/transaction_data/transaction_day*.csv"
KATALOG_CSV = "D:/agentic/chroma_db/katalog_produk.csv"


def normalize_name(name):
    return re.sub(r"\s+", " ", (name or "").strip().lower())


def sum_net_qty_per_product(paths):
    """Net qty (item_qty negatif/retur mengurangi) per nama produk
    ternormalisasi, digabung dari SEMUA file yang diberikan."""
    totals = Counter()
    for path in paths:
        with open(path, encoding="utf-8", errors="replace") as f:
            reader = csv.DictReader(f)
            for row in reader:
                key = normalize_name(row.get("product_name"))
                if not key:
                    continue
                try:
                    qty = float(row.get("item_qty", 0) or 0)
                except ValueError:
                    qty = 0
                totals[key] += qty
    return totals


def main():
    paths = sorted(glob.glob(TRANSACTION_CSV_GLOB))
    print(f"Membaca dan menghitung penjualan (net qty) dari {len(paths)} file: {paths} ...")
    sales_totals = sum_net_qty_per_product(paths)
    print(f"Total unit net terjual (semua produk): {sum(sales_totals.values()):.0f}")
    print(f"Total nama produk unik di data penjualan: {len(sales_totals)}")

    df = pd.read_csv(KATALOG_CSV)
    df["terjual"] = df["nama"].apply(lambda n: sales_totals.get(normalize_name(n), 0))

    matched = (df["terjual"] > 0).sum()
    print(f"\nKatalog: {len(df)} produk, {matched} di antaranya cocok dengan data penjualan.")

    df.to_csv(KATALOG_CSV, index=False)
    print(f"Selesai: kolom 'terjual' (net qty) ditambahkan ke {KATALOG_CSV}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run it, confirm it passes**

Run: `./Scripts/python.exe _verify_aggregate_sales_net_qty.py`
Expected: `SEMUA CEK LULUS`, both `PASS:` lines.

- [ ] **Step 5: Commit**

```bash
git add aggregate_sales.py _verify_aggregate_sales_net_qty.py
git commit -m "fix: aggregate_sales.py reads transaction_day*.csv, sums net item_qty

Old script pointed at the now-nonexistent single transaction_data.csv
and counted rows. Now globs every daily file and sums item_qty (net of
returns) per product, matching the same net-qty definition used by
query.py's get_top_sellers/get_top_categories.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 6: Fix `build_index.py` silent-staleness bug (`add` -> `upsert`)

**Files:**
- Modify: `build_index.py` (one line)
- Create: `_verify_chromadb_upsert.py`

**Interfaces:** None (internal fix, no signature changes).

- [ ] **Step 1: Write the failing verify script**

Create `_verify_chromadb_upsert.py`:

```python
"""Verifikasi manual: chromadb.Collection.add() pada ID yang SUDAH ADA diam-
diam TIDAK memperbarui dokumen (bukan error, bukan no-op yang kelihatan) --
upsert() yang benar-benar menimpa. Dikonfirmasi langsung terhadap
chromadb==1.5.9 yang terinstall. Kalau build_index.py masih pakai add(),
menjalankannya ulang setelah katalog_produk.csv berubah TIDAK memperbarui
angka 'terjual' yang ter-embed untuk produk yang sudah pernah di-index --
index semantik jadi basi selamanya. Tidak butuh Ollama (pakai
PersistentClient sementara di folder temp, bukan koleksi produksi).
Jalankan: ./Scripts/python.exe _verify_chromadb_upsert.py
"""

import tempfile

import chromadb


def main():
    tmp = tempfile.mkdtemp()
    client = chromadb.PersistentClient(path=tmp)
    col = client.get_or_create_collection(name="test")

    col.add(ids=["1"], embeddings=[[0.1, 0.2]], documents=["versi lama"])
    got = col.get(ids=["1"])
    assert got["documents"] == ["versi lama"], f"FAIL: seed awal salah: {got}"

    col.add(ids=["1"], embeddings=[[0.9, 0.9]], documents=["versi baru"])
    got = col.get(ids=["1"])
    assert got["documents"] == ["versi lama"], (
        f"FAIL (unexpected): add() pada ID yang sama ternyata MEMPERBARUI dokumen di versi "
        f"chromadb ini ({got}) -- build_index.py TIDAK perlu diubah ke upsert(), cek ulang temuan ini."
    )
    print("PASS: dikonfirmasi -- add() pada ID yang sudah ada diam-diam TIDAK memperbarui dokumen")

    col.upsert(ids=["1"], embeddings=[[0.9, 0.9]], documents=["versi baru"])
    got = col.get(ids=["1"])
    assert got["documents"] == ["versi baru"], f"FAIL: upsert() seharusnya menimpa, dapat: {got}"
    print("PASS: upsert() pada ID yang sama BENAR-BENAR menimpa dokumen")

    print("SEMUA CEK LULUS")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run it, confirm the first assertion already passes (documenting existing library behavior) and the flow reaches the upsert check**

Run: `./Scripts/python.exe _verify_chromadb_upsert.py`
Expected: `SEMUA CEK LULUS` -- this script doesn't test *our* code yet, it documents chromadb's actual behavior (already true today, nothing to implement here yet). This step exists to lock in the finding before touching `build_index.py`, so a future chromadb upgrade that changes `add()`'s semantics would be caught by this script failing.

- [ ] **Step 3: Implement the actual fix in `build_index.py`**

In `build_index.py`, replace:
```python
    collection.add(ids=[str(row["id"])], embeddings=[emb], documents=[text])
```
with:
```python
    collection.upsert(ids=[str(row["id"])], embeddings=[emb], documents=[text])
```

- [ ] **Step 4: Confirm `build_index.py` still runs end-to-end on a tiny sample**

There's no automated test for this step (it's an I/O script driving real Ollama embedding calls) -- do a manual smoke check instead: temporarily point `KATALOG_CSV` at a 2-3 row copy of the real catalog (or just read the first few rows in a REPL) and confirm `collection.upsert(...)` doesn't raise. Full production re-run happens in Task 9.

- [ ] **Step 5: Commit**

```bash
git add build_index.py _verify_chromadb_upsert.py
git commit -m "fix: build_index.py uses upsert() instead of add() for existing product IDs

Found while planning the transaction-data-v2 upgrade: chromadb's
Collection.add() silently no-ops on an ID that already exists (doesn't
raise, doesn't update the document) -- confirmed directly against the
installed chromadb==1.5.9. Every product ID already exists in the
'products' collection from the last build, so re-running build_index.py
after regenerating katalog_produk.csv's terjual values would have left
every embedded document's terjual text stale forever. upsert() has the
same call signature and actually overwrites.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 7: Rewrite the 3 instructions to English + dynamic date range + language-drift mitigation + tool registration

**Files:**
- Modify: `query.py` (`ROOT_INSTRUCTION`, `KATEGORI_INSTRUCTION`, `PRODUK_INSTRUCTION`, `_build_produk_instruction`, `root_agent`/`kategori_specialist` construction, currently around lines 375-528 and 801-838)
- Create: `_verify_bilingual_instructions.py`

**Interfaces:**
- Produces: `_available_date_range_note() -> str`, `_build_root_instruction(context) -> str` (new, replaces static `ROOT_INSTRUCTION` as `root_agent`'s `instruction=`), `_build_kategori_instruction(context) -> str` (new, replaces static `KATEGORI_INSTRUCTION` as `kategori_specialist`'s `instruction=`), `_build_produk_instruction(context) -> str` (existing function, extended with date-range note).

- [ ] **Step 1: Write the failing verify script**

Create `_verify_bilingual_instructions.py`:

```python
"""Verifikasi manual: ketiga instruksi (root/kategori/produk) sekarang
Inggris, menyisipkan rentang tanggal data yang tersedia secara dinamis, dan
memuat aturan wajib (jawab Bahasa Indonesia + jangan terjemahkan
segmen/kategori/produk yang diekstrak). Juga cek tool baru sudah terdaftar
di specialist yang benar. Tidak butuh Ollama.
Jalankan: ./Scripts/python.exe _verify_bilingual_instructions.py
"""

import query


def main():
    for builder in (query._build_root_instruction, query._build_kategori_instruction, query._build_produk_instruction):
        text = builder(None)
        assert "Bahasa Indonesia" in text, f"FAIL: {builder.__name__} tidak menyebut aturan jawab Bahasa Indonesia"
        assert "NEVER translate" in text, f"FAIL: {builder.__name__} tidak menyebut aturan larangan terjemahan entitas"
        assert str(query._load_transactions()["transaction_time"].dt.date.min()) in text, (
            f"FAIL: {builder.__name__} tidak menyisipkan tanggal minimum data yang tersedia"
        )
    print("PASS: ketiga instruksi Inggris, menyebut aturan wajib, dan menyisipkan rentang tanggal dinamis")

    kategori_tools = {t.name for t in query.kategori_specialist.tools}
    produk_tools = {t.name for t in query.produk_specialist.tools}
    assert "get_trending_categories" in kategori_tools, f"FAIL: kategori_specialist.tools = {kategori_tools}"
    assert {"get_trending_products", "get_peak_hours"} <= produk_tools, f"FAIL: produk_specialist.tools = {produk_tools}"
    print("PASS: get_trending_categories terdaftar di kategori_specialist, get_trending_products+get_peak_hours di produk_specialist")

    assert callable(query.root_agent.instruction), "FAIL: root_agent.instruction harusnya InstructionProvider (callable), bukan string statis"
    assert callable(query.kategori_specialist.instruction), "FAIL: kategori_specialist.instruction harusnya InstructionProvider (callable)"
    print("PASS: root_agent dan kategori_specialist pakai InstructionProvider dinamis, bukan string statis")

    print("SEMUA CEK LULUS")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run it, confirm it fails**

Run: `./Scripts/python.exe _verify_bilingual_instructions.py`
Expected: `AttributeError: module 'query' has no attribute '_build_root_instruction'`.

- [ ] **Step 3: Implement**

Add this helper near `_build_produk_instruction`:

```python
def _available_date_range_note() -> str:
    df = _load_transactions()
    dmin = df["transaction_time"].dt.date.min()
    dmax = df["transaction_time"].dt.date.max()
    return f"{dmin} to {dmax}"
```

Replace the static `ROOT_INSTRUCTION = """..."""` block with an `_build_root_instruction` function:

```python
def _build_root_instruction(context) -> str:
    """InstructionProvider untuk root_agent -- isinya sama seperti
    ROOT_INSTRUCTION lama (string statis), tapi sekarang menyisipkan
    rentang tanggal data transaksi yang BENAR-BENAR tersedia secara
    dinamis, pola yang sama seperti _build_produk_instruction menyisipkan
    info katalog -- perlu dinamis karena larangan blanket "tidak ada data
    waktu" yang lama sudah tidak akurat, dan rentangnya harus tetap benar
    kalau transaction_dayN.csv baru ditambahkan nanti tanpa perlu ubah
    prompt (lihat 2026-09-06-transaction-data-v2-design.md bagian 3d)."""
    date_range = _available_date_range_note()
    return f"""You are the router for Alfagift's retail sales-analysis assistant.
Your job is NOT to answer questions yourself -- pick one or more specialists
below based on the question type, and call them with a `request` parameter
containing a clear, SELF-CONTAINED instruction: if the user's question refers
back to a previous turn (e.g. "from that", "that category", "the one just
mentioned"), YOU MUST replace that reference with the concrete value (explicit
product/category name, taken from the conversation history you can see) inside
the request you send -- specialists CANNOT see the conversation history, they
only see the request text you send them.

Available specialists:
1. kategori_specialist -- category-level questions in general: most/least
   sold categories, product variety/assortment per category, category-level
   trending. Has NO price tool at all.
2. produk_specialist -- product-level questions: product search, best/worst
   sellers, price ranges, cross-sell recommendations, product-level trending,
   and peak selling hours.

IMPORTANT about the word "kategori" (category): this word appears in TWO
different contexts -- (a) a PURE category-level question (kategori_specialist),
e.g. "which category sells the most", not mentioning any specific price or
product at all, vs (b) "kategori" used only as a SEGMENT QUALIFIER for a
PRICE or PRODUCT question, e.g. "harga median kategori Keripik & Kerupuk"
("median price for the Keripik & Kerupuk category"), "produk termahal di
kategori Minuman" ("most expensive product in the Minuman category") -- these
MUST go to produk_specialist, because only it has the price tool
(get_price_range) and product tools. Rule: if the question is about PRICE,
PRODUCT, or CROSS-SELL, it ALWAYS goes to produk_specialist -- regardless of
whether the word "kategori" is also used as a segment qualifier.

If a question needs more than one specialist (e.g. best-selling product AND
its price range), call ALL relevant specialists in the same turn, then merge
their results into one coherent answer.

Some questions are framed as creative/strategic requests (e.g. "make me promo
ideas from the 5 best-selling products", "suggest a sales strategy for
category X") but still DEPEND on concrete facts (which product is the best
seller, what price, which category). Framing it as "idea/recommendation/
strategy" is NOT a reason to skip the specialists -- if your answer will
mention a specific product/category name, sales number, or price, you MUST
call the relevant specialist first for that data, then build your idea on top
of it. Parts that are purely your own suggestion (e.g. a promo discount
percentage, marketing copy) may be added, but mark them clearly as a
suggestion -- never let the reader think they came from real sales data.

If you think ADDITIONAL data (e.g. cross-sell candidates) would strengthen the
answer, CALL the relevant specialist NOW in the same turn -- do NOT just
mention the tool/specialist name as a "next step" in your final answer. The
user cannot call tools themselves, so a sentence like "use
find_cross_sell_candidates..." is useless to them and leaks an internal
implementation detail -- if you are not calling it now, do not mention that
tool/specialist name in the final answer at all.

Data time coverage: the transaction data currently available covers
{date_range} (this range is computed live from the loaded data, so it always
reflects whatever transaction_dayN.csv files are currently loaded -- if more
daily files are added later, this range updates automatically). Time-themed
questions (e.g. "sales this week", "trending now", "recently", "what's hot
lately") CAN be answered now, through get_trending_products/
get_trending_categories (compares the two most recent dates in the data) or
the optional start_date/end_date filters on get_top_sellers/get_top_categories
-- call the relevant specialist for these instead of refusing. BUT if the
question's date/range falls OUTSIDE {date_range}, or uses a relative phrase
whose coverage is unclear given how little data exists (e.g. "last month" when
only a few days of data exist), do NOT call any specialist to make up an
answer -- say plainly that it can't be answered from the available data, and
state the actual available range. Per-customer questions (e.g. "which
customer buys X the most") are still always refused outright without calling
any specialist -- the data has no user_id column at all, regardless of the
time range.

CRITICAL: when extracting a product/category/segment value from the user's
Indonesian-language question to pass to a specialist, copy it EXACTLY as
written (or exactly as it appears in prior tool results) -- NEVER translate
it into English, even though these instructions are written in English. Tool
arguments are matched against Indonesian catalog/category text via literal
substring search; an English translation of the value will silently match
nothing.

IMPORTANT: when you combine answers from more than one specialist, quote
numbers EXACTLY as each specialist returned them -- do not recompute or
estimate from memory.

Always respond in Bahasa Indonesia (Indonesian), regardless of the language
of these instructions."""
```

Replace the static `KATEGORI_INSTRUCTION = """..."""` block with:

```python
def _build_kategori_instruction(context) -> str:
    """InstructionProvider untuk kategori_specialist -- alasan sama seperti
    _build_root_instruction (rentang tanggal dinamis)."""
    date_range = _available_date_range_note()
    return f"""You are a retail category-analysis specialist for Alfagift.
You receive a request that is ALREADY self-contained (no other conversation
history needed) from the router -- answer directly based on that request.

Your job, pick the tool that matches the request type:
1. Categories in general (not a specific segment/product) -- "which category
   sells the most" / "which category sells the least" -> get_top_categories
   (terendah=True for the least-selling).
2. A specific date or date range -> pass start_date/end_date (YYYY-MM-DD) to
   get_top_categories; leave both empty ("") to use every available date
   (the default -- not a special case).
3. Categories with the least/most product variety in the catalog (assortment
   gap) -> get_category_assortment.
4. Trending categories (biggest growth between the two most recent dates in
   the data, e.g. "which category is trending/picking up now") ->
   get_trending_categories.

Data time coverage: {date_range} (computed live from the loaded data, updates
automatically as more daily files are added). If a time-themed request's
date/range falls OUTSIDE this range, or uses a relative phrase whose coverage
is unclear given how little data exists, do NOT call any tool to make up an
answer -- say plainly it can't be answered from the available data, and state
the actual available range. Per-customer requests are still always refused
outright without calling any tool -- the data has no user_id column at all.

CRITICAL: when extracting a category/segment value from the request to pass
as a tool argument, copy it EXACTLY as written -- NEVER translate it into
English, even though these instructions are written in English. Tool
arguments are matched against Indonesian catalog/category text via literal
substring search; an English translation of the value will silently match
nothing.

IMPORTANT: quote numbers (units sold, product counts) EXACTLY as returned by
the tool -- do not recompute or estimate from memory.

Always respond in Bahasa Indonesia (Indonesian), regardless of the language
of these instructions."""
```

Replace the static `PRODUK_INSTRUCTION = """..."""` block (content only -- it stays a module-level string, `_build_produk_instruction` already exists and just gets extended below):

```python
PRODUK_INSTRUCTION = """You are a retail product-analysis specialist for Alfagift.
You receive a request that is ALREADY self-contained (no other conversation
history needed) from the router -- answer directly based on that request.

Your job, pick the tool that matches the request type:
1. Best-selling products in a segment -> get_top_sellers (the result already
   includes each product's category, no extra tool needed for that). Pass
   start_date/end_date (YYYY-MM-DD) if the request names a specific date or
   date range; leave both empty ("") to use every available date (the
   default -- not a special case).
2. Least-selling / never-sold products, candidates for discontinuation or a
   price cut -> get_worst_sellers.
3. Price range (cheapest/most expensive/median) for a segment or category ->
   get_price_range (leave segment empty for the whole catalog's price range).
4. Trending products (biggest growth between the two most recent dates in the
   data, e.g. "which product is trending/picking up now") ->
   get_trending_products.
5. Busiest selling hours (for staffing/promo-timing questions, e.g. "what
   time of day sells the most") -> get_peak_hours.
6. If the request mentions "products similar/comparable to [X]" -- WHATEVER
   qualifier is attached (e.g. "with low sales", "that sell better", "for
   cross-selling") -- use find_cross_sell_candidates with product_name=X. If
   X is not yet a concrete product name (e.g. the request still names a
   category, not a specific product), call get_top_sellers first to get one
   concrete product name, THEN immediately call find_cross_sell_candidates
   with that name in the same turn -- do not stop at the first tool and tell
   the user to look it up themselves. NEVER claim a product is suitable for
   cross-selling without actually calling this tool to prove it. Cross-sell
   candidates MUST be OTHER products with lower sales than the reference
   product (exactly what this tool returns) -- NEVER suggest the reference
   product itself as its own cross-sell candidate. If this tool genuinely
   returns no candidates, say so plainly ("no suitable cross-sell candidates
   found") -- never make one up or substitute the reference product itself.
7. Use search_catalog when you need extra detail about a specific product.

If a request asks for SEVERAL things at once (e.g. best-seller AND its price
range AND a cross-sell recommendation), make sure your final answer actually
includes the result of EVERY tool you called -- never silently drop one of
the requested parts.

If a time-themed request's date/range falls OUTSIDE the available range (see
the current data time coverage noted below), or uses a relative phrase whose
coverage is unclear given how little data exists, do NOT call any tool to
make up an answer -- say plainly it can't be answered from the available
data, and state the actual available range. Per-customer requests are still
always refused outright without calling any tool -- the data has no user_id
column at all.

If you think ADDITIONAL data (e.g. cross-sell candidates) would strengthen
the answer, CALL the relevant tool NOW in the same turn -- do NOT just
mention the tool name as a "next step" in the final answer. The router that
forwards your answer to the user cannot call tools itself, so a sentence
like "use find_cross_sell_candidates..." is useless and leaks an internal
implementation detail.

CRITICAL: when extracting a product/category/segment value from the request
to pass as a tool argument, copy it EXACTLY as written -- NEVER translate it
into English, even though these instructions are written in English. Tool
arguments are matched against Indonesian catalog/category text via literal
substring search; an English translation of the value will silently match
nothing.

IMPORTANT: quote numbers (prices, units sold) EXACTLY as returned by the
tool -- do not recompute or estimate from memory. If you need a number that
isn't in any tool result yet, call the appropriate tool first -- never make
one up."""
```

Extend `_build_produk_instruction` to also append the date-range note:

```python
def _build_produk_instruction(context) -> str:
    """InstructionProvider untuk produk_specialist -- sama seperti sebelumnya
    (menyisipkan info katalog terkini), sekarang DITAMBAH rentang tanggal
    data transaksi yang tersedia (lihat _build_root_instruction untuk
    alasan lengkap kenapa ini perlu dinamis)."""
    katalog_mtime = datetime.fromtimestamp(os.path.getmtime(KATALOG_CSV)).strftime("%Y-%m-%d")
    date_range = _available_date_range_note()
    return (
        f"{PRODUK_INSTRUCTION}\n\n"
        f"Current catalog info: {len(katalog_df)} products registered, "
        f"{collection.count()} of them indexed for semantic search "
        f"(search_catalog/find_cross_sell_candidates), catalog data last updated {katalog_mtime}. "
        f"Current transaction data time coverage: {date_range} (computed live, updates "
        f"automatically as more daily files are added)."
    )
```

Update the `kategori_specialist = Agent(...)` construction: change `instruction=KATEGORI_INSTRUCTION` to `instruction=_build_kategori_instruction`, and add `get_trending_categories` to its `tools=[...]` list.

Update the `root_agent = Agent(...)` construction: change `instruction=ROOT_INSTRUCTION` to `instruction=_build_root_instruction`.

Update the `produk_specialist = Agent(...)` construction: add `get_trending_products` and `get_peak_hours` to its `tools=[...]` list.

- [ ] **Step 4: Run it, confirm it passes**

Run: `./Scripts/python.exe _verify_bilingual_instructions.py`
Expected: `SEMUA CEK LULUS`, all 3 `PASS:` lines.

- [ ] **Step 5: Commit**

```bash
git add query.py _verify_bilingual_instructions.py
git commit -m "feat: rewrite ROOT/KATEGORI/PRODUK instructions to English, inject live date range

Instructions (and tool docstrings from earlier tasks) move to English --
this project's model follows English instructions more reliably while
still understanding Indonesian input/output fine, since the instruction
language and the conversation language are read together, not as a
translation pipeline. Every instruction still ends with an explicit
'respond in Bahasa Indonesia' rule, and adds a new explicit rule against
translating extracted product/category/segment values (they're matched
against Indonesian data via literal substring search). ROOT_INSTRUCTION
and KATEGORI_INSTRUCTION become InstructionProvider callables (like
PRODUK_INSTRUCTION already was) so the available transaction date range
can be injected live instead of hardcoded -- the old blanket 'no time
data' refusal is no longer true. Also registers get_trending_categories
on kategori_specialist and get_trending_products/get_peak_hours on
produk_specialist.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 8: Translate `verify_and_revise`'s internal grading prompt to English

**Files:**
- Modify: `query.py` (`_verify_and_revise_impl`, currently around lines 685-789)

**Interfaces:** None (internal prompt text only, function signature unchanged).

- [ ] **Step 1: Confirm current behavior with a quick manual check (no code change yet)**

Run: `./Scripts/python.exe -c "import query; print(query._verify_and_revise_impl('Ada 5 produk terlaris.', 'tool data with 5 mentioned'))"`
Expected: prints back the same draft unchanged (fast path, no mismatch -- this exercises the surrounding logic without touching the prompt, confirms the baseline before the prompt text changes).

- [ ] **Step 2: Implement**

In `_verify_and_revise_impl`, replace the `prompt = (...)` assignment:
```python
    prompt = (
        "Kamu mengecek draft jawaban asisten penjualan terhadap data mentah dari tool "
        "yang benar-benar dipanggil di giliran ini. Kalau draft SUDAH akurat (semua angka "
        "dan klaim didukung data tool), ulangi draft itu PERSIS apa adanya, jangan diubah "
        "sedikit pun. Kalau ADA angka yang tidak cocok dengan data tool, atau klaim yang "
        "tidak didukung data tool (mis. menyebut suatu produk cocok untuk cross-sell "
        "padahal tool cross-sell tidak dipanggil/tidak mengembalikan produk itu), revisi "
        "jawabannya supaya akurat, HANYA berdasarkan data tool ini. Jangan tambahkan "
        "penjelasan soal proses pengecekan ini ke jawaban akhir -- keluarkan LANGSUNG "
        "jawaban akhirnya saja (yang asli atau yang sudah direvisi).\n\n"
        f"DATA TOOL (giliran ini):\n{tool_outputs[:3000]}\n\nDRAFT JAWABAN:\n{draft_answer[:2000]}"
    )
```
with:
```python
    prompt = (
        "You are checking a sales assistant's draft answer against the raw tool data "
        "that was actually called this turn. If the draft is ALREADY accurate (every "
        "number and claim is supported by the tool data), repeat the draft EXACTLY as "
        "is, do not change anything. If there IS a number that doesn't match the tool "
        "data, or a claim not supported by the tool data (e.g. claiming a product is "
        "suitable for cross-selling when the cross-sell tool wasn't called or didn't "
        "return that product), revise the answer to be accurate, based ONLY on this "
        "tool data. Do not add any explanation about this checking process to the "
        "final answer -- output ONLY the final answer itself (the original or the "
        "revised one). The final answer MUST be written in Bahasa Indonesia "
        "(Indonesian), regardless of the language of this instruction.\n\n"
        f"TOOL DATA (this turn):\n{tool_outputs[:3000]}\n\nDRAFT ANSWER:\n{draft_answer[:2000]}"
    )
```

- [ ] **Step 3: Re-run the same manual check, confirm the fast path is unaffected**

Run: `./Scripts/python.exe -c "import query; print(query._verify_and_revise_impl('Ada 5 produk terlaris.', 'tool data with 5 mentioned'))"`
Expected: identical output to Step 1 -- the fast (no-mismatch) path never reaches the prompt string at all, so this confirms the surrounding number-matching logic is untouched by the text-only prompt change.

- [ ] **Step 4: Manual live check (needs Ollama running -- do this once during Task 10's full regression, not required standalone)**

Trigger the actual escalation path: `./Scripts/python.exe -c "import asyncio, query; print(asyncio.run(query.verify_and_revise('Produk X terjual 999x.', 'Produk Y terjual 5x')))"`. Confirm the printed answer is still in Indonesian (the prompt's new "must be Bahasa Indonesia" rule is doing its job) and that it no longer contains the number 999 unmodified.

- [ ] **Step 5: Commit**

```bash
git add query.py
git commit -m "refactor: translate verify_and_revise's internal grading prompt to English

Same language-consistency rationale as Task 7. This prompt is special:
its output can become the actual final answer sent to the user, so it
gets an explicit 'the final answer MUST be written in Bahasa Indonesia'
rule, not just the general instruction-language note.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 9: Regenerate `katalog_produk.csv` and rebuild the ChromaDB index (real data operation)

**This task runs the already-modified scripts against the real ~3-million-row dataset. It is NOT a code change. `build_index.py` alone takes tens of minutes (per-row embedding calls, ~4 rows/sec observed previously). Confirm with the user before starting Step 3.**

**Files:** none modified -- this task executes `aggregate_sales.py` and `build_index.py`.

- [ ] **Step 1: Back up the current catalog before overwriting it**

```bash
cp D:/agentic/chroma_db/katalog_produk.csv D:/agentic/chroma_db/katalog_produk.csv.bak-pre-v2
```

- [ ] **Step 2: Run `aggregate_sales.py` (fast, roughly a minute or two for ~3M rows via `csv.DictReader`)**

Run: `./Scripts/python.exe aggregate_sales.py`
Expected: prints file count (3), total net units, matched-product count, ends with `Selesai: kolom 'terjual' (net qty) ditambahkan ke ...`.

- [ ] **Step 3: Spot-check the regenerated catalog before spending tens of minutes rebuilding the index**

```bash
./Scripts/python.exe -c "
import pandas as pd
old = pd.read_csv('D:/agentic/chroma_db/katalog_produk.csv.bak-pre-v2')
new = pd.read_csv('D:/agentic/chroma_db/katalog_produk.csv')
print('rows old/new:', len(old), len(new))
print('terjual sum old/new:', old[\"terjual\"].sum(), new[\"terjual\"].sum())
print(new[['nama','terjual']].sort_values('terjual', ascending=False).head(5))
"
```
Expected: same row count as before, a plausible (nonzero, not wildly different in scale) total, and the top-5 products by `terjual` look like real bestsellers, not garbage.

- [ ] **Step 4: Run `build_index.py` (slow -- run in background, tens of minutes)**

Run: `./Scripts/python.exe build_index.py` (as a background process; it prints `[i/total] ... estimasi sisa X menit` progress lines).
Expected: ends with `Selesai: <N> produk di-index ke ChromaDB.`, no exceptions.

- [ ] **Step 5: Spot-check the rebuilt index reflects the new terjual numbers**

```bash
./Scripts/python.exe -c "
import chromadb
c = chromadb.PersistentClient(path='D:/agentic/chroma_db')
col = c.get_collection('products')
print(col.count())
sample = col.get(limit=3, include=['documents'])
for doc in sample['documents']:
    print(doc)
"
```
Expected: count matches the catalog row count, and the printed embedded document text's `terjual: Nx` matches the regenerated `katalog_produk.csv` values (confirms Task 6's `upsert()` fix actually took effect, not stale `add()`-era text).

- [ ] **Step 6: Commit** (the regenerated `katalog_produk.csv` -- the ChromaDB files themselves are a persistent local DB, not typically tracked in git; check `.gitignore` before adding)

```bash
git status
git add chroma_db/katalog_produk.csv
git commit -m "chore: regenerate katalog_produk.csv with net-qty terjual from transaction_day*.csv

Re-ran aggregate_sales.py (net item_qty sum) and build_index.py
(upsert-based, see Task 6) against the new transaction_day1/2/3.csv
data. Backup of the pre-v2 catalog kept at katalog_produk.csv.bak-pre-v2.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 10: Test suite additions + full regression run

**Files:**
- Modify: `test_agent_cases.py` (module docstring, `CASES` list)

**Interfaces:** None (test data only).

- [ ] **Step 1: Update the module docstring's opening line**

Replace:
```python
"""
100 kasus uji untuk root_agent di query.py -- mencakup ketujuh tool
(search_catalog, get_top_sellers, find_cross_sell_candidates, get_top_categories,
get_worst_sellers, get_category_assortment, get_price_range) plus kasus edge:
```
with:
```python
"""
Kasus uji untuk root_agent di query.py -- mencakup kesepuluh tool
(search_catalog, get_top_sellers, find_cross_sell_candidates, get_top_categories,
get_worst_sellers, get_category_assortment, get_price_range, get_trending_products,
get_trending_categories, get_peak_hours) plus kasus edge:
```

- [ ] **Step 2: Move `EDW05` out of the time-refusal group -- it's now answerable via trending**

In the `--- Edge cases: refusal bertema waktu ... (5) ---` group, remove:
```python
    ("EDW05", "edge_waktu", ["kategori apa yang lagi tren sekarang"], [], "harus menolak, tidak ada kolom timestamp"),
```
(the other 4 EDW cases stay -- "minggu ini"/"bulan lalu"/"hari ini"/"tahun ini vs tahun lalu" are still unresolvable relative phrases against a 3-day dataset with no "today" anchor).

- [ ] **Step 3: Add new case groups** (insert after the `get_price_range (10)` group, before the edge-case groups)

```python
    # --- get_trending_products / get_trending_categories (naik daun) (5) ---
    ("TR01", "get_trending_categories", ["kategori apa yang lagi tren sekarang"], ["get_trending_categories"], "dipindah dari EDW05 -- sekarang bisa dijawab lewat trending, bukan ditolak"),
    ("TR02", "get_trending_categories", ["kategori mana yang lagi naik daun"], ["get_trending_categories"], ""),
    ("TR03", "get_trending_products", ["produk sabun mandi apa yang lagi trending"], ["get_trending_products"], ""),
    ("TR04", "get_trending_products", ["produk apa yang lagi hits di kategori minuman"], ["get_trending_products"], ""),
    ("TR05", "get_trending_products", ["produk apa yang belakangan ini penjualannya naik"], ["get_trending_products"], ""),

    # --- get_peak_hours (jam ramai) (3) ---
    ("PH01", "get_peak_hours", ["jam berapa penjualan paling ramai"], ["get_peak_hours"], ""),
    ("PH02", "get_peak_hours", ["jam berapa kategori minuman paling laris terjual"], ["get_peak_hours"], ""),
    ("PH03", "get_peak_hours", ["waktu paling ramai untuk sabun mandi jam berapa"], ["get_peak_hours"], ""),

    # --- Filter tanggal spesifik (start_date/end_date) (3) ---
    ("DT01", "date_filter", ["penjualan tanggal 2 Agustus 2026 kategori apa yang paling laris"], ["get_top_categories"], "tanggal ada di data (2026-08-02), harus terjawab dengan filter tanggal"),
    ("DT02", "date_filter", ["produk terlaris kategori Minuman tanggal 1 Agustus 2026"], ["get_top_sellers"], "tanggal ada di data (2026-08-01)"),
    ("DT03", "date_filter", ["penjualan tanggal 25 Desember 2026 gimana"], ["get_top_categories"], "tanggal DI LUAR data yang tersedia -- tool tetap terpanggil tapi harus mengembalikan pesan jujur rentang tidak tersedia, bukan mengarang; baca jawabannya, jangan cuma cek nama tool"),

    # --- Regresi drift-bahasa: segmen dengan padanan Inggris jelas (3) ---
    ("LD01", "language_drift", ["produk terlaris kategori minuman"], ["get_top_sellers"], "'minuman'='drink' -- kalau argumen tool diam-diam diterjemahkan ke Inggris, hasilnya kosong; baca jawabannya, harus berisi produk nyata bukan 'tidak ada produk yang cocok'"),
    ("LD02", "language_drift", ["kategori makanan penjualannya berapa"], ["get_top_categories"], "'makanan'='food' -- cek jawaban bukan penolakan kosong"),
    ("LD03", "language_drift", ["cari susu cair rendah lemak yang lagi trending"], ["get_trending_products"], "'susu'='milk' -- cek jawaban bukan penolakan kosong"),
```

- [ ] **Step 4: Run the full regression suite**

Run: `./Scripts/python.exe test_agent_cases.py` (25-40 min, more cases than before -- run in background)
Expected: `SELESAI` printed, `test_report.jsonl` has one line per case (now ~116 cases instead of 104).

- [ ] **Step 5: Review the report**

```bash
./Scripts/python.exe -c "
import json
mismatches = []
for line in open('test_report.jsonl', encoding='utf-8'):
    r = json.loads(line)
    if r['had_error'] or sorted(r['actual_tools']) != sorted(r['expected_tools']):
        mismatches.append((r['case_id'], r['expected_tools'], r['actual_tools'], r['had_error']))
print(f'{len(mismatches)} kasus tool-mismatch/error dari total baris')
for m in mismatches:
    print(m)
"
```
For every mismatch, read that case's `answer` field in `test_report.jsonl` manually before deciding it's a real regression (per this project's existing convention -- a different tool name isn't automatically wrong, read the answer first). Pay special attention to `LD01`/`LD02`/`LD03` (must NOT show an empty/no-match answer -- that's the language-drift failure mode Task 7's mitigation targets) and `DT03` (must show the honest out-of-range message, not a fabricated answer).

- [ ] **Step 6: Commit**

```bash
git add test_agent_cases.py test_report.jsonl
git commit -m "test: add cases for trending, peak hours, date filters, and language-drift regression

New case groups: TR01-05 (trending, includes EDW05 moved here since
'kategori apa yang lagi tren sekarang' is now answerable instead of
refused), PH01-03 (peak hours), DT01-03 (date-range filter, including
one out-of-range date), LD01-03 (regression guard for the English-
instruction language-drift risk documented in the v2 design doc section
4 -- segments with obvious English cognates must still resolve to real
products, not an empty/translated-argument match failure).

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage:** section 1 (context/decisions) -> reflected in Global Constraints and Task 1/2 rationale. Section 2 (data layer) -> Task 1 (loading/parsing), Task 2 (net qty), Task 5 (aggregate_sales.py), Task 9 (regenerate + rebuild). Section 3 (new tools) -> Task 2 (date filters, 3a), Task 4 (trending, 3b), Task 3 (peak hours, 3c), Task 7 (instruction rewrite for refusal rules, 3d), 3e (unchanged tools) needed no task, confirmed by omission. Section 4 (bilingual prompts + mitigation) -> Task 7 (three instructions) + Task 8 (verify_and_revise prompt). Section 5 (testing) -> Task 10. Section 6 (docs) -> already completed in the prior conversation turn, before this plan was written (`PANDUAN_PENGGUNAAN.md`, `rag-setup-windows.md`, `infrastructure_agentic.md` all updated; `query.py`'s own module docstring was deliberately deferred to land together with this plan's code changes -- fold that docstring update into Task 1 or Task 7's commit when executing, whichever task an executor reaches first that touches the top of the file).

**Placeholder scan:** no TBD/TODO; every step has runnable code or an exact command.

**Type consistency:** `_get_top_sellers_impl(segment, top_n, start_date, end_date)` and `get_top_sellers(segment, top_n=5, start_date="", end_date="")` match across Task 2's definition and Task 7's instruction text ("pass start_date/end_date"). `_latest_two_dates`/`_trending_impl` defined once in Task 4, used by both `_get_trending_products_impl`/`_get_trending_categories_impl` in the same task -- no cross-task signature drift. `TRANSACTION_CSV_GLOB` used consistently in both `query.py` (Task 1) and `aggregate_sales.py` (Task 5) as two independent module-level constants (matching the existing pattern where these two files never share a config module).

**One open note for the executor:** Task 1's module-docstring update (mentioned in the design spec section 6) isn't a separate task above -- fold it into Task 1's commit (update the paragraph at the top of `query.py` describing the data sources, replacing the "no timestamp, can't filter by time" claim) since Task 1 is the first task to touch that broken assumption.
