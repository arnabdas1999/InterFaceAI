# SynthCore Desktop 4.2 - a synthetic legacy Windows Forms client for the same member-inquiry flow.
# Synthetic data only. Deliberately legacy: the Member ID box has no accessible name (its label is a
# separate static control to its left), results are plain labels, and "not found" is a status line.
param([string]$Fault = "")

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()

$members = @{
    "12345" = @{ Name = "Avery Quinn"; Balance = '$1,234.56' }
    "67890" = @{ Name = "Jordan Blake"; Balance = '$15,020.00' }
    "24680" = @{ Name = "Riley Moss"; Balance = '$88.40' }
}

$form = New-Object System.Windows.Forms.Form
$form.Text = "SynthCore Desktop 4.2 - Member Inquiry"
$form.Size = New-Object System.Drawing.Size(560, 320)
$form.StartPosition = "Manual"
$form.Location = New-Object System.Drawing.Point(40, 40)
$form.Font = New-Object System.Drawing.Font("Tahoma", 9)

function Add-Label($text, $x, $y, $name, $bold = $false) {
    $l = New-Object System.Windows.Forms.Label
    $l.Text = $text; $l.Name = $name; $l.AutoSize = $true
    $l.Location = New-Object System.Drawing.Point($x, $y)
    if ($bold) { $l.Font = New-Object System.Drawing.Font("Tahoma", 9, [System.Drawing.FontStyle]::Bold) }
    $form.Controls.Add($l); return $l
}

Add-Label "Member Inquiry" 16 12 "lblTitle" $true | Out-Null
Add-Label "Member ID:" 16 48 "lblMemberId" | Out-Null
$txt = New-Object System.Windows.Forms.TextBox
$txt.Name = "txtMbr"; $txt.Location = New-Object System.Drawing.Point(150, 45); $txt.Width = 120
$txt.AccessibleName = ""   # legacy: no accessible name wired to the label
$form.Controls.Add($txt)

$btn = New-Object System.Windows.Forms.Button
$btn.Text = "Search"; $btn.Name = "btnSearch"; $btn.Location = New-Object System.Drawing.Point(290, 43)
$form.Controls.Add($btn)
$form.AcceptButton = $btn

Add-Label "Member:" 16 100 "lblMember" | Out-Null
$memberValue = Add-Label "" 150 100 "valMember"
Add-Label "Name:" 16 130 "lblName" | Out-Null
$nameValue = Add-Label "" 150 130 "valName"
Add-Label "Share Savings Balance:" 16 160 "lblBalance" | Out-Null
$balanceValue = Add-Label "" 190 160 "valBalance"
$status = Add-Label "" 16 210 "lblStatus"

$btn.Add_Click({
    $id = $txt.Text.Trim()
    $memberValue.Text = ""; $nameValue.Text = ""; $balanceValue.Text = ""; $status.Text = ""
    if ($Fault -eq "permission_denied") { $status.Text = "Access Denied: you do not have permission to view balances (ERR-SEC-403)."; return }
    if ($id -notmatch '^\d{5}$') { $status.Text = "Member ID must be exactly 5 digits."; return }
    if (-not $members.ContainsKey($id)) { $status.Text = "No records found for the search criteria."; return }
    $m = $members[$id]
    $memberValue.Text = $id
    $nameValue.Text = $m.Name
    $balanceValue.Text = $m.Balance
    $status.Text = "Record loaded for member $id."
})

[void]$form.ShowDialog()
