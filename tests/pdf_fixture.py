"""Synthetic classic-xref PDF fixtures, authored for scanner/renderer testing."""
import zlib


def stream(payload, *, compressed=False, indirect_length=None, extra=b''):
    if compressed: payload=zlib.compress(payload)
    length=str(len(payload)).encode() if indirect_length is None else f'{indirect_length} 0 R'.encode()
    dictionary=b'<< /Length '+length+(b' /Filter /FlateDecode' if compressed else b'')+extra+b' >>'
    return dictionary+b'\nstream\n'+payload+b'\nendstream'


def objects():
    content1=b'q 0.05 0.35 0.75 rg 16 130 208 140 re f Q\nBT /F1 16 Tf 20 290 Td (DeepRecover PDF 1/2) Tj ET\n'
    content2=b'q 0.95 0.45 0.08 rg 16 70 208 180 re f Q\nBT /F1 16 Tf 20 290 Td (Local / read-only 2/2) Tj ET\n'
    resources=b'/Resources << /Font << /F1 5 0 R >> >>'
    return {
        1:b'<< /Type /Catalog /Pages 2 0 R >>',
        2:b'<< /Type /Pages /Kids [3 0 R 6 0 R] /Count 2 /MediaBox [0 0 240 320] >>',
        3:b'<< /Type /Page /Parent 2 0 R '+resources+b' /Contents 4 0 R >>',
        4:stream(content1,compressed=True),
        5:b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
        6:b'<< /Type /Page /Parent 2 0 R '+resources+b' /Contents 7 0 R >>',
        7:stream(content2,compressed=True),
    }


def make_pdf(values=None, *, trailer=b'', comments=b''):
    values=objects() if values is None else values
    data=b'%PDF-1.4\n%\xe2\xe3\xcf\xd3\n'+comments
    offsets={}
    for number,body in values.items():
        offsets[number]=len(data)
        data+=f'{number} 0 obj\n'.encode()+body+b'\nendobj\n'
    xref=len(data); size=max(values)+1
    data+=f'xref\n0 {size}\n'.encode()+b'0000000000 65535 f \n'
    for number in range(1,size):
        data+=(f'{offsets[number]:010d} 00000 n \n'.encode() if number in values else b'0000000000 00000 f \n')
    return data+b'trailer\n<< /Size '+str(size).encode()+b' /Root 1 0 R '+trailer+b' >>\nstartxref\n'+str(xref).encode()+b'\n%%EOF\n'
