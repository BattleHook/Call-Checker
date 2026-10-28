param(
    [string]$DatabasePath,
    [switch]$ReadCalls,
    [string]$Table,
    [string]$Column,
    [Alias('H')][switch]$Help
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
if ($Help -or $DatabasePath -in @('/H', '/h', '/?') -or -not $DatabasePath) {
    Get-Content -LiteralPath (Join-Path $PSScriptRoot 'CALL_Checker_SourceSchema_help.txt')
    return
}
$connection = $null
try {
    if ([Environment]::Is64BitProcess) { throw 'Run the Access schema reader with 32-bit PowerShell.' }
    if (-not (Test-Path -LiteralPath $DatabasePath -PathType Leaf)) {
        throw "Source database not found: $DatabasePath"
    }
    $builder = New-Object System.Data.OleDb.OleDbConnectionStringBuilder
    $builder.Provider = 'Microsoft.Jet.OLEDB.4.0'
    if ([System.IO.Path]::GetExtension($DatabasePath) -ieq '.accdb') {
        $builder.Provider = 'Microsoft.ACE.OLEDB.12.0'
    }
    $builder['Data Source'] = $DatabasePath
    $builder['Mode'] = 'Read'
    $connection = New-Object System.Data.OleDb.OleDbConnection($builder.ConnectionString)
    $connection.Open()
    $tables = $connection.GetSchema('Tables')
    $columns = $connection.GetSchema('Columns')
    $result = @(
        foreach ($tableRow in $tables.Rows) {
            if ($tableRow['TABLE_TYPE'] -ne 'TABLE') { continue }
            $name = [string]$tableRow['TABLE_NAME']
            $names = @(
                foreach ($columnRow in $columns.Rows) {
                    if ([string]$columnRow['TABLE_NAME'] -eq $name) {
                        [string]$columnRow['COLUMN_NAME']
                    }
                }
            )
            [pscustomobject]@{ table = $name; columns = $names }
        }
    )
    if ($ReadCalls) {
        $selected = @($result | Where-Object { $_.table -ieq $Table -and $_.columns -icontains $Column })
        if ($selected.Count -ne 1 -or $Column -notmatch '^(Call|Callsign)$') {
            throw 'Selected CALL/CALLSIGN column was not found.'
        }
        $command = $connection.CreateCommand()
        $reader = $null
        try {
            $quotedTable = '[' + $Table.Replace(']', ']]') + ']'
            $quotedColumn = '[' + $Column.Replace(']', ']]') + ']'
            $command.CommandText = "SELECT DISTINCT $quotedColumn FROM $quotedTable"
            $reader = $command.ExecuteReader()
            $calls = New-Object System.Collections.Generic.List[object]
            while ($reader.Read()) {
                if ($reader.IsDBNull(0)) { $calls.Add($null) }
                else { $calls.Add([string]$reader.GetValue(0)) }
            }
            [Console]::Out.WriteLine((ConvertTo-Json -InputObject $calls.ToArray() -Compress))
        }
        finally {
            if ($null -ne $reader) { $reader.Dispose() }
            $command.Dispose()
        }
    }
    else {
        [Console]::Out.WriteLine((ConvertTo-Json -InputObject $result -Depth 4 -Compress))
    }
}
catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 1
}
finally {
    if ($null -ne $connection) { $connection.Dispose() }
}
