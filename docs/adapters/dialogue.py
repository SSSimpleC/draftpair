"""Preview a simple stage script: @ opens a scene, and Name | Line marks dialogue."""
import json
import re
import sys


def literal(value):
    return re.sub(r'([\\\x60*_{}\[\]()<>#+.!|~-])', r'\\\1', value)


def blocks(source):
    for number, line in enumerate(source.splitlines(), 1):
        if line.startswith('@ '):
            yield {'start': number, 'end': number, 'markdown': '## ' + literal(line[2:].strip())}
        elif ' | ' in line:
            speaker, words = line.split(' | ', 1)
            yield {'start': number, 'end': number, 'markdown': '**' + literal(speaker) + '**　' + literal(words)}


request = json.load(sys.stdin)
json.dump({'version': 1, 'blocks': list(blocks(request['source']))}, sys.stdout, ensure_ascii=False)
