from datetime import time

from flask import Flask, request, jsonify
import json
import threading
from collections import OrderedDict
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
import os

from getUserName import get_user_name
from donwloadImage import download_image
from database import init_db, save_error, count_robot_errors_in_shift, shift_stats
from shift import get_current_shift
from pending_photos import handle_incoming_photo, forward_error
from error_parser import parse_error_message
from lark_send import send_text_message

from sendToDataBase import send_to_data_base
from datetime import datetime

from dotenv import load_dotenv
load_dotenv()

console = Console()
app = Flask(__name__)
init_db()

ERROR_THRESHOLD = 3  # после скольки ошибок за смену слать алерт
MESSAGE_MAX_AGE_SECONDS = 120  # пропускаем сообщения старше 2 минут

_SEEN_LIMIT = 2000
_seen_lock = threading.Lock()
_seen_message_ids = OrderedDict()


def _already_processed(message_id: str) -> bool:
    with _seen_lock:
        if message_id in _seen_message_ids:
            return True
        _seen_message_ids[message_id] = True
        if len(_seen_message_ids) > _SEEN_LIMIT:
            _seen_message_ids.popitem(last=False)
        return False


def _is_message_too_old(create_time) -> bool:
    """create_time от Lark приходит в миллисекундах (unix timestamp) как строка."""
    if not create_time:
        return False

    try:
        message_time = datetime.fromtimestamp(int(create_time) / 1000)
    except (ValueError, TypeError):
        return False

    age_seconds = (datetime.now() - message_time).total_seconds()
    return age_seconds > MESSAGE_MAX_AGE_SECONDS


@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()

    if data and "challenge" in data:
        return jsonify({"challenge": data["challenge"]})

    if data.get("header", {}).get("event_type") == "im.message.receive_v1":

        event = data["event"]
        message = event["message"]
        sender = event["sender"]
        chat_id = message.get("chat_id")
        message_id = message["message_id"]

        if _already_processed(message_id):
            console.print(f"[yellow]Повторная доставка {message_id}, пропускаю[/yellow]")
            return "", 200

        create_time = message.get("create_time")
        if _is_message_too_old(create_time):
            console.print(
                f"[yellow]Сообщение {message_id} старше {MESSAGE_MAX_AGE_SECONDS}с, пропускаю[/yellow]"
            )
            return "", 200

        user_id = sender["sender_id"].get("user_id")
        user_name = get_user_name(user_id, console)

        content = json.loads(message["content"])

        table = Table(show_header=False)
        table.add_row("👤 Пользователь", user_name)
        table.add_row("🆔 ID", user_id)
        table.add_row("💬 Чат", message["chat_type"])
        table.add_row("📝 Тип", message["message_type"])
        table.add_row("📨 Message ID", message_id)

        if message["message_type"] == "text":
            text = content.get("text", "")
            table.add_row("💭 Текст", text)

            parsed = parse_error_message(text, chat_id)

            if parsed:
                shift_date, shift_name = get_current_shift()

                save_error(
                    robot=parsed["robot"],
                    error_type=parsed["error_type"],
                    error_text=parsed["error_text"],
                    raw_text=text,
                    chat_id=chat_id,
                    shift_date=shift_date,
                    shift_name=shift_name,
                )

                table.add_row("🤖 Robot", parsed["robot"])
                table.add_row("⚠️ Issue Type", parsed["error_type"])

                count = count_robot_errors_in_shift(parsed["robot"], shift_date, shift_name)
                table.add_row("📊 Shift issues:", str(count))

                now = datetime.now()
                pretty = now.strftime("%d.%m.%Y %H:%M:%S")

                table_lines = [
                    ("👤 Employee", user_name),
                    ("🤖 Robot", parsed["robot"]),
                    ("⚠️ Time", pretty),
                    ("📝 Details", parsed["error_text"]),
                    ("📊 Shift issues", str(count)),
                ]

                data_obj = {
                    'employee': user_name,
                    'robot': parsed["robot"],
                    'error_text': parsed["error_text"],
                }

                forward_error(parsed, table_lines)
                send_to_data_base(parsed, data_obj, chat_id)

                if count >= ERROR_THRESHOLD:
                    alert = (
                        f"⚠️ Robot {parsed['robot']} have {count} exceptions"
                        f". Must be send to maintenance!"
                    )
                    send_text_message(chat_id, alert)
                    console.print(f"[bold red]{alert}[/bold red]")
            else:
                send_text_message(chat_id, "Can't parse the text from message, please try again")
                return "", 400

        elif message["message_type"] == "image":
            image_key = content.get("image_key")
            table.add_row("🖼 Image key", str(image_key))

            if image_key:
                filename = download_image(image_key, message_id, console)
                table.add_row("💾 Сохранено", filename or "Ошибка")

                if filename:
                    handle_incoming_photo(filename, console)

        console.print(
            Panel(table, title="[bold cyan]📩 Lark Message[/bold cyan]", border_style="green")
        )

    return "", 200


@app.route("/shift_stats", methods=["GET"])
def shift_stats_endpoint():
    shift_date = request.args.get("date")
    shift_name = request.args.get("shift")

    if not shift_date or not shift_name:
        shift_date, shift_name = get_current_shift()

    total, by_robot, by_type = shift_stats(shift_date, shift_name)
    return jsonify({
        "shift_date": shift_date,
        "shift_name": shift_name,
        "total_errors": total,
        "by_robot": by_robot,
        "by_error_type": by_type,
    })


if __name__ == "__main__":
    console.print("[bold green]Webhook запущен :7777[/bold green]")
    port = int(os.environ.get("PORT", 7777))
    app.run(host="0.0.0.0", port=port)