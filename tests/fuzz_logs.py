"""Fuzz test: damage the bad-sector list in valid logs at random and check what the app does.

Each damaged log is judged by an independent checker written here (not the app's code):

  REFUSED      app showed no figures                                      - safe
  HARMLESS     app showed figures identical to the undamaged log          - safe
  UNDETECTABLE damage left a perfectly valid-looking log (e.g. a digit
               changed to another digit, a whole line deleted), so no
               program reading the log alone could know                   - inherent limit
  WRONG+WARN   app showed wrong figures, with a yellow warning            - fail
  WRONG        app showed wrong figures with no warning                   - fail

usage: fuzz_logs.py APP_PATH [TRIALS_PER_FORMAT] [--examples] [--more-ops] [--seed=N]

  e.g. from the main folder:  app\\runtime\\python.exe tests\\fuzz_logs.py app\\bad_sector_calculator.pyw 2000
"""
import importlib.machinery
import importlib.util
import os
import random
import re
import shutil
import sys
import tempfile

sys.dont_write_bytecode = True                   # don't leave __pycache__ folders behind
from collections import Counter, defaultdict

APP = sys.argv[1]
TRIALS = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2].isdigit() else 3000
SHOW_EXAMPLES = "--examples" in sys.argv
MORE_OPS = "--more-ops" in sys.argv          # extra kinds of damage, not used while developing
SEED = next((int(a.split("=")[1]) for a in sys.argv if a.startswith("--seed=")), 20261020)

loader = importlib.machinery.SourceFileLoader("app_under_test", APP)
spec = importlib.util.spec_from_loader("app_under_test", loader)
app = importlib.util.module_from_spec(spec)
sys.modules["app_under_test"] = app
loader.exec_module(app)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fixtures import FTK_ATTENTION, FTK_CLOSE, FTK_HEAD, FTK_TAIL, TX1_HEAD

rng = random.Random(SEED)
TOTAL = 976_773_168


# ---------------------------------------------------------------- independent reference
def merge(ranges):
    out = []
    for s, e in sorted(ranges):
        if out and s <= out[-1][1] + 1:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return [tuple(x) for x in out]


def make_ranges(n):
    ranges, pos = [], rng.randrange(0, 5000)
    for _ in range(n):
        size = rng.choice([1, 1, 2, 3, 8, 8, 16, 64, 64, 128, 500, 4096])
        ranges.append((pos, pos + size - 1))
        pos += size + rng.randrange(2, 3_000_000)
    return ranges


def ftk_text(ranges):
    rows = "\n".join(f"\t{a} through {b}" if a != b else f"\t{a}" for a, b in ranges)
    return FTK_HEAD + FTK_ATTENTION + rows + "\n" + FTK_CLOSE + FTK_TAIL


def tx1_text(ranges):
    rows = "\n".join(f"   {a:<17} {b - a + 1}" for a, b in ranges)
    return TX1_HEAD + "\nUnreadable Sectors:\n   Sector Address    Sector Length\n" + rows + "\n"


def ftk_valid(text):
    """Ranges if the list is well formed (cosmetic damage to the heading wording is allowed), else None."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")     # \r is a line break to any reader
    starts = [i for i, l in enumerate(lines) if "could not be read" in l]
    ends = [i for i, l in enumerate(lines) if "replaced with zeros" in l]
    if len(starts) != 1 or len(ends) != 1 or ends[0] < starts[0]:
        return None
    if not re.search(r"could not be read:?$", lines[starts[0]]):
        return None
    out, indents = [], set()
    for l in lines[starts[0] + 1:ends[0]]:
        if l.strip(" \t") == "":
            continue
        m = re.fullmatch(r"([ \t]*)([0-9][0-9,]*)(?: (?:through|thru|to) ([0-9][0-9,]*))?", l)
        if not m:
            return None
        indents.add(m.group(1))
        a = int(m.group(2).replace(",", ""))
        b = int((m.group(3) or m.group(2)).replace(",", ""))
        if b < a or (out and a <= out[-1][1]):
            return None
        out.append((a, b))
    if len(indents) > 1:
        return None
    # nothing list-like anywhere else
    rest = lines[:starts[0]] + lines[ends[0] + 1:]
    if any(re.fullmatch(r"\s*[0-9]+(?:\s+through\s+[0-9]+)?\s*", l) for l in rest):
        return None
    return out


def tx1_valid(text):
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    try:
        h = lines.index("   Sector Address    Sector Length")
    except ValueError:
        return None
    out = []
    for l in lines[h + 1:]:
        if l.strip(" \t") == "":
            continue
        m = re.fullmatch(r"[ \t]*([0-9][0-9,]*)[ \t]+([0-9][0-9,]*)[ \t]*", l)
        if not m:
            return None
        a, n = int(m.group(1).replace(",", "")), int(m.group(2).replace(",", ""))
        if n == 0 or (out and a <= out[-1][1]):
            return None
        out.append((a, a + n - 1))
    return out


# ---------------------------------------------------------------- damage
PRINTABLE = ("0123456789" * 3 + "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ" +
             " \t!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~ é£�\x00")


def damage(text, fmt, allow_double=True):
    lines = text.split("\n")
    try:
        if fmt == "ftk":
            lo = next(i for i, l in enumerate(lines) if "could not be read" in l) + 1
            hi = next(i for i, l in enumerate(lines) if "replaced with zeros" in l)
        else:
            lo = lines.index("Unreadable Sectors:") + 2
            hi = len(lines) - 1
    except (StopIteration, ValueError):
        return text, "skip"                       # an earlier damage removed the list's markers
    if hi <= lo:
        return text, "skip"
    ops = ["sub_char", "sub_char", "del_char", "ins_char", "swap_chars", "digit_to_digit",
           "space_in_number", "del_line", "dup_line", "swap_lines", "merge_lines", "split_line",
           "truncate", "nul_bytes", "garbage_line", "blank_line", "damage_heading"]
    if MORE_OPS:
        ops += ["unicode_digit", "cr_in_line", "change_separator", "leading_zero", "comma_group", "add_word",
                "remove_indent", "bom_in_line", "trailing_note", "decimal_point", "minus_sign"]
        ops += ["double_damage"] * allow_double
    op = rng.choice(ops)
    i = rng.randrange(lo, hi)
    line = lines[i]
    if op == "double_damage":                     # two separate damages in one log
        once, _ = damage(text, fmt, allow_double=False)
        twice, _ = damage(once, fmt, allow_double=False)
        return twice, op
    if not line and op in ("sub_char", "del_char", "nul_bytes", "split_line", "swap_chars"):
        return text, "skip"
    nums = [(m.start(), m.end()) for m in re.finditer(r"[0-9]+", line)]
    if op == "unicode_digit" and nums:
        p = rng.choice([p for s, e in nums for p in range(s, e)])
        lines[i] = line[:p] + rng.choice("０１２３４５６７８９٠١٢٣٤٥٦٧٨٩") + line[p + 1:]
    elif op == "cr_in_line":
        p = rng.randrange(len(line) + 1)
        lines[i] = line[:p] + "\r" + line[p:]
    elif op == "change_separator":
        if " through " in line:
            lines[i] = line.replace(" through ", rng.choice([" thru ", " to ", "-", " - ", "..", " – ", " THROUGH "]))
        else:
            lines[i] = re.sub(r"(?<=[0-9]) +(?=[0-9])", rng.choice(["\t", " ", "    "]), line)
    elif op == "leading_zero" and nums:
        s, e = rng.choice(nums)
        lines[i] = line[:s] + "0" * rng.randrange(1, 4) + line[s:]
    elif op == "comma_group" and nums:
        s, e = rng.choice(nums)
        lines[i] = line[:s] + f"{int(line[s:e]):,}" + line[e:]
    elif op == "add_word":
        p = rng.randrange(len(line) + 1)
        lines[i] = line[:p] + rng.choice([" sector ", " LBA ", " approx ", " bad ", " skipped ", " ok "]) + line[p:]
    elif op == "remove_indent":
        lines[i] = line.lstrip("\t ")
    elif op == "bom_in_line":
        p = rng.randrange(len(line) + 1)
        lines[i] = line[:p] + "﻿" + line[p:]
    elif op == "trailing_note":
        lines[i] = line + rng.choice([" (retried)", "  ok", " *", " ?", "  # 3 retries", " 512"])
    elif op == "decimal_point" and nums:
        s, e = rng.choice(nums)
        lines[i] = line[:e] + "." + str(rng.randrange(10)) + line[e:]
    elif op == "minus_sign" and nums:
        s, e = rng.choice(nums)
        lines[i] = line[:s] + "-" + line[s:]
    elif op == "sub_char":
        p = rng.randrange(len(line))
        lines[i] = line[:p] + rng.choice(PRINTABLE) + line[p + 1:]
    elif op == "del_char":
        p = rng.randrange(len(line))
        lines[i] = line[:p] + line[p + 1:]
    elif op == "ins_char":
        p = rng.randrange(len(line) + 1)
        lines[i] = line[:p] + rng.choice(PRINTABLE) + line[p:]
    elif op == "swap_chars" and len(line) > 1:
        p = rng.randrange(len(line) - 1)
        lines[i] = line[:p] + line[p + 1] + line[p] + line[p + 2:]
    elif op == "digit_to_digit":
        ds = [p for p, c in enumerate(line) if c.isdigit()]
        p = rng.choice(ds)
        lines[i] = line[:p] + rng.choice("0123456789".replace(line[p], "")) + line[p + 1:]
    elif op == "space_in_number":
        ds = [p for p in range(1, len(line)) if line[p].isdigit() and line[p - 1].isdigit()]
        if ds:
            p = rng.choice(ds)
            lines[i] = line[:p] + rng.choice([" ", "  ", "\t", ",", "."]) + line[p:]
    elif op == "del_line":
        del lines[i]
    elif op == "dup_line":
        lines.insert(i, line)
    elif op == "swap_lines":
        j = rng.randrange(lo, hi)
        lines[i], lines[j] = lines[j], lines[i]
    elif op == "merge_lines" and i + 1 < hi:
        lines[i] = line + rng.choice(["", " ", "\t"]) + lines[i + 1].lstrip("\t ")
        del lines[i + 1]
    elif op == "split_line" and len(line) > 2:
        p = rng.randrange(1, len(line))
        lines[i:i + 1] = [line[:p], line[p:]]
    elif op == "truncate":
        p = rng.randrange(len(line) + 1)
        lines = lines[:i] + [line[:p]]
    elif op == "nul_bytes":
        p = rng.randrange(len(line))
        n = rng.randrange(1, 6)
        lines[i] = line[:p] + "\x00" * n + line[p + n:]
    elif op == "garbage_line":
        lines.insert(i, "".join(rng.choice(PRINTABLE) for _ in range(rng.randrange(1, 30))))
    elif op == "blank_line":
        lines.insert(i, "")
    elif op == "damage_heading":
        k = lo - 1 if fmt == "ftk" else lo - 2
        h = lines[k]
        p = rng.randrange(len(h))
        lines[k] = h[:p] + rng.choice(PRINTABLE) + h[p + 1:]
    return "\n".join(lines), op


def app_reading(text):
    """What the app would show: None if it refuses, else (chunks, warned)."""
    path = os.path.join(TMP, "fuzz.txt")
    with open(path, "wb") as fh:
        fh.write(text.encode("utf-8", errors="surrogatepass"))
    try:
        info = app.parse_file(path)
    except app.LogReadError:
        return None
    if app.blocking_problems(info):
        return None
    res = app.analyse(info.ranges, info.total_sectors)
    return [tuple(c) for c in res.chunks], bool(app.result_warnings(info, res, info.total_sectors))


TMP = tempfile.mkdtemp(prefix="bsc_fuzz_")

results = defaultdict(Counter)
examples = defaultdict(list)
for fmt, make, valid in (("ftk", ftk_text, ftk_valid), ("tx1", tx1_text, tx1_valid)):
    for _ in range(TRIALS):
        truth = make_ranges(rng.randrange(5, 60))
        good = make(truth)
        clean = app_reading(good)                    # an undamaged log must always be read exactly
        if clean is None or clean[0] != merge(truth) or clean[1]:
            results[fmt]["FALSE ALARM"] += 1
            continue
        bad, op = damage(good, fmt)
        if bad == good:
            continue
        shown = app_reading(bad)
        still_valid = valid(bad)
        if shown is None:
            outcome = "REFUSED"
        elif shown[0] == merge(truth):
            outcome = "HARMLESS"
        elif still_valid is not None and shown[0] == merge(still_valid):
            outcome = "UNDETECTABLE"
        else:
            outcome = "WRONG+WARN" if shown[1] else "WRONG"
            if len(examples[(fmt, op)]) < 2:
                diff = [l for l in bad.split("\n") if l not in good.split("\n")][:2]
                examples[(fmt, op)].append(diff)
        results[fmt][outcome] += 1
        results[(fmt, op)][outcome] += 1

ORDER = ["REFUSED", "HARMLESS", "UNDETECTABLE", "WRONG+WARN", "WRONG", "FALSE ALARM"]
for fmt in ("ftk", "tx1"):
    c = results[fmt]
    n = sum(c.values())
    print(f"{fmt.upper()}: {n} damaged logs  " + "  ".join(f"{k}={c[k]}" for k in ORDER))
    fails = sorted(((key[1], r) for key, r in results.items() if isinstance(key, tuple) and key[0] == fmt
                    and (r["WRONG"] or r["WRONG+WARN"])), key=lambda x: -(x[1]["WRONG"] + x[1]["WRONG+WARN"]))
    for op, r in fails:
        print(f"    {op:<16} wrong={r['WRONG']:<4} wrong+warn={r['WRONG+WARN']:<4} of {sum(r.values())}")
        if SHOW_EXAMPLES:
            for ex in examples[(fmt, op)]:
                print(f"        e.g. {[repr(x) for x in ex]}")
bad_total = sum(results[f]["WRONG"] + results[f]["WRONG+WARN"] + results[f]["FALSE ALARM"] for f in ("ftk", "tx1"))
print("RESULT:", "no wrong figures" if not bad_total else f"{bad_total} damaged logs gave wrong figures")
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if bad_total else 0)
