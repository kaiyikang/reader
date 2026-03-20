# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "EbookLib",
#     "beautifulsoup4",
# ]
# ///

import sys
import random
from pathlib import Path
from ebooklib import epub
from bs4 import BeautifulSoup

def extract_random_samples(epub_path: Path, output_path: Path, num_chapters: int = 5, paras_per_chapter: int = 20):
    book = epub.read_epub(str(epub_path))
    
    # 1. 收集所有 HTML 章节
    all_chapters = [item for item in book.get_items() if isinstance(item, epub.EpubHtml)]
    
    valid_chapters = []
    # 2. 预处理：提取所有段落并过滤无效章节（少于10个段落的通常是目录、封面等）
    for ch in all_chapters:
        soup = BeautifulSoup(ch.get_content(), 'html.parser')
        # 提取目标标签，且过滤掉嵌套标签以防重复提取
        tags = soup.find_all(['p', 'h1', 'h2', 'h3', 'div', 'li'])
        valid_tags = [tag for tag in tags if not tag.find(['p', 'div', 'li'])]
        
        paras = [tag.get_text(strip=True) for tag in valid_tags if tag.get_text(strip=True)]
        
        if len(paras) >= 10:  # 只保留有实质内容的章节
            valid_chapters.append({
                "name": ch.get_name(),
                "paras": paras
            })

    # 3. 随机抽取指定数量的章节
    sample_size = min(num_chapters, len(valid_chapters))
    if sample_size == 0:
        print("❌ 未在 EPUB 中找到包含有效文本的章节。")
        return
        
    selected_chapters = random.sample(valid_chapters, sample_size)
    
    # 4. 格式化输出内容
    output_lines = []
    output_lines.append(f"📚 书籍: {epub_path.name}")
    output_lines.append(f"🎲 随机抽样评估: {sample_size} 个章节，每章前 {paras_per_chapter} 段\n")
    output_lines.append("=" * 60)
    
    for ch in selected_chapters:
        output_lines.append(f"\n📑 章节来源: {ch['name']}")
        output_lines.append("-" * 40)
        
        # 截取前 20 段
        chapter_paras = ch['paras'][:paras_per_chapter]
        for p in chapter_paras:
            output_lines.append(p)
            output_lines.append("")  # 空行分隔段落，呈现双语对照的最佳排版
            
        output_lines.append("=" * 60)

    # 5. 写入文件
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(output_lines))
        
    print(f"✅ 提取成功！随机抽取了 {sample_size} 个章节，已保存至: {output_path}")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: uv run extract.py <your_bilingual_book.epub>")
        sys.exit(1)
        
    epub_file = Path(sys.argv[1])
    out_file = epub_file.with_name(f"{epub_file.stem}_random_sample.txt")
    
    extract_random_samples(epub_file, out_file)