# Dependencies

Every library you added to `requirements-extra.txt`, with a one-line reason.

We are not counting libraries — a well-chosen dependency is better engineering than a
hand-rolled version of the same thing. What we are reading is whether you added each
one deliberately.

| Library | Version | Why |
|---|---|---|

No additions to `requirements-extra.txt`. Everything the pipeline needed —
HTTP (`httpx`), XML parsing (`lxml`), columnar output (`pyarrow`), the LLM client
(`openai`), and env loading (`python-dotenv`) — was already covered by the frozen
`requirements.txt`.

## Anything you considered and rejected

Considered a dedicated 13F-parsing library, but the challenge is graded on the parsing
itself, and the frozen requirements plus `lxml` were already sufficient for
namespace-agnostic XML traversal — adding one would have hidden the part being graded
rather than helped it.

## Note

Libraries that wrap 13F retrieval and parsing end to end will not, on their own,
satisfy the schema or the quality report, and we will ask you to explain the edge cases
in your output regardless of how you produced it. If you can explain it, you own it.