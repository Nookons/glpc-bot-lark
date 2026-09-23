import re


def parse_error_message(text: str):
    """
    Парсит сообщение вида:

        <error_type>: <error_text>. <robot>

    Например:
        Unable to drive: Security module failure. 3780

    Возвращает dict {error_type, error_text, robot} или None,
    если сообщение не подходит под формат.

    Чистая функция без побочных эффектов: сообщение об ошибке
    в чат отправляет вызывающий код (telegram_bot).
    """
    if not text:
        return None

    text = str(text).strip()

    match = re.match(r"^([^:]+):\s*(.+)\.\s*([^.]+)$", text)
    if not match:
        return None

    error_type = match.group(1).strip()
    error_text = match.group(2).strip()
    robot = match.group(3).strip()

    return {
        "error_type": error_type,
        "robot": robot,
        "error_text": error_text,
    }
