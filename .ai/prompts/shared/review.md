# Code Review Prompt — Trade-Bot-main

Review the provided code changes with these lenses, in order:

## 1. Correctness
- Logic errors, off-by-one, wrong operators
- Incorrect use of async/await or thread safety issues
- Financial calculation precision (use Decimal, not float for money)

## 2. Security
- Secrets or credentials exposed
- SQL/shell injection vectors
- Unvalidated external input

## 3. Performance
- N+1 queries, redundant I/O, blocking calls in async context
- Memory leaks or unbounded data structures

## 4. Maintainability
- Dead code, over-engineering, missing docstrings on public APIs
- Type annotation gaps

## 5. Test Coverage
- Happy path tested?
- Edge cases: empty, None, negative values, max values
- Error paths tested?

## Output Format
```
ISSUE: <description>
LOCATION: <file>:<line>
SEVERITY: critical|high|medium|low
FIX: <concrete suggestion>
```
List all issues, then summarize: APPROVE / REQUEST_CHANGES / NEEDS_DISCUSSION
