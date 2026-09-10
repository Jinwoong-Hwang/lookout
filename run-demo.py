#!/usr/bin/env python3
"""데모용 대시보드 런처.

config.json을 건드리지 않고 포트만 런타임에 덮는다 — 설정 파일을 바꾸면 라이브
인스턴스(:8788)와 충돌하고, 테스트도 config.json을 읽으므로 같이 깨진다.
사용: python3 run-demo.py [포트]
"""
import sys

from src import config

config.CFG["dashboard_port"] = int(sys.argv[1]) if len(sys.argv) > 1 else 8799

from src import dashboard  # noqa: E402  (포트를 덮은 뒤에 import해야 반영된다)

dashboard.main()
