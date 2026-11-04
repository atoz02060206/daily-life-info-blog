# 아꼈슈 자동발행 최종본

## 예약 발행
- 매일 오전 9시 30분(KST) GitHub Actions 자동 실행
- 예약 실행은 `DRY_RUN=false`로 실제 Blogger 공개 발행
- 수동 실행은 기본 `dry_run=true`로 미리보기만 생성
- 검색·사실·출처·글 구조 품질검사를 통과하지 못하면 발행하지 않음

## 주제 중복 방지
- 날짜 기반 카테고리 순환 및 주제 후보(seed) 순환
- 실제 발행 시 Blogger 최근 글 제목 최대 60개를 읽어 유사 주제를 피하도록 주제 선정에 반영
- 기존 제목/서명 중복 방지 유지
- 완전히 다른 주제를 보장하는 것은 아니며, 최종 주제는 검색 결과와 당일 이슈에 따라 달라짐

## 디자인
- 모바일 가독성 중심의 짧은 문단
- 그린 포인트, 도입부 요약 카드, 핵심 사실 카드, 체크리스트, 핵심 정리, 출처 링크

## GitHub 설정
필수 Secrets: `BLOG_URL`, `GROQ_API_KEY`, `TAVILY_API_KEY`, `BLOGGER_TOKEN_JSON`
선택 Variable: `BLOG_NAME`

## 적용
1. 저장소 루트 `main.py` 교체
2. `.github/workflows/life-info.yml` 교체
3. `requirements.txt` 교체
4. 수동 실행 `task=auto`, `dry_run=true`로 미리보기 확인
5. 확인 후 예약 실행은 매일 자동 발행

주의: 예약 발행은 품질검사를 통과한 경우 실제 공개됩니다. 첫 예약 실행 전에 Blogger URL과 OAuth 권한을 확인하세요. API 무료 한도 초과, 네트워크 오류, 검색 근거 부족, 품질검사 실패 시에는 발행을 건너뜁니다.
