Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = "C:\Users\User\Documents\llm-agent-test\assistant"
sh.Run """C:\Users\User\Documents\llm-agent-test\assistant\venv\Scripts\pythonw.exe"" assistant_toggle.py", 0, False
