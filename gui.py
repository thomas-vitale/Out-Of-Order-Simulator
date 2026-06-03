"""
gui.py
======

Tkinter cycle-by-cycle viewer for the DLX out-of-order simulator.

The simulator runs to completion *first* and records one immutable snapshot per
cycle (see ``Simulator.run``).  This GUI never advances the model live -- it
simply navigates that list of snapshots.  Because every cycle is a frozen
picture, stepping **backwards** is exact and instantaneous.

Controls:
    Next  ▶   /  Right arrow   -> advance one cycle
    ◀ Prev    /  Left arrow    -> go back one cycle
    Reset     /  Home          -> jump to cycle 0 (reset state)
    End                        -> jump to the last cycle

What you see, refreshed on every navigation (cells that changed since the
previous cycle are highlighted):
    * the program counter (start -> end) and a FLUSH banner on misprediction
    * the speculative RAT and the committed RAT (+ ready bits)
    * the ROB (head marked), the three reservation stations, the free list
    * the functional units currently executing, and the CDB broadcasts
    * a per-stage pipeline activity list (fetch/dispatch/issue/exec/wb/commit)
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

# colour tags
C_CHANGED = "#fff3b0"   # something changed vs. previous cycle (light amber)
C_SPEC = "#d6e4ff"      # speculative RAT mapping differs from committed (blue)
C_HEAD = "#c8f7c5"      # ROB head (green)
C_DONE = "#e6e6e6"      # completed / done (grey)
C_FLUSH = "#ffb3b3"     # flush banner (red)


class OoOViewer:
    def __init__(self, root, snapshots, program, sim, cfg):
        self.root = root
        self.snaps = snapshots
        self.program = program
        self.sim = sim
        self.cfg = cfg
        self.idx = 0

        root.title("DLX Out-of-Order Simulator -- cycle viewer")
        root.geometry("1400x860")

        self._build_controls()
        self._build_body()
        self._bind_keys()
        self.refresh()

    # ------------------------------------------------------------------ UI
    def _build_controls(self):
        bar = ttk.Frame(self.root, padding=6)
        bar.pack(side=tk.TOP, fill=tk.X)

        ttk.Button(bar, text="Reset", command=self.reset).pack(side=tk.LEFT)
        ttk.Button(bar, text="◀ Prev", command=self.prev).pack(side=tk.LEFT, padx=4)
        ttk.Button(bar, text="Next ▶", command=self.next).pack(side=tk.LEFT)
        ttk.Button(bar, text="End", command=self.end).pack(side=tk.LEFT, padx=4)

        self.cycle_var = tk.StringVar()
        self.pc_var = tk.StringVar()
        ttk.Label(bar, textvariable=self.cycle_var,
                  font=("Consolas", 13, "bold")).pack(side=tk.LEFT, padx=20)
        ttk.Label(bar, textvariable=self.pc_var,
                  font=("Consolas", 13, "bold")).pack(side=tk.LEFT, padx=10)

        self.flush_lbl = tk.Label(bar, text="", font=("Consolas", 12, "bold"),
                                  fg="#a00000")
        self.flush_lbl.pack(side=tk.LEFT, padx=20)

        # a scale to scrub through cycles
        self.scale = ttk.Scale(bar, from_=0, to=len(self.snaps) - 1,
                               orient=tk.HORIZONTAL, command=self._on_scale)
        self.scale.pack(side=tk.RIGHT, fill=tk.X, expand=True, padx=10)

    def _make_tree(self, parent, columns, widths, height=8, title=None):
        """A Treeview with vertical + horizontal scrollbars whose columns
        stretch with the (resizable) pane, so nothing is ever permanently
        hidden."""
        frame = ttk.LabelFrame(parent, text=title or "", padding=2)
        tree = ttk.Treeview(frame, columns=columns, show="headings",
                            height=height)
        for col, w in zip(columns, widths):
            tree.heading(col, text=col)
            # stretch=True lets columns grow when the pane widens; minwidth keeps
            # them readable, and the horizontal scrollbar covers the rest.
            tree.column(col, width=w, minwidth=35, anchor=tk.W, stretch=True)
        vsb = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        hsb = ttk.Scrollbar(frame, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        # row colour tags
        tree.tag_configure("changed", background=C_CHANGED)
        tree.tag_configure("spec", background=C_SPEC)
        tree.tag_configure("head", background=C_HEAD)
        tree.tag_configure("done", background=C_DONE)
        return frame, tree

    def _label_panel(self, parent, title, var):
        """A titled, auto-wrapping text panel that expands with its pane."""
        frame = ttk.LabelFrame(parent, text=title, padding=4)
        lbl = tk.Label(frame, textvariable=var, justify=tk.LEFT, anchor="nw",
                       font=("Consolas", 9))
        lbl.pack(fill=tk.BOTH, expand=True)
        # re-wrap the text whenever the panel is resized
        lbl.bind("<Configure>",
                 lambda e, l=lbl: l.config(wraplength=max(120, e.width - 10)))
        return frame

    def _build_body(self):
        # Everything sits inside draggable split panes so any region can be
        # resized by dragging the sash between panels.
        self.cdb_var = tk.StringVar()
        self.pipe_var = tk.StringVar()
        self.free_var = tk.StringVar()
        self.bp_var = tk.StringVar()

        outer = ttk.PanedWindow(self.root, orient=tk.HORIZONTAL)
        outer.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=4, pady=4)

        # --- column 0 (vertical paned): RAT, free list, BHT, predictor log ---
        col0 = ttk.PanedWindow(outer, orient=tk.VERTICAL)
        outer.add(col0, weight=2)
        f, self.t_rat = self._make_tree(
            col0, ("Reg", "Spec", "Arch", "Rdy"), (50, 55, 55, 45),
            height=12, title="Register Alias Table (spec vs committed)")
        col0.add(f, weight=3)
        col0.add(self._label_panel(col0, "Free List (physical regs)",
                                   self.free_var), weight=1)
        f, self.t_bht = self._make_tree(
            col0, ("Idx", "Bits", "State", "Pred"), (45, 55, 90, 60),
            height=8, title="Branch Predictor - BHT (2-bit saturating)")
        col0.add(f, weight=2)
        col0.add(self._label_panel(col0, "Predictor activity this cycle",
                                   self.bp_var), weight=1)

        # --- column 1 (vertical paned): ROB on top, PRF below ---
        col1 = ttk.PanedWindow(outer, orient=tk.VERTICAL)
        outer.add(col1, weight=3)
        f, self.t_rob = self._make_tree(
            col1, ("ROB", "Seq", "Instr", "Dst", "pd", "old", "St", "Done"),
            (40, 40, 150, 40, 40, 40, 90, 45),
            height=14, title="Reorder Buffer (head = oldest)")
        col1.add(f, weight=3)
        f, self.t_prf = self._make_tree(
            col1, ("Phys", "Value", "Hex", "Rdy", "Arch"),
            (45, 75, 85, 40, 70),
            height=12, title="Physical Register File (values)")
        col1.add(f, weight=2)

        # --- column 2 (vertical paned): RS x3, FUs, CDB, pipeline ---
        col2 = ttk.PanedWindow(outer, orient=tk.VERTICAL)
        outer.add(col2, weight=3)
        rs_cols = ("Seq", "Op", "src1", "src2", "Imm", "pd", "ROB", "Rdy")
        rs_w = (40, 55, 50, 50, 50, 40, 40, 40)
        f, self.t_rs_lb = self._make_tree(col2, rs_cols, rs_w, height=4,
                                          title="RS_LoadBranch")
        col2.add(f, weight=2)
        f, self.t_rs_int = self._make_tree(col2, rs_cols, rs_w, height=4,
                                           title="RS_Int")
        col2.add(f, weight=2)
        f, self.t_rs_md = self._make_tree(col2, rs_cols, rs_w, height=3,
                                          title="RS_MulDiv")
        col2.add(f, weight=2)
        f, self.t_fu = self._make_tree(
            col2, ("Unit", "Kind", "Instr(seq)", "Left", "St"),
            (55, 45, 170, 40, 40), height=6,
            title="Functional Units (executing)")
        col2.add(f, weight=3)
        col2.add(self._label_panel(col2, "CDB broadcasts this cycle",
                                   self.cdb_var), weight=1)
        col2.add(self._label_panel(col2, "Pipeline activity this cycle",
                                   self.pipe_var), weight=2)

    def _bind_keys(self):
        self.root.bind("<Right>", lambda e: self.next())
        self.root.bind("<Left>", lambda e: self.prev())
        self.root.bind("<Home>", lambda e: self.reset())
        self.root.bind("<End>", lambda e: self.end())

    # -------------------------------------------------------------- navigation
    def _on_scale(self, value):
        i = int(float(value))
        if i != self.idx:
            self.idx = i
            self.refresh(update_scale=False)

    def next(self):
        if self.idx < len(self.snaps) - 1:
            self.idx += 1
            self.refresh()

    def prev(self):
        if self.idx > 0:
            self.idx -= 1
            self.refresh()

    def reset(self):
        self.idx = 0
        self.refresh()

    def end(self):
        self.idx = len(self.snaps) - 1
        self.refresh()

    # ----------------------------------------------------------------- render
    def refresh(self, update_scale=True):
        snap = self.snaps[self.idx]
        prev = self.snaps[self.idx - 1] if self.idx > 0 else None
        ev = snap["events"]

        self.cycle_var.set(f"Cycle {snap['cycle']} / {self.snaps[-1]['cycle']}"
                           + ("  [HALTED]" if snap["halted"] else ""))
        self.pc_var.set(f"PC: {snap['pc_start']} → {snap['pc_end']}"
                        f"  (0x{snap['pc_byte']:04x})")
        if ev["flush"]:
            self.flush_lbl.config(text=f"*** FLUSH -> idx {ev['redirect']} ***")
        else:
            self.flush_lbl.config(text="")

        if update_scale:
            self.scale.set(self.idx)

        self._render_rat(snap, prev)
        self._render_free(snap, prev)
        self._render_rob(snap, prev)
        self._render_prf(snap, prev)
        self._render_rs(self.t_rs_lb, snap["rs_loadbranch"],
                        prev["rs_loadbranch"] if prev else None)
        self._render_rs(self.t_rs_int, snap["rs_int"],
                        prev["rs_int"] if prev else None)
        self._render_rs(self.t_rs_md, snap["rs_muldiv"],
                        prev["rs_muldiv"] if prev else None)
        self._render_fu(snap, prev)
        self._render_bht(snap, prev)
        self._render_cdb_pipe(snap)

    def _render_rat(self, snap, prev):
        t = self.t_rat
        t.delete(*t.get_children())
        for a in range(len(snap["rat"])):
            spec = snap["rat"][a]
            arch = snap["arch_rat"][a]
            rdy = "+" if snap["ready"][spec] else "."
            tags = []
            if prev and prev["rat"][a] != spec:
                tags.append("changed")
            elif spec != arch:
                tags.append("spec")
            t.insert("", tk.END, values=(f"R{a}", f"p{spec}", f"p{arch}", rdy),
                     tags=tags)

    def _render_free(self, snap, prev):
        cur = snap["free_list"]
        txt = " ".join(f"p{p}" for p in cur) or "(empty)"
        delta = ""
        if prev is not None:
            added = set(cur) - set(prev["free_list"])
            if added:
                delta = "   freed: " + " ".join(f"p{p}" for p in sorted(added))
        self.free_var.set(f"[{len(cur)} free]  {txt}{delta}")

    def _render_rob(self, snap, prev):
        t = self.t_rob
        t.delete(*t.get_children())
        prev_by_seq = {}
        if prev:
            prev_by_seq = {r["seq"]: r for r in prev["rob"]}
        for r in snap["rob"]:
            extra = r["store"] or r["branch"]
            tags = []
            pr = prev_by_seq.get(r["seq"])
            if pr is None or pr["done"] != r["done"]:
                tags.append("changed")
            elif r["head"]:
                tags.append("head")
            elif r["done"]:
                tags.append("done")
            t.insert("", tk.END, tags=tags, values=(
                ("▶ " if r["head"] else "") + str(r["rob"]),
                r["seq"], r["text"], r["dest"], r["pd"], r["told"],
                extra, "DONE" if r["done"] else ""))

    def _render_prf(self, snap, prev):
        """Show every physical register's value, ready bit, and the arch reg(s)
        that currently map to it in the speculative RAT."""
        t = self.t_prf
        t.delete(*t.get_children())
        # reverse the speculative RAT: phys reg -> arch reg(s) pointing at it
        rev = {}
        for a, p in enumerate(snap["rat"]):
            rev.setdefault(p, []).append(a)
        prev_prf = prev["prf"] if prev else None
        for p, val in enumerate(snap["prf"]):
            rdy = "+" if snap["ready"][p] else "."
            arch = ",".join(f"R{a}" for a in rev.get(p, [])) or "-"
            tags = []
            if prev_prf is not None and prev_prf[p] != val:
                tags = ["changed"]          # value written this/last cycle
            elif p in rev:
                tags = ["spec"]             # currently mapped (live) register
            t.insert("", tk.END, tags=tags, values=(
                f"p{p}", val, f"0x{val & 0xFFFFFFFF:08x}", rdy, arch))

    def _render_rs(self, tree, rows, prev_rows):
        tree.delete(*tree.get_children())
        prev_seqs = {r["seq"] for r in prev_rows} if prev_rows else set()
        for r in rows:
            tags = ["changed"] if r["seq"] not in prev_seqs else []
            if r["ready"] and not tags:
                tags = ["done"]
            imm = r.get("imm")
            tree.insert("", tk.END, tags=tags, values=(
                r["seq"], r["op"], r["ps1"], r["ps2"],
                str(imm) if imm is not None else "-",
                r["pd"], r["rob"],
                "yes" if r["ready"] else ""))

    def _render_fu(self, snap, prev):
        t = self.t_fu
        t.delete(*t.get_children())
        for fu in snap["fus"]:
            if not fu["slots"]:
                t.insert("", tk.END, values=(fu["name"], fu["kind"], "(idle)", "", ""))
            for sl in fu["slots"]:
                tags = ["done"] if sl["done"] else []
                t.insert("", tk.END, tags=tags, values=(
                    fu["name"], fu["kind"], f"#{sl['seq']} {sl['text']}",
                    sl["remaining"], "D" if sl["done"] else ""))

    _BHT_STATE = {0: "Strong NT", 1: "Weak NT", 2: "Weak T", 3: "Strong T"}

    def _render_bht(self, snap, prev):
        t = self.t_bht
        t.delete(*t.get_children())
        ev = snap["events"]
        # indices touched this cycle: read at fetch (pred) or trained at commit
        read_idx = {p["index"] for p in ev["pred"] if p["index"] is not None}
        upd_idx = {u["index"] for u in ev["bp_update"]}
        for i, ctr in enumerate(snap["bht"]):
            tags = []
            if i in upd_idx:
                tags = ["changed"]          # counter just trained
            elif i in read_idx:
                tags = ["spec"]             # consulted for a prediction
            t.insert("", tk.END, tags=tags, values=(
                i, f"{ctr:02b}", self._BHT_STATE[ctr],
                "TAKEN" if ctr >= 2 else "not-tk"))

        # textual activity log for predictions / training this cycle
        lines = []
        for p in ev["pred"]:
            if p["kind"] == "br":
                lines.append(f"predict #{p['seq']} {p['text']}: "
                             f"{'TAKEN' if p['taken'] else 'not-taken'} "
                             f"(BHT[{p['index']}]={p['ctr']:02b})")
            else:
                lines.append(f"predict #{p['seq']} {p['text']}: jump TAKEN")
        for u in ev["bp_update"]:
            mp = "  <MISPREDICT>" if u["mispredict"] else ""
            lines.append(f"train  #{u['seq']} BHT[{u['index']}] "
                         f"{u['old']:02b}->{u['new']:02b} "
                         f"({'taken' if u['taken'] else 'not-taken'}){mp}")
        self.bp_var.set("\n".join(lines) if lines else "(no branch activity)")

    def _render_cdb_pipe(self, snap):
        ev = snap["events"]
        if ev["cdb"]:
            self.cdb_var.set("\n".join(f"p{pd} = {val}   (from #{seq})"
                                       for pd, val, seq in ev["cdb"]))
        else:
            self.cdb_var.set("(none)")

        def line(label, items):
            return f"{label:<10s}: " + (", ".join(items) if items else "-")

        self.pipe_var.set("\n".join([
            line("Fetch", ev["fetched"]),
            line("Dispatch", ev["dispatched"]),
            line("Issue", ev["issued"]),
            line("Execute", ev["executing"]),
            line("Writeback", ev["wrote_back"]),
            line("Commit", ev["committed"]),
        ]))


def launch_gui(snapshots, program, sim, cfg):
    root = tk.Tk()
    OoOViewer(root, snapshots, program, sim, cfg)
    root.mainloop()
