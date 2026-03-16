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

| 文件 | 格式 | 说明 |
|------|------|------|
| `{原文件名}.cache.json` | JSON | 临时缓存文件，全部成功后自动删除 |
| `{原文件名}_{lang}_terms.json` | JSON | 术语表，持久保留 |

### 缓存数据结构

```json
{
  "terms": [{"original": "...", "translation": "..."}],
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

| 文件类型 | 命名格式 | 说明 |
|----------|----------|------|
| EPUB | `{原文件名}_{lang}.epub` | 翻译后的电子书 |
| 术语表 | `{原文件名}_{lang}_terms.json` | 积累的术语表，持久保留 |
| 缓存文件 | `{原文件名}.cache.json` | 临时缓存，全部成功后自动删除 |

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
