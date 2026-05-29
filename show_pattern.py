from pathlib import Path
import re

patterns = [
    r'def build_feature_matrix',
    r'def build_inference_features',
    r'def check_',
    r'class FeatureSettings',
    r'class RiskSettings',
    r'cfg: .*Optional',
    r'cfg: .*\| None',
    r'cfg is None',
    r'get_settings\(\)\.features',
    r'open_fetcher',
]
files = [
    'src/features/pipeline.py',
    'src/risk/gates.py',
    'src/engine/signal_engine.py',
    'src/api/main.py',
    'src/config.py',
    'src/execution/live.py',
    'src/engine/orchestrator.py',
    'src/regime/detector.py',
    'src/risk/kelly.py',
    'src/data/fetcher.py',
    'src/data/storage.py',
]
out = []
for file in files:
    p = Path(file)
    if not p.exists():
        out.append(f'MISSING {file}')
        continue
    out.append(f'--- {file}')
    lines = p.read_text(encoding='utf-8').splitlines()
    for i, line in enumerate(lines, start=1):
        if any(re.search(pattern, line) for pattern in patterns):
            out.append(f'{i:4d}: {line}')
    out.append('')
Path('pattern_output.txt').write_text('\n'.join(out), encoding='utf-8')
