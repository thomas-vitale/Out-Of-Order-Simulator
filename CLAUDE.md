# DLX Out-of-Order Simulator — Project Context

A **cycle-accurate Python golden model** of a DLX out-of-order processor. It is a
reference model for a SystemVerilog RTL implementation being built for a
Microelectronic Systems course (Politecnico di Torino, group "Group4").

## Big picture / why this exists

The team is implementing an **R10000/P6-style out-of-order DLX core in
SystemVerilog**. The full architecture is documented in
`../../dlx_ooo_architecture_clean (1).md` (Italian). This Python simulator
exists to validate the microarchitecture and act as a *golden reference*
(expected cycle behavior + final architectural state) before/while writing RTL.

The RTL target has a hard constraint: **standard-cell library with no RAM/CAM
macros** — everything must be flip-flops + mux + pointers. That is why wakeup
uses a **Ready Table** (indexed bit array) instead of associative tag matching.

## Fixed architecture decisions (from the arch doc)

- Explicit register renaming, **PRF holds data**, RS/ROB hold **physical tags only**.
- **2-way superscalar** (fetch/dispatch/commit width = 2).
- PRF = 64, ROB = 32, **CDB = 2 ports**.
- **Ready Table** wakeup (no CAM): writeback sets the bit, rename clears it, RS
  indexes it at select.
- **Stores live in the ROB**; memory is written only at **commit** (precise).
- **Conservative loads**: a load may execute only once all older stores have
  committed (no associative address disambiguation).
- **Branch recovery at commit**: on a mispredicted branch reaching the ROB head,
  flush everything younger and rebuild the RAT.

## Deviations / resolved open questions

- **3 split reservation stations** (per the user's request, *overriding* the
  doc's unified 16-entry pool):
  - `RS_LoadBranch` → loads, stores, branches, jumps
  - `RS_Int` → ADD/SUB/AND/OR/XOR/SLL/SRL/SLT (+ immediate forms)
  - `RS_MulDiv` → MULT (pipelined), DIV (non-pipelined)
- **Recovery (doc §8.2)**: implemented with **two RATs** — speculative `RAT` and
  committed `arch_rat`. On flush: `RAT = arch_rat`, rebuild free list as "all
  phys regs not referenced by `arch_rat`", reset Ready Table to committed regs.
  (Cleaner than the doc's ARF-copy options; behaviorally equivalent.)
- **CDB contention (doc §8.7)**: 2 ports, fixed priority DIV>MUL>LU>BU>ALU,
  losers held in the FU and retried next cycle. **Stores/branches/plain jumps
  complete via the ROB and do NOT consume a CDB port** (only register-writing
  results do — JAL/JALR do, since they write the link reg R31).
- **Branch prediction**: a fixed-size **BHT of 2-bit saturating counters**
  (`Config.n_bht`, default 16), direct-mapped by `pc_idx % n_bht`. States
  0=Strong-NT,1=Weak-NT,2=Weak-T,3=Strong-T; predict taken when ≥2; reset to 1
  (Weak-NT). A small BTB caches targets (only needed for JR/JALR). The BHT and
  its per-cycle read/train activity are shown in a dedicated GUI panel and in
  the `--console` dump (`Predict`/`BHTtrain` lines).
- **Wakeup timing**: stages run in **reverse pipeline order** each cycle
  (commit→writeback→execute→issue→dispatch→fetch), so writeback and select in
  the same cycle give back-to-back producer→consumer issue.
- **Latencies (configurable in `structures.Config`)**: ALU 1, MUL 3 (pipelined),
  DIV 8 (non-pipelined), load 2, store 1, branch 1. ALUs ×2, MUL ×1, DIV ×1,
  LU ×1, BU ×1.

## ISA (DLX, R0–R31, R0 hardwired 0)

`ADD/SUB/AND/OR/XOR/SLL/SRL/SLT` (reg-reg), `ADDI/SUBI/ANDI/ORI/XORI/SLLI/SRLI/SLTI`
(reg-imm), `MULT/DIV`, `LW Rd,off(Rb)`, `SW Rs,off(Rb)`, `BEQZ/BNEZ Rs,label`,
`J/JAL label`, `JR/JALR Rs`, `NOP`. All values wrapped to signed 32-bit (`w32`).

## File layout (this folder, `sim/ooo/`)

| File | Role |
|---|---|
| `instructions.py` | `Opcode`/`FUClass` enums, `Instruction`, opcode→(RS, semantics, flags) table, `w32` |
| `parser.py` | two-pass DLX assembler (labels, comments `;`/`#`/`//`, `off(base)`) |
| `structures.py` | `Config`, `DynInst`, PRF/ARF/RAT/FreeList/ReadyTable/ROB, 3 RS, FU model, DataMemory, BranchPredictor |
| `simulator.py` | the pipeline + per-cycle `CycleSnapshot` (dict); flush/recovery |
| `gui.py` | Tkinter snapshot viewer (Prev/Next/Reset/End + arrow keys) |
| `main.py` | entry point (GUI default, `--console`), golden in-order self-check |
| `test.s` | demo program: RAW, WAW, WAR, mispredicted BEQZ, LW/SW |

The simulator runs to completion first, recording one immutable snapshot per
cycle; the GUI just navigates that list (so step-back is exact).

## How to run

```
cd "<...>/DLX Project/sim/ooo"
python main.py test.s            # Tkinter GUI: step cycles, watch PC + all tables
python main.py test.s --console  # full per-cycle text dump
python main.py myprog.s --max 5000
```

Initial state for `test.s` is set in `main.build_config()`:
`R1=5 R2=7 R4=3 R6=100 R8=200 R9=1`, `M[200]=42`.

## Verification status (PASSING)

`python main.py test.s --console` ends with **`Golden-model check: PASS`**.
Drains in 18 cycles. Confirmed: RAW stall (consumer waits for producer's CDB
broadcast), WAW/WAR get distinct phys regs (no false stall), `BEQZ R0` mispredict
flushes at commit (~cycle 13, redirect→idx 11, wrong-path instrs never commit),
`SW` writes `M[204]=42` only at commit. Final committed regs:
`R3=12 R5=9 R6=10 R7=35 R2=99 R10=42 R13=1 R14=21`.

The `golden_reference()` in `main.py` is a trivial in-order interpreter; the sim's
committed ARF + memory are asserted equal to it. Keep this invariant when editing.

## Conventions / gotchas

- Windows console is cp1252 — **don't print non-ASCII to stdout** (the GUI may use
  Unicode, that's fine; console code uses ASCII like `+`/`.`/`->`).
- A `DynInst` is one dynamic instance (fresh `seq` per fetch); after a flush,
  re-fetched instructions get new `DynInst`s.
- Writes to R0 are discarded at rename (no phys reg allocated).
- ROB entries are keyed by `seq`; loads scan the ROB for older pending stores.
- **Memory ordering**: stores write `DataMemory` *only at commit* (`_commit`);
  speculative stores never touch memory. Loads are conservative — `_issue` blocks
  a load while `_older_store_pending(seq)` is true (any `is_store` with smaller
  `seq` still in the ROB), so a load only executes once every older store has
  committed. No address-matching CAM.
- **RS hold the immediate too**: each reservation-station entry carries the
  instruction's immediate (offset for `LW/SW`, literal for ALU-imm). Surfaced as
  `imm` in `_rs_rows`, the GUI **"Imm"** column, and the `--console` RS dump
  (`imm=<n>`). `None`/`-` for ops that don't use an immediate.

## Likely next steps (not yet done)

- More test programs (loops via `BEQZ`/`J`, JAL/JR call/return, DIV/MUL contention).
- Optional: 2 LU ports / dual-issue loads; multi-cycle memory; same-cycle vs
  registered wakeup toggle; per-instruction stage timing table export to compare
  against RTL waveforms.
- The end goal remains the SystemVerilog RTL — use this model's cycle traces and
  final state as the reference oracle.
