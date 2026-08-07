#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""서버 폴더에 필요한 도구·데이터를 `../도구` 에서 복사해 온다.

왜: 서버는 GitHub 에 따로 올라가는 **자립형 폴더**라, 분석 도구가 바뀌면
이걸 한 번 돌려서 최신본을 담아 줘야 한다. (도구 원본은 절대 여기서 고치지 말 것 —
원본은 리플레이분석/도구/ 가 진실이고, 여기 것은 사본이다.)

    python3 동기화.py        # 복사 후 바뀐 파일 목록 출력
"""
import os, shutil, json

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "도구")
DST = os.path.join(HERE, "도구")

# 서버 런타임에 실제로 쓰이는 것만 (추출용 도구·덤프 의존 도구는 제외)
FILES = [
    "replay_stream.py", "초기상태.py", "경기분석.py",
    "wire_types.json", "sync_spec.json", "read_order.json", "provider_args.json",
    "map_counts.json", "애니파라미터.json", "영웅표.json", "영웅체력.json", "refdata.json",
]

os.makedirs(DST, exist_ok=True)
os.makedirs(os.path.join(HERE, "리플레이"), exist_ok=True)
changed = []
for fn in FILES:
    s, d = os.path.join(SRC, fn), os.path.join(DST, fn)
    if not os.path.exists(s):
        raise SystemExit(f"❌ 원본에 없음: {s}")
    if (not os.path.exists(d)) or open(s, "rb").read() != open(d, "rb").read():
        shutil.copy2(s, d)
        changed.append(fn)

# 경기별 애니 세트는 서버가 요청마다 새로 푼다 — 빈 파일로 시작
anim = os.path.join(DST, "애니세트_경기별.json")
if not os.path.exists(anim):
    json.dump({}, open(anim, "w"))
    changed.append("애니세트_경기별.json (빈 파일 생성)")

keep = os.path.join(HERE, "리플레이", ".gitkeep")
open(keep, "a").close()

print(f"복사 대상 {len(FILES)}개 중 바뀐 것 {len(changed)}개:")
for fn in changed:
    print("  ·", fn)
if not changed:
    print("  (전부 최신)")
