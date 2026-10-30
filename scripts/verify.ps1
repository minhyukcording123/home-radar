<#
  HOME RADAR 전체 검증 — 한 번에 돌린다.

    .\scripts\verify.ps1              # 서버가 떠 있다고 가정
    .\scripts\verify.ps1 -Start       # 서버를 직접 띄우고 끝나면 끈다

  검사 항목
    1. 구문·임포트 (compileall)
    2. 회귀 테스트 (네트워크 미사용)
    3. 수집원 상태 (키·robots·마지막 성공)
    4. API 응답 건강도
    5. 판정 정확성 불변식
    6. 인증키 유출 점검
    7. 개인정보가 git 에 올라가지 않는지
#>
param([switch]$Start)

$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $py)) { throw "가상환경이 없습니다: $py" }

$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'
Set-Location $root

$fails = @()
function Step($name, $block) {
    Write-Host ""
    Write-Host "=== $name ===" -ForegroundColor Cyan
    $ok = & $block
    if (-not $ok) { $script:fails += $name }
}

$proc = $null
if ($Start) {
    Write-Host "서버를 띄웁니다 (127.0.0.1:8777)..."
    $proc = Start-Process -FilePath $py -PassThru -WindowStyle Hidden `
        -ArgumentList '-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', '8777'
    foreach ($i in 1..40) {
        try { Invoke-RestMethod http://127.0.0.1:8777/api/health -TimeoutSec 2 | Out-Null; break }
        catch { Start-Sleep -Milliseconds 700 }
    }
}

Step "1. 구문·임포트" {
    & $py -m compileall -q app tests
    if ($LASTEXITCODE -eq 0) { Write-Host "  OK   compileall"; $true }
    else { Write-Host "  FAIL compileall"; $false }
}

Step "2. 회귀 테스트" {
    $out = & $py -m unittest discover -s tests 2>&1 | Out-String
    $m = [regex]::Match($out, 'Ran (\d+) tests')
    $n = if ($m.Success) { $m.Groups[1].Value } else { '?' }
    if ($out -match '\bOK\b') { Write-Host "  OK   테스트 $n 개 통과"; $true }
    else { Write-Host "  FAIL 테스트 실패:`n$out"; $false }
}

Step "3. 수집원 상태" {
    & $py -m app sources 2>&1 | ForEach-Object { Write-Host ("  " + $_) }
    $true
}

Step "4. API 건강도 · 5. 정확성 불변식 · 6. 키 유출" {
    # 파이썬을 PowerShell 에 인라인으로 넣으면 따옴표가 인자 파싱에 깨진다 →
    # 같은 폴더의 verify_api.py 를 호출한다.
    # 출력은 Write-Host 로 흘려보내야 한다 — 파이프라인에 남으면 Step 이
    # 반환값으로 집어삼켜 화면에 아무것도 안 보인다.
    & $py (Join-Path $PSScriptRoot 'verify_api.py') 2>&1 |
        ForEach-Object { Write-Host ("  " + $_) }
    $LASTEXITCODE -eq 0
}

Step "7. 개인정보·비밀값이 git 에 없는지" {
    $ok = $true
    foreach ($p in @('.env', 'profile.json', 'data/')) {
        $tracked = git ls-files $p
        if ($tracked) { Write-Host "  FAIL $p 가 git 에 추적됨"; $ok = $false }
        else { Write-Host "  OK   $p 추적 안 됨" }
    }
    $dirty = git status --short | Where-Object { $_ -match '\.env$|profile\.json$' }
    if ($dirty) { Write-Host "  FAIL git status 에 비밀 파일이 보임"; $ok = $false }
    else { Write-Host "  OK   git status 에 비밀 파일 없음" }
    $ok
}

if ($proc) { Write-Host ""; Write-Host "서버를 끕니다..."; Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue }

Write-Host ""
Write-Host ("=" * 54)
if ($fails.Count) {
    Write-Host "실패 $($fails.Count)건:" -ForegroundColor Red
    $fails | ForEach-Object { Write-Host "  - $_" -ForegroundColor Red }
    exit 1
}
Write-Host "전부 통과" -ForegroundColor Green
