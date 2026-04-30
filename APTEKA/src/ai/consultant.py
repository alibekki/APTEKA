import json
import logging
import aiohttp
from openai import AsyncOpenAI
from src.db.database import ProductDB, UserDB, MAX_PRODUCTS_IN_LIST
from src.ai.embeddings import EmbeddingService

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """Ты — профессиональный фармацевт-консультант аптеки. Ты помогаешь клиентам с вопросами о лекарствах и товарах аптеки.

ПРАВИЛА:
1. Отвечай на русском языке, вежливо и профессионально.
2. ЦЕНЫ, НАЛИЧИЕ, АССОРТИМЕНТ — используй ТОЛЬКО данные из базы аптеки. Не выдумывай товары которых нет в базе.
3. ИНФОРМАЦИЯ О ПРЕПАРАТАХ (как работает, состав, показания, противопоказания, способ применения, побочные эффекты, дозировка) — используй свои фармацевтические знания. Ты профессиональный фармацевт и обязан консультировать клиентов по препаратам.
4. Если товар РЕЦЕПТУРНЫЙ — ОБЯЗАТЕЛЬНО предупреди: "Данный препарат отпускается строго по рецепту врача."
5. Если товара нет в наличии — предложи аналоги из предоставленного списка аналогов (если они есть в данных).
6. Указывай цену, наличие (количество), производителя из данных базы.
7. НЕ ставь диагнозы. При описании симптомов — можешь подсказать категорию препаратов и перечислить подходящие из базы, но рекомендуй обратиться к врачу.
8. Отвечай кратко и по делу. Не используй markdown (**, ##, ```) — только простой текст.
9. Если нет подходящих товаров — честно скажи.
10. При сравнении товаров — указывай разницу в цене, составе, форме выпуска.
11. Если клиент просит "подешевле" — сортируй по цене и предложи самые доступные варианты из данных.
12. Если клиент спрашивает про наличие — указывай точное количество из данных.
13. Если вопрос не связан с аптекой/лекарствами — вежливо скажи что ты консультант аптеки и можешь помочь с вопросами о лекарствах.
14. Если клиент спрашивает "как работает", "как принимать", "побочные эффекты", "можно ли детям/беременным" — ОБЯЗАТЕЛЬНО ответь, используя свои фармацевтические знания.
15. Всегда добавляй: "Перед применением ознакомьтесь с инструкцией и проконсультируйтесь с врачом."
16. Если клиент спрашивает про "где вы", "адрес", "как добраться", "режим работы", "телефон", "когда работаете", "контакты", "доставку", "оплату" — отвечай данными из блока "ИНФОРМАЦИЯ ОБ АПТЕКЕ" (если он есть в контексте).
    - Если есть хотя бы адрес — ОБЯЗАТЕЛЬНО его дай. Не говори "нет адреса" только потому, что нет ссылки на карту. "Как добраться" = "где находимся" — достаточно адреса.
    - Если клиент спрашивает "работаете сейчас?" — посмотри на "Сейчас: ..." в блоке информации и на "Режим работы" — сам определи, открыта ли аптека, и ответь коротко "Да, сейчас открыто, работаем до HH:MM" или "Сейчас закрыто, открываемся в HH:MM".
    - Не выдумывай данные, которых нет. Если нет блока или нужного поля — честно скажи "уточните у администратора".
17. Если клиент просит "отложите", "забронируйте", "заберу позже", "зарезервируйте" — подтверди заявку: "Передал фармацевту, с вами свяжутся по поводу резерва. На какое время вам удобно?" Не обещай, что бронь точно сделана — это делает человек.
18. Если клиент просит "свяжите с фармацевтом", "позовите человека", "мне нужна живая консультация", "хочу поговорить с настоящим" — вежливо подтверди: "Передаю ваше сообщение фармацевту — он свяжется с вами в ближайшее время."
19. НАПРАВЛЯЙ КЛИЕНТА — это важно. В конце каждого содержательного ответа добавь 1-2 короткие подсказки (максимум одна строка), что ещё можно спросить. Подсказки должны быть УМЕСТНЫ по контексту:
    - После информации о препарате (как принимать/побочные) → "Подсказать цену или аналоги? Могу также отложить пачку."
    - После показа одного товара → "Могу найти аналоги дешевле, рассказать как принимать, или отложить до вашего визита."
    - После "нет в наличии" → "Подберу аналог в наличии или сообщу, когда привезут — скажите."
    - После симптомов и списка препаратов → "Могу рассказать подробнее про любой, предложить аналог подешевле или отложить."
    - После первой помощи с адресом/часами → "Напишите название препарата или опишите, что беспокоит — подберу."
    - После "не нашёл" → "Попробуйте написать иначе, либо опишите симптомы — подберу по ассортименту."
    НЕ используй эти подсказки:
    - После приветствия (там уже есть направляющие примеры)
    - После уточняющих вопросов (уточнение сам по себе направляет)
    - После эскалации/бронирования (они самодостаточны)
    Формулируй дружелюбно, в одном предложении, БЕЗ маркированных списков и без эмодзи. Пример плохой подсказки: "Что-нибудь ещё?" — слишком общий. Пример хорошей: "Могу подсказать как принимать или найти дешевле."
"""

# Единый промпт для интеллектуального разбора ЛЮБОГО сообщения клиента
INTENT_PROMPT = """Ты — AI-система распознавания намерений клиентов аптеки. Твоя задача — максимально точно понять, что хочет клиент, и извлечь все сущности для поиска в базе данных аптеки.

ТИПЫ НАМЕРЕНИЙ (intent):
- search_product — клиент ищет конкретное лекарство/товар по названию
- search_ingredient — клиент ищет по действующему веществу
- search_symptom — клиент описывает симптомы и хочет рекомендацию
- ask_analog — клиент просит аналог/замену конкретного препарата
- ask_cheaper — клиент хочет более дешёвый вариант
- compare — клиент сравнивает два или более препарата
- check_availability — клиент проверяет наличие товара
- ask_prescription — клиент спрашивает, нужен ли рецепт
- ask_info — клиент хочет узнать информацию о препарате (состав, применение)
- ask_children — клиент ищет лекарство для ребёнка
- ask_pregnancy — клиент спрашивает про лекарства при беременности
- greeting — приветствие
- thanks — благодарность
- general_question — общий вопрос про здоровье/аптеку
- unclear — не удалось понять запрос

ПРАВИЛА ИЗВЛЕЧЕНИЯ:
1. product_names — ВСЕ названия лекарств/товаров из сообщения (бренды, МНН, жаргонные названия)
   - "нурик" → ["нурофен"]
   - "парацик" → ["парацетамол"]
   - "аспиринчик" → ["аспирин"]
   - "ибу" → ["ибупрофен"]
   - "амокси" → ["амоксициллин"]
   - "левомек" → ["левомеколь"]
   - Если клиент написал на латинице — верни как есть + русский вариант если знаешь

2. search_queries — дополнительные поисковые запросы для базы данных
   - При симптомах: добавь названия категорий лекарств ("обезболивающее", "жаропонижающее", "антигистаминное")
   - При "от кашля" → ["кашель", "муколитик", "бромгексин", "амброксол", "ацц"]
   - При "от головной боли" → ["анальгин", "парацетамол", "ибупрофен", "цитрамон", "нурофен"]
   - При "от температуры" → ["парацетамол", "ибупрофен", "нурофен"]
   - При "от аллергии" → ["лоратадин", "цетиризин", "супрастин", "кларитин", "зиртек"]
   - При "от давления" → ["каптоприл", "эналаприл", "лозартан", "амлодипин"]
   - При "от живота" → ["но-шпа", "дротаверин", "смекта", "мезим", "фестал"]
   - При "для горла" → ["стрепсилс", "фарингосепт", "лизобакт", "гексорал"]
   - При "от насморка" → ["називин", "отривин", "полидекса", "ринонорм"]
   - При "антибиотик" → ["амоксициллин", "азитромицин", "цефтриаксон", "ципрофлоксацин"]
   - При "витамины" → ["витамин", "компливит", "супрадин"]
   - При "для ребёнка/детский" → добавь "детский" к каждому поисковому запросу

3. symptoms — описанные симптомы
4. active_ingredients — упомянутые действующие вещества
5. context_hints — дополнительный контекст для формирования ответа:
   - age_group: "child" / "adult" / "elderly" / null
   - wants_cheaper: true/false
   - wants_analog: true/false
   - specific_form: форма выпуска если указана ("таблетки", "сироп", "мазь", "капли", "свечи")

6. needs_clarification — ВАЖНО! Если симптомы НЕОДНОЗНАЧНЫЕ и без уточнения невозможно подобрать правильную категорию лекарств:
   - needs_clarification: true
   - clarification_questions: массив из 1-3 коротких вопросов

   КОГДА НУЖНО УТОЧНЕНИЕ (needs_clarification: true):
   - "болит живот" — может быть спазм (Но-шпа), отравление (сорбенты), переедание (ферменты), изжога (антациды)
   - "болит горло" — может быть инфекция (антибиотики), воспаление (антисептики), аллергия
   - "кашель" — сухой (противокашлевые) или влажный (муколитики) — разные препараты!
   - "плохо себя чувствую" — слишком размыто
   - "от простуды" — температура? насморк? горло? кашель? — разные препараты
   - "для желудка" — изжога? боль? тошнота? — разные категории

   КОГДА НЕ НУЖНО УТОЧНЕНИЕ (needs_clarification: false):
   - Клиент назвал конкретное лекарство: "нурофен есть?" — не спрашивай "а что болит?"
   - Симптомы достаточно конкретные: "температура 38 и головная боль" — ясно что нужно жаропонижающее
   - Клиент уже уточнил детали: "сухой кашель у ребёнка 5 лет" — достаточно деталей
   - Клиент просит конкретную категорию: "дайте сорбент" — не спрашивай зачем
   - Клиент отвечает на уточняющий вопрос (в контексте диалога)

   ВОПРОСЫ ДОЛЖНЫ БЫТЬ:
   - Короткие (1 предложение)
   - Конкретные (не "расскажите подробнее", а "кашель сухой или с мокротой?")
   - Максимум 2-3 вопроса
   - Относящиеся к выбору КАТЕГОРИИ лекарства, а не к диагностике

ПРИМЕРЫ:

Клиент: "У меня болит голова и температура 38"
Ответ: {"intent":"search_symptom","product_names":[],"search_queries":["парацетамол","ибупрофен","нурофен","анальгин","цитрамон","жаропонижающее"],"symptoms":["головная боль","температура"],"active_ingredients":["парацетамол","ибупрофен"],"context_hints":{"age_group":"adult","wants_cheaper":false,"wants_analog":false,"specific_form":null},"needs_clarification":false,"clarification_questions":[]}

Клиент: "нурофен есть?"
Ответ: {"intent":"check_availability","product_names":["нурофен"],"search_queries":["нурофен"],"symptoms":[],"active_ingredients":[],"context_hints":{"age_group":null,"wants_cheaper":false,"wants_analog":false,"specific_form":null},"needs_clarification":false,"clarification_questions":[]}

Клиент: "что-то подешевле вместо нурофена"
Ответ: {"intent":"ask_cheaper","product_names":["нурофен"],"search_queries":["ибупрофен","ибуфен","нурофен"],"symptoms":[],"active_ingredients":["ибупрофен"],"context_hints":{"age_group":null,"wants_cheaper":true,"wants_analog":true,"specific_form":null},"needs_clarification":false,"clarification_questions":[]}

Клиент: "ребёнку 3 года, кашляет, что дать?"
Ответ: {"intent":"search_symptom","product_names":[],"search_queries":["детский кашель","детский сироп","амбробене","лазолван","амброксол","проспан","геделикс"],"symptoms":["кашель"],"active_ingredients":["амброксол"],"context_hints":{"age_group":"child","wants_cheaper":false,"wants_analog":false,"specific_form":"сироп"},"needs_clarification":true,"clarification_questions":["Кашель сухой или с мокротой?"]}

Клиент: "от боли в животе и тошноты"
Ответ: {"intent":"search_symptom","product_names":[],"search_queries":[],"symptoms":["боль в животе","тошнота"],"active_ingredients":[],"context_hints":{"age_group":null,"wants_cheaper":false,"wants_analog":false,"specific_form":null},"needs_clarification":true,"clarification_questions":["Боль связана с приёмом пищи (после еды, натощак)?","Есть ли диарея или рвота?"]}

Клиент: "от кашля"
Ответ: {"intent":"search_symptom","product_names":[],"search_queries":[],"symptoms":["кашель"],"active_ingredients":[],"context_hints":{"age_group":null,"wants_cheaper":false,"wants_analog":false,"specific_form":null},"needs_clarification":true,"clarification_questions":["Кашель сухой или влажный (с мокротой)?","Для взрослого или ребёнка?"]}

Клиент: "от простуды"
Ответ: {"intent":"search_symptom","product_names":[],"search_queries":[],"symptoms":["простуда"],"active_ingredients":[],"context_hints":{"age_group":null,"wants_cheaper":false,"wants_analog":false,"specific_form":null},"needs_clarification":true,"clarification_questions":["Какие симптомы беспокоят: температура, насморк, горло, кашель?","Для взрослого или ребёнка?"]}

Клиент: "что лучше мезим или фестал?"
Ответ: {"intent":"compare","product_names":["мезим","фестал"],"search_queries":["мезим","фестал"],"symptoms":[],"active_ingredients":["панкреатин"],"context_hints":{"age_group":null,"wants_cheaper":false,"wants_analog":false,"specific_form":null},"needs_clarification":false,"clarification_questions":[]}

Клиент: "аналог амоксиклава подешевле в таблетках"
Ответ: {"intent":"ask_analog","product_names":["амоксиклав"],"search_queries":["амоксиклав","амоксициллин","аугментин","флемоксин"],"symptoms":[],"active_ingredients":["амоксициллин","клавулановая кислота"],"context_hints":{"age_group":null,"wants_cheaper":true,"wants_analog":true,"specific_form":"таблетки"},"needs_clarification":false,"clarification_questions":[]}

Клиент: "дайте что-нибудь от аллергии, чтобы не хотелось спать"
Ответ: {"intent":"search_symptom","product_names":[],"search_queries":["лоратадин","цетиризин","кларитин","зиртек","эриус","дезлоратадин","антигистаминное"],"symptoms":["аллергия"],"active_ingredients":["лоратадин","цетиризин","дезлоратадин"],"context_hints":{"age_group":"adult","wants_cheaper":false,"wants_analog":false,"specific_form":null},"needs_clarification":false,"clarification_questions":[]}

Клиент: "после еды тяжесть и вздутие, без рвоты"
Ответ: {"intent":"search_symptom","product_names":[],"search_queries":["мезим","фестал","панкреатин","ферменты","эспумизан","метеоспазмил"],"symptoms":["тяжесть после еды","вздутие"],"active_ingredients":["панкреатин","симетикон"],"context_hints":{"age_group":"adult","wants_cheaper":false,"wants_analog":false,"specific_form":null},"needs_clarification":false,"clarification_questions":[]}

Ответь строго в формате JSON без markdown и без ```."""


# Minimal WhatsApp command router — only 6 explicit actions that need
# DB-level side effects. Everything else (search, symptoms, info, address,
# subscriptions list, "clear history" etc.) goes through `none` → consult().
# Fewer intents = fewer mis-classifications, more natural UX.
WA_COMMAND_PROMPT = """Ты — классификатор сообщений клиента аптеки в WhatsApp. Твоя задача — поймать ТОЛЬКО действия, требующие записи в базу. Всё остальное (поиск, симптомы, вопросы про лекарства, адрес, часы, "какие у меня подписки") — это command="none", ими займётся фармацевт-консультант.

Возможные действия (command):
- select_item — клиент выбирает товар из ПОКАЗАННОГО списка ("1", "первый", "расскажи про второй", "давай этот", "нурофен покажи"). ТОЛЬКО если shown_list непустой.
- show_analogs — клиент хочет аналоги КОНКРЕТНОГО товара из списка или текущего. Нужен selected_index ИЛИ current_product. Без контекста списка/карточки — command="none".
- subscribe_stock — "сообщи когда привезут", "уведоми когда будет", "пришли когда появится". Нужен контекст товара. Без контекста — "none".
- reserve_product — "отложите", "забронируйте", "заберу позже", "зарезервируйте". Нужен контекст товара.
- escalate_to_human — "позовите фармацевта", "хочу с живым", "переведите на человека", "нужна консультация человека", "свяжите с оператором"
- greeting — приветствие / первый контакт / "что ты умеешь" ("привет", "здравствуй", "добрый день", "салам", "меню", "помоги", "начни заново")
- none — ВСЁ остальное

ЖЁСТКИЕ ПРАВИЛА:
1. Если shown_list пустой И нет current_product → select_item/show_analogs/subscribe_stock/reserve_product НЕ возвращай. Только "none".
2. Опечатки/странные написания лекарств ("нурафен", "парацетомол", "нурофэн") — НИКОГДА не команда. "none".
3. Симптомы, info-вопросы (как принимать, побочные, детям), вопросы про аптеку (адрес, часы, доставка, оплата, подписки) — всегда "none".
4. Когда не уверен — верни "none" с confidence < 0.7, пусть консультант разбирается.

ПОЛЯ ОТВЕТА (JSON):
{
  "command": "select_item"|"show_analogs"|"subscribe_stock"|"reserve_product"|"escalate_to_human"|"greeting"|"none",
  "selected_index": null | 1..N,
  "selected_name": null | "название товара",
  "confidence": 0.0..1.0
}

КОНТЕКСТ СЕССИИ:
- shown_list: список показанных товаров с номерами (может быть пустым)
- current_product: товар, просматриваемый сейчас (может быть null)

ПРИМЕРЫ (контекст список: 1. Нурофен 200мг  2. Ибупрофен 400мг  3. Миг 400):

"1" → {"command":"select_item","selected_index":1,"selected_name":null,"confidence":1.0}
"первый" → {"command":"select_item","selected_index":1,"selected_name":null,"confidence":1.0}
"третий" → {"command":"select_item","selected_index":3,"selected_name":null,"confidence":1.0}
"покажи нурофен" → {"command":"select_item","selected_index":1,"selected_name":"Нурофен 200мг","confidence":0.9}
"сообщи когда появится второй" → {"command":"subscribe_stock","selected_index":2,"selected_name":null,"confidence":1.0}
"аналоги к первому" → {"command":"show_analogs","selected_index":1,"selected_name":null,"confidence":1.0}
"аналоги" (есть current_product) → {"command":"show_analogs","selected_index":null,"selected_name":null,"confidence":0.9}
"отложите первый" → {"command":"reserve_product","selected_index":1,"selected_name":null,"confidence":1.0}
"забронируйте пачку нурофена" → {"command":"reserve_product","selected_index":1,"selected_name":"Нурофен","confidence":0.9}
"позовите живого фармацевта" → {"command":"escalate_to_human","selected_index":null,"selected_name":null,"confidence":1.0}
"привет" → {"command":"greeting","selected_index":null,"selected_name":null,"confidence":1.0}
"что ты умеешь" → {"command":"greeting","selected_index":null,"selected_name":null,"confidence":0.9}

ПРИМЕРЫ "none" (консультант разберётся):

"1" без списка → {"command":"none","selected_index":null,"selected_name":null,"confidence":0.9}
"нурафен" (опечатка) → {"command":"none","selected_index":null,"selected_name":null,"confidence":0.9}
"от головной боли" → {"command":"none","selected_index":null,"selected_name":null,"confidence":1.0}
"как принимать ибупрофен?" → {"command":"none","selected_index":null,"selected_name":null,"confidence":1.0}
"есть парацетамол?" → {"command":"none","selected_index":null,"selected_name":null,"confidence":1.0}
"где вы находитесь?" → {"command":"none","selected_index":null,"selected_name":null,"confidence":1.0}
"есть доставка?" → {"command":"none","selected_index":null,"selected_name":null,"confidence":1.0}
"на что я подписан?" → {"command":"none","selected_index":null,"selected_name":null,"confidence":1.0}
"очисти историю" → {"command":"none","selected_index":null,"selected_name":null,"confidence":0.8}
"что-то дешевле" (без контекста) → {"command":"none","selected_index":null,"selected_name":null,"confidence":0.9}

Верни строго JSON без markdown и без ```."""


def _format_product(p: dict, index: int = 0) -> str:
    """Format a product dict into readable text for AI context."""
    available = max(0, p.get("rest_abs", 0) - p.get("rest_rezerv", 0))
    stock = f"{available} шт." if available > 0 else "НЕТ В НАЛИЧИИ"
    rx = " [РЕЦЕПТУРНЫЙ]" if p.get("is_prescription") else ""
    price = p.get("price", 0)
    producer = p.get("producer", "") or "не указан"
    ingredient = p.get("active_ingredient", "")

    prefix = f"{index}. " if index else ""
    lines = [
        f"{prefix}{p.get('name', '')}{rx}",
        f"   Цена: {price:,.0f} тг | Наличие: {stock}",
        f"   Производитель: {producer}",
    ]
    if ingredient:
        lines.append(f"   Действующее вещество: {ingredient}")
    if p.get("expiration_date"):
        lines.append(f"   Годен до: {p['expiration_date']}")

    return "\n".join(lines)


def _format_products(products: list[dict], label: str = "Найденные товары") -> str:
    if not products:
        return f"{label}: ничего не найдено в базе."
    lines = [f"{label}:"]
    for i, p in enumerate(products, 1):
        lines.append(_format_product(p, i))
    return "\n".join(lines)


class PharmacyConsultant:
    def __init__(self, api_key: str, embedding_service: EmbeddingService,
                 pharmacy_config=None):
        self._client = AsyncOpenAI(api_key=api_key)
        self._embedding_service = embedding_service
        self._product_db = ProductDB()
        self._user_db = UserDB()
        self._pharmacy = pharmacy_config

    def _base_system_messages(self) -> list[dict]:
        """SYSTEM_PROMPT + optional pharmacy info block (used by every call)."""
        msgs = [{"role": "system", "content": SYSTEM_PROMPT}]
        if self._pharmacy and self._pharmacy.has_any:
            msgs.append({"role": "system", "content": self._pharmacy.as_prompt_block()})
        return msgs

    async def _web_search(self, query: str) -> str:
        """Search the web for drug information using DuckDuckGo HTML."""
        try:
            search_url = "https://html.duckduckgo.com/html/"
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
                async with session.post(search_url, data={"q": query}) as resp:
                    if resp.status != 200:
                        return ""
                    html = await resp.text()

            # Extract text snippets from results
            import re
            snippets = re.findall(
                r'<a class="result__snippet"[^>]*>(.*?)</a>',
                html, re.DOTALL
            )
            if not snippets:
                # Fallback: extract from result__body
                snippets = re.findall(
                    r'class="result__snippet">(.*?)</td>',
                    html, re.DOTALL
                )

            # Clean HTML tags
            clean_snippets = []
            for s in snippets[:5]:
                text = re.sub(r'<[^>]+>', '', s).strip()
                if text and len(text) > 30:
                    clean_snippets.append(text)

            return "\n".join(clean_snippets) if clean_snippets else ""
        except Exception as e:
            logger.warning("Web search failed: %s", e)
            return ""

    async def extract_command_intent(self, message: str,
                                     shown_products: list[dict] | None = None,
                                     current_product: dict | None = None) -> dict:
        """Session-aware command router for WhatsApp.

        Figures out if the user is issuing a command on the current list
        (select/analogs/favorite/notify/etc.) or just chatting. Entirely
        LLM-driven — no string matching.

        Returns dict with keys: command, selected_index, selected_name, confidence.
        """
        shown_desc = ""
        if shown_products:
            lines = []
            for i, p in enumerate(shown_products[:MAX_PRODUCTS_IN_LIST], 1):
                lines.append(f"{i}. {p.get('name', '')[:60]}")
            shown_desc = "shown_list:\n" + "\n".join(lines)
        else:
            shown_desc = "shown_list: (пусто, список не показан)"

        current_desc = ""
        if current_product:
            current_desc = f"current_product: {current_product.get('name', '')[:80]}"
        else:
            current_desc = "current_product: null"

        user_content = f"{shown_desc}\n{current_desc}\n\nСообщение клиента: {message}"

        try:
            response = await self._client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": WA_COMMAND_PROMPT},
                    {"role": "user", "content": user_content},
                ],
                temperature=0,
                max_tokens=120,
            )
            text = response.choices[0].message.content.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[1].rsplit("```", 1)[0]
            result = json.loads(text)
            result.setdefault("command", "none")
            result.setdefault("selected_index", None)
            result.setdefault("selected_name", None)
            result.setdefault("confidence", 0.5)
            logger.info("WA command: %s idx=%s name=%s conf=%.2f",
                        result["command"], result["selected_index"],
                        result["selected_name"], result["confidence"])
            return result
        except Exception as e:
            logger.warning("WA command extraction failed: %s", e)
            return {"command": "none", "selected_index": None,
                    "selected_name": None, "confidence": 0.0}

    async def generate_greeting(self, contact_name: str = "") -> str:
        """Generate a personalised greeting via GPT — friendly, short, with
        2-3 concrete example questions so the client immediately sees what
        they can ask (WhatsApp has no menu — we guide by examples)."""
        try:
            name_hint = f"Имя клиента: {contact_name}" if contact_name else "Имя клиента не известно"
            sys_msgs = [{
                "role": "system",
                "content": (
                    "Ты — фармацевт-консультант аптеки в WhatsApp. Сформулируй "
                    "приветствие для клиента: дружелюбное, но деловое. Без markdown, "
                    "без эмодзи, без команд со слэшами.\n\n"
                    "СТРУКТУРА приветствия (3 блока, разделены пустой строкой):\n"
                    "1) Короткое приветствие (1-2 строки). Если есть название аптеки — "
                    "представься от её имени. Если есть имя клиента — обратись по имени.\n"
                    "2) Одна фраза: чем ты можешь помочь (поиск лекарства, подбор по "
                    "симптомам, наличие и цена, аналоги, адрес/часы/доставка).\n"
                    "3) 3-4 примера вопросов в кавычках, которые клиент может задать, "
                    "каждый с новой строки с дефисом в начале. Примеры должны быть "
                    "разнообразными: поиск препарата, симптом, info-вопрос, действие. "
                    "Используй типичные фразы клиента.\n\n"
                    "Заверши одной короткой фразой-приглашением, например: «Просто "
                    "напишите, что беспокоит — подскажу»."
                ),
            }]
            if self._pharmacy and self._pharmacy.has_any:
                sys_msgs.append({
                    "role": "system",
                    "content": self._pharmacy.as_prompt_block(),
                })
            sys_msgs.append({"role": "user", "content": name_hint})

            response = await self._client.chat.completions.create(
                model="gpt-4o-mini",
                messages=sys_msgs,
                temperature=0.6,
                max_tokens=350,
            )
            return response.choices[0].message.content.strip()
        except Exception as e:
            logger.warning("Greeting generation failed: %s", e)
            prefix = f"Здравствуйте! Аптека «{self._pharmacy.name}» на связи.\n\n" \
                if self._pharmacy and self._pharmacy.name else "Здравствуйте!\n\n"
            return (
                prefix +
                "Я — фармацевт-консультант. Помогу найти лекарство, подобрать по "
                "симптомам, рассказать про препарат, подсказать аналоги и адрес.\n\n"
                "Например, можно спросить:\n"
                "— «есть нурофен?»\n"
                "— «от головной боли что посоветуете?»\n"
                "— «как принимать парацетамол?»\n"
                "— «отложите пачку ацц»\n\n"
                "Просто напишите, что беспокоит — подскажу."
            )

    async def _extract_intent(self, message: str) -> dict:
        """Use OpenAI to intelligently parse any customer message."""
        try:
            response = await self._client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": INTENT_PROMPT},
                    {"role": "user", "content": message},
                ],
                temperature=0,
                max_tokens=500,
            )
            text = response.choices[0].message.content.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[1].rsplit("```", 1)[0]
            result = json.loads(text)

            # Validate structure
            result.setdefault("intent", "search_product")
            result.setdefault("product_names", [])
            result.setdefault("search_queries", [])
            result.setdefault("symptoms", [])
            result.setdefault("active_ingredients", [])
            result.setdefault("context_hints", {})
            result.setdefault("needs_clarification", False)
            result.setdefault("clarification_questions", [])

            logger.info(
                "Intent: %s | Products: %s | Queries: %s | Symptoms: %s | Clarify: %s",
                result["intent"],
                result["product_names"][:3],
                result["search_queries"][:3],
                result["symptoms"][:2],
                result["needs_clarification"],
            )
            return result

        except Exception as e:
            logger.warning("Intent extraction failed: %s", e)
            return {
                "intent": "search_product",
                "product_names": [message],
                "search_queries": [message],
                "symptoms": [],
                "active_ingredients": [],
                "context_hints": {},
            }

    async def _search_products(self, query: str, limit: int = 8) -> list[dict]:
        """Smart search: FTS5 + transliteration + fuzzy + embeddings fallback."""
        results = self._product_db.smart_search(query, limit=limit)

        if len(results) < 3 and self._embedding_service:
            existing_guids = {p["guid"] for p in results}
            try:
                semantic_hits = await self._embedding_service.semantic_search(query, limit=5)
                for guid, score in semantic_hits:
                    if guid not in existing_guids and score > 0.3:
                        product = self._product_db.get_by_guid(guid)
                        if product:
                            results.append(product)
                            existing_guids.add(guid)
            except Exception as e:
                logger.warning("Semantic search failed: %s", e)

        return results[:limit]

    async def _find_analogs(self, product: dict, limit: int = 5) -> list[dict]:
        """Find analogs using AI knowledge + database search.

        Step 1: Ask GPT what are real pharmaceutical analogs of this drug
        Step 2: Search each analog name in our database
        Step 3: Return only what we actually have in stock
        """
        product_name = product.get("name", "")
        guid = product.get("guid", "")
        ingredient = product.get("active_ingredient", "")
        producer = product.get("producer", "")

        # Step 1: Ask AI for real analog names
        try:
            response = await self._client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{
                    "role": "system",
                    "content": (
                        "Ты фармацевт. Назови аналоги лекарственного препарата — "
                        "препараты с таким же или похожим действующим веществом, "
                        "или из той же фармакологической группы с аналогичным действием.\n"
                        "Верни JSON-массив строк — только названия (МНН и бренды), без дозировок.\n"
                        "Пример: [\"ибупрофен\", \"нурофен\", \"ибуфен\", \"миг\"]\n"
                        "Верни 10-15 названий. Только JSON, без markdown."
                    )
                }, {
                    "role": "user",
                    "content": f"Аналоги для: {product_name}\nДействующее вещество: {ingredient or 'неизвестно'}\nПроизводитель: {producer}"
                }],
                temperature=0,
                max_tokens=300,
            )
            text = response.choices[0].message.content.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[1].rsplit("```", 1)[0]
            analog_names = json.loads(text)
            logger.info("AI suggested analogs for '%s': %s", product_name[:30], analog_names[:5])
        except Exception as e:
            logger.warning("AI analog lookup failed: %s", e)
            analog_names = []

        # Step 2: Search each analog in our database
        analogs: dict[str, dict] = {}
        for name in analog_names:
            results = self._product_db.smart_search(name, limit=3)
            for p in results:
                p_guid = p.get("guid", "")
                if p_guid == guid or p_guid in analogs:
                    continue
                # Only in-stock
                available = p.get("rest_abs", 0) - p.get("rest_rezerv", 0)
                if available > 0:
                    analogs[p_guid] = p

            if len(analogs) >= limit:
                break

        # Step 3: Also try DB-level search by ingredient/category (fast, no API call)
        if ingredient and len(analogs) < limit:
            for p in self._product_db.find_analogs_by_ingredient(ingredient, guid, limit):
                if p["guid"] not in analogs:
                    analogs[p["guid"]] = p

        result = list(analogs.values())
        result.sort(key=lambda p: p.get("price", 0))
        return result[:limit]

    async def _gather_context(self, intent_data: dict,
                              current_product: dict | None = None) -> tuple[list[str], list[dict]]:
        """Build product context from database based on parsed intent.

        This is the core intelligence — it decides WHAT to search
        based on HOW the customer asked.
        """
        intent = intent_data["intent"]
        product_names = intent_data["product_names"]
        search_queries = intent_data["search_queries"]
        symptoms = intent_data["symptoms"]
        ingredients = intent_data["active_ingredients"]
        hints = intent_data.get("context_hints", {})

        context_parts = []
        all_products: dict[str, dict] = {}  # guid → product, dedup across searches

        def _collect(products: list[dict], label: str):
            unique = []
            for p in products:
                guid = p.get("guid", "")
                if guid and guid not in all_products:
                    all_products[guid] = p
                    unique.append(p)
            if unique:
                context_parts.append(_format_products(unique, label))

        # 1. Search by explicit product names
        for name in product_names:
            products = await self._search_products(name)
            _collect(products, f"По запросу '{name}'")

        # 2. Search by additional search queries (symptoms → drug names)
        for query in search_queries:
            if query.lower() not in [n.lower() for n in product_names]:
                products = await self._search_products(query, limit=5)
                _collect(products, f"По запросу '{query}'")

        # 3. Search by active ingredients
        for ingredient in ingredients:
            products = self._product_db.search_by_active_ingredient(ingredient, limit=5)
            new_products = [p for p in products if p.get("guid") not in all_products]
            if new_products:
                for p in new_products:
                    all_products[p["guid"]] = p
                context_parts.append(
                    _format_products(new_products, f"С действующим веществом '{ingredient}'")
                )

        # 4. Find analogs for out-of-stock products or if explicitly asked
        needs_analogs = intent in ("ask_analog", "ask_cheaper") or hints.get("wants_analog")
        out_of_stock = [
            p for p in all_products.values()
            if (p.get("rest_abs", 0) - p.get("rest_rezerv", 0)) <= 0
        ]

        # If user asks for analogs but no products found — use current_product
        if needs_analogs and not all_products and current_product:
            all_products[current_product.get("guid", "")] = current_product
            context_parts.append(_format_products([current_product], "Текущий товар"))

        analog_sources = []
        if needs_analogs:
            # Analogs for all found products
            analog_sources = list(all_products.values())[:3]
        elif out_of_stock:
            # Analogs only for out-of-stock
            analog_sources = out_of_stock[:3]

        for p in analog_sources:
            analogs = await self._find_analogs(p)
            if analogs:
                # Filter: if wants cheaper, only show cheaper analogs
                if hints.get("wants_cheaper"):
                    analogs = [a for a in analogs if a.get("price", 0) < p.get("price", float("inf"))]
                new_analogs = [a for a in analogs if a.get("guid") not in all_products]
                if new_analogs:
                    for a in new_analogs:
                        all_products[a["guid"]] = a
                    label = f"Аналоги для '{p.get('name', '')}'"
                    if hints.get("wants_cheaper"):
                        label += " (дешевле)"
                    context_parts.append(_format_products(new_analogs, label))

        # 5. Filter by specific form if requested
        specific_form = hints.get("specific_form")
        if specific_form and context_parts:
            form_lower = specific_form.lower()
            # Add a note about form preference, GPT will use it
            context_parts.append(f"\nКлиент предпочитает форму выпуска: {specific_form}")

        # 6. Age group note
        age_group = hints.get("age_group")
        if age_group == "child":
            context_parts.append("\nВАЖНО: Клиент ищет лекарство для РЕБЁНКА. Предлагай только детские формы.")
        elif age_group == "elderly":
            context_parts.append("\nВАЖНО: Клиент пожилого возраста. Учитывай противопоказания.")

        return context_parts, list(all_products.values())

    async def consult(self, chat_id: int | str, user_message: str,
                      current_product: dict | None = None,
                      shown_products: list[dict] | None = None) -> tuple[str, list[dict]]:
        """Main consultation entry point.

        Args:
            chat_id: User identifier.
            user_message: Raw user message text.
            current_product: Product the user just drilled into (for follow-up).
            shown_products: Products last shown to the user in a list — included
                in the system context so GPT can reason about "первый" / "этот"
                references without the caller pre-processing them.

        Returns:
            (answer_text, found_products) — AI response and the actual products
            found in the database, so handlers can show correct inline buttons.
        """
        # Step 0: Fast-path for barcodes (digits only, skip GPT)
        stripped = user_message.strip()
        # Barcodes in pharmacy are typically 8 (EAN-8), 13 (EAN-13), or 14 digits.
        # Shorter digit strings (4-7) may be a medication number — still try
        # barcode lookup, but if it doesn't match anything 8+-digit, be explicit.
        is_barcode_shape = stripped.isdigit() and len(stripped) >= 8
        if stripped.isdigit() and len(stripped) >= 4:
            barcode_results = self._product_db.search_by_barcode(stripped)
            if barcode_results:
                context = _format_products(barcode_results, "Найден товар по штрихкоду")
                self._user_db.log_search(chat_id, user_message, len(barcode_results))
                history = self._user_db.get_conversation(chat_id, limit=10)
                messages = [
                    *self._base_system_messages(),
                    {"role": "system", "content": f"ДАННЫЕ ИЗ БАЗЫ АПТЕКИ:\n\n{context}"},
                    *history,
                    {"role": "user", "content": user_message},
                ]
                response = await self._client.chat.completions.create(
                    model="gpt-4o-mini", messages=messages,
                    temperature=0.3, max_tokens=800,
                )
                answer = response.choices[0].message.content.strip()
                self._user_db.save_message(chat_id, "user", user_message)
                self._user_db.save_message(chat_id, "assistant", answer)
                return answer, barcode_results
            # Barcode-shaped query but nothing found — give a direct, honest answer
            # (T25 fix: don't let it fall through to ambiguous clarification prompt)
            if is_barcode_shape:
                self._user_db.log_search(chat_id, user_message, 0)
                msg = (
                    f"Товар со штрих-кодом {stripped} не найден в нашем ассортименте. "
                    "Напишите название лекарства — помогу найти."
                )
                self._user_db.save_message(chat_id, "user", user_message)
                self._user_db.save_message(chat_id, "assistant", msg)
                return msg, []

        # Step 1: Intelligent intent extraction
        intent_data = await self._extract_intent(user_message)
        intent = intent_data["intent"]

        # Step 2: Handle non-search intents quickly
        if intent == "greeting":
            return (
                "Здравствуйте! Я фармацевт-консультант вашей аптеки. "
                "Чем могу помочь? Напишите название лекарства или опишите, что вас беспокоит."
            ), []

        if intent == "thanks":
            return "Пожалуйста! Обращайтесь, если понадобится помощь. Будьте здоровы!", []

        # Step 2.5: Handle clarification — ask questions before searching
        if intent_data.get("needs_clarification") and intent_data.get("clarification_questions"):
            questions = intent_data["clarification_questions"]
            symptoms = intent_data.get("symptoms", [])

            symptom_text = f" при {', '.join(symptoms)}" if symptoms else ""
            clarification = (
                f"Чтобы подобрать наиболее подходящий препарат{symptom_text}, "
                f"уточните, пожалуйста:\n"
            )
            for i, q in enumerate(questions, 1):
                clarification += f"\n{i}. {q}"
            clarification += "\n\nЭто поможет мне подобрать именно то, что вам нужно."

            # Save to conversation so next message will have context
            self._user_db.save_message(chat_id, "user", user_message)
            self._user_db.save_message(chat_id, "assistant", clarification)

            return clarification, []

        # Step 3: Gather product context from database
        context_parts, found_products = await self._gather_context(intent_data, current_product)

        # Log search
        self._user_db.log_search(chat_id, user_message, len(found_products))

        # Step 4: Build conversation with context
        history = self._user_db.get_conversation(chat_id, limit=10)
        messages = list(self._base_system_messages())

        # If the user has active stock-subscriptions, include them as context —
        # so questions like "на что я подписан?" / "жду ли я что-то?" answer
        # naturally without a dedicated intent.
        try:
            user_notifs = self._user_db.get_user_notifications(chat_id)
        except Exception:
            user_notifs = []
        if user_notifs:
            lines = ["АКТИВНЫЕ ПОДПИСКИ КЛИЕНТА на уведомления о поступлении:"]
            for n in user_notifs[:10]:
                lines.append(f"- {n.get('product_name', '')}")
            messages.append({"role": "system", "content": "\n".join(lines)})

        # If a list of products was previously shown to the user, include it as
        # context so references like "первый", "этот", "второй" resolve naturally.
        if shown_products:
            shown_lines = []
            for i, p in enumerate(shown_products[:MAX_PRODUCTS_IN_LIST], 1):
                price = p.get("price", 0)
                avail = max(0, p.get("rest_abs", 0) - p.get("rest_rezerv", 0))
                shown_lines.append(
                    f"{i}. {p.get('name', '')} | {price:,.0f} тг | "
                    f"{'в наличии ' + str(avail) + ' шт' if avail > 0 else 'нет в наличии'}"
                )
            if shown_lines:
                messages.append({
                    "role": "system",
                    "content": (
                        "КОНТЕКСТ ПРЕДЫДУЩЕГО СПИСКА (пользователь их видел):\n"
                        + "\n".join(shown_lines)
                        + "\n\nЕсли клиент ссылается на 'первый', 'второй', "
                        "'этот' — понимай из этого списка."
                    ),
                })

        if context_parts:
            context = "\n\n".join(context_parts)
            messages.append({
                "role": "system",
                "content": f"ДАННЫЕ ИЗ БАЗЫ АПТЕКИ:\n\n{context}"
            })
        else:
            messages.append({
                "role": "system",
                "content": (
                    "В базе аптеки не найдено товаров, соответствующих запросу клиента. "
                    "Сообщи об этом клиенту. Если можешь предположить правильное название — "
                    "попроси уточнить."
                )
            })

        # Add symptom/intent context for GPT
        if intent_data["symptoms"]:
            messages.append({
                "role": "system",
                "content": f"Клиент описал симптомы: {', '.join(intent_data['symptoms'])}. "
                           f"Рекомендуй препараты ТОЛЬКО из предоставленных данных базы. "
                           f"Обязательно порекомендуй обратиться к врачу."
            })

        if intent == "compare":
            messages.append({
                "role": "system",
                "content": "Клиент хочет СРАВНИТЬ препараты. Сравни по: цене, составу, "
                           "производителю, наличию. Укажи плюсы и минусы каждого."
            })

        # For info questions — search web for additional drug information
        if intent in ("ask_info", "ask_prescription", "ask_children", "ask_pregnancy"):
            drug_names = intent_data.get("product_names", [])
            search_terms = drug_names or intent_data.get("search_queries", [])
            if search_terms:
                web_query = f"{search_terms[0]} инструкция применение показания"
                web_info = await self._web_search(web_query)
                if web_info:
                    messages.append({
                        "role": "system",
                        "content": f"ДОПОЛНИТЕЛЬНАЯ ИНФОРМАЦИЯ ИЗ ИНТЕРНЕТА о '{search_terms[0]}':\n{web_info}\n\n"
                                   f"Используй эту информацию чтобы ответить на вопрос клиента. "
                                   f"Не ссылайся на интернет — отвечай как фармацевт от себя."
                    })

        # For follow-up info questions (user asks about a product from conversation context)
        if not intent_data.get("product_names") and not intent_data.get("search_queries"):
            # Check conversation history for product context
            if history:
                messages.append({
                    "role": "system",
                    "content": "Клиент задаёт уточняющий вопрос о препарате из предыдущего контекста. "
                               "Используй свои фармацевтические знания чтобы ответить подробно."
                })

        messages.extend(history)
        messages.append({"role": "user", "content": user_message})

        # Step 5: Generate response
        try:
            response = await self._client.chat.completions.create(
                model="gpt-4o-mini",
                messages=messages,
                temperature=0.3,
                max_tokens=1200,
            )
            answer = response.choices[0].message.content.strip()

            # Save conversation
            self._user_db.save_message(chat_id, "user", user_message)
            self._user_db.save_message(chat_id, "assistant", answer)

            return answer, found_products

        except Exception as e:
            logger.error("OpenAI API error: %s", e)
            return "Извините, произошла ошибка. Попробуйте ещё раз.", []

    def clear_history(self, chat_id: int | str):
        self._user_db.clear_conversation(chat_id)

    async def enrich_products_batch(self, batch_size: int = 50):
        """Use OpenAI to extract active ingredients and categories for products."""
        products = self._product_db.get_products_without_ai_fields(limit=batch_size)
        if not products:
            return

        logger.info("Enriching %d products with AI...", len(products))

        for product in products:
            try:
                response = await self._client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[{
                        "role": "system",
                        "content": (
                            "Определи действующее вещество и терапевтическую категорию лекарства. "
                            "Ответь JSON без markdown: "
                            '{"active_ingredient": "...", "therapeutic_category": "..."}\n'
                            "Если это не лекарство (БАД, медизделие, косметика) — укажи категорию товара. "
                            "Если не можешь определить — поставь пустые строки."
                        )
                    }, {
                        "role": "user",
                        "content": (
                            f"Название: {product['name']}\n"
                            f"Производитель: {product.get('producer', '')}\n"
                            f"Группы: {product.get('groups', '')}"
                        )
                    }],
                    temperature=0,
                    max_tokens=150,
                )

                text = response.choices[0].message.content.strip()
                if text.startswith("```"):
                    text = text.split("\n", 1)[1].rsplit("```", 1)[0]
                data = json.loads(text)

                self._product_db.update_product_ai_fields(
                    product["guid"],
                    active_ingredient=data.get("active_ingredient", ""),
                    therapeutic_category=data.get("therapeutic_category", ""),
                )
            except Exception as e:
                logger.warning("Failed to enrich %s: %s", product["name"][:30], e)
