# Project guidance

## `clean-room/` — reference material only, nothing copied out

`clean-room/` is gitignored and holds third-party reference material (captured
traffic, decompiled or vendored code, docs, samples). It exists so behavior can
be **understood**, never so it can be **reused**.

Rules for anything under `clean-room/`:

1. **No direct copying out.** Never copy, paste, move, symlink, or transcribe
   any file or fragment from `clean-room/` into the rest of the project. That
   includes code, comments, string literals, constants, regexes, JSON/YAML
   blobs, and file layouts.
2. **Inference only.** Read the material to learn *what* it does — endpoints,
   request/response shapes, field semantics, sequencing, edge cases — then
   write an independent implementation from that understanding, in this
   project's own style and naming.
3. **Two steps, not one.** When implementing from clean-room material, first
   write a short spec in your own words (what the behavior is, not how the
   source does it). Then implement from the spec, not with the source open
   side by side.
4. **Facts are fine, expression is not.** Protocol details, URLs, header
   names, enum values, and wire formats that are *necessary* for
   interoperability may be used. Anything that is a choice (structure,
   comments, helper logic, prose) must be re-derived.
5. **Scrub the output.** Inference is not enough on its own. After writing
   anything derived from clean-room material (code, spec, notes, docs),
   compare it against the source and remove or rewrite every passage that
   matches: identical or near-identical lines, comment text, string literals,
   doc sentences, and structural sequences that mirror the original. Repeat
   until nothing in the output can be traced to a copy. State in your reply
   that the scrub was done and what, if anything, was rewritten.
   **The only exception is variable names:** a variable, field, or parameter
   name may match the source. Everything else must not.
6. **Never reference it from project code.** Nothing in `src/`, tests, or
   build config may import, read, or path-reference `clean-room/`.
7. **Keep it out of history.** Do not quote clean-room content in commit
   messages, PR descriptions, code comments, or docs. Do not `git add -f` it.

If a task can only be done by copying from `clean-room/`, stop and say so
rather than doing it.
