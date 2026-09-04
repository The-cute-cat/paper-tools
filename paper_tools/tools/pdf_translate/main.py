"""模块运行入口：python -m paper_tools.tools.pdf_translate.main 文件.pdf"""

import sys

from main import main

if __name__ == "__main__":
    main(["pdf-translate", *sys.argv[1:]])
