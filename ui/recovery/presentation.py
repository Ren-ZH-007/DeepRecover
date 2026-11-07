"""Explain stored evidence without changing candidate or export semantics."""
from dataclasses import dataclass
import re


@dataclass(frozen=True)
class DisplayText:
    label: str
    explanation: str
    raw: str = ''
    tone: str = 'neutral'

    @property
    def tooltip(self):
        return self.explanation + (f'\n记录值：{self.raw}' if self.raw else '')


def _view(value, choices, unknown):
    code = str(value or '')
    label, explanation, tone = choices.get(code, unknown)
    return DisplayText(label, explanation, code, tone)


def candidate_status(value):
    return _view(value, {
        'candidate': ('候选', '已保存候选记录；预览和导出仍需检查来源与内容，不表示恢复成功。', 'info'),
        'partial': ('部分 / 受限', '检查范围或文件布局不完整，不能作为完整文件导出。', 'warning'),
        'failed': ('检查失败', '候选检查未通过，不能据此认定内容可用。', 'danger'),
        'validated': ('历史检查标记', '此为历史候选状态；请分别查看内容检查和独立导出记录。', 'neutral'),
        'exported': ('历史导出标记', '旧候选保留的导出状态不替代独立导出记录或当前目标校验。', 'neutral'),
    }, ('状态未知', '没有可解释的候选状态，不推断是否可用或已导出。', 'warning'))


def content_check(value):
    return _view(value, {
        'unknown': ('未验证', '记录未提供可解释的内容检查结果。', 'neutral'),
        'not_checked': ('未检查内容', '仅采集或索引文件；摘要或文件存在不证明内容完整。', 'neutral'),
        'metadata_checked': ('布局检查通过', '支持的文件布局与分配检查通过；不证明内容从未覆盖、完整或可解码。', 'info'),
        'structure_checked': ('图片结构检查通过', '支持范围内的图片容器结构通过检查；不等于完整解码、内容完整或文件曾被删除。', 'info'),
        'video_structure_checked': ('视频容器与采样范围检查通过', '已核对支持的容器、采样表和媒体范围；候选止于最后一个必需盒的末尾，未推断尾随数据。未逐帧解码，不能证明内容完整或曾被删除。', 'info'),
        'video_truncated': ('视频内容截断', '尚未覆盖所有必需容器与媒体范围；不能预览或导出此候选。', 'warning'),
        'video_invalid_structure': ('视频结构检查失败', '容器或采样表不一致，或采样字节越过媒体数据范围。', 'danger'),
        'video_boundary_unknown': ('视频边界未确认', '容器使用延伸至文件末尾的长度，无法从镜像确认原文件边界。', 'warning'),
        'video_validation_limit': ('达到视频检查限制', '超过单项 64 MiB、元数据 8 MiB 或表项与检查次数上限；未完成验证。', 'warning'),
        'video_unsupported': ('暂不支持此视频布局', '当前签名检查支持单 H.264 视频轨及可选 AAC 音轨的自包含 MP4/MOV；此候选超出支持范围。', 'warning'),
        'video_fragmented': ('分段视频暂不支持', '检测到分段容器；本版不拼接分段或猜测碎片范围。', 'warning'),
        'video_encrypted': ('加密视频暂不支持', '检测到加密容器或媒体标记；不尝试解密。', 'warning'),
        'video_external_reference': ('视频含外部引用', '媒体依赖外部来源或其他引用；不会访问其地址。', 'warning'),
        'pdf_structure_checked': ('PDF 结构与对象范围检查通过', '已核对传统交叉引用表、对象范围、流长度和页树；范围止于已校验 EOF 行。未渲染全部页面，不证明内容完整或曾被删除。', 'info'),
        'pdf_truncated': ('PDF 内容截断', '未找到完整的受支持结构与 EOF 边界，不能预览或导出。', 'warning'),
        'pdf_invalid_structure': ('PDF 结构检查失败', '交叉引用、对象范围、流长度或页树不符合当前支持的结构要求。', 'danger'),
        'pdf_validation_limit': ('达到 PDF 检查限制', '超过 16 MiB、200 页、对象、语法或解压上限，未完成验证。', 'warning'),
        'pdf_boundary_unknown': ('PDF 边界未确认', '检查范围末尾仍可能存在后续内容，不能将其作为完整候选。', 'warning'),
        'pdf_unsupported': ('暂不支持此 PDF 布局', '首版支持传统交叉引用表；交叉引用流、对象流及其他未支持布局保留为受限项。', 'warning'),
        'pdf_incremental': ('PDF 增量修订暂不支持', '不能将较早修订或缺少上下文的后续修订作为最终文档。', 'warning'),
        'pdf_encrypted': ('加密 PDF 暂不支持', '检测到加密标记，不尝试密码或解密。', 'warning'),
        'pdf_active_content': ('PDF 含活动内容', '脚本、表单、附件等活动内容不在静态预览范围内，不执行这些内容。', 'warning'),
        'pdf_external_reference': ('PDF 含外部引用', '不读取引用的外部文档、文件或网络地址。', 'warning'),
        'office_structure_checked': ('Office 文档结构检查通过', '新版文档：已核对 ZIP 条目与中央目录、逐项解压校验 CRC-32，并确认内容类型和主文档部件；范围止于中央目录结束记录。旧版文档：已核对复合文档扇区表、目录树和每个数据流的扇区链；范围止于最高的已分配扇区。未打开或渲染文档，不能证明内容完整或曾被删除。', 'info'),
        'office_truncated': ('Office 文档截断', '未找到完整的结束记录或所需扇区就到达来源结尾，不能导出。', 'warning'),
        'office_invalid_structure': ('Office 结构检查失败', 'ZIP 条目、CRC-32、内容类型、关系或复合文档扇区链不一致。', 'danger'),
        'office_validation_limit': ('达到 Office 检查限制', '超过单项 64 MiB、解压总量、条目或目录项上限，未完成验证。', 'warning'),
        'office_unsupported': ('暂不支持此 Office 布局', '分卷或 ZIP64 压缩包、非常见压缩方式，或无法识别的主文档类型。', 'warning'),
        'office_encrypted': ('加密 Office 文档暂不支持', '检测到加密的压缩条目或加密的文档包，不尝试密码或解密。', 'warning'),
        'office_active_content': ('Office 文档含宏或活动内容', '检测到 VBA 宏或 ActiveX 控件；这类内容不在静态检查范围内，不能导出。', 'warning'),
        'audio_structure_checked': ('音频结构检查通过', 'WAV：已核对 RIFF 长度、格式块与数据块；MP3：已逐帧核对帧头，总长度与首帧记录的字节数一致；M4A：已核对容器、采样表和媒体范围。未解码播放，不能证明内容完整或曾被删除。', 'info'),
        'audio_truncated': ('音频内容截断', '未覆盖完整的音频数据或帧就到达来源结尾，不能导出。', 'warning'),
        'audio_invalid_structure': ('音频结构检查失败', '格式字段、块长度、帧序列或采样范围不一致。', 'danger'),
        'audio_validation_limit': ('达到音频检查限制', '超过单项 64 MiB、帧数或块数上限，未完成验证。', 'warning'),
        'audio_unsupported': ('暂不支持此音频布局', '当前支持 PCM / 浮点 / A-law / μ-law WAV、带 ID3v2 标签的 MP3 Layer III，以及单 AAC 音轨的 M4A。', 'warning'),
        'audio_boundary_unknown': ('音频边界未确认', 'MP3 缺少记录总字节数的 Xing / Info / VBRI 头，或 WAV 未写入总长度，无法确认文件在哪里结束。', 'warning'),
        'audio_fragmented': ('分段音频暂不支持', '检测到分段容器；不拼接分段或猜测碎片范围。', 'warning'),
        'audio_encrypted': ('加密音频暂不支持', '检测到受保护或加密的媒体标记；不尝试解密。', 'warning'),
        'audio_external_reference': ('音频含外部引用', '媒体依赖外部来源或其他引用；不会访问其地址。', 'warning'),
        'missing': ('来源文件缺失', '索引时未找到可访问的来源文件；历史记录仍可查看。', 'danger'),
        'truncated': ('内容截断', '检查尚未完成就到达来源结尾，当前长度不是完整文件边界。', 'warning'),
        'invalid_structure': ('结构检查失败', '图片容器不符合支持的结构要求。', 'danger'),
        'validation_limit': ('达到检查限制', '超出当前检查资源或格式范围，未完成内容验证。', 'warning'),
        'size_limit': ('超出大小限制', '超过当前单文件处理上限，不能作为完整文件导出。', 'warning'),
        'layout_limit': ('来源范围受限', '文件来源范围未通过数量、长度或边界检查。', 'warning'),
        'unsupported_layout': ('暂不支持此布局', '压缩、加密、稀疏或扩展布局不在当前支持范围。', 'warning'),
        'layout_unverified': ('文件布局未确认', '缺少可靠的簇链或连续布局依据，不猜测文件的完整范围。', 'warning'),
        'allocated_clusters': ('数据区域已被使用', '记录的数据簇已分配给其他内容，不能认定为原文件字节。', 'warning'),
        'allocation_unknown': ('分配状态未确认', '无法可靠确认数据区域是否仍空闲。', 'warning'),
        'invalid_record': ('记录校验不足', '文件系统记录未通过必要校验，不使用其范围导出完整文件。', 'danger'),
        'invalid_metadata': ('元数据检查失败', '文件系统元数据不完整或不一致，文件布局未确认。', 'danger'),
        'invalid_extent': ('来源范围无效', '记录的来源范围无效或越界，不能读取为完整文件。', 'danger'),
        'uninitialized_data': ('内容范围未初始化', '文件记录未确认全部内容已初始化，不能作为完整文件导出。', 'warning'),
    }, ('未验证的检查结果', '当前版本不能解释此检查结果，不推断内容完整性。', 'warning'))


def deletion_evidence(candidate):
    source = candidate.get('source', '')
    state = candidate.get('deletion_state', 'unknown')
    raw = f"source={source}; deletion_state={state}"
    if source == 'android_file':
        return DisplayText('普通文件采集', '这是经授权采集的现存普通文件副本，采集不提供文件曾被删除的证据。', raw)
    if source == 'ios_backup':
        return DisplayText('备份文件索引', '这是备份中的文件索引；不能判断该文件在手机上的当前删除状态。', raw)
    if source in ('signature_scan', 'computer_signature'):
        return DisplayText('未知（签名候选）', '按文件签名找到连续内容，可能来自现存文件；不能判断是否曾被删除。', raw)
    if source == 'computer_deleted':
        if state == 'deleted_record':
            return DisplayText('文件系统删除记录', '读取到了文件系统的删除元数据；当前内容可能已被覆盖，仍须单独查看布局和来源检查。', raw, 'info')
        if state == 'unverified_record':
            return DisplayText('记录校验不足', '记录未通过必要校验，不能把它认定为可靠的删除记录。', raw, 'warning')
    return DisplayText('删除状态未知', '没有可解释的删除依据，不能从名称、摘要或候选状态推断文件曾被删除。', raw, 'warning')


def duplicate_label(info):
    """Short text for the duplicate column; ``info`` is (copies, is_earliest) or None."""
    if not info:
        return ''
    copies, earliest = info
    return f'共 {copies} 份 · 最早一项' if earliest else f'共 {copies} 份 · 重复'


def modified_time_note(candidate):
    """Where a shown modification time came from and how to read it."""
    origin = {'android_file': '设备文件系统', 'ios_backup': '备份索引 Manifest.db'}.get(
        candidate.get('source'), f"{candidate.get('file_system') or '文件系统'} 删除记录")
    caveat = '删除记录中的时间可能已被改写，仅供参考。' if candidate.get('source') == 'computer_deleted' else ''
    if candidate.get('modified_basis') == 'utc':
        return f'{origin}中的修改时间为 UTC，已换算为本机时区显示。{caveat}'
    return f'{origin}中的修改时间只有本地时钟、没有时区信息，按记录原样显示。{caveat}'


def export_guidance():
    return DisplayText('查看独立导出记录',
        '此处不判断是否已导出。导出操作保留独立批次记录；如已导出，请从“任务记录”打开该批次。'
        '摘要一致仅证明当次读出的来源字节与输出相同，不表示已删除数据恢复成功；历史目标不会自动重新校验。')


def task_status(value):
    return _view(value, {
        'queued': ('等待开始', '任务已入库，尚未开始执行。', 'neutral'),
        'running': ('执行中', '任务正在运行，显示的候选或导出结果可能仍不完整。', 'info'),
        'completed': ('已完成', '任务流程已完成；候选完整性与逐项导出结果分别记录。', 'info'),
        'failed': ('失败', '任务未完整完成，已入库的部分结果仍可回读。', 'danger'),
        'cancelled': ('已停止', '已请求停止并保存已提交记录，不表示完整扫描或导出。', 'warning'),
        'interrupted': ('已中断', '上次运行未正常结束，仅展示已提交记录，不会自动继续；签名扫描可在任务记录中手动从断点继续。', 'warning'),
    }, ('状态未知', '没有可解释的任务状态，请查看本地记录。', 'warning'))


def task_kind(value):
    return _view(value, {
        'android_discovery': ('检测 Android 设备', '读取本机 Android 设备状态。', 'neutral'),
        'android_listing': ('列出授权文件', '只读列出已授权设备共享存储中的普通文件。', 'neutral'),
        'android_file_listing': ('列出授权文件', '只读列出已授权设备共享存储中的普通文件。', 'neutral'),
        'android_acquisition': ('采集授权文件', '将选中的现存普通文件复制到本地工作目录。', 'neutral'),
        'ios_discovery': ('发现 iPhone 备份', '查找本地备份目录。', 'neutral'),
        'ios_manifest': ('索引 iPhone 备份', '只读索引备份中的文件记录。', 'neutral'),
        'signature_scan': ('图片 / 视频 / PDF 签名扫描', '寻找连续 JPG / PNG / MP4 / MOV / PDF 签名候选，不能判断删除状态。', 'neutral'),
        'computer_scan': ('电脑磁盘 / 镜像扫描', '根据所选模式检查删除记录或图片 / 视频签名候选。', 'neutral'),
        'image_preview': ('图片预览', '在内存中校验并解码选中图片。', 'neutral'),
        'pdf_preview': ('PDF 预览准备', '只读校验来源并在独立进程渲染静态页面，不代表全部内容完整。', 'neutral'),
        'video_preview': ('视频预览准备', '只读校验来源并准备内存播放内容；不代表完成解码或恢复。', 'neutral'),
        'export': ('校验导出', '将选中来源字节写入独立批次并比对摘要。', 'neutral'),
    }, ('其他任务', '任务类型由历史记录保存，当前版本没有对应说明。', 'neutral'))


def source_kind(value):
    return _view(value, {
        'android': ('Android 授权来源', '已授权设备文件的本地采集来源。', 'neutral'),
        'ios_backup': ('iPhone 本地备份', '本地备份来源。', 'neutral'),
        'image': ('本地文件 / 镜像', '本地普通文件或磁盘镜像来源。', 'neutral'),
        'computer': ('电脑磁盘 / 镜像', '电脑原始设备或磁盘镜像来源。', 'neutral'),
    }, ('历史来源', '来源类型保存在历史记录中。', 'neutral'))


def error_text(value):
    return _view(value, {
        '': ('', '记录未保存错误类型。', 'neutral'),
        'TaskCancelled': ('任务已停止', '任务响应停止请求后结束。', 'warning'),
        'PermissionError': ('没有访问权限', '系统拒绝访问文件或目录；应用不会自动提权。', 'danger'),
        'DiskError': ('磁盘访问检查未通过', '磁盘权限、身份或物理盘隔离检查未通过。', 'danger'),
        'ComputerError': ('电脑来源检查未通过', '电脑来源范围、格式或扫描限制检查未通过。', 'danger'),
        'CandidateAccessError': ('候选访问检查未通过', '来源身份、摘要、文件存在性或可用范围检查未通过。', 'danger'),
        'ExportError': ('导出检查未通过', '来源、目标或逐项校验未满足导出要求。', 'danger'),
        'BackupError': ('备份检查未通过', '备份状态或文件索引检查未通过。', 'danger'),
        'ScanError': ('扫描未完整完成', '扫描遇到来源或检查限制，已提交候选仍可查看。', 'danger'),
        'AdbError': ('设备通信未完成', '授权状态或 Android 工具通信未完成。', 'danger'),
        'OSError': ('文件访问未完成', '本地文件或目录访问未完成。', 'danger'),
        'ValueError': ('记录或参数无效', '记录或参数未通过检查。', 'danger'),
        'RuntimeError': ('操作未完成', '操作未完整完成，技术类型保留在提示中。', 'danger'),
    }, ('操作未完成', '当前版本未提供该错误的专门说明，技术类型保留在提示中。', 'danger'))


def export_status(record):
    value = record.get('status', '')
    if value == 'exported':
        source, target = record.get('source_sha256'), record.get('target_sha256')
        if (isinstance(source, str) and re.fullmatch('[0-9a-fA-F]{64}', source)
                and isinstance(target, str) and source.lower() == target.lower()):
            return DisplayText('摘要校验通过', '记录显示当次来源与目标 SHA-256 一致；历史目标不会自动重新校验，字节一致不证明曾被删除或恢复成功。', value, 'info')
        return DisplayText('摘要记录不足', '记录标记为已导出，但缺少一致的有效来源与目标摘要；不将此标记解释为校验通过。', value, 'warning')
    return _view(value, {
        'queued': ('等待开始', '该项尚未开始导出。', 'neutral'),
        'running': ('正在导出', '该项导出尚未结束。', 'info'),
        'failed': ('导出失败', '该项未完成来源与目标校验。', 'danger'),
        'cancelled': ('已停止', '该项因停止请求结束，不作为已校验输出。', 'warning'),
        'interrupted': ('已中断', '该项上次运行未正常结束，不自动重验或续导。', 'warning'),
    }, ('导出状态未知', '没有可解释的逐项导出状态，不推断输出已通过校验。', 'warning'))


def video_check_details(candidate):
    details = candidate.get('structure_details')
    if candidate.get('integrity') != 'video_structure_checked' or not isinstance(details, dict):
        return ''
    if details.get('check_version') != 1 or details.get('boundary_basis') != 'last_required_box' or details.get('bitstream_decoded') is not False:
        return ''
    counts = [details.get(key) for key in ('tracks', 'samples', 'chunks', 'media_boxes')]
    if any(type(n) is not int or not 1 <= n <= 200000 for n in counts):
        return ''
    return f'检查版本 1 · {counts[0]} 个轨道 · {counts[1]} 个采样 · {counts[2]} 个块 · {counts[3]} 个媒体盒。边界：最后必需盒末尾；未逐帧解码。'


def office_check_details(candidate):
    details = candidate.get('structure_details')
    if candidate.get('integrity') != 'office_structure_checked' or not isinstance(details, dict):
        return ''
    if details.get('check_version') != 1 or details.get('rendered') is not False:
        return ''
    if details.get('container') == 'ooxml' and details.get('boundary_basis') == 'end_of_central_directory':
        entries, main = details.get('entries'), details.get('main_part')
        if type(entries) is int and 1 <= entries <= 10000 and isinstance(main, str) and 0 < len(main) <= 200:
            return f'检查版本 1 · 新版 Office 文档 · {entries} 个条目 · 主文档 {main}。边界：中央目录结束记录；未打开文档。'
    if details.get('container') == 'ole2' and details.get('boundary_basis') == 'highest_allocated_sector':
        streams, sector = details.get('streams'), details.get('sector_size')
        if type(streams) is int and 1 <= streams <= 10000 and sector in (512, 4096):
            return f'检查版本 1 · 旧版 Office 复合文档 · {streams} 个数据流 · 扇区 {sector} 字节。边界：最高已分配扇区；未打开文档。'
    return ''


def audio_check_details(candidate):
    details = candidate.get('structure_details')
    if candidate.get('integrity') != 'audio_structure_checked' or not isinstance(details, dict) or details.get('check_version') != 1:
        return ''
    kind = details.get('format')
    seconds = details.get('duration_seconds')
    rate = details.get('sample_rate')
    if kind in ('wav', 'mp3') and details.get('decoded') is False and type(rate) is int and 1 <= rate <= 1_000_000 \
            and isinstance(seconds, (int, float)) and not isinstance(seconds, bool) and 0 <= seconds <= 360000:
        if kind == 'wav' and details.get('boundary_basis') == 'riff_size' and details.get('encoding') in ('pcm', 'float', 'alaw', 'mulaw') \
                and type(details.get('channels')) is int and 1 <= details['channels'] <= 32 and details.get('bits_per_sample') in (8, 16, 24, 32, 64):
            return (f"检查版本 1 · WAV {details['encoding'].upper()} · {details['channels']} 声道 · {rate} Hz · "
                    f"{details['bits_per_sample']} 位 · 约 {seconds:.1f} 秒。边界：RIFF 长度；未解码。")
        if kind == 'mp3' and details.get('boundary_basis') in ('xing_byte_count', 'vbri_byte_count') \
                and type(details.get('frames')) is int and 2 <= details['frames'] <= 500000 and details.get('channels') in (1, 2):
            header = 'Xing / Info' if details['boundary_basis'] == 'xing_byte_count' else 'VBRI'
            return (f"检查版本 1 · MP3 · {details['frames']} 帧 · {rate} Hz · {details['channels']} 声道 · 约 {seconds:.1f} 秒。"
                    f"边界：{header} 头记录的字节数；未解码。")
    if kind == 'm4a' and details.get('boundary_basis') == 'last_required_box' and details.get('bitstream_decoded') is False:
        counts = [details.get(key) for key in ('samples', 'chunks', 'media_boxes')]
        if details.get('tracks') == 1 and all(type(n) is int and 1 <= n <= 200000 for n in counts):
            return f'检查版本 1 · M4A · 1 个 AAC 音轨 · {counts[0]} 个采样 · {counts[1]} 个块。边界：最后必需盒末尾；未解码。'
    return ''


def pdf_check_details(candidate):
    details=candidate.get('structure_details')
    if candidate.get('integrity')!='pdf_structure_checked' or not isinstance(details,dict):return ''
    if details.get('check_version')!=1 or details.get('boundary_basis')!='checked_eof_line' or details.get('rendered') is not False:return ''
    pages,objects=details.get('pages'),details.get('objects')
    if type(pages) is not int or type(objects) is not int or not 1<=pages<=200 or not 1<=objects<=10000:return ''
    return f'检查版本 1 · {pages} 页 · {objects} 个对象 · 单一修订。边界：已校验 EOF 行；未渲染全部页面。'
