import json
with open('ci_jobs.json') as f:
    d = json.load(f)
print('Total jobs:', len(d['jobs']))
for j in d['jobs']:
    print(j['name'], '|', j['conclusion'], '|', j['started_at'], '->', j['completed_at'])
    for s in j['steps']:
        sa = s.get('started_at','?')
        ca = s.get('completed_at','?')
        print(' ', s['conclusion'], '|', s['name'], '|', sa, '->', ca)
