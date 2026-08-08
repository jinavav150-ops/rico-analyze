#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""🏆 경기 분석 — 리플레이 파일 하나로 **"몇 초에 무슨 일이 있었는지"** 를 뽑는다

이 프로젝트의 **목표물**이다. 앞의 도구들이 비트를 읽어 주면, 여기서 사람이 읽는 글로 바꾼다.

## 무엇을 뽑나
- **명단**: 팀 · 선수 · **영웅+레벨** (신호 셋 교차 — `identify()` 참고)
- **선수별 요약**: 처치/죽음/어시 · 준·받은 피해 · 구조물 · 회복 · 실드 · 스킬/궁 횟수
- **타임라인**: 사망(누가 잡았는지 묶어서) · 궁 사용 · 포탑 막타 · 거점 점령
- **교전 구간**: 피해가 몰린 시간대
- **검증**: 게임이 직접 보내는 전적(`PlayerStats`)과 대조해 **스스로 채점**

## 재료가 어디서 오는지 (2026-08-07 7차에 전부 확인)
- 초기 상태(Restore)에 **명단이 통째로** 들어 있다:
  `Player` 엔티티(176·178·180 = 2팀 / 182·184·186 = 1팀)마다
  `Team`·`PlayerStats`·`CurrentHero.HeroNetworkId`(→ 캐릭터 엔티티) 가 있다.
- `MatchLog` 의 가해자·피해자 번호가 **바로 그 Player 엔티티 번호**다.
- 캐릭터 엔티티에는 `Transform`(좌표)·`Hp`(체력)가 매 틱 실려 온다.

✅ `MatchLog` 의 종류별 switch 는 2026-08-07 7차에 CFG 로 **전부 풀렸다**(`replay_stream._match_log`).
   `Value` 는 종류 0·1·24·27 에만 실려 온다(`HAS_VALUE`) — 나머지는 아예 안 보낸다.

⚠️ **처치 합 ≠ 죽음 합.** 한 번의 죽음에 여러 명이 처치 크레딧을 받는다(실측 16 vs 7).
   그래서 죽음은 `deaths()` 가 `(대상, 초)` 로 묶어 **사건 하나**로 센다.

## 쓰는 법
    python3 도구/경기분석.py TLJXLVAF              # 글로 출력
    python3 도구/경기분석.py TLJXLVAF --html out.html
"""
import os
import sys
import html
import json
import collections
import itertools

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import replay_stream as R
import restore_state as S

HERE = os.path.dirname(os.path.abspath(__file__))

# MatchLogType (dump.cs 1846045행)
LOG = {0: "피해", 1: "회복", 2: "스킬발동", 3: "스킬사용", 4: "처치", 5: "결정타", 6: "어시스트",
       7: "기절", 8: "거점점령", 9: "거점방어처치", 10: "거점경합시간", 11: "거점방어시간",
       12: "거점점령시간", 13: "재생", 14: "연속처치", 15: "구급킷회복", 16: "조건부피해",
       17: "조건부처치", 18: "궁게이지획득", 19: "아이템획득", 20: "아이템보유처치",
       21: "모디파이어", 22: "물리투척", 23: "아이템보유시간", 24: "실드획득", 25: "화염시간",
       26: "모디파이어실드", 27: "비영웅피해", 28: "포탑피해", 29: "포탑막타",
       30: "조건부피해받음", 31: "전탄명중", 32: "적중첩", 33: "중첩피해", 34: "아이템드롭"}

# `Value`(피해량 등)가 **실제로 실려 오는** 종류. 나머지는 아예 안 보낸다.
# (2026-08-07 7차에 `SetValue` 0x6EE79EC 의 CFG 로 확정 — 진행기록 "MatchResultLog" 참고)
HAS_VALUE = {0, 1, 24, 27}

# 타임라인에 올릴 사건
# 타임라인에 따로 올릴 사건 (사망은 `deaths()` 가 묶어서 따로 낸다)
BIG = {7: "😵 기절", 29: "🏹 포탑 막타", 8: "🚩 거점 점령"}

# 처치 **크레딧**으로 세는 종류 — 게임의 `EliminationsCount` 와 정확히 맞는다(18/18명 검증)
KILL_CREDIT = {4, 17, 20}
# **사망 사건**으로 세는 종류 — 크레딧 없이 `결정타(5)` 만 남는 죽음이 있다(2026-08-07 실측).
#   예: TLJXLVAF P180 은 91299ms 에 결정타만 있고 처치 로그가 없는데 게임은 죽음 1로 센다.
DEATH_KINDS = KILL_CREDIT | {5}


def collect(name):
    """초기 상태 + 실시간 프레임을 한 번에 읽어 필요한 것만 모은다."""
    R.use_entity_counts(name)
    rep = R.load(S.replays()[name])

    # ── ⓪ 파일 앞머리(StartData)에 **명단이 통째로** 있다 ────────────────
    # 🎉 2026-08-07 7차 막바지 발견: 닉네임·팀·봇여부·영웅id·레벨이 전부 여기 있다.
    #    영웅을 추론할 필요가 없었다. (그래도 추론은 남겨 둔다 — 서로 대조하면 검증이 된다)
    start = rep.get("StartData") or {}
    heroes_tbl = _load("hero_names.json")
    roster = []
    for pl in start.get("Players", []):
        # 🪤 **`HeroId` 가 있을 때만** 영웅이 확정된다.
        #    연습전은 영웅을 자유롭게 바꿀 수 있어서 `HeroId` 가 없고, `Heroes` 에는
        #    **보유한 13명 전부**가 들어온다. 그때 `Heroes[0]` 을 쓰면 전원이 같은 영웅으로 나온다
        #    (Q6TSVQ8B 에서 실제로 전원 "Se-jin" 이 나왔고, 대조 검사가 바로 잡아냈다).
        #    → 그런 경기는 파서의 추론(신호 셋 교차)을 그대로 쓴다.
        hs = pl.get("Heroes") or []
        hid = str(pl.get("HeroId") or "")
        if not hid and len(hs) == 1:
            hid = str(hs[0].get("Id") or "")
            # 🪤 봇이라도 Heroes 가 **13개 전부**일 때가 있다(WX57C1JU 봇 2명, 2026-08-08).
            #    "봇 = Heroes 1개" 가정은 깨졌다 — 1개일 때만 확정으로 쓰고, 아니면 추론에 맡긴다.
        h = next((x for x in hs if str(x.get("Id")) == hid), None)
        roster.append({"match_id": pl.get("PlayerMatchId"), "team": pl.get("Team"),
                       "name": pl.get("Name"), "bot": bool(pl.get("IsBot")),
                       "hero_id": hid, "level": (h or {}).get("Level"),
                       "hero_name": (heroes_tbl.get(hid) or {}).get("en") if hid else None,
                       # 🆔 영구 계정 ID — 닉네임은 바뀌고 겹치지만 이건 안 그렇다.
                       #    누적 통계는 반드시 이걸로 묶어야 한다(2026-08-09 12차):
                       #    같은 사람이 개명하면 두 사람이 되고, 보이지 않는 닉네임(채움문자)을
                       #    쓰는 **서로 다른 두 사람**이 한 행으로 합쳐지는 일이 실제로 있었다.
                       "player_id": pl.get("PlayerId"),
                       "rating": pl.get("Rating"), "squad": pl.get("SquadId"),
                       # 🎽 선택한 퍽 4개 — 리포트 결과 카드에 빌드를 보여 줄 수 있다
                       "perks": [pk.get("Id") for pk in ((h or {}).get("Perks") or [])
                                 if pk.get("Selected")],
                       # 보유 영웅 전체의 레벨 — 추론으로 영웅이 정해진 뒤 레벨을 채우는 데 쓴다
                       "levels": {str(x.get("Id")): x.get("Level") for x in hs}})

    # ── ① 초기 상태에서 명단 ────────────────────────────────────────────
    r0 = S.walk(S.restore_bytes(name), S.HEAD_CONNECT, outer=False)
    info = collections.defaultdict(dict)
    restore_comps = collections.defaultdict(set)   # 엔티티 → 컴포넌트 지문(신호 ④)
    for e, t, c, v in r0["events"]:
        restore_comps[e].add(c)
        if c in ("Team", "CurrentHero", "PlayerStats", "AvailableHero", "Player"):
            info[e][c] = v
    players = {}
    for e, d in info.items():
        if "CurrentHero" in d and "Team" in d:
            players[e] = {"team": d["Team"].get("CurrentTeam"),
                          "hero_entity": d["CurrentHero"].get("HeroNetworkId")}
    # 영웅 판별은 아래 ③ 에서 (애니 파라미터 개수 ∩ 최대체력)

    # ── ② 실시간 프레임에서 사건·좌표·체력 ───────────────────────────────
    logs, hp, pos = [], collections.defaultdict(list), collections.defaultdict(list)
    skills, owner_of, ult_ents = [], {}, set()   # 발동 · 스킬엔티티→캐릭터 · 궁 엔티티
    respawns = collections.defaultdict(list)     # 엔티티 → 부활 시각들 (파일이 주는 실측값)
    stats = {}                                   # 선수 → 게임이 보낸 전적(검증용)
    score = {}                                   # 모드별 점수(승패 판정용)
    score_tl = []                                # 점수 변화 타임라인 (ms, 컴포넌트, {필드:값})
    hero_ents = {p["hero_entity"] for p in players.values()}
    frames = ok = 0
    for b in [x for k, x in R.frames(rep) if k == R.T_DATA]:
        fr = R.parse_frame(b)
        frames += 1
        if (fr["stopped"] or "") in ("end", "EOF", ""):
            ok += 1
        ms = fr["time"]
        for e, t, c, v in fr["events"]:
            if c == "MatchLog":
                for hit in v.get("Data", []):
                    if hit[0] in LOG:                 # 있을 수 없는 종류는 버린다
                        logs.append((ms, hit))
            elif c == "Hp" and "Hp" in v:
                hp[e].append((ms, v["Hp"], v.get("MaxHp")))
            elif c == "Transform" and "Position" in v:
                pos[e].append((ms, v["Position"]))
            elif c in ("Abilities", "ActiveAbilities") and e in hero_ents:
                # 스킬 하위 엔티티의 주인을 기록해 둔다(이웃 번호만 — 나머지는 어긋난 값)
                ids = v.get("AllAbilitiesNetworkIds") or v.get("ActiveAbilityNetworkIds") or []
                for i in ids:
                    if e < i < e + 40:
                        owner_of[i] = e
            elif c in SCORE_FIELDS:
                vals = {k: v[k] for k in v if k in SCORE_FIELDS[c]}
                score.setdefault(c, {}).update(vals)
                if vals:                              # 점수는 바뀔 때만 실려 온다 = 그대로 타임라인
                    score_tl.append((ms, c, vals))
            elif c == "PlayerStats" and e in players:
                stats.setdefault(e, {}).update(v)     # 게임이 직접 보내는 전적 = 채점표
            elif c == "AbilityUsageState" and v.get("UsageTrigger"):
                skills.append((ms, e, "스킬"))
            elif c == "UltimateAbility":
                # 이 엔티티가 **궁** 이라는 표시. 충전 진행도라 사용 시점이 아니다 —
                # 실제 사용은 위 `AbilityUsageState` 가 이 엔티티에서 뜰 때다.
                ult_ents.add(e)
            elif c == "Respawn" and "RespawnCounter" in v:
                # 🎉 **부활 시각이 파일에 있다** (2026-08-09 12차 발견).
                #    예전엔 "좌표가 다시 오는 때"로 추정했는데, 은신(칼리브리)처럼
                #    살아 있어도 좌표가 끊기는 영웅에서 추정이 통째로 빗나갔다.
                respawns[e].append(ms)
    # ── ③ 영웅 판별 — 추론(신호 넷)과 StartData 를 **서로 대조**한다 ─────
    for p in players.values():
        p.update(identify(name, p["hero_entity"], hp,
                          comps=restore_comps.get(p["hero_entity"])))
    # 선수 엔티티는 팀 안에서 번호 순 = StartData 의 PlayerMatchId 순으로 짝지어진다.
    # (세 경기 18명 전부에서 추론 결과와 StartData 의 영웅·레벨이 일치해 확인됨)
    #
    # 🪤 **그 가정이 깨지는 경기가 있다** (2026-08-09 12차 감사, J2VRV3D4).
    #    엔티티 457·461 이 서로 바뀌어 **영웅도 닉네임도 통째로 뒤바뀐** 리포트가 나갔다
    #    (콜드브루=실제 레메디인데 노바로, 그 반대도). 추론(identify)은 양쪽 다 정답을
    #    가리키고 있었는데 파일값이 덮어써 버린 것.
    #    → 짝짓기 전에 **추론과 파일이 모두 확정이면서 서로 어긋나는지** 보고,
    #      순서를 바꾸면 둘 다 맞아떨어지는 조합이 있으면 그걸 쓴다.
    for team in {r["team"] for r in roster}:
        ents = sorted(e for e, q in players.items() if q["team"] == team)
        rs = sorted((r for r in roster if r["team"] == team), key=lambda r: r["match_id"] or 0)
        rs = _fix_pairing(players, ents, rs)
        for e, r in zip(ents, rs):
            q = players[e]
            q["name"], q["bot"] = r["name"], r["bot"]
            q["player_id"] = r.get("player_id")
            q["rating"] = r.get("rating")
            q["squad"] = r.get("squad")
            q["perks"] = r.get("perks") or []
            guess = (q.get("hero") or "").lower()
            true_name = (r["hero_name"] or "").lower().replace("-", "")
            q["hero_src"] = "파일" if r["hero_name"] else "추론"
            q["hero_ok"] = (bool(true_name) and guess == true_name
                            and q.get("hero_level") == r["level"]) if r["hero_name"] else None
            if r["hero_name"]:                       # 파일에 있는 값이 진실이다
                q["hero"], q["hero_level"], q["hero_sure"] = r["hero_name"], r["level"], True
            elif q.get("hero_sure") and q.get("hero_level") is None:
                # 추론으로 영웅이 정해졌는데 레벨이 비면(퍽이 체력을 바꿔 ②가 빗나간 경우)
                # StartData 의 보유 영웅 목록에서 그 영웅의 레벨을 찾아 채운다 — 파일값이라 정확
                tbl = _load("hero_names.json")
                hid = next((k for k, v in tbl.items()
                            if (v.get("en") or "").lower().replace("-", "") == guess), None)
                if hid and r.get("levels", {}).get(hid) is not None:
                    q["hero_level"] = r["levels"][hid]
    # ── ③-b 이름 정규화 — 여기서부터 아래는 **표기가 하나**다 ────────────
    #    `hero` = 소문자·하이픈 없는 키(집계·조회용) / `hero_disp` = 공식 표기(화면용).
    #    ⚠️ 위 hero_ok 대조가 끝난 **뒤**에 해야 한다(대조는 자체적으로 정규화해 비교함).
    heroes_en = {hero_key(v.get("en")): v.get("en") for v in heroes_tbl.values() if v.get("en")}
    for q in players.values():
        q["hero"] = hero_key(q.get("hero"))
        q["hero_disp"] = "/".join(heroes_en.get(x, x.capitalize())
                                  for x in q["hero"].split("/")) if q["hero"] else "?"

    # 스킬 엔티티 → 선수 번호 (주인 캐릭터 → 그 캐릭터를 쓰는 선수)
    #   오프셋(스킬엔티티 − 영웅엔티티)은 **어느 스킬인지**의 지문이다(스킬표.json).
    ent2pid = {p["hero_entity"]: pid for pid, p in players.items()}
    raw = []
    for ms, e, kind in skills:
        he = owner_of.get(e)
        if he in ent2pid:
            raw.append((ms, ent2pid[he], "궁" if e in ult_ents else "스킬", e - he))
    skill_use = _cluster(raw)
    # 부활 시각은 **선수 번호**로 바꿔 둔다 (Respawn 은 선수 엔티티에 실려 온다)
    resp = {pid: sorted(respawns.get(pid, [])) for pid in players}
    return {"players": players, "logs": logs, "hp": hp, "pos": pos, "skills": skill_use,
            "stats": stats, "score": score, "score_tl": score_tl, "ent2pid": ent2pid,
            "respawns": resp,
            "frames": frames, "ok": ok, "start": start,
            "t0": min((m for m, _ in logs), default=0)}


# 발동 트리거를 **사용 횟수**로 묶는 간격(ms)
#   🪤 지속형 스킬은 쓰는 동안 트리거가 계속 뜬다. 그대로 세면 횟수가 부풀려진다.
#      실측: Dread 의 궁은 한 번 쓰면 약 5초간 트리거가 9번까지 떴다
#      (61.7~66.8초에 9번, 108.1~113.2초에 5번 → 실제로는 **2번 사용**).
#   그래서 같은 선수의 연속 트리거를 아래 간격 안이면 **한 번**으로 센다.
#   🪤 5000 은 **너무 빠듯했다** (2026-08-09 12차 감사): 벡터 궁(지팡이 변신)은
#      변신·복귀 두 번 트리거되고 간격이 **정확히 5040ms** 라 40ms 차로 안 묶여
#      전 경기에서 궁 횟수가 **정확히 2배**로 나왔다(ZDAVTZPC 사람 "궁 6번" ← 실제 3번).
#      드레드는 정확히 5000ms 라 `<=` 경계에 우연히 걸려 살아 있었다 — 여유를 준다.
CLUSTER_MS = {"궁": 6000, "스킬": 800}


def _cluster(raw):
    """(시각, 선수, 종류, 오프셋) 목록에서 같은 선수·같은 스킬의 연속 트리거를 묶는다.

    오프셋(스킬엔티티 − 영웅엔티티)까지 함께 받아 **어느 스킬인지**를 유지한다.
    묶는 단위도 선수·종류가 아니라 **선수·스킬**이다 — 서로 다른 스킬을 연달아 쓰면
    각각 세야 하기 때문. (예전에는 종류로만 묶어 0.8초 안의 다른 스킬이 하나로 합쳐졌다)
    """
    out, last = [], {}
    for ms, pid, kind, off in sorted(raw):
        key = (pid, kind, off)
        if key in last and ms - last[key] <= CLUSTER_MS.get(kind, 800):
            last[key] = ms                      # 같은 사용의 연장 — 세지 않는다
            continue
        last[key] = ms
        out.append((ms, pid, kind, off))
    return out


def _load(fn):
    try:
        return json.load(open(os.path.join(HERE, fn), encoding="utf-8"))
    except FileNotFoundError:
        return {}


# ── ④ 네 번째 신호: **영웅별 컴포넌트 지문** (2026-08-08, 리플레이 14개 실측) ──
#   애니 개수가 완전히 같은 쌍(Calibri≡Nova · Magnus≡Sejin)은 ①~③으로 절대 못 가른다.
#   초기 상태(Restore)의 컴포넌트 **구성 자체**가 영웅 키트를 따르는 걸 이용한다:
#   · `EnableInvisible`: Calibri 6/6 보유, 다른 영웅 78명 중 0건 (은신 키트 —
#     퍽 개발명 `calibri_invisB_…` 로도 교차 확인)
#   · `BlinkAbility`(+`LaserAbility`): Sejin 3/3 보유, Magnus 0/6
#   ⚠️ **퍽이 최대체력을 바꿀 수 있어** ②가 빗나가는 경기가 실제로 있다:
#     WX57C1JU 봇 Calibri Lv15 = 성장표 632 인데 실측 730 (`calibri_passiveA_hp_perk` +98).
#     그때 이 지문이 잡는다. (쌍, 지문 컴포넌트, 있으면, 없으면)
FINGERPRINT = [
    ({"calibri", "nova"}, "EnableInvisible", "calibri", "nova"),
    ({"magnus", "sejin"}, "BlinkAbility", "sejin", "magnus"),
]


def hero_key(s):
    """영웅 이름을 **하나의 표기**로 — 소문자·하이픈 제거 (`Se-Jin`·`sejin` → `sejin`).

    🪤 이게 없어서 같은 영웅이 두 갈래로 집계됐다(2026-08-09 12차 감사).
       StartData 경로는 영웅표의 `Se-Jin`(공식 표기), 추론 경로는 체력표 키 `sejin` 을 넣어
       한 경기 안에서도 표기가 섞였고 → 누적통계가 갈라지고, 사이트 초상화 키(`sejin`)와
       어긋나 **세진 초상화가 "?" 로 깨져 있었다.**
       후보가 여럿일 때 쓰는 `a/b` 병기 표기도 각 조각을 따로 정규화한다.
    """
    return "/".join(x.lower().replace("-", "") for x in str(s or "").split("/"))


def _fix_pairing(players, ents, rs):
    """엔티티 ↔ StartData 명단 짝짓기가 어긋났으면 바로잡는다.

    기본 가정은 "팀 안에서 엔티티 번호순 = PlayerMatchId 순"이고 거의 항상 맞다.
    🪤 그런데 J2VRV3D4 에서 깨졌다(2026-08-09 12차 감사): 두 선수의 **영웅도 닉네임도
       통째로 뒤바뀐** 리포트가 나갔다. 추론(identify)은 양쪽 다 정답을 가리키고 있었는데
       파일값이 그대로 덮어써서 아무도 몰랐다.
    → **추론이 확정(hero_sure)인 자리만** 채점해서, 순서를 바꾸면 더 잘 맞는 조합이
      있으면 그걸 쓴다. 증거가 없으면(추론이 확정 아님·파일에 영웅 없음) 손대지 않는다.
    """
    if len(ents) != len(rs) or len(ents) > 6:      # 6명 넘으면 순열이 과해진다 — 손대지 않음
        return rs

    def score(order):
        """맞으면 +1, 어긋나면 -1. 양쪽 다 확정일 때만 센다."""
        s = 0
        for e, r in zip(ents, order):
            q = players[e]
            if not q.get("hero_sure") or not r.get("hero_name"):
                continue
            s += 1 if hero_key(q.get("hero")) == hero_key(r["hero_name"]) else -1
        return s

    base = score(rs)
    if base >= 0:                                   # 어긋난 증거가 없으면 그대로
        return rs
    best, best_s = rs, base
    for order in itertools.permutations(rs):
        s = score(order)
        if s > best_s:
            best, best_s = list(order), s
    if best_s > base:
        names = " / ".join(f"{r.get('name')}→{r.get('hero_name')}" for r in best)
        print(f"   ⚠️ 명단 짝짓기 교정(점수 {base}→{best_s}): {names}")
    return best


def identify(replay, hero_entity, hp, comps=None):
    """영웅 판별 — **신호 넷을 교집합** 낸다. 하나만으로는 절대 못 정한다.

    ① **애니메이션 파라미터 개수** + ③ **실시간 프레임 완주율**
       (`초기상태.py` 의 `solve_anim` + `refine_with_data` 가 맞춰 `애니세트_경기별.json` 에 저장)
    ② **최대 체력** + 사이트 성장표(`영웅체력.json`, `hp1 + hpI×(Lv-1)`) → 영웅 후보 + **레벨**
    ④ **컴포넌트 지문**(`FINGERPRINT`) — ①~③으로 못 가르는 쌍의 마지막 결정타

    각각은 헐겁다(①은 지도 게이트가 0이면 개수가 안 쓰임, ②는 성장값이 같은 영웅끼리 못 가르고
    **퍽이 체력을 올리면 아예 빗나감**, ③은 차이가 0.5~1%). **겹치면 유일해진다.**
    ⚠️ 그래도 못 가르는 후보가 남으면 병기하고, `label()` 이 번호(`P176`)로 부른다.
    """
    # ⚠️ 솔버가 "고른" 하나가 아니라 **끝까지 읽히는 후보 전부**를 쓴다(그게 정직한 집합이다)
    saved = _load("anim_sets.json").get(replay, {})
    cands = saved.get("엔티티별_후보", {}).get(str(hero_entity))
    if cands is None:
        one = saved.get("엔티티별_영웅", {}).get(str(hero_entity))
        cands = [one] if one else []
    anim_set = {x for c in cands for x in c.lower().split("/")} or None

    growth = _load("hero_hp.json")
    maxhp = next((mx for _, _, mx in hp.get(hero_entity, []) if mx), None)
    hp_set, level = None, None
    if maxhp and growth:
        hits = []
        for k, g in growth.items():
            if g["hpI"] <= 0:
                continue
            lv = (maxhp - g["hp1"]) / g["hpI"] + 1
            if abs(lv - round(lv)) < 0.02 and 1 <= round(lv) <= 15:
                hits.append((k, int(round(lv))))
        if hits:
            hp_set = {k for k, _ in hits}
            level = hits[0][1]

    if anim_set and hp_set:
        both = anim_set & hp_set
        cand = both or (anim_set | hp_set)     # 교집합이 비면 모순 → 합집합으로 정직하게
        sure = len(cand) == 1 and bool(both)
    else:
        cand = anim_set or hp_set or set()
        sure = False

    # ④ 컴포넌트 지문 — 정확히 그 쌍일 때만 개입한다(다른 조합엔 증거가 없다)
    fp_used = None
    if comps and len(cand) == 2:
        norm = {c.lower().replace("-", "") for c in cand}
        for pair, comp, has, hasnot in FINGERPRINT:
            if norm == pair:
                pick = has if comp in comps else hasnot
                cand = {c for c in cand if c.lower().replace("-", "") == pick}
                sure, fp_used = True, comp
                break

    return {"hero_candidates": sorted(cand), "hero_level": level, "hero_sure": sure,
            "hero": (sorted(cand)[0] if len(cand) == 1 else "/".join(sorted(cand)) or "?"),
            "max_hp": maxhp, "fp": fp_used}


# 모드별 **점수 필드** — 승패는 이걸로 판정한다 (`SendResult` 메시지는 리플레이에 없다)
SCORE_FIELDS = {
    "TowersMode":  ("Team1Score", "Team2Score"),           # 강타
    "BagMode":     ("Team1Score", "Team2Score"),           # 사수
    "GemGrabMode": ("Team1Score", "Team2Score"),           # 사냥
    "Capturepoint": ("Team1CapturedPoints", "Team2CapturedPoints",
                     "Team1CaptureScore", "Team2CaptureScore"),   # 장악·점멸
}


def winner(d):
    """이긴 팀 → (1|2|None, 설명). 모드별 마지막 점수를 비교한다."""
    for comp, keys in SCORE_FIELDS.items():
        v = d.get("score", {}).get(comp)
        if not v:
            continue
        a, b = v.get(keys[0], 0) or 0, v.get(keys[1], 0) or 0
        if a == b and len(keys) > 2:                 # 점령 수가 같으면 점령 점수로
            a, b = v.get(keys[2], 0) or 0, v.get(keys[3], 0) or 0
        if a != b:
            return (1 if a > b else 2), f"{comp} {a}:{b}"
        return None, f"{comp} {a}:{b} (동점)"
    return None, "점수 못 읽음"


def match_kind(d):
    """이 경기가 **어떤 경기인지** — 코칭 결론을 낼 때 이걸 먼저 봐야 한다.

    봇이 섞였거나 연습전이면 "잘했다/못했다" 를 사람 실력으로 읽으면 안 된다.
    """
    st = d.get("start") or {}
    bots = sum(1 for p in d["players"].values() if p.get("bot"))
    bits = []
    if st.get("IsTraining"):
        bits.append("연습전")
    if bots:
        bits.append(f"봇 {bots}명 포함")
    if st.get("AveragePlayerRating"):
        bits.append(f"평균 레이팅 {st['AveragePlayerRating']}")
    warn = ("⚠️ 봇/연습 경기다 — 사람 실력으로 읽지 말 것"
            if (bots or st.get("IsTraining")) else "사람만 참여한 경기")
    return " · ".join(bits) or "정보 없음", warn


def label(d, pid):
    """짧은 이름. **영웅이 확정된 사람만 이름으로 부르고, 아니면 선수 번호로 부른다.**

    후보를 길게 늘어놓으면 읽을 수가 없다. 그렇다고 하나를 골라 쓰면 거짓말이 된다.
    → 확정된 사람만 이름, 나머지는 `P176` 같은 번호. 후보 목록은 【명단】에만 적는다.
    """
    p = d["players"].get(pid)
    if not p:
        return f"#{pid}"
    t = p["team"]
    who = p.get("name") or (f"P{pid}")
    bot = "🤖" if p.get("bot") else ""
    if p.get("hero_sure"):
        lv = f" Lv{p['hero_level']}" if p.get("hero_level") else ""
        return f"{who}{bot}({p.get('hero_disp') or p['hero'].capitalize()}{lv}·{t}팀)"
    return f"{who}{bot}({t}팀)"


def roster_line(d, pid):
    """명단용 — 후보와 근거를 다 보여준다."""
    p = d["players"][pid]
    bot = " 🤖봇" if p.get("bot") else ""
    who = p.get("name") or f"P{pid}"
    if p.get("hero_sure"):
        lv = f" Lv{p['hero_level']}" if p.get("hero_level") else ""
        # 파일의 값(진실)과 우리 추론이 맞았는지도 같이 보여 준다 = 파싱 검증
        if p.get("hero_ok") is None:
            # HeroId 없는 경우는 연습전만이 아니다 — 봇이 13명 전부 들고 오거나(WX57C1JU),
            # 랭크전 사람인데도 안 적힌 경우(같은 경기 Vampi)가 실제로 있었다.
            chk = "파서 추론 (파일에 영웅이 안 적힘)"
            if p.get("fp"):
                chk += f" + 컴포넌트 지문({p['fp']})"
        else:
            chk = "파일값·추론 일치 ✅" if p["hero_ok"] else "파일값·추론 불일치 ⚠️"
        return f"{who}{bot} = {p.get('hero_disp') or p['hero'].capitalize()}{lv}  ({chk}, 최대체력 {p.get('max_hp') or '?'})"
    cands = "/".join(x.capitalize() for x in p.get("hero_candidates") or ["?"])
    return f"{who}{bot} = {cands} 중 하나  ⚠️미확정"


def summarize(d):
    """선수별 합계. 값이 불확실한 종류는 합계에서 뺀다."""
    st = {p: collections.Counter() for p in d["players"]}
    for ms, h in d["logs"]:
        kind, owner, target, _flag = h[0], h[1], h[2], h[3]
        val = h[4] if (kind in HAS_VALUE and len(h) > 4 and h[4] is not None) else 0
        if owner in st:
            if kind == 0:
                st[owner]["준피해"] += val
            elif kind == 27:
                st[owner]["구조물피해"] += val
            elif kind in (1, 13, 15):
                st[owner]["회복"] += val
            elif kind == 24:
                st[owner]["실드"] += val
            elif kind in KILL_CREDIT:
                st[owner]["처치"] += 1
            elif kind == 6:
                st[owner]["어시"] += 1
        if target in st and kind == 0:
            st[target]["받은피해"] += val
    # 죽음은 **사건 단위**로 센다.
    # 🪤 처치(Elimination)는 **한 번의 죽음에 여러 명이 크레딧**을 받는다
    #    (게임의 EliminationsCount 합 16 vs DeathsCount 합 7 — 2026-08-07 실측).
    #    그래서 로그를 그대로 세면 죽음이 부풀려진다. 같은 대상·같은 순간을 하나로 묶는다.
    seen = set()
    for ms, h in sorted(d["logs"]):
        if h[0] not in DEATH_KINDS or h[2] not in st:
            continue
        key = (h[2], ms // 1000)
        if key in seen or (h[2], ms // 1000 - 1) in seen:
            continue
        seen.add(key)
        st[h[2]]["죽음"] += 1
    return st


def deaths(d):
    """죽음을 **사건 하나**로 묶는다 — 같은 대상·같은 순간의 처치 크레딧을 합친다.

    한 번의 죽음에 여러 명이 처치 크레딧을 받으므로(게임 규칙), 로그를 그대로 늘어놓으면
    같은 죽음이 서너 줄로 보인다. `(대상, 초)` 로 묶어 "누구 사망 ← 잡은 사람들" 한 줄로.

    🪤 **죽음 사건은 잡은 쪽이 누구든 성립한다** (2026-08-09 12차 감사).
       예전에는 `h[1] not in players` 로 걸러서 **터렛·포탑 등 비플레이어에게 잡힌 죽음이
       통째로 사라졌다**(FZ4Y6HCL P168: 여기선 5건, 게임 DeathsCount 는 6). 그 죽음은
       사망 지도·사망 심화·생존율·교전 승패에서 전부 빠져 있었다.
       → 사건은 `대상이 선수` 이면 만들고, **잡은 사람 목록에만** 선수 조건을 건다.
    """
    ev = {}
    for ms, h in sorted(d["logs"]):
        if h[0] not in DEATH_KINDS or h[2] not in d["players"]:
            continue
        key = next((k for k in ev if k[0] == h[2] and abs(k[1] - ms) <= 1200), (h[2], ms))
        ev.setdefault(key, [])
        if h[0] in KILL_CREDIT and h[1] in d["players"] and h[1] not in ev[key]:
            ev[key].append(h[1])          # 잡은 사람은 **크레딧이 있는 선수만** 적는다
    return sorted((ms, tgt, who) for (tgt, ms), who in ev.items())


def death_spots(d):
    """사망마다 **그 순간 어디에 있었는지** — 피해자 캐릭터의 좌표를 시각으로 찾아 붙인다.

    좌표는 캐릭터 엔티티의 `Transform` 에 매 틱 실려 오므로, 사망 시각에 **제일 가까운**
    좌표를 고르면 된다(틱 간격 20ms라 오차는 무시할 수준).
    """
    out = []
    for ms, tgt, who in deaths(d):
        he = d["players"][tgt]["hero_entity"]
        track = d["pos"].get(he) or []
        best = min(track, key=lambda x: abs(x[0] - ms), default=None)
        if best and abs(best[0] - ms) <= 2000:
            out.append((ms, tgt, who, best[1]))
        else:
            out.append((ms, tgt, who, None))
    return out


def fight_rounds(d):
    """교전 구간마다 **어느 팀이 이겼나** — 그 구간에서 죽은 사람을 팀별로 센다."""
    rounds = []
    ds = deaths(d)
    for a, b, tot in fights(d):
        lost = collections.Counter()
        for ms, tgt, _who in ds:
            if a - 1500 <= ms <= b + 1500:
                lost[d["players"][tgt]["team"]] += 1
        teams = sorted({p["team"] for p in d["players"].values()})
        if len(teams) == 2:
            t1, t2 = teams
            win = t2 if lost[t1] > lost[t2] else (t1 if lost[t2] > lost[t1] else None)
        else:
            win = None
        rounds.append((a, b, tot, dict(lost), win))
    return rounds


def hp_track(d, pid, step=1000):
    """선수의 체력 곡선 — (시각, 체력비율 0~1). 그래프용으로 초 단위로 솎는다."""
    he = d["players"][pid]["hero_entity"]
    mx = d["players"][pid].get("max_hp") or 0
    out, last = [], None
    for ms, hp, m in d["hp"].get(he, []):
        if m:
            mx = m
        if not mx:
            continue
        if last is None or ms - last >= step:
            out.append((ms, max(0.0, min(1.0, hp / mx))))
            last = ms
    return out


def ult_value(d, window_ms=8000):
    """궁 **한 방의 값어치** — 궁을 쓰고 8초 안에 그 팀이 몇 명을 잡았나 / 몇 명을 잃었나.

    ⚠️ 인과가 아니라 **동시성**이다. 궁을 써서 잡은 건지, 이미 이기는 싸움에 얹은 건지는
       이걸로 못 가른다. 그래도 "궁을 쓰면 보통 무슨 일이 따라오나" 는 보인다.
    """
    ds = deaths(d)
    out = {}
    for pid, p in d["players"].items():
        used = [ms for ms, q, k, _off in d.get("skills", []) if q == pid and k == "궁"]
        got = lost = 0
        for ms in used:
            for dms, tgt, who in ds:
                if not (ms <= dms <= ms + window_ms):
                    continue
                if d["players"][tgt]["team"] != p["team"]:
                    got += 1
                else:
                    lost += 1
        out[pid] = (len(used), got, lost)
    return out


def alive_time(d):
    """선수별 **살아 있던 시간 비율**. 죽은 순간부터 다시 움직일 때까지를 죽어 있던 것으로 본다.

    부활 시각은 `Respawn`/`Player.State` 를 안 쓰고 **좌표가 다시 갱신되는 시점**으로 잡는다
    (죽어 있는 동안은 `Transform` 이 안 온다). 근사값이라 정확한 초가 아니라 **비율**로만 쓴다.
    """
    ds = deaths(d)
    t0 = d["t0"]
    tend = max((ms for ms, _ in d["logs"]), default=t0)
    span = max(1, tend - t0)
    out = {}
    for pid, p in d["players"].items():
        he = p["hero_entity"]
        track = sorted(m for m, _ in d["pos"].get(he, []))
        dead = 0
        for dms, tgt, _who in ds:
            if tgt != pid:
                continue
            back = next((m for m in track if m > dms + 500), None)
            dead += min(back - dms, 20000) if back else 5000     # 못 찾으면 5초로 잡는다
        out[pid] = max(0.0, 1 - dead / span)
    return out


def first_blood_order(d):
    """교전마다 **누가 먼저 피해를 주고받았나** — 진입 순서."""
    rows = []
    for a, b, tot, lost, win in fight_rounds(d):
        first = {}
        for ms, h in sorted(d["logs"]):
            if h[0] != 0 or not (a <= ms <= b):
                continue
            for pid in (h[1], h[2]):
                if pid in d["players"] and pid not in first:
                    first[pid] = ms
        rows.append((a, b, sorted(first.items(), key=lambda x: x[1])))
    return rows


def selfcheck(d):
    """🔍 **스스로 채점한다** — 게임이 직접 보내는 `PlayerStats` 와 우리가 센 값을 맞춰 본다.

    `PlayerStats.EliminationsCount` / `DeathsCount` 는 게임이 계산해 보내 주는 값이라
    **우리 파싱의 정답지**다. 여기서 어긋나면 숫자를 믿으면 안 된다.

    ⚠️ 처치와 죽음은 **합이 다르다** — 한 번의 죽음에 여러 명이 처치 크레딧을 받기 때문이다
    (실측: 처치 합 16 vs 죽음 합 7). 그래서 죽음은 같은 대상·같은 순간을 하나로 묶어 센다.
    """
    st = summarize(d)
    rows = []
    for pid in sorted(d["players"]):
        g = d.get("stats", {}).get(pid, {})
        rows.append((pid, g.get("EliminationsCount", 0) or 0, g.get("DeathsCount", 0) or 0,
                     st[pid]["처치"], st[pid]["죽음"]))
    ok = sum(1 for _, k, dd, mk, md in rows if k == mk and dd == md)
    return rows, ok


def fights(d, gap_ms=6000, min_dmg=200):
    """피해가 몰린 구간 = 교전. 6초 넘게 조용하면 끊는다."""
    dmg = sorted((ms, h[4]) for ms, h in d["logs"] if h[0] == 0 and h[4])
    out, cur = [], None
    for ms, v in dmg:
        if cur and ms - cur[1] <= gap_ms:
            cur[1], cur[2] = ms, cur[2] + v
        else:
            if cur and cur[2] >= min_dmg:
                out.append(tuple(cur))
            cur = [ms, ms, v]
    if cur and cur[2] >= min_dmg:
        out.append(tuple(cur))
    return out


# ═══ 2026-08-08 확장 분석 (9차) ═══════════════════════════════════════════


def fight_participation(d):
    """교전마다 **끼어 있었나** — 참여율이 낮으면 "혼자 딴 데 있다 지는" 유형이다.

    참여 판정 = 그 교전 구간(±1.5초)에 **피해를 주거나·받거나·회복/실드를 준 기록**이 있다.
    ⚠️ 근처에 서 있기만 한 건 못 잡는다(MatchLog 에는 좌표가 없다). 그래서 근사값이다 —
       진짜로 아무것도 안 했다면 "그 자리에 있었다"고 쳐 줄 이유도 별로 없다.
    """
    fs = fights(d)
    took = {pid: 0 for pid in d["players"]}
    for a, b, _tot in fs:
        active = set()
        for ms, h in d["logs"]:
            if not (a - 1500 <= ms <= b + 1500):
                continue
            kind, owner, target = h[0], h[1], h[2]
            if kind in (0, 1, 13, 15, 24) and owner in took:
                active.add(owner)
            if kind == 0 and target in took:
                active.add(target)
        for pid in active:
            took[pid] += 1
    return {pid: (n, len(fs)) for pid, n in took.items()}


def _death_spans(d):
    """선수별 [사망시각, 복귀시각) 구간.

    복귀 시각은 **파일이 주는 `Respawn` 실측값**을 쓴다(2026-08-09 12차).
    🪤 예전엔 "좌표가 다시 오는 때"로 추정했는데, 은신(칼리브리)처럼 살아 있어도 좌표가
       끊기는 영웅에서 추정이 통째로 빗나가 사망 직전 체력이 "0%"로 읽히는 사고가 났다
       (7RGTWKER Vampi — 10차에 고쳤다던 그 오류의 재발).
    실측 부활 간격은 18경기 전부 **정확히 10.02초**였다. 부활 기록이 없으면 그 값으로 둔다.
    """
    RESPAWN_MS = 10020
    spans = collections.defaultdict(list)
    for dms, tgt, _who in deaths(d):
        rs = d.get("respawns", {}).get(tgt) or []
        back = next((m for m in rs if m > dms + 500), None)
        if back is None:                      # 부활 기록이 없으면(경기 끝 등) 실측 상수로
            back = dms + RESPAWN_MS
        spans[tgt].append((dms, min(back, dms + 20000)))
    return spans


def alive_counts(d, spans, ms, exclude=None):
    """그 시각에 팀별로 **살아 있는 인원** — "2대3 열세였다"를 만드는 재료."""
    cnt = collections.Counter()
    for pid, p in d["players"].items():
        if pid == exclude:
            continue
        dead = any(a < ms < b for a, b in spans.get(pid, []))
        if not dead:
            cnt[p["team"]] += 1
    return cnt


def death_details(d, lookback_ms=10000, near_r=15.0):
    """💀 **사망 심화** — 죽음 하나하나에 "그때 상황"을 붙인다 (유저가 진짜 원하는 것).

    · 피해 내역: 직전 10초의 피해 로그를 가해자별로 합산
    · 혼자였나: 사망 순간 아군이 15유닛 안에 있었는지 (감각치 — "근처" 기준으로만)
    · 순삭이었나: 첫 피해부터 죽음까지 걸린 시간
    · 🩸 깎인 채 싸웠나(hp0): **교전이 시작될 때 체력이 몇 %였나** — "빠져서 회복하고
      재진입했어야 한다"는 코칭의 근거 (2026-08-09 추가)
    · ⚔️ 인원은 몇대몇이었나(nums): 사망 직전 팀별 생존 인원 — "열세에 무리했다"의 근거
    """
    spans = _death_spans(d)
    out = []
    for ms, tgt, who in deaths(d):
        dmg_by = collections.Counter()
        first_hit = ms
        for lms, h in d["logs"]:
            if h[0] == 0 and h[2] == tgt and ms - lookback_ms <= lms <= ms and h[4]:
                if h[1] in d["players"]:
                    dmg_by[h[1]] += h[4]
                first_hit = min(first_hit, lms)
        # 사망 순간 아군 위치
        me = d["players"][tgt]
        my = _pos_at(d, me["hero_entity"], ms)
        near = []
        for pid, p in d["players"].items():
            if pid == tgt or p["team"] != me["team"]:
                continue
            q = _pos_at(d, p["hero_entity"], ms)
            if my and q and ((my[0]-q[0])**2 + (my[2]-q[2])**2) ** .5 <= near_r:
                near.append(pid)
        # 🩸 교전 시작 시점의 체력 비율 (버스트 시작 0.3초 전의 마지막 체력 샘플)
        # 🪤 체력은 **바뀔 때만** 전송된다 → 리스폰 후 무피해면 샘플이 없어서
        #    이전 죽음의 0이 그대로 읽힌다(실제로 "0%까지"라는 엉터리가 나왔다).
        #    마지막 샘플이 **마지막 리스폰보다 오래됐으면** 만피(1.0)로 본다.
        hp0 = None
        mx = me.get("max_hp")
        if mx:
            he = me["hero_entity"]
            before = [(m, v) for m, v, _x in d["hp"].get(he, []) if m <= first_hit - 300]
            respawns = [b for a, b in spans.get(tgt, []) if b <= first_hit]
            last_respawn = max(respawns) if respawns else None
            if not before:
                hp0 = 1.0
            elif last_respawn and before[-1][0] < last_respawn:
                hp0 = 1.0
            else:
                hp0 = max(0.0, min(1.0, before[-1][1] / mx))
        # ⚔️ 사망 직전 인원 (본인 포함)
        ac = alive_counts(d, spans, ms - 100)
        my_team, foe_team = me["team"], next(t for t in (1, 2) if t != me["team"])
        out.append({"ms": ms, "tgt": tgt, "killers": who, "dmg_by": dict(dmg_by),
                    "burst_s": (ms - first_hit) / 1000, "allies_near": near,
                    "solo": (my is not None and not near),
                    "hp0": hp0, "nums": [ac.get(my_team, 0), ac.get(foe_team, 0)]})
    return out


def heal_split(d):
    """💚 힐 분배 — kind 1(회복)의 대상 기록으로 "누구를 살렸나"를 본다.

    kind 13(재생)·15(구급킷)은 본인 회복이라 분배 판단에선 뺀다.
    되돌려주는 것: {pid: {"self": n, "ally": n, "recv": n}}
      · ally = 아군에게 준 힐 (팀 힐러 성향) · recv = 아군에게서 받은 힐
      · 사이트 명단 표는 ally/recv 두 칸만 쓴다 — 자가 회복은 표에서 제외
        (2026-08-09 MAMMON 결정: "아군에게 받은 힐과 준 힐로만").
    """
    out = collections.defaultdict(lambda: {"self": 0, "ally": 0, "recv": 0})
    for ms, h in d["logs"]:
        if h[0] != 1 or h[1] not in d["players"] or not h[4]:
            continue
        if h[2] == h[1]:
            out[h[1]]["self"] += h[4]
        elif h[2] in d["players"] and d["players"][h[2]]["team"] == d["players"][h[1]]["team"]:
            out[h[1]]["ally"] += h[4]
            out[h[2]]["recv"] += h[4]
    return dict(out)


# ═══ 🏷 스킬 이름 (2026-08-09 12차) ═════════════════════════════════════
#   리플레이는 "스킬을 썼다"만 남기지만, **스킬 엔티티의 오프셋**이 어느 스킬인지의 지문이다.
#   법칙·앵커 실증은 `도구/스킬표생성.py` 주석 참고. 표가 없으면 이름 없이 동작한다(선택 기능).
_SKILLS = None


def skill_table():
    global _SKILLS
    if _SKILLS is None:
        _SKILLS = (_load("skill_names.json") or {}).get("heroes", {})
    return _SKILLS


# 사이트가 실제로 쓰는 언어만 리포트에 싣는다 — 9개를 다 넣으면 리포트가 40% 커진다.
#   (나머지 언어는 사이트가 영어로 폴백한다 — RPT_L 과 같은 규칙)
SITE_LANGS = ("ko", "en", "ja", "zh")


def _names4(e):
    """스킬 이름 사전에서 사이트가 쓰는 4개 언어만."""
    n = (e or {}).get("names") or {}
    return {k: v for k, v in n.items() if k in SITE_LANGS}


def skill_entry(d, pid, off):
    """(선수, 오프셋) → 스킬표의 항목. 모르면 None (이름 없이도 기능은 돌아간다).

    🪤 칼리브리·노바는 모드 전환 쌍이 있어 두 오프셋이 같은 버튼이다 — 앞쪽으로 합친다.
    """
    p = d["players"].get(pid)
    if not p:
        return None
    h = skill_table().get(hero_key(p.get("hero")))
    if not h:
        return None
    off = h.get("pairs", {}).get(str(off), off)      # 모드 전환 쌍 병합
    return h.get("offsets", {}).get(str(off))


def skill_of(d, pid, off, lang="ko"):
    """(선수, 오프셋) → 그 스킬의 공식 이름."""
    e = skill_entry(d, pid, off)
    return (e["names"].get(lang) or e["names"].get("en")) if e else None


def ult_combos(d, window_ms=5000):
    """✨ **궁 연계** — 같은 팀의 궁이 겹쳐 들어갔고, 그 뒤에 처치가 따라왔나.

    판정: 한 선수의 궁 사용 후 `window_ms` 안에 **다른 아군의 궁**이 또 들어가고,
          그 마지막 궁으로부터 8초 안에 **상대 팀 사망**이 있으면 연계 성공으로 본다.
    ⚠️ 인과가 아니라 동시성이다(궁이 잡은 건지 이미 이기던 싸움인지는 못 가른다).
       문장도 그렇게 쓴다.
    되돌려주는 것: [{"ms", "team", "pids", "skills", "kills"}]
    """
    ds = deaths(d)
    ults = sorted((ms, pid) for ms, pid, k, _o in d.get("skills", []) if k == "궁")
    offs = {(ms, pid): o for ms, pid, k, o in d.get("skills", []) if k == "궁"}
    out, used = [], set()
    for i, (ms, pid) in enumerate(ults):
        if (ms, pid) in used:
            continue
        team = d["players"][pid]["team"]
        group = [(ms, pid)]
        for ms2, pid2 in ults[i + 1:]:
            if ms2 - group[-1][0] > window_ms:
                break
            if pid2 != pid and d["players"][pid2]["team"] == team and \
                    pid2 not in {q for _, q in group}:
                group.append((ms2, pid2))
        if len(group) < 2:
            continue
        used.update(group)
        last = group[-1][0]
        kills = sum(1 for dms, tgt, _w in ds
                    if last <= dms <= last + 8000 and d["players"][tgt]["team"] != team)
        out.append({"ms": group[0][0], "team": team,
                    "pids": [q for _, q in group],
                    "skills": [skill_of(d, q, offs.get((m, q))) for m, q in group],
                    "skills_i18n": [_names4(skill_entry(d, q, offs.get((m, q))))
                                    for m, q in group],
                    "kills": kills})
    return out


def focus_after_setup(d, window_ms=4000, min_dealers=2):
    """🎯 **판 깔고 점사** — 상대를 묶거나 기절시킨 직후 팀이 그 상대를 몰아쳐 잡았나.

    MAMMON 요청(2026-08-09)의 바로 그 장면:
      "노바가 바인드샷을 걸어 상대를 묶고 다같이 점사를 한 부분은 좋았다"

    판정: **판을 깔아주는 스킬**(스킬표의 `setup` — 공식 설명에 기절/묶음/끌어당김/표식이
          있는 것)을 쓴 뒤 `window_ms` 안에, 그 팀의 서로 다른 선수 `min_dealers` 명 이상이
          같은 상대를 때리고, 그 상대가 창 안에 죽으면 성공으로 본다.
    🪤 예전엔 **모든 스킬**을 봤더니 한 경기 24건이 잡혔다(힐 스킬로 "점사를 열었다"는
       말까지 나옴). 판정을 setup 스킬로 좁혀야 문장이 사실이 된다.
    ⚠️ 인과는 단정하지 않는다 — 스킬이 잡아준 건지 겹친 건지는 못 가른다. 문장도 그렇게 쓴다.
    되돌려주는 것: [{"ms", "pid", "skill", "target", "dealers", "team"}]
    """
    ds = deaths(d)
    dmg = [(ms, h[1], h[2]) for ms, h in d["logs"]
           if h[0] == 0 and h[1] in d["players"] and h[2] in d["players"]]
    out = []
    for ms, pid, kind, off in d.get("skills", []):
        e = skill_entry(d, pid, off)
        if not e or not e.get("setup"):
            continue
        team = d["players"][pid]["team"]
        by_target = collections.defaultdict(set)
        for dms, a, v in dmg:
            if ms <= dms <= ms + window_ms and d["players"][a]["team"] == team \
                    and d["players"][v]["team"] != team:
                by_target[v].add(a)
        # 🪤 한 번의 스킬로 **여러 명**이 죽으면 한 장면이다. 대상마다 한 줄씩 내면
        #    같은 순간이 3~4줄로 쪼개져 읽을 수가 없다(실측: 00:47 에 4줄).
        hit, dealers_all = [], set()
        for tgt, dealers in by_target.items():
            if len(dealers) < min_dealers:
                continue
            if not any(tgt == t and ms <= dms <= ms + window_ms for dms, t, _w in ds):
                continue
            hit.append(tgt)
            dealers_all |= dealers
        if hit:
            out.append({"ms": ms, "pid": pid,
                        "skill": e["names"].get("ko") or e["names"].get("en"),
                        # 사이트가 보는 언어로 고르도록 이름 사전을 통째로 넘긴다
                        "skill_i18n": _names4(e),
                        "target": hit[0], "targets": sorted(hit),
                        "dealers": sorted(dealers_all), "team": team,
                        # 효과 종류(stun/bind/pull/mark) — 스킬표생성.py 가 공식 설명문에서
                        # 자동 분류한 값을 그대로 옮긴다. 옛 스킬표(필드 없음)면 None → 옛 문장 폴백.
                        "kind": e.get("setup_kind")})
    # 🪤 두 가지 중복을 걷어낸다:
    #    ① 모드 전환 쌍(노바 바인드샷)은 두 오프셋이 각각 트리거돼 같은 장면이 두 번 잡힌다.
    #    ② 한 교전에서 여러 명이 각자 판을 깔면 같은 처치가 사람 수만큼 반복된다
    #       (00:47 오테오 '킥' + 풍산개 '때려눕히기' → 같은 2명 처치).
    #       → **같은 팀·3초 안·대상이 겹치면** 먼저 건 사람 것만 남긴다.
    dedup, seen = [], []
    for x in sorted(out, key=lambda q: q["ms"]):
        if any(y["team"] == x["team"] and abs(y["ms"] - x["ms"]) <= 3000
               and set(y["targets"]) & set(x["targets"]) for y in seen):
            continue
        seen.append(x)
        dedup.append(x)
    return dedup


_PERKS = None


def perk_name(pid, lang="ko"):
    """퍽 id → 공식 효과 설명. 모르면 None.

    🪤 이 조회는 **반드시 이 파일 안에** 있어야 한다. 서버 사본은 `동기화.py` 가
       파일명을 영문으로 바꾸는데(퍽표.json → perk_names.json), 그 치환은 `도구/` 에서
       복사되는 파이썬 파일에만 적용된다. `app.py` 에서 한글 파일명으로 열면
       **Render 에서만 조용히 실패**한다(로컬은 되고 서버는 퍽이 전부 빈칸).
    """
    global _PERKS
    if _PERKS is None:
        _PERKS = (_load("perk_names.json") or {}).get("perks", {})
    e = _PERKS.get(str(pid))
    if not e:
        return None
    return e["names"].get(lang) or e["names"].get("en")


_RANGE = None


def range_table():
    global _RANGE
    if _RANGE is None:
        _RANGE = (_load("hero_range.json") or {}).get("heroes", {})
    return _RANGE


def engage_range(d, tol_ms=1000):
    """📏 **선수별 교전 거리** — 딜을 넣을 때 상대와 얼마나 떨어져 있었나 (중앙값).

    영웅마다 무기 사거리가 달라 절대값은 의미가 없다. `사거리표.json` 의 **그 영웅 기준선**과
    비교해야 "이 영웅치고 너무 붙었다/멀었다"를 말할 수 있다.
    되돌려주는 것: {pid: {"n", "median"}}
    """
    out = collections.defaultdict(list)
    for ms, h in d["logs"]:
        if h[0] != 0 or h[1] == h[2]:
            continue
        a, v = d["players"].get(h[1]), d["players"].get(h[2])
        if not a or not v:
            continue
        pa = _pos_at(d, a["hero_entity"], ms, tol=tol_ms)
        pv = _pos_at(d, v["hero_entity"], ms, tol=tol_ms)
        if not pa or not pv:
            continue
        out[h[1]].append(((pa[0] - pv[0]) ** 2 + (pa[2] - pv[2]) ** 2) ** .5)
    res = {}
    for pid, xs in out.items():
        xs.sort()
        n = len(xs)
        res[pid] = {"n": n, "median": round(xs[n // 2] if n % 2 else
                                            (xs[n // 2 - 1] + xs[n // 2]) / 2, 2)}
    return res


def fight_story(d):
    """⚔️ 교전 일지 — 구간마다 "누가 열었고, 몇대몇으로 시작했고, 결과가 어땠나".

    유저 요청(2026-08-09): "어떤 판단으로 졌는지"는 교전 단위로 읽힌다.

    🪤 2026-08-09 14차 — "3명뿐인데 4명을 어떻게 잡았나" (MAMMON 제보).
       `fights()` 는 6초 넘게 조용해야 끊는데, 계속 치고받는 공방은 24초·86초·113초짜리가
       "한 번의 교전"으로 묶인다. 그 안에서 죽은 사람이 **부활해 또 죽으면** 3명뿐인 팀에서
       4킬·6킬이 나온다 — `lost`(구간 전체 합계)와 `nums`(교전 시작 순간 스냅샷)가
       서로 다른 시간 범위를 말하는데 한 문장에 붙어 있어서 생기는 혼란이다.
       `fights()` 자체는 손대지 않는다(교전 개수에 의존하는 8곳 이상이 재사용한다).
       대신 **그 교전 구간 안에서 실제로 부활이 몇 번 있었는지** 세어 문장 쪽에서 밝힌다.
    """
    spans = _death_spans(d)
    rows = []
    fb = {(a, b): order for a, b, order in first_blood_order(d)}
    for a, b, tot, lost, win in fight_rounds(d):
        ac = alive_counts(d, spans, a - 100)
        order = fb.get((a, b)) or []
        opener = order[0][0] if order else None
        # 🔁 부활 횟수 — "죽었던 사람이 부활해 다시 싸운" 것 = 사망·부활이 **둘 다** 이
        #    교전 구간(±1.5초, `fight_rounds`가 `lost`를 셀 때 쓰는 것과 같은 범위) 안에
        #    들어오는 경우만 센다. 부활이 구간 밖(다음 교전)에서 일어났다면 "이 교전에서
        #    다시 싸웠다"고 할 수 없으니 빼야 한다 — 실제로 위 사례의 두 번째 죽음(85.6초)의
        #    부활(95.6초)은 구간 밖이라 안 세어진다(그건 다음 교전 몫이다).
        #    팀별로 나눠 담는다 — "부활한 게 어느 팀 선수인지"가 문장의 핵심이다.
        revives = collections.Counter()
        for pid, sp in spans.items():
            team = d["players"][pid]["team"]
            for dms, rms in sp:
                if a - 1500 <= dms <= b + 1500 and a - 1500 <= rms <= b + 1500:
                    revives[team] += 1
        rows.append({"a": a, "b": b, "tot": tot, "lost": lost, "win": win,
                     "opener": opener, "nums": [ac.get(1, 0), ac.get(2, 0)],
                     "revives": dict(revives)})
    return rows


def _pos_at(d, ent, ms, tol=2000):
    """그 시각에 제일 가까운 좌표 (±2초 안에 없으면 None — 죽어 있으면 좌표가 안 온다)."""
    track = d["pos"].get(ent) or []
    best = min(track, key=lambda x: abs(x[0] - ms), default=None)
    return best[1] if best and abs(best[0] - ms) <= tol else None


def positioning(d, warmup_ms=3000):
    """🧭 **전방/후방 지수 + 이동 거리** — 누가 앞라인이고 누가 뒷라인인가.

    · 스폰 축: 경기 **맨 처음 좌표부터 3초간**의 팀별 평균 위치 = 양 팀 진영.
      그 두 점을 잇는 선에 선수의 매 좌표를 투영한다. 0 = 우리 진영, 1 = 적 진영.
    · 전방지수 = 내 평균 위치 − 우리 팀 평균 위치 (+면 팀보다 앞에 선다)
    · 이동거리 = 좌표 샘플 사이 거리 합. 한 번에 20 유닛 넘게 튀면 부활 순간이라 뺀다.

    🪤 예전에는 기준이 `t0`(첫 **전투 로그**) + 6초였다 (2026-08-09 12차 감사).
       t0 은 첫 좌표보다 1~14초 늦어서, "스폰"이라고 잡은 점이 이미 **한참 진격한 자리**였다
       (추정 스폰간 거리의 77~162% 를 이동한 뒤). 그래서 축이 짧아지고 전방지수가 부풀려졌고,
       "우리 진영보다 뒤"(음수)라는 불가능한 값도 나왔다.
       → 기준을 **첫 좌표 샘플**로 바꾼다. 그게 진짜 스폰이다.
    """
    spawn = {}
    first_ms = min((tr[0][0] for tr in d["pos"].values() if tr), default=d["t0"])
    for team in {p["team"] for p in d["players"].values()}:
        pts = []
        for p in d["players"].values():
            if p["team"] != team:
                continue
            for ms, xyz in d["pos"].get(p["hero_entity"], []):
                if ms <= first_ms + warmup_ms:
                    pts.append(xyz)
        if pts:
            spawn[team] = (sum(q[0] for q in pts) / len(pts), sum(q[2] for q in pts) / len(pts))
    teams = sorted(spawn)
    out = {}
    for pid, p in d["players"].items():
        track = d["pos"].get(p["hero_entity"]) or []
        # 이동 거리
        dist = 0.0
        for (m1, a), (m2, b) in zip(track, track[1:]):
            step = ((a[0]-b[0])**2 + (a[2]-b[2])**2) ** .5
            if step <= 20:                        # 순간이동(부활)은 이동이 아니다
                dist += step
        # 전방 지수 (양 팀 스폰이 다 잡혔을 때만)
        adv = None
        if len(teams) == 2 and p["team"] in spawn:
            own = spawn[p["team"]]
            foe = spawn[teams[0] if p["team"] == teams[1] else teams[1]]
            ax, az = foe[0] - own[0], foe[1] - own[1]
            L2 = ax*ax + az*az
            if L2 > 1:
                projs = [((q[0]-own[0])*ax + (q[2]-own[1])*az) / L2 for _, q in track]
                if projs:
                    adv = sum(projs) / len(projs)
        out[pid] = {"dist": dist, "adv": adv}
    # 전방지수는 팀 평균 대비로 바꾼다 (팀 전체가 밀렸는지와 분리)
    for team in teams:
        vals = [v["adv"] for pid, v in out.items()
                if d["players"][pid]["team"] == team and v["adv"] is not None]
        mean = sum(vals) / len(vals) if vals else 0
        for pid, v in out.items():
            if d["players"][pid]["team"] == team and v["adv"] is not None:
                v["front"] = v["adv"] - mean
    return out


def heat_grid(d, n=26):
    """🗺️ 팀별 **장악 히트맵** 격자 — 어느 팀이 맵 어디서 시간을 보냈나."""
    # 2026-08-09: bounds 도 반드시 선수(hero_entity) 좌표만으로 잡는다.
    # 예전엔 d["pos"].values() 전체(차량·터렛·소품 포함)로 min/max를 구했는데,
    # 격자를 "채우는" 쪽은 이미 hero_entity만 쓰고 있어서 축이 채워지는 데이터보다
    # 넓게 잡혔다(실측: 용광로 맵 선수 x 활동폭 60.4 vs 전체 엔티티 70.8 — 17% 헐거움).
    # 지도 좌표는 어디서든 선수 것만 쓴다는 원칙대로, bounds도 같은 좌표 집합을 쓴다.
    heropos = [q for pid, p in d["players"].items()
               for _, q in d["pos"].get(p["hero_entity"], [])]
    if not heropos:
        return None
    xs = [q[0] for q in heropos]
    zs = [q[2] for q in heropos]
    x0, x1, z0, z1 = min(xs), max(xs), min(zs), max(zs)
    grid = {}
    for pid, p in d["players"].items():
        for _, q in d["pos"].get(p["hero_entity"], []):
            i = min(n - 1, int((q[0] - x0) / max(1e-6, x1 - x0) * n))
            j = min(n - 1, int((q[2] - z0) / max(1e-6, z1 - z0) * n))
            key = (i, j)
            grid.setdefault(key, [0, 0])
            grid[key][0 if p["team"] == 1 else 1] += 1
    return {"n": n, "bounds": (x0, x1, z0, z1), "cells": grid}


def score_timeline(d):
    """📈 점수 흐름 — (초, 1팀, 2팀) 목록. 리드가 뒤집힌 순간도 같이 돌려준다.

    🪤 **모드마다 흐르는 필드가 다르다** (2026-08-09 리플레이 18개 전수 실측):
      · 강타/사수/사냥 → Team1/2Score
      · 장악(한 지점)  → Team1/2CaptureScore **만** 흐른다 (0→100 한 번)
      · 점멸(여러 지점) → Team1/2CapturedPoints(진짜 스코어)가 흐르고,
        CaptureScore 는 지점마다 0→100 을 **반복**한다(리셋) — 그래프로 쓰면 톱니가 된다.
    예전 코드는 keys[0:2](CapturedPoints)만 읽어서 **장악 그래프가 통째로 비었다**
    (TYW12FB7 실측 — 점수 변화 100건이 전부 CaptureScore 였는데 하나도 안 잡힘).
    → 쌍(pair) 단위로, CapturedPoints 가 한 번이라도 흐른 경기는 그걸 쓰고
      아니면 CaptureScore 를 쓴다.
    """
    tl = sorted(d.get("score_tl", []))
    # 컴포넌트별로 "어느 필드 쌍을 그래프로 쓸까"를 먼저 정한다 (실제로 흐른 쌍만 후보)
    seen = {}
    for _ms, comp, vals in tl:
        keys = SCORE_FIELDS.get(comp) or ()
        got = seen.setdefault(comp, set())
        for i in range(0, len(keys) - 1, 2):
            if keys[i] in vals or keys[i + 1] in vals:
                got.add((keys[i], keys[i + 1]))
    use = {}
    for comp, got in seen.items():
        keys = SCORE_FIELDS.get(comp) or ()
        pairs = [(keys[i], keys[i + 1]) for i in range(0, len(keys) - 1, 2)]
        use[comp] = next((p for p in pairs if p in got), None)   # keys 순서 = 우선순위
    rows, flips = [], []
    last = (0, 0)
    lead = 0
    for ms, comp, vals in tl:
        pair = use.get(comp)
        if not pair or (pair[0] not in vals and pair[1] not in vals):
            continue                    # 다른 쌍(점멸의 진행도 등)만 실린 항목은 그래프에 안 쓴다
        a, b = vals.get(pair[0]), vals.get(pair[1])
        cur = (a if a is not None else last[0], b if b is not None else last[1])
        if cur == last and rows:
            continue
        last = cur
        rows.append((ms, cur[0], cur[1]))
        now = 1 if cur[0] > cur[1] else (2 if cur[1] > cur[0] else 0)
        if now and now != lead:
            if lead:                                # 0→리드는 선취, 리드→리드가 역전
                flips.append((ms, now))
            lead = now
    return rows, flips


def score_unit(d):
    """그래프 값의 단위 — 장악(0~100 진행도)이면 "pct", 그 외 점수면 "pts".
    사이트가 최종 점수에 % 를 붙일지 판단하는 데 쓴다(점멸의 2:1 에 %를 붙이면 안 된다)."""
    caps = scores = False
    for _ms, comp, vals in d.get("score_tl", []):
        if comp != "Capturepoint":
            continue
        scores = True
        if "Team1CapturedPoints" in vals or "Team2CapturedPoints" in vals:
            caps = True
    return "pct" if (scores and not caps) else "pts"


def first_engage_counts(d):
    """⚔️ 교전마다 **제일 먼저 피해를 주고받은 사람** — 선봉 성향 집계."""
    cnt = collections.Counter()
    for a, b, order in first_blood_order(d):
        if order:
            cnt[order[0][0]] += 1
    return cnt


# ═══ 🎓 코칭 코멘트 룰 엔진 (2026-08-08, 9차-b) ══════════════════════════
#   유저가 원하는 건 표가 아니라 "왜 졌나 · 뭘 고치면 되나"다 (MAMMON 요청).
#   서버는 **판정(키+숫자)** 만 만들고, 문장은 표시하는 쪽(사이트 4개 언어 템플릿 /
#   이 파일의 한국어 템플릿)이 만든다 — 그래야 언어를 늘려도 서버를 안 고친다.
#   ⚠️ 정직함이 규칙이다: 전부 임계값 기반이고 숫자를 함께 내보낸다.
#      "궁 뒤 성과"처럼 인과가 아닌 동시성인 것은 문장도 그렇게 쓴다.

def coach(d):
    """판정 목록 [{k, sev, pid?, team?, ...숫자들}] — sev: bad(고칠 점)/good(잘한 점)/info/team."""
    out = []
    w, _why = winner(d)
    teams = sorted({p["team"] for p in d["players"].values()})
    fr = fight_rounds(d)
    nf = len(fr)
    dd = death_details(d)
    st = summarize(d)
    fp = fight_participation(d)
    uv = ult_value(d)
    po = positioning(d)
    t0 = d["t0"]

    # ── 팀 판정 (진 팀 관점) ──────────────────────────────────────────
    #    🪤 **사람이 한 명도 없는 팀에는 훈수하지 않는다** (2026-08-09 12차 감사).
    #       선수 판정은 봇을 빼고 있었는데 팀 판정에는 가드가 없어서, 전원 봇인 팀에게
    #       "인원이 갖춰졌을 때만 싸우세요" 같은 코칭이 3경기에서 실제로 나갔다.
    has_human = {t: any((not p.get("bot")) and p["team"] == t for p in d["players"].values())
                 for t in teams}
    if w and len(teams) == 2:
        loser = teams[0] if teams[1] == w else teams[1]
        lost_f = sum(1 for *_x, win in fr if win == w)
        won_f = sum(1 for *_x, win in fr if win == loser)
        if has_human.get(loser):
            if nf >= 3 and lost_f > nf / 2:
                out.append({"k": "t_fights", "sev": "team", "team": loser,
                            "lost": lost_f, "of": nf})
            elif nf >= 3 and won_f > nf / 2:
                # 교전은 이겼는데 경기를 짐 = 목표(점수) 관리 문제
                out.append({"k": "t_objective", "sev": "team", "team": loser,
                            "won": won_f, "of": nf})
        # 승부처: 마지막으로 리드가 뒤집힌 순간
        #   🪤 예전엔 그 순간이 든 **교전의 시작 시각**을 가리켰다. 30초+ 교전을 안 쪼개기로
        #      했기 때문에 긴 공방에서는 최대 101초나 어긋났다(7RGTWKER 실측).
        #      → 역전이 일어난 **바로 그 시각**을 가리킨다.
        _rows, flips = score_timeline(d)
        if flips and any(has_human.values()):
            fms = flips[-1][0]
            hit = next((f for f in fr if f[0] - 8000 <= fms <= f[1] + 8000), None)
            out.append({"k": "t_decisive", "sev": "team", "team": w,
                        "s": round((fms - t0) / 1000, 1),
                        "deaths": sum(hit[3].values()) if hit else 0})
        # 접전(마지막 점수 1차 이내)이면 위로도 한 줄
        _rows2 = _rows or []
        if _rows2 and has_human.get(loser):
            la, lb = _rows2[-1][1], _rows2[-1][2]
            if abs((la or 0) - (lb or 0)) <= 1 and max(la or 0, lb or 0) >= 2:
                out.append({"k": "t_close", "sev": "team", "team": loser})

    # ── 선수 판정 (봇 제외 — 봇에게 코칭은 무의미) ───────────────────
    death_by = collections.defaultdict(list)
    for ev in dd:
        death_by[ev["tgt"]].append(ev)
    # 교전마다 첫 사망자
    first_die = collections.Counter()
    ds = deaths(d)
    for a, b, *_r in fr:
        first = next((tgt for ms, tgt, _who in ds if a - 1500 <= ms <= b + 1500), None)
        if first is not None:
            first_die[first] += 1

    heals = heal_split(d)
    t0 = d["t0"]
    sec = lambda ms: round((ms - t0) / 1000, 1)

    for pid, p in d["players"].items():
        if p.get("bot"):
            continue
        mydd = death_by.get(pid, [])
        ndeath = len(mydd)
        solo_ev = [ev for ev in mydd if ev["solo"]]
        melt_ev = [ev for ev in mydd if ev["burst_s"] <= 3.0]
        # 🩸 깎인 체력(55% 미만)으로 교전을 받다 죽은 것 — "빠져서 회복 후 재진입" 코칭의 근거
        lowhp_ev = [ev for ev in mydd if ev.get("hp0") is not None and ev["hp0"] < 0.55]
        # ⚔️ 인원 열세(예: 2대3)에서 싸우다 죽은 것
        outnum_ev = [ev for ev in mydd
                     if ev.get("nums") and ev["nums"][0] < ev["nums"][1]]
        n_part, tot_f = fp[pid]
        un, ug, ul = uv[pid]
        front = po[pid].get("front")

        if ndeath >= 3 and len(solo_ev) >= 2 and len(solo_ev) * 2 >= ndeath:
            out.append({"k": "p_solo", "sev": "bad", "pid": pid, "n": len(solo_ev),
                        "of": ndeath, "ex": [sec(ev["ms"]) for ev in solo_ev[:2]]})
        if len(melt_ev) >= 2:
            # 주로 누구에게 잘렸나 — 그 죽음들에서 피해 1위 가해자
            hurt = collections.Counter()
            for ev in melt_ev:
                for q, v in ev["dmg_by"].items():
                    hurt[q] += v
            by = hurt.most_common(1)[0][0] if hurt else None
            out.append({"k": "p_melt", "sev": "bad", "pid": pid, "n": len(melt_ev),
                        "by_pid": by, "ex": [sec(ev["ms"]) for ev in melt_ev[:2]]})
        if len(lowhp_ev) >= 2:
            out.append({"k": "p_lowhp", "sev": "bad", "pid": pid, "n": len(lowhp_ev),
                        "hp": int(min(ev["hp0"] for ev in lowhp_ev) * 100),
                        "ex": [sec(ev["ms"]) for ev in lowhp_ev[:2]]})
        if len(outnum_ev) >= 2:
            worst = min(outnum_ev, key=lambda ev: ev["nums"][0] - ev["nums"][1])
            out.append({"k": "p_outnum", "sev": "bad", "pid": pid, "n": len(outnum_ev),
                        "a": worst["nums"][0], "e": worst["nums"][1],
                        "ex": [sec(ev["ms"]) for ev in outnum_ev[:2]]})
        if tot_f >= 4 and n_part * 10 <= tot_f * 6:
            out.append({"k": "p_lowpart", "sev": "bad", "pid": pid, "n": n_part, "of": tot_f})
        if first_die.get(pid, 0) >= 2:
            fd_ex = []
            for a, b, *_r in fr:
                first = next((tgt for ms, tgt, _who in ds if a - 1500 <= ms <= b + 1500), None)
                if first == pid:
                    fd_ex.append(sec(a))
            out.append({"k": "p_firstdie", "sev": "bad", "pid": pid,
                        "n": first_die[pid], "ex": fd_ex[:2]})
        if front is not None and front >= 0.08 and ndeath >= 4:
            out.append({"k": "p_overext", "sev": "bad", "pid": pid,
                        "front": round(front, 2), "n": ndeath})
        # 💚 힐 분배 — 회복 스킬(kind 1)이 거의 본인에게만 갔나 / 팀을 살렸나
        hl = heals.get(pid)
        if hl:
            tot_h = hl["self"] + hl["ally"]
            if tot_h >= 600 and hl["ally"] * 4 < tot_h:
                out.append({"k": "p_selfheal", "sev": "bad", "pid": pid,
                            "pct": int(hl["self"] * 100 / tot_h), "tot": tot_h})
            elif hl["ally"] >= 800 and hl["ally"] * 2 >= tot_h:
                out.append({"k": "p_goodheal", "sev": "good", "pid": pid,
                            "ally": hl["ally"]})
        if un >= 2 and ug == 0:
            out.append({"k": "p_ultwaste", "sev": "info", "pid": pid, "n": un})
        if tot_f >= 4 and n_part == tot_f:
            out.append({"k": "p_goodpart", "sev": "good", "pid": pid, "of": tot_f})
        if ug >= 3:
            out.append({"k": "p_ultgood", "sev": "good", "pid": pid, "n": un, "got": ug})
        if w and p["team"] == w and st[pid]["처치"] >= 2 and \
           st[pid]["준피해"] == max(st[q]["준피해"] for q in d["players"]):
            out.append({"k": "p_carry", "sev": "good", "pid": pid,
                        "k_n": st[pid]["처치"], "dmg": st[pid]["준피해"]})

    # ── 🎓 신규 판정 3종 (2026-08-09 12차) ────────────────────────────
    #    ① 판 깔고 점사  ② 궁 연계  ③ 교전 거리
    #    셋 다 **봇에게는 안 낸다**(사람 실력 판정이 아니므로).
    human = {pid for pid, p in d["players"].items() if not p.get("bot")}

    # ① 판 깔고 점사 — 묶거나 기절시킨 뒤 팀이 몰아쳐 잡은 장면
    for x in focus_after_setup(d):
        if x["pid"] not in human:
            continue
        out.append({"k": "p_setupkill", "sev": "good", "pid": x["pid"],
                    "skill": x["skill"], "skill_i18n": x.get("skill_i18n"),
                    "n": len(x["dealers"]),
                    # 효과 종류(stun/bind/pull/mark) — coach_lines()가 이걸로 문장을 고른다.
                    # 옛 스킬표(필드 없음)면 None → coach_lines()가 기존 중립 문장으로 폴백.
                    "kind": x.get("kind"),
                    "ex": [round((x["ms"] - t0) / 1000, 1)]})

    # ② 궁 연계 — 아군 궁이 겹쳐 들어가고 처치가 따라온 장면
    for c in ult_combos(d):
        if c["kills"] < 1:
            continue
        pids = [q for q in c["pids"] if q in human]
        if len(pids) < 2:
            continue
        names = [s for s in c["skills"] if s]
        out.append({"k": "p_ultcombo", "sev": "good", "pid": pids[0],
                    "with_pid": pids[1], "skills": names,
                    "skills_i18n": [x for x in (c.get("skills_i18n") or []) if x],
                    "got": c["kills"],
                    "ex": [round((c["ms"] - t0) / 1000, 1)]})

    # ③ 교전 거리 — 그 영웅 기준선보다 현저히 붙어서 싸우다 많이 죽었나
    #    🪤 거리만 보면 "죽음 0에 딜 5천" 넣은 잘한 근접 플레이를 지적한다(실측 2건).
    #       그래서 **죽음 조건이 필수**다. 표본이 적거나(n<30) 기준선 신뢰도가 낮은 영웅,
    #       모드 전환으로 거리가 두 갈래인 영웅(칼리브리·노바)은 발동하지 않는다.
    rt = range_table()
    er = engage_range(d)
    for pid in human:
        base = rt.get(hero_key(d["players"][pid].get("hero")))
        mine = er.get(pid)
        if not base or not mine or base.get("confidence") == "low" or base.get("bimodal"):
            continue
        if mine["n"] < 30 or base.get("p25") is None:
            continue
        if mine["median"] < base["p25"] and st[pid]["죽음"] >= 5:
            out.append({"k": "p_tooclose", "sev": "bad", "pid": pid,
                        "mine": mine["median"], "base": base["median"],
                        "n": st[pid]["죽음"]})

    # 한 선수당 고칠 점은 2개까지 (잔소리 폭탄 방지 — 제일 심한 것부터)
    sev_rank = {"p_solo": 0, "p_lowhp": 1, "p_tooclose": 1.5, "p_melt": 2, "p_outnum": 3,
                "p_selfheal": 4, "p_lowpart": 5, "p_firstdie": 6, "p_overext": 7}
    per = collections.Counter()
    trimmed = []
    for c in sorted(out, key=lambda c: sev_rank.get(c["k"], 9)):
        if c["sev"] == "bad":
            per[c["pid"]] += 1
            if per[c["pid"]] > 2:
                continue
        trimmed.append(c)
    # 칭찬 다듬기: "전원 참여"처럼 **다들 받는 칭찬은 칭찬이 아니다** → 4명 이상이면 통째로 뺌.
    # 같은 선수의 같은 종류 칭찬도 한 번만(판 깔고 점사를 3번 했어도 한 줄).
    if sum(1 for c in trimmed if c["k"] == "p_goodpart") >= 4:
        trimmed = [c for c in trimmed if c["k"] != "p_goodpart"]
    seen_good = set()
    dedup = []
    for c in trimmed:
        if c["sev"] == "good":
            key = (c["k"], c.get("pid"))
            if key in seen_good:
                continue
            seen_good.add(key)
        dedup.append(c)
    trimmed = dedup
    # 전체 🟢은 4줄까지 — 새로 만든 "장면" 칭찬을 먼저 보여 준다(읽는 재미가 있다).
    good_rank = {"p_setupkill": 0, "p_ultcombo": 1, "p_carry": 2,
                 "p_ultgood": 3, "p_goodheal": 4, "p_goodpart": 5}
    goods = sorted((c for c in trimmed if c["sev"] == "good"),
                   key=lambda c: good_rank.get(c["k"], 9))[:4]
    trimmed = [c for c in trimmed if c["sev"] != "good"] + goods
    order = {"team": 0, "bad": 1, "info": 2, "good": 3}
    trimmed.sort(key=lambda c: (order.get(c["sev"], 9), c.get("pid", 0)))
    return trimmed


# 한국어 템플릿 (CLI/HTML 리포트용 — 사이트는 자체 4개 언어 템플릿을 쓴다)
COACH_KO = {
    "t_fights":  "🏁 {team}팀 패인: 교전 {of}번 중 {lost}번을 졌어요 — 싸움 자체에서 밀렸습니다. 인원이 갖춰졌을 때만 싸우고, 불리하면 빠지는 판단이 필요해요.",
    "t_objective": "🏁 {team}팀 패인: 교전은 {of}번 중 {won}번 이기고도 졌어요 — 싸움을 이긴 뒤 목표(점수)를 챙기지 않은 게 원인입니다.",
    "t_decisive": "🏁 승부처: {mm}:{ss} 교전(사망 {deaths}명)에서 리드가 {team}팀으로 넘어갔어요 — 이 순간을 돌려보세요.",
    "t_close":   "🏁 {team}팀, 종이 한 장 차이였어요 — 아래 고칠 점 하나만 고쳐도 결과가 바뀌는 점수차입니다.",
    "p_solo":    "🔴 {name}: 죽음 {of}번 중 {n}번이 근처에 아군이 없는 상태였어요 — 팀과 붙어 다니면 절반은 살았을 싸움입니다.",
    "p_melt":    "🔴 {name}: 3초 안에 녹은 죽음이 {n}번(주로 {by}에게) — 각을 잡기 전에 몸이 먼저 나갔다는 신호예요. 엄폐물을 끼고 진입하세요.",
    "p_lowhp":   "🔴 {name}: 체력이 이미 깎인 채({hp}%까지) 교전을 받다 죽은 게 {n}번 — 무리하지 말고 빠져서 회복한 뒤 재진입하세요.",
    "p_outnum":  "🔴 {name}: 인원 열세({a}대{e})에서 싸우다 죽은 게 {n}번 — 아군 부활까지 시간을 끌거나 함께 빠졌어야 해요.",
    "p_lowpart": "🔴 {name}: 교전 {of}번 중 {n}번만 참여 — 팀이 인원 열세로 싸우는 동안 다른 곳에 있었어요. 미니맵에서 팀 위치를 자주 보세요.",
    "p_firstdie": "🔴 {name}: 교전이 시작되자마자 첫 사망이 된 게 {n}번 — 진입이 팀보다 반 박자 빠릅니다. 팀과 같이 들어가세요.",
    "p_overext": "🔴 {name}: 팀보다 앞에 서는 성향(전방 +{front})인데 죽음이 {n}번 — 앞라인을 서려면 빠지는 타이밍도 같이 연습해야 해요.",
    "p_selfheal": "🔴 {name}: 회복 스킬의 {pct}%가 본인에게 갔어요 — 아군에게 나눠주면 팀 전체 생존이 크게 올라갑니다.",
    "p_ultwaste": "💡 {name}: 궁을 {n}번 썼지만 이어진 처치가 없었어요 — 교전을 여는 용도보다, 이기고 있는 싸움을 굳히는 데 써보세요. (동시성 기준이라 참고용)",
    "p_goodpart": "🟢 {name}: 교전 {of}번 전부 참여 — 팀과 함께 움직이는 습관이 좋습니다.",
    "p_goodheal": "🟢 {name}: 아군 회복 {ally} — 팀을 살리는 힐 분배가 좋았어요.",
    "p_ultgood": "🟢 {name}: 궁 {n}번 뒤에 처치 {got}회가 이어졌어요 — 궁 타이밍이 좋습니다.",
    "p_carry":   "🟢 {name}: 처치 {k_n} · 팀 최고 딜({dmg}) — 이 경기의 에이스였어요.",
    # 🎯 종류별 4가지 + 종류를 모를 때(옛 데이터)를 위한 중립 폴백("p_setupkill").
    #    coach_lines() 가 c["kind"](stun/bind/pull/mark)를 보고 "p_setupkill_{kind}" 를 먼저
    #    찾고, 없으면 이 중립 문장으로 떨어진다. 효과가 다른 스킬을 전부 "잡아둔 뒤"로
    #    뭉뚱그리던 버그(2026-08-09 MAMMON 제보)의 수정 — 반드시 사실과 맞는 동사를 쓸 것.
    "p_setupkill": "🟢 {name}: '{skill}'{jo} 상대를 잡아둔 뒤 {n}명이 몰아쳐 처치했어요 — 좋은 시작이었습니다.",
    "p_setupkill_stun": "🟢 {name}: '{skill}'{jo} 상대를 기절시킨 뒤 {n}명이 몰아쳐 처치했어요 — 좋은 시작이었습니다.",
    "p_setupkill_bind": "🟢 {name}: '{skill}'{jo} 상대를 묶어둔 뒤 {n}명이 몰아쳐 처치했어요 — 좋은 시작이었습니다.",
    "p_setupkill_pull": "🟢 {name}: '{skill}'{jo} 상대를 끌어당긴 뒤 {n}명이 몰아쳐 처치했어요 — 좋은 시작이었습니다.",
    "p_setupkill_mark": "🟢 {name}: '{skill}'{jo} 상대가 받는 피해를 늘려놓은 뒤 {n}명이 몰아쳐 처치했어요 — 좋은 시작이었습니다.",
    "p_ultcombo": "🟢 {name}: 아군과 궁을 겹쳐 넣어({skills}) 처치 {got}회로 이어졌어요 — 좋은 연계입니다.",
    "p_tooclose": "🔴 {name}: 이 영웅 평균({base})보다 훨씬 붙어서({mine}) 싸우다 {n}번 죽었어요 — 사거리 이점을 살려 한 발짝 물러서서 싸워 보세요.",
}


def josa_ro(word):
    """한국어 조사 '으로/로' 고르기 — 받침이 없거나 'ㄹ' 받침이면 '로'.

    스킬 이름이 데이터에서 오기 때문에 필요하다("'킥'로"가 아니라 "'킥'으로").
    한글이 아닌 이름(영어 등)은 '로'로 둔다.
    """
    ch = (str(word or "").strip() or " ")[-1]
    if not ("가" <= ch <= "힣"):
        return "로"
    jong = (ord(ch) - 0xAC00) % 28
    return "로" if jong in (0, 8) else "으로"


def _fmt_ex(ex):
    """[75.3, 101.2] → '01:15, 01:41'"""
    out = []
    for s in ex:
        v = max(0, int(s))
        out.append(f"{v // 60:02d}:{v % 60:02d}")
    return ", ".join(out)


def coach_lines(d, tpl=None):
    """판정 → 사람이 읽는 문장 (기본 한국어)."""
    tpl = tpl or COACH_KO
    lines = []
    for c in coach(d):
        t = tpl.get(c["k"])
        if c.get("kind"):                      # 종류별 문장이 있으면 그걸 먼저 쓴다
            t = tpl.get(f"{c['k']}_{c['kind']}", t)
        if not t:
            continue
        vals = dict(c)
        if "pid" in c:
            vals["name"] = label(d, c["pid"]).split("(")[0]
        if "by_pid" in c:
            vals["by"] = label(d, c["by_pid"]).split("(")[0] if c["by_pid"] else "?"
        if "s" in c:
            vals["mm"] = f"{int(c['s']) // 60:02d}"
            vals["ss"] = f"{int(c['s']) % 60:02d}"
        if "dmg" in vals:
            vals["dmg"] = f"{vals['dmg']:,}"
        if "ally" in vals:
            vals["ally"] = f"{vals['ally']:,}"
        if isinstance(vals.get("skills"), list):          # 궁 연계: 스킬 이름 나열
            vals["skills"] = " + ".join(x for x in vals["skills"] if x) or "궁"
        if "with_pid" in c:
            vals["with"] = label(d, c["with_pid"]).split("(")[0]
        if "skill" in vals:                               # '킥'으로 / '봉쇄'로
            vals["jo"] = josa_ro(vals["skill"])
        try:
            line = t.format(**vals)
        except (KeyError, IndexError):
            continue
        if c.get("ex"):
            line += f" (예: {_fmt_ex(c['ex'])})"
        lines.append(line)
    return lines


def render_text(name, d):
    t0 = d["t0"]
    L = []
    kind, warn = match_kind(d)
    L.append(f"■ {name} — 프레임 {d['ok']}/{d['frames']} 완주 ({d['ok']*100//max(d['frames'],1)}%)")
    w, why = winner(d)
    L.append(f"   경기 성격: {kind}   {warn}")
    L.append(f"   결과: {('%d팀 승리' % w) if w else '무승부/판정불가'}  ({why})")
    L.append("")
    L.append("【명단】  ※ 이름·영웅·레벨은 리플레이 파일의 StartData 에 있는 값(정확).")
    L.append("           괄호 안은 우리 파서가 독립적으로 추론한 값과 맞는지 대조한 결과다.")
    for team in sorted({p["team"] for p in d["players"].values()}):
        L.append(f"   ── {team}팀")
        for pid, p in sorted(d["players"].items()):
            if p["team"] == team:
                L.append(f"      {roster_line(d, pid)}")
    L.append("")
    st = summarize(d)
    sk = collections.Counter((pid, k) for _, pid, k, _off in d.get("skills", []))
    L.append("【선수별 요약】")
    L.append("   선수          팀  처치 죽음 어시   준피해  받은피해  구조물   회복  실드  스킬  궁")
    for pid, p in sorted(d["players"].items(), key=lambda x: (x[1]["team"], -st[x[0]]["준피해"])):
        c = st[pid]
        L.append(f"   {label(d,pid).split('(')[0][:12]:<12} {p['team']:>3} {c['처치']:>5} {c['죽음']:>4} {c['어시']:>4} "
                 f"{c['준피해']:>8} {c['받은피해']:>9} {c['구조물피해']:>7} {c['회복']:>6} {c['실드']:>5}"
                 f"{sk[(pid,'스킬')]:>6}{sk[(pid,'궁')]:>4}")
    L.append("")
    cl = coach_lines(d)
    if cl:
        L.append("【코칭 코멘트】  ※ 데이터 기반 자동 판정 — 상황 맥락(핑·역할 분담)까지는 모릅니다")
        for line in cl:
            L.append("   " + line)
        L.append("")
    L.append("【타임라인】 (경기 시작 기준)")
    tl = []
    for ms, pid, kind, _off in d.get("skills", []):
        if kind == "궁":
            tl.append((ms, f"✨ 궁  {label(d,pid)}"))
    for ms, tgt, who in deaths(d):
        w = ", ".join(label(d, x).split("(")[0] for x in who)
        tl.append((ms, f"💀 {label(d,tgt)} 사망  ← {w}"))
    for ms, h in d["logs"]:
        kind, owner, target = h[0], h[1], h[2]
        val = h[4] if (kind in HAS_VALUE and len(h) > 4) else None
        # 가해자·피해자가 **둘 다 이 경기의 플레이어**일 때만 싣는다.
        # 아니면 어긋난 프레임에서 새어 나온 쓰레기다(엔티티 번호가 명단에 없다).
        if kind not in BIG or owner not in d["players"] or target not in d["players"]:
            continue
        sec = 0
        v = f" ({val})" if val else ""
        tl.append((ms, f"{BIG[kind]}  {label(d,owner)} → {label(d,target)}{v}"))
    for ms, txt in sorted(tl):
        sec = (ms - t0) / 1000
        L.append(f"   {int(sec)//60:02d}:{int(sec)%60:02d}  {txt}")
    L.append("")
    rows, nok = selfcheck(d)
    L.append(f"【검증】 게임이 보낸 전적과 대조 — {nok}/{len(rows)}명 완전 일치")
    for pid, k, dd, mk, md in rows:
        m = "✅" if (k == mk and dd == md) else f"⚠️ 처치{mk}/죽음{md}"
        L.append(f"   {label(d,pid).split('(')[0]:<14} 게임 {k}처치 {dd}죽음   {m}")
    L.append("")
    L.append("【교전 구간】 (피해가 몰린 때 · 그 구간 사망자로 승패 판정)")
    for a, b, tot, lost, win in fight_rounds(d):
        sa, sb = (a - t0) / 1000, (b - t0) / 1000
        sc = " / ".join(f"{t}팀 {n}명 사망" for t, n in sorted(lost.items())) or "사망 없음"
        w = f"→ {win}팀 우세" if win else "→ 무승부"
        L.append(f"   {int(sa)//60:02d}:{int(sa)%60:02d}~{int(sb)//60:02d}:{int(sb)%60:02d}"
                 f" ({sb-sa:4.1f}초) 피해 {tot:5d} · {sc}  {w}")
    L.append("")
    uv = ult_value(d)
    al = alive_time(d)
    L.append("【궁 값어치 · 생존율】  ※ 궁 뒤 8초 안에 일어난 일(인과 아님, 동시성)")
    L.append("   선수            궁  궁뒤 잡음  궁뒤 잃음   생존율")
    for pid in sorted(d["players"], key=lambda x: -uv[x][1]):
        n, got, lost = uv[pid]
        L.append(f"   {label(d,pid).split('(')[0]:<14} {n:3d} {got:9d} {lost:9d}   {al[pid]*100:5.1f}%")
    L.append("")
    # ── 경기 흐름 (점수 타임라인) ──────────────────────────────────────
    srows, flips = score_timeline(d)
    if srows:
        L.append("【경기 흐름】 (점수가 바뀐 순간)")
        for ms, a, b in srows[-12:] if len(srows) > 12 else srows:
            sec = (ms - t0) / 1000
            L.append(f"   {int(sec)//60:02d}:{int(sec)%60:02d}  1팀 {a} : {b} 2팀")
        for ms, team in flips:
            sec = (ms - t0) / 1000
            L.append(f"   ⚡ {int(sec)//60:02d}:{int(sec)%60:02d}  {team}팀이 역전!")
        L.append("")
    # ── 플레이 스타일 ─────────────────────────────────────────────────
    fp = fight_participation(d)
    po = positioning(d)
    fe = first_engage_counts(d)
    L.append("【플레이 스타일】  ※ 참여=교전 중 피해/회복 기록 기준 · 전방지수 +면 팀보다 앞에 섬")
    L.append("   선수          교전참여   전방지수   이동거리    첫진입")
    for pid in sorted(d["players"], key=lambda x: (d["players"][x]["team"])):
        n, tot = fp[pid]
        front = po[pid].get("front")
        fs = f"{front:+.2f}" if front is not None else "   ?"
        L.append(f"   {label(d,pid).split('(')[0]:<12} {n:>4}/{tot:<4} {fs:>9} "
                 f"{po[pid]['dist']:>8.0f}m {fe.get(pid,0):>7}회")
    L.append("")
    # ── 사망 심화 ─────────────────────────────────────────────────────
    dd = death_details(d)
    if dd:
        L.append("【사망 심화】  ※ 죽기 전 10초 피해 내역 · '혼자'=사망 순간 15유닛 안에 아군 없음")
        for ev in dd:
            sec = (ev["ms"] - t0) / 1000
            srcs = " + ".join(f"{label(d,q).split('(')[0]}({v})"
                              for q, v in sorted(ev["dmg_by"].items(), key=lambda x: -x[1]))
            alone = " · 🚶혼자였음" if ev["solo"] else (
                f" · 아군 {len(ev['allies_near'])}명 근처" if ev["allies_near"] else "")
            L.append(f"   {int(sec)//60:02d}:{int(sec)%60:02d}  {label(d,ev['tgt']).split('(')[0]:<12}"
                     f" {ev['burst_s']:4.1f}초 만에 사망 ← {srcs or '기록 없음'}{alone}")
        L.append("")
    L.append("【사망 위치】 (맵 좌표 x, z)")
    for ms, tgt, who, pos in death_spots(d):
        sec = (ms - t0) / 1000
        p = f"({pos[0]:7.1f}, {pos[2]:7.1f})" if pos else "(좌표 없음)"
        L.append(f"   {int(sec)//60:02d}:{int(sec)%60:02d}  {label(d,tgt).split('(')[0]:<14} {p}")
    return "\n".join(L)


def render_html(name, d):
    t0 = d["t0"]
    st = summarize(d)
    css = """body{font-family:-apple-system,'Apple SD Gothic Neo',sans-serif;margin:0;padding:24px;
    background:#12141a;color:#e8eaf0;line-height:1.6}h1,h2{color:#fff}h2{margin-top:32px;
    border-bottom:1px solid #2a2f3a;padding-bottom:6px}table{border-collapse:collapse;width:100%;
    max-width:900px}th,td{padding:6px 10px;text-align:right;border-bottom:1px solid #232833}
    th:first-child,td:first-child{text-align:left}th{color:#8b94a7;font-weight:600;font-size:13px}
    .t1{color:#4dabf7}.t2{color:#ff6b6b}.ev{display:flex;gap:14px;padding:4px 0;
    border-bottom:1px solid #1c2029;max-width:760px}.tm{color:#8b94a7;font-variant-numeric:tabular-nums;
    min-width:52px}.note{color:#8b94a7;font-size:13px}"""
    h = [f"<h1>{html.escape(name)} 경기 분석</h1>",
         f"<p class=note>프레임 {d['ok']}/{d['frames']} 완주 "
         f"({d['ok']*100//max(d['frames'],1)}%) · 리플레이 파일만으로 뽑은 것</p>"]
    kind, warn = match_kind(d)
    w, why = winner(d)
    h.append(f"<p class=note><b>경기 성격:</b> {html.escape(kind)} — {html.escape(warn)}<br>"
             f"<b>결과:</b> {('%d팀 승리' % w) if w else '무승부/판정불가'} "
             f"({html.escape(why)})</p>")
    h.append("<h2>명단</h2><p class=note>이름·영웅·레벨은 리플레이 파일의 <code>StartData</code> "
             "값(정확)이다. 괄호 안은 우리 파서가 <b>독립적으로 추론</b>한 값과 맞는지 대조한 것 — "
             "전부 ✅ 면 파싱이 맞다는 증거다.</p><ul>")
    for pid in sorted(d["players"]):
        h.append(f"<li>{html.escape(roster_line(d, pid))}</li>")
    h.append("</ul>")
    sk = collections.Counter((pid, k) for _, pid, k, _off in d.get("skills", []))
    h.append("<h2>선수별 요약</h2><table><tr><th>선수</th><th>팀</th><th>처치</th><th>죽음</th>"
             "<th>어시</th><th>준 피해</th><th>받은 피해</th><th>구조물</th><th>회복</th><th>실드</th>"
             "<th>스킬</th><th>궁</th></tr>")
    for pid, p in sorted(d["players"].items(), key=lambda x: (x[1]["team"], -st[x[0]]["준피해"])):
        c = st[pid]
        cls = "t1" if p["team"] == 1 else "t2"
        nm = label(d, pid).split("(")[0]
        h.append(f"<tr><td class={cls} title=\"{html.escape(roster_line(d,pid))}\">"
                 f"{html.escape(nm)}</td><td>{p['team']}</td>"
                 f"<td>{c['처치']}</td><td>{c['죽음']}</td><td>{c['어시']}</td><td>{c['준피해']}</td>"
                 f"<td>{c['받은피해']}</td><td>{c['구조물피해']}</td><td>{c['회복']}</td>"
                 f"<td>{c['실드']}</td><td>{sk[(pid,'스킬')]}</td><td>{sk[(pid,'궁')]}</td></tr>")
    h.append("</table>")
    cl = coach_lines(d)
    if cl:
        h.append("<h2>코칭 코멘트</h2><p class=note>데이터 기반 자동 판정 — 상황 맥락(핑·역할 분담)까지는 모른다.</p>")
        for line in cl:
            h.append(f"<div class=ev><span>{html.escape(line)}</span></div>")
    h.append("<h2>타임라인</h2>")
    tl = [(ms, f"✨ 궁 {html.escape(label(d,pid))}")
          for ms, pid, kind, _off in d.get("skills", []) if kind == "궁"]
    for ms, tgt, who in deaths(d):
        w = ", ".join(label(d, x).split("(")[0] for x in who)
        tl.append((ms, f"💀 {html.escape(label(d,tgt))} 사망 ← {html.escape(w)}"))
    for ms, hit in d["logs"]:
        kind, owner, target = hit[0], hit[1], hit[2]
        val = hit[4] if (kind in HAS_VALUE and len(hit) > 4) else None
        if kind not in BIG or owner not in d["players"] or target not in d["players"]:
            continue
        v = f" <span class=note>({val})</span>" if val else ""
        tl.append((ms, f"{BIG[kind]} {html.escape(label(d,owner))} → "
                       f"{html.escape(label(d,target))}{v}"))
    for ms, txt in sorted(tl):
        sec = (ms - t0) / 1000
        h.append(f"<div class=ev><span class=tm>{int(sec)//60:02d}:{int(sec)%60:02d}</span>"
                 f"<span>{txt}</span></div>")
    rows, nok = selfcheck(d)
    h.append(f"<h2>검증</h2><p class=note>게임이 직접 보내는 전적(PlayerStats)과 우리가 센 값을 "
             f"대조한 것. <b>{nok}/{len(rows)}명 완전 일치</b>. 어긋나면 그 선수 숫자는 믿지 말 것.</p>"
             "<table><tr><th>선수</th><th>게임 처치</th><th>게임 죽음</th>"
             "<th>우리 처치</th><th>우리 죽음</th><th></th></tr>")
    for pid, k, dd, mk, md in rows:
        m = "✅" if (k == mk and dd == md) else "⚠️"
        h.append(f"<tr><td>{html.escape(label(d,pid).split('(')[0])}</td><td>{k}</td><td>{dd}</td>"
                 f"<td>{mk}</td><td>{md}</td><td>{m}</td></tr>")
    h.append("</table>")
    # ── 경기 흐름 (점수 타임라인) ──────────────────────────────────────
    srows, flips = score_timeline(d)
    if len(srows) >= 2:
        t_lo = t0
        t_hi = max(ms for ms, _, _ in srows)
        mxv = max(max(a, b) for _, a, b in srows) or 1
        W2, H2 = 600, 120

        def px(ms):
            return (ms - t_lo) / max(1, t_hi - t_lo) * (W2 - 30) + 15

        def py(v):
            return H2 - 18 - v / mxv * (H2 - 36)
        h.append("<h2>경기 흐름</h2><p class=note>점수가 시간에 따라 어떻게 벌어졌나. "
                 "⚡ 표시는 리드가 뒤집힌 순간.</p>")
        h.append(f'<svg viewBox="0 0 {W2} {H2}" style="max-width:100%;background:#171a21;'
                 f'border:1px solid #262c38;border-radius:8px">')
        for ti, color in ((1, "#4dabf7"), (2, "#ff6b6b")):
            pts, lastv = [], 0
            for ms, a, b in srows:
                v = a if ti == 1 else b
                pts.append(f"{px(ms):.0f},{py(lastv):.0f}")   # 계단형(점수는 튀는 값)
                pts.append(f"{px(ms):.0f},{py(v):.0f}")
                lastv = v
            h.append(f'<polyline points="{" ".join(pts)}" fill=none stroke="{color}" '
                     f'stroke-width=2/>')
        for ms, team in flips:
            h.append(f'<text x="{px(ms):.0f}" y=14 text-anchor=middle font-size=12>⚡</text>')
        h.append("</svg>")
    # ── 궁 값어치 · 생존율 ────────────────────────────────────────────
    uv, al = ult_value(d), alive_time(d)
    h.append("<h2>궁 값어치 · 생존율</h2><p class=note>궁을 쓰고 <b>8초 안에</b> 그 팀이 몇 명을 "
             "잡았고 몇 명을 잃었는지. ⚠️ 인과가 아니라 동시성이다 — 궁으로 잡은 건지 "
             "이미 이기던 싸움이었는지는 이걸로 못 가른다.</p>")
    h.append("<table><tr><th>선수</th><th>팀</th><th>궁</th><th>궁 뒤 잡음</th>"
             "<th>궁 뒤 잃음</th><th>생존율</th></tr>")
    for pid in sorted(d["players"], key=lambda x: -uv[x][1]):
        n, got, lost = uv[pid]
        p = d["players"][pid]
        cls = "t1" if p["team"] == 1 else "t2"
        h.append(f"<tr><td class={cls}>{html.escape(label(d,pid).split('(')[0])}</td>"
                 f"<td>{p['team']}</td><td>{n}</td><td>{got}</td><td>{lost}</td>"
                 f"<td>{al[pid]*100:.0f}%</td></tr>")
    h.append("</table>")
    # ── 플레이 스타일 ─────────────────────────────────────────────────
    fp, po, fe = fight_participation(d), positioning(d), first_engage_counts(d)
    h.append("<h2>플레이 스타일</h2><p class=note>교전 참여 = 교전 구간에 피해/회복 기록이 있음 · "
             "전방지수 +면 팀 평균보다 앞에 서는 성향 · 첫 진입 = 교전을 제일 먼저 연 횟수.</p>")
    h.append("<table><tr><th>선수</th><th>팀</th><th>교전 참여</th><th>전방지수</th>"
             "<th>이동거리</th><th>첫 진입</th></tr>")
    for pid, p in sorted(d["players"].items(), key=lambda x: x[1]["team"]):
        n, tot = fp[pid]
        front = po[pid].get("front")
        cls = "t1" if p["team"] == 1 else "t2"
        h.append(f"<tr><td class={cls}>{html.escape(label(d,pid).split('(')[0])}</td>"
                 f"<td>{p['team']}</td><td>{n}/{tot}</td>"
                 f"<td>{f'{front:+.2f}' if front is not None else '?'}</td>"
                 f"<td>{po[pid]['dist']:.0f}m</td><td>{fe.get(pid,0)}회</td></tr>")
    h.append("</table>")
    # ── 사망 심화 ─────────────────────────────────────────────────────
    dd = death_details(d)
    if dd:
        h.append("<h2>사망 심화</h2><p class=note>죽기 전 <b>10초</b>의 피해 내역. "
                 "'혼자'는 사망 순간 15유닛 안에 아군이 없었다는 뜻 — 이게 반복되면 "
                 "포지셔닝을 의심할 것.</p>")
        for ev in dd:
            sec = (ev["ms"] - t0) / 1000
            srcs = " + ".join(f"{html.escape(label(d,q).split('(')[0])}({v})"
                              for q, v in sorted(ev["dmg_by"].items(), key=lambda x: -x[1]))
            alone = " · <b>🚶혼자였음</b>" if ev["solo"] else (
                f" · 아군 {len(ev['allies_near'])}명 근처" if ev["allies_near"] else "")
            h.append(f"<div class=ev><span class=tm>{int(sec)//60:02d}:{int(sec)%60:02d}</span>"
                     f"<span>💀 {html.escape(label(d,ev['tgt']).split('(')[0])} — "
                     f"{ev['burst_s']:.1f}초 만에 사망 ← {srcs or '기록 없음'}{alone}</span></div>")

    # ── 사망 위치 지도 ────────────────────────────────────────────────
    spots = [x for x in death_spots(d) if x[3]]
    # 2026-08-09: 뷰박스 범위도 heat_grid() bounds와 같은 원칙 — 선수(hero_entity)
    # 좌표만으로 잡는다. 차량·터렛·소품 좌표가 섞이면 축이 실제 활동 범위보다
    # 넓어져 그림이 뭉뚝해진다(18경기 실측 평균 14.3% 헐거움).
    heropos = [q for pid, p in d["players"].items()
               for _, q in d["pos"].get(p["hero_entity"], [])]
    if spots and heropos:
        xs = [p[0] for p in heropos]
        zs = [p[2] for p in heropos]
        x0, x1 = min(xs), max(xs)
        z0, z1 = min(zs), max(zs)
        W, H = 560, 560 * max(1e-6, (z1 - z0)) / max(1e-6, (x1 - x0))
        H = max(180, min(560, H))

        def sx(x):
            return (x - x0) / max(1e-6, x1 - x0) * (W - 40) + 20

        def sy(z):
            return (z - z0) / max(1e-6, z1 - z0) * (H - 40) + 20
        h.append("<h2>사망 위치 · 맵 장악</h2><p class=note>바탕의 색 번짐 = 그 팀이 그 지역에서 "
                 "보낸 시간(파랑 1팀 · 빨강 2팀). 겹치면 보라 = 격전지. 원은 죽은 자리, "
                 "번호는 순서 — 자리가 한쪽으로 밀리면 그쪽으로 전선이 밀렸다는 뜻이다.</p>")
        h.append(f'<svg viewBox="0 0 {W} {H:.0f}" style="max-width:100%;background:#171a21;'
                 f'border:1px solid #262c38;border-radius:8px">')
        hg = heat_grid(d)                                 # 팀별 장악 히트맵 (9차 추가)
        if hg:
            n = hg["n"]
            peak = max(max(c) for c in hg["cells"].values()) or 1
            cw, ch = (W - 40) / n, (H - 40) / n
            for (i, j), (c1, c2) in hg["cells"].items():
                for cnt, color in ((c1, "77,171,247"), (c2, "255,107,107")):
                    if not cnt:
                        continue
                    op = min(0.42, (cnt / peak) ** 0.5 * 0.42)
                    h.append(f'<rect x="{20+i*cw:.0f}" y="{20+j*ch:.0f}" width="{cw:.1f}" '
                             f'height="{ch:.1f}" fill="rgba({color},{op:.2f})"/>')
        for pid, p in d["players"].items():               # 선수 이동 흔적만 (소품 제외)
            # 2026-08-09: 전체 엔티티를 그리면 소품 경로가 그림 밖으로 나간다.
            # 실측: viewBox 560×560 인데 점 1.30% 밖(x: -83~645, y: -83~540).
            tr = d["pos"].get(p["hero_entity"], [])
            pts = " ".join(f"{sx(q[0]):.0f},{sy(q[2]):.0f}" for _, q in tr[::12])
            if pts:
                h.append(f'<polyline points="{pts}" fill=none stroke="#2b3240" stroke-width=1/>')
        for i, (ms, tgt, who, p) in enumerate(spots, 1):
            c = "#4dabf7" if d["players"][tgt]["team"] == 1 else "#ff6b6b"
            h.append(f'<circle cx="{sx(p[0]):.0f}" cy="{sy(p[2]):.0f}" r=9 fill="{c}" '
                     f'fill-opacity=.75/><text x="{sx(p[0]):.0f}" y="{sy(p[2])+4:.0f}" '
                     f'text-anchor=middle font-size=11 fill="#fff">{i}</text>')
        h.append("</svg>")
        h.append("<ul class=note>")
        for i, (ms, tgt, who, p) in enumerate(spots, 1):
            sec = (ms - t0) / 1000
            h.append(f"<li>{i}. {int(sec)//60:02d}:{int(sec)%60:02d} "
                     f"{html.escape(label(d,tgt).split('(')[0])} 사망</li>")
        h.append("</ul>")

    # ── 체력 곡선 ─────────────────────────────────────────────────────
    h.append("<h2>체력 곡선</h2><p class=note>선수별 체력(최대 대비). 바닥에 닿은 곳이 사망이다.</p>")
    for pid in sorted(d["players"]):
        tr = hp_track(d, pid)
        if len(tr) < 3:
            continue
        t_lo, t_hi = tr[0][0], tr[-1][0]
        pts = " ".join(f"{(m-t_lo)/max(1,t_hi-t_lo)*600:.0f},{34-v*30:.0f}" for m, v in tr)
        c = "#4dabf7" if d["players"][pid]["team"] == 1 else "#ff6b6b"
        h.append(f'<div class=ev><span class=tm>{html.escape(label(d,pid).split("(")[0])}</span>'
                 f'<svg viewBox="0 0 600 38" style="width:100%;max-width:600px">'
                 f'<polyline points="{pts}" fill=none stroke="{c}" stroke-width=1.6/></svg></div>')

    h.append("<h2>교전 구간</h2>")
    for a, b, tot, lost, win in fight_rounds(d):
        sa, sb = (a - t0) / 1000, (b - t0) / 1000
        sc = " / ".join(f"{t}팀 {n}명 사망" for t, n in sorted(lost.items())) or "사망 없음"
        w = f"<b>{win}팀 우세</b>" if win else "무승부"
        h.append(f"<div class=ev><span class=tm>{int(sa)//60:02d}:{int(sa)%60:02d}</span>"
                 f"<span>{sb-sa:.1f}초 · 피해 {tot} · {sc} → {w}</span></div>")
    return f"<!doctype html><meta charset=utf-8><title>{html.escape(name)} 경기 분석</title>" \
           f"<style>{css}</style>" + "".join(h)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        sys.exit(f"경기 이름을 줄 것. 있는 것: {', '.join(S.replays())}")
    name = args[0]
    d = collect(name)
    if "--html" in sys.argv:
        out = sys.argv[sys.argv.index("--html") + 1]
        open(out, "w", encoding="utf-8").write(render_html(name, d))
        print(f"{out} 저장")
    else:
        print(render_text(name, d))


if __name__ == "__main__":
    main()
