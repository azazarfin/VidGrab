param([Parameter(Mandatory=$true)][string]$Request,[Parameter(Mandatory=$true)][string]$Report)
$ErrorActionPreference='Stop';$receipt=[ordered]@{result='failed';input='UIAutomation Load unpacked; Win32 WM_SETTEXT and BM_CLICK'}
try {
 if($env:COMPUTERNAME -cne 'DESKTOP-OSOIBNA' -or $env:USERNAME -ine 'Azaz' -or $env:VIDGRAB_VALIDATION_VM_ISOLATED -cne '1'){throw 'Foreign validation environment'}
 $requestFile=Get-Item -LiteralPath $Request;$reply=Get-Item -LiteralPath $Report
 if($requestFile.Length -gt 8192 -or ($requestFile.Attributes -band [IO.FileAttributes]::ReparsePoint)){throw 'Unsafe request'}
 if(($reply.Attributes -band [IO.FileAttributes]::ReparsePoint) -or $reply.Length -ne 2 -or (Get-Content -LiteralPath $Report -Raw) -cne '{}'){throw 'Owned reply changed'}
 if((Get-Acl -LiteralPath $Report).Owner -cne (Get-Acl -LiteralPath $Request).Owner){throw 'Reply owner differs'}
 $expected=Get-Content -LiteralPath $Request -Raw|ConvertFrom-Json
 if($expected.browser -cne 'chrome' -or $expected.choice -cne 'load-unpacked'){throw 'Foreign folder choice'}
 $process=Get-Process -Id $expected.pid
 if($process.SessionId -ne 1 -or $process.Path -cne $expected.program -or $process.StartTime.ToFileTimeUtc() -ne $expected.creation_filetime){throw 'Browser identity changed'}
 if([Diagnostics.Process]::GetCurrentProcess().SessionId -ne 1){throw 'Wrong interactive session'}
 if((Get-FileHash -LiteralPath $process.Path -Algorithm SHA256).Hash.ToLower() -cne $expected.program_sha256){throw 'Browser executable changed'}
 $folder=Get-Item -LiteralPath $expected.extension
 if(!$folder.PSIsContainer -or ($folder.Attributes -band [IO.FileAttributes]::ReparsePoint)){throw 'Unsafe extension directory'}
 Add-Type -AssemblyName UIAutomationClient;Add-Type -AssemblyName UIAutomationTypes
 Add-Type -AssemblyName System.Drawing;Add-Type -AssemblyName System.Windows.Forms
 Add-Type -TypeDefinition @'
using System; using System.Text; using System.Collections.Generic; using System.Runtime.InteropServices;
public static class VidGrabFolderWindow {
 [DllImport("user32.dll")] public static extern IntPtr GetWindow(IntPtr h, uint command);
 [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
 [DllImport("user32.dll")] static extern bool EnumWindows(EnumProc callback, IntPtr data);
 [DllImport("user32.dll", CharSet=CharSet.Unicode)] static extern int GetWindowText(IntPtr h, StringBuilder text, int count);
 [DllImport("user32.dll", CharSet=CharSet.Unicode)] static extern int GetClassName(IntPtr h, StringBuilder text, int count);
 [DllImport("user32.dll")] static extern bool IsWindowVisible(IntPtr h);
 [DllImport("user32.dll")] static extern bool IsWindowEnabled(IntPtr h);
 [DllImport("user32.dll")] static extern int GetDlgCtrlID(IntPtr h);
 [DllImport("user32.dll")] static extern bool EnumChildWindows(IntPtr h, EnumProc callback, IntPtr data);
 [DllImport("user32.dll", CharSet=CharSet.Unicode)] static extern IntPtr SendMessageTimeout(IntPtr h, uint message, IntPtr w, string text, uint flags, uint timeout, out IntPtr result);
 [DllImport("user32.dll", CharSet=CharSet.Unicode, EntryPoint="SendMessageTimeoutW")] static extern IntPtr ReadMessageTimeout(IntPtr h, uint message, IntPtr w, StringBuilder text, uint flags, uint timeout, out IntPtr result);
 delegate bool EnumProc(IntPtr h, IntPtr data);
 public class Window { public long hwnd; public uint pid; public string name; public string className; public bool visible; public bool enabled; public int id; }
 static Window Read(IntPtr h) {
  var name=new StringBuilder(32768);var cls=new StringBuilder(1024);uint pid;
  GetWindowText(h,name,32768);GetClassName(h,cls,1024);GetWindowThreadProcessId(h,out pid);
  return new Window {hwnd=h.ToInt64(),pid=pid,name=name.ToString(),className=cls.ToString(),visible=IsWindowVisible(h),enabled=IsWindowEnabled(h),id=GetDlgCtrlID(h)};
 }
 public static Window[] Children(IntPtr root) {
  var items=new List<Window>();EnumChildWindows(root,(h,data)=>{items.Add(Read(h));return true;},IntPtr.Zero);return items.ToArray();
 }
 public static string Text(IntPtr h) {
  IntPtr length;if(SendMessageTimeout(h,14,IntPtr.Zero,null,2,1000,out length)==IntPtr.Zero) throw new Exception("Native text length unavailable");
  var count=length.ToInt32();if(count<0 || count>8192) throw new Exception("Native folder text exceeds bound");
  var text=new StringBuilder(count+1);IntPtr copied;
  if(ReadMessageTimeout(h,13,new IntPtr(count+1),text,2,1000,out copied)==IntPtr.Zero || copied.ToInt32()!=count) throw new Exception("Native folder text read failed");
  return text.ToString();
 }
 public static bool SetText(IntPtr h,string text) {IntPtr result;return SendMessageTimeout(h,12,IntPtr.Zero,text,2,1000,out result)!=IntPtr.Zero && result!=IntPtr.Zero;}
 public static bool Click(IntPtr h) {IntPtr result;return SendMessageTimeout(h,245,IntPtr.Zero,null,2,1000,out result)!=IntPtr.Zero;}
 public static Window[] Snapshot() {
  var windows=new List<Window>();
  EnumWindows((h,data)=>{var name=new StringBuilder(1024);var cls=new StringBuilder(1024);uint pid;
   GetWindowText(h,name,1024);GetClassName(h,cls,1024);GetWindowThreadProcessId(h,out pid);
   windows.Add(new Window { hwnd=h.ToInt64(),pid=pid,name=name.ToString(),className=cls.ToString(),visible=IsWindowVisible(h) });return true;
  },IntPtr.Zero);return windows.ToArray();
 }
}
'@
 $initial=@([VidGrabFolderWindow]::Snapshot()|ForEach-Object {$_.hwnd})
 $armed=[IO.Path]::ChangeExtension($Report,'.armed.json');$armedItem=Get-Item -LiteralPath $armed
 if(($armedItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -or $armedItem.Length -ne 2 -or (Get-Content -LiteralPath $armed -Raw) -cne '{}' -or (Get-Acl -LiteralPath $armed).Owner -cne (Get-Acl -LiteralPath $Request).Owner){throw 'Armed reply ownership changed'}
 [IO.File]::WriteAllText($armed,(@{stage='armed-before-dialog';acknowledgement=$expected;initial_hwnds=$initial}|ConvertTo-Json -Depth 8),[Text.UTF8Encoding]::new($false))
 $armedReady=[IO.Path]::ChangeExtension($armed,'.ready');if(Test-Path -LiteralPath $armedReady){throw 'Existing armed marker'}
 [IO.File]::WriteAllText($armedReady,'',[Text.UTF8Encoding]::new($false))
 $condition=[Windows.Automation.PropertyCondition]::new([Windows.Automation.AutomationElement]::ProcessIdProperty,[int]$expected.pid)
 $browserControls=[Windows.Automation.AutomationElement]::RootElement.FindAll([Windows.Automation.TreeScope]::Descendants,$condition)
 $load=@($browserControls|Where-Object {$_.Current.AutomationId -ceq 'loadUnpacked' -and $_.Current.Name -ceq 'Load unpacked' -and $_.Current.IsEnabled -and -not $_.Current.IsOffscreen})
 if($load.Count -ne 1){throw 'No unique owned native Load unpacked button'}
 $receipt.load_button=@{pid=$load[0].Current.ProcessId;id=$load[0].Current.AutomationId;name=$load[0].Current.Name}
 $load[0].GetCurrentPattern([Windows.Automation.InvokePattern]::Pattern).Invoke()
 $deadline=[DateTime]::UtcNow.AddSeconds(15);$dialog=$null
 do {
  $windows=[VidGrabFolderWindow]::Snapshot()
  $dialogs=@($windows|Where-Object {$_.className -ceq '#32770' -and $_.name -ceq 'Select the extension directory.' -and $_.visible -and $_.hwnd -notin $initial})
  if($dialogs.Count -gt 1){throw 'Ambiguous folder dialog'}
  if($dialogs.Count -eq 1){$dialog=[Windows.Automation.AutomationElement]::FromHandle([IntPtr]$dialogs[0].hwnd);break}
  Start-Sleep -Milliseconds 100
 }while([DateTime]::UtcNow -lt $deadline)
 if($null -eq $dialog){
  $receipt.win32_windows=$windows
  $receipt.desktop_windows=@([Windows.Automation.AutomationElement]::RootElement.FindAll([Windows.Automation.TreeScope]::Children,[Windows.Automation.Condition]::TrueCondition)|ForEach-Object {@{name=$_.Current.Name;class=$_.Current.ClassName;pid=$_.Current.ProcessId;type=$_.Current.ControlType.ProgrammaticName;offscreen=$_.Current.IsOffscreen}})
  throw 'No owned native folder dialog'
 }
 $all=$dialog.FindAll([Windows.Automation.TreeScope]::Descendants,[Windows.Automation.Condition]::TrueCondition)
 $receipt.dialog=@{name=$dialog.Current.Name;pid=$dialog.Current.ProcessId;class=$dialog.Current.ClassName}
 $provider=Get-Process -Id $dialog.Current.ProcessId
 $providerCim=Get-CimInstance Win32_Process -Filter ('ProcessId='+$provider.Id)
 $receipt.provider=@{pid=$provider.Id;parent_pid=$providerCim.ParentProcessId;creation_filetime=$provider.StartTime.ToFileTimeUtc();program=$provider.Path;program_sha256=(Get-FileHash -LiteralPath $provider.Path -Algorithm SHA256).Hash.ToLower();session=$provider.SessionId;hwnd=$dialog.Current.NativeWindowHandle;command=$providerCim.CommandLine;profile=$expected.profile}
 $owners=@();$window=[IntPtr]$dialog.Current.NativeWindowHandle
 for($index=0;$index -lt 8;$index++){
  $window=[VidGrabFolderWindow]::GetWindow($window,4);if($window -eq [IntPtr]::Zero){break}
  $ownerPid=[uint32]0;$null=[VidGrabFolderWindow]::GetWindowThreadProcessId($window,[ref]$ownerPid)
  $owners+=@{hwnd=$window.ToInt64();pid=$ownerPid}
 }
 $receipt.owner_chain=$owners
 if($provider.SessionId -ne 1 -or $providerCim.ParentProcessId -ne $expected.pid -or $provider.Path -cne $expected.program -or $receipt.provider.program_sha256 -cne $expected.program_sha256 -or $receipt.provider.creation_filetime -lt $expected.creation_filetime){throw 'Folder provider is not a child of the selected Chrome identity'}
 if(!$providerCim.CommandLine.Contains('--utility-sub-type=chrome.mojom.UtilWin') -or !$providerCim.CommandLine.Contains('--user-data-dir="'+$expected.profile+'"')){throw 'Folder provider belongs to another Chrome profile'}
 $receipt.controls=@($all|ForEach-Object {@{name=$_.Current.Name;id=$_.Current.AutomationId;type=$_.Current.ControlType.ProgrammaticName;offscreen=$_.Current.IsOffscreen}})
 $native=[VidGrabFolderWindow]::Children([IntPtr]$dialog.Current.NativeWindowHandle);$receipt.native_controls=$native
 $edits=@($native|Where-Object {$_.id -eq 1152 -and $_.className -ceq 'Edit' -and $_.visible -and $_.enabled})
 $buttons=@($native|Where-Object {$_.id -eq 1 -and $_.className -ceq 'Button' -and $_.name -ceq 'Select Folder' -and $_.visible -and $_.enabled})
 if($edits.Count -ne 1 -or $buttons.Count -ne 1){throw 'No unique native folder entry and Select Folder button'}
 if($edits[0].pid -ne $provider.Id -or $buttons[0].pid -ne $provider.Id){throw 'Native controls belong to another provider'}
 if(![VidGrabFolderWindow]::SetText([IntPtr]$edits[0].hwnd,$expected.extension)){throw 'Native folder input failed'}
 $receipt.folder=[VidGrabFolderWindow]::Text([IntPtr]$edits[0].hwnd)
 if($receipt.folder -cne $expected.extension){throw 'Native folder entry differs'}
 $bounds=[Windows.Forms.Screen]::PrimaryScreen.Bounds;$image=[Drawing.Bitmap]::new($bounds.Width,$bounds.Height);$graphics=[Drawing.Graphics]::FromImage($image)
 try{$graphics.CopyFromScreen($bounds.Location,[Drawing.Point]::Empty,$bounds.Size);$image.Save($Report+'.png',[Drawing.Imaging.ImageFormat]::Png)}finally{$graphics.Dispose();$image.Dispose()}
 if($process.HasExited -or $process.StartTime.ToFileTimeUtc() -ne $expected.creation_filetime){throw 'Browser exited before folder selection'}
 if($provider.HasExited -or $provider.StartTime.ToFileTimeUtc() -ne $receipt.provider.creation_filetime -or $dialog.Current.ProcessId -ne $provider.Id){throw 'Folder provider identity changed'}
 $receipt.target=@{name=$buttons[0].name;pid=$buttons[0].pid;id=$buttons[0].id;hwnd=$buttons[0].hwnd;browser_pid=$expected.pid}
 if(![VidGrabFolderWindow]::Click([IntPtr]$buttons[0].hwnd)){throw 'Native Select Folder input failed'}
 $receipt.acknowledgement=$expected;$receipt.result='passed'
}catch{
 $receipt.error=$_.Exception.ToString()
 try{
  $bounds=[Windows.Forms.Screen]::PrimaryScreen.Bounds;$image=[Drawing.Bitmap]::new($bounds.Width,$bounds.Height);$graphics=[Drawing.Graphics]::FromImage($image)
  try{$graphics.CopyFromScreen($bounds.Location,[Drawing.Point]::Empty,$bounds.Size);$image.Save($Report+'.failure.png',[Drawing.Imaging.ImageFormat]::Png)}finally{$graphics.Dispose();$image.Dispose()}
 }catch{$receipt.capture_error=$_.Exception.ToString()}
}
[IO.File]::WriteAllText($Report,($receipt|ConvertTo-Json -Depth 8),[Text.UTF8Encoding]::new($false))
$ready=[IO.Path]::ChangeExtension($Report,'.ready');if(Test-Path -LiteralPath $ready){throw 'Existing reply marker'}
[IO.File]::WriteAllText($ready,'',[Text.UTF8Encoding]::new($false))
if($receipt.result -cne 'passed'){exit 1}
