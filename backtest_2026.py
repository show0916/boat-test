#!/usr/bin/env python3
import csv, io, json, time
from collections import defaultdict, deque
from datetime import date, datetime, timedelta
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

BASE = "https://boatracecsv.github.io"
HISTORY_START = date(2025, 11, 1)
TARGET_START = date(2026, 1, 1)
TARGET_END = date(2026, 10, 4)

# 競艇日和の「1号艇逃率」は、1号艇として出走した回数を分母に、
# その1号艇が「逃げ」で1着になった回数を分子とする近似。
# ただし公開データ側の完全な1年履歴が2025-11-01開始のため、
# 2026年の前半は365日分の履歴を満たせない。
# そのため history_days / history_complete をCSVに残し、後で除外可能にする。

def get_bytes(url, retries=3):
    for i in range(retries):
        try:
            req = Request(url, headers={"User-Agent": "boat-test-backtest/1.0"})
            with urlopen(req, timeout=30) as r:
                return r.read()
        except (HTTPError, URLError, TimeoutError) as e:
            if i == retries - 1:
                print("DOWNLOAD FAILED:", url, e)
                return None
            
        
            time.sleep(1.5 * (i + 1))
    return None

def get_csv(path):
    b = get_bytes(BASE + path)
    if not b:
        return []
    text = b.decode("utf-8-sig", errors="replace")
    return list(csv.DictReader(io.StringIO(text)))

def dlist(start, end):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)

def player_in_boat1(row):
    # 結果CSVの「N着_艇番」と「N着_選手名」から1号艇の選手を特定。
    for n in range(1, 7):
        if str(row.get(f"{n}着_艇番", "")).strip() == "1":
            return str(row.get(f"{n}着_選手名", "")).strip()
    return ""

def boat1_won(row):
    return str(row.get("1着_艇番", "")).strip() == "1"

def is_escape(row):
    # 決まり手「逃げ」かつ1号艇1着。
    return boat1_won(row) and str(row.get("決まり手", "")).strip() == "逃げ"

def race_sort_key(row):
    return (
        row.get("レース日", ""),
        row.get("締切時刻", ""),
        row.get("レースコード", ""),
    )

def main():
    # player -> deque[(date, is_start, is_escape)]
    hist = defaultdict(deque)

    # target rows will be joined from results + od2
    results_by_code = {}
    target_odds = {}

    # 1) 結果履歴を取得。2025-11-01から2026-10-04まで。
    #    2026年1月以降の全24場を対象にするため、日次CSVを順番に処理。
    print("Downloading result history...")
    for d in dlist(HISTORY_START, TARGET_END):
        p = f"/data/results/realtime/{d:%Y/%m/%d}.csv"
        rows = get_csv(p)
        rows.sort(key=race_sort_key)
        for r in rows:
            code = r.get("レースコード", "")
            if not code:
                continue
            results_by_code[code] = r
        if d.day == 1 or d.weekday() == 6:
            print(" results", d)

    # 2) 各レースを時系列に並べ、事前の逃率を計算しながら
    #    2026年の対象レース情報を作る。
    all_results = sorted(results_by_code.values(), key=race_sort_key)
    print("DEBUG all_results:", len(all_results))
    # 出力候補を一旦保存
    candidates = []

    cutoff_365 = timedelta(days=365)
    cutoff_183 = timedelta(days=183)

    for r in all_results:
        rd_s = r.get("レース日", "")
        if not rd_s:
            continue
        try:
            rd = datetime.strptime(rd_s, "%Y-%m-%d").date()
        except ValueError:
            continue

        player = player_in_boat1(r)
        if not player:
            continue

        q = hist[player]
        # 365日より古い履歴を捨てる。
        while q and rd - q[0][0] > cutoff_365:
            q.popleft()

        # 直前時点の統計
        starts365 = sum(x[1] for x in q if rd - x[0] <= cutoff_365)
        escapes365 = sum(x[2] for x in q if rd - x[0] <= cutoff_365)

        starts183 = sum(x[1] for x in q if rd - x[0] <= cutoff_183)
        escapes183 = sum(x[2] for x in q if rd - x[0] <= cutoff_183)

        rate365 = escapes365 / starts365 * 100 if starts365 else None
        rate183 = escapes183 / starts183 * 100 if starts183 else None

        if rd >= TARGET_START:
            # 競艇日和の「1年&6ヶ月共通」に寄せるため、
            # 両方の率が70%以上なら common_70=1 とする。
            # 現在のツールは escape_rate を70%以上で見るので、
            # escape_rate は共通条件の低い方を採用。
            common_rate = rate183
            history_days = (rd - HISTORY_START).days
            candidates.append({
                "race_code": r.get("レースコード", ""),
                "date": rd_s,
                "place_code": r.get("レース場", ""),
                "race": r.get("レース回", ""),
                "player": player,
                "escape_rate_1y": "" if rate365 is None else f"{rate365:.2f}",
                "escape_rate_6m": "" if rate183 is None else f"{rate183:.2f}",
                "escape_rate": "" if common_rate is None else f"{common_rate:.2f}",
                "history_days_available": history_days,
                "history_complete_1y": int(history_days >= 365),
                "result": r,
            })

        # このレースを履歴に追加（未来レースからの情報漏洩を防ぐため最後に追加）
        q.append((rd, 1, int(is_escape(r))))
       
    print("DEBUG candidates:", len(candidates))

    # 3) 対象期間のod2を取得し、1番人気(最低オッズ)を決定。
    print("Downloading 2026 odds...")
    for d in dlist(TARGET_START, TARGET_END):
        p = f"/data/previews/od2/{d:%Y/%m/%d}.csv"
        rows = get_csv(p)
        for r in rows:
            code = r.get("レースコード", "")
            if code:
                target_odds[code] = r
        if d.day == 1 or d.weekday() == 6:
            print(" odds", d)
    print("DEBUG target_odds:", len(target_odds))
    # 4) 結果とオッズを結合してアプリ用CSVを作る。
    out = []
    for c in candidates:
        code = c["race_code"]
        o = target_odds.get(code)
        if not o:
            continue

        odds = []
        for k, v in o.items():
            if not k.startswith("2連単_"):
                continue
            combo = k.replace("2連単_", "", 1)
            try:
                val = float(str(v).replace(",", ""))
            except ValueError:
                continue
            if val > 0:
                odds.append((val, combo))

        if not odds:
            continue

        min_odds, popular = min(odds, key=lambda x: (x[0], x[1]))
        # 現行条件：2連単1番人気が1-○かつ3.0倍以上
        if not popular.startswith("1-") or min_odds < 3.0:
            continue

        result = c["result"]
        actual = f"{str(result.get('1着_艇番','')).strip()}-{str(result.get('2着_艇番','')).strip()}"
        hit = int(actual == popular)

        out.append({
            "place": c["place_code"],
            "race": c["race"],
            "date": c["date"],
            "player": c["player"],
            "escape_rate": c["escape_rate"],
            "escape_rate_1y": c["escape_rate_1y"],
            "escape_rate_6m": c["escape_rate_6m"],
            "history_complete_1y": c["history_complete_1y"],
            "popular": popular,
            "odds": f"{min_odds:.1f}",
            "actual": actual,
            "hit": hit,
            "payout": "",
            "race_code": code,
        })

print("DEBUG out:", len(out))
    # 5) 共通条件を満たす行だけ出力。
    #    ここでは現行アプリと同じくescape_rate>=70を使う。
final = []
for r in out:
        try:
            er = float(r["escape_rate"])
        except (TypeError, ValueError):
            continue
        if er >= 70:
            final.append(r)

print("DEBUG final:", len(final))
final.sort(key=lambda x: (x["date"], x["place"], x["race"]))

outpath = "data/backtest_2026.csv"
fields = [
        "place","race","date","player","escape_rate","escape_rate_1y",
        "escape_rate_6m","history_complete_1y","popular","odds",
        "actual","hit","payout","race_code"
    ]
with open(outpath, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(final)

# サマリー
summary = {
        "target_start": str(TARGET_START),
        "target_end": str(TARGET_END),
        "history_start": str(HISTORY_START),
        "qualifying_rows": len(final),
        "history_complete_1y_rows": sum(int(x["history_complete_1y"]) for x in final),
        "note": "2026-01-01〜2026-04-30は1年履歴不足。2026-05-01以降のみ365日履歴を満たす。",
    }
with open("data/backtest_2026_summary.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

print(json.dumps(summary, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
