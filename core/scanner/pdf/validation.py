"""Conservative static PDF validation; no rendering, actions, URLs or source writes.

Admit classic xref tables with one revision. Object streams, xref streams and
incremental revisions remain explicit unsupported candidates. A checked range
ends at the validated %%EOF line, not an inferred original filesystem boundary.
"""
from dataclasses import dataclass
import re
import zlib

MAX_BYTES = 16 * 1024 * 1024
MAX_OBJECTS = 10000
MAX_PAGES = 200
MAX_TOKENS = 200000
MAX_DEPTH = 32
MAX_DECODED = 32 * 1024 * 1024
MAX_STRING = 65536
MAX_PAGE_POINTS = 14400
WS = b'\0\t\n\f\r '
DELIMS = WS + b'()<>[]{}/%'
NUMBER = re.compile(rb'[+-]?(?:\d{1,10}(?:\.\d{0,10})?|\.\d{1,10})(?=$|[\x00\t\n\f\r ()<>\[\]{}/%])')
HEADER = re.compile(rb'%PDF-(1\.[0-7]|2\.0)(?:\r\n|\r|\n)')
FOOTER = re.compile(rb'(?m)(?:^|(?<=[\r\n]))startxref[\x00\t\n\f\r ]{1,32}(\d{1,10})[\x00\t\n\f\r ]{1,32}%%EOF(?:\r\n|\r|\n|$)')
SPACE = re.compile(rb'[\x00\t\n\f\r ]*')
COMMENT_END = re.compile(rb'[\r\n]')


class Rejected(Exception):
    def __init__(self, reason='invalid_structure'):
        self.reason = reason


def require(condition, reason='invalid_structure'):
    if not condition:
        raise Rejected(reason)


@dataclass(frozen=True)
class Name:
    value: bytes


@dataclass(frozen=True)
class Ref:
    number: int
    generation: int


@dataclass(frozen=True)
class String:
    value: bytes


@dataclass(frozen=True)
class Stream:
    dictionary: dict
    payload: memoryview


@dataclass(frozen=True)
class PdfValidation:
    size: int
    integrity: str
    file_type: str = 'pdf'
    details: dict | None = None


def skip_space(data, position, end=None):
    end = len(data) if end is None else end
    comments = 0
    while position < end:
        position = SPACE.match(data, position, end).end()
        if position >= end or data[position] != 37:
            return position
        comments += 1; require(comments <= MAX_OBJECTS, 'validation_limit')
        newline = COMMENT_END.search(data, position, end)
        position = end if newline is None else newline.start() + 1
    return position


class Syntax:
    def __init__(self, data, position, token, budget):
        self.data, self.pos, self.token, self.budget = data, position, token, budget

    def lex(self):
        self.token.checkpoint(); self.budget[0] -= 1
        require(self.budget[0] >= 0, 'validation_limit')
        data = self.data; self.pos = skip_space(data, self.pos)
        require(self.pos < len(data))
        at = self.pos
        for marker in (b'<<', b'>>'):
            if data.startswith(marker, at):
                self.pos += 2; return 'delimiter', marker
        byte = data[at]
        if byte in b'[]':
            self.pos += 1; return 'delimiter', bytes([byte])
        if byte == 47:
            self.pos += 1; start = self.pos
            while self.pos < len(data) and data[self.pos] not in DELIMS:
                self.pos += 1
                require(self.pos - start <= 256, 'validation_limit')
            raw = data[start:self.pos]
            require(not re.search(rb'#(?![0-9A-Fa-f]{2})', raw))
            decoded = re.sub(rb'#([0-9A-Fa-f]{2})', lambda m: bytes([int(m[1],16)]), raw)
            require(b'\0' not in decoded)
            return 'value', Name(decoded)
        if byte == 40:
            self.pos += 1; depth = 1; result = bytearray()
            while self.pos < len(data):
                self.token.checkpoint()
                b = data[self.pos]; self.pos += 1
                if b == 92:
                    require(self.pos < len(data))
                    b = data[self.pos]; self.pos += 1
                    if b == 13:
                        if self.pos < len(data) and data[self.pos] == 10: self.pos += 1
                        continue
                    if b == 10: continue
                    if 48 <= b <= 55:
                        digits = bytes([b])
                        while len(digits) < 3 and self.pos < len(data) and 48 <= data[self.pos] <= 55:
                            digits += data[self.pos:self.pos + 1]; self.pos += 1
                        b = int(digits, 8) & 255
                    else:
                        b = {110:10,114:13,116:9,98:8,102:12}.get(b,b)
                elif b == 40:
                    depth += 1; require(depth <= MAX_DEPTH, 'validation_limit')
                elif b == 41:
                    depth -= 1
                    if depth == 0: return 'value', String(bytes(result))
                result.append(b)
                require(len(result) <= MAX_STRING, 'validation_limit')
            raise Rejected()
        if byte == 60:
            end = data.find(b'>', at + 1)
            require(end >= 0 and end - at <= 2 * MAX_STRING, 'validation_limit' if end - at > 2 * MAX_STRING else 'invalid_structure')
            raw = re.sub(rb'[\x00\t\n\f\r ]', b'', data[at + 1:end])
            require(re.fullmatch(rb'[0-9A-Fa-f]*', raw) is not None)
            if len(raw) % 2: raw += b'0'
            self.pos = end + 1; return 'value', String(bytes.fromhex(raw.decode('ascii')))
        match = NUMBER.match(data, at)
        if match:
            self.pos = match.end(); raw = match[0]
            return 'value', float(raw) if b'.' in raw else int(raw)
        end = at
        while end < len(data) and data[end] not in DELIMS:
            end += 1
            require(end - at <= 64, 'validation_limit')
        require(end > at)
        self.pos = end; return 'word', data[at:end]

    def word(self, expected):
        require(self.lex() == ('word', expected))

    def integer(self):
        kind, value = self.lex()
        require(kind == 'value' and type(value) is int and value >= 0)
        return value

    def value(self, depth=0):
        require(depth <= MAX_DEPTH, 'validation_limit')
        kind, value = self.lex()
        if kind == 'delimiter' and value == b'<<':
            result = {}
            while True:
                kind, key = self.lex()
                if (kind, key) == ('delimiter', b'>>'): return result
                require(kind == 'value' and isinstance(key, Name) and key.value not in result)
                result[key.value] = self.value(depth + 1)
        if kind == 'delimiter' and value == b'[':
            result = []
            while True:
                position = self.pos; next_token = self.lex()
                if next_token == ('delimiter', b']'): return result
                self.pos = position; result.append(self.value(depth + 1))
                require(len(result) <= MAX_OBJECTS, 'validation_limit')
        if kind == 'word':
            require(value in (b'true', b'false', b'null'))
            return {b'true':True, b'false':False, b'null':None}[value]
        require(kind == 'value')
        if type(value) is int and value >= 0:
            position = self.pos
            second = self.lex()
            if second[0] == 'value' and type(second[1]) is int and second[1] >= 0:
                third = self.lex()
                if third == ('word', b'R'):
                    require(value > 0 and second[1] <= 65535)
                    return Ref(value, second[1])
            self.pos = position
        return value


class Document:
    def __init__(self, data, xref, footer, token, budget):
        self.data, self.xref, self.footer, self.token, self.budget = data, xref, footer, token, budget
        self.entries, self.objects, self.loading, self.ranges = {}, {}, set(), []
        self.decoded = 0

    def syntax(self, at): return Syntax(self.data, at, self.token, self.budget)

    def resolve(self, value):
        if not isinstance(value, Ref): return value
        require(value.number in self.entries and self.entries[value.number][1] == value.generation)
        return self.object(value.number)

    def object(self, number):
        if number in self.objects: return self.objects[number]
        require(number not in self.loading and len(self.loading) <= MAX_DEPTH, 'validation_limit')
        self.loading.add(number)
        offset, generation = self.entries[number]
        parser = self.syntax(offset)
        require(parser.integer() == number and parser.integer() == generation)
        parser.word(b'obj'); value = parser.value()
        position = parser.pos
        next_token = parser.lex()
        if next_token == ('word', b'stream'):
            require(isinstance(value, dict) and b'Length' in value)
            at = parser.pos
            require(self.data[at:at + 1] in (b'\r', b'\n'))
            at += 2 if self.data[at:at + 2] == b'\r\n' else 1
            length = self.resolve(value[b'Length'])
            require(type(length) is int and 0 <= length <= len(self.data) - at)
            end = at + length
            parser.pos = end
            if self.data[end:end + 2] == b'\r\n': parser.pos += 2
            elif self.data[end:end + 1] in (b'\r', b'\n'): parser.pos += 1
            require(self.data.startswith(b'endstream', parser.pos))
            parser.word(b'endstream'); parser.word(b'endobj')
            value = Stream(value, memoryview(self.data)[at:end])
        else:
            parser.pos = position; parser.word(b'endobj')
        require(parser.pos <= self.xref)
        self.ranges.append((offset, parser.pos))
        self.objects[number] = value; self.loading.remove(number)
        return value

    def xrefs(self):
        require(0 < self.xref < self.footer)
        parser = self.syntax(self.xref)
        if not self.data.startswith(b'xref', self.xref):
            # Only a real xref-stream object earns the unsupported label. A
            # bogus footer inside ordinary stream bytes must not hide a later
            # valid classic footer.
            probe = self.syntax(self.xref)
            probe.integer(); probe.integer(); probe.word(b'obj')
            dictionary = probe.value()
            require(isinstance(dictionary,dict) and dictionary.get(b'Type') == Name(b'XRef'))
            raise Rejected('unsupported')
        parser.word(b'xref'); all_entries = {}
        while True:
            at = parser.pos
            if parser.lex() == ('word', b'trailer'): break
            parser.pos = at; first, count = parser.integer(), parser.integer()
            require(count > 0 and first + count <= MAX_OBJECTS, 'validation_limit')
            for number in range(first, first + count):
                self.token.checkpoint()
                require(number not in all_entries)
                offset, generation = parser.integer(), parser.integer()
                kind, flag = parser.lex()
                require(kind == 'word' and flag in (b'n', b'f') and generation <= 65535)
                if flag == b'n':
                    require(number > 0 and 0 < offset < self.xref)
                    self.entries[number] = (offset, generation)
                all_entries[number] = (offset, generation, flag)
        trailer = parser.value(); require(isinstance(trailer, dict))
        require(b'Encrypt' not in trailer, 'encrypted')
        require(b'Prev' not in trailer, 'incremental')
        require(b'XRefStm' not in trailer, 'unsupported')
        size = trailer.get(b'Size')
        require(type(size) is int and 1 <= size <= MAX_OBJECTS, 'validation_limit')
        require(set(all_entries) == set(range(size)))
        require(all_entries[0][2] == b'f' and all_entries[0][1] == 65535)
        require(parser.pos <= self.footer)
        parser.word(b'startxref'); require(parser.integer() == self.xref)
        end = re.match(rb'[\x00\t\n\f\r ]{1,32}%%EOF(?:\r\n|\r|\n|$)', self.data[parser.pos:])
        require(end is not None and parser.pos + end.end() == len(self.data))
        self.trailer = trailer

    def inspect(self, value, depth=0):
        self.token.checkpoint(); require(depth <= MAX_DEPTH, 'validation_limit')
        if isinstance(value, Ref):
            require(value.number in self.entries and self.entries[value.number][1] == value.generation)
        elif isinstance(value, list):
            for item in value: self.inspect(item, depth + 1)
        elif isinstance(value, Stream):
            require(b'F' not in value.dictionary, 'external_reference')
            self.inspect(value.dictionary, depth + 1)
            self.check_stream(value)
        elif isinstance(value, dict):
            action = self.resolve(value.get(b'A'))
            if isinstance(action, dict):
                require(action.get(b'S') not in tuple(Name(n) for n in (b'URI',b'GoToR',b'Launch',b'SubmitForm',b'ImportData')), 'external_reference')
            active = {b'JS',b'JavaScript',b'OpenAction',b'AA',b'AcroForm',b'XFA',b'EmbeddedFiles',b'EF',b'AF',b'Collection',b'RichMedia',b'RichMediaContent',b'Movie',b'Sound',b'3D'}
            require(not active.intersection(value), 'active_content')
            require(value.get(b'S') not in (Name(b'JavaScript'),Name(b'Named'),Name(b'ResetForm')), 'active_content')
            require(not {b'URI',b'URL',b'Ref',b'OPI'}.intersection(value), 'external_reference')
            require(value.get(b'Type') not in (Name(b'ObjStm'),Name(b'XRef')), 'unsupported')
            require(value.get(b'Type') not in (Name(b'Filespec'),), 'external_reference')
            require(value.get(b'Type') != Name(b'EmbeddedFile'), 'active_content')
            require(value.get(b'Subtype') != Name(b'PS'), 'unsupported')
            require(value.get(b'S') not in tuple(Name(n) for n in (b'URI',b'GoToR',b'Launch',b'SubmitForm',b'ImportData')), 'external_reference')
            require(value.get(b'UserUnit', 1) == 1, 'unsupported')
            for item in value.values(): self.inspect(item, depth + 1)

    def check_stream(self, stream):
        data, info = stream.payload, stream.dictionary
        filter_name = self.resolve(info.get(b'Filter'))
        if isinstance(filter_name, list):
            require(len(filter_name) == 1, 'unsupported'); filter_name = filter_name[0]
        require(filter_name != Name(b'Crypt'), 'encrypted')
        require(filter_name in (None,Name(b'FlateDecode'),Name(b'DCTDecode')), 'unsupported')
        if filter_name == Name(b'FlateDecode'):
            inflater = zlib.decompressobj(); produced = 0
            try:
                for at in range(0, len(data), 65536):
                    self.token.checkpoint()
                    output = inflater.decompress(data[at:at + 65536], MAX_DECODED - self.decoded - produced + 1)
                    produced += len(output)
                    require(self.decoded + produced <= MAX_DECODED and not inflater.unconsumed_tail, 'validation_limit')
                require(inflater.eof and not inflater.unused_data)
            except zlib.error as error: raise Rejected() from error
        else:
            produced = len(data)
        self.decoded += produced; require(self.decoded <= MAX_DECODED, 'validation_limit')
        if info.get(b'Subtype') == Name(b'Image'):
            width, height = self.resolve(info.get(b'Width')), self.resolve(info.get(b'Height'))
            require(type(width) is int and type(height) is int and width > 0 and height > 0)
            require(width * height <= 16_000_000, 'validation_limit')
            if filter_name == Name(b'DCTDecode'):
                from io import BytesIO
                from core.scanner.image.validation import validate as validate_image
                result = validate_image(BytesIO(data), 0, len(data), 'jpg', self.token, MAX_BYTES, MAX_DECODED)
                require(result.integrity == 'structure_checked', 'validation_limit' if result.integrity == 'validation_limit' else 'invalid_structure')
        elif filter_name == Name(b'DCTDecode'):
            raise Rejected('unsupported')

    def pages(self):
        require(isinstance(self.trailer.get(b'Root'), Ref))
        catalog = self.resolve(self.trailer.get(b'Root'))
        require(isinstance(catalog, dict) and catalog.get(b'Type') == Name(b'Catalog'))
        seen, pages = set(), []
        def walk(ref, parent=None, media=None, depth=0):
            self.token.checkpoint(); require(depth <= MAX_DEPTH, 'validation_limit')
            require(isinstance(ref, Ref) and ref not in seen)
            seen.add(ref); node = self.resolve(ref)
            require(isinstance(node, dict))
            if parent is not None: require(node.get(b'Parent') == parent)
            else: require(b'Parent' not in node)
            media = self.resolve(node.get(b'MediaBox', media))
            if node.get(b'Type') == Name(b'Pages'):
                children = self.resolve(node.get(b'Kids'))
                require(isinstance(children, list) and children)
                count = 0
                for child in children: count += walk(child, ref, media, depth + 1)
                require(type(node.get(b'Count')) is int and node[b'Count'] == count)
                return count
            require(node.get(b'Type') == Name(b'Page'))
            require(isinstance(media, list) and len(media) == 4 and all(type(n) in (int,float) for n in media))
            width, height = media[2]-media[0], media[3]-media[1]
            require(width > 0 and height > 0)
            require(max(abs(n) for n in media) <= MAX_PAGE_POINTS, 'validation_limit')
            pages.append((width,height)); require(len(pages) <= MAX_PAGES, 'validation_limit')
            return 1
        walk(catalog.get(b'Pages')); require(bool(pages))
        return pages

    def check(self):
        self.xrefs()
        for number in self.entries:
            self.object(number)
        self.inspect(self.trailer)
        for value in self.objects.values(): self.inspect(value)
        header = HEADER.match(self.data); require(header is not None)
        position = header.end()
        for start, end in sorted(self.ranges + [(self.xref,len(self.data))]):
            require(position <= start and skip_space(self.data,position,start) == start)
            position = end
        require(position == len(self.data))
        pages = self.pages()
        return {'check_version':1,'boundary_basis':'checked_eof_line','pdf_version':header[1].decode('ascii'),
                'pages':len(pages),'objects':len(self.entries),'decoded_stream_bytes':self.decoded,
                'rendered':False,'revisions':1}


def validate(stream, start, source_size, token, max_size=MAX_BYTES):
    available = max(0, source_size - start); cap = min(MAX_BYTES,max_size)
    token.checkpoint(); stream.seek(start)
    first = stream.read(min(16,available,cap))
    if not HEADER.match(first):
        return PdfValidation(len(first), 'pdf_validation_limit' if available > cap and cap < 10 else ('pdf_truncated' if available < 10 else 'pdf_unsupported'))
    chunks, read = [first], len(first)
    while read < min(available,cap):
        token.checkpoint(); block = stream.read(min(1024*1024,min(available,cap)-read))
        if not block: return PdfValidation(read,'pdf_truncated')
        chunks.append(block); read += len(block)
    data = b''.join(chunks); chunks.clear()
    if not HEADER.match(data): return PdfValidation(min(read,8),'pdf_unsupported')
    budget = [MAX_TOKENS]; last = None; best = None; count = 0
    for footer in FOOTER.finditer(data):
        token.checkpoint(); count += 1
        if count > 32: return PdfValidation(read,'pdf_validation_limit')
        end = footer.end()
        try:
            details = Document(data[:end],int(footer[1]),footer.start(),token,budget).check()
            # A following object/xref indicates an incremental revision. Never
            # export an earlier revision as if it represented the final file.
            tail, comments = end, 0
            while tail < len(data):
                tail = SPACE.match(data,tail).end()
                if data.startswith(b'%PDF-',tail): break
                if tail >= len(data) or data[tail] != 37: break
                comments += 1; require(comments <= MAX_OBJECTS, 'validation_limit')
                newline = COMMENT_END.search(data,tail)
                tail = len(data) if newline is None else newline.end()
            if re.match(rb'(?:\d{1,10}[\x00\t\n\f\r ]+\d{1,5}[\x00\t\n\f\r ]+obj\b|xref\b)',data[tail:]):
                return PdfValidation(end,'pdf_incremental')
            if tail == len(data) and available > read: return PdfValidation(end,'pdf_boundary_unknown')
            return PdfValidation(end,'pdf_structure_checked',details=details)
        except Rejected as error:
            last = error.reason
            if last == 'validation_limit': return PdfValidation(end,'pdf_validation_limit')
            if last != 'invalid_structure' and best is None: best = (end,last)
    if best is not None: return PdfValidation(best[0],'pdf_'+best[1])
    return PdfValidation(read,'pdf_'+(last or ('validation_limit' if available > read else 'truncated')))
