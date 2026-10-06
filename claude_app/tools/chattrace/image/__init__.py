# -*- coding: utf-8 -*-
"""디스크 이미지를 직접 읽는다.

외부 도구·드라이버·관리자 권한 없이 E01 을 열고, 파티션을 찾고, NTFS 에서
필요한 파일만 꺼낸다. 마운트하지 않는 이유는 두 가지다.

  * 마운트는 ACL 을 그대로 유지한다. 읽기 전용으로 붙여도
    D:\\Users\\<남>\\AppData 는 'Access is denied' 가 난다(실측). 결국 관리자
    권한에 소유권 탈취까지 해야 하고, 그 과정이 증거를 건드린다.
  * 마운트 드라이버는 설치를 요구한다. 분석 PC 를 고른 상태로 두기 어렵다.

원시 파싱은 ACL 을 아예 거치지 않는다. 권한 문제가 없고, 설치도 없다.
"""
from .formats import open_image, ImageError, describe
from .partition import read_partitions, Partition
from .ntfs import NtfsVolume, NtfsError
from .extract import extract_case, ExtractResult

__all__ = ['open_image', 'ImageError', 'describe',
           'read_partitions', 'Partition',
           'NtfsVolume', 'NtfsError',
           'extract_case', 'ExtractResult']
