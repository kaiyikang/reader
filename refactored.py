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
运行方式: uv run translator.py <your_book.epub>
"""

import abc
import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import List, Dict, Tuple, Optional


# ============================================================================
# 1. DOMAIN LAYER (领域层)
# ============================================================================

@dataclass
class TranslationConfig:
    target_lang: str
    mode: str

@dataclass
class Term:
    original: str
    translation: str
    frequency: int = 1

@dataclass
class ChapterInfo:
    id: str
    title: str

def merge_terms(existing: List[Term], new_terms: List[Term]) -> List[Term]:
    """领域逻辑：合并术语并累加出现频次"""
    term_map = {t.original: t for t in existing}
    
    for t in new_terms:
        if t.original in term_map:
            # 如果术语已存在，说明它是高频核心词，权重 +1
            term_map[t.original].frequency += 1
        else:
            # 新术语，直接加入字典
            term_map[t.original] = t
            
    return list(term_map.values())


class ITranslationProvider(abc.ABC):
    @abc.abstractmethod
    def translate_blocks(self, texts: List[str], target_lang: str, terms: List[Term]) -> Tuple[List[str], List[Term]]: pass

class IBookRepository(abc.ABC):
    @abc.abstractmethod
    def load_book(self, file_path: Path) -> None: pass
    @abc.abstractmethod
    def get_book_info(self) -> Tuple[str, str, int]: pass
    @abc.abstractmethod
    def get_chapter_list(self) -> List[ChapterInfo]: pass
    @abc.abstractmethod
    def extract_translatable_blocks(self, chapter_id: str) -> List[str]: pass
    @abc.abstractmethod
    def apply_translation(self, chapter_id: str, translated_blocks: List[str], mode: str) -> None: pass
    @abc.abstractmethod
    def set_chapter_content(self, chapter_id: str, content: str) -> None: pass
    @abc.abstractmethod
    def get_chapter_content(self, chapter_id: str) -> str: pass
    @abc.abstractmethod
    def save_book(self, output_path: Path) -> None: pass

class ICacheManager(abc.ABC):
    @abc.abstractmethod
    def load_progress(self, current_chapter_ids: List[str]) -> Tuple[List[str], Dict[str, str], Optional[str], Optional[str], List[Term]]: pass
    @abc.abstractmethod
    def save_progress(self, completed_chapters: List[str], chapter_contents: Dict[str, str], target_lang: str, mode: str, terms: List[Term]) -> None: pass
    @abc.abstractmethod
    def load_terms_from_file(self, target_lang: str) -> List[Term]: pass
    @abc.abstractmethod
    def save_terms_to_file(self, target_lang: str, terms: List[Term]) -> None: pass
    @abc.abstractmethod
    def clear_progress(self) -> None: pass

class ITokenEstimator(abc.ABC):
    @abc.abstractmethod
    def estimate_cost(self, texts: List[str]) -> Tuple[int, float]: pass


# ============================================================================
# 2. USE CASE LAYER (应用/用例层)
# ============================================================================

class TranslateEpubUseCase:
    def __init__(self, 
                 book_repo: IBookRepository, 
                 translator: ITranslationProvider, 
                 cache_manager: ICacheManager,
                 token_estimator: ITokenEstimator):
        self.book_repo = book_repo
        self.translator = translator
        self.cache_manager = cache_manager
        self.token_estimator = token_estimator

    def execute(self, epub_path: Path, cli_display) -> Optional[Path]:

        chapters = self.book_repo.get_chapter_list()
        chapter_ids = [ch.id for ch in chapters]

        # 1. 加载进度、配置与缓存术语
        completed_chapters, chapter_contents, cached_lang, cached_mode, cache_terms = self.cache_manager.load_progress(chapter_ids)
        
        if cached_lang and cached_mode:
            config = TranslationConfig(target_lang=cached_lang, mode=cached_mode)
            cli_display.show_cache_resume(len(completed_chapters), cached_lang, cached_mode)
        else:
            config = cli_display.get_user_inputs()

        # 2. 从文件加载术语并与缓存术语合并双源去重
        file_terms = self.cache_manager.load_terms_from_file(config.target_lang)
        global_terms = merge_terms(cache_terms, file_terms)
        cli_display.show_terms_loaded(len(global_terms))

        # 3. 恢复已完成章节的内容
        for ch_id, content in chapter_contents.items():
            self.book_repo.set_chapter_content(ch_id, content)

        # 4. Token 预估与确认
        pending_texts = []
        for chapter in chapters:
            if chapter.id not in completed_chapters:
                blocks = self.book_repo.extract_translatable_blocks(chapter.id)
                pending_texts.extend(blocks)
                
        token_count, cost = self.token_estimator.estimate_cost(pending_texts)
        if not cli_display.confirm_translation(token_count, cost):
            return None # 用户取消翻译

        # 5. 核心翻译循环
        total = len(chapters)
        for i, chapter in enumerate(chapters, 1):
            if chapter.id in completed_chapters:
                cli_display.show_chapter_skip(i, total, chapter.title)
                continue

            cli_display.show_chapter_start(i, total, chapter.title)
            start_time = time.time()

            try:
                blocks = self.book_repo.extract_translatable_blocks(chapter.id)
                if not blocks:
                    completed_chapters.append(chapter.id)
                    cli_display.show_chapter_empty()
                    continue

                translated_blocks, new_terms = self.translator.translate_blocks(blocks, config.target_lang, global_terms)
                
                global_terms = merge_terms(global_terms, new_terms)

                self.book_repo.apply_translation(chapter.id, translated_blocks, config.mode)

                completed_chapters.append(chapter.id)
                chapter_contents[chapter.id] = self.book_repo.get_chapter_content(chapter.id)

                self.cache_manager.save_progress(completed_chapters, chapter_contents, config.target_lang, config.mode, global_terms)
                self.cache_manager.save_terms_to_file(config.target_lang, global_terms)

                cli_display.show_chapter_done(time.time() - start_time)

            except Exception as e:
                cli_display.show_error_and_exit(e, len(completed_chapters), total, len(global_terms))

        # 6. 保存最终文件并清理缓存
        lang_suffix = config.target_lang.lower().replace(" ", "_")
        output_path = epub_path.with_name(f"{epub_path.stem}_{lang_suffix}.epub")
        self.book_repo.save_book(output_path)
        self.cache_manager.clear_progress()

        return output_path


# ============================================================================
# 3. INFRASTRUCTURE LAYER (基础设施层)
# ============================================================================

import re
import requests
import tiktoken
from bs4 import BeautifulSoup, NavigableString
from ebooklib import epub

class TiktokenAdapter(ITokenEstimator):
    def estimate_cost(self, texts: List[str]) -> Tuple[int, float]:
        if not texts:
            return 0, 0.0
        encoder = tiktoken.get_encoding("cl100k_base")
        total_text = "".join(texts)
        token_count = len(encoder.encode(total_text))
        estimated_cost = (token_count * 2 / 1_000_000) * 0.15
        return token_count, estimated_cost

class ILLMClient(abc.ABC):
    @abc.abstractmethod
    def generate_json(self, prompt: str) -> str: pass

class OpenRouterClient(ILLMClient):
    def __init__(self, api_key: str, model: str = "google/gemini-3.1-flash-lite-preview"):
        self.api_key = api_key
        self.model = model

    def generate_json(self, prompt: str) -> str:
        url = "https://openrouter.ai/api/v1/chat/completions"
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        payload = {
            "model": self.model,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "user", "content": prompt}]
        }
        resp = requests.post(url, headers=headers, json=payload, timeout=120)
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]

class LLMTranslatorAdapter(ITranslationProvider):
    def __init__(self, llm_client: ILLMClient, max_retries: int = 3, batch_size: int = 30):
        self.llm_client = llm_client
        self.max_retries = max_retries
        self.batch_size = batch_size # 💡 新增：每次发送给大模型的最大段落数
        
    def _extract_json_from_text(self, content: str) -> str:
        depth, start = 0, -1
        for i, char in enumerate(content):
            if char == '{':
                if depth == 0: start = i
                depth += 1
            elif char == '}':
                depth -= 1
                if depth == 0 and start != -1:
                    return content[start:i+1]
        raise ValueError("未找到完整的 JSON 对象")

    def _is_term_in_text(self, term: str, text: str) -> bool:
        """💡 优化：更智能的术语匹配，防止单词边界假阳性"""
        # 如果术语完全由纯英文字母/数字/空格/连字符组成，使用单词边界精确匹配
        if re.match(r'^[a-zA-Z0-9\s\-_]+$', term):
            return bool(re.search(rf'\b{re.escape(term)}\b', text, re.IGNORECASE))
        # 否则（如中文术语），直接使用子串匹配
        return term in text

    def translate_blocks(self, texts: List[str], target_lang: str, terms: List[Term], max_terms: int = 50) -> Tuple[List[str], List[Term]]:
        all_translated_paragraphs = []
        all_new_terms = []
        
        # 💡 优化：切片批处理，防止单次请求超出大模型最大输出 Token 限制
        for i in range(0, len(texts), self.batch_size):
            batch_texts = texts[i:i + self.batch_size]
            batch_combined_text = " ".join(batch_texts)
            
            terms_prompt = ""
            if terms:
                # 💡 使用优化后的匹配逻辑
                active_terms = [t for t in terms if self._is_term_in_text(t.original, batch_combined_text)]
                active_terms = sorted(active_terms, key=lambda x: x.frequency, reverse=True)[:max_terms]

                if active_terms:
                    compact_glossary = ", ".join([f"'{t.original}':'{t.translation}'" for t in active_terms])
                    terms_prompt = f"## Glossary (Strictly follow): {compact_glossary}\n"

            prompt = f"""You are a professional {target_lang} native translator specialized in eBook content. Your task is to fluently translate text into {target_lang}.

## Translation Rules
1. Output only the translated content, without explanations or additional content
2. The returned translation must maintain exactly the same number of paragraphs and format as the original text
3. If the text contains HTML tags, consider where the tags should be placed in the translation while maintaining fluency
4. For content that should not be translated (such as proper nouns, code, etc.), keep the original text
5. Maintain the original tone, style, and narrative voice of the eBook
6. Ensure the translation resonates with the intended audience in the target language
7. Preserve literary devices, metaphors, and culturally significant elements appropriately

{terms_prompt}
Return JSON format: {{"paragraphs": ["..."], "terms": [{{"original": "...", "translation": "..."}}]}}
Text to translate:
{json.dumps(batch_texts, ensure_ascii=False)}
"""
            
            # 单个 Batch 的重试循环
            for attempt in range(self.max_retries):
                try:
                    content = self.llm_client.generate_json(prompt)
                    try: 
                        result = json.loads(content)
                    except json.JSONDecodeError: 
                        result = json.loads(self._extract_json_from_text(content))
                    
                    paragraphs = result.get("paragraphs", [])
                    if len(paragraphs) != len(batch_texts):
                        raise ValueError(f"段落数不匹配: 期望 {len(batch_texts)}, 实际 {len(paragraphs)}")
                    
                    new_terms = [Term(**t) for t in result.get("terms", []) if "original" in t and "translation" in t]
                    
                    # 成功后汇总结果
                    all_translated_paragraphs.extend(paragraphs)
                    all_new_terms.extend(new_terms)
                    break # 成功跳出重试循环
                    
                except Exception as e:
                    if attempt < self.max_retries - 1:
                        # 💡 优化：指数退避等待 (2s, 4s, 8s) 缓解 API 限流问题
                        sleep_time = 2 ** (attempt + 1)
                        print(f"\n    ⚠️ 批次 {i//self.batch_size + 1} 异常 ({e})，等待 {sleep_time} 秒后重试...")
                        time.sleep(sleep_time)
                    else:
                        raise Exception(f"大模型请求彻底失败: {e}")

        return all_translated_paragraphs, all_new_terms

class EbookLibAdapter(IBookRepository):
    def __init__(self):
        self.book = None
        self._chapters: Dict[str, epub.EpubHtml] = {}
        self._current_soup: Optional[BeautifulSoup] = None
        self._current_tags: List = []

    def load_book(self, file_path: Path) -> None:
        self.book = epub.read_epub(str(file_path))
        for item in self.book.get_items():
            if isinstance(item, epub.EpubHtml):
                self._chapters[item.get_name()] = item

    def get_book_info(self) -> Tuple[str, str, int]:
        title = self.book.get_metadata("DC", "title")
        title_str = title[0][0] if title and isinstance(title[0], tuple) else (title[0] if title else "未知书名")
        author = self.book.get_metadata("DC", "creator")
        author_str = author[0][0] if author and isinstance(author[0], tuple) else (author[0] if author else "未知作者")
        return title_str, author_str, len(self._chapters)

    def get_chapter_list(self) -> List[ChapterInfo]:
        return [ChapterInfo(id=name, title=name) for name in self._chapters.keys()]

    def extract_translatable_blocks(self, chapter_id: str) -> List[str]:
        chapter = self._chapters[chapter_id]
        if 'nav' in chapter_id.lower():
            return []
            
        self._current_soup = BeautifulSoup(chapter.get_content(), 'html.parser')
        block_tags = ['p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'div', 'li', 'blockquote', 'figcaption', 'td', 'th', 'dd', 'dt']
        self._current_tags = []
        
        for tag in self._current_soup.find_all(block_tags):
            if tag.find(block_tags): continue
            if tag.get_text(strip=True):
                self._current_tags.append(tag)
                
        return [tag.decode_contents() for tag in self._current_tags]

    def apply_translation(self, chapter_id: str, translated_blocks: List[str], mode: str) -> None:
        if not self._current_soup or not self._current_tags:
            return
            
        for block, translated_html in zip(self._current_tags, translated_blocks):
            trans_soup = BeautifulSoup(translated_html, 'html.parser')
            if mode == "translation_only":
                block.clear()
                block.append(trans_soup)
            else:
                new_attrs = dict(block.attrs)
                if 'id' in new_attrs:
                    new_attrs['id'] = f"{new_attrs['id']}-tr"
                new_block = self._current_soup.new_tag(block.name, **new_attrs)
                new_block.append(trans_soup)
                block.insert_after(new_block)
                block.insert_after(NavigableString("\n\n"))
                
        self._chapters[chapter_id].set_content(self._current_soup.encode('utf-8', formatter='html'))
        # 释放缓存防止串台污染
        self._current_soup = None 
        self._current_tags = []

    def set_chapter_content(self, chapter_id: str, content: str) -> None:
        self._chapters[chapter_id].set_content(content.encode('utf-8'))

    def get_chapter_content(self, chapter_id: str) -> str:
        return self._chapters[chapter_id].get_content().decode('utf-8')

    def _fix_toc_uids(self, items):
        for item in items:
            if isinstance(item, epub.Link) and not item.uid:
                item.uid = f"uid-{hash(item.href) % 10000000:07d}"
            elif isinstance(item, tuple) and len(item) == 2:
                section, subsections = item
                if isinstance(section, epub.Link) and not section.uid:
                    section.uid = f"uid-{hash(section.href) % 10000000:07d}"
                if isinstance(subsections, list):
                    self._fix_toc_uids(subsections)

    def save_book(self, output_path: Path) -> None:
        self._fix_toc_uids(self.book.toc)
        epub.write_epub(str(output_path), self.book, {})

class LocalFileCacheAdapter(ICacheManager):
    CACHE_VERSION = 1
    
    def __init__(self, epub_path: Path):
        self.cache_path = epub_path.with_suffix('.cache.json')
        self.epub_path = epub_path
        
    def _get_terms_path(self, target_lang: str) -> Path:
        lang_suffix = target_lang.lower().replace(" ", "_")
        return self.epub_path.with_name(f"{self.epub_path.stem}_{lang_suffix}_terms.json")

    def load_progress(self, current_chapter_ids: List[str]) -> Tuple[List[str], Dict[str, str], Optional[str], Optional[str], List[Term]]:
        if not self.cache_path.exists():
            return [], {}, None, None, []
            
        with open(self.cache_path, 'r', encoding='utf-8') as f:
            cache = json.load(f)
            if cache.get('version') != self.CACHE_VERSION:
                return [], {}, None, None, []
                
        completed = cache.get('completed_chapters', [])
        valid_completed = [c for c in completed if c in current_chapter_ids]
        
        # 补回原版的缓存严格校验逻辑
        invalid_count = len(completed) - len(valid_completed)
        if invalid_count > 0:
            print(f"   ⚠️  清理了 {invalid_count} 章无效缓存（章节名与当前文件不匹配）")

        contents = {k: v for k, v in cache.get('chapter_contents', {}).items() if k in current_chapter_ids}
        terms = [Term(**t) for t in cache.get('terms', []) if isinstance(t, dict)]
        
        return valid_completed, contents, cache.get('target_lang'), cache.get('mode'), terms

    def save_progress(self, completed_chapters: List[str], chapter_contents: Dict[str, str], target_lang: str, mode: str, terms: List[Term]) -> None:
        data = {
            'version': self.CACHE_VERSION,
            'completed_chapters': completed_chapters,
            'chapter_contents': chapter_contents,
            'target_lang': target_lang,
            'mode': mode,
            'terms': [asdict(t) for t in terms]
        }
        with open(self.cache_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def load_terms_from_file(self, target_lang: str) -> List[Term]:
        terms_path = self._get_terms_path(target_lang)
        if terms_path.exists():
            with open(terms_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                return [Term(**t) for t in data if isinstance(t, dict)]
        return []

    def save_terms_to_file(self, target_lang: str, terms: List[Term]) -> None:
        terms_path = self._get_terms_path(target_lang)
        with open(terms_path, 'w', encoding='utf-8') as f:
            json.dump([asdict(t) for t in terms], f, ensure_ascii=False, indent=2)

    def clear_progress(self) -> None:
        if self.cache_path.exists():
            self.cache_path.unlink()

class CLIDisplay:
    @staticmethod
    def check_env() -> str:
        api_key = os.getenv("OPENROUTER_API_KEY")
        if not api_key:
            print("❌ 错误：未设置 OPENROUTER_API_KEY 环境变量")
            sys.exit(1)
        return api_key

    @staticmethod
    def get_epub_path() -> Path:
        parser = argparse.ArgumentParser()
        parser.add_argument("epub_path", help="要翻译的 EPUB 文件路径")
        args = parser.parse_args()
        p = Path(args.epub_path)
        if not p.exists() or p.suffix.lower() != ".epub":
            print("❌ 错误：文件不存在或不是.epub格式")
            sys.exit(1)
        return p

    @staticmethod
    def get_user_inputs() -> TranslationConfig:
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

        return TranslationConfig(
            target_lang=languages[lang_choice],
            mode="translation_only" if mode_choice == "1" else "bilingual"
        )

    @staticmethod
    def show_book_info(title: str, author: str, chapters: int):
        print(f"\n{'='*40}\n📖 书名：{title}\n✍️  作者：{author}\n📑 章节数：{chapters}\n{'='*40}\n")

    @staticmethod
    def show_cache_resume(completed_count: int, lang: str, mode: str):
        print(f"\n💾 发现兼容版本缓存，将无缝继续！")
        print(f"   已完成 {completed_count} 章 | 目标语言: {lang} | 模式: {'仅译文' if mode == 'translation_only' else '双语对照'}")

    @staticmethod
    def show_terms_loaded(count: int):
        if count > 0:
            print(f"📔 成功加载术语表共 {count} 条")

    @staticmethod
    def show_chapter_start(current: int, total: int, title: str):
        print(f"[{current}/{total}] 正在翻译: {title} ", end="", flush=True)

    @staticmethod
    def show_chapter_done(elapsed: float):
        print(f"✅ 完成 ({elapsed:.1f}s)")

    @staticmethod
    def show_chapter_empty():
        print(f"⏭️ 跳过 (无文本)")

    @staticmethod
    def show_chapter_skip(current: int, total: int, title: str):
        print(f"[{current}/{total}] ⏭️ 跳过: {title} (已缓存)")

    @staticmethod
    def show_error_and_exit(e: Exception, completed: int, total: int, term_count: int):
        print(f"\n❌ 失败! 详情: {e}")
        print(f"已安全保存进度 ({completed}/{total}) 术语表共 {term_count} 条，重新运行即可无缝继续。")
        sys.exit(1)
    
    @staticmethod
    def confirm_translation(token_count: int, estimated_cost: float) -> bool:
        if token_count == 0:
            return True
            
        print(f"\n📊 预估信息（待翻译章节）：")
        print(f"   文本 Token 数：约 {token_count:,} tokens")
        print(f"   API 预估费用：约 ${estimated_cost:.4f} USD")
        
        confirm = input("\n🚀 是否开始翻译？(y/n): ").strip().lower()
        if confirm not in ['y', 'yes']:
            print("已取消翻译。")
            return False
        return True


# ============================================================================
# 4. MAIN / COMPOSITION ROOT (组装根)
# ============================================================================

def main():
    api_key = CLIDisplay.check_env()
    epub_path = CLIDisplay.get_epub_path()

    book_repo = EbookLibAdapter()
    open_router_client = OpenRouterClient(api_key)
    translator = LLMTranslatorAdapter(open_router_client)
    cache_manager = LocalFileCacheAdapter(epub_path)
    token_estimator = TiktokenAdapter()

    use_case = TranslateEpubUseCase(book_repo, translator, cache_manager, token_estimator)
    
    book_repo.load_book(epub_path)
    title, author, chapter_count = book_repo.get_book_info()
    CLIDisplay.show_book_info(title, author, chapter_count)

    print("🚀 准备翻译环境...")
    output_file = use_case.execute(epub_path, CLIDisplay)
    
    if output_file:
        print(f"\n🎉 翻译全部完成！\n📄 新文件: {output_file}")

if __name__ == "__main__":
    main()