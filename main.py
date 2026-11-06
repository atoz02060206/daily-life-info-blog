#!/usr/bin/env python3
"""아꼈슈 Blogger 자동화 v1.1
- Tavily 검색과 Groq 글 생성을 이용한 주제 조사
- 조사 근거를 바탕으로 한국어 글 생성
- 출처/구조/중복/품질 검수 후 미리보기 또는 Blogger 발행
- 체크리스트·결론이 부족하면 Groq로 1회만 자동 보완 후 재검수
- 자료 확인일은 AI가 아니라 Python이 KST 기준으로 직접 설정
- 기본값은 DRY_RUN=true (미리보기 전용)
"""
from __future__ import annotations

import email.utils
import hashlib
import html
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests

KST = timezone(timedelta(hours=9))
TODAY = datetime.now(KST).strftime('%Y-%m-%d')
BLOG_URL = os.getenv('BLOG_URL', '').strip().rstrip('/') + '/'
BLOG_NAME = os.getenv('BLOG_NAME', '').strip() or '생활정보 노트'
DRY_RUN = os.getenv('DRY_RUN', 'true').strip().lower() not in ('0', 'false', 'no')
TASK = os.getenv('TASK', '').strip().lower() or 'auto'
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

GROQ_URL = 'https://api.groq.com/openai/v1/chat/completions'
DEFAULT_GROQ_MODEL = 'openai/gpt-oss-120b'
REQUEST_TIMEOUT = (15, 120)
SERVER_ERRORS = {408, 500, 502, 503, 504}

# 재시도 정책: 무한 재시도 없이 횟수와 총 대기시간을 모두 제한한다.
GROQ_ATTEMPTS = 4
NETWORK_BACKOFF = (10, 30, 60)
SERVER_BACKOFF = (10, 30, 60)
RATE_LIMIT_WAIT_NO_HINT = (65, 95, 130)  # 대기시간 안내가 없을 때: TPM은 분 단위로 회복
RATE_LIMIT_WAIT_MIN = (10, 30, 60)       # 안내가 있어도 시도할수록 늘어나는 최소 대기시간
RATE_LIMIT_BUFFER = 3
MAX_SINGLE_RATE_WAIT = 300               # 이보다 길면 일일 한도로 보고 기다리지 않는다.
MAX_TOTAL_RATE_WAIT = 600                # 실행 1회의 429 대기시간 합계 상한
MAX_PACING_WAIT = 70

# 추론 토큰이 응답 토큰과 함께 소모될 수 있어 글 생성 한도는 여유 있게 잡는다.
MAX_COMPLETION_CAP = 4500
TOPIC_MAX_TOKENS = 1400
FACTS_MAX_TOKENS = 2400
ARTICLE_MAX_TOKENS = 3200
REPAIR_MAX_TOKENS = 1200
TOPIC_SOURCE_CHARS = 450
FACTS_SOURCE_CHARS = 1100

CHECKLIST_MIN, CHECKLIST_MAX = 3, 5
CONCLUSION_MIN_SENTENCES, CONCLUSION_MAX_SENTENCES = 2, 4
CONCLUSION_MIN_CHARS = 40
MIN_BODY_CHARS = 900
MAX_CONCLUSION_GAIN = 250  # 2~4문장 결론이 분량에 더해질 수 있는 현실적인 최대치

ERR_CHECKLIST = '실천 체크리스트가 3개 미만'
ERR_CONCLUSION_MISSING = '결론 누락'
ERR_CONCLUSION_WEAK = '결론 내용 부족(2~4문장, 40자 이상 필요)'
ERR_BODY_SHORT = '본문 전체 분량이 너무 짧음'
REPAIRABLE_ERRORS = {ERR_CHECKLIST, ERR_CONCLUSION_MISSING, ERR_CONCLUSION_WEAK}

TRUSTED_SUFFIXES = ('.go.kr', '.or.kr')
TRUSTED_DOMAINS = ('gov.kr', 'energyv.or.kr', 'kca.go.kr', 'bok.or.kr', 'fss.or.kr', 'fsc.go.kr', 'kosis.kr', 'korea.kr')


class ApiError(RuntimeError):
    """API가 응답했지만 오류를 돌려준 경우."""

    def __init__(self, label: str, status: int, code: str, message: str):
        super().__init__(f'{label} API 오류 {status}{f" ({code})" if code else ""}: {message}')
        self.status, self.code, self.detail = status, code, message


class NetworkFailure(RuntimeError):
    """응답을 받지 못한 경우(연결 실패, 시간 초과 등)."""


class RateLimitFailure(RuntimeError):
    """요청 한도 초과가 해소되지 않아 재시도를 포기한 경우."""


class GroqOutputError(RuntimeError):
    """Groq 응답이 잘렸거나 JSON으로 해석할 수 없는 경우."""


_rate_wait_total = 0.0
_groq_state = {'calls': 0, 'tokens': 0, 'remaining': None, 'reset_at': 0.0}
_blogger_service = None


def need(name: str) -> str:
    value = os.getenv(name, '').strip()
    if not value:
        raise RuntimeError(f'필수 환경변수가 없습니다: {name}')
    return value


def parse_duration(value: str) -> float | None:
    """Groq 헤더 형식(예: 2m59.56s, 7.66s, 120ms)을 초로 변환."""
    parts = re.findall(r'(\d+(?:\.\d+)?)(ms|h|m|s)', str(value or ''))
    if not parts:
        return None
    scale = {'ms': 0.001, 's': 1, 'm': 60, 'h': 3600}
    return sum(float(number) * scale[unit] for number, unit in parts)


def parse_retry_after(value: str | None) -> float | None:
    """Retry-After 헤더(초 또는 HTTP 날짜)를 초로 변환."""
    if not value:
        return None
    value = str(value).strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(value)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError):
        return None


def error_detail(response) -> tuple[str, str]:
    """오류 코드와 메시지를 추출한다. 공개 로그에 남지 않도록 조직 ID는 가린다."""
    code, message = '', ''
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        error = body.get('error')
        if isinstance(error, dict):
            code, message = str(error.get('code') or ''), str(error.get('message') or '')
        elif error:
            message = str(error)
        elif body.get('detail'):
            message = str(body.get('detail'))
    message = message or str(getattr(response, 'text', '') or '')
    message = re.sub(r'org_[A-Za-z0-9]+', 'org_***', message)
    message = re.sub(r'(?i)bearer\s+[A-Za-z0-9._\-]+', 'Bearer ***', message)
    return code, message[:500]


def rate_limit_wait(response, step: int, message: str, label: str) -> tuple[float, str]:
    """429 대기시간 계산. Retry-After > 본문 안내 > 토큰 초기화 헤더 > 단계별 기본값 순."""
    step = min(step, len(RATE_LIMIT_WAIT_NO_HINT) - 1)
    if re.search(r'(?i)tokens per day|requests per day|\bTPD\b|\bRPD\b', message):
        raise RateLimitFailure(f'{label} 일일 사용량 한도를 초과해 재시도하지 않습니다. 한도 초기화 후 다시 실행하세요. 상세: {message}')
    headers = getattr(response, 'headers', {}) or {}
    hint = parse_retry_after(headers.get('retry-after'))
    source = 'Retry-After'
    if hint is None:
        found = re.search(r'(?i)try again in\s+([0-9hms.]+)', message)
        hint = parse_duration(found.group(1)) if found else None
        source = '오류 메시지'
    if hint is None and re.search(r'(?i)tokens|TPM', message):
        hint = parse_duration(headers.get('x-ratelimit-reset-tokens', ''))
        source = '토큰 초기화 헤더'
    if hint is None:
        return float(RATE_LIMIT_WAIT_NO_HINT[step]), '단계별 기본값'
    if hint > MAX_SINGLE_RATE_WAIT:
        raise RateLimitFailure(f'{label} 요청 한도 대기시간이 {hint:.0f}초로 너무 길어 재시도하지 않습니다. 상세: {message}')
    return max(hint + RATE_LIMIT_BUFFER, float(RATE_LIMIT_WAIT_MIN[step])), source


def api_post(url: str, *, headers: dict, payload: dict, label: str, attempts: int = 3, on_response=None) -> dict:
    """POST 요청. 네트워크 오류·서버 오류·429를 구분해 제한된 횟수만 재시도한다."""
    global _rate_wait_total
    rate_steps = network_steps = server_steps = 0
    for attempt in range(attempts):
        last_try = attempt == attempts - 1
        try:
            response = requests.post(url, headers=headers, json=payload, timeout=REQUEST_TIMEOUT)
        except (requests.ConnectionError, requests.Timeout) as exc:
            reason = f'{type(exc).__name__}'
            if last_try:
                raise NetworkFailure(f'{label} 네트워크 오류로 {attempts}회 시도 후 실패: {reason}') from exc
            wait = NETWORK_BACKOFF[min(network_steps, len(NETWORK_BACKOFF) - 1)]
            network_steps += 1
            print(f'[{label} 네트워크 재시도 {attempt + 1}/{attempts - 1}] {reason}, {wait}초 대기')
            time.sleep(wait)
            continue
        except requests.RequestException as exc:
            raise NetworkFailure(f'{label} 요청을 보내지 못했습니다: {type(exc).__name__}') from exc

        if on_response:
            on_response(response)
        status = response.status_code
        if status == 429:
            code, message = error_detail(response)
            if last_try:
                raise RateLimitFailure(f'{label} 요청 한도 초과가 {attempts}회 시도 후에도 해소되지 않았습니다. 상세: {message}')
            wait, source = rate_limit_wait(response, rate_steps, message, label)
            if _rate_wait_total + wait > MAX_TOTAL_RATE_WAIT:
                raise RateLimitFailure(f'{label} 요청 한도 대기시간 합계가 {MAX_TOTAL_RATE_WAIT}초를 넘어 중단합니다. 상세: {message}')
            rate_steps += 1
            _rate_wait_total += wait
            print(f'[{label} 재시도 {attempt + 1}/{attempts - 1}] HTTP 429, {wait:.0f}초 대기 (기준: {source})')
            time.sleep(wait)
            continue
        if status in SERVER_ERRORS:
            code, message = error_detail(response)
            if last_try:
                raise ApiError(label, status, code, message)
            wait = SERVER_BACKOFF[min(server_steps, len(SERVER_BACKOFF) - 1)]
            server_steps += 1
            print(f'[{label} 서버 오류 재시도 {attempt + 1}/{attempts - 1}] HTTP {status}, {wait}초 대기')
            time.sleep(wait)
            continue
        if not response.ok:
            code, message = error_detail(response)
            raise ApiError(label, status, code, message)  # 그 외 4xx는 다시 보내도 같은 결과
        try:
            data = response.json()
        except ValueError as exc:
            if last_try:
                raise ApiError(label, status, 'invalid_json', '응답이 JSON 형식이 아닙니다.') from exc
            wait = SERVER_BACKOFF[min(server_steps, len(SERVER_BACKOFF) - 1)]
            server_steps += 1
            print(f'[{label} 응답 형식 오류 재시도 {attempt + 1}/{attempts - 1}] {wait}초 대기')
            time.sleep(wait)
            continue
        if not isinstance(data, dict):
            raise ApiError(label, status, 'invalid_shape', '응답 최상위 구조가 JSON 객체가 아닙니다.')
        return data
    raise RuntimeError(f'{label} 요청 재시도 횟수가 올바르지 않습니다: attempts={attempts}')


def _track_groq_limits(response) -> None:
    headers = getattr(response, 'headers', {}) or {}
    try:
        remaining = headers.get('x-ratelimit-remaining-tokens')
        if remaining is not None:
            _groq_state['remaining'] = int(float(remaining))
            _groq_state['reset_at'] = time.monotonic() + (parse_duration(headers.get('x-ratelimit-reset-tokens', '')) or 0.0)
    except (TypeError, ValueError):
        pass


def estimate_tokens(text: str) -> int:
    # 정확한 값이 아니라 한국어 기준의 보수적인 어림값이다.
    return int(len(text) * 0.7) + 50


def pace_groq(estimated: int) -> None:
    """남은 TPM이 다음 요청 예상치보다 적으면 429를 맞기 전에 미리 기다린다."""
    remaining = _groq_state['remaining']
    if remaining is None or estimated <= remaining:
        return
    wait = _groq_state['reset_at'] - time.monotonic()
    if wait > 0:
        wait = min(wait + RATE_LIMIT_BUFFER, MAX_PACING_WAIT)
        print(f'[Groq 사용량 조절] 남은 TPM {remaining}, 예상 사용량 {estimated}: {wait:.0f}초 대기')
        time.sleep(wait)


def groq_text(prompt: str, *, temperature: float = 0.35, max_tokens: int = 3000, reasoning_effort: str = 'low') -> str:
    system = '한국어 생활정보 전문 에디터다. 제공된 자료에 없는 사실을 만들지 말고 요청된 JSON만 출력한다.'
    model = os.getenv('GROQ_MODEL', '').strip() or DEFAULT_GROQ_MODEL
    payload = {'model': model,
               'messages': [{'role': 'system', 'content': system}, {'role': 'user', 'content': prompt}],
               'temperature': temperature, 'max_completion_tokens': max_tokens,
               'response_format': {'type': 'json_object'}}
    if model.startswith('openai/gpt-oss'):
        payload['reasoning_effort'] = reasoning_effort
    pace_groq(estimate_tokens(system + prompt) + max_tokens)
    try:
        data = api_post(GROQ_URL, headers={'Authorization': f"Bearer {need('GROQ_API_KEY')}", 'Content-Type': 'application/json'},
                        payload=payload, label='Groq', attempts=GROQ_ATTEMPTS, on_response=_track_groq_limits)
    except ApiError as exc:
        if exc.status == 400 and (exc.code == 'json_validate_failed' or 'failed_generation' in exc.detail):
            raise GroqOutputError(f'Groq가 올바른 JSON을 만들지 못했습니다: {exc.detail[:200]}') from exc
        if exc.status == 413:
            raise RuntimeError(f'Groq 요청이 TPM 한도보다 큽니다. 프롬프트를 줄여야 합니다: {exc.detail[:200]}') from exc
        raise
    _groq_state['calls'] += 1
    usage = data.get('usage')
    if isinstance(usage, dict) and isinstance(usage.get('total_tokens'), int):
        _groq_state['tokens'] += usage['total_tokens']
    try:
        choice = data['choices'][0]
        message = choice['message']
    except (KeyError, IndexError, TypeError) as exc:
        raise GroqOutputError('Groq 응답 구조에서 본문을 찾지 못했습니다.') from exc
    content = message.get('content') if isinstance(message, dict) else None
    finish_reason = choice.get('finish_reason') if isinstance(choice, dict) else None
    if finish_reason == 'length':
        raise GroqOutputError(f'Groq 응답이 토큰 한도({max_tokens})에서 잘렸습니다.')
    if not isinstance(content, str) or not content.strip():
        raise GroqOutputError(f'Groq 응답 본문이 비어 있습니다. finish_reason={finish_reason}')
    return content


def extract_json(text: str) -> dict:
    cleaned = re.sub(r'^```(?:json)?\s*', '', str(text or '').strip(), flags=re.I)
    cleaned = re.sub(r'\s*```$', '', cleaned)
    start, end = cleaned.find('{'), cleaned.rfind('}')
    if start < 0 or end <= start:
        raise GroqOutputError('AI 응답에서 JSON 객체를 찾지 못했습니다.')
    try:
        parsed = json.loads(cleaned[start:end + 1])
    except json.JSONDecodeError as exc:
        raise GroqOutputError(f'AI JSON 해석 실패: {exc}') from exc
    if not isinstance(parsed, dict):
        raise GroqOutputError('AI 응답의 최상위 구조가 JSON 객체가 아닙니다.')
    return parsed


def groq_json(prompt: str, *, temperature: float, max_tokens: int, reasoning_effort: str = 'low', output_retries: int = 1) -> dict:
    """응답이 잘리거나 JSON이 깨졌을 때만 제한적으로 다시 요청한다."""
    for attempt in range(output_retries + 1):
        try:
            return extract_json(groq_text(prompt, temperature=temperature, max_tokens=max_tokens, reasoning_effort=reasoning_effort))
        except GroqOutputError as exc:
            if attempt >= output_retries:
                raise
            max_tokens = min(int(max_tokens * 1.4), MAX_COMPLETION_CAP)
            print(f'[Groq 출력 재요청 {attempt + 1}/{output_retries}] {exc} (max_tokens={max_tokens})')
    raise RuntimeError('Groq 출력 재요청 횟수가 올바르지 않습니다.')


def tavily_search(query: str, max_results: int = 6) -> list[dict]:
    data = api_post('https://api.tavily.com/search',
                    headers={'Authorization': f"Bearer {need('TAVILY_API_KEY')}", 'Content-Type': 'application/json'},
                    payload={'query': query[:380], 'search_depth': 'basic', 'topic': 'general', 'max_results': max_results,
                             'include_answer': False, 'include_raw_content': False, 'country': 'south korea'},
                    label='Tavily', attempts=2)
    items = data.get('results')
    results, seen = [], set()
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        url = str(item.get('url') or '').strip()
        if url.startswith('https://') and url not in seen and source_host(url):
            seen.add(url)
            results.append({'title': str(item.get('title') or urlparse(url).netloc), 'url': url,
                            'domain': urlparse(url).netloc.lower(),
                            'content': str(item.get('content') or item.get('raw_content') or '')[:1600]})
    return results


def compact_results(results: list[dict], content_chars: int) -> list[dict]:
    """프롬프트에 넣을 검색 결과만 줄인다. 원본은 근거 대조용으로 그대로 보관한다."""
    return [{'title': str(r.get('title', ''))[:120], 'url': r.get('url', ''), 'content': str(r.get('content', ''))[:content_chars]}
            for r in results]


def as_list(value) -> list:
    if isinstance(value, list):
        return value
    return [value] if value not in (None, '', {}) else []


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
    blog = find_target_blog(service)
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
    recent_titles = [title[:36] for title in (recent_titles or [])[:60]]
    prompt = f'''오늘 날짜는 {TODAY}, 카테고리는 {category}, 검색 후보는 {seed}다.
아래 실제 검색 결과를 읽고 출처가 뒷받침하는 범위 안에서 구체적인 생활정보 주제 하나를 정하라.
최근 발행한 글 제목(같은 주제·유사한 질문은 피할 것): {json.dumps(recent_titles, ensure_ascii=False)}
검색결과: {json.dumps(compact_results(results, TOPIC_SOURCE_CHARS), ensure_ascii=False)}
JSON만 출력: {{"topic":"구체적인 글 주제","search_intent":"독자가 해결하려는 질문","reader_benefit":"실질적 도움","timeliness":"지금 다룰 이유","outline":["소제목1","소제목2","소제목3","소제목4"],"research_questions":["확인할 사실1","확인할 사실2","확인할 사실3"]}}
검색 결과와 무관한 정책·혜택을 만들지 말라. 최근 발행 글과 제목만 바꾼 사실상 같은 주제를 선택하지 말고, 독자의 질문과 해결 방법이 다른 주제를 선택하라.'''
    topic = groq_json(prompt, temperature=0.3, max_tokens=TOPIC_MAX_TOKENS)
    topic['_sources'] = results
    topic['_search_queries'] = [seed]
    return topic


def research_facts(topic: dict) -> dict:
    # Search the selected topic and pass source results to the writer.
    query = f"대한민국 {topic.get('topic')} {topic.get('search_intent')} 공식 안내 기준 조건 예외 {TODAY}"
    results = tavily_search(query, 5)
    all_sources = list(topic.get('_sources', []))
    seen = {x.get('url') for x in all_sources}
    for item in results:
        if item['url'] not in seen:
            all_sources.append(item)
            seen.add(item['url'])
    if len(all_sources) < 2:
        raise RuntimeError('조사 근거 출처가 부족해 발행을 중단합니다.')
    prompt = f'''다음 주제의 사실을 검색자료에 근거해 정리하라.
주제: {topic.get('topic')}
확인 질문: {json.dumps(as_list(topic.get('research_questions'))[:5], ensure_ascii=False)}
검색자료: {json.dumps(compact_results(results, FACTS_SOURCE_CHARS), ensure_ascii=False)}
JSON만 출력: {{"research_summary":"조사 요약","facts":[{{"claim":"확인된 사실","detail":"조건과 맥락","source_hint":"근거가 된 검색자료 제목"}}],"cautions":["오해하기 쉬운 점"],"how_to_check":"최신 정보를 확인할 방법"}}
검색자료에 없는 금액·날짜·제도·조건은 추정하지 말라. 근거가 약한 사실은 facts에 넣지 말라.'''
    facts = groq_json(prompt, temperature=0.2, max_tokens=FACTS_MAX_TOKENS, reasoning_effort='medium')
    facts['_sources'] = all_sources
    facts['_search_queries'] = [query]
    verify_research_facts(facts)
    return facts


def verify_research_facts(research: dict) -> None:
    """형식이 잘못됐거나 검색자료에 없는 금액·비율·날짜를 담은 사실은 제외한다."""
    corpus = source_corpus(research.get('_sources', []))
    kept = []
    for fact in as_list(research.get('facts')):
        if not isinstance(fact, dict) or not str(fact.get('claim') or '').strip():
            continue
        unsupported = find_unsupported_figures(f"{fact.get('claim', '')} {fact.get('detail', '')}", corpus)
        if unsupported:
            print(f"[사실 제외] 검색자료에서 확인되지 않은 수치 {unsupported}: {str(fact.get('claim'))[:60]}")
            continue
        kept.append(fact)
    research['facts'] = kept


def clean_text(value) -> str:
    """AI 응답 값을 일반 문자열로 정리한다. HTML 태그와 마크다운 굵게 표시는 제거한다."""
    if value is None or isinstance(value, bool):
        return ''
    if isinstance(value, dict):
        for key in ('text', 'item', 'task', 'content', 'body', 'title', 'value'):
            if value.get(key):
                return clean_text(value[key])
        return ''
    if isinstance(value, (list, tuple)):
        return '\n'.join(part for part in (clean_text(x) for x in value) if part)
    text = re.sub(r'</?[a-zA-Z][^>]*>', '', str(value))
    return text.replace('**', '').strip()


def first_value(data: dict, *keys: str):
    for key in keys:
        value = data.get(key)
        if value not in (None, '', [], {}):
            return value
    return None


def normalize_checklist(value) -> list[str]:
    """문자열·객체 등 어떤 형태든 체크리스트 문자열 목록으로 정리하고 최대 5개로 제한한다."""
    if isinstance(value, str):
        value = re.split(r'\n+', value)
    items, seen = [], set()
    for raw in as_list(value):
        text = clean_text(raw)
        text = re.sub(r'^\s*(?:[-•*·✓✔☑□]+|\d+[.)])\s*', '', text).strip()
        key = re.sub(r'\s+', '', text)
        if len(key) >= 4 and key not in seen:
            seen.add(key)
            items.append(text)
    return items[:CHECKLIST_MAX]


def split_sentences(text: str) -> list[str]:
    return [part.strip() for part in re.split(r'(?<=[.!?。…])\s+|\n+', str(text or '').strip()) if part.strip()]


def normalize_conclusion(value) -> str:
    """본문 내용을 바꾸지 않고 줄바꿈 정리와 4문장 초과분 제거만 수행한다."""
    return ' '.join(split_sentences(clean_text(value))[:CONCLUSION_MAX_SENTENCES])


def conclusion_error(text: str) -> str | None:
    text = str(text or '').strip()
    if not text:
        return ERR_CONCLUSION_MISSING
    count = len(split_sentences(text))
    if len(text) < CONCLUSION_MIN_CHARS or not CONCLUSION_MIN_SENTENCES <= count <= CONCLUSION_MAX_SENTENCES:
        return ERR_CONCLUSION_WEAK
    return None


def normalize_sections(value) -> list[dict]:
    sections = []
    for raw in as_list(value):
        if not isinstance(raw, dict):
            continue
        heading = clean_text(first_value(raw, 'heading', 'title', 'subtitle', '소제목'))
        body = clean_text(first_value(raw, 'body', 'content', 'text', '본문'))
        if heading and body:
            sections.append({'heading': heading, 'body': body})
    return sections


def normalize_tags(value) -> list[str]:
    if isinstance(value, str):
        value = re.split(r'[,\n]+', value)
    tags, seen, total = [], set(), 0
    for raw in as_list(value):
        for part in re.split(r'[,\n]+', clean_text(raw)):
            tag = part.strip().lstrip('#').strip()
            if tag and tag not in seen and total + len(tag) <= 180:  # Blogger 라벨 길이 제한 대비
                seen.add(tag)
                tags.append(tag)
                total += len(tag) + 1
    return tags[:8]


def normalize_article(raw: dict) -> dict:
    """AI 응답을 렌더링과 검수에 안전한 구조로 바꾼다. 자료 확인일은 AI 값을 쓰지 않는다."""
    return {
        'title': clean_text(first_value(raw, 'title', '제목')).replace('\n', ' '),
        'description': clean_text(first_value(raw, 'description', 'meta_description', '설명')).replace('\n', ' '),
        'intro': clean_text(first_value(raw, 'intro', 'introduction', '도입부')).replace('\n', ' '),
        'sections': normalize_sections(first_value(raw, 'sections', 'body', '본문')),
        'checklist': normalize_checklist(first_value(raw, 'checklist', 'check_list', 'checklists', 'action_items', '체크리스트')),
        'conclusion': normalize_conclusion(first_value(raw, 'conclusion', 'summary', '결론', '마무리')),
        'tags': normalize_tags(first_value(raw, 'tags', 'labels', '태그')),
        'last_checked': TODAY,
    }


def write_article(topic: dict, research: dict, category: str | None = None) -> dict:
    sources = research.get('_sources', [])
    # Never send the full Tavily payload to the writer: keep only concise verified facts
    # and source metadata so the prompt stays comfortably below Groq's free-tier TPM cap.
    compact_research = {
        'research_summary': str(research.get('research_summary', ''))[:1800],
        'facts': [
            {'claim': str(x.get('claim', ''))[:300], 'detail': str(x.get('detail', ''))[:500],
             'source_hint': str(x.get('source_hint', ''))[:120]}
            for x in as_list(research.get('facts'))[:6] if isinstance(x, dict)
        ],
        'cautions': [str(x)[:250] for x in as_list(research.get('cautions'))[:5]],
        'how_to_check': str(research.get('how_to_check', ''))[:500],
    }
    compact_topic = {
        'topic': str(topic.get('topic', ''))[:200],
        'search_intent': str(topic.get('search_intent', ''))[:300],
        'reader_benefit': str(topic.get('reader_benefit', ''))[:300],
        'outline': [str(x)[:100] for x in as_list(topic.get('outline'))[:5]],
    }
    source_lines = '\n'.join(f"- {str(s.get('title', ''))[:180]} | {s.get('url', '')}" for s in sources[:8])
    prompt = f'''너는 한국의 실용적인 생활정보 전문 에디터다. 아래 조사 결과와 출처만 근거로 블로그 글을 작성한다.
오늘 날짜: {TODAY}
카테고리: {category or weighted_category()}
주제 및 독자 의도: {json.dumps(compact_topic, ensure_ascii=False)}
검증된 조사 결과: {json.dumps(compact_research, ensure_ascii=False)}
확보된 출처 목록:
{source_lines}

JSON 객체만 출력. 아래 키를 하나도 빠뜨리지 말 것:
{{"title":"검색 의도가 명확한 제목(12~60자)","description":"검색 결과용 100~150자 설명","intro":"도입부 2~3문장","sections":[{{"heading":"소제목","body":"본문 문단"}}],"checklist":["실천 항목1","실천 항목2","실천 항목3"],"conclusion":"본문을 요약한 2~4문장","tags":["태그1","태그2"]}}

작성 규칙:
1. 1,200~1,800자 내외의 구체적이고 실용적인 글. 억지로 분량을 채우지 말 것.
2. 소제목(sections)은 3개 이상, checklist는 3~5개, conclusion은 2~4문장으로 반드시 작성할 것.
3. 독자가 실제로 따라 할 수 있는 순서, 조건, 확인 경로를 포함할 것.
4. 조사 결과에 없는 수치·날짜·기관 입장·정책·법률 해석·혜택을 만들지 말 것. 금액·비율을 직접 계산해 새로 쓰지 말 것.
5. 대상과 예외 조건을 생략하지 말 것. 불확실한 사항은 확인 필요라고 쓸 것.
6. 특정 업체/상품 광고처럼 쓰지 말고 장단점과 주의점을 균형 있게 설명할 것.
7. 건강·법률·세금·투자 등 고위험 주제는 일반 정보로 한정하고 전문가/공식기관 확인을 안내할 것.
8. 제목 낚시, 공포 조장, 과장, 반복 문장, AI식 상투어 금지.
9. 출처를 본문에서 언급할 때는 확보된 출처에 한정할 것. HTML 태그/마크다운 금지.
'''
    article = normalize_article(groq_json(prompt, temperature=0.55, max_tokens=ARTICLE_MAX_TOKENS))
    article['_sources'] = sources
    article['_research'] = research
    article['_topic'] = topic
    return article


def repair_article(article: dict, errors: list[str]) -> dict:
    """체크리스트·결론만 보완한 새 객체를 돌려준다. 다른 필드는 절대 변경하지 않는다."""
    need_checklist = ERR_CHECKLIST in errors
    need_conclusion = ERR_CONCLUSION_MISSING in errors or ERR_CONCLUSION_WEAK in errors
    repaired = dict(article)
    repaired['last_checked'] = TODAY
    if not need_checklist and not need_conclusion:
        return repaired
    context = {
        'title': article.get('title', ''),
        'intro': str(article.get('intro', ''))[:300],
        'sections': [{'heading': s.get('heading', ''), 'body': str(s.get('body', ''))[:450]} for s in article.get('sections', [])[:6]],
        'facts': [{'claim': str(f.get('claim', ''))[:150], 'detail': str(f.get('detail', ''))[:200]}
                  for f in as_list((article.get('_research') or {}).get('facts'))[:6] if isinstance(f, dict)],
        'current_checklist': article.get('checklist', []),
        'current_conclusion': article.get('conclusion', ''),
    }
    requested = {}
    rules = []
    if need_checklist:
        requested['checklist'] = ['실천 항목']
        rules.append(f'checklist: 본문에 나온 행동을 기존 항목과 합쳐 {CHECKLIST_MIN}~{CHECKLIST_MAX}개가 되도록 작성한다. 기존 항목과 중복하지 않는다.')
    if need_conclusion:
        requested['conclusion'] = '본문을 요약한 결론'
        rules.append(f'conclusion: 본문을 요약해 {CONCLUSION_MIN_SENTENCES}~{CONCLUSION_MAX_SENTENCES}문장, {CONCLUSION_MIN_CHARS}자 이상으로 작성한다.')
    prompt = f'''아래 글에서 부족한 항목만 보완하라. 글의 다른 부분은 다시 쓰지 않는다.
보완 규칙:
- {chr(10).join('- ' + rule for rule in rules)[2:]}
- 아래 제공된 내용에 있는 정보만 사용한다. 새로운 사실, 정책, 혜택, 기관, 출처, URL을 추가하지 않는다.
- 제공된 내용에 없는 숫자(금액, 비율, 날짜, 개수, 기간)는 쓰지 않는다.
- HTML 태그와 마크다운을 쓰지 않는다.
제공 내용: {json.dumps(context, ensure_ascii=False)}
JSON만 출력: {json.dumps(requested, ensure_ascii=False)}'''
    result = groq_json(prompt, temperature=0.2, max_tokens=REPAIR_MAX_TOKENS, output_retries=0)

    allowed = visible_article_text(article) + ' ' + source_corpus(article.get('_sources', []))
    facts_text = ' '.join(f"{f.get('claim', '')} {f.get('detail', '')}" for f in as_list((article.get('_research') or {}).get('facts')) if isinstance(f, dict))
    allowed_numbers = plain_numbers(allowed + ' ' + facts_text)
    if need_checklist:
        new_items = []
        for item in normalize_checklist(result.get('checklist')):
            extra = plain_numbers(item) - allowed_numbers
            if extra:
                print(f'[보완 항목 제외] 근거에 없는 숫자 {sorted(extra)}: {item[:60]}')
            else:
                new_items.append(item)
        repaired['checklist'] = normalize_checklist(list(article.get('checklist', [])) + new_items)
    if need_conclusion:
        conclusion = normalize_conclusion(result.get('conclusion'))
        extra = plain_numbers(conclusion) - allowed_numbers
        if extra:
            print(f'[보완 결론 제외] 근거에 없는 숫자 {sorted(extra)}')
        elif conclusion:
            repaired['conclusion'] = conclusion
    return repaired


NUMBER = r'\d(?:[\d,]*\d)?(?:\.\d+)?'
MONEY_UNIT = r'조|억|천만|백만|만|천|백'
MONEY_RE = re.compile(rf'((?:{NUMBER}\s*(?:{MONEY_UNIT})?\s*){{1,4}})원(?!칙|인|래|본|리|론)')
PERCENT_RE = re.compile(rf'({NUMBER})\s*(?:%|％|퍼센트)')
PERCENT_RANGE_RE = re.compile(rf'({NUMBER})\s*[~∼\-–]\s*{NUMBER}\s*(?:%|％|퍼센트)')
YEAR_MONTH_RE = re.compile(r'(?<!\d)(20\d{2})\s*(?:년|[.\-/])\s*(\d{1,2})(?!\d)(?!\s*%)')
YEAR_RE = re.compile(r'(?<!\d)(20\d{2})년')
MONTH_RE = re.compile(r'(?<!\d)(\d{1,2})월')
UNIT_SCALE = {'조': 10 ** 12, '억': 10 ** 8, '천만': 10 ** 7, '백만': 10 ** 6, '만': 10 ** 4, '천': 10 ** 3, '백': 10 ** 2, '': 1}


def _money_value(text: str) -> int:
    total = 0.0
    for number, unit in re.findall(rf'({NUMBER})\s*({MONEY_UNIT})?', text):
        total += float(number.replace(',', '')) * UNIT_SCALE[unit]
    return int(round(total))


def _money_values(text: str) -> dict[int, str]:
    return {_money_value(match.group(1)): match.group(0).strip() for match in MONEY_RE.finditer(text)}


def _evidence_money(text: str) -> set[int]:
    values = set(_money_values(text))
    # "1만~3만원"처럼 범위 앞쪽에 '원'이 없는 금액과 1,000 이상의 숫자도 근거로 인정한다.
    for number, unit in re.findall(rf'({NUMBER})\s*({MONEY_UNIT})?', text):
        value = int(round(float(number.replace(',', '')) * UNIT_SCALE[unit]))
        if unit or value >= 1000:
            values.add(value)
    return values


def _year_months(text: str) -> dict[tuple[int, int], str]:
    found = {}
    for match in YEAR_MONTH_RE.finditer(text):
        year, month = int(match.group(1)), int(match.group(2))
        if 1 <= month <= 12:
            found[(year, month)] = match.group(0).strip()
    return found


def source_corpus(sources) -> str:
    return ' '.join(f"{s.get('title', '')} {s.get('content', '')}" for s in as_list(sources) if isinstance(s, dict))


def find_unsupported_figures(text: str, corpus: str) -> list[str]:
    """글의 금액·비율·연월이 실제 검색자료에 없으면 그 표현을 돌려준다.

    요약문처럼 AI가 쓴 내용은 근거로 삼지 않고, Tavily가 돌려준 원문만 대조한다.
    """
    unsupported = []
    evidence_money = _evidence_money(corpus)
    unsupported += [label for value, label in _money_values(text).items() if value not in evidence_money]

    evidence_percent = {float(x.replace(',', '')) for x in PERCENT_RE.findall(corpus) + PERCENT_RANGE_RE.findall(corpus)}
    unsupported += [f'{x}%' for x in PERCENT_RE.findall(text) if float(x.replace(',', '')) not in evidence_percent]

    current_year = int(TODAY[:4])
    evidence_ym = set(_year_months(corpus))
    evidence_months = {int(x) for x in MONTH_RE.findall(corpus)}
    evidence_years = {int(x) for x in re.findall(r'(?<!\d)(20\d{2})(?!\d)', corpus)}
    months_in_text = _year_months(text)
    for (year, month), label in months_in_text.items():
        if (year, month) not in evidence_ym and not (year == current_year and month in evidence_months):
            unsupported.append(label)
    for raw_year in YEAR_RE.findall(text):
        year = int(raw_year)
        if year != current_year and year not in evidence_years:
            unsupported.append(f'{year}년')
    return sorted(set(unsupported))


def plain_numbers(text: str) -> set[str]:
    return {x.replace(',', '') for x in re.findall(NUMBER, str(text or ''))}


def visible_article_text(article: dict) -> str:
    sections = article.get('sections') if isinstance(article.get('sections'), list) else []
    checklist = article.get('checklist') if isinstance(article.get('checklist'), list) else []
    parts = [article.get('title', ''), article.get('description', ''), article.get('intro', '')]
    parts += [f"{s.get('heading', '')} {s.get('body', '')}" for s in sections if isinstance(s, dict)]
    parts += [str(item) for item in checklist] + [article.get('conclusion', '')]
    return ' '.join(str(x) for x in parts)


def source_host(url: str) -> str:
    try:
        host = (urlparse(str(url)).hostname or '').lower()
    except ValueError:
        return ''
    return host[4:] if host.startswith('www.') else host


def is_trusted_host(host: str) -> bool:
    if host.endswith(TRUSTED_SUFFIXES):
        return True
    return any(host == domain or host.endswith('.' + domain) for domain in TRUSTED_DOMAINS)


def evidence_errors(sources: list, facts: list) -> list[str]:
    """글을 쓰기 전에도 확인할 수 있는 출처·사실 기준."""
    errors = []
    sources = [s for s in as_list(sources) if isinstance(s, dict)]
    if len(sources) < 2:
        errors.append('검색 근거 출처가 2개 미만')
    hosts = {source_host(s.get('url', '')) for s in sources if s.get('url')} - {''}
    if len(hosts) < 2:
        errors.append('서로 다른 출처 도메인이 2개 미만')
    if not any(is_trusted_host(host) for host in hosts):
        errors.append('정부·공공기관 등 1차 출처를 확인하지 못함')
    if len(as_list(facts)) < 2:
        errors.append('검증된 핵심 사실이 2개 미만')
    if any(not str(s.get('url', '')).startswith('https://') for s in sources):
        errors.append('HTTPS가 아닌 출처 포함')
    return errors


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
    if len(title) < 12 or len(title) > 60:
        errors.append('제목 길이 기준 미달(12~60자)')
    if len(desc) < 40 or len(desc) > 180:
        errors.append('검색 설명 길이 기준 미달(40~180자)')
    if len(intro) < 50:
        errors.append('도입부가 너무 짧음')
    if not isinstance(sections, list) or len(sections) < 3:
        errors.append('본문 소제목이 3개 미만')
    elif any(not isinstance(s, dict) or not str(s.get('heading', '')).strip() or not str(s.get('body', '')).strip() for s in sections):
        errors.append('비어 있는 본문 소제목 포함')
    if not isinstance(checklist, list) or len([x for x in checklist if str(x).strip()]) < CHECKLIST_MIN:
        errors.append(ERR_CHECKLIST)
    errors += evidence_errors(sources, facts)
    problem = conclusion_error(article.get('conclusion'))
    if problem:
        errors.append(problem)
    if article.get('last_checked') != TODAY:
        errors.append('자료 확인일이 오늘 날짜와 다름')
    safe_sections = sections if isinstance(sections, list) else []
    body_text = ' '.join([intro] + [str(x.get('body', '')) for x in safe_sections if isinstance(x, dict)] + [str(article.get('conclusion', ''))])
    if len(body_text) < MIN_BODY_CHARS:
        errors.append(ERR_BODY_SHORT)
    if re.search(r'\b(무조건|100% 보장|반드시 수익|확실하게 돈 버는)\b', body_text):
        errors.append('과장·보장 표현 발견')
    unsupported = find_unsupported_figures(visible_article_text(article), source_corpus(sources))
    if unsupported:
        errors.append(f"검색자료에서 확인되지 않은 수치 포함: {', '.join(unsupported[:8])}")
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
    global _blogger_service
    if _blogger_service is not None:
        return _blogger_service
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    try:
        info = json.loads(need('BLOGGER_TOKEN_JSON'))
    except json.JSONDecodeError as exc:
        raise RuntimeError('BLOGGER_TOKEN_JSON이 올바른 JSON 형식이 아닙니다.') from exc
    try:
        creds = Credentials.from_authorized_user_info(info, scopes=['https://www.googleapis.com/auth/blogger'])
    except ValueError as exc:
        raise RuntimeError('BLOGGER_TOKEN_JSON에 필요한 항목이 없습니다. refresh_token, client_id, client_secret을 확인하세요.') from exc
    if not creds.valid and creds.refresh_token:
        try:
            creds.refresh(Request())
        except RefreshError as exc:
            raise RuntimeError('Blogger OAuth 토큰을 갱신하지 못했습니다. 토큰이 만료되었거나 취소되었는지 확인하세요.') from exc
    if not creds.valid:
        raise RuntimeError('Blogger OAuth 토큰이 유효하지 않습니다. refresh token을 확인하세요.')
    _blogger_service = build('blogger', 'v3', credentials=creds, cache_discovery=False)
    return _blogger_service


def _blog_key(url: str) -> str:
    # Blogger가 http 주소를 돌려줘도 https로 설정한 BLOG_URL과 맞도록 호스트와 경로만 비교한다.
    parsed = urlparse(str(url).strip())
    return f"{(parsed.netloc or '').lower()}{parsed.path.rstrip('/')}"


def find_target_blog(service) -> dict:
    blogs = service.blogs().listByUser(userId='self').execute().get('items', [])
    blog = next((b for b in blogs if _blog_key(b.get('url', '')) == _blog_key(BLOG_URL)), None)
    if not blog:
        raise RuntimeError(f'Blogger 계정에서 대상 블로그를 찾지 못했습니다: {BLOG_URL}')
    return blog


def normalized_title(title: str) -> str:
    return re.sub(r'\s+', '', str(title or '')).lower()


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
    blog = find_target_blog(service)
    existing = set()
    token = None
    for _ in range(5):
        args = {'blogId': blog['id'], 'fetchBodies': True, 'maxResults': 100, 'status': ['LIVE']}
        if token:
            args['pageToken'] = token
        result = service.posts().list(**args).execute()
        for post in result.get('items', []):
            body = post.get('content', '') or ''
            existing.update(re.findall(r'LIFE_INFO_SIGNATURE:([a-f0-9]{24})', body))
            existing.add('TITLE:' + normalized_title(post.get('title', '')))
        token = result.get('nextPageToken')
        if not token:
            break
    if signature in existing or 'TITLE:' + normalized_title(article['title']) in existing:
        print('[중복 발행 방지] 동일한 글이 이미 존재합니다.')
        return
    # 게시 요청은 중복 발행을 막기 위해 자동으로 재시도하지 않는다.
    posted = service.posts().insert(
        blogId=blog['id'],
        body={'kind': 'blogger#post', 'title': article['title'], 'content': content, 'labels': article.get('tags', [])[:8]},
        isDraft=False,
    ).execute()
    print(f"[발행 완료] {article['title']} status={posted.get('status', '확인 불가')} url={posted.get('url', '확인 불가')}")


def print_usage() -> None:
    print(f"[Groq 사용량] 호출 {_groq_state['calls']}회, 토큰 {_groq_state['tokens']}개, 429 대기 {_rate_wait_total:.0f}초")


def report_failure(errors: list[str]) -> None:
    print('[품질 검수 실패] 발행 중단')
    for error in errors:
        print(' - ' + error)


def check_with_repair(article: dict) -> dict:
    """1차 검수 → 필요 시 1회 보완 → 2차 검수. 통과한 글만 돌려준다."""
    article['last_checked'] = TODAY
    errors = quality_check(article)
    if errors:
        print('[1차 품질 검수] 부족한 항목 발견')
        for error in errors:
            print(' - ' + error)
        repairable = [e for e in errors if e in REPAIRABLE_ERRORS]
        others = [e for e in errors if e not in REPAIRABLE_ERRORS]
        # 분량 계산에는 결론이 포함되므로, 결론을 보완하면 해결될 수 있는 경우만 보완을 시도한다.
        if ERR_BODY_SHORT in others and (ERR_CONCLUSION_MISSING in errors or ERR_CONCLUSION_WEAK in errors):
            current = len(str(article.get('intro', ''))) + sum(len(str(x.get('body', ''))) for x in article.get('sections', []) if isinstance(x, dict))
            if current >= MIN_BODY_CHARS - MAX_CONCLUSION_GAIN:
                others.remove(ERR_BODY_SHORT)
        if repairable and others:
            print('[자동 보완 생략] 보완 대상이 아닌 문제가 있어 2차 검수를 통과할 수 없으므로 API를 호출하지 않습니다.')
        elif repairable:
            print('[자동 보완] Groq 보완 요청 1회 실행')
            try:
                article = repair_article(article, errors)
            except RuntimeError as exc:
                print(f'[자동 보완 실패] {exc}')
            article['last_checked'] = TODAY
            errors = quality_check(article)
    if errors:
        report_failure(errors)
        raise RuntimeError('품질 검수 기준을 통과하지 못해 발행하지 않았습니다.')
    return article


def main():
    if TASK not in ('auto', 'living', 'finance', 'travel', 'economy'):
        raise ValueError('TASK는 auto/living/finance/travel/economy 중 하나여야 합니다.')
    need('GROQ_API_KEY')
    need('TAVILY_API_KEY')
    if not DRY_RUN and BLOG_URL == '/':
        raise RuntimeError('BLOG_URL을 설정해야 실제 발행할 수 있습니다.')
    category = weighted_category()
    overrides = {'living': '생활비·절약', 'finance': '금융·돈 관리', 'travel': '여행·소비경제', 'economy': '물가·경제 흐름'}
    if TASK in overrides:
        category = overrides[TASK]
    print(f'[주제 탐색] category={category}, date={TODAY}')
    try:
        recent_titles = recent_published_titles() if not DRY_RUN else []
        if not DRY_RUN:
            print(f'[최근 발행 제목 확인] {len(recent_titles)}개 — 유사 주제 제외 적용')
        topic = research_topic(category, recent_titles)
        if not str(topic.get('topic') or '').strip() or not str(topic.get('search_intent') or '').strip():
            raise RuntimeError('주제 탐색 결과가 필수 항목을 충족하지 못했습니다.')
        print(f"[선정 주제] {topic['topic']}")
        research = research_facts(topic)
        # 출처나 사실이 부족하면 글 생성 토큰을 쓰기 전에 중단한다.
        errors = evidence_errors(research.get('_sources', []), research.get('facts', []))
        if errors:
            report_failure(errors)
            raise RuntimeError('품질 검수 기준을 통과하지 못해 발행하지 않았습니다.')
        article = check_with_repair(write_article(topic, research, category))
        print(f"[품질 검수 통과] sources={len(article['_sources'])}, facts={len(research.get('facts', []))}")
        publish(article, render_html(article))
    finally:
        print_usage()


if __name__ == '__main__':
    main()
