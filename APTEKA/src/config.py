import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class RubusConfig:
    token: str
    device: str
    bin: str
    base_url: str = "http://rubus.kz/node1/rapi"

    @property
    def auth_payload(self) -> dict:
        return {
            "token": self.token,
            "device": self.device,
            "bin": self.bin,
        }


@dataclass(frozen=True)
class WazzupConfig:
    api_key: str
    channel_id: str
    webhook_secret: str = ""
    webhook_url: str = ""  # public HTTPS URL for auto-registration (optional)
    # "channelId1=url1,channelId2=url2" — forward webhooks for foreign channels
    # to other services (lets one Wazzup account host multiple independent bots).
    forward_map_raw: str = ""
    base_url: str = "https://api.wazzup24.com/v3"

    @property
    def enabled(self) -> bool:
        return bool(self.api_key and self.channel_id)

    @property
    def forward_map(self) -> dict[str, str]:
        result: dict[str, str] = {}
        for pair in self.forward_map_raw.split(","):
            pair = pair.strip()
            if "=" not in pair:
                continue
            cid, url = pair.split("=", 1)
            cid, url = cid.strip(), url.strip()
            if cid and url:
                result[cid] = url
        return result


@dataclass(frozen=True)
class PharmacyConfig:
    """Physical pharmacy info — shown to clients when they ask about
    location, contacts, working hours, delivery, payment. Injected into the
    AI system prompt so the consultant answers naturally from this data."""
    name: str = ""
    address: str = ""
    phone: str = ""
    hours: str = ""  # e.g. "Пн–Вс: 08:00–22:00"
    map_url: str = ""  # Google Maps / 2GIS / Yandex link
    instagram: str = ""
    whatsapp_link: str = ""  # wa.me/7... for sharing with clients
    delivery: str = ""  # e.g. "Курьер по Алматы 1500₸, от 10000₸ бесплатно"
    payment: str = ""  # e.g. "Наличные, Kaspi QR, Halyk, Visa/MC"
    # Where human-escalation and reservation requests are logged (chat_id of admin)
    admin_chat_id: str = ""

    @property
    def has_any(self) -> bool:
        return any([self.name, self.address, self.phone, self.hours,
                    self.map_url, self.delivery, self.payment])

    def _is_open_now(self) -> str:
        """Compute 'open/closed now' from hours string.

        Supports formats like "Пн-Вс 08:00-22:00", "Круглосуточно",
        "пн-пт 09:00-21:00, сб-вс 10:00-20:00". Falls back to "" if unparseable.
        """
        import re
        from datetime import datetime
        if not self.hours:
            return ""
        h = self.hours.lower()
        if "кругл" in h or "24/7" in h:
            return "СТАТУС: АПТЕКА ОТКРЫТА СЕЙЧАС (работает круглосуточно)"
        # Find all HH:MM-HH:MM ranges in the string
        ranges = re.findall(r"(\d{1,2})[:.](\d{2})\s*[–—-]\s*(\d{1,2})[:.](\d{2})", h)
        if not ranges:
            return ""
        now = datetime.now()
        cur = now.hour * 60 + now.minute
        for h1, m1, h2, m2 in ranges:
            start = int(h1) * 60 + int(m1)
            end = int(h2) * 60 + int(m2)
            if end < start:  # e.g. 22:00-02:00 overnight
                end += 24 * 60
                cur_adj = cur if cur >= start else cur + 24 * 60
                if start <= cur_adj < end:
                    close = f"{h2}:{m2}"
                    return f"СТАТУС: АПТЕКА ОТКРЫТА СЕЙЧАС, работает до {close}"
            elif start <= cur < end:
                close = f"{h2}:{m2}"
                return f"СТАТУС: АПТЕКА ОТКРЫТА СЕЙЧАС, работает до {close}"
        # Closed — find the nearest opening
        nearest = None
        for h1, m1, h2, m2 in ranges:
            start = int(h1) * 60 + int(m1)
            if start > cur and (nearest is None or start < nearest):
                nearest = start
        if nearest is not None:
            hh, mm = divmod(nearest, 60)
            return f"СТАТУС: АПТЕКА ЗАКРЫТА СЕЙЧАС, откроется в {hh:02d}:{mm:02d}"
        # If all ranges are earlier today — find the earliest one (next-day opening)
        if ranges:
            h1, m1, _, _ = ranges[0]
            return f"СТАТУС: АПТЕКА ЗАКРЫТА СЕЙЧАС, откроется завтра в {int(h1):02d}:{m1}"
        return ""

    def as_prompt_block(self) -> str:
        """Formatted text for injection into the AI system prompt.

        Includes CURRENT date/time and a pre-computed open/closed status
        so the AI answers "работаете сейчас?" correctly without guessing.
        """
        from datetime import datetime
        now = datetime.now()
        weekday_ru = ["понедельник", "вторник", "среда", "четверг",
                      "пятница", "суббота", "воскресенье"][now.weekday()]
        lines = ["ИНФОРМАЦИЯ ОБ АПТЕКЕ (используй при вопросах о местоположении, контактах, режиме работы, доставке, оплате):"]
        lines.append(f"Сейчас: {now.strftime('%Y-%m-%d %H:%M')} ({weekday_ru})")
        open_status = self._is_open_now()
        if open_status:
            lines.append(open_status)
        if self.name:
            lines.append(f"Название: {self.name}")
        if self.address:
            lines.append(f"Адрес: {self.address}")
        if self.phone:
            lines.append(f"Телефон: {self.phone}")
        if self.hours:
            lines.append(f"Режим работы: {self.hours}")
        if self.map_url:
            lines.append(f"Карта / маршрут: {self.map_url}")
        if self.delivery:
            lines.append(f"Доставка: {self.delivery}")
        if self.payment:
            lines.append(f"Оплата: {self.payment}")
        if self.instagram:
            lines.append(f"Instagram: {self.instagram}")
        return "\n".join(lines)


@dataclass(frozen=True)
class Config:
    telegram_token: str
    openai_api_key: str
    rubus: RubusConfig
    wazzup: WazzupConfig
    pharmacy: PharmacyConfig
    cache_refresh_interval: int = 60  # seconds


def load_config() -> Config:
    telegram_token = os.getenv("TELEGRAM_BOT_TOKEN")
    openai_api_key = os.getenv("OPENAI_API_KEY")
    rubus_token = os.getenv("RUBUS_TOKEN")
    rubus_device = os.getenv("RUBUS_DEVICE")
    rubus_bin = os.getenv("RUBUS_BIN")

    missing = []
    if not telegram_token:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not openai_api_key:
        missing.append("OPENAI_API_KEY")
    if not rubus_token:
        missing.append("RUBUS_TOKEN")
    if not rubus_device:
        missing.append("RUBUS_DEVICE")
    if not rubus_bin:
        missing.append("RUBUS_BIN")

    if missing:
        raise ValueError(f"Missing environment variables: {', '.join(missing)}")

    wazzup = WazzupConfig(
        api_key=os.getenv("WAZZUP_API_KEY", ""),
        channel_id=os.getenv("WAZZUP_CHANNEL_ID", ""),
        webhook_secret=os.getenv("WAZZUP_WEBHOOK_SECRET", ""),
        webhook_url=os.getenv("WAZZUP_WEBHOOK_URL", ""),
        forward_map_raw=os.getenv("WAZZUP_FORWARD_MAP", ""),
    )

    pharmacy = PharmacyConfig(
        name=os.getenv("PHARMACY_NAME", ""),
        address=os.getenv("PHARMACY_ADDRESS", ""),
        phone=os.getenv("PHARMACY_PHONE", ""),
        hours=os.getenv("PHARMACY_HOURS", ""),
        map_url=os.getenv("PHARMACY_MAP_URL", ""),
        instagram=os.getenv("PHARMACY_INSTAGRAM", ""),
        whatsapp_link=os.getenv("PHARMACY_WHATSAPP_LINK", ""),
        delivery=os.getenv("PHARMACY_DELIVERY", ""),
        payment=os.getenv("PHARMACY_PAYMENT", ""),
        admin_chat_id=os.getenv("PHARMACY_ADMIN_CHAT_ID", ""),
    )

    return Config(
        telegram_token=telegram_token,
        openai_api_key=openai_api_key,
        rubus=RubusConfig(
            token=rubus_token,
            device=rubus_device,
            bin=rubus_bin,
        ),
        wazzup=wazzup,
        pharmacy=pharmacy,
    )
