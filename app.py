import asyncio
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import aiosqlite
import uvicorn
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, Message, ReplyKeyboardMarkup
from fastapi import FastAPI
from pydantic import BaseModel, Field

DATABASE_PATH = Path(os.getenv("DATABASE_PATH", "data/finance.db"))
BOT_TOKEN = os.getenv("BOT_TOKEN", "8964213012:AAEHV3dHlPUn9J3WOtgav21XfyXsjjk6c4I")
USER_CHAT_ID = os.getenv("USER_CHAT_ID", "879317791")
PORT = int(os.getenv("PORT", "8000"))
DAILY_LIMIT = 29.12
RISK_WORDS = ("p2p", "crypto", "bybit", "binance", "game", "steam", "caser", "phantom", "pay")

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    amount REAL NOT NULL,
    currency TEXT NOT NULL,
    merchant TEXT NOT NULL,
    source TEXT NOT NULL
)
"""
CREATE_SETTINGS_SQL = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
)
"""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def format_amount(amount: float, currency: str = "BYN") -> str:
    return f"{amount:.2f} {currency}"


class TransactionInput(BaseModel):
    amount: float = Field(gt=0)
    currency: str = Field(default="BYN", min_length=1, max_length=8)
    merchant: str = Field(default="Unknown", min_length=1, max_length=200)
    timestamp: datetime | None = None


class TransactionResponse(BaseModel):
    id: int
    risks: list[str]


class FinanceStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = await aiosqlite.connect(self.path)
        await self.db.execute(CREATE_TABLE_SQL)
        await self.db.execute(CREATE_SETTINGS_SQL)
        await self.db.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES ('daily_limit', ?)",
            (str(DAILY_LIMIT),),
        )
        await self.db.commit()

    async def close(self) -> None:
        if self.db is not None:
            await self.db.close()
            self.db = None

    def _connection(self) -> aiosqlite.Connection:
        if self.db is None:
            raise RuntimeError("Database is not connected")
        return self.db

    async def add_transaction(
        self,
        amount: float,
        currency: str,
        merchant: str,
        source: str,
        timestamp: datetime | None = None,
    ) -> tuple[int, list[str]]:
        db = self._connection()
        transaction_time = (timestamp or utc_now()).astimezone(timezone.utc)
        timestamp_text = transaction_time.isoformat()
        day_start = transaction_time.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        window_start = (transaction_time - timedelta(minutes=60)).isoformat()

        async with db.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM transactions "
            "WHERE currency = ? AND timestamp >= ? AND timestamp <= ?",
            (currency, day_start, timestamp_text),
        ) as cursor:
            daily_total = float((await cursor.fetchone())[0]) + amount

        async with db.execute(
            "SELECT COUNT(*) FROM transactions WHERE timestamp > ? AND timestamp <= ?",
            (window_start, timestamp_text),
        ) as cursor:
            recent_count = int((await cursor.fetchone())[0])

        risks: list[str] = []
        daily_limit = await self.get_limit()
        if currency.upper() == "BYN" and daily_total > daily_limit:
            risks.append(f"Суточный лимит превышен: {daily_total:.2f} BYN > {daily_limit:.2f} BYN")
        merchant_lower = merchant.casefold()
        matched_words = [word for word in RISK_WORDS if word in merchant_lower]
        if matched_words:
            risks.append(f"Стоп-слово в названии мерчанта: {', '.join(matched_words)}")
        if recent_count >= 1:
            risks.append("Серийные траты: минимум 2 покупки за последние 60 минут")

        cursor = await db.execute(
            "INSERT INTO transactions (timestamp, amount, currency, merchant, source) VALUES (?, ?, ?, ?, ?)",
            (timestamp_text, amount, currency.upper(), merchant, source),
        )
        await db.commit()
        return int(cursor.lastrowid), risks

    async def today_stats(self) -> dict[str, Any]:
        return await self.period_stats(*period_bounds("day"))

    async def get_limit(self) -> float:
        async with self._connection().execute("SELECT value FROM settings WHERE key = 'daily_limit'") as cursor:
            row = await cursor.fetchone()
        return float(row[0]) if row else DAILY_LIMIT

    async def set_limit(self, amount: float) -> None:
        db = self._connection()
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('daily_limit', ?)", (str(amount),))
        await db.commit()

    async def period_stats(self, start: str, end: str) -> dict[str, Any]:
        db = self._connection()
        conditions = "timestamp >= ? AND timestamp < ?"
        parameters = (start, end)
        async with db.execute(
            f"SELECT COUNT(*), COALESCE(SUM(amount), 0) FROM transactions WHERE {conditions} AND currency = 'BYN'",
            parameters,
        ) as cursor:
            count, total = await cursor.fetchone()
        async with db.execute(
            f"SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE {conditions} AND currency = 'BYN' AND source = 'card'",
            parameters,
        ) as cursor:
            card_total = float((await cursor.fetchone())[0])
        async with db.execute(
            f"SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE {conditions} AND currency = 'BYN' AND source = 'cash'",
            parameters,
        ) as cursor:
            cash_total = float((await cursor.fetchone())[0])
        return {"count": int(count), "total": float(total), "card": card_total, "cash": cash_total}

    async def self_control(self) -> dict[str, Any]:
        since = (utc_now() - timedelta(hours=2)).isoformat()
        async with self._connection().execute(
            "SELECT COUNT(*), COALESCE(SUM(amount), 0) FROM transactions WHERE timestamp >= ? AND currency = 'BYN'",
            (since,),
        ) as cursor:
            count, total = await cursor.fetchone()
        async with self._connection().execute(
            "SELECT merchant FROM transactions WHERE timestamp >= ? AND currency = 'BYN'",
            (since,),
        ) as cursor:
            merchants = [row[0].casefold() for row in await cursor.fetchall()]
        matched = sorted({word for merchant in merchants for word in RISK_WORDS if word in merchant})
        return {"count": int(count), "total": float(total), "words": matched}


store = FinanceStore(DATABASE_PATH)
router = Router()


class InputStates(StatesGroup):
    waiting_for_limit = State()
    waiting_for_cash = State()


MAIN_KEYBOARD = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📊 Аналитика расходов"), KeyboardButton(text="🎯 Изменить лимит")],
        [KeyboardButton(text="💵 +1 BYN (Транспорт)"), KeyboardButton(text="💵 +2 BYN"), KeyboardButton(text="💵 +5 BYN"), KeyboardButton(text="💵 +10 BYN")],
        [KeyboardButton(text="✍️ Ввести наличные"), KeyboardButton(text="🛡 Статус самоконтроля")],
    ],
    resize_keyboard=True,
)


ANALYTICS_KEYBOARD = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(text="📅 День", callback_data="analytics:day"),
            InlineKeyboardButton(text="🗓 Месяц", callback_data="analytics:month"),
            InlineKeyboardButton(text="📈 Год", callback_data="analytics:year"),
        ]
    ]
)


def is_allowed(message: Message) -> bool:
    return USER_CHAT_ID != "0" and str(message.chat.id) == USER_CHAT_ID


async def answer_with_keyboard(message: Message, text: str) -> None:
    await message.answer(text, reply_markup=MAIN_KEYBOARD)


def period_bounds(period: str) -> tuple[str, str]:
    now = utc_now()
    if period == "day":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return start.isoformat(), (start + timedelta(days=1)).isoformat()
    if period == "month":
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        next_month = start.replace(year=start.year + (start.month == 12), month=1 if start.month == 12 else start.month + 1)
        return start.isoformat(), next_month.isoformat()
    start = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    return start.isoformat(), start.replace(year=start.year + 1).isoformat()


async def analytics_text(period: str) -> str:
    start, end = period_bounds(period)
    stats = await store.period_stats(start, end)
    if period == "day":
        limit = await store.get_limit()
        difference = stats["total"] - limit
        status = f"превышение на {difference:.2f} BYN" if difference > 0 else f"осталось {abs(difference):.2f} BYN"
        return (f"📅 Сегодня\nВсего: {format_amount(stats['total'])}\n"
                f"Карта: {format_amount(stats['card'])}\nНаличные: {format_amount(stats['cash'])}\n"
                f"Лимит: {format_amount(limit)} ({status})")
    if period == "month":
        days_elapsed = max(1, utc_now().day)
        return (f"🗓 Текущий месяц\nВсего: {format_amount(stats['total'])}\n"
                f"Карта: {format_amount(stats['card'])}\nНаличные: {format_amount(stats['cash'])}\n"
                f"Среднее за прошедший день: {format_amount(stats['total'] / days_elapsed)}")
    return (f"📈 Текущий год\nВсего: {format_amount(stats['total'])}\n"
            f"Карта: {format_amount(stats['card'])}\nНаличные: {format_amount(stats['cash'])}")


@router.message(Command("cash"))
async def cash_command(message: Message, command: CommandObject) -> None:
    if not is_allowed(message):
        return
    args = (command.args or "").split(maxsplit=1)
    if len(args) != 2:
        await message.answer("Формат: /cash <сумма> <описание>")
        return
    try:
        amount = float(args[0].replace(",", "."))
        if amount <= 0:
            raise ValueError
    except ValueError:
        await message.answer("Сумма должна быть положительным числом.")
        return
    transaction_id, risks = await store.add_transaction(amount, "BYN", args[1], "cash")
    response = f"Наличный расход #{transaction_id}: {format_amount(amount)}\n{args[1]}"
    if risks:
        response += "\n⚠️ " + "\n⚠️ ".join(risks)
    await answer_with_keyboard(message, response)


@router.message(Command("start"))
async def start_command(message: Message) -> None:
    if is_allowed(message):
        await answer_with_keyboard(message, "Финансовый трекер готов. Выберите действие:")


async def record_quick_cash(message: Message, amount: float, merchant: str) -> None:
    transaction_id, risks = await store.add_transaction(amount, "BYN", merchant, "cash")
    response = f"Записано #{transaction_id}: {format_amount(amount)}\n{merchant}"
    if risks:
        response += "\n⚠️ " + "\n⚠️ ".join(risks)
    await answer_with_keyboard(message, response)


@router.message(F.text == "💵 +1 BYN (Транспорт)")
async def quick_transport(message: Message) -> None:
    if is_allowed(message):
        await record_quick_cash(message, 1, "Транспорт/Метро")


@router.message(F.text.in_({"💵 +2 BYN", "💵 +5 BYN", "💵 +10 BYN"}))
async def quick_cash(message: Message) -> None:
    if is_allowed(message):
        amount = float(message.text.split("+")[1].split()[0])
        await record_quick_cash(message, amount, "Быстрый расход")


@router.message(F.text == "🎯 Изменить лимит")
async def change_limit_start(message: Message, state: FSMContext) -> None:
    if is_allowed(message):
        await state.set_state(InputStates.waiting_for_limit)
        await answer_with_keyboard(message, "Введите новый дневной лимит в BYN:")


@router.message(InputStates.waiting_for_limit)
async def change_limit_value(message: Message, state: FSMContext) -> None:
    if not is_allowed(message):
        return
    try:
        limit = float((message.text or "").replace(",", "."))
        if limit <= 0:
            raise ValueError
    except ValueError:
        await message.answer("Введите положительное число, например 29.12.")
        return
    await store.set_limit(limit)
    await state.clear()
    await answer_with_keyboard(message, f"Дневной лимит изменён: {format_amount(limit)}")


@router.message(F.text == "✍️ Ввести наличные")
async def cash_input_start(message: Message, state: FSMContext) -> None:
    if is_allowed(message):
        await state.set_state(InputStates.waiting_for_cash)
        await answer_with_keyboard(message, "Введите расход в формате: сумма описание\nНапример: 14.50 Обед")


@router.message(InputStates.waiting_for_cash)
async def cash_input_value(message: Message, state: FSMContext) -> None:
    if not is_allowed(message):
        return
    args = (message.text or "").split(maxsplit=1)
    try:
        amount = float(args[0].replace(",", "."))
        if amount <= 0 or len(args) != 2:
            raise ValueError
    except (ValueError, IndexError):
        await message.answer("Формат: <сумма> <описание>, например 14.50 Обед")
        return
    await state.clear()
    await record_quick_cash(message, amount, args[1])


@router.message(F.text == "📊 Аналитика расходов")
async def analytics_button(message: Message) -> None:
    if is_allowed(message):
        await message.answer("Выберите период:", reply_markup=ANALYTICS_KEYBOARD)


@router.message(Command("stat"))
async def stat_command(message: Message) -> None:
    if is_allowed(message):
        await message.answer(await analytics_text("day"), reply_markup=ANALYTICS_KEYBOARD)


@router.callback_query(F.data.startswith("analytics:"))
async def analytics_callback(callback: CallbackQuery) -> None:
    if callback.message is None or str(callback.message.chat.id) != USER_CHAT_ID:
        await callback.answer()
        return
    period = callback.data.split(":", 1)[1]
    await callback.message.edit_text(await analytics_text(period), reply_markup=ANALYTICS_KEYBOARD)
    await callback.answer()


@router.message(F.text == "🛡 Статус самоконтроля")
async def self_control_status(message: Message) -> None:
    if not is_allowed(message):
        return
    status = await store.self_control()
    trigger = ", ".join(status["words"]) if status["words"] else "нет"
    await answer_with_keyboard(
        message,
        f"🛡 Самоконтроль за последние 2 часа\n"
        f"Транзакций: {status['count']}\nСумма: {format_amount(status['total'])}\n"
        f"Стоп-слова: {trigger}",
    )


@asynccontextmanager
async def lifespan(_: FastAPI):
    await store.connect()
    yield
    await store.close()


app = FastAPI(title="Finance Tracker", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/tx", response_model=TransactionResponse)
@app.post("/webhook", response_model=TransactionResponse)
async def receive_transaction(transaction: TransactionInput) -> TransactionResponse:
    transaction_id, risks = await store.add_transaction(
        amount=transaction.amount,
        currency=transaction.currency,
        merchant=transaction.merchant,
        source="card",
        timestamp=transaction.timestamp,
    )
    return TransactionResponse(id=transaction_id, risks=risks)


async def run() -> None:
    bot = Bot(token=BOT_TOKEN)
    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher.include_router(router)
    server = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=PORT, log_level="info"))
    try:
        await asyncio.gather(server.serve(), dispatcher.start_polling(bot))
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(run())
