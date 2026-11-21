"""Background comparison of single-GPU MTP decode; leaves user config intact."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tools')]
import calibrate_draft as CD
from serve.server import StrataEngine, child_env, engine_args

OUT = Path(sys.argv[1])
STATE = {'pid': os.getpid(), 'state': 'running', 'stage': 'initializing'}
INFO = {}
MEMORY = {'off': [], 'on': []}
STOP = threading.Event()
STAGE = 'off'


def save(**values):
    STATE.update(values)
    STATE['updated'] = time.strftime('%Y-%m-%d %H:%M:%S')
    temp = OUT / 'status.tmp'
    temp.write_text(json.dumps(STATE, indent=2), encoding='utf-8')
    temp.replace(OUT / 'status.json')


def say(message):
    print(message, flush=True)
    save(stage=message)


def monitor():
    while not STOP.is_set():
        try:
            value = subprocess.check_output(
                ['nvidia-smi', '--id=0', '--query-gpu=memory.used', '--format=csv,noheader,nounits'],
                creationflags=subprocess.CREATE_NO_WINDOW, timeout=10, text=True)
            MEMORY[STAGE].append(int(value.strip()))
        except Exception:
            pass
        STOP.wait(5)


def start_engine(cfg):
    global STAGE
    STAGE = 'on' if cfg['env']['MAYA_GLM_MTP_DECODE'] == '1' else 'off'
    eng = StrataEngine(cfg['exe'], engine_args(cfg), cwd=cfg.get('cwd'),
                       log=str(OUT / (STAGE + '-engine.log')), env=child_env(cfg))
    INFO[STAGE] = dict(eng.info or {})
    (OUT / 'engine-info.json').write_text(json.dumps(INFO, indent=2), encoding='utf-8')
    if STAGE == 'on' and str(INFO[STAGE].get('mtp', '')).lower() not in ('1', 'true', 'on'):
        import calibrate_glm
        calibrate_glm.close(eng)
        raise RuntimeError('MTP requested but engine INFO does not confirm mtp=1; see on-engine.log')
    return eng


if __name__ == '__main__':
    try:
        OUT.mkdir(parents=True, exist_ok=True)
        save()
        tasks = subprocess.check_output(['tasklist', '/FI', 'IMAGENAME eq maya.exe', '/FO', 'CSV', '/NH'],
                                        creationflags=subprocess.CREATE_NO_WINDOW, text=True)
        if 'maya.exe' in tasks.lower():
            raise RuntimeError('A Maya engine is already running; benchmark stopped to avoid GPU contention')
        cfg = json.loads((ROOT / 'maya-maya-s24.json').read_text(encoding='utf-8'))
        cfg['sampling'] = {'temperature': 0.0, 'top_p': 1.0}
        off, on = copy.deepcopy(cfg), copy.deepcopy(cfg)
        for c, value in ((off, '0'), (on, '1')):
            c.setdefault('env', {})['MAYA_GLM_MTP_DECODE'] = value
            c['env']['MAYA_GLM_MTP_DRAFT'] = '3'
            c['env']['MAYA_GLM_NO_MTP'] = '0'
            c['env']['MAYA_GLM_NO_SPEC'] = '0'
        (OUT / 'off-config.json').write_text(json.dumps(off, indent=2), encoding='utf-8')
        (OUT / 'on-config.json').write_text(json.dumps(on, indent=2), encoding='utf-8')
        threading.Thread(target=monitor, daemon=True).start()
        result = CD.run(off, on, say=say, start_engine=start_engine,
                        other_label='MTP off', draft_label='MTP on',
                        size={'mixed': True, 'tokens': 1024, 'answer': 256, 'warm': 64})
        report = {'off': result['other'], 'on': result['dflash2'], 'gain': result['gain'],
                  'recommendation': 'on' if result['keep'] == 'dflash2' else 'off',
                  'peak_gpu_mib': {k: max(v) if v else None for k, v in MEMORY.items()},
                  'engine_info': INFO, 'sampling': cfg['sampling'],
                  'note': 'Four mixed prompts, up to 256 tokens each; warm-up excluded; off then on; one pass.'}
        (OUT / 'result.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
        save(state='complete', stage='finished', result=str(OUT / 'result.json'))
    except Exception as exc:
        save(state='failed', error=str(exc))
        raise
    finally:
        STOP.set()
