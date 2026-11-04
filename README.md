# 아꼈슈 생활정보 블로그 자동화

## 처리 흐름
1. 날짜와 카테고리 비중에 따라 주제 후보 선택
2. Tavily로 최신 검색 결과 수집
3. Groq가 검색 결과를 바탕으로 주제와 조사 요약 작성
4. Groq가 글 초안 작성
5. 출처 수·도메인·핵심 사실·글 구조/분량 품질 검사
6. 검사 통과 시 HTML/JSON 미리보기 생성
7. `DRY_RUN=false`일 때 Blogger 공개 발행

## GitHub Secrets / Variables
필수 Secrets:
- `GROQ_API_KEY`
- `TAVILY_API_KEY`
- `BLOG_URL`
- `BLOGGER_TOKEN_JSON`

선택 Variable: `BLOG_NAME`
모델은 workflow의 `GROQ_MODEL`에서 지정합니다. 현재 기본값은 `openai/gpt-oss-120b`입니다.

## 첫 실행
1. 저장소 루트의 `main.py`, `requirements.txt`와 `.github/workflows/life-info.yml`을 반영합니다.
2. 위 Secrets를 등록합니다.
3. Actions에서 `Life Info Blogger Automation`을 실행합니다.
4. `task=auto`, `dry_run=true`로 먼저 실행합니다.
5. Artifacts의 HTML/JSON 미리보기를 확인합니다.

## 안전장치
- 기본 수동 실행과 예약 실행은 미리보기 전용(`DRY_RUN=true`)입니다.
- 검색 출처나 사실 근거가 부족하거나 품질 검사에 실패하면 발행하지 않습니다.
- API 키는 코드에 직접 입력하지 말고 GitHub Secrets에 저장합니다.
