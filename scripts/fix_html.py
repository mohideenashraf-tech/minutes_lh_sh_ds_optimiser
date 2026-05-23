from pathlib import Path
import re

p = Path(r"C:\Users\mohideen.ashraf\Downloads\Spillover_Milkrun_Web\app\templates\index.html")
t = p.read_text(encoding="utf-8")
t = t.replace("<motion", "<DIV_PLACEHOLDER")
t = t.replace("</motion>", "</DIV_PLACEHOLDER>")
t = t.replace("DIV_PLACEHOLDER", "div")
p.write_text(t, encoding="utf-8")
print("fixed", p)
