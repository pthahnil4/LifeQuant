$j = Get-Content data\_okx_list_tools.json -Raw | ConvertFrom-Json
# 判定规则：CLI 路径最后一段以写动词开头 => 写操作（WRITE）
$w = 'place','cancel','amend','close','transfer','leverage','create','stop','batch','set','trail','purchase','redeem','subscribe','withdraw','adjust'
foreach ($m in $j.modules) {
  $tot = $m.commands.Count
  if ($tot -eq 0) { continue }
  $wr = @($m.commands | Where-Object {
    $leaf = ($_.path -split '\s+')[-1]
    [bool]($w | Where-Object { $leaf -eq $_ -or $leaf.StartsWith("$_-") })
  })
  $names = ($wr | ForEach-Object { $_.path -replace '^okx ', '' }) -join '; '
  Write-Output ("MODULE {0}  total={1}  write={2}  read={3}" -f $m.name, $tot, $wr.Count, ($tot - $wr.Count))
  Write-Output ("   WRITE: " + $names)
}
