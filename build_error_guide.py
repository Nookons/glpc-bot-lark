#!/usr/bin/env python3
"""Гайд «Как записать ошибку через бота» в стиле Apple minimalism.

Принципы: белый фон, много воздуха, крупная типографика Helvetica Neue,
тонкие линии вместо рамок и заливок, один акцентный цвет (синий Apple).
Никаких карточек, теней и цветных плашек.
"""

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
SOFT = RGBColor(0xF5, 0xF5, 0xF7)
DARK_LINE = RGBColor(0x33, 0x33, 0x36)

FONT = "Helvetica Neue"
W, H = 13.333, 7.5
LM = 0.9
CW = W - 2 * LM

prs = Presentation()
prs.slide_width = Inches(W)
prs.slide_height = Inches(H)
BLANK = prs.slide_layouts[6]


def _flatten(shape):
    """Убрать ссылку на тему: иначе LibreOffice рисует тень у тонких линий."""
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
    _flatten(rect)
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
    """lines: строки или списки пар (текст, bold)."""
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


def eyebrow(s, value, color=MUTED, y=0.55):
    text(s, LM, y, CW, 0.3, value.upper(), size=10.5, color=color, spacing=220)


def headline(s, value, y=0.9, size=44, color=INK, w=None, line=1.05):
    text(s, LM, y, w or CW, 1.6, value, size=size, color=color, bold=True, line=line)


def sub(s, value, y=1.85, color=MUTED, size=18):
    text(s, LM, y, CW, 0.6, value, size=size, color=color, line=1.25)


def hairline(s, y, x=LM, w=CW, color=LINE, thickness=0.008):
    rect = s.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(x), Inches(y), Inches(w), Inches(thickness))
    rect.fill.solid()
    rect.fill.fore_color.rgb = color
    rect.line.fill.background()
    rect.shadow.inherit = False
    _flatten(rect)


def vline(s, x, y, h, color=LINE, thickness=0.008):
    rect = s.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(x), Inches(y),
                              Inches(thickness), Inches(h))
    rect.fill.solid()
    rect.fill.fore_color.rgb = color
    rect.line.fill.background()
    rect.shadow.inherit = False
    _flatten(rect)


def footer(s, n, dark=False):
    color = RGBColor(0x86, 0x86, 0x8B) if dark else MUTED
    text(s, LM, 7.08, 7.0, 0.25, "LarkBot", size=10, color=color, spacing=60)
    text(s, W - LM - 0.6, 7.08, 0.6, 0.25, f"{n:02d}", size=10, color=color, align=PP_ALIGN.RIGHT)


def ghost_number(s, n):
    text(s, W - LM - 1.6, 0.38, 1.6, 1.0, f"{n:02d}", size=56, color=LINE, bold=True,
         align=PP_ALIGN.RIGHT, line=1.0)


def note(s, value, y=6.35, size=14, color=MUTED):
    text(s, LM, y, CW, 0.5, value, size=size, color=color, line=1.3)


# ── 01. Титул ────────────────────────────────────────────────────────────
s = slide(BLACK)
text(s, LM, 0.7, CW, 0.3, "LARKBOT", size=11, color=RGBColor(0x86, 0x86, 0x8B), spacing=260)
text(s, LM, 1.95, 11.2, 2.4, "Как записать\nошибку через бота", size=62, color=WHITE,
     bold=True, line=1.02)
text(s, LM, 4.75, CW, 0.4, "Инструкция для оператора", size=20,
     color=RGBColor(0xA1, 0xA1, 0xA6))
hairline(s, 5.85, color=DARK_LINE)
text(s, LM, 6.15, 6.0, 0.3, "GLP-C  ·  GLP-C1  ·  SMALL-P3", size=13,
     color=RGBColor(0x86, 0x86, 0x8B))
text(s, W - LM - 2.0, 6.15, 2.0, 0.3, "07.11.2026", size=13,
     color=RGBColor(0x86, 0x86, 0x8B), align=PP_ALIGN.RIGHT)
footer(s, 1, dark=True)

# ── 02. Маршрут ──────────────────────────────────────────────────────────
s = slide()
eyebrow(s, "Маршрут")
headline(s, "Восемь шагов.\nОдна минута.")
sub(s, "Бот спрашивает по одному вопросу — ошибиться почти негде.", y=2.45)

steps = [
    ("01", "Фото", "в топик ошибок"),
    ("02", "Что на фото", "Robot / Charging / Workstation"),
    ("03", "Номер", "3780 · 137 · C2-H146"),
    ("04", "Модель", "обычно сама"),
    ("05", "Модуль", "где сломалось"),
    ("06", "Ошибка", "что случилось"),
    ("07", "Причина", "почему"),
    ("08", "Confirm", "отправить"),
]
for i, (num, name, descr) in enumerate(steps):
    row, col = divmod(i, 4)
    x = LM + col * (CW / 4)
    y = 3.45 + row * 1.55
    text(s, x, y, 0.45, 0.3, num, size=11, color=ACCENT, bold=True, spacing=60)
    text(s, x, y + 0.32, CW / 4 - 0.25, 0.4, name, size=20, color=INK, bold=True)
    text(s, x, y + 0.78, CW / 4 - 0.25, 0.5, descr, size=12, color=MUTED, line=1.2)
hairline(s, 4.98, color=LINE)
note(s, "Модель бот обычно подставляет сам из справочника — шаг 04 вы не увидите.", y=6.45)
footer(s, 2)

# ── 03. /reg ─────────────────────────────────────────────────────────────
s = slide()
eyebrow(s, "Перед началом")
headline(s, "Привяжите Telegram")
sub(s, "Один раз, до первого отчёта.")
text(s, LM, 2.85, CW, 0.8, "/reg Ivan Petrenko", size=40, color=INK, bold=True)
hairline(s, 3.85)
rich(s, LM, 4.1, 7.6, 1.2, [
    [("Имя — как в Lark.", True)],
    "Проверить привязку: /whoami",
], size=18, color=INK)
note(s, "Без привязки отчёт сохранится, но в поле «кто сообщил» будет пусто —\n"
         "сейчас так приходят 11 % отчётов.", y=5.75, size=15)
footer(s, 3)

# ── 04. Шаг 1 — фото ─────────────────────────────────────────────────────
s = slide()
ghost_number(s, 1)
eyebrow(s, "Шаг 01")
headline(s, "Фото")
sub(s, "Одно фото — один отчёт.")
text(s, LM, 2.55, 4.9, 0.3, "Что делать", size=11, color=MUTED, spacing=180)
rich(s, LM, 2.95, 4.9, 2.6, [
    "Прикрепите одно фото к сообщению в топике ошибок.",
    "Подпись не нужна — бот спросит всё сам.",
    "Именно фото, до 25 МБ.",
], size=17, color=INK, line=1.4)
vline(s, 6.35, 2.75, 2.9)
text(s, 6.85, 2.55, 5.4, 0.3, "Что будет", size=11, color=MUTED, spacing=180)
rich(s, 6.85, 2.95, 5.4, 2.6, [
    "Бот удалит ваше сообщение — тема не засоряется.",
    "Пришлёт то же фото с вопросом и кнопками.",
    "Иногда сам узнает оборудование и пропустит первый вопрос.",
], size=17, color=INK, line=1.4)
note(s, "Зачем — фото уходит в Lark-карточку и в журнал: по нему разбирают неисправность.")
footer(s, 4)

# ── 05. Шаг 2 — категория ────────────────────────────────────────────────
s = slide()
ghost_number(s, 2)
eyebrow(s, "Шаг 02")
headline(s, "Что на фото?")
sub(s, "Одна из трёх кнопок.")
cols = [
    ("Workstation", "Рабочее место,\nконвейер, станция отбора", "Не крутится лента,\nзастрял товар"),
    ("Charging station", "Сама зарядная станция,\nне робот на ней", "Не заряжает,\nперезагружается"),
    ("Robot", "Робот: K50H, A42T,\nA42T C2", "Не едет, не поднимает,\nаварийная кнопка"),
]
for i, (name, what, when) in enumerate(cols):
    x = LM + i * (CW / 3)
    hairline(s, 2.6, x=x, w=CW / 3 - 0.35, color=INK)
    text(s, x, 2.8, CW / 3 - 0.35, 0.4, name, size=22, color=INK, bold=True)
    text(s, x, 3.35, CW / 3 - 0.35, 0.9, what, size=14, color=MUTED, line=1.3)
    text(s, x, 4.55, CW / 3 - 0.35, 0.25, "Когда", size=10, color=MUTED, spacing=160)
    text(s, x, 4.85, CW / 3 - 0.35, 0.9, when, size=14, color=INK, line=1.3)
note(s, "QR-код пола: отдельной кнопки нет — сообщайте через Robot → QR Code.")
footer(s, 5)

# ── 06. Шаг 3 — номер ────────────────────────────────────────────────────
s = slide()
ghost_number(s, 3)
eyebrow(s, "Шаг 03")
headline(s, "Номер")
sub(s, "Одним сообщением. Только номер.")
rows = [
    ("Robot", "3780 или H108 — цифры или H + цифры"),
    ("Charging station", "137 — номер станции, только цифры"),
    ("Workstation", "C2-H146 — код с наклейки"),
]
y = 2.65
for label, value in rows:
    text(s, LM, y, 3.6, 0.35, label, size=14, color=MUTED)
    text(s, LM + 3.8, y - 0.04, CW - 3.8, 0.4, value, size=19, color=INK)
    hairline(s, y + 0.55)
    y += 0.95
rich(s, LM, 5.55, CW, 0.8, [
    [("Несколько устройств — через слэш: ", False), ("3545/314/141", True),
     (" → три отдельные записи.", False)],
], size=17, color=INK)
note(s, "Зачем — номер связывает запись с карточкой, складом и зоной.", y=6.35)
footer(s, 6)

# ── 07. Шаг 3 — проверка ─────────────────────────────────────────────────
s = slide()
ghost_number(s, 3)
eyebrow(s, "Шаг 03 · проверка")
headline(s, "Если номера нет\nв справочнике")
sub(s, "Формат — жёстко. Справочник — мягко.", y=2.5)
text(s, LM, 3.25, CW, 0.9, "Not in the registry: Robot 9999.\nSave anyway? The entry will be marked for review.",
     size=20, color=MUTED, line=1.45, italic=True)
hairline(s, 4.6)
rich(s, LM, 4.85, CW, 1.2, [
    [("Confirm", True), (" — сохранить и пометить «на разбор».", False)],
    [("Edit number", True), (" — вернуться и исправить.", False)],
], size=18, color=INK, line=1.5)
note(s, "Если ввести описание вместо номера — бот попросит номер заново.", y=6.3)
footer(s, 7)

# ── 08. Шаг 4 — модель ───────────────────────────────────────────────────
s = slide()
ghost_number(s, 4)
eyebrow(s, "Шаг 04")
headline(s, "Модель")
text(s, LM, 2.0, CW, 0.6, "Обычно этого шага нет.", size=26, color=INK, bold=True)
sub(s, "Бот берёт модель из справочника по номеру.", y=2.65)
models = [("K50H", "мини-робот\n(HAIFLEX)"), ("A42T", "большой робот"), ("A42T C2", "версия C2,\nесть Hook")]
for i, (name, descr) in enumerate(models):
    x = LM + i * (CW / 3)
    hairline(s, 3.55, x=x, w=CW / 3 - 0.35, color=INK)
    text(s, x, 3.75, CW / 3 - 0.35, 0.4, name, size=22, color=INK, bold=True)
    text(s, x, 4.3, CW / 3 - 0.35, 0.9, descr, size=14, color=MUTED, line=1.3)
note(s, "Вопрос появится, только если номера нет в реестре или в строке разные модели.", y=5.6)
footer(s, 8)

# ── 09. Шаг 5 — модуль ───────────────────────────────────────────────────
s = slide()
ghost_number(s, 5)
eyebrow(s, "Шаг 05")
headline(s, "Модуль")
sub(s, "Где именно сломалось.")
rows = [
    ("K50H", "Chassis · QR Code · Safety · Lifting · Network · Other · Program Bug"),
    ("A42T", "Chassis · Lifting · Rotation · Tray · Horn · QR Code · Safety · Fingers · Network"),
    ("A42T C2", "Chassis · Lifting · Rotation · Tray · Horn · Hook · Safety · QR Code"),
]
y = 2.6
for label, value in rows:
    text(s, LM, y, 2.0, 0.35, label, size=14, color=MUTED)
    text(s, LM + 2.2, y - 0.06, CW - 2.2, 0.9, value, size=16, color=INK, line=1.25)
    hairline(s, y + 0.85)
    y += 1.2
note(s, "Модуль — «где». Ошибка — «что».", y=6.3)
footer(s, 9)

# ── 10. Шаг 6 — ошибка ───────────────────────────────────────────────────
s = slide()
ghost_number(s, 6)
eyebrow(s, "Шаг 06")
headline(s, "Ошибка")
sub(s, "Выбирайте то, что видите. Тексты дословные.")
rows = [
    ("Chassis", "Went off the road · Robot's collision · Stop · Unable to rotate"),
    ("QR Code / Lifting", "QR Code dirty · Mechanical · Can't lift up/down the fork · Lost container"),
    ("Safety", "Front emergency button is trigger · Rear emergency button is trigger · "
               "Safety bumper trigger · Safety system disconnect"),
    ("Rotation / Tray / Horn / Hook", "Can't rotate fork · Can't extend/expand the tray · "
                                      "Can't extend/expand the HORN · Can't extend/expand the Hook"),
]
y = 2.55
for label, value in rows:
    text(s, LM, y, 3.4, 0.3, label, size=13, color=MUTED)
    text(s, LM + 3.6, y - 0.08, CW - 3.6, 0.9, value, size=15, color=INK, line=1.25)
    hairline(s, y + 0.8)
    y += 1.05
footer(s, 10)

# ── 11. Шаги 7–8 — причина ───────────────────────────────────────────────
s = slide()
ghost_number(s, 7)
eyebrow(s, "Шаги 07–08")
headline(s, "Причина")
sub(s, "Почему это произошло.")
rich(s, LM, 2.7, 6.3, 2.6, [
    [("Chassis ", True), ("Obstacle · Collision · Mechanical · Software fault", False)],
    [("Safety ", True), ("Human error · Mechanical wear · Safety Vest", False)],
    [("Charging ", True), ("Power or cable · Station fault · Battery fault", False)],
], size=16, color=INK, line=1.4)
vline(s, 7.35, 2.7, 2.6)
rich(s, 7.8, 2.7, 4.6, 2.6, [
    [("Нет подходящей — ", False), ("Other", True), (".", False)],
    "Тогда бот попросит описать словами одним сообщением.",
    "Чем точнее текст, тем полезнее разбор.",
], size=16, color=INK, line=1.4)
note(s, "Ошибка — «что». Причина — «почему». Это две разные метрики.")
footer(s, 11)

# ── 12. Шаг 9 — итог ─────────────────────────────────────────────────────
s = slide()
ghost_number(s, 9)
eyebrow(s, "Шаг 09")
headline(s, "Проверьте\nи подтвердите")
fields = [("Object", "Robot"), ("Device", "8066"), ("Module", "Chassis"),
          ("Error", "Went off the road"), ("Cause", "Mechanical")]
y = 2.7
for label, value in fields:
    text(s, LM, y, 1.8, 0.3, label, size=13, color=MUTED)
    text(s, LM + 2.0, y - 0.04, 4.6, 0.35, value, size=17, color=INK)
    y += 0.5
text(s, LM, 5.3, 6.5, 0.3, "Confirm  ·  Edit selection  ·  Cancel",
     size=18, color=ACCENT, bold=True)
rich(s, LM, 5.85, 6.6, 1.0, [
    [("Confirm", True), (" — отправить. ", False),
     ("Edit selection", True), (" — к номеру. ", False),
     ("Cancel", True), (" — отменить.", False)],
], size=13, color=MUTED, line=1.3)
text(s, 7.9, 2.55, 4.5, 0.3, "После Confirm", size=11, color=MUTED, spacing=180)
rich(s, 7.9, 2.9, 4.5, 2.2, [
    "Saving…",
    "Error report saved",
    "Журнал · Supabase · Lark",
    "Неизвестный номер — в Needs review",
], size=16, color=INK, line=1.45)
note(s, "Проверка перед записью: исправить здесь дешевле, чем искать в журнале.", y=6.4)
footer(s, 12)

# ── 13. Что в журнале ────────────────────────────────────────────────────
s = slide()
eyebrow(s, "Результат")
headline(s, "Одна запись.\nВсё внутри.")
sub(s, "Пример: запись 445928 от 06.11.2026.", y=2.5)
rows = [
    ("Склад", "GLP-C"),
    ("Номер", "8066"),
    ("Модуль", "Chassis"),
    ("Ошибка", "Went off the road"),
    ("Причина", "Mechanical"),
    ("Описание", "The robot left its route — текст из шаблона"),
]
y = 3.15
for label, value in rows:
    text(s, LM, y, 2.4, 0.3, label, size=13, color=MUTED)
    text(s, LM + 2.6, y - 0.06, 5.6, 0.4, value, size=16, color=INK)
    hairline(s, y + 0.42, w=7.9)
    y += 0.6
text(s, 9.0, 3.15, 3.4, 0.3, "Автоматически", size=11, color=MUTED, spacing=180)
rich(s, 9.0, 3.5, 3.4, 2.4, [
    "склад — по номеру",
    "зона — по устройству",
    "смена — по времени",
    "автор — из /reg",
    "фото — из сообщения",
], size=14, color=INK, line=1.5)
footer(s, 13)

# ── 14. Частые ошибки ────────────────────────────────────────────────────
s = slide()
eyebrow(s, "Если что-то не так")
headline(s, "Частые ошибки")
rows = [
    ("Пусто «кто сообщил»", "Сделайте /reg и отправьте отчёт заново"),
    ("«Please try again»", "В номере текст — введите только номер"),
    ("«Not in the registry»", "Edit number — опечатка · Confirm — новый"),
    ("Бот молчит на фото", "Это не фото или чужой топик"),
    ("Edit selection вернул к номеру", "Так и задумано — пройдите ветку заново"),
]
y = 2.45
for label, value in rows:
    text(s, LM, y, 5.0, 0.3, label, size=16, color=INK)
    text(s, LM + 5.2, y, CW - 5.2, 0.3, value, size=16, color=MUTED)
    hairline(s, y + 0.5)
    y += 0.78
note(s, "Почти все сбои — номер не в том формате или отчёт не в том топике.", y=6.5)
footer(s, 14)

# ── 15. Шпаргалка ────────────────────────────────────────────────────────
s = slide(BLACK)
text(s, LM, 0.55, CW, 0.3, "ШПАРГАЛКА", size=11, color=RGBColor(0x86, 0x86, 0x8B), spacing=260)
text(s, LM, 0.95, CW, 0.9, "Вся инструкция", size=44, color=WHITE, bold=True)
items = [
    "/reg Имя Фамилия — один раз",
    "Фото — в топик ошибок",
    "Workstation / Charging station / Robot",
    "Номер — 3780 · 137 · C2-H146 · несколько через /",
    "Модуль → Ошибка → Причина",
    "Other — опишите словами",
    "Confirm — отправить",
]
y = 2.35
for i, item in enumerate(items, 1):
    text(s, LM, y, 0.7, 0.3, f"{i:02d}", size=12, color=RGBColor(0x0A, 0x84, 0xFF), bold=True)
    text(s, LM + 0.9, y - 0.05, CW - 0.9, 0.4, item, size=18, color=WHITE)
    if i < len(items):
        hairline(s, y + 0.55, color=DARK_LINE)
    y += 0.62
text(s, LM, 6.75, CW, 0.3, "Сохраните эту страницу — остальное бот подскажет сам.",
     size=13, color=RGBColor(0x86, 0x86, 0x8B))
footer(s, 15, dark=True)

prs.save("/Users/nookon/Desktop/LarkBot/GUIDE-ERROR-REPORT.pptx")
print("saved", len(prs.slides), "slides")
