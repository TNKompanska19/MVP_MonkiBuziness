"""
MonkiBasket MVP - AI Shopping Assistant
MonkiBuziness 22 BV

Flask backend serving:
  GET  /          → render the chat UI (templates/index.html)
  GET  /pfp.png   → serve the user avatar image
  POST /chat      → talk to the LLM, optionally run a Google Shopping
                    search via SerpAPI, return either a chat reply or
                    AI-scored product results

Secrets are read from environment (or a local .env file). NEVER hardcode
tokens in this file — GitHub's secret-scanning will block any push that
contains a GitHub PAT, and the leaked key has to be revoked.
"""

import json, os, re, urllib.parse, urllib.request
from flask import Flask, render_template, jsonify, request, send_from_directory

app = Flask(__name__)


# ---------------------------------------------------------------------------
# Secret loading.
#
# Order of precedence:
#   1. Real environment variables (best for CI / Vercel / Render).
#   2. A local .env file next to this file (best for dev). Parsed inline so
#      we don't have to add python-dotenv as a dependency — keeps the
#      requirements.txt at a single line.
#   3. Empty string. The app will start but /chat will fail with a clear
#      error pointing at the missing key.
# ---------------------------------------------------------------------------
def _load_local_env():
    """
    Parse a minimal .env file (KEY=VALUE per line) sitting next to app.py
    into os.environ. No-ops if the file doesn't exist.
    """
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(env_path):
        return
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


_load_local_env()
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
SERPAPI_KEY  = os.environ.get("SERPAPI_KEY",  "")


SYSTEM_PROMPT = """You are MonkiBasket, a friendly AI shopping assistant. You help users find products across many online stores (IKEA, Bol.com, CoolBlue, Amazon, Etsy, Wehkamp, etc.).

When the user describes a product they want to buy, respond in this exact JSON format:
{"type": "search", "query": "the best English search query to find this product on Google Shopping", "reply": "a short friendly message 1-2 sentences saying what you are going to search for", "price_max": <number or null>, "price_min": <number or null>}

Extract `price_max` and `price_min` (numbers in EUR) ONLY when the user explicitly states a budget:
 - "under €3"           → price_max: 3,    price_min: null
 - "below 50 euros"     → price_max: 50,   price_min: null
 - "max €25"            → price_max: 25,   price_min: null
 - "between 10 and 30"  → price_max: 30,   price_min: 10
 - "around €20"         → price_max: 25,   price_min: 15   (give ±25% range)
 - "cheap" / "budget"   → price_max: null  (vague, do not invent)
If the user does not state a budget, set both to null. Do NOT guess.

When the user is just chatting (greeting, question, thanks, off-topic), respond in this exact JSON format:
{"type": "chat", "reply": "your friendly conversational reply"}

Always respond with ONLY valid JSON. No markdown, no backticks, no extra text whatsoever.
Be warm, helpful and concise. Never make up products or prices."""


# ---------------------------------------------------------------------------
# Per-store delivery info — keyed by lowercase store name.
#
# Each entry has:
#   country: ISO-2 of the store's primary fulfilment country
#   base:    standard-delivery cost in EUR when shipping within that country
#
# SerpAPI's Google Shopping payload does not include shipping data reliably,
# so we estimate. When the user's ship-to country differs from the store's
# home country we add a cross-border surcharge (different for EU vs non-EU
# — see estimate_delivery_for below). UI surfaces all of this as "est."
# ---------------------------------------------------------------------------
STORE_INFO = {
    "ikea":         {"country": "NL", "base":  5.95},
    "bol.com":      {"country": "NL", "base":  1.99},
    "bol":          {"country": "NL", "base":  1.99},
    "coolblue":     {"country": "NL", "base":  0.00},
    "amazon.nl":    {"country": "NL", "base":  1.99},
    "amazon":       {"country": "NL", "base":  1.99},
    "amazon.de":    {"country": "DE", "base":  0.00},
    "amazon.fr":    {"country": "FR", "base":  0.00},
    "amazon.es":    {"country": "ES", "base":  0.00},
    "amazon.it":    {"country": "IT", "base":  0.00},
    "amazon.co.uk": {"country": "GB", "base":  0.00},
    "wehkamp":      {"country": "NL", "base":  1.95},
    "etsy":         {"country": "US", "base":  4.50},   # marketplace, sellers worldwide
    "westwing":     {"country": "DE", "base":  4.95},
    "westwing.nl":  {"country": "NL", "base":  4.95},
    "h&m home":     {"country": "SE", "base":  3.95},
    "hm home":      {"country": "SE", "base":  3.95},
    "argos":        {"country": "GB", "base":  4.95},   # cross-border to NL adds Brexit costs
    "fonq":         {"country": "NL", "base":  4.95},
    "marktplaats":  {"country": "NL", "base":  0.00},
    "intratuin":    {"country": "NL", "base":  5.95},
    "123planten":   {"country": "NL", "base":  5.95},
    "plantenbron":  {"country": "NL", "base":  5.95},
    "superdecor":   {"country": "ES", "base":  4.95},
    "lumea deco":   {"country": "RO", "base":  6.95},
    "lumea":        {"country": "RO", "base":  6.95},
    "ellos":        {"country": "SE", "base":  5.95},
}

# EU/EEA member states — within this set, no customs duty / no extra VAT
# at the border (intra-EU sales handled by VAT-OSS). Anything else is
# cross-border into EU = customs declaration possible + buyer pays import VAT.
EU_COUNTRIES = {
    "NL","BE","DE","FR","ES","IT","PT","AT","IE","LU","FI","SE","DK","PL",
    "CZ","SK","HU","RO","BG","SI","HR","EE","LV","LT","CY","MT","GR",
    # EEA non-EU: included for shipping treatment though VAT differs
    "NO","IS","LI",
}
DEFAULT_BASE_DELIVERY = 4.95

# Cross-border surcharge in EUR added on top of the store's base cost.
# Numbers are realistic baselines for a small/medium parcel.
CROSS_BORDER_EU      = 4.00     # EU → EU
CROSS_BORDER_NON_EU  = 12.00    # crossing the customs border


def _lookup_store_info(store_name):
    """Resolve a SerpAPI `source` to a STORE_INFO entry (case-insensitive, contains-match)."""
    if not store_name:
        return None
    key = store_name.lower().strip()
    if key in STORE_INFO:
        return STORE_INFO[key]
    for known, info in STORE_INFO.items():
        if known in key or key in known:
            return info
    return None


def estimate_delivery_for(store_name, ship_to_country="NL"):
    """
    Estimate delivery cost (EUR) and VAT-risk flag for a given store given
    the user's chosen ship-to country.

    @return: dict with `cost` (float), `vat_risk` (bool), `cross_border` (bool)
    """
    info = _lookup_store_info(store_name)
    if info is None:
        # Unknown store — assume domestic at the default rate.
        return {"cost": DEFAULT_BASE_DELIVERY, "vat_risk": False, "cross_border": False}

    store_country = info["country"]
    base          = info["base"]

    if store_country == ship_to_country:
        return {"cost": base, "vat_risk": False, "cross_border": False}

    # Cross-border. EU↔EU is free of customs; anything that crosses the EU
    # external border can trigger VAT/duty on the buyer side.
    store_in_eu = store_country in EU_COUNTRIES
    ship_in_eu  = ship_to_country in EU_COUNTRIES
    if store_in_eu and ship_in_eu:
        return {"cost": base + CROSS_BORDER_EU, "vat_risk": False, "cross_border": True}
    return {"cost": base + CROSS_BORDER_NON_EU, "vat_risk": True, "cross_border": True}


# Backwards-compatible shim used by older callers that don't pass ship_to.
def estimate_delivery(store_name):
    return estimate_delivery_for(store_name, "NL")["cost"]


def ai_chat(messages):
    """
    Call GitHub Models' free gpt-4o-mini endpoint. Returns the assistant's
    raw text reply. Raises on HTTP error or missing token.
    """
    if not GITHUB_TOKEN:
        raise RuntimeError("GITHUB_TOKEN not set — add it to monkibasket/.env")
    url = "https://models.inference.ai.azure.com/chat/completions"
    payload = json.dumps({
        "model": "gpt-4o-mini",
        "messages": messages,
        "max_tokens": 300,
        "temperature": 0.4,
    }).encode()
    req = urllib.request.Request(url, data=payload, headers={
        "Content-Type": "application/json",
        "Authorization": "Bearer " + GITHUB_TOKEN,
    })
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read())
    return data["choices"][0]["message"]["content"].strip()


def parse_ai_response(raw):
    """Pull a JSON object out of the model's reply, tolerating ```json fences."""
    raw = raw.strip()
    if "```" in raw:
        raw = raw.replace("```json", "").replace("```", "").strip()
    start = raw.find("{")
    end   = raw.rfind("}") + 1
    if start == -1 or end == 0:
        raise ValueError("No JSON found in: " + raw[:100])
    return json.loads(raw[start:end])


def _delivery_for_item(item, ship_to="NL"):
    """
    Pick delivery info for one SerpAPI shopping result.

    Order:
      1. If SerpAPI returns a `delivery` string with a number → parse it.
      2. If it says 'free' explicitly → cost=0.
      3. Otherwise → fall back to estimate_delivery_for(store, ship_to).

    @return: (cost: float, estimated: bool, vat_risk: bool, cross_border: bool)
    """
    delivery_raw = (item.get("delivery", "") or "").strip()
    if delivery_raw:
        if "free" in delivery_raw.lower():
            return 0.0, False, False, False
        nums = re.findall(r"\d+[\.,]?\d*", delivery_raw)
        if nums:
            try:
                return float(nums[0].replace(",", ".")), False, False, False
            except ValueError:
                pass
    # Fallback: per-store + ship-to estimate.
    est = estimate_delivery_for(item.get("source", ""), ship_to)
    return est["cost"], True, est["vat_risk"], est["cross_border"]


def serpapi_search(query, num=5, price_min=None, price_max=None, ship_to="NL"):
    """
    Run a Google Shopping search via SerpAPI and normalize the results.

    Returns a list of product dicts with deterministic ids `r0..rN`. The
    `match` field is a placeholder here, overwritten by `ai_score_products`
    before the response is returned to the UI.

    @param query:     Search query (already AI-refined).
    @param num:       Max number of products to fetch.
    @param price_min: Optional lower bound in EUR. Used in SerpAPI's tbs filter.
    @param price_max: Optional upper bound in EUR. Same.
    """
    if not SERPAPI_KEY:
        raise RuntimeError("SERPAPI_KEY not set — add it to monkibasket/.env")

    # SerpAPI / Google Shopping price filter via the `tbs` parameter:
    #   mr:1          → multi-row results
    #   price:1       → enable price filtering
    #   ppr_min:X     → minimum price
    #   ppr_max:Y     → maximum price
    # We fetch 2× num so we have room to drop bad matches later.
    tbs_parts = []
    if price_min is not None or price_max is not None:
        tbs_parts.append("mr:1")
        tbs_parts.append("price:1")
        if price_min is not None:
            tbs_parts.append(f"ppr_min:{price_min}")
        if price_max is not None:
            tbs_parts.append(f"ppr_max:{price_max}")

    qs = {
        "engine":  "google_shopping",
        "q":       query,
        "api_key": SERPAPI_KEY,
        "gl":      "nl",
        "hl":      "en",
        "num":     max(num, 10),
    }
    if tbs_parts:
        qs["tbs"] = ",".join(tbs_parts)

    params = urllib.parse.urlencode(qs)
    url = "https://serpapi.com/search?" + params
    req = urllib.request.Request(url, headers={"User-Agent": "MonkiBasket/1.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read())

    raw_items = data.get("shopping_results", [])

    # Defensive post-filter: Google sometimes ignores ppr_max for marketplace
    # items, so drop anything significantly over budget here too. We allow
    # 10% over the cap (sales / VAT roundings).
    if price_max is not None:
        cap = price_max * 1.10
        raw_items = [it for it in raw_items if (
            (it.get("extracted_price") or 0) <= cap
        )]
    if price_min is not None:
        floor = price_min * 0.90
        raw_items = [it for it in raw_items if (
            (it.get("extracted_price") or float("inf")) >= floor
        )]

    products = []
    for i, item in enumerate(raw_items[:num]):
        raw_price = item.get("price", "0")
        price_str = raw_price.replace("€", "").replace("$", "").replace(",", ".").strip().split()[0]
        try:
            price = float(price_str)
        except ValueError:
            price = 0.0

        store = item.get("source", "Online store")
        delivery, est, vat_risk, cross_border = _delivery_for_item(item, ship_to)

        products.append({
            "id":                 "r" + str(i),
            "name":               item.get("title", "Unknown product"),
            "store":              store,
            "img":                item.get("thumbnail", ""),
            "price":              price,
            "delivery":           delivery,
            "delivery_estimated": est,
            "vat_risk":           vat_risk,
            "cross_border":       cross_border,
            "total":              round(price + delivery, 2),
            "rating":             float(item.get("rating", 0)) or None,
            "reviews":            item.get("reviews"),
            "url":                item.get("product_link") or item.get("link", "#"),
            "match":              max(60, 98 - i * 7),  # placeholder; AI overwrites
        })
    return products


def ai_score_products(user_query, products):
    """
    Ask the LLM to score each product 30–100 on how well it matches the
    user's stated criteria (material, color, size, brand, style, price).

    Floor at 30 reflects: "the product is in the right category (Google
    Shopping already filtered by query) but weakly matches attributes".
    Scoring 0% in the UI looks broken — 30% is honest and reads better.
    """
    if not products:
        return products

    title_list = [
        {"i": i, "title": p["name"], "store": p["store"], "price": p["price"]}
        for i, p in enumerate(products)
    ]
    score_system = (
        "You score how well products match a user's shopping request. "
        "Use a 30–100 scale (NEVER below 30):\n"
        " • 90–100: matches all stated constraints (material, color, size, "
        "price, style).\n"
        " • 70–89: matches most constraints, misses one minor detail.\n"
        " • 50–69: right category, matches some constraints but misses size "
        "or material.\n"
        " • 30–49: right category but mismatches the main constraint.\n"
        "Output ONLY a JSON array of integers."
    )
    score_prompt = (
        f'User shopping request: "{user_query}"\n\n'
        f"Products to score:\n{json.dumps(title_list, ensure_ascii=False)}\n\n"
        "Return ONLY a JSON array of integer scores in the same order, "
        "e.g. [92, 78, 64, 51, 42]. No prose, no markdown."
    )
    try:
        raw = ai_chat([
            {"role": "system", "content": score_system},
            {"role": "user",   "content": score_prompt},
        ]).strip()
        if "```" in raw:
            raw = raw.replace("```json", "").replace("```", "").strip()
        start = raw.find("[")
        end   = raw.rfind("]") + 1
        if start == -1 or end == 0:
            return products
        scores = json.loads(raw[start:end])
        for i, p in enumerate(products):
            if i < len(scores) and isinstance(scores[i], (int, float)):
                p["match"] = max(30, min(100, int(scores[i])))
    except Exception as e:
        print("Score error (keeping placeholder scores):", e)
    return products


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/pfp.png")
def pfp():
    """Serve the user-avatar image used in the sidebar and chat bubbles."""
    folder = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
    return send_from_directory(folder, "pfp.png")


@app.route("/chat", methods=["POST"])
def chat():
    body      = request.get_json(force=True)
    user_text = (body.get("message") or "").strip()
    history   = body.get("history") or []
    # Ship-to country: 2-letter ISO code from the UI dropdown. Defaults to NL
    # for backwards compatibility with older clients that don't send it.
    ship_to   = (body.get("ship_to") or "NL").upper().strip()[:2]

    if not user_text:
        return jsonify({"type": "chat", "intro": "What can I help you find?"})

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    for h in history[-6:]:
        messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": user_text})

    try:
        raw     = ai_chat(messages)
        ai_data = parse_ai_response(raw)
        print("AI OK:", ai_data)
    except Exception as e:
        print("AI error:", e)
        return jsonify({"type": "chat",
                        "intro": "Sorry, I had a hiccup. Could you rephrase that?"})

    reply = ai_data.get("reply", "")

    if ai_data.get("type") != "search":
        return jsonify({"type": "chat", "intro": reply})

    search_query = ai_data.get("query", user_text)
    price_max   = ai_data.get("price_max")
    price_min   = ai_data.get("price_min")
    # Defensive: the LLM might return strings like "25" — coerce or drop.
    def _to_num(v):
        if v is None: return None
        try: return float(v)
        except (TypeError, ValueError): return None
    price_max = _to_num(price_max)
    price_min = _to_num(price_min)

    try:
        products = serpapi_search(search_query, num=6,
                                   price_min=price_min, price_max=price_max,
                                   ship_to=ship_to)
        if not products:
            # Build a more useful "no results" message when a budget caused
            # the filter to drop everything — invites the user to relax it.
            if price_max is not None:
                msg = f" I couldn't find matches under €{price_max:g}. Try raising the budget or relaxing other constraints."
            else:
                msg = " I couldn't find results — try rephrasing."
            return jsonify({"type": "chat", "intro": reply + msg})

        # Score against the ORIGINAL user text (richer than the AI-refined
        # query — preserves price caps, style words, etc).
        products = ai_score_products(user_text, products)
        # Re-rank: highest match first, cheaper total as tie-breaker.
        products.sort(key=lambda p: (-p["match"], p["total"]))

        # Surface the constraint we applied so the UI can show it as a chip.
        applied = []
        if price_max is not None: applied.append(f"≤ €{price_max:g}")
        if price_min is not None: applied.append(f"≥ €{price_min:g}")
        intro = reply
        if applied:
            intro += f" Filtered to: {' & '.join(applied)}."

        return jsonify({
            "type":      "results",
            "intro":     intro,
            "results":   products,
            "category":  search_query,
            "price_max": price_max,
            "price_min": price_min,
        })
    except Exception as e:
        print("SerpAPI error:", e)
        return jsonify({"type": "chat",
                        "intro": reply + " (Search unavailable right now, try again.)"})


if __name__ == "__main__":
    print("GitHub token set:", bool(GITHUB_TOKEN))
    print("SerpAPI key set:",  bool(SERPAPI_KEY))
    app.run(debug=True, port=5000)
