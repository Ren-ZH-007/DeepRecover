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
        'interrupted': ('已中断', '上次运行未正常结束，仅展示已提交记录，不自动续采或续扫。', 'warning'),
    }, ('状态未知', '没有可解释的任务状态，请查看本地记录。', 'warning'))


def task_kind(value):
    return _view(value, {
        'android_discovery': ('检测 Android 设备', '读取本机 Android 设备状态。', 'neutral'),
        'android_listing': ('列出授权文件', '只读列出已授权设备共享存储中的普通文件。', 'neutral'),
        'android_file_listing': ('列出授权文件', '只读列出已授权设备共享存储中的普通文件。', 'neutral'),
        'android_acquisition': ('采集授权文件', '将选中的现存普通文件复制到本地工作目录。', 'neutral'),
        'ios_discovery': ('发现 iPhone 备份', '查找本地备份目录。', 'neutral'),
        'ios_manifest': ('索引 iPhone 备份', '只读索引备份中的文件记录。', 'neutral'),
        'signature_scan': ('图片签名扫描', '寻找连续图片签名候选，不能判断删除状态。', 'neutral'),
        'computer_scan': ('电脑磁盘 / 镜像扫描', '根据所选模式检查删除记录或图片签名候选。', 'neutral'),
        'image_preview': ('图片预览', '在内存中校验并解码选中图片。', 'neutral'),
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
