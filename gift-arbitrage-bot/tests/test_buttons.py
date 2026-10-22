"""Кнопки: меню под полем ввода, кнопки под сообщениями, подтверждения опасных действий."""
import asyncio
import json
import re

import httpx

from giftbot.markets.base import SchemaError
from giftbot.notifier import TelegramBot
from giftbot.ui import COMMANDS, MENU, Reply
from test_trader import World


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------ транспорт (notifier)


def make_bot(admin=42, edit_error=None):
    """Бот на заглушке Telegram API: calls — все вызовы (метод, тело)."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        calls.append((method, body))
        if method == "editMessageText" and edit_error:
            return httpx.Response(400, json={"ok": False, "description": edit_error})
        return httpx.Response(200, json={"ok": True, "result": [] if method == "getUpdates" else True})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return TelegramBot("123:abc", admin, client=client, send_interval=0), calls


def methods(calls):
    return [m for m, _ in calls]


def press(chat_id, data, message_id=7):
    return {
        "update_id": 2,
        "callback_query": {
            "id": "cb1",
            "data": data,
            "message": {"message_id": message_id, "chat": {"id": chat_id}},
        },
    }


def recorder(reply):
    seen = []

    async def handler(command, args):
        seen.append((command, args))
        return reply

    return handler, seen


def test_menu_button_runs_its_command_and_keeps_menu():
    bot, calls = make_bot()
    handler, seen = recorder("ok")
    run(bot.handle_update({"update_id": 1, "message": {"chat": {"id": 42}, "text": "📊 Статус"}}, handler))
    assert seen == [("/status", [])]
    [(method, body)] = calls
    assert method == "sendMessage"
    keyboard = body["reply_markup"]["keyboard"]
    assert [b["text"] for row in keyboard for b in row] == [label for row in MENU for label, _ in row]
    assert body["reply_markup"]["is_persistent"] is True


def test_menu_button_from_stranger_is_ignored():
    bot, calls = make_bot()
    handler, seen = recorder("ok")
    run(bot.handle_update({"update_id": 1, "message": {"chat": {"id": 999}, "text": "⏸ Пауза"}}, handler))
    assert seen == [] and calls == []


def test_button_updates_panel_in_place():
    bot, calls = make_bot()
    handler, seen = recorder(Reply("статус", ((("🔄 Обновить", "/status"),),)))
    run(bot.handle_update(press(42, "/autobuy off"), handler))
    assert seen == [("/autobuy", ["off"])]
    assert methods(calls) == ["answerCallbackQuery", "editMessageText"]
    edit = calls[1][1]
    assert edit["chat_id"] == 42 and edit["message_id"] == 7 and edit["text"] == "статус"
    assert edit["reply_markup"] == {"inline_keyboard": [[{"text": "🔄 Обновить", "callback_data": "/status"}]]}


def test_plus_button_answers_with_new_message():
    bot, calls = make_bot()
    handler, seen = recorder(Reply("включено", ()))
    run(bot.handle_update(press(42, "+/resume"), handler))
    assert seen == [("/resume", [])]
    assert methods(calls) == ["answerCallbackQuery", "sendMessage"]  # уведомление с кнопкой не стёрто


def test_button_from_stranger_does_nothing():
    bot, calls = make_bot()
    handler, seen = recorder("ok")
    run(bot.handle_update(press(999, "/autobuy on"), handler))
    assert seen == []
    assert methods(calls) == ["answerCallbackQuery"]  # только убрать «часики»


def test_unknown_button_data_is_rejected():
    bot, calls = make_bot()
    handler, seen = recorder("ok")
    run(bot.handle_update(press(42, "garbage"), handler))
    assert seen == []
    assert calls == [("answerCallbackQuery", {"callback_query_id": "cb1", "text": "Кнопка устарела"})]


def test_unchanged_panel_is_not_resent():
    bot, calls = make_bot(edit_error="Bad Request: message is not modified")
    handler, _ = recorder(Reply("тот же статус", ()))
    run(bot.handle_update(press(42, "/status"), handler))
    assert methods(calls) == ["answerCallbackQuery", "editMessageText"]


def test_failed_edit_falls_back_to_new_message():
    bot, calls = make_bot(edit_error="Bad Request: message to edit not found")
    handler, _ = recorder(Reply("статус", ((("🔄 Обновить", "/status"),),)))
    run(bot.handle_update(press(42, "/status"), handler))
    assert methods(calls) == ["answerCallbackQuery", "editMessageText", "sendMessage"]
    assert calls[-1][1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "/status"


def test_command_menu_matches_telegram_rules():
    bot, calls = make_bot()
    run(bot.setup_commands())
    [(method, body)] = calls
    assert method == "setMyCommands"
    for item in body["commands"]:
        assert re.fullmatch(r"[a-z0-9_]{1,32}", item["command"])
        assert 1 <= len(item["description"]) <= 256
    assert [c["command"] for c in body["commands"]] == [name for name, _ in COMMANDS]


def test_updates_include_button_presses():
    bot, calls = make_bot()
    run(bot.fetch_updates(timeout=0))
    assert calls[0][1]["allowed_updates"] == ["message", "callback_query"]


# ------------------------------------------------------------------ панели сканера и автопокупки


def all_buttons(reply):
    return [button for row in reply.buttons for button in row]


def callback_data_fits(reply):
    return all(1 <= len(data.encode()) <= 64 for _, data in all_buttons(reply))  # предел Telegram


def test_status_panel_toggles_pause_and_resume(store):
    world = World(store)
    panel = run(world.scanner.on_command("/status", []))
    assert ("🔄 Обновить", "/status") in all_buttons(panel) and ("⏸ Пауза", "/pause") in all_buttons(panel)
    paused = run(world.scanner.on_command("/pause", []))
    assert paused.text.startswith("⏸ Сканирование на паузе.") and "На паузе" in paused.text
    assert ("▶️ Продолжить", "/resume") in all_buttons(paused)
    resumed = run(world.scanner.on_command("/resume", []))
    assert ("⏸ Пауза", "/pause") in all_buttons(resumed)
    assert callback_data_fits(resumed)


def test_trip_alert_offers_resume_without_erasing_alert(store):
    world = World(store)
    world.arm()
    world.portals.errors = [SchemaError("portals", "нет поля results")]
    world.poll()
    buttons = [b for row in world.bot.buttons[-1] for b in row]
    assert ("▶️ Включить сейчас", "+/resume") in buttons  # «+»: ответ отдельным сообщением
    assert ("🤖 Автопокупка", "+/autobuy") in buttons  # автопокупка выключилась — кнопка, чтобы вернуть


def test_startup_message_shows_menu(store):
    world = World(store)
    run(world.scanner.start())
    assert "Сканер запущен" in world.bot.sent[0] and world.bot.menus[0] is True


def test_live_autobuy_needs_confirmation(store):
    world = World(store)  # режим live
    panel = run(world.scanner.on_command("/autobuy", []))
    assert ("✅ Включить", "+/autobuy ask") in all_buttons(panel)  # подтверждение — отдельным сообщением
    confirm = run(world.scanner.on_command("/autobuy", ["ask"]))
    assert "реальные TON" in confirm.text and not world.trader.armed  # одного нажатия мало
    assert confirm.buttons == ((("Отмена", "/autobuy"),), (("✅ Да, включить", "/autobuy on"),))
    armed = run(world.scanner.on_command("/autobuy", ["on"]))
    assert world.trader.armed and ("⏹ Выключить", "/autobuy off") in all_buttons(armed)
    off = run(world.scanner.on_command("/autobuy", ["off"]))
    assert not world.trader.armed and off.text.startswith("⏹ Автопокупка выключена.")


def test_dry_autobuy_turns_on_with_one_press(store):
    world = World(store, mode="dry")
    run(world.scanner.on_command("/autobuy", ["ask"]))
    assert world.trader.armed


def test_mode_off_panel_has_no_enable_button(store):
    world = World(store, mode="off")
    panel = run(world.scanner.on_command("/autobuy", []))
    assert all_buttons(panel) == [("🔄 Обновить", "/autobuy")]
    run(world.scanner.on_command("/autobuy", ["ask"]))
    assert not world.trader.armed


def test_close_position_button_asks_first(store):
    world = World(store)
    world.arm()
    world.portals_snipe()
    world.poll()
    panel = run(world.scanner.on_command("/positions", []))
    assert ("🗑 Закрыть #1", "+/forget ask 1") in all_buttons(panel)  # подтверждение — отдельным сообщением

    confirm = run(world.scanner.on_command("/forget", ["ask", "1"]))
    assert "Закрыть позицию #1" in confirm.text and world.store.positions(["listed"])  # ещё открыта
    assert confirm.buttons == ((("Отмена", "/positions"),), (("✅ Да, закрыть", "/forget 1"),))

    closed = run(world.scanner.on_command("/forget", ["1"]))
    assert closed.text.startswith("Позиция #1 закрыта.") and not world.store.positions(["listed"])
    assert all_buttons(closed) == [("🔄 Обновить", "/positions")]  # кнопок закрытия больше нет

    again = run(world.scanner.on_command("/forget", ["ask", "1"]))  # старая кнопка нажата ещё раз
    assert "уже закрыта" in again.text


def test_position_buttons_fit_telegram_limit(store):
    world = World(store)
    world.arm()
    world.portals_snipe()
    world.poll()
    world.store._db.execute("UPDATE positions SET id = 123456789012")
    assert callback_data_fits(run(world.scanner.on_command("/positions", [])))
    assert callback_data_fits(run(world.scanner.on_command("/forget", ["ask", "123456789012"])))


# ------------------------------------------------------------------ нажатия подряд в одну точку


def tap_repeatedly(world, panel, row, col, times=6):
    """Жмём в одно и то же место. Кнопка без «+» заменяет сообщение ответом, с «+» — ответ приходит
    отдельно, а под пальцем остаётся то же сообщение. Возвращает, включена ли автопокупка после каждого нажатия."""
    armed_after = []
    for _ in range(times):
        if row >= len(panel.buttons) or col >= len(panel.buttons[row]):
            break  # в этом месте кнопки больше нет
        data = panel.buttons[row][col][1]
        command, *args = data.lstrip("+").split()
        reply = run(world.scanner.on_command(command, args))
        armed_after.append(world.trader.armed)
        if not data.startswith("+") and hasattr(reply, "buttons"):
            panel = reply
    return armed_after


def test_repeated_taps_never_turn_live_autobuy_back_on(store):
    world = World(store)
    world.arm()
    armed_panel = run(world.scanner.on_command("/autobuy", []))
    for row, buttons in enumerate(armed_panel.buttons):
        for col in range(len(buttons)):
            world.arm()
            panel = run(world.scanner.on_command("/autobuy", []))
            states = tap_repeatedly(world, panel, row, col)
            if panel.buttons[row][col][1] == "/autobuy off":
                assert not any(states), f"нажатия в [{row}][{col}] снова включили автопокупку: {states}"


def test_double_tap_on_enable_does_not_skip_confirmation(store):
    world = World(store)
    panel = run(world.scanner.on_command("/autobuy", []))
    row, col = next((r, c) for r, b in enumerate(panel.buttons) for c, x in enumerate(b) if x[0] == "✅ Включить")
    states = tap_repeatedly(world, panel, row, col)
    assert states and not any(states), f"повторные нажатия на «Включить» проскочили подтверждение: {states}"


def test_repeated_taps_on_pause_never_resume(store):
    world = World(store)
    panel = run(world.scanner.on_command("/status", []))
    row, col = next((r, c) for r, b in enumerate(panel.buttons) for c, x in enumerate(b) if x[1] == "/pause")
    tap_repeatedly(world, panel, row, col)
    assert world.scanner.paused


def test_repeated_taps_on_close_never_close(store):
    world = World(store)
    world.arm()
    world.portals_snipe()
    world.poll()
    panel = run(world.scanner.on_command("/positions", []))
    tap_repeatedly(world, panel, 0, 0)
    assert world.store.positions(["listed"])


# ------------------------------------------------------------------ нажатия, сделанные пока бот был выключен


def test_stale_press_is_not_executed():
    bot, calls = make_bot()

    async def handler(command, args):  # pragma: no cover
        raise AssertionError("устаревшее нажатие выполнилось")

    def old_query(request):  # Telegram отвечает так на давнее нажатие
        return httpx.Response(400, json={"ok": False, "description": "Bad Request: query is too old and response "
                                         "timeout expired or query ID is invalid"})

    bot._client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: old_query(r) if r.url.path.endswith("answerCallbackQuery") else
        (calls.append(("sendMessage", json.loads(r.content))) or httpx.Response(200, json={"ok": True, "result": True}))
    ))
    run(bot.handle_update(press(42, "/autobuy on"), handler))
    [(method, body)] = calls
    assert method == "sendMessage" and "не выполнено" in body["text"]


def test_presses_queued_before_start_are_skipped_but_typed_commands_run():
    backlog = [
        press(42, "/autobuy on"),
        {"update_id": 3, "message": {"chat": {"id": 42}, "text": "/status"}},
    ]
    calls = []

    def handler_http(request):
        method = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        calls.append((method, body))
        if method == "getUpdates":
            result = backlog if body["offset"] == 0 else []
            return httpx.Response(200, json={"ok": True, "result": result})
        return httpx.Response(200, json={"ok": True, "result": True})

    bot = TelegramBot("123:abc", 42, client=httpx.AsyncClient(transport=httpx.MockTransport(handler_http)), send_interval=0)
    handler, seen = recorder("статус")
    run(bot.skip_stale_presses(handler))
    assert seen == [("/status", [])]  # напечатанная команда выполнена, нажатие — нет
    texts = [body["text"] for method, body in calls if method == "sendMessage"]
    assert "статус" in texts and any("не выполнено" in t for t in texts)


# ------------------------------------------------------------------ устойчивость списка позиций


def test_positions_of_removed_market_do_not_break_commands(store):
    world = World(store)
    world.arm()
    world.portals_snipe()
    world.poll()
    del world.scanner.adapters["portals"]  # площадку убрали из MARKETS, позиция на ней осталась
    assert "#1" in run(world.scanner.on_command("/positions", [])).text
    assert "Закрыть позицию #1" in run(world.scanner.on_command("/forget", ["ask", "1"])).text
    assert run(world.scanner.on_command("/forget", ["1"])).text.startswith("Позиция #1 закрыта.")


def test_more_positions_than_buttons_are_listed_honestly(store):
    from giftbot.evaluator import evaluate
    from giftbot.trader import MAX_POSITION_BUTTONS
    from conftest import lot

    world = World(store, max_open=100)
    cfg = world.scanner.cfg
    for i in range(MAX_POSITION_BUTTONS + 3):
        deal = evaluate(lot("portals", f"x{i}", 8.0, number=i), "portals", 12.0, 3, cfg.fees, cfg.thresholds)
        world.store.open_position(deal, "bought")
    panel = run(world.scanner.on_command("/positions", []))
    assert len(panel.buttons) == MAX_POSITION_BUTTONS + 1  # 20 «Закрыть» + «Обновить»
    assert "…и ещё 3" in panel.text and f"#{MAX_POSITION_BUTTONS + 1} " not in panel.text
