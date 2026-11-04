#!/usr/bin/env python3
"""생활정보 Blogger 자동화 v1.0
- Tavily 검색과 Groq 글 생성을 이용한 주제 조사
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
from datetime import date

import requests

KST = timezone(timedelta(hours=9))
TODAY = datetime.now(KST).strftime('%Y-%m-%d')
BLOG_URL = os.getenv('BLOG_URL', '').strip().rstrip('/') + '/'
BLOG_NAME = os.getenv('BLOG_NAME', '생활정보 노트').strip()
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


def api_post(url: str, *, headers: dict, payload: dict, label: str, attempts: int = 3) -> dict:
    last_error = None
    for attempt in range(attempts):
        try:
            response = requests.post(url, headers=headers, json=payload, timeout=REQUEST_TIMEOUT)
            if response.status_code in (408, 429, 500, 502, 503, 504) and attempt < attempts - 1:
                # Groq free-tier TPM limits are minute-based; a short retry repeats the same failure.
                wait = 65 if response.status_code == 429 else [5, 15][min(attempt, 1)]
                print(f'[{label} 재시도] HTTP {response.status_code}, {wait}초 대기')
                time.sleep(wait); continue
            if not response.ok:
                raise RuntimeError(f'{label} API 오류 {response.status_code}: {response.text[:700]}')
            return response.json()
        except requests.RequestException as exc:
            last_error = exc
            if attempt < attempts - 1:
                wait = [5, 15][min(attempt, 1)]
                print(f'[{label} 네트워크 재시도] {exc}, {wait}초 대기'); time.sleep(wait)
    raise RuntimeError(f'{label} 요청 실패: {last_error}')


def groq_text(prompt: str, *, temperature: float = 0.35, max_tokens: int = 3000) -> str:
    payload = {'model': os.getenv('GROQ_MODEL', 'openai/gpt-oss-120b').strip(),
               'messages': [{'role': 'system', 'content': '한국어 생활정보 전문 에디터다. 제공된 자료에 없는 사실을 만들지 말고 요청된 JSON만 출력한다.'},
                            {'role': 'user', 'content': prompt}],
               'temperature': temperature, 'max_completion_tokens': max_tokens,
               'response_format': {'type': 'json_object'}}
    data = api_post('https://api.groq.com/openai/v1/chat/completions',
                    headers={'Authorization': f"Bearer {need('GROQ_API_KEY')}", 'Content-Type': 'application/json'},
                    payload=payload, label='Groq')
    try: return data['choices'][0]['message']['content']
    except (KeyError, IndexError, TypeError): raise RuntimeError('Groq 응답에서 본문을 찾지 못했습니다.')


def extract_json(text: str) -> dict:
    cleaned = re.sub(r'^```(?:json)?\s*', '', text.strip(), flags=re.I)
    cleaned = re.sub(r'\s*```$', '', cleaned)
    start, end = cleaned.find('{'), cleaned.rfind('}')
    if start < 0 or end <= start: raise RuntimeError('AI 응답에서 JSON 객체를 찾지 못했습니다.')
    try: return json.loads(cleaned[start:end + 1])
    except json.JSONDecodeError as exc: raise RuntimeError(f'AI JSON 해석 실패: {exc}')


def tavily_search(query: str, max_results: int = 6) -> list[dict]:
    data = api_post('https://api.tavily.com/search',
                    headers={'Authorization': f"Bearer {need('TAVILY_API_KEY')}", 'Content-Type': 'application/json'},
                    payload={'query': query, 'search_depth': 'basic', 'topic': 'general', 'max_results': max_results,
                             'include_answer': False, 'include_raw_content': False, 'country': 'south korea'},
                    label='Tavily', attempts=2)
    results = []
    for item in data.get('results', []):
        url = str(item.get('url', '')).strip()
        if url.startswith('https://'):
            results.append({'title': item.get('title') or urlparse(url).netloc, 'url': url,
                            'domain': urlparse(url).netloc.lower(),
                            'content': (item.get('content') or item.get('raw_content') or '')[:1600]})
    return results

def weighted_category() -> str:
    # 100일 가중 순환을 하루 단위로 분산해 특정 카테고리가 연속되는 현상을 방지
    day = datetime.now(KST).timetuple().tm_yday
    total = sum(cfg['weight'] for cfg in CATEGORIES.values())
    scores = {name: 0 for name in CATEGORIES}
    selected = None
    for _ in range(day):
        for name, cfg in CATEGORIES.items():
            scores[name] += cfg['weight']
        selected = max(scores, key=scores.get)
        scores[selected] -= total
    return selected


def recent_published_titles(limit: int = 60) -> list[str]:
    """Fetch recent live post titles so scheduled topics avoid recent repeats."""
    service = blogger_service()
    blogs = service.blogs().listByUser(userId='self').execute().get('items', [])
    blog = next((b for b in blogs if b.get('url', '').rstrip('/').lower() == BLOG_URL.rstrip('/').lower()), None)
    if not blog:
        raise RuntimeError(f'Blogger 계정에서 대상 블로그를 찾지 못했습니다: {BLOG_URL}')
    result = service.posts().list(blogId=blog['id'], status=['LIVE'], fetchBodies=False, maxResults=limit).execute()
    return [str(item.get('title', '')).strip()[:100] for item in result.get('items', []) if item.get('title')]


def research_topic(category: str, recent_titles: list[str] | None = None) -> dict:
    # Tavily search results guide topic selection.
    seeds = CATEGORIES[category]['seeds']
    day = datetime.now(KST).timetuple().tm_yday
    seed = seeds[(day - 1) % len(seeds)]
    results = tavily_search(f'대한민국 {seed} 최신 정보 공식 정부 공공기관 소비자원 {TODAY}', 4)
    if len(results) < 2:
        raise RuntimeError('주제 탐색 출처가 2개 미만입니다. 안전을 위해 발행을 중단합니다.')
    recent_titles = recent_titles or []
    prompt = f'''오늘 날짜는 {TODAY}, 카테고리는 {category}, 검색 후보는 {seed}다.
아래 실제 검색 결과를 읽고 출처가 뒷받침하는 범위 안에서 구체적인 생활정보 주제 하나를 정하라.
최근 발행한 글 제목(같은 주제·유사한 질문은 피할 것): {json.dumps(recent_titles[:60], ensure_ascii=False)}
검색결과: {json.dumps(results, ensure_ascii=False)}
JSON만 출력: {{"topic":"구체적인 글 주제","search_intent":"독자가 해결하려는 질문","reader_benefit":"실질적 도움","timeliness":"지금 다룰 이유","outline":["소제목1","소제목2","소제목3","소제목4"],"research_questions":["확인할 사실1","확인할 사실2","확인할 사실3"]}}
검색 결과와 무관한 정책·혜택을 만들지 말라. 최근 발행 글과 제목만 바꾼 사실상 같은 주제를 선택하지 말고, 독자의 질문과 해결 방법이 다른 주제를 선택하라.'''
    topic = extract_json(groq_text(prompt, temperature=0.3, max_tokens=1800))
    topic['_sources'] = results; topic['_search_queries'] = [seed]
    return topic


def research_facts(topic: dict) -> dict:
    # Search the selected topic and pass source results to the writer.
    query = f"대한민국 {topic.get('topic')} {topic.get('search_intent')} 공식 안내 기준 조건 예외 {TODAY}"
    results = tavily_search(query, 5)
    all_sources = list(topic.get('_sources', [])); seen = {x.get('url') for x in all_sources}
    for item in results:
        if item['url'] not in seen: all_sources.append(item); seen.add(item['url'])
    if len(all_sources) < 2: raise RuntimeError('조사 근거 출처가 부족해 발행을 중단합니다.')
    prompt = f'''다음 주제의 사실을 검색자료에 근거해 정리하라.
주제: {topic.get('topic')}
확인 질문: {json.dumps(topic.get('research_questions', []), ensure_ascii=False)}
검색자료: {json.dumps(results, ensure_ascii=False)}
JSON만 출력: {{"research_summary":"조사 요약","facts":[{{"claim":"확인된 사실","detail":"조건과 맥락","source_hint":"근거가 된 검색자료 제목"}}],"cautions":["오해하기 쉬운 점"],"how_to_check":"최신 정보를 확인할 방법"}}
검색자료에 없는 금액·날짜·제도·조건은 추정하지 말라. 근거가 약한 사실은 facts에 넣지 말라.'''
    facts = extract_json(groq_text(prompt, temperature=0.2, max_tokens=2600))
    facts['_sources'] = all_sources; facts['_search_queries'] = [query]
    return facts

def write_article(topic: dict, research: dict) -> dict:
    sources = research.get('_sources', [])
    # Never send the full Tavily payload to the writer: keep only concise verified facts
    # and source metadata so the prompt stays comfortably below Groq's free-tier TPM cap.
    compact_research = {
        'research_summary': str(research.get('research_summary', ''))[:1800],
        'facts': [
            {'claim': str(x.get('claim', ''))[:300], 'detail': str(x.get('detail', ''))[:500],
             'source_hint': str(x.get('source_hint', ''))[:120]}
            for x in research.get('facts', [])[:6] if isinstance(x, dict)
        ],
        'cautions': [str(x)[:250] for x in research.get('cautions', [])[:5]],
        'how_to_check': str(research.get('how_to_check', ''))[:500],
    }
    compact_topic = {
        'topic': str(topic.get('topic', ''))[:200],
        'search_intent': str(topic.get('search_intent', ''))[:300],
        'reader_benefit': str(topic.get('reader_benefit', ''))[:300],
        'outline': [str(x)[:100] for x in topic.get('outline', [])[:5]],
    }
    source_lines = '\n'.join(f"- {s['title'][:180]} | {s['url']}" for s in sources[:8])
    prompt = f'''너는 한국의 실용적인 생활정보 전문 에디터다. 아래 조사 결과와 출처만 근거로 블로그 글을 작성한다.
오늘 날짜: {TODAY}
카테고리: {weighted_category()}
주제 및 독자 의도: {json.dumps(compact_topic, ensure_ascii=False)}
검증된 조사 결과: {json.dumps(compact_research, ensure_ascii=False)}
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
    article = extract_json(groq_text(prompt, temperature=0.55, max_tokens=1800))
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
    trusted_suffixes = ('.go.kr', 'gov.kr', '.or.kr')
    trusted_domains = ('energyv.or.kr', 'kca.go.kr', 'bok.or.kr', 'fss.or.kr', 'fsc.go.kr', 'kosis.kr', 'korea.kr')
    if not any(any(urlparse(s.get('url', '')).netloc.lower().endswith(suffix) for suffix in trusted_suffixes) or any(domain in urlparse(s.get('url', '')).netloc.lower() for domain in trusted_domains) for s in sources):
        errors.append('정부·공공기관 등 1차 출처를 확인하지 못함')
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


def split_paragraphs(text: str) -> str:
    # Split dense Korean copy into short, mobile-friendly paragraphs.
    text = str(text or '').strip()
    if not text:
        return ''
    # Preserve intentional paragraph breaks; otherwise group two sentences.
    chunks = [x.strip() for x in re.split(r'\n+', text) if x.strip()]
    paragraphs = []
    for chunk in chunks:
        sentences = re.split(r'(?<=[.!?。])\s+', chunk)
        group = []
        for sentence in sentences:
            if sentence:
                group.append(sentence)
            if len(group) >= 2:
                paragraphs.append(' '.join(group)); group = []
        if group:
            paragraphs.append(' '.join(group))
    return ''.join(f'<p style="margin:0 0 15px;line-height:1.9;color:#343a40">{html.escape(p)}</p>' for p in paragraphs)


def render_html(article: dict) -> str:
    esc = html.escape
    sections_html = ''.join(
        f'''<section style="margin:30px 0 34px">
<h2 style="font-size:21px;line-height:1.5;color:#174b38;margin:0 0 14px;padding:0 0 9px;border-bottom:1px solid #dce9e1">{esc(str(s.get("heading", "")))}</h2>
{split_paragraphs(str(s.get("body", "")))}</section>'''
        for s in article.get('sections', [])
    )
    checklist_html = ''.join(
        f'<li style="margin:0 0 11px;padding-left:3px;line-height:1.75">{esc(str(item))}</li>'
        for item in article.get('checklist', [])
    )
    sources_html = ''.join(
        f'<li style="margin:0 0 10px;line-height:1.65"><a style="color:#216b4d;text-decoration:underline;text-underline-offset:3px" href="{esc(s["url"], quote=True)}" target="_blank" rel="noopener">{esc(s.get("title") or s.get("domain") or s["url"])}</a></li>'
        for s in article.get('_sources', [])
    )
    tags = ', '.join(str(t) for t in article.get('tags', [])[:8])
    facts = (article.get('_research') or {}).get('facts', [])
    facts_html = ''.join(
        f'''<div style="background:#fff;border:1px solid #e2ebe5;border-radius:12px;padding:14px 16px;margin:0 0 10px">
<strong style="display:block;color:#174b38;margin:0 0 5px">{esc(str(f.get('claim','')))}</strong>
<p style="margin:0;color:#555;line-height:1.75">{esc(str(f.get('detail','')))}</p></div>'''
        for f in facts[:4] if isinstance(f, dict) and (f.get('claim') or f.get('detail'))
    )
    facts_block = (f'<div style="margin:24px 0 30px"><h2 style="font-size:20px;color:#174b38;margin:0 0 13px">먼저 확인할 핵심 내용</h2>{facts_html}</div>' if facts_html else '')
    return f'''<!-- LIFE_INFO_SIGNATURE:{make_signature(article)} -->
<div style="font-family:'Pretendard','Noto Sans KR','Malgun Gothic',Arial,sans-serif;max-width:780px;margin:0 auto;padding:0 2px;line-height:1.85;color:#25282c;font-size:16px;word-break:keep-all">
<div style="font-size:12px;font-weight:800;letter-spacing:1.3px;color:#28745b;margin:0 0 10px">LIFE INFO · {esc(BLOG_NAME)}</div>
<h1 style="font-size:28px;line-height:1.42;letter-spacing:-.6px;margin:0 0 13px;color:#17251e">{esc(article['title'])}</h1>
<p style="font-size:12px;color:#89918c;margin:0 0 22px">자료 확인일 · {esc(str(article.get('last_checked', TODAY)))}</p>
<div style="background:#f1f7f3;border:1px solid #e0ece4;border-radius:14px;padding:19px 20px;margin:0 0 28px">
<div style="font-size:12px;font-weight:800;letter-spacing:.5px;color:#28745b;margin:0 0 7px">이 글에서 알 수 있는 것</div>
<p style="font-size:16px;line-height:1.85;color:#34483d;margin:0">{esc(article['intro'])}</p></div>
<!--more-->
{facts_block}
{sections_html}
<section style="background:#f8faf8;border:1px solid #e5ece7;border-radius:14px;padding:18px 20px;margin:30px 0">
<h2 style="font-size:20px;color:#174b38;margin:0 0 14px">실천 전 확인할 사항</h2><ul style="padding-left:21px;margin:0">{checklist_html}</ul></section>
<div style="margin:28px 0;padding:20px 21px;background:#eaf4ee;border-left:4px solid #28745b;border-radius:0 12px 12px 0">
<div style="font-size:12px;font-weight:800;letter-spacing:.7px;color:#28745b;margin-bottom:7px">핵심 정리</div><p style="font-size:16px;line-height:1.85;color:#263b30;margin:0">{esc(article['conclusion'])}</p></div>
<section style="margin-top:34px;padding-top:20px;border-top:1px solid #e5ebe7">
<h2 style="font-size:18px;color:#174b38;margin:0 0 14px">참고 자료 및 출처</h2><ul style="padding-left:20px;margin:0">{sources_html}</ul></section>
<p style="font-size:12px;line-height:1.7;color:#858d88;border-top:1px solid #edf0ee;padding-top:14px;margin-top:22px">이 글은 일반적인 정보 제공을 목적으로 합니다. 제도·요금·상품 조건은 변경될 수 있으므로 실제 이용 전 공식 출처에서 최신 내용을 확인하세요.</p>
<p style="font-size:12px;color:#89918c;margin-top:10px">관련 주제: {esc(tags)}</p>
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
    recent_titles = recent_published_titles() if not DRY_RUN else []
    if not DRY_RUN:
        print(f'[최근 발행 제목 확인] {len(recent_titles)}개 — 유사 주제 제외 적용')
    topic = research_topic(category, recent_titles)
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
