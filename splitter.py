#!/usr/bin/env uv run
# /// script
# dependencies = [
#     "ebooklib>=0.18",
# ]
# ///

import argparse
import re
import sys
from pathlib import Path

try:
    import ebooklib
    from ebooklib import epub
except ImportError:
    print("Error: ebooklib library is required")
    print("Please run: uv add ebooklib")
    sys.exit(1)


def get_epub_info(epub_path):
    """Get EPUB chapter information"""
    try:
        book = epub.read_epub(epub_path)
        items = list(book.get_items_of_type(ebooklib.ITEM_DOCUMENT))

        # Filter actual HTML content files (exclude TOC, cover, etc.)
        content_items = []
        for item in items:
            name = item.get_name().lower()
            # Exclude common non-chapter files
            if not any(x in name for x in ['toc', 'nav', 'cover', 'copyright', 'acknowledgment']):
                content_items.append(item)

        return content_items
    except Exception as e:
        print(f"Failed to read EPUB: {e}")
        sys.exit(1)


def parse_range(user_input, total):
    """Parse user input range"""
    user_input = user_input.replace(' ', '')

    # Match single number
    if re.match(r'^\d+$', user_input):
        end = int(user_input)
        if end < 1 or end > total:
            return None, f"Range must be between 1-{total}"
        return (0, end - 1), None

    # Match range format: 1-5, etc.
    match = re.match(r'^(\d+)-(\d+)$', user_input)
    if match:
        start = int(match.group(1))
        end = int(match.group(2))
        if start < 1 or end > total or start > end:
            return None, f"Invalid range, must be between 1-{total} and start<=end"
        return (start - 1, end - 1), None

    return None, "Invalid format. Enter a single number or 'start-end' (e.g., 5 or 3-10)"


def extract_epub_range(epub_path, start_idx, end_idx, output_path):
    """Extract specified chapters to a new EPUB"""
    try:
        book = epub.read_epub(epub_path)

        # Create new book
        new_book = epub.EpubBook()
        new_book.set_title(f"{book.get_metadata('DC', 'title')[0][0]} (Excerpt)")
        new_book.set_language(book.get_metadata('DC', 'language')[0][0] if book.get_metadata('DC', 'language') else 'en')

        # Copy author information
        authors = book.get_metadata('DC', 'creator')
        for author in authors:
            new_book.add_author(author[0])

        # Get all items
        all_items = list(book.get_items())
        doc_items = list(book.get_items_of_type(ebooklib.ITEM_DOCUMENT))

        # Find cover image
        cover_image = None
        for item in all_items:
            item_name = item.get_name().lower()
            if 'cover' in item_name and item.get_type() == ebooklib.ITEM_IMAGE:
                cover_image = item
                break

        # Set cover if found
        if cover_image:
            cover_name = Path(cover_image.get_name()).name
            new_book.set_cover(cover_name, cover_image.get_content())

        # Filter actual HTML content files
        content_items = []
        for item in doc_items:
            name = item.get_name().lower()
            if not any(x in name for x in ['toc', 'nav', 'cover', 'copyright', 'acknowledgment']):
                content_items.append(item)

        # Get chapters to keep
        selected_items = content_items[start_idx:end_idx + 1]

        if not selected_items:
            print("Error: No chapters selected")
            return False

        # Collect all required resources (CSS, images, etc.)
        needed_items = set()
        for item in selected_items:
            needed_items.add(item.get_name())
            # Parse HTML to find referenced resources
            if hasattr(item, 'content'):
                content = item.content.decode('utf-8', errors='ignore')
                refs = re.findall(r'(?:src|href)=["\']([^"\']+)["\']', content)
                for ref in refs:
                    needed_items.add(ref)

        # Copy all required items to new book
        spine = []
        cover_name = cover_image.get_name() if cover_image else None
        for item in all_items:
            item_name = item.get_name()
            # Skip cover image (already added via set_cover)
            if cover_name and item_name == cover_name:
                continue
            # Add selected chapters to spine
            if item in selected_items:
                new_book.add_item(item)
                spine.append(item)
            # Copy CSS, images (except cover), fonts
            elif item.get_type() in [ebooklib.ITEM_STYLE, ebooklib.ITEM_IMAGE, ebooklib.ITEM_FONT]:
                new_book.add_item(item)
            # Copy other potentially referenced documents
            elif item_name in needed_items or any(item_name.endswith(ref.lstrip('/')) for ref in needed_items):
                new_book.add_item(item)

        # Create TOC from selected chapters
        toc = []
        for item in selected_items:
            # Get chapter title from item or use filename as fallback
            title = item.title if item.title else Path(item.get_name()).stem
            toc.append(epub.Link(item.get_name(), title, item.get_id()))
        new_book.toc = toc

        # Create navigation files
        new_book.add_item(epub.EpubNcx())
        nav = epub.EpubNav()
        new_book.add_item(nav)
        spine.append(nav)

        new_book.spine = spine

        # Write file
        epub.write_epub(output_path, new_book)
        return True

    except Exception as e:
        print(f"Extraction failed: {e}")
        import traceback
        traceback.print_exc()
        return False


def main():
    parser = argparse.ArgumentParser(
        description='EPUB Chapter Splitter - Supports Tab auto-completion',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  uv run epub-splitter.py book.epub
        """
    )
    parser.add_argument('epub', help='EPUB file path (supports Tab auto-completion)')

    args = parser.parse_args()

    print("=" * 50)
    print("EPUB Chapter Splitter")
    print("=" * 50)

    # Validate EPUB path
    epub_path = Path(args.epub).expanduser().resolve()

    if not epub_path.exists():
        print(f"Error: File not found: {epub_path}")
        sys.exit(1)

    if epub_path.suffix.lower() != '.epub':
        print(f"Error: Not an EPUB file: {epub_path}")
        sys.exit(1)

    # Get chapter information
    print(f"\nReading: {epub_path.name}")
    chapters = get_epub_info(epub_path)
    total = len(chapters)

    if total == 0:
        print("Error: No chapters detected")
        sys.exit(1)

    print(f"✓ Total: {total} chapters")

    # Show first few chapters
    print("\nChapter preview:")
    for i, ch in enumerate(chapters[:5], 1):
        name = ch.get_name().split('/')[-1][:40]
        print(f"  {i}. {name}")
    if total > 5:
        print(f"  ... and {total - 5} more")

    # Get range from user
    print(f"\nEnter the range to extract:")
    print(f"  - Single number: from start to that chapter (e.g., 5)")
    print(f"  - Range format: start-end (e.g., 3-10)")
    print(f"  - Valid range: 1-{total}")

    user_input = input("\nRange: ").strip()

    (start_idx, end_idx), error = parse_range(user_input, total)
    if error:
        print(f"Error: {error}")
        sys.exit(1)

    selected_count = end_idx - start_idx + 1
    print(f"\nExtracting chapters {start_idx + 1} to {end_idx + 1}, total {selected_count} chapters")

    # Generate output filename
    range_str = f"{start_idx + 1}-{end_idx + 1}"
    output_path = epub_path.parent / f"{epub_path.stem}_{range_str}.epub"

    # If file exists, add counter
    counter = 1
    while output_path.exists():
        output_path = epub_path.parent / f"{epub_path.stem}_{range_str}_{counter}.epub"
        counter += 1

    # Execute extraction
    print(f"\nGenerating: {output_path.name}")
    if extract_epub_range(epub_path, start_idx, end_idx, output_path):
        print(f"\n✓ Success: {output_path}")
        print(f"  Contains {selected_count} chapters")
    else:
        print("\n✗ Failed")
        sys.exit(1)


if __name__ == '__main__':
    main()
