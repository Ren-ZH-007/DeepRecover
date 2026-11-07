# Office 测试样本

由 LibreOffice 24.2 生成的真实文件，用于检查 Office 结构校验能否正确处理真实软件的输出。内容只有测试文字，作者和修改者字段为空。

| 文件 | 来源 | 用途 |
|---|---|---|
| `libreoffice.docx` | python-docx 生成，再由 LibreOffice 另存 | 首个条目为 `_rels/.rels` |
| `libreoffice.xlsx` | openpyxl 生成，再由 LibreOffice 另存 | 首个条目为 `xl/_rels/workbook.xml.rels`，不是常见的首条目名 |
| `libreoffice.pptx` | python-pptx 生成，再由 LibreOffice 另存 | 首个条目为 `_rels/.rels` |
| `libreoffice.doc` | LibreOffice 从上面的 docx 转换 | 旧版 Word 复合文档，含迷你流 |
| `libreoffice.xls` | LibreOffice 从上面的 xlsx 转换 | 旧版 Excel 复合文档，含迷你流 |
