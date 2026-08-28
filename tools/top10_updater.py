#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Pro TOP 10 main-heroes updater — runs inside the analyze server (Render, 24/7).

Every 30 minutes: fetch the pro leaderboard + 13 hero boards for the CURRENT season
from the game meta server, combine with the frozen PREVIOUS-season hero scores,
compute each top-10 player's 3 main heroes, and PUT the result to Firebase `top10`.
The fan site subscribes to that node, so the screen updates with no site deploy.

Why this design (2026-08-29):
  - The Mac LaunchAgent version (리코스탯/top10_live.py) only runs while the Mac is on.
    This server runs 24/7 on Render, so rankings stay fresh even with the Mac off.
  - Firebase write uses a DEDICATED bot account (anonymous auth, refresh-token in env).
    Rules v33 allow that uid to write ONLY the `top10` node — verified 2026-08-29 that
    the bot gets 401 on tourney/profiles/admins. (Admin key deliberately NOT on Render.)
  - Hero-board scores are cumulative play points, NOT a skill rating. Main-hero rule:
    current+previous season combined, top 3, drop anything under 25% of the player's
    best hero (so some players legitimately show only 2). Verified against play-time
    data on 2026-08-28 (rank-correlation 0.62, best of the tested combinations).

Env vars (set in the Render dashboard):
  RS_PID / RS_SEC   game meta-server account (site-only account, not MAMMON's main)
  FB_REFRESH        refresh token of the Firebase bot account
  RS_GAME_PORT      optional; game server port (default 22000, changes on game updates)

If env vars are missing the thread idles quietly — the analyze server keeps working.
Previous-season scores: on the FIRST successful fetch of a season the board data is
cached to disk (hero_cache_<season>.json). When the season rolls over, the last cache
of the old season becomes the frozen "previous" data automatically. If no cache exists
yet (fresh deploy), previous-season scores are treated as 0 with a `partial` flag.
⚠️ Render free tier wipes local disk on redeploy/restart — the cache is best-effort.
"""
import os, json, time, socket, struct, threading, datetime, urllib.request, urllib.parse, ssl
try:
    import certifi
    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())   # same fix app.py uses (Mac python lacks certs)
except ImportError:
    _SSL_CTX = ssl.create_default_context()

HOST = "frontend-a76415741fc3480f.elb.us-east-1.amazonaws.com"
IP_FALLBACK = "35.153.75.130"
GAME_PORT = int(os.environ.get("RS_GAME_PORT", "22000"))
PID = os.environ.get("RS_PID", "").strip()
SEC = os.environ.get("RS_SEC", "").strip()
FB_REFRESH = os.environ.get("FB_REFRESH", "").strip()
FB_API_KEY = "AIzaSyCP9bgIqyPgPFzC8sr4CC_0FPFWpj_X1zM"   # public web key (same one the site ships)
DB = "https://ricochet-squad-bc064-default-rtdb.asia-southeast1.firebasedatabase.app"
INTERVAL = 1800

HEROES = {"108140568":"Dread","80931949":"Khan","61223451":"Vector","631049":"Oni","631047":"Remedy",
          "97251807":"Sejin","631045":"Twinkle","631042":"Jagger","631040":"Calibri","631036":"Leo",
          "45850136":"Magnus","65893852":"Nova","91887043":"Fury"}
SITE_ID = {"Dread":"dread","Khan":"khan","Vector":"vector","Oni":"oni","Remedy":"remedy",
           "Sejin":"sejin","Twinkle":"twinkle","Jagger":"jagger","Calibri":"calibri",
           "Leo":"leo","Magnus":"magnus","Nova":"nova","Fury":"fury"}
CUTOFF = 0.25
CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "replays")  # writable dir on Render

STATE = {"last_ok": 0, "last_err": "", "runs": 0}   # surfaced via /health

def _log(msg):
    print("[top10] " + msg, flush=True)

# ── game meta-server protocol (same framing as the collector scripts) ──
def _fr(env, body):
    e = json.dumps(env, separators=(",", ":")).encode()
    b = json.dumps(body, separators=(",", ":")).encode()
    return struct.pack("<H", len(e)) + struct.pack("<I", len(b)) + e + b

def _rd(s):
    def rn(n):
        buf = b""
        while len(buf) < n:
            c = s.recv(n - len(buf))
            if not c: raise ConnectionError("closed")
            buf += c
        return buf
    h = rn(6); el = struct.unpack("<H", h[:2])[0]; bl = struct.unpack("<I", h[2:6])[0]
    return (json.loads(rn(el).decode("utf-8", "ignore")),
            json.loads(rn(bl).decode("utf-8", "ignore")) if bl else {})

def _connect():
    try: s = socket.create_connection((HOST, GAME_PORT), timeout=10)
    except Exception: s = socket.create_connection((IP_FALLBACK, GAME_PORT), timeout=10)
    s.settimeout(20)
    s.sendall(_fr({"request_id": 1, "type": "authenticate_account"},
                  {"player_id": PID, "authentication_secret": SEC}))
    _rd(s)
    return s

def _get_lb(s, name, rid, top=20000):
    s.sendall(_fr({"request_id": rid, "type": "get_leaderboard"},
                  {"leaderboard_name": name, "top_size": top}))
    for _ in range(50):
        try: env, body = _rd(s)
        except socket.timeout: return None
        if env.get("type") == "get_leaderboard" and env.get("request_id") == rid: return body
    return None

def _month_codes(back=6):
    d = datetime.date.today(); y, m = d.year, d.month
    out = []
    for _ in range(back):
        out.append("%04d%02d" % (y, m))
        m -= 1
        if m == 0: m = 12; y -= 1
    return out

def _fetch_names(s, ids, rid):
    s.sendall(_fr({"request_id": rid, "type": "get_accounts_info"},
                  {"player_ids": ids, "rich_info": True}))
    names = {}; deadline = time.time() + 15
    while time.time() < deadline and len(names) < len(ids):
        try: env, body = _rd(s)
        except Exception: break
        if env.get("type") == "get_accounts_info" and "account_info_jsons" in body:
            for js in body["account_info_jsons"]:
                try: acc = json.loads(js)
                except Exception: continue
                pid = acc.get("player_id"); nm = (acc.get("player_state") or {}).get("name", "")
                if pid and nm: names[pid] = nm
    return names

# ── Firebase: bot account (refresh token → short-lived id token) ──
def _fb_id_token():
    data = urllib.parse.urlencode({"grant_type": "refresh_token", "refresh_token": FB_REFRESH}).encode()
    req = urllib.request.Request("https://securetoken.googleapis.com/v1/token?key=" + FB_API_KEY, data=data)
    with urllib.request.urlopen(req, timeout=15, context=_SSL_CTX) as r:
        return json.load(r)["id_token"]

def _fb_put_top10(payload):
    tok = _fb_id_token()
    body = json.dumps(payload, ensure_ascii=False).encode()
    req = urllib.request.Request(DB + "/top10.json?auth=" + tok, data=body, method="PUT",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20, context=_SSL_CTX) as r:
        return json.load(r)

def _cache_path(season):
    return os.path.join(CACHE_DIR, "hero_cache_%s.json" % season)

def run_once():
    s = _connect()
    try:
        # current + previous pro season
        found = []; rid = 300
        for code in _month_codes():
            rid += 1
            b = _get_lb(s, "rating_pro_0_%s" % code, rid, top=1)
            if (b or {}).get("leaderboard_size"): found.append(code)
            if len(found) >= 2: break
        if not found: raise RuntimeError("no pro season found")
        cur = found[0]; prev = found[1] if len(found) > 1 else None

        rid += 1
        pro = _get_lb(s, "rating_pro_0_%s" % cur, rid)
        if not pro or not pro.get("top_player_ids"): raise RuntimeError("pro board empty")
        top = list(zip(pro["top_player_ids"][:10], [int(x) for x in pro["top_player_scores"][:10]]))
        ids = [p for p, _ in top]

        cur_scores = {}
        for hid, hname in HEROES.items():
            rid += 1
            b = _get_lb(s, "rating_heroes_%s_%s" % (hid, cur), rid)
            cur_scores[hname] = dict(zip(b.get("top_player_ids", []),
                                         [int(x) for x in b.get("top_player_scores", [])])) if b else {}
            time.sleep(0.15)
        try: json.dump(cur_scores, open(_cache_path(cur), "w"))
        except Exception: pass

        prev_scores = {}; partial = False
        if prev:
            try: prev_scores = json.load(open(_cache_path(prev)))
            except Exception:
                partial = True   # fresh deploy: no frozen cache for the previous season yet

        rid += 1
        names = _fetch_names(s, ids, rid)
    finally:
        s.close()

    rows = []
    for rank, (pid, rating) in enumerate(top, 1):
        combined = {}
        for hname in HEROES.values():
            v = (cur_scores.get(hname, {}).get(pid, 0) or 0) + ((prev_scores.get(hname) or {}).get(pid, 0) or 0)
            if v: combined[hname] = v
        hs = sorted(combined.items(), key=lambda x: -x[1])
        if hs:
            best = hs[0][1]
            hs = [(h, v) for h, v in hs if v >= best * CUTOFF][:3]
        rows.append({"r": rank, "n": names.get(pid) or ("???" + pid[:6]), "s": rating,
                     "h": [[SITE_ID[h], v] for h, v in hs]})

    # Render runs on UTC — show KST so the site's "기준" line reads naturally for the main audience
    kst = datetime.datetime.utcnow() + datetime.timedelta(hours=9)
    payload = {"asOf": kst.strftime("%Y-%m-%d %H:%M"),
               "ts": int(time.time() * 1000), "season": cur, "src": "render", "rows": rows}
    if partial: payload["partial"] = True
    _fb_put_top10(payload)
    return len(rows)

def _loop():
    if not (PID and SEC and FB_REFRESH):
        _log("env vars missing (RS_PID/RS_SEC/FB_REFRESH) — updater idle")
        return
    time.sleep(20)   # let the web server come up first
    while True:
        try:
            n = run_once()
            STATE["last_ok"] = int(time.time()); STATE["last_err"] = ""; STATE["runs"] += 1
            _log("updated %d rows" % n)
        except Exception as e:
            STATE["last_err"] = str(e)[:200]
            _log("failed: " + STATE["last_err"])
        time.sleep(INTERVAL)

def start_background():
    t = threading.Thread(target=_loop, daemon=True)
    t.start()
    return t
