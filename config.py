import os

from dotenv import load_dotenv

load_dotenv()

# Lark-креденшелы нужны только для одного: загрузить фото в Lark
# (im/v1/images). Если их нет — бот работает, а фото уходит ссылкой,
# поэтому падать на старте из-за них нельзя.
APP_ID = os.getenv("LARK_APP_ID")
APP_SECRET = os.getenv("LARK_APP_SECRET")

if not APP_ID or not APP_SECRET:
    print(
        "LARK_APP_ID/LARK_APP_SECRET не заданы — фото будут уходить "
        "ссылкой (загрузка картинок в Lark недоступна)"
    )