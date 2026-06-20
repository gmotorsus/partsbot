import os
import json
import logging
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
import gspread
from google.oauth2.service_account import Credentials
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

logging.basicConfig(level=logging.INFO)

# ===== НАСТРОЙКИ =====
BUDGET_SPREADSHEET_ID = "1GwxtdYFLL9965adWGw6pEK22lgm8UT112TlxR4ajacc"
BOT_TOKEN = os.environ.get("BOT_TOKEN")
TIMEZONE = ZoneInfo("America/New_York")


def now_local():
    """Текущее время в часовом поясе Филадельфии (America/New_York)."""
    return datetime.now(TIMEZONE)


def today_local():
    return now_local().date()

REMINDER_TEXT = "⚠️ Не забудь снять/архивировать листинг на eBay и Facebook и поставить реакцию!"

# Структура каждого листа машины в "Разбор бюджет":
# Строка 1 = название машины
# Строка 2 = заголовки (Запчасть, Проданно на сумму, Цена покупки, ...)
# Строка 3 = подзаголовки (cash, ebay)
# Строка 4 = итоговая строка (формулы): B=сумма cash, C=сумма ebay,
#            D=Цена покупки, E=Прочие расходы, F=Всего вложено,
#            G=Прибыль, H=ROI%, I=Маржа прибыли
# Строка 5+ = детали: A=Запчасть, B=Cash, C=ebay, J=Дата, K=Продавец (J,K скрытые)
FIRST_DATA_ROW = 5
COL_PART = 1   # A
COL_CASH = 2   # B
COL_EBAY = 3   # C
COL_PURCHASE = 4   # D (только строка 4)
COL_OTHER_EXP = 5  # E (только строка 4)
COL_DATE = 10   # J
COL_SELLER = 11  # K

# ===== ПОДКЛЮЧЕНИЕ К GOOGLE SHEETS =====
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def get_client():
    creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    creds_dict = json.loads(creds_json)
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    return gspread.authorize(creds)


def get_budget_spreadsheet():
    client = get_client()
    return client.open_by_key(BUDGET_SPREADSHEET_ID)


def get_vehicle_sheets():
    """Возвращает список всех листов-машин (исключая 'Бюджет')."""
    spreadsheet = get_budget_spreadsheet()
    return [ws for ws in spreadsheet.worksheets() if ws.title.strip().lower() != "бюджет"]


def find_budget_vehicle_sheet(query):
    """Ищет лист, название которого содержит query (без учёта регистра)."""
    query_lower = query.lower()
    for ws in get_vehicle_sheets():
        if query_lower in ws.title.lower():
            return ws
    return None


def cell_to_float(val):
    try:
        val = str(val).replace(",", ".").replace(" ", "").replace("%", "")
        return float(val) if val else 0.0
    except (ValueError, TypeError):
        return 0.0


def get_vehicle_summary(ws):
    """
    Возвращает словарь с итогами по листу машины (строка 4):
    cash, ebay, sold (cash+ebay), purchase, other_exp, profit, roi, margin.
    """
    all_values = ws.get_all_values()
    row4 = all_values[3] if len(all_values) > 3 else []

    def get(idx):
        return cell_to_float(row4[idx] if idx < len(row4) else "")

    cash = get(1)        # B
    ebay = get(2)        # C
    purchase = get(3)    # D
    other_exp = get(4)   # E
    profit = get(6)      # G
    roi = get(7)         # H
    margin = get(8)      # I

    return {
        "title": ws.title,
        "cash": cash,
        "ebay": ebay,
        "sold": cash + ebay,
        "purchase": purchase,
        "other_exp": other_exp,
        "profit": profit,
        "roi": roi,
        "margin": margin,
    }


def get_all_budget_vehicle_stats():
    """Сводка по всем листам-машинам."""
    results = []
    try:
        for ws in get_vehicle_sheets():
            try:
                results.append(get_vehicle_summary(ws))
            except Exception as e:
                logging.error(f"Ошибка чтения листа '{ws.title}': {e}")
                continue
    except Exception as e:
        logging.error(f"Ошибка чтения 'Разбор бюджет': {e}")
    return results


def find_first_empty_row(ws):
    """Находит первую пустую строку (по колонке A), начиная с FIRST_DATA_ROW."""
    all_values = ws.get_all_values()
    row_num = FIRST_DATA_ROW
    for i in range(FIRST_DATA_ROW - 1, len(all_values)):
        row = all_values[i]
        part_val = row[COL_PART - 1] if len(row) >= COL_PART else ""
        if not part_val.strip():
            return row_num
        row_num += 1
    return row_num


def get_all_detail_rows(ws):
    """
    Возвращает список деталей с листа (начиная с FIRST_DATA_ROW):
    [{row_num, part, cash, ebay, price, date, seller}, ...]
    Пропускает полностью пустые строки.
    """
    all_values = ws.get_all_values()
    rows = []
    for i in range(FIRST_DATA_ROW - 1, len(all_values)):
        row = all_values[i]
        part = row[COL_PART - 1] if len(row) >= COL_PART else ""
        if not part.strip():
            continue
        cash = cell_to_float(row[COL_CASH - 1]) if len(row) >= COL_CASH else 0.0
        ebay = cell_to_float(row[COL_EBAY - 1]) if len(row) >= COL_EBAY else 0.0
        price = cash if cash else ebay
        date_s = row[COL_DATE - 1] if len(row) >= COL_DATE else ""
        seller = row[COL_SELLER - 1] if len(row) >= COL_SELLER else ""
        rows.append({
            "row_num": i + 1,
            "vehicle": ws.title,
            "part": part,
            "cash": cash,
            "ebay": ebay,
            "price": price,
            "method": "cash" if cash else ("ebay" if ebay else "?"),
            "date": date_s,
            "seller": seller,
        })
    return rows


def get_all_sales_everywhere():
    """Собирает все строки-детали со всех листов машин."""
    all_rows = []
    for ws in get_vehicle_sheets():
        try:
            all_rows.extend(get_all_detail_rows(ws))
        except Exception as e:
            logging.error(f"Ошибка чтения деталей с листа '{ws.title}': {e}")
            continue
    return all_rows


# ===== КОМАНДЫ: ПРОДАЖИ =====

async def sold(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /sold
    Машина
    Деталь
    Цена
    Способ (cash или ebay)

    Каждая часть на новой строке. Пример:
    /sold
    Mercedes GL450
    Бампер передний
    600
    cash
    """
    raw_text = update.message.text or update.message.caption or ""
    lines_in = [l.strip() for l in raw_text.split("\n")]
    lines_in = lines_in[1:]
    lines_in = [l for l in lines_in if l]

    USAGE = (
        "Используй так (каждая часть на новой строке):\n"
        "/sold\n"
        "Машина\n"
        "Деталь\n"
        "Цена\n"
        "Способ (cash или ebay)\n\n"
        "Пример:\n"
        "/sold\n"
        "Mercedes GL450\n"
        "Бампер передний\n"
        "600\n"
        "cash\n\n"
        "Можно прикрепить фото детали к этому сообщению."
    )

    if len(lines_in) < 4:
        await update.message.reply_text(USAGE)
        return

    method = lines_in[-1].strip().lower()
    if method not in ("cash", "ebay"):
        await update.message.reply_text(f"Последняя строка должна быть 'cash' или 'ebay'.\n\n{USAGE}")
        return

    try:
        price = float(lines_in[-2].replace(",", "."))
    except ValueError:
        await update.message.reply_text(f"Предпоследняя строка должна быть ценой (числом).\n\n{USAGE}")
        return

    vehicle = lines_in[0]
    part_name = " ".join(lines_in[1:-2])
    seller = update.message.from_user.first_name
    now = now_local().strftime("%d.%m.%Y %H:%M")

    ws = find_budget_vehicle_sheet(vehicle)
    if ws is None:
        await update.message.reply_text(
            f"⚠️ Не нашёл машину '{vehicle}' в таблице 'Разбор бюджет'.\n"
            f"Сначала добавь её через /newcar."
        )
        return

    try:
        row_num = find_first_empty_row(ws)
        cash_val = price if method == "cash" else ""
        ebay_val = price if method == "ebay" else ""
        # A, B, C, D, E, F, G, H, I, (пропуск), J, K
        ws.update(f"A{row_num}", [[part_name]])
        if cash_val != "":
            ws.update(f"B{row_num}", [[cash_val]])
        if ebay_val != "":
            ws.update(f"C{row_num}", [[ebay_val]])
        ws.update(f"J{row_num}:K{row_num}", [[now, seller]])
    except Exception as e:
        logging.error(f"Ошибка записи в 'Разбор бюджет': {e}")
        await update.message.reply_text("⚠️ Не получилось записать в таблицу, но сообщение в группе оставлено.")
        return

    text = (
        f"✅ Продано: {part_name}\n"
        f"Машина: {ws.title}\n"
        f"Цена: {price:.2f} ({method})\n"
        f"Продал: {seller}\n"
        f"Дата: {now}\n\n"
        f"{REMINDER_TEXT}"
    )

    if update.message.photo:
        await update.message.reply_photo(photo=update.message.photo[-1].file_id, caption=text)
    else:
        await update.message.reply_text(text)

    try:
        await context.bot.set_message_reaction(
            chat_id=update.effective_chat.id,
            message_id=update.message.message_id,
            reaction="✅",
        )
    except Exception as e:
        logging.error(f"Не удалось поставить реакцию: {e}")


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/cancel — отменить самую последнюю по дате продажу (среди всех машин)"""
    all_rows = get_all_sales_everywhere()
    if not all_rows:
        await update.message.reply_text("Нет продаж для отмены.")
        return

    def parse_dt(r):
        try:
            return datetime.strptime(r["date"], "%d.%m.%Y %H:%M")
        except ValueError:
            return datetime.min

    last = max(all_rows, key=parse_dt)

    ws = find_budget_vehicle_sheet(last["vehicle"])
    if ws is None:
        await update.message.reply_text("⚠️ Не получилось найти лист машины для отмены.")
        return

    try:
        ws.delete_rows(last["row_num"])
    except Exception as e:
        logging.error(f"Ошибка удаления строки: {e}")
        await update.message.reply_text("⚠️ Не получилось удалить запись из таблицы.")
        return

    await update.message.reply_text(
        f"❌ Отменена продажа:\n"
        f"• {last['part']} ({last['vehicle']}) — {last['price']:.2f} ({last['method']})\n"
        f"Продал: {last['seller']}\n"
        f"Дата: {last['date']}"
    )


# ===== КОМАНДЫ: СТАТИСТИКА ПРОДАЖ =====

async def today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/today — продажи за сегодня (по всем машинам)"""
    all_rows = get_all_sales_everywhere()
    today_str = today_local().strftime("%d.%m.%Y")
    today_rows = [r for r in all_rows if r["date"].startswith(today_str)]

    if not today_rows:
        await update.message.reply_text("Сегодня пока ничего не продано.")
        return

    total_sum = sum(r["price"] for r in today_rows)
    lines = [f"📅 Продажи за сегодня ({len(today_rows)} шт.):\n"]
    for r in today_rows:
        lines.append(f"• {r['part']} ({r['vehicle']}) — {r['price']:.2f} ({r['seller']})")

    lines.append(f"\n💰 Итого за сегодня: {total_sum:.2f}")
    await update.message.reply_text("\n".join(lines))


async def month(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/month — продажи за текущий месяц (по всем машинам)"""
    all_rows = get_all_sales_everywhere()
    now = now_local()
    month_str = now.strftime(".%m.%Y")

    month_rows = [r for r in all_rows if r["date"] and month_str in r["date"][:10]]

    if not month_rows:
        await update.message.reply_text("В этом месяце пока нет продаж.")
        return

    total_sum = sum(r["price"] for r in month_rows)
    month_name = now.strftime("%m.%Y")
    await update.message.reply_text(
        f"📅 Продажи за месяц ({month_name}):\n"
        f"Количество: {len(month_rows)}\n"
        f"💰 Общая сумма: {total_sum:.2f}"
    )


async def list_sales(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/list — последние 10 продаж (по всем машинам)"""
    all_rows = get_all_sales_everywhere()
    if not all_rows:
        await update.message.reply_text("Пока нет ни одной продажи.")
        return

    def parse_dt(r):
        try:
            return datetime.strptime(r["date"], "%d.%m.%Y %H:%M")
        except ValueError:
            return datetime.min

    sorted_rows = sorted(all_rows, key=parse_dt)
    last_rows = sorted_rows[-10:]

    lines = ["📋 Последние продажи:\n"]
    for r in last_rows:
        lines.append(f"• {r['date']} — {r['part']} ({r['vehicle']}) — {r['price']:.2f} ({r['seller']})")

    await update.message.reply_text("\n".join(lines))


async def find(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/find Название — поиск по детали или машине (по всем машинам)"""
    if not context.args:
        await update.message.reply_text("Используй так:\n/find Название_детали\n\nПример:\n/find бампер")
        return

    query = " ".join(context.args).lower()
    all_rows = get_all_sales_everywhere()

    matches = [
        r for r in all_rows
        if query in r["part"].lower() or query in r["vehicle"].lower()
    ]

    if not matches:
        await update.message.reply_text(f"Ничего не найдено по запросу: {query}")
        return

    lines = [f"🔍 Найдено ({len(matches)}):\n"]
    for r in matches[-15:]:
        lines.append(f"• {r['date']} — {r['part']} ({r['vehicle']}) — {r['price']:.2f} ({r['seller']})")

    await update.message.reply_text("\n".join(lines))


async def byseller(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/byseller — статистика по каждому продавцу (по всем машинам)"""
    all_rows = get_all_sales_everywhere()
    if not all_rows:
        await update.message.reply_text("Пока нет ни одной продажи.")
        return

    stats = {}
    for r in all_rows:
        seller = r["seller"] or "?"
        if seller not in stats:
            stats[seller] = {"count": 0, "sum": 0.0}
        stats[seller]["count"] += 1
        stats[seller]["sum"] += r["price"]

    sorted_sellers = sorted(stats.items(), key=lambda x: x[1]["sum"], reverse=True)

    lines = ["👤 Статистика по продавцам:\n"]
    for seller, data in sorted_sellers:
        lines.append(f"• {seller}: {data['count']} шт. — {data['sum']:.2f}")

    await update.message.reply_text("\n".join(lines))


# ===== КОМАНДЫ: ПО МАШИНАМ =====

async def vehiclestats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/vehiclestats Машина — статистика по конкретной машине"""
    if not context.args:
        await update.message.reply_text("Используй так:\n/vehiclestats Машина\n\nПример:\n/vehiclestats GL450")
        return

    query = " ".join(context.args)
    ws = find_budget_vehicle_sheet(query)

    if ws is None:
        await update.message.reply_text(f"Не нашёл машину: {query}")
        return

    summary = get_vehicle_summary(ws)
    details = get_all_detail_rows(ws)

    lines = [f"🚗 Статистика по машине: {ws.title}\n", f"Деталей продано: {len(details)}\n"]
    for r in details[-15:]:
        lines.append(f"• {r['date']} — {r['part']} — {r['price']:.2f} ({r['method']})")

    lines.append(f"\n💰 Продано (cash): {summary['cash']:.2f}")
    lines.append(f"💰 Продано (ebay): {summary['ebay']:.2f}")
    lines.append(f"💰 Общая выручка: {summary['sold']:.2f}")
    lines.append(f"🚙 Цена покупки: {summary['purchase']:.2f}")
    lines.append(f"🔧 Прочие расходы: {summary['other_exp']:.2f}")

    total_expenses = summary['purchase'] + summary['other_exp']
    lines.append(f"💸 Всего расходов: {total_expenses:.2f}")
    lines.append(f"📊 Чистая прибыль: {summary['profit']:.2f}")
    lines.append(f"📈 ROI: {summary['roi']:.1f}%")
    lines.append(f"📐 Маржа прибыли: {summary['margin']:.1f}%")

    await update.message.reply_text("\n".join(lines))


def create_new_car_sheet(car_name, purchase_price):
    """
    Создаёт новый лист для машины в 'Разбор бюджет' путём дублирования
    первого существующего листа-машины (со всеми формулами), затем:
    - переименовывает дубликат в car_name
    - очищает строки с деталями (5+)
    - устанавливает заголовок A1 и D4 (цена покупки)
    """
    try:
        spreadsheet = get_budget_spreadsheet()

        for ws in spreadsheet.worksheets():
            if ws.title.strip().lower() == car_name.strip().lower():
                return False, f"Лист '{car_name}' уже существует."

        template = None
        for ws in spreadsheet.worksheets():
            if ws.title.strip().lower() != "бюджет":
                template = ws
                break

        if template is None:
            return False, "Не нашёл лист-шаблон для копирования."

        new_ws = template.duplicate(new_sheet_name=car_name)

        max_row = new_ws.row_count
        if max_row > 4:
            new_ws.batch_clear([f"A5:K{max_row}"])

        new_ws.update_acell("A1", car_name)
        new_ws.update_acell("D4", purchase_price)
        new_ws.update_acell("B4", "=СУММ(B5:B999)")
        new_ws.update_acell("C4", "=СУММ(C5:C999)")
        new_ws.update_acell("E4", 0)

        return True, ""
    except Exception as e:
        logging.error(f"Ошибка создания нового листа машины '{car_name}': {e}")
        return False, str(e)


async def newcar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/newcar Название_машины Цена_покупки"""
    if len(context.args) < 2:
        await update.message.reply_text(
            "Используй так:\n/newcar Название_машины Цена_покупки\n\n"
            "Пример:\n/newcar BMW_x5_black 8000\n\n"
            "Последнее слово — цена покупки (число), остальное — название машины."
        )
        return

    try:
        purchase_price = float(context.args[-1])
    except ValueError:
        await update.message.reply_text("Последним должна быть цена покупки (число). Пример: /newcar BMW_x5_black 8000")
        return

    car_name = " ".join(context.args[:-1])
    success, error = create_new_car_sheet(car_name, purchase_price)

    if not success:
        await update.message.reply_text(f"⚠️ Не получилось создать лист: {error}")
        return

    await update.message.reply_text(
        f"🚗 Создана новая машина: {car_name}\n"
        f"Цена покупки: {purchase_price:.2f}\n\n"
        f"Теперь можешь использовать:\n"
        f"/sold\n{car_name}\nДеталь\nЦена\ncash (или ebay)\n\n"
        f"/expense {car_name}_расход Сумма — для прочих расходов\n"
        f"/vehiclestats {car_name} — статистика"
    )


# ===== КОМАНДЫ: РАСХОДЫ =====

async def expense(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /expense Машина Сумма — добавить к 'Прочим расходам' указанной машины
    Пример: /expense GL450 150
    """
    if len(context.args) < 2:
        await update.message.reply_text(
            "Используй так:\n/expense Машина Сумма\n\nПример:\n/expense GL450 150\n\n"
            "Сумма добавится к 'Прочим расходам' этой машины."
        )
        return

    try:
        amount = float(context.args[-1])
    except ValueError:
        await update.message.reply_text("Последним должна быть сумма (число). Пример: /expense GL450 150")
        return

    vehicle_query = " ".join(context.args[:-1])
    ws = find_budget_vehicle_sheet(vehicle_query)

    if ws is None:
        await update.message.reply_text(f"⚠️ Не нашёл машину: {vehicle_query}")
        return

    try:
        current_val = cell_to_float(ws.acell("E4").value)
        new_val = current_val + amount
        ws.update_acell("E4", new_val)
    except Exception as e:
        logging.error(f"Ошибка записи расхода: {e}")
        await update.message.reply_text("⚠️ Не получилось записать расход в таблицу.")
        return

    added_by = update.message.from_user.first_name
    await update.message.reply_text(
        f"💸 Добавлен расход:\n"
        f"Машина: {ws.title}\n"
        f"Сумма: {amount:.2f}\n"
        f"Прочие расходы теперь: {new_val:.2f}\n"
        f"Добавил: {added_by}"
    )


async def genexpense(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /genexpense Название Сумма — общий расход бизнеса (не по конкретной машине)
    Записывает строкой в лист 'Бюджет' (колонки A=Название, B=Сумма).
    Пример: /genexpense Инструменты 100
    """
    if len(context.args) < 2:
        await update.message.reply_text(
            "Используй так:\n/genexpense Название Сумма\n\nПример:\n/genexpense Инструменты 100\n\n"
            "Это общий расход бизнеса, не привязанный к конкретной машине."
        )
        return

    try:
        amount = float(context.args[-1])
    except ValueError:
        await update.message.reply_text("Последним должна быть сумма (число). Пример: /genexpense Инструменты 100")
        return

    expense_name = " ".join(context.args[:-1])

    try:
        spreadsheet = get_budget_spreadsheet()
        ws = spreadsheet.worksheet("Бюджет")
    except Exception as e:
        logging.error(f"Не нашёл лист 'Бюджет': {e}")
        await update.message.reply_text("⚠️ Не получилось найти лист 'Бюджет'.")
        return

    try:
        all_values = ws.get_all_values()
        row_num = 1
        for i, row in enumerate(all_values):
            a_val = row[0] if len(row) > 0 else ""
            if a_val.strip():
                row_num = i + 1
        row_num += 1  # первая пустая строка после последней заполненной

        ws.update(f"A{row_num}:B{row_num}", [[expense_name, amount]])
    except Exception as e:
        logging.error(f"Ошибка записи общего расхода: {e}")
        await update.message.reply_text("⚠️ Не получилось записать расход в таблицу.")
        return

    added_by = update.message.from_user.first_name
    await update.message.reply_text(
        f"💸 Добавлен общий расход бизнеса:\n"
        f"{expense_name}: {amount:.2f}\n"
        f"Добавил: {added_by}"
    )


# ===== ЕЖЕНЕДЕЛЬНЫЙ ОТЧЁТ =====

async def weekly_report(context: ContextTypes.DEFAULT_TYPE):
    """Автоматический еженедельный отчёт в группу"""
    chat_id = os.environ.get("REPORT_CHAT_ID")
    if not chat_id:
        logging.warning("REPORT_CHAT_ID не задан, еженедельный отчёт не отправлен.")
        return

    try:
        all_rows = get_all_sales_everywhere()
    except Exception as e:
        logging.error(f"Ошибка чтения данных для отчёта: {e}")
        return

    week_ago = now_local().replace(tzinfo=None) - timedelta(days=7)

    def parse_dt(r):
        try:
            return datetime.strptime(r["date"], "%d.%m.%Y %H:%M")
        except ValueError:
            return None

    week_rows = [r for r in all_rows if parse_dt(r) and parse_dt(r) >= week_ago]

    if not week_rows:
        text = "📅 Еженедельный отчёт:\n\nЗа последние 7 дней продаж не было."
    else:
        total_sum = sum(r["price"] for r in week_rows)
        stats = {}
        for r in week_rows:
            seller = r["seller"] or "?"
            stats[seller] = stats.get(seller, 0.0) + r["price"]
        top_seller = max(stats.items(), key=lambda x: x[1])

        text = (
            f"📅 Еженедельный отчёт:\n\n"
            f"Продано: {len(week_rows)} шт.\n"
            f"💰 Общая сумма: {total_sum:.2f}\n"
            f"🏆 Топ продавец: {top_seller[0]} ({top_seller[1]:.2f})"
        )

    try:
        await context.bot.send_message(chat_id=chat_id, text=text)
    except Exception as e:
        logging.error(f"Не удалось отправить еженедельный отчёт: {e}")


# ===== ОБЩАЯ ПРИБЫЛЬ (ВСЕ МАШИНЫ ИЗ "РАЗБОР БЮДЖЕТ") =====

async def totalprofit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/totalprofit — общая прибыль по всем машинам из 'Разбор бюджет'"""
    stats = get_all_budget_vehicle_stats()

    lines = ["📊 Общая статистика:\n"]

    total_sold = 0.0
    total_expenses = 0.0
    total_profit = 0.0
    total_count = 0

    if stats:
        lines.append("По машинам:")
        for s in stats:
            lines.append(
                f"🚗 {s['title']}\n"
                f"   Продано: {s['sold']:.2f} | Прибыль: {s['profit']:.2f} | "
                f"ROI: {s['roi']:.1f}% | Маржа: {s['margin']:.1f}%"
            )
            total_sold += s["sold"]
            total_expenses += s["purchase"] + s["other_exp"]
            total_profit += s["profit"]
        lines.append("")
    else:
        lines.append("⚠️ Не получилось прочитать 'Разбор бюджет'.\n")

    try:
        total_count = len(get_all_sales_everywhere())
    except Exception:
        pass

    lines.append(f"🔢 Количество продаж: {total_count}")
    lines.append(f"💰 Всего продаж: {total_sold:.2f}")
    lines.append(f"💸 Всего расходов: {total_expenses:.2f}")
    lines.append(f"📊 Общая чистая прибыль: {total_profit:.2f}")

    if total_expenses > 0:
        overall_roi = (total_profit / total_expenses) * 100
        lines.append(f"📈 Общий ROI: {overall_roi:.1f}%")
    else:
        lines.append("📈 Общий ROI: — (расходы не записаны)")

    if total_sold > 0:
        overall_margin = (total_profit / total_sold) * 100
        lines.append(f"📐 Общая маржа: {overall_margin:.1f}%")
    else:
        lines.append("📐 Общая маржа: —")

    await update.message.reply_text("\n".join(lines))


# ===== СЛУЖЕБНОЕ =====

async def groupid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/groupid — показывает ID текущего чата"""
    chat_id = update.effective_chat.id
    await update.message.reply_text(f"ID этого чата: {chat_id}")


# ===== ПОМОЩЬ =====

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 Команды бота:\n\n"
        "Продажи:\n"
        "/sold (Машина / Деталь / Цена / cash или ebay) — отметить продажу\n"
        "/cancel — отменить последнюю продажу\n\n"
        "Финансы:\n"
        "/expense Машина Сумма — добавить прочий расход к машине\n"
        "/genexpense Название Сумма — общий расход бизнеса (не по машине)\n"
        "/totalprofit — общая прибыль по всем машинам\n\n"
        "Статистика:\n"
        "/today — продажи за сегодня\n"
        "/month — продажи за текущий месяц\n"
        "/list — последние 10 продаж\n"
        "/find Деталь_или_Машина — поиск\n"
        "/byseller — статистика по продавцам\n"
        "/vehiclestats Машина — статистика по машине\n"
        "/newcar Название Цена_покупки — добавить новую машину\n\n"
        "/help — это сообщение"
    )


def main():
    if not BOT_TOKEN:
        raise ValueError("Переменная окружения BOT_TOKEN не задана!")
    if not os.environ.get("GOOGLE_CREDENTIALS_JSON"):
        raise ValueError("Переменная окружения GOOGLE_CREDENTIALS_JSON не задана!")

    app = Application.builder().token(BOT_TOKEN).build()

    # Продажи
    app.add_handler(CommandHandler("sold", sold))
    app.add_handler(MessageHandler(
        filters.PHOTO & filters.CaptionRegex(r"^/sold"),
        sold,
    ))
    app.add_handler(CommandHandler("cancel", cancel))

    # Статистика
    app.add_handler(CommandHandler("today", today))
    app.add_handler(CommandHandler("month", month))
    app.add_handler(CommandHandler("list", list_sales))
    app.add_handler(CommandHandler("find", find))
    app.add_handler(CommandHandler("byseller", byseller))

    # По машинам
    app.add_handler(CommandHandler("vehiclestats", vehiclestats))
    app.add_handler(CommandHandler("newcar", newcar))

    # Финансы
    app.add_handler(CommandHandler("expense", expense))
    app.add_handler(CommandHandler("genexpense", genexpense))
    app.add_handler(CommandHandler("totalprofit", totalprofit))

    # Служебное
    app.add_handler(CommandHandler("groupid", groupid))

    # Помощь
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("start", help_command))

    # Еженедельный отчёт (каждый понедельник в 09:00)
    if app.job_queue:
        from datetime import time as dtime
        app.job_queue.run_daily(
            weekly_report,
            time=dtime(hour=9, minute=0),
            days=(0,),
        )

    app.run_polling()


if __name__ == "__main__":
    main()
