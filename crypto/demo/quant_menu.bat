@echo off
chcp 65001 >nul
title 多币种量化数据管理系统

:menu
cls
echo ========================================
echo 多币种量化数据管理系统
echo ========================================
echo.
echo 请选择操作模式：
echo.
echo 1. 同步历史数据 (所有币种)
echo 2. 同步历史数据 (指定币种)
echo 3. 计算技术指标 (所有币种)
echo 4. 启动WebSocket实时更新 (BTC+ETH)
echo 5. 启动WebSocket实时更新 (所有币种)
echo 6. 数据质量校验 (高频)
echo 7. 数据质量校验 (全量)
echo 8. 完整流程 (历史+指标+校验)
echo 9. 退出
echo.
set /p choice=请输入选项 (1-9): 

if "%choice%"=="1" goto history_all
if "%choice%"=="2" goto history_custom
if "%choice%"=="3" goto indicator_all
if "%choice%"=="4" goto ws_btc_eth
if "%choice%"=="5" goto ws_all
if "%choice%"=="6" goto validate_high
if "%choice%"=="7" goto validate_full
if "%choice%"=="8" goto full_process
if "%choice%"=="9" goto exit

echo 无效选项，请重新选择
pause
goto menu

:history_all
echo 🚀 同步所有币种历史数据...
python demo61_quant_db_manager.py --mode history --symbols all
pause
goto menu

:history_custom
echo 请输入币种列表 (用逗号分隔，如: BTC-USDT-SWAP,ETH-USDT-SWAP)
set /p symbols=币种列表: 
echo 🚀 同步指定币种历史数据...
python demo61_quant_db_manager.py --mode history --symbols %symbols%
pause
goto menu

:indicator_all
echo 🔄 计算所有币种技术指标...
python demo61_quant_db_manager.py --mode indicator --symbols all
pause
goto menu

:ws_btc_eth
echo 🔌 启动WebSocket实时更新 (BTC+ETH)...
echo 💡 按Ctrl+C停止
python demo61_quant_db_manager.py --mode ws --symbols BTC-USDT-SWAP,ETH-USDT-SWAP
pause
goto menu

:ws_all
echo 🔌 启动WebSocket实时更新 (所有币种)...
echo 💡 按Ctrl+C停止
python demo61_quant_db_manager.py --mode ws --symbols all
pause
goto menu

:validate_high
echo 🔍 执行高频数据校验...
python demo61_quant_db_manager.py --mode validate --level high
pause
goto menu

:validate_full
echo 🔍 执行全量数据校验...
python demo61_quant_db_manager.py --mode validate --level full
pause
goto menu

:full_process
echo 🚀 执行完整流程 (历史数据 + 技术指标 + 数据校验)...
echo.
echo 步骤1: 同步历史数据
python demo61_quant_db_manager.py --mode history --symbols all
if %errorlevel% neq 0 goto error
echo.
echo 步骤2: 计算技术指标
python demo61_quant_db_manager.py --mode indicator --symbols all
if %errorlevel% neq 0 goto error
echo.
echo 步骤3: 数据质量校验
python demo61_quant_db_manager.py --mode validate --level full
if %errorlevel% neq 0 goto error
echo.
echo 🎉 完整流程执行成功！
pause
goto menu

:error
echo ❌ 执行过程中出现错误，错误代码: %errorlevel%
pause
goto menu

:exit
echo 👋 感谢使用！
exit