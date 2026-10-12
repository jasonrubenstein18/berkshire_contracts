# Evaluation report

Chat run 2026-10-12 14:12 UTC; report generated 2026-10-12 14:14 UTC. Models: extraction `gpt-5-mini`, chat `gpt-5-mini` / `gpt-5-mini`, AI grader `gpt-5-mini` (judge-v3); search `lightonai/answerai-colbert-small-v1`. Cached model output was not reused.

## Results

- **Table extraction:** 95 of 100 field values correct (**95%**, 95% interval 89%–98%) across 10 documents. Of the values the table filled in, 94% were right (precision); of the values the documents clearly state, it got 98% (recall). It invented a value for **4 of 38** fields the document doesn't state. Correct values cite the label's page 100% of the time; 100% of evidence quotes were found word for word in the page text.
- **Chat answers:** 2 of 6 correct and 3 partially correct, graded by the AI grader (human grades pending). 4 of 5 answers were fully supported by the pages they cite.
- **Refusals:** declined 3 of 3 questions the documents can't answer, and wrongly declined 1 of 7 answerable questions.
- **Finding the source:** the expected page was among the retrieved passages or the evidence quotes shown for 4 of 6 expected pages, and the answer cited the expected document 4 of 6 times. The list question returned contracts with precision 100% and recall 89%.
- **Speed and cost:** median 4.04s per answer (p90 11.79s), $0.0013 per question.
- No human grades yet, so grader agreement isn't measured.

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

| Case   | Question                                                                                            | Correct           | Supported   | Declined             | Source found        | Cited   |   Seconds |
|:-------|:----------------------------------------------------------------------------------------------------|:------------------|:------------|:---------------------|:--------------------|:--------|----------:|
| q01    | Which contracts have auto-renewal clauses?                                                          | partially correct | yes         | no                   | list P 100% / R 89% |         |       4.2 |
| q02    | Which contracts expire in 2026?                                                                     | n/a               | n/a         | no                   | filter ok           |         |       3.5 |
| q03    | How much notice does the County need to give to cancel the Grey Wall Veoci agreement without cause? | incorrect         | n/a         | yes                  | 0/1                 | 0/1     |      13.3 |
| q04    | What is the total not-to-exceed cost of Appin Associates agreement 21163, including travel?         | partially correct | yes         | no                   | 1/1                 | 1/1     |       3.9 |
| q05    | How much did Modification 2 to agreement 21144 add, and what is the new total?                      | correct           | yes         | no                   | 1/1                 | 1/1     |       6.3 |
| q06    | What renewal options does the Pat Davis Design Group agreement 23018 give the County?               | correct           | no          | no                   | 1/1                 | 1/1     |       3.9 |
| q07    | For the Motorola tower site leases, can Motorola terminate early, and on how much notice?           | partially correct | yes         | no                   | 1/2 (rank 3)        | 1/2     |      11.6 |
| q08    | What does the Starbucks coffee supply agreement say about price increases?                          | correct           | n/a         | yes (should decline) |                     |         |       2.5 |
| q09    | What liability cap applies to Ciosek Tree Service's tree removal contract?                          | correct           | n/a         | yes (should decline) |                     |         |       7.3 |
| q10    | Who won the World Series last year?                                                                 | correct           | n/a         | yes (should decline) |                     |         |       2.1 |

Chat failures:

- **q01**: partially correct; missed 1 of the expected contracts — The Axon contract is 129 pages, so extraction reads retrieved excerpts. They included the p.97 clause ("will automatically renew for an additional 5 years"), but the model recorded auto-renewal as false, citing a Sourcewell extension clause on p.1 instead.
- **q03**: declined an answerable question; incorrect; expected page retrieved 0/1; expected document cited 0/1 — The planner scoped to a vendor named 'Grey Wall Veoci' (vendor plus product), which matched no record, so the table's correct 60-day value was skipped; the fallback search over all documents did not retrieve p.10, and the answer declined.
- **q04**: partially correct — Vendor scoping matched 'Associates' loosely and pulled in other vendors' contracts, and the planner chose a sum, so the headline added three contracts ($514,587); the 21163 row itself shows the right $152,550.
- **q06**: claims not backed by the cited pages — cause not yet reviewed
- **q07**: partially correct; expected page retrieved 1/2; expected document cited 1/2 — The Waukegan lease is recorded as an 'agreement' even though its own evidence quote says 'site lease', so the planner's lease filter dropped it and only Libertyville was answered.

## Reviewer notes

- The pattern: the table is mostly right, but the chat layer loses that accuracy. Of the six chat failures, three
  come from how the planner scopes the question (q03 matched no vendor, q04 matched too many, q07 filtered out a
  lease), and in q03 the table already held the right answer. Two come from the answer step (q05 ignored the page
  it retrieved; q08 answered about a document that doesn't exist). One is an extraction miss in a 129-page
  document (q01).
- Every failure was reviewed against the PDF. All of them are pipeline errors; no label was found to be wrong, so
  none was changed after the results were seen.
- A first chat run was discarded before grading: the scorer did not credit answers from the contracts table for
  the evidence pages they quote. After the fix, all 10 questions were run again from scratch, and that run is the
  one graded here.
- The two runs differed. In the first, q05 was answered correctly from the table and q08 (Starbucks) was declined;
  in the graded run both failed. With one run per question, a single chat result is a sample, not a fixed property.
- The AI grader judged the same answers as the person, without seeing the person's grades, so its agreement
  measures how far it can be trusted for routine monitoring.

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
