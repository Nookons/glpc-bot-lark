from lark_media import upload_image, send_image_via_hook, send_text_via_hook

TARGET_HOOK_URL = "https://open.larksuite.com/open-apis/bot/v2/hook/dc2c430d-ce07-4ff2-b5ca-0b92feb4f62a"

def handle_incoming_photo(image_path: str, console=None):
    try:
        image_key = upload_image(image_path)
    except Exception as e:
        if console:
            console.print(f"[bold red]Failed to upload image to Lark: {e}[/bold red]")
        return

    send_image_via_hook(TARGET_HOOK_URL, image_key)


def forward_error(parsed: dict, table_lines=None):
    plain_line = f"{parsed['error_type']}: {parsed['error_text']}. {parsed['robot']}"

    if table_lines:
        text_block = "\n".join(f"{label}: {value}" for label, value in table_lines)
    else:
        text_block = plain_line

    send_text_via_hook(TARGET_HOOK_URL, text_block)