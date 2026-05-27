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
{"type": "search", "query": "the best English search query to find this product on Google Shopping", "reply": "a short friendly message 1-2 sentences saying what you are going to search for", "price_max": <number or null>, "price_min": <number or null>, "dim_width_cm": {"min": <num or null>, "max": <num or null>} or null, "dim_height_cm": {"min": <num or null>, "max": <num or null>} or null, "dim_depth_cm": {"min": <num or null>, "max": <num or null>} or null}

Extract `price_max` and `price_min` (numbers in EUR) ONLY when the user explicitly states a budget:
 - "under €3"           → price_max: 3,    price_min: null
 - "below 50 euros"     → price_max: 50,   price_min: null
 - "max €25"            → price_max: 25,   price_min: null
 - "between 10 and 30"  → price_max: 30,   price_min: 10
 - "around €20"         → price_max: 25,   price_min: 15   (give ±25% range)
 - "cheap" / "budget"   → price_max: null  (vague, do not invent)
If the user does not state a budget, set both to null. Do NOT guess.

Extract dimension constraints (numbers in cm) ONLY when the user gives specific sizes:
 - "40cm wide"             → dim_width_cm: {min: 36, max: 44}            (±10%)
 - "max 60cm wide"         → dim_width_cm: {min: null, max: 60}
 - "between 30 and 50cm"   → dim_width_cm: {min: 30, max: 50}
 - "around 20cm"           → dim_width_cm: {min: 17, max: 23}            (±15%)
 - "Ø14cm"                 → dim_width_cm: {min: 13, max: 15}            (diameter ≈ width)
 - "60x30x130"             → dim_width_cm:{min:55,max:65}, dim_depth_cm:{min:27,max:33}, dim_height_cm:{min:120,max:140}
 - "180cm tall"            → dim_height_cm: {min:170, max:190}
 - "fits a 50cm niche"     → dim_width_cm: {min: null, max: 50}
If the user does not give a size, set the field to null entirely (NOT {min:null,max:null} — use null).
Width = horizontal, Height = vertical/tall, Depth = front-to-back.

When the user is just chatting (greeting, question, thanks, off-topic), respond in this exact JSON format:
{"type": "chat", "reply": "your friendly conversational reply"}

Always respond with ONLY valid JSON. No markdown, no backticks, no extra text whatsoever.
Be warm, helpful and concise. Never make up products or prices."""


# ---------------------------------------------------------------------------
# Dimension extraction from product titles.
# SerpAPI doesn't return structured dimensions, but many product titles
# include them ("60x30x130 cm", "Ø14cm", "140x260cm", "120cm wide"). We
# extract whatever we can find with regex so we can filter by the user's
# stated dimension constraints. When a product has no detectable dimensions
# we leave it in — uncertainty is not penalized.
# ---------------------------------------------------------------------------
def extract_title_dimensions(title):
    """
    Try to pull width/height/depth (cm) out of a product title.
    Returns a dict {width, height, depth} where each may be a float or None.
    """
    if not title:
        return {"width": None, "height": None, "depth": None}
    t = title.lower()

    # "WxDxH cm" / "WxH cm" — pick out 2-3 numbers around an 'x'.
    m = re.search(r"(\d{2,4})\s*[x×]\s*(\d{2,4})(?:\s*[x×]\s*(\d{2,4}))?\s*cm", t)
    if m:
        a, b, c = m.group(1), m.group(2), m.group(3)
        if c:
            # Three numbers: width × depth × height
            return {"width": float(a), "depth": float(b), "height": float(c)}
        # Two numbers: width × height (typical for curtains / wall items)
        return {"width": float(a), "height": float(b), "depth": None}

    # "D14 x H13" — explicit labels (Dutch product listings often use this).
    m = re.search(r"d\s*(\d{1,3})\s*[x×]\s*h\s*(\d{1,3})", t)
    if m:
        return {"width": float(m.group(1)), "height": float(m.group(2)), "depth": None}

    # "Ø14cm" / "Ø14 cm" — diameter, used as width for cylindrical items.
    m = re.search(r"[øØ⌀]\s*(\d{1,3})\s*cm", t)
    if m:
        return {"width": float(m.group(1)), "height": None, "depth": None}

    # "120cm wide" / "30cm hoog" / "40cm tall"
    m = re.search(r"(\d{1,3})\s*cm\s*(wide|breed|width)", t)
    width = float(m.group(1)) if m else None
    m = re.search(r"(\d{1,3})\s*cm\s*(tall|hoog|height|hoogte)", t)
    height = float(m.group(1)) if m else None
    m = re.search(r"(\d{1,3})\s*cm\s*(deep|diep|depth|diepte)", t)
    depth = float(m.group(1)) if m else None
    if any([width, height, depth]):
        return {"width": width, "height": height, "depth": depth}

    return {"width": None, "height": None, "depth": None}


def _in_range(value, rng):
    """
    True if `value` falls within the {min, max} dict (either bound may be None).
    None `value` (no detected dimension) always returns True — uncertainty
    is not penalized; only contradictions are.
    """
    if value is None or rng is None:
        return True
    mn = rng.get("min")
    mx = rng.get("max")
    if mn is not None and value < mn:
        return False
    if mx is not None and value > mx:
        return False
    return True


def filter_by_dimensions(products, dim_width, dim_height, dim_depth):
    """
    Drop products whose title-detected dimensions clearly contradict the
    user's stated constraints. Products without detectable dimensions
    pass through unchanged.

    @return: (kept_products, dropped_count)
    """
    if not any([dim_width, dim_height, dim_depth]):
        return products, 0
    kept = []
    dropped = 0
    for p in products:
        dims = extract_title_dimensions(p.get("name", ""))
        ok = (_in_range(dims["width"],  dim_width)
              and _in_range(dims["height"], dim_height)
              and _in_range(dims["depth"],  dim_depth))
        if ok:
            # Attach what we detected so the UI / debug can show it later.
            p["dims_detected"] = dims
            kept.append(p)
        else:
            dropped += 1
    return kept, dropped


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


def ai_chat(messages, max_tokens=300, temperature=0.4, timeout=20):
    """
    Call GitHub Models' free gpt-4o-mini endpoint. Returns the assistant's
    raw text reply. Raises on HTTP error or missing token.

    `messages` may contain vision content (a list of `{type, text}` /
    `{type, image_url}` parts in any user message) — the model handles it.
    Vision calls just need a higher max_tokens because outputs are richer.
    """
    if not GITHUB_TOKEN:
        raise RuntimeError("GITHUB_TOKEN not set — add it to monkibasket/.env")
    url = "https://models.inference.ai.azure.com/chat/completions"
    payload = json.dumps({
        "model": "gpt-4o-mini",
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }).encode()
    req = urllib.request.Request(url, data=payload, headers={
        "Content-Type": "application/json",
        "Authorization": "Bearer " + GITHUB_TOKEN,
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read())
    return data["choices"][0]["message"]["content"].strip()


def _strip_code_fences(raw):
    """Tolerate ```json fences the model sometimes adds despite being told not to."""
    raw = raw.strip()
    if "```" in raw:
        raw = raw.replace("```json", "").replace("```", "").strip()
    return raw


def analyze_reference_image(image_data_url):
    """
    Send a user-uploaded scene/reference photo to the vision model and
    extract structured shopping context. Used to enrich the search query
    when the user attached an image (e.g. their bathroom niche).

    @param image_data_url: full data URL ("data:image/jpeg;base64,...").
    @return: dict with summary, style_tags, materials, dominant_colors,
             suggested_categories. Empty fields on parse failure.
    """
    system_prompt = (
        "You are an interior-design shopping assistant. Look at the user's "
        "reference image and describe it in terms that help a product search. "
        "Be specific about style words shoppers use (minimalist, scandinavian, "
        "industrial, mid-century, etc.), visible materials, dominant colors, "
        "and product categories that would suit the scene. "
        "Output ONLY a JSON object — no markdown, no prose."
    )
    user_parts = [
        {"type": "text", "text":
            "Analyze this scene as a shopping reference. Return JSON exactly:\n"
            "{\n"
            '  "summary": "<1 sentence>",\n'
            '  "style_tags": ["minimalist", "scandinavian"],\n'
            '  "materials": ["white tile", "oak wood"],\n'
            '  "dominant_colors": ["#F5F5F0", "#3A3A3A"],\n'
            '  "suggested_categories": ["bathroom cabinet", "wall shelf"]\n'
            "}"
        },
        {"type": "image_url", "image_url": {"url": image_data_url, "detail": "high"}},
    ]
    try:
        raw = ai_chat(
            [{"role": "system", "content": system_prompt},
             {"role": "user",   "content": user_parts}],
            max_tokens=400, temperature=0.3, timeout=30,
        )
        data = json.loads(_strip_code_fences(raw))
        return {
            "summary":              str(data.get("summary", ""))[:240],
            "style_tags":           [str(x) for x in (data.get("style_tags") or [])][:6],
            "materials":            [str(x) for x in (data.get("materials") or [])][:6],
            "dominant_colors":      [str(x) for x in (data.get("dominant_colors") or [])][:6],
            "suggested_categories": [str(x) for x in (data.get("suggested_categories") or [])][:4],
        }
    except Exception as e:
        print("analyze_reference_image error:", e)
        return {"summary":"", "style_tags":[], "materials":[],
                "dominant_colors":[], "suggested_categories":[]}


def score_aesthetic_match(image_data_url, products, user_query, top_k=5):
    """
    For the top-k products, ask the vision model to score how well each one
    matches the reference image's aesthetic. Returns the SAME products list
    with each entry's `match` overwritten by the aesthetic score AND a new
    flag `aesthetic_scored: True` so the UI can label the score correctly.

    Single batched API call (reference photo + up to top_k product thumbnails
    in one message). If it fails, products are returned unchanged.

    Why batched: each vision call is ~3-5s. Per-product means 5×5s=25s on
    top of the already-slow search. Batching keeps the user wait reasonable.
    """
    if not products or not image_data_url:
        return products

    candidates = products[:top_k]
    # Build the multimodal user message: reference image, then each product
    # image numbered so the model knows what to return.
    parts = [
        {"type": "text",
         "text": (
            f'User shopping request: "{user_query}"\n\n'
            f"IMAGE 0 below is the user's REFERENCE space/aesthetic.\n"
            f"IMAGES 1–{len(candidates)} are candidate products to score.\n\n"
            "For each candidate (1..N) score how well the product fits the "
            "reference's aesthetic — consider style, materials, colors, "
            "and visual coherence in that space. Use a 30–100 scale:\n"
            " • 90–100: strong match (could be from the same room)\n"
            " • 70–89:  good match with minor differences\n"
            " • 50–69:  acceptable but stylistic mismatch\n"
            " • 30–49:  weak fit\n\n"
            "Return ONLY a JSON array of N integers in candidate order, "
            "e.g. [88, 72, 45, 60, 30]. No prose."
         )},
        {"type": "image_url", "image_url": {"url": image_data_url, "detail": "low"}},
    ]
    for p in candidates:
        if p.get("img"):
            parts.append({"type": "image_url",
                          "image_url": {"url": p["img"], "detail": "low"}})

    try:
        raw = ai_chat(
            [{"role": "system", "content":
                "You score how well each candidate product matches a reference "
                "space's aesthetic. Reply with ONLY a JSON array of integers."},
             {"role": "user", "content": parts}],
            max_tokens=120, temperature=0.3, timeout=45,
        )
        raw = _strip_code_fences(raw)
        start = raw.find("[")
        end   = raw.rfind("]") + 1
        if start == -1 or end == 0:
            return products
        scores = json.loads(raw[start:end])
        for i, p in enumerate(candidates):
            if i < len(scores) and isinstance(scores[i], (int, float)):
                p["match"]             = max(30, min(100, int(scores[i])))
                p["aesthetic_scored"]  = True
    except Exception as e:
        print("score_aesthetic_match error:", e)
    return products


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
    # Optional reference image attached by the user. We accept it as either
    # a full data URL ("data:image/jpeg;base64,...") or raw base64 (with a
    # separate mime). The vision model wants the data-URL form.
    image_data_url = body.get("image_data_url")
    if not image_data_url and body.get("image_base64"):
        mime = body.get("image_mime") or "image/jpeg"
        image_data_url = f"data:{mime};base64,{body['image_base64']}"

    if not user_text and not image_data_url:
        return jsonify({"type": "chat", "intro": "What can I help you find?"})

    # When the user attached a reference photo, analyze it FIRST and fold the
    # scene description into the prompt the LLM uses to generate the search
    # query. This makes searches like "find something that matches" actually
    # work without the user having to describe their room in words.
    scene_hint = ""
    scene = None
    if image_data_url:
        scene = analyze_reference_image(image_data_url)
        if scene and (scene["style_tags"] or scene["materials"] or scene["suggested_categories"]):
            scene_hint = (
                "\n\n[Reference image analysis]\n"
                f"Scene: {scene['summary']}\n"
                f"Style: {', '.join(scene['style_tags'])}\n"
                f"Materials: {', '.join(scene['materials'])}\n"
                f"Suggested categories: {', '.join(scene['suggested_categories'])}\n"
                "Use these to enrich the search query (style words, materials) "
                "but only if relevant to what the user is asking for."
            )

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    for h in history[-6:]:
        messages.append({"role": h["role"], "content": h["content"]})
    composite_user = (user_text or "Find something that matches this image.") + scene_hint
    messages.append({"role": "user", "content": composite_user})

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
    dim_width   = ai_data.get("dim_width_cm")
    dim_height  = ai_data.get("dim_height_cm")
    dim_depth   = ai_data.get("dim_depth_cm")

    # Defensive: the LLM might return strings like "25" — coerce or drop.
    def _to_num(v):
        if v is None: return None
        try: return float(v)
        except (TypeError, ValueError): return None
    price_max = _to_num(price_max)
    price_min = _to_num(price_min)

    try:
        # Fetch extras when we need to filter by dimensions — some will be dropped.
        fetch_n = 10 if any([dim_width, dim_height, dim_depth]) else 6
        products = serpapi_search(search_query, num=fetch_n,
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

        # Dimension filter (only drops products whose titles contain dimensions
        # that contradict the user's stated range — products with no detectable
        # dimensions pass through, since absence ≠ mismatch).
        products, dim_dropped = filter_by_dimensions(products, dim_width, dim_height, dim_depth)
        # Trim to top 6 after filtering so we don't show too many.
        products = products[:6]

        # Score products. With a reference image, use per-product VISION
        # scoring (compares reference vs. each product photo). Without an
        # image, fall back to text-only fit scoring against the original
        # user query. Pass top_k=len so every visible card gets a real
        # aesthetic score (no mixed "fit" / "aesthetic" labels in the UI).
        if image_data_url:
            products = score_aesthetic_match(
                image_data_url, products,
                user_text or "matches this aesthetic",
                top_k=len(products),
            )
        else:
            products = ai_score_products(user_text, products)
        # Re-rank: highest match first, cheaper total as tie-breaker.
        products.sort(key=lambda p: (-p["match"], p["total"]))

        # Surface the constraints we applied so the UI can show them.
        applied = []
        if price_max is not None: applied.append(f"≤ €{price_max:g}")
        if price_min is not None: applied.append(f"≥ €{price_min:g}")
        def _dim_label(rng, axis):
            if not rng: return None
            mn, mx = rng.get("min"), rng.get("max")
            if mn is not None and mx is not None: return f"{axis} {mn:g}–{mx:g}cm"
            if mx is not None:                    return f"{axis} ≤ {mx:g}cm"
            if mn is not None:                    return f"{axis} ≥ {mn:g}cm"
            return None
        for rng, axis in ((dim_width,"width"), (dim_height,"height"), (dim_depth,"depth")):
            lbl = _dim_label(rng, axis)
            if lbl: applied.append(lbl)
        intro = reply
        if applied:
            intro += f" Filtered to: {' · '.join(applied)}."
        if image_data_url and scene and scene["summary"]:
            intro += f" (Matched to your reference photo: {scene['summary'][:120]})"
        if dim_dropped:
            intro += f" Hid {dim_dropped} items that didn't fit your size."

        return jsonify({
            "type":      "results",
            "intro":     intro,
            "results":   products,
            "category":  search_query,
            "price_max": price_max,
            "price_min": price_min,
            "scene":     scene,
        })
    except Exception as e:
        print("SerpAPI error:", e)
        return jsonify({"type": "chat",
                        "intro": reply + " (Search unavailable right now, try again.)"})


if __name__ == "__main__":
    print("GitHub token set:", bool(GITHUB_TOKEN))
    print("SerpAPI key set:",  bool(SERPAPI_KEY))
    app.run(debug=True, port=5000)
