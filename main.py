#!/usr/bin/env python3
"""생활정보 Blogger 자동화 v1.0
- Gemini Google Search grounding으로 주제 조사
- 조사 근거를 바탕으로 한국어 글 생성
- 출처/구조/중복/품질 검수 후 미리보기 또는 Blogger 발행
- 기본값은 DRY_RUN=true (미리보기 전용)
"""
from __future__ import annotations

import os
import re
import json
import html
import hashlib
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests

KST = timezone(timedelta(hours=9))
TODAY = datetime.now(KST).strftime('%Y-%m-%d')
BLOG_URL = os.getenv('BLOG_URL', '').strip().rstrip('/') + '/'
BLOG_NAME = os.getenv('BLOG_NAME', '생활정보 노트').strip()
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY', '').strip()
GEMINI_MODEL = os.getenv('GEMINI_MODEL', 'gemini-3.8-flash').strip()
DRY_RUN = os.getenv('DRY_RUN', 'true').lower() not in ('0', 'false', 'no')
TASK = os.getenv('TASK', 'auto').strip().lower()
OUT_DIR = Path('.')

# 기존 기획의 콘텐츠 비중을 반영한 주제 영역
CATEGORIES = {
    '생활비·절약': {
        'weight': 35,
        'seeds': ['전기요금과 가스요금 절약', '통신비·구독료 줄이기', '생활용품 구매 비용 절약', '세탁·청소·관리 비용 줄이기'],
    },
    '금융·돈 관리': {
        'weight': 30,
        'seeds': ['예적금과 금융상품 이해', '신용점수와 금융생활', '카드 혜택과 수수료', '가계부와 고정지출 관리'],
    },
    '여행·소비경제': {
        'weight': 20,
        'seeds': ['국내여행 교통·숙박비 절약', '항공권·숙소 예약 시 확인사항', '온라인 쇼핑과 환불·교환', '소비자 권리와 피해 예방'],
    },
    '물가·경제 흐름': {
        'weight': 15,
        'seeds': ['생활물가 변화와 가계 영향', '공공요금 변경', '소비자 가격 비교', '생활경제 제도 변경'],
    },
}

REQUEST_TIMEOUT = (15, 120)
TRANSIENT = {408, 429, 500, 502, 503, 504}


def need(name: str) -> str:
    value = os.getenv(name, '').strip()
    if not value:
        raise RuntimeError(f'필수 환경변수가 없습니다: {name}')
    return value


def post_json_retry(url: str, *, headers: dict, payload: dict, attempts: int = 4) -> dict:
    last_error = None
    for attempt in range(attempts):
        try:
            response = requests.post(url, headers=headers, json=payload, timeout=REQUEST_TIMEOUT)
            if response.status_code in TRANSIENT and attempt < attempts - 1:
                wait = [5, 15, 30][min(attempt, 2)]
                print(f'[일시 오류 재시도] HTTP {response.status_code}, {wait}초 대기')
                time.sleep(wait)
                continue
            if not response.ok:
                raise RuntimeError(f'Gemini API 오류 {response.status_code}: {response.text[:700]}')
            return response.json()
        except requests.RequestException as exc:
            last_error = exc
            if attempt < attempts - 1:
                wait = [5, 15, 30][min(attempt, 2)]
                print(f'[네트워크 재시도] {exc}, {wait}초 대기')
                time.sleep(wait)
    raise RuntimeError(f'Gemini 요청이 {attempts}회 실패했습니다: {last_error}')


def gemini_url() -> str:
    return f'https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent'


def extract_text(data: dict) -> str:
    try:
        parts = data['candidates'][0]['content']['parts']
        return '\n'.join(str(p.get('text', '')) for p in parts if p.get('text'))
    except (KeyError, IndexError, TypeError):
        raise RuntimeError('Gemini 응답에서 본문 텍스트를 찾지 못했습니다.')


def extract_json(text: str) -> dict:
    cleaned = text.strip()
    cleaned = re.sub(r'^```(?:json)?\s*', '', cleaned, flags=re.I)
    cleaned = re.sub(r'\s*```$', '', cleaned)
    start, end = cleaned.find('{'), cleaned.rfind('}')
    if start < 0 or end <= start:
        raise RuntimeError('Gemini 응답에서 JSON 객체를 찾지 못했습니다.')
    try:
        return json.loads(cleaned[start:end + 1])
    except json.JSONDecodeError as exc:
        raise RuntimeError(f'Gemini JSON 해석 실패: {exc}')


def weighted_category() -> str:
    # 날짜 기반 회전으로 같은 날 재실행해도 카테고리가 흔들리지 않게 함
    day = datetime.now(KST).timetuple().tm_yday
    sequence = []
    for name, cfg in CATEGORIES.items():
        sequence.extend([name] * cfg['weight'])
    return sequence[(day - 1) % len(sequence)]


def research_topic(category: str) -> dict:
    """Google Search grounding으로 실제 검색 결과에 근거한 주제/자료 조사."""
    seeds = CATEGORIES[category]['seeds']
    prompt = f'''너는 한국 생활정보 전문 리서처다. 오늘은 {TODAY}이고 조사 분야는 "{category}"다.
아래 후보 영역을 참고해 한국 독자가 실제로 검색하고 당장 활용할 만한 구체적인 생활정보 글감 하나를 선정하라.
후보: {', '.join(seeds)}

검색을 통해 현재도 유효한지 확인하고, 가능하면 정부·공공기관·공식 사업자·소비자기관의 원문 자료를 우선 확인하라.
아래 JSON만 출력하라.
{{"topic":"구체적인 글 주제","search_intent":"독자가 해결하려는 질문","reader_benefit":"글을 읽고 얻는 실질적 도움","timeliness":"지금 다룰 이유 또는 상시형 주제인 이유","outline":["소제목1","소제목2","소제목3","소제목4"],"research_questions":["확인할 사실1","확인할 사실2","확인할 사실3"]}}
사실이나 제도 변경 여부를 확인하지 못하면 추정하지 말고 다른 주제를 고르라. 특정 상품 가입·구매를 유도하지 말라.'''
    payload = {
        'contents': [{'role': 'user', 'parts': [{'text': prompt}]}],
        'tools': [{'google_search': {}}],
        'generationConfig': {'temperature': 0.45, 'maxOutputTokens': 1800},
    }
    data = post_json_retry(gemini_url(), headers={'x-goog-api-key': need('GEMINI_API_KEY'), 'Content-Type': 'application/json'}, payload=payload)
    topic = extract_json(extract_text(data))
    grounding = data.get('candidates', [{}])[0].get('groundingMetadata', {})
    chunks = grounding.get('groundingChunks', []) or []
    sources = []
    for chunk in chunks:
        web = chunk.get('web') or {}
        uri, title = web.get('uri'), web.get('title')
        if uri and uri.startswith('http'):
            host = urlparse(uri).netloc.lower()
            if not any(s['url'] == uri for s in sources):
                sources.append({'title': title or host, 'url': uri, 'domain': host})
    topic['_sources'] = sources
    topic['_search_queries'] = grounding.get('webSearchQueries', [])
    return topic


def research_facts(topic: dict) -> dict:
    """주제 선정과 별도 검색 단계에서 핵심 사실과 출처를 교차 확인."""
    prompt = f'''다음 글감을 한국 생활정보 독자를 위해 추가 조사하라.
주제: {topic.get('topic')}
독자 질문: {topic.get('search_intent')}
확인 질문: {json.dumps(topic.get('research_questions', []), ensure_ascii=False)}

Google 검색을 사용해 최신 원문을 확인하라. 특히 적용 대상, 조건, 금액/기간/기준일, 예외, 신청·확인 방법을 구분하라.
JSON만 출력:
{{"research_summary":"조사 요약","facts":[{{"claim":"확인된 사실","detail":"조건과 맥락","source_hint":"해당 사실을 뒷받침하는 기관/문서"}}],"cautions":["독자가 오해하기 쉬운 점"],"how_to_check":"독자가 최신 정보를 직접 확인할 방법"}}
출처가 확인되지 않은 숫자·날짜·제도 내용을 쓰지 말라. 자료끼리 다르면 그 차이를 명시하고 단정하지 말라.'''
    payload = {
        'contents': [{'role': 'user', 'parts': [{'text': prompt}]}],
        'tools': [{'google_search': {}}],
        'generationConfig': {'temperature': 0.25, 'maxOutputTokens': 3000},
    }
    data = post_json_retry(gemini_url(), headers={'x-goog-api-key': need('GEMINI_API_KEY'), 'Content-Type': 'application/json'}, payload=payload)
    facts = extract_json(extract_text(data))
    grounding = data.get('candidates', [{}])[0].get('groundingMetadata', {})
    all_sources = list(topic.get('_sources', []))
    for chunk in grounding.get('groundingChunks', []) or []:
        web = chunk.get('web') or {}
        uri, title = web.get('uri'), web.get('title')
        if uri and uri.startswith('http') and not any(s['url'] == uri for s in all_sources):
            all_sources.append({'title': title or urlparse(uri).netloc, 'url': uri, 'domain': urlparse(uri).netloc.lower()})
    facts['_sources'] = all_sources
    facts['_search_queries'] = list(topic.get('_search_queries', [])) + list(grounding.get('webSearchQueries', []) or [])
    return facts


def write_article(topic: dict, research: dict) -> dict:
    sources = research.get('_sources', [])
    source_lines = '\n'.join(f"- {s['title']} | {s['url']}" for s in sources)
    prompt = f'''너는 한국의 실용적인 생활정보 전문 에디터다. 아래 조사 결과와 출처만 근거로 블로그 글을 작성한다.
오늘 날짜: {TODAY}
카테고리: {topic.get('_category', weighted_category())}
주제: {topic.get('topic')}
검색 의도: {topic.get('search_intent')}
독자 혜택: {topic.get('reader_benefit')}
구성 제안: {json.dumps(topic.get('outline', []), ensure_ascii=False)}
조사 결과: {json.dumps(research, ensure_ascii=False)}
확보된 출처 목록:
{source_lines}

JSON 객체만 출력:
{{"title":"검색 의도가 명확한 제목","description":"검색 결과용 100~150자 설명","intro":"도입부 2~3문장","sections":[{{"heading":"소제목","body":"본문 문단"}}],"checklist":["실천 항목"],"conclusion":"마무리 요약","last_checked":"자료 확인 기준일 YYYY-MM-DD","tags":["태그1","태그2"]}}

작성 규칙:
1. 1,200~1,800자 내외의 구체적이고 실용적인 글. 억지로 분량을 채우지 말 것.
2. 독자가 실제로 따라 할 수 있는 순서, 조건, 확인 경로를 포함할 것.
3. 조사 결과에 없는 수치·날짜·기관 입장·정책·법률 해석·혜택을 만들지 말 것.
4. 대상과 예외 조건을 생략하지 말 것. 불확실한 사항은 확인 필요라고 쓸 것.
5. 특정 업체/상품 광고처럼 쓰지 말고 장단점과 주의점을 균형 있게 설명할 것.
6. 건강·법률·세금·투자 등 고위험 주제는 일반 정보로 한정하고 전문가/공식기관 확인을 안내할 것.
7. 제목 낚시, 공포 조장, 과장, 반복 문장, AI식 상투어 금지.
8. 출처를 본문에서 언급할 때는 확보된 출처에 한정할 것. HTML 태그/마크다운 금지.
9. last_checked는 실제 조사일인 {TODAY}로 작성.
'''
    payload = {
        'contents': [{'role': 'user', 'parts': [{'text': prompt}]}],
        'generationConfig': {'temperature': 0.65, 'maxOutputTokens': 5000},
    }
    data = post_json_retry(gemini_url(), headers={'x-goog-api-key': need('GEMINI_API_KEY'), 'Content-Type': 'application/json'}, payload=payload)
    article = extract_json(extract_text(data))
    article['_sources'] = sources
    article['_research'] = research
    article['_topic'] = topic
    return article


def quality_check(article: dict) -> list[str]:
    errors = []
    title = str(article.get('title', '')).strip()
    desc = str(article.get('description', '')).strip()
    intro = str(article.get('intro', '')).strip()
    sections = article.get('sections')
    checklist = article.get('checklist')
    sources = article.get('_sources', [])
    research = article.get('_research', {})
    facts = research.get('facts', []) if isinstance(research, dict) else []
    if len(title) < 12 or len(title) > 60: errors.append('제목 길이 기준 미달(12~60자)')
    if len(desc) < 40 or len(desc) > 180: errors.append('검색 설명 길이 기준 미달(40~180자)')
    if len(intro) < 50: errors.append('도입부가 너무 짧음')
    if not isinstance(sections, list) or len(sections) < 3: errors.append('본문 소제목이 3개 미만')
    if not isinstance(checklist, list) or len(checklist) < 3: errors.append('실천 체크리스트가 3개 미만')
    if len(sources) < 2: errors.append('검색 근거 출처가 2개 미만')
    domains = {urlparse(s.get('url', '')).netloc.lower() for s in sources if s.get('url')}
    if len(domains) < 2: errors.append('서로 다른 출처 도메인이 2개 미만')
    if len(facts) < 2: errors.append('검증된 핵심 사실이 2개 미만')
    if not article.get('conclusion'): errors.append('결론 누락')
    if article.get('last_checked') != TODAY: errors.append('자료 확인일이 오늘 날짜와 다름')
    if any(not s.get('url', '').startswith('https://') for s in sources): errors.append('HTTPS가 아닌 출처 포함')
    body_text = ' '.join([intro] + [str(x.get('body', '')) for x in (sections or [])] + [str(article.get('conclusion', ''))])
    if len(body_text) < 900: errors.append('본문 전체 분량이 너무 짧음')
    if re.search(r'\b(무조건|100% 보장|반드시 수익|확실하게 돈 버는)\b', body_text): errors.append('과장·보장 표현 발견')
    return errors


def make_signature(article: dict) -> str:
    raw = re.sub(r'\s+', '', str(article.get('title', '')).lower())
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()[:24]


def render_html(article: dict) -> str:
    esc = html.escape
    sections_html = ''.join(
        f'<h2>{esc(str(s.get("heading", "")))}</h2><p>{esc(str(s.get("body", "")))}</p>'
        for s in article.get('sections', [])
    )
    checklist_html = ''.join(f'<li>{esc(str(item))}</li>' for item in article.get('checklist', []))
    sources_html = ''.join(
        f'<li><a href="{esc(s["url"], quote=True)}" target="_blank" rel="noopener">{esc(s.get("title") or s.get("domain") or s["url"])}</a></li>'
        for s in article.get('_sources', [])
    )
    tags = ', '.join(str(t) for t in article.get('tags', [])[:8])
    return f'''<!-- LIFE_INFO_SIGNATURE:{make_signature(article)} -->
<div style="font-family:'Pretendard','Noto Sans KR','Malgun Gothic',Arial,sans-serif;max-width:850px;margin:0 auto;line-height:1.85;color:#25282c;font-size:16px;word-break:keep-all">
<p style="font-size:12px;font-weight:800;letter-spacing:1.2px;color:#28745b;margin:0 0 8px">LIFE INFO · {esc(BLOG_NAME)}</p>
<h1 style="font-size:27px;line-height:1.45;margin:0 0 14px">{esc(article['title'])}</h1>
<p style="font-size:13px;color:#777;margin:0 0 22px">자료 확인일 · {esc(str(article.get('last_checked', TODAY)))}</p>
<p style="font-size:17px;color:#4b5563;margin:18px 0 26px">{esc(article['intro'])}</p>
<!--more-->
{sections_html}
<h2>실천 전 확인할 사항</h2><ul>{checklist_html}</ul>
<div style="margin:28px 0;padding:18px 20px;background:#eff7f2;border-left:4px solid #28745b;border-radius:8px"><strong>핵심 정리</strong><p style="margin:7px 0 0">{esc(article['conclusion'])}</p></div>
<h2>참고 자료 및 출처</h2><ul>{sources_html}</ul>
<p style="font-size:12px;color:#777;border-top:1px solid #e5e7eb;padding-top:14px;margin-top:24px">이 글은 일반적인 정보 제공을 목적으로 합니다. 제도·요금·상품 조건은 변경될 수 있으므로 실제 이용 전 공식 출처에서 최신 내용을 확인하세요.</p>
<p style="font-size:12px;color:#888">관련 주제: {esc(tags)}</p>
</div>'''


def blogger_service():
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from google.auth.transport.requests import Request
    info = json.loads(need('BLOGGER_TOKEN_JSON'))
    creds = Credentials.from_authorized_user_info(info, scopes=['https://www.googleapis.com/auth/blogger'])
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    if not creds.valid:
        raise RuntimeError('Blogger OAuth 토큰이 유효하지 않습니다. refresh token을 확인하세요.')
    return build('blogger', 'v3', credentials=creds, cache_discovery=False)


def publish(article: dict, content: str) -> None:
    signature = make_signature(article)
    preview = OUT_DIR / f'preview_life_{TODAY}.html'
    preview.write_text(content, encoding='utf-8')
    metadata = OUT_DIR / f'preview_life_{TODAY}.json'
    metadata.write_text(json.dumps({k: v for k, v in article.items() if not k.startswith('_')}, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'[미리보기 생성] {preview}')
    if DRY_RUN:
        print('[DRY_RUN] 미리보기만 생성했습니다. Blogger에는 발행하지 않았습니다.')
        return
    if not BLOG_URL or BLOG_URL == '/':
        raise RuntimeError('BLOG_URL을 설정해야 실제 발행할 수 있습니다.')
    service = blogger_service()
    blogs = service.blogs().listByUser(userId='self').execute().get('items', [])
    blog = next((b for b in blogs if b.get('url', '').rstrip('/').lower() == BLOG_URL.rstrip('/').lower()), None)
    if not blog:
        raise RuntimeError(f'Blogger 계정에서 대상 블로그를 찾지 못했습니다: {BLOG_URL}')
    existing = set()
    token = None
    for _ in range(5):
        args = {'blogId': blog['id'], 'fetchBodies': True, 'maxResults': 100, 'status': ['LIVE']}
        if token: args['pageToken'] = token
        result = service.posts().list(**args).execute()
        for post in result.get('items', []):
            body = post.get('content', '') or ''
            existing.update(re.findall(r'LIFE_INFO_SIGNATURE:([a-f0-9]{24})', body))
            existing.add('TITLE:' + post.get('title', ''))
        token = result.get('nextPageToken')
        if not token: break
    if signature in existing or 'TITLE:' + article['title'] in existing:
        print('[중복 발행 방지] 동일한 글이 이미 존재합니다.')
        return
    service.posts().insert(
        blogId=blog['id'],
        body={'kind': 'blogger#post', 'title': article['title'], 'content': content, 'labels': article.get('tags', [])[:8]},
        isDraft=False,
    ).execute()
    print(f"[발행 완료] {article['title']}")


def main():
    if TASK not in ('auto', 'living', 'finance', 'travel', 'economy'):
        raise ValueError('TASK는 auto/living/finance/travel/economy 중 하나여야 합니다.')
    category = weighted_category()
    overrides = {'living': '생활비·절약', 'finance': '금융·돈 관리', 'travel': '여행·소비경제', 'economy': '물가·경제 흐름'}
    if TASK in overrides: category = overrides[TASK]
    print(f'[주제 탐색] category={category}, date={TODAY}')
    topic = research_topic(category)
    topic['_category'] = category
    if not topic.get('topic') or not topic.get('search_intent'):
        raise RuntimeError('주제 탐색 결과가 필수 항목을 충족하지 못했습니다.')
    print(f"[선정 주제] {topic['topic']}")
    research = research_facts(topic)
    article = write_article(topic, research)
    errors = quality_check(article)
    if errors:
        print('[품질 검수 실패] 발행 중단')
        for error in errors: print(' - ' + error)
        raise RuntimeError('품질 검수 기준을 통과하지 못해 발행하지 않았습니다.')
    print(f"[품질 검수 통과] sources={len(article['_sources'])}, facts={len(research.get('facts', []))}")
    publish(article, render_html(article))


if __name__ == '__main__':
    main()
