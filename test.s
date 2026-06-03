; ===========================================================================
; test.s -- DLX program that exercises the out-of-order machine.
;
; Initial state provided by main.py build_config():
;   R1=5  R2=7  R4=3  R6=100  R8=200  R9=1      M[200]=42
;
; Hazards demonstrated:
;   RAW  : SUB depends on ADD's R3            -> consumer must wait for tag
;   WAW  : two writes to R6                   -> each gets its own phys reg
;   WAR  : MULT reads R2, then ADDI writes R2 -> rename keeps MULT's old value
;   Branch misprediction: BEQZ R0 is always taken but predicted not-taken,
;        so the two wrong-path instructions are fetched, then FLUSHED at commit.
;   Memory: LW then dependent SW (store writes memory only at commit).
; ===========================================================================

start:
        ADD   R3,  R1, R2        ; R3 = 5 + 7 = 12          (producer)
        SUB   R5,  R3, R4        ; RAW on R3: R5 = 12 - 3 = 9
        ADD   R6,  R1, R2        ; WAW: first write to R6 (=12)
        ADDI  R6,  R5, #1        ; WAW: second write to R6 (=10) wins at commit
        MULT  R7,  R1, R2        ; R7 = 5 * 7 = 35 (reads R2 = 7)
        ADDI  R2,  R0, #99       ; WAR on R2: R2 = 99 (must not disturb MULT)
        LW    R10, 0(R8)         ; R10 = M[200] = 42
        SW    R10, 4(R8)         ; M[204] = 42 (written only at commit)
        BEQZ  R0,  target        ; R0 == 0 -> taken; predicted NOT taken -> flush
        ADDI  R11, R0, #777      ; wrong path -- must be flushed, never commits
        ADDI  R12, R0, #888      ; wrong path -- must be flushed, never commits
target:
        ADDI  R13, R0, #1        ; correct path: R13 = 1
        ADD   R14, R3, R5        ; R14 = 12 + 9 = 21
