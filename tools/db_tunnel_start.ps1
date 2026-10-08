# 启动香港 MySQL 隧道守护：本地 127.0.0.1:13306 -> (7892代理+SSH隧道) -> 43.161.202.105 内网 3306
# 依赖：1) Nolock/Clash 的 7892 混合端口处于开启
#       2) SSH 密码存于 ~/.ssh/ubuntu_43_pass（仅当前用户可读）
# 停止：直接关掉守护窗口（Ctrl+C）或任务管理器结束对应 python 进程
$ErrorActionPreference = 'Stop'

# 已在跑就不重复启动
$running = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -match 'db_tunnel_guard' }
if ($running) {
    Write-Output "[隧道] 守护已在运行 PID=$($running.ProcessId -join ',')，跳过启动"
    exit 0
}

Write-Output '[隧道] 启动 db_tunnel_guard.py（独立窗口）...'
Start-Process -FilePath 'python' -ArgumentList 'd:\python\LifeQuant\tools\db_tunnel_guard.py'
Start-Sleep -Seconds 8

$listen = Get-NetTCPConnection -LocalPort 13306 -State Listen -ErrorAction SilentlyContinue
if ($listen) {
    Write-Output '[隧道] 成功：本地 127.0.0.1:13306 已监听，项目 db_url 指向该端口即走香港库'
} else {
    Write-Output '[隧道] 失败：本地 13306 未监听。请确认 7892 代理已开启、~/.ssh/ubuntu_43_pass 存在'
    exit 1
}
