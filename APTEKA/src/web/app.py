"""Flask admin panel + Wazzup webhook + routing.

Admin routes grouped by purpose:
  /                      — KPI dashboard with charts
  /products              — product catalog with advanced filters/sort/paging
  /products/low-stock    — items with stock ≤ threshold
  /products/expiring     — items expiring within N days
  /product/<guid>        — product detail
  /users                 — users list with platform filter
  /user/<chat_id>        — per-user deep view
  /conversations         — list chats
  /conversation/<chat_id>— full AI dialogue
  /analytics             — search trends, zero-results, hour-of-day
  /notifications         — active stock-notification subscriptions
  /favorites             — top favorited products
  /system                — DB / enrichment health + manual action triggers
  /api/...               — JSON endpoints (stats, search, timeline)
"""

import asyncio
import logging
import os
import threading

import requests
from flask import Flask, render_template, jsonify, request, redirect, url_for, flash

from src.db.database import ProductDB, UserDB

logger = logging.getLogger(__name__)


def _forward_payload(url: str, payload: dict, headers: dict):
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=15)
        logger.info("Forwarded to %s: HTTP %d", url, resp.status_code)
    except Exception as e:
        logger.error("Forward to %s failed: %s", url, e)


def create_app(whatsapp_handler=None, webhook_secret: str = "",
               own_channel_id: str = "", forward_map: dict | None = None,
               shared_services: dict | None = None) -> Flask:
    """Create Flask app.

    Args:
        whatsapp_handler: WhatsAppHandler instance (None to disable webhook).
        webhook_secret: Optional secret for webhook validation.
        own_channel_id: This bot's Wazzup channelId.
        forward_map: {channelId: url} for multi-bot routing.
        shared_services: {"cache": InventoryCache, "consultant": PharmacyConsultant,
            "embedding_service": EmbeddingService, "async_loop": loop} — lets the
            admin panel trigger manual refresh/enrichment/embeddings.
    """
    forward_map = forward_map or {}
    shared_services = shared_services or {}

    app = Flask(
        __name__,
        template_folder=str(__file__).replace("app.py", "templates"),
        static_folder=str(__file__).replace("app.py", "static"),
    )
    app.secret_key = "apteka-admin-secret-not-used-for-auth-just-flash"

    product_db = ProductDB()
    user_db = UserDB()

    @app.template_filter("money")
    def _money(value):
        try:
            return f"{float(value or 0):,.0f}".replace(",", " ")
        except (ValueError, TypeError):
            return "0"

    @app.template_filter("ts")
    def _ts(value, width: int = 16):
        if not value:
            return "—"
        return str(value)[:width]

    @app.template_filter("platform_badge")
    def _platform_badge(p: str):
        return {"telegram": "Telegram", "whatsapp": "WhatsApp"}.get(p or "", p or "—")

    # ── Dashboard ───────────────────────────────────────────────────

    @app.route("/")
    def dashboard():
        stats = {
            "total_products": product_db.get_total_count(),
            "in_stock": product_db.get_in_stock_count(),
            "out_of_stock": product_db.get_out_of_stock_count(),
            "prescription": product_db.get_prescription_count(),
            "total_value": product_db.get_total_value(),
            "total_users": user_db.get_total_users(),
            "total_searches": user_db.get_total_searches(),
            "searches_today": user_db.get_searches_today(),
            "searches_7d": user_db.get_searches_since(7),
            "searches_30d": user_db.get_searches_since(30),
        }
        timeline = user_db.get_searches_timeline(days=14)
        platform_split = user_db.get_platform_split()
        popular = user_db.get_popular_queries(limit=10)
        zero_results = user_db.get_zero_result_queries(limit=10)
        recent_activity = user_db.get_recent_activity(limit=15)
        top_fav = user_db.top_favorited_products(limit=5)
        most_wanted = user_db.most_wanted_out_of_stock(limit=5)
        enrichment = product_db.get_enrichment_progress()
        last_refresh = product_db.latest_updated_at()

        return render_template(
            "dashboard.html",
            stats=stats, timeline=timeline, platform_split=platform_split,
            popular=popular, zero_results=zero_results, recent_activity=recent_activity,
            top_fav=top_fav, most_wanted=most_wanted, enrichment=enrichment,
            last_refresh=last_refresh,
        )

    # ── Products ────────────────────────────────────────────────────

    @app.route("/products")
    def products_page():
        q = request.args.get("q", "").strip()
        in_stock = request.args.get("in_stock") == "1"
        rx = request.args.get("rx") == "1"
        otc = request.args.get("otc") == "1"
        producer = request.args.get("producer", "").strip()
        min_p = float(request.args.get("min_price", 0) or 0)
        max_p = float(request.args.get("max_price", 0) or 0)
        sort = request.args.get("sort", "name")
        page = max(1, int(request.args.get("page", 1) or 1))
        per_page = 50
        offset = (page - 1) * per_page

        total = product_db.count_advanced(
            query=q, in_stock_only=in_stock, rx_only=rx, otc_only=otc,
            producer=producer, min_price=min_p, max_price=max_p,
        )
        products = product_db.search_advanced(
            query=q, in_stock_only=in_stock, rx_only=rx, otc_only=otc,
            producer=producer, min_price=min_p, max_price=max_p,
            sort=sort, limit=per_page, offset=offset,
        )
        pages = max(1, (total + per_page - 1) // per_page)
        return render_template(
            "products.html",
            products=products, query=q, total=total, page=page, pages=pages,
            filters={"in_stock": in_stock, "rx": rx, "otc": otc,
                     "producer": producer, "min_price": min_p, "max_price": max_p,
                     "sort": sort},
        )

    @app.route("/products/low-stock")
    def low_stock_page():
        threshold = int(request.args.get("threshold", 3) or 3)
        products = product_db.get_low_stock(threshold=threshold, limit=200)
        return render_template("low_stock.html", products=products, threshold=threshold)

    @app.route("/products/expiring")
    def expiring_page():
        days = int(request.args.get("days", 90) or 90)
        products = product_db.get_expiring_soon(days=days, limit=300)
        return render_template("expiring.html", products=products, days=days)

    @app.route("/product/<guid>")
    def product_detail(guid):
        product = product_db.get_by_guid(guid)
        if not product:
            return "Product not found", 404
        fav_count = user_db.top_favorited_products(limit=1000)
        fav_count = next((f["fav_count"] for f in fav_count if f["guid"] == guid), 0)
        subscribers = [n for n in user_db.most_wanted_out_of_stock(limit=1000)
                       if n["product_guid"] == guid]
        sub_count = subscribers[0]["subscribers"] if subscribers else 0
        return render_template(
            "product_detail.html", product=product,
            fav_count=fav_count, sub_count=sub_count,
        )

    # ── Users ───────────────────────────────────────────────────────

    @app.route("/users")
    def users_page():
        platform = request.args.get("platform", "").strip()
        page = max(1, int(request.args.get("page", 1) or 1))
        per_page = 50
        offset = (page - 1) * per_page
        users = user_db.list_users(platform=platform, limit=per_page, offset=offset)
        total = user_db.count_users(platform=platform)
        pages = max(1, (total + per_page - 1) // per_page)
        return render_template("users.html", users=users, total=total, page=page,
                               pages=pages, platform=platform)

    @app.route("/user/<chat_id>")
    def user_detail(chat_id):
        user = user_db.get_user(chat_id)
        if not user:
            return "User not found", 404
        stats = user_db.get_user_stats(chat_id)
        history = user_db.get_search_history(chat_id, limit=50)
        favorites = user_db.get_favorites(chat_id)
        notifications = user_db.get_user_notifications(chat_id)
        conversation = user_db.full_conversation(chat_id, limit=200)
        return render_template(
            "user_detail.html", user=user, stats=stats, history=history,
            favorites=favorites, notifications=notifications, conversation=conversation,
        )

    # ── Conversations ───────────────────────────────────────────────

    @app.route("/conversations")
    def conversations_page():
        chats = user_db.list_conversations(limit=200)
        return render_template("conversations.html", chats=chats)

    @app.route("/conversation/<chat_id>")
    def conversation_detail(chat_id):
        user = user_db.get_user(chat_id)
        messages = user_db.full_conversation(chat_id, limit=500)
        return render_template("conversation_detail.html",
                               user=user, chat_id=chat_id, messages=messages)

    # ── Analytics ───────────────────────────────────────────────────

    @app.route("/analytics")
    def analytics_page():
        timeline = user_db.get_searches_timeline(days=30)
        hourly = user_db.get_hourly_activity()
        platform_split = user_db.get_platform_split()
        popular = user_db.get_popular_queries(limit=30)
        zero_results = user_db.get_zero_result_queries(limit=30)
        return render_template(
            "analytics.html",
            timeline=timeline, hourly=hourly, platform_split=platform_split,
            popular=popular, zero_results=zero_results,
        )

    # ── Notifications & Favorites ───────────────────────────────────

    @app.route("/notifications")
    def notifications_page():
        active = user_db.all_active_notifications_admin()
        most_wanted = user_db.most_wanted_out_of_stock(limit=20)
        return render_template("notifications.html",
                               active=active, most_wanted=most_wanted)

    @app.route("/favorites")
    def favorites_page():
        top = user_db.top_favorited_products(limit=50)
        return render_template("favorites.html", top=top)

    # ── System ──────────────────────────────────────────────────────

    @app.route("/system")
    def system_page():
        enrichment = product_db.get_enrichment_progress()
        producers = product_db.get_top_producers(limit=15)
        categories = product_db.get_top_categories(limit=15)
        last_refresh = product_db.latest_updated_at()
        db_path = str(__file__).replace("src/web/app.py", "data/apteka.db")
        try:
            db_size = os.path.getsize(db_path)
        except OSError:
            db_size = 0
        return render_template(
            "system.html",
            enrichment=enrichment, producers=producers, categories=categories,
            last_refresh=last_refresh, db_size=db_size,
            has_shared=bool(shared_services),
        )

    def _run_coro(coro):
        """Schedule an async coroutine on the bot's event loop."""
        loop = shared_services.get("async_loop")
        if loop and loop.is_running():
            asyncio.run_coroutine_threadsafe(coro, loop)
            return True
        try:
            coro.close()
        except Exception:
            pass
        return False

    @app.route("/system/refresh-inventory", methods=["POST"])
    def system_refresh():
        cache = shared_services.get("cache")
        if not cache:
            flash("Cache service недоступен", "error")
            return redirect(url_for("system_page"))
        ok = _run_coro(cache.load_full())
        flash("Обновление запущено" if ok else "Не удалось запустить (нет event loop)",
              "ok" if ok else "error")
        return redirect(url_for("system_page"))

    @app.route("/system/enrich", methods=["POST"])
    def system_enrich():
        consultant = shared_services.get("consultant")
        if not consultant:
            flash("Consultant service недоступен", "error")
            return redirect(url_for("system_page"))
        ok = _run_coro(consultant.enrich_products_batch(batch_size=100))
        flash("Обогащение запущено (100 товаров)" if ok else "Не удалось запустить",
              "ok" if ok else "error")
        return redirect(url_for("system_page"))

    @app.route("/system/embeddings", methods=["POST"])
    def system_embeddings():
        emb = shared_services.get("embedding_service")
        if not emb:
            flash("Embedding service недоступен", "error")
            return redirect(url_for("system_page"))
        ok = _run_coro(emb.build_product_embeddings(batch_size=500))
        flash("Построение эмбеддингов запущено" if ok else "Не удалось запустить",
              "ok" if ok else "error")
        return redirect(url_for("system_page"))

    @app.route("/system/rebuild-fts", methods=["POST"])
    def system_rebuild_fts():
        try:
            product_db.rebuild_fts()
            flash("FTS5 индекс пересобран", "ok")
        except Exception as e:
            flash(f"Ошибка: {e}", "error")
        return redirect(url_for("system_page"))

    # ── JSON API ────────────────────────────────────────────────────

    @app.route("/api/stats")
    def api_stats():
        return jsonify({
            "total_products": product_db.get_total_count(),
            "in_stock": product_db.get_in_stock_count(),
            "out_of_stock": product_db.get_out_of_stock_count(),
            "prescription": product_db.get_prescription_count(),
            "total_value": product_db.get_total_value(),
            "total_users": user_db.get_total_users(),
            "total_searches": user_db.get_total_searches(),
            "searches_today": user_db.get_searches_today(),
            "searches_7d": user_db.get_searches_since(7),
            "searches_30d": user_db.get_searches_since(30),
            "last_refresh": product_db.latest_updated_at(),
        })

    @app.route("/api/timeline")
    def api_timeline():
        days = int(request.args.get("days", 14) or 14)
        return jsonify(user_db.get_searches_timeline(days=days))

    @app.route("/api/recent")
    def api_recent():
        limit = int(request.args.get("limit", 20) or 20)
        return jsonify(user_db.get_recent_activity(limit=limit))

    @app.route("/api/search")
    def api_search():
        query = request.args.get("q", "")
        if not query:
            return jsonify({"error": "query required"}), 400
        products = product_db.search_by_name(query, limit=20)
        return jsonify({"results": products, "count": len(products)})

    # ── Wazzup webhook (unchanged routing) ──────────────────────────

    if whatsapp_handler is not None or forward_map:
        @app.route("/wazzup/webhook", methods=["POST", "GET"])
        def wazzup_webhook():
            if request.method == "GET":
                return "", 200

            if webhook_secret:
                auth = request.headers.get("Authorization", "")
                if auth != f"Bearer {webhook_secret}":
                    logger.warning("Wazzup webhook: invalid auth header")
                    return "", 403

            data = request.get_json(silent=True)
            if not data:
                return "", 200

            messages = data.get("messages", []) or []
            loop = app.config.get("async_loop")

            own_messages: list[dict] = []
            foreign_by_channel: dict[str, list[dict]] = {}
            for msg in messages:
                ch = msg.get("channelId", "")
                if own_channel_id and ch and ch != own_channel_id:
                    foreign_by_channel.setdefault(ch, []).append(msg)
                else:
                    own_messages.append(msg)

            for ch, msgs in foreign_by_channel.items():
                target = forward_map.get(ch)
                if not target:
                    logger.warning("Webhook: no forward target for channel %s, dropping", ch)
                    continue
                fwd_payload = {**data, "messages": msgs}
                fwd_headers = {"Content-Type": "application/json"}
                threading.Thread(
                    target=_forward_payload,
                    args=(target, fwd_payload, fwd_headers),
                    daemon=True,
                ).start()

            if whatsapp_handler is None:
                return "", 200

            for msg in own_messages:
                if (msg.get("status") != "inbound"
                        or msg.get("chatType") != "whatsapp"):
                    continue

                chat_id = msg.get("chatId", "")
                if not chat_id:
                    continue

                contact_name = (msg.get("contact") or {}).get("name", "") or ""
                msg_type = msg.get("type", "text")

                coro = None
                if msg_type == "text":
                    text = (msg.get("text") or "").strip()
                    if not text:
                        continue
                    coro = whatsapp_handler.handle_message(chat_id, text, contact_name)
                elif msg_type in ("image", "picture"):
                    image_url = msg.get("contentUri") or msg.get("content") or msg.get("url", "")
                    if not image_url:
                        continue
                    caption = (msg.get("text") or "").strip()
                    coro = whatsapp_handler.handle_image(
                        chat_id, image_url, caption, contact_name,
                    )
                elif msg_type in ("audio", "voice"):
                    coro = whatsapp_handler.handle_message(
                        chat_id,
                        "Извините, голосовые пока не распознаю. Напишите, пожалуйста, текстом.",
                        contact_name,
                    )
                else:
                    logger.info("Wazzup: skipping unsupported msg type %r", msg_type)
                    continue

                if loop and loop.is_running():
                    asyncio.run_coroutine_threadsafe(coro, loop)
                else:
                    logger.error(
                        "Wazzup webhook: no running async loop; dropping message from %s",
                        chat_id,
                    )
                    try:
                        coro.close()
                    except Exception:
                        pass

            return "", 200

    return app
