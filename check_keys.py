import asyncio
import os

import httpx

# Optional: set YANDEX_API_KEY in the environment; never store credentials here.
API_KEYS = [key for key in [os.getenv("YANDEX_API_KEY")] if key]

async def check_key(client, key):
    url = "https://geocode-maps.yandex.ru/1.x/"
    params = {
        "apikey": key,
        "geocode": "39.7015,47.2357",  # Тестовые координаты (Ростов-на-Дону)
        "format": "json"
    }
    try:
        resp = await client.get(url, params=params, timeout=5.0)
        if resp.status_code == 200:
            data = resp.json()
            found = len(data["response"]["GeoObjectCollection"]["featureMember"]) > 0
            return f"✅ РАБОТАЕТ (Нашел объектов: {found})"
        elif resp.status_code == 400:
            return f"❌ Ошибка 400: {resp.text[:100]}"
        elif resp.status_code == 403:
            return f"❌ Ошибка 403: Неверный ключ или доступ запрещен"
        else:
            return f"⚠️ Ошибка {resp.status_code}: {resp.text[:100]}"
    except Exception as e:
        return f"🔥 Сетевая ошибка: {e}"

async def main():
    if not API_KEYS:
        print("YANDEX_API_KEY is not set; no key was checked.")
        return
    print("🔍 Проверка ключа Яндекс.Геокодера...\n")
    async with httpx.AsyncClient() as client:
        result = await check_key(client, API_KEYS[0])
        print(result)

if __name__ == "__main__":
    asyncio.run(main())
