# ADA Homework Tutor — Post-v1 TODO

A running list of ideas deferred from the initial design (`01_initial_design_doc.md`). Add freely; check off as shipped.

## Integrations
- [ ] **Canvas API auto-posting** — replace receipt-paste with auto-post via Canvas LMS API. Wired in v1 via the `proof_tokens.canvas_posted_at` column and the `post_to_canvas(token)` no-op stub.
- [ ] **University SSO** auth backend (Shibboleth / SAML / CAS). The `AuthBackend` interface is already a seam for this.
- [ ] **BYO student API keys** — let students paste their own Anthropic/OpenAI/Gemini keys, encrypted at rest with a server master key. Falls back to instructor's configured backend.

## LLM / grading
- [ ] **All-local Excel grading fallback** — `openpyxl`-based text representation feeding the local LLM, for instructors who want zero cloud calls.
- [ ] **Local vision model option** — Qwen2.5-VL-7B served by llama.cpp as the `vision` role provider.
- [ ] **Tutoring guardrail upgrade** — second-LLM judge for reference-solution leakage, replacing or augmenting the v1 regex check.
- [ ] **RAG over course materials** so the tutor can cite slides/textbook passages when scaffolding.

## Authoring & ops
- [ ] **Question authoring GUI** for TAs (instead of raw markdown).
- [ ] **`make validate-assignments`** — frontmatter schema validator + dry-run loader.
- [ ] **Instructor analytics dashboard** — per-student progress, common misconceptions, time-on-question.
- [ ] **Streaming UI polish** (SSE + HTMX `hx-ext="sse"`) for token-by-token tutor replies.
- [ ] **Token batch-submit** — student selects multiple proof tokens and submits them to Canvas in one action.

## Anti-cheat / integrity
- [ ] **Image perceptual-hash dedupe** across submissions to flag students sharing graph images.
- [ ] **Code similarity check** across Python submissions (e.g. tokenized fingerprint).
- [ ] **Rate-limit attempt creation** per assignment per student per hour.

## Misc
- [ ] **Dark mode** for the student UI.
- [ ] **Printable assignment summary** PDF for instructor records.
