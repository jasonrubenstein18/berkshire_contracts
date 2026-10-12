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
