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
    
    # 1. Collect all HTML chapters
    all_chapters = [item for item in book.get_items() if isinstance(item, epub.EpubHtml)]

    valid_chapters = []
    # 2. Preprocessing: extract all paragraphs and filter invalid chapters
    #    (chapters with fewer than 10 paragraphs are usually TOC, cover, etc.)
    for ch in all_chapters:
        soup = BeautifulSoup(ch.get_content(), 'html.parser')
        # Extract target tags and filter nested tags to avoid duplicate extraction
        tags = soup.find_all(['p', 'h1', 'h2', 'h3', 'div', 'li'])
        valid_tags = [tag for tag in tags if not tag.find(['p', 'div', 'li'])]

        paras = [tag.get_text(strip=True) for tag in valid_tags if tag.get_text(strip=True)]

        if len(paras) >= 10:  # Only keep chapters with substantial content
            valid_chapters.append({
                "name": ch.get_name(),
                "paras": paras
            })

    # 3. Randomly sample specified number of chapters
    sample_size = min(num_chapters, len(valid_chapters))
    if sample_size == 0:
        print("❌ No chapters with valid text found in EPUB.")
        return

    selected_chapters = random.sample(valid_chapters, sample_size)

    # 4. Format output content
    output_lines = []
    output_lines.append(f"📚 Book: {epub_path.name}")
    output_lines.append(f"🎲 Random Sampling: {sample_size} chapters, first {paras_per_chapter} paragraphs each\n")
    output_lines.append("=" * 60)

    for ch in selected_chapters:
        output_lines.append(f"\n📑 Chapter Source: {ch['name']}")
        output_lines.append("-" * 40)

        # Extract first N paragraphs
        chapter_paras = ch['paras'][:paras_per_chapter]
        for p in chapter_paras:
            output_lines.append(p)
            output_lines.append("")  # Empty line for paragraph separation

        output_lines.append("=" * 60)

    # 5. Write to file
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(output_lines))

    print(f"✅ Extraction successful! Randomly sampled {sample_size} chapters, saved to: {output_path}")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: uv run extract.py <your_bilingual_book.epub>")
        sys.exit(1)

    epub_file = Path(sys.argv[1])
    out_file = epub_file.with_name(f"{epub_file.stem}_random_sample.txt")
    
    extract_random_samples(epub_file, out_file)