import os
import json
import logging
from datetime import datetime, date
import gspread
from google.oauth2.service_account import Credentials
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

logging.basicConfig(level=logging.INFO)

# ===== НАСТРОЙКИ =====
SPREADSHEET_ID = "1qwwCLpmu-FYMDAStR4qKWSSbpFBb-mG-kbNrcdKrSS8"
BUDGET_SPREADSHEET_ID = "1GwxtdYFLL9965adWGw6pEK22lgm8UT112TlxR4ajacc"
BOT_TOKEN = os.environ.get("BOT_TOKEN")

REMINDER_TEXT = "⚠️ Не забудь снять/архивировать листинг на eBay и Facebook!"

# ===== ПОДКЛЮЧЕНИЕ К GOOGLE SHEETS =====
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def get_client():
    creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    creds_dict = json.loads(creds_json)
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    return gspread.authorize(creds)


def get_sales_sheet():
    """Лист1 — продажи: Дата | Машина | Деталь | Цена | Продавец"""
    client = get_client()
    return client.open_by_key(SPREADSHEET_ID).sheet1


def get_expenses_sheet():
    """Expenses — расходы: Date | Expense | Amount | Added by"""
    client = get_client()
    return client.open_by_key(SPREADSHEET_ID).worksheet("Expenses")


def find_budget_vehicle_sheet(query):
    """
    Ищет в таблице 'Разбор бюджет' лист, название которого содержит query.
    Возвращает worksheet или None.
    """
    client = get_client()
    spreadsheet = client.open_by_key(BUDGET_SPREADSHEET_ID)
    query_lower = query.lower()
    for ws in spreadsheet.worksheets():
        if query_lower in ws.title.lower():
            return ws
    return None


def get_budget_vehicle_costs(query):
    """
    Возвращает (purchase_price, other_expenses, sold_total) из листа машины
    в таблице 'Разбор бюджет', или (0.0, 0.0, 0.0) если лист не найден / ошибка.
    Структура листа: строка 3 — A = Проданно на сумму, B = Цена покупки,
    C = Прочие расходы.
    """
    try:
        ws = find_budget_vehicle_sheet(query)
        if ws is None:
            return 0.0, 0.0, 0.0

        row3 = ws.get_values("A3:E3")
        row3 = row3[0] if row3 else []

        def cell_to_float(idx):
            try:
                val = row3[idx] if idx < len(row3) else ""
                val = str(val).replace(",", ".").replace(" ", "")
                return float(val) if val else 0.0
            except (ValueError, IndexError):
                return 0.0

        sold_total = cell_to_float(1)      # B — Проданно на сумму
        purchase_price = cell_to_float(2)  # C — Цена покупки
        other_expenses = cell_to_float(3)  # D — Прочие расходы

        return purchase_price, other_expenses, sold_total
    except Exception as e:
        logging.error(f"Ошибка чтения 'Разбор бюджет' для '{query}': {e}")
        return 0.0, 0.0, 0.0


def get_all_budget_vehicle_stats():
    """
    Проходит по всем листам в 'Разбор бюджет' (кроме 'Бюджет') и для каждого
    возвращает (название, sold, profit, roi, margin) из строки 3:
    B=Проданно, C=Цена покупки, D=Прочие расходы, F=Прибыль, G=ROI%, H=Маржа.
    """
    results = []
    try:
        client = get_client()
        spreadsheet = client.open_by_key(BUDGET_SPREADSHEET_ID)

        for ws in spreadsheet.worksheets():
            title = ws.title
            if title.strip().lower() == "бюджет":
                continue

            try:
                row3 = ws.get_values("A3:H3")
                row3 = row3[0] if row3 else []

                def cell_to_float(idx):
                    try:
                        val = row3[idx] if idx < len(row3) else ""
                        val = str(val).replace(",", ".").replace(" ", "").replace("%", "")
                        return float(val) if val else 0.0
                    except (ValueError, IndexError):
                        return 0.0

                sold = cell_to_float(1)      # B
                purchase = cell_to_float(2)  # C
                other_exp = cell_to_float(3) # D
                profit = cell_to_float(5)    # F
                roi = cell_to_float(6)       # G
                margin = cell_to_float(7)    # H

                results.append({
                    "title": title,
                    "sold": sold,
                    "purchase": purchase,
                    "other_exp": other_exp,
                    "profit": profit,
                    "roi": roi,
                    "margin": margin,
                })
            except Exception as e:
                logging.error(f"Ошибка чтения листа '{title}': {e}")
                continue
    except Exception as e:
        logging.error(f"Ошибка чтения 'Разбор бюджет': {e}")

    return results


async def sold(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /sold
    Машина
    Деталь
    Цена

    Каждая часть на новой строке.
    Например:
    /sold
    Mercedes GL450
    Бампер передний
    600
    """
    raw_text = update.message.text or update.message.caption or ""
    lines_in = [l.strip() for l in raw_text.split("\n")]

    # Первая строка — это сама команда /sold (может быть с текстом после неё, игнорируем)
    lines_in = lines_in[1:]
    # Убираем пустые строки
    lines_in = [l for l in lines_in if l]

    USAGE = (
        "Используй так (каждая часть на новой строке):\n"
        "/sold\n"
        "Машина\n"
        "Деталь\n"
        "Цена\n\n"
        "Пример:\n"
        "/sold\n"
        "Mercedes GL450\n"
        "Бампер передний\n"
        "600\n\n"
        "Можно прикрепить фото детали к этому сообщению."
    )

    if len(lines_in) < 3:
        await update.message.reply_text(USAGE)
        return

    try:
        price = float(lines_in[-1].replace(",", "."))
    except ValueError:
        await update.message.reply_text(f"Последняя строка должна быть ценой (числом).\n\n{USAGE}")
        return

    vehicle = lines_in[0]
    part_name = " ".join(lines_in[1:-1])
    seller = update.message.from_user.first_name
    now = datetime.now().strftime("%d.%m.%Y %H:%M")

    try:
        sheet = get_sales_sheet()
        sheet.append_row([now, vehicle, part_name, price, seller])
    except Exception as e:
        logging.error(f"Ошибка записи в таблицу: {e}")
        await update.message.reply_text("⚠️ Не получилось записать в таблицу, но сообщение в группе оставлено.")

    text = (
        f"✅ Продано: {part_name}\n"
        f"Машина: {vehicle}\n"
        f"Цена: {price:.2f}\n"
        f"Продал: {seller}\n"
        f"Дата: {now}\n\n"
        f"{REMINDER_TEXT}"
    )

    if update.message.photo:
        sent = await update.message.reply_photo(
            photo=update.message.photo[-1].file_id,
            caption=text,
        )
    else:
        sent = await update.message.reply_text(text)

    # Ставим реакцию на сообщение, чтобы все видели, что оно учтено
    try:
        await context.bot.set_message_reaction(
            chat_id=update.effective_chat.id,
            message_id=update.message.message_id,
            reaction="✅",
        )
    except Exception as e:
        logging.error(f"Не удалось поставить реакцию: {e}")


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/cancel — отменить последнюю продажу (удаляет последнюю строку из таблицы)"""
    try:
        sheet = get_sales_sheet()
        rows = sheet.get_all_values()
    except Exception as e:
        logging.error(f"Ошибка чтения таблицы: {e}")
        await update.message.reply_text("⚠️ Не получилось прочитать таблицу.")
        return

    if len(rows) <= 1:
        await update.message.reply_text("Нет продаж для отмены.")
        return

    last_row = rows[-1]
    last_row_num = len(rows)  # номер строки в таблице (1-индексация, с заголовком)

    try:
        sheet.delete_rows(last_row_num)
    except Exception as e:
        logging.error(f"Ошибка удаления строки: {e}")
        await update.message.reply_text("⚠️ Не получилось удалить запись из таблицы.")
        return

    date_s = last_row[0] if len(last_row) > 0 else "?"
    vehicle = last_row[1] if len(last_row) > 1 else "?"
    part = last_row[2] if len(last_row) > 2 else "?"
    price = last_row[3] if len(last_row) > 3 else "?"
    seller = last_row[4] if len(last_row) > 4 else "?"

    await update.message.reply_text(
        f"❌ Отменена продажа:\n"
        f"• {part} ({vehicle}) — {price} ({seller})\n"
        f"Дата: {date_s}"
    )


# ===== КОМАНДЫ: СТАТИСТИКА ПРОДАЖ =====

async def today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/today — продажи за сегодня"""
    try:
        sheet = get_sales_sheet()
        rows = sheet.get_all_values()[1:]
    except Exception as e:
        logging.error(f"Ошибка чтения таблицы: {e}")
        await update.message.reply_text("⚠️ Не получилось прочитать таблицу.")
        return

    today_str = date.today().strftime("%d.%m.%Y")
    today_rows = [r for r in rows if r and r[0].startswith(today_str)]

    if not today_rows:
        await update.message.reply_text("Сегодня пока ничего не продано.")
        return

    total_sum = 0.0
    lines = [f"📅 Продажи за сегодня ({len(today_rows)} шт.):\n"]
    for r in today_rows:
        try:
            price = float(r[3])
        except (IndexError, ValueError):
            price = 0.0
        total_sum += price
        vehicle = r[1] if len(r) > 1 else "?"
        part = r[2] if len(r) > 2 else "?"
        seller = r[4] if len(r) > 4 else "?"
        lines.append(f"• {part} ({vehicle}) — {price:.2f} ({seller})")

    lines.append(f"\n💰 Итого за сегодня: {total_sum:.2f}")
    await update.message.reply_text("\n".join(lines))


async def month(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/month — продажи за текущий месяц"""
    try:
        sheet = get_sales_sheet()
        rows = sheet.get_all_values()[1:]
    except Exception as e:
        logging.error(f"Ошибка чтения таблицы: {e}")
        await update.message.reply_text("⚠️ Не получилось прочитать таблицу.")
        return

    now = datetime.now()
    month_str = now.strftime(".%m.%Y")  # например ".06.2026"

    month_rows = []
    for r in rows:
        if not r or not r[0]:
            continue
        # Формат даты: ДД.ММ.ГГГГ ЧЧ:ММ
        if month_str in r[0][:10]:
            month_rows.append(r)

    if not month_rows:
        await update.message.reply_text("В этом месяце пока нет продаж.")
        return

    total_sum = 0.0
    for r in month_rows:
        try:
            total_sum += float(r[3])
        except (IndexError, ValueError):
            continue

    month_name = now.strftime("%m.%Y")
    await update.message.reply_text(
        f"📅 Продажи за месяц ({month_name}):\n"
        f"Количество: {len(month_rows)}\n"
        f"💰 Общая сумма: {total_sum:.2f}"
    )


async def list_sales(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/list — последние 10 продаж"""
    try:
        sheet = get_sales_sheet()
        rows = sheet.get_all_values()[1:]
    except Exception as e:
        logging.error(f"Ошибка чтения таблицы: {e}")
        await update.message.reply_text("⚠️ Не получилось прочитать таблицу.")
        return

    if not rows:
        await update.message.reply_text("Пока нет ни одной продажи.")
        return

    last_rows = rows[-10:]
    lines = ["📋 Последние продажи:\n"]
    for r in last_rows:
        date_s = r[0] if len(r) > 0 else "?"
        vehicle = r[1] if len(r) > 1 else "?"
        part = r[2] if len(r) > 2 else "?"
        price = r[3] if len(r) > 3 else "?"
        seller = r[4] if len(r) > 4 else "?"
        lines.append(f"• {date_s} — {part} ({vehicle}) — {price} ({seller})")

    await update.message.reply_text("\n".join(lines))


async def find(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/find Название — поиск по детали"""
    if not context.args:
        await update.message.reply_text("Используй так:\n/find Название_детали\n\nПример:\n/find бампер")
        return

    query = " ".join(context.args).lower()

    try:
        sheet = get_sales_sheet()
        rows = sheet.get_all_values()[1:]
    except Exception as e:
        logging.error(f"Ошибка чтения таблицы: {e}")
        await update.message.reply_text("⚠️ Не получилось прочитать таблицу.")
        return

    matches = [
        r for r in rows
        if (len(r) > 2 and (query in r[2].lower() or query in r[1].lower()))
    ]

    if not matches:
        await update.message.reply_text(f"Ничего не найдено по запросу: {query}")
        return

    lines = [f"🔍 Найдено ({len(matches)}):\n"]
    for r in matches[-15:]:
        date_s = r[0] if len(r) > 0 else "?"
        vehicle = r[1] if len(r) > 1 else "?"
        part = r[2] if len(r) > 2 else "?"
        price = r[3] if len(r) > 3 else "?"
        seller = r[4] if len(r) > 4 else "?"
        lines.append(f"• {date_s} — {part} ({vehicle}) — {price} ({seller})")

    await update.message.reply_text("\n".join(lines))


async def byseller(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/byseller — статистика по каждому продавцу"""
    try:
        sheet = get_sales_sheet()
        rows = sheet.get_all_values()[1:]
    except Exception as e:
        logging.error(f"Ошибка чтения таблицы: {e}")
        await update.message.reply_text("⚠️ Не получилось прочитать таблицу.")
        return

    if not rows:
        await update.message.reply_text("Пока нет ни одной продажи.")
        return

    stats = {}
    for r in rows:
        seller = r[4] if len(r) > 4 else "?"
        try:
            price = float(r[3])
        except (IndexError, ValueError):
            price = 0.0
        if seller not in stats:
            stats[seller] = {"count": 0, "sum": 0.0}
        stats[seller]["count"] += 1
        stats[seller]["sum"] += price

    # Сортируем по сумме продаж, по убыванию
    sorted_sellers = sorted(stats.items(), key=lambda x: x[1]["sum"], reverse=True)

    lines = ["👤 Статистика по продавцам:\n"]
    for seller, data in sorted_sellers:
        lines.append(f"• {seller}: {data['count']} шт. — {data['sum']:.2f}")

    await update.message.reply_text("\n".join(lines))


# ===== КОМАНДЫ: ПО МАШИНАМ =====

async def vehiclestats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /vehiclestats Машина — статистика по конкретной машине
    Пример: /vehiclestats GL450
    """
    if not context.args:
        await update.message.reply_text("Используй так:\n/vehiclestats Машина\n\nПример:\n/vehiclestats GL450")
        return

    query = " ".join(context.args).lower()

    try:
        sheet = get_sales_sheet()
        rows = sheet.get_all_values()[1:]
    except Exception as e:
        logging.error(f"Ошибка чтения таблицы: {e}")
        await update.message.reply_text("⚠️ Не получилось прочитать таблицу.")
        return

    matches = [r for r in rows if len(r) > 1 and query in r[1].lower()]

    total_sum = 0.0
    for r in matches:
        try:
            total_sum += float(r[3])
        except (IndexError, ValueError):
            continue

    # Расходы из таблицы "Разбор бюджет" (цена покупки + прочие расходы)
    budget_purchase, budget_other, budget_sold = get_budget_vehicle_costs(query)
    budget_expenses = budget_purchase + budget_other

    # Дополнительные расходы, записанные через /expense в боте
    bot_expenses = 0.0
    try:
        exp_sheet = get_expenses_sheet()
        exp_rows = exp_sheet.get_all_values()[1:]
        for r in exp_rows:
            if len(r) > 1 and query in r[1].lower():
                try:
                    bot_expenses += float(r[2])
                except (IndexError, ValueError):
                    continue
    except Exception as e:
        logging.error(f"Ошибка чтения Expenses для vehiclestats: {e}")

    vehicle_expenses = budget_expenses + bot_expenses

    if not matches and not budget_purchase and not budget_other and not bot_expenses:
        await update.message.reply_text(f"Нет записей по машине: {query}")
        return

    # Для расчёта прибыли используем выручку бота, либо данные из таблицы "Разбор бюджет",
    # если в боте пока нет записей по этой машине
    revenue = total_sum + budget_sold
    net_profit = revenue - vehicle_expenses

    lines = [f"🚗 Статистика по машине: {query}\n", f"Деталей продано (бот): {len(matches)}\n"]
    for r in matches[-15:]:
        date_s = r[0] if len(r) > 0 else "?"
        part = r[2] if len(r) > 2 else "?"
        price = r[3] if len(r) > 3 else "?"
        lines.append(f"• {date_s} — {part} — {price}")

    lines.append(f"\n💰 Общая выручка (бот): {total_sum:.2f}")
    if budget_sold:
        lines.append(f"📋 Продано по таблице (Разбор бюджет): {budget_sold:.2f}")
    if budget_purchase or budget_other:
        lines.append(f"🚙 Цена покупки: {budget_purchase:.2f}")
        lines.append(f"🔧 Прочие расходы: {budget_other:.2f}")
    if bot_expenses:
        lines.append(f"💸 Доп. расходы (/expense): {bot_expenses:.2f}")
    lines.append(f"💸 Всего расходов: {vehicle_expenses:.2f}")
    lines.append(f"📊 Чистая прибыль: {net_profit:.2f}")

    if vehicle_expenses > 0:
        roi = (net_profit / vehicle_expenses) * 100
        lines.append(f"📈 ROI: {roi:.1f}%")
    else:
        lines.append("📈 ROI: — (расходы не записаны)")

    if revenue > 0:
        margin = (net_profit / revenue) * 100
        lines.append(f"📐 Маржа прибыли: {margin:.1f}%")
    else:
        lines.append("📐 Маржа прибыли: —")

    await update.message.reply_text("\n".join(lines))


async def newcar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /newcar Название_машины Цена_покупки
    Создаёт новый лист в 'Разбор бюджет' для новой машины (со всеми формулами),
    указывая цену покупки. Прочие расходы и продажи добавляются позже через
    /expense и /sold.
    """
    if len(context.args) < 2:
        await update.message.reply_text(
            "Используй так:\n/newcar Название_машины Цена_покупки\n\n"
            "Пример:\n/newcar BMW_x5_black 8000\n\n"
            "Последнее слово — цена покупки (число), остальное — название машины "
            "(оно станет названием листа в 'Разбор бюджет')."
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
        f"/sold\n{car_name}\nДеталь\nЦена\n\n"
        f"/expense {car_name}_расход Сумма — для прочих расходов\n"
        f"/vehiclestats {car_name} — статистика"
    )


async def expense(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/expense Название Сумма — записать расход"""
    if len(context.args) < 2:
        await update.message.reply_text(
            "Используй так:\n/expense Название Сумма\n\nПример:\n/expense Покупка_авто_GL450 15000"
        )
        return

    try:
        amount = float(context.args[-1])
    except ValueError:
        await update.message.reply_text("Последним должна быть сумма (число). Пример: /expense Доставка 200")
        return

    expense_name = " ".join(context.args[:-1])
    added_by = update.message.from_user.first_name
    now = datetime.now().strftime("%d.%m.%Y %H:%M")

    try:
        sheet = get_expenses_sheet()
        sheet.append_row([now, expense_name, amount, added_by])
    except Exception as e:
        logging.error(f"Ошибка записи в Expenses: {e}")
        await update.message.reply_text("⚠️ Не получилось записать расход в таблицу.")
        return

    await update.message.reply_text(
        f"💸 Записан расход:\n"
        f"{expense_name}: {amount:.2f}\n"
        f"Добавил: {added_by}\n"
        f"Дата: {now}"
    )




# ===== ЕЖЕНЕДЕЛЬНЫЙ ОТЧЁТ =====

async def weekly_report(context: ContextTypes.DEFAULT_TYPE):
    """Автоматический еженедельный отчёт в группу"""
    chat_id = os.environ.get("REPORT_CHAT_ID")
    if not chat_id:
        logging.warning("REPORT_CHAT_ID не задан, еженедельный отчёт не отправлен.")
        return

    try:
        sheet = get_sales_sheet()
        rows = sheet.get_all_values()[1:]
    except Exception as e:
        logging.error(f"Ошибка чтения таблицы для отчёта: {e}")
        return

    from datetime import timedelta
    week_ago = datetime.now() - timedelta(days=7)

    week_rows = []
    for r in rows:
        if not r or not r[0]:
            continue
        try:
            row_date = datetime.strptime(r[0], "%d.%m.%Y %H:%M")
        except ValueError:
            continue
        if row_date >= week_ago:
            week_rows.append(r)

    if not week_rows:
        text = "📅 Еженедельный отчёт:\n\nЗа последние 7 дней продаж не было."
    else:
        total_sum = 0.0
        stats = {}
        for r in week_rows:
            try:
                price = float(r[3])
            except (IndexError, ValueError):
                price = 0.0
            total_sum += price
            seller = r[4] if len(r) > 4 else "?"
            stats[seller] = stats.get(seller, 0.0) + price

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


# ===== СВОДКА ПО "РАЗБОР БЮДЖЕТ" =====

# ===== ОБЩАЯ ПРИБЫЛЬ (БОТ + РАЗБОР БЮДЖЕТ) =====

async def totalprofit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/totalprofit — общая прибыль: данные бота (продажи и расходы) + все машины из 'Разбор бюджет'"""

    # ----- Данные из бота -----
    try:
        sales_sheet = get_sales_sheet()
        sales_rows = sales_sheet.get_all_values()[1:]
    except Exception as e:
        logging.error(f"Ошибка чтения продаж: {e}")
        sales_rows = []

    try:
        expenses_sheet = get_expenses_sheet()
        expense_rows = expenses_sheet.get_all_values()[1:]
    except Exception as e:
        logging.error(f"Ошибка чтения расходов: {e}")
        expense_rows = []

    bot_sales = 0.0
    for r in sales_rows:
        try:
            bot_sales += float(r[3])
        except (IndexError, ValueError):
            continue

    bot_expenses = 0.0
    for r in expense_rows:
        try:
            bot_expenses += float(r[2])
        except (IndexError, ValueError):
            continue

    # ----- Данные из "Разбор бюджет" -----
    stats = get_all_budget_vehicle_stats()

    lines = ["📊 Общая статистика:\n"]

    budget_sold = 0.0
    budget_expenses = 0.0
    budget_profit = 0.0

    if stats:
        lines.append("По машинам (Разбор бюджет):")
        for s in stats:
            lines.append(
                f"🚗 {s['title']}\n"
                f"   Продано: {s['sold']:.2f} | Прибыль: {s['profit']:.2f} | "
                f"ROI: {s['roi']:.1f}% | Маржа: {s['margin']:.1f}%"
            )
            budget_sold += s["sold"]
            budget_expenses += s["purchase"] + s["other_exp"]
            budget_profit += s["profit"]
        lines.append("")
    else:
        lines.append("⚠️ Не получилось прочитать 'Разбор бюджет'.\n")

    # ----- Итоги -----
    total_sales = bot_sales + budget_sold
    total_expenses = bot_expenses + budget_expenses
    total_profit = total_sales - total_expenses

    lines.append(f"🔢 Количество продаж (бот): {len(sales_rows)}")
    lines.append(f"💰 Продажи через бота: {bot_sales:.2f}")
    lines.append(f"💸 Расходы через бота: {bot_expenses:.2f}")
    lines.append(f"💰 Продано по 'Разбор бюджет': {budget_sold:.2f}")
    lines.append(f"💸 Расходы по 'Разбор бюджет': {budget_expenses:.2f}")
    lines.append("")
    lines.append(f"💰 Всего продаж: {total_sales:.2f}")
    lines.append(f"💸 Всего расходов: {total_expenses:.2f}")
    lines.append(f"📊 Общая чистая прибыль: {total_profit:.2f}")

    if total_expenses > 0:
        overall_roi = (total_profit / total_expenses) * 100
        lines.append(f"📈 Общий ROI: {overall_roi:.1f}%")
    else:
        lines.append("📈 Общий ROI: — (расходы не записаны)")

    if total_sales > 0:
        overall_margin = (total_profit / total_sales) * 100
        lines.append(f"📐 Общая маржа: {overall_margin:.1f}%")
    else:
        lines.append("📐 Общая маржа: —")

    await update.message.reply_text("\n".join(lines))


# ===== СЛУЖЕБНОЕ =====

async def groupid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/groupid — показывает ID текущего чата (для настройки REPORT_CHAT_ID)"""
    chat_id = update.effective_chat.id
    await update.message.reply_text(f"ID этого чата: {chat_id}")


# ===== ПОМОЩЬ =====

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 Команды бота:\n\n"
        "Продажи:\n"
        "/sold (каждая часть с новой строки: Машина / Деталь / Цена) — отметить продажу\n"
        "/cancel — отменить последнюю продажу\n\n"
        "Финансы:\n"
        "/expense Название Сумма — записать расход\n"
        "/totalprofit — общая прибыль (бот + 'Разбор бюджет')\n\n"
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
            days=(0,),  # 0 = понедельник
        )

    app.run_polling()


if __name__ == "__main__":
    main()
