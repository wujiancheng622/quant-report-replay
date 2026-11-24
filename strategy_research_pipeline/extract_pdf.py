from pathlib import Path
import json
import sys

import pdfplumber


def main():
    source,destination = map(Path,sys.argv[1:3])
    pages = []
    coverage = []
    first_line = 3
    with pdfplumber.open(source) as pdf:
        for i,page in enumerate(pdf.pages,1):
            body = (page.extract_text() or '').strip()
            text_characters = len(body)
            if not body:
                body = '[本页没有可抽取文字，请查看原页图像。]'
            last_line = first_line + body.count('\n')
            coverage.append(dict(page=i,text_characters=text_characters,has_image=bool(page.images),
                                 first_line=first_line,last_line=last_line))
            pages.append(f'\n--- PDF 第 {i} 页 ---\n{body}')
            first_line = last_line + 3
    if not pages:
        raise ValueError('PDF 没有可读页面')
    destination.write_text('\n'.join(pages), encoding='utf-8')
    if len(sys.argv)>3:
        Path(sys.argv[3]).write_text(json.dumps(dict(pages=coverage),ensure_ascii=False)+'\n',encoding='utf-8')


if __name__ == '__main__':
    main()
