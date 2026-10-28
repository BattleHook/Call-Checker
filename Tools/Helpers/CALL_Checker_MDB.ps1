param(
    [string]$DatabasePath,
    [string]$SearchText,
    [Alias('H')][switch]$Help
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
if ($Help -or $DatabasePath -in @('/H', '/h', '/?') -or -not $DatabasePath) {
    Get-Content -LiteralPath (Join-Path $PSScriptRoot 'CALL_Checker_MDB_help.txt')
    return
}

if ([Environment]::Is64BitProcess) {
    [Console]::Error.WriteLine('The Access reader requires 32-bit PowerShell.')
    exit 1
}
if (-not (Test-Path -LiteralPath $DatabasePath -PathType Leaf)) {
    [Console]::Error.WriteLine("SpotCollector database not found: $DatabasePath")
    exit 1
}
if ($SearchText -notmatch '^[A-Z0-9?/*]{3,}$') {
    [Console]::Error.WriteLine('Search text must contain at least three valid callsign characters.')
    exit 1
}

$connection = $null
try {
    $connectionString = "Provider=Microsoft.Jet.OLEDB.4.0;Data Source=$DatabasePath;Mode=Read"
    $connection = New-Object System.Data.OleDb.OleDbConnection($connectionString)
    $connection.Open()

    function Get-CallsignMatches([string]$query, [string]$value) {
        $command = $connection.CreateCommand()
        $command.CommandText = $query
        [void]$command.Parameters.AddWithValue('@search', $value)
        $reader = $command.ExecuteReader()
        $callRows = New-Object System.Collections.Generic.List[object]
        try {
            while ($reader.Read()) {
                $callsign = [string]$reader['Callsign']
                $spotCount = 0
                if (-not ($reader['SpotCount'] -is [System.DBNull])) {
                    $spotCount = [int]$reader['SpotCount']
                }
                $callRows.Add([pscustomobject]@{
                    callsign   = $callsign
                    spot_count = $spotCount
                })
            }
        }
        finally {
            $reader.Close()
            $command.Dispose()
        }
        return $callRows.ToArray()
    }

    $results = New-Object System.Collections.Generic.List[object]
    $seen = @{}
    $exactMatches = @(Get-CallsignMatches `
        'SELECT TOP 40 [Callsign], [SpotCount] FROM [Spots] WHERE InStr(1, [Callsign], ?, 1) > 0 ORDER BY [SpotCount] DESC' `
        $SearchText)
    foreach ($match in $exactMatches) {
        if (-not $seen.ContainsKey($match.callsign)) {
            $seen[$match.callsign] = $true
            $results.Add($match)
        }
    }

    if ($exactMatches.Count -lt 5) {
        $broadMatches = @(Get-CallsignMatches `
            'SELECT TOP 100 [Callsign], [SpotCount] FROM [Spots] WHERE Left([Callsign], 2) = ? ORDER BY [SpotCount] DESC' `
            $SearchText.Substring(0, 2))
        foreach ($match in $broadMatches) {
            if (-not $seen.ContainsKey($match.callsign)) {
                $seen[$match.callsign] = $true
                $results.Add($match)
            }
        }
    }

    $json = ConvertTo-Json -InputObject ([object[]]$results.ToArray()) -Compress
    [Console]::Out.WriteLine($json)
}
catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 1
}
finally {
    if ($null -ne $connection) {
        $connection.Close()
        $connection.Dispose()
    }
}
