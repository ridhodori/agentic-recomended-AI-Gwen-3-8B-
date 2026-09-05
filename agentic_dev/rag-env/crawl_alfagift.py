"""
Crawl seluruh katalog produk alfagift.id (semua kategori) dan tulis ke katalog_produk.csv.

Alfagift adalah Nuxt.js SPA yang memuat data via API JSON di
webcommerce-gw.alfagift.id, dan API itu menolak request langsung (401) tanpa
sesi browser yang sah. Script ini memakai Playwright (headless Chromium) untuk
membuka halaman utama (supaya sesi/token terpasang), lalu memanggil
window.$nuxt.$axios dari dalam halaman itu untuk mengambil data secara
terautentikasi -- sesi ini berlaku untuk kategori manapun, tidak terikat ke
satu kategori spesifik.

Endpoint yang dipakai (ditemukan lewat inspeksi network saat browsing manual):
  - GET /v2/categories
    -> pohon kategori (level 0 dengan subCategories level 1). Kategori level 1
       dipakai sebagai unit crawl (leaf category).
  - GET /v2/products/category/{categoryId}?sortDirection=asc&start={page}&limit={limit}
    -> daftar produk per kategori. "start" adalah nomor halaman (0-indexed),
       BUKAN offset item.
  - GET /v2/products/{productId}
    -> detail produk, termasuk shortDescription/longDescription yang tidak
       ada di endpoint listing.

Karena ini crawl seluruh katalog (bisa ribuan produk, beberapa jam), script
ini bisa dilanjutkan (resume): kalau OUTPUT_CSV sudah ada, id yang sudah
tercatat tidak akan di-fetch ulang -- aman untuk di-Ctrl+C dan dijalankan lagi.
"""

import csv
import html
import os
import re
import time

from playwright.sync_api import sync_playwright

API_BASE = "https://webcommerce-gw.alfagift.id"
HOMEPAGE_URL = "https://alfagift.id/"
PAGE_SIZE = 60
LISTING_FETCH_DELAY_SEC = 0.2
DETAIL_FETCH_DELAY_SEC = 0.4
OUTPUT_CSV = "D:/agentic/chroma_db/katalog_produk.csv"
CSV_HEADER = ["id", "nama", "kategori", "harga", "deskripsi", "terjual"]
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

TAG_RE = re.compile(r"<[^>]+>")


def clean_description(product_detail):
    short = product_detail.get("shortDescription") or []
    short = [s.strip() for s in short if s and s.strip()]
    if short:
        return ". ".join(short)

    long_desc = product_detail.get("longDescription") or product_detail.get("description") or ""
    text = html.unescape(TAG_RE.sub(" ", long_desc))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:600]


def fetch_json(page, url):
    return page.evaluate(
        """async (url) => {
            const resp = await window.$nuxt.$axios.get(url);
            return resp.data;
        }""",
        url,
    )


def get_leaf_categories(page):
    data = fetch_json(page, f"{API_BASE}/v2/categories")
    leaves = []
    for top in data.get("categories", []):
        subs = top.get("subCategories") or []
        if not subs:
            leaves.append({"id": top["categoryId"], "name": top["categoryName"]})
            continue
        for sub in subs:
            leaves.append({"id": sub["categoryId"], "name": sub["categoryName"]})
    return leaves


def load_seen_ids(csv_path):
    if not os.path.exists(csv_path):
        return set()
    with open(csv_path, encoding="utf-8") as f:
        return {row["id"] for row in csv.DictReader(f)}


def main():
    os.makedirs(os.path.dirname(OUTPUT_CSV), exist_ok=True)

    seen_ids = load_seen_ids(OUTPUT_CSV)
    if seen_ids:
        print(f"Melanjutkan crawl: {len(seen_ids)} produk sudah tercatat sebelumnya, akan dilewati.")
    file_mode = "a" if seen_ids else "w"

    start_time = time.time()
    total_written = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent=USER_AGENT)

        print("Membuka halaman utama untuk memulai sesi...")
        page.goto(HOMEPAGE_URL, wait_until="networkidle", timeout=60000)

        categories = get_leaf_categories(page)
        print(f"Ditemukan {len(categories)} kategori untuk di-crawl.\n")

        with open(OUTPUT_CSV, file_mode, newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if file_mode == "w":
                writer.writerow(CSV_HEADER)

            for cat_idx, cat in enumerate(categories, start=1):
                print(f"[{cat_idx}/{len(categories)}] Kategori: {cat['name']}")

                page_num = 0
                cat_products = []
                while True:
                    url = (
                        f"{API_BASE}/v2/products/category/{cat['id']}"
                        f"?sortDirection=asc&start={page_num}&limit={PAGE_SIZE}"
                    )
                    try:
                        data = fetch_json(page, url)
                    except Exception as e:
                        print(f"  gagal ambil listing halaman {page_num}: {e}")
                        break

                    products = data.get("products", [])
                    if not products:
                        break
                    cat_products.extend(products)

                    page_num += 1
                    if page_num >= data.get("totalPage", page_num):
                        break
                    time.sleep(LISTING_FETCH_DELAY_SEC)

                new_products = [p for p in cat_products if p.get("productId") not in seen_ids]
                print(f"  {len(cat_products)} produk ditemukan, {len(new_products)} baru")

                for prod in new_products:
                    pid = prod["productId"]
                    try:
                        detail_data = fetch_json(page, f"{API_BASE}/v2/products/{pid}")
                        deskripsi = clean_description(detail_data["productDetail"])
                    except Exception as e:
                        print(f"    gagal ambil detail produk {pid}: {e}")
                        deskripsi = ""

                    kategori = (
                        prod.get("categoryNameLvl2")
                        or prod.get("categoryNameLvl1")
                        or prod.get("categoryNameLvl0")
                        or cat["name"]
                    )
                    harga = prod.get("finalPrice", prod.get("basePrice", ""))

                    # terjual diisi 0 di sini; nilai sebenarnya di-backfill oleh aggregate_sales.py
                    writer.writerow([pid, prod["productName"], kategori, harga, deskripsi, 0])
                    f.flush()
                    seen_ids.add(pid)
                    total_written += 1

                    time.sleep(DETAIL_FETCH_DELAY_SEC)

                elapsed_min = (time.time() - start_time) / 60
                print(f"  selesai kategori ini. total baru ditulis sejauh ini: {total_written} ({elapsed_min:.1f} menit)\n")

        browser.close()

    elapsed_min = (time.time() - start_time) / 60
    print(f"Selesai: {total_written} produk baru ditulis ke {OUTPUT_CSV} ({elapsed_min:.1f} menit)")


if __name__ == "__main__":
    main()
