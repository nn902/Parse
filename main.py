import asyncio
import hashlib
import html
import json
import logging
import os
import re
from urllib.parse import urljoin

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import Message, InlineKeyboardButton, InlineKeyboardMarkup

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("wb_bot")

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.getenv("ADMIN_ID", "7727345054"))
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "300"))
MIN_DISCOUNT = float(os.getenv("MIN_DISCOUNT", "25"))
MAX_PRICE = float(os.getenv("MAX_PRICE", "7000"))
DEFAULT_URL = os.getenv(
    "WB_SELLER_URL",
    "https://www.wildberries.ru/seller/589603"
)

bot = Bot(BOT_TOKEN)
dp = Dispatcher()

watch_url = DEFAULT_URL
sent_products: dict[str, str] = {}


def money(value) -> float:
    if value is None:
        return 0

    text = str(value)
    text = re.sub(r"[^\d,.]", "", text)
    text = text.replace(" ", "").replace(",", ".")

    try:
        return float(text)
    except ValueError:
        return 0


def make_id(product: dict) -> str:
    raw = f"{product.get('url')}|{product.get('price')}|{product.get('discount')}"
    return hashlib.sha256(raw.encode()).hexdigest()


def extract_products(page: str, source_url: str) -> list[dict]:
    products = []

    # Пытаемся найти JSON-LD на странице
    scripts = re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        page,
        flags=re.I | re.S,
    )

    for raw in scripts:
        try:
            data = json.loads(raw.strip())
        except Exception:
            continue

        objects = data if isinstance(data, list) else [data]

        for item in objects:
            if not isinstance(item, dict):
                continue

            if item.get("@type") == "Product":
                offers = item.get("offers") or {}
                products.append({
                    "name": item.get("name", "Товар"),
                    "url": item.get("url") or source_url,
                    "image": item.get("image"),
                    "price": money(
                        offers.get("price")
                        if isinstance(offers, dict)
                        else None
                    ),
                    "old_price": 0,
                    "discount": 0,
                    "rating": (
                        item.get("aggregateRating", {})
                        .get("ratingValue", 0)
                        if isinstance(item.get("aggregateRating"), dict)
                        else 0
                    ),
                    "reviews": (
                        item.get("aggregateRating", {})
                        .get("reviewCount", 0)
                        if isinstance(item.get("aggregateRating"), dict)
                        else 0
                    ),
                })

    # Запасной вариант: ищем ссылки на карточки товаров WB
    links = re.findall(
        r'href=["\']([^"\']*?/catalog/\d+/detail[^"\']*)["\']',
        page,
        flags=re.I,
    )

    names = re.findall(
        r'<[^>]+class=["\'][^"\']*(?:product-card|j-card)[^"\']*["\'][^>]*>'
        r'.*?<[^>]+class=["\'][^"\']*product-card__name[^"\']*["\'][^>]*>'
        r'(.*?)</',
        page,
        flags=re.I | re.S,
    )

    for index, link in enumerate(dict.fromkeys(links)):
        product_url = urljoin("https://www.wildberries.ru", link)

        if any(p["url"] == product_url for p in products):
            continue

        name = "Товар Wildberries"
        if index < len(names):
            name = re.sub("<.*?>", "", names[index]).strip()

        products.append({
            "name": name,
            "url": product_url,
            "image": None,
            "price": 0,
            "old_price": 0,
            "discount": 0,
            "rating": 0,
            "reviews": 0,
        })

    return products


async def load_products(url: str) -> list[dict]:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 Chrome/124 Safari/537.36"
        ),
        "Accept-Language": "ru-RU,ru;q=0.9",
    }

    timeout = aiohttp.ClientTimeout(total=25)

    async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
        async with session.get(url, allow_redirects=True) as response:
            if response.status != 200:
                raise RuntimeError(f"Wildberries вернул HTTP {response.status}")

            page = await response.text(errors="ignore")

    return extract_products(page, url)


def product_text(product: dict) -> str:
    name = html.escape(str(product["name"]))
    url = html.escape(str(product["url"]))
    price = product.get("price", 0)
    old_price = product.get("old_price", 0)
    discount = product.get("discount", 0)
    rating = product.get("rating", 0)
    reviews = product.get("reviews", 0)

    return (
        f"<b>{name}</b>\n\n"
        f"Цена: <b>{price:.0f} ₽</b>\n"
        f"Старая цена: <b>{old_price:.0f} ₽</b>\n"
        f"Скидка: <b>{discount:.0f}%</b>\n"
        f"Рейтинг: <b>{rating}</b>\n"
        f"Отзывы: <b>{reviews}</b>\n\n"
        f"<a href=\"{url}\">Открыть товар</a>"
    )


async def send_product(product: dict):
    text = product_text(product)

    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🛒 Открыть товар",
                    url=product["url"],
                )
            ]
        ]
    )

    image = product.get("image")

    if image:
        try:
            await bot.send_photo(
                ADMIN_ID,
                image,
                caption=text,
                reply_markup=markup,
                parse_mode="HTML",
            )
            return
        except Exception:
            log.exception("Не удалось отправить изображение")

    await bot.send_message(
        ADMIN_ID,
        text,
        reply_markup=markup,
        parse_mode="HTML",
    )


async def monitor():
    global watch_url

    while True:
        try:
            products = await load_products(watch_url)

            for product in products:
                price = product.get("price", 0)
                discount = product.get("discount", 0)

                # Если страница не содержит цену/скидку — не отправляем
                if price <= 0:
                    continue

                if price > MAX_PRICE:
                    continue

                if discount < MIN_DISCOUNT:
                    continue

                product_id = make_id(product)

                if sent_products.get(product["url"]) == product_id:
                    continue

                await send_product(product)
                sent_products[product["url"]] = product_id

        except Exception as error:
            log.exception("Ошибка проверки WB: %s", error)

        await asyncio.sleep(CHECK_INTERVAL)


@dp.message(CommandStart())
async def start(message: Message):
    if message.from_user.id != ADMIN_ID:
        return

    await message.answer(
        "Бот запущен.\n"
        f"Проверка: каждые {CHECK_INTERVAL} секунд\n"
        f"Минимальная скидка: {MIN_DISCOUNT}%\n"
        f"Максимальная цена: {MAX_PRICE:.0f} ₽\n"
        f"Ссылка: {watch_url}"
    )


@dp.message(F.text.startswith("https://www.wildberries.ru/"))
async def change_url(message: Message):
    global watch_url

    if message.from_user.id != ADMIN_ID:
        return

    watch_url = message.text.strip()
    sent_products.clear()

    await message.answer(
        "Ссылка для отслеживания обновлена:\n"
        f"{html.escape(watch_url)}"
    )


async def main():
    asyncio.create_task(monitor())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
