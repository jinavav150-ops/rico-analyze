# 리코셰 경기 분석 서버

매치 코드를 받아 리플레이를 S3에서 내려받고, 리포트 JSON을 돌려주는 작은 서버.
사이트(ricosquad.netlify.app)의 **"경기 분석"** 탭이 이 서버를 부른다.

- `GET /analyze/<매치코드>` → 리포트 JSON (오류도 JSON: `{"error":..., "message":{ko,en}}`)
- `GET /health` → 살아 있나 확인

## 배포 (Render 무료 플랜)
1. 이 폴더를 GitHub 저장소로 올린다 (`서버/` 내용물이 저장소 **루트**여야 함)
2. render.com → New → **Blueprint** → 그 저장소 선택 → `render.yaml`이 알아서 설정
3. 나온 주소(`https://rico-match-analyze.onrender.com` 비슷한 것)를
   Firebase 콘솔 → Realtime Database → `config/analyzeApi` 에 문자열로 넣는다
   (사이트가 이 값을 읽어 서버를 찾는다 — 사이트 재배포 없이 주소 교체 가능)

## 주의
- **도구는 여기서 고치지 말 것.** 원본은 `../도구/` 가 진실이고, 여기 것은 `python3 동기화.py` 로 만든 사본이다.
- 게임이 패치되면 새 리플레이가 안 읽힐 수 있다 → 서버는 완주율 85% 미만이면 리포트를 내놓지 않고
  "게임 업데이트 반영 전" 오류를 준다. 분석기(도구)를 새 버전에 맞춘 뒤 `동기화.py` → git push 하면 끝.
- 무료 플랜은 15분 놀면 잠든다 → 첫 요청이 1분쯤 걸릴 수 있다(사이트가 안내 문구를 띄움).
