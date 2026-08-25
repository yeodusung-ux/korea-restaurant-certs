# -*- coding: utf-8 -*-
"""주소 → 지오코딩 질의문 만들기 (빌드·지오코더·재주입이 함께 쓰는 단 하나의 규칙).

★왜 모듈로 뺐나 — 캐시의 키가 곧 '지오코더에 보낸 질의문'이다.
  build_site.py 와 geocode.py 가 각자 문자열을 조립하면 한 글자만 달라도
  캐시가 통째로 빗나가 8.5만 건을 다시 받게 된다. 규칙은 여기 하나뿐이어야 한다.

★원본 주소의 시도 표기를 그대로 쓰면 안 된다(template.html 의 stripSido 와 같은 사유).
  실측 오염: '전남광주통합특별시'(6,111건) · '광주광역시광역시' · '부산광역시시' ·
  '경상북포' · '강원특별자치도원주시'(시군구가 붙어버림) · '강원'(약칭).
  그래서 앞머리를 버리고 정규화해 둔 시도·시군구로 다시 조립한다.
"""
import re

SIDO_STEM = ["서울", "부산", "대구", "인천", "광주", "대전", "울산", "세종", "경기", "강원",
             "충북", "충청북", "충남", "충청남", "전북", "전라북", "전남", "전라남",
             "경북", "경상북", "경남", "경상남", "제주"]

# 도로명 + 건물번호. '대로/로/길' + 숫자(-숫자)
RD = re.compile(r"([가-힣A-Za-z0-9]+(?:대?로|길))\s*(\d+(?:-\d+)?)")
# 지번 + 번지
JB = re.compile(r"([가-힣]+(?:동|리|가))\s*(\d+(?:-\d+)?)")


def strip_sido(addr):
    """주소 맨 앞의 시도 표기를 떼어낸다. '세종대로 110' 처럼 도로명이 시도명으로
    시작하는 경우는 건드리지 않는다(뒤에 로/길이 붙으면 도로명이다)."""
    t = (addr.split() or [""])[0]
    for st in SIDO_STEM:
        if t.startswith(st) and not re.search(r"[로길]", t[len(st):]):
            return addr[len(t):].strip()
    return addr


def geo_query(sido, sgg, addr):
    """지오코더에 보낼 질의문 = 캐시 키. template.html 의 mapQ() 와 같은 조립이다."""
    rest = strip_sido((addr or "").strip())
    if not rest:
        return (u"%s %s" % (sido, sgg)).strip()
    return (u"%s %s" % (sido, rest)) if rest.startswith(sgg) \
        else (u"%s %s %s" % (sido, sgg, rest))


def variants(q):
    """한 주소를 점점 헐겁게 만든 질의 후보들. 앞에서부터 시도하고 처음 맞는 것을 쓴다.

    ① 그대로  ② 괄호·층/호 같은 꼬리 제거  ③ 도로명+건물번호까지만
    ④ 지번+번지까지만  — ④까지 실패하면 그 주소는 포기한다(시군구 중심으로 때우지 않는다.
    '반경 1km' 를 내세우면서 몇 km 틀린 좌표를 섞으면 기능 자체가 거짓말이 된다)."""
    seen = []

    def put(v):
        v = re.sub(r"\s+", " ", v or "").strip()
        if v and v not in seen:
            seen.append(v)

    put(q)
    a = re.sub(r"\([^)]*\)", " ", q)                       # (연건동) 같은 법정동 괄호
    a = re.sub(r"\s+\S*(?:층|호|동\s*\d+호|지하\d*)\s*$", " ", a)   # 3층 · 101호 · 지하1
    put(a)
    m = RD.search(a)
    if m:
        put(a[:m.end()])
    else:
        m2 = JB.search(a)
        if m2:
            put(a[:m2.end()])
    return seen
