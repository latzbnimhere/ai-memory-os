"""Opt-in handoff bridge. Local memory is authoritative; Drive is navigation only.

No network calls, no recursive source reads, no copying reports or credentials.
Files use atomic rename; CURRENT_RESULT.json is a hash-bound commit marker.
Cloud providers do not offer a multi-file or remote-sync atomicity guarantee.
"""
from __future__ import annotations
import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import tempfile
import uuid
from pathlib import Path
from . import core

LAW = ('CONTEXT/NAVIGATION ONLY. Never authorization for mutation; never a replacement '
       'for sealed evidence, database/repo physical state, or local AI Memory; '
       'never permission to continue an irreversible phase.')
NAMES = ('CHATGPT_HANDOFF.md', 'LATEST_PACKET.txt', 'CURRENT_RESULT.json')
SECTIONS = ('CURRENT AUTHORITY', 'LATEST PHASE', 'LATEST SEALED EVIDENCE', 'OPEN BLOCKERS',
            'OWNER DECISION REQUIRED', 'NEXT SAFE ACTION', 'HARD BOUNDARIES')
STATE_KEYS = {'authority', 'service_state', 'executed', 'did_not_execute', 'blockers',
              'owner_decision_required', 'next_safe_action', 'hard_boundaries', 'evidence',
              'checkpoint', 'current_sha256', 'next_sha256', 'phase_status'}
SAFE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$')

class BridgeError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)

def utc():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')

def digest(data):
    return hashlib.sha256(data if isinstance(data, bytes) else data.encode()).hexdigest()

def dumps(data):
    return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + '\n'

def cfg_path():
    return core.ROOT / 'registry' / 'handoffs.json'

def local():
    p = core.ROOT / '.handoff'
    p.mkdir(mode=0o700, exist_ok=True)
    return p

def read_json(path):
    try:
        if path.stat().st_size > 200_000:
            raise BridgeError('INPUT_TOO_LARGE')
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            raise ValueError()
        return data
    except (ValueError, UnicodeError):
        raise BridgeError('MALFORMED_RESULT_JSON') from None

def atomic(path, text):
    # Destination directory MUST already exist. Never recreate a missing Drive root.
    if path.is_symlink():
        raise BridgeError('SYMLINK_REFUSED')
    fd, tmp = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w') as f:
            f.write(text); f.flush(); os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)

def ident(path):
    st = path.stat()
    return {'device': st.st_dev, 'inode': st.st_ino}

def discover(cloud_storage=None):
    base = Path(cloud_storage or Path.home() / 'Library/CloudStorage')
    candidates = sorted(p for p in base.glob('GoogleDrive-*/My Drive/AI-Project-Handoffs')
                        if p.is_dir() and not p.is_symlink())
    if len(candidates) != 1:
        raise BridgeError('DRIVE_MISSING' if not candidates else 'DRIVE_AMBIGUOUS')
    p = candidates[0]
    return {'path': str(p), 'identity': ident(p), 'my_drive_identity': ident(p.parent)}

def scan(text):
    # Reject, never redact-and-publish. No credential file is opened to build a denylist.
    if core.secret_hits_text(text):
        raise BridgeError('REFUSED_SECRET')
    patterns = [
        r'(?i)\b(?:totp(?:[_ -]?secret)?|mfa(?:[_ -]?secret)?|recovery[_ -]?codes?|api[_ -]?key|client[_ -]?secret|encryption[_ -]?(?:key|password)|private[_ -]?key|db[_ -]?password|backup[_ -]?password|access[_ -]?token|refresh[_ -]?token|secret|password|passwd)\s*["\']?\s*[:=]\s*["\']?[^\s"\',}\]]+',
        r'(?i)\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|https?)://[^\s/:]+:[^\s/@]+@',
        r'(?m)^\s*[A-Z][A-Z0-9_]{2,}\s*=\s*[^\s]+',  # raw environment values
        r'(?i)otpauth://',
    ]
    if any(re.search(rx, text) for rx in patterns):
        raise BridgeError('REFUSED_SECRET')

def config():
    c = read_json(cfg_path())
    if c.get('version') != 1 or not isinstance(c.get('projects'), list):
        raise BridgeError('INVALID_REGISTRY')
    exclusions = c.get('excluded_projects', [])
    if not isinstance(exclusions, list): raise BridgeError('INVALID_REGISTRY')
    for e in exclusions:
        if (not isinstance(e, dict) or set(e) != {'project_id', 'memory_slug', 'folder', 'reason'}
                or e['reason'] != 'EXCLUDED_BY_OWNER'
                or any(not isinstance(e[k], str) or not SAFE.fullmatch(e[k])
                       for k in ('project_id', 'memory_slug', 'folder'))):
            raise BridgeError('INVALID_REGISTRY')
    # Explicit owner exclusion wins even if a stale registration is restored.
    c['projects'] = [p for p in c['projects'] if not any(
        excluded(c, p.get(k)) for k in ('project_id', 'display_name', 'folder', 'memory_slug'))]
    seen, slugs = set(), set()
    for p in c['projects']:
        for k in ('project_id', 'display_name', 'folder'):
            if not isinstance(p.get(k), str) or not SAFE.fullmatch(p[k]):
                raise BridgeError('PROJECT_MAPPING_REQUIRED')
        if p['project_id'] in seen or p['folder'] in seen:
            raise BridgeError('PROJECT_MAPPING_REQUIRED')
        seen.update((p['project_id'], p['folder']))
        s = p.get('memory_slug')
        if s is not None:
            if not SAFE.fullmatch(s) or s in slugs:
                raise BridgeError('PROJECT_MAPPING_REQUIRED')
            slugs.add(s)
        if set(p) - {'project_id', 'display_name', 'folder', 'memory_slug', 'active'}:
            raise BridgeError('INVALID_REGISTRY')
    return c

def excluded(c, selector):
    return isinstance(selector, str) and any(
        selector.casefold() == e[k].casefold()
        for e in c.get('excluded_projects', [])
        for k in ('project_id', 'memory_slug', 'folder'))

def drive(c):
    expected = c['drive']
    p = Path(expected['path'])
    if not p.is_dir(): raise BridgeError('DRIVE_MISSING')
    if (p.is_symlink() or p.parent.name != 'My Drive' or not p.parent.parent.name.startswith('GoogleDrive-')
            or p.name != 'AI-Project-Handoffs' or ident(p) != expected['identity']
            or ident(p.parent) != expected['my_drive_identity']):
        raise BridgeError('DRIVE_IDENTITY_MISMATCH')
    return p

def target(root, folder):
    p = root / folder
    if not p.is_dir(): raise BridgeError('PROJECT_FOLDER_MISSING')
    if p.is_symlink() or p.resolve().parent != root.resolve():
        raise BridgeError('PROJECT_MAPPING_REQUIRED')
    return p

def resolve(c, selector):
    matches = [p for p in c['projects'] if selector in
               (p['project_id'], p['display_name'], p.get('memory_slug'))]
    if len(matches) != 1: raise BridgeError('PROJECT_MAPPING_REQUIRED')
    return matches[0]

def detect(cwd):
    # Unlike the legacy detector, an equal or overlapping mapping never guesses.
    reg = read_json(core.ROOT / 'registry/projects.json')
    path = Path(cwd).resolve()
    matches = []
    for slug, entry in reg.get('projects', {}).items():
        if entry.get('repo'):
            repo = Path(entry['repo']).expanduser().resolve()
            if path == repo or repo in path.parents:
                matches.append(slug)
    return matches[0] if len(matches) == 1 else None


def queue(selector):
    q = local() / 'pending'; q.mkdir(mode=0o700, exist_ok=True)
    key = digest(selector)[:24]
    path = q / (key + '.json')
    scan(selector)
    if not SAFE.fullmatch(selector): raise BridgeError('PROJECT_MAPPING_REQUIRED')
    atomic(path, dumps({'selector': selector, 'queued_at_utc': utc(), 'status': 'PENDING'}))
    return path

def record(selector, code, pending=None):
    d = local() / 'status'; d.mkdir(mode=0o700, exist_ok=True)
    # No exception strings, source bodies, labels, or secrets in remediation records.
    key = digest(selector)[:24]
    r = {'selector_hash': key, 'updated_at_utc': utc(), 'handoff_sync': code,
         'pending': bool(pending and pending.exists()), 'cloud_sync': 'UNVERIFIED'}
    atomic(d / (key + '.json'), dumps(r))
    return r

def metadata(p):
    slug = p.get('memory_slug')
    if slug is None: return {}, {}, None
    source = core.ROOT / 'projects' / slug
    if source.is_symlink(): raise BridgeError('PROJECT_MAPPING_REQUIRED')
    man = read_json(source / 'project.json')
    if man.get('slug') != slug: raise BridgeError('PROJECT_MAPPING_REQUIRED')
    cp = man.get('current_checkpoint')
    if not cp: return man, {}, source
    if Path(cp).name != cp or cp in ('.', '..'): raise BridgeError('INVALID_CHECKPOINT')
    cpdir = source / 'checkpoints' / cp
    m = read_json(cpdir / 'meta.json')
    if m.get('id') != cp or not all(isinstance(m.get(k), str) for k in ('result', 'label', 'time', 'current_sha256')):
        raise BridgeError('MALFORMED_RESULT_JSON')
    if digest((cpdir / 'CURRENT.md').read_bytes()) != m['current_sha256']:
        raise BridgeError('CHECKPOINT_INTEGRITY_FAILED')
    if m.get('next_sha256') and digest((cpdir / 'NEXT.md').read_bytes()) != m['next_sha256']:
        raise BridgeError('CHECKPOINT_INTEGRITY_FAILED')
    # Only accepted pointer; never select an uncommitted checkpoint directory by date.
    return man, m, source

def state_path(p):
    return core.ROOT / 'registry' / 'handoff-state' / (p['project_id'] + '.json')

def validate_state(s):
    scan(dumps(s))
    if set(s) - STATE_KEYS: raise BridgeError('INVALID_STATE_FIELDS')
    for k in ('authority', 'executed', 'did_not_execute', 'blockers', 'hard_boundaries'):
        if not isinstance(s.get(k), list) or len(s[k]) > 16 or any(not isinstance(x, str) or '\n' in x or len(x) > 1800 for x in s[k]):
            raise BridgeError('INVALID_STATE_FIELDS')
    for k in ('checkpoint', 'current_sha256', 'next_sha256', 'service_state', 'owner_decision_required', 'next_safe_action', 'phase_status'):
        if not isinstance(s.get(k), str) or not s[k] or len(s[k]) > 1800 or '\n' in s[k]:
            raise BridgeError('INVALID_STATE_FIELDS')
    if s['phase_status'] not in ('PASS','FAIL','STOP','PAUSED','NEEDS_RECONCILIATION'):
        raise BridgeError('INVALID_STATE_FIELDS')
    e = s.get('evidence')
    if not isinstance(e, dict) or set(e) != {'packet_path','seal_sha256','report_path','result_path'}:
        raise BridgeError('INVALID_STATE_FIELDS')
    for v in e.values():
        if not isinstance(v,str) or '\n' in v or len(v)>1800: raise BridgeError('INVALID_STATE_FIELDS')
    if e['seal_sha256'] != 'N/A' and not re.fullmatch('[a-f0-9]{64}',e['seal_sha256']):
        raise BridgeError('INVALID_SEAL')

def build(p):
    man, meta, source = metadata(p)
    s = None
    issues = []
    if state_path(p).exists():
        candidate = read_json(state_path(p)); validate_state(candidate)
        if (candidate['checkpoint'] == meta.get('id', 'N/A') and
            candidate['current_sha256'] == (digest((source/'CURRENT.md').read_bytes()) if source else 'N/A') and
            candidate['next_sha256'] == (digest((source/'NEXT.md').read_bytes()) if source else 'N/A')):
            s = candidate
        else:
            issues.append('Structured handoff is stale: checkpoint or CURRENT/NEXT hash changed; refresh its authority binding.')
    if not s:
        issues.append('Latest bounded operational authority, service state, exclusions, blockers, owner decision and next action require reconciliation from local CURRENT/NEXT.')
        s = {'authority': [f"Registered workspace: {man.get('repo') or 'UNRESOLVED'} (registration is not physical verification)."],
             'service_state': 'UNVERIFIED; no runtime/database probe performed by the bridge.',
             'executed': [meta.get('summary') or 'Latest checkpoint persisted; operational execution details are not structured.'],
             'did_not_execute': ['Bridge ran no project commands, deployments, database changes, purchases or releases.'],
             'blockers': [], 'owner_decision_required': 'UNRESOLVED: consult local CURRENT/NEXT before proceeding.',
             'next_safe_action': 'Reconcile local CURRENT/NEXT and bind a concise handoff state to their hashes.',
             'hard_boundaries': ['No mutation or irreversible continuation based on this handoff. Consult local authority for all project-specific prohibitions.'],
             'phase_status': 'NEEDS_RECONCILIATION',
             'evidence': dict.fromkeys(('packet_path','seal_sha256','report_path','result_path'),'N/A')}
    if not meta:
        issues.append('No accepted operational AI Memory checkpoint is registered for this project.')
    if source and meta and digest((source/'CURRENT.md').read_bytes()) != meta['current_sha256']:
        issues.append('Canonical CURRENT.md differs from the accepted checkpoint; newer work is not checkpoint-accepted.')
    if source and meta.get('next_sha256') and digest((source/'NEXT.md').read_bytes()) != meta['next_sha256']:
        issues.append('Canonical NEXT.md differs from the accepted checkpoint.')
    status = 'NEEDS_RECONCILIATION' if issues else s['phase_status']
    generation = uuid.uuid4().hex
    result = {'schema_version': 1, 'project_id': p['project_id'], 'display_name': p['display_name'],
              'generation': generation, 'status': status, 'last_checkpoint': meta.get('id','N/A'),
              'phase': meta.get('label','N/A'), 'checkpoint_result': meta.get('result','N/A'),
              'checkpoint_time': meta.get('time','N/A'), 'last_updated_utc': utc(),
              'memory_version': man.get('memory_version'), 'source_current_sha256': meta.get('current_sha256','N/A'),
              'source_repo_head': (meta.get('repo_state') or {}).get('head','N/A'),
              'local_memory_path': str(source) if source else 'N/A',
              'authority': s['authority'], 'service_state': s['service_state'],
              'executed': s['executed'], 'did_not_execute': s['did_not_execute'],
              'owner_decision_required': s['owner_decision_required'], 'next_safe_action': s['next_safe_action'],
              'blocker_summary': s['blockers'] + issues, 'hard_boundaries': s['hard_boundaries'],
              'evidence': s['evidence'], 'authority_law': LAW, 'cloud_sync': 'UNVERIFIED'}
    rows = [f"# {p['display_name']} Current Handoff", '', LAW, '', f'Generation: {generation}',
            'File-set rule: validate CURRENT_RESULT.json content hashes before relying on this generation.',
            'Source facts are checkpoint/locally reported, not a fresh runtime or database verification.', '', '## CURRENT AUTHORITY']
    rows += ['- '+v for v in s['authority']]
    rows += [f"- Local AI Memory: {result['local_memory_path']}", f"- Accepted checkpoint CURRENT SHA256: {result['source_current_sha256']}",
             f"- Memory version: {result['memory_version']}", f"- Service state: {s['service_state']}", '', '## LATEST PHASE',
             f"- Exact checkpoint: {result['last_checkpoint']}", f"- Phase: {result['phase']}", f"- Status: {status}; checkpoint result: {result['checkpoint_result']}"]
    rows += ['- Executed: '+v for v in s['executed']] + ['- Did not execute: '+v for v in s['did_not_execute']]
    rows += ['', '## LATEST SEALED EVIDENCE'] + ['- '+k+': '+v for k,v in s['evidence'].items()]
    rows += ['', '## OPEN BLOCKERS'] + ['- '+v for v in (result['blocker_summary'] or ['NONE'])]
    rows += ['', '## OWNER DECISION REQUIRED', 'OWNER_DECISION_REQUIRED='+s['owner_decision_required'],
             '', '## NEXT SAFE ACTION', s['next_safe_action'], '', '## HARD BOUNDARIES']
    rows += ['- '+v for v in s['hard_boundaries']]
    md = '\n'.join(rows)+'\n'
    packet = '\n'.join(k+'='+s['evidence'][v] for k,v in [('PACKET_PATH','packet_path'),('SEAL_SHA256','seal_sha256'),('REPORT_PATH','report_path'),('RESULT_PATH','result_path')])+'\n'
    result['content_sha256'] = {'CHATGPT_HANDOFF.md': digest(md), 'LATEST_PACKET.txt': digest(packet)}
    # Scan structured fields, not generated public KEY=path/hash lines.
    scan(dumps(result))
    if len(md.splitlines()) > 150: raise BridgeError('HANDOFF_TOO_LONG')
    return {NAMES[0]:md, NAMES[1]:packet, NAMES[2]:dumps(result)}

def validate_bundle(files):
    try:
        r = json.loads(files['CURRENT_RESULT.json'])
        if any(digest(files[n]) != r['content_sha256'][n] for n in NAMES[:2]):
            raise ValueError()
        if any('## '+s not in files[NAMES[0]] for s in SECTIONS): raise ValueError()
        if LAW not in files[NAMES[0]] or len(files[NAMES[0]].splitlines()) > 150: raise ValueError()
        if len(files[NAMES[1]].splitlines()) != 4: raise ValueError()
        scan(dumps(r))
        # Markdown fields are already scanned in result; also scan non-generated text.
        scan('\n'.join(l for l in files[NAMES[0]].splitlines() if not l.startswith('OWNER_DECISION_REQUIRED=')))
        return r
    except (KeyError, ValueError, TypeError):
        raise BridgeError('MALFORMED_RESULT_JSON') from None

def read_bundle(dest):
    # Read marker twice to detect concurrent generations, then verify content hashes.
    marker = (dest / NAMES[2]).read_text()
    files = {n:(dest/n).read_text() for n in NAMES[:2]}
    files[NAMES[2]] = marker
    if (dest/NAMES[2]).read_text() != marker: raise BridgeError('GENERATION_CHANGED')
    return validate_bundle(files)

def replace_bundle(dest, files):
    # Stage locally first; validate before touching the provider. Existing extra files preserved.
    with tempfile.TemporaryDirectory(prefix='stage-', dir=local()) as tmp:
        stage = Path(tmp)
        for name, text in files.items(): atomic(stage/name, text)
        validate_bundle({n:(stage/n).read_text() for n in NAMES})
        # A failure/crash retains pending work. Readers reject mixed generations via hashes.
        for name in NAMES:
            atomic(dest/name, (stage/name).read_text())
        read_bundle(dest)

def master(c, root):
    rows = []
    for p in c['projects']:
        dest = target(root,p['folder'])
        if not (dest/NAMES[2]).exists(): continue
        r = read_bundle(dest)
        if r['project_id'] != p['project_id']: raise BridgeError('MASTER_PROJECT_MISMATCH')
        row = {k:r[k] for k in ('project_id','display_name','status','last_checkpoint','last_updated_utc','owner_decision_required','blocker_summary','next_safe_action','generation')}
        row.update(handoff_path=f"{p['folder']}/{NAMES[0]}",result_path=f"{p['folder']}/{NAMES[2]}",
                   latest_packet_path=r['evidence']['packet_path'],latest_seal=r['evidence']['seal_sha256'])
        rows.append(row)
    index = dumps({'schema_version':1,'authority_law':LAW,'projects':rows})
    lines = ['# Master Project Handoff','', LAW,'']
    for row in rows:
        if not next(p for p in c['projects'] if p['project_id']==row['project_id']).get('active',True): continue
        lines += ['## '+row['display_name'], 'PROJECT: '+row['project_id'], 'STATUS: '+row['status'],
                  'LATEST CHECKPOINT: '+row['last_checkpoint'], 'OWNER DECISION REQUIRED: '+row['owner_decision_required'],
                  'NEXT SAFE ACTION: '+row['next_safe_action'],'']
    md = '\n'.join(lines)+'\n'
    last = dumps({'updated_at_utc':utc(),'handoff_sync':'SYNCED_LOCAL','cloud_sync':'UNVERIFIED',
                  'project_count':len(rows),'content_sha256':{'PROJECT_INDEX.json':digest(index),'MASTER_HANDOFF.md':digest(md)},
                  'authority_law':LAW})
    for txt in (index, md, last): scan(txt)
    dest = target(root,'00-MASTER')
    with tempfile.TemporaryDirectory(prefix='master-',dir=local()) as tmp:
        for n,txt in [('PROJECT_INDEX.json',index),('MASTER_HANDOFF.md',md),('LAST_SYNC.json',last)]:
            atomic(Path(tmp)/n,txt)
        json.loads((Path(tmp)/'PROJECT_INDEX.json').read_text())
        for n in ('PROJECT_INDEX.json','MASTER_HANDOFF.md','LAST_SYNC.json'):
            atomic(dest/n,(Path(tmp)/n).read_text())


def publish(selector):
    pending = None
    try:
        with core.lock('handoff-global'):
            c = config()
            if excluded(c, selector):
                # Touch only bridge bookkeeping, never excluded project/Drive content.
                (local() / 'pending' / (digest(selector)[:24] + '.json')).unlink(missing_ok=True)
                return record(selector, 'EXCLUDED_BY_OWNER')
            pending = queue(selector)
            p = resolve(c,selector)
            files = build(p); validate_bundle(files)
            root = drive(c); dest = target(root,p['folder'])
            replace_bundle(dest,files)
            master(c,root)
            pending.unlink(missing_ok=True)
            return record(selector,'SYNCED_LOCAL')
    except (Exception, SystemExit) as e:
        code = e.code if isinstance(e,BridgeError) else 'FAILED'
        if code == 'REFUSED_SECRET' and pending:
            pending.unlink(missing_ok=True)
        return record(selector,code if code in ('REFUSED_SECRET','PROJECT_MAPPING_REQUIRED') else 'FAILED',pending) | {'reason':code}


def after_checkpoint(slug):
    """Non-fatal post-persistence boundary. No enabled registry means no behavioral change."""
    if not cfg_path().exists(): return
    try:
        # A caller may stage a summary for the next accepted checkpoint. Bind only
        # exact content hashes after acceptance, never to an orphan/failed checkpoint.
        try:
            with core.lock('handoff-global'):
                c = config()
                try:
                    p = resolve(c, slug)
                except BridgeError:
                    p = None
                if p and state_path(p).exists():
                    candidate = read_json(state_path(p))
                    try:
                        validate_state(candidate)
                    except BridgeError:
                        candidate = {}
                    _, meta, source = metadata(p)
                    if (candidate.get('checkpoint') == 'NEXT_ACCEPTED_CHECKPOINT' and meta and
                        candidate['current_sha256'] == meta['current_sha256'] and
                        candidate['next_sha256'] == meta.get('next_sha256')):
                        candidate['checkpoint'] = meta['id']
                        atomic(state_path(p), dumps(candidate))
        except (Exception, SystemExit):
            pass  # publish below creates the failure/pending or secret-remediation record
        r = publish(slug)
        print('HANDOFF_SYNC='+r['handoff_sync'] + (' REASON='+r['reason'] if 'reason' in r else ''))
    except (Exception, SystemExit):
        # Local filesystem may be completely full/unwritable. Never convert local success to failure.
        print('HANDOFF_SYNC=FAILED RETRY=aimem_handoff_--retry-pending LOCAL_RECORD_UNAVAILABLE=YES')


def register(project_id, memory_slug=None, folder=None):
    with core.lock('handoff-global'):
        c = config()
        if any(excluded(c, value) for value in (project_id, memory_slug, folder)):
            raise BridgeError('EXCLUDED_BY_OWNER')
        p = {'project_id':project_id,'display_name':project_id,'folder':folder or project_id,'memory_slug':memory_slug,'active':True}
        if any(not SAFE.fullmatch(v) for v in (project_id,p['folder'])): raise BridgeError('PROJECT_MAPPING_REQUIRED')
        if any(project_id == x['project_id'] or p['folder']==x['folder'] or (memory_slug and memory_slug==x.get('memory_slug')) for x in c['projects']):
            raise BridgeError('PROJECT_MAPPING_REQUIRED')
        if memory_slug and (not SAFE.fullmatch(memory_slug) or not (core.ROOT/'projects'/memory_slug/'project.json').is_file()):
            raise BridgeError('PROJECT_MAPPING_REQUIRED')
        root = drive(c)
        dest = root / p['folder']
        if dest.is_symlink(): raise BridgeError('PROJECT_MAPPING_REQUIRED')
        dest.mkdir(exist_ok=True)
        c['projects'].append(p); atomic(cfg_path(),dumps(c))


def cli(a):
    try:
        if a.status:
            c = config(); health = 'AVAILABLE'
            try: drive(c)
            except (Exception, SystemExit): health = 'UNAVAILABLE'
            d = core.ROOT/'.handoff'
            print(dumps({'drive':c['drive']['path'],'drive_state':health,'registered_projects':len(c['projects']),
                         'excluded_projects':c.get('excluded_projects', []),
                         'pending_count':len(list((d/'pending').glob('*.json'))),
                         'states':[read_json(f) for f in sorted((d/'status').glob('*.json'))], 'cloud_sync':'UNVERIFIED'})); return
        if a.register:
            register(a.register,a.memory_slug,a.folder); print('REGISTERED='+a.register); return
        c = config()
        if a.set_state:
            p = resolve(c,a.project or detect(str(Path.cwd())))
            s = read_json(Path(a.set_state)); validate_state(s)
            d = state_path(p); d.parent.mkdir(mode=0o700,exist_ok=True)
            atomic(d,dumps(s)); print('HANDOFF_STATE=STORED'); return
        selectors = []
        if a.retry_pending:
            selectors += [read_json(f)['selector'] for f in sorted((local()/'pending').glob('*.json'))]
            # Also closes a crash gap between local checkpoint commit and hook enqueue.
            selectors += [p['project_id'] for p in c['projects']]
        elif a.all: selectors = [p['project_id'] for p in c['projects']]
        else: selectors = [a.project or detect(str(Path.cwd())) or 'UNMAPPED_WORKSPACE']
        failed = False
        for selector in dict.fromkeys(selectors):
            r = publish(selector); failed |= r['handoff_sync'] not in ('SYNCED_LOCAL', 'EXCLUDED_BY_OWNER')
            print(selector+' HANDOFF_SYNC='+r['handoff_sync']+(' REASON='+r['reason'] if 'reason' in r else ''))
        if failed: raise SystemExit(1)
    except BridgeError as e:
        print('HANDOFF_SYNC='+e.code); raise SystemExit(1)


def add_parser(sp):
    p = sp.add_parser('handoff',help='Publish bounded navigation handoffs to the pinned desktop Drive folder')
    g = p.add_mutually_exclusive_group()
    g.add_argument('--all',action='store_true'); g.add_argument('--status',action='store_true')
    g.add_argument('--retry-pending',action='store_true'); g.add_argument('--register')
    g.add_argument('--set-state',metavar='JSON_FILE')
    p.add_argument('--project'); p.add_argument('--memory-slug'); p.add_argument('--folder')
    p.set_defaults(func=cli)
