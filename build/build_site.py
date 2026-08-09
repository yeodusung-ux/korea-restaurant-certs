# -*- coding: utf-8 -*-
"""4개 공공데이터를 받아 조회용 index.html 을 만든다. (GitHub Actions 주 1회 실행)

  모범음식점   행정안전부/LOCALDATA   고정 URL, 인증 불필요
  착한가격업소 행정안전부/공공데이터포털 ★atchFileId 가 갱신마다 바뀌어 페이지에서 파싱
  백년가게     소상공인시장진흥공단     ODcloud API(키 필요) + 2022 파일본에서 업종·연락처 이식
  안심식당     농림축산식품부          MAFRA OpenAPI(별도 키 필요)

★설계 원칙 — 부분 실패는 전체 실패로 만든다.
  한 소스가 비면 그만큼이 조용히 사라진 사이트가 배포된다(안심식당만 42,630건).
  그래서 소스마다 최소 건수를 두고, 미달이면 예외를 던져 index.html 을 건드리지 않는다.
  워크플로가 실패로 표시되고 사이트는 직전 정상본을 유지한다.
"""
import collections, csv, io, json, os, re, sys, time, urllib.request, zlib

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")

# 소스별 최소 기대 건수(실측의 절반 수준) — 미달이면 수집이 깨진 것으로 본다
MIN_ROWS = {"모범음식점": 5000, "착한가격업소": 5000, "백년가게": 500,
            "안심식당": 20000, "관광공사 맛집": 5000, "식품안심업소": 3000}


def fetch(url, referer=None, retries=5, timeout=180):
    """gzip 대응 + 지수 백오프 재시도.

    ★국내 공공 서버는 해외(GitHub 러너)에서 느리고 간헐적으로 끊긴다.
      실측: LOCALDATA 17MB 다운로드가 90초 timeout 을 넘겨 3회 연속 실패했다.
      한 번 끊겼다고 주간 갱신을 통째로 날리지 않도록 넉넉히 기다렸다 다시 친다.
      대기 15→30→60→120초(합 ~4분). 스로틀이면 이 사이에 대개 풀린다.
    """
    last = None
    for n in range(retries):
        t0 = time.time()
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA, "Accept-Encoding": "gzip",
                **({"Referer": referer} if referer else {})})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    raw = zlib.decompress(raw, 16 + zlib.MAX_WBITS)
                return raw
        except Exception as e:                       # noqa: BLE001
            last = e
            if n == retries - 1:
                break
            wait = 15 * (2 ** n)
            print("   재시도 %d/%d (%.0f초 만에 실패) — %s → %d초 대기"
                  % (n + 1, retries, time.time() - t0, e, wait))
            sys.stdout.flush()
            time.sleep(wait)
    raise RuntimeError("수집 실패: %s — %s" % (url, last))


def csv_rows(raw, encodings=("utf-8-sig", "cp949")):
    for e in encodings:
        try:
            t = raw.decode(e)
            if "�" not in t:
                return list(csv.DictReader(io.StringIO(t)))
        except Exception:                            # noqa: BLE001
            pass
    return list(csv.DictReader(io.StringIO(raw.decode("cp949", "replace"))))


g = lambda r, k: (r.get(k) or "").strip()


def env_key(name):
    """시크릿 값에서 BOM·공백·따옴표를 걷어낸다.

    ★2026-08-09 실사고: PowerShell 파이프로 `gh secret set` 하면 값 앞에 U+FEFF 가 붙는다.
      그대로 URL 에 넣으면 urllib 이 'ascii codec can't encode \\ufeff' 로 죽는다.
      str.strip() 은 U+FEFF 를 공백으로 보지 않아 걸러지지 않는다 — 명시적으로 지운다.
    """
    v = os.environ.get(name, "")
    return v.replace("﻿", "").strip().strip('"').strip("'")

# ── 정규화 규칙 (로컬 검증본과 동일) ───────────────────────────────────
SIDO_FULL = {
    "서울": "서울특별시", "서울시": "서울특별시", "부산": "부산광역시", "대구": "대구광역시",
    "인천": "인천광역시", "광주": "광주광역시", "대전": "대전광역시", "울산": "울산광역시",
    "세종": "세종특별자치시", "세종시": "세종특별자치시", "경기": "경기도",
    "강원": "강원특별자치도", "충북": "충청북도", "충남": "충청남도",
    "전북": "전북특별자치도", "전남": "전라남도", "경북": "경상북도", "경남": "경상남도",
    "제주": "제주특별자치도",
    "강원도": "강원특별자치도", "전라북도": "전북특별자치도", "제주도": "제주특별자치도",
}
GWANGJU_GU = {"동구", "서구", "남구", "북구", "광산구"}


def norm_sido(sido, sgg):
    s = (sido or "").strip()
    if s == "전남광주통합특별시":                     # 원본 통합표기를 시군구로 되분리
        return "광주광역시" if sgg in GWANGJU_GU else "전라남도"
    return SIDO_FULL.get(s, s)


# ★'양장피'가 '양장(점)'에 걸려 중식당이 비요식으로 빠졌던 건 → 업종어는 '점'까지 붙여 좁힌다
NONFOOD_KWS = ["미용", "이용업", "이발", "헤어", "안경", "서적", "책방", "한복", "세탁",
               "목욕", "사우나", "찜질", "숙박", "여관", "모텔", "호텔", "사진", "인쇄",
               "도장", "침구", "가구", "철물", "약국", "문구", "화원", "꽃집", "자전거",
               "시계", "귀금속", "금은방", "양복점", "양장점", "구두", "제화", "표구",
               "공예", "비요식", "수선", "열쇠", "이·미용", "이미용"]
EXACT_MAP = {"회": "일식·회", "떡": "카페·제과", "빵": "카페·제과", "면": "면·분식"}
CAT_RULES = [
    ("뷔페",      ["뷔페", "부페", "샐러드바"]),
    ("카페·제과", ["카페", "커피", "제과", "제빵", "베이커리", "케이크", "다방", "찻집", "빙수"]),
    ("중식",      ["중식", "중국", "중화", "자장", "짜장", "짬뽕", "탕수육", "마라", "양장피"]),
    ("일식·회",   ["일식", "초밥", "스시", "생선회", "횟집", "회집", "활어", "물회", "사시미",
                  "장어", "복어", "참치", "돈까스", "돈가스", "우동", "덮밥", "회센타", "회센터"]),
    ("양식",      ["양식", "경양식", "스테이크", "파스타", "스파게티", "피자", "이탈리",
                  "프렌치", "햄버거", "브런치"]),
    ("고기·구이", ["식육", "구이", "갈비", "삼겹", "숯불", "정육", "곱창", "막창", "불고기",
                  "닭갈비", "오리", "샤브", "바베큐", "훈제", "족발", "보쌈", "육회"]),
    ("탕·국밥",   ["탕", "국밥", "설렁", "곰탕", "해장", "찌개", "전골", "추어", "삼계",
                  "매운탕", "지리", "죽", "순대"]),
    ("면·분식",   ["분식", "김밥", "떡볶이", "면류", "국수", "냉면", "칼국수", "막국수",
                  "라면", "만두", "수제비", "쫄면"]),
    ("한식",      ["한식", "한정식", "백반", "비빔밥", "쌈밥", "찜", "전문", "전통", "향토",
                  "산채", "정식", "생선", "해물", "해산물", "조개", "요식"]),
]
CAT_ORDER = [c for c, _ in CAT_RULES] + ["기타", "비요식"]
# 열거형 원본은 추측 없이 직접 매핑한다
CHAKHAN_MAP = {"한식": "한식", "중식": "중식", "일식": "일식·회", "양식": "양식",
               "기타요식업": "기타", "미용업": "비요식", "이용업": "비요식",
               "세탁업": "비요식", "숙박업": "비요식", "목욕업": "비요식",
               "기타비요식업": "비요식"}
ANSIM_MAP = {"한식": "한식", "일식": "일식·회", "중식": "중식", "서양식": "양식",
             "기타외국식": "기타", "기타 음식점업": "기타"}


def classify(*parts):
    for p in parts:
        if (p or "").strip() in EXACT_MAP:
            return EXACT_MAP[(p or "").strip()]
    t = "".join(parts).replace(" ", "")
    for k in NONFOOD_KWS:
        if k in t:
            return "비요식"
    for cat, kws in CAT_RULES:
        for k in kws:
            if k in t:
                return cat
    return "기타"


ODK = env_key("ODCLOUD_KEY")      # data.go.kr 계정 키 — 백년가게·관광공사가 함께 쓴다
if not ODK:
    raise RuntimeError("ODCLOUD_KEY 시크릿이 없다 (data.go.kr 일반 인증키)")

recs = []

# ── ⑤ 관광공사 맛집 (TourAPI) ─────────────────────────────────────────
# 「대한민국 구석구석」 편집 선정 음식점. data.go.kr 계정 키를 그대로 쓴다(활용신청만 별도).
#
# ★한도 설계 — 개발계정은 **하루 1,000콜**이다.
#   목록(areaBasedList2)은 1,000건씩 받아 14콜이면 전량이라 매주 통째로 갱신한다.
#   반면 대표메뉴·영업시간(detailIntro2)은 **종목당 1콜**이라 13,521콜 = 한도의 13배다.
#   → 매 실행 TOUR_DETAIL 건씩만 받아 캐시에 쌓는다(주 1회면 약 14주에 완성).
#     채워진 것부터 화면에 나오고, 캐시는 리포에 커밋돼 다음 주에 이어받는다.
print("⑤ 관광공사 맛집 수집")
TOUR = "https://apis.data.go.kr/B551011/KorService2"
# 상세 채움 건수. 주간 사이트 빌드는 0(캐시만 사용), 일일 캐시잡이 TOUR_DETAIL 을 준다.
TOUR_DETAIL = int(os.environ.get("TOUR_DETAIL", "0"))
CACHE_ONLY = bool(os.environ.get("CACHE_ONLY"))
CACHE_P = os.path.join(HERE, "tour_cache.json")

# 분류 코드 → 이름 (lclsSystmCode2 로 받은 정본, 21종. 추측 없음)
LCLS = {
    "FD010100": "관광식당", "FD010200": "모범음식점",
    "FD020100": "중식", "FD020200": "일식", "FD020300": "서양식",
    "FD020400": "기타외국식", "FD020500": "퓨전음식",
    "FD030100": "제과", "FD030200": "피자, 햄버거, 샌드위치 및 유사음식",
    "FD030300": "치킨", "FD030400": "김밥 분식", "FD030500": "이동음식",
    "FD030600": "기타간이음식",
    "FD040100": "바/펍", "FD040200": "생맥주전문점", "FD040300": "클럽",
    "FD040400": "전통주/민속주점", "FD040500": "기타주점",
    "FD050100": "카페", "FD050200": "찻집", "FD050300": "기타음료점",
}
# 코드가 열거형이라 대분류도 추측 없이 직접 매핑한다
LCLS_CAT = {
    "FD010100": "한식", "FD010200": "한식",
    "FD020100": "중식", "FD020200": "일식·회", "FD020300": "양식",
    "FD020400": "기타", "FD020500": "기타",
    "FD030100": "카페·제과", "FD030200": "양식", "FD030300": "기타",
    "FD030400": "면·분식", "FD030500": "기타", "FD030600": "기타",
    "FD040100": "기타", "FD040200": "기타", "FD040300": "비요식",
    "FD040400": "기타", "FD040500": "기타",
    "FD050100": "카페·제과", "FD050200": "카페·제과", "FD050300": "카페·제과",
}


def tour(path, **kw):
    q = "&".join("%s=%s" % (k, v) for k, v in kw.items())
    u = ("%s/%s?serviceKey=%s&MobileOS=ETC&MobileApp=certs&_type=json&%s"
         % (TOUR, path, ODK, q))
    return json.loads(fetch(u).decode("utf-8"))["response"]


tour_items, page = [], 1
while True:
    b = tour("areaBasedList2", numOfRows=1000, pageNo=page,
             contentTypeId=39, arrange="A")["body"]
    got = (b.get("items") or {}).get("item") or []
    if isinstance(got, dict):
        got = [got]
    tour_items.extend(got)
    if len(tour_items) >= int(b["totalCount"]) or not got:
        break
    page += 1
print("   목록 %s건 (%d콜)" % (format(len(tour_items), ","), page))

cache = {}
if os.path.exists(CACHE_P):
    try:
        cache = json.load(io.open(CACHE_P, encoding="utf-8"))
    except Exception:                                # noqa: BLE001
        cache = {}
todo = [i["contentid"] for i in tour_items if i["contentid"] not in cache]
print("   상세 캐시 %s / %s — 이번에 %d건 채움"
      % (format(len(cache), ","), format(len(tour_items), ","),
         min(len(todo), TOUR_DETAIL)))
# ★응답 값에 <br> 같은 태그가 그대로 들어온다("11:00~22:00<br>정확한 영업시간은 …").
#   그냥 두면 화면에 태그가 글자로 보인다.
TAG = re.compile(r"<[^>]{0,20}>")


def clean(v, lim):
    return TAG.sub(" / ", (v or "")).replace("&nbsp;", " ").strip(" /\t\r\n")[:lim]


def one_detail(cid):
    """상세 1건. 실패도 빈 값으로 기록한다 — 안 그러면 매주 같은 건에 계속 매달린다."""
    try:
        it = (tour("detailIntro2", contentId=cid, contentTypeId=39)["body"]
              .get("items") or {}).get("item") or []
        if isinstance(it, dict):
            it = [it]
        d0 = it[0] if it else {}
        return cid, {"m": clean(d0.get("firstmenu") or d0.get("treatmenu"), 120),
                     "o": clean(d0.get("opentimefood"), 60),
                     "r": clean(d0.get("restdatefood"), 40)}
    except Exception:                                # noqa: BLE001
        return cid, {"m": "", "o": "", "r": ""}


# ★병렬 금지 — 4스레드로 부르면 전량 HTTP 429 로 거부된다(2026-08-09 실측).
#   순차 ~2.7초/건이 이 API 의 사실상 상한이다. 그래서 한 번에 다 못 받고 나눠 받는다.
if todo:
    _t0 = time.time()
    for _i, cid in enumerate(todo[:TOUR_DETAIL], 1):
        cid, val = one_detail(cid)
        cache[cid] = val
        if _i % 200 == 0:
            print("      %d/%d (%.1f분)" % (_i, min(len(todo), TOUR_DETAIL), (time.time()-_t0)/60))
            sys.stdout.flush()
        time.sleep(0.05)
    print("   상세 %d건 완료 (%.1f분)" % (min(len(todo), TOUR_DETAIL), (time.time()-_t0)/60))

with io.open(CACHE_P, "w", encoding="utf-8") as f:
    f.write(json.dumps(cache, ensure_ascii=False, separators=(",", ":")))
if CACHE_ONLY:                     # 일일 캐시잡 — 사이트는 건드리지 않고 여기서 끝낸다
    print("캐시 전용 모드 종료 — %s / %s" % (format(len(cache), ","), format(len(tour_items), ",")))
    sys.exit(0)

for i in tour_items:
    addr = (i.get("addr1") or "").strip()
    t = addr.split()
    sgg = t[1] if len(t) >= 2 else "(미상)"
    code = (i.get("lclsSystm3") or "").strip()
    c = cache.get(i["contentid"], {})
    bits = [x for x in (c.get("m"), c.get("o") and "🕐 " + c["o"],
                        c.get("r") and "휴무 " + c["r"]) if x]
    recs.append(dict(ds="관광공사 맛집", name=(i.get("title") or "").strip(),
                     sido=norm_sido(t[0] if t else "", sgg), sgg=sgg,
                     cat=LCLS_CAT.get(code, "기타"), src=LCLS.get(code, "(미분류)"),
                     detail=" · ".join(bits), addr=addr,
                     tel=(i.get("tel") or "").strip(), date="",
                     # ★사진 URL 이 http 로 온다. https 사이트(Pages)에서는 혼합 콘텐츠로
                     #   차단되므로 https 로 바꾼다(호스트가 https 를 정상 지원함을 확인).
                     img=(i.get("firstimage2") or i.get("firstimage") or "")
                        .strip().replace("http://", "https://")))


# ── ① 모범음식점 — LOCALDATA 고정 URL ─────────────────────────────────
print("① 모범음식점 수집")
raw = fetch("https://file.localdata.go.kr/file/download/excellent_restaurant_info/info",
            referer="https://file.localdata.go.kr/file/excellent_restaurant_info/info")
rows = csv_rows(raw, ("cp949",))
live = [r for r in rows if g(r, "영업상태명") == "영업" and not g(r, "지정취소일자")]
print("   전체 %s → 유효 %s" % (format(len(rows), ","), format(len(live), ",")))
for r in live:
    t = (g(r, "소재지주소") or g(r, "도로명주소")).split()
    sgg = t[1] if len(t) >= 2 else "(미상)"
    typ, food = g(r, "음식의유형") or "(미분류)", g(r, "주된음식종류")
    recs.append(dict(ds="모범음식점", name=g(r, "업소명"),
                     sido=norm_sido(t[0] if t else "", sgg), sgg=sgg,
                     cat=classify(typ, food), src=typ, detail=food,
                     addr=g(r, "도로명주소") or g(r, "소재지주소"),
                     tel=g(r, "전화번호"), date=g(r, "지정일자")))
mo_total = len(rows)

# ── ② 착한가격업소 — ★atchFileId 를 페이지에서 매번 새로 찾는다 ────────
print("② 착한가격업소 수집")
page = fetch("https://www.data.go.kr/data/3045247/fileData.do").decode("utf-8", "replace")
m = re.search(r"(FILE_[0-9]+)", page)
if not m:
    raise RuntimeError("착한가격업소 atchFileId 를 찾지 못했다 — 페이지 구조가 바뀌었을 수 있다")
fid = m.group(1)
fname = re.search(r"착한가격업소[_ ]?현황[_ ]?(\d{8})", page)
print("   atchFileId=%s  버전=%s" % (fid, fname.group(1) if fname else "?"))
ch = csv_rows(fetch("https://www.data.go.kr/cmm/cmm/fileDownload.do"
                    "?atchFileId=%s&fileDetailSn=1" % fid,
                    referer="https://www.data.go.kr/data/3045247/fileData.do"), ("cp949",))
print("   %s건" % format(len(ch), ","))
for r in ch:
    menu = []
    for i in (1, 2, 3, 4):
        mn, pr = g(r, "메뉴%d" % i), g(r, "가격%d" % i)
        if mn:
            menu.append("%s %s" % (mn, format(int(pr), ",") + "원" if pr.isdigit() else pr))
    up = g(r, "업종")
    recs.append(dict(ds="착한가격업소", name=g(r, "업소명"),
                     sido=norm_sido(g(r, "시도"), g(r, "시군")), sgg=g(r, "시군"),
                     cat=CHAKHAN_MAP.get(up, classify(up)), src=up,
                     detail=" · ".join(menu), addr=g(r, "주소"),
                     tel=g(r, "연락처"), date=""))

# ── ③ 백년가게 — 2025 API 명단 + 2022 파일본에서 업종·연락처 이식 ──────
print("③ 백년가게 수집")
uddi = "uddi:82fc1cc1-f636-46fc-ae0d-b1f2da5052b4"
try:                                     # uddi 도 재발행 시 바뀔 수 있어 페이지에서 먼저 확인
    p2 = fetch("https://www.data.go.kr/data/15132695/fileData.do").decode("utf-8", "replace")
    mu = re.search(r"(uddi:[0-9a-f\-]{36})", p2)
    if mu:
        uddi = mu.group(1)
except Exception as e:                   # noqa: BLE001
    print("   uddi 확인 생략(알려진 값 사용): %s" % e)
by_new = json.loads(fetch("https://api.odcloud.kr/api/15132695/v1/%s"
                          "?page=1&perPage=3000&serviceKey=%s" % (uddi, ODK)
                          ).decode("utf-8-sig"))["data"]
print("   2025 명단 %s건" % format(len(by_new), ","))

# 2022 파일본은 더 갱신되지 않는 고정 자료라 리포에 넣어 두고 쓴다(외부 의존 제거)
by_old = csv_rows(open(os.path.join(HERE, "baengnyeon_2022.csv"), "rb").read(), ("cp949",))
norm_name = lambda s: re.sub(r"[\s()\-·,.]", "", (s or "")).lower()
idx = {}
for r in by_old:
    idx.setdefault(norm_name(g(r, "업체명")), []).append(r)

SGG2SIDO = {}
for r in recs:
    parts = (r["sgg"] or "").split()
    if parts:
        SGG2SIDO.setdefault(parts[0], r["sido"])

by_matched = 0
for n in by_new:
    name = (n.get("업체명") or "").strip()
    addr = (n.get("업체주소") or "").strip()
    t = addr.split()
    head = t[0] if t else ""
    if head and head not in SIDO_FULL and not head.endswith(
            ("특별시", "광역시", "도", "자치시", "자치도")) and head in SGG2SIDO:
        t = [SGG2SIDO[head]] + t                      # 시도 없이 시작하는 주소 구제
    sgg = t[1] if len(t) >= 2 else "(미상)"
    this_sido = norm_sido(t[0] if t else "", sgg)
    tel, biz = (n.get("연락처") or "").strip(), ""

    def loc_ok(c, _s=this_sido, _a=addr):
        # ★후보가 하나뿐일 때 지역 확인을 건너뛰면 동명이점이 엉뚱하게 붙는다
        if norm_sido(g(c, "시도"), g(c, "시군구")) != _s:
            return False
        base = g(c, "시군구").rstrip("시군구")
        return not base or base in _a

    mm = next((x for x in idx.get(norm_name(name), []) if loc_ok(x)), None)
    if mm:
        by_matched += 1
        biz = g(mm, "주요사업")
        tel = tel or g(mm, "연락처")
    recs.append(dict(ds="백년가게", name=name, sido=this_sido, sgg=sgg,
                     cat=classify(biz) if biz else "기타", src=biz or "(미상)",
                     detail=biz, addr=addr, tel=tel, date=""))
print("   2022 이식 %s건 (%.1f%%)" % (format(by_matched, ","), by_matched * 100.0 / max(1, len(by_new))))

# ── ④ 안심식당 — MAFRA OpenAPI 전량 페이징 ────────────────────────────
print("④ 안심식당 수집")
MFK = env_key("MAFRA_KEY")
if not MFK:
    raise RuntimeError("MAFRA_KEY 시크릿이 없다 (data.mafra.go.kr 인증키)")
SVC = "Grid_20200713000000000605_1"
BASE = "http://211.237.50.150:7080/openapi/%s/json/%s" % (MFK, SVC)
probe = json.loads(fetch(BASE + "/1/1").decode("utf-8-sig"))
if SVC not in probe:
    raise RuntimeError("안심식당 인증 실패: %s" % probe.get("result", {}).get("message"))
total = int(probe[SVC]["totalCnt"])
ans_all = []
for i in range(1, total + 1, 1000):
    j = min(i + 999, total)
    blk = json.loads(fetch("%s/%d/%d" % (BASE, i, j)).decode("utf-8-sig"))
    ans_all.extend(blk[SVC].get("row") or [])
    time.sleep(0.15)
# USE_YN 은 Y/N 인데 실제로 "여" 가 섞여 있다(전주시, 취소일 공백) → 유효로 본다
ans = [r for r in ans_all if (r.get("RELAX_USE_YN") or "").strip() in ("Y", "여")]
print("   전체 %s → 유효 %s" % (format(len(ans_all), ","), format(len(ans), ",")))
for r in ans:
    # ★필드명 함정: RELAX_SI_NM=시도, RELAX_SIDO_NM=시군구(이름과 반대)
    # ★RELAX_RSTRNT_REPRESENT(대표자 실명)는 개인정보라 읽지 않는다
    sgg = (r.get("RELAX_SIDO_NM") or "").strip() or "(미상)"
    kind = (r.get("RELAX_GUBUN_DETAIL") or "").strip() or "(미분류)"
    addr = " ".join(x for x in ((r.get("RELAX_ADD1") or "").strip(),
                                (r.get("RELAX_ADD2") or "").strip()) if x)
    recs.append(dict(ds="안심식당", name=(r.get("RELAX_RSTRNT_NM") or "").strip(),
                     sido=norm_sido((r.get("RELAX_SI_NM") or "").strip(), sgg), sgg=sgg,
                     cat=ANSIM_MAP.get(kind, "기타"), src=kind, detail="",
                     addr=addr, tel=(r.get("RELAX_RSTRNT_TEL") or "").strip(),
                     date=(r.get("RELAX_RSTRNT_REG_DT") or "").strip()))

# ── ⑥ 식품안심업소(구 위생등급제) ─────────────────────────────────────
# 식약처 지정. data.go.kr 은 LINK 창구일 뿐이고 실서버는 식품안전나라라 **별도 키**가 필요하다.
#
# ★이 출처만 3중 필터가 필요하다 — 다른 5종과 달리 **유효기간(ASGN_TO)** 이 있다.
#   실측에서 스타벅스 선릉로점이 당일 만료였다. 만료·지정취소·폐업을 안 거르면 죽은 인증이 뜬다.
# ★일반음식점만 + 프랜차이즈 제외(실측 37,996 → 17,481 → 11,408).
#   본사 일괄신청이라 메가커피 1,126·스타벅스 960 같은 체인이 43%였다.
# ⚠️ 이 데이터엔 **음식종류 필드가 없다.** 대분류는 상호명 추정이라 약 68%가 '기타'로 남는다.
print("⑥ 식품안심업소 수집")
MFDS = env_key("MFDS_KEY")
if not MFDS:
    raise RuntimeError("MFDS_KEY 시크릿이 없다 (식품안전나라 인증키)")
C004 = "http://openapi.foodsafetykorea.go.kr/api/%s/C004/json/%%d/%%d" % MFDS
TODAY = time.strftime("%Y%m%d")

_p = json.loads(fetch(C004 % (1, 1)).decode("utf-8"))["C004"]
if (_p.get("RESULT") or {}).get("CODE") not in (None, "INFO-000"):
    raise RuntimeError("식품안심업소 인증 실패: %s" % (_p.get("RESULT") or {}).get("MSG"))
hg_total = int(_p["total_count"])
hg_rows, _i = [], 1
while _i <= hg_total:
    _j = min(_i + 999, hg_total)
    _got = (json.loads(fetch(C004 % (_i, _j)).decode("utf-8"))["C004"].get("row")) or []
    if not _got:
        break
    hg_rows.extend(_got)
    _i = _j + 1
    time.sleep(0.1)

hg_live = [r for r in hg_rows
           if g(r, "INDUTY_NM") == "일반음식점"
           and not g(r, "ASGN_CANCEL_YMD") and not g(r, "CLSBIZ_DT")
           and (not g(r, "ASGN_TO") or g(r, "ASGN_TO") >= TODAY)]
_BR = re.compile(r"[\s()]*(\S{1,10})?(점|지점|본점|DT점)$")
_base = lambda x: _BR.sub("", (x or "").strip()) or x
_cnt = collections.Counter(_base(g(r, "BSSH_NM")) for r in hg_live)
_chain = set(k for k, v in _cnt.items() if v >= 5)
hg = [r for r in hg_live if _base(g(r, "BSSH_NM")) not in _chain]
print("   전체 %s → 일반음식점·유효 %s → 프랜차이즈 제외 %s"
      % (format(len(hg_rows), ","), format(len(hg_live), ","), format(len(hg), ",")))

for r in hg:
    # ★PRSDNT_NM(대표자 실명)은 개인정보라 읽지 않는다
    addr = g(r, "ADDR")
    t = addr.split()
    sgg = t[1] if len(t) >= 2 else "(미상)"
    ymd = g(r, "HG_ASGN_YMD")
    recs.append(dict(ds="식품안심업소", name=g(r, "BSSH_NM"),
                     sido=norm_sido(t[0] if t else "", sgg), sgg=sgg,
                     cat=classify(g(r, "BSSH_NM")), src="일반음식점", detail="",
                     addr=addr, tel=g(r, "TELNO"),
                     date=("%s-%s-%s" % (ymd[:4], ymd[4:6], ymd[6:])) if len(ymd) == 8 else ""))

# ── 수집 건전성 검사 — 하나라도 미달이면 index.html 을 건드리지 않는다 ──
cnt = collections.Counter(r["ds"] for r in recs)
short = {k: cnt[k] for k, lo in MIN_ROWS.items() if cnt[k] < lo}
if short:
    raise RuntimeError("수집량 미달 → 배포 중단: %s (기준 %s)" % (short, MIN_ROWS))
print("\n건전성 통과: %s" % dict(cnt))

# ── HTML 엔티티 되돌리기 ──────────────────────────────────────────────
# 원본 문자열에 엔티티가 그대로 들어 있다(안심식당 376건·모범음식점 24건, 전부 &amp;).
# 그냥 두면 화면에 '본죽&amp;비빔밥' 처럼 글자로 보인다.
# ★일부는 '&amp;amp;' 로 이중 인코딩돼 있어 한 번만 풀면 부족하다 → 안정될 때까지 반복.
import html as _html


def unesc(s):
    if not s or "&" not in s:
        return s
    for _ in range(3):
        n = _html.unescape(s)
        if n == s:
            break
        s = n
    return s


ent_fixed = 0
for r in recs:
    for k in ("name", "addr", "detail", "src", "sgg", "sido"):
        v = r[k]
        u = unesc(v)
        if u != v:
            r[k] = u
            ent_fixed += 1
print("HTML 엔티티 복원 %s개 필드" % format(ent_fixed, ","))

# ── 구(區) 추출 — 시도 → 시/군 → 구 3단계 선택용 ────────────────────
# 시군구는 시/군 레벨로 통일해 두었으므로(출처마다 세밀도가 달랐다) 구는 주소에서 따로 뽑는다.
# 광역시의 구는 이미 시군구 자리에 있어 여기서는 빈값이 된다.
for r in recs:
    _t = (r["addr"] or "").split()
    r["gu"] = _t[2] if (len(_t) >= 3 and _t[1].endswith("시") and _t[2].endswith("구")) else ""

# ── 시군구 표기 통일 ──────────────────────────────────────────────────
for r in recs:
    if r["sido"] == "세종특별자치시":
        r["sgg"] = "세종시"
known = collections.defaultdict(set)
for r in recs:
    if r["sgg"].endswith(("시", "군", "구")):
        known[r["sido"]].add(r["sgg"])
for r in recs:
    s, k = r["sido"], r["sgg"]
    if not k or k in known[s]:
        continue
    cand = next((k + suf for suf in ("시", "군", "구") if k + suf in known[s]), None) \
        or next((x for x in sorted(known[s], key=len, reverse=True) if k.startswith(x)), None)
    if cand:
        r["sgg"] = cand

# ── 동일 가게 매칭(교집합 모드용) ─────────────────────────────────────
RD = re.compile(r"([가-힣A-Za-z0-9]+(?:대?로|길))\s*(\d+(?:-\d+)?)")
NON = re.compile(r"[^0-9A-Za-z가-힣]")
BR = re.compile(r"(본점|직영점|지점|점)$")


def core(s):
    k = NON.sub("", s or "").lower()
    for _ in range(2):
        k = BR.sub("", k)
    return k


def similar(a, b):
    ca, cb = core(a), core(b)
    if not ca or not cb:
        return False
    if ca == cb:
        return True
    if len(ca) >= 3 and len(cb) >= 3 and (ca in cb or cb in ca):
        return True
    sa, sb = set(ca), set(cb)
    return min(len(ca), len(cb)) >= 4 and len(sa & sb) / float(len(sa | sb)) >= 0.75


par = list(range(len(recs)))


def find(x):
    while par[x] != x:
        par[x] = par[par[x]]
        x = par[x]
    return x


def union(a, b):
    ra, rb = find(a), find(b)
    if ra != rb:
        par[rb] = ra


bt, ba = collections.defaultdict(list), collections.defaultdict(list)
for i, r in enumerate(recs):
    d = re.sub(r"\D", "", r["tel"])
    if len(d) >= 9:
        bt[d].append(i)
    mm = RD.search(r["addr"])
    if mm:
        ba[(r["sgg"], mm.group(1), mm.group(2))].append(i)
for ix in bt.values():                # 한 번호에 수십 건 = 대표번호/오류 → 버림
    if len(ix) <= 8:
        for j in ix[1:]:
            union(ix[0], j)
for ix in ba.values():                # 같은 건물이라도 상호가 다르면 다른 가게
    if len(ix) <= 40:
        for x in range(len(ix)):
            for y in range(x + 1, len(ix)):
                if similar(recs[ix[x]]["name"], recs[ix[y]]["name"]):
                    union(ix[x], ix[y])

DS_ORDER = ["모범음식점", "착한가격업소", "백년가게", "안심식당", "관광공사 맛집", "식품안심업소"]
mem = collections.defaultdict(list)
for i in range(len(recs)):
    mem[find(i)].append(i)
gmask = []
for grp in mem.values():
    srcs = set(recs[i]["ds"] for i in grp)
    if len(srcs) < 2:
        continue
    msk = 0
    for s in srcs:
        msk |= 1 << DS_ORDER.index(s)
    gid = len(gmask)
    gmask.append(msk)
    for i in grp:
        recs[i]["gid"] = gid
multi = collections.Counter(bin(m).count("1") for m in gmask)
print("중복등재 그룹 %s개 — %s" % (format(len(gmask), ","), dict(sorted(multi.items()))))

# ── 직렬화 · 페이지 생성 ──────────────────────────────────────────────
sidos, sggs, cats, srcs, gus = [], [], [], [], [""]   # gus[0]="" = 구 없음
dss = DS_ORDER[:]


def ix_of(lst, v):
    if v not in lst:
        lst.append(v)
    return lst.index(v)


recs.sort(key=lambda r: (r["ds"], r["sido"], r["sgg"], r["name"]))
out = [[r["name"], ix_of(sidos, r["sido"]), ix_of(sggs, r["sgg"]), dss.index(r["ds"]),
        ix_of(cats, r["cat"]), ix_of(srcs, r["src"]), r["detail"], r["addr"],
        r["tel"], r["date"], r.get("gid", -1), r.get("img", ""),
        ix_of(gus, r.get("gu", ""))] for r in recs]

today = time.strftime("%Y-%m-%d", time.gmtime(time.time() + 9 * 3600))   # KST
DS_META = {
    "모범음식점":   {"n": cnt["모범음식점"], "date": today, "org": "행정안전부",
                  "note": "영업 중 + 지정취소 없음만"},
    "착한가격업소": {"n": cnt["착한가격업소"], "date": today, "org": "행정안전부",
                  "note": "메뉴·가격 포함, 비요식업 포함"},
    "백년가게":     {"n": cnt["백년가게"], "date": today, "org": "소상공인시장진흥공단",
                  "note": "명단 최신 API본 + 업종·연락처는 2022 파일본에서 이식"},
    "안심식당":     {"n": cnt["안심식당"], "date": today, "org": "농림축산식품부",
                  "note": "지정 유효분만 (취소분 제외)"},
    "관광공사 맛집": {"n": cnt["관광공사 맛집"], "date": today, "org": "한국관광공사",
                  "note": "구석구석 선정 · 사진·대표메뉴(점진 수집)"},
    "식품안심업소": {"n": cnt["식품안심업소"], "date": today, "org": "식품의약품안전처",
                  "note": "일반음식점·유효기간 내 · 프랜차이즈 제외 · 대분류는 상호명 추정"},
}
data = {"sido": sidos, "sgg": sggs, "ds": dss, "cat": cats, "src": srcs, "gu": gus, "rows": out,
        "cats": CAT_ORDER, "meta": DS_META, "total": len(out), "gmask": gmask,
        "built": today}

html = open(os.path.join(HERE, "template.html"), encoding="utf-8").read()
html = html.replace("/*__DATA__*/", json.dumps(data, ensure_ascii=False, separators=(",", ":")))
dst = os.path.join(ROOT, "index.html")
with open(dst, "w", encoding="utf-8") as f:
    f.write(html)
print("\nindex.html %s행 / %.2fMB 생성" % (format(len(out), ","), os.path.getsize(dst) / 1024.0 / 1024))
