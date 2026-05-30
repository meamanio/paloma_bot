import requests
import json
import os
import pytz
import asyncio
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# ============================================================
# НАСТРОЙКИ
# ============================================================

BOT_TOKEN = "8835739702:AAG04XCzLyVxym9gX0zvgzC_GVgJgTpdFgk"
MY_TELEGRAM_ID = 182114715

BASE_URL = "https://oldback.paloma365.com"
REPORT_URL = f"{BASE_URL}/company/report/report.php"

PALOMA_LOGIN   = os.environ.get("PALOMA_LOGIN",   "ayalamarket")
PALOMA_PASSWORD = os.environ.get("PALOMA_PASSWORD", "00210114")

ITEMS_FILE    = "my_items.json"
DEFAULT_ITEMS = ["50", "13", "2153", "2793", "2094", "1503"]

TZ = pytz.timezone("Asia/Atyrau")

# ============================================================
# СЕССИЯ — Playwright автологин
# ============================================================

_session: requests.Session | None = None
_session_lock = asyncio.Lock()


def _create_session_sync() -> requests.Session:
    """Запускает Chromium, проходит весь SSO-цикл и возвращает requests.Session с куками."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
        )
        ctx = browser.new_context(
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36"
        )
        page = ctx.new_page()

        try:
            # 1. Переходим на oldback — редирект на страницу логина
            page.goto(f"{BASE_URL}/company/", timeout=20000, wait_until="domcontentloaded")
            print(f"[pw] step1: {page.url}")

            # 2. Вводим логин и пароль
            page.wait_for_selector('input[name="login"]', timeout=10000)
            page.fill('input[name="login"]', PALOMA_LOGIN)
            page.fill('input[name="password"]', PALOMA_PASSWORD)

            # 3. Жмём кнопку входа (пробуем разные варианты)
            submitted = False
            for selector in ['button[type="submit"]', 'input[type="submit"]', 'button.btn-login', '.login-btn']:
                try:
                    page.click(selector, timeout=2000)
                    submitted = True
                    break
                except Exception:
                    pass
            if not submitted:
                page.press('input[name="password"]', "Enter")

            # 4. Ждём завершения всех JS-редиректов и сетевых запросов
            page.wait_for_load_state("networkidle", timeout=30000)
            print(f"[pw] step4 (after login): {page.url}")

            # 5. Если оказались на paloma365.kz — возвращаемся на oldback
            if "oldback.paloma365.com" not in page.url:
                page.goto(f"{BASE_URL}/company/", timeout=20000, wait_until="networkidle")
                print(f"[pw] step5 (back to oldback): {page.url}")

            cookies = ctx.cookies()
        finally:
            browser.close()

    cookie_names = [c["name"] for c in cookies]
    print(f"[pw] cookies: {cookie_names}")

    # Собираем requests.Session
    s = requests.Session()
    s.headers.update({"User-Agent": "Mozilla/5.0"})
    for c in cookies:
        s.cookies.set(c["name"], c["value"], domain=c["domain"].lstrip("."))

    if "key" in cookie_names:
        print("[pw] ✅ Авторизация успешна!")
    else:
        print("[pw] ⚠️  Кука 'key' не получена — возможно авторизация не прошла")

    return s


async def ensure_session(force_new: bool = False) -> requests.Session:
    """Возвращает активную сессию, при необходимости создаёт новую."""
    global _session
    async with _session_lock:
        if _session is None or force_new:
            print("[session] Запускаю Playwright для авторизации...")
            _session = await asyncio.to_thread(_create_session_sync)
    return _session


# ============================================================
# ДАТА
# ============================================================

def local_today():
    return datetime.now(TZ).date()

def local_yesterday():
    return local_today() - timedelta(days=1)

# ============================================================
# ТОВАРЫ
# ============================================================

def load_items():
    if os.path.exists(ITEMS_FILE):
        with open(ITEMS_FILE, "r") as f:
            return json.load(f)
    return DEFAULT_ITEMS.copy()

def save_items(items):
    with open(ITEMS_FILE, "w") as f:
        json.dump(items, f)

# ============================================================
# ПОЛУЧЕНИЕ ДАННЫХ
# ============================================================

def fetch_report(session: requests.Session, date_from: str, date_to: str, item_ids=None):
    if item_ids is None:
        item_ids = "|".join(load_items())

    data = {
        "printType": "phpwkhtmltopdf",
        "idautomated_point": "0",
        "price_type": "0",
        "employeeid": "0",
        "paymentid": "0",
        "divisionid": "0",
        "clientid": "0",
        "itemid": item_ids,
        "itemsTreeView": "no",
        "chb": "zaperiod",
        "chb_zaperiod1": date_from,
        "chb_zaperiod2": date_to,
        "chb_zasmenu": "1216",
        "chb_smenperiod1": date_from,
        "chb_smenperiod2": date_to,
    }

    try:
        response = session.post(f"{REPORT_URL}?do=otchet&type=akt", data=data, timeout=30)
    except Exception as e:
        return None, f"Ошибка сети: {e}"

    if response.status_code != 200:
        return None, f"Ошибка сервера: {response.status_code}"

    # Если сессия протухла — сервер редиректит на login.php
    if "login.php" in response.url or "nosession" in response.url:
        return None, "AUTH_EXPIRED"

    soup = BeautifulSoup(response.text, "html.parser")
    if soup.find("form", {"action": lambda a: a and "login" in a}):
        return None, "AUTH_EXPIRED"

    all_tables = soup.find_all("table", class_="report")
    data_table = None
    for t in all_tables:
        if t.find("tbody") and t.find("tbody").find("tr"):
            data_table = t
            break

    if not data_table:
        return None, "Данных за этот период нет."

    results = []
    total_qty = "0"
    total_sum = "0"
    rows = data_table.find("tbody").find_all("tr")

    for row in rows:
        cells = row.find_all("td")
        if not cells:
            continue
        if "row-resume" in row.get("class", []):
            spans = row.find_all("span", class_="sum")
            if len(spans) >= 2:
                total_qty = spans[0].get_text(strip=True)
                total_sum = spans[1].get_text(strip=True)
            continue
        if len(cells) < 6:
            continue
        name_cell = cells[2].get_text(strip=True)
        qty_cell  = cells[3].find("span", class_="sum")
        price_cell = cells[4].find("span", class_="sum")
        total_cell = cells[5].find("span", class_="sum")
        if not name_cell or not qty_cell:
            continue
        results.append({
            "name":  name_cell,
            "qty":   qty_cell.get_text(strip=True)   if qty_cell   else "0",
            "price": price_cell.get_text(strip=True)  if price_cell else "0",
            "total": total_cell.get_text(strip=True)  if total_cell else "0",
        })

    return (results, total_qty, total_sum), None


def format_report(results, total_qty, total_sum, date_from, date_to):
    lines = [
        f"📊 *Продажи: {date_from} — {date_to}*",
        "```",
        f"{'Товар':<28} {'Кол':>5} {'Сумма':>8}",
        "-" * 43,
    ]
    for item in results:
        name = item["name"][:27]
        lines.append(f"{name:<28} {item['qty']:>5} {item['total']:>8}")
    lines.append("-" * 43)
    lines.append(f"{'ИТОГО':<28} {str(total_qty):>5} {str(total_sum):>8}")
    lines.append("```")
    return "\n".join(lines)

# ============================================================
# ХЕЛПЕР: запустить fetch_report с автообновлением сессии
# ============================================================

async def run_report(date_from, date_to, item_ids=None):
    session = await ensure_session()
    result, error = await asyncio.to_thread(fetch_report, session, date_from, date_to, item_ids)
    if error == "AUTH_EXPIRED":
        print("[session] Сессия устарела — перелогиниваюсь через Playwright")
        session = await ensure_session(force_new=True)
        result, error = await asyncio.to_thread(fetch_report, session, date_from, date_to, item_ids)
    return result, error

# ============================================================
# ЗАЩИТА
# ============================================================

def only_me(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != MY_TELEGRAM_ID:
            await update.message.reply_text("Доступ запрещён.")
            return
        await func(update, context)
    return wrapper

# ============================================================
# КОМАНДЫ
# ============================================================

@only_me
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "👋 Привет! Я бот для отчётов Paloma365.\n\n"
        "*Отчёты:*\n"
        "/today — продажи за сегодня\n"
        "/yesterday — продажи за вчера\n"
        "/period 01.05.2026 17.05.2026 — за период\n"
        "/items 50 13 1503 — по конкретным ID (сегодня)\n\n"
        "*Управление товарами:*\n"
        "/myitems — показать список\n"
        "/additem 999 — добавить товар\n"
        "/removeitem 999 — убрать товар\n"
        "/resetitems — вернуть исходный список\n\n"
        "/relogin — принудительно перелогиниться"
    )
    await update.message.reply_text(text, parse_mode="Markdown")

@only_me
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, context)

@only_me
async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    today = local_today().strftime("%d.%m.%Y")
    await update.message.reply_text("⏳ Получаю данные...")
    result, error = await run_report(f"{today} 00:00", f"{today} 23:59")
    if error:
        await update.message.reply_text(f"❌ {error}")
        return
    items, total_qty, total_sum = result
    await update.message.reply_text(
        format_report(items, total_qty, total_sum, f"{today} 00:00", f"{today} 23:59"),
        parse_mode="Markdown"
    )

@only_me
async def cmd_yesterday(update: Update, context: ContextTypes.DEFAULT_TYPE):
    yesterday = local_yesterday().strftime("%d.%m.%Y")
    await update.message.reply_text("⏳ Получаю данные...")
    result, error = await run_report(f"{yesterday} 00:00", f"{yesterday} 23:59")
    if error:
        await update.message.reply_text(f"❌ {error}")
        return
    items, total_qty, total_sum = result
    await update.message.reply_text(
        format_report(items, total_qty, total_sum, f"{yesterday} 00:00", f"{yesterday} 23:59"),
        parse_mode="Markdown"
    )

@only_me
async def cmd_period(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("Использование: /period 01.05.2026 17.05.2026")
        return
    date_from, date_to = context.args[0], context.args[1]
    await update.message.reply_text("⏳ Получаю данные...")
    result, error = await run_report(f"{date_from} 00:00", f"{date_to} 23:59")
    if error:
        await update.message.reply_text(f"❌ {error}")
        return
    items, total_qty, total_sum = result
    await update.message.reply_text(
        format_report(items, total_qty, total_sum, f"{date_from} 00:00", f"{date_to} 23:59"),
        parse_mode="Markdown"
    )

@only_me
async def cmd_items(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Использование: /items 50 13 1503")
        return
    item_ids = "|".join(context.args)
    today = local_today().strftime("%d.%m.%Y")
    await update.message.reply_text("⏳ Получаю данные...")
    result, error = await run_report(f"{today} 00:00", f"{today} 23:59", item_ids)
    if error:
        await update.message.reply_text(f"❌ {error}")
        return
    items, total_qty, total_sum = result
    await update.message.reply_text(
        format_report(items, total_qty, total_sum, f"{today} 00:00", f"{today} 23:59"),
        parse_mode="Markdown"
    )

@only_me
async def cmd_myitems(update: Update, context: ContextTypes.DEFAULT_TYPE):
    items = load_items()
    text = ("📋 *Твои товары:*\n" + "\n".join(f"• `{i}`" for i in items)) if items else "Список пуст."
    await update.message.reply_text(text, parse_mode="Markdown")

@only_me
async def cmd_additem(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Использование: /additem 999")
        return
    item_id = context.args[0]
    items = load_items()
    if item_id in items:
        await update.message.reply_text(f"Товар `{item_id}` уже в списке.", parse_mode="Markdown")
        return
    items.append(item_id)
    save_items(items)
    await update.message.reply_text(f"✅ Товар `{item_id}` добавлен.", parse_mode="Markdown")

@only_me
async def cmd_removeitem(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Использование: /removeitem 999")
        return
    item_id = context.args[0]
    items = load_items()
    if item_id not in items:
        await update.message.reply_text(f"Товар `{item_id}` не найден.", parse_mode="Markdown")
        return
    items.remove(item_id)
    save_items(items)
    await update.message.reply_text(f"🗑 Товар `{item_id}` удалён.", parse_mode="Markdown")

@only_me
async def cmd_resetitems(update: Update, context: ContextTypes.DEFAULT_TYPE):
    save_items(DEFAULT_ITEMS.copy())
    await update.message.reply_text("🔄 Список товаров сброшен до исходного.")

@only_me
async def cmd_relogin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔄 Перелогиниваюсь через Playwright, подожди ~15 сек...")
    await ensure_session(force_new=True)
    await update.message.reply_text("✅ Готово! Проверь /today")

# ============================================================
# ЗАПУСК
# ============================================================

async def post_init(application):
    """Логинимся при старте бота."""
    print("Логинюсь через Playwright...")
    await ensure_session()
    print("Готово!")

if __name__ == "__main__":
    print("Запуск бота...")
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )
    app.add_handler(CommandHandler("start",       cmd_start))
    app.add_handler(CommandHandler("help",        cmd_help))
    app.add_handler(CommandHandler("today",       cmd_today))
    app.add_handler(CommandHandler("yesterday",   cmd_yesterday))
    app.add_handler(CommandHandler("period",      cmd_period))
    app.add_handler(CommandHandler("items",       cmd_items))
    app.add_handler(CommandHandler("myitems",     cmd_myitems))
    app.add_handler(CommandHandler("additem",     cmd_additem))
    app.add_handler(CommandHandler("removeitem",  cmd_removeitem))
    app.add_handler(CommandHandler("resetitems",  cmd_resetitems))
    app.add_handler(CommandHandler("relogin",     cmd_relogin))
    app.run_polling()
