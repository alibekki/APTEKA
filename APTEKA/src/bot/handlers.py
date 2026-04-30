import logging
from telegram import Update
from telegram.ext import ContextTypes
from src.ai.consultant import PharmacyConsultant
from src.db.database import ProductDB, UserDB
from src.bot.keyboards import (
    product_list_keyboard, product_detail_keyboard, main_menu_keyboard,
    CB_PRODUCT, CB_ANALOG, CB_FAVORITE, CB_NOTIFY,
    CB_FAVORITES_LIST, CB_NOTIFICATIONS_LIST, CB_BACK,
)

logger = logging.getLogger(__name__)


def _format_product_detail(p: dict) -> str:
    """Format product for detailed display."""
    available = max(0, p.get("rest_abs", 0) - p.get("rest_rezerv", 0))
    stock = f"{available} шт." if available > 0 else "Нет в наличии"
    price = p.get("price", 0)
    price_str = f"{price:,.0f} тг".replace(",", " ")
    lines = [p.get("name", "")]

    if p.get("is_prescription"):
        lines.append("РЕЦЕПТУРНЫЙ ПРЕПАРАТ — необходим рецепт врача")

    lines.append(f"Цена: {price_str}")
    lines.append(f"Наличие: {stock}")

    if p.get("producer"):
        lines.append(f"Производитель: {p['producer']}")
    if p.get("expiration_date"):
        lines.append(f"Срок годности: {p['expiration_date']}")
    if p.get("barcode"):
        lines.append(f"Штрихкод: {p['barcode']}")
    if p.get("active_ingredient"):
        lines.append(f"Действующее вещество: {p['active_ingredient']}")
    if p.get("price_limit") and p["price_limit"] > 0:
        lines.append(f"Предельная цена: {p['price_limit']:,.0f} тг".replace(",", " "))

    return "\n".join(lines)


def create_handlers(consultant: PharmacyConsultant):
    """Create all bot handlers."""
    product_db = ProductDB()
    user_db = UserDB()

    async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = str(update.effective_chat.id)
        user = update.effective_user
        user_db.upsert_user(
            chat_id,
            username=user.username or "",
            first_name=user.first_name or "",
            last_name=user.last_name or "",
            platform="telegram",
        )
        consultant.clear_history(chat_id)

        await update.message.reply_text(
            "Здравствуйте! Я — фармацевт-консультант вашей аптеки.\n\n"
            "Я могу помочь вам:\n"
            "- Найти нужное лекарство\n"
            "- Проверить наличие и цену\n"
            "- Подсказать аналоги\n"
            "- Рассказать о препарате\n\n"
            "Просто напишите название лекарства или опишите, что вас интересует.\n\n"
            "Команды:\n"
            "/start — начать заново\n"
            "/clear — очистить историю\n"
            "/favorites — мои избранные\n"
            "/notifications — мои уведомления",
            reply_markup=main_menu_keyboard(),
        )

    async def clear_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        consultant.clear_history(str(update.effective_chat.id))
        await update.message.reply_text("История диалога очищена.")

    async def favorites_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = str(update.effective_chat.id)
        favs = user_db.get_favorites(chat_id)
        if not favs:
            await update.message.reply_text("У вас пока нет избранных товаров.")
            return
        await update.message.reply_text(
            "Ваши избранные товары:",
            reply_markup=product_list_keyboard(favs),
        )

    async def notifications_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = str(update.effective_chat.id)
        notifs = user_db.get_user_notifications(chat_id)
        if not notifs:
            await update.message.reply_text("У вас нет активных уведомлений.")
            return
        lines = ["Ваши уведомления о поступлении:"]
        for n in notifs:
            lines.append(f"- {n['product_name']}")
        await update.message.reply_text("\n".join(lines))

    async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = str(update.effective_chat.id)
        user = update.effective_user
        user_text = update.message.text

        if not user_text or not user_text.strip():
            return

        # Track user
        user_db.upsert_user(
            chat_id,
            username=user.username or "",
            first_name=user.first_name or "",
            last_name=user.last_name or "",
            platform="telegram",
        )

        logger.info("User %s: %s", chat_id, user_text[:100])
        await update.effective_chat.send_action("typing")

        # If user has a currently viewed product, pass it as context
        current_product = context.user_data.get("current_product")
        product_context = ""
        if current_product:
            product_context = (
                f"[Пользователь сейчас просматривает товар: "
                f"{current_product.get('name', '')}. "
                f"Если вопрос относится к этому товару — отвечай именно про него.]\n\n"
            )

        # Get AI consultation + found products
        query = product_context + user_text if product_context else user_text
        answer, found_products = await consultant.consult(
            chat_id, query, current_product=current_product
        )

        # Send answer
        if len(answer) <= 4096:
            await update.message.reply_text(answer)
        else:
            for i in range(0, len(answer), 4096):
                await update.message.reply_text(answer[i:i + 4096])

        # Show inline buttons ONLY for products that AI actually found
        if found_products:
            await update.message.reply_text(
                "Выберите товар для подробной информации:",
                reply_markup=product_list_keyboard(found_products[:5]),
            )

    async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        chat_id = str(query.message.chat_id)
        data = query.data

        if data.startswith(CB_PRODUCT):
            guid = data[len(CB_PRODUCT):]
            product = product_db.get_by_guid(guid)
            if not product:
                await query.edit_message_text("Товар не найден.")
                return

            # Save as currently viewed product so follow-up questions refer to it
            context.user_data["current_product"] = product

            is_fav = user_db.is_favorite(chat_id, guid)
            available = max(0, product.get("rest_abs", 0) - product.get("rest_rezerv", 0))

            await query.edit_message_text(
                _format_product_detail(product),
                reply_markup=product_detail_keyboard(guid, is_fav, available > 0),
            )

        elif data.startswith(CB_ANALOG):
            guid = data[len(CB_ANALOG):]
            product = product_db.get_by_guid(guid)
            if not product:
                await query.edit_message_text("Товар не найден.")
                return

            await query.edit_message_text("Ищу аналоги...")

            # Use consultant to find analogs
            analogs = await consultant._find_analogs(product)
            if analogs:
                text = f"Аналоги для {product['name']}:\n\n"
                for i, a in enumerate(analogs, 1):
                    avail = max(0, a.get("rest_abs", 0) - a.get("rest_rezerv", 0))
                    price_diff = a.get("price", 0) - product.get("price", 0)
                    diff_str = f"(+{price_diff:,.0f})" if price_diff > 0 else f"({price_diff:,.0f})"
                    text += (
                        f"{i}. {a.get('name', '')}\n"
                        f"   Цена: {a.get('price', 0):,.0f} тг {diff_str}\n"
                        f"   Наличие: {avail} шт.\n\n"
                    )
                await query.edit_message_text(
                    text,
                    reply_markup=product_list_keyboard(analogs),
                )
            else:
                await query.edit_message_text(
                    f"К сожалению, аналоги для {product['name']} не найдены в базе."
                )

        elif data.startswith(CB_FAVORITE):
            guid = data[len(CB_FAVORITE):]
            if user_db.is_favorite(chat_id, guid):
                user_db.remove_favorite(chat_id, guid)
                await query.answer("Убрано из избранного", show_alert=True)
            else:
                user_db.add_favorite(chat_id, guid)
                await query.answer("Добавлено в избранное!", show_alert=True)

            # Refresh the detail view
            product = product_db.get_by_guid(guid)
            if product:
                is_fav = user_db.is_favorite(chat_id, guid)
                available = max(0, product.get("rest_abs", 0) - product.get("rest_rezerv", 0))
                await query.edit_message_text(
                    _format_product_detail(product),
                    reply_markup=product_detail_keyboard(guid, is_fav, available > 0),
                )

        elif data.startswith(CB_NOTIFY):
            guid = data[len(CB_NOTIFY):]
            product = product_db.get_by_guid(guid)
            if product:
                user_db.add_notification(chat_id, guid, product.get("name", ""))
                await query.answer(
                    f"Вы будете уведомлены, когда '{product.get('name', '')}' появится в наличии!",
                    show_alert=True,
                )

        elif data == CB_FAVORITES_LIST:
            favs = user_db.get_favorites(chat_id)
            if not favs:
                await query.edit_message_text("У вас пока нет избранных товаров.")
            else:
                await query.edit_message_text(
                    "Ваши избранные товары:",
                    reply_markup=product_list_keyboard(favs),
                )

        elif data == CB_NOTIFICATIONS_LIST:
            notifs = user_db.get_user_notifications(chat_id)
            if not notifs:
                await query.edit_message_text("У вас нет активных уведомлений.")
            else:
                lines = ["Ваши уведомления о поступлении:"]
                for n in notifs:
                    lines.append(f"- {n['product_name']}")
                await query.edit_message_text("\n".join(lines))

    return start_handler, clear_handler, favorites_handler, notifications_handler, message_handler, callback_handler
