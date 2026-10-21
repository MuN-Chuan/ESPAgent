Option Explicit

Dim fso, shell, root, logDir, logFile, launcher, pythonw, setupCommand, hostCommand, setupExitCode
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

root = fso.GetParentFolderName(WScript.ScriptFullName)
logDir = fso.BuildPath(root, "logs")
If Not fso.FolderExists(logDir) Then
  fso.CreateFolder(logDir)
End If

logFile = fso.BuildPath(logDir, "demo_host.log")
launcher = fso.BuildPath(root, "start_demo_host.cmd")
pythonw = fso.BuildPath(root, "apps\demo_host\.venv\Scripts\pythonw.exe")
setupCommand = Chr(34) & launcher & Chr(34) & " --setup-only"

' Dependency setup is hidden and exits before the long-running Host starts.
setupExitCode = shell.Run(setupCommand, 0, True)
If setupExitCode <> 0 Or Not fso.FileExists(pythonw) Then
  WScript.Quit 1
End If

' pythonw is a GUI-subsystem executable: no CMD or console process remains alive.
hostCommand = Chr(34) & pythonw & Chr(34) & " " & _
  Chr(34) & fso.BuildPath(root, "start_demo_host.py") & Chr(34) & " --log-file " & Chr(34) & logFile & Chr(34)
shell.Run hostCommand, 0, False
