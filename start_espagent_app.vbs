Option Explicit

Dim fso, shell, root, appScript, appCommand
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

root = fso.GetParentFolderName(WScript.ScriptFullName)
appScript = fso.BuildPath(root, "start_espagent_app.cmd")
appCommand = "cmd.exe /d /s /c " & Chr(34) & Chr(34) & appScript & Chr(34) & Chr(34)

shell.CurrentDirectory = root
shell.Run appCommand, 1, False
