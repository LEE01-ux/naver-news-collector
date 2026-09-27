"""
네이버 뉴스 수집기
- 키워드별 최근 24시간 이내 뉴스 제목 + 링크 수집
- 참고: https://wonhwa.tistory.com/46
"""

import sys
import io
import requests
from bs4 import BeautifulSoup
import os
import time
import re
from datetime import datetime
import trafilatura
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

# ── 설정 ──────────────────────────────────────────────
# 단순 키워드는 문자열로, 여러 검색어를 하나의 폴더로 묶을 때는
# {"folder": "폴더명", "keywords": [...]} 형태로 지정
KEYWORDS = [
    "중소벤처기업부",
    "소상공인연합회",
    "전국상인연합회",
    "한국외식업중앙회",
    "한국프랜차이즈산업협회",
    "전국카페사장협동조합",
    "배달 사회적대화기구",
    "전국가맹점주협의회",
    "배달의민족",
    "동반성장위원회",
    {"folder": "규제", "keywords": ["배달앱 수수료", "온플법", "공정위 배달앱", "공정위 플랫폼"]},
]
OUTPUT_BASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")
MAX_PAGES = 3
SIMILARITY_THRESHOLD = 0.30  # 이 값 이상이면 같은 주제로 판단

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9",
    "Referer": "https://search.naver.com/",
}

EXCLUDE_TEXTS = {"관련뉴스", "전체보기", "네이버뉴스", "언론사 선정", "카드뉴스", "그래픽"}
NAVER_ARTICLE_RE = re.compile(r"https://n\.news\.naver\.com/mnews/article/\d+/\d+")

STOPWORDS = {
    "속보", "종합", "단독", "긴급", "전문", "프로필", "일문일답", "인터뷰",
    "포토", "영상", "상보", "종합2보", "종합3보",
    "이", "가", "을", "를", "은", "는", "의", "에", "로", "으로",
    "에서", "부터", "까지", "에게", "으로서", "와", "과", "및",
    "대한", "위한", "통해", "위해", "관련", "대해", "따른",
    "한다", "했다", "한다고", "했으며",
}

# 뉴스 제목에 자주 등장하는 한자 → 한글 변환
# 성씨·기관명 한자 표기를 통일해 토큰 불일치 방지
HANJA_MAP = {
    "李": "이", "姜": "강", "鄭": "정", "金": "김", "朴": "박",
    "靑": "청", "文": "문", "尹": "윤", "盧": "노", "崔": "최",
    "韓": "한", "趙": "조", "申": "신", "洪": "홍", "吳": "오",
    "徐": "서", "安": "안", "劉": "유", "蔡": "채", "梁": "양",
    "許": "허", "宋": "송", "全": "전", "權": "권", "黃": "황",
    "張": "장", "辛": "신", "羅": "나", "具": "구", "池": "지",
}

# 공백 없이 토큰 끝에 붙는 어미·조사 (긴 것부터 순서대로 — 먼저 매칭된 것 적용)
TRAILING_SUFFIXES = [
    "할듯이", "할듯", "듯이", "듯",
    "한다며", "했다며", "한다고", "했다고",
    "이라며", "이라고", "라며", "라고",
    "으로서", "에서의",
    "하는", "한다", "했다",
    "할",   # 지명할 → 지명
    "한",   # 발표한 → 발표
]

# 뉴스 성씨 한자가 역할명과 붙어있을 때 분리용
# 예: 이대통령 → 대통령, 강총리 → 총리
_SURNAME_PAT = re.compile(
    r"^[이강정김박문윤노최한조신홍오서안유채양허송전권황장나구지]"
    r"(대통령|총리|장관|의원|대표|위원장|차관|비서실장|후보자|후보)$"
)


# ── 유사도 ──────────────────────────────────────────────

def normalize_hanja(text: str) -> str:
    """한자를 대응하는 한글로 치환"""
    return "".join(HANJA_MAP.get(ch, ch) for ch in text)


def strip_trailing(token: str) -> str:
    """토큰 끝의 어미·조사 제거 (길이 2 이상 남을 때만 적용)"""
    for suffix in TRAILING_SUFFIXES:
        if token.endswith(suffix) and len(token) - len(suffix) >= 2:
            return token[: -len(suffix)]
    return token


def normalize_token(token: str) -> str:
    """성씨+직책이 붙은 토큰을 직책만으로 정규화
    예: 이대통령 → 대통령, 강총리 → 총리
    """
    m = _SURNAME_PAT.match(token)
    return m.group(1) if m else token


def tokenize(title: str) -> set:
    """제목 → 의미 토큰 집합

    처리 순서:
    1. 한자 → 한글 변환  (李대통령 → 이대통령)
    2. 대괄호 태그 제거  ([속보] 등)
    3. 말줄임·특수문자 공백화
    4. 공백 분리 후 각 토큰에서 어미·조사 제거  (지명할듯 → 지명할)
    5. 2글자 미만·숫자·불용어 필터
    """
    t = normalize_hanja(title)
    t = re.sub(r"[\[「【『\(][^\]」】』\)]{0,10}[\]」】』\)]", " ", t)
    t = re.sub(r"[…\.]{2,}", " ", t)
    t = re.sub(r"[^\w\s]", " ", t)

    result = set()
    for tok in t.split():
        tok = strip_trailing(tok)
        tok = normalize_token(tok)   # 이대통령 → 대통령
        if len(tok) < 2:
            continue
        if re.fullmatch(r"\d+[건보]?", tok):
            continue
        if tok in STOPWORDS:
            continue
        result.add(tok)
    return result


def title_similarity(t1: str, t2: str) -> float:
    """두 제목의 유사도 (0~1)

    Jaccard와 Containment를 병행:
    - Jaccard: 전체 토큰 집합 대비 교집합 비율
    - Containment: 짧은 제목의 토큰이 긴 제목에 얼마나 포함되는지
      → '속보' 기사와 '종합' 기사처럼 핵심어는 같고 표현만 다른 경우 포착
    - 최종 = max(Jaccard, Containment × 0.8)
    """
    s1, s2 = tokenize(t1), tokenize(t2)
    if not s1 or not s2:
        return 0.0
    inter = len(s1 & s2)
    jaccard = inter / len(s1 | s2)
    containment = inter / min(len(s1), len(s2))
    return max(jaccard, containment * 0.8)


def deduplicate_by_similarity(articles: list, threshold: float = SIMILARITY_THRESHOLD) -> tuple:
    """유사도 기반 중복 제거. (채택 목록, 제거된 목록) 반환"""
    accepted = []
    removed = []
    for art in articles:
        matched = next(
            (a for a in accepted if title_similarity(art["title"], a["title"]) >= threshold),
            None,
        )
        if matched:
            removed.append({"article": art, "duplicate_of": matched["title"]})
        else:
            accepted.append(art)
    return accepted, removed


# ── 파싱 ──────────────────────────────────────────────

def find_related_urls(fdr) -> set:
    """'관련뉴스 N건' 버튼이 있는 카드에서 대표기사 이후 기사들의 URL 수집.
    이 URL들은 수집 대상에서 제외한다.
    """
    related_urls = set()

    for node in fdr.find_all(string=re.compile(r"관련뉴스\s*\d+건")):
        container = node.parent
        for _ in range(12):
            if container is None:
                break
            ext_links = [
                a for a in container.find_all("a", href=True)
                if a.get("href", "").startswith("http")
                and "naver.com" not in a.get("href", "")
                and 10 < len(a.get_text(strip=True)) < 150
                and not any(ex in a.get_text(strip=True) for ex in EXCLUDE_TEXTS)
            ]
            if len(ext_links) >= 2:
                # 첫 번째(대표기사) 제외하고 나머지를 관련기사로 등록
                for a in ext_links[1:]:
                    related_urls.add(a.get("href", ""))
                break
            container = container.parent

    return related_urls


EXCLUDED_DOMAINS = {"naver.com", "navercorp.com", "naver.me"}

def is_valid_link(href: str, title: str) -> bool:
    if not href.startswith("http"):
        return False
    if not (10 < len(title) < 150):
        return False
    if any(ex in title for ex in EXCLUDE_TEXTS):
        return False
    is_naver_article = bool(NAVER_ARTICLE_RE.match(href))
    is_external = not any(domain in href for domain in EXCLUDED_DOMAINS)
    return is_external or is_naver_article


def parse_articles_from_html(html: str) -> list:
    """HTML에서 기사 수집 (3단계)
    1. 관련뉴스 카드에서 대표기사만 추출 (인라인 관련기사 제거)
    2. 외부 URL + n.news.naver.com 링크 수집
    3. 동일 URL/제목 중복 제거 (외부 URL 우선)
    """
    soup = BeautifulSoup(html, "lxml")
    fdr = soup.find(id=re.compile(r"^fdr-"))
    target = fdr if fdr else soup

    # 1단계: 관련뉴스 카드의 관련기사 URL 수집 → 제외 대상
    related_urls = find_related_urls(target)

    # 2단계: 전체 링크 수집
    candidates = []
    seen_urls = set()

    for a in target.find_all("a", href=True):
        href = a.get("href", "")
        title = a.get_text(strip=True)

        if not is_valid_link(href, title):
            continue
        if href in seen_urls:
            continue
        if href in related_urls:
            continue  # 관련뉴스 카드의 관련기사 제외

        seen_urls.add(href)
        candidates.append({
            "title": title,
            "link": href,
            "is_naver": bool(NAVER_ARTICLE_RE.match(href)),
        })

    # 3단계: 동일 제목(외부 URL vs naver URL) 중복 제거
    by_title: dict = {}
    for art in candidates:
        key = re.sub(r"\s+", " ", re.sub(r"[…\.]{2,}$", "", art["title"])).lower()
        if key not in by_title:
            by_title[key] = art
        elif by_title[key]["is_naver"] and not art["is_naver"]:
            by_title[key] = art  # 외부 URL 우선

    return list(by_title.values())


# ── 본문 수집 ──────────────────────────────────────────

def fetch_body(url: str, session: requests.Session) -> str:
    """기사 URL에서 본문 텍스트 추출.
    - n.news.naver.com: div#dic_area 셀렉터
    - 외부 언론사: trafilatura 자동 추출
    """
    try:
        resp = session.get(url, headers=HEADERS, timeout=10, verify=False)
        resp.raise_for_status()
    except Exception as e:
        return f"[본문 수집 실패: {e}]"

    # 네이버 뉴스 본문
    if "n.news.naver.com" in url:
        soup = BeautifulSoup(resp.text, "lxml")
        area = soup.select_one("div#dic_area")
        if area:
            return re.sub(r"\s+", " ", area.get_text(separator=" ", strip=True))
        return "[본문 영역을 찾을 수 없음]"

    # 외부 언론사: __NEXT_DATA__ fallback (Next.js SSR 페이지 대응)
    soup = BeautifulSoup(resp.text, "lxml")
    next_data = soup.find("script", id="__NEXT_DATA__")
    if next_data:
        try:
            import json as _json
            av = _json.loads(next_data.string)["props"]["pageProps"]["articleView"]
            parts = [item["content"] for item in av.get("contentArrange", []) if item.get("type") == "text"]
            if parts:
                return re.sub(r"\s+", " ", " ".join(parts).strip())
        except Exception:
            pass

    # 외부 언론사: trafilatura
    body = trafilatura.extract(
        resp.text,
        include_comments=False,
        include_tables=False,
        no_fallback=False,
    )
    if body:
        return re.sub(r"\s+", " ", body.strip())
    return "[본문 추출 실패]"


def fetch_bodies(articles: list, session: requests.Session) -> list:
    """수집된 기사 목록에 본문 추가"""
    total = len(articles)
    for i, art in enumerate(articles, 1):
        print(f"  본문 수집 [{i}/{total}] {art['title'][:40]}...")
        art["body"] = fetch_body(art["link"], session)
        time.sleep(0.3)
    return articles


def keyword_in_article(keyword: str, art: dict) -> bool:
    """키워드(공백 구분 단어 전체)가 제목 또는 본문에 하나라도 포함되는지 확인"""
    text = (art.get("title", "") + " " + art.get("body", "")).lower()
    return all(word.lower() in text for word in keyword.split())


# ── 수집 ──────────────────────────────────────────────

def fetch_articles(keyword: str, max_pages: int = MAX_PAGES) -> dict:
    """페이지 순회 수집 후 유사도 기반 최종 중복 제거.
    반환: {"articles": [...], "removed": [...]}
    """
    raw = []
    session = requests.Session()

    for page in range(max_pages):
        start = page * 10 + 1
        url = make_search_url(keyword, start)
        print(f"  [페이지 {page + 1}] 요청 중...")

        try:
            resp = session.get(url, headers=HEADERS, timeout=10)
            resp.raise_for_status()
        except Exception as e:
            print(f"  요청 실패: {e}")
            break

        page_articles = parse_articles_from_html(resp.text)
        existing_links = {a["link"] for a in raw}
        new = [a for a in page_articles if a["link"] not in existing_links]
        raw.extend(new)
        print(f"  → 관련뉴스 제거 후 {len(new)}건 (누적 {len(raw)}건)")

        if not new:
            break
        time.sleep(0.5)

    # 유사도 기반 2차 중복 제거
    articles, removed = deduplicate_by_similarity(raw)
    print(f"\n  유사도 중복 제거: {len(removed)}건 제거 → 최종 {len(articles)}건")

    # 본문 수집
    print(f"\n  본문 수집 시작...")
    articles = fetch_bodies(articles, session)

    # 키워드 미포함 기사 제거
    before = len(articles)
    articles = [a for a in articles if keyword_in_article(keyword, a)]
    filtered = before - len(articles)
    if filtered:
        print(f"  키워드 미포함 기사 제거: {filtered}건 → 최종 {len(articles)}건")

    return {"articles": articles, "removed": removed}


def make_search_url(keyword: str, start: int = 1) -> str:
    import urllib.parse
    q = urllib.parse.quote(keyword)
    return (
        "https://search.naver.com/search.naver"
        f"?ssc=tab.news.all&query={q}"
        "&sm=tab_opt&sort=0&pd=4&nso=so%3Ar%2Cp%3A1d"
        f"&start={start}"
    )


# ── 저장 ──────────────────────────────────────────────

def save_results(folder_name: str, keyword_label: str, result: dict) -> str:
    """folder_name: 저장 폴더명, keyword_label: 파일 내 표시용 키워드명"""
    articles = result["articles"]
    removed = result["removed"]

    safe_folder = re.sub(r'[\\/:*?"<>|]', "_", folder_name)
    folder = os.path.join(OUTPUT_BASE, safe_folder)
    os.makedirs(folder, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    txt_path = os.path.join(folder, f"{timestamp}.txt")

    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(f"키워드: {keyword_label}\n")
        f.write(f"수집 시각: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"수집 건수: {len(articles)} (중복 제거: {len(removed)}건)\n")
        f.write("=" * 60 + "\n\n")

        for i, art in enumerate(articles, 1):
            f.write(f"[{i}] {art['title']}\n")
            f.write(f"    링크: {art['link']}\n")
            f.write(f"    본문:\n")
            body = art.get("body", "")
            for line in [body[j:j+80] for j in range(0, len(body), 80)]:
                f.write(f"    {line}\n")
            f.write("\n")

        if removed:
            f.write("\n" + "=" * 60 + "\n")
            f.write("▼ 유사도 기준 제거된 기사 (참고용)\n")
            f.write("=" * 60 + "\n\n")
            for r in removed:
                f.write(f"  제거: {r['article']['title']}\n")
                f.write(f"  사유: '{r['duplicate_of'][:40]}...' 와 유사\n\n")

    return txt_path


def run_single_keyword(keyword: str) -> dict:
    """단일 키워드 수집 후 결과 반환 (folder_name, keyword_label, result)"""
    print(f"▶ 키워드: '{keyword}'")
    result = fetch_articles(keyword)
    return {"folder_name": keyword, "keyword_label": keyword, "result": result}


def run_grouped_keywords(group: dict) -> dict:
    """그룹 키워드: 여러 검색어를 수집해 하나의 폴더로 합산"""
    folder_name = group["folder"]
    keywords = group["keywords"]
    print(f"▶ 그룹: '{folder_name}' ({', '.join(keywords)})")

    merged_articles = []
    merged_removed = []
    seen_links = set()

    for kw in keywords:
        print(f"  검색어: '{kw}'")
        result = fetch_articles(kw)
        for art in result["articles"]:
            if art["link"] not in seen_links:
                seen_links.add(art["link"])
                merged_articles.append(art)
        merged_removed.extend(result["removed"])

    # 그룹 내 전체 기사 대상 유사도 재중복 제거
    merged_articles, extra_removed = deduplicate_by_similarity(merged_articles)
    merged_removed.extend(extra_removed)
    print(f"  그룹 최종: {len(merged_articles)}건 (그룹 내 중복 제거 포함)")

    return {
        "folder_name": folder_name,
        "keyword_label": f"{folder_name} ({', '.join(keywords)})",
        "result": {"articles": merged_articles, "removed": merged_removed},
    }


def main():
    os.makedirs(OUTPUT_BASE, exist_ok=True)
    print(f"수집 시작: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")

    for entry in KEYWORDS:
        if isinstance(entry, dict):
            item = run_grouped_keywords(entry)
        else:
            item = run_single_keyword(entry)

        articles = item["result"]["articles"]
        if not articles:
            print("  24시간 이내 관련 기사가 없습니다. 스킵.\n")
            continue

        path = save_results(item["folder_name"], item["keyword_label"], item["result"])
        print(f"  저장 완료: {path}\n")
        print("  ── 최종 수집 기사 ──")
        for i, art in enumerate(articles, 1):
            print(f"  [{i:2d}] {art['title']}")
            print(f"       {art['link']}")
        print()


if __name__ == "__main__":
    main()
