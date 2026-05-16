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

BASE_URL = "https://oldback.paloma365.com/company/report/report.php"

COOKIE = "workspace_key=default; PHPSESSID=r2sqecb13um1t607ibi59bop27; key=94bacc8952bc856c45eace3bb6990603; zid=23553; _ym_uid=1778936973444711715; _ym_d=1778936973; _ym_isad=1; _fbp=fb.1.1778936972984.827730157221491058.AQYCAQIB; _tt_enable_cookie=1; _ttp=01KRREG1SG24V0BVW77CNDMXM6_.tt.1; __ddg1_=1IUCdDNFfoZ3yRsC9Yzp; ttcsid_CMJT09JC77UEKGPKG4KG=1778936973107::mSmndeRqgaWtxBARvmAA.1.1778937481743.1; ttcsid_D5SV5EJC77U1TOJ9VSLG=1778937477176::r30pPBediG7Vly_DBAxv.1.1778937481750.0; b24_sitebutton_hello=y; ttcsid_D5SUHV3C77U6BSHUJMR0=1778936973107::PtrUimeYIZoFG7i_--up.1.1778938707625.1; ttcsid=1778936973107::D1H5_EIFiwWIpEzOBwu9.1.1778938707625.0::1.499278.504068::508657.20.1047.682::283148.72.1739"

ITEMS_FILE = "my_items.json"
DEFAULT_ITEMS = ["50", "13", "2153", "2793", "2094", "1503"]

# Часовой пояс Атырау
TZ = pytz.timezone("Asia/Atyrau")

# ============================================================
# ДАТА В ТВОЁМ ЧАСОВОМ ПОЯСЕ
# ============================================================

def local_today():
    return datetime.now(TZ).date()

def local_yesterday():
    return local_today() - timedelta(days=1)

# ============================================================
# УПРАВЛЕНИЕ СПИСКОМ ТОВАРОВ
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

def fetch_report(date_from, date_to, item_ids=None):
    if item_ids is None:
        item_ids = "|".join(load_items())

    headers = {
        "Cookie": COOKIE,
        "User-Agent": "Mozilla/5.0",
        "Content-Type": "application/x-www-form-urlencoded",
    }

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

    response = requests.post(
        f"{BASE_URL}?do=otchet&type=akt",
        data=data,
        headers=headers,
        timeout=30
    )

    if response.status_code != 200:
        return None, f"Ошибка сервера: {response.status_code}"

    soup = BeautifulSoup(response.text, "html.parser")
    all_tables = soup.find_all("table", class_="report")
    data_table = None
    for t in all_tables:
        if t.find("tbody") and t.find("tbody").find("tr"):
            data_table = t
            break

    if not data_table:
        return None, "Таблица не найдена. Возможно cookie устарел."

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
        "/period 01.05.2026 16.05.2026 — за период\n"
        "/items 50 13 1503 — по конкретным ID (сегодня)\n\n"
        "*Управление товарами:*\n"
        "/myitems — показать список\n"
        "/additem 999 — добавить товар\n"
        "/removeitem 999 — убрать товар\n"
        "/resetitems — вернуть исходный список"
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
        await update.message.reply_text("Использование: /period 01.05.2026 16.05.2026")
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


# ============================================================
# ЗАПУСК
# ============================================================

if __name__ == "__main__":
    print("Бот запущен! Часовой пояс: Asia/Atyrau")
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
    app.run_polling()
