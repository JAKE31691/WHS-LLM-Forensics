# collect-min.ps1 - Claude Desktop 대화 아티팩트 최소 수집
#
# app 폴더를 통째로 /E 하면 58 MB 다. 대화 복원에 실제로 쓰이는 건
# 아래 경로뿐이고 합쳐서 2 MB 남짓이다.
#
# [중요] 실행 전에 Claude Desktop 을 완전히 종료할 것 (트레이 아이콘까지).
#        실행 중이면 LevelDB 가 잠겨 최신 레코드가 누락된다.
#
# 사용:  .\collect-min.ps1 DEL-T2
#        .\collect-min.ps1 DEL-T2 -Full      (Cache 까지 전부)

param(
    [Parameter(Mandatory = $true)][string]$Name,
    [switch]$Full,
    [string]$Dest = 'C:\Analysis'
)

$APP = "$env:LOCALAPPDATA\Packages\Claude_pzs8sxrjxfjjc\LocalCache\Roaming\Claude"
if (-not (Test-Path $APP)) {
    $APP = "$env:APPDATA\Claude"          # MSIX 가상화가 안 걸린 설치본 대비
}
if (-not (Test-Path $APP)) {
    Write-Host "앱 저장소를 찾을 수 없습니다." -ForegroundColor Red
    exit 1
}

$proc = Get-Process -Name 'Claude' -ErrorAction SilentlyContinue
if ($proc) {
    Write-Host "경고: Claude 가 실행 중입니다. 종료 후 다시 수집하세요." -ForegroundColor Yellow
}

$OUT = Join-Path $Dest "$Name\app"
New-Item $OUT -ItemType Directory -Force | Out-Null

if ($Full) {
    $PARTS = @('')
} else {
    # IndexedDB       대화 본문 + 외부 blob. 복원 1순위
    # Session Storage 프롬프트. 계정 삭제 후에도 잔존
    # Local Storage   계정 식별자
    # Network         Cookies. 삭제 레코드는 프리블록에 남는다
    # claude-local    동작 타임라인
    $PARTS = @('IndexedDB', 'Session Storage', 'Local Storage',
               'Network', 'claude-local\logs')
}

Write-Host "수집 시작 : $APP"
foreach ($p in $PARTS) {
    if ($p) { $src = Join-Path $APP $p; $dst = Join-Path $OUT $p }
    else    { $src = $APP;              $dst = $OUT }

    if (-not (Test-Path $src)) {
        Write-Host ("  없음  : {0}" -f $p)
        continue
    }
    robocopy $src $dst /E /R:0 /W:0 /NFL /NDL /NJH /NJS | Out-Null
    if ($p) { Write-Host ("  복사  : {0}" -f $p) }
    else    { Write-Host "  복사  : (전체)" }
}

# robocopy 는 정상 복사에도 1 을 반환한다. 오류로 보이지 않게 정리한다.
$global:LASTEXITCODE = 0

$files = Get-ChildItem $OUT -Recurse -File
$mb = ($files | Measure-Object Length -Sum).Sum / 1MB
"{0} : {1:N0}개 / {2:N2} MB  ->  {3}" -f $Name, $files.Count, $mb, $OUT
exit 0
