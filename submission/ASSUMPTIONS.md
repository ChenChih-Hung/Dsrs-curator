# Assumptions

Where the specification was ambiguous, or where you asked a question and kept working
rather than waiting on an answer, record the call you made and why.

This is not a penalty. A documented assumption is a normal part of data work — the
alternative is a stalled pipeline or a silent guess nobody can audit later. We read
this alongside your output, and a well-reasoned assumption that differs from ours costs
you nothing.

| # | What was unclear | What you assumed | Why |
|---|---|---|---|
| 1 | Roster gave "Tudor Investment Corp" with no CIK. Exact-name matching against SEC's cik-lookup-data.txt resolved to CIK 0001080384, a registrant that filed only one unrelated form in 2007 and has no 13F history. | Manually searched the lookup file for "TUDOR" and found the actual active 13F filer registered as "TUDOR INVESTMENT CORP ET AL" under CIK 0000923093. Overrode the name-matched CIK with this one, with the source recorded as `corrected`. | A dormant CIK with zero 13F filings could not be the intended filer for a fund actively covered by this challenge; the differently-worded registration name is exactly the kind of case simple normalization can't catch. |
| 2 | Chapter 1 says "One file per filing," but an HR filing is actually two separate EDGAR documents — `primary_doc.xml` (cover page: filer identity, report period, amendment info) and `infotable.xml` (the holdings table). Chapter 3's `filings.parquet` needs cover-page fields that don't exist anywhere in the holdings document, and NT filings have no holdings document at all. | Saved both documents per filing: `{accession}.xml` (holdings table content, or the cover page alone for NT filings) and `{accession}.cover.xml` (always the cover page). | Keeping only the holdings document would have silently discarded every filing-level field Chapter 3 requires. Two cached files per filing still satisfies the spirit of "cache everything so reruns don't re-fetch" — it just needed to be two files instead of one to keep both documents' data available. |
| 3 | 04-serve.md leaves the retrieval architecture open ("no single right shape for it"). | Built `agents/answer.py` as an LLM-driven intent classifier: the model only ever picks one of a fixed set of supported operations and extracts plain-text parameters from the question. The actual data lookup, filtering, and aggregation runs as ordinary deterministic Python against the Parquet files — the model never sees a data row and never generates code or a query string that gets executed. | This satisfies "read-only," "same question, same answer," and "don't put the dataset in the prompt" directly: the LLM's only output is a small schema-constrained JSON object (operation + a handful of strings), which is validated against a fixed operation whitelist before anything runs. A free-form code/SQL-generation approach would have needed a sandboxed executor and much more validation to meet the same security bar. |
| 4 | I do not have a local LLM endpoint (no Ollama/LM Studio installed), so I could not test real operation-selection accuracy — only wiring. | Developed and tested entirely under `LLM_MODE=mock`, which exercises the full pipeline (data load → planning call → operation dispatch → output validation) without a live model. Confirmed no crashes and correct fallback-to-null behavior this way. | This is a real limitation, not a full substitute for testing against `google/gemma-4-31B-it`. I'm noting it here rather than claiming coverage I don't have. |

## Questions you sent us

| Question | Date sent | What you did in the meantime |
|---|---|---|
| | | |