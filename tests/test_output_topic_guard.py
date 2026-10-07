"""Бот пишет **только** в обслуживаемые топики.

**Владелец 07.10.2026:** «бот время от времени продолжает слать в другие топики
что он не может его отслеживатьмне нужно чтобы он смотрел только за Ex GLPC,
Ex SP3 and Stats ни в какой больше топик он не должен писать вообще его там
нету».

**Что было не так.** Получив сообщение из чужого топика, `_handle_wrong_topic`
отвечал **в тот же чужой топик** — «This topic is not monitored». Защита сама
создавала то, от чего защищала: в топике, где бота «нет», появлялось его
сообщение. Владелец видел это «время от времени» — по числу новых чужих топиков.

**Что сделано.** Барьер `_output_topic_allowed` стоит в **единственной** точке
отправки и в остальных путях (`_send_action`, меню ошибок, фолбэк), поэтому его
не обходит ни команда, ни фоновая досылка, ни предупреждение.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _run_with_env(snippet: str, env: dict) -> dict:
    """Выполняет проверку в отдельном процессе с нужным окружением.

    Отдельный процесс обязателен: bot.py читает окружение **на импорте**, а
    подменять его в уже загруженном модуле значило бы проверять не то, что
    работает в бою.
    """
    base = {
        "TELEGRAM_BOT_TOKEN": "123:TEST",
        "SUPABASE_URL": "https://example.supabase.co",
        "SUPABASE_SERVICE_KEY": "k",
        "PATH": os.environ.get("PATH", ""),
        **env,
    }
    result = subprocess.run(
        # `DOTENV_PATH=/dev/null`: иначе рабочий `.env` подмешает свои топики, и
        # тест начнёт проверять его содержимое вместо логики.
        [sys.executable, "-c", snippet],
        cwd=ROOT,
        env=base,
        capture_output=True,
        text=True,
        timeout=60,
    )

    return {"out": result.stdout.strip(), "err": result.stderr, "code": result.returncode}


#: Сниппет выполняется в подпроцессе. **Сначала гасим `.env`.**
#:
#: `config.py` вызывает `load_dotenv()` на импорте, поэтому рабочий `.env`
#: подмешивает свои топики (`2`, `318`) в любое окружение. Без этого шага тест
#: проверял бы **содержимое файла**, а не логику барьера: на машине без `.env`
#: он проходил бы, а рядом с ним — падал.
SNIPPET = """
import os
os.environ["DOTENV_PATH"] = os.devnull
import dotenv
dotenv.load_dotenv = lambda *a, **k: False  # запрет подмешивания .env

import telegram_bot as bot
allowed = sorted(bot.allowed_output_topics())
print("allowed", allowed)
for tid in (2, 318, 320, 319, 321, 777, None):
    print("check", tid, bot._output_topic_allowed(-100, tid))
"""


class TestBotWritesOnlyWhereItBelongs:
    def test_foreign_topics_are_refused(self) -> None:
        """Чужие топики (в том числе Service 321) — молчание."""
        env = {
            "TELEGRAM_TOPIC_ID": "2",
            "TELEGRAM_TOPIC_ERROR_SP3": "318",
            "TELEGRAM_TOPIC_STATUS": "319",
            "TELEGRAM_TOPIC_STATS": "320",
            "TELEGRAM_TOPIC_SERVICE": "321",
        }
        res = _run_with_env(SNIPPET, env)

        assert res["code"] == 0, res["err"]
        lines = dict(
            line.split(" ", 1) for line in res["out"].splitlines() if " " in line
        )
        allowed = lines["allowed"]

        # Обслуживаемые топики разрешены
        for ok_topic in ("2", "318", "320", "319"):
            assert f"check {ok_topic} True" in res["out"], f"{ok_topic} должен быть разрешён"

        # Service (321) и произвольный (777) — запрещены
        assert "321" not in allowed, f"Service-топик не должен быть разрешён: {allowed}"
        assert "check 321 False" in res["out"], "321 (Service) обязан быть запрещён"
        assert "check 777 False" in res["out"], "произвольный топик обязан быть запрещён"
        assert "check None False" in res["out"], "General — молчание"

    def test_unconfigured_bot_is_not_muted(self) -> None:
        """Если топики вовсе не настроены — бот **не** немеет.

        Крайний случай: локальный прогон и свежая установка. Строгая проверка
        при пустом списке превратилась бы в полную немоту, и это выглядело бы
        как «бот перестал отвечать» без единой ошибки в логе.

        **Почему окружение очищается полностью.** Первая версия теста задавала
        только `TELEGRAM_TOPIC_ID=""` — и в списке оказывались `2` и `318`,
        подтянутые из рабочего `.env`. Тест при этом проходил бы на машине без
        `.env` и падал рядом с ним, то есть проверял бы **файл**, а не логику.
        Здесь переменные топиков удалены явно.
        """
        res = _run_with_env(
            SNIPPET,
            {
                "TELEGRAM_TOPIC_ID": "",
                "TELEGRAM_TOPIC_NAME": "",
                "TELEGRAM_TOPIC_ERROR_GLPC": "",
                "TELEGRAM_TOPIC_ERROR_SP3": "",
                "TELEGRAM_TOPIC_STATUS": "",
                "TELEGRAM_TOPIC_STATS": "",
                "TELEGRAM_TOPIC_SERVICE": "",
                "TELEGRAM_LISTEN_TOPICS": "",
                "TELEGRAM_IGNORE_TOPICS": "",
            },
        )

        assert res["code"] == 0, res["err"]
        assert "check 777 True" in res["out"], (
            f"без настроенных топиков фильтр не должен глушить. Вывод: {res['out']}"
        )

    def test_errors_topics_are_always_allowed(self) -> None:
        """Оба топика ошибок разрешены — иначе бот не смог бы отвечать людям."""
        env = {
            "TELEGRAM_TOPIC_ERROR_GLPC": "2",
            "TELEGRAM_TOPIC_ERROR_SP3": "318",
        }
        res = _run_with_env(SNIPPET, env)

        assert res["code"] == 0, res["err"]
        assert "check 2 True" in res["out"]
        assert "check 318 True" in res["out"]


class TestWrongTopicWarningGoesToOurOwnTopic:
    """Предупреждение о чужом топике уходит �� **свой** топик.

    **Это и был исходный дефект.** `_handle_wrong_topic` отвечал в тот же чужой
    топик: «This topic is not monitored». Владелец видел это как «бот продолжает
    слать в другие топики».

    Проверяется по **тексту кода**: убедиться, что уходит не `thread_id`
    источника, а топик статистики. Поведенческий тест потребовал бы поднять
    Telegram, а здесь важна именно адресация.
    """

    def test_warning_is_not_sent_to_the_source_topic(self) -> None:
        source = (ROOT / "telegram_bot.py").read_text(encoding="utf-8")
        start = source.find("def _handle_wrong_topic")
        end = source.find("\ndef ", start + 10)
        block = source[start:end]

        assert "target = topic_thread(\"stats\")" in block, (
            "предупреждение должно адресоваться в обслуживаемый топик"
        )
        assert "_send(chat_id, text, thread_id=target)" in block, (
            "отправка обязана идти в target, а не в topic-источник"
        )
        assert "_send(chat_id, text, thread_id=thread_id)" not in block, (
            "ответ в топик-источник — это тот самый дефект: бот отметится там, "
            "где его быть не должно"
        )
