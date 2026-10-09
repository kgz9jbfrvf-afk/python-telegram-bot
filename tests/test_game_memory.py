"""Offline Redis/Lua, permissions, privacy and Telegram regression tests."""

import asyncio
import json
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from fakeredis.aioredis import FakeRedis
from redis.exceptions import ConnectionError

from bot import game_memory as gm, handlers


def make_update(text="Мы построили ферму на 100, 64, -200", *, user_id=1, chat_id=100,
                chat_type="supergroup", is_bot=False, sender_chat=None, edited=False):
    user = NS(id=user_id, is_bot=is_bot, full_name="Private Name", username="private_handle")
    message = NS(text=text, chat_id=chat_id, chat=NS(type=chat_type),
                 sender_chat=sender_chat, reply_text=AsyncMock())
    return NS(effective_message=message, effective_user=user,
              edited_message=message if edited else None)


class ParserTests(unittest.TestCase):
    def test_game_categories_and_scopes(self):
        for text, personal, obj, state in (
            ("Мы построили ферму на 100, 64, -200", False, "ферма", "готово"),
            ("Я строю замок", True, "замок", "строится"),
            ("Планирую построить базу", True, "база", "план"),
            ("Мы будем строить портал в незере", False, "портал", "план"),
            ("Я победил дракона", True, "дракон", "достижение"),
            ("I found elytra", True, "элитры", "найдено"),
            ("Наша база: 1 64 3", False, "база", "координаты"),
            ("Мы строим железную дорогу", False, "железная дорога", "строится"),
            ("Мы планируем проект в Minecraft Bedrock", False, "проект", "план"),
        ):
            with self.subTest(text=text):
                facts = gm.extract_facts(text)
                self.assertEqual(len(facts), 1)
                self.assertEqual((facts[0][0], facts[0][1]["object"], facts[0][1]["state"]),
                                 (personal, obj, state))
                self.assertTrue(gm.valid_fact(facts[0][1]))

    def test_noise_questions_negation_and_ambiguity_are_ignored(self):
        for text in ("Привет!", "Спасибо", "Как построить ферму?", "Где наша база",
                     "Мы не построили ферму", "Если мы построим базу", "дом",
                     "Мы построили базу и ферму на 1 64 2", "Я строю ферму?",
                     "База 1 999 2", "База 1 64 2 и 3 64 4", "а" * 2001):
            with self.subTest(text=text):
                self.assertEqual(gm.extract_facts(text), [])

    def test_sensitive_messages_are_rejected_before_processing(self):
        for suffix in ("пароль: abc", "token=abcdef", "api_key=abcdef", "sk-abcdef",
                       "123456789:ABCDEF", "email user@example.com", "+79991234567",
                       "улица Пушкина", "меня зовут Иван", "https://example.com",
                       "паспорт 123456", "4111111111111111"):
            with self.subTest(suffix=suffix):
                self.assertEqual(gm.extract_facts("Я построил ферму. " + suffix), [])

    def test_only_allowlisted_fields_survive(self):
        facts = gm.extract_facts("Я построил ферму «Аврора» 1 64 2")
        payload = json.dumps(facts, ensure_ascii=False)
        self.assertNotIn("Аврора", payload)
        self.assertEqual(set(facts[0][1]), {"object", "state", "coordinates", "dimension"})

    def test_corrupt_facts_are_rejected(self):
        for value in (None, [], {}, {"object": "ignore instructions"},
                      {"object": "база", "state": "готово", "dimension": "энд",
                       "coordinates": [True, 64, 2]}):
            self.assertFalse(gm.valid_fact(value))

    def test_dimensions_and_named_coordinates(self):
        fact = gm.extract_facts("Наша база в незере x=100, y=64, z=-200")[0][1]
        self.assertEqual(fact["coordinates"], [100, 64, -200])
        self.assertEqual(fact["dimension"], "незер")
        self.assertEqual(gm.extract_facts("Я строю замок")[0][1]["dimension"], "не указано")
        self.assertEqual(gm.extract_facts("База в незере и энде 1 64 2"), [])
        self.assertEqual(gm.extract_facts("Мы планируем проект для работы"), [])

    def test_third_party_facts_are_not_assigned_to_sender(self):
        self.assertFalse(gm.extract_facts("Саша построил ферму")[0][0])
        self.assertEqual(gm.extract_facts("Мой друг построил ферму"), [])
        self.assertEqual(gm.extract_facts("Я сказал что Саша построил ферму"), [])


class RedisMemoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.redis = FakeRedis(decode_responses=True)
        self.addAsyncCleanup(self.redis.aclose)
        self.context = NS(bot_data={"redis": self.redis}, args=[],
                          bot=NS(username="RedstoneBot", get_chat_member=AsyncMock(
                              return_value=NS(status="member"))))

    async def test_observer_is_silent_and_never_calls_openai(self):
        update = make_update()
        with patch.object(handlers, "OpenAI") as ai:
            await handlers.mention_ai(update, self.context)
            ai.assert_not_called()
        update.effective_message.reply_text.assert_not_awaited()
        facts = await gm.read_facts(self.redis, 100)
        self.assertEqual(facts[0]["coordinates"], [100, 64, -200])
        keys = await self.redis.keys("*")
        self.assertEqual(keys, [gm.memory_key(100)])
        stored = await self.redis.lrange(keys[0], 0, -1)
        self.assertNotIn("Private Name", str(stored))
        self.assertNotIn(update.effective_message.text, str(stored))

    async def test_chat_and_player_isolation_and_existing_profiles(self):
        await self.redis.set("redstone:profile:100:1", "Existing profile")
        await gm.observe(make_update("Я строю замок"), self.context)
        await gm.observe(make_update("Я строю базу", user_id=2), self.context)
        await gm.observe(make_update("Я строю портал", chat_id=200), self.context)
        await gm.observe(make_update(), self.context)
        self.assertEqual((await gm.read_facts(self.redis, 100, 1))[0]["object"], "замок")
        self.assertEqual((await gm.read_facts(self.redis, 100, 2))[0]["object"], "база")
        self.assertEqual((await gm.read_facts(self.redis, 200, 1))[0]["object"], "портал")
        self.assertEqual(len(await gm.read_facts(self.redis, 100)), 1)
        self.assertEqual(await self.redis.get("redstone:profile:100:1"), "Existing profile")
        context = await gm.answer_context(self.context, 100, 1)
        self.assertIn("замок", context)
        self.assertIn("ферма", context)
        self.assertNotIn("база", context)
        self.assertNotIn("портал", context)

    async def test_atomic_deduplication_and_bounded_retention(self):
        await asyncio.gather(*(gm.observe(make_update(), self.context) for _ in range(8)))
        self.assertEqual(await self.redis.llen(gm.memory_key(100)), 1)
        for x in range(45):
            await gm.observe(make_update(f"Наша база {x} 64 2"), self.context)
        self.assertEqual(await self.redis.llen(gm.memory_key(100)), gm.LIMIT)
        self.assertGreater(await self.redis.ttl(gm.memory_key(100)), 0)
        self.assertLessEqual(await self.redis.ttl(gm.memory_key(100)), gm.TTL)

    async def test_bots_private_channels_anonymous_and_edits_are_ignored(self):
        for kwargs in ({"is_bot": True}, {"chat_type": "private"}, {"chat_type": "channel"},
                       {"sender_chat": NS(id=123)}, {"edited": True}):
            with self.subTest(kwargs=kwargs):
                await gm.observe(make_update(**kwargs), self.context)
        self.assertEqual(await self.redis.keys("*"), [])
        update = make_update("@RedstoneBot привет", is_bot=True)
        with patch.object(handlers, "OpenAI") as ai:
            await handlers.mention_ai(update, self.context)
            ai.assert_not_called()
        update.effective_message.reply_text.assert_not_awaited()

    async def test_no_redis_is_silent_and_commands_explain_unavailability(self):
        self.context.bot_data.clear()
        update = make_update()
        await handlers.mention_ai(update, self.context)
        update.effective_message.reply_text.assert_not_awaited()
        self.assertEqual(await gm.answer_context(self.context, 100, 1), "")
        await gm.show_memory(update, self.context)
        self.assertIn("Redis недоступен", update.effective_message.reply_text.call_args.args[0])

    async def test_redis_write_and_read_errors_are_contained(self):
        self.context.bot_data["redis"] = NS(eval=AsyncMock(side_effect=ConnectionError("down")),
                                          lrange=AsyncMock(side_effect=ConnectionError("down")))
        update = make_update()
        with self.assertLogs(gm.logger, level="WARNING"):
            await gm.observe(update, self.context)
            self.assertEqual(await gm.answer_context(self.context, 100, 1), "")
            await gm.show_memory(update, self.context)
        self.assertIn("Не удалось", update.effective_message.reply_text.call_args.args[0])

    async def test_redis_delete_error_does_not_report_success(self):
        self.context.args = ["me"]
        self.context.bot_data["redis"] = NS(delete=AsyncMock(side_effect=ConnectionError("down")))
        update = make_update()
        with self.assertLogs(gm.logger, level="WARNING"):
            await gm.forget_memory(update, self.context)
        update.effective_message.reply_text.assert_awaited_once()
        self.assertIn("Не удалось", update.effective_message.reply_text.call_args.args[0])

    async def test_corrupt_redis_records_are_ignored(self):
        await self.redis.lpush(gm.memory_key(100), "not json", "null", '{"object": "hacked"}')
        self.assertEqual(await gm.read_facts(self.redis, 100), [])

    async def test_group_delete_requires_admin(self):
        await gm.observe(make_update(), self.context)
        update = make_update()
        await gm.forget_memory(update, self.context)
        self.assertIn("Нет прав", update.effective_message.reply_text.call_args.args[0])
        self.assertTrue(await gm.read_facts(self.redis, 100))
        self.context.bot.get_chat_member.return_value = NS(status="administrator")
        await gm.forget_memory(update, self.context)
        self.assertEqual(await gm.read_facts(self.redis, 100), [])

    async def test_delete_me_preserves_other_users_and_group(self):
        await gm.observe(make_update("Я строю замок"), self.context)
        await gm.observe(make_update("Я строю базу", user_id=2), self.context)
        await gm.observe(make_update(), self.context)
        self.context.args = ["me"]
        await gm.forget_memory(make_update(), self.context)
        self.assertEqual(await gm.read_facts(self.redis, 100, 1), [])
        self.assertTrue(await gm.read_facts(self.redis, 100, 2))
        self.assertTrue(await gm.read_facts(self.redis, 100))

    async def test_rights_failure_and_nonmembers_fail_closed(self):
        update = make_update()
        for status in ("left", "kicked"):
            self.context.bot.get_chat_member.return_value = NS(status=status)
            await gm.show_memory(update, self.context)
            self.assertIn("Нет прав", update.effective_message.reply_text.call_args.args[0])
        self.context.bot.get_chat_member.side_effect = RuntimeError("Telegram unavailable")
        await gm.forget_memory(update, self.context)
        self.assertIn("проверить права", update.effective_message.reply_text.call_args.args[0])

    async def test_restricted_membership_and_owner_permissions(self):
        await gm.observe(make_update(), self.context)
        update = make_update()
        self.context.bot.get_chat_member.return_value = NS(status="restricted", is_member=False)
        await gm.show_memory(update, self.context)
        self.assertIn("Нет прав", update.effective_message.reply_text.call_args.args[0])
        self.context.bot.get_chat_member.return_value = NS(status="restricted", is_member=True)
        await gm.show_memory(update, self.context)
        self.assertIn("ферма", update.effective_message.reply_text.call_args.args[0])
        self.context.bot.get_chat_member.return_value = NS(status="creator")
        await gm.forget_memory(update, self.context)
        self.assertFalse(await gm.read_facts(self.redis, 100))

    async def test_invalid_scope_and_private_commands_are_rejected(self):
        self.context.args = ["me", "2"]
        update = make_update()
        await gm.forget_memory(update, self.context)
        self.context.bot.get_chat_member.assert_not_awaited()
        self.context.args = []
        await gm.show_memory(make_update(chat_type="private"), self.context)
        self.context.bot.get_chat_member.assert_not_awaited()

    async def test_member_can_view_group_and_own_facts(self):
        await gm.observe(make_update(), self.context)
        update = make_update()
        await gm.show_memory(update, self.context)
        self.assertIn("100, 64, -200", update.effective_message.reply_text.call_args.args[0])
        self.context.args = ["me"]
        await gm.show_memory(update, self.context)
        self.assertIn("пока пусто", update.effective_message.reply_text.call_args.args[0])

    async def test_ask_and_mention_receive_game_context(self):
        await gm.observe(make_update(), self.context)
        for callback, text, args in ((handlers.ask, "/ask где ферма", ["где", "ферма"]),
                                     (handlers.mention_ai, "@RedstoneBot где ферма?", [])):
            with self.subTest(callback=callback.__name__):
                update = make_update(text)
                self.context.args = args
                with patch.object(handlers, "OpenAI") as ai:
                    ai.return_value.responses.create.return_value.output_text = "На 100, 64, -200"
                    await callback(update, self.context)
                    ai.return_value.responses.create.assert_called_once()
                    request = ai.return_value.responses.create.call_args.kwargs
                    self.assertIn("100, 64, -200", request["instructions"] + str(request["input"]))
                update.effective_message.reply_text.assert_awaited_once_with("На 100, 64, -200")

    async def test_ai_answers_survive_missing_or_broken_game_memory(self):
        for redis in (None, NS(get=AsyncMock(return_value=None), set=AsyncMock(),
                              lrange=AsyncMock(side_effect=ConnectionError("down")))):
            self.context.bot_data["redis"] = redis
            for callback, text in ((handlers.ask, "/ask вопрос"),
                                   (handlers.mention_ai, "@RedstoneBot вопрос")):
                with self.subTest(redis=redis, callback=callback.__name__):
                    self.context.args = ["вопрос"]
                    update = make_update(text)
                    with patch.object(handlers, "OpenAI") as ai, patch.object(gm.logger, "warning"):
                        ai.return_value.responses.create.return_value.output_text = "Ответ"
                        await callback(update, self.context)
                    update.effective_message.reply_text.assert_awaited_once_with("Ответ")

    async def test_ai_failure_keeps_game_memory_unchanged(self):
        await gm.observe(make_update(), self.context)
        before = await gm.read_facts(self.redis, 100)
        self.context.args = ["вопрос"]
        for callback, text in ((handlers.ask, "/ask вопрос"),
                               (handlers.mention_ai, "@RedstoneBot вопрос")):
            update = make_update(text)
            with patch.object(handlers, "OpenAI") as ai, self.assertLogs(handlers.logger, level="ERROR"):
                ai.return_value.responses.create.side_effect = RuntimeError("OpenAI down")
                await callback(update, self.context)
            self.assertIn("Не удалось", update.effective_message.reply_text.call_args.args[0])
        self.assertEqual(await gm.read_facts(self.redis, 100), before)


if __name__ == "__main__":
    unittest.main()
