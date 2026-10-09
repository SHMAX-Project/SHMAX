#!/usr/bin/env python3
"""Check local Markdown/HTML file targets; no network requests."""
import pathlib,re,urllib.parse
root=pathlib.Path(__file__).resolve().parents[1]
errors=[]
for p in sorted(root.rglob('*.md')):
 if any(x in p.parts for x in ('node_modules','.venv','.git','dist-release')):continue
 text=p.read_text()
 for target in re.findall(r'\]\(([^\s)]+)(?:\s+[^)]*)?\)|(?:src|href)="([^"]+)"',text):
  href=next(x for x in target if x)
  if re.match(r'^[a-zA-Z][\w+.-]*:',href) or href.startswith(('#','//')):continue
  name=urllib.parse.unquote(href.split('#')[0].split('?')[0].strip('<>'))
  if name and not (p.parent/name).exists():errors.append(f'{p.relative_to(root)}: {name}')
for error in errors:print(error)
print(f'Broken local file links: {len(errors)}')
raise SystemExit(bool(errors))
