"""CSV 보고서: 시각 | 사용자 행위 | 아티팩트 | 경로 | 등급 | 근거

UTF-8 BOM (Excel에서 한글이 깨지지 않게), 줄바꿈 CRLF.
행 순서는 analyzer가 정한 순서 그대로 (시각 있는 행을 시간순으로 먼저, '-' 행은 행위 ID -> 아티팩트 이름 순).
"""
import csv

HEADER = ["시각", "사용자 행위", "아티팩트", "경로", "등급", "근거"]


def write_csv(rows, path):
    with open(path, "w", encoding="utf-8-sig", newline="", errors="backslashreplace") as f:
        w = csv.writer(f)
        w.writerow(HEADER)
        for r in rows:
            w.writerow([r.time_text, r.action_name, r.artifact, r.path, r.grade, r.evidence])
