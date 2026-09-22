#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rename_diffpair_nets.py — 将 *.kicad_sch 中全局网络标签的 AMD FPGA 风格网名
规整为 KiCad 可识别“前缀一致”的差分对网名：

    IO_L3P_T0L_N4_AD15P_66_P  ->  IO_L3_66_P
    IO_L3N_T0L_N5_AD15N_66_N  ->  IO_L3_66_N      （两者前缀同为 IO_L3_66）

改名规则（仅当网名以 _P/_N 结尾且以 IO_L 开头时生效）：
    保留 = IO + L<差分对位置编号> + 时钟输入标识(GC/QBC/DBC/HDGC) + bank号 + 原 _P/_N
    丢弃 = T* tile 段(T0L/T1U...)、N* 编号段、AD* 辅助段
    其余一律不动：单端 IO(不以 _P/_N 结尾)、GTY_* 等非 IO 差分对、总线/电源等。

安全机制：
    1. 正则只锚定 (global_label "...")，绝不触碰符号 pin name (name "...")、
       (pin "...")、(label "...")、(hierarchical_label "...")、(text ...)。
    2. 默认 dry-run，只打印报告；显式加 --apply 才写盘。
    3. 任一名字解析失败 / 改名冲突 -> 整体中止，一个文件都不写（全有或全无）。
    4. 幂等：重复运行不会二次修改（第二轮全部识别为“已是目标形式”）。
    5. 二进制读写，原样保留 CRLF/BOM/UTF-8；临时文件 + 原子替换落盘。
    6. 写后重新读回校验（标签总数一致、无残留待改名），失败自动回滚。

用法：
    python rename_diffpair_nets.py                  # dry-run，打印完整报告（不改文件）
    python rename_diffpair_nets.py --selftest       # 只跑内置用例（不读写任何原理图）
    python rename_diffpair_nets.py --apply          # 实际写入
    python rename_diffpair_nets.py --apply --backup # 写入前生成 <文件名>.bak
    可选：--verbose 逐处打印 file:line old -> new
          --dir DIR  指定扫描目录（默认：脚本所在目录）
"""

import argparse
import re
import shutil
import sys
from bisect import bisect_right
from pathlib import Path

# ========================= 可审查的配置区 =========================
# 保留的“时钟输入标识”。依据 AMD UG572 (UltraScale Clocking)：
#   GC   = Global Clock（全局时钟输入）
#   QBC / DBC = Byte-lane clock（字节通道时钟输入，专用时钟输入脚）
#   HDGC = HD bank 的 Global Clock（HD bank 全局时钟输入）
# 这些段在差分对的 P/N 两侧取值相同，保留后前缀才能对齐。
KEEP_CLOCK_TOKENS = ("GC", "QBC", "DBC", "HDGC")

# 丢弃的中间段：T<tile><方向> / N<编号> / AD<编号><极性>
DROP_TOKEN_RE = re.compile(r"T\d+[UL]|N\d+|AD\d+[PN]")

# 只锚定全局标签节点；名字 = 紧随其后的第一个字符串
GLOBAL_LABEL_RE = re.compile(r'\(global_label\s+"([^"]*)"')

# IO_L 后的一段：L<编号>（可带原 P/N 极性字母）
PAIR_SEG_RE = re.compile(r"L(\d+)([PN])?")
# ==============================================================

# 结果状态
CHANGED = "changed"    # 需要改名
ALREADY = "already"    # 已是目标形式（幂等跳过）
SINGLE = "single"      # IO_* 但不以 _P/_N 结尾 -> 单端，不改
NON_IO = "non_io"      # 以 _P/_N 结尾但非 IO_L 开头（如 GTY_0_P），不改
OTHER = "other"        # 其它网络名，不改
ERROR = "error"        # 无法按规则处理 -> 中止


def transform(name):
    """按规则计算一个网名的新名字。返回 (new_name, status, reason)。"""
    segs = name.split("_")

    # ---- 规则 0：后缀不含 _P/_N => 单端 IO / 其它网络，永不修改 ----
    if not segs or segs[-1] not in ("P", "N"):
        if name.startswith("IO_"):
            return name, SINGLE, "IO 网络但不以 _P/_N 结尾（单端）"
        return name, OTHER, ""

    # ---- 规则 1：只处理 IO_L 开头的差分对；GTY_* 等保持不变 ----
    if not name.startswith("IO_L") or len(segs) < 2:
        return name, NON_IO, "非 IO_L 差分对，前缀本身已一致"

    m = PAIR_SEG_RE.fullmatch(segs[1])
    if m is None:
        return name, ERROR, "IO_L 后的段不是 L<编号>[P/N]：%r" % segs[1]
    num, side = m.group(1), m.group(2)

    # ---- 幂等：L 后已无 P/N 极性字母 => 上一轮已改好，跳过 ----
    if side is None:
        return name, ALREADY, ""

    if len(segs) < 4:
        return name, ERROR, "段数不足（缺 bank 或后缀）：%r" % name
    bank = segs[-2]
    if not bank.isdigit():
        return name, ERROR, "倒数第二段不是纯数字 bank 号：%r" % bank

    keep = []
    for tok in segs[2:-2]:                 # 中间段（去掉 IO、L*、bank、P/N）
        if tok in KEEP_CLOCK_TOKENS:
            if tok not in keep:
                keep.append(tok)           # 时钟输入标识：保留（按原顺序、去重）
        elif DROP_TOKEN_RE.fullmatch(tok):
            continue                       # T*/N*/AD*：丢弃
        else:
            # 未知中间段：宁可中止也不猜（当前标签数据中不存在此类段）
            return name, ERROR, "未知中间段 %r，请先确认是否应保留" % tok

    new = "_".join(["IO", "L" + num] + keep + [bank, segs[-1]])
    return new, (CHANGED if new != name else ALREADY), ""


# 内置用例：(输入, 期望状态, 期望输出)
SELFTEST_CASES = [
    ("IO_L3P_T0L_N4_AD15P_66_P",    CHANGED, "IO_L3_66_P"),          # 用户给的示例
    ("IO_L3N_T0L_N5_AD15N_66_N",    CHANGED, "IO_L3_66_N"),          # 同对的 N 侧
    ("IO_L13P_T2L_N0_GC_QBC_66_P",  CHANGED, "IO_L13_GC_QBC_66_P"),  # 双时钟标识
    ("IO_L13N_T2L_N1_GC_QBC_66_N",  CHANGED, "IO_L13_GC_QBC_66_N"),
    ("IO_L11N_T1U_N9_GC_66_N",      CHANGED, "IO_L11_GC_66_N"),      # 单 GC
    ("IO_L16P_T2U_N6_QBC_AD3P_66_P", CHANGED, "IO_L16_QBC_66_P"),    # QBC
    ("IO_L19P_T3L_N0_DBC_AD9P_64_P", CHANGED, "IO_L19_DBC_64_P"),    # DBC
    ("IO_L1P_T0L_N0_DBC_66_P",      CHANGED, "IO_L1_DBC_66_P"),      # DBC 无 AD 段
    ("IO_L6N_HDGC_AD6N_84_N",       CHANGED, "IO_L6_HDGC_84_N"),     # HD bank
    ("IO_L12P_AD0P_84_P",           CHANGED, "IO_L12_84_P"),         # 无时钟标识
    ("IO_L24N_T3U_N11_64_N",        CHANGED, "IO_L24_64_N"),         # 无 AD 段
    ("IO_L8N_HDGC_87_N",            CHANGED, "IO_L8_HDGC_87_N"),     # HD 无 AD 段
    ("IO_T3U_N12_66",               SINGLE,  "IO_T3U_N12_66"),       # 单端：不改
    ("GTY_0_P",                     NON_IO,  "GTY_0_P"),             # 非 IO：不改
    ("D4_ADDR[0..16]",              OTHER,   "D4_ADDR[0..16]"),      # 总线：不改
    ("IO_L3_66_P",                  ALREADY, "IO_L3_66_P"),          # 幂等：已是目标形式
    ("IO_L3P_A14_D30_65_P",         ERROR,   "IO_L3P_A14_D30_65_P"), # 未知段 -> 中止
]


def run_selftest():
    """只运行上述用例，不读写任何原理图文件。"""
    print("运行内置测试用例（不读写任何原理图文件）……")
    failed = 0
    for name, want_status, want_new in SELFTEST_CASES:
        new, status, _ = transform(name)
        ok = (status == want_status) and (new == want_new)
        if not ok:
            failed += 1
        print("  [%s] %-34s -> %-26s status=%s%s" % (
            "PASS" if ok else "FAIL", name, new, status,
            "" if ok else "  期望 status=%s new=%s" % (want_status, want_new)))
    if failed:
        print("结果：失败 %d / %d 条" % (failed, len(SELFTEST_CASES)))
        return 1
    print("结果：全部通过（%d 条）" % len(SELFTEST_CASES))
    return 0


def scan_file(path):
    """只读扫描一个 .kicad_sch：分类其中每个全局标签，不做任何写入。"""
    raw = path.read_bytes()
    text = raw.decode("utf-8")                      # BOM 会作为 U+FEFF 原样保留
    newline_pos = [m.start() for m in re.finditer("\n", text)]

    def line_of(pos):
        return bisect_right(newline_pos, pos) + 1

    r = {
        "path": path,
        "raw": raw,                                 # 原始字节，供回滚使用
        "text": text,
        "total": 0,                                 # 全局标签总数
        "changes": [],                              # (行号, 旧名, 新名)
        "errors": [],                               # (行号, 名字, 原因)
        "names": {},                                # 旧名 -> 最终名
        "counts": {CHANGED: 0, ALREADY: 0, SINGLE: 0, NON_IO: 0,
                   OTHER: 0, ERROR: 0},
        "single_names": set(),
        "non_io_names": set(),
        "other_names": set(),
    }
    for m in GLOBAL_LABEL_RE.finditer(text):
        r["total"] += 1
        name = m.group(1)
        new, status, reason = transform(name)
        r["counts"][status] += 1
        if status == CHANGED:
            r["changes"].append((line_of(m.start()), name, new))
            r["names"][name] = new
        else:
            r["names"].setdefault(name, name)
            if status == SINGLE:
                r["single_names"].add(name)
            elif status == NON_IO:
                r["non_io_names"].add(name)
            elif status == OTHER:
                r["other_names"].add(name)
            elif status == ERROR:
                r["errors"].append((line_of(m.start()), name, reason))
    return r


def make_replacer(m):
    """sub 回调：只替换状态为 CHANGED 的全局标签名，其余原样返回。"""
    new, status, _ = transform(m.group(1))
    if status == CHANGED:
        return '(global_label "%s"' % new
    return m.group(0)


def main():
    ap = argparse.ArgumentParser(
        description="规整 KiCad 全局标签中的差分对网名（默认 dry-run）")
    ap.add_argument("--apply", action="store_true", help="实际写入（默认只报告）")
    ap.add_argument("--backup", action="store_true",
                    help="写入前生成 <文件名>.bak 备份")
    ap.add_argument("--verbose", action="store_true",
                    help="逐处打印 file:line 变更")
    ap.add_argument("--dir", default=None,
                    help="扫描目录（默认：脚本所在目录）")
    ap.add_argument("--selftest", action="store_true",
                    help="仅运行内置测试用例，不读写原理图")
    args = ap.parse_args()

    if args.selftest:
        return run_selftest()

    root = Path(args.dir).resolve() if args.dir \
        else Path(__file__).resolve().parent
    files = sorted(root.glob("*.kicad_sch"))
    if not files:
        print("错误：在 %s 下未找到 *.kicad_sch" % root)
        return 1

    print("=" * 74)
    print("保留的时钟输入标识 : %s" % ", ".join(KEEP_CLOCK_TOKENS))
    print("丢弃的中间段       : T*tile段 / N*编号段 / AD*辅助段（未知段->中止）")
    print("只修改的对象       : (global_label \"...\") —— pin name 等一律不动")
    print("扫描目录           : %s" % root)
    print("扫描文件           : %d 个 *.kicad_sch" % len(files))
    print("=" * 74)

    results = [scan_file(f) for f in files]

    # ---- 检查 1：任何名字处理不了 -> 全体中止，一个文件都不写 ----
    errors = [(r, e) for r in results for e in r["errors"]]
    if errors:
        print("\n!! 发现 %d 个无法处理的网名，已中止（未写入任何文件）：" % len(errors))
        for r, (line, name, reason) in errors:
            print("   %s:%d  %s  --  %s" % (r["path"].name, line, name, reason))
        return 1

    # ---- 检查 2：全局改名表冲突（两个不同旧名变成同名）----
    final_map = {}
    for r in results:
        final_map.update(r["names"])
    invert, collisions = {}, []
    for old in sorted(final_map):
        final = final_map[old]
        if final in invert and invert[final] != old:
            collisions.append((invert[final], old, final))
        invert.setdefault(final, old)
    if collisions:
        print("\n!! 改名冲突，已中止（未写入任何文件）：")
        for a, b, final in collisions:
            print("   %r 与 %r 会变成同名 %r" % (a, b, final))
        return 1

    changed_files = [r for r in results if r["changes"]]
    total_changes = sum(len(r["changes"]) for r in results)
    distinct = {}
    for r in results:
        for _, old, new in r["changes"]:
            distinct[old] = new

    # ---- 报告 ----
    print("\n--- 含变更的文件（%d 个）---" % len(changed_files))
    for r in changed_files:
        print("  %-22s %3d 处变更 / 共 %d 个全局标签"
              % (r["path"].name, len(r["changes"]), r["total"]))
    print("  合计：%d 处，%d 个不同网名" % (total_changes, len(distinct)))

    print("\n--- 改名映射（distinct，%d 条）---" % len(distinct))
    for old in sorted(distinct):
        print("  %s\n      -> %s" % (old, distinct[old]))

    single = sorted({n for r in results for n in r["single_names"]})
    non_io = sorted({n for r in results for n in r["non_io_names"]})
    other = sorted({n for r in results for n in r["other_names"]})
    already = sum(r["counts"][ALREADY] for r in results)

    print("\n--- 保持不变 ---")
    print("  单端 IO（不以 _P/_N 结尾）%d 个：" % len(single))
    for n in single:
        print("    " + n)
    print("  非 IO 差分对（前缀已一致）%d 个：" % len(non_io))
    for n in non_io:
        print("    " + n)
    print("  其它网络名 %d 个（总线/电源/JTAG 等）：" % len(other))
    for n in other:
        print("    " + n)
    print("  已是目标形式（幂等跳过）：%d 处" % already)

    if args.verbose:
        print("\n--- 逐处变更（verbose）---")
        for r in results:
            for line, old, new in r["changes"]:
                print("  %s:%d  %s -> %s" % (r["path"].name, line, old, new))

    # ---- dry-run 到此结束 ----
    if not args.apply:
        print("\n[DRY-RUN] 未修改任何文件。确认报告无误后运行：")
        print("    python rename_diffpair_nets.py --apply --backup")
        return 0

    # ---- 写入：原子替换 + 写后校验 + 失败回滚 ----
    print("\n[APPLY] 写入中……")
    backups, written = [], []
    try:
        for r in changed_files:
            new_text = GLOBAL_LABEL_RE.sub(make_replacer, r["text"])
            # 结构自检：全局标签数量必须与改名前完全一致
            if len(GLOBAL_LABEL_RE.findall(new_text)) != r["total"]:
                raise RuntimeError("全局标签数量变化: %s" % r["path"].name)
            new_raw = new_text.encode("utf-8")
            if args.backup:
                bak = r["path"].with_name(r["path"].name + ".bak")
                shutil.copy2(r["path"], bak)
                backups.append(bak)
            tmp = r["path"].with_name(r["path"].name + ".tmp")
            tmp.write_bytes(new_raw)
            tmp.replace(r["path"])                 # 同目录原子替换
            written.append(r)
    except Exception as exc:
        print("!! 写入异常：%s" % exc)
        for r in written:                          # 回滚已写的文件
            r["path"].write_bytes(r["raw"])
        for r in changed_files:                    # 清理残留临时文件
            t = r["path"].with_name(r["path"].name + ".tmp")
            if t.exists():
                t.unlink()
        print("已回滚 %d 个文件。" % len(written))
        return 1

    # 写后校验：重新读盘，必须“标签总数一致 + 无残留待改名”
    bad = []
    for r in written:
        again = scan_file(r["path"])
        if (again["errors"] or again["counts"][CHANGED]
                or again["total"] != r["total"]):
            bad.append(r)
    if bad:
        print("!! 写后校验失败，整体回滚：")
        for r in written:
            r["path"].write_bytes(r["raw"])
        for r in bad:
            print("   " + r["path"].name)
        return 1

    print("[APPLY] 完成：%d 个文件；写后校验通过（全局标签总数一致、"
          "无残留待改名）。" % len(written))
    if backups:
        print("备份：%s" % ", ".join(b.name for b in backups))
    return 0


if __name__ == "__main__":
    sys.exit(main())
