"""Проверки защитного слоя (security.py). Только стандартная библиотека:
    python -m unittest discover -s tests -v
"""
import asyncio
import importlib
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def load(**env):
    for k in ("PROXY_HOPS", "IP_HEADER", "ORIGIN_SECRET", "ALLOWED_HOSTS", "MAX_BODY"):
        os.environ.pop(k, None)
    os.environ.update(env)
    import security
    return importlib.reload(security)


async def _stub(scope, receive, send):
    await receive()
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


async def call(sec, method="GET", path="/", headers=None, body=b"", peer="10.0.0.1", shield=None):
    shield = shield or sec.Shield(_stub)
    scope = {"type": "http", "method": method, "path": path, "client": (peer, 1234),
             "headers": [(k.lower().encode(), v.encode("latin-1")) for k, v in (headers or {}).items()]}
    sent = []

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(m):
        sent.append(m)

    await shield(scope, receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    return start["status"], {k.decode(): v.decode() for k, v in start["headers"]}


class ShieldTests(unittest.TestCase):
    def run_(self, coro):
        return asyncio.run(coro)

    def test_security_headers_present(self):
        sec = load()
        st, h = self.run_(call(sec, path="/index.html"))
        self.assertEqual(st, 200)
        for name in ("x-content-type-options", "x-frame-options", "content-security-policy",
                     "referrer-policy", "permissions-policy"):
            self.assertIn(name, h)
        self.assertIn("frame-ancestors 'none'", h["content-security-policy"])

    def test_api_not_cached(self):
        sec = load()
        _, h = self.run_(call(sec, path="/api/tools"))
        self.assertEqual(h["cache-control"], "no-store")

    def test_forged_xff_cannot_pick_ip(self):
        """Клиент подставляет левый адрес слева в X-Forwarded-For: берётся запись с конца цепочки."""
        sec = load(PROXY_HOPS="2")
        ip = sec.ip_from_scope({"headers": [(b"x-forwarded-for", b"1.2.3.4, 203.0.113.7, 10.9.9.9")],
                                "client": ("10.0.0.1", 1)})
        self.assertEqual(ip, "203.0.113.7")

    def test_forged_cf_header_ignored_by_default(self):
        sec = load()
        ip = sec.ip_from_scope({"headers": [(b"cf-connecting-ip", b"91.224.45.1")], "client": ("198.51.100.5", 1)})
        self.assertEqual(ip, "198.51.100.5")

    def test_rate_limit_then_ban(self):
        sec = load()
        shield = sec.Shield(_stub)
        codes = [self.run_(call(sec, path="/api/tools", peer="198.51.100.9", shield=shield))[0] for _ in range(60)]
        self.assertEqual(codes[:20], [200] * 20)
        self.assertIn(429, codes)
        # после серии нарушений адрес забанен целиком, даже на других путях
        self.assertEqual(self.run_(call(sec, path="/", peer="198.51.100.9", shield=shield))[0], 429)
        # чужой адрес не задет
        self.assertEqual(self.run_(call(sec, path="/", peer="198.51.100.10", shield=shield))[0], 200)

    def test_methods_filtered(self):
        sec = load()
        self.assertEqual(self.run_(call(sec, method="TRACE"))[0], 405)
        self.assertEqual(self.run_(call(sec, method="DELETE"))[0], 405)

    def test_body_limit(self):
        sec = load(MAX_BODY="1024")
        st, _ = self.run_(call(sec, method="POST", path="/api/track", headers={"content-length": "5000"}))
        self.assertEqual(st, 413)

    def test_ai_endpoint_gets_bigger_body_limit_only(self):
        sec = load(MAX_BODY="1024")
        st, _ = self.run_(call(sec, method="POST", path="/api/ai", headers={"content-length": "9000"}))
        self.assertEqual(st, 200)          # вопрос + история диалога
        st, _ = self.run_(call(sec, method="POST", path="/api/ai", headers={"content-length": "90000"}))
        self.assertEqual(st, 413)
        st, _ = self.run_(call(sec, method="POST", path="/api/track", headers={"content-length": "9000"}))
        self.assertEqual(st, 413)          # остальные ручки по-прежнему 1 КБ

    def test_weird_content_length_does_not_crash(self):
        sec = load()
        for cl in ("²", "-1", "1e3", "99999999999999999999"):
            st, _ = self.run_(call(sec, method="POST", path="/api/track", headers={"content-length": cl},
                                   peer="198.51.100." + str(abs(hash(cl)) % 200 + 20)))
            self.assertEqual(st, 413, cl)

    def test_origin_secret_enforced(self):
        sec = load(ORIGIN_SECRET="s3cret")
        self.assertEqual(self.run_(call(sec, path="/"))[0], 403)
        self.assertEqual(self.run_(call(sec, path="/", headers={"x-origin-secret": "wrong"}))[0], 403)
        self.assertEqual(self.run_(call(sec, path="/", headers={"x-origin-secret": "s3cret"}))[0], 200)
        self.assertEqual(self.run_(call(sec, path="/ping"))[0], 200)       # health check Render идёт без секрета

    def test_allowed_hosts(self):
        sec = load(ALLOWED_HOSTS="example.com")
        self.assertEqual(self.run_(call(sec, headers={"host": "evil.test"}))[0], 400)
        self.assertEqual(self.run_(call(sec, headers={"host": "about.example.com"}))[0], 200)

    def test_inflight_shedding(self):
        sec = load()
        sec.MAX_INFLIGHT = 0
        self.assertEqual(self.run_(call(sec, path="/"))[0], 503)
        self.assertEqual(self.run_(call(sec, path="/ping"))[0], 200)

    def test_ipv6_grouped_by_64(self):
        sec = load()
        self.assertEqual(sec.rl_key("2001:db8:1:2:aaaa::1"), sec.rl_key("2001:db8:1:2:bbbb::9"))

    def test_exempt_admin_ipv6_never_banned(self):
        import ipaddress
        sec = load()
        sec.EXEMPT_NETS = [ipaddress.ip_network("2001:db8:1:2::/64")]
        key = sec.rl_key("2001:db8:1:2::5")
        for _ in range(100):
            self.assertFalse(sec.BANS.strike(key, "x", 5))

    def test_log_masking(self):
        sec = load()
        self.assertNotIn("abcdefghijklmnop", sec._mask("GET /?k=abcdefghijklmnop HTTP/1.1"))
        self.assertNotIn("AAAA", sec._mask("bot 123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"))

    def test_admin_csp_hashes_inline(self):
        sec = load()
        csp = sec.admin_csp("<script>alert(1)</script><style>a{}</style><script src=x></script>")
        self.assertIn("'sha256-", csp)
        self.assertNotIn("'unsafe-inline'; img", csp.split("style-src-attr")[0])


if __name__ == "__main__":
    unittest.main()
