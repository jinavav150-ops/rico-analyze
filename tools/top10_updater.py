#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Pro TOP 10 main-heroes updater — runs inside the analyze server (Render, 24/7).

Every 30 minutes: fetch the pro leaderboard + 13 hero boards for the CURRENT season
from the game meta server, combine with the frozen PREVIOUS-season hero scores,
compute each top-10 player's 3 main heroes, and PUT the result to Firebase `top10`.
The fan site subscribes to that node, so the screen updates with no site deploy.

It ALSO builds the "pro ranking deep stats" screen (Firebase `top10deep`, rules v35):
per-hero rating gained/lost, pro-only K/D, and win rate per teammate. Those come from
`get_match_results_info` on each player's `match_state.match_history` — the server keeps
the LAST 25 MATCHES per player, wins AND losses. Built 2026-08-29 from three suggestions
by pro player GG Qwaser.

CORRECTION 2026-08-30: MatchType was read backwards at first. The game defines
Training = 0, **Pro = 1**, **Casual = 2** — so MatchType 2 is CASUAL, not Pro. In practice
only ~5% of the top 10's last 25 matches are Pro; the rest are Casual (a `rating/` ReplayKey
prefix just means the mode is rated, not that it is Pro). Reported by pro player GG Qwaser.
So each row now carries md = {pro, casual, etc} and the site prints that breakdown on every
card instead of claiming these are Pro stats. Collecting Pro-only matches is a separate task. The deep pass runs after the
main upload in its own try/except, so a failure there never blocks the main `top10` write.

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
Previous-season scores: frozen once the season ends. Cached to disk per season
(hero_cache_<season>.json); Render wipes the disk on every deploy, so when the cache
is missing the updater fetches the previous season's boards from the game server
directly (once per boot, ~10s) and re-caches. `partial` is only set if the game
server no longer serves that season at all.
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
# A brand-new season's pro board can have 1-2 players for days (pro is rarely played early in a
# season). Picking that as "current" put a one-player TOP 10 on the site (2026-09-02). Seasons with
# fewer than MIN_BOARD entries are skipped; the previous season's final standings are used and the
# payload carries prevSeason/newSeason/newSize so the site can say so.
MIN_BOARD = 10
HERO_BY_ID = {int(k): v for k, v in HEROES.items()}   # 경기 결과의 HeroId 는 정수로 온다
MIN_MATE = 2      # 팀메이트 승률에 넣을 최소 동행 경기수 (1경기는 우연이 너무 크다)
MIN_HERO = 2      # 영웅별 레이팅 증감에 필요한 최소 경기수 (첫 값과 끝 값의 차이라 2판 이상 필요)
CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "replays")  # writable dir on Render

STATE = {"last_ok": 0, "last_err": "", "runs": 0, "deep_ok": 0, "deep_err": ""}   # surfaced via /health

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

def _fb_put(path, payload):
    """규칙 v35 기준 이 봇이 쓸 수 있는 곳은 /top10 과 /top10deep 뿐이다."""
    tok = _fb_id_token()
    body = json.dumps(payload, ensure_ascii=False).encode()
    req = urllib.request.Request(DB + path + ".json?auth=" + tok, data=body, method="PUT",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20, context=_SSL_CTX) as r:
        return json.load(r)

def _cache_path(season):
    return os.path.join(CACHE_DIR, "hero_cache_%s.json" % season)

def run_once():
    s = _connect()
    try:
        # current + previous pro season
        found = []; fresh = None; rid = 300
        for code in _month_codes():
            rid += 1
            b = _get_lb(s, "rating_pro_0_%s" % code, rid, top=1)
            n = (b or {}).get("leaderboard_size") or 0
            if n >= MIN_BOARD: found.append(code)
            elif n and not found: fresh = (code, n)
            if len(found) >= 2: break
        if not found: raise RuntimeError("no pro season found")
        cur = found[0]; prev = found[1] if len(found) > 1 else None
        if fresh: _log("new season %s has only %d pro players - using %s final standings" % (fresh[0], fresh[1], cur))

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

        # Previous season: frozen once it ends. Render wipes the disk on every deploy, so a
        # missing cache is NORMAL after a deploy — in that case fetch the prev-season boards
        # from the game server directly (once per boot, ~10s) and cache them for later runs.
        # Without this the first post-deploy write had current-season-only sums, which made
        # the site numbers jump between "full" (Mac) and "half" (cloud) every 30 min (2026-08-29).
        prev_scores = {}; partial = False
        if prev:
            try: prev_scores = json.load(open(_cache_path(prev)))
            except Exception:
                _log("prev-season cache missing — fetching %s boards from game server" % prev)
                for hid, hname in HEROES.items():
                    rid += 1
                    b = _get_lb(s, "rating_heroes_%s_%s" % (hid, prev), rid)
                    prev_scores[hname] = dict(zip(b.get("top_player_ids", []),
                                                  [int(x) for x in b.get("top_player_scores", [])])) if b else {}
                    time.sleep(0.15)
                if any(prev_scores.values()):
                    try: json.dump(prev_scores, open(_cache_path(prev), "w"))
                    except Exception: pass
                else:
                    partial = True   # game server no longer serves that season — genuinely no data

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
    if fresh: payload["prevSeason"] = True; payload["newSeason"] = fresh[0]; payload["newSize"] = fresh[1]
    _fb_put("/top10", payload)
    return len(rows)


# ── 🏅 심층 지표: 최근 25경기를 경기 단위로 뜯는다 (Firebase /top10deep) ──
def _wait_for(s, typ, timeout=45):
    dl = time.time() + timeout
    while time.time() < dl:
        try: env, body = _rd(s)
        except Exception: return None
        if env.get("type") == typ: return body
    return None

def run_deep():
    s = _connect()
    try:
        season = None; fresh = None; rid = 700
        for code in _month_codes():
            rid += 1
            b = _get_lb(s, "rating_pro_0_%s" % code, rid, top=1)
            n = (b or {}).get("leaderboard_size") or 0
            if n >= MIN_BOARD: season = code; break
            if n and fresh is None: fresh = (code, n)
        if not season: raise RuntimeError("no pro season found")
        rid += 1
        pro = _get_lb(s, "rating_pro_0_%s" % season, rid)
        if not pro or not pro.get("top_player_ids"): raise RuntimeError("pro board empty")
        top = list(zip(pro["top_player_ids"][:10], [int(x) for x in pro["top_player_scores"][:10]]))
        ids = [p for p, _ in top]

        # 계정 정보 — 이름 + 최근 25경기 코드
        rid += 1
        s.sendall(_fr({"request_id": rid, "type": "get_accounts_info"},
                      {"player_ids": ids, "rich_info": True}))
        accs = {}; dl = time.time() + 30
        while time.time() < dl and len(accs) < len(ids):
            body = _wait_for(s, "get_accounts_info", timeout=10)
            if not body: break
            for js in body.get("account_info_jsons") or []:
                try: a = json.loads(js)
                except Exception: continue
                if a.get("player_id"): accs[a["player_id"]] = a

        # 경기 상세 — 선수별로 자기 이력을 통째로 요청(여러 선수가 같은 경기를 공유하므로 dict 로 모은다)
        matches = {}; hist_of = {}
        for pid in ids:
            h = ((accs.get(pid) or {}).get("match_state") or {}).get("match_history") or []
            hist_of[pid] = h
            need = [c for c in h if c not in matches]
            if not need: continue
            rid += 1
            s.sendall(_fr({"request_id": rid, "type": "get_match_results_info"}, {"match_ids": need}))
            body = _wait_for(s, "get_match_results_info", timeout=45)
            for js in (body or {}).get("match_result_info_jsons") or []:
                try: m = json.loads(js)
                except Exception: continue
                if m.get("MatchId"): matches[m["MatchId"]] = m
            time.sleep(0.2)
    finally:
        s.close()

    rows = []
    for rank, (pid, rating) in enumerate(top, 1):
        name = ((accs.get(pid) or {}).get("player_state") or {}).get("name") or ("???" + pid[:6])
        mine = [matches[c] for c in hist_of.get(pid, []) if c in matches]
        mine.sort(key=lambda m: m.get("Timestamp", 0))

        w = l = K = D = 0
        md = {"pro": 0, "casual": 0, "etc": 0}      # MatchType: Training=0, Pro=1, Casual=2
        mate = {}; hero_seq = {}; hero_wl = {}
        for m in mine:
            prs = m.get("PlayerResults") or []
            me = next((p for p in prs if p.get("PlayerId") == pid), None)
            if not me: continue
            mt = m.get("MatchType")
            md["pro" if mt == 1 else ("casual" if mt == 2 else "etc")] += 1
            win = me.get("Team") == m.get("WinnerTeam")
            w += 1 if win else 0; l += 0 if win else 1
            K += me.get("Eliminations", 0) or 0
            D += me.get("Deaths", 0) or 0
            for p in prs:
                if p.get("PlayerId") == pid or p.get("Team") != me.get("Team"): continue
                nm = p.get("Name") or (p.get("PlayerId") or "")[:8]
                r = mate.setdefault(nm, [0, 0]); r[0] += 1 if win else 0; r[1] += 1
            for hr in (me.get("HeroResultData") or []):
                hn = HERO_BY_ID.get(hr.get("HeroId"))
                if not hn: continue
                hero_seq.setdefault(hn, []).append((m.get("Timestamp", 0), hr.get("Rating")))
                r = hero_wl.setdefault(hn, [0, 0]); r[0] += 1 if win else 0; r[1] += 1

        heroes = []
        for hn, seq in hero_seq.items():
            seq.sort(key=lambda x: x[0])
            vals = [v for _, v in seq if v is not None]
            gw, gt = hero_wl[hn]
            heroes.append({"id": SITE_ID[hn], "g": gt, "w": gw,
                           "d": (vals[-1] - vals[0]) if len(vals) >= MIN_HERO else None,
                           "cur": vals[-1] if vals else None})
        heroes.sort(key=lambda x: (-(x["d"] if x["d"] is not None else -10**9), -x["g"]))
        mates = sorted(([nm, v[0], v[1]] for nm, v in mate.items() if v[1] >= MIN_MATE),
                       key=lambda x: (-(x[1] / float(x[2])), -x[2]))[:5]
        rows.append({"r": rank, "n": name, "s": rating, "mp": w + l, "w": w, "l": l, "md": md,
                     "k": K, "d": D, "kd": round(K / float(D), 2) if D else None,
                     "heroes": heroes, "mates": mates})   # 상위 5종 자르기 폐지(2026-08-29) — 잘린 영웅의 승패가 사라져 전적 합이 안 맞았다

    kst = datetime.datetime.utcnow() + datetime.timedelta(hours=9)
    out = {"asOf": kst.strftime("%Y-%m-%d %H:%M"), "ts": int(time.time() * 1000),
           "season": season, "window": 25, "src": "render", "rows": rows}
    if fresh: out["prevSeason"] = True; out["newSeason"] = fresh[0]; out["newSize"] = fresh[1]
    _fb_put("/top10deep", out)
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
        try:
            # 심층 지표는 따로 감싼다 — 여기서 넘어져도 위의 top10 갱신은 이미 끝나 있어야 한다
            d = run_deep()
            STATE["deep_ok"] = int(time.time()); STATE["deep_err"] = ""
            _log("deep updated %d rows" % d)
        except Exception as e:
            STATE["deep_err"] = str(e)[:200]
            _log("deep failed: " + STATE["deep_err"])
        time.sleep(INTERVAL)

def start_background():
    t = threading.Thread(target=_loop, daemon=True)
    t.start()
    return t
