# Grava a imagem do Raspberry Pi OS no cartao SD e aplica a configuracao headless.
# Executar ELEVADO (admin). Uso:
#   powershell -ExecutionPolicy Bypass -File flash-sd.ps1 -DiskNumber 3 -ExpectedSize 32010928128 -Dir D:\SDs\obd
param(
  [Parameter(Mandatory)] [int]$DiskNumber,
  [Parameter(Mandatory)] [long]$ExpectedSize,
  [Parameter(Mandatory)] [string]$Dir,
  [switch]$SkipWrite
)
$ErrorActionPreference = 'Stop'
$log = Join-Path $Dir 'flash.log'
function Log($m) { $l = "$(Get-Date -Format s) $m"; Write-Host $l; Add-Content -Path $log -Value $l }
Set-Content -Path $log -Value "inicio"
try {
  $img = Get-ChildItem $Dir -Filter *.img | Select-Object -First 1
  if (-not $img) { throw "imagem .img nao encontrada em $Dir" }

  # --- Travas de seguranca: so grava no disco USB removivel esperado ---
  $d = Get-Disk -Number $DiskNumber
  if ($d.BusType -ne 'USB') { throw "Disco $DiskNumber nao e USB ($($d.BusType))" }
  if ($d.Size -ne $ExpectedSize) { throw "Tamanho inesperado: $($d.Size)" }
  if ($d.IsBoot -or $d.IsSystem) { throw "Disco $DiskNumber e de sistema!" }
  Log "Alvo: disco $DiskNumber $($d.FriendlyName) $($d.Size) bytes; imagem $($img.Name) $($img.Length) bytes"

  $buf = New-Object byte[] (4MB)
  if (-not $SkipWrite) {
  # Limpa o disco (remove particoes/volumes para liberar escrita raw)
  if ($d.PartitionStyle -ne 'RAW') { Clear-Disk -Number $DiskNumber -RemoveData -RemoveOEM -Confirm:$false; Log "Disco limpo" }

  # --- Escrita raw ---

  $src = [IO.File]::OpenRead($img.FullName)
  $dst = New-Object IO.FileStream("\\.\PhysicalDrive$DiskNumber", [IO.FileMode]::Open, [IO.FileAccess]::ReadWrite, [IO.FileShare]::ReadWrite)
  $total = 0; $sha = [Security.Cryptography.SHA256]::Create()
  while (($n = $src.Read($buf, 0, $buf.Length)) -gt 0) {
    if ($n % 512 -ne 0) { $n = [int]([math]::Ceiling($n / 512) * 512) }
    $dst.Write($buf, 0, $n); $null = $sha.TransformBlock($buf, 0, $n, $null, 0); $total += $n
    if (($total % 256MB) -eq 0) { Log ("gravado {0:N0} MB" -f ($total / 1MB)) }
  }
  $null = $sha.TransformFinalBlock($buf, 0, 0); $hashSrc = [BitConverter]::ToString($sha.Hash)
  $dst.Flush(); $dst.Close(); $src.Close()
  Log "Escrita concluida: $total bytes"
  } else {
    $total = $img.Length; $s = [IO.File]::OpenRead($img.FullName); $hashSrc = [BitConverter]::ToString([Security.Cryptography.SHA256]::Create().ComputeHash($s)); $s.Close(); Log "SkipWrite: hash da imagem calculado"
  }

  # --- Verificacao (le de volta e compara SHA256) ---
  $sha2 = [Security.Cryptography.SHA256]::Create()
  $rd = New-Object IO.FileStream("\\.\PhysicalDrive$DiskNumber", [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
  $left = $total
  while ($left -gt 0) { $want = [int][math]::Min([long]$buf.Length, [long]$left); $n = $rd.Read($buf, 0, $want); if ($n -le 0) { break }; $null = $sha2.TransformBlock($buf, 0, $n, $null, 0); $left -= $n }
  $null = $sha2.TransformFinalBlock($buf, 0, 0); $rd.Close()
  $hashDst = [BitConverter]::ToString($sha2.Hash)
  if ($hashSrc -ne $hashDst) { throw "VERIFICACAO FALHOU: $hashSrc != $hashDst" }
  Log "Verificacao OK (SHA256 identico)"

  # --- Monta a particao boot e aplica config headless ---
  Update-HostStorageCache; Start-Sleep 3
  $p = Get-Partition -DiskNumber $DiskNumber -PartitionNumber 1
  if (-not $p.DriveLetter -or $p.DriveLetter -eq "`0") { $p | Add-PartitionAccessPath -AssignDriveLetter; Start-Sleep 2; $p = Get-Partition -DiskNumber $DiskNumber -PartitionNumber 1 }
  $boot = "$($p.DriveLetter):\"
  Log "Boot montado em $boot"
  Copy-Item (Join-Path $Dir 'user-data') $boot -Force
  Copy-Item (Join-Path $Dir 'network-config') $boot -Force
  [IO.File]::WriteAllBytes((Join-Path $boot 'ssh'), [byte[]]@())
  $cmd = Join-Path $boot 'cmdline.txt'
  $c = ([IO.File]::ReadAllText($cmd)).Trim()
  if ($c -notmatch 'ieee80211_regdom') { $c = "$c cfg80211.ieee80211_regdom=BR" }
  [IO.File]::WriteAllText($cmd, "$c`n", (New-Object Text.UTF8Encoding($false)))
  Log "cmdline: $c"
  Log ("Arquivos boot: " + ((Get-ChildItem $boot | Select-Object -ExpandProperty Name) -join ', '))
  Log "SUCESSO"
} catch {
  Log "ERRO: $_"
}
