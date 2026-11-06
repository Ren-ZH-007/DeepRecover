"""Verified, local PDF bytes and cancellable one-page renderer processes."""
from dataclasses import dataclass
from io import BytesIO
import json
import os
from pathlib import Path
import subprocess
import sys
from time import monotonic
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from PySide6.QtGui import QImage
from core.candidate_io import CandidateAccessError, candidate_input, read_candidate
from core.scanner.pdf.validation import validate, MAX_BYTES, MAX_PAGES

MAX_RENDER_PIXELS = 4_000_000
MAX_RENDER_BYTES = 20 * 1024 * 1024
RENDER_TIMEOUT = 8
MAX_RENDER_RSS = 512 * 1024 * 1024
ZOOMS = (50,75,100,125,150,200)
MESSAGES = {
    'pdf_encrypted':'加密 PDF 暂不预览，不尝试密码或解密。',
    'pdf_active_content':'PDF 含脚本、表单、附件或其他活动内容，已拒绝预览。',
    'pdf_external_reference':'PDF 含外部引用，已拒绝预览且不会访问引用地址。',
    'pdf_unsupported':'本版仅预览传统交叉引用表的静态 PDF；对象流、交叉引用流等布局暂不支持。',
    'pdf_incremental':'增量修订 PDF 暂不支持，不将旧修订作为最终文档预览。',
    'pdf_validation_limit':'PDF 超过文件、对象、页数或解压检查上限。',
    'pdf_boundary_unknown':'PDF 候选边界未确认，不能预览。',
}


@dataclass(frozen=True)
class PdfPreview:
    data: bytes
    pages: int
    image: 'QImage'


def inspect_pdf(data, token):
    if not isinstance(data,bytes) or not 1 <= len(data) <= MAX_BYTES:
        raise CandidateAccessError('PDF 输入为空或超过 16 MiB 预览上限。')
    result = validate(BytesIO(data),0,len(data),token)
    if result.integrity != 'pdf_structure_checked':
        raise CandidateAccessError(MESSAGES.get(result.integrity,'PDF 内容截断或不符合支持的结构要求。'))
    if data[result.size:].strip(b'\0\t\n\f\r '):
        raise CandidateAccessError('PDF 结尾存在未验证内容，不能预览为完整文档。')
    return result.details


def renderer_command(page, zoom):
    if getattr(sys,'frozen',False):
        return [sys.executable,'--pdf-render-worker','--page',str(page),'--zoom',str(zoom)]
    return [sys.executable,str(Path(__file__).with_name('pdf_worker.py')),'--page',str(page),'--zoom',str(zoom)]


def renderer_memory(process):
    """Observed resident memory, not virtual address reservations or a hard quota."""
    try:
        if sys.platform.startswith('linux'):
            for line in Path(f'/proc/{process.pid}/status').read_text().splitlines():
                if line.startswith('VmRSS:'): return int(line.split()[1]) * 1024
        elif sys.platform == 'darwin':
            result = subprocess.run(['/bin/ps','-o','rss=','-p',str(process.pid)],
                capture_output=True,timeout=0.25,check=False)
            if result.returncode == 0 and result.stdout.strip(): return int(result.stdout) * 1024
        elif sys.platform == 'win32':
            import ctypes
            from ctypes import wintypes
            class Counters(ctypes.Structure):
                _fields_=[('cb',wintypes.DWORD),('PageFaultCount',wintypes.DWORD)]+[(name,ctypes.c_size_t) for name in (
                    'PeakWorkingSetSize','WorkingSetSize','QuotaPeakPagedPoolUsage','QuotaPagedPoolUsage',
                    'QuotaPeakNonPagedPoolUsage','QuotaNonPagedPoolUsage','PagefileUsage','PeakPagefileUsage')]
            counters=Counters();counters.cb=ctypes.sizeof(counters)
            kernel=ctypes.WinDLL('kernel32',use_last_error=True)
            method=kernel.K32GetProcessMemoryInfo
            method.argtypes=[wintypes.HANDLE,ctypes.POINTER(Counters),wintypes.DWORD];method.restype=wintypes.BOOL
            if method(int(process._handle),ctypes.byref(counters),counters.cb): return counters.WorkingSetSize
    except (OSError,ValueError,AttributeError,subprocess.TimeoutExpired):
        pass
    return None


def render_pdf(data, page, zoom, token):
    if type(page) is not int or not 0 <= page < MAX_PAGES or zoom not in ZOOMS:
        raise CandidateAccessError('无效的 PDF 页码或缩放比例。')
    if not isinstance(data,bytes) or not 1 <= len(data) <= MAX_BYTES:
        raise CandidateAccessError('PDF 输入超过预览上限。')
    token.checkpoint()
    environment = dict(os.environ, QT_QPA_PLATFORM='offscreen')
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
    try:
        process = subprocess.Popen(renderer_command(page,zoom),stdin=subprocess.PIPE,stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,env=environment,creationflags=flags)
    except OSError as error:
        raise CandidateAccessError('PDF 渲染组件无法启动。') from error
    try:
        deadline = monotonic() + RENDER_TIMEOUT
        first = True
        while True:
            token.checkpoint()
            resident = renderer_memory(process)
            if resident is not None and resident > MAX_RENDER_RSS:
                raise CandidateAccessError('PDF 渲染内存超过 512 MiB，已停止本页预览。')
            if monotonic() >= deadline:
                raise CandidateAccessError('PDF 单页渲染超过 8 秒，已停止本页预览。')
            try:
                output,_ = process.communicate(input=data if first else None,timeout=0.05)
                break
            except subprocess.TimeoutExpired:
                first = False
        token.checkpoint()
        if process.returncode != 0 or len(output) > MAX_RENDER_BYTES:
            raise CandidateAccessError('PDF 页面无法在当前资源限制内渲染。')
        header,separator,png = output.partition(b'\n')
        if not separator or len(header) > 256:
            raise CandidateAccessError('PDF 渲染结果无效。')
        try:
            metadata = json.loads(header)
            width,height = metadata['width'],metadata['height']
        except (ValueError,KeyError,TypeError) as error:
            raise CandidateAccessError('PDF 渲染结果无效。') from error
        if type(width) is not int or type(height) is not int or not 0 < width * height <= MAX_RENDER_PIXELS or width <= 0 or height <= 0:
            raise CandidateAccessError('PDF 渲染尺寸超过上限。')
        if not png.startswith(b'\x89PNG\r\n\x1a\n'):
            raise CandidateAccessError('PDF 渲染结果无效。')
        # Verify the PNG's declared dimensions before native allocation.
        if len(png) < 24 or int.from_bytes(png[16:20],'big') != width or int.from_bytes(png[20:24],'big') != height:
            raise CandidateAccessError('PDF 渲染尺寸不一致。')
        # Qt is only needed to decode rendered pixels; structure checks stay importable without it.
        from PySide6.QtGui import QImage, QImageReader
        QImageReader.setAllocationLimit(64)
        image = QImage.fromData(png,'PNG')
        if image.isNull() or image.width() != width or image.height() != height:
            raise CandidateAccessError('PDF 页面图像无法解码。')
        token.checkpoint(); return image
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate()


def prepare_pdf(repository, candidate_id, data_dir, token):
    item = candidate_input(repository,candidate_id,data_dir)
    if str(item.candidate.get('file_type','')).lower() != 'pdf':
        raise CandidateAccessError('本版文档预览仅支持 PDF。')
    if item.size > MAX_BYTES:
        raise CandidateAccessError('PDF 超过 16 MiB 预览上限。')
    data = read_candidate(repository,candidate_id,data_dir,token,max_bytes=MAX_BYTES)
    details = inspect_pdf(data,token)
    image = render_pdf(data,0,100,token)
    return PdfPreview(data,details['pages'],image)
