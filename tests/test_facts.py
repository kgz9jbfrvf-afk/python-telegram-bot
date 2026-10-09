"""Catalog, Redis/Lua, scheduler timezone, permissions and regression tests."""
import asyncio
import copy
import json
import tempfile
import time as walltime
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from fakeredis import FakeServer
from fakeredis.aioredis import FakeRedis
from redis.exceptions import ConnectionError
from telegram import User
from bot import facts, handlers
from bot.config import Settings
from bot.main import build_application
import test_handlers
from test_game_memory import make_update


class CatalogTests(unittest.TestCase):
    def test_reviewed_catalog_is_diverse_and_sources_are_real_metadata(self):
        catalog = facts.load_catalog()
        self.assertGreaterEqual(len(catalog), 100)
        self.assertEqual({f['category'] for f in catalog.values()}, facts.CATEGORIES)
        self.assertGreaterEqual(len({f['source'] for f in catalog.values()}), 20)
        self.assertGreater(sum(f['edition'] != 'Java' for f in catalog.values()), 90)
        self.assertIn('ps5-bedrock-crossplay', catalog)
        for fact in catalog.values():
            self.assertTrue(fact['source'].startswith('https://'))
            self.assertTrue(fact['source_section'])
            self.assertTrue(fact['verified_on'])
            self.assertLess(len(facts.format_fact(fact)), 3800)

    def test_invalid_catalogs_are_rejected(self):
        row = next(iter(facts.load_catalog().values()))
        bad = [[], {}, [row, row]]
        for field, value in (('id','../bad'), ('source','https://example.org/invented'),
                             ('source','https://feedback.minecraft.net/hc/en-us/community/posts/idea'),
                             ('source','http://www.minecraft.net/article'), ('edition','Unknown'),
                             ('category','random'), ('verified_on','2026-99-99'), ('text',''),
                             ('joke', []), ('version', 3), ('title','x'*121)):
            bad.append([dict(row, **{field:value})])
        bad.append([dict(row, unexpected='field')])
        bad.append([row, dict(row, id='another-id')])  # duplicate body
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'facts.json'
            for rows in bad:
                with self.subTest(rows=str(rows)[:100]):
                    path.write_text(json.dumps(rows))
                    with self.assertRaises((ValueError, TypeError)):
                        facts.load_catalog(path)

    def test_format_contains_source_edition_and_separate_joke(self):
        fact = facts.load_catalog()['sheep-pink-rare']
        text = facts.format_fact(fact)
        for expected in ('🎲 Случайный факт о Minecraft', fact['title'], fact['text'],
                         '💬 Редстоун:', fact['joke'], 'Bedrock (включая PS5)', fact['version'], fact['source']):
            self.assertIn(expected, text)
        self.assertIn('Факт дня', facts.format_fact(fact, scheduled=True))

    def test_invalid_schedule_and_custom_times(self):
        for value in ('10:00', '10:00,10:00', '25:00,19:00', '10:60,19:00', '10:00,19:00,20:00', '9:00,19:00'):
            with self.assertRaises(ValueError):
                facts.schedule_config(value)
        times, zone = facts.schedule_config('09:15,20:45')
        self.assertEqual([x.strftime('%H:%M') for x in times], ['09:15','20:45'])
        self.assertEqual(zone.key,'Europe/Warsaw')
        with self.assertRaises(KeyError):
            facts.schedule_config(timezone='Invalid/Nowhere')


class RedisFactsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        server = FakeServer()
        self.redis = FakeRedis(server=server)
        self.other = FakeRedis(server=server)
        self.addAsyncCleanup(self.redis.aclose)
        self.addAsyncCleanup(self.other.aclose)
        self.catalog = facts.load_catalog()
        self.context = NS(bot_data={'redis':self.redis,'facts_catalog':self.catalog,
                                   'facts_schedule':facts.schedule_config()},
                          bot=NS(username='RedstoneBot', id=999, send_message=AsyncMock(),
                                 get_chat_member=AsyncMock(return_value=NS(status='administrator'))), args=[])
        self.update = make_update('/fact')

    async def enable(self):
        await facts.facts_on(self.update, self.context)
        self.update.effective_message.reply_text.reset_mock()

    async def test_no_repeats_full_cycle_and_shuffle(self):
        ids = []
        for _ in self.catalog:
            result = await facts.reserve(self.redis,self.catalog,100)
            self.assertEqual(result[0],'ok')
            ids.append(result[1])
            await self.redis.delete(facts.state_keys(100)[2])
        self.assertEqual(set(ids),set(self.catalog))
        self.assertNotEqual(ids,list(self.catalog))
        result = await facts.reserve(self.redis,self.catalog,100)
        self.assertNotEqual(result[1],ids[-1])
        self.assertEqual(await self.redis.scard(facts.state_keys(100)[1]),1)

    async def test_manual_concurrency_across_instances_60_seconds(self):
        results = await asyncio.gather(*(facts.reserve(self.redis if i%2 else self.other,self.catalog,100) for i in range(20)))
        self.assertEqual(sum(r[0]=='ok' for r in results),1)
        self.assertEqual(sum(r[0]=='limited' for r in results),19)
        self.assertEqual(await self.redis.ttl(facts.state_keys(100)[2]),60)
        future = walltime.time()+61
        with patch('time.time',return_value=future):
            self.assertEqual((await facts.reserve(self.other,self.catalog,100))[0],'ok')

    async def test_manual_scheduled_share_history_and_select_distinct_facts(self):
        await self.enable()
        results = await asyncio.gather(facts.reserve(self.redis,self.catalog,100),
                                      facts.reserve(self.other,self.catalog,100,'2026-10-09:0'))
        self.assertTrue(all(r[0]=='ok' for r in results))
        self.assertNotEqual(results[0][1],results[1][1])
        self.assertEqual(await self.redis.scard(facts.state_keys(100)[1]),2)
        self.assertEqual((await facts.reserve(self.redis,self.catalog,100,'2026-10-09:1'))[0],'ok')
        self.assertEqual(await self.redis.scard(facts.state_keys(100)[1]),3)

    async def test_same_slot_only_one_send_across_instances_and_restart(self):
        await self.enable()
        other_context = NS(bot_data=dict(self.context.bot_data,redis=self.other),bot=self.context.bot)
        await asyncio.gather(*(facts.send_fact(self.context if i%2 else other_context,100,'2026-10-09:0') for i in range(20)))
        self.context.bot.send_message.assert_awaited_once()
        await facts.send_fact(other_context,100,'2026-10-09:0')  # new process uses retained reservation
        self.context.bot.send_message.assert_awaited_once()
        await facts.send_fact(other_context,100,'2026-10-09:1')
        self.assertEqual(self.context.bot.send_message.await_count,2)
        self.assertGreater(await self.redis.ttl(facts.state_keys(100,'2026-10-09:0')[3]),2*86400)

    async def test_catalog_update_removes_old_ids_and_uses_new_before_cycle_reset(self):
        await self.redis.sadd(facts.state_keys(100)[1],*self.catalog,'removed-id')
        catalog=dict(self.catalog, new_id={})
        result=await facts.reserve(self.redis,catalog,100)
        self.assertEqual(result[1],'new_id')
        self.assertFalse(await self.redis.sismember(facts.state_keys(100)[1],'removed-id'))
        self.assertLessEqual(await self.redis.scard(facts.state_keys(100)[1]),len(catalog))
        await self.redis.delete(facts.state_keys(100)[2])
        reduced={key:self.catalog[key] for key in list(self.catalog)[:3]}
        await facts.reserve(self.redis,reduced,100)
        self.assertLessEqual(await self.redis.scard(facts.state_keys(100)[1]),3)

    async def test_history_and_limits_are_group_isolated_and_old_memories_untouched(self):
        await self.redis.set('redstone:profile:100:1','existing')
        await self.redis.set('redstone:activity:{100}:cooldown','existing',ex=1800)
        for chat in (100,200):
            self.assertEqual((await facts.reserve(self.redis,self.catalog,chat))[0],'ok')
        self.assertEqual(await self.redis.scard(facts.state_keys(100)[1]),1)
        self.assertEqual(await self.redis.scard(facts.state_keys(200)[1]),1)
        self.assertEqual(await self.redis.get('redstone:profile:100:1'),b'existing')
        self.assertEqual(await self.redis.get('redstone:activity:{100}:cooldown'),b'existing')

    async def test_disabled_default_manual_available_and_mode_independent(self):
        await facts.send_fact(self.context,100,'2026-10-09:0')
        self.context.bot.send_message.assert_not_awaited()
        await facts.fact_command(self.update,self.context)
        self.assertIn('Случайный факт',self.update.effective_message.reply_text.call_args.args[0])
        await self.enable()
        await self.redis.set('redstone:activity:{100}:mode','off')
        await self.redis.set('redstone:activity:{100}:cooldown','busy',ex=1800)
        await facts.send_fact(self.context,100,'2026-10-09:0')
        self.context.bot.send_message.assert_awaited_once()

    async def test_admin_on_off_status_and_preserved_reservations(self):
        await facts.facts_status(self.update,self.context)
        self.assertIn('выключены',self.update.effective_message.reply_text.call_args.args[0])
        await self.enable()
        self.assertTrue(await self.redis.sismember(facts.ENABLED_GROUPS,'100'))
        await facts.facts_status(self.update,self.context)
        text=self.update.effective_message.reply_text.call_args.args[0]
        for expected in ('включены','10:00','19:00','Europe/Warsaw'): self.assertIn(expected,text)
        await facts.send_fact(self.context,100,'2026-10-09:0')
        await facts.facts_off(self.update,self.context)
        self.assertFalse(await self.redis.sismember(facts.ENABLED_GROUPS,'100'))
        await self.enable()
        await facts.send_fact(self.context,100,'2026-10-09:0')
        self.context.bot.send_message.assert_awaited_once()

    async def test_member_and_unavailable_rights_cannot_change_mode(self):
        for status in ('member','restricted','left','kicked'):
            self.context.bot.get_chat_member.return_value=NS(status=status)
            await facts.facts_on(self.update,self.context)
            self.assertIsNone(await self.redis.get(facts.state_keys(100)[0]))
        self.context.bot.get_chat_member.side_effect=RuntimeError('no rights API')
        await facts.facts_on(self.update,self.context)
        self.assertFalse(await self.redis.sismember(facts.ENABLED_GROUPS,'100'))
        self.context.bot.get_chat_member.side_effect=None
        self.context.bot.get_chat_member.return_value=NS(status='creator')
        await self.enable()
        self.context.bot.get_chat_member.return_value=NS(status='member')
        await facts.facts_off(self.update,self.context)
        self.assertNotEqual(await self.redis.get(facts.state_keys(100)[0]),b'off')

    async def test_telegram_error_consumes_slot_and_fact_without_retry(self):
        await self.enable()
        self.context.bot.send_message.side_effect=RuntimeError('timeout after possible delivery')
        await facts.send_fact(self.context,100,'2026-10-09:0')
        await facts.send_fact(self.context,100,'2026-10-09:0')
        self.context.bot.send_message.assert_awaited_once()
        self.assertEqual(await self.redis.scard(facts.state_keys(100)[1]),1)
        self.update.effective_message.reply_text.side_effect=RuntimeError('Telegram unavailable')
        await facts.fact_command(self.update,self.context)
        self.assertEqual(await self.redis.scard(facts.state_keys(100)[1]),2)

    async def test_no_redis_or_failed_redis_fail_closed(self):
        self.context.bot_data['redis']=None
        await facts.fact_command(self.update,self.context)
        await facts.send_fact(self.context,100,'date:0')
        await facts.facts_on(self.update,self.context)
        self.context.bot.send_message.assert_not_awaited()
        self.assertIn('недоступен',self.update.effective_message.reply_text.call_args.args[0])
        self.context.bot_data['redis']=NS(eval=AsyncMock(side_effect=ConnectionError()))
        await facts.fact_command(self.update,self.context)
        await facts.send_fact(self.context,100,'date:0')
        self.context.bot.send_message.assert_not_awaited()

    async def test_off_during_preparation_suppresses_send(self):
        await self.enable()
        original=facts.reserve
        async def reserve_and_disable(*args):
            result=await original(*args)
            await facts.facts_off(self.update,self.context)
            return result
        with patch.object(facts,'reserve',side_effect=reserve_and_disable):
            await facts.send_fact(self.context,100,'2026-10-09:0')
        self.context.bot.send_message.assert_not_awaited()

    async def test_fact_does_not_use_openai_and_button_shares_command_limit(self):
        with patch.object(handlers,'OpenAI') as ai:
            self.update.effective_message.text=handlers.MENU_FACT
            await handlers.menu_button(self.update,self.context)
            self.assertIn('Случайный факт',self.update.effective_message.reply_text.call_args.args[0])
            await facts.fact_command(self.update,self.context)
            self.assertIn('60 секунд',self.update.effective_message.reply_text.call_args.args[0])
            ai.assert_not_called()

    async def test_bots_channels_and_edits_rejected_and_private_settings_explained(self):
        for update in (make_update('/fact',is_bot=True),make_update('/fact',edited=True),
                       make_update('/fact',sender_chat=NS(id=4))):
            await facts.fact_command(update,self.context)
            update.effective_message.reply_text.assert_not_awaited()
        private=make_update('/facts_on',chat_type='private')
        await facts.facts_on(private,self.context)
        self.context.bot.get_chat_member.assert_not_awaited()
        self.assertIn('только в группе',private.effective_message.reply_text.call_args.args[0])


class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def make_app(self, **kwargs):
        app=build_application(Settings('123456:test','', '', **kwargs))
        self.addAsyncCleanup(app.bot.shutdown)
        return app

    async def test_exactly_two_daily_jobs_and_restart_does_not_send(self):
        for _ in range(2):
            app=await self.make_app()
            jobs=app.job_queue.jobs()
            self.assertEqual(len(jobs),2)
            self.assertEqual({j.data for j in jobs},{0,1})
            self.assertEqual(len(app.bot_data['facts_catalog']),105)
            self.assertEqual(app.job_queue.scheduler.running,False)
            for job in jobs:
                self.assertEqual(job.job.trigger.timezone.key,'Europe/Warsaw')
                self.assertEqual(job.job.misfire_grace_time,300)
                self.assertTrue(job.job.coalesce)
                self.assertEqual(job.job.max_instances,1)

    async def test_cron_triggers_keep_local_hours_across_dst(self):
        app=await self.make_app()
        zone=ZoneInfo('Europe/Warsaw')
        for date, offset in (('2026-03-28',1),('2026-03-29',2),('2026-10-24',2),('2026-10-25',1)):
            for job,hour in zip(app.job_queue.jobs(),(10,19)):
                start=datetime.fromisoformat(date).replace(tzinfo=zone)
                fire=job.job.trigger.get_next_fire_time(None,start)
                self.assertEqual(fire.hour,hour)
                self.assertEqual(fire.minute,0)
                self.assertEqual(fire.utcoffset(),timedelta(hours=offset))
                self.assertEqual(facts.publication_id(fire,job.data,app.bot_data['facts_schedule']),date+':'+str(job.data))

    async def test_late_jobs_and_other_slots_have_no_backlog(self):
        schedule=facts.schedule_config()
        zone=ZoneInfo('Europe/Warsaw')
        for hour,minute in ((9,59),(10,6),(18,59),(19,0),(23,59)):
            now=datetime(2026,10,9,hour,minute,tzinfo=zone)
            self.assertIsNone(facts.publication_id(now,0,schedule))
        self.assertEqual(facts.publication_id(datetime(2026,10,9,10,5,tzinfo=zone),0,schedule),'2026-10-09:0')

    async def test_daily_callback_only_current_enabled_groups_and_no_redis(self):
        client=FakeRedis()
        self.addAsyncCleanup(client.aclose)
        await client.sadd(facts.ENABLED_GROUPS,'100','200','not-a-chat')
        context=NS(bot_data={'redis':client,'facts_schedule':facts.schedule_config()},job=NS(data=0))
        zone=ZoneInfo('Europe/Warsaw')
        class Clock(datetime):
            @classmethod
            def now(cls,tz=None): return datetime(2026,10,9,10,0,tzinfo=zone).astimezone(tz)
        with patch.object(facts,'datetime',Clock), patch.object(facts,'send_fact',new_callable=AsyncMock) as send:
            await facts.daily_job(context)
            self.assertEqual({c.args[1] for c in send.call_args_list},{100,200})
            self.assertTrue(all(c.args[2]=='2026-10-09:0' for c in send.call_args_list))
        context.bot_data['redis']=None
        await facts.daily_job(context)
        context.bot_data['redis']=NS(sscan_iter=lambda *args,**kwargs: (_ for _ in ()).throw(ConnectionError()))
        await facts.daily_job(context)

    async def test_custom_configuration_from_env(self):
        with patch.dict('os.environ',{'BOT_TOKEN':'123456:test','FACTS_TIMES':'09:15,20:45','FACTS_TIMEZONE':'Europe/Warsaw'}):
            settings=Settings.from_env()
        self.assertEqual(settings.facts_times,'09:15,20:45')
        app=await self.make_app(facts_times=settings.facts_times)
        for job,hour,minute in zip(app.job_queue.jobs(),(9,20),(15,45)):
            fire=job.job.trigger.get_next_fire_time(None,datetime(2026,1,1,tzinfo=timezone.utc))
            self.assertEqual((fire.hour,fire.minute),(hour,minute))

    async def test_command_routes_help_and_menu_priority(self):
        app=await self.make_app()
        app.bot._bot_user=User(123456,'Redstone',True,username='RedstoneBot')
        for command,callback in (('fact',facts.fact_command),('facts_on',facts.facts_on),
                                 ('facts_off',facts.facts_off),('facts_status',facts.facts_status)):
            update=test_handlers.MenuTests.make_update('/'+command,app.bot,True)
            selected=next(h for h in app.handlers[0] if h.check_update(update))
            self.assertIs(selected.callback,callback)
            self.assertIn(command,dict(handlers.BOT_COMMANDS))
            self.assertIn('/'+command,handlers.HELP_TEXT)
        update=test_handlers.MenuTests.make_update(handlers.MENU_FACT,app.bot)
        self.assertIs(next(h for h in app.handlers[0] if h.check_update(update)).callback,handlers.menu_button)
        self.assertIn(handlers.MENU_FACT,[button.text for row in handlers.MAIN_MENU_KEYBOARD.keyboard for button in row])

class DSTEdgeTests(unittest.TestCase):
    def test_nonexistent_configured_time_is_skipped(self):
        schedule = facts.schedule_config('02:30,19:00')
        now = datetime(2026, 3, 29, 1, 30, tzinfo=timezone.utc)
        self.assertIsNone(facts.publication_id(now, 0, schedule))

    def test_repeated_hour_only_uses_first_occurrence(self):
        schedule = facts.schedule_config('02:30,19:00')
        self.assertEqual(facts.publication_id(datetime(2026,10,25,0,30,tzinfo=timezone.utc),0,schedule), '2026-10-25:0')
        self.assertIsNone(facts.publication_id(datetime(2026,10,25,1,30,tzinfo=timezone.utc),0,schedule))
