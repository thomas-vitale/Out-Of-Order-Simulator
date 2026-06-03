"""
parser.py
=========

A small two-pass assembler front-end for DLX assembly.

Pass 1 collects label definitions and the linear list of instruction source
lines.  Pass 2 turns each source line into an :class:`Instruction`, resolving
branch/jump label operands to the program index of their target.

Supported syntax (case-insensitive mnemonics, ``R0``..``R31`` registers)::

    label:                  ; a label on its own line, or prefixing an instr
        ADD   R3, R1, R2     ; reg, reg, reg
        ADDI  R3, R1, #10    ; reg, reg, immediate   ('#' optional)
        LW    R5, 8(R6)      ; load   : dst, offset(base)
        SW    R7, 8(R8)      ; store  : data, offset(base)
        BEQZ  R1, loop       ; branch if R1 == 0
        BNEZ  R1, loop       ; branch if R1 != 0
        J     done           ; unconditional jump
        JAL   func           ; jump and link (writes R31)
        JR    R31            ; jump to address in register
        JALR  R31            ; jump-and-link to register address
        NOP

Comments start with ``;``, ``#`` (when not an immediate) or ``//`` and run to
end of line.
"""

from __future__ import annotations

import re
from typing import Optional

from instructions import Instruction, Opcode

# Map mnemonic text -> Opcode enum.
_MNEMONICS = {op.name: op for op in Opcode}


class ParseError(Exception):
    """Raised on malformed assembly, with a 1-based line number."""


def _strip_comment(line: str) -> str:
    """Remove trailing comments.  '#' is only a comment if not part of '#imm'."""
    # '//' and ';' always start a comment.
    for marker in (";", "//"):
        pos = line.find(marker)
        if pos != -1:
            line = line[:pos]
    # '#' starts a comment only when it is not immediately followed by a digit
    # or sign (i.e. not an immediate operand like '#-4').
    out = []
    i = 0
    while i < len(line):
        ch = line[i]
        if ch == "#":
            nxt = line[i + 1] if i + 1 < len(line) else ""
            if not (nxt.isdigit() or nxt in "+-"):
                break  # it's a comment
        out.append(ch)
        i += 1
    return "".join(out)


def _parse_reg(tok: str, lineno: int) -> int:
    """Parse 'R5' / 'r5' into the integer register number 5."""
    tok = tok.strip()
    m = re.fullmatch(r"[rR](\d{1,2})", tok)
    if not m:
        raise ParseError(f"line {lineno}: expected register, got '{tok}'")
    n = int(m.group(1))
    if not 0 <= n <= 31:
        raise ParseError(f"line {lineno}: register out of range '{tok}'")
    return n


def _parse_imm(tok: str, lineno: int) -> int:
    """Parse an immediate like '#10', '10', '-4', '0x1F'."""
    tok = tok.strip().lstrip("#")
    try:
        return int(tok, 0)  # base 0 -> auto-detect 0x / decimal / etc.
    except ValueError:
        raise ParseError(f"line {lineno}: bad immediate '{tok}'")


def _parse_mem(tok: str, lineno: int) -> tuple[int, int]:
    """Parse an 'offset(base)' memory operand -> (offset, base_reg)."""
    m = re.fullmatch(r"\s*(-?(?:0x)?[0-9A-Fa-f]+)?\s*\(\s*([rR]\d{1,2})\s*\)\s*", tok)
    if not m:
        raise ParseError(f"line {lineno}: bad memory operand '{tok}'")
    off = _parse_imm(m.group(1), lineno) if m.group(1) else 0
    base = _parse_reg(m.group(2), lineno)
    return off, base


def _split_operands(rest: str) -> list[str]:
    return [t.strip() for t in rest.split(",") if t.strip()]


def parse_program(text: str) -> list[Instruction]:
    """Parse assembly source text into a list of Instructions (program order)."""
    # ---- Pass 1: gather (lineno, label, mnemonic, operand-string) records,
    #              and record the program index each label points at. ----
    records: list[tuple[int, str, str]] = []  # (lineno, mnemonic, operands)
    labels: dict[str, int] = {}

    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = _strip_comment(raw).strip()
        if not line:
            continue

        # A leading 'label:' may sit alone or in front of an instruction.
        while ":" in line:
            label, _, after = line.partition(":")
            label = label.strip()
            if not re.fullmatch(r"[A-Za-z_]\w*", label):
                raise ParseError(f"line {lineno}: invalid label '{label}'")
            labels[label] = len(records)  # next instruction's program index
            line = after.strip()
            if not line:
                break
        if not line:
            continue

        parts = line.split(None, 1)
        mnemonic = parts[0].upper()
        operands = parts[1] if len(parts) > 1 else ""
        records.append((lineno, mnemonic, operands))

    # ---- Pass 2: build Instruction objects, resolving labels. ----
    program: list[Instruction] = []
    for idx, (lineno, mnemonic, operands) in enumerate(records):
        if mnemonic not in _MNEMONICS:
            raise ParseError(f"line {lineno}: unknown mnemonic '{mnemonic}'")
        op = _MNEMONICS[mnemonic]
        ops = _split_operands(operands)
        text_repr = f"{mnemonic} {operands}".strip()

        def resolve(label: str) -> int:
            if label not in labels:
                raise ParseError(f"line {lineno}: undefined label '{label}'")
            return labels[label]

        inst = Instruction(idx=idx, text=text_repr, op=op)

        if op in (Opcode.ADD, Opcode.SUB, Opcode.AND, Opcode.OR, Opcode.XOR,
                  Opcode.SLL, Opcode.SRL, Opcode.SLT, Opcode.MULT, Opcode.DIV):
            if len(ops) != 3:
                raise ParseError(f"line {lineno}: {mnemonic} needs Rd, Rs, Rt")
            inst.rd = _parse_reg(ops[0], lineno)
            inst.rs1 = _parse_reg(ops[1], lineno)
            inst.rs2 = _parse_reg(ops[2], lineno)

        elif op in (Opcode.ADDI, Opcode.SUBI, Opcode.ANDI, Opcode.ORI,
                    Opcode.XORI, Opcode.SLLI, Opcode.SRLI, Opcode.SLTI):
            if len(ops) != 3:
                raise ParseError(f"line {lineno}: {mnemonic} needs Rd, Rs, #imm")
            inst.rd = _parse_reg(ops[0], lineno)
            inst.rs1 = _parse_reg(ops[1], lineno)
            inst.imm = _parse_imm(ops[2], lineno)

        elif op == Opcode.LW:
            if len(ops) != 2:
                raise ParseError(f"line {lineno}: LW needs Rd, off(Rbase)")
            inst.rd = _parse_reg(ops[0], lineno)
            inst.imm, inst.rs1 = _parse_mem(ops[1], lineno)

        elif op == Opcode.SW:
            if len(ops) != 2:
                raise ParseError(f"line {lineno}: SW needs Rsrc, off(Rbase)")
            inst.rs2 = _parse_reg(ops[0], lineno)        # data to store
            inst.imm, inst.rs1 = _parse_mem(ops[1], lineno)  # base + offset

        elif op in (Opcode.BEQZ, Opcode.BNEZ):
            if len(ops) != 2:
                raise ParseError(f"line {lineno}: {mnemonic} needs Rs, label")
            inst.rs1 = _parse_reg(ops[0], lineno)
            inst.label_target = ops[1]
            inst.target_idx = resolve(ops[1])

        elif op == Opcode.J:
            if len(ops) != 1:
                raise ParseError(f"line {lineno}: J needs a label")
            inst.label_target = ops[0]
            inst.target_idx = resolve(ops[0])

        elif op == Opcode.JAL:
            if len(ops) != 1:
                raise ParseError(f"line {lineno}: JAL needs a label")
            inst.rd = 31  # link register
            inst.label_target = ops[0]
            inst.target_idx = resolve(ops[0])

        elif op == Opcode.JR:
            if len(ops) != 1:
                raise ParseError(f"line {lineno}: JR needs a register")
            inst.rs1 = _parse_reg(ops[0], lineno)

        elif op == Opcode.JALR:
            if len(ops) != 1:
                raise ParseError(f"line {lineno}: JALR needs a register")
            inst.rd = 31
            inst.rs1 = _parse_reg(ops[0], lineno)

        elif op == Opcode.NOP:
            pass

        program.append(inst)

    return program


def parse_file(path: str) -> list[Instruction]:
    with open(path, "r", encoding="utf-8") as fh:
        return parse_program(fh.read())
