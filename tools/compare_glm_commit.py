"""Run one phase of the Windows commit-budget experiment, twice, in background."""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tools')]
import calibrate_draft as CD
import calibrate_glm as CAL
from serve.server import StrataEngine, child_env, engine_args


def write(path, value):
    temp = path.with_suffix('.tmp')
    with temp.open('w', encoding='utf-8') as f:
        json.dump(value, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    temp.replace(path)


def snapshot():
    script = """[ordered]@{
      computer = Get-CimInstance Win32_ComputerSystem | Select-Object TotalPhysicalMemory,AutomaticManagedPagefile
      settings = @(Get-CimInstance Win32_PageFileSetting | Select-Object Name,InitialSize,MaximumSize)
      usage = @(Get-CimInstance Win32_PageFileUsage | Select-Object Name,AllocatedBaseSize,CurrentUsage,PeakUsage)
      memory = Get-CimInstance Win32_PerfFormattedData_PerfOS_Memory | Select-Object AvailableBytes,CommittedBytes,CommitLimit,PagesInputPersec
      boot = (Get-CimInstance Win32_OperatingSystem).LastBootUpTime.ToString('o')
    } | ConvertTo-Json -Depth 5"""
    data = subprocess.check_output(['powershell.exe', '-NoProfile', '-Command', script],
                                   creationflags=subprocess.CREATE_NO_WINDOW, timeout=40)
    return json.loads(data.decode('utf-8-sig'))


def api(base, route, post=False):
    request = urllib.request.Request(base + route, data=b'{}' if post else None,
                                     headers={'Content-Type': 'application/json'})
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=240) as response:
        return json.loads(response.read())


def git(*args):
    return subprocess.check_output(['git', *args], cwd=ROOT)


def run(out, phase, say):
    cfg_path = ROOT / 'maya-maya-s24.json'
    prompts_path = ROOT / 'bench/prompts/glm-commit.json'
    cfg = json.loads(cfg_path.read_text(encoding='utf-8'))
    cfg['sampling'] = {'temperature': 0.0, 'top_p': 1.0}
    cfg.setdefault('env', {}).update(MAYA_GLM_MTP_DECODE='0', MAYA_GLM_SLOTS='0')
    system = snapshot()
    write(out / 'system-before.json', system)
    exe_hash = hashlib.sha256(Path(cfg['exe']).read_bytes()).hexdigest()
    identity = {'head': git('rev-parse', 'HEAD').decode().strip(),
                'exe_sha256': exe_hash,
                'config_sha256': hashlib.sha256(cfg_path.read_bytes()).hexdigest(),
                'prompts_sha256': hashlib.sha256(prompts_path.read_bytes()).hexdigest()}
    write(out / 'identity.json', identity)
    if phase == 'B':
        a = json.loads((out.parent / 'A/identity.json').read_text(encoding='utf-8'))
        if a != identity:
            raise RuntimeError('Code, executable, config or prompts changed since A; stop for review')
        baseline = json.loads((out.parent / 'A/result.json').read_text(encoding='utf-8'))
        old_limit = int(baseline['system_before']['memory']['CommitLimit'])
        if int(system['memory']['CommitLimit']) < old_limit + 17 * 1024**3:
            raise RuntimeError('Commit limit has not increased by 17 GiB; restart Windows after applying the pagefile setting')
    (out / 'worktree.patch').write_bytes(git('diff', '--binary', 'HEAD'))
    (out / 'worktree-status.txt').write_bytes(git('status', '--short'))
    (out / 'runner-snapshot.py').write_bytes(Path(__file__).read_bytes())
    (out / 'prompts.json').write_bytes(prompts_path.read_bytes())
    write(out / 'config.json', cfg)
    CD.MIXED = tuple((p['text'], p['thinking']) for p in json.loads(prompts_path.read_text(encoding='utf-8')))
    warm, questions, _ = CD.requests(cfg, mixed=True)
    write(out / 'prompt-token-ids.json', {'warm': warm, 'questions': questions})
    if phase == 'B':
        if json.loads((out.parent / 'A/prompt-token-ids.json').read_text(encoding='utf-8')) != {'warm': warm, 'questions': questions}:
            raise RuntimeError('Tokenized prompts changed since A')
    base = 'http://127.0.0.1:' + str(cfg.get('port', 8080))
    restore = False
    runs = []
    try:
        try:
            health = api(base, '/health')
        except OSError:
            health = None
        if health and not health.get('unloaded'):
            activity = api(base, '/status')
            if activity.get('busy') or activity.get('queued'):
                raise RuntimeError('Server has active requests; refusing to interrupt them')
            say('Unloading idle GLM server; it will be restored when this phase ends')
            api(base, '/models/unload', post=True)
            restore = True
        for repetition in range(1, 3):
            tasks = subprocess.check_output(['tasklist', '/FI', 'IMAGENAME eq maya.exe', '/FO', 'CSV', '/NH'],
                                            creationflags=subprocess.CREATE_NO_WINDOW)
            if b'maya.exe' in tasks.lower():
                raise RuntimeError('Another Maya engine is running; refusing GPU contention')
            run_dir = out / ('run-' + str(repetition))
            run_dir.mkdir()
            c = copy.deepcopy(cfg)
            c['args'] = CAL.without_writes(c['args'])
            with tempfile.TemporaryDirectory(prefix='maya-commit-') as tmp:
                c['env'].update(CAL.usage_copy(cfg, Path(tmp)))
                say(f'{phase}{repetition}: loading GLM with MTP off')
                started = time.monotonic()
                eng = StrataEngine(c['exe'], engine_args(c), cwd=c['cwd'],
                                   log=str(run_dir / 'engine.log'), env=child_env(c))
                load_s = time.monotonic() - started
                try:
                    info = dict(eng.info)
                    if int(info.get('mtp', 0)) != 0:
                        raise RuntimeError('Engine unexpectedly enabled MTP')
                    write(run_dir / 'engine-info.json', info)
                    say(f'{phase}{repetition}: warming up (excluded)')
                    CD.answer(eng, warm, 64, c['sampling'], 1)
                    rows = []
                    for i, ids in enumerate(questions):
                        n, last = CD.answer(eng, ids, 256, c['sampling'], i + 2)
                        rows.append({'prompt_index': i, 'tokens': n, 'metrics': last})
                        write(run_dir / 'requests.json', rows)
                        ms = float(last.get('decode_ms') or 0)
                        say(f'{phase}{repetition} prompt {i+1}: {n} tokens, {n/ms*1000 if ms else 0:.2f} tok/s')
                    tokens = sum(r['tokens'] for r in rows)
                    seconds = sum(float(r['metrics'].get('decode_ms') or 0) for r in rows) / 1000
                    if tokens < 512 or seconds <= 0:
                        raise RuntimeError('Too few measured tokens or missing decode timings')
                    runs.append({'tokens': tokens, 'decode_seconds': seconds, 'tok_s': tokens/seconds,
                                 'load_seconds': load_s, 'engine_info': info, 'requests': rows})
                    write(out / 'partial-results.json', runs)
                finally:
                    CAL.close(eng)
        total_tokens = sum(r['tokens'] for r in runs)
        total_seconds = sum(r['decode_seconds'] for r in runs)
        report = {'phase': phase, 'identity': identity, 'runs': runs, 'tok_s': total_tokens/total_seconds,
                  'system_before': system, 'system_after': snapshot(),
                  'method': 'Two fresh engine starts; warm-up excluded; four mixed prompts, up to 256 tokens each; MTP off; temperature 0.'}
        write(out / 'result.json', report)
        if phase == 'B':
            baseline = json.loads((out.parent / 'A/result.json').read_text(encoding='utf-8'))
            write(out.parent / 'comparison.json', {'A_tok_s': baseline['tok_s'], 'B_tok_s': report['tok_s'],
                  'gain': report['tok_s']/baseline['tok_s']-1,
                  'note': 'Sequential A,A then B,B across a system setting change; not interleaved.'})
    finally:
        if restore:
            say('Restoring the previously loaded GLM server')
            api(base, '/models/load', post=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--phase', choices=['A', 'B'], required=True)
    args = parser.parse_args()
    folder = args.out / args.phase
    folder.mkdir(parents=True, exist_ok=True)
    state = {'pid': os.getpid(), 'phase': args.phase}
    def say(message):
        print(message, flush=True)
        state.update(state='running', stage=message, updated=time.strftime('%Y-%m-%d %H:%M:%S'))
        write(folder / 'status.json', state)
    try:
        if (folder / 'result.json').exists():
            raise RuntimeError('Results already exist; refusing to overwrite this phase')
        say('Collecting environment and verifying experiment conditions')
        run(folder, args.phase, say)
        state.update(state='complete', stage='finished', updated=time.strftime('%Y-%m-%d %H:%M:%S'))
        write(folder / 'status.json', state)
    except Exception as exc:
        state.update(state='failed', error=str(exc), updated=time.strftime('%Y-%m-%d %H:%M:%S'))
        write(folder / 'status.json', state)
        raise
