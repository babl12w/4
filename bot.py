import asyncio
import logging
import os
import tempfile
from pathlib import Path
from typing import Any
from uuid import uuid4

import aiohttp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)


class BotStates(StatesGroup):
    awaiting_poll_choice = State()
    awaiting_post_confirm = State()
    awaiting_poll_confirm = State()


REQUIRED_ENV = [
    "BOT_TOKEN",
    "JAMENDO_CLIENT_ID",
    "UNSPLASH_ACCESS_KEY",
    "ADMIN_ID",
    "CHANNEL_ID",
]


def get_env() -> dict[str, str]:
    cfg: dict[str, str] = {}
    missing: list[str] = []
    for key in REQUIRED_ENV:
        value = os.getenv(key)
        if not value:
            missing.append(key)
        else:
            cfg[key] = value
    if missing:
        raise RuntimeError(f"Missing required env vars: {', '.join(missing)}")
    return cfg


CONFIG = get_env()
BOT_TOKEN = CONFIG["BOT_TOKEN"]
JAMENDO_CLIENT_ID = CONFIG["JAMENDO_CLIENT_ID"]
UNSPLASH_ACCESS_KEY = CONFIG["UNSPLASH_ACCESS_KEY"]
ADMIN_ID = int(CONFIG["ADMIN_ID"])
CHANNEL_ID = CONFIG["CHANNEL_ID"]


MAIN_MENU = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="1️⃣ Новий пост")],
        [KeyboardButton(text="2️⃣ Опитування")],
        [KeyboardButton(text="3️⃣ Скасувати")],
    ],
    resize_keyboard=True,
)

CONFIRM_KB = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Опублікувати", callback_data="publish"),
            InlineKeyboardButton(text="❌ Скасувати", callback_data="cancel"),
        ]
    ]
)

POLL_TEMPLATES: list[dict[str, Any]] = [
    {
        "id": "poll_1",
        "title": "Опитування 1",
        "question": "Який вайб сьогодні ввечері?",
        "options": ["Lo-fi chill", "Deep house", "Indie acoustic", "Synthwave"],
        "is_anonymous": False,
        "allows_multiple_answers": False,
    },
    {
        "id": "poll_2",
        "title": "Опитування 2",
        "question": "Що включаємо для нічного вайбу?",
        "options": ["Ambient", "Jazzhop", "Downtempo", "Neo soul"],
        "is_anonymous": False,
        "allows_multiple_answers": False,
    },
    {
        "id": "poll_3",
        "title": "Опитування 3",
        "question": "Який формат постів вам ближчий?",
        "options": ["Більше нових треків", "Більше цитат", "Більше опитувань", "Мікс усього"],
        "is_anonymous": False,
        "allows_multiple_answers": False,
    },
    {
        "id": "poll_4",
        "title": "Опитування 4",
        "question": "Коли публікувати вечірні добірки?",
        "options": ["18:00", "20:00", "22:00", "Після опівночі"],
        "is_anonymous": False,
        "allows_multiple_answers": False,
    },
]

router = Router()


def is_admin(message_or_callback: Message | CallbackQuery) -> bool:
    user = (
        message_or_callback.from_user
        if isinstance(message_or_callback, CallbackQuery)
        else message_or_callback.from_user
    )
    return bool(user and user.id == ADMIN_ID)


async def deny_access(message_or_callback: Message | CallbackQuery) -> None:
    text = "⛔ Доступ дозволено тільки адміністратору."
    if isinstance(message_or_callback, CallbackQuery):
        await message_or_callback.answer(text, show_alert=True)
    else:
        await message_or_callback.answer(text)


async def safe_remove_files(paths: list[str]) -> None:
    for path in paths:
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            continue


async def fetch_json(session: aiohttp.ClientSession, url: str, params: dict[str, Any]) -> Any:
    async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=25)) as response:
        response.raise_for_status()
        return await response.json()


async def download_file(session: aiohttp.ClientSession, url: str, destination: Path) -> str:
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=60)) as response:
        response.raise_for_status()
        with destination.open("wb") as file:
            async for chunk in response.content.iter_chunked(1024 * 128):
                file.write(chunk)
    return str(destination)


async def get_jamendo_tracks(session: aiohttp.ClientSession) -> list[dict[str, str]]:
    params = {
        "client_id": JAMENDO_CLIENT_ID,
        "format": "json",
        "limit": 30,
        "include": "musicinfo",
        "audioformat": "mp32",
        "fuzzytags": "chill,ambient,lofi,instrumental",
        "orderby": "popularity_total",
    }
    payload = await fetch_json(session, "https://api.jamendo.com/v3.0/tracks/", params)
    results = payload.get("results", [])
    tracks: list[dict[str, str]] = []

    for item in results:
        audio = item.get("audio")
        name = item.get("name")
        artist = item.get("artist_name")
        if audio and name and artist:
            tracks.append({"title": str(name), "artist": str(artist), "audio": str(audio)})
        if len(tracks) == 2:
            break

    if len(tracks) != 2:
        raise RuntimeError("Jamendo повернув недостатньо треків.")

    return tracks


async def get_unsplash_photo_url(session: aiohttp.ClientSession) -> str:
    params = {
        "client_id": UNSPLASH_ACCESS_KEY,
        "query": "music vibe night neon",
        "orientation": "portrait",
        "content_filter": "high",
    }
    data = await fetch_json(session, "https://api.unsplash.com/photos/random", params)
    image_url = data.get("urls", {}).get("regular")
    if not image_url:
        raise RuntimeError("Unsplash не повернув фото.")
    return str(image_url)


async def get_quote(session: aiohttp.ClientSession) -> str:
    quote_sources = [
        ("https://zenquotes.io/api/random", {}),
        ("https://api.quotable.io/random", {"tags": "inspirational"}),
    ]

    for url, params in quote_sources:
        try:
            data = await fetch_json(session, url, params)
            if "zenquotes" in url and isinstance(data, list) and data:
                text = data[0].get("q", "").strip()
                author = data[0].get("a", "").strip()
            else:
                text = str(data.get("content", "")).strip()
                author = str(data.get("author", "")).strip()

            if text:
                snippet = "\n".join(text.split(". ")[:2]).strip()
                lines = [
                    "🎧 Атмосфера вечора.",
                    snippet,
                    f"— {author or 'Unknown'}",
                ]
                return "\n".join(lines)
        except Exception:
            continue

    return "🎧 Музика лікує тишу.\nКожен звук — настрій.\n— Music Vibes"


async def build_post_assets() -> dict[str, Any]:
    tmp_dir = Path(tempfile.gettempdir()) / f"music_bot_{uuid4().hex}"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    async with aiohttp.ClientSession() as session:
        tracks = await get_jamendo_tracks(session)
        image_url = await get_unsplash_photo_url(session)
        quote = await get_quote(session)

        photo_path = await download_file(session, image_url, tmp_dir / "photo.jpg")

        audio_paths: list[str] = []
        for idx, track in enumerate(tracks, start=1):
            audio_paths.append(
                await download_file(session, track["audio"], tmp_dir / f"track_{idx}.mp3")
            )

    return {
        "tmp_dir": str(tmp_dir),
        "photo_path": photo_path,
        "audio_paths": audio_paths,
        "tracks": tracks,
        "caption": quote,
    }


async def send_main_menu(message: Message) -> None:
    await message.answer("Обери дію:", reply_markup=MAIN_MENU)


def poll_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=item["title"], callback_data=f"pick_{item['id']}")]
        for item in POLL_TEMPLATES
    ]
    rows.append([InlineKeyboardButton(text="❌ Скасувати", callback_data="cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_poll_by_id(poll_id: str) -> dict[str, Any] | None:
    for item in POLL_TEMPLATES:
        if item["id"] == poll_id:
            return item
    return None


@router.message(CommandStart())
async def start_handler(message: Message, state: FSMContext) -> None:
    if not is_admin(message):
        await deny_access(message)
        return

    await state.clear()
    await send_main_menu(message)


@router.message(F.text == "3️⃣ Скасувати")
async def cancel_handler(message: Message, state: FSMContext) -> None:
    if not is_admin(message):
        await deny_access(message)
        return

    data = await state.get_data()
    files_to_remove = []
    tmp_dir = data.get("tmp_dir")
    if tmp_dir:
        tmp_path = Path(tmp_dir)
        if tmp_path.exists():
            files_to_remove.extend([str(p) for p in tmp_path.glob("*")])
            files_to_remove.append(str(tmp_path))

    await safe_remove_files(files_to_remove)
    if tmp_dir:
        Path(tmp_dir).rmdir() if Path(tmp_dir).exists() else None

    await state.clear()
    await message.answer("Скасовано. Стан скинуто.")
    await send_main_menu(message)


@router.message(F.text == "1️⃣ Новий пост")
async def new_post_handler(message: Message, state: FSMContext) -> None:
    if not is_admin(message):
        await deny_access(message)
        return

    await state.clear()
    progress = await message.answer("Генерую превʼю посту, зачекай...")

    try:
        assets = await build_post_assets()
    except Exception as error:
        logging.exception("Post creation failed: %s", error)
        await progress.edit_text("❌ Не вдалося створити пост. Спробуй ще раз.")
        await send_main_menu(message)
        return

    await state.update_data(**assets)

    try:
        await message.answer_photo(
            FSInputFile(assets["photo_path"]),
            caption=assets["caption"],
        )

        for index, audio_path in enumerate(assets["audio_paths"], start=1):
            track = assets["tracks"][index - 1]
            await message.answer_audio(
                FSInputFile(audio_path),
                title=track["title"],
                performer=track["artist"],
            )

        await progress.edit_text("Превʼю готове.")
        await message.answer("Підтвердити публікацію?", reply_markup=CONFIRM_KB)
        await state.set_state(BotStates.awaiting_post_confirm)
    except Exception as error:
        logging.exception("Preview sending failed: %s", error)
        await progress.edit_text("❌ Помилка під час надсилання превʼю.")
        await cancel_handler(message, state)


@router.message(F.text == "2️⃣ Опитування")
async def poll_menu_handler(message: Message, state: FSMContext) -> None:
    if not is_admin(message):
        await deny_access(message)
        return

    await state.clear()
    await state.set_state(BotStates.awaiting_poll_choice)
    await message.answer("Обери заготовлене опитування:", reply_markup=poll_keyboard())


@router.callback_query(F.data.startswith("pick_"), BotStates.awaiting_poll_choice)
async def pick_poll_handler(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback):
        await deny_access(callback)
        return

    poll_id = callback.data.replace("pick_", "", 1)
    poll = get_poll_by_id(poll_id)
    if not poll:
        await callback.answer("Опитування не знайдено", show_alert=True)
        return

    await state.update_data(selected_poll=poll)

    await callback.message.answer_poll(
        question=poll["question"],
        options=poll["options"],
        is_anonymous=poll["is_anonymous"],
        allows_multiple_answers=poll["allows_multiple_answers"],
    )
    await callback.message.answer("Підтвердити публікацію опитування?", reply_markup=CONFIRM_KB)
    await state.set_state(BotStates.awaiting_poll_confirm)
    await callback.answer()


@router.callback_query(F.data == "cancel")
async def inline_cancel_handler(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback):
        await deny_access(callback)
        return

    data = await state.get_data()
    tmp_dir = data.get("tmp_dir")
    if tmp_dir:
        tmp_path = Path(tmp_dir)
        if tmp_path.exists():
            for file in tmp_path.glob("*"):
                file.unlink(missing_ok=True)
            tmp_path.rmdir()

    await state.clear()
    await callback.message.answer("Скасовано. Повертаю головне меню.", reply_markup=MAIN_MENU)
    await callback.answer()


@router.callback_query(F.data == "publish", BotStates.awaiting_post_confirm)
async def publish_post_handler(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    if not is_admin(callback):
        await deny_access(callback)
        return

    data = await state.get_data()
    photo_path = data.get("photo_path")
    audio_paths = data.get("audio_paths", [])
    tracks = data.get("tracks", [])
    caption = data.get("caption", "")
    tmp_dir = data.get("tmp_dir")

    if not photo_path or len(audio_paths) != 2 or len(tracks) != 2:
        await callback.answer("Немає даних для публікації", show_alert=True)
        return

    try:
        await bot.send_photo(CHANNEL_ID, FSInputFile(photo_path), caption=caption)
        for index, audio_path in enumerate(audio_paths, start=1):
            track = tracks[index - 1]
            await bot.send_audio(
                CHANNEL_ID,
                FSInputFile(audio_path),
                title=track["title"],
                performer=track["artist"],
            )
        await callback.message.answer("✅ Опубліковано в канал.", reply_markup=MAIN_MENU)
    except TelegramBadRequest as error:
        logging.exception("Telegram publish error: %s", error)
        await callback.message.answer("❌ Не вдалося опублікувати пост.", reply_markup=MAIN_MENU)
    finally:
        if tmp_dir:
            tmp_path = Path(tmp_dir)
            if tmp_path.exists():
                for file in tmp_path.glob("*"):
                    file.unlink(missing_ok=True)
                tmp_path.rmdir()
        await state.clear()
        await callback.answer()


@router.callback_query(F.data == "publish", BotStates.awaiting_poll_confirm)
async def publish_poll_handler(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    if not is_admin(callback):
        await deny_access(callback)
        return

    data = await state.get_data()
    poll = data.get("selected_poll")

    if not poll:
        await callback.answer("Опитування не вибране", show_alert=True)
        return

    try:
        await bot.send_poll(
            CHANNEL_ID,
            question=poll["question"],
            options=poll["options"],
            is_anonymous=poll["is_anonymous"],
            allows_multiple_answers=poll["allows_multiple_answers"],
        )
        await callback.message.answer("✅ Опитування опубліковано в канал.", reply_markup=MAIN_MENU)
    except TelegramBadRequest as error:
        logging.exception("Poll publish error: %s", error)
        await callback.message.answer("❌ Не вдалося опублікувати опитування.", reply_markup=MAIN_MENU)
    finally:
        await state.clear()
        await callback.answer()


@router.message()
async def fallback_handler(message: Message) -> None:
    if not is_admin(message):
        await deny_access(message)
        return
    await message.answer("Використай кнопки меню.", reply_markup=MAIN_MENU)


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass

