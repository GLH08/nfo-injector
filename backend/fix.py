import re
path = r'd:\Files\Code\emby-ffprobe\nfo-injector\backend\ffprobe_runner.py'
with open(path, 'r', encoding='utf-8') as f:
    lines = f.readlines()

new_lines = []
for line in lines:
    if 'SyntaxError' in line or 'unterminated f-string' in line:
        continue
    # Fix broken quotes in f-strings where chinese characters were mangled
    if line.strip().startswith('log(f"'):
        if not line.strip().endswith('"') and not line.strip().endswith('")'):
            line = line.rstrip() + '")\n'
    new_lines.append(line)

with open(path, 'w', encoding='utf-8') as f:
    f.writelines(new_lines)
print('Fixed quotes')
