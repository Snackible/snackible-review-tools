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
import csv, datetime as dt, io, itertools, json, os, random, re, sqlite3, threading, time, uuid
import requests
from bs4 import BeautifulSoup
from flask import Flask, Response, jsonify, render_template_string, request

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "synthetic_reviews.db")
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
JOBS = {}


# ------------------------------------------------------------------ db
def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


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
PERSONAS = ["college student", "working professional", "fitness-focused person", "parent of young kids",
            "frequent traveller", "homemaker", "senior citizen", "gym-goer", "snack lover", "first-time buyer",
            "repeat customer", "person who snacks late at night", "office-goer", "health-conscious shopper"]
FOCUS = ["taste", "crunch and texture", "ingredients", "occasion for eating it", "value for money", "pack size",
         "family reaction", "swap for regular snacks", "would buy again", "flavour intensity", "how filling it is",
         "packaging"]
MIX = {"realistic": {5: 45, 4: 38, 3: 10, 2: 4, 1: 3}, "balanced": {5: 20, 4: 20, 3: 20, 2: 20, 1: 20},
       "positive": {5: 60, 4: 35, 3: 5}}


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
        specs.append({"id": i, "rating": rt, "persona": random.choice(PERSONAS),
                      "focus": random.choice(FOCUS), "words": random.choice([8, 12, 18, 25, 40, 60]),
                      "hinglish": random.random() < 0.12})
    return specs


def generate_for_product(p, n, mix, batch_id):
    """Returns number of reviews saved (guarded + de-duplicated)."""
    ptext = product_text(p)
    with db() as c:
        existing = [r["body"] for r in c.execute(
            "SELECT body FROM synthetic_reviews WHERE product_id=?", (p["id"],))]
        names = [r["name"] for r in c.execute("SELECT name FROM names")]
    saved, tries = 0, 0
    while saved < n and tries < 3:
        tries += 1
        want = min(10, n - saved)
        specs = build_specs(want + 2, mix)          # small over-ask to absorb rejects
        system = ("You write realistic synthetic customer reviews for a machine-learning training dataset "
                  "for an Indian healthy-snack brand. Output valid JSON only.")
        user = f"""PRODUCT
Name: {p['name']}
Type: {p['ptype']}
Tags: {p['tags']}
Description: {p['description']}

Write one review per spec below.
{json.dumps(specs)}

RULES
- Ground each review in THIS product: its flavour, ingredients, format, pack and claims that appear in the product text above. NEVER mention a flavour, ingredient or claim that is not in the product text (for example no chocolate on a savoury chip, no spice on a sweet dip).
- Each spec has a "focus". Use it if it applies to this product, otherwise choose another focus that does.
- Match the rating: 5 and 4 positive; 3 mixed; 2 and 1 critical with a specific plausible reason (flavour too strong or too mild, pack small for the price, less crunchy than expected). No health, safety or illness claims.
- Length is about "words" words. If "hinglish" is true, include 1 to 3 Roman-script Hindi words (mast, paisa vasool, chatpata, etc.).
- Style: proper English, simple sentence structure, minimal but correct punctuation. No emojis. No competitor brands. No reviewer names in the text. Do not mention that the review is synthetic.
- Every review must be clearly different from the others in wording, opening word and angle.
- Avoid these existing reviews: {json.dumps(existing[-6:])}
- Title is 2 to 5 words.
OUTPUT: {{"reviews":[{{"id":1,"title":"...","body":"...","theme":"one of the focus words"}}]}}"""
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
                body, title = (it.get("body") or "").strip(), (it.get("title") or "").strip()
                s = spec_by_id.get(it.get("id"))
                if not body or not s or violates_guard(f"{title} {body}", ptext):
                    continue
                if any(similar(body, e) >= 0.55 for e in existing):
                    continue
                name = random.choice([x for x in names if x not in used_names] or names)
                used_names.add(name)
                c.execute("""INSERT INTO synthetic_reviews(batch_id,created_at,week,product_id,product_name,
                    reviewer_name,rating,title,body,theme,persona,model,synthetic)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,1)""",
                          (batch_id, dt.datetime.now().isoformat(timespec="seconds"), week_id(), p["id"],
                           p["name"], name, s["rating"], title, body, it.get("theme", s["focus"]),
                           s["persona"], os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")))
                existing.append(body)
                saved += 1
    return saved


def run_job(job_id, plan, mix):
    """plan = list of (product_row_dict, n)."""
    job = JOBS[job_id]
    job.update(total=sum(n for _, n in plan), done=0, status="running", errors=[])
    delay = float(os.environ.get("CALL_DELAY_SECONDS", "2.2"))   # stay under Groq free-tier RPM
    for p, n in plan:
        try:
            job["done"] += generate_for_product(p, n, mix, job_id)
        except Exception as e:
            job["errors"].append(f"{p['name']}: {e}")
            if "GROQ_API_KEY" in str(e):
                break
        time.sleep(delay)
    job["status"] = "finished"


# ------------------------------------------------------------------ api
def product_rows():
    with db() as c:
        rows = c.execute("""SELECT p.*, (SELECT COUNT(*) FROM synthetic_reviews s WHERE s.product_id=p.id) AS total,
            (SELECT COUNT(*) FROM synthetic_reviews s WHERE s.product_id=p.id AND s.week=?) AS this_week
            FROM products p ORDER BY p.name""", (week_id(),)).fetchall()
    return [dict(r) for r in rows if "free-snack" not in r["handle"]]


@app.get("/api/state")
def state():
    if not product_rows():
        sync_products()
    rows = product_rows()
    return jsonify({"products": rows, "week": week_id(), "key_set": bool(os.environ.get("GROQ_API_KEY")),
                    "model": os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b"),
                    "week_total": sum(r["this_week"] for r in rows),
                    "weekly_target": int(os.environ.get("WEEKLY_TARGET", 100))})


@app.post("/api/sync")
def sync():
    sync_products()
    return jsonify({"ok": True})


@app.post("/api/generate")
def generate():
    d = request.json
    mix = d.get("mix", "realistic")
    rows = {r["id"]: r for r in product_rows()}
    if d.get("product_id"):
        plan = [(rows[int(d["product_id"])], max(1, min(50, int(d["count"]))))]
    else:   # weekly batch: spread N reviews across products, emptiest this week first
        total = max(1, min(1000, int(d["count"])))
        order = sorted(rows.values(), key=lambda r: (r["this_week"], random.random()))
        alloc = {}
        for r in itertools.islice(itertools.cycle(order), total):
            alloc[r["id"]] = alloc.get(r["id"], 0) + 1
        plan = [(rows[i], n) for i, n in alloc.items()]
        random.shuffle(plan)
    job_id = uuid.uuid4().hex[:10]
    JOBS[job_id] = {"status": "queued", "total": sum(n for _, n in plan), "done": 0, "errors": []}
    threading.Thread(target=run_job, args=(job_id, plan, mix), daemon=True).start()
    return jsonify({"job_id": job_id})


@app.get("/api/job/<job_id>")
def job(job_id):
    return jsonify(JOBS.get(job_id, {"status": "unknown"}))


@app.get("/api/reviews/<int:pid>")
def reviews(pid):
    with db() as c:
        rows = c.execute("SELECT id,created_at,reviewer_name,rating,title,body,theme,persona FROM synthetic_reviews "
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
    q = "SELECT * FROM synthetic_reviews" + (" WHERE week=?" if week else "") + " ORDER BY id"
    with db() as c:
        rows = c.execute(q, (week,) if week else ()).fetchall()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(rows[0].keys() if rows else ["id"])
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
#err{color:#b00020;padding:0 24px;font-size:13px;white-space:pre-wrap}
</style>
<header><h1>Synthetic Review Dataset</h1><span class=badge>SYNTHETIC / ML TRAINING ONLY</span><span class=sp></span>
<span class=pill id=wk></span><span class=pill id=key></span></header>
<div class=bar>
 <b>Weekly batch</b><input type=number id=wn value=100 min=1 max=1000 style="width:80px">
 <select id=mix><option value=realistic>Realistic mix (mostly 4-5 star)</option><option value=balanced>Balanced 1-5 star</option><option value=positive>Positive only (3-5)</option></select>
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
async function load(){S=await (await fetch('/api/state')).json();
 $('#wk').textContent=`Week ${S.week}: ${S.week_total}/${S.weekly_target}`;
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
async function run(body){$('#err').textContent='';
 const r=await fetch('/api/generate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...body,mix:$('#mix').value})});
 const {job_id}=await r.json();document.querySelectorAll('button').forEach(b=>b.disabled=true);
 const t=setInterval(async()=>{const j=await (await fetch('/api/job/'+job_id)).json();
  $('#pg').style.width=(j.total?j.done/j.total*100:0)+'%';$('#pt').textContent=`${j.done||0}/${j.total||0}`;
  if(j.status=='finished'){clearInterval(t);document.querySelectorAll('button').forEach(b=>b.disabled=false);
   if(j.errors&&j.errors.length)$('#err').textContent=j.errors.join('\\n');load()}},1500)}
const gen=id=>run({product_id:id,count:+$('#n'+id).value}),weekly=()=>run({count:+$('#wn').value});
async function view(id,name){pn.classList.add('open');$('#pt2').textContent=name;$('#pb').textContent='Loading...';
 const r=await (await fetch('/api/reviews/'+id)).json();
 $('#pb').innerHTML=r.length?r.map(x=>`<div class=rv><span class=st>${'&#9733;'.repeat(x.rating)}</span> <b style="display:inline">${esc(x.title)}</b>
  <div>${esc(x.body)}</div><div class=rm>${esc(x.reviewer_name)} - ${esc(x.theme)} - ${esc(x.persona)} <a href=# onclick="del(${x.id},${id},'${esc(name).replace(/'/g,'')}');return false">delete</a></div></div>`).join(''):'No reviews yet.'}
async function del(rid,pid,name){await fetch('/api/review/'+rid,{method:'DELETE'});view(pid,name);load()}
async function syncP(){await fetch('/api/sync',{method:'POST'});load()}
const pn=$('#pn');load();
</script>"""


@app.get("/")
def home():
    return render_template_string(PAGE)


if __name__ == "__main__":
    init_db()
    app.run(port=5001, debug=False)
