#!/usr/bin/env python3
"""Гайд «Как записать ошибку через Telegram-бота» — 3 языка, стиль Apple.

Собирает три отдельные презентации (RU / EN / ZH) из одного набора макетов.
Тексты — в словаре `TEXTS`; английские названия кнопок и модулей не
переводятся, потому что так они выглядят в самом боте.
"""

import sys

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.oxml.ns import qn
from pptx.util import Inches, Pt

INK = RGBColor(0x1D, 0x1D, 0x1F)
MUTED = RGBColor(0x6E, 0x6E, 0x73)
LINE = RGBColor(0xD2, 0xD2, 0xD7)
ACCENT = RGBColor(0x00, 0x71, 0xE3)
BLACK = RGBColor(0x00, 0x00, 0x00)
WHITE = RGBColor(0xFF, 0xFF, 0xFF)
DARK_LINE = RGBColor(0x33, 0x33, 0x36)

FONT = "Helvetica Neue"
W, H = 13.333, 7.5
LM = 0.9
CW = W - 2 * LM

prs = None
BLANK = None

# ── Тексты ───────────────────────────────────────────────────────────────
TEXTS = {
    "ru": {
        "cover_title": "Как записать\nошибку через бота",
        "step_prefix": "Шаг",
        "cover_sub": "Инструкция для оператора",
        "flow_eyebrow": "Маршрут",
        "flow_title": "Восемь шагов.\nОдна минута.",
        "flow_sub": "Бот спрашивает по одному вопросу — ошибиться почти негде.",
        "flow_note": "Модель бот обычно подставляет сам из справочника — шаг 04 вы не увидите.",
        "steps": [
            ("01", "Фото", "в топик ошибок"),
            ("02", "Что на фото", "Robot / Charging / Workstation"),
            ("03", "Номер", "3780 · 137 · C2-H146"),
            ("04", "Модель", "обычно сама"),
            ("05", "Модуль", "где сломалось"),
            ("06", "Ошибка", "что случилось"),
            ("07", "Причина", "почему"),
            ("08", "Confirm", "отправить"),
        ],
        "reg_eyebrow": "Перед началом",
        "reg_title": "Привяжите Telegram",
        "reg_sub": "Один раз, до первого отчёта.",
        "reg_name": "Имя — как в Lark.",
        "reg_check": "Проверить привязку: /whoami",
        "reg_note": "Без привязки отчёт сохранится, но в поле «кто сообщил» будет пусто —\n"
                    "сейчас так приходят 11 % отчётов.",
        "photo_title": "Фото",
        "photo_sub": "Одно фото — один отчёт.",
        "photo_left": "Что делать",
        "photo_left_lines": [
            "Прикрепите одно фото к сообщению в топике ошибок.",
            "Подпись не нужна — бот спросит всё сам.",
            "Именно фото, до 25 МБ.",
        ],
        "photo_right": "Что будет",
        "photo_right_lines": [
            "Бот удалит ваше сообщение — тема не засоряется.",
            "Пришлёт то же фото с вопросом и кнопками.",
            "Иногда сам узнает оборудование и пропустит первый вопрос.",
        ],
        "photo_note": "Зачем — фото уходит в Lark-карточку и в журнал: по нему разбирают неисправность.",
        "cat_title": "Что на фото?",
        "cat_sub": "Одна из трёх кнопок.",
        "cat_cols": [
            ("Workstation", "Рабочее место,\nконвейер, станция отбора", "Не крутится лента,\nзастрял товар"),
            ("Charging station", "Сама зарядная станция,\nне робот на ней", "Не заряжает,\nперезагружается"),
            ("Robot", "Робот: K50H, A42T,\nA42T C2", "Не едет, не поднимает,\nаварийная кнопка"),
        ],
        "cat_when": "Когда",
        "cat_note": "QR-код пола: отдельной кнопки нет — сообщайте через Robot → QR Code.",
        "num_title": "Номер",
        "num_sub": "Одним сообщением. Только номер.",
        "num_rows": [
            ("Robot", "3780 или H108 — цифры или H + цифры"),
            ("Charging station", "137 — номер станции, только цифры"),
            ("Workstation", "C2-H146 — код с наклейки"),
        ],
        "num_split": [("Несколько устройств — через слэш: ", False), ("3545/314/141", True),
                      (" → три отдельные записи.", False)],
        "num_note": "Зачем — номер связывает запись с карточкой, складом и зоной.",
        "reg_title2": "Если номера нет\nв справочнике",
        "reg_sub2": "Формат — жёстко. Справочник — мягко.",
        "reg_confirm": " — сохранить и пометить «на разбор».",
        "reg_edit": " — вернуться и исправить.",
        "reg_note2": "Если ввести описание вместо номера — бот попросит номер заново.",
        "model_title": "Модель",
        "model_lead": "Обычно этого шага нет.",
        "model_sub": "Бот берёт модель из справочника по номеру.",
        "model_cols": [("K50H", "мини-робот\n(HAIFLEX)"), ("A42T", "большой робот"),
                       ("A42T C2", "версия C2,\nесть Hook")],
        "model_note": "Вопрос появится, только если номера нет в реестре или в строке разные модели.",
        "module_title": "Модуль",
        "module_sub": "Где именно сломалось.",
        "module_note": "Модуль — «где». Ошибка — «что».",
        "error_title": "Ошибка",
        "error_sub": "Выбирайте то, что видите. Тексты дословные.",
        "cause_eyebrow": "Шаги 07–08",
        "cause_precise": "Чем точнее текст, тем полезнее разбор.",
        "cause_title": "Причина",
        "cause_sub": "Почему это произошло.",
        "cause_left": [("Chassis ", True), ("Obstacle · Collision · Mechanical · Software fault", False)],
        "cause_left2": [("Safety ", True), ("Human error · Mechanical wear · Safety Vest", False)],
        "cause_left3": [("Charging ", True), ("Power or cable · Station fault · Battery fault", False)],
        "cause_other": [("Нет подходящей — ", False), ("Other", True), (".", False)],
        "cause_desc": "Тогда бот попросит описать словами одним сообщением.",
        "cause_note": "Ошибка — «что». Причина — «почему». Это две разные метрики.",
        "sum_title": "Проверьте\nи подтвердите",
        "sum_after": "После Confirm",
        "sum_after_lines": ["Saving…", "Error report saved", "Журнал · Supabase · Lark",
                            "Неизвестный номер — в Needs review"],
        "sum_btns": [("Confirm", " — отправить. "), ("Edit selection", " — к номеру. "),
                     ("Cancel", " — отменить.")],
        "sum_note": "Проверка перед записью: исправить здесь дешевле, чем искать в журнале.",
        "res_eyebrow": "Результат",
        "res_title": "Одна запись.\nВсё внутри.",
        "res_sub": "Пример: запись 445928 от 06.11.2026.",
        "res_fields": [("Склад", "GLP-C"), ("Номер", "8066"), ("Модуль", "Chassis"),
                       ("Ошибка", "Went off the road"), ("Причина", "Mechanical"),
                       ("Описание", "The robot left its route — текст из шаблона")],
        "res_auto": "Автоматически",
        "res_auto_lines": ["склад — по номеру", "зона — по устройству", "смена — по времени",
                           "автор — из /reg", "фото — из сообщения"],
        "err_eyebrow": "Если что-то не так",
        "err_title": "Частые ошибки",
        "err_rows": [
            ("Пусто «кто сообщил»", "Сделайте /reg и отправьте отчёт заново"),
            ("«Please try again»", "В номере текст — введите только номер"),
            ("«Not in the registry»", "Edit number — опечатка · Confirm — новый"),
            ("Бот молчит на фото", "Это не фото или чужой топик"),
            ("Edit selection вернул к номеру", "Так и задумано — пройдите ветку заново"),
        ],
        "err_note": "Почти все сбои — номер не в том формате или отчёт не в том топике.",
        "cheat_eyebrow": "Шпаргалка",
        "cheat_title": "Вся инструкция",
        "cheat_items": [
            "/reg Имя Фамилия — один раз",
            "Фото — в топик ошибок",
            "Workstation / Charging station / Robot",
            "Номер — 3780 · 137 · C2-H146 · несколько через /",
            "Модуль → Ошибка → Причина",
            "Other — опишите словами",
            "Confirm — отправить",
        ],
        "cheat_note": "Сохраните эту страницу — остальное бот подскажет сам.",
        "footer": "Telegram bot",
    },
    "en": {
        "cover_title": "How to report\nan error with the bot",
        "step_prefix": "Step",
        "cover_sub": "A guide for operators",
        "flow_eyebrow": "The flow",
        "flow_title": "Eight steps.\nOne minute.",
        "flow_sub": "The bot asks one question at a time — there is little room to go wrong.",
        "flow_note": "The bot usually fills in the model from the registry — you will not see step 04.",
        "steps": [
            ("01", "Photo", "to the error topic"),
            ("02", "What's in the photo", "Robot / Charging / Workstation"),
            ("03", "Number", "3780 · 137 · C2-H146"),
            ("04", "Model", "usually automatic"),
            ("05", "Module", "where it broke"),
            ("06", "Error", "what happened"),
            ("07", "Cause", "why"),
            ("08", "Confirm", "send"),
        ],
        "reg_eyebrow": "Before you start",
        "reg_title": "Link your Telegram",
        "reg_sub": "Once, before your first report.",
        "reg_name": "The name as written in Lark.",
        "reg_check": "Check the link: /whoami",
        "reg_note": "Without the link the report is saved, but “reported by” stays empty —\n"
                    "that is how 11 % of reports arrive today.",
        "photo_title": "Photo",
        "photo_sub": "One photo — one report.",
        "photo_left": "What to do",
        "photo_left_lines": [
            "Attach one photo to a message in the error topic.",
            "No caption needed — the bot asks everything itself.",
            "A photo, up to 25 MB.",
        ],
        "photo_right": "What happens",
        "photo_right_lines": [
            "The bot deletes your message — the topic stays clean.",
            "It sends the same photo back with a question and buttons.",
            "Sometimes it recognises the unit and skips the first question.",
        ],
        "photo_note": "Why — the photo goes to the Lark card and the journal: it is how the fault is reviewed.",
        "cat_title": "What is in the photo?",
        "cat_sub": "One of three buttons.",
        "cat_cols": [
            ("Workstation", "A workstation, conveyor\nor pick station", "Conveyor stopped,\nitem stuck"),
            ("Charging station", "The charging station itself,\nnot the robot on it", "Not charging,\nrebooting"),
            ("Robot", "A robot: K50H, A42T,\nA42T C2", "Not moving, not lifting,\nemergency button"),
        ],
        "cat_when": "When",
        "cat_note": "Floor QR code: there is no separate button — report it via Robot → QR Code.",
        "num_title": "Number",
        "num_sub": "One message. The number only.",
        "num_rows": [
            ("Robot", "3780 or H108 — digits, or H + digits"),
            ("Charging station", "137 — the station number, digits only"),
            ("Workstation", "C2-H146 — the code on the label"),
        ],
        "num_split": [("Several devices — separated by a slash: ", False), ("3545/314/141", True),
                      (" → three separate records.", False)],
        "num_note": "Why — the number links the record to the card, the warehouse and the zone.",
        "reg_title2": "If the number is\nnot in the registry",
        "reg_sub2": "Format is strict. The registry is lenient.",
        "reg_confirm": " — save and mark it for review.",
        "reg_edit": " — go back and fix it.",
        "reg_note2": "If you type a description instead of a number, the bot asks again.",
        "model_title": "Model",
        "model_lead": "Usually this step does not exist.",
        "model_sub": "The bot takes the model from the registry by number.",
        "model_cols": [("K50H", "mini robot\n(HAIFLEX)"), ("A42T", "large robot"),
                       ("A42T C2", "C2 version,\nwith Hook")],
        "model_note": "The question appears only if the number is unknown or the line mixes models.",
        "module_title": "Module",
        "module_sub": "Where exactly it broke.",
        "module_note": "The module is “where”. The error is “what”.",
        "error_title": "Error",
        "error_sub": "Choose what you see. The wording is fixed.",
        "cause_eyebrow": "Steps 07–08",
        "cause_precise": "The more precise the text, the more useful the review.",
        "cause_title": "Cause",
        "cause_sub": "Why it happened.",
        "cause_left": [("Chassis ", True), ("Obstacle · Collision · Mechanical · Software fault", False)],
        "cause_left2": [("Safety ", True), ("Human error · Mechanical wear · Safety Vest", False)],
        "cause_left3": [("Charging ", True), ("Power or cable · Station fault · Battery fault", False)],
        "cause_other": [("None of these — pick ", False), ("Other", True), (".", False)],
        "cause_desc": "The bot then asks you to describe it in one message.",
        "cause_note": "The error is “what”. The cause is “why”. Two different metrics.",
        "sum_title": "Check\nand confirm",
        "sum_after": "After Confirm",
        "sum_after_lines": ["Saving…", "Error report saved", "Journal · Supabase · Lark",
                            "Unknown number → Needs review"],
        "sum_btns": [("Confirm", " — send. "), ("Edit selection", " — to the number. "),
                     ("Cancel", " — discard.")],
        "sum_note": "A check before saving: fixing it here is cheaper than finding it in the journal.",
        "res_eyebrow": "Result",
        "res_title": "One record.\nEverything inside.",
        "res_sub": "Example: record 445928, 06.11.2026.",
        "res_fields": [("Warehouse", "GLP-C"), ("Number", "8066"), ("Module", "Chassis"),
                       ("Error", "Went off the road"), ("Cause", "Mechanical"),
                       ("Description", "The robot left its route — template text")],
        "res_auto": "Automatic",
        "res_auto_lines": ["warehouse — by number", "zone — by unit", "shift — by time",
                           "author — from /reg", "photo — from the message"],
        "err_eyebrow": "If something is wrong",
        "err_title": "Common issues",
        "err_rows": [
            ("“Reported by” is empty", "Run /reg and send the report again"),
            ("“Please try again”", "The number contains text — enter digits only"),
            ("“Not in the registry”", "Edit number — typo · Confirm — new unit"),
            ("The bot ignores a photo", "It is not a photo, or not the error topic"),
            ("Edit selection went to the number", "By design — walk the branch again"),
        ],
        "err_note": "Almost every issue is a wrong number format or a wrong topic.",
        "cheat_eyebrow": "Cheat sheet",
        "cheat_title": "The whole guide",
        "cheat_items": [
            "/reg Your Name — once",
            "Photo — to the error topic",
            "Workstation / Charging station / Robot",
            "Number — 3780 · 137 · C2-H146 · several via /",
            "Module → Error → Cause",
            "Other — describe it in words",
            "Confirm — send",
        ],
        "cheat_note": "Keep this page — the bot will guide you through the rest.",
        "footer": "Telegram bot",
    },
    "zh": {
        "cover_title": "如何通过\nTelegram Bot 记录故障",
        "step_prefix": "步骤",
        "cover_sub": "操作员指南",
        "flow_eyebrow": "流程",
        "flow_title": "八个步骤。\n一分钟。",
        "flow_sub": "Bot 每次只问一个问题——几乎不会出错。",
        "flow_note": "Bot 通常会自动填入型号——你不会看到第 04 步。",
        "steps": [
            ("01", "照片", "发到故障话题"),
            ("02", "照片里是什么", "Robot / Charging / Workstation"),
            ("03", "编号", "3780 · 137 · C2-H146"),
            ("04", "型号", "通常自动"),
            ("05", "模块", "哪里坏了"),
            ("06", "故障", "出了什么故障"),
            ("07", "原因", "为什么"),
            ("08", "Confirm", "发送"),
        ],
        "reg_eyebrow": "开始之前",
        "reg_title": "绑定 Telegram",
        "reg_sub": "只需一次，在第一次报故障之前。",
        "reg_name": "姓名与 Lark 中一致。",
        "reg_check": "检查绑定：/whoami",
        "reg_note": "未绑定时记录仍会保存，但“报告人”一栏为空——\n目前约 11% 的记录是这样。",
        "photo_title": "照片",
        "photo_sub": "一张照片对应一条记录。",
        "photo_left": "怎么做",
        "photo_left_lines": [
            "在故障话题中发送一条带一张照片的消息。",
            "无需文字说明——Bot 会自己提问。",
            "必须是照片，最大 25 MB。",
        ],
        "photo_right": "会发生什么",
        "photo_right_lines": [
            "Bot 会删除你的消息，话题保持整洁。",
            "会把同一张照片发回，并附上问题和按钮。",
            "有时会自动识别设备，跳过第一个问题。",
        ],
        "photo_note": "为什么——照片会进入 Lark 卡片和日志，用于排查故障。",
        "cat_title": "照片里是什么？",
        "cat_sub": "三个按钮之一。",
        "cat_cols": [
            ("Workstation", "工作台、传送带\n或拣选站", "传送带不转、\n货物卡住"),
            ("Charging station", "充电站本身，\n不是上面的机器人", "不充电、\n反复重启"),
            ("Robot", "机器人：K50H、A42T、\nA42T C2", "不走、不举升、\n急停按钮"),
        ],
        "cat_when": "何时选择",
        "cat_note": "地面二维码没有单独按钮——请通过 Robot → QR Code 报告。",
        "num_title": "编号",
        "num_sub": "一条消息，只写编号。",
        "num_rows": [
            ("Robot", "3780 或 H108——数字，或 H + 数字"),
            ("Charging station", "137——充电站编号，只能是数字"),
            ("Workstation", "C2-H146——标签上的代码"),
        ],
        "num_split": [("多台设备——用斜杠分隔：", False), ("3545/314/141", True),
                      (" → 三条独立记录。", False)],
        "num_note": "为什么——编号把记录与设备卡、仓库和区域关联起来。",
        "reg_title2": "如果编号\n不在台账中",
        "reg_sub2": "格式严格。台账宽松。",
        "reg_confirm": " —— 保存并标记为“待复核”。",
        "reg_edit": " —— 返回修改。",
        "reg_note2": "如果输入的是描述而不是编号，Bot 会重新询问编号。",
        "model_title": "型号",
        "model_lead": "通常没有这一步。",
        "model_sub": "Bot 会根据编号从台账中取型号。",
        "model_cols": [("K50H", "小型机器人\n(HAIFLEX)"), ("A42T", "大型机器人"),
                       ("A42T C2", "C2 版本，\n带 Hook")],
        "model_note": "只有当编号不在台账中，或一行里有不同型号时才会询问。",
        "module_title": "模块",
        "module_sub": "具体哪里坏了。",
        "module_note": "模块是“哪里”。故障是“什么”。",
        "error_title": "故障",
        "error_sub": "选择你看到的。文字是固定的。",
        "cause_eyebrow": "步骤 07–08",
        "cause_precise": "描述越准确，分析越有用。",
        "cause_title": "原因",
        "cause_sub": "为什么会发生。",
        "cause_left": [("Chassis ", True), ("Obstacle · Collision · Mechanical · Software fault", False)],
        "cause_left2": [("Safety ", True), ("Human error · Mechanical wear · Safety Vest", False)],
        "cause_left3": [("Charging ", True), ("Power or cable · Station fault · Battery fault", False)],
        "cause_other": [("没有合适的——选 ", False), ("Other", True), ("。", False)],
        "cause_desc": "Bot 会让你用一条消息描述。",
        "cause_note": "故障是“什么”。原因是“为什么”。这是两个不同的指标。",
        "sum_title": "检查并确认",
        "sum_after": "确认之后",
        "sum_after_lines": ["Saving…", "Error report saved", "日志 · Supabase · Lark",
                            "未知编号 → Needs review"],
        "sum_btns": [("Confirm", " —— 发送。"), ("Edit selection", " —— 返回编号。"),
                     ("Cancel", " —— 取消。")],
        "sum_note": "记录前检查：在这里修改比之后在日志里找更省事。",
        "res_eyebrow": "结果",
        "res_title": "一条记录。\n包含全部信息。",
        "res_sub": "示例：记录 445928，2026-11-06。",
        "res_fields": [("仓库", "GLP-C"), ("编号", "8066"), ("模块", "Chassis"),
                       ("故障", "Went off the road"), ("原因", "Mechanical"),
                       ("描述", "The robot left its route（模板文字）")],
        "res_auto": "自动填写",
        "res_auto_lines": ["仓库——按编号", "区域——按设备", "班次——按时间",
                           "报告人——来自 /reg", "照片——来自消息"],
        "err_eyebrow": "如果出现问题",
        "err_title": "常见问题",
        "err_rows": [
            ("“报告人”为空", "先 /reg，再重新发送"),
            ("“Please try again”", "编号里有文字——只输入编号"),
            ("“Not in the registry”", "Edit number 改错字 · Confirm 新设备"),
            ("Bot 不回应照片", "不是照片，或不是故障话题"),
            ("Edit selection 回到编号", "设计如此——请重新走一遍"),
        ],
        "err_note": "几乎所有问题——编号格式不对，或发错了话题。",
        "cheat_eyebrow": "速查表",
        "cheat_title": "全部流程",
        "cheat_items": [
            "/reg 姓名 —— 只做一次",
            "照片 —— 发到故障话题",
            "Workstation / Charging station / Robot",
            "编号 —— 3780 · 137 · C2-H146 · 多个用 /",
            "模块 → 故障 → 原因",
            "Other —— 用文字描述",
            "Confirm —— 发送",
        ],
        "cheat_note": "保存这一页——其余 Bot 会提示。",
        "footer": "Telegram bot",
    },
}

# Общие для всех языков данные: модули и ошибки — как в боте.
MODULES = [
    ("K50H", "Chassis · QR Code · Safety · Lifting · Network · Other · Program Bug"),
    ("A42T", "Chassis · Lifting · Rotation · Tray · Horn · QR Code · Safety · Fingers · Network"),
    ("A42T C2", "Chassis · Lifting · Rotation · Tray · Horn · Hook · Safety · QR Code"),
]
ERRORS = [
    ("Chassis", "Went off the road · Robot's collision · Stop · Unable to rotate"),
    ("QR Code / Lifting", "QR Code dirty · Mechanical · Can't lift up/down the fork · Lost container"),
    ("Safety", "Front emergency button is trigger · Rear emergency button is trigger · "
               "Safety bumper trigger · Safety system disconnect"),
    ("Rotation / Tray / Horn / Hook", "Can't rotate fork · Can't extend/expand the tray · "
                                      "Can't extend/expand the HORN · Can't extend/expand the Hook"),
]
SUMMARY_FIELDS = [("Object", "Robot"), ("Device", "8066"), ("Module", "Chassis"),
                  ("Error", "Went off the road"), ("Cause", "Mechanical")]


def build(lang, out_path):
    global prs, BLANK
    prs = Presentation()
    prs.slide_width = Inches(W)
    prs.slide_height = Inches(H)
    BLANK = prs.slide_layouts[6]
    t = TEXTS[lang]

    # ── помощники ────────────────────────────────────────────────────────
    def flatten(shape):
        style = shape._element.find(qn("p:style"))
        if style is not None:
            shape._element.remove(style)

    def slide(bg=WHITE):
        s = prs.slides.add_slide(BLANK)
        rect = s.shapes.add_shape(MSO_SHAPE.RECTANGLE, 0, 0, prs.slide_width, prs.slide_height)
        rect.fill.solid()
        rect.fill.fore_color.rgb = bg
        rect.line.fill.background()
        rect.shadow.inherit = False
        flatten(rect)
        return s

    def _run(p, value, size, color, bold=False, spacing=None, italic=False):
        r = p.add_run()
        r.text = value
        r.font.name = FONT
        r.font.size = Pt(size)
        r.font.bold = bold
        r.font.italic = italic
        r.font.color.rgb = color
        if spacing:
            r.font._element.set("spc", str(spacing))
        return r

    def text(s, x, y, w, h, value, size=18, color=INK, bold=False, italic=False,
             align=PP_ALIGN.LEFT, anchor=MSO_ANCHOR.TOP, spacing=None, line=1.15):
        tb = s.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
        tf = tb.text_frame
        tf.word_wrap = True
        tf.margin_left = tf.margin_right = 0
        tf.margin_top = tf.margin_bottom = 0
        tf.vertical_anchor = anchor
        p = tf.paragraphs[0]
        p.alignment = align
        p.line_spacing = line
        _run(p, value, size, color, bold=bold, spacing=spacing, italic=italic)
        return tb

    def rich(s, x, y, w, h, lines, size=18, color=INK, line=1.35):
        tb = s.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
        tf = tb.text_frame
        tf.word_wrap = True
        tf.margin_left = tf.margin_right = 0
        tf.margin_top = tf.margin_bottom = 0
        first = True
        for item in lines:
            p = tf.paragraphs[0] if first else tf.add_paragraph()
            first = False
            p.line_spacing = line
            p.space_after = Pt(10)
            parts = item if isinstance(item, list) else [(item, False)]
            for chunk, bold in parts:
                _run(p, chunk, size, color, bold=bold)
        return tb

    def eyebrow(s, value, color=MUTED, y=0.55, spacing=220):
        text(s, LM, y, CW, 0.3, value.upper(), size=10.5, color=color, spacing=spacing)

    def headline(s, value, y=0.9, size=44, color=INK, w=None, line=1.05):
        text(s, LM, y, w or CW, 1.6, value, size=size, color=color, bold=True, line=line)

    def sub(s, value, y=1.85, color=MUTED, size=18):
        text(s, LM, y, CW, 0.6, value, size=size, color=color, line=1.25)

    def haircut(s, y, x=LM, w=CW, color=LINE, thickness=0.008):
        rect = s.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(x), Inches(y),
                                  Inches(w), Inches(thickness))
        rect.fill.solid()
        rect.fill.fore_color.rgb = color
        rect.line.fill.background()
        rect.shadow.inherit = False
        flatten(rect)

    def vline(s, x, y, h, color=LINE, thickness=0.008):
        rect = s.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(x), Inches(y),
                                  Inches(thickness), Inches(h))
        rect.fill.solid()
        rect.fill.fore_color.rgb = color
        rect.line.fill.background()
        rect.shadow.inherit = False
        flatten(rect)

    def footer(s, n):
        text(s, LM, 7.08, 7.0, 0.25, t["footer"], size=10, color=MUTED, spacing=60)
        text(s, W - LM - 0.6, 7.08, 0.6, 0.25, f"{n:02d}", size=10, color=MUTED,
             align=PP_ALIGN.RIGHT)

    def ghost(s, n):
        text(s, W - LM - 1.6, 0.38, 1.6, 1.0, f"{n:02d}", size=56, color=LINE, bold=True,
             align=PP_ALIGN.RIGHT, line=1.0)

    def note(s, value, y=6.35, size=14, color=MUTED):
        text(s, LM, y, CW, 0.5, value, size=size, color=color, line=1.3)

    # 01 ─ титул
    s = slide(BLACK)
    text(s, LM, 0.7, CW, 0.3, "TELEGRAM BOT", size=11, color=RGBColor(0x86, 0x86, 0x8B),
         spacing=260)
    text(s, LM, 1.95, 11.2, 2.4, t["cover_title"], size=62, color=WHITE, bold=True, line=1.02)
    text(s, LM, 4.75, CW, 0.4, t["cover_sub"], size=20, color=RGBColor(0xA1, 0xA1, 0xA6))
    haircut(s, 5.85, color=DARK_LINE)
    text(s, LM, 6.15, 6.0, 0.3, "GLP-C  ·  GLP-C1  ·  SMALL-P3", size=13,
         color=RGBColor(0x86, 0x86, 0x8B))
    text(s, W - LM - 2.0, 6.15, 2.0, 0.3, "07.11.2026", size=13,
         color=RGBColor(0x86, 0x86, 0x8B), align=PP_ALIGN.RIGHT)
    text(s, LM, 7.08, 7.0, 0.25, t["footer"], size=10, color=RGBColor(0x86, 0x86, 0x8B))
    text(s, W - LM - 0.6, 7.08, 0.6, 0.25, "01", size=10, color=RGBColor(0x86, 0x86, 0x8B),
         align=PP_ALIGN.RIGHT)

    # 02 ─ маршрут
    s = slide()
    eyebrow(s, t["flow_eyebrow"])
    headline(s, t["flow_title"])
    sub(s, t["flow_sub"], y=2.45)
    for i, (num, name, descr) in enumerate(t["steps"]):
        row, col = divmod(i, 4)
        x = LM + col * (CW / 4)
        y = 3.45 + row * 1.55
        text(s, x, y, 0.45, 0.3, num, size=11, color=ACCENT, bold=True, spacing=60)
        text(s, x, y + 0.32, CW / 4 - 0.25, 0.55, name, size=18, color=INK, bold=True, line=1.1)
        text(s, x, y + 0.9, CW / 4 - 0.25, 0.5, descr, size=12, color=MUTED, line=1.2)
    haircut(s, 4.98, color=LINE)
    note(s, t["flow_note"], y=6.45)
    footer(s, 2)

    # 03 ─ /reg
    s = slide()
    eyebrow(s, t["reg_eyebrow"])
    headline(s, t["reg_title"])
    sub(s, t["reg_sub"])
    text(s, LM, 2.85, CW, 0.8, "/reg Ivan Petrenko", size=40, color=INK, bold=True)
    haircut(s, 3.85)
    rich(s, LM, 4.1, 8.6, 1.2,
         [[(t["reg_name"], True)], t["reg_check"]], size=18, color=INK)
    note(s, t["reg_note"], y=5.75, size=15)
    footer(s, 3)

    # 04 ─ фото
    s = slide()
    ghost(s, 1)
    eyebrow(s, f"{t['step_prefix']} 01", spacing=80)
    headline(s, t["photo_title"])
    sub(s, t["photo_sub"])
    text(s, LM, 2.55, 4.9, 0.3, t["photo_left"].upper(), size=11, color=MUTED, spacing=180)
    rich(s, LM, 2.95, 4.9, 2.8, t["photo_left_lines"], size=17, color=INK, line=1.4)
    vline(s, 6.35, 2.75, 2.9)
    text(s, 6.85, 2.55, 5.4, 0.3, t["photo_right"].upper(), size=11, color=MUTED, spacing=180)
    rich(s, 6.85, 2.95, 5.4, 2.8, t["photo_right_lines"], size=17, color=INK, line=1.4)
    note(s, t["photo_note"])
    footer(s, 4)

    # 05 ─ категория
    s = slide()
    ghost(s, 2)
    eyebrow(s, f"{t['step_prefix']} 02", spacing=80)
    headline(s, t["cat_title"])
    sub(s, t["cat_sub"])
    for i, (name, what, when) in enumerate(t["cat_cols"]):
        x = LM + i * (CW / 3)
        haircut(s, 2.6, x=x, w=CW / 3 - 0.35, color=INK)
        text(s, x, 2.8, CW / 3 - 0.35, 0.45, name, size=21, color=INK, bold=True, line=1.05)
        text(s, x, 3.35, CW / 3 - 0.35, 0.9, what, size=14, color=MUTED, line=1.3)
        text(s, x, 4.55, CW / 3 - 0.35, 0.25, t["cat_when"].upper(), size=10, color=MUTED,
             spacing=160)
        text(s, x, 4.85, CW / 3 - 0.35, 1.0, when, size=14, color=INK, line=1.3)
    note(s, t["cat_note"])
    footer(s, 5)

    # 06 ─ номер
    s = slide()
    ghost(s, 3)
    eyebrow(s, f"{t['step_prefix']} 03", spacing=80)
    headline(s, t["num_title"])
    sub(s, t["num_sub"])
    y = 2.65
    for label, value in t["num_rows"]:
        text(s, LM, y, 3.6, 0.35, label, size=14, color=MUTED)
        text(s, LM + 3.8, y - 0.06, CW - 3.8, 0.7, value, size=18, color=INK, line=1.2)
        haircut(s, y + 0.62)
        y += 0.95
    rich(s, LM, 5.55, CW, 0.8, [t["num_split"]], size=17, color=INK)
    note(s, t["num_note"], y=6.35)
    footer(s, 6)

    # 07 ─ проверка
    s = slide()
    ghost(s, 3)
    eyebrow(s, f"{t['step_prefix']} 03", spacing=80)
    headline(s, t["reg_title2"])
    sub(s, t["reg_sub2"], y=2.5)
    text(s, LM, 3.2, CW, 0.9,
         "Not in the registry: Robot 9999.\nSave anyway? The entry will be marked for review.",
         size=20, color=MUTED, line=1.45, italic=True)
    haircut(s, 4.55)
    rich(s, LM, 4.8, CW, 1.2, [
        [("Confirm", True), (t["reg_confirm"], False)],
        [("Edit number", True), (t["reg_edit"], False)],
    ], size=18, color=INK, line=1.5)
    note(s, t["reg_note2"], y=6.3)
    footer(s, 7)

    # 08 ─ модель
    s = slide()
    ghost(s, 4)
    eyebrow(s, f"{t['step_prefix']} 04", spacing=80)
    headline(s, t["model_title"])
    text(s, LM, 2.0, CW, 0.65, t["model_lead"], size=25, color=INK, bold=True, line=1.1)
    sub(s, t["model_sub"], y=2.7)
    for i, (name, descr) in enumerate(t["model_cols"]):
        x = LM + i * (CW / 3)
        haircut(s, 3.55, x=x, w=CW / 3 - 0.35, color=INK)
        text(s, x, 3.75, CW / 3 - 0.35, 0.45, name, size=21, color=INK, bold=True)
        text(s, x, 4.3, CW / 3 - 0.35, 0.9, descr, size=14, color=MUTED, line=1.3)
    note(s, t["model_note"], y=5.6)
    footer(s, 8)

    # 09 ─ модуль
    s = slide()
    ghost(s, 5)
    eyebrow(s, f"{t['step_prefix']} 05", spacing=80)
    headline(s, t["module_title"])
    sub(s, t["module_sub"])
    y = 2.6
    for label, value in MODULES:
        text(s, LM, y, 2.0, 0.35, label, size=14, color=MUTED)
        text(s, LM + 2.2, y - 0.06, CW - 2.2, 0.9, value, size=16, color=INK, line=1.25)
        haircut(s, y + 0.85)
        y += 1.2
    note(s, t["module_note"], y=6.3)
    footer(s, 9)

    # 10 ─ ошибка
    s = slide()
    ghost(s, 6)
    eyebrow(s, f"{t['step_prefix']} 06", spacing=80)
    headline(s, t["error_title"])
    sub(s, t["error_sub"])
    y = 2.55
    for label, value in ERRORS:
        text(s, LM, y, 3.4, 0.3, label, size=13, color=MUTED)
        text(s, LM + 3.6, y - 0.08, CW - 3.6, 0.9, value, size=15, color=INK, line=1.25)
        haircut(s, y + 0.8)
        y += 1.05
    footer(s, 10)

    # 11 ─ причина
    s = slide()
    ghost(s, 7)
    eyebrow(s, t["cause_eyebrow"])
    headline(s, t["cause_title"])
    sub(s, t["cause_sub"])
    rich(s, LM, 2.7, 6.3, 2.6, [t["cause_left"], t["cause_left2"], t["cause_left3"]],
         size=16, color=INK, line=1.4)
    vline(s, 7.35, 2.7, 2.6)
    rich(s, 7.8, 2.7, 4.6, 2.6,
         [t["cause_other"], t["cause_desc"], t["cause_precise"]],
         size=16, color=INK, line=1.4)
    note(s, t["cause_note"])
    footer(s, 11)

    # 12 ─ итог
    s = slide()
    ghost(s, 9)
    eyebrow(s, f"{t['step_prefix']} 09", spacing=80)
    headline(s, t["sum_title"])
    y = 2.7
    for label, value in SUMMARY_FIELDS:
        text(s, LM, y, 1.8, 0.3, label, size=13, color=MUTED)
        text(s, LM + 2.0, y - 0.04, 4.6, 0.35, value, size=17, color=INK)
        y += 0.5
    text(s, LM, 5.3, 6.5, 0.3, "Confirm  ·  Edit selection  ·  Cancel",
         size=18, color=ACCENT, bold=True)
    sum_parts = []
    for label, suffix in t["sum_btns"]:
        sum_parts += [(label, True), (suffix, False)]
    rich(s, LM, 5.85, 6.6, 1.0, [sum_parts], size=13, color=MUTED, line=1.3)
    text(s, 7.9, 2.55, 4.5, 0.3, t["sum_after"].upper(), size=11, color=MUTED, spacing=180)
    rich(s, 7.9, 2.9, 4.5, 2.2, t["sum_after_lines"], size=16, color=INK, line=1.45)
    note(s, t["sum_note"], y=6.4)
    footer(s, 12)

    # 13 ─ результат
    s = slide()
    eyebrow(s, t["res_eyebrow"])
    headline(s, t["res_title"])
    sub(s, t["res_sub"], y=2.5)
    y = 3.15
    for label, value in t["res_fields"]:
        text(s, LM, y, 2.4, 0.3, label, size=13, color=MUTED)
        text(s, LM + 2.6, y - 0.06, 5.6, 0.4, value, size=16, color=INK)
        haircut(s, y + 0.42, w=7.9)
        y += 0.6
    text(s, 9.0, 3.15, 3.4, 0.3, t["res_auto"].upper(), size=11, color=MUTED, spacing=180)
    rich(s, 9.0, 3.5, 3.4, 2.4, t["res_auto_lines"], size=14, color=INK, line=1.5)
    footer(s, 13)

    # 14 ─ частые ошибки
    s = slide()
    eyebrow(s, t["err_eyebrow"])
    headline(s, t["err_title"])
    y = 2.45
    for label, value in t["err_rows"]:
        text(s, LM, y, 5.0, 0.6, label, size=16, color=INK, line=1.15)
        text(s, LM + 5.2, y, CW - 5.2, 0.6, value, size=16, color=MUTED, line=1.15)
        haircut(s, y + 0.58)
        y += 0.82
    note(s, t["err_note"], y=6.55)
    footer(s, 14)

    # 15 ─ шпаргалка
    s = slide(BLACK)
    text(s, LM, 0.55, CW, 0.3, t["cheat_eyebrow"].upper(), size=11,
         color=RGBColor(0x86, 0x86, 0x8B), spacing=260)
    text(s, LM, 0.95, CW, 0.9, t["cheat_title"], size=44, color=WHITE, bold=True)
    y = 2.35
    for i, item in enumerate(t["cheat_items"], 1):
        text(s, LM, y, 0.7, 0.3, f"{i:02d}", size=12, color=RGBColor(0x0A, 0x84, 0xFF), bold=True)
        text(s, LM + 0.9, y - 0.05, CW - 0.9, 0.45, item, size=17, color=WHITE, line=1.1)
        if i < len(t["cheat_items"]):
            haircut(s, y + 0.55, color=DARK_LINE)
        y += 0.62
    text(s, LM, 6.75, CW, 0.3, t["cheat_note"], size=13, color=RGBColor(0x86, 0x86, 0x8B))
    footer(s, 15)

    prs.save(out_path)
    return len(prs.slides)


if __name__ == "__main__":
    total = 0
    for lang in ("ru", "en", "zh"):
        path = f"/Users/nookon/Desktop/LarkBot/TELEGRAM-BOT-GUIDE-{lang.upper()}.pptx"
        total += build(lang, path)
        print("saved", lang, path)
    print("slides total", total)
