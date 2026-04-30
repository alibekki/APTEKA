from telegram import InlineKeyboardButton, InlineKeyboardMarkup

# Callback data prefixes
CB_PRODUCT = "prod:"       # prod:<guid> — show product details
CB_ANALOG = "analog:"      # analog:<guid> — show analogs
CB_FAVORITE = "fav:"       # fav:<guid> — toggle favorite
CB_NOTIFY = "notify:"      # notify:<guid> — set stock notification
CB_FAVORITES_LIST = "favlist"
CB_NOTIFICATIONS_LIST = "notiflist"
CB_BACK = "back"


def product_list_keyboard(products: list[dict]) -> InlineKeyboardMarkup:
    """Create inline keyboard with product buttons."""
    buttons = []
    for p in products[:8]:  # max 8 products
        guid = p.get("guid", "")
        name = p.get("name", "")[:40]
        available = max(0, p.get("rest_abs", 0) - p.get("rest_rezerv", 0))
        price = p.get("price", 0)
        stock_emoji = "+" if available > 0 else "-"
        label = f"[{stock_emoji}] {name} | {price:,.0f} тг"
        buttons.append([InlineKeyboardButton(label, callback_data=f"{CB_PRODUCT}{guid}")])

    return InlineKeyboardMarkup(buttons)


def product_detail_keyboard(guid: str, is_favorite: bool = False,
                             in_stock: bool = True) -> InlineKeyboardMarkup:
    """Keyboard for product detail view."""
    buttons = []

    # Analogs button
    buttons.append([
        InlineKeyboardButton("Показать аналоги", callback_data=f"{CB_ANALOG}{guid}")
    ])

    # Favorite toggle
    fav_text = "Убрать из избранного" if is_favorite else "В избранное"
    buttons.append([
        InlineKeyboardButton(fav_text, callback_data=f"{CB_FAVORITE}{guid}")
    ])

    # Notification (only if out of stock)
    if not in_stock:
        buttons.append([
            InlineKeyboardButton(
                "Уведомить о поступлении",
                callback_data=f"{CB_NOTIFY}{guid}"
            )
        ])

    return InlineKeyboardMarkup(buttons)


def main_menu_keyboard() -> InlineKeyboardMarkup:
    """Main menu keyboard."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Мои избранные", callback_data=CB_FAVORITES_LIST)],
        [InlineKeyboardButton("Мои уведомления", callback_data=CB_NOTIFICATIONS_LIST)],
    ])


def back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Назад", callback_data=CB_BACK)]
    ])
