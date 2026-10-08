@echo off
chcp 65001 >nul
echo ========================================
echo 多币种量化数据管理系统批处理工具
echo ========================================
echo.

cd /d "%~dp0"

echo 🚀 步骤1: 同步所有币种历史数据
echo ----------------------------------------
python demo61_quant_db_manager.py --mode history --symbols all
if %errorlevel% neq 0 (
    echo ❌ 历史数据同步失败，错误代码: %errorlevel%
    pause
    exit /b %errorlevel%
)
echo ✅ 历史数据同步完成
echo.

echo 🔄 步骤2: 计算技术指标
echo ----------------------------------------
python demo61_quant_db_manager.py --mode indicator --symbols all
if %errorlevel% neq 0 (
    echo ❌ 技术指标计算失败，错误代码: %errorlevel%
    pause
    exit /b %errorlevel%
)
echo ✅ 技术指标计算完成
echo.

echo 🔍 步骤3: 数据质量校验
echo ----------------------------------------
python demo61_quant_db_manager.py --mode validate --level full
if %errorlevel% neq 0 (
    echo ❌ 数据质量校验失败，错误代码: %errorlevel%
    pause
    exit /b %errorlevel%
)
echo ✅ 数据质量校验完成
echo.

echo 🎉 所有步骤执行完成！
echo.
echo 💡 如需启动WebSocket实时更新，请运行:
echo    python demo61_quant_db_manager.py --mode ws --symbols BTC-USDT-SWAP,ETH-USDT-SWAP
echo.
pause