# ChatTrace

Claude Desktop(Electron/MSIX)의 로컬 아티팩트에서 대화 원문을 복원하는 도구.
행위 01~14 실험에서 확인한 저장 구조를 그대로 구현했다.

```
ChatTrace <분석대상> <환경> <저장위치>
```

위치 인자 셋은 **이미지 · 환경 · 저장 경로** 순서다 (2026-10-05 팀 합의).
기존 형식도 그대로 받는다 — `ChatTrace -i <분석대상> -o <저장위치>`.

설치할 게 없다. 표준 라이브러리만 쓴다. Python 3.8+.

## 디스크 이미지를 그대로 준다

`-i` 에 E01 을 주면 끝이다. 마운트하지 않는다.

```
ChatTrace -i "D:\사건\CASE-B01.E01" -o "C:\사건\B01" --timeline
```

파티션을 찾고, 윈도우가 설치된 NTFS 를 고르고, `$MFT` 를 뽑고, Claude
아티팩트만 골라 꺼낸 뒤, 그대로 분석까지 간다. 실측 **52.8초**(60 GB 이미지,
E01 22조각).

**왜 마운트하지 않나**

| | 마운트 | 원시 파싱 |
|---|---|---|
| 남의 `AppData` 읽기 | **안 된다** — ACL 이 그대로 살아서 `Access is denied` | 된다. ACL 을 아예 안 거친다 |
| 관리자 권한 | 필요 | 불필요 |
| 드라이버 설치 | 필요 | 불필요 |
| 끝난 뒤 | 떼어내야 한다 | 할 일 없음 |

읽기 전용으로 붙여도 ACL 은 유지된다. 실제로 이 실험의 이미지를 Arsenal
Image Mounter 로 붙였을 때 `D:\Users\Kwon\AppData` 가 그대로 막혔다.

**읽는 형식**

| 확장자 | 형식 |
|---|---|
| `.E01`~`.EZZ` | EnCase EWF v1. 조각은 알아서 이어 붙인다 |
| `.dd .raw .img .001` | 원시 이미지. 분할본도 한 덩어리로 본다 |
| `.vmdk` | VMware 희소·평면·분할 |

BitLocker · ReFS · Ex01 · VHDX · QCOW 는 **무엇인지 정확히 말하고 멈춘다.**
"NTFS 가 없다" 로 끝내지 않는다.

```
ChatTrace -i CASE.E01 -o OUT --list-partitions   # 파티션 목록만 보고 끝
ChatTrace -i CASE.E01 -o OUT --partition 3       # 파티션 직접 지정
ChatTrace -i CASE.E01 -o OUT --extract-only      # 꺼내기만
ChatTrace --selftest                             # 파서 자체 점검
```

꺼낸 것은 `<출력>\_추출\` 에 **KAPE 수집물과 같은 모양**으로 들어간다.
그래서 이미지에서 왔든 현장 수집에서 왔든 뒷단은 같은 것을 본다.

## 실행 경로

| 부르는 법 | 쓰는 곳 |
|---|---|
| `ChatTrace ...` | 어디서든. `C:\Users\gstop\.local\bin\ChatTrace.cmd` 가 PATH 에 있다 |
| `python -m chattrace ...` | 다른 PC 로 폴더만 복사했을 때 |
| `python chattrace_main.py ...` | 폴더 안에서 직접 |

다른 PC 에 설치하려면 `ChatTrace.cmd` 안의 `CHATTRACE_HOME` 만 고치면 된다.
pip 를 쓰고 싶으면 `pip install -e .` 해도 `ChatTrace` 명령이 생긴다.

## 옵션

| 옵션 | 뜻 |
|---|---|
| `-i, --input` | 분석 대상. KAPE 출력 루트도, **디스크 이미지 파일**도 된다 |
| `-o, --output` | 결과 저장 폴더. 회차마다 다르게 줘라 |
| `--target-mode all` | ①전체세션 → ②파편 → ③고유문자열 (기본이자 유일) |
| `--scope artifacts` | 대화 흔적이 나오는 경로만 (기본) |
| `--scope everything` | Cache / Code Cache 포함 전수 스캔 |
| `--baseline <경로>` | **계정 삭제 분석 필수.** 삭제 이전 수집분 |
| `--scenario 1\|2\|3` | 기록용 라벨 |
| `--marker <문자열>` | 실험용 고유 문자열 추적 |
| `--min-confidence 80` | 오탐 제거 |
| `--verify-crc` | LevelDB CRC 검증 (느림) |
| `--no-decompress` | 압축 해제 생략. 본문 약 40% 놓친다 |
| `--timeline` | $MFT · LevelDB 시퀀스 · 앱 로그를 엮어 타임라인 생성 |
| `--mft <경로>` | $MFT 위치 직접 지정 (생략 시 수집물에서 찾는다) |
| `--mft-csv <경로>` | MFTECmd 출력 CSV. 있으면 이쪽을 쓴다 |

디스크 이미지를 줄 때만 쓰는 것

| 옵션 | 뜻 |
|---|---|
| `--list-partitions` | 파티션 목록만 보고 끝낸다 |
| `--partition N` | 볼 파티션 지정. 생략하면 스스로 고른다 |
| `--extract-to <경로>` | 꺼낸 것을 둘 곳 (기본 `<출력>\_추출`) |
| `--reuse-extract` | 이미 꺼내 뒀으면 다시 꺼내지 않는다 |
| `--extract-only` | 꺼내기만 하고 분석은 안 한다 |
| `--with-app` | Claude 앱 본체(WindowsApps)까지. 수백 MB |
| `--with-system` | Prefetch · 레지스트리 · 이벤트로그까지 |
| `--include-noise` | Cache / Code Cache 도 꺼낸다 |
| `--exclude <문자열>` | 그 문자열이 든 경로 제외 (여러 번 가능) |
| `--selftest` | 파서 자체 점검만 하고 끝낸다 |

### `--scope` 가 속도를 결정한다

R5 실측: 파일 1,651개를 훑어서 소견이 나온 건 **5개**뿐이었고 전부
IndexedDB / Session Storage 였다. Cache 96개 · Code Cache 1,208개는 **0건**.

| 모드 | scope | 소요 | 결과 |
|---|---|---|---|
| `all` | everything | 41초 | Q/A 26쌍 · 복원율 100% |
| `all` | **artifacts** | **2.9초** | 동일 |
| `prompt_only` | everything | 40초 | 프롬프트 193 (중 3건 오탐) |
| `prompt_only` | **artifacts** | **1.8초** | 프롬프트 190 (오탐 0) |

기본값을 `artifacts` 로 두었다. 결과는 같고 14배 빠르며, 앱 동봉 문서에서
나던 오탐까지 자동으로 빠진다. 디스크 슬랙까지 훑어야 할 때만
`--scope everything` 을 쓴다.

## 세 가지 시나리오

| | 대상 | 원리 | 실측 결과 |
|---|---|---|---|
| ① | 정상 | 시퀀스 최대값 = 현재 상태 | Q/A 26쌍, 복원율 100% |
| ② | 대화 삭제 | LevelDB append-only — 삭제는 툼스톤 추가일 뿐 | 지운 대화 전체 복원 |
| ③ | 계정 삭제 | 잔존물 카빙 + 기준선 대조 | 프롬프트 1건만 잔존 |

### ③ 은 `--baseline` 없이 쓰면 안 된다

- `SURVIVED` — 삭제 **후** 이미지에도 있다 → 진짜 잔존물
- `RECOVERED` — 삭제 **전** 수집분에만 있다 → 미리 떠놨기 때문에 보이는 것

둘을 섞어 "복원했다" 고 쓰면 그 보고서는 틀린 주장이 된다.

## 타임라인 — '무엇을' 에 '언제' 를 붙인다

본체는 "이런 대화가 있었고 지워졌다" 까지만 말한다. 삭제 시각은 못 댄다.
`--timeline` 이 세 출처를 하나의 시간축으로 합친다.

| 출처 | 답하는 것 | 정밀도 |
|---|---|---|
| `$MFT` | 파일이 언제 바뀌었나 | 100 나노초 |
| LevelDB 시퀀스 | 삭제가 어느 구간에 일어났나 | 중앙값 11.5초 |
| `main.log` | 앱이 언제 뜨고 졌나 | 초 |

### 삭제 시각을 어떻게 좁히나

LevelDB 시퀀스 번호는 쓰기 순서다. 레코드 일부는 `writtenAt` 같은 밀리초
시각을 품고 있어 (시퀀스, 시각) **기준점**이 된다. 삭제 표시(툼스톤)에는
시각이 없지만 시퀀스는 있으므로 앞뒤 기준점 사이로 구간이 좁혀진다.
마지막 기준점보다 뒤에 있는 삭제는 **파일의 `$MFT` 변경 시각**을 천장으로
쓴다. 파일이 마지막으로 쓰인 뒤에 기록이 더 붙을 수는 없기 때문이다.

단정하지 않고 **구간**으로 낸다. 그게 정직하다.

### 검증 (행위 14, 정답 마커 대조)

```
MARK-14-conv-delete   04:30:02.238 UTC   삭제 직전에 남긴 마커
ChatTrace 추정        04:30:19.683 ~ 04:30:30.610   (10.9초)
MARK-15-collect-t1    04:31:14.520 UTC   수집 시작
```

실제 삭제는 두 마커 사이 **72초** 안에서 일어났고, 추정 구간이 그 안에
정확히 들어간다. 삭제 표시 1,527개 중 60초 이내로 좁혀진 것이 1,199개.

### $MFT 파서 검증 (MFTECmd 대조)

```
레코드 620,288개 → 해석 614,591 · 손상 0
표본 4,000링크 × 10필드 = 40,000회 비교 → 불일치 0건
  InUse · SequenceNumber
  Created/LastModified/LastRecordChange/LastAccess 0x10
  동 0x30 4종
경로 신뢰도  ok 332,336 · reused 321,769 · missing 814
```

부모 슬롯이 재사용된 경로는 `reused` 로 표시한다. 그대로 따라가면
'그럴듯하지만 틀린 경로' 가 나오기 때문이다. MFTECmd 가 `PathUnknown`
으로 두는 것과 같은 상황이며, 표본에서 1,946건이 서로 대응했다.

## 왜 plyvel / sqlite3 를 쓰지 않나

이 도구의 핵심 설계 결정이다.

| 접근 | 문제 |
|---|---|
| `plyvel.DB(path)` | 여는 순간 LevelDB 가 **복구(recovery)** 를 수행한다. `.log` 를 `.ldb` 로 말아 넣고 MANIFEST/CURRENT 를 갱신하며, 덮어쓰기로 밀려난 옛 값과 삭제 툼스톤을 버린다. 복원 대상 자체가 사라진다 |
| `sqlite3.connect(db)` | 살아 있는 행만 SELECT 된다. 삭제 레코드는 프리블록에 본문이 남는데 API 로는 안 보인다 |

그래서 두 포맷 모두 **바이트 단위로 직접 파싱**한다.

## 저장 구조 (실측)

IndexedDB 값은 세 겹으로 싸여 있다.

```
[IDB 래퍼]  FF 11 01 <크기> <번호>   → 외부 .blob 파일로 분리됨
            FF 11 02 <snappy>        → 내부 저장 + snappy 압축
[Blink]     FF 15  FE <8B 오프셋><4B 크기>  FF 10
[V8]        6F 22 10 "conversationUuid" ...
```

걸려 넘어질 지점 셋:

1. **`0xFE` (trailer offset, 13바이트)** — 모르면 엄격 디코드가 전부 실패한다
2. **`FF 11 01` 외부 블롭** — 긴 대화는 DB 안에 **없다**. 크기만 있고 본문은
   `IndexedDB\<오리진>.indexeddb.blob\<db>\<디렉터리>\<번호>` 에 있다
3. **길이는 uvarint, 값은 zigzag varint** — 섞으면 정확히 2배 틀린다

대화 스키마:

```
{conversationUuid, accountUuid, orgUuid, messageCount,
 tree: {uuid, name, model, created_at, current_leaf_message_uuid,
        chat_messages: [{uuid, sender, index, text,
                         content: [{type, text, start_timestamp, ...}],
                         attachments, files, parent_message_uuid}],
        parentByChildUuid, childrenByParentUuid}}
```

`parentByChildUuid` / `childrenByParentUuid` 로 편집·재생성 가지도 복원 가능하다.

## 모듈 구조

```
ChatTrace/
  chattrace_main.py    실행기
  ChatTrace.cmd        → C:\Users\gstop\.local\bin\ 에 복사돼 있다
  chattrace/
    app.py       CLI 본체 · ①②③ 우선순위 복원
    codec.py     varint · zigzag · crc32c · snappy · V8 structured clone
    leveldb.py   .log(WriteBatch) · .ldb(SST) 파서 · IndexedDB 키 해석
    sqlitefs.py  페이지 · 프리블록 · 비할당 · WAL · 롤백 저널
    blobscan.py  blob/캐시 스캐너 · 압축 해제 · UTF-16 뷰
    carve.py     패턴 스캐너
    scenarios.py 복원 엔진 · 외부 blob 색인
    report.py    JSON / CSV / HTML / 무결성 대장
    mft.py       NTFS $MFT 파서 (fixup · 하드링크 · 부모 시퀀스 검증)
    timeline.py  $MFT + 시퀀스 + 로그 병합 · 삭제 시각 추정
    cli.py       (구) 시나리오별 CLI. --root/--out 을 쓴다. ChatTrace 를 써라
    image/       디스크 이미지를 직접 읽는다 (마운트 없음)
      formats.py   형식 판별 · 원시(dd/분할) · VMDK(희소·평면)
      ewf.py       E01 세그먼트 체인 · 청크 표 · zlib · 사건정보
      partition.py GPT · MBR · 확장 파티션 · 부트섹터로 파일시스템 실측
      ntfs.py      $MFT 부트스트랩 · 데이터런 · 속성목록 · 희소 · LZNT1
      wof.py       Compact OS 시스템 압축 (XPRESS 4K/8K/16K)
      extract.py   이미지 -> KAPE 모양 수집물 + manifest + 증거연계
      selftest.py  파서 자체 점검 (--selftest)
```

### image 층이 값과 함께 '믿어도 되는가' 를 같이 낸다

`file_content()` 는 바이트만 주지 않고 상태를 같이 준다.

| 상태 | 뜻 |
|---|---|
| `ok` | 내용 전체를 읽었다 |
| `sparse` | `$DATA` 가 전부 희소. 디스크에 내용이 없다 |
| `wof_failed` | 시스템 압축인데 못 풀었다 |
| `wof_external` | 내용이 이 볼륨이 아니라 별도 `.wim` 에 있다 |
| `encrypted` | EFS |
| `partial` / `no_data` / `no_record` | 그 밖의 미완 |

이렇게 한 이유가 있다. Compact OS 로 압축된 파일은 `$DATA` 가 통째로
비어 있어서, 모르고 읽으면 **길이는 정확하고 내용은 전부 0** 인 결과가
오류 없이 나온다. 포렌식에서 제일 나쁜 실패다. 틀린 걸 틀린 줄 모른다.
그래서 0 을 내용인 척 내보내는 경로를 아예 없앴다.

## 출력

| 파일 | 내용 |
|---|---|
| `transcript.txt` | 사람이 읽는 대화록. **여기부터 열어라** |
| `pairs.csv` | 질문/답변 쌍 |
| `prompts.csv` | 사용자 프롬프트만 |
| `report.html` | 브라우저 요약 |
| `conversations.json` | 전체 구조 (논문 부록용) |
| `chain_of_custody.json` | 입력 파일 SHA-256 · 크기 · mtime |
| `timeline.csv` | 시간순 사건 (--timeline) |
| `deleted_when.csv` | 삭제 시각 추정 구간 (--timeline) |
| `chattrace.log` | 실행 로그 |

## 검증 (행위 14 데이터, 실제 KAPE 수집물 999 MB)

```
LevelDB CRC          4,398 / 4,398 통과
ObjectStoreData      226 / 226 디코드, 실패 0  (내부 143 · 외부 blob 83)
① 정상              대화 16 · Q/A 26쌍 · 복원율 100.0% (52/52턴)
② 대화 삭제         "Lunch menu recommendations" 질문·답변·타임스탬프 복원
                     (현재 뷰에는 없음 / 000003.log 오프셋 598357, seq 3584)
③ 계정 삭제         잔존 프롬프트 1건 — KEEP-7742 (Session Storage)
                     WIPE-3318 / PASTE-9160 / LONG-01 → 완전 소멸
소요                 30~50초 (파일 1,626개)
```

## 검증 (디스크 이미지 층, CASE-B01.E01 · 60 GB · 22조각)

정답지는 **같은 E01 을 Arsenal Image Mounter 로 붙인 드라이브**다. 같은
이미지·같은 순간이므로, 내용이 다르면 그건 이쪽 파서가 틀린 것이다.

```
바이트 대조          84 / 84 일치 · 불일치 0 · 1,068,076,450 B
  크기대별           ~512B / 512B~1KB / 1KB~64KB / 64KB~1MB / 1~16MB / 16MB~
  최대 파일          348,273,480 B (msedge.dll) · 302,371,110 B (mager.onnxe)
  시스템 압축 포함    XPRESS8K 37건 전부 일치
$MFT 대조            게스트에서 KAPE 로 직접 뜬 것과 391,380,992 B 크기 동일
  레코드 일치         376,820 / 382,208 (98.59%)
  구조 차이           0 — 나머지는 전부 MFT 슬롯 재사용(두 스냅샷 시점 차이)
파티션 인식          마운트 시 붙은 D:/E:/F: 와 완전 일치
자체 점검            9 / 9 통과  (--selftest)
전 구간              E01 -> 최종 리포트 52.8초
```

수집물 경로와 이미지 경로가 **같은 숫자**에 도달한다.

| | KAPE 수집물 | E01 직접 |
|---|---|---|
| Q/A 쌍 | 25 | 25 |
| 복원율(턴) | 100.0% (50/50) | 100.0% (50/50) |
| 복원율(본문) | 49.6% (97,207/196,155) | 49.6% (97,207/196,155) |
| 대조군 `VOID-9999` | 0건 | 0건 |

### 만드는 동안 잡은 것 — 전부 '값은 그럴듯한데 틀린' 부류

| 무엇 | 어떻게 드러났나 |
|---|---|
| WOF 재분석 지점을 한 겹으로 읽음 | 알고리즘 필드가 밀려 **전부 LZX 로 보였다** |
| XPRESS 길이 확장 `0xFF` 경로를 12만큼 오산 | 270바이트 넘는 매치에서만 터져 **가운데 덩어리 7개는 멀쩡**했다 |
| `$MFT` 픽스업 2중 적용 | 레코드가 조용히 `None` 이 되어 **파티션 판정이 틀렸다** |
| `.log` 를 EnCase `.LAA` 세그먼트로 오인 | 엉뚱한 파일을 이미지 조각으로 이어 붙일 뻔 |

전부 예외가 안 나고 길이도 맞는다. 눈으로는 안 잡힌다. 그래서 정답지를
두고 바이트로 대조했고, 같은 자리를 `--selftest` 에 고정 벡터로 묶었다.

## 한계

- **LZX 로 압축된 시스템 파일은 못 푼다.** XPRESS 4K/8K/16K 만 된다.
  다만 0 을 내용인 척 내보내지 않고 `wof_failed` 로 명시한다.
  실험 이미지에서는 해당 0건(전부 XPRESS8K)이었다.
- Ex01(EnCase7) · VHD / VHDX · QCOW · AFF 는 읽지 않는다. 형식을 이름으로
  알려주고 멈춘다.
- BitLocker 볼륨은 암호화된 채로는 못 읽는다. 다만 "NTFS 없음" 이 아니라
  **BitLocker 라고 말하고**, Logical Drive 로 다시 뜨라고 안내한다.
- 이미지에서 꺼낼 때 파일 하나당 512 MB 상한.
- 토큰류(`jwt`/`sk_token`/`session_key`)는 **존재와 오프셋만** 기록하고 값은 앞
  12자만 남긴다. 리포트가 그대로 자격증명이 되면 안 된다.
- 컴팩션 후에는 옛 값이 물리적으로 사라진다. 수집이 이를수록 복원률이 높다.
- 외부 blob 은 레코드가 밀려날 때 Chromium 이 같이 지운다. 수집으로는 못 살린다.
- 외부 blob 은 **선언된 크기**로 찾는다. 디스크 이미지 전체를 입력으로 주면
  우연히 같은 크기인 파일이 걸릴 수 있어 blob 저장소 경로를 우선하도록 했다.
  판단 근거는 `findings.csv` 의 `external` 열에 남는다.
- 파일당 256 MB 상한. `$MFT` / `$J` 는 건너뛴다 — MFTECmd 몫이다.
- 앱 동봉 문서(`SKILL.md` 등)에도 `"prompt":"..."` 스키마 예시가 있어 오탐이
  난다. 신뢰도를 깎아 두었으니 `--min-confidence 80` 으로 거른다.
- `$MFT` 파서는 **$ATTRIBUTE_LIST 를 따라가지 않는다.** 파일 이름이 확장
  레코드에 있으면 DOS 8.3 이름(`MICROS~1.DLL`)만 얻고 `dos_only` 로 표시한다.
  전체의 0.5% 수준이고 Windows 시스템 파일에 몰려 있다. **Claude 아티팩트
  2,220건에서는 0건**이라 이 용도에는 영향이 없다.
- 삭제 시각 추정은 기준점이 드문 구간에서 넓어진다. 신뢰도 열을 함께 본다.
