"""Write sample (synthetic) FTK and TX1 logs for manual and GUI testing."""
import os
import sys

sys.dont_write_bytecode = True                   # don't leave __pycache__ folders behind
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fixtures import FTK_ATTENTION, FTK_CLOSE, FTK_HEAD, FTK_TAIL, TX1_HEAD, ftk_log, tx1_table

out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
os.makedirs(out, exist_ok=True)


def save(name, text, encoding="utf-8"):
    with open(os.path.join(out, name), "w", encoding=encoding, newline="\r\n") as fh:
        fh.write(text)


save("EX01_ftk_sample.txt", ftk_log(), "cp1252")
save("EX02_tx1_sample.txt", tx1_table())
save("EX03_ftk_clean.txt", ftk_log([]), "cp1252")
save("EX04_ftk_damaged_list.txt", FTK_HEAD + FTK_ATTENTION + "\t140591432 through 140591447\n"
     "\t140593480 through 140593495 (retry 3)\n\t140595528 through 140595535\n" + FTK_CLOSE + FTK_TAIL, "cp1252")
save("EX05_tx1_no_headings.txt", TX1_HEAD + "\nUnreadable sectors:\n  140591432  140591447\n  300000000  300000002\n")
save("EX06_shopping_list.txt", "Shopping list\neggs\nmilk 2\n")
print("samples written to", out)
