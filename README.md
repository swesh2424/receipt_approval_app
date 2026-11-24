## AI tools and workflow

### Development assistance
I used Perplexity, an AI assistant, to generate the initial Flask
application scaffold, templates, SQLite persistence, and workflow
smoke test. I also used it to help interpret startup errors and
troubleshoot the local project structure.

### Receipt processing
Receipt extraction currently uses a mock service, as permitted by
the assignment. It returns synthetic example values and does not
perform OCR or inspect the receipt contents.

The employee must review and replace those values before submission.
The extraction adapter is isolated in MockExtractor.extract() so it
can be replaced with a live model or OCR service.
