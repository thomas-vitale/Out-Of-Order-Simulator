# DLX Out-of-Order Simulator

A **cycle-accurate Python golden model** of a DLX out-of-order processor
(R10000 / P6-style explicit register renaming). It is the reference model for a
SystemVerilog RTL implementation built for the *Microelectronic Systems* course
at Politecnico di Torino (group **Group4**).

The simulator runs a DLX assembly program to completion, recording one immutable
snapshot of the full machine state per cycle. A Tkinter GUI then lets you step
**forward / backward** through those snapshots to watch the PC and every internal
table change cycle by cycle. A trivial in-order interpreter cross-checks the
final architectural state, so the model is self-verifying.

---

## Features

- **Explicit register renaming** — the Physical Register File (PRF) holds data;
  the reservation stations and ROB hold **physical tags only**.
- **2-way superscalar** fetch / dispatch / commit.
- **Per-entry V1/V2 valid bits** (Tomasulo-style wakeup, matching the RTL RS
  field layout): each reservation-station slot stores a valid bit per source;
  initialised at dispatch, set by CDB tag-match broadcast, read at select. A
  global Ready Table backs the dispatch-time initialisation and flush rebuild.
- **Reorder Buffer (ROB)** with precise, in-order commit; the old destination
  physical register (`Told`) is freed at commit. Maintains the RTL pointers
  `oldest / newest / non_speculative_last / newest_store`.
- **4 split reservation stations**, one per functional-unit cluster, each with
  the RTL field layout:
  - `RS_Int` → `ADD/SUB/AND/OR/XOR/SLL/SRL/SLT` (+ imm) → ALUs
  - `RS_MulDiv` → `MULT` (pipelined), `DIV` (non-pipelined)
  - `RS_LoadStore` → `LW`/`SW` → Load/Store unit
  - `RS_Branch` → `BEQZ/BNEZ`, `J/JR/JAL/JALR` → Branch unit
- **Common Data Bus** with 2 ports and fixed-priority arbitration
  (DIV > MUL > LU > BU > ALU); losers are held in the FU and retried next cycle.
- **Precise memory model** — stores write memory **only at commit**; loads are
  **conservative** (a load waits until every older store has committed; no
  associative disambiguation).
- **Branch prediction** — a fixed-size **BHT of 2-bit saturating counters** plus a
  small BTB for indirect targets. Branch recovery happens at commit: on a
  mispredict reaching the ROB head, everything younger is flushed and the RAT is
  rebuilt from the committed `arch_rat`.
- **Tkinter GUI** with Prev / Next / Reset / End controls (and arrow keys), all
  tables resizable, per-cycle changes highlighted, and physical-register values
  shown.

---

## ISA (DLX, R0–R31, R0 hardwired to 0)

| Group | Instructions |
|---|---|
| Reg-reg ALU | `ADD SUB AND OR XOR SLL SRL SLT` |
| Reg-imm ALU | `ADDI SUBI ANDI ORI XORI SLLI SRLI SLTI` |
| Mul / Div | `MULT DIV` |
| Memory | `LW Rd,off(Rb)` · `SW Rs,off(Rb)` |
| Control | `BEQZ/BNEZ Rs,label` · `J/JAL label` · `JR/JALR Rs` |
| Misc | `NOP` |

All values are wrapped to signed 32-bit (`w32`).

---

## Requirements

- **Python 3.10+** (uses `from __future__ import annotations` and modern typing).
- **Tkinter** for the GUI — bundled with the standard CPython installer on
  Windows/macOS; on Debian/Ubuntu install `python3-tk`. The `--console` mode needs
  no GUI.

No third-party packages are required.

---

## How to run

```bash
cd "DLX Project/sim/ooo"

python main.py test.s            # Tkinter GUI: step cycles, watch PC + all tables
python main.py test.s --console  # full per-cycle text dump (good for diffing RTL)
python main.py myprog.s --max 5000
```

Initial state for `test.s` (set in `main.build_config()`):
`R1=5 R2=7 R4=3 R6=100 R8=200 R9=1`, `M[200]=42`.

### GUI controls

| Action | Control |
|---|---|
| Next cycle | `Next ▶` / `→` |
| Previous cycle | `◀ Prev` / `←` |
| Jump to start | `Reset` |
| Jump to end | `End` |

Panels: RAT (spec + committed), Free List, BHT/predictor log, ROB (with the
`oldest/newest/non_spec_last/newest_store` pointer markers), **PRF values**, the
four reservation stations (each with its RTL field layout — `S1/V1/S2/V2/Imm/D`
for Int/MulDiv/LoadStore, `reg/V/off/use_reg/cond` for Branch), functional units,
CDB, and a pipeline-stage view. Cells that changed since the previous cycle are
highlighted.

---

## File layout

| File | Role |
|---|---|
| `instructions.py` | `Opcode`/`FUClass` enums, `Instruction`, opcode→(RS, semantics, flags) table, `w32` |
| `parser.py` | two-pass DLX assembler (labels, comments `;`/`#`/`//`, `off(base)`) |
| `structures.py` | `Config`, `DynInst`, PRF/ARF/RAT/FreeList/ReadyTable/ROB (+pointers), 4 RS (V1/V2 valid bits), FU model, `DataMemory`, `BranchPredictor` |
| `simulator.py` | the pipeline + per-cycle `CycleSnapshot`; flush/recovery |
| `gui.py` | Tkinter snapshot viewer |
| `main.py` | entry point (GUI default, `--console`), golden in-order self-check |
| `test.s` | demo program: RAW, WAW, WAR, mispredicted `BEQZ`, `LW`/`SW` |

The pipeline stages run in **reverse order** each cycle
(commit → writeback → execute → issue → dispatch → fetch) so that resources freed
this cycle (ROB/RS/FU slots, free-list entries) are visible to earlier stages and
producer→consumer wakeup is back-to-back.

---

## Configuration

Defaults live in `structures.Config` and can be overridden when constructing a
`Config` (see `main.build_config`):

| Parameter | Default | Meaning |
|---|---|---|
| `n_phys` | 64 | physical registers |
| `n_arch` | 32 | architectural registers |
| `rob_size` | 32 | ROB entries |
| `rs_int` / `rs_muldiv` / `rs_loadstore` / `rs_branch` | 8 / 4 / 4 / 4 | RS sizes |
| `width` | 2 | superscalar width |
| `cdb_ports` | 2 | CDB write ports |
| `n_alu` | 2 | ALU units |
| `lat_alu/mul/div/load/store/branch` | 1 / 3 / 8 / 2 / 1 / 1 | FU latencies |
| `n_bht` | 16 | BHT entries (direct-mapped) |
| `reg_init` / `mem_init` | — | initial register / memory state |

---

## Verification

`python main.py test.s --console` ends with **`Golden-model check: PASS`** and
drains in **18 cycles**. The bundled `test.s` exercises:

- **RAW** — consumer stalls until the producer's CDB broadcast.
- **WAW / WAR** — renaming gives distinct physical registers, so no false stall.
- **Mispredicted `BEQZ`** — flush + RAT recovery at commit; wrong-path
  instructions never commit.
- **`LW`/`SW`** — store writes `M[204]=42` only at commit; the load executes only
  after older stores have committed.

Final committed registers:
`R2=99 R3=12 R5=9 R6=10 R7=35 R10=42 R13=1 R14=21`; `M[200]=42 M[204]=42`.

`golden_reference()` in `main.py` is a trivial in-order interpreter; the
simulator's committed ARF + data memory are asserted equal to it on every run.

---

## Why this exists

The team is implementing an R10000/P6-style out-of-order DLX core in
SystemVerilog, targeting a **standard-cell library with no RAM/CAM macros** —
everything must be flip-flops + muxes + pointers. This Python model validates the
microarchitecture and acts as the golden reference (expected cycle behavior +
final architectural state) for the RTL bring-up. Use its cycle traces and final
state as the reference oracle when comparing against RTL waveforms.
