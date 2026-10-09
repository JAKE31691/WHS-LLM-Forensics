# [LLM Artifact Analyzer 동봉 수정] 원본은 아래 두 줄을 바로 import한다.
# ChromiumProfileFolder가 brotli, zstd 패키지를 필수로 요구해서, 그 패키지가 없으면
# 이 도구가 쓰는 하위 모듈(localstorage, indexeddb 등)까지 import가 막힌다.
# 그래서 없을 때만 건너뛰도록 감쌌다. 이 도구는 ChromiumProfileFolder를 쓰지 않는다.
try:
    from .ccl_chromium_profile_folder import ChromiumProfileFolder
except ImportError:
    ChromiumProfileFolder = None
from .common import KeySearch
