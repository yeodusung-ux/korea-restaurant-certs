# -*- coding: utf-8 -*-
"""UI만 고쳤을 때 쓰는 빠른 재생성. 데이터는 건드리지 않는다.

index.html 에 박힌 데이터(const D = {...})를 그대로 꺼내 새 template.html 에 다시 끼운다.
수집·정규화·동일가게 매칭을 전부 건너뛰므로 **API 키가 필요 없고 네트워크도 안 탄다.**

    build_site.py    수집 → 정규화 → 매칭 → 렌더   (전체, 키 필요, 실측 3~18분)
    rebuild_template.py                    렌더    (템플릿만, 키 불필요, 1초 미만)

★언제 쓰면 안 되나 — 데이터가 바뀌는 변경에는 쓸 수 없다. 아래는 build_site.py 를 돌려야 한다.
    · guide_blue.csv / guide_red.csv 를 고쳤을 때
    · 소스 추가·삭제, 분류 규칙(classify)·주소 정규화·동일가게 매칭 로직을 고쳤을 때
  이 스크립트는 template.html 의 /*__DATA__*/ 자리만 갈아 끼운다.

사용법:
    python build/rebuild_template.py
"""
import io
import json
import os
import re
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
IDX = os.path.join(ROOT, "index.html")
TPL = os.path.join(HERE, "template.html")


def main():
    if not os.path.exists(IDX):
        print("index.html 이 없다. 처음이라면 build_site.py 를 먼저 돌려야 한다.")
        return 1

    old = io.open(IDX, encoding="utf-8").read()
    m = re.search(r"const D\s*=\s*", old)
    if not m:
        print("index.html 에서 데이터를 찾지 못했다(const D = ...). 형식이 바뀌었나?")
        return 1
    data, _ = json.JSONDecoder().raw_decode(old, m.end())

    # 최소한의 온전성 검사 — 빈 데이터를 다시 끼워 사이트를 날리지 않게 한다
    rows = data.get("rows") or []
    if len(rows) < 1000 or not data.get("ds"):
        print("데이터가 온전치 않다(rows=%d) → 중단" % len(rows))
        return 1

    tpl = io.open(TPL, encoding="utf-8").read()
    if "/*__DATA__*/" not in tpl:
        print("template.html 에 /*__DATA__*/ 자리가 없다 → 중단")
        return 1

    html = tpl.replace("/*__DATA__*/",
                       json.dumps(data, ensure_ascii=False, separators=(",", ":")))
    if html == old:
        print("템플릿에 바뀐 게 없다 — index.html 그대로 둔다.")
        return 0

    with io.open(IDX, "w", encoding="utf-8", newline="") as f:
        f.write(html)

    print("index.html 갱신 — %s행 · %s종 · 데이터 빌드일 %s (데이터는 그대로)"
          % (format(len(rows), ","), len(data["ds"]), data.get("built", "?")))
    print("  %.2fMB" % (os.path.getsize(IDX) / 1024.0 / 1024))
    return 0


if __name__ == "__main__":
    sys.exit(main())
