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
Usage: uv run translator.py <your_book.epub>
"""

import abc
import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import List, Dict, Tuple, Optional


# ============================================================================
# 1. DOMAIN LAYER
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


# ============================================================================
# Infrastructure Configuration
# ============================================================================

@dataclass(frozen=True)
class TranslatorConfig:
    """Static translator configuration, instantiated and injected at Composition Root"""
    # LLM Configuration
    llm_model: str = "google/gemini-3.1-flash-lite-preview"
    llm_timeout: int = 120
    llm_max_retries: int = 3
    llm_model_is_thinking: bool = False

    # Batch Processing Configuration
    batch_size: int = 30
    max_terms_per_prompt: int = 50

    # Token Estimation Configuration
    token_encoding: str = "cl100k_base"
    cost_per_1m_input_tokens: float = 0.25   # $/M input tokens
    cost_per_1m_output_tokens: float = 1.50  # $/M output tokens

    # Cache Configuration
    cache_version: int = 1

    # HTML Parsing Configuration
    block_tags: tuple = ('p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
                        'div', 'li', 'blockquote', 'figcaption',
                        'td', 'th', 'dd', 'dt')

    # Language Options
    languages: dict = field(default_factory=lambda: {
        "1": "Chinese", "2": "English", "3": "Japanese", "4": "Korean",
        "5": "French", "6": "German", "7": "Spanish", "8": "Russian"
    })

def merge_terms(existing: List[Term], new_terms: List[Term]) -> List[Term]:
    """Domain logic: merge terms and accumulate frequency"""
    term_map = {t.original: t for t in existing}

    for t in new_terms:
        if t.original in term_map:
            # If term exists, it's a high-frequency core term, weight +1
            term_map[t.original].frequency += 1
        else:
            # New term, add directly to dictionary
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
# 2. USE CASE LAYER
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

    def execute(self, epub_path: Path, cli_display, config: TranslatorConfig) -> Optional[Path]:

        chapters = self.book_repo.get_chapter_list()
        chapter_ids = [ch.id for ch in chapters]

        # 1. Load progress, configuration and cached terms
        completed_chapters, chapter_contents, cached_lang, cached_mode, cache_terms = self.cache_manager.load_progress(chapter_ids)

        if cached_lang and cached_mode:
            translation_config = TranslationConfig(target_lang=cached_lang, mode=cached_mode)
            cli_display.show_cache_resume(len(completed_chapters), cached_lang, cached_mode)
        else:
            translation_config = cli_display.get_user_inputs(config)

        # 2. Load terms from file and merge with cached terms (deduplication from dual sources)
        file_terms = self.cache_manager.load_terms_from_file(translation_config.target_lang)
        global_terms = merge_terms(cache_terms, file_terms)
        cli_display.show_terms_loaded(len(global_terms))

        # 3. Restore content of completed chapters
        for ch_id, content in chapter_contents.items():
            self.book_repo.set_chapter_content(ch_id, content)

        # 4. Token estimation and confirmation
        pending_texts = []
        for chapter in chapters:
            if chapter.id not in completed_chapters:
                blocks = self.book_repo.extract_translatable_blocks(chapter.id)
                pending_texts.extend(blocks)

        token_count, cost = self.token_estimator.estimate_cost(pending_texts)
        if not cli_display.confirm_translation(token_count, cost):
            return None  # User cancelled translation

        # 5. Core translation loop
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

                translated_blocks, new_terms = self.translator.translate_blocks(blocks, translation_config.target_lang, global_terms)

                global_terms = merge_terms(global_terms, new_terms)

                self.book_repo.apply_translation(chapter.id, translated_blocks, translation_config.mode)

                completed_chapters.append(chapter.id)
                chapter_contents[chapter.id] = self.book_repo.get_chapter_content(chapter.id)

                self.cache_manager.save_progress(completed_chapters, chapter_contents, translation_config.target_lang, translation_config.mode, global_terms)
                self.cache_manager.save_terms_to_file(translation_config.target_lang, global_terms)

                cli_display.show_chapter_done(time.time() - start_time)

            except Exception as e:
                cli_display.show_error_and_exit(e, len(completed_chapters), total, len(global_terms))

        # 6. Save final file and clear cache
        lang_suffix = translation_config.target_lang.lower().replace(" ", "_")
        output_path = epub_path.with_name(f"{epub_path.stem}_{lang_suffix}.epub")
        self.book_repo.save_book(output_path)
        self.cache_manager.clear_progress()

        return output_path


# ============================================================================
# 3. INFRASTRUCTURE LAYER
# ============================================================================

import re
import requests
import tiktoken
from bs4 import BeautifulSoup, NavigableString
from ebooklib import epub

class TiktokenAdapter(ITokenEstimator):
    def __init__(self, config: TranslatorConfig):
        self.config = config
        self._encoder = tiktoken.get_encoding(config.token_encoding)

    def estimate_cost(self, texts: List[str]) -> Tuple[int, float]:
        if not texts:
            return 0, 0.0
        total_text = "".join(texts)
        token_count = len(self._encoder.encode(total_text))
        # Estimate: input tokens + output tokens (assuming output equals input approximately)
        input_cost = (token_count / 1_000_000) * self.config.cost_per_1m_input_tokens
        output_cost = (token_count / 1_000_000) * self.config.cost_per_1m_output_tokens
        estimated_cost = input_cost + output_cost
        return token_count, estimated_cost

class ILLMClient(abc.ABC):
    @abc.abstractmethod
    def generate_json(self, prompt: str) -> str: pass

class OpenRouterClient(ILLMClient):
    def __init__(self, api_key: str, config: TranslatorConfig):
        self.api_key = api_key
        self.config = config

    def generate_json(self, prompt: str) -> str:
        url = "https://openrouter.ai/api/v1/chat/completions"
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        payload = {
            "model": self.config.llm_model,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "user", "content": prompt}],
            "reasoning": {"enabled": self.config.llm_model_is_thinking}
        }
        resp = requests.post(url, headers=headers, json=payload, timeout=self.config.llm_timeout)
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]

class LLMTranslatorAdapter(ITranslationProvider):
    def __init__(self, llm_client: ILLMClient, config: TranslatorConfig):
        self.llm_client = llm_client
        self.config = config
        
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
        raise ValueError("No complete JSON object found")

    def _is_term_in_text(self, term: str, text: str) -> bool:
        if re.match(r"^[a-zA-Z0-9\s\-_']+$", term):
            return bool(re.search(rf'\b{re.escape(term)}\b', text, re.IGNORECASE))
        return term in text

    def _translate_one_by_one(self, texts: List[str], target_lang: str, terms: List[Term]) -> Tuple[List[str], List[Term]]:
        """Fallback: translate paragraph by paragraph when batch translation fails"""
        all_paragraphs = []
        all_terms = []
        max_terms = self.config.max_terms_per_prompt

        for idx, text in enumerate(texts):
            terms_prompt = ""
            if terms:
                active_terms = [t for t in terms if self._is_term_in_text(t.original, text)]
                active_terms = sorted(active_terms, key=lambda x: x.frequency, reverse=True)[:max_terms]
                if active_terms:
                    compact_glossary = ",".join([f"'{t.original}':'{t.translation}'" for t in active_terms])
                    terms_prompt = f"Glossary:{compact_glossary}\n"

            prompt = f"""Role: Expert {target_lang} literary/web novel translator.
Translate the following text accurately. Output ONLY the translation as plain text without quotes.
{terms_prompt}
Text: {json.dumps(text, ensure_ascii=False)}
"""
            try:
                content = self.llm_client.generate_json(prompt)
                # Try to parse JSON, otherwise use content directly
                try:
                    result = json.loads(content)
                    if isinstance(result, dict) and "translation" in result:
                        translated = result["translation"]
                    elif isinstance(result, dict) and "paragraphs" in result:
                        translated = result["paragraphs"][0] if result["paragraphs"] else text
                    elif isinstance(result, str):
                        translated = result
                    else:
                        translated = content.strip()
                except json.JSONDecodeError:
                    # Not JSON, use raw content
                    translated = content.strip().strip('"').strip("'")

                all_paragraphs.append(translated)

                # Try to extract terms
                try:
                    result = json.loads(content) if content.strip().startswith('{') else {}
                    if isinstance(result, dict) and "terms" in result:
                        new_terms = [Term(**t) for t in result["terms"] if "original" in t and "translation" in t]
                        all_terms.extend(new_terms)
                except:
                    pass

            except Exception as e:
                print(f"\n      ⚠️ Paragraph {idx+1}/{len(texts)} translation failed, keeping original: {str(e)[:50]}")
                all_paragraphs.append(text)  # Keep original on failure

        return all_paragraphs, all_terms

    def translate_blocks(self, texts: List[str], target_lang: str, terms: List[Term]) -> Tuple[List[str], List[Term]]:
        all_translated_paragraphs = []
        all_new_terms = []
        batch_size = self.config.batch_size
        max_terms = self.config.max_terms_per_prompt
        max_retries = self.config.llm_max_retries

        for i in range(0, len(texts), batch_size):
            batch_texts = texts[i:i + batch_size]
            batch_combined_text = " ".join(batch_texts)

            terms_prompt = ""
            if terms:
                active_terms = [t for t in terms if self._is_term_in_text(t.original, batch_combined_text)]
                active_terms = sorted(active_terms, key=lambda x: x.frequency, reverse=True)[:max_terms]

                if active_terms:
                    compact_glossary = ",".join([f"'{t.original}':'{t.translation}'" for t in active_terms])
                    terms_prompt = f"Glossary:{compact_glossary}\n"

            # Build indexed texts to force LLM to maintain paragraph independence
            indexed_texts = [{"i": idx, "t": text} for idx, text in enumerate(batch_texts)]

            prompt = f"""Role: Expert {target_lang} literary/web novel translator.
CRITICAL RULES:
1. EXACT COUNT: Input has {len(batch_texts)} items. Output MUST have exactly {len(batch_texts)} paragraphs in the exact same order.
2. NEVER merge or skip paragraphs, even if they are very short or look similar. Translate each item independently.
3. HTML & TAGS: Intelligently place existing HTML tags in the {target_lang} translation. Keep raw code/formatting tags exactly as they are.
4. NO LEFTOVERS: Translate fully. NEVER leave source language characters (e.g., Chinese) in the output.
5. ACCURACY: Strictly preserve physical descriptions/actions; NEVER invert meanings (e.g., do not translate "thin" as "sturdy").
6. LOCALIZATION: Adapt onomatopoeia, sighs, and idioms naturally to {target_lang} without literal phonetics.
7. TONE: Maintain the original eBook tone (suspense, steampunk, fantasy) and preserve metaphors appropriately.
{terms_prompt}
Output ONLY JSON: {{"paragraphs":["translated_para_1","translated_para_2",...],"terms":[{{"original":"...","translation":"..."}}]}}
Input items (DO NOT modify count or order):{json.dumps(indexed_texts, ensure_ascii=False)}
"""

            # Single batch retry loop
            for attempt in range(max_retries):
                try:
                    content = self.llm_client.generate_json(prompt)
                    try:
                        result = json.loads(content)
                    except json.JSONDecodeError:
                        result = json.loads(self._extract_json_from_text(content))

                    paragraphs = result.get("paragraphs", [])
                    if len(paragraphs) != len(batch_texts):
                        raise ValueError(f"Paragraph count mismatch: expected {len(batch_texts)}, got {len(paragraphs)}")

                    new_terms = [Term(**t) for t in result.get("terms", []) if "original" in t and "translation" in t]

                    # Success: aggregate results
                    all_translated_paragraphs.extend(paragraphs)
                    all_new_terms.extend(new_terms)
                    break  # Success: exit retry loop

                except Exception as e:
                    if attempt < max_retries - 1:
                        # Exponential backoff (2s, 4s, 8s) to mitigate API rate limiting
                        sleep_time = 2 ** (attempt + 1)
                        print(f"\n    ⚠️ Batch {i//batch_size + 1} error ({e}), retrying in {sleep_time}s...")
                        time.sleep(sleep_time)
                    else:
                        # Final attempt: fallback to paragraph-by-paragraph translation
                        print(f"\n    ⚠️ Batch {i//batch_size + 1} batch translation failed, trying paragraph by paragraph...")
                        try:
                            paragraphs, new_terms = self._translate_one_by_one(batch_texts, target_lang, terms)
                            all_translated_paragraphs.extend(paragraphs)
                            all_new_terms.extend(new_terms)
                            break  # Success: exit retry loop
                        except Exception as e2:
                            raise Exception(f"LLM request failed completely: {e}; fallback also failed: {e2}")

        return all_translated_paragraphs, all_new_terms

class EbookLibAdapter(IBookRepository):
    def __init__(self, config: TranslatorConfig):
        self.config = config
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
        title_str = title[0][0] if title and isinstance(title[0], tuple) else (title[0] if title else "Unknown Title")
        author = self.book.get_metadata("DC", "creator")
        author_str = author[0][0] if author and isinstance(author[0], tuple) else (author[0] if author else "Unknown Author")
        return title_str, author_str, len(self._chapters)

    def get_chapter_list(self) -> List[ChapterInfo]:
        return [ChapterInfo(id=name, title=name) for name in self._chapters.keys()]

    def extract_translatable_blocks(self, chapter_id: str) -> List[str]:
        chapter = self._chapters[chapter_id]
        if 'nav' in chapter_id.lower():
            return []
            
        self._current_soup = BeautifulSoup(chapter.get_content(), 'html.parser')
        block_tags = list(self.config.block_tags)
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
        # Clear cache to prevent cross-contamination
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
    def __init__(self, epub_path: Path, config: TranslatorConfig):
        self.config = config
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
            if cache.get('version') != self.config.cache_version:
                return [], {}, None, None, []
                
        completed = cache.get('completed_chapters', [])
        valid_completed = [c for c in completed if c in current_chapter_ids]
        
        # Restore original strict cache validation logic
        invalid_count = len(completed) - len(valid_completed)
        if invalid_count > 0:
            print(f"   ⚠️  Cleared {invalid_count} invalid cached chapters (chapter names don't match current file)")

        contents = {k: v for k, v in cache.get('chapter_contents', {}).items() if k in current_chapter_ids}
        terms = [Term(**t) for t in cache.get('terms', []) if isinstance(t, dict)]
        
        return valid_completed, contents, cache.get('target_lang'), cache.get('mode'), terms

    def save_progress(self, completed_chapters: List[str], chapter_contents: Dict[str, str], target_lang: str, mode: str, terms: List[Term]) -> None:
        data = {
            'version': self.config.cache_version,
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
            print("❌ Error: OPENROUTER_API_KEY environment variable not set")
            sys.exit(1)
        return api_key

    @staticmethod
    def get_epub_path() -> Path:
        parser = argparse.ArgumentParser()
        parser.add_argument("epub_path", help="Path to the EPUB file to translate")
        args = parser.parse_args()
        p = Path(args.epub_path)
        if not p.exists() or p.suffix.lower() != ".epub":
            print("❌ Error: File does not exist or is not an .epub file")
            sys.exit(1)
        return p

    @staticmethod
    def get_user_inputs(config: TranslatorConfig) -> TranslationConfig:
        languages = config.languages
        print("Please select target language:")
        for k, v in languages.items():
            print(f"  {k}. {v}")
        lang_choice = input("👉 Enter number (1-8): ").strip()
        if lang_choice not in languages:
            print("❌ Invalid selection.")
            sys.exit(1)

        print("\nPlease select output mode:")
        print("  1. Translation only")
        print("  2. Original + Translation (bilingual)")
        mode_choice = input("👉 Enter number (1-2): ").strip()
        if mode_choice not in ["1", "2"]:
            print("❌ Invalid selection.")
            sys.exit(1)

        return TranslationConfig(
            target_lang=languages[lang_choice],
            mode="translation_only" if mode_choice == "1" else "bilingual"
        )

    @staticmethod
    def show_book_info(title: str, author: str, chapters: int):
        print(f"\n{'='*40}\n📖 Title: {title}\n✍️  Author: {author}\n📑 Chapters: {chapters}\n{'='*40}\n")

    @staticmethod
    def show_cache_resume(completed_count: int, lang: str, mode: str):
        print(f"\n💾 Compatible cache found, resuming seamlessly!")
        print(f"   {completed_count} chapters completed | Target: {lang} | Mode: {'Translation only' if mode == 'translation_only' else 'Bilingual'}")

    @staticmethod
    def show_terms_loaded(count: int):
        if count > 0:
            print(f"📔 Successfully loaded {count} glossary terms")

    @staticmethod
    def show_chapter_start(current: int, total: int, title: str):
        print(f"[{current}/{total}] Translating: {title} ", end="", flush=True)

    @staticmethod
    def show_chapter_done(elapsed: float):
        print(f"✅ Done ({elapsed:.1f}s)")

    @staticmethod
    def show_chapter_empty():
        print(f"⏭️ Skipped (no text)")

    @staticmethod
    def show_chapter_skip(current: int, total: int, title: str):
        print(f"[{current}/{total}] ⏭️ Skipped: {title} (cached)")

    @staticmethod
    def show_error_and_exit(e: Exception, completed: int, total: int, term_count: int):
        print(f"\n❌ Failed! Details: {e}")
        print(f"Progress safely saved ({completed}/{total}) with {term_count} glossary terms. Re-run to resume seamlessly.")
        sys.exit(1)
    
    @staticmethod
    def confirm_translation(token_count: int, estimated_cost: float) -> bool:
        if token_count == 0:
            return True

        print(f"\n📊 Estimation (pending chapters):")
        print(f"   Text tokens: ~{token_count:,} tokens")
        print(f"   API estimated cost: ~${estimated_cost:.4f} USD")

        confirm = input("\n🚀 Start translation? (y/n): ").strip().lower()
        if confirm not in ['y', 'yes']:
            print("Translation cancelled.")
            return False
        return True


# ============================================================================
# 4. MAIN / COMPOSITION ROOT
# ============================================================================

def main():
    # Composition Root: create configuration and inject into all dependencies
    config = TranslatorConfig()

    api_key = CLIDisplay.check_env()
    epub_path = CLIDisplay.get_epub_path()

    book_repo = EbookLibAdapter(config)
    open_router_client = OpenRouterClient(api_key, config)
    translator = LLMTranslatorAdapter(open_router_client, config)
    cache_manager = LocalFileCacheAdapter(epub_path, config)
    token_estimator = TiktokenAdapter(config)

    use_case = TranslateEpubUseCase(book_repo, translator, cache_manager, token_estimator)

    book_repo.load_book(epub_path)
    title, author, chapter_count = book_repo.get_book_info()
    CLIDisplay.show_book_info(title, author, chapter_count)

    print("🚀 Preparing translation environment...")
    output_file = use_case.execute(epub_path, CLIDisplay, config)

    if output_file:
        print(f"\n🎉 Translation complete!\n📄 New file: {output_file}")

if __name__ == "__main__":
    main()