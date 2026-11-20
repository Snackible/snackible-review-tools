"""Review writing assistant for a human reviewer.

The writer supplies their real impressions of a product; Claude turns them into
polished draft reviews; the writer edits and approves. Tracks a weekly quota
per product so nothing gets missed.

Setup:  pip install anthropic     and set ANTHROPIC_API_KEY
Usage:  python review_helper.py status [--quota 5]
        python review_helper.py draft  [--quota 5]
"""
import argparse, csv, datetime as dt, sys
from pathlib import Path

import anthropic

MODEL = "claude-sonnet-5-5"
HERE = Path(__file__).parent
PRODUCTS = HERE / "products.csv"   # columns: name,url,description
LOG = HERE / "reviews_log.csv"     # every approved review
FIELDS = ["date", "week", "product", "rating", "title", "body", "reviewer_label", "status"]


def week_id(d=None):
    y, w, _ = (d or dt.date.today()).isocalendar()
    return f"{y}-W{w:02d}"


def load_products():
    with open(PRODUCTS, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load_log():
    if not LOG.exists():
        return []
    with open(LOG, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def append_log(row):
    new = not LOG.exists()
    with open(LOG, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new:
            w.writeheader()
        w.writerow(row)


def progress(quota):
    done = {}
    for r in load_log():
        if r["week"] == week_id():
            done[r["product"]] = done.get(r["product"], 0) + 1
    return [(p, done.get(p["name"], 0), quota) for p in load_products()]


def cmd_status(a):
    print(f"Week {week_id()} - quota {a.quota} per product")
    for p, n, q in progress(a.quota):
        print(f"  {'OK ' if n >= q else '...'} {p['name']}: {n}/{q}")


def ask(prompt, default=""):
    v = input(f"{prompt}{f' [{default}]' if default else ''}: ").strip()
    return v or default


def generate(product, notes, rating, tone, existing):
    client = anthropic.Anthropic()
    prior = "\n".join(f"- {t}" for t in existing[-8:]) or "none"
    prompt = f"""You are helping a reviewer write a product review for an online snack store.
The reviewer has really tried the product. Use ONLY their notes below; never invent
flavors, ingredients, health claims, or experiences they didn't mention.

Product: {product['name']}
Product description: {product.get('description','')}
Reviewer's rating: {rating}/5
Reviewer's notes: {notes}
Tone: {tone}

Earlier reviews of this product (do NOT repeat their phrasing or structure):
{prior}

Write 3 distinct draft reviews. Each must be ONE short sentence (two at most), 6-14 words,
in exactly the style of these examples:
"Loved the masala flavour, very tasty." / "Very crunchy and full of flavour!" /
"Carried these while travelling, easy to snack on." / "Love that these are not made from palm oil." /
"Good change from regular namkeen." / "Tried these during my office break, really good." Natural first-person voice, no hype clichés, no emojis unless the
tone asks. Style: proper English, simple sentence structures, minimal but correct punctuation.
Only mention themes (taste, crunch, kids, travel, protein, ingredients) that the
reviewer's notes actually cover. Output exactly:
### 1
TITLE: ...
BODY: ...
### 2
...
### 3
..."""
    msg = client.messages.create(
        model=MODEL, max_tokens=1200,
        messages=[{"role": "user", "content": prompt}],
    )
    text = msg.content[0].text
    drafts = []
    for block in text.split("###")[1:]:
        t = b = ""
        for line in block.splitlines():
            if line.startswith("TITLE:"):
                t = line[6:].strip()
            elif line.startswith("BODY:"):
                b = line[5:].strip()
            elif b and line.strip():
                b += " " + line.strip()
        if b:
            drafts.append((t, b))
    return drafts


def cmd_draft(a):
    todo = sorted(progress(a.quota), key=lambda x: x[1] - x[2])
    todo = [t for t in todo if t[1] < t[2]]
    if not todo:
        print("Quota met for every product this week.")
        return
    while todo:
        product, n, q = todo[0]
        print(f"\n=== {product['name']} ({n}/{q} this week) ===")
        if ask("Write one for this product? (y/n/quit)", "y").lower().startswith("q"):
            break
        notes = ask("Your real impressions (taste, texture, packaging, who it's for)")
        rating = ask("Rating 1-5", "5")
        tone = ask("Tone", "casual and honest")
        label = ask("Reviewer label shown on site (e.g. 'Snackible team', 'Verified buyer')", "Snackible team")
        prior = [r["body"] for r in load_log() if r["product"] == product["name"]]
        drafts = generate(product, notes, rating, tone, prior)
        for i, (t, b) in enumerate(drafts, 1):
            print(f"\n[{i}] {t}\n    {b}")
        pick = ask("\nPick 1-3, or 'e' to edit your own, 's' to skip", "1")
        if pick == "s":
            todo.append(todo.pop(0))
            continue
        if pick == "e":
            title, body = ask("Title"), ask("Body")
        else:
            title, body = drafts[int(pick) - 1]
            title = ask("Title (edit or Enter)", title)
            body = ask("Body (edit or Enter)", body)
        append_log({"date": dt.date.today().isoformat(), "week": week_id(),
                    "product": product["name"], "rating": rating, "title": title,
                    "body": body, "reviewer_label": label, "status": "ready_to_post"})
        print("Saved to reviews_log.csv")
        todo = sorted([t for t in progress(a.quota) if t[1] < t[2]], key=lambda x: x[1] - x[2])
    print("\nDone. Post the 'ready_to_post' rows from reviews_log.csv to the site.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["status", "draft"])
    ap.add_argument("--quota", type=int, default=5, help="reviews per product per week")
    a = ap.parse_args()
    if not PRODUCTS.exists():
        sys.exit("Create products.csv first (name,url,description).")
    {"status": cmd_status, "draft": cmd_draft}[a.cmd](a)
