"""Offline regression tests for menu routing and personal memory."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram import Update, User
from telegram.ext import ApplicationBuilder

from bot import game_memory, handlers


class MenuTests(unittest.IsolatedAsyncioTestCase):
    async def test_menu_buttons_reach_their_actions(self):
        application = ApplicationBuilder().token("123456:test-token").build()
        self.addAsyncCleanup(application.bot.shutdown)
        handlers.register_handlers(application)
        for text, action in (
            (handlers.MENU_HELP, "help_command"),
            (handlers.MENU_ABOUT, "about"),
            (handlers.MENU_PING, "ping"),
            (handlers.MENU_NEWS, "news"),
        ):
            with self.subTest(text=text):
                update = self.make_update(text, application.bot)
                selected = next(h for h in application.handlers[0] if h.check_update(update))
                self.assertIs(selected.callback, handlers.menu_button)
                with patch.object(handlers, action, new_callable=AsyncMock) as callback:
                    await selected.callback(update, SimpleNamespace())
                    callback.assert_awaited_once()

    @staticmethod
    def make_update(text, bot, command=False):
        message = {
            "message_id": 1, "date": 0,
            "chat": {"id": 100, "type": "group", "title": "Realm"},
            "from": {"id": 1, "is_bot": False, "first_name": "Player"},
            "text": text,
        }
        if command:
            message["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text)}]
        return Update.de_json({"update_id": 1, "message": message}, bot)

    async def test_commands_and_mentions_keep_their_routes(self):
        application = ApplicationBuilder().token("123456:test-token").build()
        self.addAsyncCleanup(application.bot.shutdown)
        application.bot._bot_user = User(123456, "Redstone", True, username="RedstoneBot")
        handlers.register_handlers(application)
        for text, expected in (
            ("/start", handlers.start), ("/help", handlers.help_command),
            ("/about", handlers.about), ("/ping", handlers.ping),
            ("/news", handlers.news), ("/ask", handlers.ask),
            ("/unknown", handlers.unknown_command),
            ("/game_memory", game_memory.show_memory),
            ("/forget_game_memory", game_memory.forget_memory),
            ("@RedstoneBot привет", handlers.mention_ai),
        ):
            with self.subTest(text=text):
                update = self.make_update(text, application.bot, text.startswith("/"))
                selected = next(h for h in application.handlers[0] if h.check_update(update))
                self.assertIs(selected.callback, expected)

    async def test_no_registered_handler_accepts_bot_messages(self):
        application = ApplicationBuilder().token("123456:test-token").build()
        self.addAsyncCleanup(application.bot.shutdown)
        application.bot._bot_user = User(123456, "Redstone", True, username="RedstoneBot")
        handlers.register_handlers(application)
        for text in ("/ask", "/news", "/start", "/game_memory", "/forget_game_memory",
                     "@RedstoneBot вопрос", handlers.MENU_NEWS, "Мы построили ферму"):
            data = self.make_update(text, application.bot, text.startswith("/")).to_dict()
            data["message"]["from"]["is_bot"] = True
            update = Update.de_json(data, application.bot)
            with self.subTest(text=text):
                self.assertFalse(any(h.check_update(update) for h in application.handlers[0]))


class MemoryCandidateTests(unittest.TestCase):
    def test_greetings_and_questions_are_skipped(self):
        for text in (
            "Привет!", "Спасибо", "Как найти древний город?",
            "Как улучшить мою ферму", "У меня есть алмазы?",
            "Мой дом защищён от криперов?", "What is my favorite biome?",
            "Как построить ферму железа?", "hello",
        ):
            with self.subTest(text=text):
                self.assertFalse(handlers.has_personal_memory_candidate(text))

    def test_explicit_facts_and_corrections_are_selected(self):
        for text in (
            "Меня зовут Саша", "Зови меня Алекс", "У меня есть пёс Рекс",
            "У меня больше нет кота", "Мой любимый биом — вишнёвая роща",
            "Я строю замок", "Я играю в Bedrock", "I have a dog named Rex",
            "My favorite biome is cherry grove", "Call me Alex",
            "Привет! У меня есть кот Барсик. Как построить ферму?",
            "Как построить ферму? Я строю замок.",
        ):
            with self.subTest(text=text):
                self.assertTrue(handlers.has_personal_memory_candidate(text))


class MemoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.data = {
            "redstone:profile:100:1": "У пользователя есть пёс Рекс.",
            "redstone:profile:100:2": "У пользователя есть кот.",
            "redstone:profile:200:1": "Другой профиль.",
        }
        self.redis = SimpleNamespace(
            get=AsyncMock(side_effect=lambda key: self.data.get(key)),
            set=AsyncMock(side_effect=lambda key, value: self.data.__setitem__(key, value)),
        )
        self.context = SimpleNamespace(bot_data={handlers.REDIS_KEY: self.redis})

    async def test_skipped_messages_preserve_existing_memory_without_ai(self):
        original = dict(self.data)
        with patch.object(handlers, "OpenAI") as client:
            for text in ("Привет", "Как построить ферму?", "Мой дом безопасен?"):
                result = await handlers.update_user_profile(
                    self.context, 100, 1, original["redstone:profile:100:1"], text,
                )
                self.assertEqual(result, original["redstone:profile:100:1"])
            client.assert_not_called()
        self.redis.set.assert_not_awaited()
        self.assertEqual(self.data, original)

    async def test_fact_updates_only_the_correct_profile(self):
        original = dict(self.data)
        with patch.object(handlers, "OpenAI") as client:
            client.return_value.responses.create.return_value.output_text = "Пёс Рекс. Строит замок."
            result = await handlers.update_user_profile(
                self.context, 100, 1, original["redstone:profile:100:1"], "Я строю замок",
            )
            client.return_value.responses.create.assert_called_once()
            request = client.return_value.responses.create.call_args.kwargs
            self.assertIn(original["redstone:profile:100:1"], request["input"])
        self.assertEqual(await handlers.get_user_profile(self.context, 100, 1), result)
        self.assertEqual(await handlers.get_user_profile(self.context, 100, 2), original["redstone:profile:100:2"])
        self.assertEqual(await handlers.get_user_profile(self.context, 200, 1), original["redstone:profile:200:1"])
        self.redis.set.assert_awaited_once_with("redstone:profile:100:1", result)

    async def test_no_redis_means_no_profile_request(self):
        self.context.bot_data.clear()
        with patch.object(handlers, "OpenAI") as client:
            self.assertEqual(await handlers.update_user_profile(
                self.context, 100, 1, "Старый профиль", "У меня есть пёс",
            ), "Старый профиль")
            client.assert_not_called()

    async def test_unchanged_or_empty_response_does_not_overwrite_memory(self):
        original = dict(self.data)
        current = original["redstone:profile:100:1"]
        for response in (current, "   "):
            with self.subTest(response=response), patch.object(handlers, "OpenAI") as client:
                client.return_value.responses.create.return_value.output_text = response
                self.assertEqual(await handlers.update_user_profile(
                    self.context, 100, 1, current, "У меня есть пёс Рекс",
                ), current)
        self.redis.set.assert_not_awaited()
        self.assertEqual(self.data, original)

    async def test_api_failure_preserves_existing_memory(self):
        original = dict(self.data)
        current = original["redstone:profile:100:1"]
        with patch.object(handlers, "OpenAI") as client, self.assertLogs(handlers.logger, level="ERROR"):
            client.return_value.responses.create.side_effect = RuntimeError("offline failure")
            self.assertEqual(await handlers.update_user_profile(
                self.context, 100, 1, current, "Я строю замок",
            ), current)
        self.redis.set.assert_not_awaited()
        self.assertEqual(self.data, original)

    async def test_mention_uses_one_request_for_question_two_for_fact(self):
        self.context.bot = SimpleNamespace(username="RedstoneBot")
        for prompt, expected_calls in (("Как построить ферму?", 1), ("У меня есть пёс Рекс", 2)):
            with self.subTest(prompt=prompt):
                message = SimpleNamespace(
                    text=f"@RedstoneBot {prompt}", chat_id=100, reply_text=AsyncMock(),
                )
                update = SimpleNamespace(effective_message=message, effective_user=SimpleNamespace(
                    id=1, full_name="Player", username="player",
                ))
                with patch.object(handlers, "OpenAI") as client:
                    client.return_value.responses.create.return_value.output_text = "Ответ"
                    await handlers.mention_ai(update, self.context)
                    self.assertEqual(client.return_value.responses.create.call_count, expected_calls)
                message.reply_text.assert_awaited_once_with("Ответ")


if __name__ == "__main__":
    unittest.main()
