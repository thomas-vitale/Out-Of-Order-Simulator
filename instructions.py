"""
instructions.py
===============

ISA definition for the DLX out-of-order golden model.

This module is *purely data*: it describes what each opcode is, which of the
three reservation stations it belongs to, how many architectural source
registers it reads, whether it writes a register, and the functional semantics
used to compute its result.  The pipeline timing lives elsewhere
(``simulator.py``); here we only model *what* an instruction computes, not
*when*.

Four reservation-station classes (one per functional-unit cluster, matching the
RTL field diagrams):

  * ``FUClass.INT``        -> RS_Int       : ADD/SUB/AND/OR/XOR/shift/SLT (+imm)
  * ``FUClass.MULDIV``     -> RS_MulDiv    : MULT, DIV
  * ``FUClass.LOADSTORE``  -> RS_LoadStore : LW, SW
  * ``FUClass.BRANCH``     -> RS_Branch    : BEQZ/BNEZ, J/JR/JAL/JALR

Register convention: DLX has 32 architectural GPRs (R0..R31).  R0 is hardwired
to zero -- writes to it are discarded and it never consumes a physical register
(handled in the rename stage, see simulator.py).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto
from typing import Callable, Optional


# ---------------------------------------------------------------------------
# 32-bit two's-complement helper.  Internally we keep Python ints but every
# arithmetic result is wrapped to a signed 32-bit value so the model matches
# what the RTL datapath would produce.
# ---------------------------------------------------------------------------
def w32(x: int) -> int:
    """Wrap an integer into the signed 32-bit range, like a hardware ALU."""
    x &= 0xFFFFFFFF
    return x - 0x100000000 if x & 0x80000000 else x


class FUClass(Enum):
    """Which reservation station / functional-unit cluster an op uses."""
    INT = auto()        # RS_Int       -> ALU units
    MULDIV = auto()     # RS_MulDiv    -> MUL (pipelined) + DIV (non-pipelined)
    LOADSTORE = auto()  # RS_LoadStore -> Load/Store unit (LU)
    BRANCH = auto()     # RS_Branch    -> Branch unit (BU)


class Opcode(Enum):
    # --- integer register-register (RS_Int) -------------------------------
    ADD = auto()
    SUB = auto()
    AND = auto()
    OR = auto()
    XOR = auto()
    SLL = auto()
    SRL = auto()
    SLT = auto()
    # --- integer register-immediate (RS_Int) -----------------------------
    ADDI = auto()
    SUBI = auto()
    ANDI = auto()
    ORI = auto()
    XORI = auto()
    SLLI = auto()
    SRLI = auto()
    SLTI = auto()
    # --- multiply / divide (RS_MulDiv) ------------------------------------
    MULT = auto()
    DIV = auto()
    # --- memory (RS_LoadStore) --------------------------------------------
    LW = auto()
    SW = auto()
    # --- control flow (RS_Branch) -----------------------------------------
    BEQZ = auto()
    BNEZ = auto()
    J = auto()
    JR = auto()
    JAL = auto()
    JALR = auto()
    # --- misc -------------------------------------------------------------
    NOP = auto()


@dataclass(frozen=True)
class OpInfo:
    """Static metadata for one opcode."""
    fu: FUClass
    writes_reg: bool          # does it produce an architectural result?
    n_src: int                # number of architectural source registers read
    use_imm: bool = False     # second operand is an immediate, not a register
    is_load: bool = False
    is_store: bool = False
    is_branch: bool = False   # conditional branch
    is_jump: bool = False     # unconditional control transfer
    # compute(a, b) -> result, where a = value(src1), b = value(src2)/imm.
    # For non-ALU ops (loads, stores, control) the function is None and the
    # behaviour is handled specially by the simulator.
    compute: Optional[Callable[[int, int], int]] = None


# Functional semantics for the ALU-style opcodes.  ``a`` is the first source
# value; ``b`` is either the second source value or the (sign-extended)
# immediate, depending on ``use_imm``.
_ALU = {
    Opcode.ADD:  lambda a, b: w32(a + b),
    Opcode.SUB:  lambda a, b: w32(a - b),
    Opcode.AND:  lambda a, b: w32(a & b),
    Opcode.OR:   lambda a, b: w32(a | b),
    Opcode.XOR:  lambda a, b: w32(a ^ b),
    Opcode.SLL:  lambda a, b: w32(a << (b & 31)),
    Opcode.SRL:  lambda a, b: w32((a & 0xFFFFFFFF) >> (b & 31)),
    Opcode.SLT:  lambda a, b: 1 if a < b else 0,
    Opcode.ADDI: lambda a, b: w32(a + b),
    Opcode.SUBI: lambda a, b: w32(a - b),
    Opcode.ANDI: lambda a, b: w32(a & b),
    Opcode.ORI:  lambda a, b: w32(a | b),
    Opcode.XORI: lambda a, b: w32(a ^ b),
    Opcode.SLLI: lambda a, b: w32(a << (b & 31)),
    Opcode.SRLI: lambda a, b: w32((a & 0xFFFFFFFF) >> (b & 31)),
    Opcode.SLTI: lambda a, b: 1 if a < b else 0,
    Opcode.MULT: lambda a, b: w32(a * b),
    Opcode.DIV:  lambda a, b: w32(int(a / b)) if b != 0 else 0,  # trunc toward 0
}


# The master opcode table.  Everything the rest of the simulator needs to know
# about an opcode is looked up here.
OPINFO: dict[Opcode, OpInfo] = {
    # RS_Int, register-register: read 2 regs, write 1
    Opcode.ADD: OpInfo(FUClass.INT, True, 2, compute=_ALU[Opcode.ADD]),
    Opcode.SUB: OpInfo(FUClass.INT, True, 2, compute=_ALU[Opcode.SUB]),
    Opcode.AND: OpInfo(FUClass.INT, True, 2, compute=_ALU[Opcode.AND]),
    Opcode.OR:  OpInfo(FUClass.INT, True, 2, compute=_ALU[Opcode.OR]),
    Opcode.XOR: OpInfo(FUClass.INT, True, 2, compute=_ALU[Opcode.XOR]),
    Opcode.SLL: OpInfo(FUClass.INT, True, 2, compute=_ALU[Opcode.SLL]),
    Opcode.SRL: OpInfo(FUClass.INT, True, 2, compute=_ALU[Opcode.SRL]),
    Opcode.SLT: OpInfo(FUClass.INT, True, 2, compute=_ALU[Opcode.SLT]),
    # RS_Int, register-immediate: read 1 reg, write 1
    Opcode.ADDI: OpInfo(FUClass.INT, True, 1, use_imm=True, compute=_ALU[Opcode.ADDI]),
    Opcode.SUBI: OpInfo(FUClass.INT, True, 1, use_imm=True, compute=_ALU[Opcode.SUBI]),
    Opcode.ANDI: OpInfo(FUClass.INT, True, 1, use_imm=True, compute=_ALU[Opcode.ANDI]),
    Opcode.ORI:  OpInfo(FUClass.INT, True, 1, use_imm=True, compute=_ALU[Opcode.ORI]),
    Opcode.XORI: OpInfo(FUClass.INT, True, 1, use_imm=True, compute=_ALU[Opcode.XORI]),
    Opcode.SLLI: OpInfo(FUClass.INT, True, 1, use_imm=True, compute=_ALU[Opcode.SLLI]),
    Opcode.SRLI: OpInfo(FUClass.INT, True, 1, use_imm=True, compute=_ALU[Opcode.SRLI]),
    Opcode.SLTI: OpInfo(FUClass.INT, True, 1, use_imm=True, compute=_ALU[Opcode.SLTI]),
    # RS_MulDiv
    Opcode.MULT: OpInfo(FUClass.MULDIV, True, 2, compute=_ALU[Opcode.MULT]),
    Opcode.DIV:  OpInfo(FUClass.MULDIV, True, 2, compute=_ALU[Opcode.DIV]),
    # RS_LoadStore -- memory.  LW writes a reg; SW does not.
    # For both, src1 = base register, imm = offset.  For SW src2 = data reg.
    Opcode.LW: OpInfo(FUClass.LOADSTORE, True, 1, use_imm=True, is_load=True),
    Opcode.SW: OpInfo(FUClass.LOADSTORE, False, 2, use_imm=True, is_store=True),
    # RS_Branch -- control.  Conditional branches read 1 reg, write none.
    Opcode.BEQZ: OpInfo(FUClass.BRANCH, False, 1, is_branch=True),
    Opcode.BNEZ: OpInfo(FUClass.BRANCH, False, 1, is_branch=True),
    # Unconditional jumps.  J/JR write no reg; JAL/JALR write the link reg R31.
    Opcode.J:    OpInfo(FUClass.BRANCH, False, 0, is_jump=True),
    Opcode.JR:   OpInfo(FUClass.BRANCH, False, 1, is_jump=True),
    Opcode.JAL:  OpInfo(FUClass.BRANCH, True, 0, is_jump=True),
    Opcode.JALR: OpInfo(FUClass.BRANCH, True, 1, is_jump=True),
    # No-op
    Opcode.NOP:  OpInfo(FUClass.INT, False, 0),
}

LINK_REG = 31  # R31 holds the return address for JAL/JALR


@dataclass
class Instruction:
    """One *static* instruction from the program (program order = ``idx``)."""
    idx: int                       # static program index (0-based)
    text: str                      # original source line (for display)
    op: Opcode
    rd: Optional[int] = None       # architectural destination register
    rs1: Optional[int] = None      # architectural source register 1
    rs2: Optional[int] = None      # architectural source register 2 (or store data)
    imm: int = 0                   # sign-extended immediate / offset
    target_idx: Optional[int] = None  # resolved branch/jump target (program index)
    label_target: Optional[str] = None  # original label name, for display

    # --- convenience views onto the opcode metadata -----------------------
    @property
    def info(self) -> OpInfo:
        return OPINFO[self.op]

    @property
    def fu(self) -> FUClass:
        return self.info.fu

    @property
    def writes_reg(self) -> bool:
        return self.info.writes_reg

    @property
    def use_imm(self) -> bool:
        return self.info.use_imm

    @property
    def is_load(self) -> bool:
        return self.info.is_load

    @property
    def is_store(self) -> bool:
        return self.info.is_store

    @property
    def is_branch(self) -> bool:
        return self.info.is_branch

    @property
    def is_jump(self) -> bool:
        return self.info.is_jump

    @property
    def is_control(self) -> bool:
        return self.info.is_branch or self.info.is_jump

    @property
    def pc(self) -> int:
        """Byte address of this instruction (DLX instructions are 4 bytes)."""
        return self.idx * 4

    def __str__(self) -> str:
        return f"[{self.idx:02d}] {self.text}"
