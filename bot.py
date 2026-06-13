import os
import json
import logging
from datetime import datetime, date
import gspread
from google.oauth2.service_account import Credentials
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

logging.basicConfig(level=logging.INFO)

# ===== НАСТРОЙКИ =====
SPREADSHEET_ID = "1qwwCLpmu-FYMDAStR4qKWSSbpFBb-mG-kbNrcdKrSS8"
BOT_TOKEN = os.environ.get("BOT_TOKEN")

# ===== ПОДКЛЮЧЕНИЕ К GOOGLE SHEETS =====
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def get_sheet():
    creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    creds_dict = json.loads(creds_json)
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    client = gspread.authorize(creds)
    sheet = client.open_by_key(SPREADSHEET_ID).sheet1
    return sheet


# ===== КОМАНДЫ =====

async def sold(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /sold Название_детали Цена
    Можно отправить вместе с фото (фото как подпись к команде).
    """
    if len(context.args) < 2:
        await update.message.reply_text(
            "Используй так:\n/sold Название_детали Цена\n\nПример:\n/sold Бампер_GL450 5000\n\n"
            "Можно прикрепить фото детали к этому сообщению."
        )
        return

    try:
        price = float(context.args[-1])
    except ValueError:
        await update.message.reply_text("Последним должна быть цена (число). Пример: /sold Бампер_GL450 5000")
        return

    part_name = " ".join(context.args[:-1])
    seller = update.message.from_user.first_name
    now = datetime.now().strftime("%d.%m.%Y %H:%M")

    # Записываем в Google Таблицу
    try:
        sheet = get_sheet()
        sheet.append_row([now, part_name, price, seller])
    except Exception as e:
        logging.error(f"Ошибка записи в таблицу: {e}")
        await update.message.reply_text("⚠️ Не получилось записать в таблицу, но сообщение в группе оставлено.")

    text = (
        f"✅ Продано: {part_name}\n"
        f"Цена: {price:.2f}\n"
        f"Продал: {seller}\n"
        f"Дата: {now}"
    )

    # Если есть фото в этом же сообщении — отвечаем на него с фото
    if update.message.photo:
        await update.message.reply_photo(
            photo=update.message.photo[-1].file_id,
            caption=text,
        )
    else:
        await update.message.reply_text(text)


async def total(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/total — общая сумма всех продаж"""
    try:
        sheet = get_sheet()
        rows = sheet.get_all_values()[1:]  # пропускаем заголовок
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
            total_sum += float(row[2])
        except (IndexError, ValueError):
            continue

    await update.message.reply_text(
        f"📊 Всего продаж: {len(rows)}\n"
        f"💰 Общая сумма: {total_sum:.2f}"
    )


async def today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/today — продажи за сегодня"""
    try:
        sheet = get_sheet()
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
            price = float(r[2])
        except (IndexError, ValueError):
            price = 0.0
        total_sum += price
        part = r[1] if len(r) > 1 else "?"
        seller = r[3] if len(r) > 3 else "?"
        lines.append(f"• {part} — {price:.2f} ({seller})")

    lines.append(f"\n💰 Итого за сегодня: {total_sum:.2f}")
    await update.message.reply_text("\n".join(lines))


async def list_sales(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/list — последние 10 продаж"""
    try:
        sheet = get_sheet()
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
        part = r[1] if len(r) > 1 else "?"
        price = r[2] if len(r) > 2 else "?"
        seller = r[3] if len(r) > 3 else "?"
        lines.append(f"• {date_s} — {part} — {price} ({seller})")

    await update.message.reply_text("\n".join(lines))


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 Команды бота:\n\n"
        "/sold Название_детали Цена — отметить деталь как проданную (можно с фото)\n"
        "/total — общая сумма всех продаж\n"
        "/today — продажи за сегодня\n"
        "/list — последние 10 продаж\n"
        "/help — это сообщение"
    )


def main():
    if not BOT_TOKEN:
        raise ValueError("Переменная окружения BOT_TOKEN не задана!")
    if not os.environ.get("GOOGLE_CREDENTIALS_JSON"):
        raise ValueError("Переменная окружения GOOGLE_CREDENTIALS_JSON не задана!")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("sold", sold))
    app.add_handler(CommandHandler("total", total))
    app.add_handler(CommandHandler("today", today))
    app.add_handler(CommandHandler("list", list_sales))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("start", help_command))

    app.run_polling()


if __name__ == "__main__":
    main()
