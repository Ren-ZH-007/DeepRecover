# 音频测试样本

由 ffmpeg（libmp3lame、内置 AAC 编码器）用 `-fflags +bitexact` 从正弦波生成的真实文件，用于检查音频结构校验能否正确处理真实编码器的输出。不含任何个人信息。

| 文件 | 内容 | 用途 |
|---|---|---|
| `tone_pcm16.wav` | 8 kHz 单声道 16 位 PCM，1 秒 | WAV PCM |
| `tone_float.wav` | 8 kHz 立体声 32 位浮点，0.2 秒 | WAV 浮点 |
| `tone_44k.mp3` | 44.1 kHz 立体声 128 kbit/s，带 Info 头 | MPEG-1 Layer III |
| `tone_cbr.mp3` | 8 kHz 单声道 32 kbit/s，带 Info 头 | MPEG-2.5 固定码率 |
| `tone_vbr.mp3` | 8 kHz 立体声可变码率，带 Xing 头 | 可变码率 |
| `tone_id3v1.mp3` | 同 `tone_cbr.mp3`，末尾另有 ID3v1 标签 | 尾部标签计入边界 |
| `tone_noxing.mp3` | 同 `tone_cbr.mp3`，不写 Info 头 | 边界无法确认，不能导出 |
| `tone.m4a` | 8 kHz AAC，moov 在 mdat 之后 | M4A |
| `tone_faststart.m4a` | 同上，moov 在 mdat 之前 | M4A（faststart） |
