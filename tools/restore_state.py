#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""🧩 초기 상태(Restore) 메시지 파서 — 경기 시작 시점의 **모든 엔티티 전체 값**

## 왜 이게 중요한가 (2026-08-07 진행기록의 "다음 세션 1순위")

실시간 프레임(Data)은 **바뀐 값만** 실어 보낸다. 그래서 프레임 하나가 막히면
그 뒤를 못 읽는다. 반면 **Restore 는 경기 시작 상태를 한 덩어리로** 담고 있다.

- 프레임 경계가 없다 → 조각조각 끊길 일이 없다
- 엔티티마다 **모든 필드가 다 실려 있다** → 지도(map) 필드의 **항목 수를 역산**할 수 있다
  (`AnimationParameters` 항목 수 = 마지막 벽)
- 6인의 초기 체력이 여기 다 들어 있다 → 파싱이 맞는지 **정답과 대조**할 수 있다

## 전송·헤더 (코드에서 확인, 2026-08-07 7차)

`ClientTransportListener.OnReceiveServer`(0x75835A8) 의 분기:

    Parse() 결과 타입 1·2(StartRestore·Restore) → OnConnect   (필드 +0x48)
                    3(SendResult)              → OnFinishMatch(+0x58)
                    4(Data)                    → OnDataReceived(+0x50)

즉 **Restore 는 `PresentationHandler.Connect`(0x4C18D4C) 가 읽는다.** 그 안의 순서:

    ReadContainer.SetResult(bytes, BufferType.Restore=1)   # 0x6F16808
        └ stream.Reset(bytes) · logger.ClearRead
        └ ReadInt(8)          ← 첫 8비트   (Data 프레임에선 전송타입 바이트였던 자리)
        └ ReadPackageId(20)   ← 틱
        └ ReadTime(24)        ← 시각 ms
    ReadContainer.ReadPlayerId()      # 0x6F17498
    ReadContainer.ReadPacketType()    # 0x6F16E48 = 2비트
    _output.Read(container)           ← 여기부터는 Data 프레임 본문과 같은 문법

⚠️ **Data 프레임과 다른 점**: Data 는 SetResult 뒤에 `ReadPackageId(20)`(패킷번호)가 한 번 더
오지만, Restore 는 그 자리에 `ReadPlayerId` 가 온다. 또 Data 는 `ReadPacketType` 을 **반복**하는데
Restore 는 **한 번만** 부른다.

⚠️ **재조립 바이트에는 전송타입 바이트가 없다** (`TransportMessageParser.Parse` 0x6E8D964 확인:
StartRestore 는 `Array.Copy(bytes, 5, buf, 0, len-5)`, Restore 는 `Array.Copy(bytes, 1, buf, off, len-1)`).
그런데 SetResult 는 그래도 맨 앞 8비트를 읽는다 → **Restore 에선 그 8비트가 그냥 0으로 채워진 자리**다.

## ✅ 헤더는 **쓰는 쪽 코드 + 실측** 양쪽으로 확정됐다 (57비트)

쓰는 쪽 `WriteStreamContainer.ResetRestore`(0x6F1533C)가 순서대로 부르는 것:

    stream.WriteInt(0, 8)      → 8비트 0
    SetPackageIndex(packageId) → PackageBits 20
    SetTime(time)              → TimeBits    24
    SetPlayerId(playerId)      → PlayerBits   3
    SetPacketType(1)           → 2비트, 값 1(Data)
                                 합 = 57비트

`python3 도구/초기상태.py --헤더` 실측(세 경기 전부):

    경기        틱  시각ms  플레이어  패킷종류   첫 Data 프레임 시각   차이
    Q6TSVQ8B     0    9619        7         1              9639     +20ms
    R4A7SZ4A     0    8779        7         1              8799     +20ms
    TLJXLVAF     0    8299        7         1              8319     +20ms

**초기 상태는 첫 실시간 프레임보다 정확히 한 틱(20ms) 앞선다.** 세 경기 모두 딱 맞는다 =
헤더 해석이 맞다. (틱은 0, 플레이어는 7 고정)

## ✅ 본문도 뚫렸다 (2026-08-07 7차) — TLJXLVAF 99.99% · R4A7SZ4A 100% 완주

본문 문법은 Data 프레임과 **똑같다**(`BaseLogicOutputObject.Write` 0x6F1AF28):
`SetType(1) · SetEntityId · SetClearCacheTimeBit · 컴포넌트마다 SetType(t)+값`, 끝은 `SetType(0)` 한 번.

## 🎯 이 파일의 진짜 쓸모 = **비트 폭 검산기**

Data 프레임은 **바뀐 값만** 보낸다 → 값 필드의 게이트가 대부분 0 →
**폭이 틀려도 그 필드를 안 읽으니 안 걸린다.** Restore 는 전체 스냅샷이라 **전부 걸린다.**

> **판정법: 값(value) 필드의 게이트가 0으로 읽히면 그 직전 필드의 폭이 틀린 것이다.**
> `--추적` 이 그 자리를 ⚠️ 로 찍어 준다.

이걸로 7차에 폭 오류 5건을 잡았다(`AbilityStackOutputData` 23→43 · `TimerData` 조건분기 누락 ·
`Outgrowth.TargetRadius`/`ScaleTimerData` 추출 실패 · `Ragdoll.Bones` 24→**31**).

⚠️ **트리거(trigger) 필드는 스냅샷에 안 실린다** → 트리거 구조체 폭은 여기서 검산 못 한다.
그건 Data 프레임 완주 수로 판정할 것.

## 🎞️ 지도(map) 항목 수

`AnimationParameters` 4개 지도의 항목 수 = **그 캐릭터 애니메이션 컨트롤러의 파라미터 개수**
(`애니파라미터.py` 가 에셋에서 뽑는다). 엔티티↔영웅 짝은 `--맞추기` 가 13개를 넣어 보고 고른다.
`Ragdoll.Bones` = **31** (코드 상수 `IRagdollOutputComponent.MAX_POSSIBLE_BONE_SYNC_ID`).

## 쓰는 법

    python3 도구/초기상태.py TLJXLVAF          # 초기 상태 전체 (엔티티·컴포넌트·값)
    python3 도구/초기상태.py TLJXLVAF --추적   # ⭐ 필드 단위 추적 = 폭 오류 찾기
    python3 도구/초기상태.py TLJXLVAF --맞추기 # 엔티티 ↔ 영웅 세트 맞추기
    python3 도구/초기상태.py --저장            # 맞춘 결과 저장 (Data 프레임 파싱도 이걸 씀)
    python3 도구/초기상태.py --헤더            # 헤더 57비트 검산표
    python3 도구/초기상태.py --앵커            # 두 경기 공통 구간 앵커
"""
import os
import sys
import json
import collections

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import replay_stream as R

HERE = os.path.dirname(os.path.abspath(__file__))
REPLAY_DIR = os.path.join(os.path.dirname(HERE), "replays")

# 성공 판정용 정답 (진행기록 §"성공 판정 기준")
KNOWN_HP = {
    "TLJXLVAF": [1056, 774, 703, 968, 726, 645],
}

# SetResult 가 소비하는 폭 = 8 + PackageBits(20) + TimeBits(24)
HEAD_SETRESULT = 8 + R.PACKAGE_BITS + R.TIME_BITS          # 52
# 그 뒤 ReadPlayerId + ReadPacketType
HEAD_CONNECT = HEAD_SETRESULT + R.PLAYER_BITS + 2          # 57


def replays():
    """{경기이름: 경로}"""
    out = {}
    for fn in sorted(os.listdir(REPLAY_DIR)):
        if fn.endswith(".rsrpl.gz"):
            out[fn.split(".")[0]] = os.path.join(REPLAY_DIR, fn)
    return out


def restore_bytes(name):
    rep = R.load(replays()[name])
    msg, declared = R.restore_message(rep)
    if declared != len(msg):
        raise SystemExit(f"❌ {name}: 재조립 길이 불일치 {declared} ≠ {len(msg)}")
    return msg


def walk(msg, head_bits, outer=True):
    """헤더를 head_bits 만큼 건너뛰고 본문 문법으로 읽는다.

    본문 문법(Data 프레임과 같음): `타입7 → 0=끝 / 1=새엔티티(id11+1) / 그 외=컴포넌트`.

    `outer=True` 면 **바깥 반복**(`ReadPacketType(2)`)까지 돈다.
    코드상 `Connect` 는 `ReadPacketType` 을 한 번만 부르지만, 한 번만 읽으면
    6383바이트 중 **139비트에서 끝 표시가 나온다**(= 말이 안 된다). 그래서 실측으로 확인한다.

    되돌려주는 것:
      ok        끝(타입 3 또는 소비 완료)까지 깨끗하게 갔나
      events    [(엔티티, 타입, 컴포넌트, 값)]
      bad       있을 수 없는 타입(>81)·있을 수 없는 패킷종류가 나온 횟수 (↓좋음)
      stopped   멈춘 이유
      used/total 소비한 비트 / 전체 비트
    """
    s = R.Stream(msg)
    for _ in range(head_bits):
        s.read(1)
    ent, events, bad, stopped, ok = None, [], 0, None, False
    bodies = 0
    try:
        while True:
            t = s.read(R.TYPE_BITS)
            if t == R.TYPE_END:
                bodies += 1
                if not outer:
                    ok = True
                    stopped = "끝 표시"
                    break
                # 바깥 반복: 다음 패킷 종류를 읽는다
                if s.left < 2:
                    ok = True
                    stopped = "소비 완료"
                    break
                pt = s.read(2)
                if pt == 1:
                    continue
                if pt == 2:
                    s.read(R.PACKAGE_BITS)
                    continue
                if pt == 3:
                    ok = True
                    stopped = "끝(pt=3)"
                    break
                bad += 1
                stopped = f"있을 수 없는 패킷종류 {pt}"
                break
            if t == R.TYPE_NEW_ENTITY:
                ent = s.read(R.ENTITY_BITS)
                s.read_bool()
                continue
            if t not in R.T2C:
                bad += 1
                stopped = f"있을 수 없는 타입 {t}"
                break
            events.append((ent, t, R.T2C[t], R.read_component(s, t, ent)))
    except R.UnknownComponent as e:
        stopped = f"미지 컴포넌트 {e}"
    except EOFError:
        stopped = "EOF"
    total = len(msg) * 8
    return {"ok": ok, "events": events, "bad": bad, "stopped": stopped, "bodies": bodies,
            "used": total - s.left, "total": total, "at_entity": ent}


def score(r):
    """헤더 후보 비교용 점수. 끝까지 갔으면 압도적으로 높게."""
    return (1_000_000 if r["ok"] else 0) + len(r["events"]) - 200 * r["bad"]


def cmd_scan(names, lo=0, hi=96):
    """헤더 비트 폭을 실측으로 고른다.

    코드상 정답은 57(HEAD_CONNECT)이어야 하지만, 단정하지 않고 전 구간을 훑어
    **실제로 제일 멀리 읽히는 값**을 본다. 57 이 이기면 코드 해석이 맞은 것이다.
    """
    print(f"■ 헤더 비트 폭 훑기 ({lo}~{hi})   ※ 코드 예상 = {HEAD_CONNECT}"
          f" (SetResult {HEAD_SETRESULT} + PlayerId {R.PLAYER_BITS} + PacketType 2)")
    for name in names:
        msg = restore_bytes(name)
        rows = []
        for h in range(lo, hi + 1):
            r = walk(msg, h)
            rows.append((score(r), h, r))
        rows.sort(key=lambda x: -x[0])
        print(f"\n  ▸ {name}  ({len(msg)}바이트 = {len(msg)*8}비트)")
        print("     비트  이벤트  어긋남  소비/전체        멈춘 이유")
        for sc, h, r in rows[:6]:
            mark = " ←코드예상" if h == HEAD_CONNECT else ""
            print(f"     {h:4d} {len(r['events']):7d} {r['bad']:7d}  "
                  f"{r['used']:6d}/{r['total']:<6d} {r['stopped']}{mark}")
        best = rows[0]
        if best[1] != HEAD_CONNECT:
            got = next(r for sc, h, r in rows if h == HEAD_CONNECT)
            print(f"     ⚠️ 1등({best[1]})이 코드예상({HEAD_CONNECT})과 다르다 — "
                  f"예상값은 이벤트 {len(got['events'])} · 어긋남 {got['bad']}")


def cmd_dump(name, head, hp_only=False):
    msg = restore_bytes(name)
    R.use_entity_counts(name)
    r = walk(msg, head)
    print(f"■ {name} 초기 상태 — {len(msg)}바이트 · 헤더 {head}비트")
    print(f"  이벤트 {len(r['events'])}개 · 어긋남 {r['bad']} · "
          f"{r['used']}/{r['total']}비트 소비 · 멈춤: {r['stopped']}")

    by_ent = collections.OrderedDict()
    for ent, t, comp, val in r["events"]:
        by_ent.setdefault(ent, []).append((t, comp, val))
    print(f"  엔티티 {len(by_ent)}개\n")

    if hp_only:
        print("   엔티티   MaxHp     Hp  Shields")
        hps = []
        for ent, rows in by_ent.items():
            for t, comp, v in rows:
                if comp == "Hp":
                    print(f"  {ent:7d} {v.get('MaxHp','·'):>7} {v.get('Hp','·'):>6} "
                          f"{v.get('Shields','·'):>8}")
                    if v.get("MaxHp"):
                        hps.append(v["MaxHp"])
        ans = KNOWN_HP.get(name)
        if ans:
            print(f"\n  정답(진행기록): {sorted(ans)}")
            print(f"  나온 값       : {sorted(hps)}")
            print("  → " + ("✅ 일치" if sorted(hps) == sorted(ans) else "❌ 불일치"))
        return

    for ent, rows in by_ent.items():
        print(f"  ▸ 엔티티 {ent}  ({len(rows)}개 컴포넌트)")
        for t, comp, v in rows:
            body = ", ".join(f"{k}={_short(x)}" for k, x in v.items()) or "(빈 값)"
            print(f"      {t:3d} {comp:<26} {body}")


def _short(x):
    s = repr(x)
    return s if len(s) <= 90 else s[:87] + "…"


def anim_sets(dedupe=True):
    """`애니파라미터.py` 가 뽑아 둔 영웅별 지도 항목 수 (13종).

    `dedupe=True` 면 **항목 수가 완전히 같은 영웅은 하나로 합친다**
    (Calibri≡Nova, Magnus≡Sejin). 파싱에는 개수만 쓰이므로 탐색 후보가 줄어 빨라진다.
    이름은 "Calibri/Nova" 처럼 붙여 표시한다 — 개수만으로는 둘을 구분할 수 없다는 뜻이다.
    """
    path = os.path.join(HERE, "anim_params.json")
    if not os.path.exists(path):
        raise SystemExit("먼저 `python3 도구/애니파라미터.py --json` 을 돌릴 것")
    data = json.load(open(path, encoding="utf-8"))
    out = {}
    for name, v in data.items():
        c = v["counts"]
        if name and sum(c.values()) > 0:          # 이름 없는 오탐 1건 제외
            out[name.replace("_Controller_ParameterSet", "")] = c
    if not dedupe:
        return out
    merged = {}
    for hero, c in out.items():
        key = tuple(sorted(c.items()))
        merged.setdefault(key, []).append(hero)
    return {"/".join(v): dict(k) for k, v in merged.items()}


def solve_anim(name, head=None, verbose=False):
    """🧠 엔티티마다 어느 영웅 세트인지 **실측으로 고른다**.

    `AnimationParameters` 지도 항목 수는 캐릭터(영웅)마다 다르다. 후보가 13개뿐이라
    "넣어 보고 더 멀리 읽히는 쪽"을 고르면 된다 — 틀린 값은 몇 십 비트 안에 죽는다.
    (예전에 실패한 방식은 후보가 0~64 전 범위라 퇴화 해가 잔뜩 나왔던 것이다)
    """
    head = HEAD_CONNECT if head is None else head
    msg = restore_bytes(name)
    sets = anim_sets()
    R.use_entity_counts(name)
    AP = "AnimationParameters"

    def apply(assign):
        for k in [k for k in R.ENTITY_MAP_COUNTS if k[1] == AP]:
            del R.ENTITY_MAP_COUNTS[k]
        for ent, hero in assign.items():
            for f, n in sets[hero].items():
                R.ENTITY_MAP_COUNTS[(ent, AP, f)] = n

    def run(assign):
        apply(assign)
        r = walk(msg, head, outer=False)
        need = None
        if "항목수 미상" in (r["stopped"] or "") and AP in (r["stopped"] or ""):
            need = r["at_entity"]
        return r, need

    best = [None, {}]
    budget = [4000]          # 최대 걷기 횟수 (폭주 방지)

    def dfs(assign, depth=0):
        budget[0] -= 1
        """엔티티마다 후보 13개를 넣어 보고 **더 멀리 읽히는 쪽**을 고른다.

        전수 탐색(13^깊이)은 너무 크므로 **상위 2개만** 파고든다. 맞는 값은 보통
        압도적으로 멀리 가기 때문에 이 정도로 충분하다(틀린 값은 몇 십 비트 안에 죽는다).
        """
        r, need = run(assign)
        if best[0] is None or r["used"] > best[0]["used"]:
            best[0], best[1] = r, dict(assign)
        if need is None or need in assign or depth > 40:
            return
        here = r["used"]
        tried = []
        for hero in sets:
            a = dict(assign)
            a[need] = hero
            r2, _ = run(a)
            if r2["used"] > here:
                tried.append((r2["used"], hero, a))
        tried.sort(key=lambda x: -x[0])
        # 맞는 값은 보통 압도적으로 멀리 간다 → **1등만** 파고든다(탐욕).
        # 1등이 애매할 때만 2등도 보되 **얕은 깊이에서만** — 안 그러면 2^깊이로 터진다.
        # (Q6TSVQ8B 는 캐릭터가 15명이라 제한이 없으면 몇 분씩 걸렸다)
        take = 2 if (depth < 3 and len(tried) > 1 and tried[0][0] - tried[1][0] < 200) else 1
        for _, _, a in tried[:take]:
            if budget[0] <= 0:
                return
            dfs(a, depth + 1)

    dfs({})
    r, assign = best
    apply(assign)                      # 고른 답을 남겨 둔다(다른 명령이 이어서 쓴다)
    if verbose:
        print(f"■ {name} — 영웅 세트 맞추기")
        for ent, hero in sorted(assign.items()):
            c = sets[hero]
            print(f"   엔티티 {ent:5d} → {hero:<8} (Trigger {c['TriggerParameters']} · "
                  f"Bool {c['BoolParameters']} · Int {c['IntParameters']} · Float {c['FloatParameters']})")
        print(f"   → {r['used']}/{r['total']}비트 ({r['used']*100//r['total']}%) · "
              f"이벤트 {len(r['events'])} · 멈춤: {r['stopped']}")
    return assign, r


def cmd_solve(name, head=None):
    return solve_anim(name, head, verbose=True)


ANIM_ASSIGN = os.path.join(HERE, "anim_sets.json")


def candidates(name, assign, sets):
    """엔티티마다 **끝까지 읽히는 영웅을 전부** 모은다.

    🪤 솔버가 고른 하나를 정답처럼 쓰면 안 된다. 지도(map) 게이트가 0 인 프레임에서는
       항목 수가 아예 안 쓰여서, **여러 영웅이 똑같이 통과**한다.
       실제로 TLJXLVAF 엔티티 67 은 Fury·Magnus/Sejin·Twinkle 셋 다 통과했다.
       → 여기서 후보를 전부 남기고, 최종 판별은 `경기분석.py` 가 **최대 체력과 교차**해서 한다.
    """
    msg = restore_bytes(name)
    out = {}
    for ent in assign:
        ok = []
        for hero, c in sets.items():
            R.ENTITY_MAP_COUNTS.clear()
            for e, h in assign.items():
                use = c if e == ent else sets[h]
                for f, k in use.items():
                    R.ENTITY_MAP_COUNTS[(e, "AnimationParameters", f)] = k
            r = walk(msg, HEAD_CONNECT, outer=False)
            if r["used"] * 100 / r["total"] > 99:
                ok.append(hero)
        out[ent] = ok or [assign[ent]]
    # 원래 답을 되돌려 놓는다
    R.ENTITY_MAP_COUNTS.clear()
    for e, h in assign.items():
        for f, k in sets[h].items():
            R.ENTITY_MAP_COUNTS[(e, "AnimationParameters", f)] = k
    return out


def player_hero_entities(name):
    """선수 6명이 **조종하는 캐릭터 엔티티** 번호. (초기 상태의 `CurrentHero.HeroNetworkId`)

    영웅 **이름**이 필요한 건 이 엔티티들뿐이다 — 포탑·오브젝트 같은 나머지는 파싱만 맞으면 된다.
    """
    try:
        r = walk(restore_bytes(name), HEAD_CONNECT, outer=False)
    except Exception:
        return []
    out = []
    for e, t, c, v in r["events"]:
        if c == "CurrentHero" and isinstance(v, dict) and v.get("HeroNetworkId"):
            out.append(v["HeroNetworkId"])
    return out


def refine_with_data(name, assign, cand, sets, quick=False):
    """🎯 세 번째 신호 — **실시간 프레임**으로 영웅 후보를 더 좁힌다.

    초기 상태에서는 `AnimationParameters` 지도의 게이트가 0 인 경우가 많아 **항목 수가 아예
    안 쓰이고**, 그래서 여러 영웅이 똑같이 통과한다. 반면 **실시간 프레임에서는 게이트가 1**
    이라 개수가 실제로 쓰인다 → 틀린 개수는 그 캐릭터가 나오는 프레임을 깨뜨린다.

    그래서 **그 캐릭터가 등장하는 프레임만 세서** 완주 비율이 제일 높은 후보를 고른다.
    차이는 0.5~1% 로 작지만 **여섯 명 모두 일관되게** 갈렸고, 그 결과가 진행기록에 적힌
    예상 명단(Fury11·Khan12·Sejin11·Fury15·Leo15·Nova15)과 **정확히 일치**했다(2026-08-07).

    ⚡ `quick=True` (서버용) — 이 단계가 **전체 시간의 94%**를 먹는다(2026-08-08 실측:
       47초 중 44초. 후보마다 7,271프레임을 통째로 다시 파싱하기 때문). Render 무료 CPU에선
       그게 8분이 된다. 그래서 빠른 길을 따로 둔다:
         ① 먼저 **실패한 프레임만** 모아 "어느 엔티티에서 멈췄나"로 범인을 찾고,
            그 엔티티의 후보만 바꿔 가며 **실패 프레임만** 다시 읽는다(전체가 아니라).
         ② 그래도 안 끝나면 남은 애매한 엔티티만 원래 방식으로 가른다.
       느린 길(quick=False)은 로컬 표본 만들 때 쓴다 — 결과가 같은지 회귀검사로 확인한다.
    """
    rep = R.load(replays()[name])
    D = [b for k, b in R.frames(rep) if k == R.T_DATA]

    def apply(a):
        R.ENTITY_MAP_COUNTS.clear()
        for e, h in a.items():
            for f, k in sets[h].items():
                R.ENTITY_MAP_COUNTS[(e, "AnimationParameters", f)] = k

    def ratio(ent, pool=None):
        tot = done = 0
        for b in (pool if pool is not None else D):
            fr = R.parse_frame(b)
            if not (fr.get("at_entity") == ent
                    or any(e == ent and c == "AnimationParameters" for e, t, c, v in fr["events"])):
                continue
            tot += 1
            if (fr["stopped"] or "") in ("end", "EOF", ""):
                done += 1
        return done / max(tot, 1)

    out_assign, out_cand = dict(assign), dict(cand)

    if quick:
        def failures(a, pool):
            """실패한 프레임과 '멈춘 자리의 엔티티' 목록."""
            apply(a)
            out = []
            for b in pool:
                fr = R.parse_frame(b)
                if (fr["stopped"] or "") not in ("end", "EOF", ""):
                    out.append((b, fr.get("at_entity")))
            return out

        bad = failures(out_assign, D)
        for _round in range(8):                       # 안 나아지면 그만둔다
            if not bad:
                break
            pool = [b for b, _e in bad]
            ranked = collections.Counter(e for _b, e in bad if e is not None).most_common()
            fixed = False
            for ent, _n in ranked:
                if len(out_cand.get(ent, [])) < 2:
                    continue
                base = len(bad)
                pick = None
                for h in out_cand[ent]:
                    if h == out_assign.get(ent):
                        continue
                    a = dict(out_assign)
                    a[ent] = h
                    n = len(failures(a, pool))         # ⚡ 실패 프레임만 다시 읽는다
                    if n < base:
                        base, pick = n, h
                if pick:
                    out_assign[ent] = pick
                    out_cand[ent] = [pick]
                    fixed = True
                    break
            if not fixed:
                break
            bad = failures(out_assign, D)

        # ② 이름이 **필요한 곳만** 정밀 판정한다.
        #    파싱은 위 ①로 이미 100% 가 된다. 그런데 `Leo`↔`Remedy` 처럼 애니 개수·체력 성장·
        #    컴포넌트 지문이 **전부 같은 쌍**은 완주율(신호 ③)로만 갈린다.
        #    예전엔 엔티티 20여 개를 전부 이 방식으로 돌려 44초를 썼는데, 정작 이름이 필요한 건
        #    **선수 캐릭터 6명뿐**이다 → 대상만 좁히니 몇 초로 끝난다.
        for ent in player_hero_entities(name):
            cs = out_cand.get(ent) or []
            if len(cs) < 2:
                continue
            scored = []
            for h in cs:
                a = dict(out_assign)
                a[ent] = h
                apply(a)
                scored.append((ratio(ent), h))
            scored.sort(reverse=True)
            top = scored[0][0]
            out_assign[ent] = scored[0][1]
            out_cand[ent] = [h for r, h in scored if top - r < 0.002]
        apply(out_assign)
        return out_assign, out_cand

    for ent, cs in cand.items():
        if len(cs) < 2:
            continue
        scored = []
        for h in cs:
            a = dict(out_assign)
            a[ent] = h
            apply(a)
            scored.append((ratio(ent), h))
        scored.sort(reverse=True)
        top = scored[0][0]
        # 1등과 사실상 동률인 것만 후보로 남긴다(0.2%p 이내)
        keep = [h for r, h in scored if top - r < 0.002]
        out_assign[ent] = scored[0][1]
        out_cand[ent] = keep
    apply(out_assign)
    return out_assign, out_cand


def cmd_save(names, quick=False):
    """푼 결과를 저장 — **실시간 프레임(Data) 파싱에도 그대로 쓴다.**

    지도 항목 수는 캐릭터마다 달라서, 초기 상태에서 한 번 풀어 두면 그 경기 전체에 쓸 수 있다.
    (예전 `entity_map_counts.json` 은 폭이 틀렸을 때 맞춘 값이라 **쓰면 안 된다** — 이걸로 대체)
    """
    out = {}
    if os.path.exists(ANIM_ASSIGN):
        out = json.load(open(ANIM_ASSIGN, encoding="utf-8"))
    sets = anim_sets()
    for name in names:
        assign, r = solve_anim(name)
        done = r["used"] * 100 // r["total"]
        cand = candidates(name, assign, sets)
        assign, cand = refine_with_data(name, assign, cand, sets, quick=quick)
        out[name] = {"완주율": done,
                     "엔티티별_영웅": {str(e): h for e, h in sorted(assign.items())},
                     "엔티티별_후보": {str(e): v for e, v in sorted(cand.items())},
                     "항목수": {str(e): sets[h] for e, h in assign.items()}}
        uniq = sum(1 for v in cand.values() if len(v) == 1)
        print(f"  {name}: 엔티티 {len(assign)}개 · 초기상태 {done}% 완주 · "
              f"영웅 확정 {uniq}/{len(cand)}명 (나머지는 후보 여럿)")
    json.dump(out, open(ANIM_ASSIGN, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"저장: {ANIM_ASSIGN}")


def cmd_trace(name, head=None, tail=14):
    """🔬 필드 단위 추적 — 폭이 틀린 provider 를 찾는 작업용.

    **핵심 판정법**: 초기 상태는 전체 스냅샷이라 **값(value) 필드의 게이트는 전부 1이어야 한다.**
    게이트 0 이 보이면 그 **직전 필드의 비트 폭이 틀린 것**이다(그만큼 밀려서 엉뚱한 비트를 읽음).
    실시간 프레임은 게이트가 대부분 0이라 이 오차가 안 드러난다 — 그래서 여기서만 잡힌다.
    """
    head = HEAD_CONNECT if head is None else head
    msg = restore_bytes(name)
    NB = len(msg) * 8
    R.use_entity_counts(name)
    solve_anim(name, head)          # 영웅별 지도 항목 수를 먼저 맞춰 둔다
    s = R.Stream(msg)
    for _ in range(head):
        s.read(1)

    def pos():
        return NB - s.left

    log, ent, zero_gates = [], None, []
    try:
        while True:
            p = pos()
            t = s.read(R.TYPE_BITS)
            if t == R.TYPE_END:
                log.append((p, f"■ 끝 표시(type 0)"))
                break
            if t == R.TYPE_NEW_ENTITY:
                e = s.read(R.ENTITY_BITS)
                s.read_bool()
                ent = e
                log.append((p, f"── 엔티티 {e}"))
                continue
            if t not in R.T2C:
                log.append((p, f"❌ 있을 수 없는 타입 {t}"))
                break
            comp = R.T2C[t]
            log.append((p, f"   [{t}] {comp}"))
            fields = R.INT_READERS.get(t)
            if fields is None:
                log.append((pos(), f"   ❌ {comp} 리더 없음"))
                break
            for fname, kind, fn, mreader in fields:
                q = pos()
                if kind == "value":
                    if s.read_bool():
                        v = fn(s)
                        log.append((q, f"      {fname} = {str(v)[:46]}  ({pos()-q}비트)"))
                    else:
                        log.append((q, f"      {fname} ⚠️ 게이트0 ← 앞 필드 폭 의심"))
                        zero_gates.append((q, comp, fname))
                elif kind == "trigger":
                    n = 0
                    while s.read_bool():
                        s.read_ranged(*R.TRIGGER_INDEX_BITS)
                        s.read_ranged(*R.OFFSET_TIME_BITS)
                        fn(s)
                        n += 1
                    log.append((q, f"      {fname} 트리거 {n}건 ({pos()-q}비트)"))
                else:
                    if not s.read_bool():
                        log.append((q, f"      {fname} 지도 없음 (1비트)"))
                        continue
                    key = (ent, comp, fname)
                    cnt = R.ENTITY_MAP_COUNTS.get(key, R.MAP_COUNTS.get((comp, fname)))
                    log.append((q, f"      {fname} 지도 항목수={cnt}"))
                    if cnt is None:
                        raise R.UnknownComponent(f"{comp}.{fname} 항목수 미상")
                    for i in range(cnt):
                        if not s.read_bool():
                            continue
                        if mreader == "TriggerSyncMapReader":
                            while True:
                                s.read_ranged(*R.TRIGGER_INDEX_BITS)
                                s.read_ranged(*R.OFFSET_TIME_BITS)
                                fn(s)
                                if not s.read_bool():
                                    break
                        else:
                            fn(s)
    except R.UnknownComponent as e:
        log.append((pos(), f"❌ 미지: {e}"))
    except EOFError:
        log.append((pos(), "❌ EOF"))

    print(f"■ {name} 초기 상태 추적 — {NB}비트 중 {pos()}비트까지 ({pos()*100//NB}%)")
    print(f"  마지막 {tail}줄:")
    for p, line in log[-tail:]:
        print(f"  {p:6d} {line}")
    if zero_gates:
        print(f"\n  ⚠️ 게이트0 인 값 필드 {len(zero_gates)}건 (전부 폭 오류 후보):")
        for p, c, f in zero_gates[:8]:
            print(f"     {p:6d} {c}.{f}")


def cmd_head():
    """헤더 실측표 — 초기 상태의 시각이 첫 Data 프레임보다 정확히 한 틱(20ms) 앞서야 맞다."""
    print("■ 초기 상태 헤더 (8비트0 + 틱20 + 시각24 + 플레이어3 + 패킷종류2 = 57비트)")
    print("  경기        바이트    틱   시각ms  플레이어  패킷종류 | 첫 Data 시각    차이")
    for name, path in replays().items():
        rep = R.load(path)
        msg, _ = R.restore_message(rep)
        s = R.Stream(msg)
        s.read(8)
        tick, t = s.read(R.PACKAGE_BITS), s.read(R.TIME_BITS)
        pid, pt = s.read(R.PLAYER_BITS), s.read(2)
        first = R.parse_frame([b for k, b in R.frames(rep) if k == R.T_DATA][0], walk=False)
        ok = "✅" if first["time"] - t == 20 else "❌"
        print(f"  {name}  {len(msg):7d} {tick:5d} {t:8d} {pid:9d} {pt:9d} | "
              f"{first['time']:11d} {first['time']-t:+7d}ms {ok}")


def cmd_anchor():
    """두 경기의 초기 상태가 겹치는 구간 — 첫 엔티티 블록 길이를 확정해 준다."""
    a, b = restore_bytes("TLJXLVAF"), restore_bytes("R4A7SZ4A")
    best = (0, 0, 0)
    for oa in range(80):
        for ob in range(80):
            n = 0
            while n < min(len(a) - oa, len(b) - ob) and a[oa + n] == b[ob + n]:
                n += 1
            if n > best[2]:
                best = (oa, ob, n)
    oa, ob, n = best
    print(f"■ 공통 구간: TLJXLVAF[{oa}바이트:] == R4A7SZ4A[{ob}바이트:] 로 {n}바이트 일치")
    print(f"  = 비트로 {oa*8} ↔ {ob*8}  (차이 {oa*8-ob*8}비트)")
    blk_end = oa * 8 - R.TYPE_BITS          # 그 앞 7비트는 다음 엔티티의 type=1 태그
    print(f"  → TLJXLVAF 첫 엔티티 블록 = 64 ~ {blk_end-1}비트 = {blk_end-64}비트")
    print(f"     (id 11 + 캐시비트 1 + 컴포넌트 {blk_end-76}비트)")
    s = R.Stream(a)
    for _ in range(blk_end):
        s.read(1)
    t = s.read(R.TYPE_BITS)
    print(f"  → {blk_end}비트째 타입 = {t}  " + ("✅ 1(새 엔티티) 맞음" if t == 1 else "❌ 1이 아님"))


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    opts = {a for a in sys.argv[1:] if a.startswith("--")}
    head = HEAD_CONNECT
    for a in list(opts):
        if a.startswith("--head="):
            head = int(a.split("=")[1])
    names = args or list(replays())
    if "--저장" in opts:
        cmd_save(names, quick="--빠르게" in opts)
        return
    if "--맞추기" in opts:
        for n in names:
            cmd_solve(n, head)
            print()
        return
    if "--추적" in opts:
        for n in names:
            cmd_trace(n, head)
            print()
        return
    if "--헤더" in opts:
        cmd_head()
        return
    if "--앵커" in opts:
        cmd_anchor()
        return
    if "--scan" in opts:
        cmd_scan(names)
        return
    for n in names:
        cmd_dump(n, head, hp_only="--hp" in opts)
        print()


if __name__ == "__main__":
    main()
