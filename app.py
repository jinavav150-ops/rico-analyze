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

# 🪤 python.org 빌드는 CA 인증서가 비어 있어 SSL 검증이 실패한다 (맥 로컬 시험에서 실제 발생).
#    certifi 가 있으면 그걸 쓴다 — Render(리눅스)는 시스템 인증서로도 되지만 이중 안전.
try:
    import certifi
    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL_CTX = ssl.create_default_context()

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(BASE, "도구"))
S = importlib.import_module("초기상태")
A = importlib.import_module("경기분석")
R = importlib.import_module("replay_stream")

from flask import Flask, jsonify, request  # noqa: E402  (경로 세팅 뒤에)

app = Flask(__name__)

BUCKET = "https://ricochet-replays-production.s3.us-east-1.amazonaws.com"
REPLAY_DIR = os.path.join(BASE, "리플레이")
CODE_RE = re.compile(r"^[A-Z0-9]{6,12}$")
SEARCH_DAYS = 41                 # 리플레이는 S3에서 약 40일 뒤 삭제된다
MIN_OK_RATIO = 0.85              # 프레임 완주율이 이보다 낮으면 "버전 불일치"로 친다

REFDATA = json.load(open(os.path.join(BASE, "도구", "refdata.json"), encoding="utf-8"))
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

    ⚠️ "없음(404)" 과 "네트워크 고장" 을 구분한다 — SSL/연결 오류를 조용히 넘기면
       진짜 존재하는 리플레이도 not_found 로 오판한다(로컬 시험에서 실제 발생)."""
    today = datetime.datetime.now(datetime.timezone.utc).date()
    net_fail = 0
    for back in range(SEARCH_DAYS):
        d = today - datetime.timedelta(days=back)
        key = f"rating/{d.year}/{d.month:02d}/{d.day:02d}/{code}.rsrpl.gz"
        try:
            _http(f"{BUCKET}/{key}", method="HEAD", timeout=10)
            return key, d.isoformat()
        except urllib.error.HTTPError as e:
            if e.code not in (403, 404):
                raise
        except urllib.error.URLError as e:
            net_fail += 1
            if net_fail >= 3:                 # 계속 고장이면 탐색 실패가 아니라 통신 실패다
                raise RuntimeError(f"S3 통신 실패: {e.reason}")
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
    sk = _c.Counter((pid, k) for _, pid, k in d.get("skills", []))

    start = d.get("start") or {}
    modbits = start.get("ModifierType") or 0
    players = []
    for pid, p in sorted(d["players"].items()):
        c = st[pid]
        n, tot = fp[pid]
        players.append({
            "pid": pid, "name": p.get("name") or f"P{pid}", "bot": bool(p.get("bot")),
            "team": p.get("team"), "hero": (p.get("hero") or "?").capitalize(),
            "level": p.get("hero_level"), "sure": bool(p.get("hero_sure")),
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

    srows, flips = A.score_timeline(d)
    deaths_out = []
    for ev in A.death_details(d):
        deaths_out.append({"s": _sec(ev["ms"], t0), "tgt": ev["tgt"],
                           "by": ev["killers"],
                           "dmg": sorted(([q, v] for q, v in ev["dmg_by"].items()),
                                         key=lambda x: -x[1]),
                           "burst": round(ev["burst_s"], 1),
                           "solo": ev["solo"], "near": len(ev["allies_near"])})
    ults = [{"s": _sec(ms, t0), "pid": pid}
            for ms, pid, kind in d.get("skills", []) if kind == "궁"]
    objs = [{"s": _sec(ms, t0), "kind": h[0], "by": h[1]}
            for ms, h in d["logs"]
            if h[0] in (8, 29) and h[1] in d["players"]]
    fights_out = [{"a": _sec(a, t0), "b": _sec(b, t0), "dmg": tot,
                   "lost": {str(k): v for k, v in lost.items()}, "win": win}
                  for a, b, tot, lost, win in A.fight_rounds(d)]
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
        "flips": [[_sec(ms, t0), team] for ms, team in flips],
        "ults": ults, "objs": objs, "deaths": deaths_out,
        "fights": fights_out, "spots": spots, "traces": traces, "heat": heat,
        # 🎓 코칭 판정 — 서버는 (키+숫자)만 보내고 문장은 사이트가 언어별 템플릿으로 만든다
        "coach": A.coach(d),
    }
    return report, ok_ratio


@app.get("/health")
def health():
    return jsonify({"ok": True, "cached": len(_cache)})


@app.get("/analyze/<raw_code>")
def analyze(raw_code):
    code = raw_code.strip().upper()
    if not CODE_RE.match(code):
        return err("bad_code", "매치 코드 형식이 아니에요 (영문 대문자·숫자).",
                   "Not a valid match code.")
    hit = _cache.get(code)
    if hit:
        return jsonify(hit[1])

    if not _lock.acquire(timeout=150):        # 앞 요청이 너무 오래 물고 있으면
        return err("busy", "지금 다른 경기를 분석하는 중이에요. 잠시 뒤 다시 눌러 주세요.",
                   "Server is busy analyzing another match. Try again shortly.")
    try:
        hit = _cache.get(code)                # 기다리는 동안 끝났을 수도
        if hit:
            return jsonify(hit[1])
        key, s3date = find_on_s3(code)
        if not key:
            return err("not_found",
                       "리플레이를 못 찾았어요. 코드 오타이거나, 40일이 지나 서버에서 지워진 경기예요.",
                       "Replay not found — wrong code, or older than 40 days (deleted).")
        dst = os.path.join(REPLAY_DIR, f"{code}.rsrpl.gz")
        try:
            with _http(f"{BUCKET}/{key}", timeout=60) as r:
                data = r.read()
            os.makedirs(REPLAY_DIR, exist_ok=True)
            open(dst, "wb").write(data)
            # 게임 버전은 파일 맨 앞에 있다 — 앞 64KB만 부분 해제해서 뽑는다
            head = zlib.decompressobj(47).decompress(data[:65536]).decode("utf-8", "replace")
            m = re.search(r'"CompatibilityVersion":"([^"]+)"', head)
            gamever = m.group(1) if m else None
            S.cmd_save([code])                # 애니 항목 수 풀기 (경기마다 다르다)
            report, ok_ratio = build_report(code, s3date)
            if report is None:
                return err("version_mismatch",
                           f"이 리플레이는 아직 못 읽어요 (완주율 {ok_ratio*100:.0f}%). "
                           "게임이 업데이트된 직후라면 분석기 갱신을 기다려 주세요.",
                           "Cannot parse this replay yet — likely a new game version.")
            report["gamever"] = gamever
            if len(_cache) >= _CACHE_MAX:     # 캐시 자리 비우기 (오래된 것부터)
                oldest = min(_cache, key=lambda k: _cache[k][0])
                del _cache[oldest]
            _cache[code] = (time.time(), report)
            return jsonify(report)
        finally:
            if os.path.exists(dst):
                os.remove(dst)                # 디스크는 임시 — 결과만 남긴다
    except Exception as e:                    # 예상 밖 오류는 정직하게
        app.logger.exception("analyze 실패: %s", code)
        return err("internal", f"분석 중 오류가 났어요 ({type(e).__name__}). "
                   "같은 오류가 반복되면 피드백으로 알려 주세요.",
                   f"Analysis failed ({type(e).__name__}).")
    finally:
        try:
            _lock.release()
        except RuntimeError:
            pass


@app.after_request
def cors(resp):
    # 리포트는 공개 데이터 — 어느 origin 이든 읽게 둔다 (쓰기는 어차피 여기 없음)
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Cache-Control"] = "no-store"
    return resp


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
