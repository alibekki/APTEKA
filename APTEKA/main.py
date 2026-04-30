import asyncio
import logging
import os
import threading
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters,
)

from src.config import load_config
from src.db.database import init_db, ProductDB, UserDB, WASessionDB
from src.api.rubus_client import RubusClient
from src.cache.inventory import InventoryCache
from src.ai.embeddings import EmbeddingService
from src.ai.consultant import PharmacyConsultant
from src.bot.handlers import create_handlers
from src.web.app import create_app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


async def post_init(application):
    """Load inventory into DB and start periodic refresh."""
    cache: InventoryCache = application.bot_data["cache"]
    embedding_service: EmbeddingService = application.bot_data["embedding_service"]
    consultant: PharmacyConsultant = application.bot_data["consultant"]

    # Store the running event loop so Flask can schedule coroutines on it
    application.bot_data["async_loop"] = asyncio.get_running_loop()

    # Load full inventory into SQLite
    await cache.load_full()
    logger.info("Inventory loaded: %d products", cache.product_count)

    # Build embeddings in background (non-blocking)
    asyncio.create_task(_build_embeddings_background(embedding_service, consultant))

    # Schedule periodic cache refresh
    application.job_queue.run_repeating(
        refresh_cache_job,
        interval=application.bot_data["refresh_interval"],
        first=application.bot_data["refresh_interval"],
    )

    # Schedule notification checker every 5 minutes
    application.job_queue.run_repeating(
        check_notifications_job,
        interval=300,
        first=120,
    )

    # Schedule conversation cleanup daily (every 24 hours)
    application.job_queue.run_repeating(
        cleanup_conversations_job,
        interval=86400,
        first=600,
    )

    # Auto-register Wazzup webhook if configured
    wazzup_client = application.bot_data.get("wazzup_client")
    config = application.bot_data.get("config")
    if wazzup_client and config and config.wazzup.webhook_url:
        try:
            ok = await wazzup_client.setup_webhook(config.wazzup.webhook_url)
            if ok:
                logger.info("Wazzup webhook auto-registered: %s", config.wazzup.webhook_url)
            else:
                logger.warning("Wazzup webhook auto-registration returned False")
        except Exception as e:
            logger.error("Wazzup webhook auto-registration error: %s", e)

    logger.info("All jobs scheduled")


async def _build_embeddings_background(embedding_service: EmbeddingService,
                                        consultant: PharmacyConsultant):
    """Background task to enrich products with AI fields and build embeddings."""
    try:
        # First enrich products with active ingredients/categories
        logger.info("Starting product enrichment...")
        await consultant.enrich_products_batch(batch_size=100)

        # Then build embeddings for semantic search
        logger.info("Starting embedding generation...")
        await embedding_service.build_product_embeddings(batch_size=500)
        embedding_service.load_cache()

        logger.info("Background enrichment complete")
    except Exception as e:
        logger.error("Background enrichment failed: %s", e)


async def refresh_cache_job(context):
    """Periodic inventory refresh."""
    cache: InventoryCache = context.application.bot_data["cache"]
    await cache.refresh()


async def check_notifications_job(context):
    """Check if any notified products are back in stock.
    Sends notifications via the appropriate platform (Telegram or WhatsApp).
    """
    from src.whatsapp.handlers import wa_phone, WA_PREFIX

    user_db = UserDB()
    notifications = user_db.get_active_notifications()
    wazzup_client = context.application.bot_data.get("wazzup_client")

    for notif in notifications:
        available = notif.get("rest_abs", 0) - notif.get("rest_rezerv", 0)
        if available <= 0:
            continue

        platform = notif.get("platform", "telegram")
        chat_id = notif["chat_id"]
        message_text = (
            f"Товар появился в наличии!\n\n"
            f"{notif['product_name']}\n"
            f"Количество: {available} шт."
        )

        try:
            if platform == "whatsapp" and wazzup_client:
                # Strip internal namespace prefix before sending to Wazzup
                raw_phone = wa_phone(chat_id) if chat_id.startswith(WA_PREFIX) else chat_id
                result = await wazzup_client.send_message(raw_phone, message_text)
                if result is None:
                    logger.error("Failed to send WhatsApp notification to %s", chat_id)
                    continue
            else:
                await context.bot.send_message(
                    chat_id=int(chat_id),
                    text=message_text,
                )
        except Exception as e:
            logger.error("Failed to send notification to %s (%s): %s", chat_id, platform, e)
            continue

        user_db.mark_notified(notif["id"])
        logger.info("Notified %s user %s about %s", platform, chat_id, notif["product_name"])


async def cleanup_conversations_job(context):
    """Periodic cleanup of old conversation messages and WhatsApp sessions."""
    user_db = UserDB()
    user_db.cleanup_old_conversations(max_age_days=7, max_per_user=50)
    WASessionDB().cleanup_stale(max_age_days=7)
    logger.info("Conversation + WA session cleanup completed")


async def post_shutdown(application):
    """Cleanup on shutdown."""
    client: RubusClient = application.bot_data["rubus_client"]
    await client.close()

    wazzup_client = application.bot_data.get("wazzup_client")
    if wazzup_client:
        await wazzup_client.close()

    logger.info("Shutdown complete")


def main():
    config = load_config()

    # Initialize database
    init_db()

    # Initialize services
    rubus_client = RubusClient(config.rubus)
    cache = InventoryCache(rubus_client)
    embedding_service = EmbeddingService(config.openai_api_key)
    consultant = PharmacyConsultant(
        config.openai_api_key, embedding_service,
        pharmacy_config=config.pharmacy,
    )
    if config.pharmacy.has_any:
        logger.info("Pharmacy info loaded: %s%s",
                    config.pharmacy.name or "(no name)",
                    f" @ {config.pharmacy.address}" if config.pharmacy.address else "")
    else:
        logger.warning(
            "PHARMACY_* env vars not set — bot will say 'address unknown' when asked. "
            "Set PHARMACY_NAME, PHARMACY_ADDRESS, PHARMACY_PHONE, PHARMACY_HOURS, "
            "PHARMACY_MAP_URL in .env to enable location answers.",
        )

    # Initialize WhatsApp (optional)
    wazzup_client = None
    whatsapp_handler = None
    if config.wazzup.enabled:
        from src.whatsapp.client import WazzupClient
        from src.whatsapp.handlers import WhatsAppHandler
        wazzup_client = WazzupClient(config.wazzup)
        whatsapp_handler = WhatsAppHandler(
            wazzup_client, consultant,
            admin_chat_id=config.pharmacy.admin_chat_id,
        )
        logger.info("WhatsApp (Wazzup) integration enabled")
        if config.pharmacy.admin_chat_id:
            logger.info("Admin notifications → %s", config.pharmacy.admin_chat_id)
    else:
        logger.info("WhatsApp (Wazzup) integration disabled (no WAZZUP_API_KEY/WAZZUP_CHANNEL_ID)")

    # Create Telegram bot handlers
    (start_handler, clear_handler, favorites_handler,
     notifications_handler, message_handler, callback_handler) = create_handlers(consultant)

    # Build Telegram application
    app = (
        ApplicationBuilder()
        .token(config.telegram_token)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    # Store shared state
    app.bot_data["cache"] = cache
    app.bot_data["rubus_client"] = rubus_client
    app.bot_data["embedding_service"] = embedding_service
    app.bot_data["consultant"] = consultant
    app.bot_data["refresh_interval"] = config.cache_refresh_interval
    app.bot_data["wazzup_client"] = wazzup_client
    app.bot_data["config"] = config

    # Register Telegram handlers
    app.add_handler(CommandHandler("start", start_handler))
    app.add_handler(CommandHandler("clear", clear_handler))
    app.add_handler(CommandHandler("favorites", favorites_handler))
    app.add_handler(CommandHandler("notifications", notifications_handler))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))

    # Start Flask admin panel + Wazzup webhook in background thread
    def run_flask_with_loop():
        # Wait briefly for the event loop to be available
        import time
        for _ in range(30):
            loop = app.bot_data.get("async_loop")
            if loop:
                break
            time.sleep(0.5)
        else:
            loop = None
            logger.warning("Could not get async loop for Flask thread")

        flask_app = create_app(
            whatsapp_handler=whatsapp_handler,
            webhook_secret=config.wazzup.webhook_secret if config.wazzup.enabled else "",
            own_channel_id=config.wazzup.channel_id if config.wazzup.enabled else "",
            forward_map=config.wazzup.forward_map if config.wazzup.enabled else {},
            shared_services={
                "cache": cache,
                "consultant": consultant,
                "embedding_service": embedding_service,
                "async_loop": loop,
            },
        )
        flask_app.config["async_loop"] = loop
        port = int(os.getenv("FLASK_PORT", "5001"))
        flask_app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

    flask_thread = threading.Thread(target=run_flask_with_loop, daemon=True)
    flask_thread.start()
    port = int(os.getenv("FLASK_PORT", "5001"))
    logger.info("Flask admin panel started on http://localhost:%d", port)
    if config.wazzup.enabled:
        logger.info("Wazzup webhook endpoint: http://localhost:%d/wazzup/webhook", port)

    # Start Telegram bot (blocking)
    logger.info("Starting Telegram bot...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
