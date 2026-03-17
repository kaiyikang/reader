# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "EbookLib",
#     "tiktoken",
#     "requests",
#     "beautifulsoup4",
# ]
# ///

#!/usr/bin/env python3
"""
EPUB 翻译器 - 使用 OpenRouter API (Gemini-3-flash-preview) 翻译电子书
运行方式: uv run translator.py <your_book.epub>
"""

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

import requests
import tiktoken
from bs4 import BeautifulSoup, NavigableString
from ebooklib import epub



# ==========================================
# 缓存管理与验证函数
# ==========================================
CACHE_VERSION = 1

def get_cache_path(epub_path: Path) -> Path:
    return epub_path.with_suffix('.cache.json')

def get_terms_path(epub_path: Path, target_lang: str) -> Path:
    lang_suffix = target_lang.lower().replace(" ", "_")
    return epub_path.with_name(f"{epub_path.stem}_{lang_suffix}_terms.json")

def load_cache(epub_path: Path) -> dict:
    cache_path = get_cache_path(epub_path)
    if cache_path.exists():
        try:
            with open(cache_path, 'r', encoding='utf-8') as f:
                cache = json.load(f)
                # 版本检查，不兼容时忽略旧缓存
                if cache.get('version', 1) != CACHE_VERSION:
                    print("   ⚠️  缓存版本不兼容，将开启全新翻译流程")
                    return {}
                return cache
        except (json.JSONDecodeError, IOError):
            return {}
    return {}

def validate_cache(chapters: list[epub.EpubHtml], cache: dict) -> dict:
    """验证缓存的章节是否在当前 EPUB 中，清理无效缓存"""
    if not cache:
        return cache

    chapter_names = {ch.get_name() for ch in chapters}
    completed = cache.get('completed_chapters', [])

    valid_completed = [c for c in completed if c in chapter_names]
    invalid_count = len(completed) - len(valid_completed)

    if invalid_count > 0:
        print(f"   ⚠️  清理了 {invalid_count} 章无效缓存（章节名与当前文件不匹配）")
        cache['completed_chapters'] = valid_completed
        contents = cache.get('chapter_contents', {})
        cache['chapter_contents'] = {k: v for k, v in contents.items() if k in chapter_names}

    return cache

def save_cache(epub_path: Path, cache_data: dict) -> None:
    cache_path = get_cache_path(epub_path)
    cache_data['version'] = CACHE_VERSION
    with open(cache_path, 'w', encoding='utf-8') as f:
        json.dump(cache_data, f, ensure_ascii=False, indent=2)

def save_terms(terms_path: Path, terms: list[dict]) -> None:
    valid_terms = [t for t in terms if isinstance(t, dict) and "original" in t and "translation" in t]
    with open(terms_path, 'w', encoding='utf-8') as f:
        json.dump(valid_terms, f, ensure_ascii=False, indent=2)

def clear_cache(epub_path: Path) -> None:
    cache_path = get_cache_path(epub_path)
    if cache_path.exists():
        cache_path.unlink()

# ==========================================
# 核心配置与工具函数
# ==========================================

def load_config() -> str:
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        print("❌ 错误：未设置 OPENROUTER_API_KEY 环境变量")
        print("请在终端运行以下命令后重试：")
        print("export OPENROUTER_API_KEY='your_api_key_here'")
        sys.exit(1)
    return api_key

def parse_args() -> Path:
    parser = argparse.ArgumentParser(description="EPUB 沉浸式翻译器")
    parser.add_argument("epub_path", help="要翻译的 EPUB 文件路径")
    args = parser.parse_args()

    epub_path = Path(args.epub_path)
    if not epub_path.exists():
        print(f"❌ 错误：文件不存在 - {epub_path}")
        sys.exit(1)
    if epub_path.suffix.lower() != ".epub":
        print(f"❌ 错误：不支持的文件格式，仅支持 .epub - {epub_path}")
        sys.exit(1)
    return epub_path

def show_epub_info(epub_path: Path) -> tuple[epub.EpubBook, list[epub.EpubHtml]]:
    book = epub.read_epub(str(epub_path))
    
    title = book.get_metadata("DC", "title")
    title_str = title[0][0] if title and isinstance(title[0], tuple) else (title[0] if title else "未知书名")
    
    author = book.get_metadata("DC", "creator")
    author_str = author[0][0] if author and isinstance(author[0], tuple) else (author[0] if author else "未知作者")

    chapters = [item for item in book.get_items() if isinstance(item, epub.EpubHtml)]

    print(f"\n{'='*40}")
    print(f"📖 书名：{title_str}")
    print(f"✍️  作者：{author_str}")
    print(f"📑 章节数：{len(chapters)}")
    print(f"{'='*40}\n")

    return book, chapters

def get_user_choices() -> tuple[str, str]:
    languages = {
        "1": "Chinese", "2": "English", "3": "Japanese", "4": "Korean",
        "5": "French", "6": "German", "7": "Spanish", "8": "Russian"
    }

    print("请选择目标语言：")
    for k, v in languages.items():
        print(f"  {k}. {v}")
    
    lang_choice = input("👉 输入数字 (1-8): ").strip()
    if lang_choice not in languages:
        print("❌ 无效的选择。")
        sys.exit(1)

    print("\n请选择输出模式：")
    print("  1. 仅译文")
    print("  2. 原文 + 译文（双语对照）")
    
    mode_choice = input("👉 输入数字 (1-2): ").strip()
    if mode_choice not in ["1", "2"]:
        print("❌ 无效的选择。")
        sys.exit(1)

    return languages[lang_choice], "translation_only" if mode_choice == "1" else "bilingual"

def estimate_tokens_and_confirm(chapters: list[epub.EpubHtml]) -> None:
    if not chapters:
        return
        
    encoder = tiktoken.get_encoding("cl100k_base")
    total_text = "".join(
        BeautifulSoup(ch.get_content(), "html.parser").get_text() 
        for ch in chapters
    )
    
    token_count = len(encoder.encode(total_text))
    estimated_cost = (token_count * 2 / 1_000_000) * 0.15

    print(f"\n📊 预估信息（待翻译章节）：")
    print(f"   文本 Token 数：约 {token_count:,} tokens")
    print(f"   API 预估费用：约 ${estimated_cost:.4f} USD")
    
    confirm = input("\n🚀 是否开始翻译？(y/n): ").strip().lower()
    if confirm not in ['y', 'yes']:
        print("已取消翻译。")
        sys.exit(0)

# ==========================================
# 翻译核心逻辑
# ==========================================

class TranslationError(Exception):
    pass

def extract_json_from_text(content: str) -> str:
    """使用括号匹配算法精准提取最外层 JSON 对象，解决嵌套大括号与外部 Markdown 干扰问题"""
    depth = 0
    start = -1
    for i, char in enumerate(content):
        if char == '{':
            if depth == 0:
                start = i
            depth += 1
        elif char == '}':
            depth -= 1
            if depth == 0 and start != -1:
                return content[start:i+1]
    raise ValueError("未在返回文本中找到完整的 JSON 对象")

def build_prompt(target_lang: str, texts: list[str], terms: list[dict]) -> str:
    terms_prompt = ""
    valid_terms = [t for t in terms if isinstance(t, dict) and "original" in t and "translation" in t]
    if valid_terms:
        terms_prompt = "## Glossary (use these translations consistently)\n"
        for t in valid_terms:
            terms_prompt += f"- {t['original']} -> {t['translation']}\n"

    template = f"""You are a professional {target_lang} native translator specialized in eBook content. Your task is to fluently translate text into {target_lang}.

## Translation Rules

1. Output only the translated content, without explanations or additional content
2. The returned translation must maintain exactly the same number of paragraphs and format as the original text
3. If the text contains HTML tags, consider where the tags should be placed in the translation while maintaining fluency
4. For content that should not be translated (such as proper nouns, code, etc.), keep the original text
5. Maintain the original tone, style, and narrative voice of the eBook
6. Ensure the translation resonates with the intended audience in the target language
7. Preserve literary devices, metaphors, and culturally significant elements appropriately

{terms_prompt}

Translate to {target_lang}. Return the result as a JSON object with the following structure:
{{
  "paragraphs": ["translated paragraph 1", "translated paragraph 2", ...],
  "terms": [{{"original": "original term", "translation": "translated term"}}]
}}

Text to translate:
{json.dumps(texts, ensure_ascii=False)}
"""
    return template

def call_openrouter_api(prompt: str, api_key: str, required_len: int, max_retries: int = 2) -> dict:
    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "model": "google/gemini-3.1-flash-lite-preview",
        "response_format": {"type": "json_object"},
        "messages": [{"role": "user", "content": prompt}]
    }

    for attempt in range(max_retries + 1):
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=120)
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]

            # 更健壮的 JSON 解析逻辑
            try:
                result = json.loads(content)
            except json.JSONDecodeError:
                clean_json_str = extract_json_from_text(content)
                result = json.loads(clean_json_str)

            # 验证结果结构
            if not isinstance(result, dict):
                raise ValueError(f"API 返回的不是字典类型: {type(result)}")

            paragraphs = result.get("paragraphs")
            if not isinstance(paragraphs, list):
                raise ValueError(f"paragraphs 不是列表类型: {type(paragraphs)}")

            if len(paragraphs) != required_len:
                raise ValueError(f"段落数不匹配: 期望 {required_len}, 实际 {len(paragraphs)}")

            # 验证 terms 格式
            terms = result.get("terms", [])
            if terms is None:
                result["terms"] = []
            elif not isinstance(terms, list):
                result["terms"] = []
            else:
                valid_terms = [t for t in terms if isinstance(t, dict) and "original" in t and "translation" in t]
                result["terms"] = valid_terms

            return result

        except (requests.RequestException, json.JSONDecodeError, ValueError) as e:
            if attempt < max_retries:
                print(f"    ⚠️ API 异常或校验失败 ({e})，正在进行第 {attempt + 1} 次重试...")
                time.sleep(2)
            else:
                raise TranslationError(f"API 请求彻底失败: {e}")

def translate_chapter(chapter: epub.EpubHtml, target_lang: str, mode: str, terms: list[dict], api_key: str) -> tuple[list[dict], bool]:
    soup = BeautifulSoup(chapter.get_content(), 'html.parser')
    
    if 'nav' in chapter.get_name().lower():
        return [], False

    block_tags = [
        'p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'div', 'li',
        'blockquote', 'figcaption', 'td', 'th', 'dd', 'dt'
    ]
    target_blocks = []

    for tag in soup.find_all(block_tags):
        if tag.find(block_tags):
            continue
        if tag.get_text(strip=True):
            target_blocks.append(tag)

    if not target_blocks:
        return [], False

    original_texts = [tag.decode_contents() for tag in target_blocks]
    prompt = build_prompt(target_lang, original_texts, terms)
    
    result = call_openrouter_api(prompt, api_key, required_len=len(original_texts))
    translated_texts = result["paragraphs"]
    new_terms = result.get("terms", [])

    for block, translated_html in zip(target_blocks, translated_texts):
        trans_soup = BeautifulSoup(translated_html, 'html.parser')
        
        if mode == "translation_only":
            block.clear()
            block.append(trans_soup)
        else:
            new_attrs = dict(block.attrs)
            if 'id' in new_attrs:
                new_attrs['id'] = f"{new_attrs['id']}-tr"
                
            new_block = soup.new_tag(block.name, **new_attrs)
            new_block.append(trans_soup)
            block.insert_after(new_block)
            block.insert_after(NavigableString("\n\n"))

    chapter.set_content(soup.encode('utf-8', formatter='html'))
    return new_terms, True

# ==========================================
# 主流程
# ==========================================

def main():
    api_key = load_config()
    epub_path = parse_args()
    book, chapters = show_epub_info(epub_path)

    if not chapters:
        print("❌ 未找到可提取文本的章节。")
        sys.exit(1)

    # 读取并验证缓存
    cache_path = get_cache_path(epub_path)
    raw_cache = load_cache(epub_path)
    cache = validate_cache(chapters, raw_cache)

    completed_chapters = cache.get('completed_chapters', [])

    if cache and 'target_lang' in cache and 'mode' in cache:
        target_lang = cache['target_lang']
        mode = cache['mode']
        print(f"\n💾 发现兼容版本缓存: {cache_path.name}")
        print(f"   已缓存 {len(completed_chapters)} 章翻译内容，将无缝继续未完成的翻译")
        print(f"   目标语言: {target_lang} | 模式: {'仅译文' if mode == 'translation_only' else '双语对照'}")
        
        pending_chapters = [ch for ch in chapters if ch.get_name() not in completed_chapters]
        estimate_tokens_and_confirm(pending_chapters)
    else:
        target_lang, mode = get_user_choices()
        estimate_tokens_and_confirm(chapters)

    terms_path = get_terms_path(epub_path, target_lang)

    print(f"\n🚀 开始翻译 ({'仅译文' if mode == 'translation_only' else '双语对照'} -> {target_lang})")
    print("-" * 50)

    # 完善的术语表双源合并逻辑
    global_terms = cache.get('terms', [])
    loaded_from_file = False

    if terms_path.exists():
        try:
            with open(terms_path, 'r', encoding='utf-8') as f:
                file_terms = json.load(f)
                if isinstance(file_terms, list):
                    existing_originals = {t.get("original") for t in global_terms if isinstance(t, dict)}
                    for t in file_terms:
                        if isinstance(t, dict) and t.get("original") not in existing_originals:
                            global_terms.append(t)
                            existing_originals.add(t.get("original"))
                    if file_terms:
                        loaded_from_file = True
        except Exception:
            pass

    if global_terms:
        source_info = "（包含历史持久化文件合并）" if loaded_from_file else "（来自缓存）"
        print(f"📔 成功加载术语表共 {len(global_terms)} 条 {source_info}")

    chapter_contents = cache.get('chapter_contents', {})

    # 恢复已翻译章节的内容
    for chapter in chapters:
        chapter_name = chapter.get_name()
        if chapter_name in chapter_contents:
            chapter.set_content(chapter_contents[chapter_name].encode('utf-8'))

    total_chapters = len(chapters)

    for i, chapter in enumerate(chapters, 1):
        chapter_name = chapter.get_name()

        # 跳过已完成的章节
        if chapter_name in completed_chapters:
            print(f"[{i}/{total_chapters}] ⏭️  跳过: {chapter_name} (已缓存)")
            continue

        start_time = time.time()
        print(f"[{i}/{total_chapters}] 正在翻译: {chapter_name} ", end="", flush=True)

        try:
            new_terms, was_translated = translate_chapter(chapter, target_lang, mode, global_terms, api_key)

            # 安全地合并术语表并进行去重
            if new_terms and isinstance(new_terms, list):
                existing_originals = {t.get("original") for t in global_terms if isinstance(t, dict)}
                for t in new_terms:
                    if isinstance(t, dict) and t.get("original") not in existing_originals:
                        global_terms.append(t)
                        existing_originals.add(t.get("original"))

            elapsed = time.time() - start_time
            completed_chapters.append(chapter_name)
            chapter_contents[chapter_name] = chapter.get_content().decode('utf-8')

            # 实时保存缓存和术语表
            cache_data = {
                'terms': global_terms,
                'completed_chapters': completed_chapters,
                'chapter_contents': chapter_contents,
                'target_lang': target_lang,
                'mode': mode
            }
            save_cache(epub_path, cache_data)
            save_terms(terms_path, global_terms)

            if was_translated:
                print(f"✅ 完成 ({elapsed:.1f}s)")
            else:
                print(f"⏭️ 跳过 (无文本)")

        except Exception as e:
            # 捕获所有运行时异常并安全退出
            print(f"\n❌ 失败!")
            print(f"\n{'='*50}")
            print(f"翻译中断，已安全保存进度到缓存文件")
            print(f"缓存文件: {cache_path}")
            print(f"术语表: {terms_path} (共 {len(global_terms)} 条)")
            print(f"已完成 {len(completed_chapters)}/{total_chapters} 章")
            print(f"\n错误详情: {e}")
            print(f"\n💡 重新运行脚本将从断点无缝继续，无需重新翻译")
            print("="*50)
            sys.exit(1)

    # 修复缺失的 UID，避免写入报错
    def fix_toc_uids(items):
        for item in items:
            if isinstance(item, epub.Link) and not item.uid:
                item.uid = f"uid-{hash(item.href) % 10000000:07d}"
            elif isinstance(item, tuple) and len(item) == 2:
                section, subsections = item
                if isinstance(section, epub.Link) and not section.uid:
                    section.uid = f"uid-{hash(section.href) % 10000000:07d}"
                if isinstance(subsections, list):
                    fix_toc_uids(subsections)

    fix_toc_uids(book.toc)

    lang_suffix = target_lang.lower().replace(" ", "_")
    output_epub = epub_path.with_name(f"{epub_path.stem}_{lang_suffix}.epub")

    epub.write_epub(str(output_epub), book, {})

    # 翻译全部成功，清除缓存文件，但保留术语表
    clear_cache(epub_path)

    print(f"\n🎉 翻译全部完成！")
    print(f"📄 新 EPUB 文件: {output_epub}")
    print(f"📔 术语表文件: {terms_path} (共 {len(global_terms)} 条)")
    print(f"🗑️  已清除缓存文件")

if __name__ == "__main__":
    main()