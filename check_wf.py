import os
for wf in ['.github/workflows/ci.yml','.github/workflows/release.yml','.github/workflows/security.yml']:
    content = open(wf, encoding='utf-8').read()
    bad = [(i, hex(ord(c)), repr(c)) for i, c in enumerate(content) if ord(c) > 127]
    if bad:
        print(wf, 'HAS NON-ASCII:', bad[:15])
    else:
        print(wf, 'clean')
