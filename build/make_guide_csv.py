# -*- coding: utf-8 -*-
"""네이버 지도 즐겨찾기 내보내기(JSON) → 직접 선별 목록 시드 CSV.

파란인증·빨간인증은 공공 API 가 없어 주간 자동수집에 태울 수 없다.
그래서 사람이 네이버 지도 즐겨찾기로 관리하고, 여기서 CSV 로 굳혀 리포에 커밋한다.
build_site.py 는 그 CSV 를 읽는다(네트워크 접근 없음).

  파란인증 → build/guide_blue.csv
  빨간인증 → build/guide_red.csv

즐겨찾기 JSON 받는 법 — 네이버 로그인 상태로 주소창에서
  https://pages.map.naver.com/save-pages/api/maps-bookmark/v3/folders?start=0&limit=200&sort=lastUseTime&folderType=all
  → 원하는 폴더의 shareId 를 찾고
  https://pages.map.naver.com/save-pages/api/maps-bookmark/v3/shares/<shareId>/bookmarks?start=0&limit=5000&sort=lastUseTime
  → 우클릭 '다른 이름으로 저장'

사용법:
  python build/make_guide_csv.py <즐겨찾기.json> build/guide_blue.csv
"""
import csv
import io
import json
import sys


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    src, dst = sys.argv[1], sys.argv[2]

    data = json.load(io.open(src, encoding="utf-8"))
    bl = data.get("bookmarkList") or []
    if not bl:
        print("bookmarkList 가 비어 있다: %s" % src)
        return 1

    seen, rows = set(), []
    for b in bl:
        name = (b.get("name") or "").strip()
        addr = (b.get("address") or "").strip()
        kind = (b.get("mcidName") or "").strip()      # 음식점 / 카페 / BAR
        if not name or not addr:
            continue
        k = (name, addr)
        if k in seen:
            continue
        seen.add(k)
        rows.append({"name": name, "addr": addr, "kind": kind})

    rows.sort(key=lambda r: (r["addr"], r["name"]))
    with io.open(dst, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["name", "addr", "kind"], lineterminator="\n")
        w.writeheader()
        w.writerows(rows)

    print("%s → %s : %d행 (원본 %d, 중복 %d 제거)"
          % (src, dst, len(rows), len(bl), len(bl) - len(rows)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
