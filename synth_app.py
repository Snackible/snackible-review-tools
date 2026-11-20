"""Snackible synthetic review dataset builder (for ML training only).

* Product-aware: every review is generated from that product's own title, description
  and tags, and a guard rejects reviews that mention flavours/ingredients/claims the
  product doesn't have (no "chocolate" on a peri peri chip).
* 500-name corpus (modern Indian + generic) assigned at random.
* Everything is stored in synthetic_reviews.db with synthetic=1. There is NO code in
  this app that posts to the website or Judge.me.

Setup:   pip install flask requests beautifulsoup4
Keys:    copy .env.example to .env and put your GROQ_API_KEY in it
Run:     python synth_app.py   ->  http://localhost:5001
"""
import csv, datetime as dt, io, itertools, json, os, random, re, sqlite3, time, uuid
from contextlib import contextmanager
import requests
from bs4 import BeautifulSoup
from flask import Flask, Response, jsonify, render_template_string, request

HERE = os.path.dirname(os.path.abspath(__file__))
ON_VERCEL = bool(os.environ.get("VERCEL"))
# Vercel's filesystem is read-only except /tmp, and /tmp is wiped between cold starts.
# For permanent data on Vercel, switch to a hosted database (Postgres/Neon).
DB = "/tmp/synthetic_reviews.db" if ON_VERCEL else os.path.join(HERE, "synthetic_reviews.db")
SITE = "https://snackible.com"


def load_env():
    p = os.path.join(HERE, ".env")
    if os.path.exists(p):
        for line in open(p, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env()
app = Flask(__name__)


# ------------------------------------------------------------------ db
@contextmanager
def db():
    """Open a connection, commit on success, roll back on error, always close it."""
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS products(id INTEGER PRIMARY KEY, name TEXT, handle TEXT, ptype TEXT,
            tags TEXT, description TEXT, image TEXT, price REAL);
        CREATE TABLE IF NOT EXISTS names(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE, origin TEXT);
        CREATE TABLE IF NOT EXISTS synthetic_reviews(id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id TEXT,
            created_at TEXT, week TEXT, product_id INTEGER, product_name TEXT, reviewer_name TEXT,
            rating INTEGER, title TEXT, body TEXT, theme TEXT, persona TEXT, model TEXT,
            synthetic INTEGER DEFAULT 1);
        """)
        cols = [r["name"] for r in c.execute("PRAGMA table_info(synthetic_reviews)")]
        if "sentiment" not in cols:
            c.execute("ALTER TABLE synthetic_reviews ADD COLUMN sentiment TEXT")
            c.execute("UPDATE synthetic_reviews SET sentiment = CASE WHEN rating>=4 THEN 'positive' "
                      "WHEN rating<=2 THEN 'negative' ELSE 'neutral' END")
    ensure_names()


def week_id():
    y, w, _ = dt.date.today().isocalendar()
    return f"{y}-W{w:02d}"


# ------------------------------------------------------------------ names (500)
IN_M = """Aarav Vivaan Aditya Arjun Reyansh Ishaan Kabir Rohan Karan Rahul Siddharth Dev Veer Aryan Nikhil Varun Yash Harsh
Kunal Mohit Tanmay Pranav Ankit Manav Raghav Shaurya Atharv Dhruv Krish Neel Rudra Sahil Tushar Vikram Abhay Jay Parth
Naman Lakshya Ayaan Rishi Samar Utkarsh Hrithik Jatin Gaurav Devansh Kartik Madhav Piyush Sarthak""".split()
IN_F = """Ananya Aanya Diya Ishita Kavya Meera Myra Navya Riya Saanvi Tanvi Zara Aditi Priya Sneha Neha Pooja Shreya Anjali
Kritika Mehak Simran Nisha Ritika Aisha Avni Ira Kiara Mira Pari Sanya Trisha Vaani Anushka Bhavya Disha Esha Gauri Isha
Jiya Khushi Lavanya Muskan Nandini Prisha Radhika Sakshi Tara Urvi Yamini""".split()
IN_L = """Sharma Verma Gupta Mehta Iyer Nair Reddy Patel Shah Kapoor Malhotra Joshi Kulkarni Desai Bose Chatterjee Banerjee
Singh Khanna Bhatia Saxena Agarwal Jain Menon Pillai Rao Chopra Arora Sethi Kohli Trivedi Bhatt Pandey Mishra Das Ghosh
Sinha Tiwari Yadav Naidu Bhargava Dutta Goel Kaur Lal Mathur Oberoi Rastogi Srinivasan Thakur Varma""".split()
GN_M = """Liam Noah Ethan Lucas Daniel Ryan Adam Jacob Nathan Max Oliver Henry Jack Leo Mason Logan Owen Caleb Dylan Eli""".split()
GN_F = """Emma Olivia Sophia Mia Ava Chloe Grace Lily Hannah Ella Amelia Zoe Nora Ruby Layla Stella Maya Alice Claire Ivy""".split()
GN_L = """Smith Brown Taylor Walker Clark Young Hall Allen King Wright Scott Green Baker Adams Turner Hill Cooper Reed
Bailey Moore Evans Carter Morgan Bell Murphy Ross""".split()


def ensure_names():
    with db() as c:
        if c.execute("SELECT COUNT(*) FROM names").fetchone()[0] >= 500:
            return
        rnd = random.Random(42)
        out = {}
        while len(out) < 500:
            indian = rnd.random() < 0.7
            first = rnd.choice((IN_M + IN_F) if indian else (GN_M + GN_F))
            last = rnd.choice(IN_L if indian else GN_L)
            name = f"{first} {last}" if rnd.random() < 0.6 else f"{first} {last[0]}."
            out.setdefault(name, "indian" if indian else "generic")
        c.execute("DELETE FROM names")
        c.executemany("INSERT INTO names(name,origin) VALUES(?,?)", list(out.items()))


# ------------------------------------------------------------------ products
def sync_products():
    page = 1
    with db() as c:
        while True:
            r = requests.get(f"{SITE}/products.json", params={"limit": 250, "page": page}, timeout=30,
                             headers={"User-Agent": "SnackibleSyntheticData/1.0"})
            r.raise_for_status()
            items = r.json()["products"]
            if not items:
                break
            for p in items:
                desc = BeautifulSoup(p.get("body_html") or "", "html.parser").get_text(" ", strip=True)[:1500]
                tags = p.get("tags", [])
                tags = ", ".join(tags) if isinstance(tags, list) else tags
                c.execute("INSERT OR REPLACE INTO products VALUES(?,?,?,?,?,?,?,?)",
                          (p["id"], p["title"], p["handle"], p.get("product_type", ""), tags, desc,
                           p["images"][0]["src"] if p["images"] else "",
                           min(float(v["price"]) for v in p["variants"])))
            page += 1


def load_snapshot():
    """Fallback product list bundled with the app (used when snackible.com can't be reached)."""
    try:
        with open(os.path.join(HERE, "products_snapshot.json"), encoding="utf-8") as f:
            rows = json.load(f)
    except (OSError, ValueError) as e:
        print("snapshot failed:", e)
        return
    with db() as c:
        c.executemany("INSERT OR REPLACE INTO products VALUES(:id,:name,:handle,:ptype,:tags,:description,:image,:price)", rows)


def product_text(p):
    return f"{p['name']} {p['ptype']} {p['tags']} {p['description']} {p['handle'].replace('-', ' ')}".lower()


# Flavour / ingredient / claim guard: review regex -> evidence required in the product's own text
GUARD = [
    (r"chocolate|choco|cocoa", r"chocolate|choco|cocoa"), (r"peri.?peri", r"peri.?peri"),
    (r"spic(y|e|iness)|chilli|chili|jalape|fiery", r"spic|chilli|chili|peri|masala|jalape|sriracha|pepper|hot|chatpata"),
    (r"masala", r"masala"), (r"chees(e|y)", r"chees"), (r"pizza", r"pizza"), (r"vanilla", r"vanilla"),
    (r"almond", r"almond"), (r"\bragi\b", r"ragi"), (r"jowar", r"jowar"), (r"millet", r"millet|ragi|jowar|bajra"),
    (r"ginger", r"ginger"), (r"coconut", r"coconut"), (r"peanut", r"peanut"), (r"beetroot", r"beetroot"),
    (r"zucchini", r"zucchini"), (r"quinoa", r"quinoa"), (r"\boats?\b", r"oat"), (r"\bdates?\b", r"date"),
    (r"caramel", r"caramel"), (r"\bmint", r"mint"), (r"onion", r"onion"), (r"garlic", r"garlic"),
    (r"tomato", r"tomato"), (r"lemon|citrus", r"lemon|citrus"), (r"cookie", r"cookie"), (r"chikki", r"chikki"),
    (r"chakli", r"chakli"), (r"bhakarwadi", r"bhakarwadi"), (r"makhana|fox nut", r"makhana|fox"),
    (r"popcorn", r"popcorn"), (r"\bdip\b|dipping", r"\bdip"), (r"protein", r"protein"),
    (r"fibre|fiber", r"fib(re|er)"), (r"sugar.?free|no (added )?sugar|low sugar", r"sugar"),
    (r"vegan", r"vegan"), (r"gluten", r"gluten"), (r"palm oil", r"palm"), (r"baked", r"baked"),
    (r"roasted", r"roast"), (r"keto", r"keto"), (r"calcium", r"calcium"), (r"electrolyte", r"electrolyte"),
    (r"salted|salty", r"salt"), (r"sweet", r"sweet|chocolate|caramel|jaggery|dates|cookie|chikki|vanilla|sugar|honey"),
]


GUARD += [
    (r"pani.?puri", r"pani"), (r"khakra", r"khakra"), (r"dosa", r"dosa"), (r"bhujia", r"bhujia"),
    (r"chakli", r"chakli"), (r"pudina", r"pudina|mint"), (r"thai", r"thai"), (r"cheddar", r"chedd"),
    (r"calori", r"calori"), (r"maida", r"maida|refined flour"), (r"popped", r"popped"),
    (r"healthy|guilt", r"health|guilt|fibre|fiber|protein|no refined|no added|baked|roast|millet|ragi|jowar|source of"),
    (r"fried", r"fried|baked|roast|popped|air"), (r"oily", r"oil|baked|roast|popped"),
    (r"chips?\b", r"chip"), (r"\bpuffs?\b", r"puff"), (r"cracker", r"cracker"), (r"\bbar\b", r"\bbar"),
]


def violates_guard(text, ptext):
    t = text.lower()
    for rv, ev in GUARD:
        if re.search(rv, t) and not re.search(ev, ptext):
            return rv
    return None


def similar(a, b):
    A, B = set(re.findall(r"[a-z]+", a.lower())), set(re.findall(r"[a-z]+", b.lower()))
    return len(A & B) / max(1, len(A | B))


# ------------------------------------------------------------------ generation
# Binary sentiment only (no 3-star), so every review has a clean positive/negative label.
MIX = {"binary": {5: 35, 4: 35, 2: 15, 1: 15},        # 70% positive / 30% negative
       "balanced": {5: 25, 4: 25, 2: 25, 1: 25},      # 50 / 50
       "realistic": {5: 50, 4: 40, 2: 6, 1: 4},       # mostly positive
       "positive": {5: 50, 4: 50}}                    # positive only


def sentiment_of(rating):
    return "positive" if rating >= 4 else "negative"


# ---- sample corpus (style anchors) and quality filters ----
CORPUS = json.load(open(os.path.join(HERE, "style_corpus.json"), encoding="utf-8"))
CORPUS_BODIES = [b for v in CORPUS.values() for b in v]
CORPUS_VOCAB = set(re.findall(r"[a-z']+", " ".join(CORPUS_BODIES).lower()))
PROMPT_FILE = os.path.join(HERE, "prompt_template.txt")
FANCY = re.compile(r"delight|exquisite|indulg|satisf|enjoyable|remarkable|palate|culinary|symphony|elevat|savou?r|"
                   r"scrumptious|wholesome|texture profile|well.balanced|impeccabl|exceptional|superb|flawless", re.I)
MAX_WORDS, MAX_SENTENCES = 15, 2
# Plain English a school student knows. Anything else must come from the sample reviews or the product's own text.
BASIC = set("""a an the and or but so if as at by for from in into of on to with without not no yes very really too quite
just still even also more most much many some any all every each both only i me my we our you your it its this that
these those they them their he she him her his is are was were be been am do does did done have has had can could will
would should may might must good great nice best better love loved like liked likes enjoy enjoyed tasty taste tastes
tasted flavour flavor flavours yummy delicious fresh crunchy crispy crisp crunch light soft hard spicy spice hot sweet
salty salt sour mild strong bland tangy snack snacks chips chip pack packet packing price size small big large less
enough little lot lots bit again always never ever often once first time times day days week anytime today buy bought
order ordered reorder try tried trying eat eating ate get got give gave make made makes feel feels felt find found keep
kept come coming go family kids kid friends friend guests office travel trip movie night evening morning tea chai
thing things something nothing everyone people one two three now here there when while after before during than then
because though expected expect hoped worth skip next won't don't doesn't didn't isn't wasn't it's i'm i've same
different new old real actual proper perfect right wrong okay fine bad poor way own such what which who how why where
healthy health too tasting cheap costly expensive money value quality bag box jar piece pieces bite bites kind sort
bought buying home work lunch dinner breakfast kids' mine yours ours am pm up down out over off back away around about
sure maybe probably definitely surely anyway though although however instead rather quite pretty fairly slightly
bit lot too many few couple dry oily greasy stale burnt dull weak heavy filling full empty finish finished left
disappointed happy glad sad upset satisfied surprised surprise surprising unique interesting amazing awesome wonderful
excellent superb fantastic terrible awful horrible worst waste overpriced""".split())
CUT_OFF = {"no", "and", "the", "a", "an", "of", "to", "with", "for", "or", "but", "is", "are", "it", "i", "my", "not",
           "so", "very", "too", "in", "on", "at", "as", "that", "this", "than", "but", "if"}


def pick_samples(p, ptext, k=8):
    """Prefer the sample reviews written for this product; fill with generic ones that don't name other products."""
    ptoks = set(re.findall(r"[a-z]+", ptext))
    best, score = None, 0
    for key in CORPUS:
        kt = set(re.findall(r"[a-z]+", key.lower()))
        s = len(kt & ptoks) / len(kt)
        if s > score:
            best, score = key, s
    own = list(CORPUS[best]) if best and score >= 0.8 else []
    others = [b for key, v in CORPUS.items() if key != best for b in v if not violates_guard(b, ptext)]
    random.shuffle(own); random.shuffle(others)
    own = own[:5]
    return (own + others)[:k]


def quality_problem(body, ptext):
    """Return a reason string if the review breaks the style or grounding rules, else None."""
    words = re.findall(r"[A-Za-z0-9'%]+", body)
    if not (3 <= len(words) <= MAX_WORDS):
        return "length"
    if len(re.findall(r"[.!?]+(?:\s|$)", body.strip())) > MAX_SENTENCES:
        return "sentences"
    if FANCY.search(body):
        return "fancy vocab"
    ptoks = set(re.findall(r"[a-z]+", ptext))
    allowed = CORPUS_VOCAB | BASIC | ptoks          # sample words + plain English + this product's own words
    for w in re.findall(r"[a-z']+", body.lower()):
        if w not in allowed and not (w.endswith("s") and w[:-1] in allowed):
            return f"outside vocabulary: {w}"
    last = (re.findall(r"[a-z']+", body.lower()) or [""])[-1]
    if last in CUT_OFF or re.search(r"\bno (refined|palm|added|artificial)\W*$", body.lower()):
        return "cut-off ending"
    for num in re.findall(r"\d+", body):
        if num not in ptext:
            return f"number not in product text: {num}"
    return violates_guard(body, ptext) and "not in product"


def llm_call(system, user):
    """Groq chat completion (OpenAI-compatible). Replace this function to use another provider."""
    key = os.environ.get("GROQ_API_KEY", "").strip()
    if not key:
        raise RuntimeError("GROQ_API_KEY missing. Put it in the .env file.")
    model = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")
    for attempt in range(4):
        r = requests.post("https://api.groq.com/openai/v1/chat/completions",
                          headers={"Authorization": f"Bearer {key}"},
                          json={"model": model, "temperature": 1.0, "response_format": {"type": "json_object"},
                                "messages": [{"role": "system", "content": system},
                                             {"role": "user", "content": user}]}, timeout=90)
        if r.status_code == 429:
            time.sleep(float(r.headers.get("retry-after", 5 * (attempt + 1))))
            continue
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]
    raise RuntimeError("Groq rate limit: retries exhausted")


def build_specs(n, mix):
    ratings = random.choices(list(MIX[mix]), weights=list(MIX[mix].values()), k=n)
    specs = []
    for i, rt in enumerate(ratings, 1):
        specs.append({"id": i, "rating": rt, "sentiment": sentiment_of(rt),
                      "words": random.choice([4, 5, 6, 8, 10, 12, 14])})
    return specs


def generate_for_product(p, n, mix, batch_id):
    """Returns number of reviews saved (guarded + de-duplicated)."""
    ptext = product_text(p)
    with db() as c:
        existing = [r["body"] for r in c.execute(
            "SELECT body FROM synthetic_reviews WHERE product_id=?", (p["id"],))]
        names = [r["name"] for r in c.execute("SELECT name FROM names")]
    tmpl = open(PROMPT_FILE, encoding="utf-8").read()       # editable prompt, re-read on every call
    sys_part, user_part = tmpl.split("\n\nPRODUCT", 1)
    system = sys_part.replace("SYSTEM\n", "", 1).strip()
    saved, tries, dropped = 0, 0, 0
    while saved < n and tries < 3:
        tries += 1
        want = min(10, n - saved)
        specs = build_specs(want + 3, mix)          # small over-ask to absorb rejected reviews
        user = ("PRODUCT" + user_part).replace("{{PRODUCT_NAME}}", p["name"]) \
            .replace("{{PRODUCT_TYPE}}", p["ptype"] or "").replace("{{PRODUCT_TAGS}}", p["tags"] or "") \
            .replace("{{PRODUCT_DESCRIPTION}}", p["description"] or "") \
            .replace("{{STYLE_SAMPLES}}", "\n".join(f"- {s}" for s in pick_samples(p, ptext))) \
            .replace("{{SPECS}}", json.dumps([{k: v for k, v in s.items() if k != "rating"} for s in specs]))
        raw = llm_call(system, user)
        try:
            items = json.loads(raw)["reviews"]
        except Exception:
            continue
        spec_by_id = {s["id"]: s for s in specs}
        used_names = set()
        with db() as c:
            for it in items:
                if saved >= n:
                    break
                body = re.sub(r"\s+", " ", (it.get("body") or "")).strip()
                s = spec_by_id.get(it.get("id"))
                if not body or not s:
                    continue                      # empty body = model chose to drop it (not grounded)
                why = quality_problem(body, ptext)
                if not why and (any(similar(body, e) >= 0.8 for e in existing)
                                or any(similar(body, e) >= 0.85 for e in CORPUS_BODIES)):
                    why = "duplicate"
                if why:
                    dropped += 1
                    continue
                name = random.choice([x for x in names if x not in used_names] or names)
                used_names.add(name)
                c.execute("""INSERT INTO synthetic_reviews(batch_id,created_at,week,product_id,product_name,
                    reviewer_name,rating,title,body,theme,persona,model,sentiment,synthetic)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,1)""",
                          (batch_id, dt.datetime.now().isoformat(timespec="seconds"), week_id(), p["id"],
                           p["name"], name, s["rating"], "", body, "", "",
                           os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b"), s["sentiment"]))
                existing.append(body)
                saved += 1
    return saved, dropped


CHUNK = 8     # reviews per request: one Groq call, fits inside a serverless function's time limit


# ------------------------------------------------------------------ api
def product_rows():
    with db() as c:
        rows = c.execute("""SELECT p.*, (SELECT COUNT(*) FROM synthetic_reviews s WHERE s.product_id=p.id) AS total,
            (SELECT COUNT(*) FROM synthetic_reviews s WHERE s.product_id=p.id AND s.week=?) AS this_week
            FROM products p ORDER BY p.name""", (week_id(),)).fetchall()
    return [dict(r) for r in rows if "free-snack" not in r["handle"]]


@app.get("/api/state")
def state():
    note = ""
    if not product_rows():
        try:
            sync_products()
        except Exception as e:              # site unreachable from this host -> use bundled snapshot
            note = f"Live product sync failed ({e}); using bundled snapshot."
            print("sync failed:", e)
        if not product_rows():
            load_snapshot()
    rows = product_rows()
    with db() as c:
        sc = {r["sentiment"]: r["n"] for r in c.execute(
            "SELECT sentiment, COUNT(*) n FROM synthetic_reviews WHERE week=? GROUP BY sentiment", (week_id(),))}
    return jsonify({"note": note, "products": rows, "week": week_id(), "key_set": bool(os.environ.get("GROQ_API_KEY")),
                    "pos": sc.get("positive", 0), "neg": sc.get("negative", 0),
                    "model": os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b"),
                    "week_total": sum(r["this_week"] for r in rows),
                    "weekly_target": int(os.environ.get("WEEKLY_TARGET", 100))})


@app.post("/api/sync")
def sync():
    sync_products()
    return jsonify({"ok": True})


@app.get("/api/plan")
def plan():
    """Split a request into small chunks (<= CHUNK reviews, one product each). The browser runs them
    one after another, so no background thread is needed (works on Vercel and locally)."""
    rows = {r["id"]: r for r in product_rows()}
    pid = request.args.get("product_id", type=int)
    count = max(1, request.args.get("count", 100, type=int))
    if pid is not None and pid not in rows:
        return jsonify({"error": "unknown product"}), 404
    alloc = {}
    if pid is not None:
        alloc[pid] = min(count, 50)
    else:   # weekly batch: spread across products, emptiest this week first
        order = sorted(rows.values(), key=lambda r: (r["this_week"], random.random()))
        for r in itertools.islice(itertools.cycle(order), min(count, 1000)):
            alloc[r["id"]] = alloc.get(r["id"], 0) + 1
    chunks = []
    for i, n in alloc.items():
        while n > 0:
            chunks.append({"product_id": i, "n": min(CHUNK, n)})
            n -= CHUNK
    random.shuffle(chunks)
    return jsonify({"batch_id": uuid.uuid4().hex[:10], "chunks": chunks, "total": sum(c["n"] for c in chunks)})


@app.post("/api/generate")
def generate():
    d = request.get_json(silent=True) or {}
    rows = {r["id"]: r for r in product_rows()}
    try:
        p, n = rows.get(int(d.get("product_id"))), int(d.get("n", 1))
    except (TypeError, ValueError):
        return jsonify({"error": "product_id and n must be numbers"}), 400
    if not p:
        return jsonify({"error": "unknown product"}), 404
    mix = d.get("mix") if d.get("mix") in MIX else "binary"
    try:
        saved, dropped = generate_for_product(p, max(1, min(CHUNK, n)), mix, d.get("batch_id", "manual"))
    except Exception as e:
        return jsonify({"error": f"{p['name']}: {e}"}), 500
    with db() as c:
        counts = {r["sentiment"]: r["n"] for r in c.execute(
            "SELECT sentiment, COUNT(*) n FROM synthetic_reviews WHERE batch_id=? GROUP BY sentiment",
            (d.get("batch_id", "manual"),))}
    return jsonify({"saved": saved, "dropped": dropped, "pos": counts.get("positive", 0),
                    "neg": counts.get("negative", 0)})


@app.get("/api/reviews/<int:pid>")
def reviews(pid):
    with db() as c:
        rows = c.execute("SELECT id,created_at,reviewer_name,rating,sentiment,title,body,theme,persona FROM synthetic_reviews "
                         "WHERE product_id=? ORDER BY id DESC LIMIT 200", (pid,)).fetchall()
    return jsonify([dict(r) for r in rows])


@app.delete("/api/review/<int:rid>")
def delete_review(rid):
    with db() as c:
        c.execute("DELETE FROM synthetic_reviews WHERE id=?", (rid,))
    return jsonify({"ok": True})


@app.get("/api/export.csv")
def export():
    week = request.args.get("week")
    cols = ["id", "batch_id", "created_at", "week", "product_id", "product_name", "reviewer_name", "rating",
            "sentiment", "body", "model", "synthetic"]
    q = f"SELECT {','.join(cols)} FROM synthetic_reviews" + (" WHERE week=?" if week else "") + " ORDER BY id"
    with db() as c:
        rows = c.execute(q, (week,) if week else ()).fetchall()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(cols)
    for r in rows:
        w.writerow(list(r))
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=synthetic_reviews.csv"})


PAGE = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Synthetic Review Dataset</title>
<style>
:root{--bg:#f6f3ee;--card:#fff;--ink:#1d1d1f;--mut:#6b6b70;--acc:#d9480f;--ok:#2a9d4a;--line:#e5e0d8}
*{box-sizing:border-box}body{margin:0;font:14px/1.45 system-ui,sans-serif;background:var(--bg);color:var(--ink)}
header{position:sticky;top:0;z-index:5;background:#1d1d1f;color:#fff;padding:12px 24px;display:flex;gap:16px;align-items:center;flex-wrap:wrap}
header h1{font-size:16px;margin:0}.badge{background:var(--acc);padding:2px 8px;border-radius:10px;font-size:11px;font-weight:700}
.sp{flex:1}.pill{background:#333;border-radius:14px;padding:4px 12px;font-size:12px}.pill.ok{background:#1f5a31}.pill.bad{background:#7a1f1f}
button,select,input{font:inherit}button{border:0;border-radius:7px;padding:7px 14px;background:var(--acc);color:#fff;cursor:pointer;font-weight:600}
button.sec{background:#fff;color:var(--ink);border:1px solid var(--line)}button:disabled{opacity:.5;cursor:default}
input[type=number],input[type=search],select{border:1px solid var(--line);border-radius:7px;padding:6px 8px;background:#fff}
.bar{padding:14px 24px;display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.prog{height:8px;background:#e5e0d8;border-radius:4px;overflow:hidden;width:220px}.prog i{display:block;height:100%;background:var(--ok);width:0}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:14px;padding:0 24px 40px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:hidden;display:flex;flex-direction:column}
.card img{width:100%;height:150px;object-fit:cover;background:#eee}.cb{padding:12px;display:flex;flex-direction:column;gap:8px;flex:1}
.nm{font-weight:650;line-height:1.3;min-height:36px}.meta{color:var(--mut);font-size:12px;display:flex;justify-content:space-between}
.tags{display:flex;gap:4px;flex-wrap:wrap}.tag{background:#f1ece4;border-radius:10px;padding:1px 8px;font-size:11px}
.act{display:flex;gap:6px;margin-top:auto}.act input{width:64px}
.panel{position:fixed;top:0;right:0;bottom:0;width:min(520px,100%);background:#fff;border-left:1px solid var(--line);transform:translateX(100%);transition:.2s;z-index:10;display:flex;flex-direction:column}
.panel.open{transform:none;box-shadow:-8px 0 30px rgba(0,0,0,.15)}.ph{padding:14px 16px;border-bottom:1px solid var(--line);display:flex;gap:10px;align-items:center}
.pb{overflow:auto;padding:12px 16px;flex:1}.rv{border-bottom:1px solid var(--line);padding:10px 0}.rv b{display:block}
.st{color:#e8a200;letter-spacing:1px}.rm{font-size:12px;color:var(--mut)}.x{margin-left:auto;background:none;color:var(--mut);padding:0 6px;font-size:20px}
.sent{font-size:11px;font-weight:700;border-radius:9px;padding:1px 8px;margin-left:6px}.sent.positive{background:#dff3e4;color:#17692f}.sent.negative{background:#fbe0e0;color:#a31b1b}
#err{color:#b00020;padding:0 24px;font-size:13px;white-space:pre-wrap}
</style>
<header><h1>Synthetic Review Dataset</h1><span class=badge>SYNTHETIC / ML TRAINING ONLY</span><span class=sp></span>
<span class=pill id=wk></span><span class=pill id=key></span></header>
<div class=bar>
 <b>Weekly batch</b><input type=number id=wn value=100 min=1 max=1000 style="width:80px">
 <select id=mix><option value=binary>70% positive / 30% negative</option><option value=balanced>50% positive / 50% negative</option><option value=realistic>Mostly positive (90 / 10)</option><option value=positive>Positive only (4-5 star)</option></select>
 <button id=wb onclick=weekly()>Generate weekly batch</button>
 <div class=prog><i id=pg></i></div><span id=pt></span><span class=sp style="flex:1"></span>
 <input type=search id=q placeholder="Search products" oninput=draw()>
 <button class=sec onclick="location='/api/export.csv'">Export all CSV</button>
 <button class=sec onclick="syncP()">Re-sync products</button>
</div><div id=err></div><div class=grid id=g></div>
<aside class=panel id=pn><div class=ph><b id=pt2></b><button class="x" onclick="pn.classList.remove('open')">&times;</button></div><div class=pb id=pb></div></aside>
<script>
const $=s=>document.querySelector(s),esc=s=>String(s??'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/"/g,'&quot;');
let S={products:[]};
const FL=['peri peri','masala','cheese','pizza','jalapeno','chocolate','vanilla','almond','ragi','jowar','millet','zucchini','beetroot','quinoa','ginger','coconut','peanut','caramel','protein','baked','sweet chilli','sriracha'];
async function load(){try{const r=await fetch('/api/state');if(!r.ok)throw new Error('HTTP '+r.status+' '+(await r.text()).slice(0,300));S=await r.json()}
 catch(e){$('#err').textContent='Could not load products: '+e;return}
 if(S.note)$('#err').textContent=S.note;
 $('#wk').textContent=`Week ${S.week}: ${S.week_total}/${S.weekly_target}  |  positive ${S.pos} / negative ${S.neg}`;
 const k=$('#key');k.textContent=S.key_set?`Groq key set - ${S.model}`:'No GROQ_API_KEY in .env';k.className='pill '+(S.key_set?'ok':'bad');
 $('#wn').value=S.weekly_target;draw()}
function draw(){const q=$('#q').value.toLowerCase();
 $('#g').innerHTML=S.products.filter(p=>p.name.toLowerCase().includes(q)).map(p=>{
 const t=(p.name+' '+p.tags+' '+p.description).toLowerCase(),tags=FL.filter(f=>t.includes(f)).slice(0,4);
 return `<div class=card><img loading=lazy src="${esc(p.image)}"><div class=cb><div class=nm>${esc(p.name)}</div>
 <div class=tags>${tags.map(x=>`<span class=tag>${x}</span>`).join('')}</div>
 <div class=meta><span>${p.total} generated</span><span>${p.this_week} this week</span></div>
 <div class=act><input type=number id="n${p.id}" value=5 min=1 max=50><button onclick="gen(${p.id})">Generate</button>
 <button class=sec onclick="view(${p.id},'${esc(p.name).replace(/'/g,'')}')">View</button></div></div></div>`}).join('')}
async function run(qs){$('#err').textContent='';
 const btns=document.querySelectorAll('button');btns.forEach(b=>b.disabled=true);
 const mix=$('#mix').value,errs=[];let done=0,drop=0,pos=0,neg=0;
 try{const pl=await (await fetch('/api/plan?'+qs)).json();
  for(const ch of pl.chunks){
   try{const r=await fetch('/api/generate',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({product_id:ch.product_id,n:ch.n,mix,batch_id:pl.batch_id})});
    const j=await r.json();if(j.error){errs.push(j.error);if(/GROQ_API_KEY/.test(j.error))break}
    else{done+=j.saved;drop+=j.dropped;pos=j.pos;neg=j.neg}}catch(e){errs.push(String(e))}
   $('#pg').style.width=(done/pl.total*100)+'%';
   $('#pt').textContent=`${done}/${pl.total}  (positive ${pos} / negative ${neg}, dropped ${drop})`;
   await new Promise(r=>setTimeout(r,2200))}}
 catch(e){errs.push(String(e))}
 btns.forEach(b=>b.disabled=false);if(errs.length)$('#err').textContent=errs.slice(0,5).join('\n');load()}
const gen=id=>run(`product_id=${id}&count=${+$('#n'+id).value}`),weekly=()=>run(`count=${+$('#wn').value}`);
async function view(id,name){pn.classList.add('open');$('#pt2').textContent=name;$('#pb').textContent='Loading...';
 const r=await (await fetch('/api/reviews/'+id)).json();
 $('#pb').innerHTML=r.length?r.map(x=>`<div class=rv><span class=st>${'&#9733;'.repeat(x.rating)}</span> <span class="sent ${x.sentiment}">${x.sentiment}</span>
  <div>${esc(x.body)}</div><div class=rm>${esc(x.reviewer_name)} <a href=# onclick="del(${x.id},${id},'${esc(name).replace(/'/g,'')}');return false">delete</a></div></div>`).join(''):'No reviews yet.'}
async function del(rid,pid,name){await fetch('/api/review/'+rid,{method:'DELETE'});view(pid,name);load()}
async function syncP(){await fetch('/api/sync',{method:'POST'});load()}
const pn=$('#pn');load();
</script>"""


@app.get("/")
def home():
    return render_template_string(PAGE)


@app.before_request
def password_gate():
    """Optional login: set APP_PASSWORD (any username). Strongly recommended on any public URL."""
    pw = os.environ.get("APP_PASSWORD")
    if pw:
        a = request.authorization
        if not a or a.password != pw:
            return Response("Login required", 401, {"WWW-Authenticate": 'Basic realm="Synthetic dataset"'})


init_db()      # runs on import too, so serverless platforms (Vercel) get their tables

if __name__ == "__main__":
    app.run(port=5001, debug=False)
