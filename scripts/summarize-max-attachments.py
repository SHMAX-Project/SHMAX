#!/usr/bin/env python3
"""Offline safe metadata from PyMax Message.model_dump(mode='json', by_alias=True).

Reads stdin; never emits URLs, tokens, IDs, names, captions or arbitrary fields.
Keep raw input private. Output alone can be shared for compatibility diagnosis.
"""
import json,sys
value=json.load(sys.stdin)
attachments=value.get('attaches')
if not isinstance(attachments,list):raise SystemExit('Expected PyMax Message JSON with attaches list')
known={'PHOTO','FILE','VIDEO','AUDIO','STICKER','CONTACT','SHARE','POLL','CONTROL','CALL','INLINE_KEYBOARD'}
records=[]
for index,item in enumerate(attachments):
    if not isinstance(item,dict):records.append({'index':index,'type':'UNKNOWN'});continue
    kind=item.get('_type',item.get('type'))
    def dimension(key):
        v=item.get(key);return v if type(v) is int and 0<v<=50000 else None
    records.append({'index':index,'type':kind if isinstance(kind,str) and kind in known else 'UNKNOWN',
                    'has_photo_id':'photoId' in item or 'photo_id' in item,
                    'has_base_url':bool(item.get('baseUrl',item.get('base_url'))),
                    'has_photo_token':bool(item.get('photoToken',item.get('photo_token'))),
                    'width':dimension('width'),'height':dimension('height')})
print(json.dumps({'attachment_count':len(records),'attachments':records},ensure_ascii=False,indent=2))
