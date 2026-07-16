# Task: Investigate and Fix `Live bars insufficient (0 bars)`

## Objective

Systematically diagnose and fix the recurring
`Live bars insufficient (0 bars)` issue. Do not apply superficial
changes. Trace the root cause.

## Investigation Checklist

1.  Identify every function responsible for:
    -   Requesting live bars
    -   Building start/end timestamps
    -   Selecting the market data feed
    -   Polling frequency
    -   Strategy warm-up requirements
2.  Instrument the code with detailed logging before making changes.
    Log:
    -   Symbol
    -   Timeframe
    -   Feed
    -   Extended-hours flag
    -   Start/end timestamps
    -   Requested bar count
    -   Returned bar count
    -   Raw provider response (or error)
    -   Market open/closed state
3.  Verify the following likely causes:
    -   Polling faster than completed bars become available.
    -   Wrong market data feed (e.g. `iex` vs `sip`).
    -   Invalid or empty time window due to timestamp calculations.
    -   Startup requesting more history than exists.
    -   Market closed or extended-hours mismatch.
    -   Invalid or delisted symbol.
    -   Time zone conversion bugs.
    -   API/network errors hidden behind empty responses.
4.  Review retry behavior:
    -   Retry transient failures with exponential backoff.
    -   Distinguish "no new bar yet" from genuine failures.
5.  Improve resilience:
    -   Cache previous bars.
    -   Reuse cached data when a single poll returns zero bars.
    -   Avoid evaluating strategies until sufficient history exists.
6.  After identifying the root cause:
    -   Implement the minimal, correct fix.
    -   Remove any unnecessary workarounds.
    -   Keep existing behavior unchanged where unrelated.

## Deliverables

-   Explain the root cause.
-   List every file modified.
-   Show a diff for each change.
-   Explain why each change fixes the issue.
-   Note any remaining risks or assumptions.

## Additional Things
    - Audit the entire market-data pipeline end-to-end.
    - Add unit tests for timestamp generation and bar retrieval.
    - Validate behavior across pre-market, regular market, after-hours, weekends, and holidays.
    - Ensure the bot can recover gracefully from temporary data-provider outages without interrupting trading

## Success Criteria

The bot should: - Consistently retrieve live bars when data is
available. - Correctly handle market open/close transitions. - Never
silently fail with only `Live bars insufficient (0 bars)`. - Produce
actionable logs when data cannot be retrieved.
