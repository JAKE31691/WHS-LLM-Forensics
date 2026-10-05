#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""ChatTrace 실행기 — 실제 구현은 chattrace/app.py 에 있다."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from chattrace.app import main

if __name__ == '__main__':
    sys.exit(main())
