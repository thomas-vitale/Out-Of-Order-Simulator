"""
main.py
=======

Entry point for the DLX out-of-order simulator.

Usage::

    python main.py [program.s] [--console] [--max N]

By default it launches the Tkinter GUI (step Next / Prev / Reset through the
cycles, watching the PC and every table change).  ``--console`` instead prints
the full machine state for every cycle to stdout (handy for diffing against the
RTL).  In both modes a golden in-order reference is run at the end and compared
against the simulator's committed state, so the model is self-checking.
"""

from __future__ import annotations

import argparse
import os
import sys

from instructions import Opcode
from parser import parse_file
from simulator import Simulator
from structures import Config


# ---------------------------------------------------------------------------
# Golden in-order reference: a trivial sequential interpreter used to verify
# that the out-of-order machine produces the architecturally correct result.
# ---------------------------------------------------------------------------
def golden_reference(program, cfg: Config):
    regs = [0] * cfg.n_arch
    for a, v in cfg.reg_init.items():
        regs[a] = v
    mem = dict(cfg.mem_init)
    pc = 0
    steps = 0
    while 0 <= pc < len(program) and steps < 1_000_000:
        steps += 1
        inst = program[pc]
        info = inst.info
        nxt = pc + 1
        a = regs[inst.rs1] if inst.rs1 is not None else 0
        if inst.is_load:
            regs[inst.rd] = mem.get((a + inst.imm) & 0xFFFFFFFF, 0)
        elif inst.is_store:
            mem[(a + inst.imm) & 0xFFFFFFFF] = regs[inst.rs2]
        elif inst.is_branch:
            taken = (a == 0) if inst.op == Opcode.BEQZ else (a != 0)
            if taken:
                nxt = inst.target_idx
        elif inst.op in (Opcode.J, Opcode.JAL):
            if inst.op == Opcode.JAL:
                regs[31] = (pc + 1) * 4
            nxt = inst.target_idx
        elif inst.op in (Opcode.JR, Opcode.JALR):
            if inst.op == Opcode.JALR:
                regs[31] = (pc + 1) * 4
            nxt = a // 4
        elif info.compute is not None:
            b = inst.imm if info.use_imm else (regs[inst.rs2] if inst.rs2 is not None else 0)
            if inst.rd is not None:
                regs[inst.rd] = info.compute(a, b)
        regs[0] = 0
        pc = nxt
    return regs, mem


def check_against_golden(sim: Simulator, program, cfg: Config) -> bool:
    gold_regs, gold_mem = golden_reference(program, cfg)
    ok = True
    for a in range(cfg.n_arch):
        if (gold_regs[a] & 0xFFFFFFFF) != (sim.arf.regs[a] & 0xFFFFFFFF):
            print(f"  MISMATCH R{a}: golden={gold_regs[a]} sim={sim.arf.regs[a]}")
            ok = False
    keys = set(gold_mem) | set(sim.mem.mem)
    for k in sorted(keys):
        g = gold_mem.get(k, 0) & 0xFFFFFFFF
        s = sim.mem.mem.get(k, 0) & 0xFFFFFFFF
        if g != s:
            print(f"  MISMATCH MEM[{k}]: golden={g} sim={s}")
            ok = False
    return ok


# ---------------------------------------------------------------------------
# Console renderer
# ---------------------------------------------------------------------------
def _fmt_list(items, empty="-"):
    return ", ".join(items) if items else empty


def print_console(snapshots, program):
    for s in snapshots:
        ev = s["events"]
        print("=" * 78)
        flush = "  *** FLUSH -> idx %s ***" % ev["redirect"] if ev["flush"] else ""
        print(f"CYCLE {s['cycle']:3d}   PC: {s['pc_start']} -> {s['pc_end']} "
              f"(byte 0x{s['pc_byte']:04x})   {'[HALTED]' if s['halted'] else ''}{flush}")
        print(f"  Fetch   : {_fmt_list(ev['fetched'])}")
        print(f"  Dispatch: {_fmt_list(ev['dispatched'])}")
        print(f"  Issue   : {_fmt_list(ev['issued'])}")
        print(f"  Execute : {_fmt_list(ev['executing'])}")
        print(f"  Writeback:{_fmt_list(ev['wrote_back'])}")
        print(f"  Commit  : {_fmt_list(ev['committed'])}")
        if ev["cdb"]:
            print("  CDB     : " + ", ".join(f"p{pd}={val} (#{seq})"
                                             for pd, val, seq in ev["cdb"]))
        for p in ev.get("pred", []):
            if p["kind"] == "br":
                print(f"  Predict : #{p['seq']} {p['text']} -> "
                      f"{'TAKEN' if p['taken'] else 'not-taken'} "
                      f"(BHT[{p['index']}]={p['ctr']:02b})")
            else:
                print(f"  Predict : #{p['seq']} {p['text']} -> jump TAKEN")
        for u in ev.get("bp_update", []):
            mp = "  <MISPREDICT>" if u["mispredict"] else ""
            print(f"  BHTtrain: #{u['seq']} BHT[{u['index']}] "
                  f"{u['old']:02b}->{u['new']:02b} "
                  f"({'taken' if u['taken'] else 'not-taken'}){mp}")
        # RAT: only show entries that differ from the identity/committed map
        rat_diffs = [f"R{a}->p{p}" for a, p in enumerate(s["rat"])
                     if p != s["arch_rat"][a]]
        print(f"  RAT(spec!=arch): {_fmt_list(rat_diffs)}")
        print(f"  FreeList: {_fmt_list(['p%d' % p for p in s['free_list']])}")
        if s["rob"]:
            print("  ROB:")
            for r in s["rob"]:
                mark = ">" if r["head"] else " "
                print(f"   {mark}[{r['rob']:02d}] #{r['seq']:<3d} {r['text']:<16s}"
                      f" dst={r['dest']:<4s} pd={r['pd']:<4s} old={r['told']:<4s}"
                      f" {'DONE' if r['done'] else '....'} {r['store']}{r['branch']}")
        for label, key in (("RS_Int", "rs_int"), ("RS_MulDiv", "rs_muldiv"),
                           ("RS_LoadBranch", "rs_loadbranch")):
            rows = s[key]
            if rows:
                cells = [f"#{r['seq']}({r['op']} s1={r['ps1']} s2={r['ps2']}"
                         + (f" imm={r['imm']}" if r.get('imm') is not None else "")
                         + f" pd={r['pd']}{' RDY' if r['ready'] else ''})"
                         for r in rows]
                print(f"  {label}: " + " | ".join(cells))
        busy = [f"{f['name']}[" + ",".join(f"#{sl['seq']}({sl['remaining']}"
                f"{'D' if sl['done'] else ''})" for sl in f["slots"]) + "]"
                for f in s["fus"] if f["slots"]]
        if busy:
            print("  FUs busy: " + "  ".join(busy))
    print("=" * 78)


def build_config(program) -> Config:
    # A couple of preset memory/register values so test.s has something to load.
    return Config(
        reg_init={1: 5, 2: 7, 4: 3, 6: 100, 8: 200, 9: 1},
        mem_init={200: 42},
    )


def main(argv=None):
    ap = argparse.ArgumentParser(description="DLX out-of-order simulator")
    default_prog = os.path.join(os.path.dirname(__file__), "test.s")
    ap.add_argument("program", nargs="?", default=default_prog,
                    help="DLX assembly file (default: test.s)")
    ap.add_argument("--console", action="store_true",
                    help="print every cycle to stdout instead of opening the GUI")
    ap.add_argument("--max", type=int, default=2000, help="max cycles")
    args = ap.parse_args(argv)

    program = parse_file(args.program)
    cfg = build_config(program)
    sim = Simulator(program, cfg)
    snapshots = sim.run(max_cycles=args.max)

    if args.console:
        print_console(snapshots, program)

    print(f"\nFinished in {sim.cycle} cycles "
          f"({'halted/drained' if sim.halted else 'hit max-cycles'}).")
    print("Committed architectural registers (non-zero):")
    for a in range(cfg.n_arch):
        if sim.arf.regs[a] != 0:
            print(f"  R{a} = {sim.arf.regs[a]}")
    if sim.mem.mem:
        print("Data memory (non-zero):")
        for addr in sorted(sim.mem.mem):
            print(f"  M[{addr}] = {sim.mem.mem[addr]}")

    print("\nGolden-model check: ", end="")
    print("PASS" if check_against_golden(sim, program, cfg) else "FAIL")

    if not args.console:
        try:
            from gui import launch_gui
        except Exception as exc:  # pragma: no cover - GUI optional
            print(f"\n(Could not start GUI: {exc}\n Run with --console instead.)")
            return
        launch_gui(snapshots, program, sim, cfg)


if __name__ == "__main__":
    main()
