' Start with no console window at all (tray icon only).
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
d = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = d
sh.Run "cmd /c call """ & d & "\start.bat""", 0, False
