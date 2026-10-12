<#
Start AeroChorus on this PC (or check what is running). The same as
deploy\windows\start-aerochorus.ps1; see the runbook, "Start here".

  .\start-aerochorus.ps1          start whatever is not running
  .\start-aerochorus.ps1 -Check   only report what is running
#>
& (Join-Path $PSScriptRoot "deploy\windows\start-aerochorus.ps1") @args
exit $LASTEXITCODE
