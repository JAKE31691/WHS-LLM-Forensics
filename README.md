# WHS-LLM-Forensics

Windows 11의 ChatGPT와 Claude 사용 흔적을 수집·파싱하고, 명시된 규칙으로 사용자 행위를 분석하는 Python 도구 모음입니다. `common`은 네 환경을 자동 탐지·분석하는 **통합 도구**이고, 나머지 네 환경 폴더에는 각각 **단독 실행할 수 있는 개별 도구**가 있습니다. 통합 도구는 개별 도구의 기존 파서와 판정 규칙을 사용합니다.

## 도구 구조

| 종류 | 경로 | 기능과 실행 안내 |
| --- | --- | --- |
| 통합 도구 | [`common/`](common/) | ChatGPT Desktop·Web, Claude Desktop·Web의 흔적을 확인하고 발견된 환경만 분석. [실행 안내](common/README.md) |
| 개별 도구 | [`chatgpt_app/`](chatgpt_app/) | ChatGPT Desktop만 분석. [실행 안내](chatgpt_app/README.md) |
| 개별 도구 | [`chatgpt_web/`](chatgpt_web/) | ChatGPT Web만 분석. [실행 안내](chatgpt_web/LLMTrace_최종본/README.md) |
| 개별 도구 | [`claude_app/`](claude_app/) | Claude Desktop만 분석. [실행 안내](claude_app/README.md) |
| 개별 도구 | [`claude_web/`](claude_web/) | Claude Web만 분석. [실행 안내](claude_web/tools/README.md) |
| 문서 | [`docs/`](docs/) | 설계 및 실험 문서 |

**어느 도구를 실행할까?** 분석 환경을 미리 정하지 않고 네 환경을 한 번에 확인하려면 `common`을 실행합니다. 특정 환경 하나만 분석하려면 해당 환경 폴더의 개별 도구를 실행합니다. 개별 도구마다 명령어, 의존성 및 출력 형식이 다를 수 있으므로 해당 README를 참고하세요.

## 통합 도구 설치 및 실행

Windows 11, Python 3.12 64비트, Git이 필요합니다. 저장소 전체를 내려받고 저장소 루트에서 다음 명령을 실행합니다.

```cmd
py -3.12 -m venv common\.venv
common\.venv\Scripts\python.exe -m pip install --upgrade pip
common\.venv\Scripts\python.exe -m pip install --only-binary=zstd -r common\requirements.txt
```

입력은 정확히 두 개입니다. `source`에는 `live` 또는 첫 E01 세그먼트의 경로를, `output`에는 새 결과 폴더의 절대 경로를 지정합니다. 분할 E01의 나머지 세그먼트는 첫 세그먼트와 같은 폴더에 둡니다.

```cmd
common\act_trace.cmd live "C:\Result\live01"
common\act_trace.cmd "C:\Evidence\case01.E01" "C:\Result\case01"
```

Live 수집은 관리자 권한의 Windows 터미널에서 실행합니다. VSS 생성에 실패하면 실행이 중단됩니다. E01은 읽기 전용으로 열며, 원래 드라이브 문자를 확인하지 못한 볼륨에는 임의의 문자를 지정하지 않습니다. 상세한 실행·검증 방법은 [통합 도구 안내](common/README.md)를 참고하세요.

## 통합 도구 분석 흐름과 결과

1. **환경 탐지:** 수집한 흔적을 확인해 분석할 환경을 선택합니다. 환경의 흔적만으로 사용자 행위를 확정하지 않습니다.
2. **수집:** 원본을 수정하지 않고 아티팩트를 복사합니다. 수집 실패와 파일 부재를 구분합니다.
3. **파싱·판정:** 수집본에서 확인한 값을 각 환경의 규칙과 비교하고 직접·보조 근거를 구분합니다. 파싱 실패는 수집 실패와 별도로 기록합니다.
4. **보고:** 탐지된 환경마다 `<output>\<환경 이름>\` 아래에 결과를 생성합니다.

| 파일 | 내용 |
| --- | --- |
| `parsed_artifacts.json` | 파싱한 기록, 원본·수집 경로, 처리 상태 및 오류. |
| `LLM_Artifact_Report.csv` | 사용자 행위와 매칭된 아티팩트·등급·근거를 담은 표. |
| `LLM_Artifact_Report.html` | 분석 결과를 탐색할 수 있는 독립 실행형 보고서. |

수집 ZIP의 제공 여부와 이름은 현재 GitHub 버전에서 환경별로 다릅니다. ChatGPT Desktop과 Claude Desktop은 `collected_artifacts.zip`, Claude Web은 `LLM_Artifacts.zip`을 생성하며, ChatGPT Web은 ZIP 없이 위 세 보고 파일을 생성합니다. 환경별 상세 출력은 [통합 도구 안내](common/README.md)를 참고하세요.

아티팩트가 없거나 일부 처리에 실패해도 가능한 결과는 생성하며, 누락과 오류는 CLI와 결과에 표시합니다. 실제 E01·Live 사례의 분석 범위는 이미지 내용, 접근 권한, 파일 손상 및 설치된 파서 의존성에 영향을 받습니다.

## 검증

```cmd
common\.venv\Scripts\python.exe -m pip install pytest
common\.venv\Scripts\python.exe -m pytest common\tests -q
```

가공한 테스트 데이터로 환경 탐지, 경로·ZIP 보존, 실패 후 계속 실행, 네 환경의 결과 파일 생성을 검사합니다. 실제 Windows Live와 E01의 전체 실행은 별도 사례에서 확인해야 합니다.

## 라이선스와 데이터

프로젝트가 작성한 소스코드와 문서는 [MIT 라이선스](LICENSE)로 공개합니다. 저장소에 포함된 외부 코드는 각자의 라이선스를 유지하며 [외부 코드 고지](THIRD_PARTY_NOTICES.md)에 정리했습니다. `도구결과물_예시` 등 분석 결과와 증거 데이터는 이 코드 라이선스의 대상이 아닙니다. 공개하거나 재배포할 때에는 데이터에 대한 별도 권한과 민감정보 포함 여부를 확인해야 합니다.
