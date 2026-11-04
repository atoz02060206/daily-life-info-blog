# 생활정보 Blogger 자동화 v1.0

## 기능 흐름
1. 날짜와 콘텐츠 비중에 따라 카테고리 선택
2. Gemini Google Search grounding으로 주제 발굴
3. 별도 검색 단계에서 사실·조건·주의사항 조사
4. 조사 결과를 근거로 글 생성
5. 출처 2개 이상, 서로 다른 도메인, 핵심 사실, 글 구조/분량 등 품질 검수
6. 검수 통과 시 HTML/JSON 미리보기 생성
7. DRY_RUN=false일 때만 Blogger 공개 발행

## GitHub 저장소 Secrets / Variables
- `GEMINI_API_KEY` (필수 Secret)
- `BLOGGER_TOKEN_JSON` (Blogger OAuth JSON 전체를 저장하는 Secret)
- `BLOG_URL` (대상 블로그 URL, Secret)
- `BLOG_NAME` (선택 Variable)

`GEMINI_MODEL`은 workflow 파일에서 지정합니다.

## 첫 실행
1. 새 GitHub 저장소에 이 폴더의 파일을 업로드합니다.
2. 위 Secrets/Variables를 등록합니다.
3. Actions에서 `Life Info Blogger Automation`을 선택합니다.
4. `task=auto`, `dry_run=true`로 실행합니다.
5. 실행 결과의 Artifacts에서 HTML/JSON 미리보기를 내려받아 확인합니다.

## 안전장치
- 기본 실행은 미리보기 전용입니다.
- 검색 grounding 출처가 부족하거나 검수에 실패하면 발행을 중단합니다.
- 예약 실행도 현재 `DRY_RUN=true`로 설정되어 있습니다. 글 품질과 블로그 설정을 확인한 뒤 workflow의 예약 실행 값을 바꿔야 자동 공개 발행됩니다.
- Gemini 검색 grounding 결과가 매번 충분한 출처를 제공하는 것은 아니므로, 실패하면 억지로 글을 만들지 않고 중단하도록 설계했습니다.

## 주의
첫 버전은 초안 자동 생성과 미리보기 검증에 초점을 둡니다. 실제 공개 발행을 켜기 전에 Blogger 대상 URL, OAuth 권한, 라벨, 글 레이아웃을 테스트하세요.
