"""Review portal for snackible.com (Shopify + Judge.me + SQLite).

Flow: pick product -> enter your name -> tick keywords that are TRUE for you
(or type your own) -> AI drafts -> edit -> save to DB and publish to the
product's Judge.me reviews.

Setup:   pip install flask anthropic requests
Env:     ANTHROPIC_API_KEY, JUDGEME_API_TOKEN (private token), SHOP_DOMAIN (xxx.myshopify.com)
         optional: WEEKLY_QUOTA (default 5)
Run:     python portal.py   -> http://localhost:5000
"""
import datetime as dt, os, sqlite3
import requests
from flask import Flask, jsonify, request, render_template_string

import review_helper as rh

app = Flask(__name__)
SITE = "https://snackible.com"
QUOTA = int(os.environ.get("WEEKLY_QUOTA", 5))
# Vercel's filesystem is read-only except /tmp (wiped between cold starts); use a hosted DB for permanence.
DB = "/tmp/reviews.db" if os.environ.get("VERCEL") else os.path.join(os.path.dirname(os.path.abspath(__file__)), "reviews.db")


def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS products(
            id INTEGER PRIMARY KEY, name TEXT, handle TEXT, description TEXT, image TEXT, synced_at TEXT);
        CREATE TABLE IF NOT EXISTS reviews(
            id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT, week TEXT,
            product_id INTEGER REFERENCES products(id), reviewer_name TEXT NOT NULL,
            rating INTEGER NOT NULL, title TEXT, body TEXT, keywords TEXT,
            got_sample INTEGER, status TEXT, response TEXT);
        """)


def sync_products():
    """Pull the live product list from the Shopify storefront into the DB."""
    r = requests.get(f"{SITE}/products.json?limit=250", timeout=20)
    r.raise_for_status()
    now = dt.datetime.now().isoformat(timespec="seconds")
    with db() as c:
        for p in r.json()["products"]:
            c.execute("INSERT OR REPLACE INTO products VALUES(?,?,?,?,?,?)",
                      (p["id"], p["title"], p["handle"], (p.get("body_html") or "")[:400],
                       p["images"][0]["src"] if p["images"] else "", now))


@app.get("/api/products")
def products():
    with db() as c:
        if not c.execute("SELECT 1 FROM products").fetchone():
            pass
    sync_products()
    with db() as c:
        rows = c.execute("""
            SELECT p.*, (SELECT COUNT(*) FROM reviews r WHERE r.product_id=p.id
                         AND r.week=? AND r.status='published') AS done
            FROM products p ORDER BY p.name""", (rh.week_id(),)).fetchall()
    return jsonify([{**dict(r), "quota": QUOTA} for r in rows])


@app.get("/api/reviews/<int:pid>")
def product_reviews(pid):
    with db() as c:
        rows = c.execute("SELECT created_at,reviewer_name,rating,title,body,status FROM reviews "
                         "WHERE product_id=? ORDER BY id DESC", (pid,)).fetchall()
    return jsonify([dict(r) for r in rows])


# Sentence bank: each ticked keyword maps to simple, correct sentences.
SENTENCES = {
    "masala flavour": ["Loved the masala flavour, very tasty.", "Proper Indian masala taste, loved it.",
                       "The masala taste is quite good."],
    "cheese flavour": ["The cheese flavour is such a tasty combination!", "Cheese flavour is very tasty."],
    "zucchini taste": ["The zucchini taste is actually very nice."],
    "beetroot taste": ["The beetroot taste is really nice."],
    "different flavour": ["This flavour is different and amazing!", "A different flavour and a nice one."],
    "crunchy": ["Very crunchy and full of flavour!", "Tastes good and crunchy."],
    "light": ["Light and easy to eat."],
    "full of flavour": ["Full of flavour in every bite."],
    "travel snack": ["Carried these while travelling, easy to snack on."],
    "late night snack": ["Nice healthy option for late night munching."],
    "office break": ["Tried these during my office break, really good."],
    "road trip": ["Took these on a road trip, everyone liked them."],
    "serving guests": ["Good snack to serve with other namkeen when guests come."],
    "no palm oil": ["Love that these are not made from palm oil."],
    "veggie based": ["Nice way to get some vegetables in a snack."],
    "has protein": ["Nice to eat something tasty with protein also.",
                    "I love that I can get some protein while snacking."],
    "kids liked it": ["My kids liked these a lot."],
    "family liked it": ["The whole family liked these."],
    "elders liked it": ["The elders at home liked these too."],
    "good change from regular namkeen": ["Good change from regular namkeen."],
    "would order again": ["Will order again.", "Will definitely order again."],
}


TITLES_5 = ["Really tasty", "Loved it", "Very good snack", "Tasty and crunchy", "Great snacking option",
            "Worth trying", "Will buy again", "Good taste", "Nice snack", "Highly recommend", "So good",
            "Perfect for snacking"]
TITLES_4 = ["Good snack", "Tasty", "Nice taste", "Worth trying", "Good option", "Liked it", "Pretty good",
            "Good for snacking"]


def template_drafts(keywords, extra, rating):
    """Build reviews from the ticked keywords. Only ticked (true) options are used."""
    import random
    def sents(k):
        if k in SENTENCES:
            return SENTENCES[k]
        if k.endswith(" flavour"):          # product-specific flavour chip
            f = k[:-8]
            return [f"The {f} flavour is very tasty.", f"Loved the {f} flavour.",
                    f"The {f} taste is really nice."]
        return None
    ks = [k for k in keywords if sents(k)]
    if int(rating) <= 3 or not ks:
        return []          # low ratings need the reviewer's own words
    # Like the examples: each review is ONE short sentence, one per ticked keyword.
    titles = TITLES_5 if int(rating) == 5 else TITLES_4
    pool = random.sample(titles, len(titles))      # no repeated title within one set
    out = []
    for i, k in enumerate(random.sample(ks, len(ks))):
        s = random.choice(sents(k))
        if extra:
            s += " " + extra.strip()
        out.append({"title": pool[i % len(pool)], "body": s})
    return out[:6]


@app.post("/api/draft")
def draft():
    d = request.json
    kws, extra = d.get("keywords", []), d.get("extra", "")
    notes = ", ".join(kws) + (". " + extra if extra else "")
    if not notes.strip():
        return jsonify({"error": "Tick or type at least one keyword that is true for you."}), 400
    out = template_drafts(kws, extra, d["rating"])
    if os.environ.get("ANTHROPIC_API_KEY"):
        with db() as c:
            prior = [r["body"] for r in c.execute(
                "SELECT body FROM reviews WHERE product_id=? ORDER BY id DESC LIMIT 8", (d["id"],))]
        try:
            ai = rh.generate({"name": d["name"], "description": d.get("description", "")},
                             notes, d["rating"], "natural and honest", prior)
            out += [{"title": t, "body": b} for t, b in ai]
        except Exception as e:
            print("Claude draft failed:", e)
    if not out:
        out = [{"title": "", "body": extra}]   # write-your-own fallback
    return jsonify(out)


@app.post("/api/publish")
def publish():
    d = request.json
    if not d.get("reviewer_name", "").strip():
        return jsonify({"ok": False, "detail": "Name required"}), 400
    body = d["body"]
    if d.get("sample"):
        body += "\n\n(Reviewer received a free sample of this product.)"
    resp = requests.post("https://judge.me/api/v1/reviews", json={
        "shop_domain": os.environ["SHOP_DOMAIN"], "platform": "shopify",
        "api_token": os.environ["JUDGEME_API_TOKEN"], "id": d["id"],
        "name": d["reviewer_name"].strip(),
        "email": d.get("reviewer_email") or "reviews@snackible.com",
        "rating": int(d["rating"]), "title": d["title"], "body": body,
    }, timeout=30)
    ok = resp.status_code < 300
    with db() as c:
        c.execute("INSERT INTO reviews(created_at,week,product_id,reviewer_name,rating,title,body,"
                  "keywords,got_sample,status,response) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                  (dt.datetime.now().isoformat(timespec="seconds"), rh.week_id(), d["id"],
                   d["reviewer_name"].strip(), int(d["rating"]), d["title"], body,
                   ", ".join(d.get("keywords", [])), int(bool(d.get("sample"))),
                   "published" if ok else f"failed_{resp.status_code}", resp.text[:500]))
    return jsonify({"ok": ok, "detail": resp.text[:300]}), (200 if ok else 502)


PAGE = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Snackible Review Portal</title>
<style>
body{font:15px system-ui;margin:0;background:#faf7f2;color:#222}
header{padding:16px 24px;background:#222;color:#fff}
main{display:grid;grid-template-columns:320px 1fr;gap:20px;padding:20px;max-width:1100px;margin:auto}
.p{display:flex;gap:10px;align-items:center;padding:8px;border-radius:8px;cursor:pointer}
.p:hover,.p.sel{background:#fff}.p img{width:44px;height:44px;object-fit:cover;border-radius:6px}
.bar{height:5px;background:#ddd;border-radius:3px;margin-top:4px}.bar i{display:block;height:5px;background:#2a9d4a;border-radius:3px}
.card{background:#fff;padding:18px;border-radius:10px}
label{display:block;margin:10px 0 3px;font-weight:600}
input,textarea,select{width:100%;padding:8px;border:1px solid #ccc;border-radius:6px;font:inherit;box-sizing:border-box}
button{padding:9px 16px;border:0;border-radius:6px;background:#222;color:#fff;cursor:pointer;margin-top:10px}
.d{border:1px solid #ddd;border-radius:8px;padding:10px;margin-top:8px;cursor:pointer}.d:hover{border-color:#222}
.chips{display:flex;flex-wrap:wrap;gap:6px}.chip{border:1px solid #bbb;border-radius:16px;padding:4px 12px;cursor:pointer;user-select:none}
.chip.on{background:#222;color:#fff;border-color:#222}
.old{border-top:1px solid #eee;margin-top:8px;padding-top:6px;font-size:13px;color:#555}
@media(max-width:800px){main{grid-template-columns:1fr}}
</style>
<header><b>Snackible Review Portal</b></header>
<main><div id=list></div><div class=card id=form><p>Select a product.</p></div></main>
<script>
const FLAV=['masala','cheese','zucchini','beetroot','pizza','jalapeno','peri peri','sweet chilli','sriracha','chocolate','almond','ginger','ragi','jowar','quinoa','tomato','mint','onion','garlic','coconut','peanut','oats','cinnamon','vanilla','salt','pepper'];
function chipsFor(p){const t=(p.name+' '+(p.description||'')+' '+p.handle).toLowerCase().replace(/-/g,' ');
 const has=r=>new RegExp(r).test(t);
 const taste=FLAV.filter(f=>t.includes(f)).map(f=>f+' flavour');
 taste.push('different flavour');
 const ing=['no palm oil'];if(has('protein'))ing.push('has protein');if(has('veg|zucchini|beetroot|spinach|carrot|pulp'))ing.push('veggie based');
 return {Taste:taste,Texture:['crunchy','light','full of flavour'],
 Occasion:['travel snack','late night snack','office break','road trip','serving guests'],
 Ingredients:ing,Family:['kids liked it','family liked it','elders liked it'],
 Other:['good change from regular namkeen','would order again']}}
let P=[],cur=null,sel=new Set(),D=[];
const $=s=>document.querySelector(s);
const esc=s=>String(s).replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;');
async function load(){P=await (await fetch('/api/products')).json();
 $('#list').innerHTML=P.map((p,i)=>`<div class="p ${cur&&cur.id==p.id?'sel':''}" onclick="pick(${i})"><img src="${p.image}"><div style="flex:1">${esc(p.name)}<div class=bar><i style="width:${Math.min(100,p.done/p.quota*100)}%"></i></div><small>${p.done}/${p.quota} this week</small></div></div>`).join('')}
async function pick(i){cur=P[i];sel=new Set();load();
 $('#form').innerHTML=`<h2>${esc(cur.name)}</h2>
 <label>Your name (shown on the review)</label><input id=rn value="${esc(localStorage.rn||'')}" placeholder="e.g. Priya">
 <button onclick=step2()>Continue</button><div id=old></div>`;
 const r=await (await fetch('/api/reviews/'+cur.id)).json();
 $('#old').innerHTML=r.length?'<h4>Existing reviews</h4>'+r.map(x=>`<div class=old>${x.rating}/5 - ${esc(x.reviewer_name)}: ${esc(x.title||'')} <i>(${x.status})</i></div>`).join(''):''}
function step2(){const n=$('#rn').value.trim();if(!n){alert('Please write your name first.');return}
 localStorage.rn=n;
 $('#form').innerHTML=`<h2>${esc(cur.name)}</h2><p>Reviewing as <b>${esc(n)}</b>. Tick only what is true for you.</p>
 ${Object.entries(chipsFor(cur)).map(([g,a])=>`<label>${g}</label><div class=chips>${a.map(k=>`<span class=chip onclick="tg(this,'${k}')">${k}</span>`).join('')}</div>`).join('')}
 <label>Your own words / anything else you noticed</label><textarea id=extra rows=3></textarea>
 <label>Your rating</label><select id=rating><option value="">Choose...</option>${[5,4,3,2,1].map(n=>`<option>${n}</option>`).join('')}</select>
 <label><input type=checkbox id=sample style="width:auto"> I received a free sample (adds disclosure)</label>
 <button onclick=gen()>Generate drafts</button><div id=drafts></div>`;window.NAME=n}
function tg(el,k){sel.has(k)?sel.delete(k):sel.add(k);el.classList.toggle('on')}
async function gen(){if(!$('#rating').value){alert('Choose your rating.');return}
 $('#drafts').innerHTML='Writing...';
 const r=await fetch('/api/draft',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:cur.id,name:cur.name,description:cur.description,keywords:[...sel],extra:$('#extra').value,rating:$('#rating').value})});
 const j=await r.json();if(j.error){$('#drafts').textContent=j.error;return}
 D=j;$('#drafts').innerHTML=j.map((d,i)=>`<div class=d onclick="use(${i})"><b>${esc(d.title)}</b><br>${esc(d.body)}</div>`).join('')}
function use(i){$('#drafts').innerHTML=`<label>Title</label><input id=t value="${esc(D[i].title)}">
 <label>Review</label><textarea id=b rows=5>${esc(D[i].body)}</textarea>
 <button onclick=pub()>Publish to ${esc(cur.name)}</button> <span id=msg></span>`}
async function pub(){const r=await fetch('/api/publish',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:cur.id,reviewer_name:window.NAME,rating:$('#rating').value,title:$('#t').value,body:$('#b').value,keywords:[...sel],sample:$('#sample').checked})});
 const j=await r.json();$('#msg').textContent=j.ok?'Published!':'Failed: '+j.detail;if(j.ok)load()}
load();
</script>"""


@app.get("/")
def home():
    return render_template_string(PAGE)


@app.before_request
def password_gate():
    """This app can publish to Judge.me: set APP_PASSWORD (any username) before putting it on a public URL."""
    from flask import Response
    pw = os.environ.get("APP_PASSWORD")
    if pw:
        a = request.authorization
        if not a or a.password != pw:
            return Response("Login required", 401, {"WWW-Authenticate": 'Basic realm="Review portal"'})


init_db()      # also on import, so serverless platforms get their tables

if __name__ == "__main__":
    app.run(port=5000, debug=False)
