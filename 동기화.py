#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""서버 폴더에 필요한 도구·데이터를 `../도구` 에서 복사해 온다. (+ 한글 이름 → 영문 변환)

## 🪤 왜 이름을 바꾸는가 — 맥에서 만든 한글 파일명은 리눅스에서 안 열린다
맥(APFS)은 한글 파일명을 **자모 분해형(NFD)** 으로 저장하고, 찾을 땐 알아서 합쳐서 비교한다.
리눅스(Render)는 **바이트 그대로** 비교한다 → 코드가 찾는 `초기상태`(NFC)와
디스크의 `초기상태`(NFD)가 **서로 다른 이름**이 되어 `ModuleNotFoundError` 로 서버가 죽는다.
GitHub 업로드는 NFD를 그대로 보존하므로 이 사고는 100% 재현된다(2026-08-08 실제로 겪음).
→ **서버 사본은 전부 ASCII 이름**으로 만들고, 코드 안의 상호 참조도 같이 바꾼다.
   (원본 `../도구/` 는 한글 그대로 둔다 — MAMMON이 평소 쓰는 이름이라 안 건드림)

## 쓰는 법
    python3 동기화.py        # 복사·변환 후 바뀐 파일 목록 출력
도구를 고쳤으면 이걸 돌리고 GitHub에 다시 올리면 된다.
⚠️ 이 파일(동기화.py) 자체는 **올리지 않는다** — 맥에서만 쓰는 개발 도구다.
"""
import os, re, shutil, json, unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "도구")
DST = os.path.join(HERE, "tools")

# 원본 이름 → 서버에서 쓸 ASCII 이름
RENAME = {
    "초기상태.py": "restore_state.py",
    "경기분석.py": "match_report.py",
    "애니파라미터.json": "anim_params.json",
    "애니세트_경기별.json": "anim_sets.json",
    "영웅표.json": "hero_names.json",
    "영웅체력.json": "hero_hp.json",
    # 2026-08-09 12차 — 코칭용 데이터 3종 (도구/*생성.py 로 만든다)
    "스킬표.json": "skill_names.json",     # 스킬 엔티티 오프셋 → 공식 스킬명(9개 언어)
    "퍽표.json": "perk_names.json",        # 퍽 id → 공식 효과 설명
    "사거리표.json": "hero_range.json",    # 영웅별 평소 교전 거리 기준선
}
# 이름이 이미 ASCII라 그대로 쓰는 것
KEEP = [
    "replay_stream.py",
    "wire_types.json", "sync_spec.json", "read_order.json", "provider_args.json",
    "map_counts.json", "refdata.json",
]
# 코드 안에서 서로를 부르는 이름 (문자열·import 문만 정확히 골라 바꾼다 — 주석은 건드리지 않음)
PATCH = [
    ('import 초기상태 as S', 'import restore_state as S'),
    ('import_module("초기상태")', 'import_module("restore_state")'),
    ('import_module("경기분석")', 'import_module("match_report")'),
    ('"애니세트_경기별.json"', '"anim_sets.json"'),
    ('"애니파라미터.json"', '"anim_params.json"'),
    ('"영웅표.json"', '"hero_names.json"'),
    ('"영웅체력.json"', '"hero_hp.json"'),
    ('"스킬표.json"', '"skill_names.json"'),
    ('"퍽표.json"', '"perk_names.json"'),
    ('"사거리표.json"', '"hero_range.json"'),
    ('"리플레이"', '"replays"'),
    ('os.path.join(BASE, "도구")', 'os.path.join(BASE, "tools")'),
    ('os.path.join(BASE, "도구", "refdata.json")', 'os.path.join(BASE, "tools", "refdata.json")'),
]


def patch(text):
    for a, b in PATCH:
        text = text.replace(a, b)
    return text


def main():
    os.makedirs(DST, exist_ok=True)
    os.makedirs(os.path.join(HERE, "replays"), exist_ok=True)

    changed, wrote = [], set()
    for src_name in KEEP + list(RENAME):
        dst_name = RENAME.get(src_name, src_name)
        s = os.path.join(SRC, src_name)
        d = os.path.join(DST, dst_name)
        if not os.path.exists(s):
            raise SystemExit(f"❌ 원본에 없음: {s}")
        data = open(s, "rb").read()
        if src_name.endswith(".py"):
            data = patch(data.decode("utf-8")).encode("utf-8")
        old = open(d, "rb").read() if os.path.exists(d) else None
        if old != data:
            open(d, "wb").write(data)
            changed.append(dst_name)
        wrote.add(dst_name)

    # 경기별 애니 세트는 서버가 요청마다 새로 푼다 — 빈 파일로 시작
    anim = os.path.join(DST, "anim_sets.json")
    if not os.path.exists(anim):
        json.dump({}, open(anim, "w"))
        changed.append("anim_sets.json (빈 파일 생성)")
    wrote.add("anim_sets.json")

    # 옛 한글 이름 사본이 남아 있으면 지운다 (섞여 있으면 어느 게 진짜인지 헷갈린다)
    stale = [f for f in os.listdir(DST) if f not in wrote]
    for f in stale:
        q = os.path.join(DST, f)
        shutil.rmtree(q) if os.path.isdir(q) else os.remove(q)   # __pycache__ 는 폴더다
    old_dir = os.path.join(HERE, "도구")
    if os.path.isdir(old_dir):
        shutil.rmtree(old_dir)
        stale.append("도구/ (옛 폴더 통째)")
    old_rep = os.path.join(HERE, "리플레이")
    if os.path.isdir(old_rep):
        shutil.rmtree(old_rep)
        stale.append("리플레이/ (옛 폴더 통째)")

    open(os.path.join(HERE, "replays", ".gitkeep"), "a").close()

    # ✅ 자체 검사: 올라갈 파일 이름에 ASCII 아닌 글자가 하나라도 있으면 실패로 본다
    bad = []
    for root, dirs, files in os.walk(HERE):
        dirs[:] = [x for x in dirs if x not in ("__pycache__", ".git")]
        for f in dirs + files:
            if any(ord(c) > 127 for c in f):
                bad.append(os.path.relpath(os.path.join(root, f), HERE))
    bad = [b for b in bad if not b.endswith("동기화.py")]   # 이건 안 올린다

    print(f"복사·변환 {len(wrote)}개 · 바뀐 것 {len(changed)}개")
    for f in changed:
        print("  ·", f)
    if stale:
        print("정리한 옛 파일:", ", ".join(stale))
    if bad:
        print("❌ 한글 이름이 남아 있다(리눅스에서 못 읽음):", bad)
        raise SystemExit(1)
    print("✅ 올라갈 파일 이름 전부 ASCII — 리눅스에서 안전")


if __name__ == "__main__":
    main()
