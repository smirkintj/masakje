#!/usr/bin/env python3
"""Econsave weekly catalogue (West Malaysia) -> promo prices for MasakJe ingredients.

Econsave publishes its weekly promotions as a flipbook PDF of photo-and-price-tag pages. The PDF's
text isn't usable (prices split, some fonts scrambled), so each page is rendered to an image and read
by a vision model (DeepSeek Flash). A second, text-only call matches the items to our ingredient list.
Every match is checked against the KPDN PriceCatcher national median; anything below 30% or above
300% of it is dropped as a likely misread. Only promo items appear, so this sits on top of survey prices.

Writes public/data/prices/econsave.json with the catalogue's validity dates. Needs DEEPSEEK_API_KEY,
pdftoppm (poppler-utils) and pypdf. Polite: a few requests a week to econsave.com.my.

Usage: python3 scripts/fetch_econsave.py [--cache DIR] [--pages N] [--from-json EXTRACTED.json]
"""
import argparse, base64, datetime as dt, glob, json, os, re, subprocess, sys, tempfile, time, urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "public", "data", "prices", "econsave.json")
SITE = "https://econsave.com.my"
PROMO_PAGE = SITE + "/econsave-supermarket-promotions/"
UA = "MasakJe-personal/0.1 (weekly; contact via GitHub repo)"
API = os.environ.get("MASAKJE_AI_BASE", "https://api.deepseek.com") + "/chat/completions"   # override only for local tests
MODEL = os.environ.get("MASAKJE_AI_MODEL", "deepseek-flash")
NON_HALAL = re.compile(r"non[- ]?halal|\bpork\b|\bbabi\b|\bbacon\b|\blard\b|\bham\b", re.I)


def get(url, binary=False):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = r.read()
    time.sleep(2)
    return data if binary else data.decode("utf-8", "replace")


def find_pdf():
    """The promotions page links each region's flipbook page; that page names the PDF in its dFlip options."""
    promo = get(PROMO_PAGE)
    m = re.search(r'href="(https://econsave\.com\.my/[^"]*west_malaysia_catalogue[^"]*)"', promo)
    if not m:
        raise SystemExit("econsave: no West Malaysia catalogue link on the promotions page")
    book = get(m.group(1))
    pdfs = re.findall(r'https:\\?/\\?/econsave\.com\.my\\?/wp-content\\?/uploads\\?/\d{4}\\?/\d{2}\\?/[^"\'\s]+?\.pdf', book)
    if not pdfs:
        raise SystemExit("econsave: no PDF in the West Malaysia flipbook page")
    return pdfs[0].replace("\\/", "/"), m.group(1)


def validity(pdf_path):
    from pypdf import PdfReader
    text = " ".join((p.extract_text() or "") for p in PdfReader(pdf_path).pages[:2])
    m = re.search(r"Valid\s*From:?\s*(\d{1,2})/(\d{1,2})/(\d{4})\s*[-–]\s*(\d{1,2})/(\d{1,2})/(\d{4})", text)
    if not m:
        return None, None
    d1 = dt.date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    d2 = dt.date(int(m.group(6)), int(m.group(5)), int(m.group(4)))
    return d1.isoformat(), d2.isoformat()


def deepseek(messages, max_tokens=4000, json_mode=True):
    body = {"model": MODEL, "thinking": {"type": "disabled"}, "max_tokens": max_tokens, "messages": messages}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    req = urllib.request.Request(API, data=json.dumps(body).encode(), method="POST",
                                 headers={"Authorization": f"Bearer {os.environ['DEEPSEEK_API_KEY']}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            j = json.loads(r.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        if json_mode and e.code == 400:   # if JSON mode can't be combined with images, ask for plain text and parse it
            return deepseek(messages, max_tokens, json_mode=False)
        raise SystemExit(f"deepseek {e.code}: {detail}")
    text = (j["choices"][0]["message"].get("content") or "").strip()
    m = re.search(r"\{.*\}", text, re.S)
    try:
        return json.loads(m.group(0) if m else text), j.get("usage", {})
    except Exception:
        return {}, j.get("usage", {})


READ_PAGE = """This is one page of a Malaysian supermarket promotion flyer. List every product that has a price on it.
Reply in json only: {"items":[{"name":"Kobis Bunga","size":"1kg","price":4.59,"deal":""}]}
- name: as printed (Malay or English); add the English in brackets if both are shown.
- size: the pack or unit printed with the price, e.g. "1kg", "500g", "1Pkt", "5Pcs", "Each", "2 x 200ml".
- price: RM for that size, as a number. Big digits then small digits are ringgit and sen: "4 59" is 4.59, "1599" printed with small "99" is 15.99.
- deal: any multi-buy or member text next to it, e.g. "Buy 3pkt @RM10", else "".
- Skip anything without a readable price. Don't guess."""


def read_pages(pdf_path, pages, cache):
    out = os.path.join(cache, "econsave_pages")
    os.makedirs(out, exist_ok=True)
    for f in glob.glob(os.path.join(out, "*.png")):
        os.remove(f)
    subprocess.run(["pdftoppm", "-r", "110", "-png", "-l", str(pages), pdf_path, os.path.join(out, "p")], check=True)
    items, usage = [], {"prompt_tokens": 0, "completion_tokens": 0}
    for i, img in enumerate(sorted(glob.glob(os.path.join(out, "p-*.png"))), 1):
        b64 = base64.b64encode(open(img, "rb").read()).decode()
        data, u = deepseek([{"role": "user", "content": [{"type": "text", "text": READ_PAGE},
                                                         {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}]}], 4000)
        for k in usage:
            usage[k] += u.get(k, 0)
        got = [dict(x, page=i) for x in (data.get("items") or []) if isinstance(x, dict) and x.get("name") and isinstance(x.get("price"), (int, float))]
        print(f"  page {i}: {len(got)} items")
        items += got
    return items, usage


def match(items, ing):
    cat = "\n".join(f"{k} | {v['name']} | {v['unit']}" for k, v in ing.items())
    system = f"""You match supermarket flyer items to a home-cooking ingredient list. Reply in json only:
{{"matches":[{{"i":0,"key":"cauliflower","qty":1000}}]}}
- Only match when the item IS that raw ingredient (fresh or plain packaged), not a product made from it: chicken nuggets are not chicken, chili sauce is not chili_red, flavoured milk is not milk.
- qty: the item's size converted to the ingredient's unit (g, ml or pc), e.g. "1kg" -> 1000, "5Pcs" -> 5, "2 x 200ml" -> 400. If the size can't be converted (e.g. "Each" for a vegetable sold by weight), skip the item.
- Each flyer item matches at most one key. Skip anything that doesn't fit.

Ingredient list (key | name | unit):
{cat}"""
    listing = "\n".join(f'{i}: {x["name"]} | {x.get("size", "")} | RM {x["price"]} | {x.get("deal", "")}' for i, x in enumerate(items))
    data, usage = deepseek([{"role": "system", "content": system}, {"role": "user", "content": listing}], 4000)
    return data.get("matches") or [], usage


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default=os.path.join(ROOT, ".cache"))
    ap.add_argument("--pages", type=int, default=24)
    ap.add_argument("--from-json", help="skip the download and vision step; use items extracted earlier (for testing)")
    a = ap.parse_args()
    os.makedirs(a.cache, exist_ok=True)
    ing = json.load(open(os.path.join(ROOT, "src", "data", "ingredients.json")))["items"]
    median = json.load(open(os.path.join(ROOT, "public", "data", "prices", "index.json")))["national"]["median"]

    if a.from_json:
        src = json.load(open(a.from_json))
        items, pdf_url, book_url, vfrom, vto, usage = src["items"], src.get("url", ""), src.get("page", ""), src.get("valid_from"), src.get("valid_to"), {}
    else:
        if not os.environ.get("DEEPSEEK_API_KEY"):
            raise SystemExit("econsave: DEEPSEEK_API_KEY isn't set")
        pdf_url, book_url = find_pdf()
        pdf_dir = tempfile.mkdtemp(prefix="econsave-")   # downloaded file in its own empty folder
        pdf_path = os.path.join(pdf_dir, "catalogue.pdf")
        open(pdf_path, "wb").write(get(pdf_url, binary=True))
        vfrom, vto = validity(pdf_path)
        print(f"econsave: {pdf_url} valid {vfrom} to {vto}")
        items, usage = read_pages(pdf_path, a.pages, a.cache)
        json.dump({"items": items, "url": pdf_url, "page": book_url, "valid_from": vfrom, "valid_to": vto},
                  open(os.path.join(a.cache, "econsave_items.json"), "w"), ensure_ascii=False, indent=1)

    items = [x for x in items if not NON_HALAL.search(str(x.get("name", "")))]
    matches, u2 = match(items, ing) if items else ([], {})
    best = {}
    for mt in matches:
        try:
            i, key, qty = int(mt["i"]), mt["key"], float(mt["qty"])
        except Exception:
            continue
        if key not in ing or not (0 <= i < len(items)) or qty <= 0:
            continue
        x = items[i]
        per = round(float(x["price"]) / qty * ing[key]["size"], 2)
        ref = median.get(key) or ing[key]["fallback"]
        if not (0.3 * ref <= per <= 3 * ref):
            print(f"  dropped {key}: {x['name']} {x.get('size')} RM{x['price']} -> RM{per}/{ing[key]['size']}{ing[key]['unit']} vs typical RM{ref}")
            continue
        if key not in best or per < best[key]["per"]:
            best[key] = {"per": per, "title": x["name"], "size": x.get("size", ""), "price": x["price"], "deal": x.get("deal", ""), "page": x.get("page")}
    for k, v in sorted(best.items()):
        print(f"  {k:<16} RM {v['per']:>6.2f}/{ing[k]['size']}{ing[k]['unit']}  <- {v['title']} {v['size']} RM{v['price']} {v['deal']}")
    tok = {k: usage.get(k, 0) + u2.get(k, 0) for k in ("prompt_tokens", "completion_tokens")}
    out = {"source": "Econsave weekly catalogue, West Malaysia", "url": pdf_url, "page": book_url, "valid_from": vfrom, "valid_to": vto,
           "fetchedAt": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "flyerItems": len(items), "tokens": tok, "items": best}
    if not vto:
        out["warning"] = "No validity dates found on the catalogue; the app ignores it"
    json.dump(out, open(OUT, "w"), ensure_ascii=False, indent=1)
    print(f"econsave: {len(best)} ingredients on promo from {len(items)} flyer items; tokens {tok}")


if __name__ == "__main__":
    main()
