@echo off
chcp 65001 >nul
echo ========================================
echo WebSocket实时数据更新
echo ========================================
echo.

cd /d "%~dp0"

echo 🔌 启动WebSocket实时更新 (BTC + ETH)
echo ----------------------------------------
echo 💡 提示：按Ctrl+C可停止实时更新
echo.
python demo61_quant_db_manager.py --mode ws --symbols BTC-USDT-SWAP,ETH-USDT-SWAP

echo.
echo 👋 WebSocket连接已断开
pause