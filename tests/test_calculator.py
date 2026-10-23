"""Tests for bad_sector_calculator.pyw.

Run from the main folder (the one with the .exe), with the tests folder next to app:

    app\\runtime\\python.exe tests\\test_calculator.py

Accuracy - every figure is checked against an independent brute-force count
(sets of individual sector numbers), not against the app's own merge code.

Safety - every way a log can be unreadable must stop the app showing figures,
and must name the lines it couldn't read.

The checks follow the app's settings (BANDS, FIRST_REGION, APP_VERSION), so
changing those doesn't need any change here.
"""
import itertools
import os
import re
import shutil
import subprocess
import sys
import tempfile

sys.dont_write_bytecode = True                   # don't leave __pycache__ folders behind
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fixtures import (APP, FTK_ATTENTION, FTK_CLOSE, FTK_HEAD, FTK_RANGES, FTK_TAIL, TOTAL, TX1_CLEAN_HEAD,
                      TX1_HEAD, TX1_NO_COUNT_HEAD, TX1_RANGES, TX1_REGIONS, ftk_list, ftk_log, line_of,
                      load_app, tx1_table)

bsc = load_app()
FAILS = []
TMP = tempfile.mkdtemp(prefix="bsc_tests_")      # everything the checks write goes here, removed at the end
_counter = itertools.count(1)


def check(name, got, want):
    if got != want:
        FAILS.append(f"{name}: got {got!r}, want {want!r}")


# ---------------------------------------------------------------- brute force reference
def reference(sector_sets, total):
    bad = set()
    for a, b in sector_sets:
        bad.update(range(a, b + 1))
    runs = []
    for x in sorted(bad):
        if runs and x == runs[-1][1] + 1:
            runs[-1][1] = x
        else:
            runs.append([x, x])
    sizes = [e - s + 1 for s, e in runs]

    def band(lo, hi):
        sel = [z for z in sizes if lo <= z <= (hi if hi is not None else 10**18)]
        return len(sel), sum(sel)
    bands = [band(lo, hi) for _label, lo, hi in bsc.BANDS]          # the app's band settings
    return {"total": len(bad), "chunks": len(runs), "bands": bands,
            "first": sum(1 for x in bad if x < bsc.FIRST_REGION)}


def write(name, text=None, raw=None, encoding="utf-8", bom=b"", newline="\n"):
    path = os.path.join(TMP, f"{next(_counter)}_{name}")
    data = raw if raw is not None else bom + text.replace("\n", newline).encode(encoding)
    with open(path, "wb") as fh:
        fh.write(data)
    return path


def run_case(name, text, expect_ranges, total, tool, sector_size=512, encoding="utf-8", bom=b"", newline="\n",
             reading_contains=None, warn_contains=None, model=None, pair_mode=None):
    """A log the app must read completely, with figures that match the brute-force count."""
    path = write(name + ".txt", text, encoding=encoding, bom=bom, newline=newline)
    info = bsc.parse_file(path, pair_mode)
    problems = bsc.blocking_problems(info)
    if problems:
        FAILS.append(f"{name}: app refused a readable log: {problems} {info.problem_lines}")
        return info, None, ""
    res = bsc.analyse(info.ranges, info.total_sectors)
    ref = reference(expect_ranges, total)
    check(f"{name} tool", info.tool, tool)
    check(f"{name} total sectors", info.total_sectors, total)
    check(f"{name} sector size", info.sector_size, sector_size)
    check(f"{name} total bad", res.total_bad, ref["total"])
    check(f"{name} chunks", len(res.chunks), ref["chunks"])
    check(f"{name} bands", [(c, s) for _l, c, s in res.bands], ref["bands"])
    check(f"{name} first 2048", res.first_bad, ref["first"])
    if model is not None:
        check(f"{name} model", info.model, model)
    if reading_contains and reading_contains not in info.reading:
        FAILS.append(f"{name}: reading {info.reading!r} lacks {reading_contains!r}")
    warns = bsc.result_warnings(info, res, info.total_sectors)
    if warn_contains:
        if not any(warn_contains in w for w in warns):
            FAILS.append(f"{name}: no warning containing {warn_contains!r}; got {warns}")
    elif warns:
        FAILS.append(f"{name}: unexpected warnings {warns}")
    rep = bsc.build_report(info, res, info.total_sectors, info.sector_size or 512)
    if f"{res.total_bad:,}" not in rep:
        FAILS.append(f"{name}: report missing total")
    return info, res, rep


def expect_blocked(name, text=None, raw=None, path=None, contains="", lines=None, ambiguous=False):
    """A log the app must refuse: no figures, a reason, and the lines it couldn't read."""
    path = path or write(name, text, raw=raw)
    try:
        info = bsc.parse_file(path)
        msgs = bsc.blocking_problems(info)
    except bsc.LogReadError as exc:
        info, msgs = None, [str(exc)]
    if not msgs:
        res = bsc.analyse(info.ranges, info.total_sectors)
        FAILS.append(f"{name}: should have refused, but would show {res.total_bad:,} bad sectors")
        return info
    if contains and not any(contains.lower() in m.lower() for m in msgs):
        FAILS.append(f"{name}: no message containing {contains!r}; got {msgs}")
    if lines is not None:
        check(f"{name} lines to check", [no for no, _t in bsc.check_lines(info)] if info else [], lines)
    check(f"{name} ambiguous", bool(info and info.ambiguous and not info.errors), ambiguous)
    return info


# ================================================================ FTK Imager - accuracy
text = ftk_log()
info, res, rep = run_case("ftk_main", text, FTK_RANGES, TOTAL, "FTK Imager", model="ST500LM012 HN-M500MBB")
check("ftk_main entries", res.entries, len(FTK_RANGES))
check("ftk_main serial", info.serial, "TEST-SERIAL-0001")
check("ftk_main pct", bsc.fmt_pct(res.total_bad, TOTAL), "0.21%")
check("ftk_main size", bsc.size_pair(res.total_bad * 512), ("981.53 MB", "0.96 GB"))

run_case("ftk_crlf_cp1252", text, FTK_RANGES, TOTAL, "FTK Imager", encoding="cp1252", newline="\r\n")
run_case("ftk_utf16_bom", text, FTK_RANGES, TOTAL, "FTK Imager", encoding="utf-16-le", bom=b"\xff\xfe",
         newline="\r\n")
run_case("ftk_utf16_nobom", text, FTK_RANGES, TOTAL, "FTK Imager", encoding="utf-16-le", newline="\r\n")
run_case("ftk_utf8_bom", text, FTK_RANGES, TOTAL, "FTK Imager", bom=b"\xef\xbb\xbf")
run_case("ftk_stray_nul", text + "\x00", FTK_RANGES, TOTAL, "FTK Imager")

info, res, rep = run_case("ftk_clean", ftk_log([]), [], TOTAL, "FTK Imager")
check("ftk_clean list_found", info.list_found, False)
if "No bad sectors: FTK Imager didn't list" not in rep:
    FAILS.append("ftk_clean: report doesn't say why it's clean")

one = FTK_HEAD + "\nThe following sector(s) on the source drive could not be read: 234507218\n" + FTK_CLOSE + FTK_TAIL
run_case("ftk_inline_single", one, [(234507218, 234507218)], TOTAL, "FTK Imager")

big = FTK_HEAD + ("\nThe following sector(s) on the source drive could not be read\n342999808 through 359907583\n"
                  + FTK_CLOSE) + FTK_TAIL
run_case("ftk_big_range", big, [(342999808, 359907583)], TOTAL, "FTK Imager")

dup = FTK_HEAD + ftk_list(FTK_RANGES[:3]) + ftk_list(FTK_RANGES[:3]) + FTK_TAIL
run_case("ftk_duplicate", dup, FTK_RANGES[:3], TOTAL, "FTK Imager", warn_contains="more than once")

nocount = text.replace(" Sector Count: 976,773,168\n", "").replace(" Sector count:    976773168\n", "")
info = bsc.parse_log(nocount, "x")
check("ftk_nocount estimate", info.total_sectors, 476940 * 1024 * 1024 // 512)
check("ftk_nocount note", info.total_note, "estimated from data size")

run_case("ftk_past_end", ftk_log([(976773160, 976773175)]), [(976773160, 976773175)], TOTAL, "FTK Imager",
         warn_contains="past the end")
check("ftk_4k sector size", bsc.parse_log(text.replace("Bytes per Sector: 512", "Bytes per Sector: 4096")).sector_size,
      4096)

# ================================================================ FTK Imager - must warn (figures still right)
run_case("ftk_logical", ftk_log().replace("Source Type: Physical", "Source Type: Logical"), FTK_RANGES, TOTAL,
         "FTK Imager", warn_contains="logical (partition) image")
run_case("ftk_no_finish", ftk_log().replace(" Acquisition finished:  Mon Oct 19 13:40:02 2026\n", ""),
         FTK_RANGES, TOTAL, "FTK Imager", warn_contains="Acquisition finished")

# ================================================================ FTK Imager - must refuse
t = FTK_HEAD + FTK_ATTENTION + "\t100 through 199\n\t500 through 599 (retried)\n\t900 through 999\n" + \
    FTK_CLOSE + FTK_TAIL
expect_blocked("ftk_bad_line", t, contains="couldn't be read", lines=[line_of(t, "(retried)")])

t = FTK_HEAD + FTK_ATTENTION + "\t100 through 199\n-- page 2 --\n\t500 through 599\n" + FTK_CLOSE + FTK_TAIL
expect_blocked("ftk_page_break", t, contains="couldn't be read", lines=[line_of(t, "page 2")])

t = FTK_HEAD + "\nATTENTION:\nUnreadable sectors on source:\n\t100 through 199\n" + FTK_CLOSE + FTK_TAIL
expect_blocked("ftk_other_wording", t, contains="mentions bad or unreadable",
               lines=[line_of(t, "Unreadable sectors on source"), line_of(t, "100 through 199"),
                      line_of(t, "replaced with zeros")])

# v1.2 - damage that v1.1 read as wrong figures
t = FTK_HEAD + FTK_ATTENTION + "\t100 through 199\n\t300 through 399\n" + FTK_TAIL
expect_blocked("ftk_cut_off", t, contains="cut off", lines=[])
base = FTK_ATTENTION + "\t1000 through 1015\n\t140591432 through 140591447\n\t300000000 through 300000063\n" + FTK_CLOSE


def ftk_damaged(old, new):
    text = FTK_HEAD + base.replace(old, new) + FTK_TAIL
    single = new.strip("\t\n")
    return text, (line_of(text, single) if single and "\n" not in single else None)


for name, old, new in (("symbol in number", "140591432", "1405?1432"),
                       ("space in number", "140591432", "140 591 432"),
                       ("no 'through'", "140591432 through 140591447", "140591432 140591447"),
                       ("zero bytes", "140591432", "1405\x00\x001432"),
                       ("non-breaking space", "140591447", "1405914\xa047"),
                       ("unicode digit", "140591432", "14059１432"),
                       ("decimal point", "140591447", "140591447.5"),
                       ("note after range", "140591447", "140591447 (retried)"),
                       ("capital THROUGH", "140591432 through", "140591432 THROUGH"),
                       ("lost indent", "\t140591432", "140591432")):
    t, ln = ftk_damaged(old, new)
    expect_blocked(f"ftk {name}", t, contains="couldn't be read", lines=[ln])

# a lost digit is caught when the bigger range runs into its neighbours (a lone range that stays in
# order is indistinguishable from a real one - no program reading only the log can spot that)
t, _ = ftk_damaged("\t140591432 through", "\t50000000 through 50000015\n\t14059432 through")
expect_blocked("ftk digit lost", t, contains="goes backwards or overlaps",
               lines=[line_of(t, "50000000 through"), line_of(t, "14059432 through")])
t, _ = ftk_damaged("\t300000000 through 300000063\n", "\t300000000 through 300000063\n\t300000000 through 300000063\n")
expect_blocked("ftk duplicated line", t, contains="goes backwards or overlaps",
               lines=[line_of(t, "300000000 through"), line_of(t, "300000000 through") + 1])
t, _ = ftk_damaged("\t1000 through 1015\n\t140591432 through 140591447\n",
                   "\t140591432 through 140591447\n\t1000 through 1015\n")
expect_blocked("ftk swapped lines", t, contains="goes backwards", lines=[line_of(t, "140591432"), line_of(t, "1000")])
t = FTK_HEAD + "\nATTENTION:\nThe following sector(s) on the source drive could not be read9\n\t1000 through 1015\n" + \
    FTK_CLOSE + FTK_TAIL
expect_blocked("ftk digit on heading", t, contains="couldn't be read", lines=[line_of(t, "could not be read9")])
t = FTK_HEAD + "\nThe following sector(s) on the source drive could not be read: 5\n\t1000 through 1015\n" + \
    FTK_CLOSE + FTK_TAIL
expect_blocked("ftk number on heading and list", t, contains="couldn't be read", lines=[line_of(t, "read: 5")])

t = FTK_HEAD + FTK_ATTENTION + FTK_CLOSE + FTK_TAIL
expect_blocked("ftk_empty_list", t, contains="no sector numbers could be read", lines=[])

t = FTK_HEAD + FTK_ATTENTION + "\t500 through 400\n" + FTK_CLOSE + FTK_TAIL
expect_blocked("ftk_backwards_range", t, contains="couldn't be read", lines=[line_of(t, "500 through 400")])

t = FTK_HEAD + FTK_ATTENTION + "\t100 through 199\n" + FTK_CLOSE + "\t700 through 799\n" + FTK_TAIL
expect_blocked("ftk_entry_after_list", t, contains="couldn't be read", lines=[line_of(t, "700 through 799")])

# ================================================================ not a usable log at all
expect_blocked("empty.txt", raw=b"", contains="empty")
expect_blocked("blank.txt", raw=b"  \r\n\r\n \t", contains="empty")
expect_blocked("image.E01", raw=b"EVF\x09\x0d\x0a\xff\x00" + os.urandom(8192), contains="isn't a text file")
expect_blocked("random.bin", raw=os.urandom(8192), contains="isn't a text file")
limit = bsc.MAX_LOG_BYTES
bsc.MAX_LOG_BYTES = 1000                         # pretend the size limit is tiny rather than write 200 MB
expect_blocked("huge.txt", raw=b"x" * 1001, contains="far too big")
bsc.MAX_LOG_BYTES = limit
os.makedirs(os.path.join(TMP, "a folder"))
expect_blocked("folder", path=os.path.join(TMP, "a folder"), contains="couldn't be opened")
expect_blocked("missing.txt", path=os.path.join(TMP, "nope.txt"), contains="couldn't be opened")
expect_blocked("shopping.txt", "Shopping list\neggs\nmilk 2\n", contains="doesn't look like")
expect_blocked("other_tool.txt", "Bad sectors:\n  1000  64\n  5000  64\n", contains="doesn't look like")
ok_info = bsc.parse_log(ftk_log(), "x.txt")
ok_res = bsc.analyse(ok_info.ranges, ok_info.total_sectors)
expect_blocked("own_report.txt", bsc.build_report(ok_info, ok_res, TOTAL, 512), contains="report saved by")

# ================================================================ Tableau TX1 - accuracy
info, res, _ = run_case("tx1_table", tx1_table(), TX1_RANGES, TOTAL, "Tableau TX1",
                        reading_contains="start sector + length (from the column headings)",
                        model="WDC WD5000AAKX-00ERMA0")
check("tx1_table serial", info.serial, "TEST-SERIAL-0002")
check("tx1_table reported", (info.reported_errors, info.reported_label), (4, "Total unrecoverable errors"))

run_case("tx1_commas", TX1_HEAD + "\nBad Sectors\n  Address          Length\n" +
         "\n".join(f"  {a:<16,} {n:,}" for a, n in TX1_REGIONS) + "\n", TX1_RANGES, TOTAL, "Tableau TX1")
run_case("tx1_hex", TX1_HEAD + "\nBad sector regions (LBA, length):\n" +
         "\n".join(f"  0x{a:010X}  0x{n:X}" for a, n in TX1_REGIONS) + "\n", TX1_RANGES, TOTAL, "Tableau TX1")
run_case("tx1_words", TX1_HEAD + "\nUnreadable regions\n" +
         "\n".join(f"  Sector {a:,}, length {n}" for a, n in TX1_REGIONS) + "\n", TX1_RANGES, TOTAL, "Tableau TX1")
run_case("tx1_paren", TX1_HEAD + "\nBad sectors:\n" +
         "\n".join(f"  LBA {a} ({n} sectors)" for a, n in TX1_REGIONS) + "\n", TX1_RANGES, TOTAL, "Tableau TX1")
run_case("tx1_numbered", TX1_HEAD + "\nUnreadable Sectors\n  #   Start Sector   Sector Count\n" +
         "\n".join(f"  {i}   {a}   {n}" for i, (a, n) in enumerate(TX1_REGIONS, 1)) + "\n",
         TX1_RANGES, TOTAL, "Tableau TX1")
run_case("tx1_noheadings", TX1_HEAD + "\nUnreadable sectors:\n" +
         "\n".join(f"  {a}  {n}" for a, n in TX1_REGIONS) + "\n", TX1_RANGES, TOTAL, "Tableau TX1",
         reading_contains="(worked out from the numbers)")
SE = [(140591432, 140591447), (140593480, 140593495), (300000000, 300000002)]
run_case("tx1_startend_headings", TX1_HEAD + "\nUnreadable Sectors\n  Start LBA    End LBA\n" +
         "\n".join(f"  {a}  {b}" for a, b in SE) + "\n", SE, TOTAL, "Tableau TX1",
         reading_contains="first – last sector")
run_case("tx1_through", TX1_HEAD + "\nUnreadable sectors:\n" +
         "\n".join(f"  {a} through {b}" for a, b in SE) + "\n", SE, TOTAL, "Tableau TX1")
run_case("tx1_three_consistent", TX1_HEAD + "\nUnreadable sectors\n  1000 1063 64\n  5000 64 5063\n",
         [(1000, 1063), (5000, 5063)], TOTAL, "Tableau TX1")
run_case("tx1_html", "<html><body><pre>" + tx1_table().replace("&", "&amp;") + "</pre></body></html>",
         TX1_RANGES, TOTAL, "Tableau TX1")
run_case("tx1_html_table", "<html><body><h1>Tableau TX1 Forensic Imager</h1><table>"
         "<tr><td>Block Size:</td><td>512 bytes</td></tr><tr><td>Block Count:</td><td>976,773,168</td></tr></table>"
         "<h2>Unreadable Sectors</h2><table><tr><th>Sector Address</th><th>Sector Length</th></tr>" +
         "".join(f"<tr><td>{a}</td><td>{n}</td></tr>" for a, n in TX1_REGIONS) + "</table></body></html>",
         TX1_RANGES, TOTAL, "Tableau TX1")

info, res, rep = run_case("tx1_clean", TX1_CLEAN_HEAD, [], TOTAL, "Tableau TX1")
check("tx1_clean list_found", info.list_found, False)
if "“Total unrecoverable errors” as 0" not in rep:
    FAILS.append("tx1_clean: report doesn't say why it's clean")
run_case("tx1_clean_statement", TX1_NO_COUNT_HEAD + "\nBad sectors: None\n", [], TOTAL, "Tableau TX1")

dest_first = TX1_HEAD.replace("Source Disk:", "SOURCE_TMP").replace("Destination Disk:", "Source Disk:") \
    .replace("SOURCE_TMP", "Destination Disk:")
check("tx1 block count follows the Source heading", bsc.parse_log(dest_first, "x").total_sectors, 1953525168)

# ambiguous two-number list: refuses until told how to read it, then exact either way
AMB = TX1_HEAD + "\nUnreadable sectors:\n  1000  1500\n  4000  4100\n"
expect_blocked("tx1_ambiguous", AMB, contains="can't tell whether the second number", ambiguous=True)
run_case("tx1_ambiguous_as_end", AMB, [(1000, 1500), (4000, 4100)], TOTAL, "Tableau TX1", pair_mode="end",
         reading_contains="first – last sector (as you chose)")
run_case("tx1_ambiguous_as_length", AMB, [(1000, 2499), (4000, 8099)], TOTAL, "Tableau TX1", pair_mode="length",
         reading_contains="start sector + length (as you chose)")

# ================================================================ Tableau TX1 - must warn
run_case("tx1_job_failed", tx1_table().replace("Status: Ok", "Status: Failed\nReason for failure: Destination is full")
         + "\n*** CAUTION: THE OPERATION RECORDED IN THIS LOG DID NOT COMPLETE NORMALLY ***\n",
         TX1_RANGES, TOTAL, "Tableau TX1", warn_contains="didn't finish normally (Destination is full)")
run_case("tx1_count_zero_but_list", tx1_table(head=TX1_CLEAN_HEAD), TX1_RANGES, TOTAL, "Tableau TX1",
         warn_contains="no unrecoverable errors, but it also lists")

# ================================================================ Tableau TX1 - must refuse
t = TX1_HEAD + "\nUnreadable Sectors:\n  Address: 1000 (0x3E8) Length: 64\n  Address: 5000 (0x1388) Length: 64\n"
expect_blocked("tx1_three_numbers", t, contains="couldn't be read",
               lines=[line_of(t, "Address: 1000"), line_of(t, "Address: 5000")])
expect_blocked("tx1_errors_no_list", TX1_HEAD, contains="“Total unrecoverable errors” as 4")
expect_blocked("tx1_no_count_no_list", TX1_NO_COUNT_HEAD, contains="can't confirm")
t = TX1_HEAD + "\nUnreadable data:\n  offset 72,057,594 bytes for 32,768 bytes\n"
expect_blocked("tx1_unknown_list_format", t, contains="as 4, but", lines=[line_of(t, "Unreadable data")])
t = tx1_table().rstrip("\n") + "\n   (retrying)\n   600000000         64\n"
expect_blocked("tx1_line_inside_list", t, contains="couldn't be read", lines=[line_of(t, "(retrying)")])
t = tx1_table() + "Report generated by TX1\nFirmware 22.3\n   900000000   64\n"
expect_blocked("tx1_text_stuck_to_list", t, contains="couldn't be read",
               lines=[line_of(t, "Report generated"), line_of(t, "Firmware 22.3")])
t = tx1_table() + "\nReport generated by TX1\n\n   900000000   64\n"
expect_blocked("tx1_entry_after_list", t, contains="couldn't be read", lines=[line_of(t, "Report generated")])

# v1.2 - layout checks on TX1 rows
for name, old, new in (("tab for a digit", "   140591424         64", "   140591424         \t4"),
                       ("comma for a digit", "   140591424         64", "   140591424         ,4"),
                       ("leading zero", "   140591424         64", "   140591424         064"),
                       ("non-breaking space", "   140591424         64", "   140591424         6\xa0")):
    t = tx1_table().replace(old, new)
    expect_blocked(f"tx1 {name}", t, contains="couldn't be read", lines=[line_of(t, "140591424 ")])
t = tx1_table().replace("   2048              64", "   2048")
expect_blocked("tx1 one number under two headings", t, contains="couldn't be read", lines=[line_of(t, "   2048")])
t = tx1_table().replace("   2048              64", "   20  48              64")
expect_blocked("tx1 three numbers under two headings", t, contains="couldn't be read", lines=[line_of(t, "20  48")])
t = TX1_HEAD + "\nUnreadable Sectors\n  #   Start Sector   Sector Count\n  1   1000   64\n  2   2000   64\n  4   3000   64\n"
expect_blocked("tx1 row numbers out of step", t, contains="couldn't be read")
run_case("tx1 'None' under heading", TX1_NO_COUNT_HEAD + "\nUnreadable Sectors:\n  None\n", [], TOTAL, "Tableau TX1")
t = TX1_HEAD + "\nUnreadable Sectors:\n   Sector Address    Sector Length\n   5000   64\n   1000   64\n"
expect_blocked("tx1 out of order", t, contains="goes backwards",
               lines=[line_of(t, "   5000   64"), line_of(t, "   1000   64")])
run_case("tx1 html &nbsp; padding", "<html><body><h1>Tableau TX1</h1><table><tr><td>Block Size:</td><td>512</td></tr>"
         "<tr><td>Block Count:</td><td>976,773,168</td></tr></table><h2>Unreadable Sectors</h2><table>"
         "<tr><th>Sector Address</th><th>Sector Length</th></tr>" +
         "".join(f"<tr><td>&nbsp;{a}</td><td>{n}&nbsp;</td></tr>" for a, n in TX1_REGIONS) + "</table></body></html>",
         TX1_RANGES, TOTAL, "Tableau TX1")

if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)+", bsc.APP_VERSION):
    FAILS.append(f"APP_VERSION {bsc.APP_VERSION!r} should look like 1.2 or 1.2.3")
t = TX1_HEAD + "\nUnreadable Sectors\n  Start LBA    End LBA\n  1000  64\n"
expect_blocked("tx1_headings_contradict", t, contains="headings say")
t = tx1_table().rstrip("\n") + "\n   3\n   600000000         64\n"
expect_blocked("tx1_lone_number", t, contains="couldn't be read", lines=[line_of(t, "   3")])
t = tx1_table(regions=[(1000, 0)])
expect_blocked("tx1_zero_length", t, contains="couldn't be read", lines=[line_of(t, "1000  ")])
t = TX1_HEAD + "\nEvents:\n  09:01:00 Read error at LBA 2048, 64 sectors\n"
expect_blocked("tx1_error_events", t, contains="as 4, but", lines=[line_of(t, "Read error at")])

# ================================================================ command line
def cli(*args):
    p = subprocess.run([sys.executable, APP, "--report", *args], capture_output=True, text=True, encoding="utf-8")
    return p.returncode, p.stdout


code, out = cli(write("bad.txt", FTK_HEAD + FTK_ATTENTION + "\t1 through 9\n\tjunk\n" + FTK_CLOSE + FTK_TAIL))
check("cli refused exit code", code, 1)
for needle in ("couldn't calculate", "Line ", "junk", "No figures were calculated"):
    if needle not in out:
        FAILS.append(f"cli refused output lacks {needle!r}")
if "Total bad sectors" in out:
    FAILS.append("cli refused output still shows figures")
amb = write("amb.txt", AMB)
code, out = cli(amb)
check("cli ambiguous exit code", code, 1)
if "--pairs" not in out:
    FAILS.append("cli ambiguous output doesn't explain --pairs")
code, out = cli(amb, "--pairs", "end")
check("cli --pairs end exit code", code, 0)
if "Total bad sectors  : 602" not in out:
    FAILS.append("cli --pairs end total wrong")
code, out = cli(write("ok.txt", ftk_log()))
check("cli ok exit code", code, 0)

# ================================================================ formatting + bands
check("fmt_pct small", bsc.fmt_pct(8192, 976773168), "0.00084%")
check("fmt_pct half", bsc.fmt_pct(1, 200), "0.50%")
check("fmt_pct big", bsc.fmt_pct(1234, 10000), "12.34%")
check("fmt_pct none", bsc.fmt_pct(5, None), "—")
check("fmt_num tiny", bsc.fmt_num(4 * 1024 * 1024 / 1024 ** 3), "0.0039")
check("fmt_bytes 512", bsc.fmt_bytes(512), "512 bytes")
check("fmt_bytes 1.5K", bsc.fmt_bytes(1536), "1.50 KB")
check("size_pair 4MB", bsc.size_pair(4 * 1024 ** 2), ("4.00 MB", "0.0039 GB"))
check("size_pair 2GB", bsc.size_pair(2 * 1024 ** 3), ("2.00 GB", "2,048.00 MB"))

# a chunk exactly on each band's smallest and largest size must land in that band
edges = sorted({lo for _l, lo, _h in bsc.BANDS} | {hi for _l, _lo, hi in bsc.BANDS if hi is not None}
               | {bsc.BANDS[-1][1] * 5})
rs, pos = [], 10_000_000
for z in edges:
    rs.append((pos, pos + z - 1))
    pos += z + 10
res = bsc.analyse(rs, 10**12)
want = [sum(1 for z in edges if lo <= z <= (hi if hi is not None else 10**18)) for _l, lo, hi in bsc.BANDS]
check("band boundaries", [c for _l, c, _s in res.bands], want)
check("merge adjacent", bsc.merge_ranges([(5, 9), (10, 12), (11, 11), (20, 20)]), [(5, 12), (20, 20)])
R = bsc.FIRST_REGION
check("first region edge", bsc.analyse([(R - 1, R)], 10**12).first_bad, 1)
check("first region outside", bsc.analyse([(R, R + 2000)], 10**12).first_bad, 0)

shutil.rmtree(TMP, ignore_errors=True)
if FAILS:
    print("FAILED:")
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("All checks passed.")
