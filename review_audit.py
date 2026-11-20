"""Snackible review audit: scrape products + existing Judge.me reviews, analyse, write Excel.
Only retrieved/computed data. Respects robots.txt (allowed), 1.5-2s delay, max 3 retries."""
import re, time, random, datetime as dt, collections, statistics
import requests
from bs4 import BeautifulSoup
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.utils import get_column_letter

SITE = "https://snackible.com"
SHOP = "snackible.myshopify.com"
WEEKLY_TARGET = 5          # assumption: target reviews per product per week
UA = {"User-Agent": "SnackibleReviewAudit/1.0 (internal marketing audit; contact: marketing)"}
OUT = "snackible_review_audit.xlsx"
qa = []                    # (type, item, detail)
S = requests.Session(); S.headers.update(UA)


def get(url, **kw):
    err = ""
    for i in range(3):
        try:
            time.sleep(random.uniform(1.5, 2.0))
            r = S.get(url, timeout=30, **kw)
            if r.status_code == 200:
                return r
            if r.status_code in (404, 410):
                break
        except requests.RequestException as e:
            err = str(e)
        time.sleep(2 ** i)
    qa.append(("failed_url", url, f"after 3 tries {err}".strip()))
    return None


# ---------- STEP 1 discovery ----------
def discover():
    prods = {}
    page = 1
    while True:
        r = get(f"{SITE}/products.json?limit=250&page={page}")
        if not r or not r.json()["products"]:
            break
        for p in r.json()["products"]:
            prods[p["handle"]] = {"id": p["id"], "handle": p["handle"], "title": p["title"],
                                  "type": p.get("product_type", ""), "vendor": p.get("vendor", ""),
                                  "tags": ", ".join(p.get("tags", [])) if isinstance(p.get("tags"), list) else p.get("tags", ""),
                                  "price": min(float(v["price"]) for v in p["variants"]),
                                  "available": any(v["available"] for v in p["variants"]),
                                  "url": f"{SITE}/products/{p['handle']}", "src": {"json"}}
        page += 1
    json_n = len(prods)
    sm = set()
    r = get(f"{SITE}/sitemap.xml")
    if r:
        for loc in re.findall(r"<loc>([^<]+sitemap_products[^<]+)</loc>", r.text):
            rr = get(loc.replace("&amp;", "&"))
            if rr:
                sm |= set(re.findall(r"<loc>https://snackible\.com/products/([^<]+)</loc>", rr.text))
    for h in sm:
        if h in prods:
            prods[h]["src"].add("sitemap")
        else:
            prods[h] = {"id": None, "handle": h, "title": "", "type": "", "vendor": "", "tags": "", "price": None,
                        "available": None, "url": f"{SITE}/products/{h}", "src": {"sitemap"}}
            qa.append(("discovery", h, "in sitemap but not in products.json"))
    for h, p in prods.items():
        if "json" in p["src"] and h not in sm:
            qa.append(("discovery", h, "in products.json but not in sitemap"))
    print(f"Discovered {len(prods)} products (json={json_n}, sitemap={len(sm)})", flush=True)
    return prods


# ---------- STEP 2 reviews ----------
def parse_reviews(html, p):
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for rv in soup.select(".jdgm-rev"):
        score = rv.select_one(".jdgm-rev__rating")
        ts = rv.select_one(".jdgm-rev__timestamp")
        out.append({
            "review_id": rv.get("data-review-id"), "product_id": p["id"], "handle": p["handle"],
            "product": p["title"],
            "rating": int(score["data-score"]) if score and score.get("data-score") else None,
            "date": (ts.get("datetime") or "")[:10] if ts else "",
            "reviewer": (rv.select_one(".jdgm-rev__author") or {}).get_text(strip=True) if rv.select_one(".jdgm-rev__author") else "",
            "verified_buyer": rv.get("data-verified-buyer") == "true",
            "title": (rv.select_one(".jdgm-rev__title").get_text(" ", strip=True) if rv.select_one(".jdgm-rev__title") else ""),
            "body": (rv.select_one(".jdgm-rev__body").get_text(" ", strip=True) if rv.select_one(".jdgm-rev__body") else ""),
            "photos": len(rv.select(".jdgm-rev__pic-link, .jdgm-rev__pics img")),
            "thumbs_up": int(rv.get("data-thumb-up-count") or 0),
            "has_reply": bool(rv.select_one(".jdgm-rev__reply")),
        })
    return out


def scrape_reviews(p):
    allr, page = [], 1
    while page <= 60:
        r = get("https://judge.me/reviews/reviews_for_widget", params={
            "url": SHOP, "shop_domain": SHOP, "platform": "shopify", "page": page,
            "per_page": 20, "product_id": p["id"]})
        if not r:
            break
        try:
            rows = parse_reviews(r.json().get("html", ""), p)
        except ValueError:
            qa.append(("parse_error", p["handle"], f"page {page}")); break
        if not rows:
            break
        allr += rows
        if len(rows) < 20:
            break
        page += 1
    return allr


def displayed_count(p):
    """Review count/avg shown on the product page's Judge.me badge, for cross-checking."""
    r = get(p["url"])
    if not r:
        return None, None
    a = re.search(r"data-average-rating=['\"]([\d.]+)['\"]", r.text)
    n = re.search(r"data-number-of-reviews=['\"](\d+)['\"]", r.text)
    if not (a and n):
        return None, None
    return float(a.group(1)), int(n.group(1))


# ---------- analysis ----------
STOP = set("""a an the and or but if so of to in on at by for with from as is are was were be been it its this that these those
i me my we our you your they them their he she his her not no very really also just too than then there here have has had do does
did will would can could get got one two all any some more most much many lot lots am im ive iv""".split())
THEMES = {
    "Taste/flavour": r"tast|flavou?r|delicious|yumm|tasty",
    "Masala": r"masala|spic",
    "Crunch/texture": r"crunch|crisp|crispy|texture|soft|chewy",
    "Kids/family": r"kid|child|son\b|daughter|family|nani|dadi|grand",
    "Healthy/guilt-free": r"health|guilt|nutrit|clean",
    "Protein": r"protein",
    "Ingredients/oil": r"palm|oil|ingredient|natural|preservative|millet|ragi|jowar",
    "Sugar/sweetness": r"sugar|sweet|jaggery|dates",
    "Travel/on-the-go": r"travel|trip|carry|office|on the go",
    "Late-night/munching": r"late night|midnight|munch|evening",
    "Value/price": r"price|value|worth|costly|expensive|cheap",
    "Packaging/delivery": r"packag|deliver|shipping|arrived|damag|seal",
    "Repeat purchase": r"order again|reorder|repeat|buy again|will buy|regular",
    "Complaint": r"stale|bad|bland|salty|hard|stuck|disappoint|not good|worst|awful|broken|expired",
}
INTENT = {
    "Ingredient": r"ragi|millet|jowar|almond|chocolate|beetroot|zucchini|cheese|masala|oats|dates|palm|oil|sugar|jaggery|soya|peanut",
    "Health-benefit": r"health|protein|fibre|fiber|nutrit|guilt",
    "Diet/Claim": r"vegan|gluten|keto|sugar free|no sugar|palm",
    "Occasion": r"travel|trip|office|guest|night|kids|party|evening|gift",
    "Format": r"chip|cookie|chakli|namkeen|bar|bhakarwadi|puff|makhana|bundle|pack",
    "Product/Brand": r"snackible",
    "Price/Offer": r"price|value|worth|offer|cheap|expensive",
}


def tokens(text):
    return [w for w in re.findall(r"[a-z]+", text.lower()) if w not in STOP and len(w) > 2]


def ngrams(text, n):
    t = tokens(text)
    return [" ".join(t[i:i + n]) for i in range(len(t) - n + 1)]


def intent(k):
    for name, rx in INTENT.items():
        if re.search(rx, k):
            return name
    return "Other"


def main():
    prods = discover()
    reviews = []
    for i, (h, p) in enumerate(sorted(prods.items()), 1):
        if p["id"] is None:
            qa.append(("missing", h, "no product_id; reviews not fetched")); continue
        rv = scrape_reviews(p)
        avg_d, n_d = displayed_count(p)
        reviews += rv
        if n_d is not None and n_d != len(rv):
            qa.append(("count_mismatch", h, f"scraped {len(rv)} vs badge {n_d}"))
        p["displayed_n"], p["displayed_avg"] = n_d, avg_d
        print(f"[{i}/{len(prods)}] {h}: {len(rv)} reviews", flush=True)
    print(f"Scraped {len(reviews)} reviews total", flush=True)

    today = dt.date.today()
    by = collections.defaultdict(list)
    for r in reviews:
        by[r["handle"]].append(r)

    wb = Workbook()
    HDR = PatternFill("solid", fgColor="222222"); HF = Font(bold=True, color="FFFFFF")

    def sheet(name, headers, rows, widths=None, wrap_cols=()):
        ws = wb.create_sheet(name)
        ws.append(headers)
        for r in rows:
            ws.append(r)
        for c in ws[1]:
            c.fill, c.font = HDR, HF
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions
        for i, h in enumerate(headers, 1):
            w = (widths or {}).get(h, min(max(len(str(h)) + 2, 12), 40))
            ws.column_dimensions[get_column_letter(i)].width = w
            if h in wrap_cols:
                for row in ws.iter_rows(min_row=2, min_col=i, max_col=i):
                    for c in row:
                        c.alignment = Alignment(wrap_text=True, vertical="top")
        return ws

    # Products
    P = []
    for h, p in sorted(prods.items()):
        rv = by.get(h, [])
        ds = [dt.date.fromisoformat(r["date"]) for r in rv if r["date"]]
        rt = [r["rating"] for r in rv if r["rating"]]
        last7 = sum(1 for d in ds if (today - d).days <= 7)
        last30 = sum(1 for d in ds if (today - d).days <= 30)
        last90 = sum(1 for d in ds if (today - d).days <= 90)
        P.append([p["id"], h, p["title"], p["type"], p["tags"], p["price"], p["available"], p["url"],
                  len(rv), p.get("displayed_n"), round(statistics.mean(rt), 2) if rt else None,
                  sum(1 for x in rt if x == 5), sum(1 for x in rt if x == 4), sum(1 for x in rt if x <= 3),
                  round(sum(r["verified_buyer"] for r in rv) / len(rv) * 100, 1) if rv else None,
                  sum(1 for r in rv if r["photos"]), max(ds).isoformat() if ds else None,
                  (today - max(ds)).days if ds else None, last7, last30, last90, WEEKLY_TARGET, None])
    ws = sheet("Products", ["product_id", "handle", "title", "product_type", "tags", "price_min_inr", "in_stock", "url",
                            "reviews_scraped", "reviews_badge_shown", "avg_rating", "n_5star", "n_4star", "n_3star_or_less",
                            "verified_buyer_pct", "reviews_with_photos", "last_review_date", "days_since_last_review",
                            "reviews_last_7d", "reviews_last_30d", "reviews_last_90d", "weekly_target", "weekly_gap"], P)
    for r in range(2, ws.max_row + 1):
        ws[f"W{r}"] = f"=MAX(0,V{r}-S{r})"
        ws[f"H{r}"].hyperlink = ws[f"H{r}"].value
    # Reviews
    sheet("Reviews", ["review_id", "product_id", "handle", "product", "rating", "date", "reviewer", "verified_buyer",
                      "title", "body", "photos", "thumbs_up", "has_reply"],
          [[r[k] for k in ["review_id", "product_id", "handle", "product", "rating", "date", "reviewer", "verified_buyer",
                           "title", "body", "photos", "thumbs_up", "has_reply"]] for r in reviews],
          {"body": 70, "title": 30, "product": 30}, ("body", "title"))

    # Keywords by product / sitewide
    KP, site = [], collections.defaultdict(lambda: [0, set()])
    for h, rv in sorted(by.items()):
        text = " . ".join(f"{r['title']} {r['body']}" for r in rv)
        for n, label in ((1, "unigram"), (2, "bigram"), (3, "trigram")):
            c = collections.Counter(ngrams(text, n))
            tot = sum(c.values()) or 1
            for k, v in c.most_common(10):
                KP.append([prods[h]["id"], h, label, k, v, round(v / tot * 100, 2)])
        for n in (1, 2, 3):
            for k, v in collections.Counter(ngrams(text, n)).items():
                site[k][0] += v; site[k][1].add(h)
    sheet("Review_Keywords_by_Product", ["product_id", "handle", "ngram_type", "keyword", "count", "density_pct"], KP)
    SW = sorted(([k, v[0], len(v[1]), ", ".join(sorted(v[1]))[:250], intent(k), None, None, None]
                 for k, v in site.items() if v[0] >= 3 and (len(k.split()) > 1 or v[0] >= 5)), key=lambda x: -x[1])[:1500]
    sheet("Review_Keywords_Sitewide", ["keyword", "total_occurrences", "products_using", "products", "intent_bucket",
                                       "search_volume (fill from Ahrefs/SEMrush/GKP)", "cpc (fill from tool)",
                                       "keyword_difficulty (fill from tool)"], SW, {"products": 60})

    # Theme coverage
    themes = list(THEMES)
    TC = []
    for h, p in sorted(prods.items()):
        rv = by.get(h, [])
        row = [p["id"], h, p["title"], len(rv)]
        for t in themes:
            row.append(sum(1 for r in rv if re.search(THEMES[t], f"{r['title']} {r['body']}".lower())))
        TC.append(row)
    ws = sheet("Theme_Coverage", ["product_id", "handle", "title", "reviews"] + themes, TC)
    # Gaps
    GP = []
    for row in TC:
        n = row[3]; counts = dict(zip(themes, row[4:]))
        missing = [t for t in themes if t != "Complaint" and counts[t] == 0]
        prow_ = next(x for x in P if x[1] == row[1])
        GP.append([row[0], row[1], row[2], n, prow_[17], prow_[22],
                   "No reviews" if n == 0 else ("<5 reviews" if n < 5 else ("No review in 90d" if prow_[20] == 0 else "OK")),
                   ", ".join(missing)])
    sheet("Review_Gaps", ["product_id", "handle", "title", "reviews", "days_since_last_review", "weekly_gap_vs_target",
                          "status", "themes_never_mentioned_in_reviews"], sorted(GP, key=lambda x: x[3]), {"themes_never_mentioned_in_reviews": 70})

    # Summary
    rt_all = [r["rating"] for r in reviews if r["rating"]]
    zero = sum(1 for x in P if x[8] == 0)
    low = sum(1 for x in P if 0 < x[8] < 5)
    top = sorted(P, key=lambda x: -x[8])[:20]
    dist = collections.Counter(rt_all)
    S_ = [["Products discovered", len(prods)], ["Total reviews scraped", len(reviews)],
          ["Average rating (all reviews)", round(statistics.mean(rt_all), 2) if rt_all else None],
          ["Products with 0 reviews", zero], ["Products with <5 reviews", low],
          ["Products with no review in last 90 days", sum(1 for x in P if x[20] == 0)],
          ["Reviews in last 7 days (all products)", sum(x[18] for x in P)],
          ["Weekly target per product (assumption)", WEEKLY_TARGET],
          ["Verified-buyer share %", round(sum(r["verified_buyer"] for r in reviews) / max(1, len(reviews)) * 100, 1)],
          ["Reviews with photos", sum(1 for r in reviews if r["photos"])],
          ["Products in stock %", round(sum(1 for p in prods.values() if p["available"]) / len(prods) * 100, 1)]]
    S_ += [[f"{k}-star reviews", dist.get(k, 0)] for k in (5, 4, 3, 2, 1)]
    S_ += [[], ["TOP 20 PRODUCTS BY REVIEW COUNT", "reviews / avg rating"]] + [[x[2], f"{x[8]} / {x[10]}"] for x in top]
    ws = wb.active; ws.title = "Summary"
    ws.append(["Metric", "Value"])
    for r in S_:
        ws.append(r)
    for c in ws[1]:
        c.fill, c.font = HDR, HF
    ws.column_dimensions["A"].width = 55; ws.column_dimensions["B"].width = 25
    wb.move_sheet("Summary", offset=-len(wb.sheetnames))
    qa.append(("assumption", "weekly_target", f"{WEEKLY_TARGET} reviews/product/week assumed"))
    qa.append(("assumption", "review_source", "Judge.me public widget endpoint; counts cross-checked with on-page badge"))
    qa.append(("note", "search metrics", "volume/CPC/difficulty left blank on purpose"))
    sheet("QA_Log", ["type", "item", "detail"], qa, {"detail": 80})
    wb.save(OUT)
    print("Saved", OUT, flush=True)


if __name__ == "__main__":
    main()
