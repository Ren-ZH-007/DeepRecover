"""One-shot PDF rasterization worker; input/output are anonymous pipes only."""
import argparse
import json
import os
from pathlib import Path
import sys

if not __package__:
    sys.path.insert(0,str(Path(__file__).resolve().parents[2]))


def pipe(name, handle, mode):
    stream = getattr(sys,name,None)
    if stream is not None and getattr(stream,'buffer',None) is not None:
        return stream.buffer
    # PyInstaller --windowed intentionally sets sys.stdin/stdout to None.
    # The parent explicitly supplies real anonymous pipe handles on Windows.
    if sys.platform == 'win32':
        import ctypes
        import msvcrt
        kernel = ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.GetStdHandle.argtypes=[ctypes.c_ulong]; kernel.GetStdHandle.restype=ctypes.c_void_p
        value=kernel.GetStdHandle(handle & 0xffffffff)
        if value in (None,ctypes.c_void_p(-1).value): raise OSError('Missing pipe')
        flags=os.O_BINARY | (os.O_RDONLY if mode=='rb' else os.O_WRONLY)
        return os.fdopen(msvcrt.open_osfhandle(value,flags),mode)
    return os.fdopen(0 if mode=='rb' else 1,mode,closefd=False)


def main():
    parser=argparse.ArgumentParser(add_help=False)
    parser.add_argument('--pdf-render-worker',action='store_true')
    parser.add_argument('--page',type=int,required=True);parser.add_argument('--zoom',type=int,required=True)
    args=parser.parse_args()
    from core.tasks import CancellationToken
    from core.preview.pdf import inspect_pdf, MAX_RENDER_PIXELS, MAX_RENDER_BYTES, ZOOMS
    from core.scanner.pdf.validation import MAX_BYTES, MAX_PAGE_POINTS
    try:
        if sys.platform != 'win32':
            import resource
            resource.setrlimit(resource.RLIMIT_CPU,(6,6))
        source=pipe('stdin',-10,'rb');output=pipe('stdout',-11,'wb')
        data=source.read(MAX_BYTES+1)
        token=CancellationToken(); details=inspect_pdf(data,token)
        if args.zoom not in ZOOMS or not 0 <= args.page < details['pages']: return 2
        from PySide6.QtCore import QByteArray, QBuffer, QIODevice, QSize, QLoggingCategory
        from PySide6.QtGui import QGuiApplication
        from PySide6.QtPdf import QPdfDocument, QPdfDocumentRenderOptions
        QLoggingCategory.setFilterRules('qt.pdf.*=false')
        app=QGuiApplication.instance() or QGuiApplication([])
        buffer=QBuffer();buffer.setData(QByteArray(data));buffer.open(QIODevice.OpenModeFlag.ReadOnly)
        document=QPdfDocument();document.load(buffer)
        if document.status() != QPdfDocument.Status.Ready or document.pageCount() != details['pages']: return 3
        point=document.pagePointSize(args.page)
        if not 0 < point.width() <= MAX_PAGE_POINTS or not 0 < point.height() <= MAX_PAGE_POINTS: return 4
        factor=min(768/point.width(),1024/point.height())*args.zoom/100
        width,height=max(1,round(point.width()*factor)),max(1,round(point.height()*factor))
        if width*height > MAX_RENDER_PIXELS: return 4
        options=QPdfDocumentRenderOptions() # Annotation rendering is disabled.
        image=document.render(args.page,QSize(width,height),options)
        if image.isNull(): return 5
        encoded=QBuffer();encoded.open(QIODevice.OpenModeFlag.WriteOnly)
        if not image.save(encoded,'PNG') or encoded.size()>MAX_RENDER_BYTES-256: return 5
        document.close();buffer.close();buffer.setData(QByteArray())
        output.write(json.dumps({'width':width,'height':height},separators=(',',':')).encode()+b'\n')
        output.write(bytes(encoded.data()));output.flush()
        return 0
    except Exception:
        return 6


if __name__ == '__main__':
    raise SystemExit(main())
