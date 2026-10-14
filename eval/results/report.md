# Evaluation report

Chat run 2026-10-12 15:01 UTC; report generated 2026-10-14 01:16 UTC. Models: extraction `gpt-5-mini`, chat `gpt-5-mini` / `gpt-5-mini`, AI grader `gpt-5-mini` (judge-v3); search `lightonai/answerai-colbert-small-v1`. Cached model output was not reused.

## Results

- **Table extraction:** 95 of 100 field values correct (**95%**, 95% interval 89%–98%) across 10 documents. Of the values the table filled in, 94% were right (precision); of the values the documents clearly state, it got 98% (recall). It invented a value for **4 of 38** fields the document doesn't state. Correct values cite the label's page 100% of the time; 100% of evidence quotes were found word for word in the page text.
- **Chat answers:** 6 of 6 correct, graded by a person. 6 of 6 answers were fully supported by the pages they cite.
- **Refusals:** declined 3 of 3 questions the documents can't answer, and wrongly declined 0 of 7 answerable questions.
- **Finding the source:** the expected page was among the retrieved passages or the evidence quotes shown for 6 of 6 expected pages, and the answer cited the expected document 6 of 6 times. The list question returned contracts with precision 100% and recall 100%.
- **Speed and cost:** median 4.04s per answer (p90 7.55s), $0.0010 per question.
- The AI grader agreed with the person on 9 of 9 correctness grades.

## What was tested

- **Extraction:** 100 field labels, 10 decision-critical fields on each of 10 documents drawn at random (fixed seed) by document type from files not used in earlier testing, covering agreements, renewals, an amendment, an award letter, a vendor disclosure, a lease and a task order, including scanned files. An AI assistant drafted each label with its page and quote; a person checked every one against the PDF. 0 labels were changed after results were seen (logged in the label file).
- **Chat:** 10 questions with reference answers: 2 answered from the table, 4 clause lookups, 1 needing both, and 3 the system should decline (a vendor that doesn't exist, a clause the contract doesn't have, an off-topic question).

## Table extraction by field

| field                       |   labels | accuracy   |   invented |   missed |   wrong |
|:----------------------------|---------:|:-----------|-----------:|---------:|--------:|
| Document type               |       10 | 100%       |          0 |        0 |       0 |
| Vendor                      |       10 | 100%       |          0 |        0 |       0 |
| Effective date              |       10 | 90%        |          1 |        0 |       0 |
| Expiration date             |       10 | 100%       |          0 |        0 |       0 |
| Auto-renewal                |       10 | 100%       |          0 |        0 |       0 |
| Renewal notice (days)       |       10 | 100%       |          0 |        0 |       0 |
| Award amount (USD)          |       10 | 90%        |          0 |        1 |       0 |
| Termination for convenience |       10 | 90%        |          1 |        0 |       0 |
| Termination notice (days)   |       10 | 90%        |          1 |        0 |       0 |
| Governing law               |       10 | 90%        |          1 |        0 |       0 |

| text    |   documents |   labels | accuracy   | precision   | recall   |
|:--------|------------:|---------:|:-----------|:------------|:---------|
| native  |           5 |       50 | 98%        | 100%        | 96%      |
| scanned |           5 |       50 | 92%        | 90%         | 100%     |

Extraction failures:

- **23018_Fully_Executed_Agreement_3_16_23.pdf, Award amount (USD)**: left blank: expected 48000, got not stated — The fee table on p.2 is an image on a page that also has a text layer, so the page was not OCR'd and $48,000 never reached the model.
- **2022__2024_Vendor_Disclosure_Statement.pdf, Effective date**: invented a value: expected not stated, got 2022-06-09 — Took the form's signature date (June 9, 2022) as an effective date; a vendor disclosure has no contract term.
- **23173_1_Task_Order_A__Fully_Executed.pdf, Termination for convenience**: invented a value: expected not stated, got true — Read the termination clause from the attached subconsultant terms (p.6), not from the County's task order.
- **23173_1_Task_Order_A__Fully_Executed.pdf, Termination notice (days)**: invented a value: expected not stated, got 30 — Same clause; the 30-day notice is in the subconsultant terms on p.6.
- **23173_1_Task_Order_A__Fully_Executed.pdf, Governing law**: invented a value: expected not stated, got Illinois — Same attachment; p.6 names the law of the subconsultant's place of business, which the model turned into Illinois.

## Chat cases

| Case   | Question                                                                                            | Correct   | Supported   | Declined             | Source found         | Cited   |   Seconds |
|:-------|:----------------------------------------------------------------------------------------------------|:----------|:------------|:---------------------|:---------------------|:--------|----------:|
| q01    | Which contracts have auto-renewal clauses?                                                          | correct   | yes         | no                   | list P 100% / R 100% |         |       4.2 |
| q02    | Which contracts expire in 2026?                                                                     | n/a       | n/a         | no                   | filter ok            |         |       3.1 |
| q03    | How much notice does the County need to give to cancel the Grey Wall Veoci agreement without cause? | correct   | yes         | no                   | 1/1                  | 1/1     |       5.7 |
| q04    | What is the total not-to-exceed cost of Appin Associates agreement 21163, including travel?         | correct   | yes         | no                   | 1/1                  | 1/1     |       3.2 |
| q05    | How much did Modification 2 to agreement 21144 add, and what is the new total?                      | correct   | yes         | no                   | 1/1                  | 1/1     |       6.5 |
| q06    | What renewal options does the Pat Davis Design Group agreement 23018 give the County?               | correct   | yes         | no                   | 1/1                  | 1/1     |       4.7 |
| q07    | For the Motorola tower site leases, can Motorola terminate early, and on how much notice?           | correct   | yes         | no                   | 2/2 (rank 5)         | 2/2     |      16.6 |
| q08    | What does the Starbucks coffee supply agreement say about price increases?                          | correct   | n/a         | yes (should decline) |                      |         |       3.9 |
| q09    | What liability cap applies to Ciosek Tree Service's tree removal contract?                          | correct   | n/a         | yes (should decline) |                      |         |       3.4 |
| q10    | Who won the World Series last year?                                                                 | correct   | n/a         | yes (should decline) |                      |         |       1.8 |

Chat failures:

No failures.

## Reviewer notes

- This run follows fixes to the six chat failures of the previous run, which is kept with its notes in
  `run2_before_scope_fix/`. All 10 questions were run again from scratch. A person graded all 10 answers against
  the reference answers and the PDFs; the AI grader's verdicts are kept alongside for comparison.
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

## How it is measured, and why

- **Extraction** is compared by code, not an AI grader, so the score is reproducible: amounts within 0.5%, dates, day counts, yes/no and document type exactly, vendor names ignoring suffixes like "Inc.". Precision and recall are separate because the errors cost differently: a wrong value misleads, a blank only sends the reader to the PDF. The invented-value rate is the hallucination measure. Page accuracy and verified quotes show whether a value can be checked.
- **Chat** is graded by a person against the reference: *correct* (the key facts) and *supported* (every claim backed by a cited page; not applicable when it declines), because a right answer with no support can't be trusted. *Source found / cited* separates search failures from answer-writing failures. *Refusals* are scored both ways, since a system that refused everything would ace the unanswerable questions. Latency and cost are reported because a slow or expensive answer fails in use. The route is logged for diagnosis, not scored.

## Monitoring in production

- **Drift signals already logged.** Per answer: route, retrieved pages and scores, refusal flag, latency, cost. Per extracted value: quote, page, confidence, and whether the quote appears in the page text. Track weekly: refusal rate, uncited answers, top retrieval score, fill rate and quote-verified rate per field, OCR confidence. A drop in one field suggests new templates or clause wording; a drop across fields points to the model or prompt.
- **Release gate and reruns.** Rerun this set fresh on every prompt, model or parser change, and weekly with no change to catch provider-side drift; block a release that lowers accuracy or refusals or raises invented values. Prompts are versioned in the database; pin dated model snapshots rather than aliases.
- **Human audit and feedback.** A person reviews a weekly sample, with the AI grader screening more; grader-human agreement decides how far the grader is trusted. Answers users mark wrong and documents unlike anything in the test set become new test cases.

## Limitations

- The sample is small: one chat question moves chat accuracy by 17 points, and the 95% interval on extraction accuracy is 89%–98%. Read the numbers as a map of where the pipeline fails, and the harness as something to grow, not as a precise score.
- An AI assistant drafted the labels and a person verified them, so the draft can anchor the reviewer.
- Free-text fields (description, renewal options, liability cap, insurance) are not scored here because they need judgment; the chat questions cover some of the same clauses.
- Each question was graded on one run, and chat answers change between runs. Running each question several times and reporting how often the outcome holds would give a steadier chat score.
