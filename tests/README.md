# Tester

Kjøres direkte, ingen pytest nødvendig:

    python tests/test_p21_beats.py
    python tests/test_p21_alignment.py

Suitene fra øktene 3-4 (test_p4_p10, test_p7_p11_p13, test_p9_p12_p15,
test_p18, test_p17_p19) lå i sesjons-scratchpad og er tapt — nye tester
legges HER i repoet så de overlever.

## Mønstre (fra øktene 3-4, verdt å gjenbruke)

- **Mock-klipp**: `testsrc2` i 720p/30fps — IKKE 1080p60. Motoren skal
  normalisere selv; mock i målformatet skjuler fps/oppløsnings-feller
  (zoompan regner tid som `on/60` og bommet på 30fps-kilder før fiksen).
- **Effekt-verifisering**: PSNR mellom med/uten effekt, målt i tidsvinduer —
  effekten skal gi lav PSNR i sitt vindu og ~inf utenfor.
- **freezedetect virker IKKE med grain** (temporal støy) — bruk kontaktark
  (tile av frames) eller PSNR mellom nabo-frames i stedet.
- Encode med `-preset ultrafast` i tester; skru av `smooth_slowmo`
  (minterpolate) med mindre det er det som testes.
