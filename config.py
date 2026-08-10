import os
from dotenv import load_dotenv

load_dotenv()

APP_ID = os.getenv("LARK_APP_ID")
APP_SECRET = os.getenv("LARK_APP_SECRET")

if not APP_ID or not APP_SECRET:
    raise RuntimeError(
        "LARK_APP_ID and LARK_APP_SECRET must be set as environment variables"
    )