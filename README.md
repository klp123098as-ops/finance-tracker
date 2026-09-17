# Finance Tracker

Асинхронный финансовый трекер на Python 3.11+: FastAPI принимает расходы с iOS Shortcuts, а aiogram-бот позволяет добавлять наличные расходы и смотреть статистику.

## Запуск локально

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:BOT_TOKEN = "токен_бота"
$env:USER_CHAT_ID = "ваш_chat_id"
python app.py
```

Переменные `BOT_TOKEN` и `USER_CHAT_ID` имеют локальные значения по умолчанию, но для работы Telegram нужно задать реальные значения. База создаётся автоматически в `data/finance.db`. Порт берётся из `PORT` и по умолчанию равен `8000`.

## Webhook

POST `/webhook` принимает JSON:

```json
{
  "amount": 12.5,
  "currency": "BYN",
  "merchant": "Coffee Shop"
}
```

Поле `timestamp` необязательно и принимает ISO 8601. Ответ содержит ID записи и сработавшие риск-триггеры. Для iOS Shortcuts используйте URL публичного сервера, например `https://your-host.example/webhook`.

## Telegram

- `/cash <сумма> <описание>` добавляет наличный расход в BYN.
- `/stat` показывает операции и сумму за текущие сутки UTC.

Команды обрабатываются только для чата, чей ID совпадает с `USER_CHAT_ID`.

## Риск-триггеры

- суточная сумма расходов в BYN выше `29.12`;
- стоп-слова в названии мерчанта: `p2p`, `crypto`, `bybit`, `binance`, `game`, `steam`, `caser`, `phantom`, `pay`;
- предыдущая покупка в течение 60 минут.

## Render

Файлы `Procfile` и `render.yaml` готовы для Render. Добавьте секретные переменные `BOT_TOKEN` и `USER_CHAT_ID` в настройках сервиса. Для webhook нужен публичный URL Render.
