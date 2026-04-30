# APTEKA - Telegram AI Pharmacy Consultant Bot

## Project Overview
Telegram chatbot for pharmacy consultation powered by OpenAI GPT + RUBUS API inventory.
The bot helps customers find medications, check availability, identify prescription drugs, suggest analogs.
Includes Flask admin panel for business analytics.

## Architecture
```
main.py                  - Entry point: runs Telegram bot + Flask admin
src/
  config.py              - Environment-based configuration
  db/
    database.py          - SQLite layer: products, users, history, favorites, notifications
  api/
    rubus_client.py      - Async RUBUS API client (checkToken, getRest, getRestOnTime)
  cache/
    inventory.py         - Loads RUBUS → SQLite, periodic refresh via getRestOnTime
  ai/
    consultant.py        - OpenAI pharmacy consultant: intent extraction, multi-strategy search, analog finder, product enrichment
    embeddings.py        - OpenAI embeddings for semantic search (text-embedding-3-small)
  bot/
    handlers.py          - Telegram handlers: /start, /clear, /favorites, /notifications, inline callbacks
    keyboards.py         - Inline keyboards: product list, detail view, favorites, notifications
  models/
    product.py           - Product dataclass (legacy, DB uses dicts now)
  web/
    app.py               - Flask admin panel routes
    templates/           - Jinja2 templates (dashboard, products, users, history)
    static/css/          - Admin panel CSS
data/
  apteka.db              - SQLite database (auto-created)
```

## Key Design Decisions
- **SQLite as central store**: Products cached from RUBUS API into SQLite for fast queries
- **Multi-strategy search**: DB text search → barcode → active ingredient → semantic embeddings
- **AI enrichment**: OpenAI extracts active ingredients and therapeutic categories from product names
- **Embeddings**: text-embedding-3-small vectors stored in SQLite for semantic similarity search
- **Intent extraction**: OpenAI classifies user intent (search, analog, symptom, greeting) before search
- **Analog matching**: By active ingredient → by therapeutic category → by embedding similarity
- **Notifications**: Users subscribe to out-of-stock products, notified when available
- **Flask admin**: Runs in daemon thread alongside Telegram bot

## RUBUS API
- Base URL: `POST http://rubus.kz/node1/rapi/<method>`
- Auth: token + device + bin (see .env)
- Methods: `checkToken`, `getRest`, `getRestOnTime`

## Tech Stack
- Python 3.13, SQLite, python-telegram-bot, openai, aiohttp, Flask, numpy

## Environment Variables (.env)
- TELEGRAM_BOT_TOKEN, OPENAI_API_KEY
- RUBUS_TOKEN, RUBUS_DEVICE, RUBUS_BIN

## Rules
- All data comes from RUBUS API → SQLite. Never invent products.
- Prescription detection via "рецептурн" in product groups field.
- User-facing text in Russian. No markdown in bot responses.
- Never expose API credentials in bot output.
