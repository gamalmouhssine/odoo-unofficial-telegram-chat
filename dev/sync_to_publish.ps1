# Copies the module, docs and dev tools from the workspace into the publish
# repository (D:\ProjectOdoo\publish\odoo-unofficial-telegram-chat), without
# compiled Python files. Run it before committing a new version.
$ErrorActionPreference = 'Stop'
$workspace = 'D:\ProjectOdoo'
$repo = Join-Path $workspace 'publish\odoo-unofficial-telegram-chat'

$copies = @(
    @{ From = 'MyOdooModules\telegram_personal_client'; To = 'telegram_personal_client' },
    @{ From = 'docs'; To = 'docs' },
    @{ From = 'telegram_dev'; To = 'dev' }
)
foreach ($copy in $copies) {
    $source = Join-Path $workspace $copy.From
    $target = Join-Path $repo $copy.To
    # /MIR mirrors (also removes files deleted in the workspace); /XD skips caches.
    robocopy $source $target /MIR /XD __pycache__ .pytest_cache /XF *.pyc /NFL /NDL /NJH /NJS /NP | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "robocopy failed for $($copy.From) (exit $LASTEXITCODE)" }
}
Copy-Item (Join-Path $repo 'telegram_personal_client\LICENSE') (Join-Path $repo 'LICENSE') -Force
Write-Output "Synchronized into $repo"
exit 0  # robocopy's "files copied" codes (1-7) are success
