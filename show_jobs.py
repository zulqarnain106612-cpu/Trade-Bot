import json
with open('jobs.json') as f:
    d = json.load(f)
for j in d['jobs']:
    print(j['name'], '|', j['conclusion'], '|', j['started_at'], '->', j['completed_at'])
    for s in j['steps']:
        sa = s.get('started_at','?')
        ca = s.get('completed_at','?')
        print(' ', s['conclusion'], '|', s['name'], '|', sa, '->', ca)
