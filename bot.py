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


# ===== КОМАНДЫ: ПРОДАЖИ =====

async def sold(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /sold Машина Деталь Цена
    Например: /sold GL450 Бампер передний 5000
    Можно отправить вместе с фото (фото как подпись к команде).
    """
    raw_text = update.message.text or update.message.caption or ""
    parts = raw_text.split()
    if parts and parts[0].startswith("/sold"):
        parts = parts[1:]

    if len(parts) < 3:
        await update.message.reply_text(
            "Используй так:\n/sold Машина Деталь Цена\n\nПример:\n/sold GL450 Бампер передний 5000\n\n"
            "Первое слово — машина, последнее — цена, всё остальное — название детали.\n"
            "Можно прикрепить фото детали к этому сообщению."
        )
        return

    try:
        price = float(parts[-1])
    except ValueError:
        await update.message.reply_text("Последним должна быть цена (число). Пример: /sold GL450 Бампер передний 5000")
        return

    vehicle = parts[0]
    part_name = " ".join(parts[1:-1])
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

async def total(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/total — общая сумма всех продаж"""
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

    total_sum = 0.0
    for row in rows:
        try:
            total_sum += float(row[3])
        except (IndexError, ValueError):
            continue

    await update.message.reply_text(
        f"📊 Всего продаж: {len(rows)}\n"
        f"💰 Общая сумма: {total_sum:.2f}"
    )


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

    if not matches:
        await update.message.reply_text(f"Нет записей по машине: {query}")
        return

    total_sum = 0.0
    for r in matches:
        try:
            total_sum += float(r[3])
        except (IndexError, ValueError):
            continue

    lines = [f"🚗 Статистика по машине: {query}\n", f"Деталей продано: {len(matches)}\n"]
    for r in matches[-15:]:
        date_s = r[0] if len(r) > 0 else "?"
        part = r[2] if len(r) > 2 else "?"
        price = r[3] if len(r) > 3 else "?"
        lines.append(f"• {date_s} — {part} — {price}")

    lines.append(f"\n💰 Общая выручка: {total_sum:.2f}")
    await update.message.reply_text("\n".join(lines))


# ===== КОМАНДЫ: РАСХОДЫ И ПРИБЫЛЬ =====

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


async def profit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/profit — продажи минус расходы (за всё время)"""
    try:
        sales_sheet = get_sales_sheet()
        sales_rows = sales_sheet.get_all_values()[1:]
    except Exception as e:
        logging.error(f"Ошибка чтения продаж: {e}")
        await update.message.reply_text("⚠️ Не получилось прочитать таблицу продаж.")
        return

    try:
        expenses_sheet = get_expenses_sheet()
        expense_rows = expenses_sheet.get_all_values()[1:]
    except Exception as e:
        logging.error(f"Ошибка чтения расходов: {e}")
        await update.message.reply_text("⚠️ Не получилось прочитать таблицу расходов.")
        return

    total_sales = 0.0
    for r in sales_rows:
        try:
            total_sales += float(r[3])
        except (IndexError, ValueError):
            continue

    total_expenses = 0.0
    for r in expense_rows:
        try:
            total_expenses += float(r[2])
        except (IndexError, ValueError):
            continue

    net_profit = total_sales - total_expenses

    await update.message.reply_text(
        f"📈 Итоги:\n\n"
        f"💰 Продажи: {total_sales:.2f}\n"
        f"💸 Расходы: {total_expenses:.2f}\n"
        f"📊 Чистая прибыль: {net_profit:.2f}"
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
        "/sold Машина Деталь Цена — отметить продажу (можно с фото)\n"
        "/cancel — отменить последнюю продажу\n\n"
        "Статистика:\n"
        "/total — общая сумма всех продаж\n"
        "/today — продажи за сегодня\n"
        "/month — продажи за текущий месяц\n"
        "/list — последние 10 продаж\n"
        "/find Деталь_или_Машина — поиск\n"
        "/byseller — статистика по продавцам\n"
        "/vehiclestats Машина — статистика по машине\n\n"
        "Финансы:\n"
        "/expense Название Сумма — записать расход\n"
        "/profit — чистая прибыль (продажи минус расходы)\n\n"
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
    app.add_handler(CommandHandler("total", total))
    app.add_handler(CommandHandler("today", today))
    app.add_handler(CommandHandler("month", month))
    app.add_handler(CommandHandler("list", list_sales))
    app.add_handler(CommandHandler("find", find))
    app.add_handler(CommandHandler("byseller", byseller))

    # По машинам
    app.add_handler(CommandHandler("vehiclestats", vehiclestats))

    # Финансы
    app.add_handler(CommandHandler("expense", expense))
    app.add_handler(CommandHandler("profit", profit))

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
