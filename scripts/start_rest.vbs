' Palimpsest REST 启动脚本（隐藏窗口，登录时/看门狗拉起）
' 日志：scripts/start_rest.log（每次运行覆盖）
Option Explicit
Dim fso, logFile, WshShell, cmd, scriptDir, projectRoot
Set fso = CreateObject("Scripting.FileSystemObject")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
projectRoot = fso.GetParentFolderName(scriptDir)
Set logFile = fso.CreateTextFile(scriptDir & "\start_rest.log", True)
Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = projectRoot
logFile.WriteLine "CD set, now checking port via python launcher"
cmd = "venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8090 --log-level warning"
WshShell.Run cmd, 0, False
logFile.WriteLine "Run issued: " & cmd
logFile.WriteLine "Error code: " & Err.Number & " desc: " & Err.Description
logFile.Close
