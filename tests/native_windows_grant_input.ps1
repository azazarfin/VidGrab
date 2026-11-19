param([Parameter(Mandatory=$true)][string]$Request,[Parameter(Mandatory=$true)][string]$Report,[switch]$OwnedReply)
$ErrorActionPreference='Stop';$ProgressPreference='SilentlyContinue'
$receipt=[ordered]@{result='failed';input='UIAutomation InvokePattern'}
try {
 if($env:COMPUTERNAME -cne 'DESKTOP-OSOIBNA' -or $env:USERNAME -ine 'Azaz' -or $env:VIDGRAB_VALIDATION_VM_ISOLATED -cne '1'){throw 'Foreign validation environment'}
 if((Get-Item -LiteralPath $Request).Length -gt 8192){throw 'Native input requires a fresh bounded request'}
 if($OwnedReply){
  $reply=Get-Item -LiteralPath $Report
  if(($reply.Attributes -band [IO.FileAttributes]::ReparsePoint) -or $reply.Length -ne 2 -or (Get-Content -LiteralPath $Report -Raw) -cne '{}'){throw 'Owned reply placeholder changed'}
  if((Get-Acl -LiteralPath $Report).Owner -cne (Get-Acl -LiteralPath $Request).Owner){throw 'Reply and request have different owners'}
 }elseif(Test-Path -LiteralPath $Report){throw 'Existing native reply refused'}
 $expected=Get-Content -LiteralPath $Request -Raw | ConvertFrom-Json
 if($expected.choice -cnotin @('deny','allow') -or $expected.browser -cnotin @('chrome','firefox')){throw 'Unknown native input'}
 $process=Get-Process -Id $expected.pid
 if($process.SessionId -ne 1 -or $process.Path -cne $expected.program -or $process.StartTime.ToFileTimeUtc() -ne $expected.creation_filetime){throw 'Selected browser creation identity changed'}
 if((Get-FileHash -LiteralPath $process.Path -Algorithm SHA256).Hash.ToLower() -cne $expected.program_sha256){throw 'Selected browser executable changed'}
 if([Diagnostics.Process]::GetCurrentProcess().SessionId -ne $process.SessionId){throw 'Input must run in the selected interactive session'}
 Add-Type -AssemblyName UIAutomationClient;Add-Type -AssemblyName UIAutomationTypes
 Add-Type -AssemblyName System.Drawing;Add-Type -AssemblyName System.Windows.Forms
 $principal=[Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent())
 $receipt.user=$env:USERNAME;$receipt.session=$process.SessionId;$receipt.input_administrator=$principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
 $condition=[Windows.Automation.PropertyCondition]::new([Windows.Automation.AutomationElement]::ProcessIdProperty,[int]$expected.pid)
 $names=if($expected.choice -ceq 'allow'){@('Allow','Add permissions','Allow permissions')}else{@('Deny','Cancel',"Don't Allow")}
 $deadline=[DateTime]::UtcNow.AddSeconds(15);$selected=$null;$observed=@()
 do {
  $elements=[Windows.Automation.AutomationElement]::RootElement.FindAll([Windows.Automation.TreeScope]::Descendants,$condition)
  $visible=@($elements | Where-Object {-not $_.Current.IsOffscreen -and $_.Current.IsEnabled -and -not $_.Current.BoundingRectangle.IsEmpty})
  $observed=@($visible | Select-Object -First 256 | ForEach-Object {[ordered]@{name=$_.Current.Name;id=$_.Current.AutomationId;type=$_.Current.ControlType.ProgrammaticName;pid=$_.Current.ProcessId;rectangle=$_.Current.BoundingRectangle.ToString();focused=$_.Current.HasKeyboardFocus}})
  $choices=@($visible | Where-Object {$_.Current.ControlType -eq [Windows.Automation.ControlType]::Button -and $_.Current.Name -cin $names})
  if($choices.Count -gt 1){throw 'Ambiguous native permission choices'}
  if($choices.Count -eq 1 -and @($visible | Where-Object {$_.Current.Name -clike '*VidGrab*'}).Count -gt 0){$selected=$choices[0];break}
  Start-Sleep -Milliseconds 100
 }while([DateTime]::UtcNow -lt $deadline)
 $receipt.controls=$observed
 if($null -eq $selected){throw 'No unique visible VidGrab native permission choice before the deadline'}
 $bounds=[Windows.Forms.Screen]::PrimaryScreen.Bounds;$image=[Drawing.Bitmap]::new($bounds.Width,$bounds.Height);$graphics=[Drawing.Graphics]::FromImage($image)
 try {$graphics.CopyFromScreen($bounds.Location,[Drawing.Point]::Empty,$bounds.Size);$image.Save($Report+'.png',[Drawing.Imaging.ImageFormat]::Png)}finally{$graphics.Dispose();$image.Dispose()}
 if($process.HasExited -or $process.StartTime.ToFileTimeUtc() -ne $expected.creation_filetime){throw 'Selected browser exited before input'}
 $receipt.target=[ordered]@{name=$selected.Current.Name;id=$selected.Current.AutomationId;pid=$selected.Current.ProcessId;rectangle=$selected.Current.BoundingRectangle.ToString()}
 $pattern=$selected.GetCurrentPattern([Windows.Automation.InvokePattern]::Pattern)
 $pattern.Invoke()
 $receipt.acknowledgement=$expected;$receipt.result='passed'
}catch{$receipt.error=$_.Exception.ToString()}
[IO.File]::WriteAllText($Report,($receipt|ConvertTo-Json -Depth 8),[Text.UTF8Encoding]::new($false))
$ready=[IO.Path]::ChangeExtension($Report,'.ready')
if(Test-Path -LiteralPath $ready){throw 'Existing native reply marker'}
[IO.File]::WriteAllText($ready,'',[Text.UTF8Encoding]::new($false))
if($receipt.result -cne 'passed'){exit 1}
