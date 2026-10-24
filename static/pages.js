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

/* ---------- оформление «О нас» и «Сотрудничество» ---------- */
.hero{position:relative;margin-bottom:40px}
.hero::before{content:"";position:absolute;left:-60px;top:-70px;width:340px;height:340px;z-index:-1;pointer-events:none;
  background:radial-gradient(circle,rgba(79,134,179,.22),transparent 68%)}
.eyebrow{display:inline-flex;align-items:center;gap:8px;font-size:12px;font-weight:700;letter-spacing:.2em;text-transform:uppercase;
  color:var(--accent);padding:5px 12px;border:1px solid var(--line);border-radius:99px;margin-bottom:18px}
.eyebrow::before{content:"";width:7px;height:7px;border-radius:50%;background:var(--accent)}
.hero h1{margin-bottom:20px}
.sec{font-size:13px;font-weight:700;letter-spacing:.14em;text-transform:uppercase;color:var(--mute);margin:42px 0 14px}
.cards{display:grid;grid-template-columns:repeat(2,1fr);gap:14px}
.cards.three{grid-template-columns:repeat(3,1fr)}
.cards .card{margin:0;transition:transform .2s,border-color .2s}
.cards .card:hover{transform:translateY(-3px);border-color:var(--accent)}
.card h3{font-size:16px;color:var(--fg-h);margin-bottom:6px}
.ico{width:42px;height:42px;display:grid;place-items:center;border-radius:12px;margin-bottom:14px;
  color:var(--accent);background:rgba(79,134,179,.12);border:1px solid rgba(79,134,179,.28)}
.ico svg{width:22px;height:22px}
.note{margin-top:16px;padding:14px 18px;border-left:3px solid var(--accent);border-radius:0 12px 12px 0;
  background:rgba(79,134,179,.08);font-size:14px;color:var(--fg)}
.box{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:20px 24px;margin-bottom:14px}
.price{display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap;margin-bottom:6px;
  padding:26px 28px;border-radius:20px;border:1px solid rgba(79,134,179,.45);
  background:linear-gradient(135deg,rgba(79,134,179,.16),rgba(79,134,179,.03))}
.amount{display:flex;align-items:baseline;gap:12px}
.amount b{font-size:60px;line-height:1;font-weight:800;color:var(--fg-h)}
.amount span{font-size:20px;color:var(--fg-h)}
.price-sub{font-size:13px;color:var(--mute);border:1px solid var(--line);border-radius:99px;padding:6px 14px}
.steps{list-style:none;padding:0;counter-reset:s;display:grid;gap:12px}
.steps li{counter-increment:s;position:relative;display:grid;gap:2px;padding:16px 20px 16px 64px;
  background:var(--card);border:1px solid var(--line);border-radius:16px}
.steps li::before{content:counter(s);position:absolute;left:18px;top:50%;transform:translateY(-50%);width:32px;height:32px;
  display:grid;place-items:center;border-radius:50%;font-weight:800;color:var(--accent);border:1px solid rgba(79,134,179,.5)}
.steps b{color:var(--fg-h);font-size:15px}
.steps span{font-size:14px}
@media (max-width:640px){.cards,.cards.three{grid-template-columns:1fr}.amount b{font-size:48px}}
