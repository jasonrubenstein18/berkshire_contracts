- This run follows fixes to the six chat failures of the previous run, which is kept with its notes in
  `run2_before_scope_fix/`. All 10 questions were run again from scratch, and the AI grader marked every graded
  answer correct. A person has not graded this run yet.
- What changed:
  - Scoping: a named contract number now outranks the vendor name (q04). Vendor names match only when every word
    matches, so "Appin Associates" no longer pulls in other "Associates" firms. A vendor plus product name such as
    "Grey Wall Veoci" matches that vendor's documents that mention the product (q03).
  - Filters the planner adds to describe a named contract (document type, description) are dropped when they
    remove all of its documents (q05). A named vendor with no extracted record is searched only in the documents
    that mention it.
  - Extraction: two documents titled as leases were stored as agreements (q07), and Axon's "will automatically
    renew" clause on p.97 was recorded as no auto-renewal (q01). Two narrow rules fixed both, and only those three
    documents were re-extracted. None of them is in the extraction test set.
- q03, q04, q05, q07 and q09 were also asked several more times without cached output. q03, q04, q07 and q09 were
  right every time. q05 failed two of three times until the descriptive-filter fix, then passed five of five.
