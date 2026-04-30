"""WhatsApp message handlers via Wazzup API.

Design principles (tailored to real WhatsApp pharmacy-client behavior):

1. **Minimal intent set** (6 explicit actions). Everything else → consult.
   Active: greeting · select_item · show_analogs · subscribe_stock
           · reserve_product · escalate_to_human · none.
   Dropped from earlier versions: favorites (not natural in chat),
   show_more/go_back (rarely used), help (merged into greeting),
   show_notifications / clear_history (rare — handled via consult with
   user subscriptions injected as context).

2. **Guided conversation, not a form.** After every non-trivial reply the
   bot suggests 1-2 concrete next moves ("Могу подсказать аналоги или
   отложить пачку") so the client isn't left wondering what else to say.
   The SYSTEM_PROMPT asks GPT to add these inline; handlers append
   context-aware hints for the list/detail screens.

3. **Interactive reply buttons** (max 3 per WhatsApp) as shortcuts on
   detail cards: Аналоги / Отложить / Уведомить. Tap = text inbound →
   same intent router. Never a requirement, just a convenience.

4. **Photos** go through OpenAI Vision → search as text query.
"""

import base64
import logging

from src.ai.consultant import PharmacyConsultant
from src.db.database import (
    MAX_PRODUCTS_IN_LIST,
    ProductDB,
    UserDB,
    WASessionDB,
)
from src.whatsapp.client import WazzupClient

logger = logging.getLogger(__name__)


WA_PREFIX = "wa:"


def wa_key(raw_phone: str) -> str:
    """Internal DB key for a WhatsApp user (namespaced vs Telegram IDs)."""
    if raw_phone.startswith(WA_PREFIX):
        return raw_phone
    return WA_PREFIX + raw_phone


def wa_phone(db_key: str) -> str:
    """Strip namespace prefix to get the raw phone for Wazzup API calls."""
    if db_key.startswith(WA_PREFIX):
        return db_key[len(WA_PREFIX):]
    return db_key


# ── Formatting ──────────────────────────────────────────────────────


def _format_product_detail_wa(p: dict) -> str:
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

    return "\n".join(lines)


def _format_product_list_wa(products: list[dict]) -> str:
    if not products:
        return ""
    lines = []
    for i, p in enumerate(products[:MAX_PRODUCTS_IN_LIST], 1):
        available = max(0, p.get("rest_abs", 0) - p.get("rest_rezerv", 0))
        stock = "✓" if available > 0 else "—"
        name = p.get("name", "")[:55]
        price = p.get("price", 0)
        lines.append(f"{i}. [{stock}] {name} | {price:,.0f} тг")
    return "\n".join(lines)


def _detail_buttons(full: dict, in_stock: bool) -> list[str]:
    """Up to 3 quick-action labels for a single-product detail view.

    On WhatsApp only 3 reply buttons are allowed. Chosen by real-world value:
    — Аналоги (most asked after details)
    — Отложить (reservation — high-conversion for pharmacy)
    — Уведомить (only if out-of-stock — replaces "Отложить" if unavailable)
    """
    buttons: list[str] = ["Аналоги", "Отложить"]
    if not in_stock:
        buttons[1] = "Уведомить"
    return buttons[:3]


def _list_hint(products: list[dict]) -> str:
    """Context-aware trailing hint for a product list — tells the user what
    they can say next, tailored to what's actually in the list.

    Examples:
    - All in stock, mixed: "Напишите номер или название — покажу подробнее.
      Можно также: «аналоги второго», «отложите первый», «как принимать третий»."
    - Some out-of-stock: adds "сообщи когда привезут Nый"
    - All rx: mentions "рецепт — подготовим к выдаче"
    - Single item: simpler hint
    """
    if not products:
        return ""
    n = len(products)
    out_of_stock = [p for p in products
                    if (p.get("rest_abs", 0) - p.get("rest_rezerv", 0)) <= 0]
    all_rx = all(p.get("is_prescription") for p in products)

    if n == 1:
        p = products[0]
        avail = max(0, p.get("rest_abs", 0) - p.get("rest_rezerv", 0))
        parts = ["Напишите «подробнее» — расскажу всё про препарат."]
        if avail > 0:
            parts.append("Могу также подобрать аналог подешевле или отложить до вашего визита.")
        else:
            parts.append("Товара сейчас нет — могу подписать на поступление или подобрать аналог.")
        return "\n\n" + " ".join(parts)

    hints = [
        "Напишите номер или название — покажу подробнее.",
    ]
    tips: list[str] = []
    tips.append("«аналоги второго» — покажу альтернативы")
    if out_of_stock:
        # pick first out-of-stock item index for a natural example
        for i, p in enumerate(products, 1):
            if (p.get("rest_abs", 0) - p.get("rest_rezerv", 0)) <= 0:
                tips.append(f"«сообщи когда привезут {i}» — подпишу на поступление")
                break
    else:
        tips.append("«отложите первый» — забронирую пачку")
    tips.append("«как принимать третий» — расскажу про приём")
    hints.append("Можно также:")
    hints.append("• " + "\n• ".join(tips[:3]))
    if all_rx:
        hints.append("\nВсе эти препараты рецептурные — уточните выбор, подготовим к выдаче по рецепту.")
    return "\n\n" + "\n".join(hints)


def _detail_hint(product: dict, in_stock: bool, is_prescription: bool) -> str:
    """Contextual text under a detail card — buttons complement this."""
    lines = ["Могу помочь дальше:"]
    opts = []
    opts.append("«как принимать» — расскажу про приём, дозировку и побочные")
    if in_stock:
        opts.append("«аналоги» или «что дешевле» — подберу альтернативы")
        opts.append("«отложите пачку» — забронирую до вашего визита")
    else:
        opts.append("«аналог в наличии» — покажу, что есть сейчас")
        opts.append("«сообщи когда привезут» — напишу вам при поступлении")
    if is_prescription:
        opts.append("«можно без рецепта?» — подскажу условия отпуска")
    lines.append("• " + "\n• ".join(opts[:4]))
    return "\n\n" + "\n".join(lines)


# ── Handler ─────────────────────────────────────────────────────────


class WhatsAppHandler:
    """Processes incoming WhatsApp messages and sends responses via Wazzup."""

    def __init__(self, wazzup_client: WazzupClient, consultant: PharmacyConsultant,
                 admin_chat_id: str = ""):
        self._client = wazzup_client
        self._consultant = consultant
        self._product_db = ProductDB()
        self._user_db = UserDB()
        self._session_db = WASessionDB()
        # Phone of the pharmacist/admin who receives escalation/reservation alerts
        self._admin_chat_id = admin_chat_id

    # ── Public entry points ─────────────────────────────────────────

    async def handle_message(self, raw_phone: str, text: str, contact_name: str = ""):
        """Entry point for text messages (including button taps, which arrive
        as plain text matching the button label)."""
        text = (text or "").strip()
        if not text:
            return

        db_id = wa_key(raw_phone)
        self._user_db.upsert_user(
            db_id, first_name=contact_name or "", platform="whatsapp",
        )
        logger.info("WhatsApp text %s: %s", raw_phone, text[:120])

        try:
            await self._client.mark_read(raw_phone)
        except Exception:
            pass

        shown_products = self._session_db.get_list(db_id)
        current_guid = self._session_db.get_current_product_guid(db_id)
        current_product = self._product_db.get_by_guid(current_guid) if current_guid else None

        cmd = await self._consultant.extract_command_intent(
            text, shown_products=shown_products, current_product=current_product,
        )
        command = cmd["command"]
        confidence = cmd.get("confidence", 0.0) or 0.0

        if command == "none" or confidence < 0.55:
            await self._run_consultation(raw_phone, db_id, text, current_product, shown_products)
            return

        idx = self._resolve_index(cmd, shown_products)

        if command == "greeting":
            greeting = await self._consultant.generate_greeting(contact_name)
            self._consultant.clear_history(db_id)
            self._session_db.clear(db_id)  # fresh slate after hello
            await self._client.send_message(raw_phone, greeting)
            return

        if command == "escalate_to_human":
            await self._escalate(raw_phone, db_id, text, contact_name)
            return

        if command == "select_item":
            if idx is None:
                await self._run_consultation(raw_phone, db_id, text, current_product, shown_products)
                return
            await self._send_detail_by_index(raw_phone, db_id, idx)
            return

        if command == "show_analogs":
            target = self._product_from_index_or_current(idx, shown_products, current_product)
            if target:
                await self._show_analogs(raw_phone, db_id, target)
            else:
                # T14 fix: no context → let consult() handle it naturally
                # (it can find analogs via intent_data ask_analog + product_names)
                await self._run_consultation(raw_phone, db_id, text, current_product, shown_products)
            return

        if command == "subscribe_stock":
            target = self._product_from_index_or_current(idx, shown_products, current_product)
            if target:
                self._user_db.add_notification(db_id, target["guid"], target.get("name", ""))
                await self._client.send_message(
                    raw_phone,
                    f"Записал подписку на «{target.get('name', '')}». "
                    f"Как только появится в наличии — напишу вам сюда.\n\n"
                    f"А пока могу подобрать аналог в наличии — скажите «аналоги», "
                    f"и покажу варианты с похожим действием.",
                )
            else:
                await self._run_consultation(raw_phone, db_id, text, current_product, shown_products)
            return

        if command == "reserve_product":
            target = self._product_from_index_or_current(idx, shown_products, current_product)
            if target is None and cmd.get("selected_name"):
                # User named a product that's not in current list — search it
                hits = self._product_db.smart_search(cmd["selected_name"], limit=1)
                target = hits[0] if hits else None
            await self._handle_reservation(raw_phone, db_id, target, text, contact_name)
            return

        # Unknown command → consult
        await self._run_consultation(raw_phone, db_id, text, current_product, shown_products)

    async def handle_image(self, raw_phone: str, image_url: str, caption: str = "",
                           contact_name: str = ""):
        """Extract a product name from a photo via OpenAI Vision, then search."""
        db_id = wa_key(raw_phone)
        self._user_db.upsert_user(
            db_id, first_name=contact_name or "", platform="whatsapp",
        )
        logger.info("WhatsApp image %s: %s (caption=%r)", raw_phone, image_url, caption[:60])

        try:
            await self._client.mark_read(raw_phone)
        except Exception:
            pass

        await self._client.send_message(
            raw_phone, "Получил фото. Разбираю, что на нём...",
        )

        try:
            image_bytes = await self._client.fetch_media(image_url)
            if not image_bytes:
                await self._client.send_message(
                    raw_phone,
                    "Не удалось скачать изображение. Попробуйте ещё раз или напишите название текстом.",
                )
                return
            extracted = await self._vision_extract(image_bytes, caption)
        except Exception as e:
            logger.error("Vision extraction failed: %s", e)
            await self._client.send_message(
                raw_phone,
                "Не смог распознать фото. Напишите название лекарства текстом — помогу.",
            )
            return

        if not extracted:
            await self._client.send_message(
                raw_phone,
                "Я не увидел на фото название лекарства. Пришлите более чёткое фото упаковки "
                "или напишите название текстом.",
            )
            return

        logger.info("Vision extracted from image: %s", extracted[:120])
        shown = self._session_db.get_list(db_id)
        current_guid = self._session_db.get_current_product_guid(db_id)
        current = self._product_db.get_by_guid(current_guid) if current_guid else None
        await self._run_consultation(
            raw_phone, db_id, extracted, current, shown,
            preamble=f"Распознал на фото: {extracted}\n\n",
        )

    # ── Private handlers ────────────────────────────────────────────

    async def _run_consultation(self, raw_phone: str, db_id: str, text: str,
                                current_product: dict | None,
                                shown_products: list[dict],
                                preamble: str = ""):
        """Default AI consultation path — search, answer, show list,
        then append a context-aware trailing hint so the client knows what
        to say next (без него клиент теряется в чате)."""
        answer, found_products = await self._consultant.consult(
            db_id, text,
            current_product=current_product,
            shown_products=shown_products,
        )

        body = (preamble + answer).strip()
        if found_products:
            trimmed = found_products[:MAX_PRODUCTS_IN_LIST]
            self._session_db.save_list(db_id, trimmed, query=text, intent="consultation")
            body += "\n\n" + _format_product_list_wa(trimmed)
            body += _list_hint(trimmed)

        await self._client.send_message(raw_phone, body)

    async def _send_detail_by_index(self, raw_phone: str, db_id: str, index: int):
        shown = self._session_db.get_list(db_id)
        if not shown or index < 1 or index > len(shown):
            await self._client.send_message(
                raw_phone, "В текущем списке нет такого номера. Напишите, что ищете — помогу.",
            )
            return
        guid = shown[index - 1].get("guid", "")
        full = self._product_db.get_by_guid(guid)
        if not full:
            await self._client.send_message(raw_phone, "Товар больше недоступен.")
            return
        await self._send_detail(raw_phone, db_id, full)

    async def _send_detail(self, raw_phone: str, db_id: str, product: dict, prefix: str = ""):
        """Send a single-product detail card with contextual text guidance +
        up to 3 interactive buttons. Guidance text is the primary nudge —
        buttons are a bonus for clients who tap."""
        self._session_db.set_current_product(db_id, product.get("guid", ""))
        self._session_db.save_list(db_id, [product], intent="detail")

        available = max(0, product.get("rest_abs", 0) - product.get("rest_rezerv", 0))
        is_rx = bool(product.get("is_prescription"))
        text = prefix + _format_product_detail_wa(product)
        text += _detail_hint(product, available > 0, is_rx)
        buttons = _detail_buttons(product, available > 0)
        await self._client.send_message(raw_phone, text, buttons=buttons)

    async def _show_analogs(self, raw_phone: str, db_id: str, product: dict):
        full = self._product_db.get_by_guid(product.get("guid", "")) or product
        await self._client.send_message(raw_phone, f"Ищу аналоги для {full.get('name', '')}...")
        analogs = await self._consultant._find_analogs(full)
        if not analogs:
            await self._client.send_message(
                raw_phone,
                f"К сожалению, подходящих аналогов для {full.get('name', '')} "
                "в нашей базе не нашлось.\n\nМогу подписать на поступление — "
                "напишите «сообщи когда привезут». Или подскажите симптом — "
                "подберу препарат с похожим действием.",
            )
            return
        self._session_db.save_list(db_id, analogs[:MAX_PRODUCTS_IN_LIST],
                                   query=f"analogs:{full.get('guid', '')}",
                                   intent="analogs")
        lines = [f"Аналоги для {full.get('name', '')}:", ""]
        for i, a in enumerate(analogs[:MAX_PRODUCTS_IN_LIST], 1):
            avail = max(0, a.get("rest_abs", 0) - a.get("rest_rezerv", 0))
            diff = a.get("price", 0) - full.get("price", 0)
            diff_str = f"(+{diff:,.0f} тг)" if diff > 0 else (
                f"({diff:,.0f} тг)" if diff < 0 else "(та же цена)"
            )
            stock = "✓" if avail > 0 else "—"
            lines.append(f"{i}. [{stock}] {a.get('name', '')[:55]} | {a.get('price', 0):,.0f} тг {diff_str}")
        body = "\n".join(lines) + _list_hint(analogs[:MAX_PRODUCTS_IN_LIST])
        await self._client.send_message(raw_phone, body)

    async def _handle_reservation(self, raw_phone: str, db_id: str,
                                  product: dict | None, text: str, contact_name: str):
        """Reservation is a human-handled action: acknowledge to client, ping admin."""
        if product is None:
            await self._client.send_message(
                raw_phone,
                "Уточните, какой препарат отложить — напишите название или номер "
                "из последнего списка.",
            )
            return

        name = product.get("name", "")
        guid = product.get("guid", "")
        available = max(0, product.get("rest_abs", 0) - product.get("rest_rezerv", 0))

        # Log the reservation intent as a high-priority notification so it shows
        # up in the admin panel and (if configured) notifies the pharmacist.
        self._user_db.add_notification(db_id, guid, f"[БРОНЬ] {name}")

        if available > 0:
            ack = (
                f"Записал заявку: «{name}» — отложить.\n"
                f"Передал фармацевту, с вами свяжутся для подтверждения времени выдачи.\n"
                f"Сейчас в наличии: {available} шт.\n\n"
                f"Если нужно — могу рассказать как принимать этот препарат, "
                f"подобрать сопутствующие или подсказать адрес аптеки."
            )
        else:
            ack = (
                f"Записал заявку: «{name}», но сейчас товара нет в наличии.\n"
                f"Фармацевт свяжется с вами, как только поступит.\n\n"
                f"Пока что — могу подобрать аналог в наличии: напишите «аналоги», "
                f"покажу препараты с похожим действием."
            )
        await self._client.send_message(raw_phone, ack)

        await self._notify_admin(
            f"🔔 Запрос на бронь\n"
            f"Клиент: {contact_name or raw_phone}\n"
            f"Телефон: {raw_phone}\n"
            f"Товар: {name}\n"
            f"В наличии: {available} шт.\n"
            f"Сообщение клиента: {text[:200]}"
        )

    async def _escalate(self, raw_phone: str, db_id: str, text: str, contact_name: str):
        """Human-consultation request: acknowledge + ping admin."""
        ack = (
            "Передаю ваше сообщение фармацевту — он свяжется с вами в ближайшее время. "
            "Если вопрос срочный — позвоните в аптеку."
        )
        # Include pharmacy phone if known
        if self._consultant._pharmacy and self._consultant._pharmacy.phone:
            ack = ack.replace(
                "позвоните в аптеку.",
                f"позвоните по номеру {self._consultant._pharmacy.phone}.",
            )
        await self._client.send_message(raw_phone, ack)

        await self._notify_admin(
            f"🆘 Запрос живой консультации\n"
            f"Клиент: {contact_name or raw_phone}\n"
            f"Телефон: {raw_phone}\n"
            f"Сообщение: {text[:300]}"
        )

    async def _notify_admin(self, text: str):
        """Best-effort notification to the pharmacy admin phone (if set)."""
        if not self._admin_chat_id:
            logger.info("Admin notification (no PHARMACY_ADMIN_CHAT_ID set):\n%s", text)
            return
        try:
            await self._client.send_message(self._admin_chat_id, text)
        except Exception as e:
            logger.error("Admin notification failed: %s", e)

    # ── Helpers ─────────────────────────────────────────────────────

    def _resolve_index(self, cmd: dict, shown: list[dict]) -> int | None:
        idx = cmd.get("selected_index")
        if isinstance(idx, int) and 1 <= idx <= len(shown):
            return idx
        name = (cmd.get("selected_name") or "").strip().lower()
        if name and shown:
            for i, p in enumerate(shown, 1):
                if name in p.get("name", "").lower():
                    return i
        return None

    def _product_from_index_or_current(self, idx: int | None,
                                       shown: list[dict],
                                       current: dict | None) -> dict | None:
        if idx is not None and 1 <= idx <= len(shown):
            guid = shown[idx - 1].get("guid", "")
            return self._product_db.get_by_guid(guid) or shown[idx - 1]
        return current

    async def _vision_extract(self, image_bytes: bytes, caption: str) -> str:
        b64 = base64.b64encode(image_bytes).decode("ascii")
        caption_part = f"\nПодпись клиента: {caption}" if caption else ""
        response = await self._consultant._client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{
                "role": "system",
                "content": (
                    "Ты помогаешь фармацевту. На фото может быть: упаковка лекарства, "
                    "рецепт врача, рукописный список. Верни ТОЛЬКО название препарата (или "
                    "нескольких через запятую) на русском языке — без комментариев, "
                    "без markdown, без пояснений. Если на фото не лекарство и не "
                    "рецепт — верни пустую строку."
                ),
            }, {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Что за лекарство на фото?" + caption_part},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                ],
            }],
            temperature=0,
            max_tokens=80,
        )
        return response.choices[0].message.content.strip()
