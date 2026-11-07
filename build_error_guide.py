#!/usr/bin/env python3
"""Сборка гайда «Как записать ошибку через бота» — PPTX для работников.

Один источник — `GUIDE-ERROR-REPORT.md`; здесь та же информация, но
разложенная по слайдам: крупный текст, минимум на слайде, каждый шаг —
«что делать / зачем / что будет».
"""

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.util import Inches, Pt

# ── Палитра ──────────────────────────────────────────────────────────────
NAVY = RGBColor(0x0F, 0x17, 0x2A)
NAVY_SOFT = RGBColor(0x1E, 0x29, 0x3B)
TEAL = RGBColor(0x0F, 0x76, 0x6E)
TEAL_SOFT = RGBColor(0xE6, 0xF4, 0xF1)
AMBER = RGBColor(0xB4, 0x53, 0x09)
AMBER_SOFT = RGBColor(0xFE, 0xF3, 0xC7)
GRAY = RGBColor(0xF1, 0xF5, 0xF9)
GRAY_LINE = RGBColor(0xE2, 0xE8, 0xF0)
TEXT = RGBColor(0x0F, 0x17, 0x2A)
MUTED = RGBColor(0x47, 0x55, 0x69)
WHITE = RGBColor(0xFF, 0xFF, 0xFF)
RED = RGBColor(0xB9, 0x1C, 0x1C)

FONT = "Arial"
W, H = 13.333, 7.5

prs = Presentation()
prs.slide_width = Inches(W)
prs.slide_height = Inches(H)
BLANK = prs.slide_layouts[6]


# ── Помощники ────────────────────────────────────────────────────────────
def slide(fill=WHITE):
    s = prs.slides.add_slide(BLANK)
    bg = s.shapes.add_shape(MSO_SHAPE.RECTANGLE, 0, 0, prs.slide_width, prs.slide_height)
    bg.fill.solid()
    bg.fill.fore_color.rgb = fill
    bg.line.fill.background()
    bg.shadow.inherit = False
    return s


def box(s, x, y, w, h, fill=GRAY, line=None, radius=True, line_w=1.0):
    shape = s.shapes.add_shape(
        MSO_SHAPE.ROUNDED_RECTANGLE if radius else MSO_SHAPE.RECTANGLE,
        Inches(x), Inches(y), Inches(w), Inches(h),
    )
    shape.fill.solid()
    shape.fill.fore_color.rgb = fill
    if line is None:
        shape.line.fill.background()
    else:
        shape.line.color.rgb = line
        shape.line.width = Pt(line_w)
    shape.shadow.inherit = False
    if radius:
        try:
            shape.adjustments[0] = 0.08
        except Exception:
            pass
    return shape


def text(s, x, y, w, h, value, size=14, bold=False, color=TEXT,
         align=PP_ALIGN.LEFT, anchor=MSO_ANCHOR.TOP, spacing=1.0, italic=False):
    tb = s.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    tf = tb.text_frame
    tf.word_wrap = True
    tf.margin_left = tf.margin_right = Inches(0.02)
    tf.margin_top = tf.margin_bottom = Inches(0.01)
    tf.vertical_anchor = anchor
    p = tf.paragraphs[0]
    p.alignment = align
    p.line_spacing = spacing
    run = p.add_run()
    run.text = value
    run.font.name = FONT
    run.font.size = Pt(size)
    run.font.bold = bold
    run.font.italic = italic
    run.font.color.rgb = color
    return tb


def rich(s, x, y, w, h, lines, size=14, color=TEXT, spacing=1.15):
    """lines: список абзацев; абзац — список (текст, bold) или строка."""
    tb = s.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    tf = tb.text_frame
    tf.word_wrap = True
    tf.margin_left = tf.margin_right = Inches(0.02)
    tf.margin_top = tf.margin_bottom = Inches(0.01)
    first = True
    for line in lines:
        p = tf.paragraphs[0] if first else tf.add_paragraph()
        first = False
        p.line_spacing = spacing
        p.space_after = Pt(4)
        parts = line if isinstance(line, list) else [(line, False)]
        for chunk, bold in parts:
            r = p.add_run()
            r.text = chunk
            r.font.name = FONT
            r.font.size = Pt(size)
            r.font.bold = bold
            r.font.color.rgb = color
    return tb


def header(s, kicker, title, sub=None):
    if kicker:
        pill = box(s, 0.7, 0.42, 0.12, 0.42, fill=TEAL, radius=False)
        text(s, 0.95, 0.40, 6.0, 0.45, kicker, size=12, bold=True, color=TEAL)
    text(s, 0.7, 0.82, 11.9, 0.75, title, size=32, bold=True, color=TEXT)
    bar = s.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(0.7), Inches(1.56), Inches(1.3), Inches(0.045))
    bar.fill.solid()
    bar.fill.fore_color.rgb = TEAL
    bar.line.fill.background()
    bar.shadow.inherit = False
    if sub:
        text(s, 0.7, 1.62, 11.9, 0.4, sub, size=14, color=MUTED)


def footer(s, n):
    text(s, 0.7, 7.08, 7.0, 0.3, "LarkBot · инструкция для оператора · 07.11.2026",
         size=9, color=MUTED)
    text(s, 12.2, 7.08, 0.45, 0.3, str(n), size=9, color=MUTED, align=PP_ALIGN.RIGHT)


def why(s, value, y=6.15, h=0.72, x=0.7, w=11.93):
    box(s, x, y, w, h, fill=AMBER_SOFT)
    bar = s.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(x), Inches(y), Inches(0.08), Inches(h))
    bar.fill.solid()
    bar.fill.fore_color.rgb = AMBER
    bar.line.fill.background()
    bar.shadow.inherit = False
    rich(s, x + 0.22, y + 0.08, w - 0.35, h - 0.16,
         [[("ЗАЧЕМ:  ", True), (value, False)]], size=13, color=RGBColor(0x78, 0x3C, 0x08))


def note(s, value, y=1.72, x=0.7, w=11.9, color=MUTED, size=13):
    text(s, x, y, w, 0.35, value, size=size, color=color, italic=False)


def table(s, rows, x, y, w, col_widths, row_h=0.5, font=12, header_fill=NAVY,
          zebra=GRAY, header_font=WHITE, align_first_left=True):
    shape = s.shapes.add_table(len(rows), len(rows[0]), Inches(x), Inches(y),
                               Inches(w), Inches(row_h * len(rows)))
    tbl = shape.table
    for i, width in enumerate(col_widths):
        tbl.columns[i].width = Inches(width)
    for r, row in enumerate(rows):
        tbl.rows[r].height = Inches(row_h)
        for c, value in enumerate(row):
            cell = tbl.cell(r, c)
            cell.margin_left = cell.margin_right = Inches(0.10)
            cell.margin_top = cell.margin_bottom = Inches(0.04)
            cell.vertical_anchor = MSO_ANCHOR.MIDDLE
            cell.fill.solid()
            if r == 0:
                cell.fill.fore_color.rgb = header_fill
            else:
                cell.fill.fore_color.rgb = WHITE if r % 2 else zebra
            tf = cell.text_frame
            tf.word_wrap = True
            p = tf.paragraphs[0]
            p.alignment = PP_ALIGN.LEFT if (c == 0 or align_first_left) else PP_ALIGN.LEFT
            run = p.add_run()
            run.text = str(value)
            run.font.name = FONT
            run.font.size = Pt(font)
            run.font.bold = r == 0
            run.font.color.rgb = header_font if r == 0 else TEXT
    return tbl



def rows_list(s, rows, x, y, w, col_w, row_h=0.6, gap=0.05, font=12, head=None,
              head_h=0.42):
    """Строки фиксированной высоты — вместо таблицы.

    Таблица в PowerPoint расширяет строки под текст (LibreOffice — заметно),
    и предсказать, где она закончится, нельзя. Здесь высота задана, поэтому
    подпись и «зачем» не наезжают друг на друга.
    """
    cy = y
    if head:
        box(s, x, cy, w, head_h, fill=NAVY, radius=False)
        text(s, x + 0.15, cy + 0.07, col_w[0] - 0.2, head_h - 0.1, head[0],
             size=10.5, bold=True, color=WHITE)
        text(s, x + col_w[0] + 0.15, cy + 0.07, w - col_w[0] - 0.2, head_h - 0.1,
             head[1], size=10.5, bold=True, color=WHITE)
        cy += head_h + gap
    for i, (label, value) in enumerate(rows):
        box(s, x, cy, w, row_h, fill=WHITE if i % 2 == 0 else GRAY, radius=False)
        text(s, x + 0.15, cy + 0.09, col_w[0] - 0.2, row_h - 0.1, label,
             size=font, bold=True, color=TEXT)
        text(s, x + col_w[0] + 0.15, cy + 0.09, w - col_w[0] - 0.2, row_h - 0.1,
             value, size=font, color=TEXT)
        cy += row_h + gap
    return cy


def numbered(s, items, x=0.7, y=1.95, w=11.9, size=16, gap=0.62):
    for i, item in enumerate(items, 1):
        cy = y + (i - 1) * gap
        circ = s.shapes.add_shape(MSO_SHAPE.OVAL, Inches(x), Inches(cy - 0.02),
                                  Inches(0.42), Inches(0.42))
        circ.fill.solid()
        circ.fill.fore_color.rgb = TEAL
        circ.line.fill.background()
        circ.shadow.inherit = False
        tf = circ.text_frame
        tf.margin_left = tf.margin_right = 0
        p = tf.paragraphs[0]
        p.alignment = PP_ALIGN.CENTER
        r = p.add_run()
        r.text = str(i)
        r.font.name = FONT
        r.font.size = Pt(13)
        r.font.bold = True
        r.font.color.rgb = WHITE
        parts = item if isinstance(item, list) else [(item, False)]
        rich(s, x + 0.6, cy - 0.03, w - 0.6, 0.55, [parts], size=size)


# ══════════════════════════════════════════════════════════════════════════
# 1. Титул
# ══════════════════════════════════════════════════════════════════════════
s = slide(NAVY)
# декоративные полосы
for i, (yy, ww, col) in enumerate([(0.0, 13.333, TEAL), (0.12, 4.2, AMBER)]):
    bar = s.shapes.add_shape(MSO_SHAPE.RECTANGLE, 0, Inches(yy), Inches(ww), Inches(0.12))
    bar.fill.solid()
    bar.fill.fore_color.rgb = col
    bar.line.fill.background()
    bar.shadow.inherit = False

text(s, 0.9, 1.15, 11.5, 0.4, "ИНСТРУКЦИЯ ДЛЯ ОПЕРАТОРА", size=14, bold=True,
     color=RGBColor(0x5E, 0xEA, 0xD4))
text(s, 0.9, 1.75, 11.6, 1.9, "Как записать\nошибку через бота", size=46, bold=True,
     color=WHITE, spacing=1.02)
text(s, 0.9, 3.95, 11.6, 0.5, "Telegram-бот @tk_servie_bot  ·  GLP-C  ·  GLP-C1  ·  SMALL-P3",
     size=16, color=RGBColor(0xCB, 0xD5, 0xE1))
line = s.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(0.9), Inches(4.75), Inches(2.2), Inches(0.05))
line.fill.solid()
line.fill.fore_color.rgb = AMBER
line.line.fill.background()
line.shadow.inherit = False
text(s, 0.9, 5.0, 11.5, 0.4, "Один отчёт = одно фото + несколько кнопок", size=18, bold=True, color=WHITE)
text(s, 0.9, 5.55, 11.5, 0.9,
     "Бот спрашивает по шагам: что на фото, номер, модуль, ошибка и причина.\n"
     "Ответы сохраняются в журнал и уходят карточкой в Lark.",
     size=14, color=RGBColor(0x94, 0xA3, 0xB8), spacing=1.25)
text(s, 0.9, 6.95, 6.0, 0.3, "7 ноября 2026", size=11, color=RGBColor(0x64, 0x74, 0x8B))

# ══════════════════════════════════════════════════════════════════════════
# 2. Маршрут
# ══════════════════════════════════════════════════════════════════════════
s = slide()
header(s, "ОБЩАЯ КАРТИНА", "Маршрут отчёта: 8 шагов",
       "Каждый шаг уточняет, что случилось и с каким оборудованием.")
steps = [
    ("Фото", "в топик\nошибок"),
    ("Что на\nфото", "Robot /\nCharging /\nWorkstation"),
    ("Номер", "3780\nили 137"),
    ("Модель", "обычно\nсама"),
    ("Модуль", "где\nсломалось"),
    ("Ошибка", "что\nслучилось"),
    ("Причина", "почему"),
    ("Итог", "Confirm"),
]
bx, by, bw, bh, gap = 0.7, 2.15, 1.32, 1.55, 0.17
for i, (title_, sub) in enumerate(steps):
    x = bx + i * (bw + gap)
    fill = TEAL if i in (0, 7) else GRAY
    col = WHITE if i in (0, 7) else TEXT
    box(s, x, by, bw, bh, fill=fill, line=None if i in (0, 7) else GRAY_LINE)
    text(s, x + 0.08, by + 0.18, bw - 0.16, 0.35, title_, size=13, bold=True, color=col,
         align=PP_ALIGN.CENTER)
    text(s, x + 0.06, by + 0.65, bw - 0.12, 0.8, sub, size=10, color=col, align=PP_ALIGN.CENTER,
         spacing=1.05)
    if i < len(steps) - 1:
        arrow = s.shapes.add_shape(MSO_SHAPE.RIGHT_ARROW, Inches(x + bw + 0.03),
                                    Inches(by + bh / 2 - 0.07), Inches(0.11), Inches(0.14))
        arrow.fill.solid()
        arrow.fill.fore_color.rgb = RGBColor(0x94, 0xA3, 0xB8)
        arrow.line.fill.background()
        arrow.shadow.inherit = False

box(s, 0.7, 4.05, 11.93, 0.9, fill=TEAL_SOFT)
text(s, 0.95, 4.18, 11.4, 0.65,
     "Модель бот обычно подставляет сам из справочника — шаг 4 вы не увидите.",
     size=15, bold=True, color=TEAL)
rich(s, 0.7, 5.2, 11.9, 0.9, [
    [("Сколько это занимает: ", True), ("около 30–60 секунд, если номер под рукой.", False)],
    [("Что нужно от вас: ", True), ("одно фото и номер оборудования. Остальное — кнопки.", False)],
], size=14, color=MUTED)
why(s, "бот ведёт по одному вопросу — так отчёт нельзя заполнить неправильно.", y=6.45, h=0.55)
footer(s, 2)

# ══════════════════════════════════════════════════════════════════════════
# 3. /reg
# ══════════════════════════════════════════════════════════════════════════
s = slide()
header(s, "ПЕРЕД НАЧАЛОМ", "Привяжите Telegram к сотруднику",
       "Один раз, до первого отчёта.")
box(s, 0.7, 2.0, 11.93, 1.15, fill=NAVY)
text(s, 1.0, 2.22, 7.0, 0.7, "/reg Ivan Petrenko", size=28, bold=True,
     color=RGBColor(0x5E, 0xEA, 0xD4))
text(s, 8.2, 2.2, 4.2, 0.8, "имя — как в Lark", size=13, color=RGBColor(0x94, 0xA3, 0xB8),
     align=PP_ALIGN.RIGHT)
rich(s, 0.7, 3.55, 11.9, 1.5, [
    [("Проверить привязку: ", True), ("/whoami", False)],
    [("Зачем: ", True), ("без привязки отчёт сохранится, но в поле «кто сообщил» будет пусто — "
                        "карточка сотрудника не покажет ошибку.", False)],
], size=16, color=TEXT, spacing=1.25)
why(s, "сейчас 11 % отчётов приходят без автора — это те, кто не сделал /reg.", y=5.35, h=0.7)
footer(s, 3)

# ══════════════════════════════════════════════════════════════════════════
# 4. Шаг 1 — фото
# ══════════════════════════════════════════════════════════════════════════
s = slide()
header(s, "ШАГ 1", "Фото неисправности", "Одно фото — один отчёт.")
box(s, 0.7, 2.0, 5.85, 3.6, fill=GRAY)
text(s, 0.95, 2.15, 5.4, 0.4, "Что делать", size=16, bold=True, color=TEAL)
rich(s, 0.95, 2.6, 5.35, 2.8, [
    "• Прикрепите одно фото к сообщению в топике ошибок.",
    "• Подпись не нужна — бот спросит всё сам.",
    "• Фото до 25 МБ, именно фото (не видео и не файл).",
], size=14, color=TEXT, spacing=1.35)
box(s, 6.78, 2.0, 5.85, 3.6, fill=TEAL_SOFT)
text(s, 7.03, 2.15, 5.4, 0.4, "Что будет", size=16, bold=True, color=TEAL)
rich(s, 7.03, 2.6, 5.35, 2.8, [
    "• Бот удалит ваше сообщение — тема не засоряется.",
    "• Пришлёт то же фото с вопросом и кнопками.",
    "• Иногда сам узнает оборудование и пропустит первый вопрос.",
], size=14, color=TEXT, spacing=1.35)
why(s, "фото уходит в Lark-карточку и в журнал — по нему разбирают неисправность.", y=5.85, h=0.7)
footer(s, 4)

# ══════════════════════════════════════════════════════════════════════════
# 5. Шаг 2 — категория
# ══════════════════════════════════════════════════════════════════════════
s = slide()
header(s, "ШАГ 2", "Что на фото?", "Выберите одну из трёх кнопок.")
cards = [
    ("Workstation", "Рабочее место,\nконвейер, станция отбора", "не крутится лента,\nзастрял товар"),
    ("Charging station", "Сама зарядная станция,\nне робот на ней", "не заряжает,\nперезагружается"),
    ("Robot", "Робот:\nK50H, A42T, A42T C2", "не едет, не поднимает,\nаварийная кнопка"),
]
for i, (name, what, when) in enumerate(cards):
    x = 0.7 + i * 4.02
    box(s, x, 2.0, 3.85, 3.15, fill=WHITE, line=GRAY_LINE)
    box(s, x, 2.0, 3.85, 0.12, fill=TEAL, radius=False)
    text(s, x + 0.25, 2.3, 3.35, 0.45, name, size=17, bold=True, color=TEXT)
    text(s, x + 0.25, 2.9, 3.35, 0.9, what, size=12, color=MUTED, spacing=1.15)
    text(s, x + 0.25, 3.5, 3.35, 0.35, "Когда выбирать", size=10, bold=True, color=TEAL)
    text(s, x + 0.25, 3.82, 3.35, 1.0, when, size=12, color=TEXT, spacing=1.15)
why(s, "категория задаёт всю ветку вопросов и статистику — ошибка здесь уведёт отчёт в чужой склад.", y=5.45, h=0.7)
text(s, 0.7, 6.7, 11.9, 0.3,
     "QR-код пола: отдельной кнопки нет — сообщайте через Robot → QR Code.",
     size=11, color=MUTED)
footer(s, 5)

# ══════════════════════════════════════════════════════════════════════════
# 6. Шаг 3 — номер
# ══════════════════════════════════════════════════════════════════════════
s = slide()
header(s, "ШАГ 3", "Номер оборудования", "Вводится текстом, одним сообщением.")
table(s, [
    ["Ветка", "Что ввести", "Пример"],
    ["Robot", "только цифры или H + цифры", "3780   ·   H108"],
    ["Charging station", "номер станции, только цифры", "137"],
    ["Workstation", "код с наклейки", "C2-H146"],
], x=0.7, y=2.0, w=11.93, col_widths=[2.6, 5.1, 4.23], row_h=0.55, font=13)
box(s, 0.7, 4.35, 11.93, 1.15, fill=TEAL_SOFT)
text(s, 0.95, 4.5, 11.4, 0.35, "Несколько устройств — через слэш", size=14, bold=True, color=TEAL)
text(s, 0.95, 4.9, 11.4, 0.45, "3545/314/141  →  три отдельные записи, у каждой свой номер и своя зона",
     size=15, bold=True, color=TEXT)
why(s, "номер — единственная связь с карточкой: по нему бот определяет склад и зону.", y=5.75, h=0.7)
text(s, 0.7, 6.6, 11.9, 0.3, "Для зарядки бот затем спросит номер робота, который стоял на станции.",
     size=11, color=MUTED)
footer(s, 6)

# ══════════════════════════════════════════════════════════════════════════
# 7. Шаг 3 — нет в справочнике
# ══════════════════════════════════════════════════════════════════════════
s = slide()
header(s, "ШАГ 3 · ПРОВЕРКА", "Если номера нет в справочнике",
       "Формат проверяется жёстко, справочник — мягко.")
box(s, 0.7, 2.0, 7.6, 1.75, fill=GRAY, line=GRAY_LINE)
text(s, 0.95, 2.18, 7.1, 0.4, "Not in the registry: Robot 9999.", size=15, bold=True, color=RED)
text(s, 0.95, 2.62, 7.1, 0.7, "Save anyway? The entry will be marked for review.",
     size=14, color=TEXT)
btn1 = box(s, 0.95, 3.2, 1.8, 0.4, fill=TEAL)
text(s, 0.95, 3.25, 1.8, 0.3, "Confirm", size=12, bold=True, color=WHITE, align=PP_ALIGN.CENTER)
btn2 = box(s, 2.9, 3.2, 2.0, 0.4, fill=WHITE, line=GRAY_LINE)
text(s, 2.9, 3.25, 2.0, 0.3, "Edit number", size=12, bold=True, color=TEXT, align=PP_ALIGN.CENTER)
rich(s, 8.6, 2.05, 4.0, 1.9, [
    [("Confirm", True), (" — сохранить и пометить «на разбор».", False)],
    [("Edit number", True), (" — вернуться и исправить.", False)],
], size=13, color=TEXT, spacing=1.25)
why(s, "не терять реальную ошибку из-за опечатки или робота, которого ещё нет в реестре.", y=4.55, h=0.7)
rich(s, 0.7, 5.55, 11.9, 1.2, [
    [("Если ввести описание вместо номера", True), (" — бот ответит «Please try again» и попросит номер заново.", False)],
    [("Если справочник недоступен", True), (" — бот не предупреждает и не тормозит диалог.", False)],
], size=13, color=MUTED, spacing=1.2)
footer(s, 7)

# ══════════════════════════════════════════════════════════════════════════
# 8. Шаг 4 — модель
# ══════════════════════════════════════════════════════════════════════════
s = slide()
header(s, "ШАГ 4", "Модель робота", "Обычно этого шага нет — бот подставляет модель сам.")
box(s, 0.7, 2.0, 11.93, 1.0, fill=TEAL_SOFT)
text(s, 0.95, 2.2, 11.4, 0.6,
     "Бот берёт модель из справочника по номеру. Вопрос появится, только если номера нет в реестре\n"
     "или в строке несколько номеров разных моделей.",
     size=14, color=TEAL, spacing=1.2)
models = [("K50H", "мини-робот\n(HAIFLEX)"), ("A42T", "большой робот"), ("A42T C2", "версия C2,\nесть Hook")]
for i, (name, descr) in enumerate(models):
    x = 0.7 + i * 4.02
    box(s, x, 3.35, 3.85, 1.75, fill=WHITE, line=GRAY_LINE)
    text(s, x + 0.25, 3.55, 3.35, 0.4, name, size=18, bold=True, color=TEXT)
    text(s, x + 0.25, 4.05, 3.35, 0.8, descr, size=12, color=MUTED, spacing=1.15)
why(s, "у моделей разные модули — без модели бот показал бы чужие варианты.", y=5.45, h=0.7)
footer(s, 8)

# ══════════════════════════════════════════════════════════════════════════
# 9. Шаг 5 — модуль
# ══════════════════════════════════════════════════════════════════════════
s = slide()
header(s, "ШАГ 5", "Модуль: где именно сломалось",
       "Список модулей зависит от модели.")
end9 = rows_list(s, [
    ("K50H", "Chassis · QR Code · Safety · Lifting · Network · Other · Program Bug"),
    ("A42T", "Chassis · Lifting · Rotation · Tray · Horn · QR Code · Safety · Fingers · Network"),
    ("A42T C2", "Chassis · Lifting · Rotation · Tray · Horn · Hook · Safety · QR Code"),
], x=0.7, y=2.0, w=11.93, col_w=[2.0], row_h=0.66, gap=0.06, font=11.5,
   head=("Модель", "Модули"))

rich(s, 0.7, end9 + 0.12, 11.9, 0.8, [
    [("Chassis", True), (" — ходовая: не едет, съехал, столкновение   ·   ", False),
     ("Lifting", True), (" — вилы   ·   ", False),
     ("Rotation", True), (" — поворот", False)],
    [("Tray", True), (" — лоток   ·   ", False),
     ("Horn", True), (" — рог   ·   ", False),
     ("Hook", True), (" — крюк   ·   ", False),
     ("Safety", True), (" — аварийные кнопки   ·   ", False),
     ("QR Code", True), (" — код пола", False)],
], size=11.5, color=MUTED, spacing=1.15)
why(s, "модуль отвечает «где», ошибка — «что». От модуля зависит список ошибок и причин.", y=6.15, h=0.6)
footer(s, 9)

# ══════════════════════════════════════════════════════════════════════════
# 10. Шаг 6 — ошибка
# ══════════════════════════════════════════════════════════════════════════
s = slide()
header(s, "ШАГ 6", "Ошибка: что именно случилось",
       "Выбирайте то, что видите. Тексты дословные.")
end10 = rows_list(s, [
    ("Chassis", "Went off the road · Robot's collision · Stop · Unable to rotate"),
    ("QR Code / Lifting", "QR Code dirty · Mechanical · Can't lift up/down the fork · Lost container"),
    ("Safety", "Front emergency button is trigger · Rear emergency button is trigger · "
               "Safety bumper trigger · Safety system disconnect"),
    ("Rotation / Tray / Horn / Hook", "Can't rotate fork · Can't extend/expand the tray · "
                                      "Can't extend/expand the HORN · Can't extend/expand the Hook"),
], x=0.7, y=2.0, w=11.93, col_w=[3.0], row_h=0.58, gap=0.05, font=10.5,
   head=("Модуль", "Ошибки"))

rich(s, 0.7, end10 + 0.1, 11.9, 0.4, [
    [("Не выбирайте «похожее»", True), (" — тип ошибки попадает в статистику и связывается с шаблоном.", False)],
], size=12, color=MUTED)
why(s, "тип ошибки — главное поле статистики: топ ошибок, шаблон решения, сравнение смен.", y=6.15, h=0.6)
footer(s, 10)

# ══════════════════════════════════════════════════════════════════════════
# 11. Шаг 7 + 8 — причина и описание
# ══════════════════════════════════════════════════════════════════════════
s = slide()
header(s, "ШАГИ 7–8", "Причина и описание", "Ошибка — это «что». Причина — «почему».")
box(s, 0.7, 2.0, 5.85, 3.4, fill=GRAY)
text(s, 0.95, 2.15, 5.4, 0.4, "Причина", size=16, bold=True, color=TEAL)
rich(s, 0.95, 2.6, 5.35, 2.6, [
    "• Chassis: Obstacle on the path, Collision, Mechanical, Software fault…",
    "• Safety: Human error, Mechanical wear, Safety Vest…",
    "• Charging: Power or cable fault, Station fault, Battery fault…",
    "• Нет подходящей — выберите Other.",
], size=13, color=TEXT, spacing=1.3)
box(s, 6.78, 2.0, 5.85, 3.4, fill=TEAL_SOFT)
text(s, 7.03, 2.15, 5.4, 0.4, "Описание (только для Other)", size=16, bold=True, color=TEAL)
text(s, 7.03, 2.65, 5.35, 1.0, "Напишите своими словами одним сообщением:", size=13, color=TEXT)
box(s, 7.03, 3.35, 5.3, 0.65, fill=WHITE, line=GRAY_LINE)
text(s, 7.2, 3.48, 5.0, 0.4, "does not lift the forks, red light is blinking",
     size=12, italic=True, color=MUTED)
text(s, 7.03, 4.2, 5.35, 1.0, "Чем точнее текст, тем полезнее разбор и новые причины в справочнике.",
     size=12, color=MUTED)
why(s, "причина показывает, что чинить в процессе (обучение, чистота, износ), а не только узел.", y=5.65, h=0.7)
footer(s, 11)

# ══════════════════════════════════════════════════════════════════════════
# 12. Шаг 9 — итог
# ══════════════════════════════════════════════════════════════════════════
s = slide()
header(s, "ШАГ 9", "Итог и подтверждение", "Последняя проверка перед записью.")
box(s, 0.7, 2.0, 6.1, 3.15, fill=GRAY)
text(s, 0.95, 2.15, 5.6, 0.35, "Inspection result", size=13, bold=True, color=MUTED)
rich(s, 0.95, 2.55, 5.6, 2.1, [
    [("Object: ", True), ("Robot", False)],
    [("Device: ", True), ("8066", False)],
    [("Module: ", True), ("Chassis", False)],
    [("Error: ", True), ("Went off the road", False)],
    [("Cause: ", True), ("Mechanical", False)],
], size=12, color=TEXT, spacing=1.2)
text(s, 0.95, 4.75, 5.6, 0.35, "Check and confirm.", size=12, italic=True, color=MUTED)
for i, (label, fill, col, line) in enumerate([
    ("Edit selection", WHITE, TEXT, GRAY_LINE),
    ("Confirm", TEAL, WHITE, None),
    ("Cancel", WHITE, MUTED, GRAY_LINE),
]):
    bx_ = 0.7 + i * 2.05
    box(s, bx_, 5.3, 1.9, 0.45, fill=fill, line=line)
    text(s, bx_, 5.37, 1.9, 0.3, label, size=11, bold=True, color=col, align=PP_ALIGN.CENTER)
rich(s, 7.1, 2.0, 5.5, 3.3, [
    [("Confirm", True), (" — отправить отчёт.", False)],
    [("Edit selection", True), (" — вернуться к шагу номера (остальное заново).", False)],
    [("Cancel", True), (" — отменить.", False)],
    [("", False)],
    [("После Confirm: ", True), ("«Saving…», затем «Error report saved» и статусы: журнал, Supabase, Lark.", False)],
], size=13, color=TEXT, spacing=1.25)
why(s, "проверка перед записью: ошибку здесь исправить дешевле, чем искать в журнале.", y=5.95, h=0.6)
footer(s, 12)

# ══════════════════════════════════════════════════════════════════════════
# 13. Что сохраняется + пример
# ══════════════════════════════════════════════════════════════════════════
s = slide()
header(s, "РЕЗУЛЬТАТ", "Что попадает в журнал",
       "Пример реальной записи 445928 от 06.11.2026.")
rows_list(s, [
    ("Склад", "GLP-C"),
    ("Номер", "8066"),
    ("Модуль", "Chassis"),
    ("Ошибка", "Went off the road"),
    ("Причина", "Mechanical"),
    ("Описание", "The robot left its route — текст из шаблона"),
    ("Фото / смена / автор", "есть · по времени · из /reg"),
], x=0.7, y=2.0, w=8.3, col_w=[2.6], row_h=0.44, gap=0.03, font=11.5,
   head=("Поле", "Значение"))

box(s, 9.35, 2.0, 3.28, 3.55, fill=TEAL_SOFT)
text(s, 9.6, 2.18, 2.85, 0.4, "Автоматически", size=13, bold=True, color=TEAL)
rich(s, 9.6, 2.62, 2.8, 2.7, [
    "склад — по номеру",
    "зона — по устройству",
    "смена — по времени",
    "автор — из /reg",
    "фото — из сообщения",
], size=12, color=TEXT, spacing=1.35)

why(s, "одна запись содержит всё, что нужно отчёту и разбору — вручную ничего не переносится.", y=5.95, h=0.6)
footer(s, 13)

# ══════════════════════════════════════════════════════════════════════════
# 14. Частые ошибки
# ══════════════════════════════════════════════════════════════════════════
s = slide()
header(s, "ЕСЛИ ЧТО-ТО ПОШЛО НЕ ТАК", "Частые ошибки", "Короткие ответы на частые ситуации.")
rows_list(s, [
    ("В отчёте пусто «кто сообщил»", "Сделайте /reg Имя Фамилия и отправьте отчёт заново"),
    ("«Please try again»", "В номере текст/описание — введите только номер"),
    ("«Not in the registry»", "Edit number — опечатка · Confirm — реально новый"),
    ("Бот молчит на фото", "Это не фото или чужой топик — отправьте фото в топик ошибок"),
    ("Две одинаковые кнопки причины", "Выбирайте вторую — она связана с шаблоном"),
    ("Edit selection вернул к номеру", "Так и задумано — пройдите ветку заново"),
], x=0.7, y=2.0, w=11.93, col_w=[4.4], row_h=0.5, gap=0.04, font=11.5,
   head=("Симптом", "Что делать"))
why(s, "почти все сбои — это номер не в том формате или отчёт не в том топике.", y=5.9, h=0.6)
footer(s, 14)

# ══════════════════════════════════════════════════════════════════════════
# 15. Шпаргалка
# ══════════════════════════════════════════════════════════════════════════
s = slide(NAVY)
text(s, 0.8, 0.6, 11.5, 0.5, "ШПАРГАЛКА", size=13, bold=True, color=RGBColor(0x5E, 0xEA, 0xD4))
text(s, 0.8, 1.05, 11.8, 0.8, "Вся инструкция на одной странице", size=32, bold=True, color=WHITE)
items = [
    [("/reg Имя Фамилия", True), (" — один раз", False)],
    [("Фото", True), (" — в топик ошибок", False)],
    [("Workstation / Charging station / Robot", True), (" — что на фото", False)],
    [("Номер", True), (" — 3780 · 137 · C2-H146 · несколько через /", False)],
    [("Модель", True), (" — обычно сама", False)],
    [("Модуль → Ошибка → Причина", True), (" — кнопками", False)],
    [("Other", True), (" — опишите словами", False)],
    [("Confirm", True), (" — отправить", False)],
]
for i, parts in enumerate(items):
    y = 2.15 + i * 0.55
    circ = s.shapes.add_shape(MSO_SHAPE.OVAL, Inches(0.8), Inches(y), Inches(0.38), Inches(0.38))
    circ.fill.solid()
    circ.fill.fore_color.rgb = RGBColor(0x5E, 0xEA, 0xD4)
    circ.line.fill.background()
    circ.shadow.inherit = False
    tf = circ.text_frame
    tf.margin_left = tf.margin_right = 0
    p = tf.paragraphs[0]
    p.alignment = PP_ALIGN.CENTER
    r = p.add_run()
    r.text = str(i + 1)
    r.font.name = FONT
    r.font.size = Pt(12)
    r.font.bold = True
    r.font.color.rgb = NAVY
    rich(s, 1.4, y - 0.02, 11.2, 0.45, [parts], size=15, color=WHITE, spacing=1.0)
text(s, 0.8, 6.75, 11.8, 0.4, "Сохраните эту страницу — остальное бот подскажет сам.",
     size=13, color=RGBColor(0x94, 0xA3, 0xB8))

prs.save("/Users/nookon/Desktop/LarkBot/GUIDE-ERROR-REPORT.pptx")
print("saved", len(prs.slides), "slides")
