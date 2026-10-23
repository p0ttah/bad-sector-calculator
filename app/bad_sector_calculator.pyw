#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bad Sector Calculator  v1.2

Reads an FTK Imager or Tableau TX1 acquisition log and reports how much of the
source drive could not be read:

  * size failed (MB / GB)
  * total bad sectors and % of the drive
  * failed chunk sizes: singles, doubles, triplets ... 1,000,000+
  * whether any bad sectors fall in the first 2048 sectors (LBA 0-2047)

Figures are only shown when the whole bad-sector list in the log was read and
hangs together. If any line can't be read, or the list goes backwards or
overlaps, the app says why, names the lines, and shows no figures.

Runs on Python 3.8+ using only the standard library.

  GUI:          double-click, or   python bad_sector_calculator.pyw [logfile]
  Text report:  python bad_sector_calculator.pyw --report logfile [--pairs end|length]
"""

from __future__ import annotations

import datetime
import html
import math
import os
import re
import sys
import traceback
from collections import Counter, defaultdict
from dataclasses import dataclass, field

APP_NAME = "Bad Sector Calculator"
APP_VERSION = "1.2"
FIRST_REGION = 2048              # "first 2048 sectors" = LBA 0-2047
DEFAULT_SECTOR_SIZE = 512
MAX_LIST_ROWS = 5000             # rows shown in the on-screen list (report has all)
KB = 1024
MB = 1024 ** 2
GB = 1024 ** 3
TB = 1024 ** 4
MAX_LOG_BYTES = 200 * MB         # imaging logs are KB to a few MB; anything this big isn't a log

# Chunk-size bands: (label, smallest, largest or None for no upper limit)
BANDS = (
    ("Singles (1)", 1, 1),
    ("Doubles (2)", 2, 2),
    ("Triplets (3)", 3, 3),
    ("4 – 9", 4, 9),
    ("10 – 99", 10, 99),
    ("100 – 999", 100, 999),
    ("1,000 – 9,999", 1_000, 9_999),
    ("10,000 – 99,999", 10_000, 99_999),
    ("100,000 – 999,999", 100_000, 999_999),
    ("1,000,000 +", 1_000_000, None),
)


# =============================================================================
#  Reading log files
# =============================================================================

# A number: hex (0x1F40), comma-grouped decimal (976,773,168) or plain decimal - ASCII digits only.
NUM = r"(?:0[xX][0-9A-Fa-f]+|[0-9]{1,3}(?:,[0-9]{3})+(?![0-9])|[0-9]+)"
NUM_RE = re.compile(NUM)
RANGE_SEP = r"(?:through|thru|to|-|–|—|\.\.)"


class LogReadError(Exception):
    """The file can't be used as a log at all (can't open, empty, not text, too big)."""


def to_int(token):
    token = token.strip()
    if token[:2].lower() == "0x":
        return int(token, 16)
    return int(token.replace(",", ""))


def _looks_binary(raw):
    """True for disk images, executables and other files that aren't text."""
    sample = raw[:65536]
    if sample.startswith((b"\xef\xbb\xbf", b"\xff\xfe", b"\xfe\xff")):
        return False
    nul = sample.count(0)
    if nul > len(sample) // 4:
        # UTF-16 text without a byte-order mark has its zero bytes on one side only
        even, odd = sample[0::2].count(0), sample[1::2].count(0)
        return max(even, odd) < 0.9 * nul
    control = sum(1 for b in sample if b < 32 and b not in (9, 10, 12, 13))
    return control > len(sample) * 0.02


def _decode(raw):
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8", errors="replace")
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        return raw.decode("utf-16", errors="replace")
    sample = raw[:4096]
    if sample and sample.count(b"\x00") > len(sample) // 4:
        # UTF-16 without a byte-order mark
        enc = "utf-16-le" if sample[1::2].count(0) >= sample[0::2].count(0) else "utf-16-be"
        return raw.decode(enc, errors="replace")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")


def _html_to_text(text):
    text = re.sub(r"(?is)<(script|style)\b.*?</\1\s*>", " ", text)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(?:p|div|tr|li|h[1-6]|table|thead|tbody|pre)\s*>", "\n", text)
    text = re.sub(r"(?i)</t[dh]\s*>", "\t", text)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).replace("\xa0", " ")      # &nbsp; is ordinary spacing in a web page


def read_text(path):
    """Read a log file as text. Raises LogReadError if the file can't be a log."""
    try:
        size = os.path.getsize(path)
        if size > MAX_LOG_BYTES:
            raise LogReadError(f"The file is {fmt_bytes(size)} – far too big to be an imaging log. "
                               "Check you picked the log (.txt), not the image itself.")
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError as exc:
        raise LogReadError(f"The file couldn't be opened ({exc.strerror or exc}).") from None
    if not raw.strip(b" \t\r\n\x00\xef\xbb\xbf\xff\xfe"):
        raise LogReadError("The file is empty.")
    if _looks_binary(raw):
        raise LogReadError("This isn't a text file, so it can't be an FTK Imager or TX1 log. "
                           "Check you picked the log (.txt), not the image or another file.")
    text = _decode(raw)
    if re.search(r"<\s*(?:html|body|table|tr|td|br|pre|div)\b", text[:20000], re.I):
        text = _html_to_text(text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # zero bytes and unusual spaces in a text log are damage: mark them so a line holding one can't be
    # read, rather than quietly joining or splitting the numbers around them
    text = re.sub(r"[^\S \t\n]|\x00", "�", text)
    if not text.strip(" \t\n�"):
        raise LogReadError("The file is empty.")
    return text


# =============================================================================
#  Parsing
# =============================================================================

@dataclass
class LogInfo:
    path: str = ""
    tool: str = "Unknown"
    ranges: list = field(default_factory=list)          # (first, last) sector ranges as listed
    list_found: bool = False                             # the log has a bad-sector list/section
    section_start: int | None = None                     # index of the list's first line
    total_sectors: int | None = None
    total_note: str = ""
    sector_size: int | None = None
    model: str = ""
    serial: str = ""
    reading: str = ""                                    # how the bad-sector list was read
    reported_errors: int | None = None                   # the log's own error count (TX1)
    reported_label: str = ""
    zero_statement: bool = False                         # the log says outright there were none
    errors: list = field(default_factory=list)           # problems that stop any figures being shown
    problem_lines: list = field(default_factory=list)    # (line number, text) the app couldn't read
    order_lines: list = field(default_factory=list)      # (line number, text) where the list goes wrong
    ambiguous: tuple | None = None                       # ((first, second), rows): needs the user's choice
    warnings: list = field(default_factory=list)         # figures are shown, but check these
    consumed: set = field(default_factory=set)           # line indexes the parser used


AMBIGUOUS_MSG = ("The bad-sector list has two numbers on each line and no column headings, so the app can't "
                 "tell whether the second number is the last sector or the number of sectors.")


def blocking_problems(info):
    """Reasons no figures can be shown for this log. Empty list = safe to show figures."""
    if info.errors:
        return list(info.errors)
    return [AMBIGUOUS_MSG] if info.ambiguous else []


def check_lines(info):
    """Every log line the user should look at: unreadable lines and lines where the list goes wrong."""
    return sorted(set(info.problem_lines) | set(info.order_lines))


def _search(pattern, text, pos=0):
    return re.compile(pattern, re.I | re.M).search(text, pos)


def _line_index(text, pos):
    return text.count("\n", 0, pos)


def _check_order(entries, lines, info):
    """Imagers read a drive from start to end, so each range in a list starts after the one
    before it ends. A list that goes backwards or overlaps has been damaged or edited."""
    for (_a1, b1, k1), (a2, _b2, k2) in zip(entries, entries[1:]):
        if a2 <= b1:
            info.order_lines += [(k1 + 1, lines[k1].strip()), (k2 + 1, lines[k2].strip())]


def _shape(raw_line):
    """How a list line is laid out once its numbers are taken out, e.g. '\\tN through N'.
    Runs of spaces count as one (columns are padded to line up); tabs and leading zeros count."""
    text = raw_line.rstrip(" \t")
    body = text.lstrip(" \t")
    indent = text[:len(text) - len(body)]

    def number(m):
        t = m.group(0)
        return "0N" if len(t) > 1 and t[0] == "0" and t[1] not in "xX" else "N"
    body = re.sub(r"\t+", "\t", re.sub(r" +", " ", NUM_RE.sub(number, body)))
    return indent + re.sub(r"(?i)\b(sector|block|region|range)s\b", r"\1", body)


def _check_layout(idxs, lines, info, by_count=False):
    """A program prints every line of a list the same way. A line indented or laid out
    differently from the others has been damaged or edited, even if it still reads as numbers."""
    def flag_odd(keyed):
        counts = Counter(key for _k, key in keyed).most_common()
        if len(counts) < 2:
            return
        if counts[0][1] == counts[1][1]:                 # no clear majority: can't tell which is right
            odd = [k for k, _key in keyed]
        else:
            odd = [k for k, key in keyed if key != counts[0][0]]
        info.problem_lines.extend((k + 1, lines[k].strip()) for k in odd)

    if len(idxs) < 2:
        return
    flag_odd([(k, lines[k][:len(lines[k]) - len(lines[k].lstrip())]) for k in idxs])
    groups = defaultdict(list)
    for k in idxs:                                       # FTK: singles and ranges are laid out differently
        groups[len(NUM_RE.findall(lines[k])) if by_count else 0].append(k)
    for group in groups.values():
        flag_odd([(k, _shape(lines[k])) for k in group])


# ---------------------------------------------------------------- which tool wrote the log

FTK_HEADER = re.compile(r"(?im)^\W*created\s+by\b.*\bftk\b.*\bimager\b")
FTK_MARKERS = tuple(re.compile(p, re.I | re.M) for p in (
    r"physical\s+evidentiary\s+item", r"evidentiary\s+item\s*\(\s*source\s*\)",
    r"^\s*\[device\s+info\]", r"^\s*\[drive\s+geometry\]", r"^\s*\[physical\s+drive\s+information\]",
    r"^\s*\[computed\s+hashes\]", r"^\s*image\s+information\s*:", r"^\s*acquisition\s+(?:started|finished)\s*:",
    r"^\s*segment\s+list\s*:", r"^\s*image\s+verification\s+results\s*:", r"^\s*acquired\s+using\s*:",
    r"^\s*source\s+type\s*:", r"^\s*drive\s+(?:model|serial\s+number|interface\s+type)\s*:"))
TX1_NAME = re.compile(r"(?i)\btableau\b|\btx1\b|\bopentext\b")
TX1_MARKERS = tuple(re.compile(p, re.I | re.M) for p in (
    r"^\s*(?:block|sector)\s+(?:count|size)\s*:", r"^\s*capacity(?:\s+in\s+bytes)?\s*:",
    r"^\s*total\s+(?:(?:un)?recoverable\s+)?(?:read\s+)?errors?\s*:", r"^\s*job\s+(?:id|number|type|name)\s*:",
    r"^\s*task\s*:", r"^\s*status\s*:", r"^\s*(?:source|destination)s?\b[^:\n]*:\s*$"))


def detect_tool(text):
    if FTK_HEADER.search(text):
        return "FTK Imager"
    if TX1_NAME.search(text) and any(m.search(text) for m in TX1_MARKERS):
        return "Tableau TX1"
    if sum(1 for m in FTK_MARKERS if m.search(text)) >= 3:
        return "FTK Imager"
    return "Unknown"


# ---------------------------------------------------------------- FTK Imager

FTK_START = re.compile(r"(?i)\bsector.*?could\s+not\s+be\s+read")
FTK_END = re.compile(r"(?i)replaced\s+with\s+zeros")
RANGE_TOKEN = re.compile(rf"({NUM})(?:[ \t]*{RANGE_SEP}[ \t]*({NUM}))?", re.I)
# a list line is sector numbers and nothing else: "12", "12 through 20", "40-44, 50"
FTK_ITEM = rf"(?:(?:sectors?|lba)[ \t]+)?{NUM}(?:[ \t]*{RANGE_SEP}[ \t]*{NUM})?"
FTK_LINE = re.compile(rf"(?i)[ \t]*{FTK_ITEM}(?:[ \t]*(?:,|\band\b)[ \t]*{FTK_ITEM})*[ \t]*")
FTK_END_MISSING = ("FTK's bad-sector list should finish with the line “The contents of these sectors were "
                   "replaced with zeros…”, but that line is missing (the list stops at line {line}), so the log "
                   "may have been cut off or edited.")
FTK_LOGICAL = ("This is a logical (partition) image, so sector numbers – including the first-2048 check – "
               "count from the start of the partition, not the start of the disk.")
FTK_NO_FINISH = ("This log has no “Acquisition finished” line, so the imaging may not have completed and the "
                 "list may not cover the whole drive.")


def _number_list(segment):
    """'12 through 20' / '35' / '40-44, 50' -> [(a, b), ...]
    Returns None unless the text is sector numbers in exactly that form."""
    if not FTK_LINE.fullmatch(segment):
        return None
    out = []
    for m in RANGE_TOKEN.finditer(segment):
        a = to_int(m.group(1))
        b = to_int(m.group(2)) if m.group(2) else a
        if b < a:
            return None                                  # a range that ends before it starts
        out.append((a, b))
    return out or None


def _ftk_details(text, info):
    m = _search(rf"^[ \t]*Sector[ \t]+Count[ \t]*:[ \t]*({NUM})", text)
    if m:
        info.total_sectors, info.total_note = to_int(m.group(1)), "from log"
    m = _search(rf"^[ \t]*Bytes[ \t]+per[ \t]+Sector[ \t]*:[ \t]*({NUM})", text)
    if m:
        info.sector_size = to_int(m.group(1))
    m = _search(r"^[ \t]*Drive[ \t]+Model[ \t]*:[ \t]*(\S.*?)[ \t]*$", text)
    if m:
        info.model = m.group(1)
    m = _search(r"^[ \t]*Drive[ \t]+Serial[ \t]+Number[ \t]*:[ \t]*(\S.*?)[ \t]*$", text)
    if m:
        info.serial = m.group(1)
    if info.total_sectors is None:
        m = _search(rf"^[ \t]*Source[ \t]+data[ \t]+size[ \t]*:[ \t]*({NUM})[ \t]*MB", text)
        if m:
            bps = info.sector_size or DEFAULT_SECTOR_SIZE
            info.total_sectors = to_int(m.group(1)) * MB // bps
            info.total_note = "estimated from data size"
    m = _search(r"^[ \t]*Source[ \t]+Type[ \t]*:[ \t]*(\S.*?)[ \t]*$", text)
    if m and m.group(1).lower().startswith("logical"):
        info.warnings.append(FTK_LOGICAL)
    if not _search(r"^[ \t]*Acquisition[ \t]+(?:finished|completed|ended)[ \t]*:", text):
        info.warnings.append(FTK_NO_FINISH)


def _ftk_ranges(lines, info):
    n = len(lines)
    starts = [i for i, line in enumerate(lines) if FTK_START.search(line)]
    for pos, i in enumerate(starts):
        info.list_found = True
        if info.section_start is None:
            info.section_start = i
        info.consumed.add(i)
        entries = []                                      # (first, last, line index) in listed order
        m = FTK_START.search(lines[i])
        # after "could not be read" there may be nothing, ":" or ": <sectors>" - anything else is damage
        tail = re.fullmatch(r"[ \t]*(?:\.[ \t]*|:(.*))?", lines[i][m.end():])
        if tail is None:
            info.problem_lines.append((i + 1, lines[i].strip()))
        elif tail.group(1) and tail.group(1).strip(" \t"):
            nums = _number_list(tail.group(1))
            if nums is None:
                info.problem_lines.append((i + 1, lines[i].strip()))
            else:
                entries += [(a, b, i) for a, b in nums]
        inline = bool(entries)
        limit = starts[pos + 1] if pos + 1 < len(starts) else n
        end = next((k for k in range(i + 1, limit) if FTK_END.search(lines[k])), None)
        last = i
        for k in range(i + 1, end if end is not None else limit):
            s = lines[k].strip()
            if not s:
                continue
            nums = _number_list(s)
            if nums is None:
                if end is None:
                    break                                 # no closing line: the list ends here
                info.problem_lines.append((k + 1, s))     # inside the list, so it must be sector numbers
            else:
                entries += [(a, b, k) for a, b in nums]
                last = k
            info.consumed.add(k)
        if end is not None:
            info.consumed.add(end)
        else:
            msg = FTK_END_MISSING.format(line=f"{last + 1:,}")
            if msg not in info.errors:
                info.errors.append(msg)
        listed = sorted({k for _a, _b, k in entries if k != i})
        if inline and listed:
            # sectors on the heading line and a list under it: one of them shouldn't be there
            info.problem_lines.append((i + 1, lines[i].strip()))
        _check_order(entries, lines, info)
        _check_layout(listed, lines, info, by_count=True)
        info.ranges += [(a, b) for a, b, _k in entries]
    if info.list_found:
        info.reading = "first sector through last sector"


# ---------------------------------------------------------------- Tableau TX1

SECTION_HEAD = re.compile(
    r"(?i)\bunreadable\b|\bunrecoverable\b|could\s+not\s+be\s+read"
    r"|\b(?:bad|failed|failing|defective|damaged)\s+(?:sectors?|blocks?|regions?|ranges?|areas?|lbas?)\b"
    r"|\b(?:read\s+)?errors?\s+(?:regions?|ranges?|sectors?|blocks?|list|lbas?|map)\b"
    r"|\b(?:sectors?|blocks?|lbas?)\s+(?:errors?|failures?)\b"
    r"|^\s*(?:sector|start|first|lba)\s*(?:address|lba|sector)?\s+.*\b(?:length|count|size|end|last)\b\s*$"
)
ROW_WORDS = re.compile(
    r"(?i)\b(?:sectors?|lba|address(?:es)?|addr|start(?:ing)?|first|last|end(?:ing)?|length|len"
    r"|count|size|blocks?|through|thru|to|and|at|from|of|for|regions?|ranges?|no)\b")
ROW_ALLOWED = re.compile(r"[ \t,:()=|#\-–—]*")     # the only punctuation a list line may use
COLUMN_WORDS = re.compile(
    r"(?i)\b(?:address|lba|start(?:ing)?|first|sectors?|length|len|count|size|end|last|blocks?|offset)\b")
META_ROW = re.compile(r"(?i)^\s*(?:block|sector)s?\s+(?:size|count)\b|^\s*capacity\b")
SEPARATOR = re.compile(r"[-=_*~#|+.:\s]+")
TX1_COUNT = re.compile(
    rf"(?im)^[ \t]*(?P<name>(?:total[ \t]+)?(?:number[ \t]+of[ \t]+)?(?:unrecoverable|uncorrectable|unreadable|bad)"
    rf"[ \t]+(?:read[ \t]+)?(?:errors?|sectors?|blocks?)(?:[ \t]+count)?)[ \t]*:[ \t]*(?P<n>{NUM})\b")
TX1_TOTAL = re.compile(
    rf"(?im)^[ \t]*(?P<name>total[ \t]+(?:read[ \t]+)?errors?(?:[ \t]+count)?)[ \t]*:[ \t]*(?P<n>{NUM})\b")
TX1_RECOVERABLE = re.compile(rf"(?im)^[ \t]*(?:total[ \t]+)?recoverable[ \t]+(?:read[ \t]+)?errors?[ \t]*:[ \t]*{NUM}")


def _blank(s):
    return not s or SEPARATOR.fullmatch(s) is not None


def _header_semantics(line):
    low = line.lower()
    has_len = re.search(r"\b(?:length|len|count|size|number\s+of)\b", low)
    has_end = re.search(r"\b(?:end|ending|last|through|thru|stop)\b", low)
    if has_end and not has_len:
        return "end"
    if has_len and not has_end:
        return "length"
    return None


def _parse_row(s):
    """Classify one line of a bad-sector list. Returns None if it isn't a clean one."""
    if META_ROW.search(s):
        return None
    toks = list(NUM_RE.finditer(s))
    if not toks:
        return None
    leftover = ROW_WORDS.sub(" ", NUM_RE.sub(" ", s)).replace("..", " ")
    if not ROW_ALLOWED.fullmatch(leftover):
        return None                                      # other words or stray symbols
    vals = [to_int(t.group(0)) for t in toks]
    if len(vals) == 1 and re.search(r"[A-Za-z][^:]*:", s):
        return None                                      # a "Key: value" line
    if len(vals) == 2:
        m = re.search(rf"(?i)\b(?:length|len|count|size|for)\b\s*[:=]?\s*({NUM})", s)
        if m:
            other = next(t for t in toks if t.start() != m.start(1))
            return ("len", to_int(other.group(0)), to_int(m.group(1)))
        m = re.search(rf"(?i)\b(?:end|ending|last)\b\s*[:=]?\s*({NUM})", s)
        if m:
            other = next(t for t in toks if t.start() != m.start(1))
            return ("range", to_int(other.group(0)), to_int(m.group(1)))
        if re.search(rf"{NUM}\s*\(\s*{NUM}\s*(?:sectors?|blocks?)?\s*\)", s) or \
           re.search(rf"{NUM}\s*(?:sectors?|blocks?)\s*\)?\s*$", s, re.I):
            return ("len", vals[0], vals[1])
        m = re.search(rf"({NUM})\s*({RANGE_SEP})\s*({NUM})", s, re.I)
        if m:
            a, b = to_int(m.group(1)), to_int(m.group(3))
            if b >= a or m.group(2).lower() in ("through", "thru", "to"):
                return ("range", a, b)
        return ("pair", vals[0], vals[1])
    if len(vals) == 1:
        return ("single", vals[0], vals[0])
    return ("multi", vals)                               # sorted out by _settle_vals


def _settle_vals(v):
    """Rows with 3 numbers are only accepted when the numbers agree with each other."""
    if len(v) == 2:
        return ("pair", v[0], v[1])
    if len(v) == 3:
        a, b, c = v
        if b >= a and c == b - a + 1:
            return ("range", a, b)                       # first, last, count
        if c >= a and b == c - a + 1:
            return ("range", a, c)                       # first, count, last
        if b > a and c == b - a:
            return ("range", a, b - 1)                   # start, end (exclusive), count
    return None                                          # can't tell what these numbers are


def _resolve(rows, sem, total, pair_mode):
    """How to read two-number rows: 'length' (start, count) or 'end' (first, last)."""
    pairs = [(r[1], r[2]) for r in rows if r[0] == "pair"]
    if not pairs:
        return None, ""
    descending = any(b < a for a, b in pairs)
    if sem == "end" and descending:
        return "conflict", ""
    if sem:
        return sem, "headings"
    if descending:
        return "length", "numbers"                       # an end can't come before its start
    if total:
        over_len = any(a + b - 1 >= total for a, b in pairs)
        over_end = any(b >= total for a, b in pairs)
        if over_len and not over_end:
            return "end", "numbers"
        if over_end and not over_len:
            return "length", "numbers"
    if pair_mode in ("end", "length"):
        return pair_mode, "user"
    return "ask", ""


def _tableau_details(text, info):
    anchor = _search(r"^[ \t]*(?:[\w./()-]+[ \t]+){0,3}source\b", text)
    pos = anchor.start() if anchor else 0

    def near(pattern):
        return _search(pattern, text, pos) or _search(pattern, text)

    m = near(rf"^[ \t]*(?:Block|Sector)[ \t]+Size[ \t]*:[ \t]*({NUM})")
    if m:
        info.sector_size = to_int(m.group(1))
    m = near(rf"^[ \t]*Block[ \t]+Count[ \t]*:[ \t]*({NUM})")
    if m:
        info.total_sectors, info.total_note = to_int(m.group(1)), "from log"
    if info.total_sectors is None:
        m = near(rf"^[ \t]*(?:Total[ \t]+)?Sector[ \t]+Count[ \t]*:[ \t]*({NUM})")
        if m:
            info.total_sectors, info.total_note = to_int(m.group(1)), "from log"
    if info.total_sectors is None:
        m = near(rf"^[ \t]*Capacity[^:\n]*:[ \t]*({NUM})(?![\d.])")
        if m and to_int(m.group(1)) >= 1_000_000:
            info.total_sectors = to_int(m.group(1)) // (info.sector_size or DEFAULT_SECTOR_SIZE)
            info.total_note = "from capacity in log"
    m = near(r"^[ \t]*Model[ \t]*:[ \t]*(\S.*?)[ \t]*$")
    if m:
        info.model = m.group(1)
    m = near(r"^[ \t]*Serial(?:[ \t]+(?:Number|No\.?|#))?[ \t]*:[ \t]*(\S.*?)[ \t]*$")
    if m:
        info.serial = m.group(1)

    # the log's own error count ("Total unrecoverable errors: 4")
    m = TX1_COUNT.search(text) or TX1_TOTAL.search(text)
    if m:
        info.reported_errors = to_int(m.group("n"))
        info.reported_label = re.sub(r"\s+", " ", m.group("name")).strip()
        info.consumed.add(_line_index(text, m.start()))
    m = TX1_RECOVERABLE.search(text)
    if m:
        info.consumed.add(_line_index(text, m.start()))

    # a job that stopped early only lists the bad sectors it reached
    status = _search(r"^[ \t]*Status[ \t]*:[ \t]*(\S.*?)[ \t]*$", text)
    caution = _search(r"did[ \t]+not[ \t]+complete[ \t]+normally|possible[ \t]+power[ \t]+loss", text)
    if caution or (status and re.match(r"(?i)(?:fail|cancel|abort|incomplete|error|stop)", status.group(1))):
        reason = _search(r"^[ \t]*Reason(?:[ \t]+for[ \t]+failure)?[ \t]*:[ \t]*(\S.*?)[ \t]*$", text)
        why = f" ({reason.group(1)})" if reason else ""
        info.warnings.append(f"The log says this job didn't finish normally{why}, so part of the drive may not "
                             "have been read – the real number of bad sectors could be higher.")


def _tableau_ranges(lines, info, pair_mode=None):
    sections = []
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        if i in info.consumed or not SECTION_HEAD.search(line) or _parse_row(line.strip()) is not None:
            i += 1
            continue
        sem = _header_semantics(line)
        raw, bad, used, j = [], [], [i], i + 1
        while j < n:
            s = lines[j].strip()
            if _blank(s):
                j += 1
                continue
            row = _parse_row(s)
            if row is not None:
                raw.append((row, j))
                used.append(j)
                j += 1
                continue
            if not raw and not bad and COLUMN_WORDS.search(s) and not NUM_RE.search(s):
                sem = _header_semantics(s) or sem          # the column headings
                used.append(j)
                j += 1
                continue
            if SECTION_HEAD.search(s):
                break                                      # another section starts
            # a line touching the list, or with list lines after it, is part of the list we couldn't read
            k = j + 1
            while k < n and _blank(lines[k].strip()):
                k += 1
            glued = bool(raw or bad) and not _blank(lines[j - 1].strip())
            more = bool(raw or bad) and k < n and _parse_row(lines[k].strip()) is not None
            if glued or more:
                bad.append((j + 1, s))
                used.append(j)
                j += 1
                continue
            break
        if not raw and not bad:
            i += 1
            continue
        info.list_found = True
        if info.section_start is None:
            info.section_start = i
        info.consumed.update(used)
        # rows numbered 1, 2, 3 ... in front - only when every row is numbered that way
        numbered = all(r[0] == "multi" and r[1][0] == idx + 1 for idx, (r, _j) in enumerate(raw))
        two_columns = sem is not None                    # headings name two columns, so two numbers a row
        rows = []
        for r, rj in raw:
            if r[0] == "multi":
                vals = r[1][1:] if numbered else r[1]
                r = None if two_columns and len(vals) != 2 else _settle_vals(vals)
            elif r[0] == "single" and two_columns:
                r = None
            if r is None:
                bad.append((rj + 1, lines[rj].strip()))
                continue
            rows.append(r + (rj,))
        _check_layout([rj for _r, rj in raw], lines, info)
        kinds = {r[0] for r in rows}
        if "single" in kinds and kinds - {"single"}:
            # a lone number inside a list of ranges isn't a sector entry we can trust
            bad += [(r[3] + 1, lines[r[3]].strip()) for r in rows if r[0] == "single"]
            rows = [r for r in rows if r[0] != "single"]
        info.problem_lines.extend(bad)
        sections.append((rows, sem))
        i = j

    kinds = set()
    for rows, sem in sections:
        mode, how = _resolve(rows, sem, info.total_sectors, pair_mode)
        if mode == "conflict":
            info.errors.append("The bad-sector list's headings say the second number is the last sector, but "
                               "some lines end before they start, so the list can't be read reliably.")
            continue
        if mode == "ask":
            pairs = [(r[1], r[2]) for r in rows if r[0] == "pair"]
            info.ambiguous = (pairs[0], len(pairs))
            continue
        entries = []
        for kind, a, b, idx in rows:
            if kind == "pair":
                kind = "len" if mode == "length" else "range"
            if kind == "len":
                if b <= 0:
                    info.problem_lines.append((idx + 1, lines[idx].strip()))
                    continue
                entries.append((a, a + b - 1, idx))
                kinds.add("start sector + length")
            elif kind == "range":
                if b < a:
                    info.problem_lines.append((idx + 1, lines[idx].strip()))
                    continue
                entries.append((a, b, idx))
                kinds.add("first – last sector")
            else:
                entries.append((a, a, idx))
                kinds.add("single sectors")
        _check_order(entries, lines, info)
        info.ranges += [(a, b) for a, b, _k in entries]
        if how:
            kinds.add({"headings": "(from the column headings)", "numbers": "(worked out from the numbers)",
                       "user": "(as you chose)"}[how])
    words = sorted(k for k in kinds if not k.startswith("("))
    extra = sorted(k for k in kinds if k.startswith("("))
    info.reading = ", ".join(words) + (" " + extra[0] if extra and words else "")


# ---------------------------------------------------------------- lines nothing used

BAD_PHRASE = re.compile(
    r"(?i)could\s+not\s+be\s+read|replaced\s+with\s+zeros|\bunreadable\b|\bunrecoverable\b|\buncorrectable\b"
    r"|\bbad\s+(?:sectors?|blocks?|regions?|ranges?)\b|\bread\s+(?:errors?|failures?)\b"
    r"|\b(?:sectors?|blocks?|lbas?)\s+(?:errors?|failures?)\b|\bfailed\s+(?:sectors?|blocks?|reads?)\b")
FREE_TEXT = re.compile(
    r"(?i)^\s*(?:case\s+)?(?:notes?|comments?|description|unique\s+description|examiner|user"
    r"|case\s+(?:number|id|name)|evidence\s+(?:number|id))\s*:")
ZERO_STATEMENT = re.compile(
    r"(?i):\s*(?:none|no|0|nil|n/?a)\s*\.?\s*$"
    r"|^\s*(?:no|zero|0)\s+(?:\w+\s+){0,2}(?:bad|unreadable|unrecoverable|uncorrectable|read\s+errors?)")
ZERO_LINE = re.compile(r"(?i)\s*(?:none|no|nil|n/?a|0)\s*\.?\s*")


def _benign(s):
    """Lines that mention bad sectors without recording any: typed notes and settings."""
    if FREE_TEXT.match(s) or re.search(r"(?i)granularity|\bretr(?:y|ies)\b", s):
        return True
    return bool(re.search(r"(?i)\b(?:settings?|options?|handling|mode)\b", s)) and not re.search(r"[0-9]", s)


def _check_leftovers(lines, info):
    """Anything about bad sectors that the list reader didn't account for stops the figures."""
    n = len(lines)
    for idx, line in enumerate(lines):
        if idx in info.consumed:
            continue
        s = line.strip()
        if not s:
            continue
        if BAD_PHRASE.search(s):
            if _benign(s):
                continue
            if ZERO_STATEMENT.search(s):
                info.zero_statement = True
                continue
            k = idx + 1
            while k < n and not lines[k].strip():
                k += 1
            if s.endswith(":") and k < n and ZERO_LINE.fullmatch(lines[k]):
                info.zero_statement = True               # "Bad sectors:" followed by "None"
                info.consumed.add(k)
                continue
            if not info.list_found or re.search(r"[0-9]", s):
                info.problem_lines.append((idx + 1, s))
        else:
            row = _parse_row(s)
            if row is not None and row[0] != "single":
                info.problem_lines.append((idx + 1, s))          # list entries outside any list


# ---------------------------------------------------------------- entry point

def parse_log(text, path="", pair_mode=None):
    info = LogInfo(path=path)
    if re.match(rf"\s*{re.escape(APP_NAME)}\s+[–-]\s+(?:report|couldn)", text):
        info.errors.append(f"This is a report saved by {APP_NAME}, not an imaging log. "
                           "Open the original FTK Imager or TX1 log instead.")
        return info
    info.tool = detect_tool(text)
    if info.tool == "Unknown":
        info.errors.append("This doesn't look like an FTK Imager or Tableau TX1 log.")
        return info
    lines = text.split("\n")
    if info.tool == "FTK Imager":
        _ftk_details(text, info)
        _ftk_ranges(lines, info)
    else:
        _tableau_details(text, info)
        _tableau_ranges(lines, info, pair_mode)
    _check_leftovers(lines, info)
    info.problem_lines = sorted(set(info.problem_lines))
    info.order_lines = sorted(set(info.order_lines))

    count = (f"The log gives “{info.reported_label}” as {info.reported_errors:,}, but"
             if info.reported_errors else "")
    if not info.list_found:
        if count:
            info.errors.append(f"{count} the app couldn't find a list of bad sectors it can read.")
        elif info.problem_lines:
            info.errors.append("The log mentions bad or unreadable sectors (lines below), but the app couldn't "
                               "find a list of them it can read.")
        elif info.tool == "Tableau TX1" and info.reported_errors is None and not info.zero_statement:
            info.errors.append("No list of bad sectors was found, and the log doesn't give an error count, so "
                               "the app can't confirm whether there were any bad sectors.")
    elif info.problem_lines:
        n = len(info.problem_lines)
        info.errors.append(f"{n:,} line{'s' if n != 1 else ''} in the bad-sector part of the log couldn't be "
                           "read, so any total would be wrong.")
    elif not info.ranges and not info.ambiguous and not info.errors and not info.order_lines:
        info.errors.append("A bad-sector section was found, but no sector numbers could be read from it.")
    if info.order_lines:
        info.errors.append("The bad-sector list goes backwards or overlaps – each range should start after the "
                           "one before it ends – so the log may have been damaged or edited.")

    if info.ranges and (info.reported_errors == 0 or info.zero_statement):
        info.warnings.append("The log says there were no unrecoverable errors, but it also lists unreadable "
                             "sectors – check the log.")
    if info.total_sectors is None:
        info.warnings.append("The drive's total sector count isn't in the log. Type it into 'Total sectors' to "
                             "get the % figure.")
    if info.sector_size is None and info.ranges:
        info.warnings.append("Bytes per sector isn't in the log, so 512 is assumed.")
    return info


def parse_file(path, pair_mode=None):
    """Raises LogReadError if the file can't be read as text at all."""
    return parse_log(read_text(path), path, pair_mode)


def clean_basis(info):
    """Why a log with no bad sectors is being reported as clean."""
    if info.tool == "FTK Imager":
        return "FTK Imager didn't list any sectors that couldn't be read."
    if info.reported_errors == 0:
        return f"The log gives “{info.reported_label}” as 0."
    return "The log says there were none."


# =============================================================================
#  Analysis
# =============================================================================

@dataclass
class Result:
    chunks: list            # merged contiguous (first, last) runs
    total_bad: int
    entries: int            # distinct ranges listed in the log
    duplicates: int
    bands: list             # (label, chunks, sectors)
    first_bad: int          # bad sectors inside LBA 0-2047
    first_chunks: list      # those runs, clipped to the region
    beyond_end: int         # bad sectors at or past the drive's last sector


def merge_ranges(ranges):
    norm = sorted((min(a, b), max(a, b)) for a, b in ranges)
    merged = []
    for s, e in norm:
        if merged and s <= merged[-1][1] + 1:
            if e > merged[-1][1]:
                merged[-1][1] = e
        else:
            merged.append([s, e])
    return [(s, e) for s, e in merged]


def analyse(ranges, total_sectors=None):
    distinct = set((min(a, b), max(a, b)) for a, b in ranges)
    chunks = merge_ranges(distinct)
    sizes = [e - s + 1 for s, e in chunks]
    bands = []
    for label, lo, hi in BANDS:
        picked = [z for z in sizes if z >= lo and (hi is None or z <= hi)]
        bands.append((label, len(picked), sum(picked)))
    first = [(s, min(e, FIRST_REGION - 1)) for s, e in chunks if s < FIRST_REGION]
    beyond = 0
    if total_sectors:
        beyond = sum(e - max(s, total_sectors) + 1 for s, e in chunks if e >= total_sectors)
    return Result(chunks=chunks, total_bad=sum(sizes), entries=len(distinct),
                  duplicates=len(ranges) - len(distinct), bands=bands,
                  first_bad=sum(e - s + 1 for s, e in first), first_chunks=first,
                  beyond_end=beyond)


# =============================================================================
#  Formatting and the text report
# =============================================================================

def fmt_num(x):
    """2 decimal places, or 2 significant figures for very small values."""
    if x == 0:
        return "0.00"
    if abs(x) >= 0.01:
        return f"{x:,.2f}"
    decimals = -int(math.floor(math.log10(abs(x)))) + 1
    return f"{x:.{decimals}f}"


def fmt_pct(bad, total):
    if not total:
        return "—"
    p = bad * 100.0 / total
    if p == 0:
        return "0%"
    if p >= 1:
        return f"{p:,.2f}%"
    decimals = max(2, -int(math.floor(math.log10(p))) + 1)
    return f"{p:.{decimals}f}%"


def fmt_bytes(n):
    if n < KB:
        return f"{n:,} bytes"
    for unit, div in (("KB", KB), ("MB", MB), ("GB", GB)):
        if n < div * 1024:
            return f"{fmt_num(n / div)} {unit}"
    return f"{fmt_num(n / TB)} TB"


def size_pair(nbytes):
    """('4.00 MB', '0.0039 GB') – MB first unless it's a gigabyte or more."""
    mb, gb = f"{fmt_num(nbytes / MB)} MB", f"{fmt_num(nbytes / GB)} GB"
    return (gb, mb) if nbytes >= GB else (mb, gb)


def clip(s, n=110):
    s = s.replace("�", "�")
    return s if len(s) <= n else s[:n - 1] + "…"


def drive_text(info):
    if info.model and info.serial:
        return f"{info.model}  (S/N {info.serial})"
    return info.model or (f"S/N {info.serial}" if info.serial else "")


def result_warnings(info, res, total):
    notes = list(info.warnings)
    if res.duplicates:
        notes.append(f"{res.duplicates:,} range{' was' if res.duplicates == 1 else 's were'} "
                     "listed more than once in the log and counted once.")
    if res.beyond_end:
        notes.append(f"{res.beyond_end:,} listed sectors are past the end of the drive "
                     f"({total:,} sectors) – check the log and the total sector count.")
    return notes


def merge_note(res):
    if res.entries and res.entries != len(res.chunks):
        return (f"The log lists {res.entries:,} ranges; where ranges touch they are joined, "
                f"giving {len(res.chunks):,} contiguous chunks.")
    return ""


def build_report(info, res, total, bps, include_list=True):
    bad_bytes = res.total_bad * bps
    out = []
    add = out.append
    add(f"{APP_NAME} – report")
    add("=" * 66)
    add(f"Generated          : {datetime.datetime.now():%d/%m/%Y %H:%M}  (v{APP_VERSION})")
    add(f"Log file           : {info.path}")
    add(f"Log type           : {info.tool}")
    if drive_text(info):
        add(f"Drive              : {drive_text(info)}")
    add(f"Total sectors      : {total:,}" if total else "Total sectors      : not known")
    add(f"Bytes per sector   : {bps:,}")
    if info.reading and info.tool != "FTK Imager":
        add(f"Bad-sector list    : read as {info.reading}")
    if info.reported_errors is not None:
        add(f"Errors in log      : {info.reported_errors:,}  ({info.reported_label})")
    add("")
    add(f"Size failed        : {fmt_num(bad_bytes / MB)} MB  /  {fmt_num(bad_bytes / GB)} GB"
        f"  ({bad_bytes:,} bytes)")
    add(f"Total bad sectors  : {res.total_bad:,}")
    add(f"% bad sectors      : {fmt_pct(res.total_bad, total)}")
    if res.first_bad:
        where = ", ".join(f"{s:,}–{e:,}" if s != e else f"{s:,}" for s, e in res.first_chunks[:6])
        more = " …" if len(res.first_chunks) > 6 else ""
        add(f"First 2048 sectors : YES – {res.first_bad:,} bad sector{'s' if res.first_bad != 1 else ''} "
            f"in LBA 0–2047 (LBA {where}{more})")
    else:
        add("First 2048 sectors : NO – no bad sectors in LBA 0–2047")
    add("")
    add("Failed chunk sizes (contiguous runs of bad sectors)")
    add(f"  {'Chunk size (sectors)':<24}{'Chunks':>10}{'Bad sectors':>16}{'Size':>14}")
    add("  " + "-" * 64)
    for label, count, sectors in res.bands:
        size = fmt_bytes(sectors * bps) if sectors else "–"
        add(f"  {label:<24}{count:>10,}{sectors:>16,}{size:>14}")
    add("  " + "-" * 64)
    add(f"  {'Total':<24}{len(res.chunks):>10,}{res.total_bad:>16,}{fmt_bytes(bad_bytes) if res.total_bad else '–':>14}")
    notes = result_warnings(info, res, total) + ([merge_note(res)] if merge_note(res) else [])
    if not res.chunks:
        notes.insert(0, "No bad sectors: " + clean_basis(info))
    if notes:
        add("")
        add("Notes")
        for note in notes:
            add(f"  * {note}")
    if include_list and res.chunks:
        add("")
        add(f"Bad sector chunks ({len(res.chunks):,})")
        add(f"  {'First LBA':>16}{'Last LBA':>16}{'Sectors':>12}")
        for s, e in res.chunks:
            add(f"  {s:>16,}{e:>16,}{e - s + 1:>12,}")
    return "\n".join(out) + "\n"


def build_problem_report(info, path):
    out = [f"{APP_NAME} – couldn't calculate", "=" * 66, f"Program  : {APP_NAME} v{APP_VERSION}",
           f"Log file : {path}"]
    if info.tool != "Unknown":
        out.append(f"Log type : {info.tool}")
    out.append("")
    out.extend(blocking_problems(info))
    lines = check_lines(info)
    if lines:
        out += ["", "Lines to check in the log:"]
        out += [f"  Line {no:,}:  {clip(text, 200)}" for no, text in lines]
    if info.ambiguous and not info.errors:
        out += ["", "Run again with  --pairs end     (second number = last sector)",
                "            or  --pairs length  (second number = number of sectors)."]
    out += ["", "No figures were calculated."]
    return "\n".join(out) + "\n"


def parse_user_int(text):
    t = re.sub(r"[\s,_]", "", text or "")
    if t.isdigit() and int(t) > 0:
        return int(t)
    return None


# =============================================================================
#  Window
# =============================================================================

P = {
    "bg": "#EEF2F6",
    "card": "#FFFFFF",
    "border": "#D5DCE5",
    "header": "#14304D",
    "header_sub": "#A9BFD6",
    "version_bg": "#A9C7E8",
    "primary": "#2D7FF9",
    "primary_hover": "#1E6BE0",
    "secondary": "#DCE6F2",
    "secondary_hover": "#CAD8EA",
    "text": "#1A2433",
    "muted": "#5E6C7E",
    "faint": "#A7B1BD",
    "bad": "#C0392B",
    "good": "#1E8449",
    "warn_bg": "#FFF3D1",
    "warn_fg": "#664A00",
    "ok_bg": "#E3F3E8",
    "ok_fg": "#1D5631",
    "err_bg": "#FBE7E4",
    "err_fg": "#8C2117",
    "first_bg": "#FCE9E7",
    "total_bg": "#F2F5F9",
}


def _pick_family(root):
    import tkinter.font as tkfont
    available = set(tkfont.families(root))
    for fam in ("Segoe UI", "SF Pro Text", "Helvetica Neue", "Helvetica", "Arial",
                "DejaVu Sans", "Liberation Sans"):
        if fam in available:
            return fam
    return "TkDefaultFont"


class App:
    def __init__(self, root, start_file=None):
        import tkinter as tk
        import tkinter.font as tkfont
        from tkinter import ttk
        self.tk, self.ttk, self.tkfont = tk, ttk, tkfont
        self.root = root
        self.info = None
        self.res = None
        self.total = None
        self.bps = DEFAULT_SECTOR_SIZE
        self._dialog = None
        self._dialog_pick = None
        self.last_dir = os.path.expanduser("~")
        self.scale = max(1.0, root.winfo_fpixels("1i") / 96.0)
        fam = _pick_family(root)
        self.F = {
            "title": (fam, 16, "bold"),
            "subtitle": (fam, 10),
            "version": (fam, 9, "bold"),
            "file": (fam, 12, "bold"),
            "body": (fam, 10),
            "body_b": (fam, 10, "bold"),
            "small": (fam, 9),
            "card_t": (fam, 8, "bold"),
            "card_v": (fam, 21, "bold"),
            "btn": (fam, 10, "bold"),
        }
        root.title(f"{APP_NAME}  v{APP_VERSION}")
        root.configure(bg=P["bg"])
        root.geometry(f"{self.px(1000)}x{self.px(740)}")
        root.minsize(self.px(900), self.px(640))
        self._set_icon()
        self._styles()
        self._build_header()
        self._build_footer()
        self._build_body()
        self._show_empty()
        root.bind_all("<Control-o>", lambda e: self.choose_file())
        root.bind_all("<Control-s>", lambda e: self.save_report())
        if start_file:
            root.after(200, lambda: self.load(start_file))

    # ------------------------------------------------------------ helpers
    def px(self, n):
        return int(round(n * self.scale))

    def _set_icon(self):
        here = os.path.dirname(os.path.abspath(__file__))
        ico, png = os.path.join(here, "icon.ico"), os.path.join(here, "icon.png")
        try:
            if sys.platform == "win32" and os.path.exists(ico):
                self.root.iconbitmap(default=ico)
            elif os.path.exists(png):
                self._icon = self.tk.PhotoImage(file=png)
                self.root.iconphoto(True, self._icon)
        except Exception:
            pass

    def _styles(self):
        st = self.ttk.Style(self.root)
        if "vista" in st.theme_names():
            st.theme_use("vista")
        elif "clam" in st.theme_names():
            st.theme_use("clam")
            st.configure("TNotebook", background=P["bg"], borderwidth=0)
            st.configure("TNotebook.Tab", background=P["secondary"], foreground=P["header"], borderwidth=0)
            st.map("TNotebook.Tab", background=[("selected", P["card"])], foreground=[("selected", P["text"])])
            st.configure("Treeview.Heading", background="#E8EDF3", relief="flat")
            st.map("Treeview.Heading", background=[("active", "#DCE4EE")])
        line = self.tkfont.Font(root=self.root, font=self.F["body"]).metrics("linespace")
        st.configure("Treeview", font=self.F["body"], rowheight=line + self.px(9))
        st.configure("Treeview.Heading", font=self.F["body_b"])
        st.configure("TNotebook.Tab", font=self.F["body"], padding=(self.px(14), self.px(5)))

        def fixed(option):   # lets row colours show on older Tk builds
            return [e for e in st.map("Treeview", query_opt=option) if e[:2] != ("!disabled", "!selected")]
        st.map("Treeview", foreground=fixed("foreground"), background=fixed("background"))

    def _button(self, parent, text, command, primary=False):
        bg = P["primary"] if primary else P["secondary"]
        hover = P["primary_hover"] if primary else P["secondary_hover"]
        fg = "#FFFFFF" if primary else P["header"]
        b = self.tk.Button(parent, text=text, command=command, font=self.F["btn"], bg=bg, fg=fg,
                           activebackground=hover, activeforeground=fg, disabledforeground=P["faint"],
                           relief="flat", bd=0, padx=self.px(16), pady=self.px(6), cursor="hand2",
                           highlightthickness=0)
        b.bind("<Enter>", lambda e: b.configure(bg=hover) if str(b["state"]) != "disabled" else None)
        b.bind("<Leave>", lambda e: b.configure(bg=bg))
        return b

    # ------------------------------------------------------------ layout
    def _build_header(self):
        tk = self.tk
        hdr = tk.Frame(self.root, bg=P["header"])
        hdr.pack(fill="x")
        inner = tk.Frame(hdr, bg=P["header"])
        inner.pack(fill="x", padx=self.px(24), pady=self.px(14))
        left = tk.Frame(inner, bg=P["header"])
        left.pack(side="left")
        title = tk.Frame(left, bg=P["header"])
        title.pack(anchor="w")
        tk.Label(title, text=APP_NAME, font=self.F["title"], fg="#FFFFFF", bg=P["header"]).pack(side="left")
        self.version_lbl = tk.Label(title, text=f"v{APP_VERSION}", font=self.F["version"], fg=P["header"],
                                    bg=P["version_bg"], padx=self.px(7), pady=self.px(1))
        self.version_lbl.pack(side="left", padx=(self.px(10), 0), pady=(self.px(3), 0))
        tk.Label(left, text="Reads FTK Imager and Tableau TX1 acquisition logs",
                 font=self.F["subtitle"], fg=P["header_sub"], bg=P["header"]).pack(anchor="w")
        self.open_btn = self._button(inner, "Open log file…", self.choose_file, primary=True)
        self.open_btn.pack(side="right")

    def _build_footer(self):
        tk, ttk = self.tk, self.ttk
        foot = tk.Frame(self.root, bg=P["bg"])
        foot.pack(side="bottom", fill="x", padx=self.px(24), pady=(self.px(10), self.px(16)))
        left = tk.Frame(foot, bg=P["bg"])
        left.pack(side="left")
        tk.Label(left, text="Total sectors on drive", font=self.F["small"], fg=P["muted"],
                 bg=P["bg"]).grid(row=0, column=0, columnspan=2, sticky="w")
        self.total_var = tk.StringVar()
        self.total_entry = ttk.Entry(left, textvariable=self.total_var, width=16, font=self.F["body"])
        self.total_entry.grid(row=1, column=0, sticky="w")
        self.total_src = tk.Label(left, text="", font=self.F["small"], fg=P["muted"], bg=P["bg"])
        self.total_src.grid(row=1, column=1, sticky="w", padx=(self.px(6), self.px(18)))
        tk.Label(left, text="Bytes per sector", font=self.F["small"], fg=P["muted"],
                 bg=P["bg"]).grid(row=0, column=2, sticky="w")
        self.bps_var = tk.StringVar(value=str(DEFAULT_SECTOR_SIZE))
        self.bps_entry = ttk.Entry(left, textvariable=self.bps_var, width=7, font=self.F["body"])
        self.bps_entry.grid(row=1, column=2, sticky="w")
        self.update_btn = self._button(left, "Update", self.refresh)
        self.update_btn.grid(row=1, column=3, sticky="w", padx=(self.px(10), 0))
        for entry in (self.total_entry, self.bps_entry):
            entry.bind("<Return>", lambda e: self.refresh())
        right = tk.Frame(foot, bg=P["bg"])
        right.pack(side="right", anchor="s")
        self.save_btn = self._button(right, "Save report…", self.save_report)
        self.save_btn.pack(side="right")
        self.copy_btn = self._button(right, "Copy results", self.copy_results)
        self.copy_btn.pack(side="right", padx=(0, self.px(8)))

    def _build_body(self):
        tk, ttk = self.tk, self.ttk
        body = tk.Frame(self.root, bg=P["bg"])
        body.pack(fill="both", expand=True, padx=self.px(24), pady=(self.px(16), 0))
        self.file_lbl = tk.Label(body, text="", font=self.F["file"], fg=P["text"], bg=P["bg"], anchor="w")
        self.file_lbl.pack(fill="x")
        self.detail_lbl = tk.Label(body, text="", font=self.F["small"], fg=P["muted"], bg=P["bg"],
                                   anchor="w", justify="left")
        self.detail_lbl.pack(fill="x", pady=(self.px(3), 0))
        body.bind("<Configure>", lambda e: self.detail_lbl.configure(wraplength=max(self.px(300), e.width - self.px(10))), add="+")
        self.banner = tk.Label(body, text="", font=self.F["small"], anchor="w", justify="left",
                               padx=self.px(12), pady=self.px(8))

        self.cards_frame = tk.Frame(body, bg=P["bg"])
        self.cards_frame.pack(fill="x", pady=(self.px(14), self.px(14)))
        self.cards = {}
        specs = (("size", "SIZE FAILED"), ("total", "TOTAL BAD SECTORS"),
                 ("pct", "% BAD SECTORS"), ("first", "BAD SECTORS IN FIRST 2048?"))
        for col, (key, title) in enumerate(specs):
            self.cards_frame.columnconfigure(col, weight=1, uniform="cards")
            card = tk.Frame(self.cards_frame, bg=P["card"], highlightthickness=1,
                            highlightbackground=P["border"], highlightcolor=P["border"])
            card.grid(row=0, column=col, sticky="nsew",
                      padx=(0 if col == 0 else self.px(6), 0 if col == len(specs) - 1 else self.px(6)))
            tk.Label(card, text=title, font=self.F["card_t"], fg=P["muted"], bg=P["card"],
                     anchor="w").pack(fill="x", padx=self.px(16), pady=(self.px(13), 0))
            value = tk.Label(card, text="—", font=self.F["card_v"], fg=P["text"], bg=P["card"], anchor="w")
            value.pack(fill="x", padx=self.px(16))
            sub = tk.Label(card, text="", font=self.F["small"], fg=P["muted"], bg=P["card"], anchor="w",
                           justify="left", wraplength=self.px(190))
            sub.pack(fill="x", padx=self.px(16), pady=(0, self.px(13)))
            card.bind("<Configure>", lambda e, s=sub: s.configure(wraplength=max(self.px(100), e.width - self.px(46))))
            self.cards[key] = (value, sub)
        body.bind("<Configure>", lambda e: self.banner.configure(wraplength=max(self.px(300), e.width - self.px(30))), add="+")

        self.nb = ttk.Notebook(body)
        self.nb.pack(fill="both", expand=True)

        tab1 = tk.Frame(self.nb, bg=P["card"])
        self.nb.add(tab1, text="Failed chunk sizes")
        cols = ("band", "chunks", "sectors", "size")
        self.bands_tv = ttk.Treeview(tab1, columns=cols, show="headings", selectmode="none",
                                     height=len(BANDS) + 1)
        for col, text, width, anchor in (("band", "Chunk size (sectors)", 250, "w"),
                                         ("chunks", "Chunks", 130, "e"),
                                         ("sectors", "Bad sectors", 150, "e"),
                                         ("size", "Size", 150, "e")):
            self.bands_tv.heading(col, text=text, anchor=anchor)
            self.bands_tv.column(col, width=self.px(width), anchor=anchor, stretch=True)
        self.bands_tv.tag_configure("zero", foreground=P["faint"])
        self.bands_tv.tag_configure("hit", foreground=P["text"])
        self.bands_tv.tag_configure("total", font=self.F["body_b"], background=P["total_bg"])
        self.bands_tv.pack(fill="both", expand=True)

        tab2 = tk.Frame(self.nb, bg=P["card"])
        self.list_tab = tab2
        self.nb.add(tab2, text="Bad sector list")
        cols = ("n", "first", "last", "count", "size", "where")
        self.list_tv = ttk.Treeview(tab2, columns=cols, show="headings", selectmode="extended")
        for col, text, width, anchor in (("n", "#", 60, "e"), ("first", "First sector (LBA)", 170, "e"),
                                         ("last", "Last sector (LBA)", 170, "e"),
                                         ("count", "Sectors", 110, "e"), ("size", "Size", 110, "e"),
                                         ("where", "", 150, "w")):
            self.list_tv.heading(col, text=text, anchor=anchor)
            self.list_tv.column(col, width=self.px(width), anchor=anchor, stretch=(col == "where"))
        self.list_tv.tag_configure("first", background=P["first_bg"])
        self.list_tv.tag_configure("more", foreground=P["muted"])
        sb = ttk.Scrollbar(tab2, orient="vertical", command=self.list_tv.yview)
        self.list_tv.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.list_tv.pack(side="left", fill="both", expand=True)

    # ------------------------------------------------------------ states
    def _show_banner(self, text, kind):
        if not text:
            self.banner.pack_forget()
            return
        bg, fg = {"warn": (P["warn_bg"], P["warn_fg"]), "ok": (P["ok_bg"], P["ok_fg"]),
                  "error": (P["err_bg"], P["err_fg"])}[kind]
        self.banner.configure(text=text, bg=bg, fg=fg,
                              font=self.F["body"] if kind == "error" else self.F["small"])
        self.banner.pack(fill="x", pady=(self.px(10), 0), before=self.cards_frame)

    def _set_buttons(self, enabled):
        state = "normal" if enabled else "disabled"
        for b in (self.copy_btn, self.save_btn, self.update_btn):
            b.configure(state=state, cursor="hand2" if enabled else "arrow")

    def _blank_figures(self, note=""):
        for value, sub in self.cards.values():
            value.configure(text="—", fg=P["faint"])
            sub.configure(text=note)
        self.bands_tv.delete(*self.bands_tv.get_children())
        for label, _lo, _hi in BANDS:
            self.bands_tv.insert("", "end", values=(label, "", "", ""), tags=("zero",))
        self.list_tv.delete(*self.list_tv.get_children())
        self.nb.tab(self.list_tab, text="Bad sector list")
        self._set_buttons(False)

    def _show_empty(self):
        self.file_lbl.configure(text="No log loaded")
        self.detail_lbl.configure(text="Click “Open log file…” and choose an FTK Imager or Tableau TX1 log (.txt). "
                                       "You can also drag a log onto the app's icon.")
        self._show_banner("", None)
        self._blank_figures()

    def _show_problem(self):
        """The log couldn't be read reliably: say why and show no figures at all."""
        info = self.info
        self.res = None
        self.file_lbl.configure(text=os.path.basename(info.path) or "Log")
        bits = [f"{info.tool} log"] if info.tool != "Unknown" else []
        if drive_text(info):
            bits.append(drive_text(info))
        self.detail_lbl.configure(text="   ·   ".join(bits))
        text = "✕  No figures shown – the bad-sector list in this file couldn't be read reliably.\n\n"
        text += "\n".join(blocking_problems(info))
        lines = check_lines(info)
        if lines:
            text += "\n\nLines to check in the log:"
            for no, line in lines[:6]:
                text += f"\n     Line {no:,}:   {clip(line)}"
            if len(lines) > 6:
                text += f"\n     … and {len(lines) - 6:,} more"
        self._show_banner(text, "error")
        self.total_src.configure(text="")
        self._blank_figures("not calculated")

    def _show_pending(self, info):
        """Switch to the new file (no figures) while the user answers a question about it."""
        self.res = None
        self.file_lbl.configure(text=os.path.basename(info.path))
        bits = [f"{info.tool} log"] if info.tool != "Unknown" else []
        if drive_text(info):
            bits.append(drive_text(info))
        self.detail_lbl.configure(text="   ·   ".join(bits))
        self._show_banner("", None)
        self.total_src.configure(text="")
        self._blank_figures("waiting for your answer")
        self.root.update_idletasks()

    def ask_pair_mode(self, info):
        """Ask how to read a two-number list the log doesn't explain. Returns 'end', 'length' or None."""
        tk = self.tk
        (a, b), count = info.ambiguous
        win = tk.Toplevel(self.root)
        win.title("How should this list be read?")
        win.configure(bg=P["card"])
        win.transient(self.root)
        win.resizable(False, False)
        choice = {"mode": None}

        def pick(mode):
            choice["mode"] = mode
            win.destroy()

        self._dialog, self._dialog_pick = win, pick
        pad = self.px(22)
        tk.Label(win, text="How should the bad-sector list be read?", font=self.F["file"], fg=P["text"],
                 bg=P["card"], anchor="w").pack(fill="x", padx=pad, pady=(pad, self.px(8)))
        lines = "1 line" if count == 1 else f"{count:,} lines"
        tk.Label(win, text=(f"The list in this log has {lines} of two numbers and no column headings, so the app "
                            "can't tell what the second number means. Choose the option that matches the log. "
                            "If you're not sure, cancel – no figures will be shown."),
                 font=self.F["body"], fg=P["text"], bg=P["card"], justify="left", anchor="w",
                 wraplength=self.px(480)).pack(fill="x", padx=pad)
        tk.Label(win, text=f"First line of the list:      {a:,}      {b:,}", font=self.F["body_b"],
                 fg=P["header"], bg=P["total_bg"], anchor="w", padx=self.px(12),
                 pady=self.px(8)).pack(fill="x", padx=pad, pady=(self.px(12), self.px(14)))
        options = (("end", "The second number is the LAST sector",
                    f"Sectors {a:,} to {b:,}  =  {b - a + 1:,} sectors"),
                   ("length", "The second number is the NUMBER of sectors",
                    f"Sectors {a:,} to {a + b - 1:,}  =  {b:,} sectors"))
        for mode, title, example in options:
            tk.Button(win, text=f"{title}\n{example}", command=lambda m=mode: pick(m), font=self.F["body"],
                      justify="left", anchor="w", bg=P["secondary"], fg=P["header"],
                      activebackground=P["secondary_hover"], activeforeground=P["header"], relief="flat", bd=0,
                      padx=self.px(14), pady=self.px(9), cursor="hand2",
                      highlightthickness=0).pack(fill="x", padx=pad, pady=(0, self.px(8)))
        self._button(win, "Cancel", win.destroy).pack(anchor="e", padx=pad, pady=(self.px(6), pad))
        win.protocol("WM_DELETE_WINDOW", win.destroy)
        win.bind("<Escape>", lambda e: win.destroy())
        win.update_idletasks()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - win.winfo_width()) // 2
        y = self.root.winfo_rooty() + self.px(150)
        win.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        try:
            win.grab_set()
        except Exception:
            pass
        win.focus_set()
        self.root.wait_window(win)
        self._dialog, self._dialog_pick = None, None
        return choice["mode"]

    # ------------------------------------------------------------ actions
    def choose_file(self):
        from tkinter import filedialog
        path = filedialog.askopenfilename(
            parent=self.root, title="Choose an FTK Imager or TX1 log", initialdir=self.last_dir,
            filetypes=[("Log files", "*.txt *.log *.htm *.html"), ("All files", "*.*")])
        if path:
            self.load(path)

    def load(self, path, pair_mode=None):
        self.root.configure(cursor="watch")
        self.root.update_idletasks()
        try:
            info = parse_file(path, pair_mode)
        except LogReadError as exc:
            info = LogInfo(path=path, errors=[str(exc)])
        except Exception as exc:                 # never show figures after an unexpected failure
            info = LogInfo(path=path, errors=[f"Something went wrong while reading this file: {exc}"])
        finally:
            self.root.configure(cursor="")
        if info.ambiguous and not info.errors and pair_mode is None:
            self._show_pending(info)
            mode = self.ask_pair_mode(info)
            if mode:
                self.load(path, mode)
                return
            info.errors = [AMBIGUOUS_MSG, "You didn't choose how to read it, so no figures are shown. "
                                          "Open the log again to choose."]
        self.info = info
        self.last_dir = os.path.dirname(os.path.abspath(path))
        self.total_var.set(f"{info.total_sectors:,}" if info.total_sectors else "")
        self.bps_var.set(str(info.sector_size or DEFAULT_SECTOR_SIZE))
        self.refresh()

    def refresh(self):
        if not self.info:
            return
        info = self.info
        if blocking_problems(info):
            self._show_problem()
            return
        total = parse_user_int(self.total_var.get())
        bps = parse_user_int(self.bps_var.get()) or DEFAULT_SECTOR_SIZE
        self.bps_var.set(str(bps))
        if total:
            self.total_var.set(f"{total:,}")
        res = analyse(info.ranges, total)
        self.info, self.res, self.total, self.bps = info, res, total, bps

        if total and total == info.total_sectors:
            self.total_src.configure(text=f"({info.total_note})")
        elif total:
            self.total_src.configure(text="(entered)")
        else:
            self.total_src.configure(text="(not in log – type it in)")

        # file + details
        self.file_lbl.configure(text=os.path.basename(info.path))
        bits = [f"{info.tool} log"]
        if drive_text(info):
            bits.append(drive_text(info))
        if info.reading and info.tool != "FTK Imager" and res.chunks:
            bits.append(f"bad-sector list read as {info.reading}")
        if info.reported_errors is not None:
            bits.append(f"{info.reported_label}: {info.reported_errors:,}")
        self.detail_lbl.configure(text="   ·   ".join(bits))

        warn = result_warnings(info, res, total)
        if warn:
            self._show_banner("⚠  " + "\n⚠  ".join(warn), "warn")
        elif not res.chunks:
            self._show_banner("✓  No unreadable sectors.  " + clean_basis(info), "ok")
        else:
            self._show_banner("", None)

        # cards
        bad_bytes = res.total_bad * bps
        big, other = size_pair(bad_bytes)
        v, s = self.cards["size"]
        v.configure(text=big, fg=P["text"])
        s.configure(text=f"{other}  ·  {bad_bytes:,} bytes")
        v, s = self.cards["total"]
        v.configure(text=f"{res.total_bad:,}", fg=P["text"])
        n = len(res.chunks)
        sub = f"in {n:,} chunk{'s' if n != 1 else ''}" if n else "none recorded"
        if res.entries and res.entries != n:
            sub += f" (from {res.entries:,} log ranges)"
        s.configure(text=sub)
        v, s = self.cards["pct"]
        v.configure(text=fmt_pct(res.total_bad, total), fg=P["text"] if total else P["faint"])
        s.configure(text=f"of {total:,} sectors" if total else "needs the total sector count")
        v, s = self.cards["first"]
        if res.first_bad:
            v.configure(text="YES", fg=P["bad"])
            s.configure(text=f"{res.first_bad:,} bad sector{'s' if res.first_bad != 1 else ''} in LBA 0–2047")
        else:
            v.configure(text="NO", fg=P["good"])
            s.configure(text="LBA 0–2047 read OK")

        # chunk-size table
        tv = self.bands_tv
        tv.delete(*tv.get_children())
        for label, count, sectors in res.bands:
            tv.insert("", "end", tags=("hit" if count else "zero",),
                      values=(label, f"{count:,}", f"{sectors:,}", fmt_bytes(sectors * bps) if sectors else "–"))
        tv.insert("", "end", tags=("total",),
                  values=("Total", f"{n:,}", f"{res.total_bad:,}", fmt_bytes(bad_bytes) if res.total_bad else "–"))

        # list of chunks
        lt = self.list_tv
        lt.delete(*lt.get_children())
        for idx, (a, b) in enumerate(res.chunks[:MAX_LIST_ROWS], 1):
            first = a < FIRST_REGION
            lt.insert("", "end", tags=("first",) if first else (),
                      values=(f"{idx:,}", f"{a:,}", f"{b:,}", f"{b - a + 1:,}", fmt_bytes((b - a + 1) * bps),
                              "in first 2048" if first else ""))
        if n > MAX_LIST_ROWS:
            lt.insert("", "end", tags=("more",),
                      values=("", f"+ {n - MAX_LIST_ROWS:,} more", "", "", "", "full list in Save report"))
        self.nb.tab(self.list_tab, text=f"Bad sector list ({n:,})")
        self._set_buttons(True)

    def copy_results(self):
        if not self.res:
            return
        text = build_report(self.info, self.res, self.total, self.bps, include_list=False)
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.copy_btn.configure(text="Copied ✓")
        self.root.after(1400, lambda: self.copy_btn.configure(text="Copy results"))

    def save_report(self):
        from tkinter import filedialog, messagebox
        if not self.res:
            return
        stem = os.path.splitext(os.path.basename(self.info.path))[0]
        path = filedialog.asksaveasfilename(
            parent=self.root, title="Save report", initialdir=self.last_dir,
            initialfile=f"{stem} - bad sector report.txt", defaultextension=".txt",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")])
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8", newline="\r\n" if sys.platform == "win32" else "\n") as fh:
                fh.write(build_report(self.info, self.res, self.total, self.bps, include_list=True))
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"Couldn't save the report:\n\n{exc}", parent=self.root)
            return
        self.save_btn.configure(text="Saved ✓")
        self.root.after(1400, lambda: self.save_btn.configure(text="Save report…"))


# =============================================================================
#  Start-up
# =============================================================================

def _windows_setup():
    if sys.platform != "win32":
        return
    try:
        import ctypes
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("BadSectorCalculator.App")
        except Exception:
            pass
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass
    except Exception:
        pass


def run_gui(start_file=None):
    import tkinter as tk
    from tkinter import messagebox
    _windows_setup()
    root = tk.Tk()

    def on_error(*exc):
        messagebox.showerror(APP_NAME, "Something went wrong:\n\n" + "".join(traceback.format_exception(*exc))[-1500:],
                             parent=root)
    root.report_callback_exception = on_error
    App(root, start_file)
    root.mainloop()
    return 0


def run_report(args):
    pair_mode = None
    if "--pairs" in args:
        k = args.index("--pairs")
        pair_mode = args[k + 1] if k + 1 < len(args) else ""
        del args[k:k + 2]
        if pair_mode not in ("end", "length"):
            print("--pairs must be 'end' or 'length'")
            return 2
    if not args:
        print("usage: bad_sector_calculator.pyw --report LOGFILE [--pairs end|length]")
        return 2
    path = args[0]
    try:
        info = parse_file(path, pair_mode)
    except LogReadError as exc:
        info = LogInfo(path=path, errors=[str(exc)])
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if blocking_problems(info):
        sys.stdout.write(build_problem_report(info, path))
        return 1
    total, bps = info.total_sectors, info.sector_size or DEFAULT_SECTOR_SIZE
    sys.stdout.write(build_report(info, analyse(info.ranges, total), total, bps, include_list=True))
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    if argv and argv[0] in ("-v", "--version"):
        print(f"{APP_NAME} v{APP_VERSION}")
        return 0
    if argv and argv[0] in ("-r", "--report"):
        if sys.stdout is None:          # started without a console (pythonw)
            return 2
        return run_report(argv[1:])
    start = argv[0] if argv and os.path.isfile(argv[0]) else None
    try:
        return run_gui(start)
    except Exception:
        try:
            import tkinter as tk
            from tkinter import messagebox
            r = tk.Tk()
            r.withdraw()
            messagebox.showerror(APP_NAME, "The app couldn't start:\n\n" + traceback.format_exc()[-1500:])
            r.destroy()
        except Exception:
            traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
