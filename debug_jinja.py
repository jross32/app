import re
from pathlib import Path
text = Path('templates/profile_detail.html').read_text()
pattern = re.compile(r"\{%\s*(if|for|elif|else|endif|endfor)[^%]*%}\s*")
stack = []
for match in pattern.finditer(text):
    token = match.group(1)
    line = text.count('\n', 0, match.start()) + 1
    if token in ('if', 'for'):
        stack.append((token, line))
    elif token == 'endif':
        while stack and stack[-1][0] in ('elif', 'else'):
            stack.pop()
        if stack and stack[-1][0] == 'if':
            stack.pop()
        else:
            print('unexpected endif at', line, 'stack', stack)
            break
    elif token == 'endfor':
        if stack and stack[-1][0] == 'for':
            stack.pop()
        else:
            print('unexpected endfor at', line, 'stack', stack[-1] if stack else None)
            break
    elif token in ('elif', 'else'):
        while stack and stack[-1][0] in ('elif', 'else'):
            stack.pop()
        if stack and stack[-1][0] == 'if':
            stack.append((token, line))
        else:
            print('orphan', token, 'at', line)
            break
else:
    print('stack remains', stack)
