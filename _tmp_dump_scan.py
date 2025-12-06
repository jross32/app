import json,re,collections
entries=json.load(open('data/apa_api_captures/2025-12-01_14-46-33/api_dump_v2.json',encoding='utf-8'))
alias_count=collections.Counter(); alias_records=[]
for e in entries:
    for resp in e.get('response_body',[]) or []:
        data=resp.get('data') or {}
        alias=data.get('alias')
        if alias and alias.get('__typename')=='Alias':
            disp=alias.get('displayName'); alias_count[disp]+=1; alias_records.append(alias)
        member=data.get('member')
        if member:
            for al in member.get('aliases',[]) or []:
                disp=al.get('displayName'); alias_count[disp]+=1; alias_records.append(al)
print('aliases found',len(alias_records))
print('top',alias_count.most_common(5))
