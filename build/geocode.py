# -*- coding: utf-8 -*-
"""주소 → 위경도 캐시 채우기 (카카오 로컬 API).

원본 6종 어디에도 좌표가 없어서(모범음식점·착한가격업소 CSV 헤더까지 확인) '내 주변 반경'
검색을 하려면 좌표를 직접 만들어야 한다. 이 스크립트가 그 일만 한다 —
사이트도 데이터도 건드리지 않고 **build/geo_cache.csv 만 채운다.**

    python build/geocode.py                # 아직 좌표 없는 주소를 전부
    python build/geocode.py --limit 20000  # 그중 2만 건만(쿼터 나눠 쓰기)
    python build/geocode.py --retry-fail   # 지난번 실패분도 다시
    python build/geocode.py --self-test    # 키 없이 규칙만 점검

키:  환경변수 KAKAO_KEY  또는  build/kakao_key.txt (gitignore 됨)
     카카오 개발자 > 내 애플리케이션 > 앱 키 > **REST API 키**.
     로컬 API 무료 쿼터는 일 10만 건이라 8.5만 주소를 하루에 완주할 수 있다.

★캐시는 CSV(주소순 정렬)다. JSON 으로 두면 한 건만 늘어도 git 이 7MB 를 통째로 새로
  저장한다. 정렬된 CSV 는 줄 단위 델타가 먹어 리포가 덜 비대해진다.
★실패한 주소도 빈 좌표로 기록한다 — 안 그러면 돌릴 때마다 같은 주소를 또 물어보며
  쿼터만 태운다. 다시 시도하려면 --retry-fail.
"""
import argparse
import csv
import io
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from addr import geo_query, variants          # noqa: E402

# ★stdout 재포장은 직접 실행할 때만 한다. 모듈로 import 될 때 하면 부른 쪽(build_site·
#   rebuild_template)의 stdout 을 가로채고, 먼저 있던 래퍼가 GC 되며 버퍼를 닫아
#   'I/O operation on closed file' 로 빌드가 죽는다(실측).
if __name__ == "__main__":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
IDX = os.path.join(ROOT, "index.html")
CACHE = os.path.join(HERE, "geo_cache.csv")
API = "https://dapi.kakao.com/v2/local/search/address.json?"

N_SIDO, N_SGG, N_ADDR = 1, 2, 7            # index.html rows 의 열 번호


def key():
    k = (os.environ.get("KAKAO_KEY") or "").strip()
    if not k:
        p = os.path.join(HERE, "kakao_key.txt")
        if os.path.exists(p):
            k = io.open(p, encoding="utf-8-sig").read().strip()
    # BOM 이 섞이면 urllib 이 헤더를 만들다 ascii 오류로 죽는다(build_site.env_key 와 같은 함정)
    return k.replace(u"﻿", "").strip()


# ── 캐시 ──────────────────────────────────────────────────────────────
def load_cache():
    """{질의문: (lat, lng) 또는 None}. None = 지난번 실패."""
    c = {}
    if not os.path.exists(CACHE):
        return c
    with io.open(CACHE, encoding="utf-8", newline="") as f:
        for row in csv.reader(f):
            if not row or row[0] == "addr":
                continue
            q = row[0]
            if len(row) >= 3 and row[1] and row[2]:
                c[q] = (float(row[1]), float(row[2]))
            else:
                c[q] = None
    return c


def save_cache(c):
    """정렬해서 통째로 다시 쓴다(부분 갱신을 하면 순서가 깨져 델타가 안 먹는다).
    ★임시파일에 쓰고 바꿔치기 — 중간에 죽어도 8.5만 건짜리 캐시가 반쪽이 되지 않게."""
    tmp = CACHE + ".part"
    with io.open(tmp, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["addr", "lat", "lng"])
        for q in sorted(c):
            v = c[q]
            w.writerow([q, "%.6f" % v[0], "%.6f" % v[1]] if v else [q, "", ""])
    if os.path.exists(CACHE):
        os.remove(CACHE)
    os.rename(tmp, CACHE)


# ── 대상 주소 뽑기 ────────────────────────────────────────────────────
def targets():
    """index.html 에 박힌 데이터에서 질의문을 뽑는다.

    ★왜 build_site.py 를 안 거치나 — 지오코딩은 API 키 6종이 필요한 전체 수집과
      아무 상관이 없다. 이미 만들어진 사이트의 주소만 있으면 되고, 그래야 로컬에서도
      카카오 키 하나로 돌릴 수 있다. 주간 갱신으로 새 주소가 생기면 다음 실행이 줍는다."""
    if not os.path.exists(IDX):
        print("index.html 이 없다 — build_site.py 를 먼저 돌려야 한다.")
        return []
    s = io.open(IDX, encoding="utf-8").read()
    m = re.search(r"const D\s*=\s*", s)
    if not m:
        print("index.html 에서 데이터를 찾지 못했다(const D = ...).")
        return []
    d, _ = json.JSONDecoder().raw_decode(s, m.end())
    sido, sgg = d["sido"], d["sgg"]
    seen, out = set(), []
    for r in d["rows"]:
        q = geo_query(sido[r[N_SIDO]], sgg[r[N_SGG]], r[N_ADDR])
        if q and q not in seen:
            seen.add(q)
            out.append(q)
    return out


# ── 호출 ──────────────────────────────────────────────────────────────
class Quota(Exception):
    pass


# ★번지 없는 질의는 아예 보내지 않는다.
#   카카오는 '서울특별시 강남구' 같은 질의에도 **구청 좌표**를 성공으로 돌려준다(address_type
#   =REGION). 실측 2026-08-25: 주소칸이 비어 시도·시군구만 남은 행 1,000여 개가 강남구청·
#   제주시청 한 점에 쌓였다(강남구 473행). 그런 좌표로 '반경 1km' 를 말하면 거짓말이 된다.
#   → 번지가 없으면 실패로 두고 반경 검색에서 빼는 게 맞다.
HAS_NO = re.compile(r"\d")
# 지역·도로 대표점이 아니라 '진짜 주소'로 맞은 것만 받는다
OK_TYPES = ("ROAD_ADDR", "REGION_ADDR")


def has_number(q):
    from addr import RD, JB
    return bool(RD.search(q) or JB.search(q))


def ask(q, k):
    """카카오 로컬 주소검색. 좌표 (lat, lng) 또는 None."""
    req = urllib.request.Request(API + urllib.parse.urlencode({"query": q, "size": 1}),
                                 headers={"Authorization": "KakaoAK " + k})
    for attempt in range(4):
        try:
            r = urllib.request.urlopen(req, timeout=20)
            try:
                doc = json.loads(r.read().decode("utf-8")).get("documents") or []
            finally:
                r.close()
            if not doc:
                return None
            # ★address_type 이 REGION(지역명)·ROAD(번호 없는 도로)면 대표점이다 — 버린다
            if doc[0].get("address_type") not in OK_TYPES:
                return None
            return (float(doc[0]["y"]), float(doc[0]["x"]))     # y=위도, x=경도
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise Quota("키가 거부됐다(HTTP %d) — REST API 키가 맞는지 확인" % e.code)
            if e.code == 429:                                    # 쿼터·초당한도
                time.sleep(2 + attempt * 3)
                continue
            if 500 <= e.code < 600:
                time.sleep(1 + attempt)
                continue
            return None
        except Exception:
            time.sleep(1 + attempt)
    return None


def geocode_one(q, k):
    """헐거운 질의까지 차례로 시도한다. 어느 하나가 맞으면 그 좌표."""
    if not has_number(q):
        return None                    # 번지 없는 주소 — 쿼터도 안 쓴다
    for v in variants(q):
        xy = ask(v, k)
        if xy and 33.0 <= xy[0] <= 39.0 and 124.0 <= xy[1] <= 132.0:   # 남한 밖은 오매칭
            return xy
    return None


# ── 사이트 데이터에 좌표 심기 ─────────────────────────────────────────
# 좌표는 rows 에 끼우지 않고 별도의 평평한 배열 xy 로 둔다(rows 의 열 번호를
# 건드리지 않는다 — N_* 상수를 쓰는 코드가 사방에 있다).
#   xy[2i], xy[2i+1] = 행 i 의 (위도, 경도)  ·  좌표가 없으면 -1
#   ★부호 없는 작은 정수로 만들려고 기준점을 빼고 10만 배 한다(≈1m).
#     그대로 넣으면 3712345,12697531 처럼 15자리인데, 한 페이지에 9.3만 행이라
#     이 차이만으로 파일이 300KB 쯤 갈린다.
GEO_LAT0, GEO_LNG0, GEO_SCALE = 33.0, 124.0, 100000.0


def attach(data, cache=None):
    """data 에 xy(좌표 배열)와 geo(적중 통계)를 넣는다. 캐시에 없는 행은 -1."""
    cache = load_cache() if cache is None else cache
    sido, sgg, rows = data["sido"], data["sgg"], data["rows"]
    xy = [-1] * (len(rows) * 2)
    hit = 0
    for i, r in enumerate(rows):
        v = cache.get(geo_query(sido[r[N_SIDO]], sgg[r[N_SGG]], r[N_ADDR]))
        if not v:
            continue
        xy[2 * i] = int(round((v[0] - GEO_LAT0) * GEO_SCALE))
        xy[2 * i + 1] = int(round((v[1] - GEO_LNG0) * GEO_SCALE))
        hit += 1
    data["xy"] = xy
    data["geo"] = {"n": hit, "of": len(rows),
                   "lat0": GEO_LAT0, "lng0": GEO_LNG0, "scale": int(GEO_SCALE)}
    return hit


def run(limit, retry_fail, threads):
    k = key()
    if not k:
        print("카카오 REST API 키가 없다.\n"
              "  환경변수 KAKAO_KEY 에 넣거나 build/kakao_key.txt 에 한 줄로 저장해라.")
        return 1
    cache = load_cache()
    todo = [q for q in targets()
            if q not in cache or (retry_fail and cache[q] is None)]
    have = sum(1 for v in cache.values() if v)
    print("캐시 %s건(성공 %s) · 이번에 받을 주소 %s건"
          % (format(len(cache), ","), format(have, ","), format(len(todo), ",")))
    if limit:
        todo = todo[:limit]
        print("  --limit %s 적용 -> %s건" % (format(limit, ","), format(len(todo), ",")))
    if not todo:
        print("받을 게 없다 — 캐시가 이미 최신이다.")
        return 0

    lock = threading.Lock()
    state = {"i": 0, "ok": 0, "bad": 0, "stop": None, "t0": time.time()}

    def worker(chunk):
        for q in chunk:
            if state["stop"]:
                return
            try:
                xy = geocode_one(q, k)
            except Quota as e:
                with lock:
                    state["stop"] = str(e)
                return
            with lock:
                cache[q] = xy
                state["i"] += 1
                if xy:
                    state["ok"] += 1
                else:
                    state["bad"] += 1
                n = state["i"]
                if n % 500 == 0:
                    el = max(time.time() - state["t0"], 1.0)
                    rate = n / el
                    print("  %s/%s  성공 %s · 실패 %s · %.0f건/초 · 남은시간 %.0f분"
                          % (format(n, ","), format(len(todo), ","),
                             format(state["ok"], ","), format(state["bad"], ","),
                             rate, (len(todo) - n) / max(rate, .1) / 60))
                if n % 5000 == 0:
                    save_cache(cache)

    ts = []
    for i in range(threads):
        t = threading.Thread(target=worker, args=(todo[i::threads],))
        t.daemon = True
        t.start()
        ts.append(t)
    try:
        for t in ts:
            t.join()
    except KeyboardInterrupt:
        state["stop"] = "사용자 중단"
    save_cache(cache)

    done = sum(1 for v in cache.values() if v)
    print("\n캐시 %s건(성공 %s · 실패 %s) — %.1f분"
          % (format(len(cache), ","), format(done, ","),
             format(len(cache) - done, ","), (time.time() - state["t0"]) / 60))
    if state["stop"]:
        print("★중단: %s" % state["stop"])
        return 2
    return 0


def self_test():
    """키 없이 도는 점검 — 캐시 왕복과 질의문 규칙만 본다."""
    global CACHE
    cases = [
        (u"서울특별시", u"종로구", u"서울특별시 종로구 대학로5길 5 (연건동)",
         u"서울특별시 종로구 대학로5길 5 (연건동)"),
        (u"강원특별자치도", u"원주시", u"강원특별자치도원주시 시청로 1", u"강원특별자치도 원주시 시청로 1"),
        (u"광주광역시", u"동구", u"전남광주통합특별시 동구 예술길 5", u"광주광역시 동구 예술길 5"),
        (u"서울특별시", u"중구", u"세종대로 110", u"서울특별시 중구 세종대로 110"),
    ]
    bad = 0
    for sido, sgg, a, want in cases:
        got = geo_query(sido, sgg, a)
        if got != want:
            print("  FAIL %s -> %s (기대 %s)" % (a, got, want))
            bad += 1
    v = variants(u"서울특별시 종로구 대학로5길 5 (연건동) 2층")
    if u"서울특별시 종로구 대학로5길 5" not in v:
        print("  FAIL variants: %s" % v)
        bad += 1
    keep, CACHE = CACHE, os.path.join(HERE, "_geo_selftest.csv")
    try:
        save_cache({u"가 나,다": (37.5, 127.0), u"실패주소": None})
        c = load_cache()
        if c.get(u"가 나,다") != (37.5, 127.0) or u"실패주소" not in c or c[u"실패주소"] is not None:
            print("  FAIL 캐시 왕복: %s" % c)
            bad += 1
    finally:
        if os.path.exists(CACHE):
            os.remove(CACHE)
        CACHE = keep
    print("self-test %s" % ("OK" if not bad else "%d건 실패" % bad))
    return 1 if bad else 0


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, default=int(os.environ.get("GEO_LIMIT") or 0))
    p.add_argument("--retry-fail", action="store_true")
    p.add_argument("--threads", type=int, default=int(os.environ.get("GEO_THREADS") or 6))
    p.add_argument("--self-test", action="store_true")
    a = p.parse_args()
    sys.exit(self_test() if a.self_test
             else run(a.limit, a.retry_fail, max(1, min(16, a.threads))))
