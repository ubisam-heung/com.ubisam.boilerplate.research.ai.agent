#!/usr/bin/env python3
"""작업 지표 리포트 (경진대회 기대효과 정량화용)

사용법:
    python metrics.py [프로젝트 디렉토리]   # 기본: 현재 디렉토리

logs/metrics.jsonl 을 읽어 로컬 처리 비율·평균 처리시간·검증/복구 성공률·
비용 절감 추정치를 출력한다. ./agent 로 작업을 몇 건 실행한 뒤 사용하세요.
"""
import os
import sys

import yaml

from harness import metrics


def main():
    project_dir = sys.argv[1] if len(sys.argv) > 1 else "."

    log_dir_name = "logs"
    cfg_path = os.path.join(project_dir, "config.yaml")
    if os.path.exists(cfg_path):
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        log_dir_name = cfg.get("logging", {}).get("log_dir", "logs")

    log_dir = os.path.join(project_dir, log_dir_name)
    records = metrics.load(log_dir)
    print()
    print(metrics.format_report(metrics.summarize(records)))
    print()


if __name__ == "__main__":
    main()
