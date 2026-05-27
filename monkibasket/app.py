"""
MonkiBasket MVP - AI Shopping Assistant
MonkiBuziness 22 BV
"""

from flask import Flask, render_template, jsonify, request
import json, urllib.parse, urllib.request

app = Flask(__name__)

GITHUB_TOKEN = "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"
SERPAPI_KEY  = "fd5deb2450c242ba6ff87d990815241b879a378f3ac6cea793de41fc4e859895"

SYSTEM_PROMPT = """You are MonkiBasket, a friendly AI shopping assistant. You help users find products across many online stores (IKEA, Bol.com, CoolBlue, Amazon, Etsy, Wehkamp, etc.).

When the user describes a product they want to buy, respond in this exact JSON format:
{"type": "search", "query": "the best English search query to find this product on Google Shopping", "reply": "a short friendly message 1-2 sentences saying what you are going to search for"}

When the user is just chatting (greeting, question, thanks, off-topic), respond in this exact JSON format:
{"type": "chat", "reply": "your friendly conversational reply"}

Always respond with ONLY valid JSON. No markdown, no backticks, no extra text whatsoever.
Be warm, helpful and concise. Never make up products or prices."""


def ai_chat(messages):
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
    raw = raw.strip()
    if "```" in raw:
        raw = raw.replace("```json", "").replace("```", "").strip()
    start = raw.find("{")
    end   = raw.rfind("}") + 1
    if start == -1 or end == 0:
        raise ValueError("No JSON found in: " + raw[:100])
    return json.loads(raw[start:end])


def serpapi_search(query, num=5):
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
        price_str = raw_price.replace("€","").replace("$","").replace(",",".").strip().split()[0]
        try:
            price = float(price_str)
        except ValueError:
            price = 0.0

        products.append({
            "id":       "r" + str(i),
            "name":     item.get("title", "Unknown product"),
            "store":    item.get("source", "Online store"),
            "img":      item.get("thumbnail", ""),
            "price":    price,
            "delivery": 0.0,
            "total":    price,
            "material": (item.get("snippet") or "")[:60],
            "dims":     "",
            "rating":   float(item.get("rating", 0)) or None,
            "note":     item.get("delivery", "") or "",
            "url":      item.get("link") or item.get("product_link", "#"),
            "match":    max(60, 98 - i * 7),
        })
    return products


@app.route("/")
def index():
    return render_template("index.html")


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
    print("SerpAPI key set:", bool(SERPAPI_KEY))
    app.run(debug=True, port=5000)