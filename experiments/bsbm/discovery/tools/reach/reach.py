"""Simulate the link closure a Comunica LTQP engine (cMatch + seeAlso/sameAs + pim:storage +
ldp:contains + walk-up) has to exhaust before it can terminate, per workload instance.

usage: python reach.py <benchmark dir> [cache.pkl]
"""
import os, sys, re, json, pickle, collections
from array import array

BENCH = sys.argv[1]
CACHE = sys.argv[2] if len(sys.argv) > 2 else None
WEB = os.path.join(BENCH, 'web')
pub = json.load(open(os.path.join(BENCH, 'publishers.json')))
BASE = pub['taxonomy']['storage'].rsplit('taxonomy/', 1)[0]

SEEALSO = 'http://www.w3.org/2000/01/rdf-schema#seeAlso'
SAMEAS = 'http://www.w3.org/2002/07/owl#sameAs'
PTOPIC = 'http://xmlns.com/foaf/0.1/isPrimaryTopicOf'
STORAGE = 'http://www.w3.org/ns/pim/space#storage'
UNCOND = {SAMEAS, PTOPIC} | (set() if os.environ.get('NO_SEEALSO') else {SEEALSO})


def build():
    ids, names = {}, []
    def I(x):
        i = ids.get(x)
        if i is None:
            i = ids[x] = len(names); names.append(x)
        return i
    docs = {}       # doc url -> {pred id: array(s,o,...)}; o = -1 for literals
    children = collections.defaultdict(list)  # container url -> child urls
    for root, dirs, files in os.walk(WEB):
        rel = os.path.relpath(root, WEB)
        cont = BASE if rel == '.' else BASE + rel + '/'
        for d in dirs:
            children[cont].append(cont + d + '/')
        for f in files:
            if f == '.meta' or not f.endswith('.nq'):
                continue
            url = cont + f[:-3]
            children[cont].append(url)
            by_p = {}
            with open(os.path.join(root, f), encoding='utf-8') as fh:
                for line in fh:
                    if line[0] != '<':
                        continue
                    a = line.index('> <')
                    s = line[1:a]
                    b = line.index('> ', a + 3)
                    p = line[a + 3:b]
                    rest = line[b + 2:]
                    o = I(rest[1:rest.index('>')]) if rest[0] == '<' else -1
                    arr = by_p.get(I(p))
                    if arr is None:
                        arr = by_p[I(p)] = array('i')
                    arr.append(I(s)); arr.append(o)
            docs[url] = by_p
    return ids, names, docs, dict(children)


if CACHE and os.path.exists(CACHE):
    ids, names, docs, children = pickle.load(open(CACHE, 'rb'))
else:
    ids, names, docs, children = build()
    if CACHE:
        pickle.dump((ids, names, docs, children), open(CACHE, 'wb'), protocol=5)
print('docs', len(docs), 'containers', len(children), 'iris', len(names), file=sys.stderr)


def pod_of(url):
    return url[len(BASE):].split('/', 1)[0]


def kind(pod):
    return pub[pod]['kind'] if pod in pub else '?'


PREFIX_RE = re.compile(r'PREFIX\s+(\w*):\s*<([^>]*)>', re.I)


def patterns(q):
    """Crude triple-pattern extraction: enough for these templates (no paths, no blank nodes)."""
    pre = dict(PREFIX_RE.findall(q))
    body = q[q.index('{'):]
    body = re.sub(r'(?m)^\s*#[^\n]*', '', body)
    body = re.sub(r'FILTER\s*(\((?:[^()]|\((?:[^()]|\([^()]*\))*\))*\))', ' ', body, flags=re.I)
    body = re.sub(r'\b(OPTIONAL|UNION|MINUS|SERVICE\s+<[^>]*>|FILTER NOT EXISTS|BIND\s*\([^)]*\))', ' ', body, flags=re.I)
    toks = re.findall(r'<[^>]*>|"(?:[^"\\]|\\.)*"(?:\^\^\S+|@\w+)?|\?\w+|[\w-]*:[\w.-]*\w|\ba\b|[{}.;,]', body)
    def term(t):
        if t.startswith('?'): return None
        if t.startswith('<'): return t[1:-1]
        if t == 'a': return 'http://www.w3.org/1999/02/22-rdf-syntax-ns#type'
        if t.startswith('"'): return 'LIT'
        p, l = t.split(':', 1); return pre[p] + l
    pats, cur = [], []
    for t in toks:
        if t in ('{', '}', '.'):
            cur = []; continue
        if t == ';':
            cur = cur[:1]; continue
        if t == ',':
            cur = cur[:2]; continue
        if len(cur) == 3:
            cur = []
        cur.append(t)
        if len(cur) == 3:
            pats.append(tuple(term(x) for x in cur))
    return pats


def simulate(q):
    pats = patterns(q)
    if os.environ.get('NO_PUBPAT'):
        PUBP={'http://purl.org/dc/elements/1.1/publisher','http://www4.wiwiss.fu-berlin.de/bizer/bsbm/v01/vocabulary/vendor','http://www4.wiwiss.fu-berlin.de/bizer/bsbm/v01/vocabulary/producer'}
        pats=[p for p in pats if not (p[1] in PUBP and p[2] is None)]
    cp = []   # compiled: (s id/None, p id/None, o id/None/'LIT', var positions)
    for s, p, o in pats:
        cp.append((None if s is None else ids.get(s, -2), None if p is None else ids.get(p, -2),
                   o if o in (None, 'LIT') else ids.get(o, -2)))
    uncond = [ids[x] for x in UNCOND if x in ids]
    LISTINGOF = ids.get('http://fedshop.example.org/vocab#listingOf')
    storage = ids.get(STORAGE)
    seeds = {i.split('#')[0] for i in re.findall(r'<(' + re.escape(BASE) + r'[^>]*)>', q)}
    seen, queue = set(), collections.deque()
    K = int(os.environ.get('CLUSTERS', '0')); MU = float(os.environ.get('MU', '0'))
    import zlib
    def cl(pod): return zlib.crc32(pod.encode()) % K
    src = [None]
    def push(u):
        if u.startswith(BASE):
            u = u.split('#')[0]
            if K and src[0]:
                a, b = src[0], pod_of(u)
                if b and a != b and 'taxonomy' not in (a, b) and cl(a) != cl(b) \
                        and zlib.crc32((a + '|' + u).encode()) / 2**32 >= MU:
                    return
            if u not in seen:
                seen.add(u); queue.append(u)
    for s in seeds: push(s)
    pods = set()
    while queue:
        u = queue.popleft()
        pod = pod_of(u)
        src[0] = pod
        if pod: pods.add(pod)  # walk-up reads <pod>/.meta: one request per pod
        if u in children:
            if os.environ.get('NO_LDP'): continue
            for c in children[u]: push(c)
            continue
        by_p = docs.get(u)
        if by_p is None:
            continue
        for pid in uncond:
            arr = by_p.get(pid)
            if arr and os.environ.get('SELECTIVE') and pid == ids.get(SEEALSO):
                # brand product -> listing seeAlso becomes '<listing> fs:listingOf <product>' placed in the
                # brand document, followed only through cMatch; member -> member seeAlso is dropped;
                # taxonomy -> brand record seeAlso is kept
                for k in range(0, len(arr), 2):
                    sk = kind(pod_of(names[arr[k]]))
                    if sk == 'taxonomy':
                        tb = int(os.environ.get('TYPE_BRANDS', '0'))
                        tm = int(os.environ.get('TYPE_MARKETS', '0'))
                        if tm and K:
                            home = zlib.crc32(names[arr[k]].encode()) % K
                            if (cl(pod_of(names[arr[k + 1]])) - home) % K >= tm:
                                continue
                        if not tb or zlib.crc32((names[arr[k]] + '|' + names[arr[k + 1]]).encode()) % 1000 < tb * 1000 // 220:
                            push(names[arr[k + 1]])
                    elif sk == 'producer':
                        for ps, pp, po in cp:
                            if pp == LISTINGOF and ps is None and (po is None or po == arr[k]):
                                push(names[arr[k + 1]]); break
                continue
            if arr:
                lim = os.environ.get('SEEALSO_FROM')
                for k in range(1, len(arr), 2):
                    if arr[k] >= 0 and (not lim or kind(pod_of(names[arr[k-1]])) in lim.split(',')): push(names[arr[k]])
        if storage is not None and storage in by_p and not os.environ.get('NO_STORAGE'):
            arr = by_p[storage]
            for k in range(0, len(arr), 2):
                if names[arr[k]].split('#')[0] == u and arr[k + 1] >= 0: push(names[arr[k + 1]])
        for s, p, o in cp:
            preds = [p] if p is not None else list(by_p)
            for pid in preds:
                arr = by_p.get(pid)
                if not arr: continue
                for k in range(0, len(arr), 2):
                    ss, oo = arr[k], arr[k + 1]
                    if s is not None and ss != s: continue
                    if o == 'LIT':
                        if oo != -1: continue
                    elif o is not None and oo != o: continue
                    if s is None: push(names[ss])
                    if o is None and oo >= 0: push(names[oo])
                    if p is None: push(names[pid])
    by_kind = collections.Counter(); svc_docs = 0; nosvc_docs = 0
    for u in seen:
        pod = pod_of(u)
        by_kind[kind(pod)] += 1
        if pod in pub and pub[pod].get('service'): svc_docs += 1
        else: nosvc_docs += 1
    pods_kind = collections.Counter(kind(p) for p in pods)
    import statistics
    per_pod = collections.Counter(pod_of(u) for u in seen)
    svc_pods = sum(1 for p in pods if pub.get(p, {}).get('service'))
    return dict(fetched=len(seen), pods=len(pods), by_kind=dict(by_kind), pods_kind=dict(pods_kind),
                svc_pods=svc_pods, nosvc_docs=nosvc_docs, svc_docs=svc_docs)


out = []
wl = os.path.join(BENCH, 'workload')
only = os.environ.get('ONLY')
for qn in sorted(os.listdir(wl)):
    if only and qn not in only.split(','): continue
    for inst in sorted(os.listdir(os.path.join(wl, qn))):
        f = os.path.join(wl, qn, inst, 'plain.sparql')
        if not os.path.exists(f): continue
        a = json.load(open(os.path.join(wl, qn, inst, 'analysis.json')))
        r = simulate(open(f).read())
        r.update(q=qn, inst=inst, best_case_pods=a.get('storages_reached_best_case'),
                 answer_pods=len(a.get('storages_in_answer', [])))
        out.append(r)
        print(json.dumps(r), flush=True)
