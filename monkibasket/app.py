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
{"type": "search", "query": "the best English search query to find this product on Google Shopping", "reply": "a short friendly message 1-2 sentences saying what you are going to search for"}

When the user is just chatting (greeting, question, thanks, off-topic), respond in this exact JSON format:
{"type": "chat", "reply": "your friendly conversational reply"}

Always respond with ONLY valid JSON. No markdown, no backticks, no extra text whatsoever.
Be warm, helpful and concise. Never make up products or prices."""


# ---------------------------------------------------------------------------
# Per-store delivery estimates (Netherlands, EUR).
#
# SerpAPI's Google Shopping engine does NOT reliably return shipping info in
# its `shopping_results` payload (the `delivery` field is absent for most
# items). The previous implementation either hardcoded delivery = 0.0 or
# regex-parsed an empty string — both produced "✓ free delivery" on every
# product, which is misleading and undermines the cross-store comparison
# that is core to MonkiBasket's value prop.
#
# Strategy: try the regex first (in case SerpAPI ever does return a string),
# fall back to a per-store baseline estimate. Surface "est." in the UI so
# the user knows it's a baseline.
# ---------------------------------------------------------------------------
STORE_DELIVERY = {
    "ikea":         5.95,
    "bol.com":      1.99,
    "bol":          1.99,
    "coolblue":     0.00,
    "amazon.nl":    1.99,
    "amazon":       1.99,
    "amazon.de":    9.99,
    "wehkamp":      1.95,
    "etsy":         4.50,
    "westwing":     4.95,
    "westwing.nl":  4.95,
    "h&m home":     3.95,
    "hm home":      3.95,
    "argos":       15.00,
    "fonq":         4.95,
    "marktplaats":  0.00,
    "intratuin":    5.95,
    "123planten":   5.95,
    "plantenbron":  5.95,
    "superdecor":   4.95,
    "lumea deco":   6.95,
    "lumea":        6.95,
    "ellos":        5.95,
}
DEFAULT_DELIVERY = 4.95


def estimate_delivery(store_name):
    """
    Return a typical NL delivery cost (EUR) for a given store name.

    Lookup strategy:
      1. Exact case-insensitive match on STORE_DELIVERY keys.
      2. Substring match (either direction) so "Amazon NL" matches "amazon.nl".
      3. DEFAULT_DELIVERY otherwise.
    """
    if not store_name:
        return DEFAULT_DELIVERY
    key = store_name.lower().strip()
    if key in STORE_DELIVERY:
        return STORE_DELIVERY[key]
    for known, cost in STORE_DELIVERY.items():
        if known in key or key in known:
            return cost
    return DEFAULT_DELIVERY


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


def _delivery_for_item(item):
    """
    Pick a delivery value for one SerpAPI shopping result. Order:
      1. If SerpAPI returns a `delivery` string with a number → parse it.
      2. If it says 'free' explicitly → 0.0.
      3. Otherwise → fall back to per-store estimate (STORE_DELIVERY).
    """
    delivery_raw = (item.get("delivery", "") or "").strip()
    if delivery_raw:
        if "free" in delivery_raw.lower():
            return 0.0
        nums = re.findall(r"\d+[\.,]?\d*", delivery_raw)
        if nums:
            try:
                return float(nums[0].replace(",", "."))
            except ValueError:
                pass
    # Fallback: per-store estimate. Better than lying about free shipping.
    return estimate_delivery(item.get("source", ""))


def serpapi_search(query, num=5):
    """
    Run a Google Shopping search via SerpAPI and normalize the results.

    Returns a list of product dicts with deterministic ids `r0..rN`. The
    `match` field is a placeholder here, overwritten by `ai_score_products`
    before the response is returned to the UI.
    """
    if not SERPAPI_KEY:
        raise RuntimeError("SERPAPI_KEY not set — add it to monkibasket/.env")
    params = urllib.parse.urlencode({
        "engine":  "google_shopping",
        "q":       query,
        "api_key": SERPAPI_KEY,
        "gl":      "nl",
        "hl":      "en",
        "num":     num,
    })
    url = "https://serpapi.com/search?" + params
    req = urllib.request.Request(url, headers={"User-Agent": "MonkiBasket/1.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read())

    products = []
    for i, item in enumerate(data.get("shopping_results", [])[:num]):
        raw_price = item.get("price", "0")
        price_str = raw_price.replace("€", "").replace("$", "").replace(",", ".").strip().split()[0]
        try:
            price = float(price_str)
        except ValueError:
            price = 0.0

        store    = item.get("source", "Online store")
        delivery = _delivery_for_item(item)
        # If SerpAPI gave us a real string we used regex; otherwise the cost
        # came from STORE_DELIVERY — mark the row so the UI can show "est."
        delivery_estimated = not bool(item.get("delivery"))

        products.append({
            "id":                 "r" + str(i),
            "name":               item.get("title", "Unknown product"),
            "store":              store,
            "img":                item.get("thumbnail", ""),
            "price":              price,
            "delivery":           delivery,
            "delivery_estimated": delivery_estimated,
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
    try:
        products = serpapi_search(search_query, num=5)
        if not products:
            return jsonify({"type": "chat",
                            "intro": reply + " I couldn't find results — try rephrasing."})

        # Score against the ORIGINAL user text (richer than the AI-refined
        # query — preserves price caps, style words, etc).
        products = ai_score_products(user_text, products)
        # Re-rank: highest match first, cheaper total as tie-breaker.
        products.sort(key=lambda p: (-p["match"], p["total"]))

        return jsonify({
            "type":     "results",
            "intro":    reply,
            "results":  products,
            "category": search_query,
        })
    except Exception as e:
        print("SerpAPI error:", e)
        return jsonify({"type": "chat",
                        "intro": reply + " (Search unavailable right now, try again.)"})


if __name__ == "__main__":
    print("GitHub token set:", bool(GITHUB_TOKEN))
    print("SerpAPI key set:",  bool(SERPAPI_KEY))
    app.run(debug=True, port=5000)
