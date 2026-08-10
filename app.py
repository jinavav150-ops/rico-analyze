#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""🔍 리코셰 경기 분석 서버 — 매치 코드 하나로 경기 리포트 JSON 을 만든다.

흐름:  GET /analyze/<코드>
  ① S3 에서 리플레이 찾기 (날짜를 몰라서 최근 41일을 거꾸로 훑는다 — HEAD 요청이라 싸다)
  ② 내려받아 `리플레이/` 에 저장 (도구들이 이 폴더를 본다)
  ③ 애니 항목 수 풀기(초기상태.cmd_save) → 경기분석.collect → 리포트 JSON
  ④ 파일 삭제(디스크는 임시), 결과는 메모리 캐시. 저장은 **사이트(클라이언트)가 Firebase 에** 한다.

왜 이 구조인가:
  · 사이트는 서버가 없는 정적 페이지 + Firebase 무료 플랜 → 무거운 파싱은 여기(무료 Render)서.
  · 서버는 **읽기 전용·비밀키 없음** — 뚫려도 잃을 게 없다. Firebase 쓰기는 클라이언트가
    자기 권한으로 한다(규칙 v26 이 구조를 검증).
  · ⚠️ 게임이 패치되면 새 리플레이가 안 읽힐 수 있다 → 완주율이 낮으면 분석을 내놓지 않고
    "게임 업데이트 반영 전" 오류를 준다 (틀린 리포트를 주느니 안 주는 게 낫다).

로컬 시험:  python3 app.py  →  http://localhost:8000/analyze/BYDPH6ZM
Render 시작 명령:  gunicorn -w 1 -t 300 -b 0.0.0.0:$PORT app:app
"""
import os
import re
import sys
import json
import gzip
import zlib
import time
import threading
import datetime
import ssl
import urllib.request
import urllib.error
import importlib
import concurrent.futures as cf

# 🪤 python.org 빌드는 CA 인증서가 비어 있어 SSL 검증이 실패한다 (맥 로컬 시험에서 실제 발생).
#    certifi 가 있으면 그걸 쓴다 — Render(리눅스)는 시스템 인증서로도 되지만 이중 안전.
try:
    import certifi
    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL_CTX = ssl.create_default_context()

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(BASE, "tools"))
S = importlib.import_module("restore_state")
A = importlib.import_module("match_report")
R = importlib.import_module("replay_stream")

from flask import Flask, jsonify, request  # noqa: E402  (경로 세팅 뒤에)

app = Flask(__name__)

BUCKET = "https://ricochet-replays-production.s3.us-east-1.amazonaws.com"
REPLAY_DIR = os.path.join(BASE, "replays")
CODE_RE = re.compile(r"^[A-Z0-9]{6,12}$")
SEARCH_DAYS = 41                 # 리플레이는 S3에서 약 40일 뒤 삭제된다
# 경기 종류별 S3 경로 — 게임 문자열 상수(dump.cs)에 있는 셋. 흔한 순서대로 둔다.
S3_PREFIXES = ("rating", "training", "custom")
MIN_OK_RATIO = 0.85              # 프레임 완주율이 이보다 낮으면 "버전 불일치"로 친다

REFDATA = json.load(open(os.path.join(BASE, "tools", "refdata.json"), encoding="utf-8"))
MODS = {int(k): v.get("official") or "?" for k, v in REFDATA.get("modifiers", {}).items()
        if v.get("bit") is not None}
MOD_BITS = {int(k): v["bit"] for k, v in REFDATA.get("modifiers", {}).items()
            if v.get("bit") is not None}

_cache = {}                      # 코드 → (시각, 리포트) — Render 재시작에 날아가도 무방
_CACHE_MAX = 60
_lock = threading.Lock()         # 512MB 램에서 분석은 한 번에 하나만


def _http(url, method="GET", timeout=30):
    req = urllib.request.Request(url, method=method, headers={"User-Agent": "rs-analyze/1"})
    return urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX)


def find_on_s3(code):
    """날짜를 몰라 최근부터 거꾸로 HEAD 로 찾는다. 찾으면 (키, 날짜문자열).

    🪤 **경기 종류마다 경로가 다르다** (2026-08-08 실측: MAMMON 경기 TYW12FB7 은
       `training/` 에 있었고 `rating/` 만 뒤지던 서버는 "못 찾음"을 냈다).
       게임 문자열 상수에 있는 종류는 `rating`·`training`·`custom` 셋 — 전부 뒤진다.
    ⚠️ "없음(404)" 과 "네트워크 고장" 을 구분한다 — SSL/연결 오류를 조용히 넘기면
       진짜 존재하는 리플레이도 not_found 로 오판한다(로컬 시험에서 실제 발생).
    """
    today = datetime.datetime.now(datetime.timezone.utc).date()
    # 하루당 3경로 × 41일 = 123번이라 순차로는 느리다 → 날짜를 묶어 병렬로 확인하고
    # **결과는 원래 순서대로** 본다(가장 최근·가장 앞 경로가 이기도록).
    cands = []
    for back in range(SEARCH_DAYS):
        d = today - datetime.timedelta(days=back)
        for pre in S3_PREFIXES:
            cands.append((f"{pre}/{d.year}/{d.month:02d}/{d.day:02d}/{code}.rsrpl.gz",
                          d.isoformat()))

    def probe(item):
        key, iso = item
        try:
            _http(f"{BUCKET}/{key}", method="HEAD", timeout=10)
            return (key, iso, None)
        except urllib.error.HTTPError as e:
            if e.code in (403, 404):
                return None
            return (None, None, e)
        except urllib.error.URLError as e:
            return (None, None, e)

    net_fail = 0
    with cf.ThreadPoolExecutor(max_workers=12) as pool:
        for i in range(0, len(cands), 12):
            chunk = cands[i:i + 12]
            for res in list(pool.map(probe, chunk)):   # map 은 입력 순서를 지킨다
                if res is None:
                    continue
                key, iso, exc = res
                if key:
                    return key, iso
                net_fail += 1
                if net_fail >= 6:            # 계속 고장이면 탐색 실패가 아니라 통신 실패다
                    raise RuntimeError(f"S3 통신 실패: {exc}")
    return None, None


def err(code, ko, en, status=200):
    """오류도 200 + JSON — 사이트가 메시지를 그대로 보여 주기 쉽게."""
    return jsonify({"error": code, "message": {"ko": ko, "en": en}}), status


def _sec(ms, t0):
    return round((ms - t0) / 1000, 1)


def build_report(code, s3date):
    """경기분석 모듈의 함수들을 사이트가 그리기 좋은 **작은 JSON** 으로 요약한다."""
    d = A.collect(code)
    t0 = d["t0"]
    ok_ratio = d["ok"] / max(1, d["frames"])
    if ok_ratio < MIN_OK_RATIO:
        return None, ok_ratio

    st = A.summarize(d)
    uv, al = A.ult_value(d), A.alive_time(d)
    fp, po, fe = A.fight_participation(d), A.positioning(d), A.first_engage_counts(d)
    rows, nok = A.selfcheck(d)
    check = {pid: (k == mk and dd == md) for pid, k, dd, mk, md in rows}
    import collections as _c
    sk = _c.Counter((pid, k) for _, pid, k, _off in d.get("skills", []))

    start = d.get("start") or {}
    modbits = start.get("ModifierType") or 0
    players = []
    for pid, p in sorted(d["players"].items()):
        c = st[pid]
        n, tot = fp[pid]
        players.append({
            "pid": pid, "name": p.get("name") or f"P{pid}", "bot": bool(p.get("bot")),
            # 🪤 `hero` 는 **정규화 키**(소문자·하이픈 없음)를 그대로 보낸다.
            #    예전엔 capitalize() 해서 보냈는데 'Se-Jin' → 'Se-jin' 이 되고
            #    사이트 초상화 키('sejin')와 어긋나 **세진 초상화가 늘 "?" 로 깨졌다**.
            #    화면에 쓸 표기는 `hero_disp` 로 따로 보낸다.
            "team": p.get("team"), "hero": p.get("hero") or "?",
            "hero_disp": p.get("hero_disp") or "?",
            "level": p.get("hero_level"), "sure": bool(p.get("hero_sure")),
            # 🎽 로드아웃 — 레이팅·파티·퍽 빌드 (StartData 에 있다, 2026-08-09 발견)
            "rating": p.get("rating"),
            "squad": p.get("squad") if not p.get("bot") else None,
            "perks": [n for n in (A.perk_name(x) for x in (p.get("perks") or [])) if n],
            "stats": {"k": c["처치"], "d": c["죽음"], "a": c["어시"], "dmg": c["준피해"],
                      "taken": c["받은피해"], "obj": c["구조물피해"], "heal": c["회복"],
                      "shield": c["실드"], "skill": sk[(pid, "스킬")], "ult": sk[(pid, "궁")]},
            "check": check.get(pid),
            "style": {"part": [n, tot],
                      "front": round(po[pid]["front"], 3) if po[pid].get("front") is not None else None,
                      "dist": round(po[pid]["dist"]), "first": fe.get(pid, 0)},
            "ultv": list(uv[pid]), "alive": round(al[pid], 3),
            "hp": [[_sec(m, t0), round(v, 2)] for m, v in A.hp_track(d, pid, step=2000)],
        })
    # 💚 힐 분배를 선수 stats에 덧붙인다 (추가 필드 — 구버전 캐시와 호환)
    # (heals 는 아래에서 계산 — 여기선 자리만; 실제 주입은 report 조립 직전)

    srows, flips = A.score_timeline(d)
    deaths_out = []
    for i, ev in enumerate(A.death_details(d), 1):
        deaths_out.append({"i": i, "s": _sec(ev["ms"], t0), "tgt": ev["tgt"],
                           "by": ev["killers"],
                           "dmg": sorted(([q, v] for q, v in ev["dmg_by"].items()),
                                         key=lambda x: -x[1]),
                           "burst": round(ev["burst_s"], 1),
                           "solo": ev["solo"], "near": len(ev["allies_near"]),
                           # 🩸 교전 시작 시 체력 % · ⚔️ 그 순간 인원(아군, 적)
                           "hp0": (round(ev["hp0"], 2) if ev.get("hp0") is not None else None),
                           "nums": ev.get("nums")})
    ults = [{"s": _sec(ms, t0), "pid": pid}
            for ms, pid, kind, _off in d.get("skills", []) if kind == "궁"]
    objs = [{"s": _sec(ms, t0), "kind": h[0], "by": h[1]}
            for ms, h in d["logs"]
            if h[0] in (8, 29) and h[1] in d["players"]]
    # ⚔️ 교전 일지 — 누가 열었고 몇대몇으로 시작했나 (fight_story)
    # 🔁 revives = 그 교전 구간 안에서 죽었다 부활해 다시 싸운 횟수(팀별) — 2026-08-09 14차.
    #    "3명뿐인데 4명을 잡았다"는 혼란을 없애려고 사이트가 문장에 접어 넣는다.
    #    옛 캐시 리포트에는 이 키가 없다 — 없으면 사이트가 조용히 옛 문장으로 떨어진다.
    fights_out = [{"a": _sec(f["a"], t0), "b": _sec(f["b"], t0), "dmg": f["tot"],
                   "lost": {str(k): v for k, v in f["lost"].items()}, "win": f["win"],
                   "opener": f.get("opener"), "nums": f.get("nums"),
                   "revives": {str(k): v for k, v in f.get("revives", {}).items()}}
                  for f in A.fight_story(d)]
    # 💚 힐 분배 (kind 1 회복의 자기/아군 나눔) → 선수 stats에 추가 필드로
    #    healA=아군에게 준 힐 · healR=아군에게서 받은 힐 (사이트 명단 표는 이 둘만 씀)
    heals = A.heal_split(d)
    for p in players:
        # 힐 기록이 아예 없는 선수도 0으로 채운다 — 새 리포트에서 "–"(값 없음)가 안 나오게
        hl = heals.get(p["pid"]) or {"ally": 0, "self": 0, "recv": 0}
        p["stats"]["healA"] = hl["ally"]
        p["stats"]["healS"] = hl["self"]
        p["stats"]["healR"] = hl.get("recv", 0)
    spots = [{"s": _sec(ms, t0), "tgt": tgt, "x": round(p[0], 1), "z": round(p[2], 1)}
             for ms, tgt, who, p in A.death_spots(d) if p]

    # 이동 흔적(맵 모양용) — 선수당 최대 200점으로 솎는다
    traces = []
    for pid, p in d["players"].items():
        tr = d["pos"].get(p["hero_entity"]) or []
        step = max(1, len(tr) // 200)
        traces.append({"team": p["team"],
                       "pts": [[round(q[0], 1), round(q[2], 1)] for _, q in tr[::step]]})
    hg = A.heat_grid(d)
    heat = None
    if hg:
        heat = {"n": hg["n"], "bounds": [round(v, 1) for v in hg["bounds"]],
                "cells": [[i, j, c1, c2] for (i, j), (c1, c2) in hg["cells"].items()]}

    w, why = A.winner(d)
    kind, warn = A.match_kind(d)
    dur = 0
    for tr in d["pos"].values():
        if tr:
            dur = max(dur, tr[-1][0])

    # ✨ 명장면 — A.scene_list() 가 "같은 선수+같은 스킬 반복"을 하나로 묶고 우선순위로
    #    5개까지만 남긴다(근거는 scene_list() 문서 참고). 여기서는 JSON 모양만 만든다.
    scene_rows, scenes_dropped = A.scene_list(d)
    scenes_out = []
    for sc in scene_rows:
        base = {"s": _sec(sc["ms"], t0), "kind": sc["kind"], "pid": sc["pid"],
                # count=1 이면 옛 리포트와 완전히 같은 모양 → 사이트가 기존 문장 그대로 쓴다.
                # 2 이상이면 사이트가 "N번 반복(예시 시각 · 외 M회)" 문장으로 묶어 보여준다.
                "count": sc["count"], "times": [_sec(t, t0) for t in sc["times"]]}
        if sc["kind"] == "setup":
            base.update({"skill": sc["skill"], "skill_i18n": sc.get("skill_i18n"),
                         "n": len(sc["dealers"]), "kills": sc["kills"],
                         # 효과 종류(stun/bind/pull/mark) — "kind" 는 이미 장면 종류(setup/ult)로
                         # 쓰고 있어 이름이 안 겹치게 "setup_kind" 로 따로 보낸다. 옛 스킬표엔
                         # 없어서 None 이 나올 수 있고, 그때 사이트는 기존 sc_setup 문장으로 떨어진다.
                         "setup_kind": sc.get("setup_kind")})
        else:
            base.update({"pids": sc["pids"], "skills": sc["skills"],
                         "skills_i18n": sc.get("skills_i18n"), "got": sc["kills"]})
        scenes_out.append(base)

    report = {
        "v": 1, "code": code, "date": s3date,
        "made": int(time.time()),
        "gamever": None,   # 아래에서 파일값으로 채움
        "map": (REFDATA.get("maps", {}).get(str(start.get("LevelId"))) or {}).get("official"),
        "mode": (REFDATA.get("preset_to_mode", {}).get(str(start.get("LevelPresetId"))) or {}).get("official"),
        "mods": [MODS[k] for k, bit in MOD_BITS.items() if modbits & bit],
        "rating": start.get("AveragePlayerRating"),
        "kind": kind, "warn": warn, "winner": w, "why": why,
        "frames": [d["ok"], d["frames"]], "check_ok": [nok, len(rows)],
        "dur": _sec(dur, t0) if dur else None,
        "players": players,
        "score": [[_sec(ms, t0), a, b] for ms, a, b in srows],
        # "pct"=장악(0~100 진행도, %로 표시) · "pts"=그 외 점수 — 사이트가 % 붙일지 판단
        "scorekind": A.score_unit(d),
        "flips": [[_sec(ms, t0), team] for ms, team in flips],
        "ults": ults, "objs": objs, "deaths": deaths_out,
        "fights": fights_out, "spots": spots, "traces": traces, "heat": heat,
        # 🎓 코칭 판정 — 서버는 (키+숫자)만 보내고 문장은 사이트가 언어별 템플릿으로 만든다
        "coach": A.coach(d),
        # 🧩 왜 졌나 — 인과 사슬. 판이 갈린 순간의 **앞 칸**(상대 궁이 차 있었다·뭉쳐
        #    있었다·인원 열세·정비 없이 재진입)을 결과와 이어 붙인 고리 목록.
        #    문장은 사이트(RPT_WHY)가 만든다. 조건이 안 맞으면 None → 사이트가 절을 통째로 뺀다.
        #    ⚠️ 픽 상성은 **여기 안 들어간다** — 상성표의 주인은 index.html 이고 관리자가
        #       고칠 수 있어서, 사이트가 자기 MATCHUPS 로 직접 계산한다(match_report.pick_matchup 주석).
        "why": A.why_lost(d),
        # ✨ 명장면 — "판 깔고 점사"·"궁 연계" (사이트가 타임라인처럼 보여 준다)
        "scenes": scenes_out,
        # 우선순위 컷으로 빠진 "묶은 뒤" 장면 개수 — 0 이면 안 잘림(옛 리포트엔 이 키가 없어
        # 사이트가 N(r.scenes_more||0)으로 읽으면 자동으로 0 취급된다).
        "scenes_more": scenes_dropped,
    }
    return report, ok_ratio


@app.get("/health")
def health():
    return jsonify({"ok": True, "cached": len(_cache), "jobs": len(_jobs)})


# ═══ 🕒 비동기 처리 — Render 무료 플랜(CPU 0.1코어) 대응 ═══════════════════
#   🪤 2026-08-08 실측: 맥에서 32초 걸리는 분석이 여기선 훨씬 오래 걸려
#      **Render 게이트웨이가 먼저 요청을 끊었다**(`{"ok":false,"error":"timed out"}`
#      — 우리 형식이 아니라 Render 것). 오래 붙잡는 방식으로는 절대 못 넘긴다.
#   → 요청은 **즉시** 상태만 돌려주고, 실제 분석은 뒤에서 돌린다. 사이트가 몇 초마다 다시 물어본다.
#     한 번 분석한 경기는 사이트가 Firebase에 저장하므로, 이 기다림은 **경기당 딱 한 번**뿐이다.
_jobs = {}                       # 코드 → {state, started, report?, err?}
_current = None                  # 지금 실제로 분석 중인 코드 (나머지는 순서 대기)
_JOB_MAX = 40
_JOB_TIMEOUT = 1800              # 30분 넘게 붙잡고 있으면 죽은 것으로 본다


def _run_job(code):
    """백그라운드 워커 — 한 번에 하나만 (512MB 램이라 동시 분석은 위험)."""
    global _current
    dst = os.path.join(REPLAY_DIR, f"{code}.rsrpl.gz")
    try:
        with _lock:
            _current = code
            key, s3date = find_on_s3(code)
            if not key:
                _jobs[code] = {"state": "error", "code": "not_found",
                               "ko": "리플레이를 못 찾았어요. 코드 오타이거나, 40일이 지나 서버에서 지워진 경기예요.",
                               "en": "Replay not found — wrong code, or older than 40 days (deleted)."}
                return
            with _http(f"{BUCKET}/{key}", timeout=90) as r:
                data = r.read()
            os.makedirs(REPLAY_DIR, exist_ok=True)
            open(dst, "wb").write(data)
            # 게임 버전은 파일 맨 앞에 있다 — 앞 64KB만 부분 해제해서 뽑는다
            head = zlib.decompressobj(47).decompress(data[:65536]).decode("utf-8", "replace")
            m = re.search(r'"CompatibilityVersion":"([^"]+)"', head)
            gamever = m.group(1) if m else None
            S.cmd_save([code], quick=True)    # 애니 항목 수 풀기 (⚡ 빠른 길 — 느린 길은 47초→Render 8분)
            report, ok_ratio = build_report(code, s3date)
            if report is None:
                _jobs[code] = {"state": "error", "code": "version_mismatch",
                               "ko": f"이 리플레이는 아직 못 읽어요 (완주율 {ok_ratio*100:.0f}%). "
                                     "게임이 업데이트된 직후라면 분석기 갱신을 기다려 주세요.",
                               "en": "Cannot parse this replay yet — likely a new game version."}
                return
            report["gamever"] = gamever
            if len(_cache) >= _CACHE_MAX:     # 캐시 자리 비우기 (오래된 것부터)
                del _cache[min(_cache, key=lambda k: _cache[k][0])]
            _cache[code] = (time.time(), report)
            _jobs[code] = {"state": "done"}
    except Exception as e:
        app.logger.exception("analyze 실패: %s", code)
        _jobs[code] = {"state": "error", "code": "internal",
                       "ko": f"분석 중 오류가 났어요 ({type(e).__name__}). 같은 오류가 반복되면 피드백으로 알려 주세요.",
                       "en": f"Analysis failed ({type(e).__name__})."}
    finally:
        if _current == code:
            _current = None
        if os.path.exists(dst):
            os.remove(dst)                    # 디스크는 임시 — 결과만 남긴다


@app.get("/analyze/<raw_code>")
def analyze(raw_code):
    """즉시 응답한다. 아직이면 `{"status":"running"}` — 사이트가 다시 물어본다."""
    code = raw_code.strip().upper()
    if not CODE_RE.match(code):
        return err("bad_code", "매치 코드 형식이 아니에요 (영문 대문자·숫자).",
                   "Not a valid match code.")
    hit = _cache.get(code)
    if hit:
        out = dict(hit[1])
        out["status"] = "done"
        return jsonify(out)

    job = _jobs.get(code)
    if job:
        if job["state"] == "error":
            _jobs.pop(code, None)             # 다시 누르면 재시도할 수 있게
            return err(job["code"], job["ko"], job["en"])
        if job["state"] == "running":
            if time.time() - job["started"] > _JOB_TIMEOUT:
                _jobs.pop(code, None)
                return err("timeout", "분석이 너무 오래 걸려 중단했어요. 다시 시도해 주세요.",
                           "Analysis took too long and was dropped. Please try again.")
            return jsonify({"status": "running",
                            "elapsed": int(time.time() - job["started"]),
                            "queued": _current is not None and _current != code})
        # state == done 인데 캐시에 없으면(캐시 밀림) 다시 돌린다
        _jobs.pop(code, None)

    if len(_jobs) >= _JOB_MAX:                # 오래된 기록 정리
        for k in [k for k, v in _jobs.items() if v.get("state") != "running"][:20]:
            _jobs.pop(k, None)
    _jobs[code] = {"state": "running", "started": time.time()}
    threading.Thread(target=_run_job, args=(code,), daemon=True).start()
    return jsonify({"status": "started", "elapsed": 0})


@app.after_request
def cors(resp):
    # 리포트는 공개 데이터 — 어느 origin 이든 읽게 둔다 (쓰기는 어차피 여기 없음)
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Cache-Control"] = "no-store"
    return resp


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
