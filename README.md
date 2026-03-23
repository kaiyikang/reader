# EPUB Translation Toolkit

A collection of tools for splitting, extracting, and translating EPUB e-books using LLM APIs.

## Prerequisites

- Python 3.10+
- [uv](https://docs.astral.sh/uv/) (Python package manager)
- OpenRouter API key

## Setup

1. **Install uv** (if not already installed):
   ```bash
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```

2. **Set up environment variable**:
   ```bash
   export OPENROUTER_API_KEY="your-api-key-here"
   ```

## Workflow

### Step 1: Split EPUB (Optional)

If you only want to translate specific chapters, use the splitter to create a smaller EPUB:

```bash
uv run splitter.py <input.epub>
```

This will:
- Show available chapters
- Prompt you for a range (e.g., `5` for chapters 1-5, or `3-10` for chapters 3-10)
- Create a new EPUB file: `<input>_<range>.epub`

### Step 2: Translate

Run the translator on your EPUB file:

```bash
uv run translater.py <input.epub>
```

The tool will:
1. **Select target language** - Choose from Chinese, English, Japanese, Korean, French, German, Spanish, or Russian
2. **Select output mode**:
   - Translation only
   - Bilingual (original + translation side by side)
3. **Show cost estimation** - Token count and estimated API cost
4. **Translate chapters** - Progress is cached automatically; resume anytime by re-running
5. **Save output** - Creates `<input>_<language>.epub`

## Features

- **Resume capability**: Translation progress is cached automatically. Re-run the command to resume from where you left off.
- **Glossary management**: Automatically extracts and manages terminology for consistent translations.
- **Batch processing**: Translates multiple paragraphs per API call for efficiency.
- **Cost estimation**: Shows approximate token count and API cost before starting.

## Files

| File | Purpose |
|------|---------|
| `splitter.py` | Extract specific chapter ranges from EPUB files |
| `extract.py` | Extract random text samples for preview/testing |
| `translater.py` | Main translation tool using LLM APIs |

## Tips

- For large books, split into smaller chunks first to manage costs and time
- The translator creates `.cache.json` and `_terms.json` files - don't delete these if you want to resume
- Translation quality depends on the LLM model configured in `translater.py`
