# Деплой APTEKA на Fly.io

Полная инструкция для развертывания на бесплатном (фактически — в рамках $5 кредита) тарифе Fly.io. После настройки бот работает 24/7, не засыпает, SQLite живёт постоянно.

## Что получите

- **Постоянный публичный URL**: `https://apteka-bot.fly.dev` (имя меняется в `fly.toml`)
- **Wazzup webhook** автоматически регистрируется на этот URL при старте
- **SQLite + AI-эмбеддинги** на постоянном volume (3 ГБ free)
- **Нет «засыпания»** в отличие от Render Free
- **Стоимость**: одна shared-cpu-1x@512MB машина + 1 GB volume = **$0** (полностью покрывается ежемесячным $5 кредитом Fly)

---

## Шаг 1. Установить Fly CLI

```bash
brew install flyctl     # macOS
# или: curl -L https://fly.io/install.sh | sh   # Linux
```

## Шаг 2. Создать аккаунт и залогиниться

```bash
fly auth signup    # если новый
fly auth login     # если уже есть
```

При регистрации Fly попросит привязать карту — это нужно для антифрода, **списания нет**, $5 кредита покрывают нашу конфигурацию с запасом.

## Шаг 3. Запустить из директории проекта

```bash
cd /Users/a1111/Desktop/projects/APTEKA
fly launch --no-deploy --copy-config
```

Fly прочитает существующий `fly.toml`. На вопросы ответьте:
- **Choose region**: `fra` (Frankfurt) — ближайший к Казахстану в free-tier
- **Use existing fly.toml?** → Yes
- **Set up Postgres / Redis / sentry?** → No (нам не нужны)
- **Deploy now?** → No (сначала зальём секреты)

## Шаг 4. Создать постоянный volume для SQLite

```bash
fly volumes create apteka_data --region fra --size 1
```

`apteka_data` — должно совпадать с `[[mounts]] source` в `fly.toml`. 1 GB достаточно с большим запасом.

## Шаг 5. Загрузить секреты (env-переменные)

```bash
fly secrets set \
  TELEGRAM_BOT_TOKEN="8657401745:AAFkQMByTbSVZh8cqb8lWMmA5c70HSgZ9OU" \
  OPENAI_API_KEY="sk-proj-..." \
  RUBUS_TOKEN="m6gtcs5kb4jh7lnjh14h8" \
  RUBUS_DEVICE="1584" \
  RUBUS_BIN="870127400057" \
  WAZZUP_API_KEY="17332741ec9d42eaa1726db792bd46c6" \
  WAZZUP_CHANNEL_ID="ab31afec-d660-4f5a-aa4a-52f1fa085ae4" \
  WAZZUP_WEBHOOK_URL="https://apteka-bot.fly.dev/wazzup/webhook" \
  WAZZUP_WEBHOOK_SECRET="$(openssl rand -hex 32)" \
  WAZZUP_FORWARD_MAP="9a515b32-a33e-4c21-baf6-bab908170d1b=https://toi-nonlegal-nonpreciously.ngrok-free.dev/wazzup/webhook" \
  PHARMACY_NAME="Аптека Алмалы" \
  PHARMACY_ADDRESS="Алматы, ул. Абая 150" \
  PHARMACY_PHONE="+7 727 333 44 55" \
  PHARMACY_HOURS="Пн-Вс 08:00-22:00" \
  PHARMACY_DELIVERY="Курьер по Алматы 1500тг, от 10000тг бесплатно" \
  PHARMACY_PAYMENT="Наличные, Kaspi QR, Halyk, Visa/MC" \
  PHARMACY_ADMIN_CHAT_ID="77XXXXXXXXX"
```

> Замените значения на свои реальные. `WAZZUP_WEBHOOK_URL` должен соответствовать имени приложения из `fly.toml` (по умолчанию `apteka-bot`). `WAZZUP_WEBHOOK_SECRET` — лучше сгенерировать заново на сервере.

## Шаг 6. Деплой

```bash
fly deploy
```

Fly соберёт Docker-образ, загрузит на серверы, поднимет машину, прицепит volume, начнёт healthcheck. Через 1-2 минуты:

```
✓ Configuration is valid
✓ Image deployed
✓ Health check passing
✓ App is healthy at https://apteka-bot.fly.dev
```

## Шаг 7. Проверка

```bash
# Логи в реальном времени
fly logs

# Статус машин
fly status

# Открыть админку в браузере
fly open

# Открыть конкретную страницу
open https://apteka-bot.fly.dev/analytics
```

В Wazzup вебхук должен автоматически зарегистрироваться на `https://apteka-bot.fly.dev/wazzup/webhook` при старте (логи покажут «Wazzup webhook auto-registered»).

---

## Обновить код (повторный деплой)

```bash
git add -A && git commit -m "update"
fly deploy
```

Volume не трогается — все данные (SQLite, эмбеддинги, история, подписки) сохраняются.

## Посмотреть данные

```bash
# Открыть SSH в контейнер
fly ssh console

# Внутри контейнера:
sqlite3 /data/apteka.db "SELECT COUNT(*) FROM products;"
sqlite3 /data/apteka.db "SELECT chat_id, platform, last_active FROM users LIMIT 10;"
exit
```

## Сбросить базу (если новая аптека)

```bash
fly ssh console -C "rm -f /data/apteka.db /data/apteka.db-shm /data/apteka.db-wal"
fly machine restart
# После рестарта приложение пересоздаст пустую БД и подтянет каталог из RUBUS заново
```

## Подсмотреть/обновить секреты

```bash
fly secrets list                          # список (значений не показывает)
fly secrets set OPENAI_API_KEY="новый"    # обновит и автоматически перезапустит
fly secrets unset PHARMACY_INSTAGRAM      # удалить
```

## Масштабирование

```bash
fly scale memory 1024     # на 1 GB RAM (всё ещё в кредите $5)
fly scale count 1         # одна машина (нельзя 2 — SQLite single-writer)
```

---

## Тонкости и подводные камни

- **Только одна машина**. SQLite не поддерживает многих писателей. Если когда-нибудь понадобится горизонтальное масштабирование — переход на PostgreSQL (Fly.io предлагает managed Postgres).
- **Telegram polling vs webhook**. Сейчас бот опрашивает Telegram (`getUpdates`). При деплое **в любой момент работает только одна копия**, иначе будет `409 Conflict`. Если запускаете локально для отладки — сначала остановите Fly: `fly scale count 0`, потом включайте обратно: `fly scale count 1`.
- **Стоимость свыше Free**. Если активно используется, потребление ОЗУ может вырасти при больших каталогах (10k+ товаров с эмбеддингами). На 512 MB запаса хватает на 5k товаров. Если упадёт по OOM — `fly scale memory 1024` (≈$3-4/месяц сверх кредита).
- **Возрождение-форвард**. `WAZZUP_FORWARD_MAP` пересылает чужой канал на ngrok-URL Возрождения. Если Возрождение тоже переедет на Fly — поменяйте URL на её `*.fly.dev`.
- **Логи**. Fly хранит логи **в реальном времени**, история ограничена. Для долгого хранения — подключить Logtail/Datadog или собирать через `fly logs > /tmp/log` локально.
- **Bot Token security**. После загрузки в `fly secrets` они зашифрованы. Локальный `.env` остаётся нужен только для запуска `python3 main.py` на вашей машине.

---

## Откат на ngrok / локалку

Если что-то пошло не так и нужно временно вернуться:

```bash
fly scale count 0          # остановить Fly-машину
# Локально:
ngrok http 5001
# Прописать новый ngrok URL в Wazzup webhook (через .env + перезапуск main.py)
```

И обратно:

```bash
fly scale count 1
```
