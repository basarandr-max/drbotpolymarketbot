# -*- coding: utf-8 -*-
import json, time, sqlite3, logging, threading, urllib.request, urllib.parse, re
from datetime import datetime, timezone

TELEGRAM_TOKEN   = "8833789042:AAHTsblDfaHEzuHO5wp3uLZ-CZPbgFDmb_M"
TELEGRAM_CHAT_ID = 860803224
ODDS_API_KEY     = "BURAYA_THE_ODDS_API_KEY_GIR"
STARTING_BALANCE = 100.0
STAKE            = 5.0
MIN_EDGE         = 3.0
SCAN_INTERVAL    = 300
DB               = "arb_bot.db"

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
    handlers=[logging.FileHandler("arb_bot.log", encoding="utf-8"), logging.StreamHandler()]
)
log = logging.getLogger(__name__)

SPORTS = [
    "soccer_epl","soccer_spain_la_liga","soccer_italy_serie_a",
    "soccer_germany_bundesliga","soccer_france_ligue_one",
    "soccer_uefa_champs_league","basketball_nba","americanfootball_nfl",
    "baseball_mlb","icehockey_nhl","soccer_usa_mls",
]

def init_db():
    c = sqlite3.connect(DB)
    c.execute("""CREATE TABLE IF NOT EXISTS portfolio
                 (id INTEGER PRIMARY KEY, balance REAL, updated_at TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT, opened_at TEXT, closed_at TEXT,
        match_name TEXT, league TEXT, market_type TEXT, side TEXT,
        stake REAL, entry_price REAL, edge_pct REAL, status TEXT DEFAULT 'open', pnl REAL DEFAULT 0)""")
    c.execute("""CREATE TABLE IF NOT EXISTS seen
                 (key TEXT PRIMARY KEY, seen_at TEXT)""")
    if c.execute("SELECT COUNT(*) FROM portfolio").fetchone()[0] == 0:
        c.execute("INSERT INTO portfolio VALUES (1,?,?)", (STARTING_BALANCE, now()))
    c.commit(); c.close()

def now(): return datetime.now(timezone.utc).isoformat()
def get_bal():
    c=sqlite3.connect(DB); v=c.execute("SELECT balance FROM portfolio WHERE id=1").fetchone()[0]; c.close(); return v
def add_bal(d):
    c=sqlite3.connect(DB); c.execute("UPDATE portfolio SET balance=balance+?,updated_at=? WHERE id=1",(d,now())); c.commit(); c.close()
def open_trade(match,league,mtype,side,price,edge):
    c=sqlite3.connect(DB)
    c.execute("INSERT INTO trades (opened_at,match_name,league,market_type,side,stake,entry_price,edge_pct,status) VALUES (?,?,?,?,?,?,?,?,'open')",
              (now(),match,league,mtype,side,STAKE,price,edge))
    tid=c.lastrowid; c.commit(); c.close(); add_bal(-STAKE); return tid
def get_open():
    c=sqlite3.connect(DB)
    rows=c.execute("SELECT * FROM trades WHERE status='open' ORDER BY opened_at DESC").fetchall()
    cols=[d[0] for d in c.description]; c.close()
    return [dict(zip(cols,r)) for r in rows]
def get_stats():
    c=sqlite3.connect(DB)
    r=c.execute("SELECT COUNT(*),SUM(status='won'),SUM(status='lost'),SUM(status='open'),ROUND(SUM(pnl),2),ROUND(AVG(edge_pct),2) FROM trades").fetchone()
    c.close(); return {"total":r[0],"wins":r[1] or 0,"losses":r[2] or 0,"open":r[3] or 0,"pnl":r[4] or 0,"avg_edge":r[5] or 0}
def is_seen(k):
    c=sqlite3.connect(DB); f=c.execute("SELECT 1 FROM seen WHERE key=?",(k,)).fetchone() is not None; c.close(); return f
def mark_seen(k):
    c=sqlite3.connect(DB)
    try: c.execute("INSERT INTO seen VALUES (?,?)",(k,now())); c.commit()
    except: pass
    c.close()

def http_get(url, timeout=15):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        log.warning(f"GET hatasi {url[:60]}: {e}"); return None

def tg(text):
    clean = re.sub(r"<[^>]+>", "", text)
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = json.dumps({"chat_id": TELEGRAM_CHAT_ID, "text": clean}).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type":"application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        log.error(f"Telegram hatasi: {e}"); return None

def tg_updates(offset=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates?timeout=30"
    if offset: url += f"&offset={offset}"
    return http_get(url, timeout=35)

def fetch_poly_sports():
    markets=[]; offset=0
    kws=["vs.","vs "," v ","win","goal","over","under","total","epl","premier",
         "liga","serie","bundesliga","champions","nba","nfl","mlb","nhl","mls"]
    while True:
        d=http_get(f"https://gamma-api.polymarket.com/markets?active=true&closed=false&limit=100&offset={offset}")
        if not d or not isinstance(d,list) or len(d)==0: break
        for m in d:
            q=(m.get("question") or "").lower()
            if any(k in q for k in kws): markets.append(m)
        if len(d)<100: break
        offset+=100; time.sleep(0.3)
    log.info(f"Polymarket: {len(markets)} spor marketi"); return markets

def fetch_odds(sport):
    if ODDS_API_KEY=="BURAYA_THE_ODDS_API_KEY_GIR": return []
    url=(f"https://api.the-odds-api.com/v4/sports/{sport}/odds/"
         f"?apiKey={ODDS_API_KEY}&regions=eu,uk,us&markets=h2h,totals&oddsFormat=decimal")
    return http_get(url) or []

def match_event(question, events):
    q=question.lower()
    for ev in events:
        h=(ev.get("home_team") or "").lower()
        a=(ev.get("away_team") or "").lower()
        hw=set(h.split()); aw=set(a.split()); qw=set(q.replace("?","").split())
        if len(hw&qw)>=1 and len(aw&qw)>=1: return ev
    return None

def analyze(market, events):
    q=market.get("question",""); prices={}
    raw_p=market.get("outcomePrices"); raw_o=market.get("outcomes")
    try:
        if isinstance(raw_p,str): raw_p=json.loads(raw_p)
        if isinstance(raw_o,str): raw_o=json.loads(raw_o)
        if raw_p and raw_o:
            for i,o in enumerate(raw_o):
                if i<len(raw_p):
                    try: prices[o.lower()]=float(raw_p[i])
                    except: pass
    except: pass
    if not prices: return []

    ev=match_event(q, events)
    if not ev: return []

    opps=[]; ql=q.lower()
    is_total=any(k in ql for k in ["goal","over","under","total"])
    is_h2h=any(k in ql for k in ["win","beat","vs","v "])

    bms=ev.get("bookmakers",[])
    home=ev.get("home_team",""); away=ev.get("away_team","")
    match_name=f"{home} vs {away}"
    league=ev.get("sport_key","").replace("_"," ").upper()

    for label, poly_p in prices.items():
        if poly_p<=0.02 or poly_p>=0.98: continue

        if is_total:
            for bm in bms:
                for mkt in bm.get("markets",[]):
                    if mkt.get("key")!="totals": continue
                    for o in mkt.get("outcomes",[]):
                        oname=(o.get("name") or "").lower()
                        if oname in label or label in oname:
                            sb_prob=1/o.get("price",99)
                            edge=sb_prob-poly_p
                            if edge*100>=MIN_EDGE:
                                key=f"{market.get('id','')}-{label}-tot"
                                opps.append({"key":key,"match_name":match_name,"league":league,
                                    "market_type":f"TOTALS ({o.get('point','?')} gol)","side":label.upper(),
                                    "poly_price":poly_p,"sb_prob":round(sb_prob,4),
                                    "edge_pct":round(edge*100,2),"sportsbook":bm.get("title",""),
                                    "market_id":market.get("id","")})

        if is_h2h:
            for bm in bms:
                for mkt in bm.get("markets",[]):
                    if mkt.get("key")!="h2h": continue
                    for o in mkt.get("outcomes",[]):
                        oname=(o.get("name") or "").lower()
                        if oname in label or label in oname or label in home.lower() or label in away.lower():
                            sb_prob=1/o.get("price",99)
                            edge=sb_prob-poly_p
                            if edge*100>=MIN_EDGE:
                                key=f"{market.get('id','')}-{label}-h2h"
                                opps.append({"key":key,"match_name":match_name,"league":league,
                                    "market_type":"MAC SONUCU","side":label.upper(),
                                    "poly_price":poly_p,"sb_prob":round(sb_prob,4),
                                    "edge_pct":round(edge*100,2),"sportsbook":bm.get("title",""),
                                    "market_id":market.get("id","")})
    return opps

def run_scan():
    log.info("Tarama basliyor...")
    all_events=[]
    for sp in SPORTS:
        evs=fetch_odds(sp)
        if evs: all_events.extend(evs)
        time.sleep(0.4)
    log.info(f"Odds API: {len(all_events)} mac")

    markets=fetch_poly_sports()
    found=0
    for m in markets:
        for opp in analyze(m, all_events):
            if is_seen(opp["key"]): continue
            mark_seen(opp["key"])
            bal=get_bal()
            if bal<STAKE: continue
            tid=open_trade(opp["match_name"],opp["league"],opp["market_type"],
                           opp["side"],opp["poly_price"],opp["edge_pct"])
            new_bal=get_bal()
            e=opp["edge_pct"]; em="🔥" if e>=7 else "✅" if e>=5 else "📊"
            msg=(f"{em} ARB FIRSATI #{tid}\n"
                 f"{'='*25}\n"
                 f"⚽ {opp['match_name']}\n"
                 f"🏆 {opp['league']}\n"
                 f"📋 {opp['market_type']}\n"
                 f"➡️ {opp['side']}\n\n"
                 f"📈 Edge: +{e}%\n"
                 f"🎯 Sportsbook ({opp['sportsbook']}): %{round(opp['sb_prob']*100,1)}\n"
                 f"🟣 Polymarket: {opp['poly_price']:.2f} (¢{round(opp['poly_price']*100)})\n\n"
                 f"💵 Sanal pozisyon: ${STAKE:.0f} acildi\n"
                 f"💼 Portfolyo: ${new_bal:.2f} / ${STARTING_BALANCE:.0f}\n"
                 f"🔗 polymarket.com/event/{opp['market_id']}")
            tg(msg); found+=1
    if found==0: log.info("Yeni firsat bulunamadi.")
    else: log.info(f"{found} firsat bildirildi.")
    return found

def handle_cmd(text):
    t=text.strip().lower()
    if t=="/start":
        bal=get_bal()
        tg(f"Arbitraj Botu Aktif!\n\nSanal bakiye: ${bal:.2f}\nMin edge: %{MIN_EDGE}\nPozisyon: ${STAKE}\nTarama: {SCAN_INTERVAL//60} dakika\n\nKomutlar:\n/scan - Anlik tarama\n/portfolio - Bakiye\n/trades - Acik pozisyonlar\n/stats - Istatistikler")
    elif t in ["/portfolio","/p"]:
        bal=get_bal(); s=get_stats(); op=get_open()
        roi=round((bal-STARTING_BALANCE)/STARTING_BALANCE*100,2)
        tg(f"Portfolyo\n{'='*20}\nBakiye: ${bal:.2f}\nAcik pozisyon: {len(op)} adet (${len(op)*STAKE:.0f} kilitli)\nToplam P&L: ${s['pnl']:.2f}\nROI: {'+' if roi>=0 else ''}{roi}%\n\nKazanilan: {s['wins']}\nKaybedilen: {s['losses']}\nAcik: {s['open']}")
    elif t in ["/trades","/t"]:
        ops=get_open()
        if not ops: tg("Acik pozisyon yok.")
        else:
            msg=f"Acik Pozisyonlar ({len(ops)})\n{'='*20}\n"
            for tr in ops[:10]:
                msg+=f"\n#{tr['id']} {tr['match_name']}\n  {tr['market_type']} | {tr['side']}\n  Giris: {tr['entry_price']:.2f} | Edge: +{tr['edge_pct']}%\n"
            tg(msg)
    elif t in ["/stats","/s"]:
        s=get_stats(); bal=get_bal()
        wr=round(s['wins']/(s['wins']+s['losses'])*100,1) if s['wins']+s['losses']>0 else 0
        tg(f"Istatistikler\n{'='*20}\nToplam islem: {s['total']}\nKazanilan: {s['wins']} | Kaybedilen: {s['losses']}\nAcik: {s['open']}\nWin rate: %{wr}\nP&L: ${s['pnl']:.2f}\nOrtalama edge: %{s['avg_edge']}\nBakiye: ${bal:.2f}")
    elif t in ["/scan","/tarama"]:
        tg("Tarama baslatiliyor...")
        c=run_scan()
        tg(f"Tarama tamamlandi. {c} yeni firsat." if c else "Tarama tamamlandi. Yeni firsat bulunamadi.")
    elif t=="/help":
        tg("/start /scan /portfolio /trades /stats /help")

def polling():
    offset=None; log.info("Polling basliyor...")
    while True:
        try:
            d=tg_updates(offset)
            if d and d.get("ok"):
                for u in d.get("result",[]):
                    offset=u["update_id"]+1
                    msg=u.get("message") or u.get("edited_message")
                    if not msg: continue
                    if str(msg.get("chat",{}).get("id",""))!=str(TELEGRAM_CHAT_ID): continue
                    txt=msg.get("text","")
                    if txt.startswith("/"): handle_cmd(txt)
        except Exception as e:
            log.error(f"Polling hatasi: {e}"); time.sleep(5)

def scan_loop():
    time.sleep(20)
    while True:
        try: run_scan()
        except Exception as e: log.error(f"Scan hatasi: {e}")
        time.sleep(SCAN_INTERVAL)

if __name__=="__main__":
    log.info("Bot baslatiliyor...")
    init_db()
    me=http_get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getMe")
    if me and me.get("ok"):
        log.info(f"Telegram OK - Bot: {me['result']['first_name']}")
        tg(f"Arbitraj Botu Basladi!\nSanal bakiye: ${STARTING_BALANCE:.0f}\nMin edge: %{MIN_EDGE}\n/help yazarak baslayabilirsin.")
    else:
        log.error("Telegram baglantisi kurulamadi!")
    threading.Thread(target=scan_loop, daemon=True).start()
    threading.Thread(target=polling, daemon=True).start()
    log.info("Bot calisiyor. Durdurmak icin Ctrl+C")
    try:
        while True: time.sleep(1)
    except KeyboardInterrupt:
        log.info("Bot durduruldu.")
