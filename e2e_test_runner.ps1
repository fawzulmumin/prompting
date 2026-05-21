# e2e_test_runner.ps1 — End-to-end test runner against live local server
# Usage: .\e2e_test_runner.ps1
# Server: http://localhost:4000/api/ask

$BASE_URL = "http://localhost:4000/api/ask"
$TIMEOUT  = 120  # seconds per request

$queries = @(
    # Grup A
    @{ id="A1"; group="A"; q="SMA mana saja yang belum terakreditasi di Provinsi Jakarta?" },
    @{ id="A2"; group="A"; q="SD yang masih menunggu akreditasi di Bandung" },
    @{ id="A3"; group="A"; q="berapa SD di desa cibunar kecamatan parungpanjang" },
    # Grup B
    @{ id="B4"; group="B"; q="berapa total siswa di Jawa Barat" },
    @{ id="B5"; group="B"; q="jumlah guru di DKI Jakarta" },
    @{ id="B6"; group="B"; q="berapa tenaga kependidikan SD di Jawa Timur" },
    @{ id="B7"; group="B"; q="jumlah siswa SMA negeri di Surabaya" },
    # Grup C
    @{ id="C8"; group="C"; q="berapa sekolah di pulau Jawa" },
    @{ id="C9"; group="C"; q="daftar SMA di Kalimantan" },
    @{ id="C10"; group="C"; q="berapa siswa di Sumatera" },
    # Grup D
    @{ id="D11"; group="D"; q="SMA di Jakarta" },
    @{ id="D12"; group="D"; q="profil SMAN 1 Bandung" },
    @{ id="D13"; group="D"; q="bandingkan jumlah sekolah di Bandung vs Surabaya" },
    @{ id="D14"; group="D"; q="berapa sekolah negeri di Indonesia" },
    @{ id="D15"; group="D"; q="sekolah terdekat dari titik koordinat" },
    # Grup E
    @{ id="E16"; group="E"; q="bandingkan jumlah siswa SMA di Bandung vs Surabaya" }
)

$results = @()

Write-Host "`n=== E2E TEST RUNNER — $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ===" -ForegroundColor Cyan
Write-Host "Target: $BASE_URL`n" -ForegroundColor Gray

foreach ($item in $queries) {
    $id    = $item.id
    $group = $item.group
    $q     = $item.q

    Write-Host "[$id] $q" -ForegroundColor Yellow

    $bodyJson = @{ question = $q } | ConvertTo-Json -Compress
    $status   = "ERROR"
    $provider = "-"
    $numDocs  = "-"
    $module   = "-"
    $metricT  = "-"
    $scope    = "-"
    $wilayah  = "-"
    $confident = "-"
    $snippet  = ""
    $errMsg   = ""

    try {
        $resp = Invoke-RestMethod `
            -Uri $BASE_URL `
            -Method Post `
            -ContentType "application/json; charset=utf-8" `
            -Body ([System.Text.Encoding]::UTF8.GetBytes($bodyJson)) `
            -TimeoutSec $TIMEOUT

        # Extract key fields
        $provider  = if ($resp.provider)         { $resp.provider }          else { "-" }
        $numDocs   = if ($null -ne $resp.num_docs){ $resp.num_docs }          else { "-" }
        $confident = if ($null -ne $resp.answer_confident) { $resp.answer_confident } else { "-" }
        $snippet   = if ($resp.answer_markdown)  {
            $md = $resp.answer_markdown -replace "`n", " "
            if ($md.Length -gt 120) { $md.Substring(0, 120) + "..." } else { $md }
        } else { "(no answer_markdown)" }

        # Intent fields (may be nested under .intent or top-level)
        $intentObj = if ($resp.intent)        { $resp.intent }
                     elseif ($resp.debug_intent) { $resp.debug_intent }
                     else { $null }
        if ($intentObj) {
            $module  = if ($intentObj.module)        { $intentObj.module }        else { "-" }
            $metricT = if ($intentObj.metric_target) { $intentObj.metric_target } else { "null" }
            $scope   = if ($intentObj.scope)         { $intentObj.scope }         else { "-" }
            $wil     = $intentObj.wilayah
            if ($wil) {
                $parts = @()
                if ($wil.provinsi)   { $parts += "prov=$($wil.provinsi)" }
                if ($wil.kabupaten)  { $parts += "kab=$($wil.kabupaten)" }
                if ($wil.kecamatan) { $parts += "kec=$($wil.kecamatan)" }
                if ($wil.pulau)     { $parts += "pulau=$($wil.pulau)" }
                $wilayah = if ($parts) { $parts -join ", " } else { "-" }
            }
        }

        $status = if ($resp.answer_confident -eq $true)  { "PASS" }
                  elseif ($resp.answer_confident -eq $false) { "FAIL?" }
                  else { "OK" }

    } catch {
        $errMsg = $_.Exception.Message
        if ($errMsg.Length -gt 80) { $errMsg = $errMsg.Substring(0, 80) + "..." }
        $status = "ERROR"
        $snippet = "ERROR: $errMsg"
    }

    $row = [PSCustomObject]@{
        ID        = $id
        Group     = $group
        Status    = $status
        Provider  = $provider
        NumDocs   = $numDocs
        Module    = $module
        Metric    = $metricT
        Scope     = $scope
        Wilayah   = $wilayah
        Confident = $confident
        Snippet   = $snippet
    }
    $results += $row

    # Print per-query summary
    $color = switch ($status) {
        "PASS"  { "Green" }
        "FAIL?" { "Red" }
        "ERROR" { "Magenta" }
        default { "White" }
    }
    Write-Host "  Status    : $status" -ForegroundColor $color
    Write-Host "  Provider  : $provider | num_docs=$numDocs | confident=$confident"
    Write-Host "  Intent    : module=$module | metric=$metricT | scope=$scope | $wilayah"
    Write-Host "  Snippet   : $snippet" -ForegroundColor Gray
    Write-Host ""
}

# ── Summary table ────────────────────────────────────────────────────────────
Write-Host "`n=== SUMMARY TABLE ===" -ForegroundColor Cyan
$results | Format-Table ID, Group, Status, Provider, NumDocs, Module, Metric, Scope -AutoSize

# Per-group count
Write-Host "`n=== PASS/FAIL PER GRUP ===" -ForegroundColor Cyan
foreach ($g in @("A","B","C","D","E")) {
    $grp   = $results | Where-Object { $_.Group -eq $g }
    $total = $grp.Count
    $pass  = ($grp | Where-Object { $_.Status -eq "PASS" -or $_.Status -eq "OK" }).Count
    $fail  = $total - $pass
    Write-Host "  Grup $g : $pass/$total pass  ($fail fail)" -ForegroundColor $(if ($fail -eq 0) {"Green"} else {"Yellow"})
}

$totalPass = ($results | Where-Object { $_.Status -eq "PASS" -or $_.Status -eq "OK" }).Count
$totalAll  = $results.Count
Write-Host "`n  TOTAL: $totalPass/$totalAll pass" -ForegroundColor $(if ($totalPass -eq $totalAll) {"Green"} else {"Yellow"})
Write-Host ""
