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
EPUB 翻译器 (Clean Architecture 重构版)
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
# 规则：绝对禁止导入外部的具体实现库 (如 requests, ebooklib, bs4)

@dataclass
class TranslationConfig:
    """值对象：翻译配置"""
    target_lang: str
    mode: str  # 'translation_only' | 'bilingual'

@dataclass
class Term:
    """实体：术语"""
    original: str
    translation: str

@dataclass
class ChapterInfo:
    """实体：章节信息摘要"""
    id: str
    title: str

# --- services ---

def merge_terms(existing: List[Term], new_terms: List[Term]) -> List[Term]:
    """领域逻辑：去重并合并术语"""
    existing_originals = {t.original for t in existing}
    merged = list(existing)
    for t in new_terms:
        if t.original not in existing_originals:
            merged.append(t)
            existing_originals.add(t.original)
    return merged

# --- 端口 (Ports) / 抽象接口 ---

class ITranslationProvider(abc.ABC):
    @abc.abstractmethod
    def translate_blocks(self, texts: List[str], target_lang: str, terms: List[Term]) -> Tuple[List[str], List[Term]]:
        pass

class IBookRepository(abc.ABC):
    @abc.abstractmethod
    def load_book(self, file_path: Path) -> None: pass
    
    @abc.abstractmethod
    def get_book_info(self) -> Tuple[str, str, int]: pass # 返回 (书名, 作者, 章节数)

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
    def load_progress(self, current_chapter_ids: List[str]) -> Tuple[List[str], Dict[str, str], Optional[str], Optional[str]]: pass
    
    @abc.abstractmethod
    def save_progress(self, completed_chapters: List[str], chapter_contents: Dict[str, str], target_lang: str, mode: str) -> None: pass
    
    @abc.abstractmethod
    def load_terms(self, target_lang: str) -> List[Term]: pass
    
    @abc.abstractmethod
    def save_terms(self, target_lang: str, terms: List[Term]) -> None: pass

    @abc.abstractmethod
    def clear_progress(self) -> None: pass


class ITokenEstimator(abc.ABC):
    @abc.abstractmethod
    def estimate_cost(self, texts: List[str]) -> Tuple[int, float]: pass


# ============================================================================
# 2. USE CASE LAYER (应用/用例层)
# ============================================================================
# 规则：只依赖 Domain 层，编排业务流程

class TranslateEpubUseCase:
    """核心业务：执行整本 EPUB 的翻译"""
    
    def __init__(self, 
                 book_repo: IBookRepository, 
                 translator: ITranslationProvider, 
                 cache_manager: ICacheManager,
                 token_estimator: ITokenEstimator):
        self.book_repo = book_repo
        self.translator = translator
        self.cache_manager = cache_manager
        self.token_estimator = token_estimator


    def execute(self, epub_path: Path, config: TranslationConfig, cli_display) -> Path:
        self.book_repo.load_book(epub_path)
        chapters = self.book_repo.get_chapter_list()
        chapter_ids = [ch.id for ch in chapters]

        # 1. 加载缓存与术语
        completed_chapters, chapter_contents, cached_lang, cached_mode = self.cache_manager.load_progress(chapter_ids)
        if cached_lang and cached_mode:
            config.target_lang = cached_lang
            config.mode = cached_mode
            cli_display.show_cache_resume(len(completed_chapters), cached_lang, cached_mode)

        global_terms = self.cache_manager.load_terms(config.target_lang)
        cli_display.show_terms_loaded(len(global_terms))

        # 2. 恢复已完成章节的内容
        for ch_id, content in chapter_contents.items():
            self.book_repo.set_chapter_content(ch_id, content)

        # 2.1 Token 预估与确认
        pending_texts = []
        for chapter in chapters:
            if chapter.id not in completed_chapters:
                blocks = self.book_repo.extract_translatable_blocks(chapter.id)
                pending_texts.extend(blocks)
                
        token_count, cost = self.token_estimator.estimate_cost(pending_texts)
        cli_display.confirm_translation(token_count, cost)

        # 3. 核心翻译循环
        total = len(chapters)
        for i, chapter in enumerate(chapters, 1):
            if chapter.id in completed_chapters:
                cli_display.show_chapter_skip(i, total, chapter.title)
                continue

            cli_display.show_chapter_start(i, total, chapter.title)
            start_time = time.time()

            try:
                # 提取
                blocks = self.book_repo.extract_translatable_blocks(chapter.id)
                if not blocks:
                    completed_chapters.append(chapter.id)
                    cli_display.show_chapter_empty()
                    continue

                # 翻译
                translated_blocks, new_terms = self.translator.translate_blocks(blocks, config.target_lang, global_terms)
                
                # 合并术语
                global_terms = merge_terms(global_terms, new_terms)

                # 回写 DOM
                self.book_repo.apply_translation(chapter.id, translated_blocks, config.mode)

                # 记录状态
                completed_chapters.append(chapter.id)
                chapter_contents[chapter.id] = self.book_repo.get_chapter_content(chapter.id)

                # 持久化
                self.cache_manager.save_progress(completed_chapters, chapter_contents, config.target_lang, config.mode)
                self.cache_manager.save_terms(config.target_lang, global_terms)

                cli_display.show_chapter_done(time.time() - start_time)

            except Exception as e:
                cli_display.show_error_and_exit(e, len(completed_chapters), total, len(global_terms))

        # 4. 保存最终文件并清理缓存
        lang_suffix = config.target_lang.lower().replace(" ", "_")
        output_path = epub_path.with_name(f"{epub_path.stem}_{lang_suffix}.epub")
        self.book_repo.save_book(output_path)
        self.cache_manager.clear_progress()

        return output_path


# ============================================================================
# 3. INFRASTRUCTURE LAYER (基础设施层)
# ============================================================================
# 规则：实现 Domain 接口，可以任意使用第三方库

import requests
import tiktoken
from bs4 import BeautifulSoup, NavigableString
from ebooklib import epub

class TiktokenAdapter(ITokenEstimator):
    """具体实现：使用 tiktoken 估算成本"""
    def estimate_cost(self, texts: List[str]) -> Tuple[int, float]:
        if not texts:
            return 0, 0.0
        
        encoder = tiktoken.get_encoding("cl100k_base")
        total_text = "".join(texts)
        token_count = len(encoder.encode(total_text))
        
        # 按照原脚本的定价逻辑：每百万 token $0.15 (假设输入输出比例约 1:1，乘 2)
        estimated_cost = (token_count * 2 / 1_000_000) * 0.15
        return token_count, estimated_cost

class OpenRouterTranslatorAdapter(ITranslationProvider):
    def __init__(self, api_key: str):
        self.api_key = api_key
        
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

    def translate_blocks(self, texts: List[str], target_lang: str, terms: List[Term]) -> Tuple[List[str], List[Term]]:
        terms_prompt = ""
        if terms:
            terms_prompt = "## Glossary (use these translations consistently)\n"
            for t in terms:
                terms_prompt += f"- {t.original} -> {t.translation}\n"

        prompt = f"""You are a professional {target_lang} native translator specialized in eBook content... (遵守你的翻译规则)
{terms_prompt}
Return JSON format: {{"paragraphs": ["..."], "terms": [{{"original": "...", "translation": "..."}}]}}
Text to translate:
{json.dumps(texts, ensure_ascii=False)}
"""
        url = "https://openrouter.ai/api/v1/chat/completions"
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        payload = {
            "model": "google/gemini-3.1-flash-lite-preview",
            "response_format": {"type": "json_object"},
            "messages": [{"role": "user", "content": prompt}]
        }

        for attempt in range(3):
            try:
                resp = requests.post(url, headers=headers, json=payload, timeout=120)
                resp.raise_for_status()
                content = resp.json()["choices"][0]["message"]["content"]
                
                try: result = json.loads(content)
                except: result = json.loads(self._extract_json_from_text(content))
                
                paragraphs = result.get("paragraphs", [])
                if len(paragraphs) != len(texts):
                    raise ValueError(f"段落数不匹配: {len(paragraphs)} vs {len(texts)}")
                
                new_terms = [Term(**t) for t in result.get("terms", []) if "original" in t and "translation" in t]
                return paragraphs, new_terms
                
            except Exception as e:
                if attempt < 2:
                    print(f"\n    ⚠️ API 异常 ({e})，正在重试...")
                    time.sleep(2)
                else:
                    raise Exception(f"API 请求彻底失败: {e}")

class EbookLibAdapter(IBookRepository):
    def __init__(self):
        self.book = None
        self._chapters: Dict[str, epub.EpubHtml] = {}
        # 临时保存当前章节的 BS4 对象，避免序列化导致的对象引用丢失
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
        block_tags = ['p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'div', 'li', 'blockquote', 'td', 'th']
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
                
        # 写回到内存中的 chapter 对象
        self._chapters[chapter_id].set_content(self._current_soup.encode('utf-8', formatter='html'))
        self._current_soup = None # 清理缓存

    def set_chapter_content(self, chapter_id: str, content: str) -> None:
        self._chapters[chapter_id].set_content(content.encode('utf-8'))

    def get_chapter_content(self, chapter_id: str) -> str:
        return self._chapters[chapter_id].get_content().decode('utf-8')

    def _fix_toc_uids(self, items):
        """修复目录 UID 防止写入报错"""
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

    def load_progress(self, current_chapter_ids: List[str]) -> Tuple[List[str], Dict[str, str], Optional[str], Optional[str]]:
        if not self.cache_path.exists():
            return [], {}, None, None
            
        with open(self.cache_path, 'r', encoding='utf-8') as f:
            cache = json.load(f)
            if cache.get('version') != self.CACHE_VERSION:
                return [], {}, None, None
                
        completed = [c for c in cache.get('completed_chapters', []) if c in current_chapter_ids]
        contents = {k: v for k, v in cache.get('chapter_contents', {}).items() if k in current_chapter_ids}
        return completed, contents, cache.get('target_lang'), cache.get('mode')

    def save_progress(self, completed_chapters: List[str], chapter_contents: Dict[str, str], target_lang: str, mode: str) -> None:
        data = {
            'version': self.CACHE_VERSION,
            'completed_chapters': completed_chapters,
            'chapter_contents': chapter_contents,
            'target_lang': target_lang,
            'mode': mode
        }
        with open(self.cache_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def load_terms(self, target_lang: str) -> List[Term]:
        terms_path = self._get_terms_path(target_lang)
        if terms_path.exists():
            with open(terms_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                return [Term(**t) for t in data if isinstance(t, dict)]
        return []

    def save_terms(self, target_lang: str, terms: List[Term]) -> None:
        terms_path = self._get_terms_path(target_lang)
        with open(terms_path, 'w', encoding='utf-8') as f:
            json.dump([asdict(t) for t in terms], f, ensure_ascii=False, indent=2)

    def clear_progress(self) -> None:
        if self.cache_path.exists():
            self.cache_path.unlink()

class CLIDisplay:
    """表现层：负责与用户的终端交互"""
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
        print("请选择目标语言: 1.Chinese 2.English 3.Japanese ... (输入具体语言如 Chinese):")
        lang = input("👉 目标语言: ").strip() or "Chinese"
        print("请选择输出模式: 1.单语 2.双语对照")
        mode = "translation_only" if input("👉 输入 (1/2): ").strip() == "1" else "bilingual"
        return TranslationConfig(target_lang=lang, mode=mode)

    @staticmethod
    def show_book_info(title: str, author: str, chapters: int):
        print(f"\n{'='*40}\n📖 书名：{title}\n✍️  作者：{author}\n📑 章节数：{chapters}\n{'='*40}\n")

    @staticmethod
    def show_cache_resume(completed_count: int, lang: str, mode: str):
        print(f"\n💾 发现兼容版本缓存，将无缝继续！(已完成 {completed_count} 章, {lang}, {mode})")

    @staticmethod
    def show_terms_loaded(count: int):
        if count > 0:
            print(f"📔 成功加载历史术语表共 {count} 条")

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
        print(f"已安全保存进度 ({completed}/{total})，重新运行即可无缝继续。")
        sys.exit(1)
    
    @staticmethod
    def confirm_translation(token_count: int, estimated_cost: float) -> None:
        if token_count == 0:
            return
            
        print(f"\n📊 预估信息（待翻译章节）：")
        print(f"   文本 Token 数：约 {token_count:,} tokens")
        print(f"   API 预估费用：约 ${estimated_cost:.4f} USD")
        
        confirm = input("\n🚀 是否开始翻译？(y/n): ").strip().lower()
        if confirm not in ['y', 'yes']:
            print("已取消翻译。")
            sys.exit(0)


# ============================================================================
# 4. MAIN / COMPOSITION ROOT (组装根)
# ============================================================================

def main():
    # 1. 启动与配置检查
    api_key = CLIDisplay.check_env()
    epub_path = CLIDisplay.get_epub_path()

    sys.exit(1)
    # 2. 实例化基础设施 (Adapters)
    book_repo = EbookLibAdapter()
    translator = OpenRouterTranslatorAdapter(api_key)
    cache_manager = LocalFileCacheAdapter(epub_path)
    token_estimator = TiktokenAdapter() # 👈 新增实例化

    # 3. 注入到用例层 (UseCase)
    use_case = TranslateEpubUseCase(book_repo, translator, cache_manager, token_estimator)

    # 4. 初始化基础交互
    book_repo.load_book(epub_path)
    title, author, chapter_count = book_repo.get_book_info()
    CLIDisplay.show_book_info(title, author, chapter_count)
    config = CLIDisplay.get_user_inputs()

    # 5. 执行核心业务
    print("\n🚀 开始翻译...")
    output_file = use_case.execute(epub_path, config, CLIDisplay)
    print(f"\n🎉 翻译全部完成！\n📄 新文件: {output_file}")


if __name__ == "__main__":
    main()