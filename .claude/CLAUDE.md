## STRICT: Python execution rules
- NEVER use python -c with inline comments
- ALWAYS write multi-line Python to a temp .py file, run it, then delete it
- This is non-negotiable. No exceptions.


## Bash rules
- Never put comments inside `python -c` quoted strings
- For multi-line Python, write to a temp .py file and execute it instead of using `-c`
