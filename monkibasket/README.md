# MonkiBasket MVP
### AI-Powered Shopping Assistant — MonkiBuziness 22 BV

A chat-style AI shopping assistant. Describe what you want in plain language; it
searches across many webshops, ranks the best price/quality matches with real
product photos, links to the actual stores, and builds one unified basket so you
check out everything in a single click.

---

## How to run (zero setup, zero cost)

```
pip install flask
python app.py
```
Open: http://localhost:5000

That's it — no API key, no accounts, no cost. Just make sure you're online so
the product photos load.

---

## Features

- **Chat interface** — type naturally; saved chat history in the left sidebar
- **Real chat behaviour** — if you type something that isn't a product search
  (e.g. "hi", "who are you", "tell me a joke") it replies conversationally, just
  like a real assistant. Only product requests trigger a search.
- **Searches 12 webshops** — IKEA, Bol.com, CoolBlue, Wehkamp, Etsy, Ellos,
  H&M Home, Argos, Amazon.nl, Marktplaats, Westwing, fonQ
- **Real product photos** on every result and basket item
- **Clickable links** — each product opens that store's real search page
- **Unified basket (right panel)** — items grouped by store, with photos,
  subtotal, per-store delivery, grand total
- **Batch checkout** — one button to order across all stores

---

## The 5 P's (for your video)

| P | Where you see it |
|---|---|
| **Problem** | Searching 12 shops manually is slow — solved in one query |
| **Promise** | "I searched 12 webshops and found your best matches" |
| **People** | Budget-conscious NL shoppers 18–40 (footer: free vs €4.99 Pro) |
| **Price** | Free + €4.99/mo Pro — below Amazon Prime |
| **Packaging** | Chat UI + saved history + unified basket with photos |

---

## Suggested 5-min demo flow

1. Type "hi" → it replies conversationally (shows it's a real assistant)
2. Click a suggestion chip or type a detailed product request
3. Watch it "search" and return ranked cards with photos
4. Click a product link → real IKEA/Bol/Amazon page opens
5. Add the best match → appears in the basket with its photo
6. Search again, add from a different store
7. Basket groups items by store, one total, one batch-checkout button
8. Point to the sidebar — every search saved as chat history

---

## A note for your report / Q&A

This MVP demonstrates the full user experience — the AI chat, cross-store
search, ranking, and unified basket — using a curated demo catalog with real
product photos. In a funded build, the demo catalog is replaced by a live
product-data API (e.g. SerpAPI's Google Shopping endpoint, free for 100
searches/month) so results come from real-time store inventory. That two-stage
approach — prove the experience first, plug in live data later — is exactly how
an early-stage MVP is meant to work.

---

## File structure

```
monkibasket/
├── app.py            ← Flask backend: stores, catalog w/ images, chat + search logic
├── requirements.txt
└── templates/
    └── index.html    ← 3-panel chat UI (sidebar · chat · basket)
```

---

## Roadmap (post-MVP slide)

- Live product-data API instead of the demo catalog
- AR visualizer — overlay the product onto a photo of your room
- Real batch checkout via retailer APIs
- User accounts + persistent saved baskets
