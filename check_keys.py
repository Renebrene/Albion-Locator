import httpx
import asyncio

# Вставьте ваши ключи сюда
API_KEYS = [
    "1991c890-cb4c-42e4-86be-f81348013342",
    "892d3b65-eb5c-4c57-92d8-666266f880fa",
    "440cc39f-a2e1-4562-8c52-6d1c406ca018",
    "d69ffe17-b984-45d3-b435-f06bdb8853f6"
    # ... добавьте остальные
]

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
    print("🔍 Проверка ключей Яндекс.Геокодера...\n")
    async with httpx.AsyncClient() as client:
        for i, key in enumerate(API_KEYS, 1):
            result = await check_key(client, key)
            print(f"[{i}] {key[:10]}...{key[-6:]} -> {result}")

if __name__ == "__main__":
    asyncio.run(main())
