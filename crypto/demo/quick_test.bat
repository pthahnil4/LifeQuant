@echo off
chcp 65001 >nul
echo 🎯 Demo61 模块化系统快速测试
echo =======================================

echo.
echo 📍 当前目录: %CD%
echo 📅 测试时间: %DATE% %TIME%
echo.

echo 🔄 1. 检查Python环境...
python --version
if errorlevel 1 (
    echo ❌ Python环境异常，请检查Python是否正确安装
    pause
    exit /b 1
)

echo.
echo 🔄 2. 检查项目依赖...
python -c "import influxdb_client, pandas, numpy; print('✅ 主要依赖包检查通过')" 2>nul
if errorlevel 1 (
    echo ⚠️ 缺少依赖包，正在安装...
    pip install influxdb-client pandas numpy
)

echo.
echo 🔄 3. 运行系统测试...
python test_modular_system.py
if errorlevel 1 (
    echo ❌ 系统测试失败，请检查错误信息
    pause
    exit /b 1
)

echo.
echo 🔄 4. 运行单币种测试...
echo    正在测试 BTC-USDT-SWAP 5m 历史数据...
python demo61_quant_db_manager.py --mode history --symbols BTC-USDT-SWAP --periods 5m --verbose
if errorlevel 1 (
    echo ❌ 历史数据测试失败
    goto show_log
)

echo.
echo 🔄 5. 运行数据校验...
python demo61_quant_db_manager.py --mode validate --level high --symbols BTC-USDT-SWAP --periods 5m
if errorlevel 1 (
    echo ❌ 数据校验失败
    goto show_log
)

echo.
echo 🎉 所有测试通过！系统可以正常使用
echo.
echo 💡 接下来您可以尝试：
echo    • 全币种历史数据: python demo61_quant_db_manager.py --mode history --symbols all
echo    • WebSocket实时: python demo61_quant_db_manager.py --mode ws --symbols BTC-USDT-SWAP --periods 5m
echo    • 技术指标计算: python demo61_quant_db_manager.py --mode indicator --symbols all
echo.
goto end

:show_log
echo.
echo 📋 查看详细日志信息:
echo ========================================
if exist "quant_db_manager.log" (
    echo 📄 最新日志内容:
    powershell "Get-Content quant_db_manager.log -Tail 20"
) else (
    echo ⚠️ 日志文件不存在
)

:end
echo.
echo 📊 测试完成！按任意键退出...
pause >nul
