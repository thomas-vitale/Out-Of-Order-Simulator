"""
simulator.py
============

The cycle-accurate out-of-order pipeline.

Pipeline stages (the classic R10000 / P6 flow)::

    FETCH -> DISPATCH(rename) -> [RS wait] -> ISSUE -> EXECUTE -> WRITEBACK(CDB) -> COMMIT

Each clock the stages are processed in **reverse pipeline order**
(commit -> writeback -> execute -> issue -> dispatch -> fetch).  Walking
backwards is the standard trick that makes a single-threaded model behave like
real pipelined hardware: an instruction can advance at most one stage per cycle,
and a slot freed by an older stage only becomes visible to a younger stage on
the *next* cycle.

Out-of-order mechanics implemented here:

* **Renaming** removes WAR and WAW hazards.  At dispatch each destination gets a
  brand-new physical register from the free list, so two writes to the same
  architectural register (WAW) get different tags, and a later writer never
  clobbers a register an older reader still needs (WAR).

* **RAW** hazards are the only *real* dependences left.  They are enforced by the
  Ready Table: a consumer cannot be selected until the producer's writeback has
  set the ready bit of the producing physical register.

* **Tag broadcast on the CDB.**  When a functional unit finishes, writeback puts
  ``{pd, value}`` on the Common Data Bus: it writes ``PRF[pd]``, sets
  ``ReadyTable[pd]``, and marks the matching ROB entry completed.  Waiting
  reservation stations "wake up" by re-reading the Ready Table next cycle.

* **In-order retirement & precise state.**  The ROB commits from its head in
  program order; only then does an architectural register (ARF + committed RAT)
  or memory get updated, and the *old* physical register is recycled to the free
  list.  A mispredicted branch flushes everything younger at commit and restores
  the speculative RAT from the committed one.
"""

from __future__ import annotations

from collections import deque
from typing import Optional

from instructions import Instruction, Opcode, FUClass, w32
from structures import (
    Config, DynInst, PRF, ARF, RAT, FreeList, ReadyTable, ROB, ROBEntry,
    ReservationStation, FunctionalUnit, DataMemory, BranchPredictor,
)


# Priority order for CDB arbitration when more results are ready than ports.
# Slow / scarce units win to avoid back-pressure stalling the pipeline.
_CDB_PRIORITY = {"DIV": 0, "MUL": 1, "LU": 2, "BU": 3, "ALU": 4}


class Simulator:
    def __init__(self, program: list[Instruction], config: Optional[Config] = None):
        self.program = program
        self.cfg = config or Config()
        c = self.cfg

        # ---- register state ----------------------------------------------
        # Identity rename at reset: arch reg i -> phys reg i.  Physical regs
        # [0, n_arch) therefore hold the architectural state; [n_arch, n_phys)
        # start free.
        self.prf = PRF(c.n_phys)
        self.arf = ARF(c.n_arch)
        for a, v in c.reg_init.items():
            if a != 0:
                self.prf.write(a, v)
                self.arf.write(a, v)
        self.rat = RAT(list(range(c.n_arch)))         # speculative map
        self.arch_rat = RAT(list(range(c.n_arch)))    # committed map
        self.free_list = FreeList(list(range(c.n_arch, c.n_phys)))
        self.ready = ReadyTable(c.n_phys)             # all ready at reset

        # ---- queues / buffers --------------------------------------------
        self.rob = ROB(c.rob_size)
        self.rs_int = ReservationStation("RS_Int", c.rs_int, "int")
        self.rs_muldiv = ReservationStation("RS_MulDiv", c.rs_muldiv, "muldiv")
        self.rs_loadstore = ReservationStation("RS_LoadStore", c.rs_loadstore,
                                               "loadstore")
        self.rs_branch = ReservationStation("RS_Branch", c.rs_branch, "branch")
        self.all_rs = [self.rs_int, self.rs_muldiv, self.rs_loadstore,
                       self.rs_branch]

        # ---- functional units --------------------------------------------
        self.alus = [FunctionalUnit(f"ALU{i}", "ALU", c.lat_alu, pipelined=False)
                     for i in range(c.n_alu)]
        self.mul = FunctionalUnit("MUL", "MUL", c.lat_mul, pipelined=True)
        self.div = FunctionalUnit("DIV", "DIV", c.lat_div, pipelined=False)
        self.lu = FunctionalUnit("LU", "LU", c.lat_load, pipelined=False)
        self.bu = FunctionalUnit("BU", "BU", c.lat_branch, pipelined=False)
        self.all_fus = [*self.alus, self.mul, self.div, self.lu, self.bu]

        # ---- memory / prediction -----------------------------------------
        self.mem = DataMemory(c.mem_init)
        self.bp = BranchPredictor(c.n_bht)

        # ---- control state -----------------------------------------------
        self.pc = 0                       # program index of next fetch
        self.fetch_q: deque[DynInst] = deque()  # decoded, awaiting dispatch
        self.seq_ctr = 0                  # dynamic-instruction id generator
        self.cycle = 0
        self.halted = False

        # per-cycle event log (for the GUI pipeline view); reset each cycle
        self._ev: dict = {}
        self.snapshots: list[dict] = []

    # =====================================================================
    # Top-level run loop
    # =====================================================================
    def run(self, max_cycles: int = 2000) -> list[dict]:
        """Simulate to completion, recording one snapshot per cycle."""
        # cycle 0 = the reset state, before any work
        self.snapshots.append(self._snapshot(reset=True))
        while not self.halted and self.cycle < max_cycles:
            self.step()
            self.snapshots.append(self._snapshot())
        return self.snapshots

    def step(self) -> None:
        """Advance the machine by one clock, processing stages in reverse."""
        self.cycle += 1
        self._ev = {k: [] for k in
                    ("fetched", "dispatched", "issued", "executing",
                     "wrote_back", "committed", "pred", "bp_update")}
        self._ev["flush"] = False
        self._ev["redirect"] = None
        self._ev["pc_start"] = self.pc

        self._commit()
        self._writeback()
        self._execute()
        self._issue()
        self._dispatch()
        self._fetch()

        self._ev["pc_end"] = self.pc
        self._check_halt()

    # =====================================================================
    # Stage 6: COMMIT (in-order retire from the ROB head)
    # =====================================================================
    def _commit(self) -> None:
        for _ in range(self.cfg.width):
            head = self.rob.head_entry()
            if head is None or not head.completed:
                break  # in-order: stall until the oldest instruction is done
            self.rob.pop_head()
            dyn = head.dyn

            # Architectural register update: copy PRF->ARF, advance committed
            # RAT, and recycle the *old* physical register.
            if head.writes_reg and head.arch_dest is not None:
                value = self.prf.read(head.pd)
                self.arf.write(head.arch_dest, value)
                self.arch_rat.set(head.arch_dest, head.pd)
                if head.told is not None and head.told != head.pd:
                    self.free_list.push(head.told)

            # Stores write memory ONLY now -- speculative stores never commit
            # to memory, guaranteeing precise memory state.
            if head.is_store:
                self.mem.store(head.st_addr, head.st_data)

            self._ev["committed"].append(dyn.short())

            # Branch / jump: update the predictor and, on a misprediction,
            # flush everything younger and redirect the front-end.
            if head.is_branch or head.is_jump:
                tgt = (head.redirect_idx if head.redirect_idx is not None
                       else dyn.inst.idx + 1)
                # capture the 2-bit counter before/after training (for the GUI)
                old_ctr = self.bp.counter(dyn.inst.idx) if head.is_branch else None
                self.bp.update(dyn.inst, bool(head.actual_taken), tgt)
                if head.is_branch:
                    self._ev["bp_update"].append({
                        "seq": dyn.seq,
                        "text": dyn.inst.text,
                        "index": self.bp.index(dyn.inst.idx),
                        "old": old_ctr,
                        "new": self.bp.counter(dyn.inst.idx),
                        "taken": bool(head.actual_taken),
                        "mispredict": head.mispredict,
                    })
                if head.mispredict:
                    self._flush(head.redirect_idx)
                    break  # nothing past the flushing branch may commit

    # =====================================================================
    # Flush + precise recovery (R10000 two-RAT scheme)
    # =====================================================================
    def _flush(self, redirect_idx: int) -> None:
        """Squash all in-flight work younger than the just-committed branch."""
        self.rob.clear()
        self.fetch_q.clear()
        for rs in self.all_rs:
            rs.clear()
        for fu in self.all_fus:
            fu.clear()

        # Restore the speculative RAT from the committed RAT.
        self.rat.map = list(self.arch_rat.map)

        # Rebuild the Ready Table: only committed physical registers hold valid
        # values now; everything else is free (ready bit irrelevant).
        committed = set(self.arch_rat.map)
        for p in range(self.cfg.n_phys):
            self.ready.ready[p] = p in committed

        # Rebuild the free list: every physical register not referenced by the
        # committed RAT is available again.
        self.free_list = FreeList([p for p in range(self.cfg.n_phys)
                                   if p not in committed])

        self.pc = redirect_idx
        self._ev["flush"] = True
        self._ev["redirect"] = redirect_idx

    # =====================================================================
    # Stage 5: WRITEBACK (broadcast results on the CDB)
    # =====================================================================
    def _writeback(self) -> None:
        # Collect every operation that finished EX (done) across all FUs.
        done: list[tuple[FunctionalUnit, "object"]] = []
        for fu in self.all_fus:
            for slot in fu.done_slots():
                done.append((fu, slot))

        # Operations that do NOT write a register (stores, conditional
        # branches, plain jumps) complete via the ROB directly and never
        # occupy a CDB port.
        cdb_candidates = []
        for fu, slot in done:
            dyn = slot.dyn
            if dyn.pd is None:
                self._finalize_rob(dyn)
                fu.release(slot)
                self._ev["wrote_back"].append(dyn.short())
            else:
                cdb_candidates.append((fu, slot))

        # Register-producing results contend for the limited CDB ports.
        cdb_candidates.sort(key=lambda fs: (_CDB_PRIORITY[fs[0].kind],
                                            fs[1].dyn.seq))
        broadcasts = []
        for fu, slot in cdb_candidates[: self.cfg.cdb_ports]:
            dyn = slot.dyn
            # ---- the actual CDB broadcast ----
            self.prf.write(dyn.pd, dyn.result)   # write data into the PRF
            self.ready.set(dyn.pd)               # global ready status (for dispatch)
            for rs in self.all_rs:               # Tomasulo wakeup: tag-match
                rs.wake(dyn.pd)                  # set V1/V2 of matching RS entries
            self._finalize_rob(dyn)              # mark ROB entry completed
            fu.release(slot)
            broadcasts.append((dyn.pd, dyn.result, dyn.seq))
            self._ev["wrote_back"].append(dyn.short())
        # Losers keep their result held in the FU and retry next cycle.
        self._ev["cdb"] = broadcasts

    def _finalize_rob(self, dyn: DynInst) -> None:
        """Write per-instruction completion info into the ROB entry."""
        entry = self.rob.entry_at(dyn.rob_idx)
        if entry is None:
            return  # squashed by an earlier flush this cycle
        entry.completed = True
        if dyn.pd is not None:
            entry.value = dyn.result
        if entry.is_store:
            entry.st_addr = dyn.addr
            entry.st_data = dyn.st_data
        if dyn.inst.is_control:
            entry.actual_taken = dyn.taken
            entry.mispredict = dyn.mispredict
            entry.redirect_idx = dyn.redirect_idx

    # =====================================================================
    # Stage 4: EXECUTE (advance functional-unit timers)
    # =====================================================================
    def _execute(self) -> None:
        for fu in self.all_fus:
            for slot in fu.slots:
                if not slot.done:
                    self._ev["executing"].append(slot.dyn.short())
            fu.tick()

    # =====================================================================
    # Stage 3: ISSUE / SELECT (wakeup + operand read + dispatch to FU)
    # =====================================================================
    def _older_store_pending(self, load_seq: int) -> bool:
        """Conservative load disambiguation: true while an older store is still
        in the ROB (i.e. has not yet committed to memory)."""
        for e in self.rob.in_order():
            if e.is_store and e.seq < load_seq:
                return True
        return False

    def _is_non_speculative(self, dyn: DynInst) -> bool:
        """True if ``dyn``'s ROB entry lies in the non-speculative region
        (no older *unresolved* branch shadows it)."""
        ns = self.rob.non_speculative_last_idx()
        if ns is None:
            return False
        # Distance from head, comparing program-order positions in the ROB.
        head = self.rob.head
        pos = (dyn.rob_idx - head) % self.rob.size
        ns_pos = (ns - head) % self.rob.size
        return pos <= ns_pos

    def _issue_to(self, fu: FunctionalUnit, dyn: DynInst) -> None:
        # Read operand *values* from the PRF now (they are guaranteed ready).
        dyn.val1 = self.prf.read(dyn.ps1) if dyn.ps1 is not None else 0
        dyn.val2 = self.prf.read(dyn.ps2) if dyn.ps2 is not None else 0
        self._compute(dyn)             # functional result (golden model)
        fu.accept(dyn)
        dyn.stage = "execute"
        self._ev["issued"].append(dyn.short())

    def _issue(self) -> None:
        # Selection now reads the per-entry V1/V2 valid bits (RSEntry.ready()),
        # not the global Ready Table: classic Tomasulo wakeup.

        # --- RS_Int -> ALUs ---
        for e in sorted(self.rs_int.live(), key=lambda x: x.dyn.seq):
            if not e.ready():
                continue
            fu = next((u for u in self.alus if u.can_accept()), None)
            if fu is None:
                break
            self._issue_to(fu, e.dyn)
            self.rs_int.remove(e.dyn)

        # --- RS_MulDiv -> MUL / DIV ---
        for e in sorted(self.rs_muldiv.live(), key=lambda x: x.dyn.seq):
            if not e.ready():
                continue
            fu = self.mul if e.dyn.op == Opcode.MULT else self.div
            if not fu.can_accept():
                continue
            self._issue_to(fu, e.dyn)
            self.rs_muldiv.remove(e.dyn)

        # --- RS_LoadStore -> LU ---
        for e in sorted(self.rs_loadstore.live(), key=lambda x: x.dyn.seq):
            if not e.ready():
                continue
            inst = e.dyn.inst
            if inst.is_load:
                # Conservative load: must be non-speculative AND have no older
                # uncommitted store ahead of it.
                if not self._is_non_speculative(e.dyn):
                    continue
                if self._older_store_pending(e.dyn.seq):
                    continue
            if not self.lu.can_accept():
                break
            self._issue_to(self.lu, e.dyn)
            self.rs_loadstore.remove(e.dyn)

        # --- RS_Branch -> BU ---
        for e in sorted(self.rs_branch.live(), key=lambda x: x.dyn.seq):
            if not e.ready():
                continue
            if not self.bu.can_accept():
                break
            self._issue_to(self.bu, e.dyn)
            self.rs_branch.remove(e.dyn)

    def _compute(self, dyn: DynInst) -> None:
        """Compute the functional result of an instruction (executed at issue)."""
        inst = dyn.inst
        info = inst.info
        a = dyn.val1
        b = inst.imm if info.use_imm else dyn.val2

        if inst.is_load:
            dyn.addr = w32(dyn.val1 + inst.imm)
            dyn.result = self.mem.load(dyn.addr)
        elif inst.is_store:
            dyn.addr = w32(dyn.val1 + inst.imm)
            dyn.st_data = dyn.val2
        elif inst.is_branch:
            taken = (dyn.val1 == 0) if inst.op == Opcode.BEQZ else (dyn.val1 != 0)
            dyn.taken = taken
            dyn.redirect_idx = inst.target_idx if taken else inst.idx + 1
            dyn.mispredict = (dyn.redirect_idx != dyn.pred_target)
        elif inst.is_jump:
            if inst.op in (Opcode.JR, Opcode.JALR):
                dyn.redirect_idx = (dyn.val1 // 4)      # register holds byte addr
            else:
                dyn.redirect_idx = inst.target_idx
            dyn.taken = True
            dyn.mispredict = (dyn.redirect_idx != dyn.pred_target)
            if inst.op in (Opcode.JAL, Opcode.JALR):
                dyn.result = w32((inst.idx + 1) * 4)    # link = address of NPC
        elif info.compute is not None:
            dyn.result = info.compute(a, b)
        else:
            dyn.result = 0  # NOP and the like

    # =====================================================================
    # Stage 2: DISPATCH (decode buffer -> rename + ROB/RS allocation)
    # =====================================================================
    def _dispatch(self) -> None:
        for _ in range(self.cfg.width):
            if not self.fetch_q:
                break
            dyn = self.fetch_q[0]
            inst = dyn.inst
            rs = self._rs_for(inst.fu)

            need_phys = inst.writes_reg and inst.rd is not None and inst.rd != 0

            # Structural stalls: stall the WHOLE front-end in program order if
            # the ROB, the target RS, or (when needed) the free list is full.
            if self.rob.full() or rs.full() or (need_phys and self.free_list.empty()):
                break

            self.fetch_q.popleft()

            # ---- rename sources: arch reg -> current physical tag ----
            dyn.ps1 = self.rat.lookup(inst.rs1) if inst.rs1 is not None else None
            dyn.ps2 = self.rat.lookup(inst.rs2) if inst.rs2 is not None else None

            # ---- rename destination: grab a fresh physical reg ----
            if need_phys:
                dyn.told = self.rat.lookup(inst.rd)   # save old map -> free at commit
                dyn.pd = self.free_list.pop()
                self.rat.set(inst.rd, dyn.pd)         # speculative RAT update
                self.ready.clear(dyn.pd)              # result not produced yet
            else:
                dyn.pd = None
                dyn.told = None

            # ---- allocate the ROB entry (in program order) ----
            entry = ROBEntry(
                seq=dyn.seq, dyn=dyn,
                arch_dest=(inst.rd if need_phys else None),
                pd=dyn.pd, told=dyn.told, writes_reg=need_phys,
                is_store=inst.is_store, is_branch=inst.is_branch,
                is_jump=inst.is_jump, pred_taken=dyn.pred_taken,
            )
            dyn.rob_idx = self.rob.alloc(entry)

            # ---- allocate the reservation-station entry (tags + valid bits) --
            # Initialise the per-source valid bits from the global Ready Table:
            # a source is valid now iff it is unused or its producer already
            # broadcast.  Subsequent producers wake the entry via rs.wake().
            v1 = dyn.ps1 is None or self.ready.is_ready(dyn.ps1)
            v2 = dyn.ps2 is None or self.ready.is_ready(dyn.ps2)
            rs.insert(dyn, v1, v2)
            dyn.stage = "dispatch"
            self._ev["dispatched"].append(dyn.short())

    def _rs_for(self, fu: FUClass) -> ReservationStation:
        if fu == FUClass.INT:
            return self.rs_int
        if fu == FUClass.MULDIV:
            return self.rs_muldiv
        if fu == FUClass.LOADSTORE:
            return self.rs_loadstore
        return self.rs_branch

    # =====================================================================
    # Stage 1: FETCH (with branch prediction; follows the predicted stream)
    # =====================================================================
    def _fetch(self) -> None:
        cap = self.cfg.width * 2          # bound the decode buffer
        for _ in range(self.cfg.width):
            if self.pc >= len(self.program) or len(self.fetch_q) >= cap:
                break
            inst = self.program[self.pc]
            dyn = DynInst(seq=self.seq_ctr, inst=inst)
            self.seq_ctr += 1
            taken, nxt = self.bp.predict(inst)
            dyn.pred_taken = taken
            dyn.pred_target = nxt
            self.fetch_q.append(dyn)
            dyn.stage = "fetch"
            self._ev["fetched"].append(dyn.short())
            # Log the predictor read so the GUI can show what the BHT decided.
            if inst.is_branch or inst.is_jump:
                self._ev["pred"].append({
                    "seq": dyn.seq,
                    "text": inst.text,
                    "kind": "br" if inst.is_branch else "jmp",
                    "taken": taken,
                    "target": nxt,
                    "index": self.bp.index(inst.idx) if inst.is_branch else None,
                    "ctr": self.bp.counter(inst.idx) if inst.is_branch else None,
                })
            self.pc = nxt                 # follow the predicted path

    # =====================================================================
    # Halt detection
    # =====================================================================
    def _check_halt(self) -> None:
        drained = (self.pc >= len(self.program)
                   and not self.fetch_q
                   and self.rob.empty()
                   and all(not rs.live() for rs in self.all_rs)
                   and all(not fu.slots for fu in self.all_fus))
        if drained:
            self.halted = True

    # =====================================================================
    # Snapshot: an immutable picture of the whole machine for the GUI.
    # =====================================================================
    @staticmethod
    def _tag(p: Optional[int]) -> str:
        return f"p{p}" if p is not None else "-"

    def _rs_rows(self, rs: ReservationStation) -> list[dict]:
        """Build display rows whose keys match the RTL field diagram for the
        station's ``kind``."""
        rows = []
        for e in rs.live():
            d = e.dyn
            inst = d.inst
            base = {
                "seq": d.seq,
                "op": d.op.name,
                "text": inst.text,
                "rob": d.rob_idx,
                "ready": e.ready(),
            }
            if rs.kind == "branch":
                # Branch-Unit fields: reg, V(valid), imm(offset), use_reg, is_cond
                use_reg = inst.op in (Opcode.JR, Opcode.JALR)
                base.update({
                    "reg": self._tag(d.ps1),
                    "v": e.v1,
                    "imm": (None if use_reg else inst.target_idx),
                    "use_reg": use_reg,
                    "is_cond": inst.is_branch,
                })
            else:
                # Int / MulDiv / LoadStore fields: S1,V1,S2,V2,Imm,is_imm,D,ROB
                base.update({
                    "s1": self._tag(d.ps1), "v1": e.v1,
                    "s2": self._tag(d.ps2), "v2": e.v2,
                    "imm": (inst.imm if inst.use_imm else None),
                    "is_imm": inst.use_imm,
                    "d": self._tag(d.pd),
                })
                if rs.kind == "loadstore":
                    base["is_store"] = inst.is_store
            rows.append(base)
        return rows

    def _fu_rows(self) -> list[dict]:
        rows = []
        for fu in self.all_fus:
            slots = [{
                "seq": s.dyn.seq,
                "text": s.dyn.inst.text,
                "remaining": s.remaining,
                "done": s.done,
                "pd": f"p{s.dyn.pd}" if s.dyn.pd is not None else "-",
            } for s in fu.slots]
            rows.append({"name": fu.name, "kind": fu.kind, "slots": slots})
        return rows

    def _rob_rows(self) -> list[dict]:
        rows = []
        head = self.rob.head
        ns = self.rob.non_speculative_last_idx()
        newest_store = self.rob.newest_store_idx()
        newest = self.rob.newest_idx()
        for i in range(self.rob.count):
            idx = (head + i) % self.rob.size
            e = self.rob.buf[idx]
            rows.append({
                "rob": idx,
                "head": i == 0,
                # ---- maintained ROB pointers (RTL field diagram) ----
                "oldest": idx == self.rob.oldest_idx(),
                "newest": idx == newest,
                "non_spec_last": idx == ns,
                "newest_store": idx == newest_store,
                # ---- diagram fields: OPCODE/FUNC, Dest(arch), Finished ----
                "seq": e.seq,
                "opcode": e.dyn.op.name,
                "text": e.dyn.inst.text,
                "dest": f"R{e.arch_dest}" if e.arch_dest is not None else "-",
                "finished": e.completed,
                # ---- internal rename bookkeeping (not in the simplified RTL
                #      diagram, but needed for PRF->ARF copy + free-list recycle)
                "pd": f"p{e.pd}" if e.pd is not None else "-",
                "told": f"p{e.told}" if e.told is not None else "-",
                "store": (f"M[{e.st_addr}]={e.st_data}"
                          if e.is_store and e.st_addr is not None else
                          ("store" if e.is_store else "")),
                "branch": (("mispred->%d" % e.redirect_idx) if e.mispredict
                           else ("ctl" if (e.is_branch or e.is_jump) else "")),
            })
        return rows

    def _snapshot(self, reset: bool = False) -> dict:
        ev = self._ev if not reset else {
            "fetched": [], "dispatched": [], "issued": [], "executing": [],
            "wrote_back": [], "committed": [], "cdb": [], "flush": False,
            "redirect": None, "pc_start": 0, "pc_end": self.pc,
            "pred": [], "bp_update": [],
        }
        return {
            "cycle": self.cycle,
            "halted": self.halted,
            "pc_start": ev.get("pc_start", self.pc),
            "pc_end": ev.get("pc_end", self.pc),
            "pc_byte": ev.get("pc_end", self.pc) * 4,
            # register state
            "rat": list(self.rat.map),
            "arch_rat": list(self.arch_rat.map),
            "free_list": self.free_list.as_list(),
            "ready": list(self.ready.ready),
            "arf": list(self.arf.regs),
            "prf": list(self.prf.regs),
            "mem": dict(self.mem.mem),
            # queues
            "rob": self._rob_rows(),
            "rob_ptrs": {
                "oldest": self.rob.oldest_idx(),
                "newest": self.rob.newest_idx(),
                "non_spec_last": self.rob.non_speculative_last_idx(),
                "newest_store": self.rob.newest_store_idx(),
            },
            "rs_int": self._rs_rows(self.rs_int),
            "rs_muldiv": self._rs_rows(self.rs_muldiv),
            "rs_loadstore": self._rs_rows(self.rs_loadstore),
            "rs_branch": self._rs_rows(self.rs_branch),
            "fus": self._fu_rows(),
            # branch predictor: BHT of 2-bit saturating counters + BTB
            "bht": list(self.bp.bht),
            "btb": dict(self.bp.btb),
            # per-cycle activity
            "events": {
                "fetched": list(ev.get("fetched", [])),
                "dispatched": list(ev.get("dispatched", [])),
                "issued": list(ev.get("issued", [])),
                "executing": list(ev.get("executing", [])),
                "wrote_back": list(ev.get("wrote_back", [])),
                "committed": list(ev.get("committed", [])),
                "cdb": list(ev.get("cdb", [])),
                "flush": ev.get("flush", False),
                "redirect": ev.get("redirect", None),
                "pred": list(ev.get("pred", [])),
                "bp_update": list(ev.get("bp_update", [])),
            },
        }
