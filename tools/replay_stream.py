#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""⭐⭐⭐ 리플레이 스트림 해독기 — 전송계층 + 프레임 헤더 + 본문 문법

2026-08-07에 **본문 문법까지 전부 뚫렸다.** 이 파일이 그 결론이다.

────────────────────────────────────────────────────────────────────────
## 1) 비트 순서 — **LSB-first** (예전 두 세션의 결론은 둘 다 틀렸다)

`BinaryStream.Reset`(RVA 0x6F1D60C) 디스어셈블:
    buf[i>>2] |= data[i] << (8*(i&3))          # 리틀엔디안 워드 패킹
`BinaryStream.ReadInt(bits)`(RVA 0x6F1E158):
    value = (buf[off>>5] << ((32-bits-off)&31)) >>> (32-bits)

`<< (32-bits-off)` 는 **워드의 하위 비트를 위로 밀어 올리는** 연산이라,
결과는 "워드의 off번째 비트부터 bits개" = 워드 안에서 **하위 비트부터** 읽는다는 뜻이다.
워드 패킹이 리틀엔디안이므로 이 둘을 합치면 결국

    스트림 비트 k = 바이트 (k>>3) 의 비트 (k&7)      ← 그냥 **LSB-first**

워드 경계를 넘을 땐 `(뒷부분 << 앞부분폭) | 앞부분` 으로 잇는데, 이것도 LSB-first 와 같다.

> 🚩 그래서 "워드 4바이트 뒤집기"(2·3·4차)도, "평범한 MSB"(5차 정정)도 **둘 다 틀렸다.**
>    정답은 **LSB-first**. 이 파일의 `Stream` 은 어셈블리를 그대로 옮긴 것이라 논쟁이 필요 없다.

## 2) 전송 계층 — `TransportMessageParser` (dump.cs 1983519행)

리플레이 `InData[].Data` 는 **전송 패킷 원본 그대로**다(`ReplayWriteProcessor.OnInDataReceived`
가 `Array.Copy(bytes, 0, …, length)` 로 통째 저장). 첫 바이트가 전송 타입이다.

    TransportMessage: Connect=0 · StartRestore=1 · Restore=2 · SendResult=3 · Data=4
    Data(4)         : [0]=4, 그 뒤가 실시간 페이로드
    StartRestore(1) : [0]=1, [1..5]=전체 길이(LE32), [5..]=첫 조각
    Restore(2)      : [0]=2, [1..]=이어지는 조각      → 다 모으면 '초기 상태' 한 덩어리

    ✅ 검증: 길이 필드 = 실제 조각 합계, **세 리플레이 전부 오차 0**
             (TLJXLVAF 6383 · R4A7SZ4A 6448 · Q6TSVQ8B 11961)

## 3) 프레임 헤더 — `ReadContainer.SetResult`(0x6F16808) + `PresentationHandler.Receive`(0x4C193C4)

    ReadInt(8)          ← 전송 타입 바이트(스트림이 그대로 다시 읽고 버린다)
    ReadPackageId(20)   ← **틱**
    ReadTime(24)        ← **경기 시각(ms)**
    ReadPackageId(20)   ← 패킷 번호
    반복 { ReadPacketType(2) : 0=?  1=출력본문  2=수신확인(+PackageId 20)  3=끝 }

    ✅ 검증(세 리플레이 Data 프레임 14,390개 전부):
       · ReadInt(8) == 전송 타입 100%
       · 시각 Δ = **정확히 +20ms** (50Hz)
       · 첫 ReadPacketType == **1** 100%   ← 1이어야만 출력 본문이 이어진다
       · 시각 첫값이 ReadContainer 의 ConnectTime 과 일치 (8319ms vs 8.350s 등)
       · 틱 Δ = 강타 +4 · 장악 +7 (모드별 틱 간격과 일치)

## 4) 본문 문법 — `BasePresentationOutput.Read`(0x56C139C) + `PresentationOutput.UpdateBuffer`(0x6EF2728)

    while (true) {
        type = ReadType()                  # ReadInt(0, CountTypes=100) = 7비트
        if (type == 0) break                       # 메시지 끝
        if (type == 1) {                           # 다음 엔티티로 전환
            entityId = ReadEntityId()              # ReadInt(EntityBits=11)
            clearCache = ReadClearCacheTimeBit()   # ReadBool() = 1비트
            continue
        }
        components[type].Read(reader)              # 현재 엔티티의 컴포넌트 갱신
    }

`type` ↔ 컴포넌트는 UpdateBuffer 의 **점프 테이블에서 직접** 뽑았다 → `wire_types.json`
(`도구/wire_types.py`). **Hp=3 · Transform=23 · UltimateAbility=17 · AbilityUsageState=65**

    ✅ 검증: 첫 type 이 1(새 엔티티) = 4,267/4,278. 엔티티 번호 65종(6인+오브젝트).
       두 번째 type 상위 = Transform·AnimationParameters·Ragdoll·Hit·Hp … 전부 말이 됨.

## 5) 컴포넌트 읽기 규칙 — `HpPresentationReader.Read`(0x6F27F24) 로 확인

    필드마다:  if (ReadCheck())  value = provider.GetValue(stream)
    ReadCheck() = ReadBool() = **1비트**. 필드 순서 = 인터페이스 선언 순서(sync_spec.json).

    Hp(type 3) = [1비트]MaxHp(14) · [1비트]Hp(14) · [1비트]Shields(10)

    ✅ 실측: 엔티티 1800의 체력이 1616→1607→1299→1199→1190→1129 로 **단조 감소**(교전),
             엔티티 1802 는 1519→200→58(사망 직전). 값이 실제 전투와 맞는다.

## ⛔ 아직 남은 것
`ISerializableProvider<T>.GetValue(IReadStream)` 구현체 **30여 종**의 비트 형식.
int 계열은 `sync_spec.json` 의 (min,max) 로 이미 알지만, float·bool·enum·Vector3·
Quaternion·구조체는 각 provider 를 디스어셈블해야 한다(→ `providers.py` 로 만들 것).
그게 끝나면 **모든 컴포넌트를 끝까지 걸어갈 수 있다.**
힌트: 좌표 범위는 `BufferProviderHelper` = X ±127 · Y ±31 · Z ±127.

## 쓰는 법
    python3 도구/replay_stream.py ../리플레이/TLJXLVAF.rsrpl.gz            # 헤더 검증
    python3 도구/replay_stream.py ../리플레이/TLJXLVAF.rsrpl.gz --types    # 타입 통계
    python3 도구/replay_stream.py ../리플레이/TLJXLVAF.rsrpl.gz --hp       # 체력 이벤트
"""
import gzip, json, base64, os, sys, struct, collections

HERE = os.path.dirname(os.path.abspath(__file__))

# ── 설정 상수 (에셋 SharedMatchSettings — match_settings.json) ────────────────
PACKAGE_BITS = 20
TIME_BITS = 24
PLAYER_BITS = 3
ENTITY_BITS = 11
COUNT_TYPES = 100          # ReadType = ReadInt(0, 100) → 7비트
TYPE_BITS = (COUNT_TYPES).bit_length()      # = 7

TYPE_END = 0
TYPE_NEW_ENTITY = 1

# 전송 타입
T_CONNECT, T_START_RESTORE, T_RESTORE, T_SEND_RESULT, T_DATA = 0, 1, 2, 3, 4


class Stream:
    """`Panzerdog.Gameplay.Realtime.Stream.BinaryStream` 정확 이식.

    Reset(0x6F1D60C) + ReadInt(0x6F1E158) 을 그대로 옮겼다. 결과적으로 LSB-first 다.
    """

    def __init__(self, data: bytes):
        n = (len(data) + 3) // 4
        buf = [0] * max(n, 1)
        for i, b in enumerate(data):
            buf[i >> 2] |= b << (8 * (i & 3))
        self.buf = buf
        self.off = 0
        self.nbits = len(data) * 8

    def read(self, bits: int) -> int:
        if bits < 1:
            return 0
        if self.off + bits > self.nbits:
            raise EOFError(f"비트 부족: {bits}개 요청, {self.nbits - self.off}개 남음")
        off = self.off
        if ((off + bits - 1) ^ off) >> 5:              # 32비트 워드 경계를 넘는다
            first = 32 - (off & 31)
            lo = self.read(first)
            hi = self.read(bits - first)
            return (hi << first) | lo
        w = self.buf[off >> 5]
        self.off = off + bits
        return ((w << ((32 - bits - off) & 31)) & 0xFFFFFFFF) >> (32 - bits)

    def read_bool(self) -> bool:
        return self.read(1) == 1

    def read_ranged(self, lo: int, hi: int) -> int:
        """ReadInt(minValue, maxValue)(0x6F1E284) — 폭 = (max-min).bit_length(), 읽고 min 을 더한다."""
        span = hi - lo
        return lo + (self.read(span.bit_length()) if span > 0 else 0)

    def read_float(self, frac_bits: int, lo: int, hi: int) -> float:
        """ReadFloat(fracBits, minValue, maxValue)(0x6F1E4D8) — 디스어셈블 그대로.

            bits = (max-min).bit_length() + fracBits
            raw  = ReadInt(bits)
            v    = raw / (2**bits - 1) * (max-min) + min      (그리고 [min,max] 로 clamp)
        """
        bits = (hi - lo).bit_length() + frac_bits
        raw = self.read(bits)
        v = raw / ((1 << bits) - 1) * (hi - lo) + lo
        return min(hi, max(lo, v))

    def read_quaternion(self, frac_bits: int = 9) -> tuple:
        """QuaternionSerializableProvider.ReadQuaternion(0x6EE9050) — smallest-three 압축.

            idx = ReadInt(0,3)                       # 2비트: 가장 큰 성분의 번호
            나머지 세 성분 = ReadFloat(9,-1,1) / √2   # 각 (1+1).bit_length()+9 = 11비트
            빠진 성분 = sqrt(1 - 나머지 제곱합)
        → 합계 **35비트**. 성분 순서는 (x, y, z, w).
        """
        import math
        idx = self.read_ranged(0, 3)
        q = [0.0, 0.0, 0.0, 0.0]
        acc = 1.0
        for i in range(4):
            if i == idx:
                continue
            v = self.read_float(frac_bits, -1, 1) / math.sqrt(2.0)
            q[i] = v
            acc -= v * v
        q[idx] = math.sqrt(max(0.0, acc))
        return tuple(q)

    @property
    def left(self) -> int:
        return self.nbits - self.off


# ── 리플레이 파일 ────────────────────────────────────────────────────────────
def load(path):
    return json.load(gzip.open(path))


def frames(rep):
    """[(전송타입, 원본바이트)] — InData 순서 그대로"""
    return [(b[0], b) for b in (base64.b64decode(f["Data"]) for f in rep["InData"]) if b]


def restore_message(rep):
    """StartRestore + Restore 조각을 합쳐 '초기 상태' 한 덩어리로. (길이 검증 포함)"""
    parts, declared = [], None
    for kind, b in frames(rep):
        if kind == T_START_RESTORE:
            declared = struct.unpack_from("<i", b, 1)[0]
            parts.append(b[5:])
        elif kind == T_RESTORE:
            parts.append(b[1:])
    out = b"".join(parts)
    return out, declared


# ── 컴포넌트 읽기 ────────────────────────────────────────────────────────────
class UnknownComponent(Exception):
    """비트 형식을 아직 모르는 컴포넌트 — 여기서 걸으면 그 프레임은 더 못 간다."""


def _unknown_value(what):
    """값 형식을 아직 모르는 필드.

    **트리거 필드는 대부분 '이번 프레임엔 없음'(게이트 0 = 1비트)** 이라,
    값 형식을 몰라도 게이트만 읽고 지나갈 수 있다. 실제로 값이 실린 프레임에서만 멈춘다.
    이 덕분에 MatchLog 하나 때문에 막히던 프레임 1만 개가 대부분 뚫린다.
    """
    def rd(s, what=what):
        raise UnknownComponent(f"값 형식 미상 {what}")
    return rd


# 값 형식을 모르는 **트리거** 필드를 "게이트만 읽고 통과"로 처리할지.
# 켜면 프레임을 더 멀리 읽지만, 그 뒤에 아직 틀린 형식이 있으면 조용히 어긋난다.
# 2026-08-07 측정: 켜면 이벤트 +50% 지만 어긋난 프레임이 208 → 2,431 로 폭증 → **기본 끔**.
TRIGGER_GATE_PASS = False

DEFAULT_INT = (-1, 1000000)     # Int32SerializableProvider 기본 생성자 → ReadInt(-1,1000000) = 20비트

# ── 구조체·열거형 provider (각 GetValue 를 디스어셈블해서 옮긴 것) ──────────────
# 열거형은 전부 `ReadInt(0, N)` 한 번. 구조체는 필드 순서대로.
# 값 자체보다 **소비하는 비트 수**가 중요하다 — 하나라도 틀리면 그 뒤가 통째로 어긋난다.
_WORLD = ((-127, 127), (-31, 31), (-127, 127))      # BufferProviderHelper 월드 경계


def _match_log(s):
    """MatchResultLog — **로그 종류에 따라 네 갈래**. 29 / 35 / 40 / 57비트.

    2026-08-07 7차에 `도구/흐름도.py` 로 `SetValue`(0x6EE79EC) 의 CFG 를 그려서 확정했다.
    공통 앞부분(29비트) 뒤에 종류별로 다르다:

        LogType(0~35) 6 · OwnerHeroNetworkId(0~2022) 11 · TargetHeroNetworkId(0~2022) 11
        · IsTargetIndependentFromHero(bool) 1                                    = 29비트
        ├ 0·1·24·27 (피해·회복·실드·비영웅피해) → Value(0~1000) 10 + bool 1        = 40비트
        ├ 4·14 (처치·연속처치)                → Int(0~32) 6                      = 35비트
        ├ 5 (결정타)                          → Int(0~2022) 11 ×2 + Int(0~32) 6  = 57비트
        └ 그 외 전부                          → **아무것도 더 안 읽는다**          = 29비트

    분기 지점: 0x6EE7BC8 `cmp w8,#14` · 0x6EE7BE4 `cmp #27 / ccmp #24` ·
              0x6EE7C00 `cmp #4` · 0x6EE7C08 `cmp #2` · 0x6EE7C54 `cmp #5 / #14` ·
              0x6EE7D00 `cmp #4`

    🪤 **세 세션이 여기서 헤맸다.** 6차는 실측으로 40, 7차 전반의 추출기는 9개 호출(63비트)을 셌다.
       둘 다 갈래 하나씩만 본 것이다. 실측으로도 못 갈렸다(0~40비트 전수 → 차이 0.03% = 노이즈).
       종류 0·1·24·27 이 전체 로그의 **97%** 라 40비트가 대부분 맞아떨어졌기 때문이다.
    🪤 **꼬리 호출(`br xN`)을 놓치지 말 것** — 마지막 필드가 `blr` 이 아니라 `br` 로 불린다.
    """
    t = s.read_ranged(0, 35)
    head = (t, s.read_ranged(0, 2022), s.read_ranged(0, 2022), int(s.read_bool()))
    if t in (0, 1, 24, 27):
        return head + (s.read_ranged(0, 1000), int(s.read_bool()))
    if t in (4, 14):
        return head + (None, None, s.read_ranged(0, 32))
    if t == 5:
        return head + (None, None, s.read_ranged(0, 2022),
                       s.read_ranged(0, 2022), s.read_ranged(0, 32))
    return head + (None, None)


def _consumable_trigger(s):
    """ConsumableTriggerData — GetValue(0x6EE4B60), **조건 분기**: 5비트 또는 50비트.

        ReadInt(0,10)=4  ·  b=ReadBool()=1
        b 가 참일 때만  위치 Vector3(fracBits 4) 12+10+12  +  **회전 Quaternion 35**   = 74비트
    0x6EE4C7C 의 `tbz w0,#0` 이 그 분기다(거짓이면 나머지를 0으로 채우고 바로 반환).
    🪤 마지막 조각은 `ReadFloat` 이 아니라 **중첩 provider 의 GetValue**(0x6EE4E80)다 —
       `x0` 가 스트림이 아니고, 반환값 `s0~s3` **네 개**를 저장한다 = 쿼터니언.
       (`IReadStream` 슬롯 0 = ReadFloat, `ISerializableProvider` 슬롯 0 = GetValue 라 헷갈린다)
    """
    kind = s.read_ranged(0, 10)
    if not s.read_bool():
        return (kind, 0, None)
    return (kind, 1, (round(s.read_float(4, -127, 127), 3), round(s.read_float(4, -31, 31), 3),
                      round(s.read_float(4, -127, 127), 3), s.read_quaternion()))


def _shot_params(s):
    """ShotParams — 마지막 필드가 **조건부**다 (위 표 주석 참고)."""
    to = _vec3(s, 7)
    surface, crit, stack = int(s.read_bool()), int(s.read_bool()), int(s.read_bool())
    return (to, surface, crit, stack, s.read_ranged(1, 4) if stack else None)


def _vec3(s, frac):
    return tuple(round(s.read_float(frac, lo, hi), 3) for lo, hi in _WORLD)


_STRUCT = {
    # ── 열거형 ────────────────────────────────────────────────
    "AbilityRestrictionSerializableProvider":  lambda s, a: s.read_ranged(0, 4),
    "AbilityStateSerializableProvider":        lambda s, a: s.read_ranged(0, 4),
    "PlayerStateSerializableProvider":         lambda s, a: s.read_ranged(0, 5),
    "EConsumableStateSerializableProvider":    lambda s, a: s.read_ranged(0, 4),
    "EConsumableBodyStateSerializableProvider": lambda s, a: s.read_ranged(0, 3),
    "EConsumableTriggerSerializableProvider":  lambda s, a: s.read_ranged(0, 10),
    # ── 구조체 ────────────────────────────────────────────────
    # AbilityStackOutputData { StacksCount, EndTime, ? } — **43비트**
    # 🪤 2026-08-07 7차 정정: 예전엔 읽기를 2개(23비트)로 적었는데 `GetValue`(0x6EE33BC)에는
    #    ReadInt 호출이 **3개**다 — ReadInt(-1,6) + ReadInt(0,900000) ×2.
    #    Data 프레임은 이 필드의 게이트가 거의 0이라 20비트 오차가 **드러나지 않았다.**
    #    초기 상태(Restore)는 모든 값을 실어 보내서 바로 걸렸다. (`초기상태.py` 참고)
    "AbilityStackOutputDataSerializableProvider":
        lambda s, a: (s.read_ranged(-1, 6), s.read_ranged(0, 900000), s.read_ranged(0, 900000)),
    # BodyStateNetworkData { CurrentState(enum), StateEndTimer(float) }
    "BodyStateNetworkDataSerializableProvider":
        lambda s, a: (s.read_ranged(0, 5), round(s.read_float(4, 0, 1000), 3)),
    # TowerLastHitData { TowerId, ? }
    "TowerLastHitDataSerializableProvider":
        lambda s, a: (s.read_ranged(0, 16000), int(s.read_bool())),
    # ExplosionOutputData { Position(Vector3), IsGrounded(bool) } — GetValue(0x6EE5CC0)
    #   Vector3SerializableProvider.GetValue + ReadBool()  → fracBits 는 인자에서 (보통 7 = 44비트)
    "ExplosionOutputDataSerializableProvider":
        lambda s, a: (_vec3(s, a.get("fracBits", 7)), int(s.read_bool())),
    # ModifierCollection { float × 7 } — 전부 ReadFloat(8,-128,128) = 17비트
    "ModifierCollectionSerializableProvider":
        lambda s, a: tuple(round(s.read_float(8, -128, 128), 3) for _ in range(7)),
    # ShotParams { To(Vector3), IsSurfaceHit, IsCrit, IsStackShot, StacksCount }
    #   GetValue(0x6EE9FE0) — **조건 분기**: 46비트, 스택샷이면 48비트
    #   Vector3(fracBits 7)=43 · bool ×2 · **IsStackShot(bool)** ·
    #   그게 참일 때만 StacksCount = ReadInt(1,4) = 2비트
    # 🪤 2026-08-07 7차 정정: 예전엔 bool 3개 뒤에 `ReadInt(0,6)`(3비트)을 **항상** 읽었다.
    #    0x6EEA274 의 `cbnz w0` 가 그 분기다. 뜻도 딱 맞는다 — 스택샷일 때만 스택 수를 보낸다.
    "ShotParamsSerializableProvider":
        lambda s, a: _shot_params(s),
    # AvailableHeroData[] { PlayerNetworkId, HeroNetworkId } × n
    "AvailableHeroDataArraySerializableProvider":
        lambda s, a: [(s.read_ranged(a.get("minValue", 0), a.get("maxValue", 1023)),
                       s.read_ranged(a.get("minValue", 0), a.get("maxValue", 1023)))
                      for _ in range(s.read(a.get("lengthBits", 2)))],
    # MatchResultLog — GetValue(0x6EE734C). **로그 종류가 4일 때만 길어진다: 40비트 / 63비트**
    #   LogType(0~35) 6 · 네트워크ID(0~2022) 11 ×2 · bool 1 · Value(0~1000) 10 · bool 1   = 40
    #   LogType == 4 이면 그 뒤로 ReadInt(0,32) 6 · ReadInt(0,2022) 11 · ReadInt(0,32) 6  = +23
    # 🪤 여기서 세 세션이 헤맸다. 6차는 실측으로 40을 찾았고(74비트로 넣었더니 어긋남 폭증),
    #    7차의 고친 추출기는 논리 호출 9개(63비트)를 셌다. **둘 다 맞았다** — 0x6EE7774 의
    #    `cmp w25,#4 / b.ne` 가 갈림길이라, 보통 프레임은 40비트이고 종류 4 에서만 63비트다.
    #    교훈: 호출 개수를 셀 때 **분기 안에 있는지**를 꼭 볼 것.
    # ⚠️ `AbilityIds`·`ImpactSources` 배열은 길이 0으로 새로 만들 뿐 스트림에서 안 읽는다
    #    (`bl 0x19478C` 앞 인자가 `mov w1,#0`).
    "MatchResultLogSerializableProvider":
        lambda s, a: _match_log(s),
    # ── 2026-08-07 7차에 새로 뚫린 트리거 구조체 2종 (마지막 미해독 컴포넌트였다) ──
    # ConsumableTriggerData — GetValue(0x6EE4B60), **조건 분기**: 5비트 또는 74비트 (_consumable_trigger)
    "ConsumableTriggerDataSerializableProvider":
        lambda s, a: _consumable_trigger(s),
    # PlayWorldEffectTriggerData — GetValue(0x6EE89FC) = **80비트**
    #   ReadInt(bits=11) 11 · 위치 Vector3(fracBits 4) 12+10+12 · **회전 Quaternion 35**
    # 🪤 2026-08-07 7차 정정: 마지막 조각을 `ReadFloat(4,0,127)`(11비트)로 잘못 읽고 있었다.
    #    0x6EE8C88 의 마지막 `blr` 은 **스트림 읽기가 아니라 중첩 provider 의 GetValue** 다
    #    (`x0` 가 스트림이 아니라 다른 객체이고, 스트림은 `x1` 로 넘어간다).
    #    반환값 `s0·s1·s2·s3` **네 개를 저장**하는 걸 보고 쿼터니언인 걸 확정했다.
    #    ⚠️ **슬롯 번호가 겹치는 함정**: `IReadStream` 슬롯 0 = ReadFloat, `ISerializableProvider`
    #       슬롯 0 = GetValue. 어느 인터페이스인지 안 보면 중첩 provider 를 실수로 float 로 읽는다.
    #    실측으로도 확인: 완주 14,296 → **14,381**. (실측만 보면 "3비트"가 봉우리로 보이는데
    #    그건 우연히 맞는 국소 최적이다 — 코드 근거가 있는 쪽이 더 높았다)
    "PlayWorldEffectTriggerDataSerializableProvider":
        lambda s, a: (s.read(11),
                      round(s.read_float(4, -127, 127), 3), round(s.read_float(4, -31, 31), 3),
                      round(s.read_float(4, -127, 127), 3), s.read_quaternion()),
    # HeroPerkData { PlayerNetworkId, Perks[] } — GetValue(0x6EE64D4)
    #   PlayerNetworkId = ReadInt(min,max) · n = ReadInt(lengthBits)
    #   그 뒤 n번 반복하는데 **항목마다 ReadInt 가 두 개**다:
    #   `HeroPerkData.Data = { AbilityNetworkId, Index }` (dump.cs 1848179행)
    # 🪤 2026-08-07 7차: 예전 세 리플레이는 전부 봇전/연습전이라 퍽이 **항상 비어 있었고**
    #    (`SelectPerk = (0, [])`) 이 코드가 **한 번도 시험되지 않았다.** 사람 경기를 받자마자
    #    바로 걸렸다 — "안 나온 컴포넌트 = 안 시험된 코드" 라는 걸 보여주는 사례.
    "HeroPerkDataSerializableProvider":
        lambda s, a: (s.read_ranged(a.get("minValue", 0), a.get("maxValue", 1023)),
                      [(s.read_ranged(a.get("minValue", 0), a.get("maxValue", 1023)),
                        s.read_ranged(a.get("minValue", 0), a.get("maxValue", 1023)))
                       for _ in range(s.read(a.get("lengthBits", 3)))]),
}


def _field_reader(iface, f, pargs, slot_type=None):
    """필드 하나를 읽는 함수. 형식을 모르면 None.

    **provider 는 `(인터페이스, 필드이름)` 으로 정해진다** — `provider_args.json`
    (`SerializableContainer..ctor` 에서 뽑은 것). 같은 이름이라도 인터페이스가 다르면
    폭이 다르다(예: `Hp` = IHp 14비트 / IShieldHp 10비트).

    provider 별 읽는 법 (전부 디스어셈블로 확인):
      · Boolean(0x6EE49E4)      = ReadBool()                          → 1비트
      · Team(0x6EEA714)         = ReadInt(0,3)                        → 2비트
      · Int32(0x6EE6F24)        = Range면 ReadInt(min,max) / BitsCount면 ReadInt(bits)
                                  / 기본생성자면 ReadInt(-1,1000000)  → 20비트
      · Single                  = ReadFloat(fracBits,min,max)
      · Vector3 / Vector2       = 축마다 ReadFloat
      · Quaternion(0x6EE9050)   = smallest-three, 35비트
    """
    p = pargs.get((iface, f["name"]))
    if p:
        cls, a = p["provider"], p["args"]
        # 🪤 `provider_args` 는 **안쪽 provider** 를 적어 놓는 경우가 있다.
        #    예: `Explosion.ExplosionOutputData` 가 `Vector3SerializableProvider` 로 잡힌다.
        #    실제로는 `ExplosionOutputData.GetValue`(0x6EE5CC0) = Vector3 + **ReadBool()** 이라
        #    그대로 믿으면 **1비트를 덜 읽는다.** `read_order` 의 슬롯 타입이 진짜 이름이므로
        #    그쪽에 맞는 구조체 리더가 있으면 **그게 우선**이다. (2026-08-07 7차)
        if slot_type and f"{slot_type}SerializableProvider" in _STRUCT:
            cls = f"{slot_type}SerializableProvider"
        if cls == "BooleanSerializableProvider":
            return lambda s: int(s.read_bool())
        if cls == "TeamSerializableProvider":
            return lambda s: s.read_ranged(0, 3)
        if cls == "QuaternionSerializableProvider":
            return lambda s: s.read_quaternion()
        if cls == "Int32SerializableProvider":
            # 🪤 **선언값(`[CustomSerialize(min,max)]`)이 기계어 추출보다 우선**이다.
            #    `provider_args.py` 는 생성자 기계어에서 뽑는 거라 드물게 틀린다 — 46개 중 1개
            #    (`IPowerUpOutputComponent.PowerUpState`: 선언 (-1,4)=3비트인데 추출은
            #     (-65532,65535)=17비트였다. 2026-08-07 사람 경기 리플레이에서 걸렸다).
            #    선언은 `sync_spec.json`(dump.cs 의 속성)에서 온다.
            if f.get("read") == "ReadInt" and "min" in f and "max" in f:
                lo, hi = f["min"], f["max"]
            elif "minValue" in a and "maxValue" in a:
                lo, hi = a["minValue"], a["maxValue"]
            elif a.get("bitsCount"):
                n = a["bitsCount"]
                return lambda s: s.read(n)
            else:
                lo, hi = DEFAULT_INT
            return lambda s: s.read_ranged(lo, hi)
        if cls == "SingleSerializableProvider" and "maxValue" in a:
            fb, lo, hi = a["fracBits"], a["minValue"], a["maxValue"]
            return lambda s: round(s.read_float(fb, lo, hi), 3)
        if cls == "Vector3SerializableProvider" and "zMax" in a:
            fb = a["fracBits"]
            ax = [(a["xMin"], a["xMax"]), (a["yMin"], a["yMax"]), (a["zMin"], a["zMax"])]
            return lambda s: tuple(round(s.read_float(fb, lo, hi), 3) for lo, hi in ax)
        if cls == "TimerDataSerializableProvider" and "maxDuration" in a:
            # TimerDataSerializableProvider.GetValue(0x6EEA888) — **조건 분기가 있다**
            #   ReadFloat(fracBits=4, 0, _maxDuration)
            #   b = ReadBool()
            #   b 면 ReadInt(-1, 900000)  (20비트)   아니면 ReadBool()  (1비트)
            # 🪤 2026-08-07 7차 정정: 예전엔 세 번째 읽기가 통째로 빠져 있었다.
            #    0x6EEA9A8 의 `cbz w0, 0x6EEA9E0` 이 그 분기다(참=긴 쪽, 거짓=1비트).
            md = a["maxDuration"]
            def _timer(s, md=md):
                t = round(s.read_float(4, 0, md), 3)
                b = s.read_bool()
                return (t, int(b), s.read_ranged(-1, 900000) if b else int(s.read_bool()))
            return _timer
        if cls == "Int32ArraySerializableProvider" and "lengthBits" in a:
            # Int32ArraySerializableProvider.GetValue(0x6EE6AD0)
            #   n = ReadInt(_lengthBits);  n번 ReadInt(_minValue, _maxValue)
            lb, lo, hi = a["lengthBits"], a.get("minValue", 0), a.get("maxValue", 0)
            def _arr(s, lb=lb, lo=lo, hi=hi):
                n = s.read(lb)
                return [s.read_ranged(lo, hi) for _ in range(n)]
            return _arr
        st = _STRUCT.get(cls)
        if st:
            return lambda s, st=st, a=a: st(s, a)
        if cls == "Vector2SerializableProvider" and "yMax" in a:
            fb = a["fracBits"]
            ax = [(a["xMin"], a["xMax"]), (a["yMin"], a["yMax"])]
            return lambda s: tuple(round(s.read_float(fb, lo, hi), 3) for lo, hi in ax)
        return None
    # provider 등록이 없으면 덤프의 [CustomSerialize] 로 폴백
    if f.get("read") == "ReadInt" and "min" in f:
        lo, hi = f["min"], f["max"]
        return lambda s: s.read_ranged(lo, hi)
    if f.get("type") == "bool":
        return lambda s: int(s.read_bool())
    return None


MAP_COUNTS = {}                      # (컴포넌트, 필드) → 항목 수. `map_counts.py` 가 실측으로 채운다
try:
    MAP_COUNTS = {tuple(k.split("/")): v for k, v in json.load(
        open(os.path.join(HERE, "map_counts.json"), encoding="utf-8")).items()}
except FileNotFoundError:
    pass

TRIGGER_INDEX_BITS = (-1, 4096)      # ReadContainer.GetTriggerIndex → 13비트
OFFSET_TIME_BITS = (-1000, 0)        # ReadContainer.ReadOffsetTime  → 10비트


# ── provider_args.json 을 손으로 덮어쓸 곳 (지금은 비어 있다) ────────────────
# 🪤 2026-08-07 7차: 한때 7건이 `maxValue: 0`(=추출 실패) 이라 여기에 손으로 넣었었다.
#    원인은 `provider_args.py` 가 **함수 호출을 지날 때 레지스터를 전부 버린** 것이었다.
#    ARM64 는 **x19~x28 이 호출을 넘어 살아남는다**(callee-saved). 그걸 고치니 7건 전부
#    코드에서 제대로 나왔고, 실측으로 찾았던 값(TargetRadius 15 · ScaleTimerData 63)과
#    **정확히 일치**했다. → 손으로 넣을 게 없어져 비웠다.
#    ⚠️ 여기에 값을 넣게 되면 그건 "아직 코드에서 못 뽑았다"는 신호다. 추출기를 고치는 게 먼저.
ARG_FIX = {}


def load_spec():
    """`read_order.json`(읽는 순서·종류) + `provider_args.json`(비트 폭) → 컴포넌트 리더."""
    wt = json.load(open(os.path.join(HERE, "wire_types.json"), encoding="utf-8"))
    spec = json.load(open(os.path.join(HERE, "sync_spec.json"), encoding="utf-8"))
    order = json.load(open(os.path.join(HERE, "read_order.json"), encoding="utf-8"))
    pa = {}
    for r in json.load(open(os.path.join(HERE, "provider_args.json"), encoding="utf-8")):
        pa.setdefault((r.get("group"), r["key"]), r)
    for k, fix in ARG_FIX.items():
        if k in pa:
            pa[k] = dict(pa[k], args=dict(pa[k]["args"], **fix))

    t2c = {int(k): v for k, v in wt["type_to_component"].items()}
    readers = {}
    for t, comp in t2c.items():
        iface = f"I{comp}OutputComponent"
        prog = order.get(comp, {})
        slots = prog.get("slots")
        if slots is None:
            continue
        decl = {f["name"]: f for f in (spec.get(iface) or {}).get("fields", [])}
        built, ok = [], True
        for s in slots:
            name = s.get("field")
            if not name:
                ok = False
                break
            fn = _field_reader(iface, decl.get(name, {"name": name}), pa, s.get("type"))
            if fn is None:
                if not (TRIGGER_GATE_PASS and s["kind"] == "trigger"):
                    ok = False        # 형식을 모르면 지나갈 방법이 없다
                    break
                fn = _unknown_value(f"{comp}.{name}")   # 트리거는 게이트가 0이면 통과
            built.append((name, s["kind"], fn, s.get("reader")))
        if ok:
            readers[t] = built      # 빈 목록이면 0비트 (Animator.Read 는 그냥 false 를 돌려준다)
    return t2c, readers


T2C, INT_READERS = load_spec()


ENTITY_MAP_COUNTS = {}   # (엔티티, 컴포넌트, 필드) → 항목 수. 지도 항목수는 **엔티티마다 다르다**


def use_entity_counts(replay_name):
    """그 경기의 **엔티티별 `AnimationParameters` 지도 항목 수**를 켠다.

    항목 수 = 그 캐릭터(영웅) 애니메이션 컨트롤러의 파라미터 개수다
    (`애니파라미터.py` 가 에셋에서 뽑고, `초기상태.py --저장` 이 엔티티별로 맞춰 둔다).

    🚩 **옛 `entity_map_counts.json` 은 쓰지 말 것** — 비트 폭이 틀렸던 시절에 실측으로
       맞춘 값이라 지금 기준으론 오답이다. `entity_map_counts_폐기_2026-08-07.json` 으로
       이름만 바꿔 남겨 뒀다. 폭을 다 고친 뒤 이 파일을 계속 쓰니 51%에서 멈췄었다.
    """
    ENTITY_MAP_COUNTS.clear()
    try:
        data = json.load(open(os.path.join(HERE, "anim_sets.json"), encoding="utf-8"))
    except FileNotFoundError:
        return 0
    got = data.get(replay_name)
    if not got:
        return 0
    n = 0
    for ent, counts in got.get("항목수", {}).items():
        for field, cnt in counts.items():
            ENTITY_MAP_COUNTS[(int(ent), "AnimationParameters", field)] = cnt
            n += 1
    return n


def read_component(s: Stream, t: int, ent=None):
    """타입 t 의 컴포넌트를 읽는다. 형식을 모르면 UnknownComponent.

    슬롯 종류에 따라 읽는 법이 다르다(`read_order.py` 문서 참고):
      · 값     : `if (ReadCheck()) 값`
      · 트리거 : `while (ReadCheck()) { 인덱스13 + 오프셋시각10 + 값 }`
    필드 순서는 **선언 순서가 아니라 리더 코드 순서**다(read_order.json).
    """
    fields = INT_READERS.get(t)
    if fields is None:
        raise UnknownComponent(f"type {t} ({T2C.get(t,'?')})")
    comp = T2C.get(t, "?")
    out = {}
    for name, kind, fn, mreader in fields:
        if kind == "trigger":
            hits = []
            while s.read_bool():
                s.read_ranged(*TRIGGER_INDEX_BITS)
                s.read_ranged(*OFFSET_TIME_BITS)
                hits.append(fn(s))
            if hits:
                out[name] = hits
        elif kind == "map":
            # `if (!ReadCheck()) return;  for i in 0.._count: if (ReadCheck()) {항목}`
            # 게이트가 0이면 1비트로 끝난다 — _count 를 몰라도 지나갈 수 있다.
            if not s.read_bool():
                continue
            key = (ent, comp, name)
            n = ENTITY_MAP_COUNTS[key] if key in ENTITY_MAP_COUNTS else MAP_COUNTS.get((comp, name))
            if n is None:
                raise UnknownComponent(f"map {comp}.{name} 항목수 미상")
            got = {}
            for i in range(n):
                if not s.read_bool():
                    continue
                if mreader == "TriggerSyncMapReader":
                    # 항목마다 트리거가 여러 개 올 수 있다 (ReadCheck 3개짜리 구조)
                    hits = []
                    while True:
                        s.read_ranged(*TRIGGER_INDEX_BITS)
                        s.read_ranged(*OFFSET_TIME_BITS)
                        hits.append(fn(s))
                        if not s.read_bool():
                            break
                    got[i] = hits
                else:
                    got[i] = fn(s)
            if got:
                out[name] = got
        elif s.read_bool():
            out[name] = fn(s)
    return out


def parse_frame(data: bytes, walk=True):
    """한 전송 프레임 → {tick, time, package, events:[…], stopped:이유}"""
    s = Stream(data)
    r = {"kind": s.read(8), "tick": s.read(PACKAGE_BITS), "time": s.read(TIME_BITS),
         "package": s.read(PACKAGE_BITS), "events": [], "stopped": None}
    if not walk:
        return r
    ent = None
    try:
        while True:
            pt = s.read(2)
            if pt == 1:                                    # 출력 본문
                while True:
                    t = s.read(TYPE_BITS)
                    if t == TYPE_END:
                        break
                    if t == TYPE_NEW_ENTITY:
                        ent = s.read(ENTITY_BITS)
                        s.read_bool()                      # ClearCacheTimeBit
                        continue
                    r["events"].append((ent, t, T2C.get(t, "?"), read_component(s, t, ent)))
            elif pt == 2:                                  # 수신확인
                s.read(PACKAGE_BITS)
            elif pt == 3:                                  # 끝
                r["stopped"] = "end"
                break
            else:
                r["stopped"] = f"pt={pt}"
                break
    except UnknownComponent as e:
        r["stopped"] = f"미지 컴포넌트 {e}"
        r["at_entity"] = ent
    except EOFError:
        r["stopped"] = "EOF"
    r["left"] = s.left
    return r


# ── CLI ─────────────────────────────────────────────────────────────────────
def cmd_header(rep, name):
    fs = frames(rep)
    D = [b for k, b in fs if k == T_DATA]
    rows = [parse_frame(b, walk=False) for b in D]
    kinds = collections.Counter(r["kind"] for r in rows)
    dt = collections.Counter(y["time"] - x["time"] for x, y in zip(rows, rows[1:]))
    dk = collections.Counter(y["tick"] - x["tick"] for x, y in zip(rows, rows[1:]))
    first_pt = collections.Counter()
    for b in D:
        s = Stream(b)
        s.read(8); s.read(PACKAGE_BITS); s.read(TIME_BITS); s.read(PACKAGE_BITS)
        first_pt[s.read(2)] += 1
    msg, declared = restore_message(rep)
    print(f"■ {name}   Data 프레임 {len(D)}개 · ConnectTime {rep['ConnectTime']:.3f}s")
    print(f"  ReadInt(8) 값       : {dict(kinds)}   (전송타입 4 와 일치해야 함)")
    print(f"  시각 Δ 상위          : {dt.most_common(3)}   ← +20ms 여야 정상")
    print(f"  틱  Δ 상위          : {dk.most_common(3)}")
    print(f"  첫 ReadPacketType   : {dict(first_pt)}   ← 1 이어야 본문이 이어짐")
    print(f"  시각 첫값            : {rows[0]['time']}ms  (ConnectTime {rep['ConnectTime']*1000:.0f}ms)")
    print(f"  초기상태 재조립      : 길이필드 {declared} / 실제 {len(msg)}  → "
          f"{'일치 ✅' if declared == len(msg) else '불일치 ❌'}")


def cmd_types(rep, name):
    fs = [b for k, b in frames(rep) if k == T_DATA]
    first, second, ents = collections.Counter(), collections.Counter(), collections.Counter()
    for b in fs:
        s = Stream(b)
        s.read(8); s.read(PACKAGE_BITS); s.read(TIME_BITS); s.read(PACKAGE_BITS); s.read(2)
        t = s.read(TYPE_BITS); first[t] += 1
        if t != TYPE_NEW_ENTITY:
            continue
        ents[s.read(ENTITY_BITS)] += 1
        s.read_bool()
        second[s.read(TYPE_BITS)] += 1
    print(f"■ {name}")
    print(f"  본문 첫 타입 : {[(t, T2C.get(t, '끝' if t == 0 else '새엔티티'), n) for t, n in first.most_common(5)]}")
    print(f"  엔티티 번호  : 고유 {len(ents)}개, 상위 {ents.most_common(8)}")
    print(f"  둘째 타입    : {[(t, T2C.get(t, '?'), n) for t, n in second.most_common(10)]}")


def cmd_hp(rep, name):
    """새 엔티티 바로 뒤에 Hp(type 3)가 오는 경우만 뽑는다 (그 뒤는 미지 컴포넌트라 못 감)."""
    fs = [b for k, b in frames(rep) if k == T_DATA]
    print(f"■ {name} — 체력 이벤트")
    print("   틱      시각ms  엔티티  MaxHp    Hp  Shields")
    n = 0
    for b in fs:
        s = Stream(b)
        s.read(8); tick = s.read(PACKAGE_BITS); tm = s.read(TIME_BITS)
        s.read(PACKAGE_BITS); s.read(2)
        if s.read(TYPE_BITS) != TYPE_NEW_ENTITY:
            continue
        e = s.read(ENTITY_BITS); s.read_bool()
        if s.read(TYPE_BITS) != 3:
            continue
        v = read_component(s, 3)
        print(f"  {tick:6d} {tm:8d} {e:7d} "
              f"{v.get('MaxHp', '·'):>6} {v.get('Hp', '·'):>6} {v.get('Shields', '·'):>7}")
        n += 1
    print(f"  {n}건")


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    opts = {a for a in sys.argv[1:] if a.startswith("--")}
    paths = args or [os.path.join(os.path.dirname(HERE), "replays", f)
                     for f in ("TLJXLVAF.rsrpl.gz", "R4A7SZ4A.rsrpl.gz", "Q6TSVQ8B.rsrpl.gz")]
    for p in paths:
        rep = load(p)
        name = os.path.basename(p).split(".")[0]
        if "--types" in opts:
            cmd_types(rep, name)
        elif "--hp" in opts:
            cmd_hp(rep, name)
        else:
            cmd_header(rep, name)
        print()


if __name__ == "__main__":
    main()
