# Production deployment checklist

- [ ] Current branch is `main`.
- [ ] Back up current `smc_scanner.py`.
- [ ] Add `bingx_position_tracker.py`.
- [ ] Run `apply_athena_bingx_patch.py`.
- [ ] `py_compile` passes.
- [ ] Workflow remains `7,22,37,52 * * * *`.
- [ ] Workflow still passes all four secrets.
- [ ] No write endpoint appears in `bingx_position_tracker.py`.
- [ ] Manual GitHub Action run.
- [ ] Log shows `BingX sync` states.
- [ ] Real OPEN position freezes actual average fill.
- [ ] Closed position becomes terminal.
- [ ] UNKNOWN/ERROR/NOT_FOUND never trigger active update.
- [ ] TP1/TP2/TP3 alerts are one-shot.
- [ ] No post-closure R update.
- [ ] Only after all above pass: review entry-behavior learning.
