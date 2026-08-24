# AI Usage

Declare what you used and how. We are not scoring the amount — we are checking that
you can account for your own work.

## Tools used

Claude (Anthropic), used interactively throughout via chat as a step-by-step tutor and
pair-programming partner.

## Where you used them

| Chapter | How you used AI |
|---|---|
| 1 · Source | Explained the EDGAR CIK-lookup / submissions / archive APIs and their path conventions before I wrote anything. Diagnosed the Tudor Investment Corp CIK mismatch with me (why 38 filings instead of 40) by walking through the lookup file together; I typed and ran every function myself (`normalize`, `verify_ciks`, `find_filings`, `save_filing`, etc.), and Claude reviewed my code and helped me fix two bugs in `fetch_filing_document` (a mixed if/else return, then a misindented `return` inside a loop). |
| 2 · Interrogate | Explained what real cross-filer inconsistencies to look for (namespace prefixes, optional-field presence, count validation) and helped debug a `TypeError` in `submission/eda.py` caused by XML comment nodes having a non-string `.tag`. I wrote and ran `eda.py` myself against the real downloaded filings and wrote up the findings from the actual output. |
| 3 · Structure | Explained pyarrow schema construction and the float/pandas-index pitfalls called out in the docs. I typed and ran the parsing/schema-building code myself, and Claude helped me verify determinism (SHA256 hashing two runs) and understand why that check matters. |
| 4 · Serve | Discussed architecture options for `agents/answer.py` given the security/reliability/scalability constraints in the docs, and Claude provided a full draft implementation (intent-classification + deterministic operation dispatch) which I reviewed, pasted in, and tested myself under `LLM_MODE=mock`. I have not yet tested it against a live model, since I do not have a local LLM endpoint. |

## What you would change

The Chapter 4 operation set (`agents/answer.py`) only covers the patterns implied by
`docs/questions-examples.md`. I understand the classify-then-execute design and why it
was chosen, but I have not verified it against a real model's behavior, and I'd want to
add more operations and test against a live endpoint (e.g. a small local model) with
more time.