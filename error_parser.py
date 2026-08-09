import re
from lark_send import send_text_message

def parse_error_message(text: str, chat_id: str):
    text = text.strip()

    match = re.match(r"^([^:]+):\s*(.+)\.\s*([^.]+)$", text)
    if not match:
        send_text_message(chat_id, 'The message is not fit to issue pattern. Please check the manual')
        return None

    error_type = match.group(1).strip()
    error_text = match.group(2).strip()
    robot = match.group(3).strip()

    return {
        "error_type": error_type,
        "robot": robot,
        "error_text": error_text,
    }