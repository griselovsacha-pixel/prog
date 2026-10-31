"""Проверки модуля алертов (alerts.py) с поддельным ботом, без сети:
    python -m unittest discover -s tests -v
"""
import asyncio
import logging
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import alerts  # noqa: E402


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat, text, parse_mode=None):
        self.sent.append((chat, text, parse_mode))


class AlertTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        alerts._last.clear()
        alerts._limiter = alerts.sec.Limiter(20, 600)
        alerts._dropped = 0
        alerts._pending.clear()
        alerts.MUTE.clear()
        alerts.ENABLED = True
        self.bot = FakeBot()
        alerts.start(self.bot, 42, "Europe/Kyiv")

    async def asyncTearDown(self):
        await alerts.stop()

    async def test_delivers_and_batches_burst(self):
        for i in range(3):
            alerts.notify("user", "Новый пользователь", f"ID {i}")
        await alerts.stop()
        self.assertEqual(len(self.bot.sent), 1)             # всплеск склеен в одно сообщение
        chat, text, mode = self.bot.sent[0]
        self.assertEqual((chat, mode), (42, "HTML"))
        for i in range(3):
            self.assertIn(f"ID {i}", text)

    async def test_cooldown_dedupes(self):
        for _ in range(5):
            alerts.notify("admin", "Неверный пароль", "IP 1.2.3.4", key="pw", cooldown=60)
        await alerts.stop()
        self.assertEqual(self.bot.sent[0][1].count("Неверный пароль"), 1)

    async def test_secrets_are_scrubbed(self):
        os.environ["BOT_TOKEN"] = "123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        os.environ["ADMIN_PASSWORD"] = "supersecretpassword123"
        try:
            alerts.notify("error", "Сбой", "postgresql://user:hunter2pass@db.example.com:6543/x "
                          "pw=supersecretpassword123 ?k=abcdefghijklmnop")
            await alerts.stop()
            text = self.bot.sent[0][1]
            for leak in ("hunter2pass", "supersecretpassword123", "abcdefghijklmnop"):
                self.assertNotIn(leak, text)
        finally:
            os.environ.pop("BOT_TOKEN"); os.environ.pop("ADMIN_PASSWORD")

    async def test_html_is_escaped(self):
        alerts.notify("user", "<b>Вася</b>", "<script>alert(1)</script>")
        await alerts.stop()
        text = self.bot.sent[0][1]
        self.assertNotIn("<script>", text)
        self.assertIn("&lt;script&gt;", text)

    async def test_mute_and_disable(self):
        alerts.MUTE.add("user")
        alerts.notify("user", "тихо")
        alerts.notify("ban", "громко")
        await alerts.stop()
        joined = " ".join(m[1] for m in self.bot.sent)
        self.assertNotIn("тихо", joined)
        self.assertIn("громко", joined)

    async def test_flood_is_capped_and_reported(self):
        alerts._limiter = alerts.sec.Limiter(1, 600)        # одно сообщение за окно
        alerts.notify("ban", "первое")
        await asyncio.sleep(2.2)                            # первое ушло
        alerts.notify("ban", "второе")
        await asyncio.sleep(2.2)                            # второе отброшено лимитом
        self.assertEqual(len(self.bot.sent), 1)
        self.assertEqual(alerts._dropped, 1)

    async def test_error_log_is_forwarded(self):
        lg = logging.getLogger("test.alerts.forward")
        h = alerts.ErrorForwarder(level=logging.ERROR)
        lg.addHandler(h)
        lg.propagate = False
        try:
            try:
                raise ValueError("boom")
            except ValueError:
                lg.exception("что-то сломалось")
            lg.warning("это предупреждение не уходит")
            await alerts.stop()
        finally:
            lg.removeHandler(h)
        text = " ".join(m[1] for m in self.bot.sent)
        self.assertIn("ValueError", text)
        self.assertIn("что-то сломалось", text)
        self.assertNotIn("предупреждение", text)

    async def test_events_before_start_are_kept(self):
        await alerts.stop()
        alerts._loop = None
        alerts.notify("start", "Ранний алерт")
        bot2 = FakeBot()
        alerts.start(bot2, 7, "UTC")
        await alerts.stop()
        self.assertIn("Ранний алерт", bot2.sent[0][1])

    async def test_stats_counter(self):
        alerts.take_stats()
        alerts.notify("ban", "a"); alerts.notify("ban", "b"); alerts.notify("error", "c")
        self.assertEqual(alerts.take_stats(), {"ban": 2, "error": 1})


if __name__ == "__main__":
    unittest.main()
