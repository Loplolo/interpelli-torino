import csv
import datetime
import io
import json
import os
import re
import urllib.parse

import requests
from bs4 import BeautifulSoup
from telegram.error import BadRequest, Forbidden
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters

url = os.environ.get("URL") or (
    "https://servizi.istruzionepiemonte.it/interpello2026/ric_interpello_ambito_to.php"
)
cache = os.environ.get("CACHE") or "interpelli.json"
TOKEN = os.environ.get("BOT_TOKEN")
USERS_FILE = os.environ.get("USERS_FILE") or "users.json"
SENT_FILE = os.environ.get("SENT_FILE") or "sent.json"


def strip_header(rows):
    if not rows:
        return rows
    header = rows[0]
    return [header] + [row for row in rows[1:] if row != header]


def scrape(url, cache):
    html = requests.get(url, timeout=30).text
    soup = BeautifulSoup(html, "lxml")
    rows = []
    for row in soup.find_all("tr"):
        cells = row.find_all(["td", "th"])
        record = [c.get_text(strip=True) for c in cells]
        rows.append(record)

    rows = strip_header(rows)

    if cache:
        with open(cache, "w", encoding="utf-8") as f:
            json.dump(rows, f, indent=2)
    return rows


def load_cache():
    try:
        with open(cache, encoding="utf-8") as f:
            return strip_header(json.load(f))
    except FileNotFoundError:
        return []


def load_sent():
    try:
        with open(SENT_FILE, encoding="utf-8") as f:
            return {tuple(row) for row in json.load(f)}
    except FileNotFoundError:
        return None


def save_sent(sent):
    with open(SENT_FILE, "w", encoding="utf-8") as f:
        json.dump([list(row) for row in sorted(sent)], f)


def to_csv(rows):
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerows(rows)
    return io.BytesIO(buf.getvalue().encode("utf-8"))


def parse_date(date_str):
    if not date_str:
        return None
    match = re.search(r"\d{2}/\d{2}/\d{4}", date_str)
    if match:
        try:
            return datetime.datetime.strptime(match.group(), "%d/%m/%Y").date()
        except ValueError:
            return None
    return None


def load_users():
    try:
        with open(USERS_FILE, "r") as f:
            return set(json.load(f))
    except FileNotFoundError:
        return set()


def save_user(chat_id):
    users = load_users()
    if chat_id not in users:
        users.add(chat_id)
        with open(USERS_FILE, "w") as f:
            json.dump(list(users), f)


def remove_user(chat_id):
    users = load_users()
    if chat_id in users:
        users.remove(chat_id)
        with open(USERS_FILE, "w") as f:
            json.dump(list(users), f)


def maps_link(row):
    name = row[1].strip() if len(row) > 1 else ""
    if not name:
        return None
    query = urllib.parse.quote(name)
    return f"https://www.google.com/maps/search/?api=1&query={query}"


def email_link(row):
    code = row[0].strip() if len(row) > 0 else ""
    if not code:
        return None
    email = code + "@istruzione.it"
    return email


def split_message(blocks, limit=4000):
    chunks = []
    current = ""
    for block in blocks:
        while len(block) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(block[:limit])
            block = block[limit:]
        if current and len(current) + len(block) > limit:
            chunks.append(current)
            current = ""
        current += block
    if current:
        chunks.append(current)
    return chunks


async def download(update, context):
    opening_filter = None
    closing_filter = None
    state_filter = None

    for arg in context.args:
        if "=" in arg:
            key, val = arg.split("=", 1)
            key = key.lower()
            if key in ["apertura", "da"]:
                opening_filter = parse_date(val)
            elif key in ["chiusura", "a"]:
                closing_filter = parse_date(val)
            elif key in ["stato"]:
                state_filter = val.lower()

    text = load_cache()

    filtered_text = []
    if text:
        filtered_text.append(text[0])
        for row in text[1:]:
            if state_filter and (len(row) <= 8 or state_filter not in row[8].lower()):
                continue

            if opening_filter or closing_filter:
                row_dates = [d for d in (parse_date(c) for c in row) if d is not None]

                if opening_filter and not any(d >= opening_filter for d in row_dates):
                    continue
                if closing_filter and not any(d <= closing_filter for d in row_dates):
                    continue

            filtered_text.append(row)

    if len(filtered_text) <= 1:
        await update.message.reply_text("Nessun dato rilevato")
        return

    csv_file = to_csv(filtered_text)

    filename_parts = ["interpelli"]
    if state_filter:
        filename_parts.append(state_filter)
    if opening_filter:
        filename_parts.append(f"ap_{opening_filter.strftime('%Y%m%d')}")
    if closing_filter:
        filename_parts.append(f"ch_{closing_filter.strftime('%Y%m%d')}")
    filename_parts.append(datetime.datetime.now().strftime("%Y%m%d_%H%M"))

    out_filename = "_".join(filename_parts) + ".csv"

    await update.message.reply_document(
        document=csv_file,
        filename=out_filename,
    )


async def update_data(context):
    new_text = scrape(url, cache)
    if not new_text:
        return

    sent = load_sent()
    if sent is None:
        save_sent({tuple(row) for row in new_text[1:]})
        return

    current = {tuple(row) for row in new_text[1:]}
    stale = sent - current if current else set()
    if stale:
        sent -= stale

    new_rows = []
    seen = set(sent)
    for row in new_text[1:]:
        key = tuple(row)
        if key in seen:
            continue
        seen.add(key)
        new_rows.append(row)

    if not new_rows:
        if stale:
            save_sent(sent)
        return

    known_records = {tuple(row[:8]) for row in sent}
    added = [row for row in new_rows if tuple(row[:8]) not in known_records]
    new_open = [row for row in added if "aperto" in row[8].lower()]

    blocks = []
    if new_open:
        blocks.append("Nuovi Interpelli Aperti:\n\n")
        for row in new_open:
            block = " - ".join(row) + "\n"
            link = maps_link(row)
            email = email_link(row)
            if link:
                block += f"📍 {link}\n"
            if email:
                block += f"📧 {email}\n"
            blocks.append(block + "\n")

    users = load_users()
    for chat_id in list(users):
        try:
            for chunk in split_message(blocks):
                await context.bot.send_message(chat_id=chat_id, text=chunk)
        except (Forbidden, BadRequest):
            remove_user(chat_id)

    sent.update(tuple(row) for row in new_rows)
    save_sent(sent)


async def start(update, context):
    save_user(update.effective_chat.id)
    messaggio = (
        "Comandi disponibili:\n"
        "- /download [data_apertura] [data_chiusura] [stato]\n\n"
        "Esempi:\n"
        "- /download\n"
        "- /download apertura=01/11/2026\n"
        "- /download chiusura=31/10/2026\n"
        "- /download stato=aperto\n"
    )
    await update.message.reply_text(messaggio)


if __name__ == "__main__":
    if not load_cache():
        try:
            scrape(url, cache)
        except Exception as exc:
            print(f"Initial scrape failed: {exc}", flush=True)

    if load_sent() is None:
        initial = load_cache()
        if initial:
            save_sent({tuple(row) for row in initial[1:]})

    app = ApplicationBuilder().token(TOKEN).build()

    app.job_queue.run_daily(
        update_data,
        time=datetime.time(hour=0, minute=5),
    )
    app.job_queue.run_daily(
        update_data,
        time=datetime.time(hour=6, minute=5),
    )
    app.job_queue.run_daily(
        update_data,
        time=datetime.time(hour=12, minute=5),
    )
    app.job_queue.run_daily(
        update_data,
        time=datetime.time(hour=18, minute=5),
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("download", download))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND))

    app.run_polling()
