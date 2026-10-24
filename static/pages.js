:root{--bg:#1b1b1b;--card:#222;--fg:#b4b4b4;--fg-h:#fff;--line:#363636;--mute:#8a8a8a;--accent:#4f86b3}
:root[data-theme="light"]{--bg:#f4f4f2;--card:#fff;--fg:#555;--fg-h:#000;--line:#dcdcdc;--mute:#777;--accent:#2f6fa5}
*{box-sizing:border-box;margin:0}
body{background:var(--bg);color:var(--fg);min-height:100vh;
  font-family:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,Arial,sans-serif;line-height:1.6}
.bar{position:fixed;top:18px;left:20px;z-index:2}
.pill{display:inline-block;color:var(--fg);text-decoration:none;font-size:13px;padding:7px 14px;
  border:1px solid var(--line);border-radius:8px;background:var(--bg);transition:color .2s,border-color .2s}
.pill:hover{color:var(--fg-h);border-color:var(--fg)}
.wrap{max-width:720px;margin:0 auto;padding:100px 24px 90px}
.wrap>*{animation:rise .7s both;animation-delay:calc(var(--i,0)*90ms)}
@keyframes rise{from{opacity:0;transform:translateY(14px)}to{opacity:1;transform:none}}
.brand{display:flex;align-items:center;gap:10px;color:var(--fg-h);font-weight:800;font-size:18px;margin-bottom:34px}
.brand svg{width:22px;height:22px}
h1{font-size:clamp(34px,6vw,52px);line-height:1.1;font-weight:800;color:var(--fg-h);letter-spacing:-.01em;margin-bottom:22px}
.lead{font-size:19px;line-height:1.75;color:var(--fg-h);opacity:.88;margin-bottom:34px}
.card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:22px 24px;margin-bottom:14px}
.card h2{font-size:15px;font-weight:700;color:var(--fg-h);margin-bottom:6px}
.card p{font-size:16px}
.label{font-size:13px;color:var(--mute);margin-bottom:4px}
.value{font-size:22px;font-weight:700;color:var(--fg-h);text-decoration:none}
a.value:hover{color:var(--accent)}
.row{display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap}
.gap{height:12px}
.price{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap}
.price b{font-size:56px;line-height:1;font-weight:800;color:var(--fg-h)}
.price span{font-size:20px;color:var(--fg-h)}
.btn-tg{display:inline-flex;align-items:center;justify-content:center;gap:10px;color:#fff;text-decoration:none;
  font-weight:700;font-size:16px;padding:13px 24px;border-radius:12px;
  background:linear-gradient(135deg,#2aabee,#229ed9);box-shadow:0 6px 18px rgba(34,158,217,.28);
  transition:transform .15s,box-shadow .15s,filter .15s}
.btn-tg:hover{transform:translateY(-2px);box-shadow:0 10px 24px rgba(34,158,217,.38);filter:brightness(1.06)}
.btn-tg svg{width:20px;height:20px}
.cta{margin-top:26px}
@media (max-width:560px){.wrap{padding-top:90px}.btn-tg{width:100%}}
@media (prefers-reduced-motion:reduce){.wrap>*{animation:none}}

/* единый фон с главной: точечная сетка и виньетка */
body{background-image:radial-gradient(circle,var(--dot,#2c2c2c) 1.2px,transparent 1.6px);background-size:26px 26px}
:root{--dot:#2c2c2c}
:root[data-theme="light"]{--dot:#dcdcd8}
