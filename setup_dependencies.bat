@echo off
setlocal EnableExtensions EnableDelayedExpansion

REM ============================================================
REM LDModuleSystem_V5.0 의존성 설정 스크립트
REM ============================================================

title LDModuleSystem_V5.0 Dependency Setup

REM 이 BAT 파일이 있는 폴더를 기준으로 사용합니다.
REM 따라서 PC마다 경로가 달라도 동작합니다.
set "BASE_DIR=%~dp0"
set "INSTALLER_DIR=%BASE_DIR%installer"

echo.
echo ============================================================
echo   LDModuleSystem_V5.0 의존성 설정
echo ============================================================
echo.
echo 기준 폴더:
echo %BASE_DIR%
echo.

REM ------------------------------------------------------------
REM 관리자 권한 확인
REM ------------------------------------------------------------
net session >nul 2>&1

if not "%errorlevel%"=="0" (
    echo [오류] 관리자 권한이 필요합니다.
    echo.
    echo setup_dependencies.bat를 마우스 오른쪽 버튼으로 클릭한 뒤
    echo "관리자 권한으로 실행"을 선택하세요.
    echo.
    pause
    exit /b 1
)

REM ------------------------------------------------------------
REM installer 폴더 확인
REM ------------------------------------------------------------
if not exist "%INSTALLER_DIR%" (
    echo [오류] installer 폴더가 없습니다.
    echo 다음 폴더를 만들어 주세요:
    echo %INSTALLER_DIR%
    echo.
    pause
    exit /b 1
)

REM ------------------------------------------------------------
REM Visual C++ x86 런타임 설치
REM ------------------------------------------------------------
echo [1/4] Visual C++ x86 런타임 확인 중...

set "VC_INSTALLER=%INSTALLER_DIR%\vc_redist.x86.exe"

if exist "%VC_INSTALLER%" (
    echo [확인] vc_redist.x86.exe를 찾았습니다.
    echo [설치] Visual C++ x86 런타임 설치를 시작합니다.
    echo.

    start /wait "" "%VC_INSTALLER%" /install /quiet /norestart

    if errorlevel 3010 (
        echo [안내] Visual C++ 설치가 완료되었습니다.
        echo [안내] 재부팅이 필요할 수 있습니다.
    ) else if errorlevel 1 (
        echo [경고] Visual C++ 설치 결과 코드가 %errorlevel%입니다.
        echo 이미 설치된 경우 계속 진행합니다.
    ) else (
        echo [완료] Visual C++ x86 런타임 처리가 완료되었습니다.
    )
) else (
    echo [경고] vc_redist.x86.exe를 찾지 못했습니다.
    echo 경로:
    echo %VC_INSTALLER%
)

echo.

REM ------------------------------------------------------------
REM 32비트 Python 검색
REM ------------------------------------------------------------
echo [2/4] Python 3.11 32비트 검색 중...

set "PY32_EXE="

REM 이미 등록된 환경 변수 확인
if defined GRAPHETC_PYTHON32 (
    if exist "%GRAPHETC_PYTHON32%" (
        set "PY32_EXE=%GRAPHETC_PYTHON32%"
        echo [확인] GRAPHETC_PYTHON32 환경 변수를 사용합니다.
    )
)

REM 일반적인 사용자 설치 경로 확인
if not defined PY32_EXE (
    if exist "%LocalAppData%\Programs\Python\Python311-32\python.exe" (
        set "PY32_EXE=%LocalAppData%\Programs\Python\Python311-32\python.exe"
    )
)

REM 일반적인 전체 사용자 설치 경로 확인
if not defined PY32_EXE (
    if exist "%ProgramFiles(x86)%\Python311-32\python.exe" (
        set "PY32_EXE=%ProgramFiles(x86)%\Python311-32\python.exe"
    )
)

if not defined PY32_EXE (
    if exist "%ProgramFiles(x86)%\Python311\python.exe" (
        set "PY32_EXE=%ProgramFiles(x86)%\Python311\python.exe"
    )
)

REM Python Launcher로 확인
if not defined PY32_EXE (
    for /f "delims=" %%P in (
        'py -3.11-32 -c "import sys; print(sys.executable)" 2^>nul'
    ) do (
        set "PY32_EXE=%%P"
    )
)

REM ------------------------------------------------------------
REM Python 비트 수 확인
REM ------------------------------------------------------------
if defined PY32_EXE (
    echo [확인] Python 실행 파일:
    echo %PY32_EXE%

    for /f "delims=" %%B in (
        '"%PY32_EXE%" -c "import struct; print(struct.calcsize(\"P\") * 8)"'
    ) do (
        set "PY_BITS=%%B"
    )

    if "%PY_BITS%"=="32" (
        echo [완료] 32비트 Python을 확인했습니다.
    ) else (
        echo [경고] 확인된 Python은 %PY_BITS%비트입니다.
        echo [안내] Graphtec DLL은 32비트 Python에서 실행해야 합니다.
        set "PY32_EXE="
    )
)

REM ------------------------------------------------------------
REM Python이 없을 때만 설치 프로그램 실행
REM ------------------------------------------------------------
if not defined PY32_EXE (
    set "PY_INSTALLER=%INSTALLER_DIR%\python-3.11.9.exe"

    if not exist "%PY_INSTALLER%" (
        echo [오류] 32비트 Python을 찾지 못했습니다.
        echo Python 설치 파일도 없습니다.
        echo 경로:
        echo %PY_INSTALLER%
        echo.
        pause
        exit /b 1
    )

    echo.
    echo [3/4] Python 3.11.9 32비트 설치 프로그램을 실행합니다.
    echo 설치 화면에서 32비트 Python인지 확인하세요.
    echo.

    start /wait "" "%PY_INSTALLER%" InstallAllUsers=0 PrependPath=1 Include_launcher=1

    if errorlevel 1 (
        echo [오류] Python 설치가 취소되었거나 실패했습니다.
        echo.
        pause
        exit /b 1
    )

    REM 설치 후 표준 경로 재확인
    if exist "%LocalAppData%\Programs\Python\Python311-32\python.exe" (
        set "PY32_EXE=%LocalAppData%\Programs\Python\Python311-32\python.exe"
    )

    if not defined PY32_EXE (
        if exist "%ProgramFiles(x86)%\Python311-32\python.exe" (
            set "PY32_EXE=%ProgramFiles(x86)%\Python311-32\python.exe"
        )
    )
) else (
    echo [3/4] 기존 Python을 사용하므로 설치를 건너뜁니다.
)

if not defined PY32_EXE (
    echo [오류] 32비트 Python 실행 파일을 확인하지 못했습니다.
    echo.
    pause
    exit /b 1
)

REM ------------------------------------------------------------
REM Graphtec DLL 배치
REM ------------------------------------------------------------
echo.
echo [4/4] Graphtec DLL 설정 중...

set "DLL_SOURCE=%INSTALLER_DIR%\gtcusbr.dll"
set "DLL_TARGET=%BASE_DIR%gtcusbr.dll"

if exist "%DLL_TARGET%" (
    echo [확인] 배포 폴더에 gtcusbr.dll이 이미 있습니다.
) else if exist "%DLL_SOURCE%" (
    copy /Y "%DLL_SOURCE%" "%DLL_TARGET%" >nul

    if exist "%DLL_TARGET%" (
        echo [완료] gtcusbr.dll을 배포 폴더로 복사했습니다.
    ) else (
        echo [오류] gtcusbr.dll 복사에 실패했습니다.
        pause
        exit /b 1
    )
) else (
    echo [오류] gtcusbr.dll을 찾지 못했습니다.
    echo 다음 위치 중 한 곳에 파일을 넣어 주세요.
    echo %DLL_SOURCE%
    echo %DLL_TARGET%
    echo.
    pause
    exit /b 1
)

REM ------------------------------------------------------------
REM GRAPHETC_PYTHON32 환경 변수 등록
REM ------------------------------------------------------------
echo.
echo 환경 변수 등록 중...

setx GRAPHETC_PYTHON32 "%PY32_EXE%" >nul

if errorlevel 1 (
    echo [경고] GRAPHETC_PYTHON32 환경 변수 등록에 실패했습니다.
) else (
    echo [완료] GRAPHETC_PYTHON32 환경 변수를 등록했습니다.
)

echo.
echo ============================================================
echo   설정이 완료되었습니다.
echo ============================================================
echo.
echo Python:
echo %PY32_EXE%
echo.
echo Graphtec DLL:
echo %DLL_TARGET%
echo.
echo 새로 연 터미널부터 환경 변수가 적용됩니다.
echo 필요하면 PC를 재부팅한 후 프로그램을 실행하세요.
echo.
pause

endlocal
exit /b 0
