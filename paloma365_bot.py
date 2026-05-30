import requests
import json
import os
import pytz
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
LOGIN_URL = f"{BASE_URL}/company/user/login_ajax.php"
REPORT_URL = f"{BASE_URL}/company/report/report.php"

# Логин и пароль — берём из переменных окружения Railway (безопаснее)
# или fallback прямо здесь
PALOMA_LOGIN = os.environ.get("PALOMA_LOGIN", "ayalamarket")
PALOMA_PASSWORD = os.environ.get("PALOMA_PASSWORD", "00210114")

ITEMS_FILE = "my_items.json"
DEFAULT_ITEMS = ["50", "13", "2153", "2793", "2094", "1503"]

TZ = pytz.timezone("Asia/Atyrau")

# ============================================================
# СЕССИЯ С АВТОЛОГИНОМ
# ============================================================

_session = None

def create_session():
    """Создаёт новую сессию через полный SSO-цикл: oldback → paloma365.kz → oldback."""
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    })

    # Шаг 1: GET oldback/company/ → получаем PHPSESSID, нас редиректит на login
    r1 = s.get(f"{BASE_URL}/company/", timeout=15, allow_redirects=True)
    print(f"[step1] url={r1.url}")
    print(f"[step1] cookies={[(c.name, c.domain) for c in s.cookies]}")

    # Шаг 2: POST login_ajax.php, следуем ВСЕМ редиректам (oldback → paloma365.kz → ...)
    payload = {
        "login": PALOMA_LOGIN,
        "password": PALOMA_PASSWORD,
        "phone": "+7undefined",
    }
    r2 = s.post(LOGIN_URL, data=payload, timeout=30, allow_redirects=True)
    print(f"[step2] final_url={r2.url}")
    print(f"[step2] cookies={[(c.name, c.domain) for c in s.cookies]}")

    # Шаг 3: Возвращаемся на oldback — здесь должны появиться key и zid
    r3 = s.get(f"{BASE_URL}/company/", timeout=15, allow_redirects=True)
    print(f"[step3] final_url={r3.url}")
    print(f"[step3] cookies={[(c.name, c.value[:8] if len(c.value)>8 else c.value, c.domain) for c in s.cookies]}")

    # Проверяем есть ли ключевые куки
    cookie_names = [c.name for c in s.cookies]
    if "key" in cookie_names and "zid" in cookie_names:
        print("[login] SUCCESS — key и zid получены!")
    else:
        print(f"[login] FAIL — нужных кук нет. Есть: {cookie_names}")

    return s


def get_session(force_new=False):
    """Возвращает активную сессию, при необходимости создаёт новую."""
    global _session
    if _session is None or force_new:
        _session = create_session()
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

def fetch_report(date_from, date_to, item_ids=None, retry=True):
    if item_ids is None:
        item_ids = "|".join(load_items())

    session = get_session()

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
        response = session.post(
            f"{REPORT_URL}?do=otchet&type=akt",
            data=data,
            timeout=30
        )
    except Exception as e:
        return None, f"Ошибка сети: {e}"

    print(f"[report] status={response.status_code} url={response.url}")
    print(f"[report] body_start={response.text[:300]}")

    if response.status_code != 200:
        return None, f"Ошибка сервера: {response.status_code}"

    soup = BeautifulSoup(response.text, "html.parser")

    # Если сессия протухла — сайт редиректит на страницу логина
    if "login" in response.url or soup.find("form", {"action": lambda a: a and "login" in a}):
        if retry:
            print("Сессия устарела — перелогиниваюсь...")
            get_session(force_new=True)
            return fetch_report(date_from, date_to, item_ids, retry=False)
        return None, "Не удалось авторизоваться. Проверь логин/пароль."

    all_tables = soup.find_all("table", class_="report")
    data_table = None
    for t in all_tables:
        if t.find("tbody") and t.find("tbody").find("tr"):
            data_table = t
            break

    if not data_table:
        # Возможно это тоже признак устаревшей сессии
        if retry:
            print("Таблица не найдена — пробую перелогиниться...")
            get_session(force_new=True)
            return fetch_report(date_from, date_to, item_ids, retry=False)
        return None, "Таблица не найдена. Данных нет за этот период."

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
        qty_cell = cells[3].find("span", class_="sum")
        price_cell = cells[4].find("span", class_="sum")
        total_cell = cells[5].find("span", class_="sum")

        if not name_cell or not qty_cell:
            continue

        results.append({
            "name": name_cell,
            "qty": qty_cell.get_text(strip=True) if qty_cell else "0",
            "price": price_cell.get_text(strip=True) if price_cell else "0",
            "total": total_cell.get_text(strip=True) if total_cell else "0",
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
        name = item['name'][:27]
        lines.append(f"{name:<28} {item['qty']:>5} {item['total']:>8}")
    lines.append("-" * 43)
    lines.append(f"{'ИТОГО':<28} {str(total_qty):>5} {str(total_sum):>8}")
    lines.append("```")
    return "\n".join(lines)

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
        "*Прочее:*\n"
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
    result, error = fetch_report(f"{today} 00:00", f"{today} 23:59")
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
    result, error = fetch_report(f"{yesterday} 00:00", f"{yesterday} 23:59")
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
    result, error = fetch_report(f"{date_from} 00:00", f"{date_to} 23:59")
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
    result, error = fetch_report(f"{today} 00:00", f"{today} 23:59", item_ids=item_ids)
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
    if items:
        text = "📋 *Твои товары по умолчанию:*\n" + "\n".join(f"• `{i}`" for i in items)
    else:
        text = "Список пуст. Добавь товары через /additem 999"
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
    await update.message.reply_text(f"✅ Товар `{item_id}` добавлен. Всего: {len(items)}.", parse_mode="Markdown")


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
    await update.message.reply_text(f"🗑 Товар `{item_id}` удалён. Осталось: {len(items)}.", parse_mode="Markdown")


@only_me
async def cmd_resetitems(update: Update, context: ContextTypes.DEFAULT_TYPE):
    save_items(DEFAULT_ITEMS.copy())
    await update.message.reply_text("🔄 Список товаров сброшен до исходного.")


@only_me
async def cmd_relogin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔄 Перелогиниваюсь...")
    get_session(force_new=True)
    await update.message.reply_text("✅ Готово! Проверь командой /today")


# ============================================================
# ЗАПУСК
# ============================================================

if __name__ == "__main__":
    print("Бот запущен! Часовой пояс: Asia/Atyrau")
    print("Логинюсь на Paloma365...")
    get_session()  # логинимся сразу при старте
    print("Авторизация выполнена!")
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("yesterday", cmd_yesterday))
    app.add_handler(CommandHandler("period", cmd_period))
    app.add_handler(CommandHandler("items", cmd_items))
    app.add_handler(CommandHandler("myitems", cmd_myitems))
    app.add_handler(CommandHandler("additem", cmd_additem))
    app.add_handler(CommandHandler("removeitem", cmd_removeitem))
    app.add_handler(CommandHandler("resetitems", cmd_resetitems))
    app.add_handler(CommandHandler("relogin", cmd_relogin))
    app.run_polling()
