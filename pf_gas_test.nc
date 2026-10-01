%
O0001 (PF GAS TEST - 5 SEC DWELL)
(Description: Turn ON PF Gas, Dwell for 5 Seconds, Turn OFF PF Gas)

G90 G94 (Absolute coordinates, feed per minute)

(--- 1. TURN ON PF GAS ---)
M64 P24       (PF Gas ON - Digital output bit 24)

(--- 2. DWELL FOR 5 SECONDS ---)
G04 P5        (Dwell 5 seconds - LinuxCNC / Haas / Grbl / Marlin)
; G04 P5000   (Use G04 P5000 if your controller reads P in milliseconds: Fanuc/Mach3)
; G04 X5.0    (Use G04 X5.0 if Fanuc X-parameter dwell is used)

(--- 3. TURN OFF PF GAS ---)
M65 P24       (PF Gas OFF)

M30           (Program end and reset)
%
