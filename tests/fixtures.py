"""Synthetic FTK Imager and Tableau TX1 log text shared by the tests and sample generators."""
import importlib.machinery
import importlib.util
import os
import sys

sys.dont_write_bytecode = True                   # don't leave __pycache__ folders behind
HERE =os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(HERE, "..", "app", "bad_sector_calculator.pyw")


def load_app():
    if "bsc" in sys.modules:
        return sys.modules["bsc"]
    loader = importlib.machinery.SourceFileLoader("bsc", APP)
    spec = importlib.util.spec_from_loader("bsc", loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["bsc"] = mod
    loader.exec_module(mod)
    return mod


FTK_HEAD = """Created By AccessData\u00ae FTK\u00ae Imager 4.7.1.2

Case Information:
Acquired using: ADI4.7.1.2
Case Number: 24-0001
Evidence Number: EX01
Unique Description: Laptop HDD
Examiner: Test
Notes:

--------------------------------------------------------------

Information for D:\\Cases\\24-0001\\EX01:

Physical Evidentiary Item (Source) Information:
[Device Info]
 Source Type: Physical
[Drive Geometry]
 Cylinders: 60,801
 Tracks per Cylinder: 255
 Sectors per Track: 63
 Bytes per Sector: 512
 Sector Count: 976,773,168
[Physical Drive Information]
 Drive Model: ST500LM012 HN-M500MBB
 Drive Serial Number: TEST-SERIAL-0001
 Drive Interface Type: USB
 Removable drive: False
 Source data size: 476940 MB
 Sector count:    976773168
[Computed Hashes]
 MD5 checksum:    0b7d0f1a2c3e4d5f6a7b8c9d0e1f2a3b
 SHA1 checksum:   1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b

Image Information:
 Acquisition started:   Mon Oct 19 09:12:44 2026
 Acquisition finished:  Mon Oct 19 13:40:02 2026
 Segment list:
  D:\\Cases\\24-0001\\EX01.E01
  D:\\Cases\\24-0001\\EX01.E02
"""

FTK_TAIL = """
Image Verification Results:
 Verification started:  Mon Oct 19 13:40:03 2026
 Verification finished: Mon Oct 19 15:01:17 2026
 MD5 checksum:    0b7d0f1a2c3e4d5f6a7b8c9d0e1f2a3b : verified
 SHA1 checksum:   1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b : verified
"""

FTK_ATTENTION = "\nATTENTION:\nThe following sector(s) on the source drive could not be read:\n"
FTK_CLOSE = "The contents of these sectors were replaced with zeros in the image.\n"

FTK_RANGES = [(0, 7), (2040, 2055), (140591432, 140591447), (140593480, 140593495),
              (140595528, 140595535), (140595536, 140595543), (300000000, 300000000),
              (300000002, 300000002), (300000003, 300000004), (400000000, 400000099),
              (500000000, 500009999), (600000000, 601999999), (976773167, 976773167)]
TOTAL = 976773168


def ftk_list(ranges):
    rows = "\n".join(f"\t{a} through {b}" if a != b else f"\t{a}" for a, b in ranges)
    return FTK_ATTENTION + rows + "\n" + FTK_CLOSE


def ftk_log(ranges=FTK_RANGES):
    return FTK_HEAD + (ftk_list(ranges) if ranges else "") + FTK_TAIL


TX1_HEAD = """Tableau TX1 Forensic Imager
Log File
Job ID: 31
Task: Disk to File
Status: Ok
User: Test
Case ID: 24-0001
Case Notes: Laptop HDD
Start time: Mon Oct 19 09:10:02 2026
End time: Mon Oct 19 13:02:44 2026

Source Disk:
  Interface: SATA
  Model: WDC WD5000AAKX-00ERMA0
  Serial number: TEST-SERIAL-0002
  Capacity in Bytes: 500,107,862,016 (500.1 GB)
  Block Size: 512 bytes
  Block Count: 976,773,168
  Power-ON Block Count: 976,773,168
  HPA Block Count: 976,773,168 (HPA In Use: No)
  DCO Block Count: 976,773,168 (DCO In Use: No)

Destination Disk:
  Interface: USB
  Model: Samsung T7
  Serial number: TEST-SERIAL-0003
  Capacity in Bytes: 1,000,204,886,016 (1.0 TB)
  Block Size: 512 bytes
  Block Count: 1,953,525,168

Settings:
  Error Granularity: Standard (32 KiB, 64 sectors)
  Error Retry: 3
  Hash: MD5, SHA-1

Results:
  Total recoverable errors: 3
  Total unrecoverable errors: 4
  MD5: 0b7d0f1a2c3e4d5f6a7b8c9d0e1f2a3b
  SHA-1: 1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b
"""
TX1_CLEAN_HEAD = TX1_HEAD.replace("Total recoverable errors: 3", "Total recoverable errors: 0") \
                         .replace("Total unrecoverable errors: 4", "Total unrecoverable errors: 0")
TX1_NO_COUNT_HEAD = TX1_HEAD.replace("  Total recoverable errors: 3\n  Total unrecoverable errors: 4\n", "")

TX1_REGIONS = [(0, 64), (2048, 64), (140591424, 64), (140591488, 64), (500000000, 1)]
TX1_RANGES = [(a, a + n - 1) for a, n in TX1_REGIONS]


def tx1_table(regions=TX1_REGIONS, head=TX1_HEAD):
    return head + "\nUnreadable Sectors:\n   Sector Address    Sector Length\n" + \
        "\n".join(f"   {a:<17} {n}" for a, n in regions) + "\n"


def line_of(text, needle):
    """1-based line number of the first line containing needle."""
    for no, line in enumerate(text.split("\n"), 1):
        if needle in line:
            return no
    raise ValueError(needle)
