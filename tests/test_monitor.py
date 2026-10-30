"""Проверки автопроверки (monitor.py) без внешней сети:
    python -m unittest discover -s tests -v
"""
import asyncio
import json
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

from aiohttp import web

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import monitor as mon  # noqa: E402

mon.ALLOW_PRIVATE = True
mon.POLL = 0.02
mon.SEND_GAP = 0
mon.SITE_TIMEOUT = 2
mon.RETRY_PAUSE = 0

TOOLS = json.loads((Path(ROOT) / "data" / "tools.json").read_text("utf-8"))


class ParseTests(unittest.TestCase):
    def test_username(self):
        f = mon.tg_username
        self.assertEqual(f("https://t.me/SomeBot"), "SomeBot")
        self.assertEqual(f("https://t.me/SomeBot?start=abc"), "SomeBot")
        self.assertEqual(f("t.me/some_bot"), "some_bot")
        self.assertEqual(f("https://telegram.me/xbot/"), "xbot")
        self.assertEqual(f("tg://resolve?domain=abc_bot"), "abc_bot")
        self.assertIsNone(f("https://t.me/+AbCdEf123"))
        self.assertIsNone(f("https://t.me/joinchat/AAAA"))
        self.assertIsNone(f("https://t.me/c/123/4"))
        self.assertIsNone(f("https://example.com/bot"))
        self.assertIsNone(f(""))

    def test_collect_catalog(self):
        items = mon.collect(TOOLS)
        self.assertGreater(len(items), 20)
        keys = [i["tool"] for i in items]
        self.assertEqual(len(keys), len(set(keys)), "ключи инструментов должны быть уникальны")
        self.assertIn("Поиск данных › Telegram bots › Sherlock", keys)
        for i in items:
            self.assertNotIn(i["tool"].split(" › ")[0], ("Harledy Project",))
            if i["kind"] == "bot":
                self.assertTrue(i["username"])
        kinds = {i["kind"] for i in items}
        self.assertTrue({"bot", "site"} <= kinds)


class SiteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        async def ok(r): return web.Response(text="<html>" + "x" * 300 + "</html>", content_type="text/html")
        async def empty(r): return web.Response(body=b"")
        async def nf(r): return web.Response(status=404, text="no")
        async def boom(r): return web.Response(status=502, text="bad")
        async def forb(r): return web.Response(status=403, text="Just a moment...", headers={"Server": "cloudflare"})
        async def slow(r):
            await asyncio.sleep(10)
            return web.Response(text="late")
        async def redir(r): raise web.HTTPFound("/ok")
        app = web.Application()
        for n, h in dict(ok=ok, empty=empty, nf=nf, boom=boom, forb=forb, slow=slow, redir=redir).items():
            app.router.add_get("/" + n, h)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.base = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
        import aiohttp
        self.s = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=mon.SITE_TIMEOUT))

    async def asyncTearDown(self):
        await self.s.close()
        await self.runner.cleanup()

    async def test_ok_and_redirect(self):
        for path in ("/ok", "/redir"):
            r = await mon.check_site(self.s, self.base + path)
            self.assertEqual((r.state, r.message), (mon.OK, mon.MSG_SITE_OK), path)

    async def test_errors_explain_what_to_do(self):
        r = await mon.check_site(self.s, self.base + "/nf")
        self.assertEqual((r.state, r.code), (mon.SITE_ERROR, 404))
        self.assertIn("Ошибка 404", r.message)
        self.assertIn("Что делать", r.message)
        r = await mon.check_site(self.s, self.base + "/boom")
        self.assertEqual((r.state, r.code), (mon.SITE_ERROR, 502))
        r = await mon.check_site(self.s, self.base + "/empty")
        self.assertEqual(r.state, mon.SITE_ERROR)
        self.assertIn("пустая", r.message)

    async def test_cloudflare_is_not_dead(self):
        r = await mon.check_site(self.s, self.base + "/forb")
        self.assertEqual(r.state, mon.LIMITED)
        self.assertNotIn(r.state, mon.BAD)

    async def test_timeout_and_dns(self):
        r = await mon.check_site(self.s, self.base + "/slow")
        self.assertEqual(r.state, mon.SITE_ERROR)
        self.assertIn("не ответил", r.message)
        r = await mon.check_site(self.s, "http://no-such-host-harledy.invalid/")
        self.assertEqual(r.state, mon.SITE_ERROR)
        self.assertIn("DNS", r.message)

    async def test_internal_addresses_skipped_in_prod(self):
        mon.ALLOW_PRIVATE = False
        try:
            r = await mon.check_site(self.s, "http://127.0.0.1:1/")
            self.assertEqual(r.state, mon.UNKNOWN)
        finally:
            mon.ALLOW_PRIVATE = True


class FakeClient:
    """Подделка Telethon-клиента: бот либо отвечает, либо молчит, либо удалён."""

    def __init__(self, behaviour):
        self.behaviour, self.sent, self.n = behaviour, [], 100
        self.probe = None

    async def __call__(self, req):
        from telethon import errors
        name = type(req).__name__
        if name == "ResolveUsernameRequest":
            b = self.behaviour.get(req.username)
            if b == "deleted":
                raise errors.UsernameNotOccupiedError(request=req)
            if b == "channel":
                return SimpleNamespace(users=[], chats=[object()])
            if b == "person":
                return SimpleNamespace(users=[SimpleNamespace(id=7, bot=False, deleted=False)], chats=[])
            return SimpleNamespace(users=[SimpleNamespace(id=hash(req.username) % 1000 + 1, bot=True, deleted=False)], chats=[])
        return None

    async def send_message(self, user, text):
        self.n += 1
        self.sent.append((user.id, text))
        mine = SimpleNamespace(id=self.n)
        name = next(k for k, v in self.behaviour.items() if hash(k) % 1000 + 1 == user.id)
        if self.behaviour[name] == "reply":
            ev = SimpleNamespace(is_private=True, sender_id=user.id, message=SimpleNamespace(id=self.n + 1))
            asyncio.get_running_loop().call_later(0.05, lambda: asyncio.ensure_future(self.probe.on_event(ev)))
        return mine


class BotTests(unittest.IsolatedAsyncioTestCase):
    async def test_states(self):
        mon.BOT_WAIT = 0.4
        c = FakeClient({"alive_bot": "reply", "silent_bot": "silent", "gone_bot": "deleted", "chan": "channel", "human": "person"})
        c.probe = mon.BotProbe(c)
        out = {n: await c.probe.probe(n) for n in c.behaviour}
        self.assertEqual((out["alive_bot"].state, out["alive_bot"].message), (mon.OK, mon.MSG_BOT_OK))
        self.assertEqual((out["silent_bot"].state, out["silent_bot"].message), (mon.NO_REPLY, mon.MSG_BOT_NO_REPLY))
        self.assertEqual((out["gone_bot"].state, out["gone_bot"].message), (mon.DELETED, mon.MSG_BOT_DELETED))
        self.assertEqual(out["chan"].state, mon.OK)
        self.assertEqual(out["human"].state, mon.OK)
        self.assertEqual([t for _, t in c.sent], ["/start", "/start"])      # /start только живым ботам

    async def test_old_message_is_not_a_reply(self):
        mon.BOT_WAIT = 0.3
        c = FakeClient({"silent_bot": "silent"})
        c.probe = mon.BotProbe(c)
        uid = hash("silent_bot") % 1000 + 1
        await c.probe.on_event(SimpleNamespace(is_private=True, sender_id=uid, message=SimpleNamespace(id=5)))   # старое сообщение
        r = await c.probe.probe("silent_bot")
        self.assertEqual(r.state, mon.NO_REPLY)


class ReportTests(unittest.TestCase):
    def test_report_counts(self):
        rows = {
            "A › Bot1": dict(state="no_reply", code=None, fails=3),
            "A › Bot2": dict(state="ok", code=None, fails=0),
            "B › Site<1>": dict(state="site_error", code=404, fails=1),
            "B › Bot3": dict(state="deleted", code=None, fails=1),
        }
        prev = {"A › Bot2": {"state": "no_reply"}, "A › Bot1": {"state": "no_reply"}}
        txt = mon.build_report(rows, prev, set(rows), 4, None)
        self.assertIn("Активны: 1 из 4", txt)
        self.assertIn("Бот не отвечает (1): Bot1, 3-й день", txt)
        self.assertIn("Site&lt;1&gt; (404)", txt)            # имена экранируются: отчёт уходит в HTML-режиме
        self.assertIn("Новые проблемы: Site&lt;1&gt;, Bot3", txt)
        self.assertIn("Снова работают: Bot2", txt)
        self.assertIn("Боты не проверены", mon.build_report(rows, prev, set(), 4, "нет сессии"))


if __name__ == "__main__":
    unittest.main()
