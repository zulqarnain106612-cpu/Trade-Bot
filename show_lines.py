from pathlib import Path

files = {
    'src/features/pipeline.py': range(520, 741),
    'src/risk/gates.py': range(1, 340),
    'src/engine/signal_engine.py': range(1, 170),
    'src/api/main.py': range(1, 120),
    'src/config.py': range(1, 120),
    'src/execution/live.py': range(1, 90),
    'src/engine/orchestrator.py': range(1, 90),
    'src/regime/detector.py': range(1, 120),
    'src/risk/kelly.py': range(1, 90),
    'src/data/fetcher.py': range(1, 60),
    'src/data/storage.py': range(1, 30),
}

out = []
for path, lines in files.items():
    p = Path(path)
    if not p.exists():
        out.append(f'MISSING {path}')
        continue
    out.append(f'--- {path}')
    text = p.read_text(encoding='utf-8').splitlines()
    for i in lines:
        if i - 1 < len(text):
            out.append(f'{i:4d}: {text[i-1]}')
    out.append('')
Path('diagnostics_output.txt').write_text('\n'.join(out), encoding='utf-8')
