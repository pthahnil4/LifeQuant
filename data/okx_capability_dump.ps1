# OKX 交易能力清单 —— 证据重生成脚本
# 用法: powershell -NoProfile -ExecutionPolicy Bypass -File data\okx_capability_dump.ps1
# 产出: _okx_list_tools.json / _okx_tools_flat.txt / _okx_param_detail.txt
$ErrorActionPreference = 'Stop'
Set-Location (Split-Path -Parent $PSScriptRoot)

# 1) 拉取官方机器可读 schema（需已 npm install -g @okx_ai/okx-trade-cli）
$okx = (Get-Command okx -ErrorAction SilentlyContinue)
if (-not $okx) { $prefix = (npm prefix -g); $okx = Join-Path $prefix 'okx.cmd' }
else { $okx = $okx.Source }
& $okx list-tools --json | Out-File -FilePath 'data\_okx_list_tools.json' -Encoding utf8
$j = Get-Content data\_okx_list_tools.json -Raw | ConvertFrom-Json
"OKX Agent Trade Kit v$($j.version)  totalTools=$($j.totalTools)  modules=$($j.modules.Count)"

# 2) 扁平化：CLI path || MCP toolName || 必填 || 选填 || 描述
$flat = New-Object System.Text.StringBuilder
foreach ($m in $j.modules) {
  [void]$flat.AppendLine("### MODULE: $($m.name)  -- $($m.description)")
  foreach ($c in $m.commands) {
    $req = ($c.parameters | Where-Object { $_.required } | ForEach-Object { $_.name }) -join ","
    $opt = ($c.parameters | Where-Object { -not $_.required } | ForEach-Object { $_.name }) -join ","
    [void]$flat.AppendLine("$($c.path)  ||TOOL=$($c.toolName)  ||REQ=$req  ||OPT=$opt  ||DESC=$($c.description)")
  }
  [void]$flat.AppendLine("")
}
[System.IO.File]::WriteAllText("$PWD\data\_okx_tools_flat.txt", $flat.ToString(), [System.Text.Encoding]::UTF8)

# 3) 关键下单工具的逐参数明细（ordType 枚举、拆单参数族都在这里）
$targets = @('spot_place_order','swap_place_order','swap_place_algo_order','futures_place_algo_order',
             'spot_place_algo_order','swap_place_move_stop_order','spot_batch_orders','swap_batch_orders',
             'swap_close_position','spot_amend_order','swap_amend_algo_order','account_set_position_mode',
             'swap_set_leverage','grid_create_order','grid_amend_order','dca_create_order')
$detail = New-Object System.Text.StringBuilder
foreach ($m in $j.modules) {
  foreach ($c in $m.commands) {
    if ($targets -notcontains $c.toolName) { continue }
    [void]$detail.AppendLine("############ $($c.path)  [$($c.toolName)]")
    [void]$detail.AppendLine("DESC: $($c.description)")
    foreach ($p in $c.parameters) {
      $enumTxt = ""; if ($p.enum) { $enumTxt = " ENUM=" + (($p.enum) -join "|") }
      $need = if ($p.required) { 'required' } else { 'optional' }
      [void]$detail.AppendLine("  - $($p.name) ($($p.type), $need)$enumTxt :: $($p.description)")
    }
    [void]$detail.AppendLine("")
  }
}
[System.IO.File]::WriteAllText("$PWD\data\_okx_param_detail.txt", $detail.ToString(), [System.Text.Encoding]::UTF8)

foreach ($f in @('_okx_list_tools.json','_okx_tools_flat.txt','_okx_param_detail.txt')) {
  "  -> data\$f  " + (Get-Item "data\$f").Length + " bytes"
}
