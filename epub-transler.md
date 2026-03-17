# EPUB 翻译器开发文档

## 项目概述

使用 Python + uv run 开发的 EPUB 电子书翻译工具，通过 OpenRouter API 调用 Google Gemini-3-flash-preview 模型进行翻译。

## 运行方式

```bash
export OPENROUTER_API_KEY="your-api-key"
uv run epub-translater.py <input.epub>
```

## 核心功能

1. **语言选择**：支持 8 种目标语言（中/英/日/韩/法/德/西/俄）
2. **输出模式**：
   - 仅译文：用译文替换原文
   - 双语对照：原文后添加译文，用空行分隔
3. **术语表积累**：跨章节维护术语一致性，保存为 JSON
4. **Token 预估**：使用 tiktoken 预估费用
5. **进度展示**：按章节显示翻译进度
6. **断点续传**：API 失败自动保存进度，重新运行从断点继续
7. **实时缓存**：每完成一章自动保存，无需重复翻译

## 开发遇到的问题与解决方案

### 问题 1：API 响应数据格式不一致

**现象**：`string indices must be integers, not 'str'`

**原因**：

- API 返回的 `terms` 字段有时是字符串而非列表
- 段落数可能不匹配（期望 71，实际 70）

**解决方案**：

```python
# 1. 验证响应结构
def call_openrouter_api(prompt, api_key, required_len, max_retries=2):
    # 验证返回的是字典
    if not isinstance(result, dict):
        raise ValueError(f"API 返回的不是字典: {type(result)}")

    # 验证 paragraphs 是列表且长度匹配
    paragraphs = result.get("paragraphs")
    if not isinstance(paragraphs, list):
        raise ValueError(f"paragraphs 不是列表: {type(paragraphs)}")
    if len(paragraphs) != required_len:
        raise ValueError(f"段落数不匹配: 期望 {required_len}, 实际 {len(paragraphs)}")

    # 验证 terms 格式，过滤无效条目
    terms = result.get("terms", [])
    valid_terms = []
    for t in terms:
        if isinstance(t, dict) and "original" in t and "translation" in t:
            valid_terms.append(t)
    result["terms"] = valid_terms
```

### 问题 2：HTML 解析不可靠

**现象**：正则表达式无法正确处理嵌套标签和行内元素

**解决方案**：使用 BeautifulSoup

```python
from bs4 import BeautifulSoup, NavigableString

soup = BeautifulSoup(chapter.get_content(), 'html.parser')

# 只翻译块级元素的叶子节点
block_tags = ['p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'div', 'li',
              'blockquote', 'figcaption', 'td', 'th', 'dd', 'dt']

target_blocks = []
for tag in soup.find_all(block_tags):
    # 如果包含其他块级标签，说明是容器，跳过
    if tag.find(block_tags):
        continue
    if tag.get_text(strip=True):
        target_blocks.append(tag)

# 获取内部 HTML（保留行内标签如 <em>, <a>）
original_texts = [tag.decode_contents() for tag in target_blocks]
```

### 问题 3：EPUB 导航文件被修改导致结构损坏

**现象**：保存 EPUB 时出错，目录结构损坏

**原因**：nav.xhtml 被翻译修改，破坏了 EPUB 的导航结构

**解决方案**：跳过导航文件

```python
def translate_chapter(chapter, target_lang, mode, terms, api_key):
    # 跳过导航文件
    if 'nav' in chapter.get_name().lower():
        return [], False
    # ... 翻译逻辑
```

### 问题 4：TOC 条目缺少 uid 导致写入失败

**现象**：`TypeError: Argument must be bytes or unicode, got 'NoneType'`

**原因**：ebooklib 写入 EPUB 时要求目录条目有 uid 属性

**解决方案**：修复缺失的 uid

```python
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
```

### 问题 5：XHTML 兼容性

**现象**：EPUB 阅读器无法正确显示内容

**原因**：BeautifulSoup 默认输出可能不符合 XHTML 规范（自闭合标签）

**解决方案**：使用 HTML formatter

```python
chapter.set_content(soup.encode('utf-8', formatter='html'))
```

### 问题 6：双语模式下 ID 冲突

**现象**：译文标签复制了原文标签的 id，导致重复

**解决方案**：修改译文标签的 id

```python
if mode == "bilingual":
    new_attrs = dict(block.attrs)
    if 'id' in new_attrs:
        new_attrs['id'] = f"{new_attrs['id']}-tr"

    new_block = soup.new_tag(block.name, **new_attrs)
    new_block.append(trans_soup)
    block.insert_after(new_block)
    block.insert_after(NavigableString("\n\n"))
```

## 缓存机制

### 功能概述

实现断点续传功能，避免因网络中断或 API 错误导致翻译进度丢失。

### 缓存文件

| 文件                           | 格式 | 说明                             |
| ------------------------------ | ---- | -------------------------------- |
| `{原文件名}.cache.json`        | JSON | 临时缓存文件，全部成功后自动删除 |
| `{原文件名}_{lang}_terms.json` | JSON | 术语表，持久保留                 |

### 缓存数据结构

```json
{
  "terms": [{ "original": "...", "translation": "..." }],
  "completed_chapters": ["chapter1.xhtml", "chapter2.xhtml"],
  "chapter_contents": {
    "chapter1.xhtml": "<html>...translated content...</html>"
  },
  "target_lang": "English",
  "mode": "translation_only"
}
```

### 核心实现

```python
def load_cache(epub_path: Path) -> dict:
    """加载缓存数据"""
    cache_path = epub_path.with_suffix('.cache.json')
    if cache_path.exists():
        with open(cache_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}

def save_cache(epub_path: Path, cache_data: dict) -> None:
    """每完成一章实时保存缓存"""
    cache_path = epub_path.with_suffix('.cache.json')
    with open(cache_path, 'w', encoding='utf-8') as f:
        json.dump(cache_data, f, ensure_ascii=False, indent=2)

def save_terms(terms_path: Path, terms: list[dict]) -> None:
    """实时保存术语表"""
    valid_terms = [t for t in terms if "original" in t and "translation" in t]
    with open(terms_path, 'w', encoding='utf-8') as f:
        json.dump(valid_terms, f, ensure_ascii=False, indent=2)
```

### 断点续传流程

```python
def main():
    # 1. 加载缓存
    cache = load_cache(epub_path)
    global_terms = cache.get('terms', [])
    completed_chapters = cache.get('completed_chapters', [])

    # 2. 恢复已翻译章节
    for chapter in chapters:
        if chapter.get_name() in cache.get('chapter_contents', {}):
            chapter.set_content(cache['chapter_contents'][chapter_name])

    # 3. 跳过已完成章节
    for i, chapter in enumerate(chapters, 1):
        if chapter.get_name() in completed_chapters:
            print(f"[{i}] ⏭️ 跳过: {chapter_name} (已缓存)")
            continue

        # 4. 翻译并实时保存
        try:
            new_terms, _ = translate_chapter(chapter, ...)
            save_cache(epub_path, cache_data)
            save_terms(terms_path, global_terms)
        except TranslationError as e:
            print(f"API 失败，缓存已保存，重新运行将从断点继续")
            sys.exit(1)

    # 5. 全部成功，清除缓存
    clear_cache(epub_path)
```

### 使用示例

```bash
# 第一次运行，翻译到第10章时中断
$ uv run epub-translater.py book.epub
[1/20] ⏭️ 跳过: chapter1.xhtml (已缓存)
...
[10/20] 正在翻译: chapter10.xhtml
❌ 失败!
缓存文件: book.cache.json
术语表: book_english_terms.json (共 156 条)
已完成 9/20 章
💡 重新运行脚本将从断点继续翻译

# 重新运行，自动恢复进度
$ uv run epub-translater.py book.epub
💾 发现缓存文件: book.cache.json
   已缓存 9 章翻译内容，将继续未完成的翻译
[1-9/20] ⏭️ 跳过: ... (已缓存)
[10/20] 正在翻译: chapter10.xhtml ✅ 完成
...
🎉 翻译全部完成！
🗑️ 已清除缓存文件
```

## 代码结构

```python
# /// script
# requires-python = ">=3.10"
# dependencies = ["EbookLib", "tiktoken", "requests", "beautifulsoup4"]
# ///

# 缓存管理函数
def get_cache_path(epub_path: Path) -> Path: ...        # 获取缓存文件路径
def get_terms_path(epub_path: Path, target_lang: str) -> Path: ...  # 获取术语表路径
def load_cache(epub_path: Path) -> dict: ...            # 加载缓存数据
def save_cache(epub_path: Path, cache_data: dict) -> None: ...  # 保存缓存
def save_terms(terms_path: Path, terms: list[dict]) -> None: ...  # 保存术语表
def clear_cache(epub_path: Path) -> None: ...           # 清除缓存文件

# 核心配置与工具函数
def load_config() -> str: ...           # 读取 OPENROUTER_API_KEY
def parse_args() -> Path: ...           # 解析命令行参数
def show_epub_info(epub_path) -> tuple: ...  # 显示书籍信息
def get_user_choices() -> tuple: ...    # 获取语言和模式选择
def estimate_tokens_and_confirm(chapters) -> None: ...  # Token 预估

# 翻译核心逻辑
class TranslationError(Exception): ...
def build_prompt(target_lang: str, texts: list[str], terms: list[dict]) -> str: ...
def call_openrouter_api(prompt, api_key, required_len, max_retries=2) -> dict: ...
def translate_chapter(chapter, target_lang, mode, terms, api_key) -> tuple: ...

def fix_toc_uids(items): ...            # 修复目录 uid
def main(): ...                         # 主流程
```

## Prompt 模板

```
You are a professional {target_lang} native translator specialized in eBook content.

## Translation Rules
1. Output only the translated content, without explanations
2. Maintain exactly the same number of paragraphs as the original
3. Keep HTML tags placement while maintaining fluency
4. Keep proper nouns, code, etc. untranslated
5. Maintain original tone, style, and narrative voice
6. Ensure translation resonates with target audience
7. Preserve literary devices and metaphors appropriately

## Glossary (if provided)
- original -> translation

Return result as JSON:
{
  "paragraphs": ["translated 1", "translated 2", ...],
  "terms": [{"original": "...", "translation": "..."}]
}

Text to translate:
[JSON array of texts]
```

## 输出文件命名

| 文件类型 | 命名格式                       | 说明                         |
| -------- | ------------------------------ | ---------------------------- |
| EPUB     | `{原文件名}_{lang}.epub`       | 翻译后的电子书               |
| 术语表   | `{原文件名}_{lang}_terms.json` | 积累的术语表，持久保留       |
| 缓存文件 | `{原文件名}.cache.json`        | 临时缓存，全部成功后自动删除 |

## 注意事项

1. **依赖管理**：使用 PEP 723 内联依赖元数据，uv 自动安装
2. **错误处理**：API 失败重试 2 次，保留上下文便于 debug
3. **空章节处理**：无文本章节自动跳过
4. **术语去重**：合并跨章节术语表时检查 original 字段唯一性
5. **超时设置**：API 调用设置 120 秒超时
6. **缓存安全**：
   - 缓存文件保存章节内容和术语表
   - 翻译过程中断可重新运行从断点继续
   - 全部成功后缓存自动清理，术语表保留
   - 更换目标语言时需手动删除旧缓存文件

## new Refactoring version 1

作为一个专业的架构师，我非常赞同你引入 **Clean Architecture (整洁架构)** 或 **Hexagonal Architecture (六边形架构 / 端口与适配器)** 的想法。

你目前的脚本虽然功能完整，但存在典型的“面条代码”特征：业务逻辑（如何判断哪些HTML标签需要翻译）、基础设施（请求OpenRouter API、读写文件）和表现层（CLI交互）严重耦合在一起。这会导致代码难以编写单元测试，且一旦需要更换大模型API或更换EPUB解析库，改动成本极大。

按照你的要求，我将为你设计一个**单文件中的类组织结构骨架**，剥离具体的实现细节，仅展示架构的层级、类的职责以及依赖注入（Dependency Injection）的关系。

### 重构核心思想：依赖倒置原则 (DIP)

- **Domain (领域层)**：包含核心数据模型和接口定义（Ports）。不依赖任何第三方库（如 `requests`, `ebooklib`, `bs4`）。
- **UseCase (应用层)**：编排业务流程。只依赖领域层定义的接口，不关心具体实现。
- **Infrastructure (基础设施层)**：实现领域层的接口（Adapters）。这里才是真正调用外部API、读写文件、解析HTML的地方。

### 架构骨架设计 (Python 代码结构)

```python
import abc
from dataclasses import dataclass
from pathlib import Path
from typing import List, Dict, Optional, Tuple

# ============================================================================
# 1. DOMAIN LAYER (领域层)
# ============================================================================
# 包含：核心实体 (Entities)、值对象 (Value Objects) 和 抽象接口 (Ports)
# 规则：这一层绝不允许导入 requests, ebooklib, bs4 等第三方具体实现库。

@dataclass
class TranslationConfig:
    """值对象：翻译配置"""
    target_lang: str
    mode: str # 'translation_only' | 'bilingual'

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
    is_completed: bool = False

# --- 端口 (Ports) / 抽象接口 ---

class ITranslationProvider(abc.ABC):
    """防腐层接口：LLM 翻译服务提供者"""
    @abc.abstractmethod
    def translate_blocks(self, texts: List[str], target_lang: str, terms: List[Term]) -> Tuple[List[str], List[Term]]:
        """输入原文列表，返回译文列表和提取出的新术语"""
        pass

class IBookRepository(abc.ABC):
    """接口：电子书仓储 (负责读、写、解析)"""
    @abc.abstractmethod
    def load_book(self, file_path: Path) -> None: pass

    @abc.abstractmethod
    def get_chapter_list(self) -> List[ChapterInfo]: pass

    @abc.abstractmethod
    def extract_translatable_blocks(self, chapter_id: str) -> List[str]: pass

    @abc.abstractmethod
    def apply_translation(self, chapter_id: str, original_blocks: List[str], translated_blocks: List[str], mode: str) -> None: pass

    @abc.abstractmethod
    def save_book(self, output_path: Path) -> None: pass

class ICacheManager(abc.ABC):
    """接口：缓存与持久化管理器"""
    @abc.abstractmethod
    def load_progress(self, book_id: str) -> Dict: pass

    @abc.abstractmethod
    def save_progress(self, book_id: str, completed_chapters: List[str], chapter_contents: Dict) -> None: pass

    @abc.abstractmethod
    def load_terms(self, book_id: str) -> List[Term]: pass

    @abc.abstractmethod
    def save_terms(self, book_id: str, terms: List[Term]) -> None: pass


# ============================================================================
# 2. USE CASE LAYER (应用/用例层)
# ============================================================================
# 包含：具体的业务流程编排。
# 规则：只依赖 Domain 层，通过依赖注入接收 Infra 层的实例。

class TranslateEpubUseCase:
    """用例：执行整本 EPUB 翻译的核心流程"""

    def __init__(self,
                 book_repo: IBookRepository,
                 translator: ITranslationProvider,
                 cache_manager: ICacheManager):
        # 依赖注入 (Dependency Injection)
        self.book_repo = book_repo
        self.translator = translator
        self.cache_manager = cache_manager

    def execute(self, epub_path: Path, config: TranslationConfig) -> Path:
        """
        核心业务编排逻辑 (伪代码描述步骤)：
        1. book_repo.load_book(epub_path)
        2. cache_manager.load_progress() 检查断点
        3. cache_manager.load_terms() 加载历史术语
        4. 遍历 book_repo.get_chapter_list():
            a. 跳过已完成的章节
            b. book_repo.extract_translatable_blocks() 获取待翻译文本
            c. 如果没有文本，跳过
            d. translator.translate_blocks() 调用大模型翻译
            e. 合并新老术语，去重
            f. book_repo.apply_translation() 将译文写回章节
            g. cache_manager.save_progress() & save_terms() 实时保存
        5. book_repo.save_book(output_path)
        6. 返回新书路径
        """
        pass


# ============================================================================
# 3. INFRASTRUCTURE LAYER (基础设施层)
# ============================================================================
# 包含：对 Domain 层接口的具体实现 (Adapters)。
# 规则：这里可以尽情使用 requests, ebooklib, bs4, json 等库。

class OpenRouterTranslatorAdapter(ITranslationProvider):
    """具体实现：调用 OpenRouter (Gemini) 的 API，包含 Prompt 组装、网络重试、JSON 解析提取等脏活累活"""
    def __init__(self, api_key: str):
        self.api_key = api_key

    def translate_blocks(self, texts: List[str], target_lang: str, terms: List[Term]) -> Tuple[List[str], List[Term]]:
        # 你的 extract_json_from_text, call_openrouter_api 等细节逻辑全部封装在这里
        pass

class EbookLibAdapter(IBookRepository):
    """具体实现：使用 ebooklib 和 bs4 解析和修改 EPUB"""
    def __init__(self):
        self.book = None

    def load_book(self, file_path: Path) -> None:
        # epub.read_epub
        pass

    def extract_translatable_blocks(self, chapter_id: str) -> List[str]:
        # BeautifulSoup 解析 HTML，过滤 p, h1, div 等标签的逻辑放在这里
        pass

    def apply_translation(self, chapter_id: str, original_blocks: List[str], translated_blocks: List[str], mode: str) -> None:
        # BeautifulSoup 组装 DOM，实现双语/单语替换的逻辑放在这里
        pass

class LocalFileCacheAdapter(ICacheManager):
    """具体实现：读写本地 .cache.json 和 _terms.json"""
    def __init__(self, base_dir: Path):
        self.base_dir = base_dir

    # JSON 的读写、版本号校验等具体逻辑
    pass

class CLIDisplay:
    """具体实现：表现层，负责与用户交互 (CLI)"""
    @staticmethod
    def get_user_inputs() -> TranslationConfig:
        # argparse 和 input() 的逻辑
        pass

    @staticmethod
    def show_progress(current: int, total: int, chapter_title: str):
        # 终端打印进度条
        pass


# ============================================================================
# 4. MAIN / COMPOSITION ROOT (组装根)
# ============================================================================
# 程序的入口，负责将所有的 Infra 实现实例化，并注入到 UseCase 中。

def main():
    # 1. 初始化配置与表现层 (UI)
    config = CLIDisplay.get_user_inputs()
    epub_path = Path("...") # 从 CLI 参数获取
    api_key = "..." # 从环境变量获取

    # 2. 实例化基础设施 (Adapters)
    book_repo = EbookLibAdapter()
    translator = OpenRouterTranslatorAdapter(api_key)
    cache_manager = LocalFileCacheAdapter(epub_path.parent)

    # 3. 依赖注入到 用例层 (UseCase)
    use_case = TranslateEpubUseCase(
        book_repo=book_repo,
        translator=translator,
        cache_manager=cache_manager
    )

    # 4. 执行业务逻辑
    try:
        output_file = use_case.execute(epub_path, config)
        print(f"🎉 翻译完成: {output_file}")
    except Exception as e:
        print(f"❌ 发生致命错误: {e}")

if __name__ == "__main__":
    main()
```

### 架构师的建议与洞察：

1.  **HTML 处理归属问题（经典难题）**：
    在你的原始脚本中，`translate_chapter` 既做了业务编排，又做了 HTML 解析。在整洁架构中，HTML 是 EPUB 的**存储细节**。因此，我将它划分到了 `EbookLibAdapter` (基础设施)。
    - **Domain 层** 只应该看到“待翻译的字符串列表 (`List[str]`)”。
    - **Adapter** 负责将 HTML DOM 剥离成字符串给 Domain，然后再把 Domain 翻译好的字符串塞回 HTML DOM 里。
2.  **API 健壮性的隔离**：
    你的 `extract_json_from_text` 和重试逻辑是非常好的工程实践。但在架构上，它们属于“为了应对网络 API 不稳定而做的补救措施”，并非翻译的核心业务。将它们封装在 `OpenRouterTranslatorAdapter` 的内部，可以保证 `UseCase` 层的代码干净清爽（它只知道调用了 `translate_blocks` 并拿到了结果）。
3.  **可测试性 (Testability)**：
    按照现在的结构，如果你想测试你的断点续传逻辑 (`TranslateEpubUseCase`)，你完全不需要真的去读取 EPUB 也不需要消耗 token 调 API。你可以传入一个 `MockBookRepository` 和一个 `MockTranslator`，这在以前的“面条代码”中是做不到的。
