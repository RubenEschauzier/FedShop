import json,collections,statistics as st,sys
rows=[json.loads(l) for l in open(sys.argv[1])]
g=collections.defaultdict(list)
for r in rows: g[r['q']].append(r)
for q,l in sorted(g.items()):
    f=[r['fetched'] for r in l]
    kinds=collections.defaultdict(list); pk=collections.defaultdict(list)
    for r in l:
        for k in ['producer','vendor','ratingsite','reviewer']:
            kinds[k].append(r['by_kind'].get(k,0)); pk[k].append(r['pods_kind'].get(k,0))
    print(f"{q:4} med={int(st.median(f)):>7} max={max(f):>7} nosvc_docs={int(st.median([r['nosvc_docs'] for r in l])):>6} | "+' '.join(f"{k[:5]}={int(st.median(v))}" for k,v in kinds.items())+' | pods '+' '.join(f"{k[:5]}={int(st.median(v))}" for k,v in pk.items()))
