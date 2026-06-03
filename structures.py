"""
structures.py
=============

All the *passive* hardware structures of the DLX out-of-order core, plus the
dynamic-instruction record (:class:`DynInst`) that flows through them.

Design highlights (matching the team architecture document):

* **Explicit register renaming, R10000 / P6 style.** The *Physical Register
  File* (PRF) holds the actual data.  Reservation stations and the ROB carry
  *physical register tags*, never operand values.

* **Two RATs for precise recovery.**  ``RAT`` is the *speculative* map updated at
  rename; ``arch_rat`` is the *committed* (architectural) map updated at retire.
  On a branch flush we simply copy ``arch_rat`` back into ``RAT`` and rebuild the
  free list -- a one-shot, CAM-free recovery.

* **Per-entry V1/V2 valid bits (Tomasulo-style wakeup).**  Every reservation
  station entry stores a *valid* bit per source operand.  At dispatch the bits
  are initialised from the global ``ReadyTable`` (was the producer already
  done?); thereafter a CDB writeback *broadcasts* its destination tag and every
  RS entry whose source tag matches sets the corresponding valid bit.  An entry
  becomes selectable once all its used valid bits are set.  (The ``ReadyTable``
  is retained for dispatch-time initialisation, flush rebuild, and the GUI.)

* **Stores live in the ROB.**  A store allocates no physical register; its
  address/data are written into its ROB entry at execute and the memory write
  happens only at commit (so speculative stores never corrupt memory).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Optional

from instructions import Instruction, Opcode, FUClass, LINK_REG, w32


# ===========================================================================
# Configuration
# ===========================================================================
@dataclass
class Config:
    """Tunable microarchitecture parameters (all configurable from main.py)."""
    n_phys: int = 64           # physical registers in the PRF
    n_arch: int = 32           # architectural registers (DLX GPRs)
    rob_size: int = 32         # reorder-buffer entries
    rs_int: int = 8            # RS_Int entries
    rs_muldiv: int = 4         # RS_MulDiv entries
    rs_loadstore: int = 4      # RS_LoadStore entries
    rs_branch: int = 4         # RS_Branch entries
    width: int = 2             # superscalar width (fetch/dispatch/commit per cyc)
    cdb_ports: int = 2         # common-data-bus broadcast ports per cycle
    n_alu: int = 2             # ALU units behind RS_Int
    # functional-unit latencies (cycles spent in EX before the result is ready)
    lat_alu: int = 1
    lat_mul: int = 3           # MUL is pipelined (throughput 1/cycle)
    lat_div: int = 8           # DIV is NOT pipelined (occupies its unit)
    lat_load: int = 2          # address calc + data-memory access
    lat_store: int = 1         # address + data gather (memory write is at commit)
    lat_branch: int = 1        # condition + target resolution
    n_bht: int = 16            # branch history table entries (2-bit counters)
    mem_init: dict = field(default_factory=dict)   # initial data-memory contents
    reg_init: dict = field(default_factory=dict)   # initial architectural regs


# ===========================================================================
# Dynamic instruction: a single in-flight *instance* of a static Instruction.
# A new DynInst is created at fetch; after a flush, re-fetched instructions get
# fresh DynInsts (and fresh sequence numbers) so identity is never ambiguous.
# ===========================================================================
@dataclass
class DynInst:
    seq: int                       # unique, monotonically increasing dynamic id
    inst: Instruction              # the static instruction it instantiates

    # ---- renaming results (filled at dispatch) ----
    ps1: Optional[int] = None      # physical tag of source 1 (None if unused)
    ps2: Optional[int] = None      # physical tag of source 2 (None if imm/unused)
    pd: Optional[int] = None       # physical tag of destination (None if no dst)
    told: Optional[int] = None     # previous mapping of dst (freed at commit)
    rob_idx: Optional[int] = None  # index of its ROB entry

    # ---- branch prediction carried from fetch ----
    pred_taken: Optional[bool] = None
    pred_target: Optional[int] = None  # predicted next program index

    # ---- operand values (read from PRF at issue) and result (from EX) ----
    val1: int = 0
    val2: int = 0
    result: Optional[int] = None
    addr: Optional[int] = None     # effective address for loads/stores
    taken: Optional[bool] = None   # resolved branch outcome
    mispredict: bool = False
    redirect_idx: Optional[int] = None  # correct next program index

    # ---- bookkeeping for the GUI pipeline view ----
    stage: str = "fetch"           # last stage entered this run

    @property
    def op(self) -> Opcode:
        return self.inst.op

    @property
    def fu(self) -> FUClass:
        return self.inst.fu

    def short(self) -> str:
        return f"#{self.seq}:{self.inst.text}"


# ===========================================================================
# Register files
# ===========================================================================
class PRF:
    """Physical Register File -- holds the actual (speculative) data values."""
    def __init__(self, n: int):
        self.regs = [0] * n

    def read(self, p: int) -> int:
        return self.regs[p]

    def write(self, p: int, value: int) -> None:
        self.regs[p] = w32(value)


class ARF:
    """Architectural Register File -- committed values, written at retire."""
    def __init__(self, n: int):
        self.regs = [0] * n

    def read(self, a: int) -> int:
        return self.regs[a]

    def write(self, a: int, value: int) -> None:
        if a != 0:                 # R0 is hardwired to zero
            self.regs[a] = w32(value)


# ===========================================================================
# Register Alias Table + Free List + Ready Table
# ===========================================================================
class RAT:
    """Maps architectural register -> physical register (one map = one RAT)."""
    def __init__(self, mapping: list[int]):
        self.map = list(mapping)

    def lookup(self, a: int) -> int:
        return self.map[a]

    def set(self, a: int, p: int) -> None:
        self.map[a] = p


class FreeList:
    """FIFO of physical registers currently available for allocation."""
    def __init__(self, free: list[int]):
        self.q: deque[int] = deque(free)

    def empty(self) -> bool:
        return len(self.q) == 0

    def pop(self) -> int:
        return self.q.popleft()

    def push(self, p: int) -> None:
        self.q.append(p)

    def as_list(self) -> list[int]:
        return list(self.q)


class ReadyTable:
    """One bit per physical register: 1 == value available in the PRF.

    This replaces Tomasulo's associative tag match.  Writeback *sets* the bit
    (indexed write); rename *clears* it; the reservation station *reads* the
    bits of its source tags at select time (indexed read).  No CAM.
    """
    def __init__(self, n: int):
        self.ready = [True] * n   # at reset every phys reg is considered valid

    def is_ready(self, p: int) -> bool:
        return self.ready[p]

    def set(self, p: int) -> None:
        self.ready[p] = True

    def clear(self, p: int) -> None:
        self.ready[p] = False


# ===========================================================================
# Reorder Buffer
# ===========================================================================
@dataclass
class ROBEntry:
    seq: int
    dyn: DynInst
    arch_dest: Optional[int]   # architectural destination (None if none)
    pd: Optional[int]          # new physical dest tag
    told: Optional[int]        # old physical dest tag (freed at commit)
    writes_reg: bool
    completed: bool = False
    value: Optional[int] = None
    # memory
    is_store: bool = False
    st_addr: Optional[int] = None
    st_data: Optional[int] = None
    # control
    is_branch: bool = False
    is_jump: bool = False
    pred_taken: Optional[bool] = None
    actual_taken: Optional[bool] = None
    mispredict: bool = False
    redirect_idx: Optional[int] = None


class ROB:
    """Circular FIFO that enforces in-order allocation and in-order commit.

    The RTL exposes four maintained pointers (see the ROB field diagram):

    * ``oldest``  -- the head, the next entry eligible to commit.
    * ``newest``  -- the tail, the next free slot to allocate into.
    * ``non_speculative_last`` -- index of the youngest entry that is *not*
      shadowed by an older unresolved branch.  Everything from ``oldest`` up to
      and including this pointer is non-speculative; younger entries are
      speculative.  Loads may only execute inside the non-speculative region.
    * ``newest_store`` -- index of the youngest store still in the ROB, used by
      loads for conservative ordering (a load waits for older stores).
    """
    def __init__(self, size: int):
        self.size = size
        self.buf: list[Optional[ROBEntry]] = [None] * size
        self.head = 0     # oldest (next to commit)
        self.tail = 0     # next free slot (newest)
        self.count = 0

    def full(self) -> bool:
        return self.count == self.size

    def empty(self) -> bool:
        return self.count == 0

    def alloc(self, entry: ROBEntry) -> int:
        assert not self.full()
        idx = self.tail
        self.buf[idx] = entry
        self.tail = (self.tail + 1) % self.size
        self.count += 1
        return idx

    def head_entry(self) -> Optional[ROBEntry]:
        return self.buf[self.head] if self.count else None

    def pop_head(self) -> ROBEntry:
        assert self.count
        e = self.buf[self.head]
        self.buf[self.head] = None
        self.head = (self.head + 1) % self.size
        self.count -= 1
        return e

    def entry_at(self, idx: int) -> Optional[ROBEntry]:
        return self.buf[idx]

    def in_order(self) -> list[ROBEntry]:
        """All live entries from oldest to youngest (for display / scanning)."""
        out = []
        for i in range(self.count):
            out.append(self.buf[(self.head + i) % self.size])
        return out

    # ---- maintained pointers ------------------------------------------------
    def oldest_idx(self) -> Optional[int]:
        return self.head if self.count else None

    def newest_idx(self) -> Optional[int]:
        """Index of the youngest live entry (one before the tail)."""
        if not self.count:
            return None
        return (self.tail - 1) % self.size

    def non_speculative_last_idx(self) -> Optional[int]:
        """Youngest entry not shadowed by an older *unresolved* branch/jump.

        We walk oldest->youngest and stop just before the first control
        instruction that has not completed yet (its outcome is still unknown,
        so everything after it is speculative)."""
        last = None
        for i in range(self.count):
            idx = (self.head + i) % self.size
            e = self.buf[idx]
            if (e.is_branch or e.is_jump) and not e.completed:
                break          # this branch is unresolved -> stop the run
            last = idx
        return last

    def newest_store_idx(self) -> Optional[int]:
        """Index of the youngest store currently in the ROB (None if none)."""
        found = None
        for i in range(self.count):
            idx = (self.head + i) % self.size
            if self.buf[idx].is_store:
                found = idx
        return found

    def clear(self) -> None:
        self.buf = [None] * self.size
        self.head = self.tail = self.count = 0


# ===========================================================================
# Reservation stations (4 split stations, tags only -- no operand data)
# ===========================================================================
@dataclass
class RSEntry:
    """One reservation-station slot.

    Per the RTL field diagrams the slot carries, besides the physical source
    tags (held on the referenced :class:`DynInst`), a *valid* bit per source
    operand: ``v1`` for source 1 and ``v2`` for source 2.  A bit is set when the
    operand value is available in the PRF.  Unused sources keep their valid bit
    ``True`` so they never gate selection.  The single-source Branch RS uses
    ``v1`` as its lone ``valid`` bit (``v2`` stays ``True``).
    """
    busy: bool = False
    dyn: Optional[DynInst] = None
    issued: bool = False       # already sent to a functional unit?
    v1: bool = True            # source-1 valid bit
    v2: bool = True            # source-2 valid bit

    def ready(self) -> bool:
        """Selectable iff both operand valid bits are set and not yet issued."""
        return self.busy and not self.issued and self.v1 and self.v2

    def clear(self) -> None:
        self.busy = False
        self.dyn = None
        self.issued = False
        self.v1 = True
        self.v2 = True


class ReservationStation:
    """A fixed-size pool of RS entries dedicated to one FU class.

    Entries store only physical tags (via the referenced DynInst) plus the
    per-source valid bits.  ``kind`` identifies the station ("int", "muldiv",
    "loadstore", "branch") and selects the field layout shown in the GUI/console.
    """
    def __init__(self, name: str, size: int, kind: str):
        self.name = name
        self.kind = kind
        self.entries = [RSEntry() for _ in range(size)]

    def free_slot(self) -> Optional[RSEntry]:
        for e in self.entries:
            if not e.busy:
                return e
        return None

    def full(self) -> bool:
        return all(e.busy for e in self.entries)

    def insert(self, dyn: DynInst, v1: bool = True, v2: bool = True) -> None:
        slot = self.free_slot()
        assert slot is not None
        slot.busy = True
        slot.dyn = dyn
        slot.issued = False
        slot.v1 = v1
        slot.v2 = v2

    def wake(self, tag: int) -> None:
        """CDB broadcast: set the valid bit of every entry sourcing ``tag``."""
        for e in self.entries:
            if not e.busy:
                continue
            if e.dyn.ps1 == tag:
                e.v1 = True
            if e.dyn.ps2 == tag:
                e.v2 = True

    def remove(self, dyn: DynInst) -> None:
        for e in self.entries:
            if e.dyn is dyn:
                e.clear()
                return

    def live(self) -> list[RSEntry]:
        return [e for e in self.entries if e.busy]

    def clear(self) -> None:
        for e in self.entries:
            e.clear()


# ===========================================================================
# Functional units
# ===========================================================================
@dataclass
class FUSlot:
    """One in-flight operation inside a functional unit."""
    dyn: DynInst
    remaining: int             # cycles left in EX
    done: bool = False         # result computed, waiting for a CDB port


class FunctionalUnit:
    """A timed execution resource.

    * Non-pipelined units (ALU, DIV, LU, BU) hold at most one operation and
      cannot accept a new one until the current result has been broadcast.
    * Pipelined units (MUL) accept a new operation every cycle and hold several
      in-flight operations, each with its own countdown.
    """
    def __init__(self, name: str, kind: str, latency: int, pipelined: bool):
        self.name = name
        self.kind = kind          # 'ALU' | 'MUL' | 'DIV' | 'LU' | 'BU'
        self.latency = latency
        self.pipelined = pipelined
        self.slots: list[FUSlot] = []

    def can_accept(self) -> bool:
        if self.pipelined:
            # A pipelined unit can accept one new op per cycle as long as no
            # other op is *entering* this same cycle (latency == fresh slot).
            return not any(s.remaining == self.latency for s in self.slots)
        return len(self.slots) == 0

    def accept(self, dyn: DynInst) -> None:
        self.slots.append(FUSlot(dyn=dyn, remaining=self.latency))

    def tick(self) -> None:
        """Advance one execution cycle for every in-flight operation."""
        for s in self.slots:
            if not s.done:
                s.remaining -= 1
                if s.remaining <= 0:
                    s.done = True

    def done_slots(self) -> list[FUSlot]:
        return [s for s in self.slots if s.done]

    def release(self, slot: FUSlot) -> None:
        self.slots.remove(slot)

    def clear(self) -> None:
        self.slots = []


# ===========================================================================
# Data memory (word-addressed model) and branch predictor
# ===========================================================================
class DataMemory:
    """Simple word memory.  Stores write here ONLY at commit time."""
    def __init__(self, init: Optional[dict] = None):
        self.mem: dict[int, int] = dict(init or {})

    def load(self, addr: int) -> int:
        return self.mem.get(addr, 0)

    def store(self, addr: int, value: int) -> None:
        self.mem[addr] = w32(value)


class BranchPredictor:
    """Branch History Table of 2-bit saturating counters + a tiny BTB.

    The BHT is a fixed-size, direct-mapped table indexed by the low bits of the
    branch's program index (its "PC").  Each entry is a 2-bit saturating
    counter::

        0  Strongly Not-Taken     2  Weakly Taken
        1  Weakly Not-Taken       3  Strongly Taken

    Prediction is *taken* when the counter is >= 2.  On resolution the counter
    is incremented (saturating at 3) if the branch was taken, decremented
    (saturating at 0) if not.  Counters reset to 1 (Weakly Not-Taken), so a
    branch that turns out taken on its first sighting is mispredicted -- which is
    exactly what demonstrates the flush in test.s.

    The BTB caches the resolved target program index of taken control transfers;
    it is only really needed for register-indirect jumps (JR/JALR) whose target
    is not known at fetch.  Conditional branches and J/JAL carry a static target
    from decode.
    """
    # human-readable name for each 2-bit state
    STATE = {0: "Strong NT", 1: "Weak NT", 2: "Weak T", 3: "Strong T"}

    def __init__(self, n_entries: int = 16):
        self.size = n_entries
        self.bht: list[int] = [1] * n_entries  # 2-bit counters, weakly-NT reset
        self.btb: dict[int, int] = {}          # program idx -> target program idx

    def index(self, pc_idx: int) -> int:
        """Direct-mapped BHT index from the branch's program index (PC)."""
        return pc_idx % self.size

    def counter(self, pc_idx: int) -> int:
        return self.bht[self.index(pc_idx)]

    def predict(self, inst: Instruction) -> tuple[bool, int]:
        """Return (predicted_taken, predicted_next_program_index)."""
        if inst.is_jump:
            # J/JAL have a static target; JR/JALR target is data-dependent and
            # unknown at fetch, so consult the BTB (fall through if unseen).
            if inst.target_idx is not None:
                return True, inst.target_idx
            return True, self.btb.get(inst.idx, inst.idx + 1)
        if inst.is_branch:
            taken = self.bht[self.index(inst.idx)] >= 2
            return (taken, inst.target_idx if taken else inst.idx + 1)
        return False, inst.idx + 1

    def update(self, inst: Instruction, taken: bool, target_idx: int) -> None:
        """Train the 2-bit counter (and BTB) when a branch/jump retires."""
        if inst.is_branch:
            i = self.index(inst.idx)
            self.bht[i] = min(3, self.bht[i] + 1) if taken else max(0, self.bht[i] - 1)
        if taken:
            self.btb[inst.idx] = target_idx
