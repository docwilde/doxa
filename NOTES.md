# Fleet supervision work

Brief: independent read-only fleet supervision with a selectable model, plus a separate selectable fast LLM or TypeSafe Jev message judge. Real peer admission must enforce typed provenance and bounded host policy. No live paid calls, release edits, installs, pushes, or worker tools.

Design: preserve the existing acting `--supervisor` coordinator; add a distinct alignment supervisor. Host-issued immutable charter and assignments are installed at the fleet dispatch barrier. The daemon validates kernel sender PID, fleet membership, typed envelopes, duplicates, limits, and paused state before peer messages can start a turn. A shared private durable guard journal records decisions, reservations, and pauses. Model results cannot confer authority. Resume verifies the frozen charter and host state.

TypeSafe API verified against https://docs.typesafe.ai/api and https://docs.typesafe.ai/models on 2026-10-08: POST https://api.typesafe.ai/v1/systemone, Bearer TYPESAFE_API_KEY, state/model/questions; Noul answers contain type/noul, usage input_tokens/output_tokens. jev-1.13.0 input $0.042/M; output free. No fabricated SDK dependency.

Validation pending. Full suites remain root-coordinated and serial. TMPDIR points to /home/docwilde/ssd-cache/tmp; build targets remain real disk and jobs <= 3.
