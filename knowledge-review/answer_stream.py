"""Incrementally expose final answer text, retaining full result validation."""
from agent_cancel import checkpoint


class AnswerText:
    """A JSON string-path lexer; raw JSON and hidden reasoning never reach the UI."""
    def __init__(self, emit):
        self.emit = emit
        self.stack = []
        self.string = False
        self.key = False
        self.visible = False
        self.value = ''
        self.escape = False
        self.unicode = None
        self.high = None
        self.primitive = False
        self.blocks = 0

    def path(self):
        if not self.stack:
            return []
        top = self.stack[-1]
        return top['path'] + [top['key'] if top['kind'] == '{' else top['index']]

    def consume_value(self):
        if self.stack:
            top = self.stack[-1]
            top['expect'] = 'comma'
            if top['kind'] == '[':
                top['index'] += 1

    def feed(self, fragment):
        output = []
        for c in fragment:
            if self.string:
                if self.unicode is not None:
                    self.unicode += c
                    if len(self.unicode) < 4:
                        continue
                    unit = int(self.unicode, 16)
                    self.unicode = None
                    if 0xD800 <= unit <= 0xDBFF:
                        if self.high is not None:
                            raise ValueError('Invalid surrogate pair')
                        self.high = unit
                        continue
                    if 0xDC00 <= unit <= 0xDFFF:
                        if self.high is None:
                            raise ValueError('Invalid surrogate pair')
                        c = chr(0x10000 + ((self.high - 0xD800) << 10) + unit - 0xDC00)
                        self.high = None
                    else:
                        if self.high is not None:
                            raise ValueError('Invalid surrogate pair')
                        c = chr(unit)
                elif self.escape:
                    self.escape = False
                    if c == 'u':
                        self.unicode = ''
                        continue
                    c = {'"': '"', '\\': '\\', '/': '/', 'b': '\b', 'f': '\f', 'n': '\n', 'r': '\r', 't': '\t'}[c]
                elif c == '\\':
                    self.escape = True
                    continue
                elif c == '"':
                    if self.high is not None:
                        raise ValueError('Invalid surrogate pair')
                    self.string = False
                    if self.key:
                        self.stack[-1]['key'] = self.value
                        self.stack[-1]['expect'] = 'colon'
                    continue
                if self.key:
                    self.value += c
                elif self.visible:
                    output.append(c)
                continue
            if self.primitive:
                if c not in ',}] \t\r\n':
                    continue
                self.primitive = False
            if c.isspace():
                continue
            if c == '"':
                self.string = True
                self.key = bool(self.stack and self.stack[-1]['kind'] == '{' and self.stack[-1]['expect'] == 'key')
                self.value = ''
                path = self.path()
                self.visible = not self.key and len(path) == 3 and path[0] == 'blocks' and isinstance(path[1], int) and path[2] == 'text'
                if self.visible:
                    if self.blocks:
                        output.append('\n\n')
                    self.blocks += 1
                if not self.key:
                    self.consume_value()
            elif c in '{[':
                path = self.path()
                self.consume_value()
                self.stack.append({'kind': c, 'path': path, 'key': None, 'index': 0, 'expect': 'key' if c == '{' else 'value'})
            elif c in '}]':
                self.stack.pop()
            elif c == ':':
                self.stack[-1]['expect'] = 'value'
            elif c == ',':
                self.stack[-1]['expect'] = 'key' if self.stack[-1]['kind'] == '{' else 'value'
            else:
                self.consume_value()
                self.primitive = True
        if output:
            text = ''.join(output)
            for offset in range(0, len(text), 1024):
                self.emit(text[offset:offset + 1024])


def collect_answer(client, parameters, schema, emit, *, tools=False):
    lexer = AnswerText(emit)
    parts = []
    size = 0
    finished = None
    names = {}
    pending = ''
    with client.chat.completions.create(**parameters, stream=True) as stream:
        for chunk in stream:
            checkpoint()
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            delta = choice.delta
            if getattr(delta, 'refusal', None):
                raise RuntimeError('The model declined the request')
            if tools:
                piece = ''
                for call in delta.tool_calls or []:
                    if call.index != 0:
                        raise RuntimeError('Expected one structured result')
                    if call.function:
                        names[call.index] = names.get(call.index, '') + (call.function.name or '')
                        if not 'submit_result'.startswith(names[call.index]):
                            raise RuntimeError('Unexpected structured result tool')
                        piece += call.function.arguments or ''
            else:
                if delta.tool_calls:
                    raise RuntimeError('Unexpected result tool')
                piece = delta.content or ''
            size += len(piece.encode('utf-8'))
            if size > 4 * 1024 * 1024:
                raise RuntimeError('Structured result exceeded its byte limit')
            parts.append(piece)
            if tools and names.get(0) != 'submit_result':
                pending += piece
            else:
                lexer.feed(pending + piece)
                pending = ''
            if choice.finish_reason:
                finished = choice.finish_reason
    if finished not in ('stop', 'tool_calls') or (tools and names != {0: 'submit_result'}):
        raise RuntimeError('The model stream ended without a complete result')
    checkpoint()
    return schema.model_validate_json(''.join(parts))
