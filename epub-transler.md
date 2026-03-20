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
8. **配置集中管理**：通过 `TranslatorConfig` 类统一管理所有静态配置，支持依赖注入

## 项目架构

### Clean Architecture 分层

```
┌─────────────────────────────────────────────────────────────┐
│ 4. MAIN / COMPOSITION ROOT (组装根)                          │
│    - 创建 TranslatorConfig 配置实例                           │
│    - 实例化所有 Adapter 并注入配置                            │
│    - 组装 UseCase 并执行                                     │
├─────────────────────────────────────────────────────────────┤
│ 3. INFRASTRUCTURE LAYER (基础设施层)                         │
│    - TranslatorConfig: 静态配置中心 (frozen dataclass)        │
│    - EbookLibAdapter: EPUB 读写实现                          │
│    - LLMTranslatorAdapter: 翻译 API 实现                     │
│    - LocalFileCacheAdapter: 缓存持久化实现                   │
│    - TiktokenAdapter: Token 估算实现                         │
│    - CLIDisplay: CLI 交互实现                                │
├─────────────────────────────────────────────────────────────┤
│ 2. USE CASE LAYER (应用/用例层)                              │
│    - TranslateEpubUseCase: 翻译流程编排                       │
├─────────────────────────────────────────────────────────────┤
│ 1. DOMAIN LAYER (领域层)                                     │
│    - TranslationConfig: 运行时翻译配置 (目标语言/模式)         │
│    - Term: 术语实体                                          │
│    - ChapterInfo: 章节信息                                   │
│    - 接口定义: ITranslationProvider, IBookRepository, etc.   │
└─────────────────────────────────────────────────────────────┘
```

### 配置管理设计

所有静态配置集中在 `TranslatorConfig` 类中，通过依赖注入传递给各组件：

```python
@dataclass(frozen=True)
class TranslatorConfig:
    """翻译器静态配置，在 Composition Root 中实例化并注入"""
    # LLM 配置
    llm_model: str = "google/gemini-3.1-flash-lite-preview"
    llm_timeout: int = 120
    llm_max_retries: int = 3

    # 批处理配置
    batch_size: int = 30
    max_terms_per_prompt: int = 50

    # Token 估算配置
    token_encoding: str = "cl100k_base"
    cost_per_1m_tokens: float = 0.15
    token_multiplier: float = 2.0

    # 缓存配置
    cache_version: int = 1

    # HTML 解析配置
    block_tags: tuple = ('p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
                        'div', 'li', 'blockquote', 'figcaption',
                        'td', 'th', 'dd', 'dt')

    # 语言选项
    languages: dict = {...}
```

**设计理由**：

- **符合 Clean Architecture**：配置属于技术细节，应放在基础设施层
- **依赖注入友好**：通过构造函数注入，便于单元测试时 mock
- **不可变性**：`frozen=True` 防止运行时意外修改
- **单一真相源**：所有配置集中管理，避免硬编码散落在各处

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

# ============================================================================
# DOMAIN LAYER (领域层)
# ============================================================================
@dataclass
class TranslationConfig: ...            # 运行时翻译配置 (目标语言/模式)
@dataclass
class Term: ...                         # 术语实体
@dataclass
class ChapterInfo: ...                  # 章节信息

# 抽象接口 (Ports)
class ITranslationProvider(abc.ABC): ...
class IBookRepository(abc.ABC): ...
class ICacheManager(abc.ABC): ...
class ITokenEstimator(abc.ABC): ...

# ============================================================================
# INFRASTRUCTURE LAYER (基础设施层)
# ============================================================================
@dataclass(frozen=True)
class TranslatorConfig: ...             # 静态配置中心

class TiktokenAdapter(ITokenEstimator): ...        # Token 估算实现
class OpenRouterClient(ILLMClient): ...           # OpenRouter API 客户端
class LLMTranslatorAdapter(ITranslationProvider): ...  # 翻译服务实现
class EbookLibAdapter(IBookRepository): ...        # EPUB 读写实现
class LocalFileCacheAdapter(ICacheManager): ...    # 缓存持久化实现
class CLIDisplay: ...                               # CLI 交互实现

# ============================================================================
# USE CASE LAYER (应用层)
# ============================================================================
class TranslateEpubUseCase: ...         # 翻译流程编排

# ============================================================================
# COMPOSITION ROOT (组装根)
# ============================================================================
def main():                             # 主流程：配置创建与依赖注入
    config = TranslatorConfig()         # 创建配置实例
    book_repo = EbookLibAdapter(config)
    translator = LLMTranslatorAdapter(llm_client, config)
    cache_manager = LocalFileCacheAdapter(epub_path, config)
    token_estimator = TiktokenAdapter(config)
    use_case = TranslateEpubUseCase(book_repo, translator, cache_manager, token_estimator)
    use_case.execute(epub_path, CLIDisplay, config)
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
6. **配置管理**：
   - 静态配置集中在 `TranslatorConfig` 类中（基础设施层）
   - 通过依赖注入传递给各 Adapter，避免硬编码
   - 修改配置只需修改 `TranslatorConfig` 默认值
   - 单元测试时可注入 mock 配置
7. **缓存安全**：
   - 缓存文件保存章节内容和术语表
   - 翻译过程中断可重新运行从断点继续
   - 全部成功后缓存自动清理，术语表保留
   - 更换目标语言时需手动删除旧缓存文件

## new Refactoring version 1

作为一个专业的架构师，我非常赞同你引入 **Clean Architecture (整洁架构)** 或 **Hexagonal Architecture (六边形架构 / 端口与适配器)** 的想法。

你目前的脚本虽然功能完整，但存在典型的”面条代码”特征：业务逻辑（如何判断哪些HTML标签需要翻译）、基础设施（请求OpenRouter API、读写文件）和表现层（CLI交互）严重耦合在一起。这会导致代码难以编写单元测试，且一旦需要更换大模型API或更换EPUB解析库，改动成本极大。

按照你的要求，我将为你设计一个**单文件中的类组织结构骨架**，剥离具体的实现细节，仅展示架构的层级、类的职责以及依赖注入（Dependency Injection）的关系。

### 重构核心思想：依赖倒置原则 (DIP)

- **Domain (领域层)**：包含核心数据模型和接口定义（Ports）。不依赖任何第三方库（如 `requests`, `ebooklib`, `bs4`）。
- **UseCase (应用层)**：编排业务流程。只依赖领域层定义的接口，不关心具体实现。
- **Infrastructure (基础设施层)**：实现领域层的接口（Adapters）。这里才是真正调用外部API、读写文件、解析HTML的地方。**新增：配置集中管理 (TranslatorConfig)**。

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

class ITokenEstimator(abc.ABC):
    """防腐层接口：Token 估算器"""
    @abc.abstractmethod
    def estimate_cost(self, texts: List[str]) -> Tuple[int, float]: pass

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
# 包含：对 Domain 层接口的具体实现 (Adapters) 和静态配置。
# 规则：这里可以尽情使用 requests, ebooklib, bs4, json 等库。

@dataclass(frozen=True)
class TranslatorConfig:
    """
    静态配置中心：所有技术细节配置集中管理
    - frozen=True: 不可变，确保配置在运行时不被意外修改
    - 在 Composition Root 中实例化，通过依赖注入传递给各 Adapter
    """
    # LLM 配置
    # llm_model: str = "google/gemini-3.1-flash-lite-preview"
    llm_model: str = "x-ai/grok-4.1-fast"
    llm_timeout: int = 120
    llm_max_retries: int = 3

    # 批处理配置
    batch_size: int = 30
    max_terms_per_prompt: int = 50

    # Token 估算配置
    token_encoding: str = "cl100k_base"
    cost_per_1m_tokens: float = 0.15
    token_multiplier: float = 2.0  # 输入+输出的估算倍数

    # 缓存配置
    cache_version: int = 1

    # HTML 解析配置
    block_tags: tuple = ('p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
                        'div', 'li', 'blockquote', 'figcaption',
                        'td', 'th', 'dd', 'dt')

    # 语言选项
    languages: dict = None

    def __post_init__(self):
        if self.languages is None:
            object.__setattr__(self, 'languages', {
                "1": "Chinese", "2": "English", "3": "Japanese", "4": "Korean",
                "5": "French", "6": "German", "7": "Spanish", "8": "Russian"
            })


class OpenRouterTranslatorAdapter(ITranslationProvider):
    """具体实现：调用 OpenRouter (Gemini) 的 API"""
    def __init__(self, llm_client: 'ILLMClient', config: TranslatorConfig):
        self.llm_client = llm_client
        self.config = config  # 注入配置，使用 config.batch_size, config.llm_max_retries 等

    def translate_blocks(self, texts: List[str], target_lang: str, terms: List[Term]) -> Tuple[List[str], List[Term]]:
        # 使用 self.config.batch_size 进行切片批处理
        # 使用 self.config.max_terms_per_prompt 限制术语数量
        # 使用 self.config.llm_max_retries 控制重试次数
        pass

class TiktokenAdapter(ITokenEstimator):
    """具体实现：使用 tiktoken 进行 Token 估算"""
    def __init__(self, config: TranslatorConfig):
        self.config = config
        self._encoder = tiktoken.get_encoding(config.token_encoding)

    def estimate_cost(self, texts: List[str]) -> Tuple[int, float]:
        # 使用 self.config.cost_per_1m_tokens 和 self.config.token_multiplier
        pass

class EbookLibAdapter(IBookRepository):
    """具体实现：使用 ebooklib 和 bs4 解析和修改 EPUB"""
    def __init__(self, config: TranslatorConfig):
        self.config = config  # 注入配置，使用 config.block_tags
        self.book = None

    def load_book(self, file_path: Path) -> None:
        # epub.read_epub
        pass

    def extract_translatable_blocks(self, chapter_id: str) -> List[str]:
        # 使用 list(self.config.block_tags) 获取要翻译的 HTML 标签
        pass

    def apply_translation(self, chapter_id: str, translated_blocks: List[str], mode: str) -> None:
        # BeautifulSoup 组装 DOM
        pass

class LocalFileCacheAdapter(ICacheManager):
    """具体实现：读写本地 .cache.json 和 _terms.json"""
    def __init__(self, epub_path: Path, config: TranslatorConfig):
        self.config = config  # 注入配置，使用 config.cache_version
        self.cache_path = epub_path.with_suffix('.cache.json')

    def load_progress(self, current_chapter_ids: List[str]) -> Tuple[...]:
        # 使用 self.config.cache_version 进行版本校验
        pass

class CLIDisplay:
    """具体实现：表现层，负责与用户交互 (CLI)"""
    @staticmethod
    def get_user_inputs(config: TranslatorConfig) -> TranslationConfig:
        # 使用 config.languages 获取语言选项
        # argparse 和 input() 的逻辑
        pass

    @staticmethod
    def show_progress(current: int, total: int, chapter_title: str):
        # 终端打印进度条
        pass


# 额外的内部接口（基础设施层内部使用）
class ILLMClient(abc.ABC):
    """内部接口：LLM API 客户端抽象"""
    @abc.abstractmethod
    def generate_json(self, prompt: str) -> str: pass


class OpenRouterClient(ILLMClient):
    """具体实现：OpenRouter API 客户端"""
    def __init__(self, api_key: str, config: TranslatorConfig):
        self.api_key = api_key
        self.config = config  # 使用 config.llm_model, config.llm_timeout


# ============================================================================
# 4. MAIN / COMPOSITION ROOT (组装根)
# ============================================================================
# 程序的入口，负责：
# 1. 创建配置实例 (TranslatorConfig)
# 2. 将所有 Infra 实现实例化并注入配置
# 3. 组装 UseCase 并执行

def main():
    # 1. 创建静态配置实例（整个应用共享）
    config = TranslatorConfig()

    # 2. 获取运行时参数
    api_key = CLIDisplay.check_env()  # 从环境变量读取
    epub_path = CLIDisplay.get_epub_path()  # 从命令行读取

    # 3. 实例化基础设施 (Adapters)，统一注入 config
    book_repo = EbookLibAdapter(config)
    open_router_client = OpenRouterClient(api_key, config)
    translator = LLMTranslatorAdapter(open_router_client, config)
    cache_manager = LocalFileCacheAdapter(epub_path, config)
    token_estimator = TiktokenAdapter(config)

    # 4. 依赖注入到 用例层 (UseCase)
    use_case = TranslateEpubUseCase(
        book_repo=book_repo,
        translator=translator,
        cache_manager=cache_manager,
        token_estimator=token_estimator
    )

    # 5. 执行业务逻辑
    book_repo.load_book(epub_path)
    title, author, chapter_count = book_repo.get_book_info()
    CLIDisplay.show_book_info(title, author, chapter_count)

    output_file = use_case.execute(epub_path, CLIDisplay, config)

    if output_file:
        print(f"🎉 翻译完成: {output_file}")

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
    按照现在的结构，如果你想测试你的断点续传逻辑 (`TranslateEpubUseCase`)，你完全不需要真的去读取 EPUB 也不需要消耗 token 调 API。你可以传入一个 `MockBookRepository` 和一个 `MockTranslator`，这在以前的"面条代码"中是做不到的。

### 配置管理设计洞察

**TranslatorConfig 的设计决策**：

1. **集中管理 vs 分散硬编码**：将所有静态配置（模型名称、超时、重试次数、HTML标签等）集中在 `TranslatorConfig`，避免散落在代码各处
2. **frozen dataclass**：使用 `frozen=True` 确保配置不可变，防止运行时意外修改
3. **依赖注入**：通过构造函数传递给各 Adapter，而非使用全局变量或单例模式
4. **配置分层**：
   - `TranslatorConfig`：静态技术配置（代码中定义，运行时不变）
   - `TranslationConfig`：运行时用户选择（目标语言、输出模式）
5. **可测试性**：单元测试时可轻松注入 mock 配置，无需修改代码

**使用示例**：

```python
# 测试时注入 mock 配置
mock_config = TranslatorConfig(
    llm_model="test-model",
    llm_timeout=5,
    llm_max_retries=1,
    batch_size=2
)
adapter = LLMTranslatorAdapter(mock_client, mock_config)
```
